#!/usr/bin/env python3
"""Generate the GridWatch Excalidraw diagram set.

    python scripts/make_diagrams.py

Writes docs/diagrams/*.excalidraw. Open at https://excalidraw.com (File ->
Open) or with the VS Code Excalidraw extension. They are plain JSON and stay
editable — this script produces the starting point, not a locked artifact.

Generating rather than drawing keeps seven diagrams consistent in palette,
spacing and vocabulary, and means an architecture change is a re-run.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

OUT = Path(__file__).parent.parent / "docs" / "diagrams"

# NVIDIA green anchors the palette; everything else is muted so the accent reads.
NV = "#76b900"
INK = "#1e1e1e"
GREY = "#868e96"
BLUE = "#1971c2"
RED = "#e03131"
ORANGE = "#f08c00"
VIOLET = "#6741d9"
TEAL = "#0c8599"

FILL = {
    NV: "#e9f7d4", BLUE: "#d0ebff", RED: "#ffe3e3", ORANGE: "#fff3bf",
    VIOLET: "#e5dbff", TEAL: "#c5f6fa", GREY: "#f1f3f5", INK: "#ffffff",
}

HAND, CODE = 1, 3  # Excalidraw fontFamily ids: Virgil, Cascadia


def _nonce() -> int:
    return random.randint(1, 2**31 - 1)


def _base(kind: str, x: float, y: float, w: float, h: float, stroke: str, **kw) -> dict:
    el = {
        "id": f"{kind}-{_nonce()}", "type": kind,
        "x": x, "y": y, "width": w, "height": h,
        "angle": 0, "strokeColor": stroke,
        "backgroundColor": kw.pop("bg", "transparent"),
        "fillStyle": "solid", "strokeWidth": kw.pop("sw", 2),
        "strokeStyle": kw.pop("dash", "solid"), "roughness": 1,
        "opacity": 100, "groupIds": [], "frameId": None,
        "roundness": kw.pop("round", {"type": 3}),
        "seed": _nonce(), "version": 1, "versionNonce": _nonce(),
        "isDeleted": False, "boundElements": None,
        "updated": 1, "link": None, "locked": False,
    }
    el.update(kw)
    return el


def box(x, y, w, h, stroke=INK, dashed=False, **kw) -> dict:
    return _base("rectangle", x, y, w, h, stroke,
                 bg=FILL.get(stroke, "#ffffff"),
                 dash="dashed" if dashed else "solid", **kw)


def panel(x, y, w, h, stroke=GREY) -> dict:
    """A background grouping container — no fill, dashed, thin."""
    return _base("rectangle", x, y, w, h, stroke, bg="transparent",
                 dash="dashed", sw=1)


def text(x, y, s, size=16, color=INK, font=HAND, align="left", w=None) -> dict:
    lines = s.split("\n")
    width = w if w is not None else max(len(ln) for ln in lines) * size * 0.58
    el = _base("text", x, y, width, len(lines) * size * 1.25, color,
               round=None, sw=1)
    el.update({
        "text": s, "originalText": s, "fontSize": size, "fontFamily": font,
        "textAlign": align, "verticalAlign": "top", "containerId": None,
        "lineHeight": 1.25, "baseline": size,
    })
    return el


def label(cx, y, s, size=16, color=INK, font=HAND) -> dict:
    """Horizontally centred text around cx."""
    lines = s.split("\n")
    width = max(len(ln) for ln in lines) * size * 0.58
    return text(cx - width / 2, y, s, size, color, font, "center", w=width)


def arrow(x1, y1, x2, y2, stroke=INK, dashed=False, label_text=None,
          label_size=12) -> list:
    a = _base("arrow", x1, y1, x2 - x1, y2 - y1, stroke, round={"type": 2},
              dash="dashed" if dashed else "solid")
    a.update({
        "points": [[0, 0], [x2 - x1, y2 - y1]],
        "lastCommittedPoint": None, "startBinding": None, "endBinding": None,
        "startArrowhead": None, "endArrowhead": "arrow",
    })
    out = [a]
    if label_text:
        out.append(label((x1 + x2) / 2, (y1 + y2) / 2 - 20, label_text,
                         label_size, GREY))
    return out


def node(x, y, w, h, title, subtitle="", stroke=INK, title_size=17) -> list:
    """A titled box with optional code-font detail underneath."""
    els = [box(x, y, w, h, stroke)]
    cx = x + w / 2
    if subtitle:
        els.append(label(cx, y + h / 2 - (title_size * 1.25 + 22) / 2,
                         title, title_size, stroke))
        els.append(label(cx, y + h / 2 + 2, subtitle, 12, GREY, CODE))
    else:
        els.append(label(cx, y + h / 2 - title_size * 0.62, title,
                         title_size, stroke))
    return els


def write(name: str, elements: list, title: str) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    doc = {
        "type": "excalidraw", "version": 2,
        "source": "https://github.com/rajanimaski2486/GRIDWATCH",
        "elements": elements,
        "appState": {"gridSize": None, "viewBackgroundColor": "#ffffff"},
        "files": {},
    }
    path = OUT / f"{name}.excalidraw"
    path.write_text(json.dumps(doc, indent=2))
    print(f"  {path.name:34} {len(elements):3} elements   {title}")
    return path


def header(t: str, sub: str) -> list:
    return [text(40, 30, t, 30, INK), text(40, 72, sub, 14, GREY, CODE)]


# ===========================================================================
# 00 — Master
# ===========================================================================

def master() -> list:
    e = header("GridWatch — System Map",
               "NYC dispatch console on NVIDIA NeMo Agent Toolkit  ·  each layer has its own diagram")

    layers = [
        ("1. Intake", "every channel, one path", "01-intake", BLUE, "SMS · Voice · Discord · Web · Monitor"),
        ("2. Orchestration", "NAT multi-agent workflow", "02-orchestration", NV, "react_agent · Nemotron 49B + nano-9b"),
        ("3. Data Access", "32 tools, 6 groups", "03-data-access", TEAL, "live NYC APIs · incidents DB · analysis"),
        ("4. Retrieval", "hosted search, NIM embeddings", "04-retrieval", VIOLET, "OpenSearch · nv-embedqa-e5-v5"),
        ("5. Policy Gate", "deterministic, no LLM", "05-policy-gate", RED, "every mutation · every alert"),
        ("6. Deployment", "all free tier", "06-deployment", ORANGE, "Vercel · HF Spaces · Turso · Aiven"),
    ]

    x, y, w, h, gap = 60, 130, 400, 92, 130
    for i, (title, sub, ref, color, detail) in enumerate(layers):
        yy = y + i * (h + gap - 40)
        e += node(x, yy, w, h, title, detail, color, 19)
        e.append(text(x + w + 26, yy + 18, sub, 14, INK))
        e.append(text(x + w + 26, yy + 44, f"{ref}.excalidraw", 12, GREY, CODE))
        if i < len(layers) - 1:
            e += arrow(x + w / 2, yy + h, x + w / 2, yy + h + (gap - 40))

    # The one external dependency, called and never hosted.
    bx = 830
    # Outer container first and transparent: a filled box drawn afterwards
    # paints over the text inside it.
    e.append(panel(bx - 14, 118, 328, 220, NV))
    e += node(bx, 138, 300, 84, "NVIDIA API", "build.nvidia.com", NV, 20)
    e.append(text(bx + 16, 240, "llama-3.3-nemotron-super-49b\nnvidia-nemotron-nano-9b-v2\nnv-embedqa-e5-v5", 12, GREY, CODE))
    e.append(label(bx + 150, 352, "no GPU deployed — reasoning and\nembeddings are HTTPS calls", 13, GREY))

    e.append(panel(bx - 20, 400, 340, 250))
    e.append(text(bx, 418, "What makes it a NAT project", 16, INK))
    for i, line in enumerate([
        "config drives models, prompts,",
        "routing, tools and eval",
        "",
        "custom function groups + a custom",
        "retriever provider registered",
        "through NAT's own APIs",
        "",
        "nat run / serve / eval / info",
    ]):
        e.append(text(bx, 448 + i * 22, line, 13, GREY if i else INK, CODE))
    return e


# ===========================================================================
# 01 — Intake
# ===========================================================================

def intake() -> list:
    e = header("1. Intake — every channel, one path",
               "src/hackathon_nyc/intake.py")

    chans = [("SMS", "Twilio"), ("Voice", "Twilio + Whisper"),
             ("Discord", "discord.py"), ("Web form", "dashboard"),
             ("Monitor", "FloodNet + 311 poll")]
    for i, (name, tech) in enumerate(chans):
        y = 140 + i * 92
        e += node(60, y, 210, 66, name, tech, BLUE, 16)
        e += arrow(270, y + 33, 380, 300 if i != 2 else 300)

    e.append(panel(380, 130, 330, 420))
    e.append(label(545, 142, "intake.process_report()", 17, INK, CODE))
    steps = [
        ("normalize", "repair STT: 'bleeding' → 'flooding'"),
        ("locate", "8 address candidates, NYC bbox"),
        ("classify", "ordered rules, longest phrase wins"),
        ("score urgency", "keyword tiers → severity"),
        ("life safety?", "gas · fire · collapse → call 911"),
    ]
    for i, (s, d) in enumerate(steps):
        y = 180 + i * 68
        e += node(400, y, 290, 52, s, d, INK, 15)
        if i < len(steps) - 1:
            e += arrow(545, y + 52, 545, y + 68)

    e += arrow(545, 550, 545, 600)
    e += node(400, 600, 290, 62, "db.create_incident", "dedupe: same category, 0.25 mi", TEAL, 16)
    e += arrow(545, 662, 545, 712)
    e += node(400, 712, 290, 62, "policy.evaluate_alert", "the only door to outbound", RED, 16)

    e += arrow(690, 743, 800, 700)
    e += node(800, 660, 250, 62, "alerts sent", "confirmed + recipients", NV, 16)
    e += arrow(690, 760, 800, 790)
    e += node(800, 760, 250, 62, "suppressed + logged", "reason recorded", GREY, 16)

    e.append(panel(760, 130, 400, 330))
    e.append(text(780, 148, "Why this exists", 17, INK))
    for i, line in enumerate([
        "Four channels each had their own",
        "keyword tables, urgency scoring and",
        "address parsing. The same words",
        "produced different incidents.",
        "",
        "Two bugs fell out of merging them:",
        "",
        "· 'I smell gas' classified as 'other'",
        "  — single words beat phrases",
        "· life-safety reports got a ticket",
        "  number instead of 'call 911'",
        "",
        "Verified: identical category, severity,",
        "911 flag and alert decision on all four.",
    ]):
        e.append(text(780, 180 + i * 20, line, 13, INK if i in (0, 5) else GREY))
    return e


# ===========================================================================
# 02 — Orchestration
# ===========================================================================

def orchestration() -> list:
    e = header("2. Orchestration — the NAT workflow",
               "configs/config_gridwatch.yml  ·  nothing here is hardcoded in Python")

    e += node(60, 140, 230, 70, "POST /generate", "server.py", INK, 16)
    e += arrow(290, 175, 400, 175)

    e += node(400, 130, 330, 92, "react_agent", "llm: Nemotron 49B  ·  orchestrator", NV, 20)
    e.append(label(565, 236, "routes by intent, enforces the system prompt", 13, GREY))

    specialists = [
        ("floodwatch_agent", "flood_tools + geo", "nano-9b", TEAL),
        ("command_center_agent", "311 + geo + history", "nano-9b", TEAL),
        ("risk_analyst_agent", "analyst + history", "nano-9b", VIOLET),
        ("both_agents", "parallel_agent_query", "asyncio.gather", ORANGE),
    ]
    for i, (name, tools, model, color) in enumerate(specialists):
        y = 300 + i * 96
        e += arrow(565, 222 if i == 0 else 300 + (i - 1) * 96 + 70, 300, y + 35, dashed=True)
        e += node(120, y, 300, 70, name, f"{tools}   [{model}]", color, 15)

    e.append(panel(470, 300, 330, 384))
    e.append(text(490, 316, "Direct tools", 16, INK))
    for i, (g, n) in enumerate([("crm_tools", 13), ("geo_tools", 3), ("history_tools", 2),
                                ("analyst_tools", 3), ("current_datetime", 1)]):
        e.append(text(490, 350 + i * 30, f"{g:20} {n:>2}", 13, GREY, CODE))
    e.append(text(490, 520, "32 tools across 6 groups", 14, INK))
    e.append(text(490, 548, "Agents use react_agent, not\ntool_calling_agent: with a\nFunctionGroup the 9B model emits\nan empty tool call that fails\nToolMessage validation. Measured,\nnot assumed.", 12, GREY))

    e.append(panel(840, 130, 340, 290))
    e.append(text(860, 148, "Config-driven", 17, INK))
    for i, line in enumerate([
        "models · prompts · routing rules",
        "tool membership · thresholds",
        "eval dataset · evaluators",
        "",
        "profile_local.yml swaps both LLMs",
        "to Ollama and changes nothing else.",
        "",
        "nat run   — one-shot",
        "nat serve — OpenAPI at /docs",
        "nat eval  — the scenario suite",
    ]):
        e.append(text(860, 180 + i * 23, line, 13, GREY, CODE if i > 6 else HAND))

    e.append(panel(840, 450, 340, 234))
    e.append(text(860, 468, "Degraded mode", 17, RED))
    for i, line in enumerate([
        "NAT build is wrapped in a 90s",
        "timeout. If it fails, the dashboard,",
        "incident API and citizen intake keep",
        "working and /generate serves a",
        "read-only summary that structurally",
        "cannot mutate anything.",
        "",
        "/api/agent/status reports the reason.",
    ]):
        e.append(text(860, 500 + i * 22, line, 13, GREY))
    return e


# ===========================================================================
# 03 — Data access
# ===========================================================================

def data_access() -> list:
    e = header("3. Data Access — 32 tools across 6 groups",
               "src/hackathon_nyc/register.py  ·  discovered via the nat.components entry point")

    groups = [
        ("nyc_flood_tools", 7, "FloodNet sensors, active/worst floods,\nvulnerability, air quality", TEAL, "FloodNet + NYC Open Data"),
        ("nyc_311_tools", 4, "complaints by type/borough/zip,\nstats, proximity, keyword search", TEAL, "NYC Open Data (SODA)"),
        ("nyc_geo_tools", 3, "geocode, reverse geocode,\nnearest sensors", BLUE, "Nominatim / OSM"),
        ("nyc_crm_tools", 13, "incident CRUD, subscriptions,\nconfirmation, policy checks", RED, "SQLite / Turso"),
        ("nyc_history_tools", 2, "historical search, all indices\nor one topic", VIOLET, "OpenSearch (Aiven)"),
        ("nyc_analyst_tools", 3, "correlations, prediction accuracy,\nrisk scoring", ORANGE, "cached analysis output"),
    ]

    for i, (name, count, what, color, backing) in enumerate(groups):
        col, row = i % 2, i // 2
        x, y = 60 + col * 560, 140 + row * 200
        e += node(x, y, 330, 74, name, f"{count} tools", color, 17)
        e.append(text(x, y + 88, what, 13, GREY))
        e += arrow(x + 330, y + 37, x + 420, y + 37, GREY, dashed=True)
        e += node(x + 420, y + 10, 110, 54, "", "", GREY)
        e.append(label(x + 475, y + 26, backing.split(" (")[0], 11, INK))
        if "(" in backing:
            e.append(label(x + 475, y + 42, "(" + backing.split(" (")[1], 10, GREY, CODE))

    e.append(panel(60, 750, 1090, 180))
    e.append(text(80, 768, "Two things worth saying out loud in a demo", 17, INK))
    for i, line in enumerate([
        "Tool output is capped at ~12k characters. An unbounded return once sent 125,953 tokens into a 128,000",
        "token context and the request was rejected — get_flood_sensors was dumping all 479 sensors. Tools return",
        "JSON for a model to read, not a data export, so every payload trims and says when it did.",
        "",
        "The include list on each group is an allowlist. It once named 7 of the 13 CRM tools, and the 6 it dropped",
        "were the alert ones — including check_alerts, the only code enforcing 'unconfirmed incidents never notify'.",
    ]):
        e.append(text(80, 800 + i * 22, line, 12, GREY))
    return e


# ===========================================================================
# 04 — Retrieval
# ===========================================================================

def retrieval() -> list:
    e = header("4. Retrieval — hosted OpenSearch, NVIDIA embeddings",
               "retrievers/opensearch.py — a retriever provider registered through NAT's own API")

    # Ingest lane
    e.append(panel(50, 120, 1100, 210))
    e.append(text(70, 134, "INDEX   python -m hackathon_nyc.ingest_opensearch --all --download", 15, INK, CODE))
    ingest = [
        ("NYC Open Data", "SODA API"), ("one doc per record", "not 5 per blob"),
        ("priority fields first", "complaint_type, address"), ("NIM embed", "nv-embedqa-e5-v5"),
        ("OpenSearch index", "knn_vector, hnsw, cosine"),
    ]
    for i, (t, s) in enumerate(ingest):
        x = 70 + i * 218
        e += node(x, 180, 190, 70, t, s, VIOLET, 14)
        if i < len(ingest) - 1:
            e += arrow(x + 190, 215, x + 218, 215)
    e.append(text(70, 276, "1024 dimensions  ·  lat/lon indexed as real fields, which is what deleted the regex that used to scrape", 12, GREY))
    e.append(text(70, 296, "coordinates back out of concatenated chunk text", 12, GREY))

    # Query lane
    e.append(panel(50, 360, 1100, 270))
    e.append(text(70, 374, "QUERY   nyc_history_tools.search_history()", 15, INK, CODE))
    e += node(70, 420, 180, 66, "question", "from the agent", INK, 15)
    e += arrow(250, 453, 300, 453)
    e += node(300, 420, 180, 66, "NIM embed", "same model", NV, 15)
    e += arrow(480, 453, 530, 453)
    e += node(530, 400, 190, 54, "kNN", "vector search", VIOLET, 15)
    e += node(530, 470, 190, 54, "BM25", "lexical — OFF", GREY, 15)
    e += arrow(720, 427, 780, 453)
    e += arrow(720, 497, 780, 453, GREY, dashed=True)
    e += node(780, 420, 150, 66, "RRF", "rank fusion", ORANGE, 15)
    e += arrow(930, 453, 980, 453)
    e += node(980, 420, 150, 66, "documents", "+ lat/lon", TEAL, 15)
    e.append(text(70, 560, "Results feed the agent's answer AND the map layer — the tool records coordinates so the dashboard", 12, GREY))
    e.append(text(70, 580, "can plot 'N historical records' without a second query.", 12, GREY))

    # The honest bit
    e.append(panel(50, 660, 1100, 200))
    e.append(text(70, 678, "Hybrid search is implemented and OFF by default — because it was measured", 17, RED))
    for i, line in enumerate([
        "The pitch for OpenSearch over a pure vector store was hybrid retrieval. Then it was tested on ~360 documents:",
        "",
        "· exact-token queries it was supposed to win  ('TIEBOUT AVENUE', 'Concord St/Navy St sensor')  →  identical results",
        "· 'flooding water depth'  →  BM25 pulled an Illegal Parking record to rank 2 on a stray token match,",
        "   where vector-only returned three clean flood events",
        "",
        "Small corpus + field-value text gives BM25 too many spurious matches, and RRF rewards a top-ranked lexical hit",
        "regardless of relevance. Turn it on when the index is large enough to pay for itself — and re-measure.",
    ]):
        e.append(text(70, 706 + i * 19, line, 12, GREY))
    return e


# ===========================================================================
# 05 — Policy gate
# ===========================================================================

def policy_gate() -> list:
    e = header("5. Policy Gate — deterministic, no LLM",
               "src/hackathon_nyc/policy.py  ·  thresholds in configs/policy.yml")

    e.append(box(50, 120, 1100, 78, RED, dashed=True))
    e.append(label(600, 134, "A system prompt is a suggestion. This is enforcement.", 19, RED))
    e.append(label(600, 164, "No LLM call belongs in this module — the agent refuses mass deletion because it was asked nicely.", 13, GREY))

    # evaluate_alert
    e.append(panel(50, 230, 520, 440))
    e.append(text(70, 246, "evaluate_alert(incident_id)", 17, INK, CODE))
    checks = [
        ("ALERTS_ENABLED?", "deploy-time kill switch"),
        ("confirmed?", "3 reports or a dispatcher"),
        ("has coordinates?", "who is nearby is unanswerable"),
        ("recipients ≤ cap?", "50, else human sign-off"),
    ]
    for i, (c, d) in enumerate(checks):
        y = 290 + i * 88
        e += node(80, y, 300, 60, c, d, RED, 15)
        e += arrow(380, y + 30, 440, y + 30, GREY)
        e.append(label(490, y + 20, "refuse", 13, GREY))
        if i < len(checks) - 1:
            e += arrow(230, y + 60, 230, y + 88)
    e += arrow(230, 642, 230, 664)
    e.append(label(300, 646, "→ send to exactly these recipients", 13, NV))

    # mutations
    e.append(panel(600, 230, 550, 200))
    e.append(text(620, 246, "evaluate_mutation(action, id)", 17, INK, CODE))
    e.append(text(620, 285, "delete · resolve_all · mass_alert require an explicit\nincident id. 'Delete every open incident in Brooklyn'\nis not an explicit id.", 13, GREY))
    e.append(text(620, 360, "delete_incident calls the gate itself — a control the\nmodel can choose to skip is not a control.", 13, INK))

    # confirmation
    e.append(panel(600, 460, 550, 210))
    e.append(text(620, 476, "evaluate_confirmation(id, actor)", 17, INK, CODE))
    e += node(620, 515, 230, 56, "actor = dispatcher", "allowed — human judgement", NV, 14)
    e += node(880, 515, 240, 56, "actor = agent", "only at/above threshold", RED, 14)
    e.append(text(620, 590, "Found by nat eval: the agent was confirming a 2-report\nincident and announcing subscribers would be notified,\nwith the threshold set to 3. Confirmation unlocks alerts,\nwhich makes this the same class of bug as the original.", 13, GREY))

    e.append(panel(50, 700, 1100, 150))
    e.append(text(70, 718, "The bug this was built for", 17, INK))
    for i, line in enumerate([
        "/api/webhook/report used to text every subscriber in range the instant an incident was created, with no",
        "confirmation check at all. Every phone, SMS and Discord report bypassed the anti-spam rule — one false",
        "report produced real SMS to real people. The CRM tool that enforced the rule correctly was, separately,",
        "filtered out of its own tool group, so the agent could not have applied it either.",
    ]):
        e.append(text(70, 750 + i * 22, line, 12, GREY))
    return e


# ===========================================================================
# 06 — Deployment
# ===========================================================================

def deployment() -> list:
    e = header("6. Deployment — all free tier, no GPU",
               "DEPLOY.md  ·  verified against the built image")

    e += node(60, 150, 280, 90, "Dashboard", "index.html — one file", BLUE, 18)
    e.append(text(60, 250, "Vercel\nfree, no credit card\nstatic + rewrites to the API", 13, GREY))

    e += arrow(340, 195, 430, 195, GREY, dashed=True)
    e.append(label(385, 160, "/api/* proxy", 11, GREY))

    e += node(430, 130, 340, 130, "FastAPI + NAT runtime", "one container, port 7860", NV, 19)
    e.append(text(430, 272, "Hugging Face Spaces (Docker)\nfree CPU, 2 vCPU / 16 GB, no card\n2.46 GB image · healthy in ~15 s", 13, GREY))

    stores = [
        ("Turso", "libSQL — incidents,\nvotes, subscriptions", TEAL, 60),
        ("Aiven OpenSearch", "historical index,\nsurvives restarts", VIOLET, 430),
        ("NVIDIA API", "reasoning + embeddings\ncalled, never hosted", NV, 800),
    ]
    for name, detail, color, x in stores:
        e += node(x, 420, 300, 80, name, "", color, 17)
        e.append(text(x + 14, 512, detail, 12, GREY))
        e += arrow(600, 260, x + 150, 420, GREY, dashed=True)

    e += node(800, 130, 300, 130, "Channels", "Twilio · Discord", ORANGE, 18)
    e += arrow(800, 195, 770, 195)

    e.append(panel(60, 600, 1040, 200))
    e.append(text(80, 618, "Before the URL is public", 17, RED))
    for i, line in enumerate([
        "GRIDWATCH_TOKEN        state-changing routes; without it anyone who finds the deployment can delete incidents",
        "ALERTS_ENABLED=false   until the confirmation flow is verified with real subscribers",
        "ALLOWED_ORIGINS        the Vercel domain, not *",
        "rate limit /generate   every call spends NIM credits and nothing stops a script looping it",
        "",
        "Reads stay open either way, so the public map keeps working.",
    ]):
        e.append(text(80, 650 + i * 23, line, 12, GREY, CODE if i < 4 else HAND))

    e.append(panel(60, 830, 1040, 110))
    e.append(text(80, 848, "Not deployed on purpose", 16, INK))
    e.append(text(80, 878, "The background monitor stays behind GRIDWATCH_MONITOR=1 — it now goes through the policy gate and\npersists its cursors, but it assumes a long-lived process, which a scale-to-zero host breaks.", 12, GREY))
    return e


DIAGRAMS = [
    ("00-master", master, "the layer map — start here"),
    ("01-intake", intake, "channels → normalize → gate"),
    ("02-orchestration", orchestration, "NAT workflow and routing"),
    ("03-data-access", data_access, "32 tools and what they touch"),
    ("04-retrieval", retrieval, "OpenSearch + NIM embeddings"),
    ("05-policy-gate", policy_gate, "the safety boundary"),
    ("06-deployment", deployment, "free-tier topology"),
]


def main() -> None:
    random.seed(7)  # stable ids across runs, so re-running produces a clean diff
    print(f"Writing to {OUT}/")
    for name, fn, desc in DIAGRAMS:
        write(name, fn(), desc)
    print("\nOpen at https://excalidraw.com (File -> Open), or use the VS Code Excalidraw extension.")


if __name__ == "__main__":
    main()
