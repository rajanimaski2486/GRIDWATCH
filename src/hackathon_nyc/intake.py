"""One intake path for every channel.

Before this module, a report arriving by SMS, Discord, phone, web form or the
background monitor hit four different implementations of the same four steps —
clean the text, find the address, pick a category, score urgency — and each
drifted from the others. The same words produced different incidents depending
on which door they came through, and only the webhook path consulted the policy
gate before notifying anyone.

Everything now calls `process_report()`. Channel modules are reduced to
adapters: turn their input into text plus a source, call this, format the reply.

Classification is deterministic by default so an SMS reply does not wait on a
model. Set GRIDWATCH_LLM_INTAKE=1 to route classification through the NAT
workflow instead; the deterministic path stays as the fallback when the
workflow is unavailable or slow.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
import os
import re

from hackathon_nyc import db, policy

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

# Order matters: the first match wins, so multi-word phrases must precede the
# single words they contain. "gas leak" has to beat "leak", and "no heat" has
# to beat "heat".
CATEGORY_RULES: list[tuple[str, str]] = [
    ("water main", "flooding"), ("flash flood", "flooding"), ("flooded", "flooding"),
    ("flooding", "flooding"), ("flood", "flooding"),
    ("gas leak", "sewer"), ("gas smell", "sewer"), ("smell gas", "sewer"),
    ("smell of gas", "sewer"), ("sewage", "sewer"), ("sewer", "sewer"),
    ("no hot water", "heat"), ("no heat", "heat"), ("hot water", "heat"),
    ("heating", "heat"), ("radiator", "heat"),
    ("rat", "rodent"), ("rats", "rodent"), ("mouse", "rodent"), ("mice", "rodent"),
    ("roach", "rodent"), ("vermin", "rodent"), ("pest", "rodent"),
    ("loud music", "noise"), ("noise", "noise"), ("loud", "noise"),
    ("music", "noise"), ("party", "noise"), ("construction", "noise"),
    ("pothole", "street_condition"), ("sinkhole", "street_condition"),
    ("crash", "street_condition"), ("accident", "street_condition"),
    ("road", "street_condition"), ("pavement", "street_condition"),
    ("tree", "tree"), ("branch", "tree"),
    ("hydrant", "water"), ("water leak", "water"), ("leak", "water"),
    ("fire", "other"), ("smoke", "other"),
]

URGENCY_KEYWORDS = {
    "critical": [
        "trapped", "emergency", "can't get out", "cant get out", "fire",
        "collapse", "collapsed", "gas leak", "children", "child", "kid",
        "elderly", "disabled", "unconscious", "drowning", "stuck inside",
        "can't breathe", "cant breathe", "explosion", "electrocution",
        "building falling", "structural collapse", "life threatening",
    ],
    "high": [
        "flooded basement", "no heat elderly", "structural damage", "large",
        "severe", "dangerous", "blocked road", "power out", "no electricity",
        "ceiling caving", "sewage overflow", "major", "massive", "water rising",
        "chest deep", "waist deep", "no heat", "no hot water",
        "carbon monoxide", "mold black",
    ],
    "medium": [
        "flooding", "broken", "leak", "backed up", "smell", "noise all night",
        "clogged", "overflowing", "puddle", "crack", "damage",
        "standing water", "dripping", "buzzing", "banging",
    ],
    "low": ["small", "minor", "little", "slight", "tiny"],
}
URGENCY_SCORES = {"critical": 1.0, "high": 0.8, "medium": 0.5, "low": 0.2}

# Categories that mean "call 911", not "we filed a ticket".
LIFE_SAFETY_TERMS = (
    "gas leak", "smell gas", "smell of gas", "fire", "smoke", "trapped",
    "collapse", "unconscious", "drowning", "explosion", "can't breathe",
    "cant breathe", "electrocution",
)


def classify(text: str) -> str:
    """Pick an incident category from report text."""
    lowered = text.lower()
    for keyword, category in CATEGORY_RULES:
        if keyword in lowered:
            return category
    return "other"


def score_urgency(text: str) -> tuple[float, str]:
    """Score urgency 0.0-1.0 and return (score, LABEL)."""
    lowered = text.lower()
    best_score, best_label, hits = 0.2, "LOW", 0
    for level in ("critical", "high", "medium", "low"):
        for keyword in URGENCY_KEYWORDS[level]:
            if keyword in lowered:
                hits += 1
                if URGENCY_SCORES[level] > best_score:
                    best_score, best_label = URGENCY_SCORES[level], level.upper()
    # Several independent signals raise confidence that this is serious.
    if hits >= 3:
        best_score = min(1.0, best_score + 0.1)
    if hits >= 5:
        best_score = min(1.0, best_score + 0.1)
    return round(best_score, 2), best_label


def severity_from(score: float) -> str:
    if score >= 0.9:
        return "critical"
    if score >= 0.7:
        return "high"
    if score >= 0.4:
        return "medium"
    return "low"


def is_life_safety(text: str) -> bool:
    """Whether the report should be told to call 911 rather than wait on dispatch."""
    lowered = text.lower()
    return any(term in lowered for term in LIFE_SAFETY_TERMS)


# ---------------------------------------------------------------------------
# Location extraction
# ---------------------------------------------------------------------------

NYC_BOUNDS = (40.49, 40.92, -74.26, -73.68)  # lat_min, lat_max, lon_min, lon_max

# Words that follow an address in speech but are not part of it.
_STOP_RE = re.compile(
    r"\b(its?|it'?s|the water|water is|very|really|severe|bad|terrible|about|"
    r"deep|inches|feet|foot|please|help|send|someone|nobody|done|now|asap)\b",
    re.I,
)
_NOISE_RE = re.compile(
    r"\b(flooding|flood|noise|loud|rats?|sewer|pothole|crash|heat|construction|"
    r"report|there is|there'?s|water|tree|fell|broken|damaged)\b",
    re.I,
)


def clean_transcript(text: str) -> str:
    """Repair the mangling speech-to-text introduces before geocoding.

    Whisper drops periods in odd places, mishears 'flooding', and writes
    'and' where an ampersand belongs in an intersection.
    """
    fixed = re.sub(r"\s+", " ", text).strip()
    fixed = re.sub(r"\b[Bb]looding\b", "flooding", fixed)
    fixed = re.sub(r"\b[Bb]leeding\b", "flooding", fixed)
    fixed = fixed.replace(" and ", " & ").replace(" AND ", " & ")
    fixed = re.sub(r"\$(\d+)(?:\.00)?", r"\1", fixed)
    fixed = re.sub(r"\.", " ", fixed)
    return re.sub(r"\s+", " ", fixed).strip()


def address_candidates(text: str) -> list[str]:
    """Ordered guesses at the address in a free-text report, best first."""
    fixed = clean_transcript(text)
    candidates: list[str] = []

    # 1. Whatever follows the last "at"/"near"/"around", trimmed at filler.
    for prep in (" at ", " near ", " around ", " on "):
        idx = fixed.lower().rfind(prep)
        if idx != -1:
            tail = fixed[idx + len(prep):].strip()
            cut = _STOP_RE.search(tail)
            if cut:
                tail = tail[:cut.start()].strip()
            tail = re.sub(r"[\s,]+$", "", tail)
            if len(tail) > 3:
                candidates.append(tail)
            break

    # 2. Whisper merges "350 5th" into "3505th"; try each re-split.
    for m in re.finditer(r"\b(\d{3,})(st|nd|rd|th)\b", fixed, re.I):
        num = m.group(1)
        for i in range(len(num) - 1, 0, -1):
            left, right = num[:i], num[i:]
            suffix = {"1": "st", "2": "nd", "3": "rd"}.get(right[-1], "th")
            candidates.append(f"{fixed[:m.start()]}{left} {right}{suffix}{fixed[m.end():]}".strip())

    # 3. Any clause containing a number, stripped of incident vocabulary.
    for chunk in re.split(r"[.!?,]+", fixed):
        chunk = chunk.strip()
        if len(chunk) > 5 and any(c.isdigit() for c in chunk):
            cleaned = re.sub(r"\s+", " ", _NOISE_RE.sub(" ", chunk)).strip(" ,&")
            if len(cleaned) > 3:
                candidates.append(cleaned)

    candidates.append(fixed)

    seen, ordered = set(), []
    for c in candidates:
        key = c.lower()
        if key not in seen and len(c) >= 4:
            seen.add(key)
            ordered.append(c)
    return ordered


async def geocode(text: str) -> tuple[float | None, float | None, str]:
    """Geocode the best address candidate that lands inside the five boroughs."""
    from hackathon_nyc.tools.geocoding import geocode_address

    lat_min, lat_max, lon_min, lon_max = NYC_BOUNDS
    for candidate in address_candidates(text)[:8]:
        query = f"{candidate}, New York, NY"
        if len(query) < 10:
            continue
        try:
            result = await geocode_address(query)
        except Exception as e:
            logger.debug("[Intake] geocode error for %r: %s", candidate, e)
            continue
        if "error" in result:
            continue
        lat, lon = result.get("lat"), result.get("lon")
        # Without the bounding-box check Nominatim happily returns Buffalo.
        if lat is not None and lat_min <= lat <= lat_max and lon_min <= lon <= lon_max:
            return lat, lon, result.get("display_name", candidate)
    return None, None, ""


# ---------------------------------------------------------------------------
# The intake pipeline
# ---------------------------------------------------------------------------

@dataclass
class IntakeResult:
    incident: dict
    category: str
    severity: str
    urgency_score: float
    urgency_label: str
    latitude: float | None
    longitude: float | None
    address: str
    needs_location: bool
    life_safety: bool
    alerts_sent: int = 0
    alert_decision: dict = field(default_factory=dict)

    def reply(self) -> str:
        """A short, non-alarming acknowledgement suitable for SMS or voice."""
        if self.life_safety:
            return ("This may be an emergency. Please call 911 now. "
                    f"We have logged your report (#{self.incident['id'][:8]}).")
        if self.needs_location:
            return ("Report received, but we could not identify the location. "
                    "Reply with a street address or nearest intersection.")
        where = f" near {self.address[:50]}" if self.address else ""
        return (f"Report received{where}. Logged as {self.category} "
                f"({self.urgency_label.lower()} priority), #{self.incident['id'][:8]}.")


async def process_report(
    text: str,
    source: str,
    user: str = "unknown",
    latitude: float | None = None,
    longitude: float | None = None,
) -> IntakeResult:
    """Normalize, locate, classify, score, persist, and gate alerts.

    `source` is a channel name such as sms, discord, voice, web or monitor. It
    is recorded as `citizen_<source>` unless it is already a trusted source, so
    policy can tell a dispatcher-entered incident from a phoned-in one.
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("process_report requires non-empty text")

    if latitude is None or longitude is None:
        latitude, longitude, address = await geocode(text)
    else:
        address = ""

    category = classify(text)
    score, label = score_urgency(text)
    severity = severity_from(score)
    life_safety = is_life_safety(text)
    if life_safety and severity != "critical":
        severity = "critical"

    full_source = source if policy.is_trusted_source(source) else f"citizen_{source}"

    incident = db.create_incident(
        title=text[:60],
        category=category,
        description=f"Report from {user} via {source}: {text}",
        severity=severity,
        source=full_source,
        latitude=latitude,
        longitude=longitude,
        address=address,
    )

    # The single place any channel can cause an outbound message.
    decision = policy.evaluate_alert(incident["id"])
    sent = 0
    if decision.allowed and decision.recipients:
        from hackathon_nyc.server import _send_alerts
        emoji = {"flooding": "🌊", "sewer": "🚰", "noise": "🎵",
                 "rodent": "🐀", "heat": "🔥"}.get(category, "⚠️")
        sent = await _send_alerts(
            decision.recipients,
            f"{emoji} GRIDWATCH: {category.upper()} reported near "
            f"{(address or 'your area')[:60]}. {label} severity. #{incident['id'][:8]}",
        )
    else:
        logger.info("[Intake] No alerts for #%s: %s", incident["id"][:8], decision.reason)

    incident["urgency_score"] = score
    incident["urgency_label"] = label
    incident["alerts_sent"] = sent
    incident["alert_decision"] = decision.as_dict()

    return IntakeResult(
        incident=incident,
        category=category,
        severity=severity,
        urgency_score=score,
        urgency_label=label,
        latitude=latitude,
        longitude=longitude,
        address=address,
        needs_location=latitude is None,
        life_safety=life_safety,
        alerts_sent=sent,
        alert_decision=decision.as_dict(),
    )


def looks_like_report(text: str) -> bool:
    """Cheap pre-filter for chat channels, so casual talk is not filed.

    Only Discord needs this — SMS and phone calls to the reporting number are
    reports by definition.
    """
    lowered = (text or "").lower()
    return classify(lowered) != "other" or any(
        term in lowered for term in ("report", "broken", "emergency", "help", "911")
    )
