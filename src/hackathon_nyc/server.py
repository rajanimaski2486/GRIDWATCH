"""FastAPI server for the NYC Urban Intelligence CRM.

Provides REST API endpoints for dispatchers to manage incidents,
plus serves the AI agent for analysis and triage.

Run: uvicorn hackathon_nyc.server:app --reload --port 8000
"""

from contextlib import asynccontextmanager
import asyncio
import logging
import os
import secrets

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from pathlib import Path

from hackathon_nyc import db
from hackathon_nyc import policy

logger = logging.getLogger(__name__)


async def _send_alerts(recipients: list, body: str) -> int:
    """Deliver an approved alert. Callers must pass policy-approved recipients.

    This function decides nothing — policy.evaluate_alert() does. Keeping
    delivery dumb means there is exactly one place where the confirmation rule
    can be got wrong.
    """
    sent = 0
    sid = os.getenv("TWILIO_ACCOUNT_SID")
    token = os.getenv("TWILIO_AUTH_TOKEN")
    from_num = os.getenv("TWILIO_PHONE_NUMBER")

    twilio_client = None
    if sid and token and from_num:
        try:
            from twilio.rest import Client as TwilioClient
            twilio_client = TwilioClient(sid, token)
        except Exception as e:
            logger.warning("[Alert] Twilio unavailable: %s", e)

    for sub in recipients:
        contact = sub.get("contact")
        if not contact:
            continue
        try:
            if sub.get("contact_type") == "sms":
                if twilio_client is None:
                    logger.warning("[Alert] Twilio not configured; SMS skipped")
                    continue
                # Twilio's client is blocking; keep it off the event loop.
                await asyncio.to_thread(
                    twilio_client.messages.create, body=body, from_=from_num, to=contact
                )
            else:
                from hackathon_nyc.openclaw_alerts import send_alert
                result = await send_alert(sub["contact_type"], contact, body)
                if result.get("status") != "sent":
                    continue
            sent += 1
        except Exception as e:
            logger.warning("[Alert] Delivery failed for a subscriber: %s", e)
    return sent

# ---------------------------------------------------------------------------
# NeMo ReAct Agent + RAG — global state
# ---------------------------------------------------------------------------
_nemo_workflow = None
_nemo_builder_ctx = None
_nemo_builder = None
_rag_status: dict = {"backend": "none", "reachable": False, "docs": 0, "detail": ""}


_nat_status: dict = {"state": "not_started", "detail": "", "config": "", "tools": 0}

# Which workflow config to build. Override with NAT_CONFIG to point at
# profile_local.yml for offline work.
NAT_CONFIG = os.getenv(
    "NAT_CONFIG",
    str(Path(__file__).parent / "configs" / "config_gridwatch.yml"),
)
NAT_BUILD_TIMEOUT = float(os.getenv("NAT_BUILD_TIMEOUT", "90"))


async def _build_nat_workflow():
    """Build the NAT workflow. Raises on failure — the caller handles it."""
    global _nemo_workflow, _nemo_builder_ctx, _nemo_builder

    from nat.runtime.loader import PluginTypes, discover_and_register_plugins
    discover_and_register_plugins(PluginTypes.ALL)

    from nat.utils.io.yaml_tools import yaml_load
    from nat.utils.data_models.schema_validator import validate_schema
    from nat.data_models.config import Config
    from nat.builder.workflow_builder import WorkflowBuilder

    import hackathon_nyc.register  # noqa: F401  (registers the tool groups)

    config = validate_schema(yaml_load(Path(NAT_CONFIG)), Config)

    _nemo_builder_ctx = WorkflowBuilder.from_config(config)
    _nemo_builder = await _nemo_builder_ctx.__aenter__()
    _nemo_workflow = await _nemo_builder.build()

    tool_count = 0
    for group_name in ("flood_tools", "complaint_tools", "geo_tools", "crm_tools", "history_tools"):
        try:
            group = await _nemo_builder.get_function_group(group_name)
            tool_count += len(await group.get_accessible_functions())
        except Exception:
            pass
    return tool_count


async def _init_nemo_agent():
    """Initialize the NAT workflow, bounded by a timeout.

    A hung build must not take the whole server down with it — the dashboard,
    incident CRM and citizen intake all work without the agent.
    """
    global _nemo_workflow
    _nat_status["config"] = Path(NAT_CONFIG).name
    try:
        tool_count = await asyncio.wait_for(_build_nat_workflow(), timeout=NAT_BUILD_TIMEOUT)
        _nat_status.update(state="ready", detail="", tools=tool_count)
        logger.info("[NAT] Workflow ready from %s (%d tools)", Path(NAT_CONFIG).name, tool_count)
    except asyncio.TimeoutError:
        _nemo_workflow = None
        _nat_status.update(state="degraded", detail=f"build timed out after {NAT_BUILD_TIMEOUT:.0f}s")
        logger.error("[NAT] Build timed out after %.0fs — serving in degraded mode", NAT_BUILD_TIMEOUT)
    except Exception as e:
        _nemo_workflow = None
        _nat_status.update(state="degraded", detail=f"{type(e).__name__}: {e}")
        logger.error("[NAT] Build failed — serving in degraded mode: %s", e)


def _check_rag() -> dict:
    """Report OpenSearch index health for the dashboard status panel.

    Retrieval itself runs inside the NAT workflow via the opensearch_retriever;
    this only answers "is the index reachable and does it have documents".
    """
    url = os.getenv("OPENSEARCH_URL", "")
    if not url:
        return {"backend": "none", "reachable": False, "docs": 0,
                "detail": "OPENSEARCH_URL not set — historical search is unavailable."}
    try:
        from opensearchpy import OpenSearch
        kwargs = {"hosts": [url], "verify_certs": os.getenv("OPENSEARCH_VERIFY_CERTS", "true") != "false"}
        user, password = os.getenv("OPENSEARCH_USER", ""), os.getenv("OPENSEARCH_PASSWORD", "")
        if user and password and "@" not in url.split("//", 1)[-1]:
            kwargs["http_auth"] = (user, password)
        client = OpenSearch(**kwargs)
        prefix = os.getenv("OPENSEARCH_INDEX_PREFIX", "nyc_")
        stats = client.count(index=f"{prefix}*")
        return {"backend": "opensearch", "reachable": True,
                "docs": int(stats.get("count", 0)), "detail": ""}
    except Exception as e:
        logger.error("[RAG] OpenSearch unreachable: %s", e)
        return {"backend": "opensearch", "reachable": False, "docs": 0,
                "detail": f"{type(e).__name__}: {e}"}


async def _shutdown_nemo_agent():
    global _nemo_workflow, _nemo_builder_ctx, _nemo_builder
    if _nemo_builder_ctx is not None:
        try:
            await _nemo_builder_ctx.__aexit__(None, None, None)
        except Exception:
            pass
    _nemo_workflow = None
    _nemo_builder = None
    _nemo_builder_ctx = None


@asynccontextmanager
async def lifespan(application: FastAPI):
    await _init_nemo_agent()
    global _rag_status
    _rag_status = _check_rag()
    # Monitor stays disabled until incident mutation goes through the policy
    # gate — see architecture_review.md §4 and NAT_DEPLOYMENT_PLAN.md phase 4.
    # Enable with GRIDWATCH_MONITOR=1 once that lands.
    if os.getenv("GRIDWATCH_MONITOR") == "1":
        try:
            from hackathon_nyc.monitor_agent import start_monitor
            await start_monitor()
        except Exception as e:
            logger.error("[Monitor] Failed to start: %s", e)
    yield
    if os.getenv("GRIDWATCH_MONITOR") == "1":
        try:
            from hackathon_nyc.monitor_agent import stop_monitor
            await stop_monitor()
        except Exception:
            pass
    await _shutdown_nemo_agent()


app = FastAPI(title="NYC Urban Intelligence System", version="2.0.0", lifespan=lifespan)

# Lock CORS to the deployed dashboard when ALLOWED_ORIGINS is set. Left open
# for local development, where the dashboard is served from this same app.
_origins = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Optional shared secret for state-changing routes. Unset (the default) leaves
# the API open, which is fine locally and on a trusted network. Set it before
# putting this on a public URL: without it, anyone who finds the deployment can
# create, edit and delete incidents.
DISPATCHER_TOKEN = os.getenv("GRIDWATCH_TOKEN", "")

_MUTATING_PREFIXES = ("/api/incidents", "/api/alerts", "/api/webhook")


@app.middleware("http")
async def require_dispatcher_token(request: Request, call_next):
    if DISPATCHER_TOKEN and request.method in ("POST", "PUT", "PATCH", "DELETE"):
        path = request.url.path
        # Twilio and Discord authenticate by their own means and cannot send
        # this header; they are covered by the policy gate instead.
        if path.startswith(_MUTATING_PREFIXES) and not path.startswith("/api/webhook/twilio"):
            supplied = request.headers.get("X-GridWatch-Token", "")
            if not secrets.compare_digest(supplied, DISPATCHER_TOKEN):
                return JSONResponse({"detail": "Missing or invalid X-GridWatch-Token"},
                                    status_code=401)
    return await call_next(request)

FRONTEND_DIR = Path(__file__).parent / "frontend"

# Register Twilio voice + SMS routes (phone number for citizen reporting)
from hackathon_nyc.twilio_voice import register_twilio_routes
register_twilio_routes(app)


# ---------------------------------------------------------------------------
# Pydantic Models
# ---------------------------------------------------------------------------

class IncidentCreate(BaseModel):
    title: str
    category: str
    description: str = ""
    severity: str = "medium"
    latitude: float | None = None
    longitude: float | None = None
    address: str = ""
    borough: str = ""
    zip_code: str = ""
    source: str = "dispatcher"
    assigned_to: str = ""
    related_311_id: str = ""
    related_sensor_id: str = ""


class IncidentUpdate(BaseModel):
    status: str | None = None
    severity: str | None = None
    assigned_to: str | None = None
    notes: str | None = None
    message: str = ""
    updated_by: str = "dispatcher"


class AlertSubscribe(BaseModel):
    name: str
    contact: str
    contact_type: str = "sms"
    address: str = ""
    latitude: float | None = None
    longitude: float | None = None
    radius_miles: float = 1.0
    categories: str = ""


# ---------------------------------------------------------------------------
# Incident CRUD Endpoints
# ---------------------------------------------------------------------------

@app.post("/api/incidents")
def create_incident(data: IncidentCreate):
    """Create a new incident."""
    return db.create_incident(**data.model_dump())


@app.get("/api/incidents")
def list_incidents(
    status: str = "",
    category: str = "",
    borough: str = "",
    assigned_to: str = "",
    limit: int = 100,
):
    """List incidents with optional filters."""
    return db.list_incidents(status=status, category=category, borough=borough, assigned_to=assigned_to, limit=limit)


@app.get("/api/incidents/stats")
def get_stats():
    """Get incident statistics for the dashboard."""
    return db.get_stats()


@app.get("/api/incidents/{incident_id}")
def get_incident(incident_id: str):
    """Get a single incident by ID."""
    incident = db.get_incident(incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")
    return incident


@app.put("/api/incidents/{incident_id}")
def update_incident(incident_id: str, data: IncidentUpdate):
    """Update an incident (status, severity, assignment, notes)."""
    incident = db.update_incident(incident_id, **data.model_dump())
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")
    return incident


@app.post("/api/incidents/{incident_id}/confirm")
async def confirm_incident(incident_id: str):
    """Dispatcher confirms an incident — enables alert notifications and sends alerts."""
    result = db.confirm_incident(incident_id, confirmed_by="dispatcher")
    if not result:
        raise HTTPException(status_code=404, detail="Incident not found")

    # Same gate as every other alert path — confirmation alone is not
    # authorization; the kill switch and recipient cap apply here too.
    decision = policy.evaluate_alert(incident_id)
    result["alert_decision"] = decision.as_dict()
    if decision.allowed and decision.recipients:
        cat_emoji = {"flooding": "🌊", "sewer": "🚰", "noise": "🎵",
                     "rodent": "🐀", "heat": "🔥"}.get(result.get("category", ""), "⚠️")
        result["alerts_sent"] = await _send_alerts(
            decision.recipients,
            f"{cat_emoji} NYC Alert: {result['title']} near "
            f"{result.get('address', 'your area')[:60]}. #{result['id'][:8]}",
        )
        result["alerts_total"] = len(decision.recipients)
    else:
        result["alerts_sent"] = 0
        logger.info("[Alert] Suppressed for #%s: %s", incident_id[:8], decision.reason)

    return result


@app.delete("/api/incidents/{incident_id}")
def delete_incident(incident_id: str):
    """Delete an incident."""
    if not db.delete_incident(incident_id):
        raise HTTPException(status_code=404, detail="Incident not found")
    return {"deleted": True}


@app.get("/api/incidents/{incident_id}/history")
def get_incident_history(incident_id: str):
    """Get update history for an incident."""
    return db.get_incident_history(incident_id)


@app.get("/api/urgency/{text}")
def score_urgency(text: str):
    """Score the urgency of arbitrary text. Used by the frontend for display."""
    score, label = compute_urgency(text.lower())
    return {"urgency_score": score, "urgency_label": label}


# ---------------------------------------------------------------------------
# Alert Subscription Endpoints
# ---------------------------------------------------------------------------

@app.post("/api/alerts/subscribe")
async def subscribe_alerts(data: AlertSubscribe):
    """Subscribe to alerts for incidents near a location."""
    lat = data.latitude
    lon = data.longitude

    # Geocode address if no coordinates provided
    if not lat or not lon:
        if not data.address:
            raise HTTPException(status_code=400, detail="Provide address or lat/lon")
        import aiohttp
        async with aiohttp.ClientSession() as session:
            params = {"q": data.address, "format": "json", "limit": "1", "countrycodes": "us"}
            async with session.get("https://nominatim.openstreetmap.org/search",
                                   params=params,
                                   headers={"User-Agent": "HackathonNYC/1.0"}) as resp:
                results = await resp.json()
                if not results:
                    raise HTTPException(status_code=400, detail=f"Could not geocode '{data.address}'")
                lat = float(results[0]["lat"])
                lon = float(results[0]["lon"])

    return db.subscribe_alerts(
        name=data.name, contact=data.contact, contact_type=data.contact_type,
        latitude=lat, longitude=lon, address=data.address,
        radius_miles=data.radius_miles, categories=data.categories,
    )


@app.get("/api/alerts/subscriptions")
def list_subscriptions():
    """List all active alert subscriptions."""
    return db.list_subscriptions()


@app.delete("/api/alerts/{sub_id}")
def unsubscribe(sub_id: str):
    """Unsubscribe from alerts."""
    if not db.unsubscribe(sub_id):
        raise HTTPException(status_code=404, detail="Subscription not found")
    return {"unsubscribed": True}


@app.get("/api/alerts/check/{incident_id}")
def check_alerts_for_incident(incident_id: str):
    """Check which subscribers should be alerted for a given incident.

    Returns list of subscribers within their alert radius of the incident.
    This is what OpenClaw would call after an incident is created to know
    who to notify.
    """
    incident = db.get_incident(incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")
    if not incident.get("latitude") or not incident.get("longitude"):
        return []
    return db.find_subscribers_near(
        incident["latitude"], incident["longitude"], incident.get("category", ""),
    )


# ---------------------------------------------------------------------------
# Natural Language Urgency Scoring
# ---------------------------------------------------------------------------

URGENCY_KEYWORDS = {
    "critical": [
        "trapped", "emergency", "can't get out", "cant get out", "fire",
        "collapse", "collapsed", "gas leak", "children", "child", "kid",
        "elderly", "disabled", "unconscious", "drowning", "stuck inside",
        "can't breathe", "cant breathe", "explosion", "electrocution",
        "building falling", "structural collapse", "life threatening",
    ],
    "high": [
        "flooded basement", "no heat elderly", "structural damage",
        "large", "severe", "dangerous", "blocked road", "power out",
        "no electricity", "ceiling caving", "sewage overflow",
        "major", "massive", "water rising", "chest deep", "waist deep",
        "no heat", "no hot water", "carbon monoxide", "mold black",
    ],
    "medium": [
        "flooding", "broken", "leak", "backed up", "smell", "noise all night",
        "clogged", "overflowing", "puddle", "crack", "damage",
        "standing water", "dripping", "buzzing", "banging",
    ],
    "low": [
        "small", "minor", "little", "slight", "tiny",
    ],
}

URGENCY_SCORES = {"critical": 1.0, "high": 0.8, "medium": 0.5, "low": 0.2}


def compute_urgency(text_lower: str) -> tuple[float, str]:
    """Score the urgency of a citizen report based on keyword matching.
    Returns (score, label) where score is 0.0-1.0 and label is CRITICAL/HIGH/MEDIUM/LOW."""
    best_score = 0.2
    best_label = "LOW"
    hit_count = 0

    for level in ["critical", "high", "medium", "low"]:
        for kw in URGENCY_KEYWORDS[level]:
            if kw in text_lower:
                score = URGENCY_SCORES[level]
                hit_count += 1
                if score > best_score:
                    best_score = score
                    best_label = level.upper()

    # Boost slightly for multiple keyword hits (compound urgency)
    if hit_count >= 3 and best_score < 1.0:
        best_score = min(1.0, best_score + 0.1)
    if hit_count >= 5 and best_score < 1.0:
        best_score = min(1.0, best_score + 0.1)

    return round(best_score, 2), best_label


# ---------------------------------------------------------------------------
# OpenClaw / Discord Webhook — accepts messages, creates incidents
# ---------------------------------------------------------------------------

@app.post("/api/webhook/report")
async def webhook_report(request: Request):
    """Accept a report from any source (OpenClaw, Discord bot, etc).
    Body: { "message": "flooding at 200 Broadway Manhattan", "source": "discord", "user": "Colin#1234" }
    Geocodes the message, creates an incident, returns the result.
    """
    import aiohttp
    data = await request.json()
    message = data.get("message", "")
    source = data.get("source", "citizen_discord")
    user = data.get("user", "unknown")

    if not message:
        return {"error": "No message provided"}

    # Geocode: extract address from message
    lat, lon, address = None, None, ""
    try:
        from hackathon_nyc.tools.geocoding import geocode_address
        import re

        # Clean common transcription errors
        import re as _re
        fixed = _re.sub(r'\s+', ' ', message).strip()  # normalize whitespace
        fixed = fixed.replace(' and ', ' & ').replace(' AND ', ' & ')
        fixed = _re.sub(r'\.', ' ', fixed)  # remove ALL periods (STT adds them randomly)
        fixed = _re.sub(r'\s+', ' ', fixed).strip()  # re-normalize after period removal
        fixed = _re.sub(r'\b[Bb]looding\b', 'flooding', fixed)  # common Whisper error
        fixed = _re.sub(r'\b[Bb]leeding\b', 'flooding', fixed)  # another Whisper error
        fixed = _re.sub(r'\bin\b', ',', fixed)  # "in Manhattan" → ", Manhattan"
        fixed = _re.sub(r'\s+', ' ', fixed).strip()
        fixed = _re.sub(r'\$(\d+)\.00', r'\1', fixed)  # "$350.00" → "350"
        fixed = _re.sub(r'\$(\d+)', r'\1', fixed)       # "$350" → "350"
        # Fix Whisper merging "350 5th" → "355th": generate alternate split versions
        alt_splits = []
        for m in _re.finditer(r'\b(\d{3,})(st|nd|rd|th)\b', fixed, _re.IGNORECASE):
            num = m.group(1)
            suffix = m.group(2)
            # Try splitting at each position: "355" → "35 5", "3 55"
            for i in range(len(num)-1, 0, -1):
                left = num[:i]
                right = num[i:]
                right_suffix = {'1':'st','2':'nd','3':'rd'}.get(right[-1], 'th')
                alt = fixed[:m.start()] + left + ' ' + right + right_suffix + fixed[m.end():]
                alt_splits.append(alt)

        # Strategy 1: grab everything after the LAST "at"/"near"/"around", stop at noise
        after_prep = ""
        for prep in [' at ', ' near ', ' around ']:
            idx = fixed.lower().rfind(prep)
            if idx != -1:
                after_prep = fixed[idx + len(prep):].strip()
                # Cut at noise words that aren't part of the address
                cut = _re.search(r'\b(its?|it\'s|the water|water is|very|really|severe|bad|terrible|about|deep|inches|feet|foot|please|help|send|someone|nobody|done)\b', after_prep, _re.IGNORECASE)
                if cut:
                    after_prep = after_prep[:cut.start()].strip()
                    after_prep = _re.sub(r'[\s,]+$', '', after_prep)
                break

        # Strategy 2: try multiple approaches
        queries = []
        if after_prep:
            queries.append(after_prep + ', New York, NY')
        # Strategy 3: split on periods and strip noise words from each chunk
        noise = r'\b(flooding|flood|noise|loud|rats?|sewer|pothole|crash|heat|construction|report|there is|water|tree|fell|broken|damaged|its?|severe|bad|terrible|really|very|about|deep|inches|feet|foot)\b'
        for chunk in _re.split(r'[.!?]+', fixed):
            chunk = chunk.strip()
            if len(chunk) > 5 and any(c.isdigit() for c in chunk):
                clean_chunk = _re.sub(noise, ' ', chunk, flags=_re.IGNORECASE)
                clean_chunk = _re.sub(r'\s+', ' ', clean_chunk).strip()
                clean_chunk = _re.sub(r'^[\s,&]+|[\s,&]+$', '', clean_chunk)
                if len(clean_chunk) > 3:
                    queries.append(clean_chunk + ', New York, NY')
                queries.append(chunk + ', New York, NY')
        # Strategy 4: try split alternatives for merged ordinals
        for alt in alt_splits:
            queries.append(alt + ', New York, NY')
        queries.append(fixed + ', New York, NY')
        queries.append(message + ', New York, NY')

        for query in queries:
            if len(query) < 10:
                continue
            # Force NYC bounding box to prevent Buffalo/other city matches
            geo = await geocode_address(query)
            if "error" not in geo:
                # Verify it's in NYC proper (5 boroughs only)
                qlat, qlon = geo["lat"], geo["lon"]
                if 40.49 <= qlat <= 40.92 and -74.26 <= qlon <= -73.68:
                    lat, lon = qlat, qlon
                    address = geo.get("display_name", "")
                    break
                # Not in NYC — skip this result and try next query
    except Exception:
        pass

    # Guess category from keywords
    msg_lower = message.lower()
    category = "other"
    for keyword, cat in [("flood", "flooding"), ("water main", "flooding"), ("sewer", "sewer"),
                         ("gas leak", "sewer"), ("gas smell", "sewer"), ("noise", "noise"),
                         ("loud", "noise"), ("music", "noise"), ("party", "noise"),
                         ("rat", "rodent"), ("mouse", "rodent"), ("roach", "rodent"), ("pest", "rodent"),
                         ("heat", "heat"), ("hot water", "heat"), ("no heat", "heat"),
                         ("pothole", "street_condition"), ("road", "street_condition"), ("crack", "street_condition"),
                         ("crash", "street_condition"), ("accident", "street_condition"),
                         ("tree", "tree"), ("branch", "tree"),
                         ("water", "water"), ("hydrant", "water"), ("leak", "water"),
                         ("fire", "other"), ("smoke", "other"), ("construction", "noise")]:
        if keyword in msg_lower:
            category = cat
            break

    # --- Natural Language Urgency Scoring ---
    urgency_score, urgency_label = compute_urgency(msg_lower)
    # Map urgency to severity
    if urgency_score >= 0.9:
        severity = "critical"
    elif urgency_score >= 0.7:
        severity = "high"
    elif urgency_score >= 0.4:
        severity = "medium"
    else:
        severity = "low"

    incident = db.create_incident(
        title=message[:60],
        category=category,
        description=f"Report from {user}: {message}",
        severity=severity,
        source=f"citizen_{source}",
        latitude=lat, longitude=lon, address=address,
    )
    # Attach urgency metadata to response
    incident["urgency_score"] = urgency_score
    incident["urgency_label"] = urgency_label

    # Notify nearby subscribers — but only if the policy gate allows it.
    #
    # This used to text everyone in range the moment an incident was created,
    # with no confirmation check, so every citizen report bypassed the
    # anti-spam rule. A single false report produced real outbound SMS.
    decision = policy.evaluate_alert(incident["id"])
    incident["alert_decision"] = decision.as_dict()
    if decision.allowed and decision.recipients:
        sent = await _send_alerts(
            decision.recipients,
            f"⚠️ GRIDWATCH ALERT: {category.upper()} reported near {address[:60]}. "
            f"{urgency_label} severity. #{incident['id'][:8]}",
        )
        incident["alerts_sent"] = sent
    else:
        incident["alerts_sent"] = 0
        logger.info("[Alert] Suppressed for #%s: %s", incident["id"][:8], decision.reason)

    return incident


# ---------------------------------------------------------------------------
# Pipecat Voice Agent WebSocket (Twilio audio stream → live AI conversation)
# ---------------------------------------------------------------------------

from fastapi import WebSocket
import asyncio

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """Twilio streams call audio here → Pipecat pipeline processes it →
    STT (Whisper) → LLM (Nemotron) → TTS (Kokoro) → audio back to caller.
    """
    await websocket.accept()

    from pipecat.audio.vad.silero import SileroVADAnalyzer
    from pipecat.frames.frames import LLMRunFrame
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.runner import PipelineRunner
    from pipecat.pipeline.task import PipelineParams, PipelineTask
    from pipecat.processors.aggregators.llm_context import LLMContext
    from pipecat.processors.aggregators.llm_response_universal import (
        LLMContextAggregatorPair, LLMUserAggregatorParams,
    )
    from pipecat.services.ollama.llm import OLLamaLLMService
    from pipecat.adapters.schemas.function_schema import FunctionSchema
    from pipecat.adapters.schemas.tools_schema import ToolsSchema
    from pipecat.services.llm_service import FunctionCallParams
    from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams

    from hackathon_nyc.voice_agent import (
        handle_create_incident, handle_check_floods,
        handle_check_complaints, handle_subscribe_alerts,
        handle_get_incident_stats, tools, SYSTEM_PROMPT,
    )

    import os
    from pipecat.runner.utils import parse_telephony_websocket, _create_telephony_transport

    # Use Pipecat's official Twilio handshake parser
    transport_type, call_data = await parse_telephony_websocket(websocket)
    print(f"[Pipecat] Detected: {transport_type}, stream={call_data.get('stream_id')}")

    params = FastAPIWebsocketParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
    )
    transport = await _create_telephony_transport(websocket, params, transport_type, call_data)

    # STT
    try:
        from pipecat.services.whisper.stt import WhisperSTTService
        stt = WhisperSTTService(model_size="tiny.en")
    except ImportError:
        from pipecat.services.deepgram.stt import DeepgramSTTService
        import os
        stt = DeepgramSTTService(api_key=os.getenv("DEEPGRAM_API_KEY", ""))

    # TTS
    try:
        from pipecat.services.kokoro.tts import KokoroTTSService
        tts = KokoroTTSService(
            settings=KokoroTTSService.Settings(voice="af_bella"),
        )
    except ImportError:
        try:
            from pipecat.services.piper.tts import PiperTTSService
            tts = PiperTTSService()
        except ImportError:
            # Minimal fallback — will error but at least doesn't crash import
            raise ImportError("No TTS provider available. Install kokoro or piper: pip install 'pipecat-ai[kokoro]'")

    # LLM — Nemotron via Ollama
    llm = OLLamaLLMService(
        settings=OLLamaLLMService.Settings(
            model="nemotron-mini",
            system_instruction=SYSTEM_PROMPT,
        ),
    )

    # Register tool handlers
    llm.register_function("create_incident", handle_create_incident)
    llm.register_function("check_floods", handle_check_floods)
    llm.register_function("check_complaints", handle_check_complaints)
    llm.register_function("subscribe_alerts", handle_subscribe_alerts)
    llm.register_function("get_incident_stats", handle_get_incident_stats)

    context = LLMContext(
        messages=[{"role": "user", "content": "Greet me in one short sentence as a NYC dispatch operator."}],
    )
    user_agg, assistant_agg = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer()),
    )

    pipeline = Pipeline([
        transport.input(),
        stt,
        user_agg,
        llm,
        tts,
        transport.output(),
        assistant_agg,
    ])

    task = PipelineTask(pipeline, params=PipelineParams(enable_metrics=True))

    @transport.event_handler("on_client_connected")
    async def on_connected(t, client):
        print("[Pipecat] Client connected, triggering greeting")
        context.add_message({"role": "user", "content": "Greet the caller briefly."})
        await task.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def on_disconnected(t, client):
        print("[Pipecat] Client disconnected")
        # After call ends, extract what the caller said and create incident via webhook
        try:
            user_messages = [m["content"] for m in context.messages if m.get("role") == "user" and "greet" not in m["content"].lower()]
            if user_messages:
                full_report = " ".join(user_messages)
                print(f"[Pipecat] Creating incident from conversation: {full_report[:80]}")
                import aiohttp
                async with aiohttp.ClientSession() as session:
                    async with session.post("http://localhost:8000/api/webhook/report", json={
                        "message": full_report,
                        "source": "voice_pipecat",
                        "user": "phone_caller",
                    }) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            print(f"[Pipecat] Incident created: #{data.get('id')} lat={data.get('latitude')}")
                        else:
                            print(f"[Pipecat] Webhook failed: {resp.status}")
        except Exception as e:
            print(f"[Pipecat] Post-call incident creation failed: {e}")
        await task.cancel()

    print("[Pipecat] Starting pipeline runner...")
    runner = PipelineRunner(handle_sigint=False)
    await runner.run(task)
    print("[Pipecat] Pipeline runner finished")


# ---------------------------------------------------------------------------
# AI Chat — dispatcher command interface via the NAT workflow
# ---------------------------------------------------------------------------

CHAT_HISTORY: list[dict] = []

# Guards the module-level map-point buffer in register.py for the duration of a
# workflow run. Concurrent dispatcher questions queue rather than interleave.
_generate_lock = asyncio.Lock()


@app.get("/api/agent/status")
def agent_status():
    """Report what the reasoning path is actually doing, for the dashboard panel."""
    return {
        "agent": _nemo_workflow is not None,
        "state": _nat_status["state"],          # ready | degraded | not_started
        "detail": _nat_status["detail"],        # why, when degraded
        "config": _nat_status["config"],        # which YAML is live
        "tools": _nat_status["tools"],
        "rag": _rag_status["reachable"],
        "rag_backend": _rag_status["backend"],
        "rag_docs": _rag_status["docs"],
        "rag_detail": _rag_status["detail"],
        "mode": "nat" if _nemo_workflow else "degraded_readonly",
    }


def _degraded_answer(user_input: str) -> dict:
    """Read-only reply used when the NAT workflow is unavailable.

    Deliberately incapable of mutating anything. The previous fallback asked an
    LLM to emit a markdown ```action``` block which the server then parsed and
    executed — create, delete, confirm and resolve with no policy gate. That is
    gone. If the agent is down, GridWatch reports state and nothing more.
    """
    stats = db.get_stats()
    incidents = db.list_incidents(limit=10)
    lines = [
        "⚠️ Reasoning agent unavailable — read-only mode. "
        f"({_nat_status['detail'] or 'see /api/agent/status'})",
        "",
        f"Incidents: {stats.get('total', 0)} total · "
        f"{stats.get('open', 0)} open · {stats.get('resolved', 0)} resolved",
    ]
    by_cat = stats.get("by_category", {})
    if by_cat:
        top = sorted(by_cat.items(), key=lambda kv: -kv[1])[:5]
        lines.append("Top categories: " + ", ".join(f"{k} ({v})" for k, v in top))
    if incidents:
        lines.append("")
        lines.append("Most recent:")
        lines += [
            f"  • #{i['id'][:8]} {i.get('title', '?')[:48]} [{i.get('status', '?')}]"
            for i in incidents[:5]
        ]
    return {"output": "\n".join(lines), "rag_points": [], "mode": "degraded_readonly"}


@app.post("/generate")
async def generate_chat(request: Request):
    """Dispatcher chat — routed through the NAT workflow.

    The agent decides which tools to call: specialists, CRM, geocoding and the
    historical ChromaDB search. There are no trigger-word lists and no
    server-side action execution; incident mutations happen inside NAT tools.
    """
    data = await request.json()
    user_input = data.get("input", "").strip()
    if not user_input:
        return {"output": "No input provided."}

    if _nemo_workflow is None:
        return _degraded_answer(user_input)

    from hackathon_nyc.register import reset_map_points, get_map_points

    # Serialized: the map-point buffer the history tools write to is module
    # level, because NAT runs tools in a context the request handler cannot
    # reach. See the note in register.py.
    async with _generate_lock:
        return await _run_workflow(user_input, reset_map_points, get_map_points)


async def _run_workflow(user_input: str, reset_map_points, get_map_points) -> dict:
    reset_map_points()
    try:
        from nat.data_models.api_server import ChatRequest, Message, UserMessageContentRoleType

        messages = [
            Message(content=m["content"], role=UserMessageContentRoleType(m["role"]))
            for m in CHAT_HISTORY[-10:]
        ]
        messages.append(Message(content=user_input, role=UserMessageContentRoleType.USER))

        async def _run() -> str:
            # NAT 1.8: workflow.run() yields a Runner; result() converts output.
            async with _nemo_workflow.run(ChatRequest(messages=messages)) as runner:
                return await runner.result(to_type=str)

        output = await asyncio.wait_for(
            _run(), timeout=float(os.getenv("NAT_INVOKE_TIMEOUT", "120")),
        )

    except asyncio.TimeoutError:
        logger.error("[NAT] Invoke timed out for input: %.80s", user_input)
        return {"output": "The agent took too long to respond. Try a narrower question.",
                "rag_points": [], "mode": "timeout"}
    except Exception as e:
        logger.error("[NAT] Invoke failed: %s", e, exc_info=True)
        return {"output": f"Agent error: {e}", "rag_points": [], "mode": "error"}

    CHAT_HISTORY.append({"role": "user", "content": user_input})
    CHAT_HISTORY.append({"role": "assistant", "content": output})

    # Points recorded by history tools during this run, for the map layer.
    return {"output": output, "rag_points": get_map_points(), "mode": "nat"}


@app.get("/api/risk/{address:path}")
async def neighborhood_risk(address: str):
    """Generate a neighborhood risk score for a given address."""
    from hackathon_nyc.tools.geocoding import geocode_address
    from math import radians, sin, cos, asin, sqrt

    def _hav(la1, lo1, la2, lo2):
        R = 3958.8
        la1, lo1, la2, lo2 = map(radians, (la1, lo1, la2, lo2))
        return R * 2 * asin(sqrt(sin((la2-la1)/2)**2 + cos(la1)*cos(la2)*sin((lo2-lo1)/2)**2))

    # Geocode the address
    geo = await geocode_address(address + ", New York City, NY")
    if "error" in geo or not geo.get("lat"):
        return {"error": "Could not geocode address", "address": address}

    clat, clon = float(geo["lat"]), float(geo["lon"])
    display_addr = geo.get("display_name", address)
    radius_miles = 0.5

    # Query LIVE NYC Open Data APIs for this location
    import aiohttp
    risk_data = {"flooding": [], "rodent": [], "collision": [], "housing": [], "pothole": [], "noise": []}
    all_points = []

    async with aiohttp.ClientSession() as session:
        queries = [
            ("flooding", f"https://data.cityofnewyork.us/resource/erm2-nwe9.json?$limit=50&$order=created_date%20DESC&$where=complaint_type%20in('Sewer','Street%20Flooding','Water%20System')%20AND%20within_circle(location,{clat},{clon},800)&$select=latitude,longitude,complaint_type,created_date,descriptor"),
            ("rodent", f"https://data.cityofnewyork.us/resource/erm2-nwe9.json?$limit=50&$order=created_date%20DESC&$where=complaint_type='Rodent'%20AND%20within_circle(location,{clat},{clon},800)&$select=latitude,longitude,complaint_type,created_date"),
            ("collision", f"https://data.cityofnewyork.us/resource/h9gi-nx95.json?$limit=50&$order=crash_date%20DESC&$where=within_circle(location,{clat},{clon},800)&$select=latitude,longitude,number_of_persons_injured,number_of_persons_killed,crash_date"),
            ("housing", f"https://data.cityofnewyork.us/resource/wvxf-dwi5.json?$limit=50&$order=inspectiondate%20DESC&$where=class='C'%20AND%20within_circle(location,{clat},{clon},800)&$select=latitude,longitude,inspectiondate,novdescription"),
            ("pothole", f"https://data.cityofnewyork.us/resource/erm2-nwe9.json?$limit=50&$order=created_date%20DESC&$where=complaint_type='Street%20Condition'%20AND%20within_circle(location,{clat},{clon},800)&$select=latitude,longitude,complaint_type,created_date"),
            ("noise", f"https://data.cityofnewyork.us/resource/erm2-nwe9.json?$limit=50&$order=created_date%20DESC&$where=complaint_type%20in('Noise%20-%20Residential','Noise%20-%20Street/Sidewalk')%20AND%20within_circle(location,{clat},{clon},800)&$select=latitude,longitude,complaint_type,created_date"),
        ]
        for risk_key, url in queries:
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    data = await resp.json()
                    for d in data:
                        if d.get("latitude") and d.get("longitude"):
                            risk_data[risk_key].append(d)
                            all_points.append({"lat": float(d["latitude"]), "lon": float(d["longitude"]), "collection": risk_key})
            except Exception:
                pass

    # Also check dispatch DB
    incidents = db.list_incidents(limit=100)
    nearby_incidents = [i for i in incidents if i.get("latitude") and i.get("longitude")
                        and _hav(clat, clon, i["latitude"], i["longitude"]) <= radius_miles]

    # Score each category (0-100)
    def score_cat(count, thresholds=(2, 5, 10)):
        if count >= thresholds[2]: return 100
        if count >= thresholds[1]: return 75
        if count >= thresholds[0]: return 50
        if count >= 1: return 25
        return 0

    scores = {
        "flooding": score_cat(len(risk_data["flooding"]), (2, 4, 8)),
        "rodent": score_cat(len(risk_data["rodent"]), (2, 5, 10)),
        "collision": score_cat(len(risk_data["collision"]), (1, 3, 6)),
        "housing": score_cat(len(risk_data["housing"]), (2, 5, 10)),
        "pothole": score_cat(len(risk_data["pothole"]), (2, 4, 8)),
        "noise": score_cat(len(risk_data["noise"]), (3, 6, 12)),
    }

    risk_labels = {0: "NONE", 25: "LOW", 50: "MEDIUM", 75: "HIGH", 100: "CRITICAL"}
    overall = max(1, 100 - int(sum(scores.values()) / len(scores)))

    # Find correlations
    correlations = []
    if scores["rodent"] >= 50 and scores["noise"] >= 50:
        correlations.append("Noise + Rodents (16.7x correlation)")
    if scores["rodent"] >= 50 and scores["housing"] >= 50:
        correlations.append("Rodents + Housing violations (13.4x correlation)")
    if scores["pothole"] >= 50 and scores["collision"] >= 50:
        correlations.append("Potholes + Crashes (3.2x correlation)")
    if scores["flooding"] >= 50 and scores["rodent"] >= 50:
        correlations.append("Flooding + Rodents (6.9x correlation)")

    # Top concern
    top_key = max(scores, key=scores.get)
    top_concern = {"flooding": "Flooding/Sewer", "rodent": "Rodent Activity", "collision": "Vehicle Crashes",
                   "housing": "Housing Violations", "pothole": "Potholes", "noise": "Noise"}[top_key]

    # Build all points for map plotting
    all_points = []
    for key, pts in risk_data.items():
        for p in pts:
            all_points.append(p)

    return {
        "address": display_addr,
        "lat": clat, "lon": clon,
        "overall_score": overall,
        "overall_label": "SAFE" if overall >= 80 else "MODERATE RISK" if overall >= 50 else "HIGH RISK" if overall >= 25 else "CRITICAL",
        "categories": {k: {"score": v, "label": risk_labels.get(v, "?"), "count": len(risk_data.get(k, []))} for k, v in scores.items()},
        "nearby_dispatch_incidents": len(nearby_incidents),
        "correlations": correlations,
        "top_concern": top_concern,
        "rag_points": all_points,
    }


# ---------------------------------------------------------------------------
# Frontend
# ---------------------------------------------------------------------------

@app.get("/api/cameras")
async def get_cameras():
    """Proxy NYC DOT traffic cameras to avoid CORS issues."""
    import aiohttp
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://webcams.nyctmc.org/api/cameras/", timeout=aiohttp.ClientTimeout(total=10)) as resp:
                cameras = await resp.json()
        return [c for c in cameras if c.get("isOnline") == "true" and c.get("latitude") and c.get("longitude")]
    except Exception as e:
        return []

@app.get("/")
def serve_frontend():
    """Serve the map dashboard."""
    return FileResponse(FRONTEND_DIR / "index.html")
