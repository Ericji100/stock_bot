"""Contract tests for the R7 outcome-blind role shortlist presentation layer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.v2_core_legacy_role_shortlist_r7 import (
    INPUT_MANIFEST,
    OUTPUT_DIRECTORY,
    OUTPUT_MANIFEST,
    ROLE_LIMITS,
    build_all,
    build_shortlists,
    canonical_bytes,
    sha256_path,
)


def candidate(index: int, direction: str, status: str, basis: str = "MACD_COMPLETED_REGIME_SPAN") -> dict:
    date = f"2023-01-{index + 1:02d}"
    return {
        "candidate_id": f"CAMSEG-{index:04d}",
        "evidence_option_id": f"CAOPT-{index:04d}",
        "direction": direction,
        "status": status,
        "basis": basis,
        "scale": "LARGE" if index % 2 else "SMALL",
        "start_date": date,
        "confirmed_end_date": date if status == "CONFIRMED" else None,
        "observed_through": "2023-02-01" if status == "FORMING" else date,
        "start_price": 20.0,
        "observed_end_price": 21.0,
        "source_evidence_refs": [f"PRICE:{date}"],
        "objective_metrics": {
            "trading_bars": index + 1,
            "directional_move_atr_at_start": float(index + 1),
        },
    }


def packet(candidates: list[dict]) -> dict:
    return {"review_id": "FP-test", "as_of": "2023-02-01", "candidate_pool": candidates}


def test_objective_role_partitions_do_not_assign_course_answers() -> None:
    rows = [
        candidate(0, "DOWN", "CONFIRMED"),
        candidate(1, "UP", "FORMING", "MACD_FORMING_REGIME_SPAN"),
        candidate(2, "UP", "CONFIRMED", "PIVOT_CONFIRMED_CAMPAIGN"),
    ]
    result = build_shortlists(packet(rows))
    roles = result["roles"]
    assert [row["candidate_id"] for row in roles["ACTIVE_DOWN_CONTROLLER"]["candidate_rows"]] == ["CAMSEG-0000"]
    assert [row["candidate_id"] for row in roles["CURRENT_EPISODE_UP"]["candidate_rows"]] == ["CAMSEG-0001"]
    assert [row["candidate_id"] for row in roles["IMMEDIATE_COMPLETED_UP_PARENT"]["candidate_rows"]] == ["CAMSEG-0002"]
    assert len(roles["UP_CONTROL_CHALLENGER"]["candidate_rows"]) == 2
    assert len(roles["CONTROLLING_MATURE_CAMPAIGN"]["candidate_rows"]) == 2
    serialized = json.dumps(result)
    assert "teacher" not in serialized.lower() or '"teacher_answers_read": false' in serialized
    assert '"scenario"' not in serialized
    assert '"trade_permission"' not in serialized
    assert result["contract"]["course_role_selected_by_program"] is False


def test_shortlist_caps_and_basis_balance() -> None:
    rows = [
        candidate(i, "UP", "CONFIRMED", "MACD_COMPLETED_REGIME_SPAN" if i < 30 else "PIVOT_CONFIRMED_CAMPAIGN")
        for i in range(60)
    ]
    result = build_shortlists(packet(rows))
    parent = result["roles"]["IMMEDIATE_COMPLETED_UP_PARENT"]
    assert parent["objective_universe_count"] == 60
    assert parent["shortlist_count"] == ROLE_LIMITS["IMMEDIATE_COMPLETED_UP_PARENT"]
    assert parent["truncated"] is True
    families = {row["basis"].split("_")[0] for row in parent["candidate_rows"]}
    assert families == {"MACD", "PIVOT"}
    assert len({row["candidate_id"] for row in parent["candidate_rows"]}) == parent["shortlist_count"]


def test_post_as_of_and_duplicate_candidates_are_rejected() -> None:
    row = candidate(0, "UP", "FORMING")
    with pytest.raises(ValueError, match="duplicate"):
        build_shortlists(packet([row, row]))
    future = dict(row, observed_through="2023-02-02")
    with pytest.raises(ValueError, match="post-AS-OF"):
        build_shortlists(packet([future]))


def test_manifest_is_hash_bound_and_idempotent(tmp_path: Path) -> None:
    input_dir = tmp_path / "inputs"
    input_dir.mkdir()
    source_path = input_dir / "FP-test.json"
    source_path.write_bytes(canonical_bytes(packet([candidate(0, "UP", "FORMING")])))
    source_manifest = {
        "input_packet_directory": "inputs",
        "rows": [{
            "review_id": "FP-test",
            "as_of": "2023-02-01",
            "input_packet_file": source_path.name,
            "input_packet_sha256": sha256_path(source_path),
        }],
    }
    (tmp_path / INPUT_MANIFEST).write_bytes(canonical_bytes(source_manifest))
    first = build_all(tmp_path)
    assert first == build_all(tmp_path)
    assert first["case_count"] == 1
    assert (tmp_path / OUTPUT_DIRECTORY / "FP-test.json").exists()
    assert (tmp_path / OUTPUT_MANIFEST).exists()
    source_path.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        build_all(tmp_path)
