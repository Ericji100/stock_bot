"""R11 candidate: objective upper-space evidence without a fixed RR veto.

No 120-day cutoff, invented price target, or automatic trade permission. AI
must still decide which confirmed highs are structurally comparable/relevant.
"""

from __future__ import annotations

from datetime import date
from math import isfinite
from typing import Any


VERSION = "v2-core-upper-space-evidence-r11-candidate-r1"
_FIELDS = {"candidate_id", "pivot_date", "confirmed_on", "price", "scale"}


def _iso(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be ISO date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{field} must be ISO date")
    return value


def build_upper_space_evidence_asof(
    *, as_of: str, signal_close: float, frozen_episode_stop: float,
    confirmed_highs: list[dict[str, Any]],
) -> dict[str, Any]:
    as_of = _iso(as_of, "as_of")
    if any(
        isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value) or value <= 0
        for value in (signal_close, frozen_episode_stop)
    ):
        raise ValueError("signal and stop prices must be finite positive numbers")
    if signal_close <= frozen_episode_stop:
        raise ValueError("frozen long episode stop must be below signal close")
    if not isinstance(confirmed_highs, list):
        raise ValueError("confirmed_highs must be a list")
    ids: set[str] = set()
    above = []
    for item in confirmed_highs:
        if not isinstance(item, dict) or set(item) != _FIELDS:
            raise ValueError("confirmed high must use exact AS-OF evidence fields")
        candidate_id = item["candidate_id"]
        if not isinstance(candidate_id, str) or not candidate_id or candidate_id in ids:
            raise ValueError("candidate IDs must be distinct nonempty strings")
        ids.add(candidate_id)
        pivot = _iso(item["pivot_date"], "pivot_date")
        confirmed = _iso(item["confirmed_on"], "confirmed_on")
        if not pivot <= confirmed <= as_of:
            raise ValueError("future or causally unconfirmed upper high")
        price = item["price"]
        if isinstance(price, bool) or not isinstance(price, (int, float)) or not isfinite(price) or price <= 0:
            raise ValueError("high price must be finite and positive")
        if item["scale"] not in {"LARGE", "SMALL", "UNRESOLVED"}:
            raise ValueError("invalid proposed scale")
        if price > signal_close:
            above.append({
                "candidate_id": candidate_id, "pivot_date": pivot,
                "confirmed_on": confirmed, "price": price, "proposed_scale": item["scale"],
                "distance_pct_from_signal_close": round((price / signal_close - 1.0) * 100.0, 8),
            })
    above.sort(key=lambda item: (item["price"], item["pivot_date"], item["candidate_id"]))
    return {
        "gate_version": VERSION,
        "as_of": as_of,
        "signal_close": signal_close,
        "frozen_episode_stop": frozen_episode_stop,
        "close_to_stop_risk_pct": round((signal_close - frozen_episode_stop) / signal_close * 100.0, 8),
        "upper_space_status": "CONFIRMED_HIGH_CANDIDATES_AVAILABLE" if above else "NO_CONFIRMED_ABOVE_HIGH_SPACE_UNKNOWN",
        "confirmed_above_high_candidates": above,
        "course_relevance_or_target_selected_by_program": False,
        "fixed_rr_veto_applied": False,
        "trade_permission_granted": False,
    }
