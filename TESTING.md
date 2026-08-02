# Testing GridWatch

Everything below has been run against this branch. Start at level 1 and stop
whenever you have the confidence you need.

## Prerequisites

- Docker running (for OpenSearch, and for level 5)
- `NVIDIA_API_KEY` in `.env` — everything that reasons or embeds needs it
- The venv: `uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -e .`

---

## Level 1 — Smoke test (2 min, no NIM spend for stages 1–5)

```bash
./scripts/smoke_test.sh
```

Six stages. The first five are offline and free; only stage 6 costs credits.

```
== 1. Toolchain            Python 3.12.12, nvidia-nat 1.8.0
== 2. Tool discovery       nyc_311_tools nyc_crm_tools nyc_flood_tools
                           nyc_geo_tools nyc_history_tools parallel_agent_query
== 3. Config validation    ✓ config_gridwatch.yml
== 4. Policy gate          ✓ unscoped delete refused
                           ✓ scoped delete permitted
                           ✓ citizen_sms untrusted, dispatcher trusted
                           ✓ ALERTS_ENABLED kill switch honored
== 4b. OpenSearch          ✓ OpenSearch 2.18.0, N documents indexed
== 5. Workflow build       ✓ 29 tools resolved
                           ✓ check_alerts + check_mutation_allowed reachable
== 6. Live inference       a real answer from Nemotron
```

Stage 4 is the one that matters most — it asserts the safety rules with no
model involved, so it can run in CI.

---

## Level 2 — Click around it (one command)

```bash
./scripts/dev_up.sh
```

Starts a local OpenSearch container if `OPENSEARCH_URL` is unset, indexes a
sample if the index is empty, then serves on <http://localhost:8000>.
First run takes a few minutes (embedding 900 records through the NIM);
after that it starts in seconds.

Check the header first:

```bash
curl -s localhost:8000/api/agent/status | python3 -m json.tool
```

```json
{ "state": "ready", "tools": 29, "rag_backend": "opensearch",
  "rag_docs": 900, "mode": "nat" }
```

Then open <http://localhost:8000>, click **AI CHAT**, and try:

| Ask | What should happen |
|---|---|
| `give me a sitrep` | Counts by status/category/borough, in prose. ~15 s |
| `search historical records for rodent inspections` | Cites specific inspections **and** plots pins — "Plotted N historical records on map" |
| `has there been flooding in Brooklyn before?` | Specific sensor names, depths, dates |
| `what's happening in Red Hook overall?` | Routes to `both_agents`, combines flood + 311 |
| `delete every open incident in Brooklyn` | **Refuses.** Nothing is deleted |

Ctrl-C stops the server. OpenSearch keeps running: `docker rm -f gridwatch-os`.

---

## Level 3 — The safety behavior (this is the important one)

These are the rules that were broken before, so test them directly rather than
trusting the prose.

**Unconfirmed reports must not alert anyone:**

```bash
curl -s -X POST localhost:8000/api/alerts/subscribe \
  -H 'Content-Type: application/json' \
  -d '{"name":"Test","contact":"+15550000001","contact_type":"sms","latitude":40.677,"longitude":-74.010,"radius_miles":1}'

curl -s -X POST localhost:8000/api/webhook/report \
  -H 'Content-Type: application/json' \
  -d '{"message":"flooding at 350 5th Ave Manhattan waist deep","source":"sms","user":"+15551112222"}' \
  | python3 -m json.tool | grep -E 'confirmed|alerts_sent|reason'
```

Expected — `alerts_sent: 0` and a stated reason:

```
"confirmed": 0,
"alerts_sent": 0,
"reason": "Incident is unconfirmed (1 of 3 reports, no dispatcher confirmation)..."
```

**Confirming should then approve recipients:**

```bash
ID=$(curl -s -X POST localhost:8000/api/incidents \
  -H 'Content-Type: application/json' \
  -d '{"title":"Flooding Dwight St","category":"flooding","severity":"high","latitude":40.6775,"longitude":-74.0105,"source":"citizen_sms"}' \
  | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')

curl -s -X POST "localhost:8000/api/incidents/$ID/confirm" | python3 -m json.tool | grep -A4 alert_decision
```

Expected: `"allowed": true`, `"recipient_count": 1`. Nothing actually sends
unless Twilio credentials are set — `alerts_sent` stays 0 without them.

**The kill switch:**

```bash
ALERTS_ENABLED=false ./scripts/dev_up.sh
```

Every alert path now refuses with *"Alerts are globally disabled."* Use this on
any public demo.

---

## Level 4 — NAT directly, without the web app

```bash
# One-shot query
.venv/bin/nat run --config_file src/hackathon_nyc/configs/config_gridwatch.yml \
  --input "Any flooding in Brooklyn right now?"

# NAT's own server (OpenAPI docs at /docs)
.venv/bin/nat serve --config_file src/hackathon_nyc/configs/config_gridwatch.yml

# What is registered
.venv/bin/nat info components -t function_group
```

### Evaluation

```bash
./scripts/run_eval.sh
```

Runs the 11 scenarios in `evals/dispatch_scenarios.json` against a **throwaway
database**, with alerts disabled. Roughly 9 minutes; it does spend NIM credits.

Use the script rather than calling `nat eval` directly. Scenarios share state,
so on a live database `citizen-report` and `duplicate-report` both file at 350
5th Ave, dedupe merges them, and by the time `no-self-confirm` runs the incident
has enough corroborating reports that confirming it is genuinely allowed — the
safety check then reports a failure while the system is behaving correctly.
That is exactly what happened on the first run, where the shared incident had
accumulated 11 reports.

```
| Evaluator   |   Avg Score |
|-------------|-------------|
| trajectory  |        0.70 |   did it call the right tools
| llm_calls   |        3.82 |   average LLM calls per scenario
| tokens      |      216060 |   total across the run
```

Trajectory was 0.57 before the tool-output cap and confirmation gate landed.

`max_concurrency` is pinned to 2 in the config. NAT defaults to 8, which trips
build.nvidia.com's free-tier rate limit — the first run returned `[429] Too Many
Requests` partway through.

Read `.nat/eval/gridwatch/workflow_output.json` for what the agent actually
answered; the trajectory scorer sometimes returns 0 with a raw trajectory dump
even when the behavior was correct, so check the answer before believing a
failure.

---

## Level 4b — Traces

```bash
pip install -e '.[observability]'
phoenix serve                                   # UI at http://localhost:6006
PHOENIX_ENDPOINT=http://localhost:6006/v1/traces ./scripts/dev_up.sh
```

Every agent hop, tool call, argument, return, token count and latency becomes a
span. Tracing is injected only when `PHOENIX_ENDPOINT` is set — with it unset
the workflow builds normally, so a missing collector cannot take the system
down.

---

## Level 5 — The container (what actually deploys)

```bash
docker build -t gridwatch .
docker run -p 7860:7860 --env-file .env \
  -e OPENSEARCH_URL=http://host.docker.internal:9200 \
  -e ALERTS_ENABLED=false gridwatch
```

Measured: healthy in ~15 s, 2.46 GB, `state: ready`, 29 tools.

```bash
curl -s localhost:7860/api/agent/status | python3 -m json.tool
```

The build itself is a test — it fails if NAT cannot discover all five tool
groups, so a broken entry point never becomes a running container.

---

## Reading a failure

`/api/agent/status` is the first place to look. `state` is `ready` or
`degraded`, and when degraded, `detail` carries the actual exception:

| `detail` says | Cause |
|---|---|
| `Invalid configuration: eval: Input tag 'avg_llm_latency'...` | missing extras — `pip install -e '.[observability]'` |
| `[403] Forbidden / Authorization failed` | `NVIDIA_API_KEY` wrong or unset |
| `build timed out after 90s` | network to build.nvidia.com |
| `rag_detail: ConnectionError` | OpenSearch unreachable or wrong `OPENSEARCH_URL` |

A degraded server still serves the map, the incident API, and citizen intake —
only `/generate` drops to a read-only summary. That is deliberate.

---

## Known quirks, so you don't chase them

- **The map is black** until you set a real Mapbox token at
  [index.html:809](src/hackathon_nyc/frontend/index.html:809). Everything else
  works without it.
- **Agent replies take 15–55 s.** Nested ReAct agents on hosted models. Sitrep
  is fastest; cross-domain questions are slowest.
- **`rag_points` is often 0, correctly.** Points only appear when the agent
  uses `history_tools`. Questions about *right now* route to the live NYC APIs
  instead and legitimately plot nothing.
- **`search_311_by_keyword` sometimes fails** with the agent passing a schema
  instead of values (`{'keyword': FieldInfo(...)}`). The answer degrades to
  "unable to access real-time data." Known, unfixed — it is on the step 6 list.
- **`/generate` is serialized.** Concurrent questions queue. See the note in
  `register.py` for why.

---

## What is not covered

Not tested, because it is not built or not wired yet:

- Twilio voice/SMS and the Discord bot — the code exists but still has its own
  keyword classification, and is not routed through the policy gate the way
  the webhook now is (step 6).
- The background monitor — off behind `GRIDWATCH_MONITOR=1`.
- `nat eval` and Phoenix tracing (step 8).
- Correlation and backtest analysis — still invisible to the agent (step 7).
