"""
Order service.

Owns the "place an order" workflow:
  1. Insert a pending order row into Postgres
  2. Call payment-service to charge it
  3. Update the order row with the result
  4. Return the final order state

This is deliberately synchronous (order -> payment -> response) rather than
event-driven. That's a real architectural tradeoff worth being able to
explain: synchronous is simpler to reason about and demo, but it means a
slow/broken payment-service directly slows down/breaks order-service too
(the "cascading failure" behavior you WANT to see for this demo).
"""
import os
import time
import uuid
from contextlib import asynccontextmanager

import asyncpg
import httpx
from fastapi import FastAPI
from pydantic import BaseModel
from prometheus_client import Counter, Histogram, make_asgi_app

DATABASE_URL = os.environ["DATABASE_URL"]
PAYMENT_SERVICE_URL = os.environ["PAYMENT_SERVICE_URL"]

# --- Prometheus metrics ---------------------------------------------
# Counter: how many orders, broken down by final status (completed/failed).
# Histogram: end-to-end latency distribution, so we can compute p95/p99
# later instead of just an average that hides outliers.
ORDERS_TOTAL = Counter("orders_total", "Total orders processed", ["status"])
ORDER_LATENCY = Histogram("order_latency_seconds", "End-to-end order latency in seconds")

db_pool: asyncpg.Pool | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id UUID PRIMARY KEY,
                amount NUMERIC NOT NULL,
                status TEXT NOT NULL,
                payment_latency_ms NUMERIC,
                created_at TIMESTAMPTZ DEFAULT now()
            )
            """
        )
    yield
    await db_pool.close()


app = FastAPI(title="order-service", lifespan=lifespan)
app.mount("/metrics", make_asgi_app())


class OrderRequest(BaseModel):
    amount: float


@app.get("/health")
async def health():
    try:
        async with db_pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        db_ok = True
    except Exception:
        db_ok = False
    return {"status": "ok" if db_ok else "degraded", "db_connected": db_ok}


@app.post("/orders")
async def create_order(req: OrderRequest):
    order_id = uuid.uuid4()
    start = time.time()

    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO orders (id, amount, status) VALUES ($1, $2, 'pending')",
            order_id, req.amount,
        )

    # Call payment-service. This is the hop that chaos testing targets.
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{PAYMENT_SERVICE_URL}/payments",
                json={"order_id": str(order_id), "amount": req.amount},
            )
            payment_result = resp.json()
    except httpx.TimeoutException:
        payment_result = {"status": "failed", "reason": "payment_timeout"}
    except httpx.ConnectError:
        payment_result = {"status": "failed", "reason": "payment_unreachable"}

    total_latency_ms = round((time.time() - start) * 1000, 1)
    final_status = "completed" if payment_result.get("status") == "success" else "failed"

    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE orders SET status = $1, payment_latency_ms = $2 WHERE id = $3",
            final_status, total_latency_ms, order_id,
        )

    ORDERS_TOTAL.labels(status=final_status).inc()
    ORDER_LATENCY.observe(total_latency_ms / 1000)

    return {
        "order_id": str(order_id),
        "status": final_status,
        "total_latency_ms": total_latency_ms,
        "payment_result": payment_result,
    }


@app.get("/orders/stats")
async def order_stats():
    """Quick rollup used by the dashboard - counts + error rate over last 60s."""
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT status, count(*) as n, avg(payment_latency_ms) as avg_latency
            FROM orders
            WHERE created_at > now() - interval '60 seconds'
            GROUP BY status
            """
        )
    return {row["status"]: {"count": row["n"], "avg_latency_ms": float(row["avg_latency"] or 0)} for row in rows}
