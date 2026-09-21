"""Read-only R11 AS-OF input coverage audit for the 14 calibration cases.

This audits *availability and provenance*, not course correctness. It reads
neither old teacher answers nor future prices and never grants a trade.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from scripts.v2_core_trade_role_preflight_r11 import preflight_trade_role_asof


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = ROOT / "reports/course_backtest/2026-09-10/v2_core_reproducible_goal_v1"
VERSION = "v2-core-r11-input-coverage-audit-candidate-r1"


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def audit_case(anchor: dict[str, Any], source: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    """Check causal coverage without using identity, teacher, or outcomes."""
    review_id = anchor["review_id"]
    as_of = anchor["as_of"]
    _require(source["review_id"] == snapshot["review_id"] == review_id, "review ID mismatch")
    _require(source["as_of"] == snapshot["as_of"] == as_of, "AS-OF mismatch")
    _require(snapshot["calibration_only_prior_legacy_fills"] is True, "snapshot purpose mismatch")
    _require(snapshot["end_to_end_new_policy_ledger"] is False, "snapshot is not new-policy ledger")
    _require(snapshot["position_snapshot_as_of"]["as_of"] == as_of, "position AS-OF mismatch")
    _require(snapshot["role_preflight"]["as_of"] == as_of, "role AS-OF mismatch")
    _require(
        preflight_trade_role_asof(as_of, snapshot["position_snapshot_as_of"]) == snapshot["role_preflight"],
        "position role/digest mismatch",
    )

    full_bars = source["daily_structure_context_to_as_of"]
    sampled_bars = anchor["daily_context_to_as_of"]
    full_dates = [bar["date"] for bar in full_bars]
    sampled_dates = [bar["date"] for bar in sampled_bars]
    _require(full_dates == sorted(set(full_dates)), "source daily dates not unique and ordered")
    _require(sampled_dates == sorted(set(sampled_dates)), "sample daily dates not unique and ordered")
    _require(full_dates and full_dates[-1] == as_of, "source lacks signal-day close")
    _require(sampled_dates and sampled_dates[-1] == as_of, "sample lacks signal-day close")
    _require(len(full_bars) == source["data_quality"]["context_bars"], "source bar count mismatch")
    _require(len(sampled_bars) == anchor["context_resolution"]["retained_bar_count"], "sample bar count mismatch")
    full_by_date = {bar["date"]: bar for bar in full_bars}
    for bar in sampled_bars:
        full = full_by_date.get(bar["date"])
        _require(full is not None, "sample contains date absent from full source")
        _require(all(bar[key] == full[key] for key in ("open", "high", "low", "close")), "sample OHLC mismatch")
    _require(anchor["confirmed_pivots_to_as_of"] == source["confirmed_pivots_to_as_of"], "pivot catalog mismatch")
    _require(anchor["completed_macd_21_55_55_cycles_to_as_of"] == source["completed_macd_21_55_55_cycles_to_as_of"], "MACD cycle catalog mismatch")

    pivots = source["confirmed_pivots_to_as_of"]
    for pivot in pivots:
        _require(pivot["source_date"] <= pivot["confirmation_date"] <= as_of, "future/unconfirmed pivot")
        _require(pivot["price"] > 0, "nonpositive pivot price")
    high_rows = [pivot for pivot in pivots if pivot["side"] == "HIGH"]
    signal_close = full_bars[-1]["close"]  # adjusted structural price, never raw fill price
    above_rows = [pivot for pivot in high_rows if pivot["price"] > signal_close]
    before_full_window = [pivot for pivot in high_rows if pivot["source_date"] < full_dates[0]]
    return {
        "review_id": review_id,
        "as_of": as_of,
        "full_daily_bar_count": len(full_bars),
        "full_daily_start": full_dates[0],
        "sampled_daily_bar_count": len(sampled_bars),
        "sampled_daily_is_complete_for_close_path": len(sampled_bars) == len(full_bars),
        "available_bars_reported": source["data_quality"]["available_bars_to_as_of"],
        "confirmed_pivot_count": len(pivots),
        "confirmed_high_row_count": len(high_rows),
        "above_signal_high_row_count": len(above_rows),
        "confirmed_high_rows_before_full_daily_window": len(before_full_window),
        "upper_space_when_no_above_high": "UNKNOWN_NOT_UNLIMITED" if not above_rows else "AI_RELEVANCE_REVIEW_REQUIRED",
        "position_review_role": snapshot["role_preflight"]["review_role"],
        "position_preflight_status": snapshot["role_preflight"]["status"],
    }


def audit_inputs(artifact_dir: Path = ARTIFACT) -> dict[str, Any]:
    input_manifest = _load(artifact_dir / "legacy_anchor_alignment_input_manifest_candidate_r1.json")
    position_manifest = _load(artifact_dir / "legacy_position_snapshot_manifest_candidate_r11.json")
    positions = {row["review_id"]: row for row in position_manifest["rows"]}
    _require(len(positions) == len(position_manifest["rows"]), "duplicate position manifest review ID")
    _require(input_manifest["case_count"] == position_manifest["case_count"], "manifest case count mismatch")
    rows = []
    for bound in input_manifest["rows"]:
        review_id = bound["review_id"]
        position = positions[review_id]
        anchor_path = artifact_dir / input_manifest["input_packet_directory"] / bound["input_packet_file"]
        source_path = artifact_dir / "feasibility_probe_packets_r2" / f"{review_id}.json"
        snapshot_path = artifact_dir / "legacy_position_snapshots_candidate_r11" / position["packet_file"]
        _require(_sha(anchor_path) == bound["input_packet_sha256"], "anchor input SHA mismatch")
        _require(_sha(source_path) == bound["source_packet_sha256"], "full source SHA mismatch")
        _require(_sha(snapshot_path) == position["packet_sha256"], "position packet SHA mismatch")
        anchor, source, snapshot = _load(anchor_path), _load(source_path), _load(snapshot_path)
        _require(anchor["source_bindings"]["source_packet_sha256"] == _sha(source_path), "source binding mismatch")
        _require(snapshot["role_preflight"]["snapshot_sha256"] == position["snapshot_sha256"], "position digest mismatch")
        rows.append(audit_case(anchor, source, snapshot))
    _require(len(rows) == input_manifest["case_count"], "audited case count mismatch")
    _require(len({row["review_id"] for row in rows}) == len(rows), "duplicate input review ID")
    return {
        "audit_version": VERSION,
        "status": "INPUT_COVERAGE_VERIFIED_WITH_OPEN_GAPS",
        "formal_ai_calls": 0,
        "teacher_answer_read": False,
        "future_outcome_read": False,
        "case_count": len(rows),
        "full_750_bar_source_cases": sum(row["full_daily_bar_count"] >= 750 for row in rows),
        "sampled_context_incomplete_for_close_path_cases": sum(not row["sampled_daily_is_complete_for_close_path"] for row in rows),
        "position_snapshot_cases": len(rows),
        "no_confirmed_above_high_cases": sum(row["above_signal_high_row_count"] == 0 for row in rows),
        "cases_with_high_rows_before_full_daily_window": sum(row["confirmed_high_rows_before_full_daily_window"] > 0 for row in rows),
        "full_historical_high_completeness_proven": False,
        "end_to_end_new_policy_position_ledger": False,
        "rows": rows,
    }


if __name__ == "__main__":
    print(json.dumps(audit_inputs(), ensure_ascii=False, sort_keys=True, indent=2))
