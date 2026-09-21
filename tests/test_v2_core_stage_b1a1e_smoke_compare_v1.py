from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess

from scripts.v2_core_stage_b1a1e_codex_runner_v1 import run_stage_b1a1e_case
from scripts.v2_core_stage_b1a1e_focus_input_v1 import ARTIFACT_DIR
from scripts.v2_core_stage_b1a1e_smoke_compare_v1 import compare_smoke
from scripts.v2_core_stage_b1a1e_smoke_manifest_v1 import build_manifest


def _response(packet: dict):
    options = {}
    for option in packet["evidence_options"]:
        options.setdefault(option["candidate_id"], {}).setdefault(
            option["atom_name"], []
        ).append(option)
    rows = []
    for segment in packet["selected_focus_segments"]:
        candidate_id = segment["candidate_id"]
        directional = options[candidate_id]["directional_coherence"][0]
        targets = options[candidate_id]["structural_challenge_or_break"]
        rows.append(
            {
                "candidate_id": candidate_id,
                "directional_path": {
                    "evidence_option_id": directional["evidence_option_id"],
                    "judgement": "INSUFFICIENT",
                    "reason_code": "REQUIRED_EVIDENCE_INSUFFICIENT",
                },
                "structural_targets": [
                    {
                        "evidence_option_id": option["evidence_option_id"],
                        "same_level_meaningful_target": {
                            "judgement": "INSUFFICIENT",
                            "reason_code": "REQUIRED_EVIDENCE_INSUFFICIENT",
                        },
                        "challenge_or_break_realized": {
                            "judgement": (
                                "INSUFFICIENT"
                                if option["target"]["price_reached_or_crossed_objective"]
                                else "CONTRADICTS"
                            ),
                            "reason_code": (
                                "REQUIRED_EVIDENCE_INSUFFICIENT"
                                if option["target"]["price_reached_or_crossed_objective"]
                                else "VISIBLE_EVIDENCE_CONTRADICTS"
                            ),
                        },
                    }
                    for option in targets
                ],
            }
        )
    return {
        "schema_version": "v2-core-stage-b1a1e-focus-r1-candidate",
        "candidate_perceptions": rows,
    }


def _attestation(path: Path):
    path.write_text(
        json.dumps(
            {
                "source": "CODEX_APP_GET_USAGE_LIMITS",
                "limit_id": "codex",
                "used_percent": 50,
                "remaining_percent": 50,
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "check_id": "test-b1a1e-smoke",
            }
        ),
        encoding="utf-8",
    )


def test_preflight_is_incomplete_and_does_not_publish_metrics(tmp_path: Path):
    manifest = build_manifest(ARTIFACT_DIR)
    report = compare_smoke(
        manifest_path=ARTIFACT_DIR
        / "stage_b1a1e_smoke_execution_manifest_candidate_r1.json",
        artifact_dir=ARTIFACT_DIR,
        run_dir=tmp_path,
    )
    assert manifest["status"] == "READY_FOR_FORMAL_AI_SMOKE（可執行正式AI Smoke）"
    assert report["status"] == "NOT_READY_AI_RUNS_INCOMPLETE（AI三輪尚未完成）"
    assert report["completed_case_rounds"] == 0
    assert report["metrics_published"] is False
    assert report["metrics"] is None


def test_three_identical_fake_rounds_pass_and_are_revalidated(tmp_path: Path):
    manifest = build_manifest(ARTIFACT_DIR)
    manifest_path = (
        ARTIFACT_DIR / "stage_b1a1e_smoke_execution_manifest_candidate_r1.json"
    )
    input_manifest_path = ARTIFACT_DIR / manifest["input_manifest_file"]
    schema_path = ARTIFACT_DIR / manifest["schema_file"]
    prompt_path = ARTIFACT_DIR / manifest["prompt_file"]
    usage = tmp_path / "usage.json"
    _attestation(usage)

    for round_number in (1, 2, 3):
        for row in manifest["rows"]:
            input_path = (
                ARTIFACT_DIR
                / manifest["input_packet_directory"]
                / row["input_packet_file"]
            )
            packet = json.loads(input_path.read_text(encoding="utf-8"))
            response = _response(packet)

            def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                target = Path(command[command.index("--output-last-message") + 1])
                target.write_text(json.dumps(response, ensure_ascii=False), encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            output = (
                tmp_path
                / f"round_{round_number}"
                / "stage_b1a1e"
                / f"{row['review_id']}.json"
            )
            receipt = (
                tmp_path
                / f"round_{round_number}"
                / "receipts"
                / f"{row['review_id']}.stage_b1a1e.json"
            )
            run_stage_b1a1e_case(
                input_packet_path=input_path,
                input_manifest_path=input_manifest_path,
                schema_path=schema_path,
                prompt_path=prompt_path,
                usage_attestation_path=usage,
                stop_remaining_percent_lte=25,
                output_path=output,
                receipt_path=receipt,
                timeout_seconds=30,
                run_command=fake_run,
            )

    report = compare_smoke(
        manifest_path=manifest_path,
        artifact_dir=ARTIFACT_DIR,
        run_dir=tmp_path,
    )
    assert report["status"] == "SMOKE_PASSED（Smoke通過）"
    assert report["completed_case_rounds"] == 12
    assert report["metrics"]["ai_perception_consistency_percent"] == 100.0
    assert report["metrics"]["derived_atom_consistency_percent"] == 100.0
    assert report["metrics"]["primary_option_consistency_percent"] == 100.0
    assert report["metrics"]["partial_eligible_set_consistency_percent"] == 100.0


def test_invalid_raw_is_terminal_protocol_failure(tmp_path: Path):
    manifest = build_manifest(ARTIFACT_DIR)
    first = manifest["rows"][0]
    invalid = (
        tmp_path
        / "round_1"
        / "stage_b1a1e"
        / f"{first['review_id']}.invalid.raw"
    )
    invalid.parent.mkdir(parents=True, exist_ok=True)
    invalid.write_text("invalid", encoding="utf-8")
    report = compare_smoke(
        manifest_path=ARTIFACT_DIR
        / "stage_b1a1e_smoke_execution_manifest_candidate_r1.json",
        artifact_dir=ARTIFACT_DIR,
        run_dir=tmp_path,
    )
    assert report["status"] == "SMOKE_PROTOCOL_FAILED（Smoke協定失敗）"
    assert report["metrics_published"] is False
    assert len(report["invalid_artifacts"]) == 1
