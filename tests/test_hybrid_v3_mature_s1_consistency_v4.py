from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from scripts import hybrid_v3_mature_s1_consistency_v4 as v4
from scripts.hybrid_v3_sharding_v2 import canonical_sha256, file_sha256


def _packet(empty: bool = True) -> dict:
    manifest = {
        "anchor_candidates": [],
        "relation_candidates": [],
        "stop_candidates": [],
        "global_question_ids": [],
    }
    if not empty:
        manifest["global_question_ids"] = ["ANCHOR_CLEAN"]
    return {"review_id": "D-test", "question_manifest": manifest}


def _empty_output(marker: str | None = None) -> dict:
    value = {
        "candidate_answers": {
            "anchor_candidates": [],
            "relation_candidates": [],
            "stop_candidates": [],
        },
        "global_answers": {},
        "causal_attestation": {"answered_complete_manifest": True},
    }
    if marker is not None:
        value["marker"] = marker
    return value


def _verdict() -> dict:
    return {
        "result": "PASS",
        "supporting_evidence_refs": ["BAR:2023-01-01"],
        "contradicting_evidence_refs": [],
        "missing_evidence_codes": [],
        "reason_code": "VISIBLE_EVIDENCE_SATISFIES_DEFINITION",
    }


@pytest.fixture
def valid_semantics(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(v4, "validate_atomic", lambda _packet, _output: [])
    monkeypatch.setattr(v4, "reduce_atomic_v3", lambda _packet, _output: {"permission": "REMOVE"})
    monkeypatch.setattr(v4, "decision_signature", lambda value: dict(value))


def test_exact_empty_frozen_manifest_allows_vacuous_passthrough(valid_semantics: None) -> None:
    output = _empty_output()

    merged, metrics, handled = v4.merge_case_outputs_v4(
        _packet(), [copy.deepcopy(output) for _ in range(3)], critical_ids={"ANCHOR_CLEAN"}
    )

    assert handled is True
    assert merged == output
    assert metrics["critical_atom_fields"] == 0
    assert metrics["critical_atom_rate"] is None
    assert metrics["technical_correction"] == v4.EMPTY_REASON
    assert v4._not_applicable_rate() == {
        "exact": 0,
        "total": 0,
        "rate": None,
        "applicable": False,
        "status": "N/A",
        "reason": "EMPTY_FROZEN_MANIFEST_EXACT",
    }


def test_partial_empty_outputs_fail_closed(valid_semantics: None) -> None:
    nonempty = _empty_output()
    nonempty["global_answers"] = {"ANCHOR_CLEAN": _verdict()}
    with pytest.raises(v4.MatureS1ConsistencyV4Error, match="partial-empty"):
        v4.merge_case_outputs_v4(
            _packet(), [_empty_output(), _empty_output(), nonempty], critical_ids={"ANCHOR_CLEAN"}
        )


def test_empty_atoms_with_nonempty_frozen_manifest_fail_closed(valid_semantics: None) -> None:
    with pytest.raises(v4.MatureS1ConsistencyV4Error, match="exactly empty frozen manifest"):
        v4.merge_case_outputs_v4(
            _packet(empty=False), [_empty_output() for _ in range(3)], critical_ids={"ANCHOR_CLEAN"}
        )


def test_empty_manifest_must_equal_deterministic_atomic_policy(
    valid_semantics: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    changed = _packet()
    monkeypatch.setattr(
        v4,
        "expected_question_manifest",
        lambda _packet: {
            "anchor_candidates": [],
            "relation_candidates": [],
            "stop_candidates": [],
            "global_question_ids": ["ANCHOR_CLEAN"],
        },
    )
    with pytest.raises(v4.MatureS1ConsistencyV4Error, match="deterministic atomic policy"):
        v4.merge_case_outputs_v4(
            changed, [_empty_output() for _ in range(3)], critical_ids={"ANCHOR_CLEAN"}
        )


def test_empty_outputs_must_be_canonically_identical(valid_semantics: None) -> None:
    with pytest.raises(v4.MatureS1ConsistencyV4Error, match="canonically identical"):
        v4.merge_case_outputs_v4(
            _packet(), [_empty_output(), _empty_output(), _empty_output("changed")],
            critical_ids={"ANCHOR_CLEAN"},
        )


def test_empty_outputs_must_pass_atomic_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(v4, "validate_atomic", lambda _packet, _output: ["invalid"])
    with pytest.raises(v4.MatureS1ConsistencyV4Error, match="atomic validation failed"):
        v4.merge_case_outputs_v4(
            _packet(), [_empty_output() for _ in range(3)], critical_ids={"ANCHOR_CLEAN"}
        )


def test_empty_outputs_must_have_identical_decision_signature(
    valid_semantics: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    signatures = iter([{"permission": "REMOVE"}, {"permission": "WAIT"}, {"permission": "REMOVE"}])
    monkeypatch.setattr(v4, "decision_signature", lambda _value: next(signatures))
    with pytest.raises(v4.MatureS1ConsistencyV4Error, match="decision signatures differ"):
        v4.merge_case_outputs_v4(
            _packet(), [_empty_output() for _ in range(3)], critical_ids={"ANCHOR_CLEAN"}
        )


def test_nonempty_atoms_without_critical_match_fail_closed(valid_semantics: None) -> None:
    output = _empty_output()
    output["global_answers"] = {"NOT_FROZEN_CRITICAL": _verdict()}
    with pytest.raises(v4.MatureS1ConsistencyV4Error, match="do not match frozen critical"):
        v4.merge_case_outputs_v4(
            _packet(empty=False), [copy.deepcopy(output) for _ in range(3)],
            critical_ids={"ANCHOR_CLEAN"},
        )


def test_nonempty_matching_atoms_use_unchanged_merge(
    valid_semantics: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = _empty_output()
    output["global_answers"] = {"ANCHOR_CLEAN": _verdict()}
    sentinel = {"critical_disagreement_paths": [], "critical_atom_fields": 1}
    monkeypatch.setattr(
        v4,
        "conservative_merge_atomic_outputs",
        lambda outputs, critical_ids: (copy.deepcopy(dict(outputs[0])), sentinel),
    )
    merged, metrics, handled = v4.merge_case_outputs_v4(
        _packet(empty=False), [copy.deepcopy(output) for _ in range(3)],
        critical_ids={"ANCHOR_CLEAN"},
    )
    assert handled is False
    assert merged == output
    assert metrics is sentinel


def _touch(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _receipt_kwargs(tmp_path: Path) -> dict:
    artifacts = {}
    for name in (
        "source", "source_manifest", "track_manifest", "assignment_manifest",
        "execution_freeze", "stage_protocol", "research_protocol", "schema",
    ):
        path = tmp_path / f"{name}.json"
        _touch(path, "{}\n")
        artifacts[name] = path
    roots = []
    for run in range(1, 4):
        root = tmp_path / f"run{run}"
        for case in range(v4.EXPECTED_CASES):
            _touch(root / "s0" / "cases" / f"case_{case:03d}.json", f"case-{run}-{case}")
            _touch(
                root / "s0" / "attempts" / f"case_{case:03d}" / "attempt_001.json",
                f"attempt-{run}-{case}",
            )
        roots.append(root)
    return {**artifacts, "run_roots": roots}


@pytest.fixture
def receipt_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    kwargs = _receipt_kwargs(tmp_path)
    records = [{"review_id": f"D-{index}"} for index in range(v4.EXPECTED_CASES)]
    monkeypatch.setattr(
        v4.v3,
        "verify_track",
        lambda **_kwargs: (records, {}, {}, {}, {"status": "PASS"}),
    )
    monkeypatch.setattr(v4.v3, "_reviewer_code_sha256", lambda _path: "a" * 64)
    monkeypatch.setattr(
        v4.v3,
        "validate_run_artifacts",
        lambda _records, _roots, reviewer_code_sha256: ([], []),
    )
    return kwargs


def test_receipt_pins_artifacts_v3_v4_and_complete_run_trees(receipt_environment: dict) -> None:
    receipt = v4.build_technical_correction_receipt(**receipt_environment)
    assert receipt["receipt_version"] == v4.RECEIPT_VERSION
    assert receipt["strategy_changed"] is False
    assert receipt["gate_changed"] is False
    assert receipt["outputs_changed"] is False
    assert receipt["sample_changed"] is False
    assert receipt["model_changed"] is False
    assert receipt["prompt_changed"] is False
    assert receipt["ai_rerun"] is False
    assert receipt["performance_sealed"] is True
    assert set(receipt["pins"]) == {
        "v3_track_manifest", "v3_execution_freeze", "v3_source_manifest",
        "v3_assignment_manifest", "v3_evaluator", "v4_evaluator",
    }
    assert [row["case_files"] for row in receipt["run_trees"]] == [36, 36, 36]
    assert [row["attempt_files"] for row in receipt["run_trees"]] == [36, 36, 36]
    assert receipt["receipt_sha256"] == canonical_sha256(
        {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    )


def test_external_receipt_file_hash_is_mandatory(receipt_environment: dict, tmp_path: Path) -> None:
    receipt = v4.build_technical_correction_receipt(**receipt_environment)
    path = tmp_path / "receipt.json"
    v4.publish_technical_correction_receipt(path, receipt)
    with pytest.raises(v4.MatureS1ConsistencyV4Error, match="external.*hash differs"):
        v4.verify_technical_correction_receipt(
            receipt_path=path,
            expected_receipt_file_sha256="0" * 64,
            **receipt_environment,
        )


def test_run_tree_tamper_invalidates_receipt(receipt_environment: dict, tmp_path: Path) -> None:
    receipt = v4.build_technical_correction_receipt(**receipt_environment)
    path = tmp_path / "receipt.json"
    v4.publish_technical_correction_receipt(path, receipt)
    expected = file_sha256(path)
    _touch(receipt_environment["run_roots"][0] / "s0/cases/case_000.json", "tampered")
    with pytest.raises(v4.MatureS1ConsistencyV4Error, match="receipt pins differ"):
        v4.verify_technical_correction_receipt(
            receipt_path=path,
            expected_receipt_file_sha256=expected,
            **receipt_environment,
        )


def test_receipt_publish_is_immutable(receipt_environment: dict, tmp_path: Path) -> None:
    receipt = v4.build_technical_correction_receipt(**receipt_environment)
    path = tmp_path / "receipt.json"
    first = v4.publish_technical_correction_receipt(path, receipt)
    second = v4.publish_technical_correction_receipt(path, receipt)
    assert first == second == file_sha256(path)
    changed = dict(receipt)
    changed["receipt_sha256"] = "0" * 64
    with pytest.raises(v4.MatureS1ConsistencyV4Error):
        v4.publish_technical_correction_receipt(path, changed)


def test_evaluate_reports_v4_empty_handling_and_receipt_file_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    packet = _packet()
    output = _empty_output()
    record = {
        "review_id": "D-test",
        "packet": packet,
        "packet_sha256": canonical_sha256(packet),
        "model": "gpt-5.6-sol",
        "reasoning_effort": "xhigh",
    }
    stage = {
        "repeatability_acceptance": {
            "schema_and_causal_fields": 1.0,
            "material_permission": 1.0,
            "scenario_and_left_right_phase": 1.0,
            "minimum_unanimous_s1_trade_cases": 0,
            "minimum_unanimous_s1_nontrade_cases": 1,
        }
    }
    signature = {
        "permission": "REMOVE",
        "route": "NO_TRADE",
        "scenario": "NO_TRADE",
        "left_right_phase": None,
    }
    monkeypatch.setattr(v4, "EXPECTED_CASES", 1)
    monkeypatch.setattr(
        v4,
        "verify_technical_correction_receipt",
        lambda **_kwargs: {"receipt_sha256": "a" * 64},
    )
    monkeypatch.setattr(
        v4.v3,
        "verify_track",
        lambda **_kwargs: ([record], stage, {}, {"D-test": "MACRO_DEFENSE_REMOVE"}, {"status": "PASS"}),
    )
    monkeypatch.setattr(v4.v3, "_reviewer_code_sha256", lambda _path: "b" * 64)
    runs = [{"D-test": {"output": copy.deepcopy(output)}} for _ in range(3)]
    monkeypatch.setattr(
        v4.v3,
        "validate_run_artifacts",
        lambda _records, _roots, reviewer_code_sha256: ([{"status": "PASS"}] * 3, runs),
    )
    monkeypatch.setattr(v4, "critical_question_ids", lambda _protocol, _schema: {"ANCHOR_CLEAN"})
    monkeypatch.setattr(v4, "validate_atomic", lambda _packet, _output: [])
    monkeypatch.setattr(v4, "reduce_atomic_v3", lambda _packet, _output: signature)
    monkeypatch.setattr(v4, "decision_signature", lambda value: dict(value))
    schema = tmp_path / "schema.json"
    schema.write_text("{}\n", encoding="utf-8")

    report, ledger = v4.evaluate(
        source=tmp_path / "source",
        source_manifest=tmp_path / "source_manifest",
        track_manifest=tmp_path / "track",
        assignment_manifest=tmp_path / "assignment",
        execution_freeze=tmp_path / "freeze",
        stage_protocol=tmp_path / "stage",
        research_protocol=tmp_path / "protocol",
        schema=schema,
        run_roots=[tmp_path / f"run{number}" for number in range(1, 4)],
        technical_correction_receipt=tmp_path / "receipt",
        expected_receipt_file_sha256="c" * 64,
    )

    assert report["report_version"] == v4.REPORT_VERSION
    assert report["technical_correction"] == {
        "receipt_sha256": "a" * 64,
        "receipt_file_sha256": "c" * 64,
        "reason": v4.EMPTY_REASON,
        "handled_cases": 1,
        "ai_rerun": False,
        "performance_sealed": True,
        "strategy_or_gate_change": False,
    }
    assert report["metrics"]["empty_frozen_manifest_cases"] == 1
    assert report["per_case"][0]["critical_atom_agreement"]["applicable"] is False
    assert ledger[0]["zero_atom_manifest"] is True
    assert ledger[0]["merge_mode"] == "IDENTICAL_ZERO_ATOM_IDENTITY"
