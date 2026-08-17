"""
Payment service.

This is the service you will "break" during demos. It holds an in-memory
chaos state that three endpoints can toggle:

  POST /chaos/latency   {"ms": 800}   -> adds artificial delay to every payment
  POST /chaos/errors    {"rate": 0.5} -> fails that fraction of payments
  POST /chaos/reset                  -> clears all injected faults

Everything else (POST /payments, GET /health) behaves like a normal service
that happens to be reading the chaos state before it acts.
"""
import asyncio
import random
import time
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from prometheus_client import Counter, Histogram, make_asgi_app

app = FastAPI(title="payment-service")
app.mount("/metrics", make_asgi_app())

# Counter labeled by outcome (success/declined/down) - this is what lets
# a detector distinguish "payments are slow" from "payments are failing"
# from "payment-service is unreachable", which need different responses.
PAYMENTS_TOTAL = Counter("payments_total", "Total payments processed", ["status"])
PAYMENT_LATENCY = Histogram("payment_latency_seconds", "Payment processing latency in seconds")

# --- in-memory "is something wrong right now" state -------------------
chaos_state = {
    "latency_ms": 0,
    "error_rate": 0.0,
    "down": False,
}


class LatencyInject(BaseModel):
    ms: int


class ErrorInject(BaseModel):
    rate: float  # 0.0 - 1.0


class PaymentRequest(BaseModel):
    order_id: str
    amount: float


@app.get("/health")
async def health():
    # A real health check reports what state you're in, not just "alive".
    return {
        "status": "down" if chaos_state["down"] else "ok",
        "chaos": chaos_state,
    }


@app.post("/payments")
async def process_payment(req: PaymentRequest):
    start = time.time()
    status_label = "success"
    try:
        if chaos_state["down"]:
            status_label = "down"
            raise HTTPException(status_code=503, detail="service_down")

        if chaos_state["latency_ms"] > 0:
            await asyncio.sleep(chaos_state["latency_ms"] / 1000)

        if chaos_state["error_rate"] > 0 and random.random() < chaos_state["error_rate"]:
            status_label = "declined"
            return {
                "status": "failed",
                "order_id": req.order_id,
                "reason": "payment_declined",
                "latency_ms": round((time.time() - start) * 1000, 1),
            }

        return {
            "status": "success",
            "order_id": req.order_id,
            "amount": req.amount,
            "latency_ms": round((time.time() - start) * 1000, 1),
        }
    finally:
        # finally runs on every exit path - normal return OR the raised
        # HTTPException above - so the "down" case gets counted too.
        PAYMENTS_TOTAL.labels(status=status_label).inc()
        PAYMENT_LATENCY.observe(time.time() - start)


# --- chaos endpoints ----------------------------------------------------
@app.post("/chaos/latency")
async def inject_latency(body: LatencyInject):
    chaos_state["latency_ms"] = body.ms
    return {"ok": True, "chaos": chaos_state}


@app.post("/chaos/errors")
async def inject_errors(body: ErrorInject):
    chaos_state["error_rate"] = max(0.0, min(1.0, body.rate))
    return {"ok": True, "chaos": chaos_state}


@app.post("/chaos/down")
async def inject_down():
    chaos_state["down"] = True
    return {"ok": True, "chaos": chaos_state}


@app.post("/chaos/reset")
async def reset_chaos():
    chaos_state["latency_ms"] = 0
    chaos_state["error_rate"] = 0.0
    chaos_state["down"] = False
    return {"ok": True, "chaos": chaos_state}
