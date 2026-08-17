"""
Traffic generator.

Continuously creates orders against the gateway so there's always a live
signal to watch. Without this, "inject a fault" has nothing to visibly
affect - the whole demo depends on background traffic existing.
"""
import os
import random
import time
import httpx

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://localhost:8000")
REQUESTS_PER_SECOND = float(os.environ.get("REQUESTS_PER_SECOND", "3"))
SLEEP_INTERVAL = 1.0 / REQUESTS_PER_SECOND


def main():
    print(f"[traffic-generator] sending ~{REQUESTS_PER_SECOND} req/s to {GATEWAY_URL}")
    with httpx.Client(timeout=15.0) as client:
        while True:
            amount = round(random.uniform(5, 500), 2)
            start = time.time()
            try:
                resp = client.post(f"{GATEWAY_URL}/api/orders", json={"amount": amount})
                elapsed_ms = round((time.time() - start) * 1000, 1)
                data = resp.json()
                print(f"[order] status={data.get('status')} amount={amount} took={elapsed_ms}ms")
            except Exception as e:
                elapsed_ms = round((time.time() - start) * 1000, 1)
                print(f"[order] ERROR {type(e).__name__} took={elapsed_ms}ms")
            time.sleep(SLEEP_INTERVAL)


if __name__ == "__main__":
    main()
