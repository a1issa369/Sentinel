"""
Live health/stats dashboard.

Run this in its own terminal (outside Docker, on your host) while the
traffic generator runs. It polls the gateway every second and renders a
live table: this is what makes "inject a fault, watch it break, watch it
recover" actually visible during a demo instead of living in log lines.

Usage:
    pip install rich httpx
    python dashboard/watch.py
"""
import time
import httpx
from rich.console import Console
from rich.table import Table
from rich.live import Live

GATEWAY_URL = "http://localhost:8000"
DETECTOR_URL = "http://localhost:8003"
AGENT_URL = "http://localhost:8004"


def fetch_state():
    with httpx.Client(timeout=3.0) as client:
        try:
            health = client.get(f"{GATEWAY_URL}/health").json()
        except Exception as e:
            health = {"status": "unreachable", "services": {}, "error": str(e)}
        try:
            stats = client.get(f"{GATEWAY_URL}/api/stats").json()
        except Exception:
            stats = {}
        try:
            alerts = client.get(f"{DETECTOR_URL}/alerts").json().get("active_alerts", {})
        except Exception:
            alerts = {}
        try:
            diagnosis_resp = client.get(f"{AGENT_URL}/diagnosis").json()
        except Exception:
            diagnosis_resp = {"status": "unavailable", "diagnosis": None}
    return health, stats, alerts, diagnosis_resp


def render(health, stats, alerts, diagnosis_resp):
    table = Table(title="Sentinel - live system status (last 60s)")
    table.add_column("Service")
    table.add_column("Status")
    table.add_column("Detail")

    overall = health.get("status", "unknown")
    color = {"ok": "green", "degraded": "yellow"}.get(overall, "red")
    table.add_row("gateway (overall)", f"[{color}]{overall}[/{color}]", "")

    for name, info in health.get("services", {}).items():
        status = info.get("status", "unknown")
        color = {"ok": "green"}.get(status, "red")
        detail = ""
        if "chaos" in info:
            c = info["chaos"]
            if c.get("latency_ms") or c.get("error_rate") or c.get("down"):
                detail = f"latency={c.get('latency_ms')}ms error_rate={c.get('error_rate')} down={c.get('down')}"
        table.add_row(name, f"[{color}]{status}[/{color}]", detail)

    for status, info in stats.items():
        color = "green" if status == "completed" else "red"
        table.add_row(
            f"orders: {status}",
            f"[{color}]{info['count']}[/{color}]",
            f"avg latency {info['avg_latency_ms']:.0f}ms",
        )

    for rule_name, info in alerts.items():
        table.add_row(f"[bold red]ALERT: {rule_name}[/bold red]", "[red]firing[/red]", info.get("message", ""))

    diag = diagnosis_resp.get("diagnosis")
    agent_status = diagnosis_resp.get("status", "unknown")
    if diag:
        color = {"awaiting_approval": "yellow", "diagnosing": "cyan", "remediated": "green"}.get(agent_status, "white")
        table.add_row(
            f"[bold {color}]AI diagnosis[/bold {color}]",
            f"[{color}]{agent_status}[/{color}]",
            f"{diag.get('root_cause', '')} (confidence={diag.get('confidence')})",
        )
        if agent_status == "awaiting_approval":
            table.add_row("", "", f"recommends: {diag.get('recommended_action')} -> approve at POST {AGENT_URL}/diagnosis/approve")

    return table


def main():
    console = Console()
    with Live(console=console, refresh_per_second=2) as live:
        while True:
            health, stats, alerts, diagnosis_resp = fetch_state()
            live.update(render(health, stats, alerts, diagnosis_resp))
            time.sleep(1)


if __name__ == "__main__":
    main()
