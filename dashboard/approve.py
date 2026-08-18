"""
Approve the AI agent's currently pending diagnosis, triggering its
recommended remediation. This is the human-in-the-loop step - the agent
never acts without this being run.

Usage:
    python dashboard/approve.py
"""
import httpx

AGENT_URL = "http://localhost:8004"

if __name__ == "__main__":
    with httpx.Client(timeout=10.0) as client:
        resp = client.post(f"{AGENT_URL}/diagnosis/approve")
        print(resp.json())
