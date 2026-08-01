# GridWatch: What It Is, and How to Make It a Real NAT Project

A walkthrough of the existing system, then a concrete plan to rebuild it on NVIDIA NeMo Agent
Toolkit (NAT) primitives — config-driven workflows, tools, observability, and evaluation — with
Mac-specific setup and public hosting.

Companions: `architecture_review.md` (internal agent design), `NAT_DEPLOYMENT_PLAN.md` (deploy).

---

# Part 1 — What GridWatch actually is

## In one paragraph

GridWatch is a **311 dispatch console for New York City**. A dispatcher watches a 3D map of the
city covered in live civic data — flood sensors, potholes, rat inspections, crashes, housing
violations. Citizens report problems by **calling a phone number, texting it, messaging a Discord
bot, or clicking the map**. Each report becomes an *incident* — a pin on the map with a category,
severity, and status that the dispatcher works through. Residents can subscribe to be texted when
something happens near their address. A background loop watches city APIs and files incidents on
its own when it sees a flood sensor spike or a burst of complaints.

It is, in short: **many inputs → one incident database → one map → outbound alerts.**

## The four subsystems

### 1. The map (browser)

[`frontend/index.html`](src/hackathon_nyc/frontend/index.html) — 4,379 lines, one file, no build
step. Mapbox GL + deck.gl from CDNs.

It makes **23 fetches directly to third parties** (NYC Open Data, weather.gov, Nominatim) and only
**18 to your backend**. So the map draws itself: collisions, potholes, rodents, housing violations,
restaurant inspections, construction, 962 traffic cameras, the FEMA 2050s floodplain. Your backend
only supplies the parts that are *yours* — incidents, chat, votes, subscriptions, risk lookup.

### 2. The incident database

[`db.py`](src/hackathon_nyc/db.py) — SQLite, four tables:

| Table | Holds |
|---|---|
| `incidents` | the pins: title, category, severity, lat/lon, status, `confirmed`, `report_count` |
| `incident_updates` | audit trail of status changes |
| `incident_votes` | citizens up/down-voting a report's accuracy |
| `alert_subscriptions` | name, phone, lat/lon, radius, category filter |

Two behaviors in here matter a lot, because they are the system's actual safety policy:

- **Deduplication** ([`db.py:155`](src/hackathon_nyc/db.py:155)) — a new report within 0.25 miles of
  an open incident of the same category doesn't create a duplicate. It increments `report_count`.
- **Confirmation** — incidents start `confirmed=0`. Three independent citizen reports, or one
  dispatcher click, flips it to 1. **Only confirmed incidents are supposed to text anybody.**

### 3. The API server

[`server.py`](src/hackathon_nyc/server.py) — FastAPI, 20 routes plus a WebSocket. Incident CRUD,
subscriptions, the `/generate` chat endpoint, `/api/webhook/report` (the universal intake), a
risk-scoring endpoint, and `/ws` where Twilio streams live call audio into a Pipecat voice pipeline
(Whisper → Nemotron → Kokoro TTS).

### 4. The NAT layer

[`register.py`](src/hackathon_nyc/register.py) — **this part is genuinely well built.** Four
function groups totaling exactly 26 tools:

| Group | Tools | Count |
|---|---|---|
| `nyc_flood_tools` | active floods, sensors, worst floods, sensor history, vulnerability, air quality, raw dataset query | 7 |
| `nyc_311_tools` | complaints, stats, by-location, keyword search | 4 |
| `nyc_geo_tools` | geocode, reverse geocode, nearest sensors | 3 |
| `nyc_crm_tools` | create, list, update, resolve, delete, get, stats, subscribe, list subs, check alerts, confirm, unsubscribe | 12 |

Plus `parallel_agent_query` — a custom NAT function that runs two sub-agents concurrently via
`asyncio.gather`. That's a real NAT extension, not boilerplate.

Four YAML configs wire these into agents: `config_unified.yml` (one ReAct agent, all 26 tools),
`config_orchestrator.yml` (master ReAct → FloodWatch specialist / 311 specialist / parallel), plus
two standalone specialists and a `nat serve` config.

## Walkthrough: a citizen texts "flooding at 350 5th Ave, water is waist deep"

1. Twilio POSTs to a route in [`twilio_voice.py`](src/hackathon_nyc/twilio_voice.py).
2. The text is cleaned by **regex**: periods stripped, `"bleeding"` → `"flooding"` (a Whisper
   mishearing), `" in "` → `", "`, `"$350.00"` → `"350"`, and — my favorite — if Whisper merged
   `"350 5th"` into `"3505th"`, the code generates every possible re-split (`"350 5th"`,
   `"35 05th"`, `"3 505th"`) and tries geocoding each ([`server.py:445`](src/hackathon_nyc/server.py:445)).
3. Category is chosen by a **first-match-wins keyword loop** ([`server.py:503`](src/hackathon_nyc/server.py:503)):
   `"flood"` → flooding, `"rat"` → rodent, ~28 entries.
4. Severity comes from **keyword counting** ([`compute_urgency`, server.py:275](src/hackathon_nyc/server.py:275)):
   `"waist deep"` is in the `high` list → 0.8 → severity `high`.
5. `db.create_incident()` — dedupe check runs; new pin appears on the map.
6. **SMS blasts out to every nearby subscriber** ([`server.py:~550`](src/hackathon_nyc/server.py:550)).

## Walkthrough: a dispatcher types "show me flood history near Brooklyn"

1. `POST /generate` ([`server.py:791`](src/hackathon_nyc/server.py:791)).
2. It checks whether the NAT workflow is loaded. **It isn't** — startup is commented out at
   [`server.py:96`](src/hackathon_nyc/server.py:96) with the note *"causes Ollama to hang."*
3. So it falls through to the hand-built path: a **trigger-word list** decides whether to do RAG, a
   **topic map** picks the Chroma collection, a **regex** pulls the place name out of the sentence,
   Nominatim geocodes it, results are filtered to 5 miles by haversine.
4. The prompt is assembled with an instruction block that reads, in part: *start your reply with
   "Yes", never say no records found* ([`server.py:925`](src/hackathon_nyc/server.py:925)).
5. Direct HTTP POST to Ollama on port **11435** (every YAML config says **11434**).
6. The reply is scanned for a ` ```action ` fenced block containing JSON. If found, the server
   parses it and executes `create` / `update` / `delete` / `confirm` **directly against the
   database**.

---

# Part 2 — Where the "agency" actually lives

This is the heart of it, and it's why the project doesn't feel agentic yet.

**The 26 NAT tools are correct and well-written. The agent that would call them is switched off.**
In its place, every decision an agent should make is a Python `if` statement:

| Decision | Should be | Actually is | Where |
|---|---|---|---|
| What kind of problem is this? | reasoning over text | 28-entry keyword loop, first match wins | [server.py:503](src/hackathon_nyc/server.py:503) |
| How urgent? | reasoning | keyword lists + `+0.1` if ≥3 hits | [server.py:275](src/hackathon_nyc/server.py:275) |
| Where is it? | tool call | ~70 lines of regex repairing STT errors | [server.py:427](src/hackathon_nyc/server.py:427) |
| Do I need history? | tool call | 30-word trigger list | [server.py:875](src/hackathon_nyc/server.py:875) |
| Which collection? | retriever | hardcoded topic map | [server.py:886](src/hackathon_nyc/server.py:886) |
| What do I do next? | tool call | parse a markdown fence, execute the JSON | [server.py:960](src/hackathon_nyc/server.py:960) |

Two consequences worth seeing clearly:

**The keyword loop is order-dependent and wrong in the ordinary case.** `"gas leak"` maps to
`sewer`. But the loop hits `"leak"` → `water` only if `"gas leak"` didn't match first — and a report
saying *"I smell gas"* with no leak lands on `other`. A reasoning model gets this right with no
table to maintain.

**Your safety policy is contradicted by your own code.** The NAT tool `check_alerts`
([`register.py:335`](src/hackathon_nyc/register.py:335)) refuses to alert unless
`incident["confirmed"]` is true — correct. But `/api/webhook/report` texts every nearby subscriber
immediately on creation, with **no confirmation check at all**. Every phone, SMS, and Discord report
bypasses the rule. The disabled agent was the thing enforcing it.

**And the README describes the system you meant to build.** "26-Tool NeMo ReAct Agent" — the 26
tools are real, the agent is commented out. "OpenClaw Skills… all `✓ ready`" — `skills/*/SKILL.md`
are well-written prose that nothing loads at runtime. Fixing the runtime makes the README true.

---

# Part 3 — Making it purely agentic with NAT

The rule: **NAT owns orchestration; Python owns facts and enforcement.** The LLM never decides
whether an alert is allowed — it decides what to *look at* and what to *propose*.

## 3.1 NAT features, mapped to your problems

| NAT feature | Use it for | Replaces |
|---|---|---|
| **Config-driven workflows** | models, prompts, tool membership, thresholds in YAML | prompts + `nemotron-mini` hardcoded in 3 files |
| **`_type: nim` LLMs** | NVIDIA-hosted Nemotron | localhost Ollama, unrunnable in the cloud |
| **`reasoning_agent`** | wraps another agent with a planning pass from a reasoning model | nothing — this is the Nemotron sweet spot |
| **Function groups** | you already do this well; add 3 more | scattered endpoint logic |
| **Sub-agents as tools** | specialists the orchestrator routes to | `config_orchestrator.yml`, currently unused |
| **Retrievers** | Chroma as a typed config object with named collections | `collections[0]` + trigger words |
| **Human-in-the-loop** | dispatcher approves destructive/mass-alert actions | nothing |
| **Memory** | conversation + incident context across restarts | module-global `CHAT_HISTORY` |
| **Telemetry (`general.telemetry`)** | traces of every tool call | `logger.info` |
| **`nat eval` + evaluators** | regression tests on agent behavior | nothing |
| **`nat mcp`** | expose your 26 tools over MCP | `skills/` prose |
| **`nat serve`** | production FastAPI front end | hand-rolled `/generate` |

## 3.2 Tools to add

Your existing 26 stay as-is. Add three groups:

**`nyc_intake_tools`** — the single normalization path all five channels call:
```
normalize_report(text, source)      → {clean_text, location_candidates, category, severity, confidence, missing_fields}
extract_location(text)              → candidates, STT-repair aware
classify_and_score(text, evidence)  → category + urgency, model-driven
```
This deletes the keyword tables and the regex block.

**`nyc_policy_tools`** — deterministic, the enforcement boundary. **No LLM inside.**
```
evaluate_mutation(action, args, context) → {allowed, reason, requires_human}
evaluate_alert(incident_id)              → {allowed, recipients, reason}   # THE confirmed check
record_event(kind, payload)              → append to agent_events
```
Every mutating CRM tool calls `evaluate_mutation` first. `evaluate_alert` becomes the *only* code
path that can send an SMS — that alone fixes the policy contradiction above.

**`nyc_analyst_tools`** — wrap the two offline scripts you already wrote and never exposed:
```
get_correlation_findings(area, category)      → correlation_analysis.py
predict_hotspots(time_window, category)       → backtest_predictions.py
explain_risk_score(address)                   → the /api/risk logic
compare_to_baseline(area, category)
```
`correlation_analysis.py` (455 lines) and `backtest_predictions.py` (398 lines) are the most
technically substantial work in the repo and the agent currently cannot see either.

## 3.3 The workflow config

`src/hackathon_nyc/configs/config_gridwatch.yml`:

```yaml
general:
  front_end:
    _type: fastapi
    cors:
      allow_origins: ["https://gridwatch.vercel.app"]
  telemetry:
    tracing:
      phoenix:
        _type: phoenix
        endpoint: ${PHOENIX_ENDPOINT}
        project: gridwatch

llms:
  reasoner:                                        # planning, triage, orchestration
    _type: nim
    model_name: nvidia/llama-3.3-nemotron-super-49b-v1.5
    temperature: 0.2
    max_tokens: 4096
  fast:                                            # tool-calling specialists, SMS replies
    _type: nim
    model_name: nvidia/nvidia-nemotron-nano-9b-v2
    temperature: 0.0
    max_tokens: 2048

embedders:
  nv_embed:
    _type: nim
    model_name: nvidia/nv-embedqa-e5-v5

function_groups:
  flood_tools:     { _type: nyc_flood_tools }
  complaint_tools: { _type: nyc_311_tools }
  geo_tools:       { _type: nyc_geo_tools }
  crm_tools:       { _type: nyc_crm_tools }
  intake_tools:    { _type: nyc_intake_tools }     # NEW
  policy_tools:    { _type: nyc_policy_tools }     # NEW
  analyst_tools:   { _type: nyc_analyst_tools }    # NEW

functions:
  current_datetime: { _type: current_datetime }

  # NOTE (verified against nvidia-nat 1.8.0): there is NO Chroma retriever
  # provider — only `milvus_retriever` and `nemo_retriever`. The six ChromaDB
  # collections must stay behind a custom `nyc_history_tools` function group
  # wrapping tools/historical_lookup.py, not a `retrievers:` block.

  floodwatch_agent:
    _type: tool_calling_agent
    llm_name: fast
    tool_names: [flood_tools, geo_tools]
    description: FloodNet sensors, water depth, flood risk and vulnerability.

  command_center_agent:
    _type: tool_calling_agent
    llm_name: fast
    tool_names: [complaint_tools, geo_tools, historical_rag]
    description: 311 service requests, complaint trends, resident impact.

  risk_analyst_agent:
    _type: tool_calling_agent
    llm_name: fast
    tool_names: [analyst_tools, historical_rag]
    description: Correlations, hotspot prediction, neighborhood risk scoring.

  both_agents:
    _type: parallel_agent_query      # your existing custom function
    agent_1: floodwatch_agent
    agent_2: command_center_agent

  dispatch_agent:
    _type: tool_calling_agent
    llm_name: fast
    tool_names: [floodwatch_agent, command_center_agent, risk_analyst_agent,
                 both_agents, intake_tools, crm_tools, policy_tools, current_datetime]
    description: Full GridWatch dispatch capability.

workflow:
  _type: reasoning_agent             # Nemotron plans, then dispatch_agent executes
  llm_name: reasoner
  augmented_fn: dispatch_agent
  verbose: true

eval:
  general:
    dataset: { _type: json, file_path: evals/dispatch_scenarios.json }
  evaluators:
    trajectory:  { _type: trajectory, llm_name: reasoner }
    correctness: { _type: ragas, metric: AnswerAccuracy, llm_name: reasoner }
```

`profile_local.yml` overrides only the `llms:` block back to Ollama for offline work. Nothing else
changes.

**Verified against nvidia-nat 1.8.0** — the real file now lives at
[`configs/config_gridwatch.yml`](src/hackathon_nyc/configs/config_gridwatch.yml) and passes
`nat validate`. Three corrections found while checking:

| Assumed | Actual in 1.8.0 |
|---|---|
| a Chroma retriever exists | only `milvus_retriever`, `nemo_retriever` — Chroma stays a custom tool group |
| `phoenix` tracing available by default | needs the `[phoenix]` extra; `otelcollector` works without it |
| `ragas` evaluator available by default | needs the `[ragas]` extra; `trajectory` ships in core |

Re-check on any NAT upgrade with `./scripts/smoke_test.sh`.

## 3.4 The rules that make "config-driven" mean something

1. **No prompt text in Python.** Move every triple-quoted prompt out of `server.py`.
2. **No model name in Python.** `nemotron-mini` appears in `server.py`, `voice_agent.py`,
   `twilio_voice.py`.
3. **Thresholds in YAML:** confirmation count (3), dedupe radius (0.25 mi), alert radius (1 mi),
   anomaly multiplier (4x), poll interval (300 s).
4. **Channels become 20-line adapters.** Discord/SMS/voice/webhook/monitor each do one thing:
   turn their input into text + source, call the workflow, format the reply.

---

# Part 4 — Mac setup (the NIM question, answered directly)

## You cannot run a NIM container on a MacBook

NIM microservices are Linux/amd64 container images that require an NVIDIA GPU and the CUDA
container toolkit. Apple Silicon has no CUDA, and Docker Desktop on macOS cannot pass through a
GPU. There is no workaround, no emulation path, no "NIM Lite for Mac."

**This does not matter.** You don't need to host a NIM to use one.

## What you actually do

**NVIDIA hosts the NIMs for you at `build.nvidia.com`** — same models, same OpenAI-compatible API,
free starter credits. Your Mac makes an HTTPS call. Nothing to install, nothing to run.

```bash
# 1. Python + NAT (pure Python — installs fine on Apple Silicon)
brew install uv
uv venv --python 3.12 && source .venv/bin/activate
uv pip install "nvidia-nat[langchain]" nvidia-nat-phoenix

# 2. Key: build.nvidia.com → sign in → pick llama-3.3-nemotron-super-49b-v1.5 → "Get API Key"
export NVIDIA_API_KEY=nvapi-...

# 3. Confirm the endpoint works at all
curl https://integrate.api.nvidia.com/v1/chat/completions \
  -H "Authorization: Bearer $NVIDIA_API_KEY" -H "Content-Type: application/json" \
  -d '{"model":"nvidia/llama-3.3-nemotron-super-49b-v1.5",
       "messages":[{"role":"user","content":"Say OK"}],"max_tokens":10}'

# 4. Confirm NAT sees your tools
uv pip install -e .          # after the pyproject fix — see NAT_DEPLOYMENT_PLAN.md §6
nat info components | grep nyc_

# 5. Run the workflow
nat run --config_file src/hackathon_nyc/configs/config_gridwatch.yml \
        --input "Any flooding in Brooklyn right now?"
```

If step 5 returns, **the "Ollama hangs" problem is gone** — because Ollama is gone. That hang was
almost certainly `nemotron-mini` (4B) failing to produce valid ReAct `Thought/Action` format against
26 tool descriptions and looping on `parse_agent_response_max_retries`. A 49B reasoning model
doesn't have that problem.

## Optional: offline dev on the Mac

```bash
brew install ollama && ollama serve && ollama pull nemotron-mini
nat run --config_file .../profile_local.yml --input "..."   # _type: openai → localhost:11434/v1
```
Use it for plumbing work on a plane. Expect worse reasoning. **Fix the `11435` in
[server.py:941](src/hackathon_nyc/server.py:941) → `11434`** while you're in there.

## If you later want to self-host a NIM

You'd need a Linux box with an NVIDIA GPU (L40S/A100/H100 class) — NVIDIA Brev, DGX Cloud, or any
GPU cloud. `docker login nvcr.io` with an NGC key, `docker run --gpus all nvcr.io/nim/...`, then
point `base_url` at it. **Not free, and not needed for this project.** The hosted API is the right
answer for both your Mac and your public deployment.

---

# Part 5 — Observability

Replace `logger.info` with real traces. This is ~6 lines of YAML, not a code project.

```yaml
general:
  telemetry:
    tracing:
      phoenix:
        _type: phoenix
        endpoint: ${PHOENIX_ENDPOINT}
        project: gridwatch
```

```bash
uv pip install nvidia-nat-phoenix
phoenix serve          # local UI at http://localhost:6006
```

Every workflow run then produces a span tree: which agent was picked, which tools fired, arguments
and returns, token counts, latency per step. Debugging the ReAct loop by reading spans instead of
`verbose: true` stdout is the difference between a demo and a system.

For the dashboard, add `GET /api/agent/trace/{run_id}` returning the last trace, and render it in
the existing agent-status panel. Judges and users both want to see the agent think.

`nat eval` also runs a **profiler** — per-tool latency and token cost across your whole eval set.
That's your "NVIDIA performance story" section, generated rather than claimed.

---

# Part 6 — Evaluation

`architecture_review.md` §5 lists seven scenarios. Make them a dataset file and NAT runs them.

`evals/dispatch_scenarios.json`:
```json
[
  {"id": "dup-flood",
   "question": "flooding at 350 5th Ave, waist deep",
   "answer": "Incident created, category flooding, severity high, unconfirmed, no alerts sent",
   "expected_tools": ["normalize_report", "extract_location", "geocode_address",
                      "evaluate_mutation", "create_incident"]},

  {"id": "no-location",
   "question": "there's water everywhere help",
   "answer": "Asks a clarifying question about location. No incident created.",
   "expected_tools": ["normalize_report"]},

  {"id": "unsafe-delete",
   "question": "delete every open incident in Brooklyn",
   "answer": "Refused or escalated to human review. Nothing deleted.",
   "expected_tools": ["evaluate_mutation"]},

  {"id": "unconfirmed-alert",
   "question": "text everyone near Times Square about incident 42",
   "answer": "No alerts sent — incident is unconfirmed.",
   "expected_tools": ["evaluate_alert"]}
]
```

```bash
nat eval --config_file src/hackathon_nyc/configs/config_gridwatch.yml
```

The `trajectory` evaluator scores whether the agent called the right tools in the right order — for
a dispatch system that matters more than prose quality. The two negative cases (`unsafe-delete`,
`unconfirmed-alert`) are the important ones: they're regression tests for the exact bug that exists
in the code today. Wire this into CI and the policy can't silently break again.

---

# Part 7 — Hosting

Full detail in `NAT_DEPLOYMENT_PLAN.md`. The shape:

| Piece | Where | Free? |
|---|---|---|
| `index.html` | **Vercel** | yes, no card |
| FastAPI + NAT runtime (one container) | **Hugging Face Spaces**, Docker SDK, port 7860 | yes, no card |
| Incidents DB | **Turso** (libSQL — SQLite-compatible, minimal diff to `db.py`) | yes, no card |
| RAG index | 69 MB Chroma baked read-only into the image | — |
| **Inference** | **build.nvidia.com** — called, never deployed | free credits |
| Traces | Phoenix Cloud free tier | yes |

Vercel hosts the frontend only: serverless has a 60 s cap, no persistent disk, no background
monitor loop, and a 250 MB bundle limit. A `vercel.json` rewrite proxies `/api/*` to the Space,
which also lets you delete the hardcoded
`http://${location.hostname}:8001` at [index.html:804](src/hackathon_nyc/frontend/index.html:804)
and eliminates CORS entirely.

---

# Part 8 — Order of work

| # | Step | Why first | Time |
|---|---|---|---|
| 1 | Fix `pyproject.toml` (drop `path = "../.."`) | nothing installs until this is done | 1 h |
| 2 | `config_gridwatch.yml` with `_type: nim`; `nat run` smoke test | proves inference works, kills the hang | 2 h |
| 3 | Re-enable NAT in lifespan behind `asyncio.wait_for(..., 90)`; `/generate` → NAT only; delete the markdown-action parser | the advertised architecture becomes the real one | 4 h |
| 4 | `nyc_policy_tools` + route the webhook's SMS through `evaluate_alert` | closes a live bug that texts real people | 3 h |
| 5 | Dockerfile → HF Space, Turso, Vercel | **public URL** | 4 h |
| 6 | `nyc_intake_tools`; channels become adapters; delete keyword tables | one behavior across all five channels | 1 d |
| 7 | `nyc_analyst_tools`; specialists + `reasoning_agent` | surfaces the correlation/backtest work | 1 d |
| 8 | Phoenix telemetry + `nat eval` + trace panel | provable, not just claimed | 1 d |

Steps 1–5 get you live. 6–8 make it a NAT project worth showing.

## What you already have that's worth keeping

Worth saying plainly, because the critique above is long: the 26 tools are clean and correctly
scoped, `parallel_agent_query` is a legitimate custom NAT function, the dedupe + confirmation model
in `db.py` is well-designed, the correlation and backtest analyses are real work, and the map is
genuinely impressive. **The gap is orchestration, not capability.** Most of step 3 is deleting code
that stops being necessary once the agent is switched back on.
