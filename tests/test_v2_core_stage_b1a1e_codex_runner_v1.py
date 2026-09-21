from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess

import pytest

from scripts.v2_core_stage_b1a1e_codex_runner_v1 import (
    MODEL,
    REASONING,
    StageB1a1ERunnerError,
    run_stage_b1a1e_case,
    validate_existing_stage_b1a1e_artifacts,
)
from scripts.v2_core_stage_b1a1e_focus_input_v1 import ARTIFACT_DIR, generate_all
from scripts.v2_core_stage_b1a_codex_runner_v1 import raw_output_path


def _paths():
    manifest = generate_all(ARTIFACT_DIR)
    row = manifest["rows"][0]
    return {
        "manifest": ARTIFACT_DIR / "stage_b1a1e_input_manifest_candidate_r1.json",
        "input": ARTIFACT_DIR
        / "stage_b1a1e_input_packets_candidate_r1"
        / row["input_packet_file"],
        "schema": ARTIFACT_DIR / "v2_core_stage_b1a1e.schema.candidate_r1.json",
        "prompt": ARTIFACT_DIR / "v2_core_stage_b1a1e.prompt.candidate_r1.md",
    }


def _response(input_path: Path):
    packet = json.loads(input_path.read_text(encoding="utf-8"))
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


def _attestation(path: Path, remaining: float):
    path.write_text(
        json.dumps(
            {
                "source": "CODEX_APP_GET_USAGE_LIMITS",
                "limit_id": "codex",
                "used_percent": 100 - remaining,
                "remaining_percent": remaining,
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "check_id": "test-b1a1e-r1",
            }
        ),
        encoding="utf-8",
    )


def test_fake_transport_persists_and_revalidates(tmp_path: Path):
    paths = _paths()
    response = _response(paths["input"])
    usage = tmp_path / "usage.json"
    _attestation(usage, 40)
    output = tmp_path / "stage_b1a1e" / "case.json"
    receipt = tmp_path / "receipts" / "case.json"
    commands = []

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        target = Path(command[command.index("--output-last-message") + 1])
        target.write_text(json.dumps(response, ensure_ascii=False), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    result = run_stage_b1a1e_case(
        input_packet_path=paths["input"],
        input_manifest_path=paths["manifest"],
        schema_path=paths["schema"],
        prompt_path=paths["prompt"],
        usage_attestation_path=usage,
        stop_remaining_percent_lte=25,
        output_path=output,
        receipt_path=receipt,
        timeout_seconds=30,
        run_command=fake_run,
    )
    assert result["status"] == "VALID"
    assert raw_output_path(output).is_file()
    assert validate_existing_stage_b1a1e_artifacts(
        input_packet_path=paths["input"],
        input_manifest_path=paths["manifest"],
        schema_path=paths["schema"],
        prompt_path=paths["prompt"],
        output_path=output,
        receipt_path=receipt,
    ) == result
    assert commands[0][commands[0].index("--model") + 1] == MODEL
    assert f'model_reasoning_effort="{REASONING}"' in commands[0]


def test_budget_threshold_stops_before_transport(tmp_path: Path):
    paths = _paths()
    usage = tmp_path / "usage.json"
    _attestation(usage, 25)
    called = False

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        nonlocal called
        called = True
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    with pytest.raises(StageB1a1ERunnerError, match="BUDGET_STOP"):
        run_stage_b1a1e_case(
            input_packet_path=paths["input"],
            input_manifest_path=paths["manifest"],
            schema_path=paths["schema"],
            prompt_path=paths["prompt"],
            usage_attestation_path=usage,
            stop_remaining_percent_lte=25,
            output_path=tmp_path / "output.json",
            receipt_path=tmp_path / "receipt.json",
            timeout_seconds=30,
            run_command=fake_run,
        )
    assert called is False
