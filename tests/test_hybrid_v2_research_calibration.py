from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

from scripts.hybrid_v2_research_calibration import (
    AI_INPUT_CLASSIFICATION,
    CANDIDATE_CASES,
    EVALUATOR_CLASSIFICATION,
    REVIEW_PACKET_DIR,
    SNAPSHOT_CLASSIFICATION,
    SOURCE_CASES,
    SOURCE_MANIFEST,
    STATUS,
    TOOL_STATUS,
    CalibrationError,
    assert_outcome_blind_packet,
    build_calibration,
    causal_adjusted_price_frame,
    cutoff_corporate_actions,
    evaluate_incomplete_calibration,
    file_sha256,
    read_json,
    rubric_completeness_errors,
    verify_calibration,
    write_immutable,
)


@pytest.fixture(scope="module")
def calibration_run(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict]:
    run_dir = tmp_path_factory.mktemp("research-calibration")
    return run_dir, build_calibration(run_dir)


def _walk(value):
    if isinstance(value, dict):
        for key, nested in value.items():
            yield key, nested
            yield from _walk(nested)
    elif isinstance(value, list):
        for nested in value:
            yield None, nested
            yield from _walk(nested)


def test_eight_cases_are_forced_anonymous_snapshots_not_production_reviews(calibration_run) -> None:
    run_dir, manifest = calibration_run
    assert manifest["status"] == STATUS
    assert manifest["case_count"] == 8
    assert manifest["human_frozen_rubric_count"] == 0
    assert manifest["production_review_points_modified"] is False
    assert {row["review_point_classification"] for row in manifest["cases"]} == {
        SNAPSHOT_CLASSIFICATION
    }
    assert {row["ai_input_classification"] for row in manifest["cases"]} == {
        AI_INPUT_CLASSIFICATION
    }
    assert verify_calibration(run_dir) == []


def test_final_tool_status_does_not_promote_incomplete_calibration_data(calibration_run) -> None:
    _, manifest = calibration_run
    assert TOOL_STATUS == "FINAL"
    assert STATUS == "DRAFT/INCOMPLETE_RESEARCH_CALIBRATION"
    assert manifest["status"] == STATUS
    assert manifest["human_frozen_rubric_count"] == 0


def test_ai_packets_have_strict_asof_cutoff_and_no_identity_outcome_or_return(calibration_run) -> None:
    run_dir, manifest = calibration_run
    identity = read_json(run_dir / "evaluator_only/sealed_identity_map.json")
    assert identity["classification"] == EVALUATOR_CLASSIFICATION
    identities = {row["anonymous_stock_id"]: row for row in identity["cases"]}
    for row in manifest["cases"]:
        packet = read_json(run_dir / row["packet"])
        assert_outcome_blind_packet(packet, as_of=row["as_of"])
        serialized = json.dumps(packet, ensure_ascii=False)
        private = identities[row["anonymous_stock_id"]]
        assert private["source_case_id"] not in serialized
        assert private["stock"]["name"] not in serialized
        assert private["stock"]["symbol"] not in serialized
        lowered_keys = {str(key).lower() for key, _ in _walk(packet) if key is not None}
        assert not {"stock", "code", "name", "symbol", "mfe", "mae", "pnl", "profit", "exit"} & lowered_keys
        assert not any(key.startswith("return") or "_return" in key for key in lowered_keys)


def test_corporate_action_audits_contain_only_actions_visible_by_asof(calibration_run) -> None:
    run_dir, manifest = calibration_run
    assert any(
        read_json(run_dir / row["corporate_action_audit"])["future_corporate_actions_removed"] > 0
        for row in manifest["cases"]
    )
    for row in manifest["cases"]:
        audit = read_json(run_dir / row["corporate_action_audit"])
        assert audit["last_visible_bar"] == row["as_of"]
        assert audit["technical_coordinate"] == "AS_OF_REBASED_ADJUSTED_OHLC"
        for events in audit["corporate_actions_to_as_of"].values():
            assert all(event["date"] <= row["as_of"] for event in events)


def test_asof_rebase_cancels_future_adjustment_factor_and_removes_future_ohlc() -> None:
    source = pd.DataFrame({
        "date": ["2025-01-02", "2025-01-03", "2025-01-06"],
        "raw_open": [99.0, 109.0, 119.0],
        "raw_high": [101.0, 111.0, 121.0],
        "raw_low": [98.0, 108.0, 118.0],
        "raw_close": [100.0, 110.0, 120.0],
        "adj_close": [72.0, 88.0, 120.0],
        "volume": [1000, 1100, 1200],
    })
    frame, audit = causal_adjusted_price_frame(source, "2025-01-03")
    assert list(frame["date"].dt.date.astype(str)) == ["2025-01-02", "2025-01-03"]
    assert audit["future_price_rows_removed"] == 1
    assert frame.iloc[-1]["close"] == pytest.approx(110.0)

    rescaled = source.copy()
    rescaled.loc[:1, "adj_close"] *= 0.5
    second, _ = causal_adjusted_price_frame(rescaled, "2025-01-03")
    assert list(second["close"]) == pytest.approx(list(frame["close"]))


def test_corporate_action_cutoff_removes_future_events() -> None:
    visible, removed = cutoff_corporate_actions(
        {"dividends": [{"date": "2025-01-02", "amount": 1}, {"date": "2025-02-03", "amount": 2}]},
        "2025-01-31",
    )
    assert visible == {"dividends": [{"date": "2025-01-02", "amount": 1}]}
    assert removed == 1


@pytest.mark.parametrize(
    "mutation,match",
    [
        (lambda packet: packet.update({"stock": {"code": "9999"}}), "forbidden AI-visible field"),
        (lambda packet: packet.update({"prior_ai_verdict": "PASS"}), "forbidden AI-visible field"),
        (lambda packet: packet["evidence"][0]["values"].update({"return_1d_pct": 99}), "forbidden AI-visible field"),
        (lambda packet: packet["evidence"][0].update({"date": "2099-01-01"}), "future date leaked"),
    ],
)
def test_leakage_regression_guards_fail_closed(calibration_run, mutation, match) -> None:
    run_dir, manifest = calibration_run
    row = manifest["cases"][0]
    packet = read_json(run_dir / row["packet"])
    mutation(packet)
    with pytest.raises(CalibrationError, match=match):
        assert_outcome_blind_packet(packet, as_of=row["as_of"])


def test_immutable_writer_and_manifest_detect_tampering(calibration_run, tmp_path: Path) -> None:
    path = tmp_path / "immutable.json"
    write_immutable(path, {"value": 1})
    write_immutable(path, {"value": 1})
    with pytest.raises(CalibrationError, match="immutable artifact differs"):
        write_immutable(path, {"value": 2})

    run_dir, manifest = calibration_run
    packet_path = run_dir / manifest["cases"][0]["packet"]
    original = packet_path.read_bytes()
    packet_path.write_bytes(original + b" ")
    try:
        assert any("snapshot hash mismatch" in error for error in verify_calibration(run_dir))
    finally:
        packet_path.write_bytes(original)


def test_unfinished_human_rubrics_are_na_and_fail_closed(calibration_run) -> None:
    run_dir, manifest = calibration_run
    rubrics = read_json(run_dir / "evaluator_only/pending_human_rubrics.json")
    evaluation = evaluate_incomplete_calibration(manifest, rubrics["rubrics"])
    assert evaluation["overall_evaluation"] == "N/A"
    assert evaluation["overall_gate_result"] == "FAIL_CLOSED"
    assert evaluation["claim_gold_pass"] is False
    assert evaluation["human_frozen_rubric_count"] == 0
    assert all(row["evaluation"] == "N/A" and row["rubric_errors"] for row in evaluation["cases"])
    for rubric in rubrics["rubrics"]:
        assert rubric_completeness_errors(rubric, rubric["snapshot_sha256"])
        assert rubric["source_evaluator_notes"]


def test_twelve_new_cases_are_candidates_only_and_match_outcome_blind_sampling_rule() -> None:
    candidate_payload = read_json(CANDIDATE_CASES)
    candidates = candidate_payload["candidates"]
    assert candidate_payload["status"] == "CANDIDATE_ONLY_NO_GOLD"
    assert len(candidates) == 12
    assert len({row["candidate_id"] for row in candidates}) == 12
    assert all(row["rubric_status"] == "NOT_AUTHORED" for row in candidates)
    assert not any(
        str(key).lower() in {"gold", "gold_label", "gold_answer", "expected_answer"}
        for key, _ in _walk(candidate_payload)
        if key is not None
    )

    existing_codes = {str(row["stock"]["code"]) for row in read_json(SOURCE_CASES)["cases"]}
    ranked = []
    for path in sorted(REVIEW_PACKET_DIR.glob("*.json")):
        item = read_json(path)
        code = str(item["stock"]["code"])
        if code in existing_codes:
            continue
        first_selected_on = str(item["first_selected_on"])
        digest = hashlib.sha256(
            f"V2_RESEARCH_CALIBRATION_CANDIDATES|{code}|{first_selected_on}".encode()
        ).hexdigest()
        ranked.append((digest, code, first_selected_on))
    expected = [(code, as_of) for _, code, as_of in sorted(ranked)[:12]]
    assert [(row["code"], row["proposed_as_of"]) for row in candidates] == expected


def test_manifest_and_source_hashes_are_stable(calibration_run) -> None:
    run_dir, manifest = calibration_run
    core = dict(manifest)
    supplied = core.pop("manifest_sha256")
    from scripts.hybrid_v3_sharding_v2 import canonical_sha256

    assert supplied == canonical_sha256(core)
    assert manifest["source_cases_sha256"] == file_sha256(SOURCE_CASES)
    assert manifest["candidate_only_cases_sha256"] == file_sha256(CANDIDATE_CASES)
    assert manifest["source_manifest_sha256"] == file_sha256(SOURCE_MANIFEST)
    assert manifest["sealed_identity_map_sha256"] == file_sha256(
        run_dir / "evaluator_only/sealed_identity_map.json"
    )
    assert manifest["pending_human_rubrics_sha256"] == file_sha256(
        run_dir / "evaluator_only/pending_human_rubrics.json"
    )
    assert manifest["evaluation_sha256"] == file_sha256(run_dir / "evaluation.json")
    assert file_sha256(run_dir / "immutable_manifest.json")
