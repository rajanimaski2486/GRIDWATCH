# GridWatch → Full NAT Project + Public Deployment Plan

Companion to `architecture_review.md`. That doc covers *internal* agent design. This one covers
what `architecture_review.md` does not: **NVIDIA-hosted inference, config-driven NAT, packaging,
and how to get this live on free infrastructure.**

Date: 2026-07-31

---

## 1. Verdict

Three blockers stand between the current repo and a deployable NAT project. None are in
`architecture_review.md`.

| # | Blocker | Evidence | Fix |
|---|---------|----------|-----|
| B1 | **The package cannot be installed anywhere but the NAT monorepo** | `pyproject.toml:26` → `nvidia-nat = { path = "../..", editable = true }`; `pyproject.toml:7` → `setuptools_scm root = "../.."` | Depend on `nvidia-nat[langchain]` from PyPI, pin a version, drop the `[tool.uv.sources]` path override |
| B2 | **Inference is bound to a local Ollama process** | every config: `base_url: http://localhost:11434/v1`, `model_name: nemotron-mini`; fallback chat posts to `:11435` (`server.py:941`) | Move reasoning to **NVIDIA-hosted Nemotron NIMs** (`build.nvidia.com`). No GPU to host, free credits, and it removes the hang |
| B3 | **All state is on local disk** | SQLite at `data/incidents.db`, 69 MB ChromaDB at `data/chromadb/` | Incidents → hosted Postgres/libSQL. RAG → bake read-only into the image (v1) or Qdrant free tier (v2) |

The "NeMo hangs Ollama" note in `server.py:96` is very likely **not** a port bug. `nemotron-mini`
is a 4B model; the ReAct agent asks it to emit `Thought/Action/Action Input` with 26 tool
descriptions in the prompt, it fails the format, and `parse_agent_response_max_retries` loops.
`config_orchestrator.yml` compounds this by using `tool_calling_agent` — which needs real
OpenAI-style function-calling that the Ollama shim serves inconsistently for that model.
Swapping to a hosted Nemotron reasoning model (49B/253B class) fixes the root cause; fixing the
port alone will not.

---

## 2. Review of `architecture_review.md`

### What is correct and should be kept

- The core diagnosis — NAT is designed but bypassed, and each channel reimplements
  classification/geocoding/incident-creation — is accurate. Confirmed in `server.py:410`
  (webhook), `twilio_voice.py`, `discord_bot.py`, `monitor_agent.py`.
- "Promote `config_orchestrator.yml`" is the right direction.
- "Typed structured outputs + deterministic policy gate instead of markdown action parsing" is the
  single highest-value change in the document.
- The alert-before-confirmation bug (§4) is real and is a **public-safety and cost** bug once
  deployed, since it sends real SMS.

### What is missing — add these sections

| Gap | Why it matters | What to add |
|-----|----------------|-------------|
| **G1. No hosting story** | The doc assumes localhost Ollama forever. Nothing in it can go live. | A deployment topology section: which processes exist, which are stateful, what the public surface is |
| **G2. No NVIDIA-hosted inference path** | §7 mentions NIM only as "consider … beyond hackathon constraints". It is actually the *enabling* step — it removes the GPU requirement entirely | Make `_type: nim` + `NVIDIA_API_KEY` the default; Ollama becomes the offline dev profile |
| **G3. Packaging is never mentioned** | B1 above. The project literally cannot `pip install .` outside the NAT source tree | A packaging/dependency section |
| **G4. "Config-driven" is asserted, not defined** | Prompts, tool lists, thresholds, and model choices are hardcoded across `server.py`, `twilio_voice.py`, `voice_agent.py` | Define the contract: *anything a demo would tune lives in YAML* — models, prompts, tool membership, confirmation thresholds, alert radius, monitor intervals |
| **G5. Eval is described generically** | §5 says "add an evaluation suite" but NAT ships one | Use NAT's `eval` config block + `nat eval`; commit the seven fixtures the doc already lists as a dataset file |
| **G6. Observability is described generically** | §5 lists what to track but not how | NAT's telemetry plugins (Phoenix / OTel / W&B Weave) via `general.telemetry.tracing` — near-zero code |
| **G7. No public-exposure security model** | Once deployed, `/api/webhook/report` and `/generate` are open to the internet and can trigger **outbound SMS and LLM spend** | Auth on mutating routes, Twilio signature validation, per-IP rate limits, spend cap, `allow_origins` narrowed from `*` (`server.py:114`) |
| **G8. No MCP** | NAT can both serve its tools as an MCP server and consume MCP servers. `skills/` is currently prose | Expose the GridWatch tool groups over MCP; it is a strong, cheap differentiator |
| **G9. Frontend is not deployable as written** | `index.html:804` hardcodes `http://${location.hostname}:8001` | Config-injected API base URL |

### One claim to soften

§2 of the review says the port mismatch "is likely one cause of confusing runtime behavior."
Keep it as a hygiene fix, but do not present it as the cause of the hang — see §1.

---

## 3. Target architecture

```
                    ┌──────────────────────────────────────┐
   Browser  ───────▶│  gridwatch-web  (static dashboard)   │  Vercel  (free)
                    │  index.html, VITE_API_BASE injected  │
                    └───────────────┬──────────────────────┘
                                    │ HTTPS
                    ┌───────────────▼──────────────────────┐
   SMS / Voice ────▶│  gridwatch-api  (FastAPI)            │  HF Spaces / Cloud Run
   Discord     ────▶│  • channel adapters (thin)           │  (free, container)
   Monitor loop ───▶│  • incident CRM REST                 │
                    │  • deterministic POLICY GATE         │
                    │  • NAT workflow (embedded or HTTP)   │
                    └───┬──────────────┬───────────────┬───┘
                        │              │               │
             ┌──────────▼───┐  ┌───────▼───────┐  ┌────▼──────────────┐
             │ NAT runtime  │  │ Postgres/Turso│  │ ChromaDB (read-only│
             │ nat serve    │  │ incidents,    │  │ in image) or Qdrant│
             │ config-driven│  │ subs, traces  │  └───────────────────┘
             └──────┬───────┘  └───────────────┘
                    │ HTTPS
        ┌───────────▼─────────────────────────┐
        │ NVIDIA API Catalog (build.nvidia.com)│
        │ nemotron reasoning + nv-embedqa      │   ← the only "GPU" you deploy
        └──────────────────────────────────────┘
```

**Three deployables. Only one of them is stateful.**

---

## 4. Component inventory — what to build

### 4.1 New components (do not exist yet)

| Component | File | Purpose |
|-----------|------|---------|
| **NAT client shim** | `src/hackathon_nyc/nat_client.py` | One entry point every channel calls: `await run_workflow(text, source, context)`. Supports embedded (in-process `WorkflowBuilder`) *and* remote (`POST {NAT_URL}/generate`) modes via `NAT_MODE` env |
| **Policy gate** | `src/hackathon_nyc/policy.py` | Deterministic. Every mutation and every outbound alert passes through it. Thresholds read from YAML, not code |
| **Intake service** | `src/hackathon_nyc/intake.py` | The single normalize → geocode → classify → score path. Deletes the duplicate logic in webhook / SMS / Discord / voice |
| **Event log** | table `agent_events` in `db.py` | `report_received → normalized → geocoded → classified → evidence_gathered → policy_decision → incident_mutated → alert_sent`. Fixes review §8 |
| **Analyst tool group** | `register.py` | Wraps `correlation_analysis.py` + `backtest_predictions.py` as NAT tools. Fixes review §6 (Analyst Agent) |
| **Policy-gate NAT tool** | `register.py` | `nyc_policy_tools` — the agent *must* call it before mutating |
| **Eval dataset** | `evals/dispatch_scenarios.json` | The 7 fixtures from review §5 |
| **Container** | `Dockerfile` | Single image, both processes |
| **Vercel config** | `web/vercel.json` | Static frontend + API base injection |

### 4.2 Components to rewrite

| Existing | Change |
|----------|--------|
| `server.py:96` lifespan | Re-enable NAT init, bounded by `asyncio.wait_for(..., timeout=60)`; health-gate on it |
| `server.py:791` `/generate` | Delete the markdown-action fallback path (~200 lines). Route to NAT, degrade to a *read-only* answer if NAT is down |
| `server.py:941` | Remove the `:11435` direct Ollama call entirely |
| `server.py:63` `_init_rag` | Stop taking `collections[0]`; select by name from config |
| `server.py:114` CORS | `allow_origins=[VERCEL_URL]`, not `*` |
| `db.py:13` | Path-based SQLite → `DATABASE_URL` (libSQL/Postgres) |
| `monitor_agent.py` | Persist cursors to DB; emit normalized reports into intake instead of creating incidents directly |
| `twilio_voice.py`, `discord_bot.py` | Strip local classification/geocoding; call `intake.py` |
| `index.html:804` | `const API_URL = window.__GRIDWATCH_API__ \|\| ...` |

### 4.3 Components to delete

- The markdown ```action``` block parser in `server.py`.
- Per-channel category keyword tables (4 copies).
- Pipecat's separate tool schemas in `voice_agent.py` — point them at the same NAT tools.

---

## 5. The config-driven NAT layer

Replace the four overlapping configs with **one config + profile overlays**.

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
  # Deep reasoning: orchestration, triage, policy recommendation
  reasoner:
    _type: nim
    model_name: nvidia/llama-3.3-nemotron-super-49b-v1.5
    temperature: 0.2
    max_tokens: 4096
  # Fast path: tool-calling specialists, intake normalization, SMS replies
  fast:
    _type: nim
    model_name: nvidia/nvidia-nemotron-nano-9b-v2
    temperature: 0.0
    max_tokens: 2048

embedders:
  nv_embed:
    _type: nim
    model_name: nvidia/nv-embedqa-e5-v5

function_groups:
  flood_tools:    { _type: nyc_flood_tools }
  complaint_tools:{ _type: nyc_311_tools }
  geo_tools:      { _type: nyc_geo_tools }
  crm_tools:      { _type: nyc_crm_tools }
  analyst_tools:  { _type: nyc_analyst_tools }    # NEW
  policy_tools:   { _type: nyc_policy_tools }     # NEW — deterministic gate

functions:
  current_datetime: { _type: current_datetime }

  historical_rag:
    _type: nat_retriever          # verify exact type: `nat info components`
    embedder_name: nv_embed
    collection_name: nyc_311_history

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
    description: Correlation findings, hotspot prediction, risk scoring.

  both_agents:
    _type: parallel_agent_query
    agent_1: floodwatch_agent
    agent_2: command_center_agent

workflow:
  _type: react_agent
  llm_name: reasoner
  tool_names: [floodwatch_agent, command_center_agent, risk_analyst_agent,
               both_agents, crm_tools, policy_tools, current_datetime]
  verbose: true
  handle_tool_errors: true
  parse_agent_response_max_retries: 3
  system_prompt: |
    ... (moved verbatim out of server.py — prompts live in config, not code)

eval:
  general:
    dataset: { _type: json, file_path: evals/dispatch_scenarios.json }
  evaluators:
    tool_sequence: { _type: trajectory }
    answer_quality: { _type: ragas, metric: AnswerAccuracy, llm_name: reasoner }
```

Overlays: `profile_local.yml` swaps both `llms` to `_type: openai` + `base_url:
http://localhost:11434/v1` for offline dev. **Everything else stays identical.** That is what
"config-driven" has to mean here.

Verify type names against your installed version before trusting them:

```bash
nat info components | grep -Ei "nim|retriever|agent"
```

### Hard rules to enforce alongside the config

1. **No prompt text in Python.** Grep `server.py` for triple-quoted prompts; they move to YAML.
2. **No model name in Python.** `nemotron-mini` appears in `server.py`, `voice_agent.py`,
   `twilio_voice.py`.
3. **Thresholds in YAML**: confirmation report-count (3), duplicate radius (0.25 mi), alert radius
   (1 mi), monitor spike factor (4x), poll interval.
4. **Secrets only from env.** `NVIDIA_API_KEY`, `TWILIO_*`, `DISCORD_TOKEN`, `DATABASE_URL`.

---

## 6. Packaging fix (B1)

```toml
[project]
name = "gridwatch"
version = "0.2.0"                      # drop setuptools_scm root="../.."
requires-python = ">=3.11,<3.13"
dependencies = [
  "nvidia-nat[langchain]>=1.3",        # from PyPI, not ../..
  "nvidia-nat-phoenix",                # telemetry, optional
  "fastapi>=0.110", "uvicorn[standard]>=0.27",
  "aiohttp>=3.9", "pydantic>=2", "chromadb>=0.4",
  "libsql-client>=0.3",                # or psycopg[binary] for Postgres
]

[project.entry-points.'nat.components']
gridwatch = "hackathon_nyc.register"   # keep — this is how NAT finds your tools

# DELETE [tool.uv.sources] and [tool.setuptools_scm] entirely
```

The `nat.components` entry point is what makes `nat serve` discover `nyc_flood_tools` etc. It must
survive; everything else in that file changes. Verify after install:

```bash
pip install -e . && nat info components | grep nyc_
```

---

## 7. Where to deploy

### Free-tier reality check

| Host | Free? | Long-running proc | Persistent disk | Verdict for GridWatch |
|------|-------|-------------------|-----------------|------------------------|
| **Vercel** | Yes, no card | ❌ serverless only, 60 s cap (Hobby) | ❌ | **Frontend only.** 250 MB bundle cap and no background loop rules out the NAT backend |
| **Hugging Face Spaces (Docker)** | Yes, **no card** | ✅ | ⚠️ ephemeral (resets on rebuild) | ✅ **Recommended for the NAT backend.** 2 vCPU / 16 GB, public HTTPS URL, Docker SDK, secrets UI |
| **Google Cloud Run** | Free tier, **card required** | ✅ (scales to zero) | ❌ | Good alt. Scale-to-zero kills the monitor loop → use Cloud Scheduler to poke it |
| **Render** | Yes, no card | ✅ | ❌ | 512 MB RAM is tight with ChromaDB + onnxruntime; spins down after 15 min (≈50 s cold start) |
| **Fly.io / Railway** | Trial credit only | ✅ | ✅ | Not free long-term |
| **Turso (libSQL)** | Yes, no card | — | ✅ | **Incidents DB.** SQLite-compatible → smallest diff from `db.py` |
| **Neon Postgres** | Yes, no card | — | ✅ | Alternative if you'd rather move to Postgres |
| **Qdrant Cloud** | Yes, 1 GB, no card | — | ✅ | Optional v2 home for the RAG index |
| **build.nvidia.com** | Free credits | — | — | **All inference.** This is the entire GPU story |

### Recommended stack (zero credit card)

```
Frontend   → Vercel                     https://gridwatch.vercel.app
Backend    → Hugging Face Space (Docker) https://<user>-gridwatch.hf.space
Incidents  → Turso                       libsql://gridwatch-<user>.turso.io
RAG        → ChromaDB baked read-only into the image (69 MB — fine)
Inference  → NVIDIA API Catalog NIMs     integrate.api.nvidia.com
Traces     → Phoenix Cloud free tier (optional)
```

---

## 8. Deploying the NAT part

Two valid shapes. Pick one deliberately.

### Option A — Embedded (recommended for free tier)

One container. FastAPI builds the NAT workflow in-process at startup via `WorkflowBuilder` — the
code `server.py:32` already has, just re-enabled and health-gated. Same config, same registry,
same tools, one cold start, one free service.

```python
# server.py lifespan
try:
    await asyncio.wait_for(_init_nemo_agent(), timeout=90)
except asyncio.TimeoutError:
    logger.error("[NAT] build timed out — serving in degraded read-only mode")
```

### Option B — Split (the textbook NAT deployment)

`nat serve` is its own service; the API calls it over HTTP. Cleaner separation, independently
scalable, but two free services to keep warm.

```bash
nat serve --config_file src/hackathon_nyc/configs/config_gridwatch.yml --host 0.0.0.0 --port 8000
# → POST /generate, POST /chat (OpenAI-compatible), /generate/stream, /docs
```

Then `NAT_MODE=remote NAT_URL=https://<space>.hf.space` in the API service, and `nat_client.py`
POSTs to `/generate`.

### Dockerfile (works for both; HF Spaces requires port 7860)

```dockerfile
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential git curl && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir -e .

# Read-only RAG index baked in (69 MB). Skip if you move to Qdrant.
COPY data/chromadb ./data/chromadb

ENV PYTHONUNBUFFERED=1 \
    NAT_CONFIG=/app/src/hackathon_nyc/configs/config_gridwatch.yml \
    PORT=7860

# Fail the build if NAT can't see the tools
RUN nat info components | grep -q nyc_flood_tools

EXPOSE 7860
CMD ["sh", "-c", "uvicorn hackathon_nyc.server:app --host 0.0.0.0 --port ${PORT}"]
```

For Option B, add a second stage or a second Space whose `CMD` is the `nat serve` line above.

### Deploy to Hugging Face Spaces

```bash
pip install huggingface_hub && huggingface-cli login
huggingface-cli repo create gridwatch --type space --space_sdk docker
git remote add hf https://huggingface.co/spaces/<user>/gridwatch
git push hf master
```

Add a `README.md` header block that HF reads (`sdk: docker`, `app_port: 7860`), then set
**Settings → Variables and secrets**: `NVIDIA_API_KEY`, `DATABASE_URL`, `TWILIO_AUTH_TOKEN`,
`DISCORD_TOKEN`, `ALLOWED_ORIGIN`.

### Deploy the frontend to Vercel

```bash
mkdir -p web && cp src/hackathon_nyc/frontend/index.html web/
```

`web/vercel.json`:

```json
{
  "rewrites": [
    { "source": "/api/(.*)", "destination": "https://<user>-gridwatch.hf.space/api/$1" },
    { "source": "/generate",  "destination": "https://<user>-gridwatch.hf.space/generate" }
  ]
}
```

With the rewrite in place, change `index.html:804` to `const API_URL = '';` — same-origin calls,
no CORS, no hardcoded port. Then `vercel --prod`.

### Get the NVIDIA key

1. `build.nvidia.com` → sign in → pick `llama-3.3-nemotron-super-49b-v1.5` → **Get API Key**.
2. `export NVIDIA_API_KEY=nvapi-...`
3. Smoke test before deploying anything:

```bash
nat run --config_file src/hackathon_nyc/configs/config_gridwatch.yml --input "What is the current time?"
```

If that returns without hanging, the Ollama problem is gone.

---

## 9. Security checklist before going public

The backend can send SMS and spend NVIDIA credits. Open endpoints are a real liability.

- [ ] `POST /api/webhook/report` — require a shared secret header; Twilio routes validate
      `X-Twilio-Signature`
- [ ] Rate-limit `/generate` per IP (`slowapi`) and cap tokens per request
- [ ] `allow_origins` = the Vercel domain only (`server.py:114`)
- [ ] Mutating CRM routes (`PUT`/`DELETE`/`confirm`) behind a dispatcher token
- [ ] **Fix the review's §4 bug before launch** — the webhook must not alert unconfirmed incidents
- [ ] Daily alert cap + kill switch env var (`ALERTS_ENABLED=false`) so a demo can't spam people
- [ ] Scrub reporter phone numbers from logs and traces

---

## 10. Phases

| Phase | Work | Outcome |
|-------|------|---------|
| **0 — Unblock** (½ day) | Fix `pyproject.toml`; `pip install -e .`; `nat info components` sees the tools | Installable outside the monorepo |
| **1 — NVIDIA inference** (½ day) | `config_gridwatch.yml` with `_type: nim`; `nat run` smoke test; delete `:11435` path | Hang gone, no GPU needed |
| **2 — NAT is the runtime** (1 day) | Re-enable init with timeout; `/generate` → NAT only; degraded mode is read-only | The advertised architecture is the real one |
| **3 — Ship it** (1 day) | Dockerfile, HF Space, Turso, Vercel, secrets, security checklist | **Publicly live** |
| **4 — Centralize** (2 days) | `intake.py` + `policy.py` + event log; channels become thin adapters | Review §3/§4/§8 closed |
| **5 — Multi-agent + analyst** (1 day) | Orchestrator with specialists, analyst tool group, structured synthesis | Review §6 closed |
| **6 — Prove it** (1 day) | `nat eval` with the 7 fixtures, Phoenix traces, agent status panel | Review §5 closed |

Phases 0–3 are the path to a live URL. 4–6 are what make it a *good* NAT project.

---

## 11. Answers to the direct questions

**What components does it need?** — §4. Net new: NAT client shim, policy gate, intake service,
event log, analyst + policy tool groups, eval dataset, Dockerfile, Vercel config.

**What needs deploying, and where?** — Three things. Static dashboard → **Vercel**. FastAPI +
NAT runtime → **Hugging Face Spaces (Docker)**, free and no card. State → **Turso**. Inference is
not deployed at all; it is called on **build.nvidia.com**.

**Can it go on Vercel?** — The frontend, yes. The backend, no: serverless, 60 s cap, no background
monitor, no persistent disk, and a 250 MB bundle limit that ChromaDB alone strains.

**How do you deploy the NAT part?** — §8. Embedded in the FastAPI process (Option A, recommended
here) or as a standalone `nat serve` container (Option B). Same image either way; only the `CMD`
and `NAT_MODE` differ.
