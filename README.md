# Sentinel — Phase 1: the skeleton

A minimal microservice system you can deliberately break and watch break.
No AI, no Kubernetes, no observability stack yet — that's Phase 2 and 3.
The only goal here: **inject a fault, see it happen, in real time.**

## Architecture

```
traffic-generator --> gateway --> order-service --> payment-service
                                        |
                                    postgres
```

- **gateway** (`:8000`) — single entry point, aggregates `/health` across services
- **order-service** (`:8001`) — creates orders, calls payment-service, writes to Postgres
- **payment-service** (`:8002`) — processes payments, has `/chaos/*` fault-injection endpoints
- **traffic-generator** — background script hitting the gateway continuously so there's always live signal
- **dashboard** — runs on your host (not in Docker), polls `/health` and `/api/stats` and renders a live table

## Running it

```bash
docker compose up --build
```

This starts postgres, payment-service, order-service, gateway, and the traffic generator. Leave it running in one terminal.

In a second terminal, install the two dashboard dependencies and run the live view:

```bash
pip install rich httpx
python dashboard/watch.py
```

You should see a live-updating table: overall status, each service's status, and rolling order counts/latency.

## The demo

With `docker compose up` and `dashboard/watch.py` both running, open a third terminal and inject faults:

```bash
python dashboard/chaos.py latency 800   # payment-service now adds 800ms to every call
python dashboard/chaos.py errors 0.5    # 50% of payments now fail
python dashboard/chaos.py down          # payment-service goes fully unavailable
python dashboard/chaos.py reset         # back to healthy
```

Watch the dashboard react: latency climbs, the order failure count climbs, gateway's aggregate health flips to `degraded`. This loop — inject, observe, reset — is the whole story of Phase 1, and it's also exactly the loop Phase 2 (automated detection) and Phase 3 (AI diagnosis) build on top of.

## What each design choice is teaching you

- **Why a gateway at all, with only 3 services?** It's overkill for 3 services functionally, but it's the right habit: one aggregation point for health checks means your future detection engine (Phase 2) has one place to poll instead of three. Be ready to say this out loud in an interview.
- **Why synchronous order → payment calls, not a queue?** Simpler to build and debug in week 1, and — importantly — it means a broken payment-service directly breaks order-service too. That cascading behavior is *the point* of the demo; an async/queued design would hide it. Know this tradeoff, and know that a production system might choose the queue specifically to *avoid* this cascade.
- **Why is chaos state in-memory on payment-service itself**, instead of some separate chaos-injection sidecar? Simplicity for Phase 1. A more "real" chaos engineering setup (e.g. Chaos Mesh on Kubernetes) injects faults at the network/infra layer without the app knowing — that's a good thing to mention you're aware of, even if you don't build it.
- **Why `/orders/stats` computes a 60-second rolling window in SQL** rather than the dashboard doing it — services should own their own aggregation logic; the dashboard should just be a dumb viewer. This same principle is why Phase 2 hands metrics to Prometheus rather than each service inventing its own stats format.

## Troubleshooting

- If `order-service` fails to start, it's almost always because Postgres wasn't ready yet — the `depends_on: condition: service_healthy` in `docker-compose.yml` should prevent this, but if you hit it anyway, `docker compose up` again (Postgres data persists in the `pgdata` volume).
- `docker compose logs -f payment-service` (swap the service name) is your best friend for debugging any single service.
- If ports 8000-8002 or 5432 are already in use on your machine, edit the `ports:` mappings in `docker-compose.yml`.

## Next: Phase 2

Once this is solid and you can explain every file, Phase 2 adds OpenTelemetry instrumentation, Prometheus + Grafana, and a rule-based detection engine that automatically notices when `dashboard/chaos.py` has been run — instead of you having to look at the table yourself.
