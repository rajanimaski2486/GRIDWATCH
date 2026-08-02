# GridWatch

AI dispatch console for New York City, built on the **NVIDIA NeMo Agent Toolkit (NAT)**.

Citizens report problems by phone, SMS, Discord, or the map. Each report becomes an incident a
dispatcher can triage. Residents subscribe to be alerted when something is confirmed nearby. A
multi-agent NAT workflow reasons over live city data — flood sensors, 311 complaints, crashes,
rodent inspections, housing violations — using NVIDIA Nemotron models.

**Many inputs → one incident database → one map → policy-gated alerts.**

Built for Spark Hack NYC 2026.

---

## Architecture

Diagrams for each layer live in [docs/diagrams/](docs/diagrams/) — open the
`.excalidraw` files at excalidraw.com. Start with `00-master`.

```
   Browser ──▶ Dashboard (index.html, Mapbox GL + deck.gl)
                    │
   SMS / Voice ─────┼──▶ FastAPI  ──▶ NAT workflow (react_agent, Nemotron 49B)
   Discord     ─────┤       │            ├─ floodwatch_agent      (nano-9b) 7 flood + 3 geo tools
   Web form    ─────┘       │            ├─ command_center_agent  (nano-9b) 4 x 311 + geo + history
                            │            ├─ both_agents           (parallel, asyncio.gather)
                            │            ├─ risk_analyst_agent    (nano-9b) correlations, backtest
                            │            └─ crm_tools (13) · history_tools (2) · analyst_tools (3)
                            │
                            ├──▶ policy.py ── deterministic gate: every mutation, every alert
                            ├──▶ SQLite / Turso ── incidents, votes, subscriptions
                            └──▶ OpenSearch ── historical NYC records, NIM embeddings
                                     │
                          NVIDIA API (build.nvidia.com)
                          nemotron reasoning + nv-embedqa
```

Nothing runs a model locally. Reasoning and embeddings are called over HTTPS, which is why the
whole system fits on a free CPU tier.

---

## Stack

| Layer | Technology |
|---|---|
| Agent framework | NVIDIA NeMo Agent Toolkit (`nvidia-nat` 1.8) |
| Reasoning | `nvidia/llama-3.3-nemotron-super-49b-v1.5` (orchestrator) |
| Fast path | `nvidia/nvidia-nemotron-nano-9b-v2` (specialists) |
| Embeddings | `nvidia/nv-embedqa-e5-v5` (1024-dim) |
| Retrieval | OpenSearch — custom NAT retriever provider, kNN + optional BM25 |
| Backend | FastAPI, 20 routes + WebSocket |
| Database | SQLite locally, libSQL/Turso when `DATABASE_URL` is set |
| Frontend | Mapbox GL JS + deck.gl, single file, no build step |
| Voice/SMS | Twilio + Pipecat (Whisper STT → Nemotron → Kokoro TTS) |
| Chat bot | discord.py |

---

## Quick start

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e .

cp .env.example .env      # then paste your NVIDIA_API_KEY from build.nvidia.com
./scripts/dev_up.sh
```

`dev_up.sh` starts a local OpenSearch if you have not configured a hosted one, indexes a sample,
and serves the dashboard at <http://localhost:8000>.

Verify with:

```bash
./scripts/smoke_test.sh
```

Six stages; five are offline and free. See [TESTING.md](TESTING.md) for what to try, and
[DEPLOY.md](DEPLOY.md) to put it online.

> Set a Mapbox token at `src/hackathon_nyc/frontend/index.html:809` or the map renders black.
> Everything else works without it.

---

## The NAT workflow

Everything the agent does is defined in
[`configs/config_gridwatch.yml`](src/hackathon_nyc/configs/config_gridwatch.yml) — models, prompts,
tool membership, routing, evaluation. No prompt text or model name lives in Python.

```bash
nat run --config_file src/hackathon_nyc/configs/config_gridwatch.yml --input "Any flooding in Brooklyn?"
nat serve --config_file src/hackathon_nyc/configs/config_gridwatch.yml
nat info components -t function_group
```

`profile_local.yml` swaps the two LLMs to a local Ollama for offline work and changes nothing else.

### Tools — 32 across 6 function groups

| Group | Count | What it does |
|---|---|---|
| `nyc_flood_tools` | 7 | FloodNet sensors, active/worst floods, vulnerability, air quality |
| `nyc_311_tools` | 4 | Complaints by type/borough/zip, stats, proximity, keyword search |
| `nyc_geo_tools` | 3 | Geocode, reverse geocode, nearest sensors |
| `nyc_crm_tools` | 13 | Incident CRUD, alert subscriptions, confirmation, policy checks |
| `nyc_history_tools` | 2 | Historical search over OpenSearch, all indices or one topic |
| `nyc_analyst_tools` | 3 | Cross-dataset correlations, prediction accuracy, risk scoring |

Plus `parallel_agent_query`, a custom NAT function running two specialists concurrently.

Agents use `react_agent` rather than `tool_calling_agent` deliberately — see the comment in the
config for the measured reason.

---

## Safety: the policy gate

[`policy.py`](src/hackathon_nyc/policy.py) is the only code that can authorize an outbound
notification or a destructive mutation. It contains no LLM calls and should not gain any: a system
prompt is a suggestion, this is enforcement. Thresholds live in
[`configs/policy.yml`](src/hackathon_nyc/configs/policy.yml).

- Citizen reports start **unconfirmed**. Only confirmed incidents notify anyone.
- Confirmation needs 3 independent reports or one dispatcher click.
- A report within 0.25 mi of an open incident of the same category bumps `report_count` instead of
  creating a duplicate.
- Destructive actions require an explicit incident id — no bulk deletes.
- The agent may not confirm a citizen report below the threshold; only a dispatcher can.
- `ALERTS_ENABLED=false` is a hard kill switch for all outbound messaging.
- `GRIDWATCH_TOKEN` gates state-changing routes; reads stay open so the public map works.

---

## Features

### Map
3D Mapbox view with building extrusions colored by nearby incident type. Layer buttons load live
NYC Open Data: incidents, floods, crashes, potholes, rodents, housing, restaurants, construction,
962 traffic cameras, and a combined heatmap. The browser calls most of these APIs directly, so the
map still draws when the backend is down.

### AI chat
Natural-language dispatch. Ask for a sitrep, historical flooding near an address, cross-domain
questions about a neighborhood, or ask it to file and triage an incident. Historical results are
plotted on the map. Answers take 15–55 s — nested agents on hosted models.

### Citizen reporting
Phone, SMS, Discord, and the web form all land as incidents with geocoding, category, and urgency.

### Predict & correlate
Cross-references datasets to surface danger zones — potholes near crash sites, chronic 311
hotspots, flood risk from sensor history plus sewer complaints plus weather alerts.

### Proximity alerts
Subscribe an address and radius. Confirmed incidents nearby trigger a notification, subject to the
policy gate above.

---

## Data sources

| Dataset | Endpoint |
|---|---|
| 311 Service Requests | `erm2-nwe9` |
| FloodNet Sensors / Events | `kb2e-tjy3` / `aq7i-eu5q` |
| Motor Vehicle Collisions | `h9gi-nx95` |
| DOT Potholes | `x9wy-ing4` |
| Rodent Inspections | `p937-wjvj` |
| Housing Violations (HPD Class C) | `wvxf-dwi5` |
| Restaurant Inspections | `43nn-pn8j` |
| Construction Permits | `rbx6-tga4` |
| Flood Vulnerability | `mrjc-v9pm` |
| FEMA 2050s Floodplain | `27ya-gqtm` |

Plus NWS weather alerts, NYC DOT traffic cameras, and Nominatim geocoding.

Index the historical corpus with:

```bash
python -m hackathon_nyc.ingest_opensearch --all --download
```

---

## Documentation

| File | Covers |
|---|---|
| [TESTING.md](TESTING.md) | How to verify what works, layered from smoke test to container |
| [DEPLOY.md](DEPLOY.md) | Vercel + Hugging Face Spaces + Turso + Aiven, all free tier |
| [docs/diagrams/](docs/diagrams/) | Excalidraw set — one per layer, plus a walkthrough script |
| [architecture_review.md](architecture_review.md) | The original diagnosis |
| [GRIDWATCH_EXPLAINED.md](GRIDWATCH_EXPLAINED.md) | Walkthrough and NAT feature mapping |
| [NAT_DEPLOYMENT_PLAN.md](NAT_DEPLOYMENT_PLAN.md) | The migration plan |

The last three describe the system as it was found plus the plan that followed; most of it has
landed. Read TESTING.md and DEPLOY.md for how things work today.

---

## Known gaps

Stated plainly so nobody has to discover them:

- **`skills/*/SKILL.md` are prose**, not executable agents. They document intended behavior.
- **The background monitor is off by default**, behind `GRIDWATCH_MONITOR=1`. It now goes through
  the policy gate and persists its cursors, but has not run for a sustained period.
- **Multi-argument tools fail when the agent passes a JSON string.** NAT expands a dict into a
  tool's schema but not a string containing one, so calls like
  `query_nyc_dataset {"dataset_key": ..., "where_clause": ...}` raise TypeError and that one tool
  call degrades. Single-argument tools are unaffected.
- **Trajectory score is 0.70**, not 1.0 — `./scripts/run_eval.sh` shows which scenarios miss.

---

## License

Built for Spark Hack NYC 2026. All rights reserved.
