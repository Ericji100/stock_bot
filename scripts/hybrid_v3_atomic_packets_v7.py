"""Candidate6 packets: relation-local causal comparison evidence.

V7 pins V6 and adds raw, as-of measurements needed by the frozen V4-S1
questions.  It does not pre-label PASS/FAIL, change the semantic prompt, or
alter any trading gate.  Each typed relation receives a causal evidence row
covering its parent, correction, prior same-scale impulse, current episode,
overhead space, stop risk, and already-observed continuation failures.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd

try:
    from . import hybrid_v3_atomic_packets_v4 as _v4
    from . import hybrid_v3_atomic_packets_v6 as _v6
except ImportError:  # direct script import
    from scripts import hybrid_v3_atomic_packets_v4 as _v4
    from scripts import hybrid_v3_atomic_packets_v6 as _v6


BUILDER_VERSION = "hybrid-v3-atomic-packets-v7"
BUILDER_STATUS = "CANDIDATE_FOR_OUTCOME_BLIND_EVIDENCE_VALIDATION"
BASE_BUILDER_VERSION = "hybrid-v3-atomic-packets-v6"
BASE_BUILDER_SHA256 = "f50c92d577e8af67eaf2b983d83aaef818cf078bb9a6c0fc34b5bbf32d78fd3f"
EVIDENCE_CONTRACT_VERSION = "relation-local-causal-comparison-v1"
FIX_CODE = "ADD_RELATION_LOCAL_RAW_COMPARISON_AND_SPACE_EVIDENCE"
_WRAPPER_FILE = Path(__file__).resolve()
_BASE_FILE = Path(_v6.__file__).resolve()
_ORIGINAL_BUILD_ATOMIC_PACKET = _v4.build_atomic_packet


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def assert_base_builder_frozen() -> None:
    if _file_sha256(_BASE_FILE) != BASE_BUILDER_SHA256:
        raise RuntimeError("V6 base builder bytes changed")
    if _v4.BUILDER_VERSION != BUILDER_VERSION:
        _v6.assert_base_builder_frozen()


def _ratio(current: Any, baseline: Any) -> float | None:
    if current is None or baseline in {None, 0, 0.0}:
        return None
    return _v4._number(float(current) / float(baseline), 6)


def _point(
    ref: Any,
    *,
    evidence: Mapping[str, Mapping[str, Any]],
    pivots: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any] | None:
    ref = str(ref or "")
    pivot = pivots.get(ref)
    if pivot:
        return {
            "ref": ref,
            "source_date": str(pivot["source_date"]),
            "price": float(pivot["price"]),
        }
    row = evidence.get(ref)
    if not row:
        return None
    values = row.get("values") or {}
    if row.get("kind") == "CONFIRMED_PIVOT":
        return {
            "ref": ref,
            "source_date": str(values["source_date"]),
            "price": float(values["price"]),
        }
    if row.get("kind") == "BAR":
        return {
            "ref": ref,
            "source_date": str(row["date"]),
            "price": float(values["close"]),
        }
    return None


def _segment(
    *,
    start: Mapping[str, Any] | None,
    end: Mapping[str, Any] | None,
    visible: pd.DataFrame,
    role: str,
    endpoint_state: str,
) -> dict[str, Any] | None:
    if not start or not end or str(start["source_date"]) > str(end["source_date"]):
        return None
    measured = _v4._comparison_leg(start=dict(start), end=dict(end), visible=visible)
    part = visible[
        (visible["date"] >= pd.Timestamp(str(start["source_date"])))
        & (visible["date"] <= pd.Timestamp(str(end["source_date"])))
    ]
    average_volume = float(part["volume"].astype(float).mean()) if not part.empty else None
    median_volume = float(part["volume"].astype(float).median()) if not part.empty else None
    end_row = part.iloc[-1] if not part.empty else None
    return {
        "role": role,
        "endpoint_state": endpoint_state,
        **measured,
        "absolute_price_change": _v4._number(
            float(end["price"]) - float(start["price"]), 4
        ),
        "average_volume": _v4._number(average_volume, 2),
        "median_volume": _v4._number(median_volume, 2),
        "end_volume": _v4._number(None if end_row is None else end_row["volume"], 2),
        "end_volume_ratio_20": _v4._number(
            None if end_row is None else end_row.get("volume_ratio_20"), 4
        ),
    }


def _comparison_ratios(
    baseline: Mapping[str, Any] | None, current: Mapping[str, Any] | None
) -> dict[str, Any] | None:
    if not baseline or not current:
        return None
    return {
        "current_to_baseline_abs_amplitude_ratio": _ratio(
            abs(float(current["absolute_price_change"])),
            abs(float(baseline["absolute_price_change"])),
        ),
        "current_to_baseline_bar_count_ratio": _ratio(
            current["bar_count"], baseline["bar_count"]
        ),
        "current_to_baseline_abs_slope_ratio": _ratio(
            abs(float(current["slope_pct_per_bar"])),
            abs(float(baseline["slope_pct_per_bar"])),
        ),
        "current_to_baseline_true_range_ratio": _ratio(
            current["mean_true_range_pct"], baseline["mean_true_range_pct"]
        ),
        "current_to_baseline_realized_volatility_ratio": _ratio(
            current["realized_close_volatility_pct"],
            baseline["realized_close_volatility_pct"],
        ),
        "current_to_baseline_average_volume_ratio": _ratio(
            current["average_volume"], baseline["average_volume"]
        ),
        "threshold_or_pass_fail_precomputed": False,
    }


def _prior_same_scale_up_segment(
    *,
    attack: Mapping[str, Any],
    all_pivots: Sequence[Mapping[str, Any]],
    pivot_by_ref: Mapping[str, Mapping[str, Any]],
    visible: pd.DataFrame,
) -> dict[str, Any] | None:
    control = pivot_by_ref.get(str(attack.get("control_pivot_ref") or ""))
    if not control or control.get("side") != "HIGH":
        return None
    lows = [
        row
        for row in all_pivots
        if row.get("scale") == attack.get("scale")
        and row.get("side") == "LOW"
        and str(row.get("source_date")) < str(control.get("source_date"))
        and str(row.get("confirmation_date")) <= str(attack.get("confirmed_on"))
    ]
    if not lows:
        return None
    start = max(
        lows,
        key=lambda row: (
            str(row["source_date"]), str(row["confirmation_date"]), str(row["ref"])
        ),
    )
    return _segment(
        start=start,
        end=control,
        visible=visible,
        role="PRIOR_COMPLETED_SAME_SCALE_UP_IMPULSE",
        endpoint_state="CONFIRMED_PIVOT_ENDPOINTS",
    )


def _continuation_observation(
    *,
    attack: Mapping[str, Any],
    visible: pd.DataFrame,
) -> dict[str, Any] | None:
    if attack.get("origin_pivot_ref") is None or attack.get("control_price") is None:
        return None
    start_date = str(attack.get("origin_pivot_source_date") or "")
    if not start_date:
        return None
    as_of = str(attack["confirmed_on"])
    part = visible[
        (visible["date"] >= pd.Timestamp(start_date))
        & (visible["date"] <= pd.Timestamp(as_of))
    ].copy()
    if part.empty:
        return None
    control = float(attack["control_price"])
    prior = part.iloc[:-1]
    prior_above = prior["close"].astype(float) > control
    prior_reclose = 0
    was_above = False
    for close in prior["close"].astype(float):
        if close > control:
            was_above = True
        elif was_above:
            prior_reclose += 1
            was_above = False
    last = part.iloc[-1]
    return {
        "start_date": start_date,
        "end_date": as_of,
        "control_price": _v4._number(control),
        "prior_closes_above_control_before_as_of": int(prior_above.sum()),
        "prior_break_then_reclose_events_before_as_of": int(prior_reclose),
        "as_of_close": _v4._number(last["close"]),
        "as_of_close_above_control": bool(float(last["close"]) > control),
        "as_of_high_above_control": bool(float(last["high"]) > control),
        "post_as_of_follow_through_used": False,
    }


def _space_and_risk(
    *,
    hypothesis: Mapping[str, Any],
    evidence: Mapping[str, Mapping[str, Any]],
    all_pivots: Sequence[Mapping[str, Any]],
    visible: pd.DataFrame,
    as_of: str,
    current_origin: Mapping[str, Any] | None,
) -> dict[str, Any]:
    close = float(visible.iloc[-1]["close"])
    atr = float(visible.iloc[-1]["atr14"])
    stop_row = evidence.get(str(hypothesis.get("episode_stop_ref") or "")) or {}
    stop_values = stop_row.get("values") or {}
    stop_price = stop_values.get("price")
    resistance = sorted(
        (
            row
            for row in all_pivots
            if row.get("side") == "HIGH"
            and str(row.get("confirmation_date")) <= as_of
            and float(row.get("price")) > close
        ),
        key=lambda row: (float(row["price"]), str(row["source_date"]), str(row["ref"])),
    )
    nearest = resistance[0] if resistance else None
    risk_amount = close - float(stop_price) if stop_price is not None else None
    reward_amount = float(nearest["price"]) - close if nearest else None
    visible_high = float(visible["high"].astype(float).max())
    origin_price = None if current_origin is None else float(current_origin["price"])
    return {
        "as_of_close": _v4._number(close),
        "as_of_atr14": _v4._number(atr),
        "episode_stop_ref": hypothesis.get("episode_stop_ref"),
        "episode_stop_price": _v4._number(stop_price),
        "risk_to_episode_stop_pct": _v4._number(
            None if risk_amount is None else risk_amount / close * 100.0, 4
        ),
        "risk_to_episode_stop_atr": _v4._number(
            None if risk_amount is None or atr == 0 else risk_amount / atr, 4
        ),
        "nearest_confirmed_overhead_ref": None if nearest is None else nearest["ref"],
        "nearest_confirmed_overhead_price": _v4._number(
            None if nearest is None else nearest["price"]
        ),
        "nearest_overhead_distance_pct": _v4._number(
            None if reward_amount is None else reward_amount / close * 100.0, 4
        ),
        "nearest_overhead_reward_to_episode_risk": _v4._number(
            None
            if reward_amount is None or risk_amount in {None, 0, 0.0}
            else reward_amount / risk_amount,
            4,
        ),
        "confirmed_overhead_count": len(resistance),
        "visible_high_through_as_of": _v4._number(visible_high),
        "close_distance_to_visible_high_pct": _v4._number(
            (visible_high - close) / close * 100.0, 4
        ),
        "current_origin_ref": None if current_origin is None else current_origin["ref"],
        "current_origin_price": _v4._number(origin_price),
        "extension_from_origin_pct": _v4._number(
            None if not origin_price else (close / origin_price - 1.0) * 100.0, 4
        ),
        "extension_from_origin_atr": _v4._number(
            None if origin_price is None or atr == 0 else (close - origin_price) / atr,
            4,
        ),
        "threshold_or_adequacy_precomputed": False,
    }


def build_relation_comparison_evidence(
    *,
    packet: Mapping[str, Any],
    visible: pd.DataFrame,
    review_packet: Mapping[str, Any],
    as_of: str,
) -> list[dict[str, Any]]:
    """Build one raw causal comparison record per AI-visible relation."""

    evidence = {
        str(row.get("ref")): row
        for row in packet.get("evidence") or []
        if isinstance(row, dict) and row.get("ref")
    }
    all_pivots = _v4.causal_pivots_as_of(dict(review_packet), as_of)
    pivot_by_ref = {str(row["ref"]): row for row in all_pivots}
    objective = packet.get("objective_facts") or {}
    hypotheses = [
        (scenario, row)
        for scenario, rows in (objective.get("scenario_hypotheses") or {}).items()
        for row in rows or []
    ]
    attack_evidence = {
        str(row.get("ref")): row.get("values") or {}
        for row in packet.get("evidence") or []
        if isinstance(row, dict) and row.get("kind") == "CAUSAL_CONTROL_ATTACK"
    }
    rows: list[dict[str, Any]] = []
    for scenario, hypothesis in hypotheses:
        relation_ref = str(hypothesis.get("relation_ref") or "")
        relation_row = evidence.get(relation_ref) or {}
        relation = relation_row.get("values") or {}
        attack = attack_evidence.get(str(relation.get("current_attack_ref") or ""))
        if not relation or not attack:
            continue
        attack = dict(attack)
        origin_ref = attack.get("origin_pivot_ref")
        origin = _point(origin_ref, evidence=evidence, pivots=pivot_by_ref)
        if origin is not None:
            attack["origin_pivot_source_date"] = origin["source_date"]
        current_end = {
            "ref": f"BAR:{as_of}",
            "source_date": as_of,
            "price": float(visible.iloc[-1]["close"]),
        }
        current = _segment(
            start=origin,
            end=current_end,
            visible=visible,
            role="CURRENT_EPISODE_THROUGH_AS_OF_CLOSE",
            endpoint_state="AS_OF_CLOSE_IS_ENTRY_STAGE_ENDPOINT_NOT_COMPLETED_ANCHOR",
        )
        baseline = _prior_same_scale_up_segment(
            attack=attack,
            all_pivots=all_pivots,
            pivot_by_ref=pivot_by_ref,
            visible=visible,
        )
        parent_anchor = (evidence.get(str(relation.get("parent_anchor_ref") or "")) or {}).get(
            "values"
        ) or {}
        correction_anchor = (
            evidence.get(str(relation.get("correction_anchor_ref") or "")) or {}
        ).get("values") or {}
        parent = _segment(
            start=_point(parent_anchor.get("start_ref"), evidence=evidence, pivots=pivot_by_ref),
            end=_point(parent_anchor.get("end_ref"), evidence=evidence, pivots=pivot_by_ref),
            visible=visible,
            role="BOUND_PARENT_ANCHOR",
            endpoint_state="CONFIRMED_ANCHOR_ENDPOINTS",
        )
        correction = _segment(
            start=_point(
                correction_anchor.get("start_ref"), evidence=evidence, pivots=pivot_by_ref
            ),
            end=_point(
                correction_anchor.get("end_ref"), evidence=evidence, pivots=pivot_by_ref
            ),
            visible=visible,
            role="BOUND_PARENT_CORRECTION",
            endpoint_state="CONFIRMED_ANCHOR_ENDPOINTS",
        )
        values = {
            "contract_version": EVIDENCE_CONTRACT_VERSION,
            "scenario": scenario,
            "hypothesis_id": hypothesis.get("hypothesis_id"),
            "relation_ref": relation_ref,
            "current_attack_ref": relation.get("current_attack_ref"),
            "as_of": as_of,
            "causal_cutoff_enforced": True,
            "pass_fail_or_trade_label_included": False,
            "parent_segment": parent,
            "correction_segment": correction,
            "prior_same_scale_up_segment": baseline,
            "current_episode_segment": current,
            "current_vs_prior_same_scale_ratios": _comparison_ratios(baseline, current),
            "continuation_observation": _continuation_observation(
                attack=attack, visible=visible
            ),
            "space_and_risk": _space_and_risk(
                hypothesis=hypothesis,
                evidence=evidence,
                all_pivots=all_pivots,
                visible=visible,
                as_of=as_of,
                current_origin=origin,
            ),
            "missing_components": sorted(
                name
                for name, value in {
                    "PARENT_SEGMENT": parent,
                    "CORRECTION_SEGMENT": correction,
                    "PRIOR_SAME_SCALE_UP_SEGMENT": baseline,
                    "CURRENT_EPISODE_SEGMENT": current,
                }.items()
                if value is None
            ),
        }
        ref = "RELATION_COMPARISON:C-" + _v4.canonical_sha256(values)[:20]
        rows.append(
            {
                "ref": ref,
                "kind": "CAUSAL_RELATION_COMPARISON",
                "date": as_of,
                "values": values,
            }
        )
    return rows


def build_atomic_packet(**kwargs: Any) -> dict[str, Any]:
    packet = _ORIGINAL_BUILD_ATOMIC_PACKET(**kwargs)
    as_of = str(kwargs["as_of"])
    visible = kwargs["visible_frame"]
    visible = visible[visible["date"] <= pd.Timestamp(as_of)].copy()
    added = build_relation_comparison_evidence(
        packet=packet,
        visible=visible,
        review_packet=kwargs["review_packet"],
        as_of=as_of,
    )
    packet["evidence"] = _v4._deduplicate_evidence([*packet["evidence"], *added])
    objective = packet["objective_facts"]
    objective["relation_comparison_contract_version"] = EVIDENCE_CONTRACT_VERSION
    objective["relation_comparison_evidence_count"] = len(added)
    objective["ai_visible_evidence_sha256"] = _v4.canonical_sha256(packet["evidence"])
    packet["evidence_catalog_sha256"] = _v4.canonical_sha256(packet["evidence"])
    core = dict(packet)
    core.pop("input_packet_sha256", None)
    packet["input_packet_sha256"] = _v4.canonical_sha256(core)
    _v4.assert_anonymous_and_causal(packet, as_of=as_of)
    return packet


def activate() -> None:
    assert_base_builder_frozen()
    if _v4.BUILDER_VERSION == BUILDER_VERSION:
        return
    if _v4.BUILDER_VERSION not in {
        "hybrid-v3-atomic-packets-v4",
        "hybrid-v3-atomic-packets-v5",
        BASE_BUILDER_VERSION,
    }:
        raise RuntimeError("unexpected active packet builder version")
    _v6.activate()
    _v4.build_atomic_packet = build_atomic_packet
    _v4.BUILDER_VERSION = BUILDER_VERSION
    _v4.BUILDER_STATUS = BUILDER_STATUS
    _v4.__file__ = str(_WRAPPER_FILE)


def __getattr__(name: str) -> Any:
    return getattr(_v4, name)


def main() -> int:
    activate()
    return int(_v4._main())


if __name__ == "__main__":
    raise SystemExit(main())
