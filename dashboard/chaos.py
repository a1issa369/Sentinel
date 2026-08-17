"""
Chaos CLI - convenience wrapper around the payment-service /chaos endpoints
so you don't have to remember curl syntax during a live demo.

Usage:
    python dashboard/chaos.py latency 800     # inject 800ms latency
    python dashboard/chaos.py errors 0.5      # 50% of payments fail
    python dashboard/chaos.py down             # take payment-service down
    python dashboard/chaos.py reset            # clear all faults
"""
import sys
import httpx

PAYMENT_URL = "http://localhost:8002"


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return

    action = sys.argv[1]
    with httpx.Client(timeout=5.0) as client:
        if action == "latency":
            ms = int(sys.argv[2]) if len(sys.argv) > 2 else 800
            resp = client.post(f"{PAYMENT_URL}/chaos/latency", json={"ms": ms})
        elif action == "errors":
            rate = float(sys.argv[2]) if len(sys.argv) > 2 else 0.5
            resp = client.post(f"{PAYMENT_URL}/chaos/errors", json={"rate": rate})
        elif action == "down":
            resp = client.post(f"{PAYMENT_URL}/chaos/down")
        elif action == "reset":
            resp = client.post(f"{PAYMENT_URL}/chaos/reset")
        else:
            print(f"Unknown action: {action}")
            print(__doc__)
            return

    print(resp.json())


if __name__ == "__main__":
    main()
