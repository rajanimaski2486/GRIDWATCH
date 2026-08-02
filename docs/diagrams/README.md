# GridWatch diagrams

Seven Excalidraw files: one master map plus one per layer. Open at
[excalidraw.com](https://excalidraw.com) (**File → Open**) or with the VS Code
Excalidraw extension. They are plain JSON and fully editable — move things,
recolour, delete what you don't need for a given audience.

Regenerate after an architecture change:

```bash
.venv/bin/python scripts/make_diagrams.py
```

| File | Layer | The one sentence |
|---|---|---|
| `00-master` | System map | Six layers, one external dependency, no GPU deployed |
| `01-intake` | Channels | Five ways in, one path through |
| `02-orchestration` | NAT workflow | An orchestrator routes to specialists; the config is the system |
| `03-data-access` | Tools | 32 tools, and what each actually touches |
| `04-retrieval` | Search | Hosted OpenSearch, NVIDIA embeddings, hybrid off *because it was measured* |
| `05-policy-gate` | Safety | The model proposes; deterministic code disposes |
| `06-deployment` | Hosting | Free tier end to end, because nothing runs a model locally |

---

## A 6-minute walkthrough

Open `00-master`, then follow the numbered layers. Each has a point worth
making — the numbers below are real and reproducible, which is what makes them
worth saying.

**Open on the master (45s).** Six layers. The only thing outside the box is the
NVIDIA API — reasoning and embeddings are HTTPS calls, so there is no GPU in
this architecture. That is what lets the whole system sit on free infrastructure.

**01 · Intake (60s).** Five channels: SMS, voice, Discord, web, and a background
monitor. They used to be four separate implementations of the same four steps,
so the same words produced different incidents depending on which door they came
through. Now they call one function. Two bugs fell out of merging them: *"I smell
gas"* classified as `other` because single keywords beat multi-word phrases, and
life-safety reports got a ticket number instead of *"call 911."*

**02 · Orchestration (75s).** A ReAct agent on Nemotron 49B routes to
specialists running the 9B model. Everything — models, prompts, routing, tool
membership, thresholds, the eval suite — lives in one YAML. Worth pausing on:
the agents use `react_agent` rather than `tool_calling_agent` because with a
FunctionGroup the 9B model emits an empty tool call that fails validation. That
was bisected, not guessed.

**03 · Data access (45s).** 32 tools in 6 groups, each pointing at what it
touches. The `include` list on each group is an allowlist — it once named 7 of
13 CRM tools, and the 6 it dropped were the alert ones, including the only code
enforcing the confirmation rule.

**04 · Retrieval (75s).** Index and query lanes. The honest part is at the
bottom: hybrid kNN+BM25 is implemented and **off by default**, because it was
measured on ~360 documents and lost — "flooding water depth" pulled an *Illegal
Parking* record to rank 2 on a stray token match, where vector-only returned
three clean flood events. Shipping the measurement rather than the pitch tends
to land well.

**05 · Policy gate (75s).** The safety boundary, and the best story in the deck.
`/api/webhook/report` used to text every nearby subscriber the instant an
incident was created, with no confirmation check — one false report produced
real SMS. Now every mutation and every alert passes through code with no LLM in
it. And `nat eval` later caught a second instance of the same bug class: the
agent was confirming a 2-report incident against a threshold of 3, and
announcing that subscribers would be notified.

**06 · Deployment (45s).** Vercel, Hugging Face Spaces, Turso, Aiven, and the
NVIDIA API. All free tier, no credit card for most of it. Container is 2.46 GB
and healthy in about 15 seconds.

---

## If you only have two minutes

`00-master` → `05-policy-gate`. The system map establishes that this is a real
multi-agent architecture on NVIDIA models; the policy gate shows you found a bug
that would have texted strangers and then built the thing that makes it
impossible. That pairing is the strongest two minutes in the set.

## If the audience is technical

Add `02-orchestration` and `04-retrieval`. Both contain a decision that was
measured rather than assumed — the agent-type bisection and the hybrid-search
regression — and those are the moments where a technical audience starts
believing the rest.
