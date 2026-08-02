"""Structured access to GridWatch's offline analysis.

`correlation_analysis.py` and `backtest_predictions.py` are the most
substantial data work in the project — 10 cross-dataset correlations against a
random baseline, and a grid-based prediction model backtested on 672k training
and 158k test records. Both were scripts that printed to a terminal, so the
agent could not use any of it.

This module parses their committed outputs into typed structures the NAT tool
group can return. Reading cached results rather than recomputing is deliberate:
a full correlation run refetches ten datasets and takes minutes, which is not
something to do inside a dispatcher's question. `refresh_*` helpers exist for
when the numbers should be regenerated.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
import logging
import re

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).parent.parent.parent
CORRELATION_FILE = REPO_ROOT / "correlation_results.txt"
BACKTEST_FILE = REPO_ROOT / "backtest_results.txt"


@dataclass
class Correlation:
    pair: str
    ratio: float
    strength: str
    finding: str
    by_distance: dict[str, float]

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class TypeAccuracy:
    complaint_type: str
    hit_rate: float
    hits: int
    predictions: int


# ---------------------------------------------------------------------------
# Correlation findings
# ---------------------------------------------------------------------------

_PAIR_RE = re.compile(r"^\s{2}(.+?) <-> (.+?): ([\d.]+)x correlation\s*$")
_DIST_RE = re.compile(r"^\s+([\d.]+) km: actual=([\d.]+), random=([\d.]+), ratio=([\d.]+)x")
_FINDING_RE = re.compile(r"->\s*FINDING:\s*(.+)$")
_SECTION_RE = re.compile(r"^\[.*?\]\s*(.+?)\s*(?:\(|$)")


def load_correlations() -> list[Correlation]:
    """Parse correlation_results.txt into structured findings, strongest first."""
    if not CORRELATION_FILE.exists():
        return []

    results: list[Correlation] = []
    section = "unknown"
    current: Correlation | None = None

    for line in CORRELATION_FILE.read_text().splitlines():
        sec = _SECTION_RE.match(line.strip())
        if sec and "CORRELATION" in line.upper():
            section = sec.group(1).strip().title()
            continue

        pair = _PAIR_RE.match(line)
        if pair:
            current = Correlation(
                pair=f"{pair.group(1)} <-> {pair.group(2)}",
                ratio=float(pair.group(3)),
                strength=section,
                finding="",
                by_distance={},
            )
            results.append(current)
            continue

        if current is None:
            continue

        dist = _DIST_RE.match(line)
        if dist:
            current.by_distance[f"{dist.group(1)}km"] = float(dist.group(4))
            continue

        found = _FINDING_RE.search(line)
        if found:
            current.finding = found.group(1).strip()

    results.sort(key=lambda c: -c.ratio)
    return results


def find_correlations(topic: str = "", min_ratio: float = 0.0) -> list[Correlation]:
    """Correlations mentioning `topic` (matched loosely) above `min_ratio`."""
    topic = topic.strip().lower()
    return [
        c for c in load_correlations()
        if c.ratio >= min_ratio and (not topic or topic in c.pair.lower() or topic in c.finding.lower())
    ]


# ---------------------------------------------------------------------------
# Backtest / prediction accuracy
# ---------------------------------------------------------------------------

_METRIC_RE = re.compile(r"^\s{2}(.+?):\s+([\d,]+|[\d.]+%)\s*$")
_TYPE_RE = re.compile(r"^\s+(.+?): ([\d.]+)% \((\d+)/(\d+) predictions hit\)")


def load_backtest() -> dict:
    """Parse backtest_results.txt into overall metrics plus per-type accuracy."""
    if not BACKTEST_FILE.exists():
        return {"available": False}

    text = BACKTEST_FILE.read_text()
    metrics: dict[str, str] = {}
    for line in text.splitlines():
        m = _METRIC_RE.match(line)
        if m:
            metrics[m.group(1).strip()] = m.group(2).strip()

    types = [
        TypeAccuracy(complaint_type=m.group(1).strip(),
                     hit_rate=float(m.group(2)),
                     hits=int(m.group(3)),
                     predictions=int(m.group(4)))
        for m in (_TYPE_RE.match(line) for line in text.splitlines()) if m
    ]
    types.sort(key=lambda t: -t.hit_rate)

    return {
        "available": True,
        "overall_hit_rate": metrics.get("Overall hit rate", "unknown"),
        "false_positive_rate": metrics.get("False positive rate", "unknown"),
        "total_predictions": metrics.get("Total predictions generated", "unknown"),
        "grid_cell": "~500m x 400m",
        "by_type": [asdict(t) for t in types],
        "caveat": (
            "Backtested on Jan-Feb 2026 training and Mar 2026 test data. "
            "Hit rate is per grid cell and complaint type, not a forecast of "
            "any individual incident."
        ),
    }


def predictable_types(top_n: int = 10) -> list[dict]:
    """Complaint types the model predicts most reliably."""
    data = load_backtest()
    return data.get("by_type", [])[:top_n] if data.get("available") else []


# ---------------------------------------------------------------------------
# Live risk scoring
# ---------------------------------------------------------------------------

# Weights carried over from the risk endpoint. Kept here so the tool and the
# HTTP route cannot drift apart.
RISK_WEIGHTS = {
    "flooding": 2.0, "sewer": 1.5, "rodent": 1.0,
    "housing": 1.5, "street_condition": 1.0, "noise": 0.5,
}


def score_from_counts(counts: dict[str, int]) -> dict:
    """Turn nearby-incident counts into a 0-100 risk score with a breakdown.

    Deterministic and explainable on purpose: a dispatcher asking "why is this
    address risky" should get arithmetic, not a model's opinion.
    """
    breakdown, total = {}, 0.0
    for category, weight in RISK_WEIGHTS.items():
        n = counts.get(category, 0)
        contribution = min(n * weight * 4, 100 * weight / sum(RISK_WEIGHTS.values()))
        breakdown[category] = {"count": n, "weight": weight,
                               "contribution": round(contribution, 1)}
        total += contribution

    score = round(min(total, 100.0), 1)
    label = ("low" if score < 25 else
             "moderate" if score < 50 else
             "elevated" if score < 75 else "high")
    drivers = sorted(breakdown.items(), key=lambda kv: -kv[1]["contribution"])[:3]
    return {
        "risk_score": score,
        "risk_label": label,
        "breakdown": breakdown,
        "top_drivers": [d[0] for d in drivers if d[1]["contribution"] > 0],
    }


# ---------------------------------------------------------------------------
# Refresh helpers
# ---------------------------------------------------------------------------

async def refresh_correlations() -> str:
    """Re-run the correlation analysis. Minutes, and refetches ten datasets."""
    from hackathon_nyc import correlation_analysis
    await correlation_analysis.main()
    return str(CORRELATION_FILE)


async def refresh_backtest() -> str:
    """Re-run the prediction backtest. Minutes, and refetches 311 history."""
    from hackathon_nyc import backtest_predictions
    await backtest_predictions.main()
    return str(BACKTEST_FILE)
