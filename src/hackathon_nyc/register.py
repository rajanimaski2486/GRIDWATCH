"""Register custom tool groups for the NYC Hackathon agents.

Registers four function groups + one parallel executor:
  - nyc_flood_tools: FloodNet sensor queries and flood data
  - nyc_311_tools: 311 complaint queries and aggregation
  - nyc_geo_tools: Geocoding and spatial utilities
  - nyc_crm_tools: Incident CRM for dispatchers (create, update, resolve, delete)
  - parallel_agent_query: Runs FloodWatch + 311 agents concurrently via asyncio.gather
"""

import asyncio
import json
from collections.abc import AsyncGenerator

from pydantic import Field

from nat.builder.builder import Builder
from nat.builder.function import FunctionGroup
from nat.builder.function_info import FunctionInfo
from nat.cli.register_workflow import register_function, register_function_group
from nat.data_models.component_ref import FunctionRef, RetrieverRef
from nat.data_models.function import FunctionBaseConfig, FunctionGroupBaseConfig

from hackathon_nyc.tools import nyc_opendata, floodnet, geocoding
from hackathon_nyc import db
from hackathon_nyc import policy
from hackathon_nyc import retrievers  # noqa: F401  (registers opensearch_retriever)


# ---------------------------------------------------------------------------
# Map-point side channel
#
# The historical search tool returns prose to the agent, but the dashboard also
# wants the underlying lat/lon records so it can plot them. Rather than
# re-running retrieval in the server (which is what the old trigger-word
# fallback did), the tool records what it found here and /generate reads it
# after the workflow completes.
#
# This is a plain module-level list, and /generate holds a lock across the
# whole workflow run so only one request fills it at a time.
#
# A ContextVar was tried first and does not work here: NAT executes tools in a
# context unrelated to the caller's, so a value set in the request handler is
# invisible inside the tool — and `Context.get().workflow_run_id` is None on the
# caller's side, so there is no shared key to correlate on either. Both were
# verified, not assumed.
#
# The cost is that concurrent /generate calls serialize. For a dispatcher
# console that is acceptable; the honest alternative is to read tool outputs
# from NAT's intermediate step stream, which `Workflow.result_with_steps()`
# should provide but which raises "await wasn't used with future" in 1.8.0.
# ---------------------------------------------------------------------------
_MAP_POINTS: list = []


def reset_map_points() -> list:
    """Clear the point buffer. Call under the lock, before invoking the workflow."""
    _MAP_POINTS.clear()
    return _MAP_POINTS


def get_map_points() -> list:
    """Return map points recorded by history tools during the last run."""
    return list(_MAP_POINTS)



# ---------------------------------------------------------------------------
# Tool output budget
#
# An unbounded tool return can end a conversation outright: during a `nat eval`
# run one call came back with 125,953 tokens against the model's 128,000 limit
# and the request was rejected. Tools return JSON for a model to read, not a
# data export, so every payload is capped and says so when it truncates.
# ---------------------------------------------------------------------------
MAX_TOOL_CHARS = 12000


def _json_capped(payload, max_chars: int = MAX_TOOL_CHARS) -> str:
    """Serialize for the model, trimming list payloads until they fit."""
    text = json.dumps(payload, indent=2, default=str)
    if len(text) <= max_chars:
        return text

    if isinstance(payload, list):
        kept = list(payload)
        while kept and len(json.dumps(kept, indent=2, default=str)) > max_chars - 200:
            kept = kept[:max(1, len(kept) * 3 // 4)]
        return json.dumps(
            {"truncated": True,
             "showing": len(kept),
             "of": len(payload),
             "note": "Result set trimmed to fit the model context. Narrow the query for more.",
             "results": kept},
            indent=2, default=str)

    return text[:max_chars] + '\n... [truncated to fit model context]'


# ---------------------------------------------------------------------------
# FloodNet / Environmental Tools
# ---------------------------------------------------------------------------

class FloodToolConfig(FunctionGroupBaseConfig, name="nyc_flood_tools"):
    include: list[str] = Field(
        default_factory=lambda: [
            "get_active_floods",
            "get_flood_sensors",
            "get_worst_floods",
            "get_flood_history",
            "get_flood_vulnerability",
            "get_air_quality",
            "query_nyc_dataset",
        ],
        description="Flood and environmental monitoring tools",
    )


@register_function_group(config_type=FloodToolConfig)
async def nyc_flood_tools(_config: FloodToolConfig, _builder: Builder) -> AsyncGenerator[FunctionGroup, None]:
    group = FunctionGroup(config=_config)

    async def _get_active_floods(hours_back: int = 24) -> str:
        """Get flooding events from the last N hours from FloodNet sensors across NYC."""
        result = await floodnet.get_active_floods(hours_back)
        return _json_capped(result[:20])

    async def _get_flood_sensors(unused: str = "") -> str:
        """Get all FloodNet sensor deployment locations and coordinates across NYC."""
        del unused  # NAT requires single_fn to take exactly one parameter
        result = await floodnet.get_sensor_locations()
        return _json_capped(result)

    async def _get_worst_floods(top_n: int = 10) -> str:
        """Get the worst flooding events by maximum water depth in inches."""
        result = await floodnet.get_worst_floods(top_n)
        return _json_capped(result)

    async def _get_flood_history(sensor_id: str) -> str:
        """Get flood history for a specific FloodNet sensor by its sensor ID."""
        result = await floodnet.get_flood_history_for_sensor(sensor_id)
        return _json_capped(result[:20])

    async def _get_flood_vulnerability(limit: int = 50) -> str:
        """Get flood vulnerability index scores by NYC neighborhood."""
        result = await nyc_opendata.get_flood_vulnerability(limit)
        return _json_capped(result)

    async def _get_air_quality(neighborhood: str = "") -> str:
        """Get air quality data (PM2.5, NO2) by NYC neighborhood. Optionally filter by neighborhood name."""
        result = await nyc_opendata.get_air_quality(neighborhood)
        return _json_capped(result[:20])

    async def _query_nyc_dataset(dataset_key: str, where_clause: str = "", limit: int = 50) -> str:
        """Query any NYC Open Data dataset by key. Available datasets: 311_current, air_quality, flood_events, flood_sensors, flood_vulnerability, heat_vulnerability, street_trees, greenhouse_gas, community_gardens, pluto."""
        result = await nyc_opendata.query_dataset(dataset_key, where_clause=where_clause, limit=limit)
        return _json_capped(result[:20])

    group.add_function(name="get_active_floods", fn=_get_active_floods, description=_get_active_floods.__doc__)
    group.add_function(name="get_flood_sensors", fn=_get_flood_sensors, description=_get_flood_sensors.__doc__)
    group.add_function(name="get_worst_floods", fn=_get_worst_floods, description=_get_worst_floods.__doc__)
    group.add_function(name="get_flood_history", fn=_get_flood_history, description=_get_flood_history.__doc__)
    group.add_function(name="get_flood_vulnerability", fn=_get_flood_vulnerability, description=_get_flood_vulnerability.__doc__)
    group.add_function(name="get_air_quality", fn=_get_air_quality, description=_get_air_quality.__doc__)
    group.add_function(name="query_nyc_dataset", fn=_query_nyc_dataset, description=_query_nyc_dataset.__doc__)

    yield group


# ---------------------------------------------------------------------------
# 311 Complaint / Human Impact Tools
# ---------------------------------------------------------------------------

class ThreeOneOneToolConfig(FunctionGroupBaseConfig, name="nyc_311_tools"):
    include: list[str] = Field(
        default_factory=lambda: [
            "get_311_complaints",
            "get_311_stats",
            "get_311_by_location",
            "search_311_by_keyword",
        ],
        description="NYC 311 service request tools",
    )


@register_function_group(config_type=ThreeOneOneToolConfig)
async def nyc_311_tools(_config: ThreeOneOneToolConfig, _builder: Builder) -> AsyncGenerator[FunctionGroup, None]:
    group = FunctionGroup(config=_config)

    async def _get_311_complaints(complaint_type: str = "", borough: str = "", zip_code: str = "", limit: int = 20) -> str:
        """Get recent 311 service requests. Filter by complaint_type (e.g. 'Noise - Residential', 'Sewer', 'Rodent', 'HEAT/HOT WATER'), borough (e.g. 'BROOKLYN'), or zip_code."""
        result = await nyc_opendata.get_311_complaints(complaint_type, borough, zip_code, limit)
        return _json_capped(result)

    async def _get_311_stats(complaint_type: str = "", borough: str = "", group_by: str = "complaint_type") -> str:
        """Get aggregated 311 complaint statistics. Returns counts grouped by complaint_type, borough, or other field."""
        result = await nyc_opendata.get_311_complaint_stats(complaint_type, borough, group_by)
        return _json_capped(result)

    async def _get_311_by_location(lat: float, lon: float, radius_meters: int = 500, limit: int = 20) -> str:
        """Get 311 complaints near a specific lat/lon location within a radius in meters."""
        where = f"within_circle(location, {lat}, {lon}, {radius_meters})"
        result = await nyc_opendata.query_dataset(
            "311_current",
            where_clause=where,
            select="unique_key,created_date,complaint_type,descriptor,latitude,longitude,status",
            limit=limit,
        )
        return _json_capped(result)

    async def _search_311_by_keyword(keyword: str, limit: int = 20) -> str:
        """Search 311 complaints by keyword in the descriptor field."""
        where = f"upper(descriptor) like '%{keyword.upper()}%'"
        result = await nyc_opendata.query_dataset(
            "311_current",
            where_clause=where,
            select="unique_key,created_date,complaint_type,descriptor,borough,latitude,longitude,status",
            limit=limit,
        )
        return _json_capped(result)

    group.add_function(name="get_311_complaints", fn=_get_311_complaints, description=_get_311_complaints.__doc__)
    group.add_function(name="get_311_stats", fn=_get_311_stats, description=_get_311_stats.__doc__)
    group.add_function(name="get_311_by_location", fn=_get_311_by_location, description=_get_311_by_location.__doc__)
    group.add_function(name="search_311_by_keyword", fn=_search_311_by_keyword, description=_search_311_by_keyword.__doc__)

    yield group


# ---------------------------------------------------------------------------
# Geocoding / Spatial Tools
# ---------------------------------------------------------------------------

class GeoToolConfig(FunctionGroupBaseConfig, name="nyc_geo_tools"):
    include: list[str] = Field(
        default_factory=lambda: ["geocode_address", "reverse_geocode", "find_nearest_sensors"],
        description="Geocoding and spatial utility tools",
    )


@register_function_group(config_type=GeoToolConfig)
async def nyc_geo_tools(_config: GeoToolConfig, _builder: Builder) -> AsyncGenerator[FunctionGroup, None]:
    group = FunctionGroup(config=_config)

    async def _geocode_address(address: str) -> str:
        """Convert a NYC street address to lat/lon coordinates."""
        result = await geocoding.geocode_address(address)
        return json.dumps(result, indent=2)

    async def _reverse_geocode(lat: float, lon: float) -> str:
        """Convert lat/lon coordinates to a street address."""
        result = await geocoding.reverse_geocode(lat, lon)
        return json.dumps(result, indent=2)

    async def _find_nearest_sensors(lat: float, lon: float, top_n: int = 5) -> str:
        """Find the nearest FloodNet sensors to a given lat/lon location."""
        sensors = await floodnet.get_sensor_locations()
        nearest = geocoding.find_nearest_points(lat, lon, sensors, top_n)
        return json.dumps(nearest, indent=2, default=str)

    # A `historical_lookup` tool used to be registered here against ChromaDB.
    # It was already excluded by the include list above, so nothing could call
    # it; retrieval now lives in nyc_history_tools over OpenSearch.

    group.add_function(name="geocode_address", fn=_geocode_address, description=_geocode_address.__doc__)
    group.add_function(name="reverse_geocode", fn=_reverse_geocode, description=_reverse_geocode.__doc__)
    group.add_function(name="find_nearest_sensors", fn=_find_nearest_sensors, description=_find_nearest_sensors.__doc__)

    yield group


# ---------------------------------------------------------------------------
# CRM / Incident Management Tools (Dispatcher Interface)
# ---------------------------------------------------------------------------

class CRMToolConfig(FunctionGroupBaseConfig, name="nyc_crm_tools"):
    include: list[str] = Field(
        default_factory=lambda: [
            "create_incident",
            "list_incidents",
            "update_incident",
            "resolve_incident",
            "delete_incident",
            "get_incident",
            "get_incident_stats",
            # Alert subscription + confirmation tools. These MUST stay included:
            # check_alerts is the only code path that enforces the anti-spam
            # rule that unconfirmed incidents never notify subscribers.
            "subscribe_alerts",
            "list_subscriptions",
            "check_alerts",
            "confirm_incident",
            "unsubscribe",
            # Deterministic policy gate, exposed so the agent can check before
            # acting. delete_incident enforces it regardless.
            "check_mutation_allowed",
        ],
        description="Incident CRM tools for dispatchers to manage events on the map",
    )


@register_function_group(config_type=CRMToolConfig)
async def nyc_crm_tools(_config: CRMToolConfig, _builder: Builder) -> AsyncGenerator[FunctionGroup, None]:
    group = FunctionGroup(config=_config)

    async def _create_incident(
        title: str,
        category: str,
        description: str = "",
        severity: str = "medium",
        latitude: float = None,
        longitude: float = None,
        address: str = "",
        borough: str = "",
        zip_code: str = "",
        assigned_to: str = "",
    ) -> str:
        """Create a new incident on the map. Categories: flooding, sewer, noise, rodent, heat, air_quality, street_condition, water, tree, other. Severity: low, medium, high, critical."""
        result = db.create_incident(
            title=title, category=category, description=description,
            severity=severity, latitude=latitude, longitude=longitude,
            address=address, borough=borough, zip_code=zip_code,
            assigned_to=assigned_to, source="agent",
        )
        return _json_capped(result)

    async def _list_incidents(status: str = "", category: str = "", borough: str = "", limit: int = 50) -> str:
        """List all incidents, optionally filtered by status (open, in_progress, resolved), category, or borough."""
        result = db.list_incidents(status=status, category=category, borough=borough, limit=limit)
        return _json_capped(result)

    async def _update_incident(
        incident_id: str,
        status: str = "",
        severity: str = "",
        assigned_to: str = "",
        notes: str = "",
        message: str = "",
    ) -> str:
        """Update an existing incident. Change status (open, in_progress, resolved), severity, assignment, or add notes."""
        result = db.update_incident(
            incident_id,
            status=status or None,
            severity=severity or None,
            assigned_to=assigned_to if assigned_to else None,
            notes=notes or None,
            message=message,
            updated_by="agent",
        )
        if not result:
            return json.dumps({"error": f"Incident {incident_id} not found"})
        return _json_capped(result)

    async def _resolve_incident(incident_id: str, resolution_notes: str = "") -> str:
        """Mark an incident as resolved with optional resolution notes."""
        result = db.update_incident(
            incident_id,
            status="resolved",
            notes=resolution_notes or None,
            message="Incident resolved",
            updated_by="agent",
        )
        if not result:
            return json.dumps({"error": f"Incident {incident_id} not found"})
        return _json_capped(result)

    async def _check_mutation_allowed(action: str, incident_id: str = "") -> str:
        """Check whether a state-changing action is permitted before doing it. action is one of: delete, resolve_all, mass_alert, confirm, update. Bulk or unscoped destructive actions are refused. Call this before any delete or bulk operation."""
        decision = policy.evaluate_mutation(action, incident_id)
        return json.dumps(decision.as_dict(), indent=2, default=str)

    async def _delete_incident(incident_id: str) -> str:
        """Delete a single incident by its exact ID. Requires a specific ID — bulk deletion is not supported."""
        # The gate runs here too, not only in check_mutation_allowed. A tool
        # the model can skip is not a control.
        decision = policy.evaluate_mutation("delete", incident_id)
        if not decision.allowed:
            return json.dumps({"deleted": False, "refused": decision.reason}, indent=2)
        success = db.delete_incident(incident_id)
        if not success:
            return json.dumps({"error": f"Incident {incident_id} not found"})
        return json.dumps({"deleted": True, "id": incident_id})

    async def _get_incident(incident_id: str) -> str:
        """Get full details and history for a specific incident by its ID."""
        incident = db.get_incident(incident_id)
        if not incident:
            return json.dumps({"error": f"Incident {incident_id} not found"})
        history = db.get_incident_history(incident_id)
        incident["history"] = history
        return json.dumps(incident, indent=2, default=str)

    async def _get_incident_stats(unused: str = "") -> str:
        """Get dashboard statistics: counts by status, category, borough, severity."""
        del unused  # NAT requires single_fn to take exactly one parameter
        result = db.get_stats()
        return _json_capped(result)

    async def _subscribe_alerts(
        name: str,
        contact: str,
        address: str,
        contact_type: str = "sms",
        radius_miles: float = 1.0,
        categories: str = "",
    ) -> str:
        """Subscribe a person to alerts for incidents near their address. They'll be notified when new incidents happen within their radius. contact_type: sms, whatsapp, email, discord. categories: comma-separated filter (e.g. 'flooding,sewer') or empty for all."""
        # Geocode the address first
        geo_result = await geocoding.geocode_address(address)
        if "error" in geo_result:
            return json.dumps({"error": f"Could not geocode address: {geo_result['error']}"})
        result = db.subscribe_alerts(
            name=name, contact=contact, contact_type=contact_type,
            latitude=geo_result["lat"], longitude=geo_result["lon"],
            address=address, radius_miles=radius_miles, categories=categories,
        )
        return _json_capped(result)

    async def _list_subscriptions(unused: str = "") -> str:
        """List all active alert subscriptions."""
        del unused  # NAT requires single_fn to take exactly one parameter
        result = db.list_subscriptions()
        return _json_capped(result)

    async def _check_alerts(incident_id: str) -> str:
        """Check whether an incident may alert subscribers, and who would be notified. Only confirmed incidents trigger alerts. Incidents are confirmed by dispatchers or auto-confirmed after enough independent reports. Call this before telling anyone that notifications were sent."""
        # Delegates to the deterministic gate so the agent, the webhook and the
        # confirm endpoint cannot disagree about who may be notified.
        decision = policy.evaluate_alert(incident_id)
        incident = db.get_incident(incident_id)
        return json.dumps({
            "incident_id": incident_id,
            "incident_title": (incident or {}).get("title", ""),
            "alert_allowed": decision.allowed,
            "reason": decision.reason,
            "requires_human_approval": decision.requires_human,
            "subscribers_to_alert": decision.recipients if decision.allowed else [],
            "count": len(decision.recipients) if decision.allowed else 0,
        }, indent=2, default=str)

    async def _confirm_incident(incident_id: str) -> str:
        """Confirm an incident so it can alert nearby subscribers. Only permitted when the incident already has enough independent reports or came from a trusted source; otherwise a human dispatcher must confirm it."""
        # actor="agent": you are not a dispatcher. Confirmation unlocks
        # outbound alerts, so the corroboration threshold has to hold even
        # when confirming would neatly resolve the request.
        decision = policy.evaluate_confirmation(incident_id, actor="agent")
        if not decision.allowed:
            return json.dumps({"confirmed": False, "refused": decision.reason,
                               "requires_human": decision.requires_human}, indent=2)
        result = db.confirm_incident(incident_id, confirmed_by="agent")
        if not result:
            return json.dumps({"error": f"Incident {incident_id} not found"})
        return _json_capped(result)

    async def _unsubscribe(subscription_id: str) -> str:
        """Unsubscribe a person from alerts by their subscription ID."""
        success = db.unsubscribe(subscription_id)
        if not success:
            return json.dumps({"error": f"Subscription {subscription_id} not found"})
        return json.dumps({"unsubscribed": True, "id": subscription_id})

    group.add_function(name="create_incident", fn=_create_incident, description=_create_incident.__doc__)
    group.add_function(name="list_incidents", fn=_list_incidents, description=_list_incidents.__doc__)
    group.add_function(name="update_incident", fn=_update_incident, description=_update_incident.__doc__)
    group.add_function(name="resolve_incident", fn=_resolve_incident, description=_resolve_incident.__doc__)
    group.add_function(name="delete_incident", fn=_delete_incident, description=_delete_incident.__doc__)
    group.add_function(name="get_incident", fn=_get_incident, description=_get_incident.__doc__)
    group.add_function(name="get_incident_stats", fn=_get_incident_stats, description=_get_incident_stats.__doc__)
    group.add_function(name="subscribe_alerts", fn=_subscribe_alerts, description=_subscribe_alerts.__doc__)
    group.add_function(name="list_subscriptions", fn=_list_subscriptions, description=_list_subscriptions.__doc__)
    group.add_function(name="check_alerts", fn=_check_alerts, description=_check_alerts.__doc__)
    group.add_function(name="confirm_incident", fn=_confirm_incident, description=_confirm_incident.__doc__)
    group.add_function(name="unsubscribe", fn=_unsubscribe, description=_unsubscribe.__doc__)
    group.add_function(name="check_mutation_allowed", fn=_check_mutation_allowed, description=_check_mutation_allowed.__doc__)

    yield group


# ---------------------------------------------------------------------------
# Historical RAG Tools (ChromaDB)
#
# NAT 1.8 ships only `milvus_retriever` and `nemo_retriever` retriever
# providers — there is no Chroma provider — so the six local collections are
# exposed as an ordinary function group instead of a `retrievers:` block.
# ---------------------------------------------------------------------------

TOPIC_INDICES = {
    "flood": "nyc_flood_events",
    "rodent": "nyc_rodent_inspections",
    "pothole": "nyc_potholes",
    "collision": "nyc_collisions",
    "housing": "nyc_housing_violations",
    "311": "nyc_311_current",
}


class HistoryToolConfig(FunctionGroupBaseConfig, name="nyc_history_tools"):
    retriever: RetrieverRef = Field(
        description="Retriever holding the historical NYC Open Data indices",
    )
    top_k: int = Field(default=6, gt=0, description="Records to return per search")
    include: list[str] = Field(
        default_factory=lambda: [
            "search_history",
            "search_history_by_topic",
        ],
        description="Historical NYC Open Data retrieval",
    )


@register_function_group(config_type=HistoryToolConfig)
async def nyc_history_tools(_config: HistoryToolConfig, _builder: Builder) -> AsyncGenerator[FunctionGroup, None]:
    group = FunctionGroup(config=_config)
    retriever = await _builder.get_retriever(_config.retriever)

    def _format(docs, **extra) -> str:
        """Render results for the agent and record map points as a side effect.

        Coordinates come from indexed `lat`/`lon` fields. The ChromaDB version
        had to regex them back out of concatenated chunk text, because it
        stored five records per document with no structured metadata.
        """
        records, points = [], []
        for d in docs:
            meta = d.metadata or {}
            records.append({
                "source": meta.get("dataset") or meta.get("_index", ""),
                "date": meta.get("date", ""),
                "text": d.page_content[:300],
            })
            lat, lon = meta.get("lat"), meta.get("lon")
            if lat is not None and lon is not None:
                points.append({
                    "lat": float(lat),
                    "lon": float(lon),
                    "collection": meta.get("_index") or meta.get("dataset", ""),
                    "label": (meta.get("label") or d.page_content)[:120],
                })
        if points:
            _MAP_POINTS.extend(points)
        return json.dumps({"found": len(records), "records": records, **extra},
                          indent=2, default=str)

    async def _search_history(query: str) -> str:
        """Search historical NYC Open Data records (311 complaints, collisions, potholes, rodent inspections, housing violations, flood events) for anything relevant to the query. Use this for questions about the past, trends, repeat locations, or whether something has happened before."""
        out = await retriever.search(query, top_k=_config.top_k)
        return _format(out.results)

    async def _search_history_by_topic(query: str, topic: str) -> str:
        """Search historical NYC records limited to one topic. topic must be one of: flood, rodent, pothole, collision, housing, 311. Use when the question is clearly about a single domain."""
        index = TOPIC_INDICES.get(topic.strip().lower())
        if index is None:
            return json.dumps({"error": f"Unknown topic '{topic}'. Use one of: {', '.join(TOPIC_INDICES)}"})
        out = await retriever.search(query, index_name=index, top_k=_config.top_k)
        return _format(out.results, topic=topic)

    group.add_function(name="search_history", fn=_search_history, description=_search_history.__doc__)
    group.add_function(name="search_history_by_topic", fn=_search_history_by_topic, description=_search_history_by_topic.__doc__)

    yield group


# ---------------------------------------------------------------------------
# Analyst Tools
#
# correlation_analysis.py and backtest_predictions.py were terminal scripts —
# real data work the agent had no way to reach. These expose them.
# ---------------------------------------------------------------------------

class AnalystToolConfig(FunctionGroupBaseConfig, name="nyc_analyst_tools"):
    include: list[str] = Field(
        default_factory=lambda: [
            "get_correlation_findings",
            "get_prediction_accuracy",
            "explain_risk_score",
        ],
        description="Cross-dataset correlations, prediction backtest, risk scoring",
    )


@register_function_group(config_type=AnalystToolConfig)
async def nyc_analyst_tools(_config: AnalystToolConfig, _builder: Builder) -> AsyncGenerator[FunctionGroup, None]:
    from hackathon_nyc import analysis

    group = FunctionGroup(config=_config)

    async def _get_correlation_findings(topic: str = "") -> str:
        """Cross-dataset correlation findings for NYC incident types, measured against a random baseline. Optionally filter by topic such as rodent, flooding, noise, housing, crash or pothole. Use when asked why incident types cluster, or what tends to co-occur with something."""
        found = analysis.find_correlations(topic)
        if not found:
            return json.dumps({"found": 0,
                               "message": f"No correlation findings for '{topic}'." if topic
                               else "No correlation analysis available."})
        return json.dumps({
            "found": len(found),
            "note": "Ratio is how much more often the second type appears within "
                    "0.25 km of the first than near a random NYC point.",
            "findings": [c.as_dict() for c in found[:6]],
        }, indent=2, default=str)

    async def _get_prediction_accuracy(complaint_type: str = "") -> str:
        """How reliably the grid-based model predicts 311 complaints, from a backtest on 672k training and 158k test records. Optionally name a complaint type. Use when asked what can be predicted or how good the predictions are."""
        data = analysis.load_backtest()
        if not data.get("available"):
            return json.dumps({"available": False, "message": "No backtest results available."})
        if complaint_type:
            needle = complaint_type.strip().lower()
            matches = [t for t in data["by_type"] if needle in t["complaint_type"].lower()]
            return json.dumps({
                "complaint_type": complaint_type,
                "matches": matches[:5],
                "overall_hit_rate": data["overall_hit_rate"],
                "caveat": data["caveat"],
            }, indent=2, default=str)
        return json.dumps({
            "overall_hit_rate": data["overall_hit_rate"],
            "false_positive_rate": data["false_positive_rate"],
            "most_predictable": data["by_type"][:10],
            "caveat": data["caveat"],
        }, indent=2, default=str)

    async def _explain_risk_score(address: str, radius_km: float = 0.8) -> str:
        """Score infrastructure risk for a NYC address 0-100 and explain what drives it, using live incidents nearby. Use when asked how risky, dangerous or problem-prone a location is."""
        geo = await geocoding.geocode_address(f"{address}, New York, NY")
        if "error" in geo:
            return json.dumps({"error": f"Could not geocode '{address}'"})
        lat, lon = geo["lat"], geo["lon"]

        # Count open incidents near the point, by category. geocoding's
        # haversine returns MILES; the analysis and this tool speak km.
        radius_miles = radius_km * 0.621371
        counts: dict[str, int] = {}
        for inc in db.list_incidents(limit=500):
            ilat, ilon = inc.get("latitude"), inc.get("longitude")
            if ilat is None or ilon is None:
                continue
            if geocoding.haversine_distance(lat, lon, ilat, ilon) <= radius_miles:
                cat = inc.get("category", "other")
                counts[cat] = counts.get(cat, 0) + 1

        scored = analysis.score_from_counts(counts)
        correlations = [c.as_dict() for c in analysis.load_correlations()
                        if any(d in c.pair.lower() for d in scored["top_drivers"])][:2]
        return json.dumps({
            "address": geo.get("display_name", address),
            "latitude": lat, "longitude": lon, "radius_km": radius_km,
            **scored,
            "related_correlations": correlations,
        }, indent=2, default=str)

    group.add_function(name="get_correlation_findings", fn=_get_correlation_findings,
                       description=_get_correlation_findings.__doc__)
    group.add_function(name="get_prediction_accuracy", fn=_get_prediction_accuracy,
                       description=_get_prediction_accuracy.__doc__)
    group.add_function(name="explain_risk_score", fn=_explain_risk_score,
                       description=_explain_risk_score.__doc__)

    yield group


# ---------------------------------------------------------------------------
# Parallel Agent Executor
# Runs two sub-agents concurrently using asyncio.gather
# ---------------------------------------------------------------------------

class ParallelAgentQueryConfig(FunctionBaseConfig, name="parallel_agent_query"):
    """Runs two sub-agents in parallel and returns combined results."""
    agent_1: FunctionRef = Field(description="First sub-agent (e.g. floodwatch_agent)")
    agent_2: FunctionRef = Field(description="Second sub-agent (e.g. command_center_agent)")
    description: str = Field(
        default="Query both FloodWatch and 311 Command Center agents simultaneously. "
                "Use this when a question involves BOTH environmental data AND complaint data, "
                "or when you want a comprehensive cross-domain analysis. "
                "Both agents run in parallel for faster results.",
    )


@register_function(config_type=ParallelAgentQueryConfig)
async def parallel_agent_query(config: ParallelAgentQueryConfig, builder: Builder) -> AsyncGenerator:
    """Build a function that runs two sub-agents concurrently."""

    # Resolve both sub-agent references from the builder
    agent_1 = await builder.get_function(config.agent_1)
    agent_2 = await builder.get_function(config.agent_2)

    agent_1_name = config.agent_1 if isinstance(config.agent_1, str) else str(config.agent_1)
    agent_2_name = config.agent_2 if isinstance(config.agent_2, str) else str(config.agent_2)

    async def _parallel_query(query: str) -> str:
        """Run both FloodWatch and 311 Command Center agents in parallel on the same query and return combined results."""

        # Fire both agents concurrently
        result_1, result_2 = await asyncio.gather(
            agent_1.ainvoke(query),
            agent_2.ainvoke(query),
            return_exceptions=True,
        )

        # Format results, handling any errors gracefully
        parts = []
        if isinstance(result_1, Exception):
            parts.append(f"=== {agent_1_name} ===\nERROR: {result_1}")
        else:
            parts.append(f"=== {agent_1_name} ===\n{result_1}")

        if isinstance(result_2, Exception):
            parts.append(f"=== {agent_2_name} ===\nERROR: {result_2}")
        else:
            parts.append(f"=== {agent_2_name} ===\n{result_2}")

        return "\n\n".join(parts)

    yield FunctionInfo.from_fn(_parallel_query, description=config.description)
