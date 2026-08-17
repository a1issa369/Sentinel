"""
API gateway.

Single entry point for the outside world (traffic generator, you, curl).
Two jobs:
  1. Proxy /api/orders -> order-service
  2. Aggregate /health across all downstream services, so the dashboard
     has one place to check "is everything healthy right now"

A real gateway would also do auth, rate limiting, routing rules, etc.
Keeping it minimal on purpose - the point of this service in the demo is
to be the thing a human/traffic-generator talks to, not to be clever.
"""
import os
import httpx
from fastapi import FastAPI

ORDER_SERVICE_URL = os.environ["ORDER_SERVICE_URL"]
PAYMENT_SERVICE_URL = os.environ["PAYMENT_SERVICE_URL"]

app = FastAPI(title="gateway")


@app.get("/health")
async def health():
    services = {}
    async with httpx.AsyncClient(timeout=3.0) as client:
        for name, url in [("order-service", ORDER_SERVICE_URL), ("payment-service", PAYMENT_SERVICE_URL)]:
            try:
                resp = await client.get(f"{url}/health")
                services[name] = resp.json()
            except Exception as e:
                services[name] = {"status": "unreachable", "error": str(e)}
    overall = "ok" if all(s.get("status") == "ok" for s in services.values()) else "degraded"
    return {"status": overall, "services": services}


@app.post("/api/orders")
async def create_order(body: dict):
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.post(f"{ORDER_SERVICE_URL}/orders", json=body)
            return resp.json()
        except httpx.TimeoutException:
            return {"status": "failed", "reason": "order_service_timeout"}
        except httpx.ConnectError:
            return {"status": "failed", "reason": "order_service_unreachable"}


@app.get("/api/stats")
async def stats():
    async with httpx.AsyncClient(timeout=3.0) as client:
        resp = await client.get(f"{ORDER_SERVICE_URL}/orders/stats")
        return resp.json()
