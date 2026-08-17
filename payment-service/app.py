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

app = FastAPI(title="payment-service")

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

    if chaos_state["down"]:
       raise HTTPException(status_code=503, detail="service_down")

    if chaos_state["latency_ms"] > 0:
        await asyncio.sleep(chaos_state["latency_ms"] / 1000)

    if chaos_state["error_rate"] > 0 and random.random() < chaos_state["error_rate"]:
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
