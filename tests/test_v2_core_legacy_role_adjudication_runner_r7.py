"""R7 adjudication fake transport never alters frozen role qualification."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.v2_core_legacy_role_adjudication_runner_r7 import (
    RunnerError,
    output_paths,
    prepare,
    run_adjudication,
    validate_existing,
)
from scripts.v2_core_legacy_role_qualification_runner_r7 import run_role
from tests.test_v2_core_legacy_role_adjudication_r7 import ROLE, response_for
from tests.test_v2_core_legacy_role_qualification_r7 import REVIEW_ID, answer, data
from tests.test_v2_core_legacy_role_qualification_runner_r7 import attestation, fake_command_for, workspace


def prepared_ambiguity(tmp_path: Path) -> tuple[Path, dict, dict, dict]:
    artifact_dir = workspace(tmp_path)
    prompt = "v2_core_legacy_role_adjudication.prompt.candidate_r7.md"
    from scripts.v2_core_legacy_role_shortlist_r7 import ARTIFACT_DIR
    (artifact_dir / prompt).write_bytes((ARTIFACT_DIR / prompt).read_bytes())
    packet, shortlist = data()
    qualification = run_role(
        artifact_dir=artifact_dir,
        review_id=REVIEW_ID,
        role=ROLE,
        usage_attestation_path=attestation(tmp_path / "usage-stage1.json"),
        run_command=fake_command_for(answer(packet, shortlist, ROLE, (0, 1))),
    )
    prepare(artifact_dir, REVIEW_ID, ROLE)
    return artifact_dir, packet, shortlist, qualification


def test_fake_adjudication_writes_receipt_and_revalidates(tmp_path: Path) -> None:
    artifact_dir, packet, shortlist, qualification = prepared_ambiguity(tmp_path)
    chosen = qualification["eligible_candidate_ids"][0]
    response = response_for(packet, shortlist, qualification, {chosen})
    result = run_adjudication(
        artifact_dir=artifact_dir,
        review_id=REVIEW_ID,
        role=ROLE,
        usage_attestation_path=attestation(tmp_path / "usage-stage2.json"),
        run_command=fake_command_for(response),
    )
    assert result["status"] == "VALID"
    assert result["selected_candidate_id"] == chosen
    assert validate_existing(artifact_dir=artifact_dir, review_id=REVIEW_ID, role=ROLE) == result
    with pytest.raises(RunnerError, match="output exists"):
        run_adjudication(
            artifact_dir=artifact_dir,
            review_id=REVIEW_ID,
            role=ROLE,
            usage_attestation_path=attestation(tmp_path / "usage-again.json"),
            run_command=fake_command_for(response),
        )


def test_adjudication_receipt_tamper_detected(tmp_path: Path) -> None:
    artifact_dir, packet, shortlist, qualification = prepared_ambiguity(tmp_path)
    response = response_for(packet, shortlist, qualification, set())
    run_adjudication(
        artifact_dir=artifact_dir,
        review_id=REVIEW_ID,
        role=ROLE,
        usage_attestation_path=attestation(tmp_path / "usage-stage2.json"),
        run_command=fake_command_for(response),
    )
    _output, receipt_path = output_paths(artifact_dir, REVIEW_ID, ROLE)
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["qualification_output_sha256"] = "0" * 64
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(RunnerError, match="receipt mismatch: qualification_output_sha256"):
        validate_existing(artifact_dir=artifact_dir, review_id=REVIEW_ID, role=ROLE)
