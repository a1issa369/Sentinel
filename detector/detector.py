"""
Detection engine.

Polls Prometheus every few seconds and evaluates a small set of threshold
rules against the metrics order-service and payment-service expose. This
is deliberately simple - PromQL queries and if-statements, no ML - because
the point of Phase 2 is proving the detection LOOP works end to end:
metric changes -> engine notices -> alert fires -> alert clears when the
system recovers. Phase 3 replaces these hardcoded thresholds with an LLM
reading the same underlying signals and explaining *why*, not just *that*.
"""
import asyncio
import os
import time

import httpx
from fastapi import FastAPI

PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://prometheus:9090")
POLL_INTERVAL_SECONDS = 5

# Each rule maps a PromQL query to a threshold. "comparator" decides
# whether breaching means going above (gt) or below (lt) the threshold -
# service-down uses "lt" because `up` reports 1 when healthy, 0 when not.
RULES = {
    "high_latency": {
        "query": "histogram_quantile(0.95, sum(rate(order_latency_seconds_bucket[1m])) by (le))",
        "threshold": 0.3,  # seconds
        "comparator": "gt",
        "message": "p95 order latency above 300ms",
    },
    "high_error_rate": {
        "query": (
            'sum(rate(orders_total{status="failed"}[1m])) '
            "/ clamp_min(sum(rate(orders_total[1m])), 0.001)"
        ),
        "threshold": 0.2,  # 20%
        "comparator": "gt",
        "message": "order failure rate above 20%",
    },
    "payment_service_down": {
        "query": 'up{job="payment-service"}',
        "threshold": 1,
        "comparator": "lt",
        "message": "payment-service is not responding to Prometheus scrapes",
    },
}

app = FastAPI(title="detector")

# In-memory alert state: rule_name -> {active, message, value, since}
alerts_state: dict[str, dict] = {name: {"active": False, "message": r["message"], "value": None, "since": None}
                                  for name, r in RULES.items()}


async def query_prometheus(client: httpx.AsyncClient, query: str) -> float | None:
    try:
        resp = await client.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": query})
        result = resp.json().get("data", {}).get("result", [])
        if not result:
            return None
        return float(result[0]["value"][1])
    except Exception:
        return None


def breaches(value: float, threshold: float, comparator: str) -> bool:
    if comparator == "gt":
        return value > threshold
    if comparator == "lt":
        return value < threshold
    return False


async def evaluate_rules():
    async with httpx.AsyncClient(timeout=5.0) as client:
        while True:
            for rule_name, rule in RULES.items():
                value = await query_prometheus(client, rule["query"])
                was_active = alerts_state[rule_name]["active"]
                is_active = value is not None and breaches(value, rule["threshold"], rule["comparator"])

                if is_active and not was_active:
                    print(f"[ALERT FIRING]   {rule_name}: {rule['message']} (value={value:.3f})")
                elif was_active and not is_active:
                    print(f"[ALERT RESOLVED] {rule_name}")

                alerts_state[rule_name] = {
                    "active": is_active,
                    "message": rule["message"],
                    "value": round(value, 3) if value is not None else None,
                    "since": alerts_state[rule_name]["since"] if is_active and was_active else (time.time() if is_active else None),
                }

            await asyncio.sleep(POLL_INTERVAL_SECONDS)


@app.on_event("startup")
async def start_loop():
    asyncio.create_task(evaluate_rules())


@app.get("/alerts")
async def get_alerts():
    active = {k: v for k, v in alerts_state.items() if v["active"]}
    return {"active_alerts": active, "all_rules": alerts_state}


@app.get("/health")
async def health():
    return {"status": "ok"}
