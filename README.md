# Sentinel — an AI-native incident response system

A small microservice system that deliberately breaks itself, detects its
own failures, diagnoses the root cause using an LLM with real tool access,
and proposes a fix — which a human has to approve before it acts.

Built in three phases, each one fully working and demoed before the next
was added:

- **Phase 1** — working microservices you can deliberately break, with a fault-injection API and a live terminal dashboard
- **Phase 2** — Prometheus + Grafana + a rule-based detector that notices failures automatically, no human watching a screen
- **Phase 3** — an AI agent with tool-calling access to the same metrics a human on-call engineer would check, producing a root-cause hypothesis and a human-approved remediation


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
- **ai-agent** (`:8004`) — watches the detector; when alerts change, gives Claude tool access to Prometheus and payment-service's state to diagnose root cause and propose a fix
- **dashboard** — runs on your host (not in Docker), polls `/health`, `/api/stats`, `/alerts`, and `/diagnosis`

## Running it

First, copy `.env.example` to `.env` and add your own Anthropic API key (get one at console.anthropic.com):

```bash
cp .env.example .env
# then edit .env and set ANTHROPIC_API_KEY=sk-ant-...
```

`.env` is gitignored — your key never gets committed. Then:

```bash
docker compose up --build
```

This starts all 9 containers: postgres, payment-service, order-service, gateway, traffic-generator, prometheus, grafana, detector, and ai-agent. Leave it running in one terminal.

In a second terminal, install the dashboard dependencies and run the live view:

```bash
pip install rich httpx
python dashboard/watch.py
```

You should see a live-updating table: overall status, each service's status, rolling order counts/latency, active alerts, and the AI agent's diagnosis when one exists.

## The demo

With `docker compose up` and `dashboard/watch.py` both running, open a third terminal and inject faults:

```bash
python dashboard/chaos.py latency 800   # payment-service now adds 800ms to every call
python dashboard/chaos.py errors 0.5    # 50% of payments now fail
python dashboard/chaos.py down          # payment-service goes fully unavailable
python dashboard/chaos.py reset         # back to healthy
```

Watch the dashboard react: latency climbs, the order failure count climbs, gateway's aggregate health flips to `degraded`. Keep watching after you inject — within ~10-15 seconds (2-3 detector poll cycles) a red `ALERT: <rule_name>` row should appear in the dashboard *on its own*, without you deciding anything is wrong. That's detection moved from your eyes to code.

**Detector rules** (in `detector/detector.py`):
- `high_latency` — p95 order latency above 300ms
- `high_error_rate` — order failure rate above 20%
- `payment_service_down` — Prometheus can't scrape payment-service at all

Try each chaos command and confirm the *matching* rule fires — `latency 800` should trigger `high_latency` but not `high_error_rate`, and vice versa for `errors 0.5`. If a rule fires for the wrong reason, that's useful signal about how coarse your thresholds are — a good thing to be able to discuss.

## The full AI diagnosis loop

This is the actual centerpiece of the project. With everything running:

```bash
python dashboard/chaos.py latency 800
# wait ~20-30s (Prometheus scrape + detector poll + ai-agent poll + the Claude API call itself)
curl http://localhost:8004/diagnosis
```

You should get back a real diagnosis — not a canned string, an actual reasoned response from Claude using tool calls against your live Prometheus data:

```json
{
  "status": "awaiting_approval",
  "diagnosis": {
    "root_cause": "Injected 800ms artificial latency in payment-service's fault-injection state is propagating upstream and inflating order-service p95 latency past the 300ms threshold.",
    "confidence": 0.85,
    "evidence": [
      "high_latency alert shows p95 order latency ~0.988s, far above 300ms threshold",
      "payment-service chaos state shows latency_ms=800 actively injected",
      "no error_rate or down flag set, ruling out failure-based causes"
    ],
    "recommended_action": "reset_payment_service_chaos",
    "action_reasoning": "The injected 800ms latency on payment-service directly explains the elevated order-service p95 latency, so clearing it should resolve the alert."
  }
}
```

Then approve it and watch the system actually heal itself:

```bash
python dashboard/approve.py
curl http://localhost:8003/alerts    # high_latency should clear after ~60-90s (see note below)
```

**The agent never acts on its own.** It writes a diagnosis and stops; a human has to call `/diagnosis/approve` before `reset_payment_service_chaos` (or any future action) actually executes. This is the single most important design decision in Phase 3 — an LLM making irreversible changes to a running system based on its own judgment, with no human checkpoint, is a real production risk, not a hypothetical one. Every response you give about this project should mention this boundary explicitly.

## Two things testing revealed that the plan didn't anticipate

These weren't bugs in the traditional sense — they're genuine, valuable discoveries about how monitoring systems behave, found by actually running the system rather than just reading the code:

1. **App-level "down" isn't the same as infra-level "down".** `chaos.py down` makes `/payments` return a 503, but payment-service's `/metrics` endpoint keeps responding fine — so Prometheus's `up` metric stays `1` even though the business function is broken. The failure shows up as `high_error_rate`, not `payment_service_down`. To trigger the latter for real, you have to actually stop the container (`docker compose stop payment-service`) — a true infrastructure-level outage. **The lesson**: "the process is alive" (liveness) and "the service actually works" (business-logic health) are different things, and conflating them is a real gap in a lot of production monitoring setups.

2. **Metric-window recovery lag.** After approving the fix, `high_latency` stayed active for ~60-90 seconds even though the underlying fault was already cleared. The detector's rule uses `rate(...[1m])` — a rolling 1-minute window — so old slow requests linger in the calculation until enough new fast requests displace them. **The lesson**: detection has a built-in lag equal to the metric window size; a fix being instant doesn't mean an alert clears instantly, and that gap is worth explaining rather than being confused by.

## Grafana and Prometheus

- **Grafana**: http://localhost:3000 — logs in anonymously as Admin (no password needed, this is fine for local demo purposes only — never do this in production). Open "Sentinel overview" from the dashboard list; it's pre-provisioned, no manual setup. Four panels: order p95 latency, order failure rate, payments by outcome, and service up/down.
- **Prometheus**: http://localhost:9090 — useful for testing PromQL queries directly before you put them in a dashboard panel or detector rule. Try the query bar with `orders_total` or `up` to see raw scraped data.
- **Detector API**: http://localhost:8003/alerts — raw JSON of current alert state, same data the dashboard is polling.

## What each design choice is teaching you

**Phase 1 — the basics:**
- **Why a gateway at all, with only 3 services?** It's overkill for 3 services functionally, but it's the right habit: one aggregation point for health checks means your detection engine has one place to poll instead of three. Be ready to say this out loud in an interview.
- **Why synchronous order → payment calls, not a queue?** Simpler to build and debug early on, and — importantly — it means a broken payment-service directly breaks order-service too. That cascading behavior is *the point* of the demo; an async/queued design would hide it. Know this tradeoff, and know that a production system might choose the queue specifically to *avoid* this cascade.
- **Why is chaos state in-memory on payment-service itself**, instead of some separate chaos-injection sidecar? Simplicity. A more "real" chaos engineering setup (e.g. Chaos Mesh on Kubernetes) injects faults at the network/infra layer without the app knowing — that's a good thing to mention you're aware of, even if you don't build it.
- **Why `/orders/stats` computes a 60-second rolling window in SQL** rather than the dashboard doing it — services should own their own aggregation logic; the dashboard should just be a dumb viewer. This same principle is why Phase 2 hands metrics to Prometheus rather than each service inventing its own stats format.

**Phase 2 — observability and detection:**
- **Why a Histogram instead of just averaging latency yourself?** Prometheus histograms let you compute *any* percentile after the fact (`histogram_quantile(0.95, ...)`, `0.99`, `0.5`) from the same stored buckets. An average hides outliers — if 95% of requests are fast and 5% are catastrophically slow, the average can still look fine. p95/p99 is what real SRE teams actually alert on, not averages.
- **Why does the detector poll Prometheus instead of querying order-service/payment-service directly?** Decoupling. The detector doesn't need to know how many services exist or where they live — it asks Prometheus one question ("what's the p95 latency") and Prometheus already did the work of collecting and storing that from every scrape target. Add a fourth service later and the detector's rules don't change at all.
- **Why hardcoded thresholds instead of something smarter?** Because Phase 2's job is to prove the *loop* — metric change → detection → alert → resolution — actually works end to end, reliably, before adding any intelligence on top. Skipping straight to AI without this baseline would make it much harder to tell whether Phase 3 is actually working, or just guessing.
- **Why `GF_AUTH_ANONYMOUS_ENABLED: true` on Grafana?** Pure demo convenience — skips a login screen for local development. Worth stating out loud in an interview that you know this is a "never do this in production" shortcut, not something you didn't think about.

**Phase 3 — AI diagnosis:**
- **Why does the agent only diagnose when the alert SET changes, not on every poll?** Cost and signal-to-noise. Calling an LLM API every 10 seconds while an incident is ongoing would burn money for no new information — the diagnosis doesn't change if nothing about the underlying alerts has changed. Only a change in which rules are firing triggers a new (paid) diagnosis call.
- **Why give the agent tools instead of just dumping all the metrics into the prompt?** Tool calling means the agent decides what's relevant to check, the same way a human on-call engineer doesn't stare at every dashboard panel — they form a hypothesis and check specific things. It also means adding a new tool (e.g. querying recent logs) doesn't require changing the prompt's structure, just adding one more function the model can choose to call.
- **Why cap tool-use turns at 6?** Without a limit, a model that gets stuck in a query-result-query loop could run indefinitely, burning API cost with no guaranteed resolution. Capping turns and falling back to a "did not converge" result is a safety valve, not just an optimization.
- **Why wrap the diagnosis call in try/except inside the watch loop?** Found this one the hard way — the first version let an API error (in this case, an empty credit balance) crash the entire background `asyncio` task, permanently stuck on `"diagnosing"` even after the underlying problem was fixed, because the loop that would have retried was already dead. Any external API call in a long-running loop needs to assume it can fail without killing the loop itself.
- **Why is `reset_payment_service_chaos` the only action the agent can recommend?** A deliberately tiny action space. In a real system you'd want a small, explicit allowlist of safe, reversible actions (restart a pod, roll back a deploy, scale replicas) rather than giving an LLM open-ended execution ability — the model proposes from a fixed menu, it doesn't invent arbitrary commands to run.

## Troubleshooting

- If `order-service` fails to start, it's almost always because Postgres wasn't ready yet — the `depends_on: condition: service_healthy` in `docker-compose.yml` should prevent this, but if you hit it anyway, `docker compose up` again (Postgres data persists in the `pgdata` volume).
- `docker compose logs -f <service-name>` is your best friend for debugging any single service.
- If ports 8000-8004, 9090, 3000, or 5432 are already in use on your machine, edit the `ports:` mappings in `docker-compose.yml`.
- `credit_balance_too_low` from the ai-agent means your Anthropic Console balance is empty — add credits at console.anthropic.com/settings/billing. Thanks to the try/except fix above, this no longer permanently breaks the agent; it just reports the error as the diagnosis and will retry on the next alert-signature change.

## Possible extensions (not built, but worth knowing the shape of)

- **Incident replay mode** — record a full incident's Prometheus data and diagnosis, then replay it later to compare the AI's hypothesis against what you know the actual root cause was, building a track record of diagnosis accuracy over time.
- **Kubernetes / OpenShift deployment** — port the docker-compose setup to a local cluster (kind/minikube) and then Red Hat's OpenShift (free via the Developer Sandbox), making remediation actions real K8s API calls (rollout restart, scale deployment) instead of a single reset endpoint.
- **More failure modes** — a slow-memory-leak simulation, or a "bad deploy" flag that changes behavior gradually instead of via an instant on/off toggle, would stress-test whether the detector's fixed thresholds and the agent's diagnosis still work when the signal is noisier and less binary than the current chaos faults.
