# Sentinel — Phase 2: observability + automatic detection

Phase 1 proved the fault-injection loop works, but *you* had to watch the
terminal table to notice something broke. Phase 2 adds real metrics
(Prometheus), a dashboard you'd actually show someone (Grafana), and a
detection engine that watches those metrics and fires alerts on its own.
Phase 3 (AI diagnosis) builds directly on top of this.

## Architecture

```
traffic-generator --> gateway --> order-service --> payment-service
                                        |                  |
                                    postgres          (both expose /metrics)
                                        |                  |
                                        +------> prometheus <------+
                                                    |
                                            +-------+-------+
                                            |               |
                                        grafana          detector
```

- **gateway** (`:8000`) — single entry point, aggregates `/health` across services
- **order-service** (`:8001`) — creates orders, calls payment-service, writes to Postgres, exposes `/metrics`
- **payment-service** (`:8002`) — processes payments, has `/chaos/*` fault-injection endpoints, exposes `/metrics`
- **traffic-generator** — background script hitting the gateway continuously so there's always live signal
- **prometheus** (`:9090`) — scrapes `/metrics` from order-service and payment-service every 5s
- **grafana** (`:3000`) — pre-provisioned dashboard reading from Prometheus, no manual setup needed
- **detector** (`:8003`) — polls Prometheus every 5s, evaluates 3 threshold rules, exposes `/alerts`
- **dashboard** — runs on your host (not in Docker), polls `/health`, `/api/stats`, and now `/alerts` too

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

Watch the dashboard react: latency climbs, the order failure count climbs, gateway's aggregate health flips to `degraded`. Now, with Phase 2 running, keep watching after you inject — within ~10-15 seconds (2-3 detector poll cycles) a red `ALERT: <rule_name>` row should appear in the dashboard *on its own*, without you deciding anything is wrong. That's the actual Phase 2 milestone: detection moved from your eyes to code.

**Detector rules** (in `detector/detector.py`):
- `high_latency` — p95 order latency above 300ms
- `high_error_rate` — order failure rate above 20%
- `payment_service_down` — Prometheus can't scrape payment-service at all

Try each chaos command and confirm the *matching* rule fires — `latency 800` should trigger `high_latency` but not `high_error_rate`, and vice versa for `errors 0.5`. If a rule fires for the wrong reason, that's useful signal about how coarse your thresholds are — a good thing to be able to discuss.

## Grafana and Prometheus

- **Grafana**: http://localhost:3000 — logs in anonymously as Admin (no password needed, this is fine for local demo purposes only — never do this in production). Open "Sentinel overview" from the dashboard list; it's pre-provisioned, no manual setup. Four panels: order p95 latency, order failure rate, payments by outcome, and service up/down.
- **Prometheus**: http://localhost:9090 — useful for testing PromQL queries directly before you put them in a dashboard panel or detector rule. Try the query bar with `orders_total` or `up` to see raw scraped data.
- **Detector API**: http://localhost:8003/alerts — raw JSON of current alert state, same data the dashboard is polling.

## What each design choice is teaching you

- **Why a gateway at all, with only 3 services?** It's overkill for 3 services functionally, but it's the right habit: one aggregation point for health checks means your future detection engine (Phase 2) has one place to poll instead of three. Be ready to say this out loud in an interview.
- **Why synchronous order → payment calls, not a queue?** Simpler to build and debug in week 1, and — importantly — it means a broken payment-service directly breaks order-service too. That cascading behavior is *the point* of the demo; an async/queued design would hide it. Know this tradeoff, and know that a production system might choose the queue specifically to *avoid* this cascade.
- **Why is chaos state in-memory on payment-service itself**, instead of some separate chaos-injection sidecar? Simplicity for Phase 1. A more "real" chaos engineering setup (e.g. Chaos Mesh on Kubernetes) injects faults at the network/infra layer without the app knowing — that's a good thing to mention you're aware of, even if you don't build it.
- **Why `/orders/stats` computes a 60-second rolling window in SQL** rather than the dashboard doing it — services should own their own aggregation logic; the dashboard should just be a dumb viewer. This same principle is why Phase 2 hands metrics to Prometheus rather than each service inventing its own stats format.

## Troubleshooting

- If `order-service` fails to start, it's almost always because Postgres wasn't ready yet — the `depends_on: condition: service_healthy` in `docker-compose.yml` should prevent this, but if you hit it anyway, `docker compose up` again (Postgres data persists in the `pgdata` volume).
- `docker compose logs -f payment-service` (swap the service name) is your best friend for debugging any single service.
- If ports 8000-8002 or 5432 are already in use on your machine, edit the `ports:` mappings in `docker-compose.yml`.

## What Phase 2's design choices are teaching you

- **Why a Histogram instead of just averaging latency yourself?** Prometheus histograms let you compute *any* percentile after the fact (`histogram_quantile(0.95, ...)`, `0.99`, `0.5`) from the same stored buckets. An average hides outliers — if 95% of requests are fast and 5% are catastrophically slow, the average can still look fine. p95/p99 is what real SRE teams actually alert on, not averages.
- **Why does the detector poll Prometheus instead of querying order-service/payment-service directly?** Decoupling. The detector doesn't need to know how many services exist or where they live — it asks Prometheus one question ("what's the p95 latency") and Prometheus already did the work of collecting and storing that from every scrape target. Add a fourth service later and the detector's rules don't change at all.
- **Why hardcoded thresholds instead of something smarter?** Because Phase 2's job is to prove the *loop* — metric change → detection → alert → resolution — actually works end to end, reliably, before adding any intelligence on top. Phase 3 replaces `value > 0.3` with an LLM that can say *why* latency is high and *what* to do about it. Skipping straight to AI without this baseline would make it much harder to tell whether Phase 3 is actually working, or just guessing.
- **Why `GF_AUTH_ANONYMOUS_ENABLED: true` on Grafana?** Pure demo convenience — skips a login screen for local development. Worth stating out loud in an interview that you know this is a "never do this in production" shortcut, not something you didn't think about.

## Next: Phase 3

With detection working, Phase 3 adds an LLM with tool-calling access to the same Prometheus data the detector already reads. Instead of a flat "high_latency alert fired," it produces something like *"payment-service p95 latency is 850ms, up from a 20ms baseline, correlated with the down flag in payment-service's chaos state — recommend investigating or rolling back."* You approve or reject the suggested remediation, and the system acts.
