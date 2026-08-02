"""Deterministic policy gate for GridWatch.

Every state mutation and every outbound notification passes through here. No
LLM calls live in this module and none should be added: a system prompt is a
suggestion, and the agent demonstrably refuses mass deletion because it was
asked nicely. This module is the part that holds when it isn't.

Two rules matter most:

1. Only CONFIRMED incidents may notify the public. Before this existed,
   /api/webhook/report texted every nearby subscriber the instant an incident
   was created, with no confirmation check — so every phone, SMS and Discord
   report bypassed the anti-spam rule that the CRM tool `check_alerts`
   correctly enforced.

2. Destructive and bulk actions require an explicit target. "Delete every open
   incident in Brooklyn" is not an explicit target.

Thresholds are read from configs/policy.yml so they are tunable without code
changes, matching how the rest of the workflow is configured.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import logging
import os

logger = logging.getLogger(__name__)

POLICY_PATH = Path(__file__).parent / "configs" / "policy.yml"

_DEFAULTS: dict = {
    # An incident notifies subscribers only when confirmed. Citizen reports
    # confirm after this many independent corroborating reports.
    "confirmation_reports_required": 3,
    # Sources trusted to create pre-confirmed incidents.
    "trusted_sources": ["dispatcher", "dispatcher_chat", "monitor_crossref"],
    # Sources that may never auto-confirm, whatever they claim about themselves.
    "untrusted_sources": ["citizen_sms", "citizen_discord", "citizen_voice", "citizen_web"],
    # Max subscribers a single incident may notify before a human signs off.
    "max_alert_recipients": 50,
    # Global kill switch. Set ALERTS_ENABLED=false to silence all outbound.
    "alerts_enabled": True,
    # Actions that always require an explicit incident id.
    "destructive_actions": ["delete", "resolve_all", "mass_alert"],
}


def _load_config() -> dict:
    cfg = dict(_DEFAULTS)
    try:
        if POLICY_PATH.exists():
            import yaml
            loaded = yaml.safe_load(POLICY_PATH.read_text()) or {}
            cfg.update(loaded)
    except Exception as e:  # a malformed policy file must not disable the gate
        logger.error("[Policy] Could not read %s, using defaults: %s", POLICY_PATH, e)
    # Env override wins — it is the deploy-time kill switch.
    if os.getenv("ALERTS_ENABLED", "").lower() in ("0", "false", "no"):
        cfg["alerts_enabled"] = False
    return cfg


@dataclass
class Decision:
    """Result of a policy check. `allowed` is the only field callers may act on."""

    allowed: bool
    reason: str
    requires_human: bool = False
    recipients: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "requires_human": self.requires_human,
            "recipient_count": len(self.recipients),
        }


# ---------------------------------------------------------------------------
# Alerts — the single chokepoint for anything that reaches the public
# ---------------------------------------------------------------------------

def evaluate_alert(incident_id: str) -> Decision:
    """Decide whether an incident may notify subscribers, and who.

    This is the ONLY function permitted to authorize outbound notification.
    Callers must send exactly to `decision.recipients` and only when
    `decision.allowed` is true.
    """
    from hackathon_nyc import db

    cfg = _load_config()

    if not cfg["alerts_enabled"]:
        return Decision(False, "Alerts are globally disabled (ALERTS_ENABLED=false).")

    incident = db.get_incident(incident_id)
    if not incident:
        return Decision(False, f"Incident {incident_id} not found.")

    if not incident.get("confirmed"):
        count = incident.get("report_count", 1)
        needed = cfg["confirmation_reports_required"]
        return Decision(
            False,
            f"Incident is unconfirmed ({count} of {needed} reports, no dispatcher "
            f"confirmation). Unconfirmed incidents never notify subscribers.",
        )

    lat, lon = incident.get("latitude"), incident.get("longitude")
    if lat is None or lon is None:
        return Decision(False, "Incident has no coordinates; cannot determine who is nearby.")

    recipients = db.find_subscribers_near(lat, lon, incident.get("category", ""))
    if not recipients:
        return Decision(True, "Confirmed, but no subscribers are within range.", recipients=[])

    if len(recipients) > cfg["max_alert_recipients"]:
        return Decision(
            False,
            f"{len(recipients)} recipients exceeds the {cfg['max_alert_recipients']} "
            f"cap for automatic sending. Needs dispatcher sign-off.",
            requires_human=True,
            recipients=recipients,
        )

    return Decision(True, f"Confirmed incident; {len(recipients)} subscribers in range.",
                    recipients=recipients)


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------

def evaluate_mutation(action: str, incident_id: str = "", source: str = "") -> Decision:
    """Decide whether a state-changing action may proceed."""
    cfg = _load_config()
    action = (action or "").strip().lower()

    if action in cfg["destructive_actions"] and not incident_id:
        return Decision(
            False,
            f"'{action}' requires a specific incident id. Bulk or unscoped "
            f"destructive actions are not permitted.",
            requires_human=True,
        )

    if action == "confirm" and source in cfg["untrusted_sources"]:
        return Decision(
            False,
            f"Source '{source}' may not confirm incidents. Confirmation comes "
            f"from a dispatcher or from corroborating reports.",
            requires_human=True,
        )

    return Decision(True, f"'{action}' permitted.")


def evaluate_confirmation(incident_id: str, actor: str = "agent") -> Decision:
    """Decide whether `actor` may confirm this incident.

    Confirmation is what unlocks alerts, so it is the second thing worth
    gating after alerting itself. A human dispatcher clicking confirm is
    exercising judgement and is allowed. The agent is not: left ungated it
    will helpfully confirm a single citizen report to "resolve" a request,
    which silently defeats the corroboration threshold.

    Found by `nat eval` — the duplicate-report scenario had the agent
    confirming a 2-report incident and announcing that subscribers would be
    notified, with the threshold set to 3.
    """
    from hackathon_nyc import db

    cfg = _load_config()
    incident = db.get_incident(incident_id)
    if not incident:
        return Decision(False, f"Incident {incident_id} not found.")

    if incident.get("confirmed"):
        return Decision(True, "Already confirmed.")

    if actor == "dispatcher":
        return Decision(True, "Confirmed by a dispatcher.")

    source = (incident.get("source") or "").strip().lower()
    count = incident.get("report_count", 1)
    needed = cfg["confirmation_reports_required"]

    if source in cfg["trusted_sources"]:
        return Decision(True, f"Trusted source '{source}'.")

    if count >= needed:
        return Decision(True, f"{count} independent reports meets the threshold of {needed}.")

    return Decision(
        False,
        f"Only a dispatcher may confirm this. It has {count} of {needed} required "
        f"reports and came from '{source or 'unknown'}'.",
        requires_human=True,
    )


def is_trusted_source(source: str) -> bool:
    """Whether a source may create incidents that are confirmed on arrival."""
    cfg = _load_config()
    source = (source or "").strip().lower()
    if source in cfg["untrusted_sources"]:
        return False
    return source in cfg["trusted_sources"]
