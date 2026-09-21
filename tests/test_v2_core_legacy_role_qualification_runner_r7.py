"""R7 fake-transport and immutable receipt regression tests."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from scripts.v2_core_legacy_role_qualification_manifest_r7 import (
    BUDGET_FILE,
    INPUT_MANIFEST,
    OUTPUT_MANIFEST,
    PROMPT_FILE,
    SHORTLIST_MANIFEST,
)
from scripts.v2_core_legacy_role_qualification_runner_r7 import (
    RunnerError,
    output_paths,
    run_role,
    validate_existing,
)
from scripts.v2_core_legacy_role_shortlist_r7 import ARTIFACT_DIR
from tests.test_v2_core_legacy_role_qualification_r7 import REVIEW_ID, answer, data


ROLE = "CURRENT_EPISODE_UP"


def workspace(tmp_path: Path) -> Path:
    for name in (BUDGET_FILE, INPUT_MANIFEST, SHORTLIST_MANIFEST, OUTPUT_MANIFEST, PROMPT_FILE):
        shutil.copyfile(ARTIFACT_DIR / name, tmp_path / name)
    input_dir = tmp_path / "legacy_anchor_alignment_input_packets_candidate_r1"
    shortlist_dir = tmp_path / "legacy_role_shortlists_candidate_r7_v2"
    input_dir.mkdir()
    shortlist_dir.mkdir()
    shutil.copyfile(ARTIFACT_DIR / input_dir.name / f"{REVIEW_ID}.json", input_dir / f"{REVIEW_ID}.json")
    shutil.copyfile(ARTIFACT_DIR / shortlist_dir.name / f"{REVIEW_ID}.json", shortlist_dir / f"{REVIEW_ID}.json")
    return tmp_path


def attestation(path: Path, remaining: int = 98) -> Path:
    payload = {
        "source": "CODEX_APP_GET_USAGE_LIMITS",
        "limit_id": "codex",
        "used_percent": 100 - remaining,
        "remaining_percent": remaining,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "check_id": "fake-transport-only",
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def fake_command_for(payload: dict):
    def fake_command(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text(json.dumps(payload), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
    return fake_command


def test_fake_transport_writes_and_revalidates_bound_outputs(tmp_path: Path) -> None:
    artifact_dir = workspace(tmp_path)
    packet, shortlist = data()
    response = answer(packet, shortlist, ROLE, (0,))
    result = run_role(
        artifact_dir=artifact_dir,
        review_id=REVIEW_ID,
        role=ROLE,
        usage_attestation_path=attestation(tmp_path / "usage.json"),
        run_command=fake_command_for(response),
    )
    assert result["status"] == "VALID"
    assert result["role_status"] == "SELECTED"
    assert validate_existing(artifact_dir=artifact_dir, review_id=REVIEW_ID, role=ROLE) == result
    with pytest.raises(RunnerError, match="already exists"):
        run_role(
            artifact_dir=artifact_dir,
            review_id=REVIEW_ID,
            role=ROLE,
            usage_attestation_path=attestation(tmp_path / "usage2.json"),
            run_command=fake_command_for(response),
        )


def test_budget_stop_prevents_transport_call(tmp_path: Path) -> None:
    artifact_dir = workspace(tmp_path)
    called = False

    def forbidden_command(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal called
        called = True
        raise AssertionError("transport must not be called")

    with pytest.raises(RunnerError, match="BUDGET_STOP"):
        run_role(
            artifact_dir=artifact_dir,
            review_id=REVIEW_ID,
            role=ROLE,
            usage_attestation_path=attestation(tmp_path / "usage.json", remaining=70),
            run_command=forbidden_command,
        )
    assert called is False


def test_local_invalid_keeps_only_immutable_invalid_raw(tmp_path: Path) -> None:
    artifact_dir = workspace(tmp_path)
    packet, shortlist = data()
    response = answer(packet, shortlist, ROLE)
    response["candidate_assessments"][0]["evidence_option_id"] = response["candidate_assessments"][1]["evidence_option_id"]
    with pytest.raises(RunnerError, match="local validation failed"):
        run_role(
            artifact_dir=artifact_dir,
            review_id=REVIEW_ID,
            role=ROLE,
            usage_attestation_path=attestation(tmp_path / "usage.json"),
            run_command=fake_command_for(response),
        )
    output, receipt = output_paths(artifact_dir, REVIEW_ID, ROLE)
    assert not output.exists()
    assert not receipt.exists()
    assert output.with_name(output.stem + ".invalid.raw").is_file()


def test_receipt_tamper_detected(tmp_path: Path) -> None:
    artifact_dir = workspace(tmp_path)
    packet, shortlist = data()
    run_role(
        artifact_dir=artifact_dir,
        review_id=REVIEW_ID,
        role=ROLE,
        usage_attestation_path=attestation(tmp_path / "usage.json"),
        run_command=fake_command_for(answer(packet, shortlist, ROLE, (0,))),
    )
    _output, receipt_path = output_paths(artifact_dir, REVIEW_ID, ROLE)
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["shortlist_sha256"] = "0" * 64
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(RunnerError, match="receipt mismatch: shortlist_sha256"):
        validate_existing(artifact_dir=artifact_dir, review_id=REVIEW_ID, role=ROLE)
