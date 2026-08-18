"""
AI diagnosis agent.

Watches the detector for active alerts. When something is actively firing,
it gives Claude tool-calling access to the same data a human on-call
engineer would check (Prometheus metrics, payment-service's internal
fault state), and asks for a root-cause hypothesis plus a recommended
remediation.

Phase 2's detector tells you THAT something is wrong. This tells you WHY,
and proposes WHAT to do about it - the same way an SRE reads a dashboard,
forms a hypothesis, and only then decides on an action.

Important design choice: this agent NEVER acts on its own. It writes a
diagnosis and waits. A human calls POST /diagnosis/approve to actually
execute the recommended fix. That boundary - propose, don't act - is the
whole point of Phase 3; skipping it would mean an LLM making irreversible
changes to a running system based on its own (possibly wrong) judgment.
"""
import asyncio
import json
import os
import time

import httpx
from fastapi import FastAPI
from anthropic import Anthropic

PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://prometheus:9090")
DETECTOR_URL = os.environ.get("DETECTOR_URL", "http://detector:8003")
PAYMENT_SERVICE_URL = os.environ.get("PAYMENT_SERVICE_URL", "http://payment-service:8002")
POLL_INTERVAL_SECONDS = 10

client = Anthropic()  # reads ANTHROPIC_API_KEY from the environment
MODEL = "claude-sonnet-5"

app = FastAPI(title="ai-agent")

state = {
    "last_alert_signature": None,
    "diagnosis": None,   # latest diagnosis dict, or None
    "status": "idle",    # idle | diagnosing | awaiting_approval | remediated
}


# --- Tools Claude can call to investigate --------------------------------
TOOLS = [
    {
        "name": "query_prometheus",
        "description": "Run a PromQL instant query against Prometheus and get the current numeric value.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "A valid PromQL query"}},
            "required": ["query"],
        },
    },
    {
        "name": "get_payment_service_chaos_state",
        "description": "Get payment-service's current fault-injection state: latency_ms, error_rate, and down flag.",
        "input_schema": {"type": "object", "properties": {}},
    },
]


async def run_tool(client_http: httpx.AsyncClient, name: str, tool_input: dict) -> str:
    if name == "query_prometheus":
        resp = await client_http.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": tool_input["query"]})
        return json.dumps(resp.json().get("data", {}).get("result", []))
    if name == "get_payment_service_chaos_state":
        resp = await client_http.get(f"{PAYMENT_SERVICE_URL}/health")
        return json.dumps(resp.json())
    return json.dumps({"error": f"unknown tool {name}"})


SYSTEM_PROMPT = """You are an SRE diagnosis agent for a small microservice system called Sentinel.
Services: gateway -> order-service -> payment-service (+ Postgres).
You are given a set of currently-firing alerts from a rule-based detector. Investigate using
the tools available (query Prometheus, check payment-service's fault-injection state) to form
a root-cause hypothesis, then respond with ONLY a JSON object (no markdown, no prose outside
it) in exactly this shape:

{
  "root_cause": "<one sentence, specific>",
  "confidence": <0.0-1.0>,
  "evidence": ["<short evidence point>", ...],
  "recommended_action": "reset_payment_service_chaos" | "no_action_needed",
  "action_reasoning": "<one sentence explaining the recommendation>"
}

"reset_payment_service_chaos" clears payment-service's injected latency/error/down state and
should be recommended when the evidence points to that fault state being the cause. Use
"no_action_needed" if you're not confident enough, or the issue looks unrelated to that state.
"""


async def diagnose(active_alerts: dict) -> dict:
    messages = [{
        "role": "user",
        "content": f"Active alerts right now:\n{json.dumps(active_alerts, indent=2)}\n\nInvestigate and diagnose.",
    }]

    async with httpx.AsyncClient(timeout=10.0) as client_http:
        for _ in range(6):  # cap tool-use turns so a stuck loop can't run forever
            response = client.messages.create(
                model=MODEL,
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=messages,
            )

            if response.stop_reason != "tool_use":
                text = "".join(b.text for b in response.content if b.type == "text")
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return {
                        "root_cause": "Agent returned non-JSON output",
                        "confidence": 0.0,
                        "evidence": [text[:300]],
                        "recommended_action": "no_action_needed",
                        "action_reasoning": "Could not parse diagnosis.",
                    }

            # Claude wants to use a tool - run it and feed the result back.
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    result = await run_tool(client_http, block.name, block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })
            messages.append({"role": "user", "content": tool_results})

    return {
        "root_cause": "Agent exceeded tool-use turn limit without a final answer",
        "confidence": 0.0,
        "evidence": [],
        "recommended_action": "no_action_needed",
        "action_reasoning": "Diagnosis did not converge.",
    }


async def watch_loop():
    async with httpx.AsyncClient(timeout=5.0) as client_http:
        while True:
            try:
                resp = await client_http.get(f"{DETECTOR_URL}/alerts")
                active_alerts = resp.json().get("active_alerts", {})
            except Exception:
                active_alerts = {}

            signature = json.dumps(sorted(active_alerts.keys()))

            # Only run a (paid) diagnosis when the SET of active alerts
            # actually changes - not on every 10s poll while the same
            # incident is still ongoing.
            if active_alerts and signature != state["last_alert_signature"]:
                state["status"] = "diagnosing"
                state["last_alert_signature"] = signature
                try:
                    diagnosis = await diagnose(active_alerts)
                except Exception as e:
                    # A failed API call (bad key, no credits, network issue)
                    # should not kill this loop forever - fall back to a
                    # clear error state and let the NEXT alert-signature
                    # change try again, instead of leaving the agent stuck
                    # on "diagnosing" indefinitely.
                    print(f"[diagnosis ERROR] {type(e).__name__}: {e}")
                    diagnosis = {
                        "root_cause": f"Diagnosis failed: {type(e).__name__}",
                        "confidence": 0.0,
                        "evidence": [str(e)[:300]],
                        "recommended_action": "no_action_needed",
                        "action_reasoning": "The diagnosis call itself failed - see root_cause.",
                    }
                state["diagnosis"] = {**diagnosis, "diagnosed_at": time.time(), "alerts": list(active_alerts.keys())}
                state["status"] = "awaiting_approval" if diagnosis.get("recommended_action") != "no_action_needed" else "idle"
                print(f"[diagnosis] {diagnosis.get('root_cause')} (confidence={diagnosis.get('confidence')})")
            if not active_alerts and state["last_alert_signature"] is not None:
                state["last_alert_signature"] = None
                state["status"] = "idle"

            await asyncio.sleep(POLL_INTERVAL_SECONDS)


@app.on_event("startup")
async def start():
    asyncio.create_task(watch_loop())


@app.get("/diagnosis")
async def get_diagnosis():
    return {"status": state["status"], "diagnosis": state["diagnosis"]}


@app.post("/diagnosis/approve")
async def approve_diagnosis():
    """Human-in-the-loop approval. The agent never calls this itself."""
    if state["status"] != "awaiting_approval" or not state["diagnosis"]:
        return {"ok": False, "reason": "no diagnosis awaiting approval"}

    action = state["diagnosis"].get("recommended_action")
    if action == "reset_payment_service_chaos":
        async with httpx.AsyncClient(timeout=5.0) as client_http:
            await client_http.post(f"{PAYMENT_SERVICE_URL}/chaos/reset")
        state["status"] = "remediated"
        return {"ok": True, "action_taken": action}

    return {"ok": False, "reason": f"unknown action {action}"}


@app.get("/health")
async def health():
    return {"status": "ok"}
