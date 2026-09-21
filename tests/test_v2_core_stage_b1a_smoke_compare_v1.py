from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess

from scripts.v2_core_stage_b1a_codex_runner_v1 import run_stage_b1a_case
from scripts.v2_core_stage_b1a_smoke_compare_v1 import compare_smoke, render_markdown


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_DIR = (
    ROOT
    / "reports"
    / "course_backtest"
    / "2026-09-10"
    / "v2_core_reproducible_goal_v1"
)
MANIFEST_PATH = ARTIFACT_DIR / "stage_b1a_smoke_execution_manifest_candidate_r1.json"


def _unknown_atomic() -> dict:
    return {
        "result": "UNKNOWN",
        "supporting_evidence_refs": [],
        "contradicting_evidence_refs": [],
        "missing_evidence_codes": ["OTHER_REQUIRED_EVIDENCE"],
        "reason_code": "REQUIRED_EVIDENCE_MISSING",
    }


def _pass_atomic(ref: str) -> dict:
    return {
        "result": "PASS",
        "supporting_evidence_refs": [ref],
        "contradicting_evidence_refs": [],
        "missing_evidence_codes": [],
        "reason_code": "VISIBLE_EVIDENCE_SATISFIES_DEFINITION",
    }


def _response(focus_path: Path, *, change_first: bool, as_of: str) -> dict:
    focus = json.loads(focus_path.read_text(encoding="utf-8"))
    rows = [
        {
            "candidate_id": row["segment_id"],
            "coarse_anchor_fit": _unknown_atomic(),
            "as_of_structural_relevance": _unknown_atomic(),
            "role_assignability": _unknown_atomic(),
            "role_candidates": [],
        }
        for row in focus["focus_segments"]
    ]
    if change_first:
        ref = f"PRICE:{as_of}"
        rows[0]["coarse_anchor_fit"] = _pass_atomic(ref)
        rows[0]["as_of_structural_relevance"] = _pass_atomic(ref)
        rows[0]["role_assignability"] = _pass_atomic(ref)
        rows[0]["role_candidates"] = ["ALTERNATIVE"]
    return {
        "schema_version": "v2-core-stage-b1a-r1-candidate",
        "segment_assessments": rows,
    }


def _write_attestation(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "source": "CODEX_APP_GET_USAGE_LIMITS",
                "limit_id": "codex",
                "used_percent": 28,
                "remaining_percent": 72,
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "check_id": "smoke-compare-test",
            }
        ),
        encoding="utf-8",
    )


def _materialize_fake_runs(tmp_path: Path, *, divergent_case_count: int = 0) -> Path:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    run_dir = tmp_path / "runs"
    usage = tmp_path / "usage.json"
    _write_attestation(usage)
    for case_index, row in enumerate(manifest["rows"]):
        review_id = row["review_id"]
        input_path = (
            ARTIFACT_DIR
            / "stage_b1a_input_packets_candidate_r1"
            / row["input_packet_file"]
        )
        candidate_path = ARTIFACT_DIR / "objective_candidate_catalogs_r1" / f"{review_id}.json"
        focus_path = ARTIFACT_DIR / "objective_focus_catalogs_r1" / f"{review_id}.json"
        for round_number in range(1, 4):
            response = _response(
                focus_path,
                change_first=case_index < divergent_case_count and round_number == 3,
                as_of=row["as_of"],
            )

            def fake_run(
                command: list[str],
                *,
                _response: dict = response,
                **_: object,
            ) -> subprocess.CompletedProcess[str]:
                target = Path(command[command.index("--output-last-message") + 1])
                target.write_text(
                    json.dumps(_response, ensure_ascii=False), encoding="utf-8"
                )
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            output = run_dir / f"round_{round_number}" / "stage_b1a" / f"{review_id}.json"
            receipt = (
                run_dir
                / f"round_{round_number}"
                / "receipts"
                / f"{review_id}.stage_b1a.json"
            )
            run_stage_b1a_case(
                input_packet_path=input_path,
                candidate_catalog_path=candidate_path,
                focus_catalog_path=focus_path,
                schema_path=ARTIFACT_DIR / manifest["output_schema_file"],
                prompt_path=ARTIFACT_DIR / manifest["prompt_file"],
                usage_attestation_path=usage,
                stop_remaining_percent_lte=70,
                output_path=output,
                receipt_path=receipt,
                timeout_seconds=30,
                run_command=fake_run,
            )
    return run_dir


def test_incomplete_run_publishes_no_partial_consistency(tmp_path: Path):
    report = compare_smoke(
        manifest_path=MANIFEST_PATH,
        artifact_dir=ARTIFACT_DIR,
        run_dir=tmp_path / "missing",
    )
    assert report["status"] == "NOT_READY_AI_RUNS_INCOMPLETE（AI三輪尚未完成）"
    assert report["completed_case_rounds"] == 0
    assert report["metrics_published"] is False
    assert report["metrics"] is None
    assert "不發布部分一致率" in render_markdown(report)


def test_three_identical_rounds_pass_smoke(tmp_path: Path):
    run_dir = _materialize_fake_runs(tmp_path)
    report = compare_smoke(
        manifest_path=MANIFEST_PATH,
        artifact_dir=ARTIFACT_DIR,
        run_dir=run_dir,
    )
    assert report["status"] == "SMOKE_PASSED（Smoke通過）"
    assert report["completed_case_rounds"] == 12
    assert report["metrics"]["atomic_result_consistency_percent"] == 100.0
    assert report["metrics"]["role_set_consistency_percent"] == 100.0
    assert report["metrics"]["deep_review_pool_consistency_percent"] == 100.0


def test_two_divergent_case_pools_fail_smoke(tmp_path: Path):
    run_dir = _materialize_fake_runs(tmp_path, divergent_case_count=2)
    report = compare_smoke(
        manifest_path=MANIFEST_PATH,
        artifact_dir=ARTIFACT_DIR,
        run_dir=run_dir,
    )
    assert report["status"] == "SMOKE_FEASIBILITY_FAILED（Smoke可行性失敗）"
    assert report["metrics"]["deep_review_pool_consistency_percent"] == 50.0
    assert report["future_performance_used"] is False
    assert report["identity_used"] is False
    assert report["sealed_labels_used"] is False


def test_tampered_complete_artifact_is_protocol_failure(tmp_path: Path):
    run_dir = _materialize_fake_runs(tmp_path)
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    review_id = manifest["rows"][0]["review_id"]
    output = run_dir / "round_1" / "stage_b1a" / f"{review_id}.json"
    value = json.loads(output.read_text(encoding="utf-8"))
    value["segment_assessments"].reverse()
    output.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    report = compare_smoke(
        manifest_path=MANIFEST_PATH,
        artifact_dir=ARTIFACT_DIR,
        run_dir=run_dir,
    )
    assert report["status"] == "SMOKE_PROTOCOL_FAILED（Smoke協定失敗）"
    assert report["metrics_published"] is False
    assert any("normalized_output_sha256" in error for error in report["errors"])
