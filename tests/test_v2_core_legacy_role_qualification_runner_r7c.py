"""R7C fixed-key runner: immutable fake transport and budget guard."""

from __future__ import annotations

from pathlib import Path
import shutil

import pytest

from scripts.v2_core_legacy_role_qualification_runner_r7c import (
    RunnerError, output_paths, prepare, run_role, validate_existing,
)
from scripts.v2_core_legacy_role_shortlist_r7 import ARTIFACT_DIR
from tests.test_v2_core_legacy_role_qualification_r7c import REVIEW_ID, ROLE, context, keyed_answer
from tests.test_v2_core_legacy_role_qualification_runner_r7 import attestation, fake_command_for, workspace


def r7c_workspace(tmp_path: Path) -> Path:
    artifact_dir = workspace(tmp_path)
    for name in (
        "v2_core_legacy_role_qualification.prompt.candidate_r7b.md",
        "v2_core_legacy_role_qualification.transport_addendum.candidate_r7c.md",
        f"legacy_anchor_alignment_input_packets_candidate_r1/{REVIEW_ID}.json",
        f"legacy_role_shortlists_candidate_r7_v2/{REVIEW_ID}.json",
        f"legacy_role_qualification_runs_candidate_r7/CURRENT_EPISODE_UP/{REVIEW_ID}.json",
        f"legacy_role_qualification_runs_candidate_r7/CURRENT_EPISODE_UP/{REVIEW_ID}.raw.json",
        f"legacy_role_qualification_runs_candidate_r7/CURRENT_EPISODE_UP/{REVIEW_ID}.receipt.json",
    ):
        destination = artifact_dir / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ARTIFACT_DIR / name, destination)
    return artifact_dir


def test_r7c_fake_transport_and_receipt_revalidate(tmp_path: Path) -> None:
    artifact_dir = r7c_workspace(tmp_path)
    manifest = prepare(artifact_dir, REVIEW_ID)
    packet, shortlist, episode = context()
    response = keyed_answer(packet, shortlist, episode)
    result = run_role(
        artifact_dir=artifact_dir, review_id=REVIEW_ID, role=ROLE,
        usage_attestation_path=attestation(tmp_path / "usage.json"),
        run_command=fake_command_for(response),
    )
    assert manifest["frozen_episode_candidate_id"] == episode["selected_candidate_id"]
    assert result["status"] == "VALID"
    assert result["transport_version"] == "v2-core-legacy-role-qualification-r7c-candidate-r1"
    assert validate_existing(artifact_dir=artifact_dir, review_id=REVIEW_ID, role=ROLE) == result
    with pytest.raises(RunnerError, match="output exists"):
        run_role(
            artifact_dir=artifact_dir, review_id=REVIEW_ID, role=ROLE,
            usage_attestation_path=attestation(tmp_path / "usage2.json"),
            run_command=fake_command_for(response),
        )


def test_r7c_budget_stop_prevents_transport(tmp_path: Path) -> None:
    artifact_dir = r7c_workspace(tmp_path)
    prepare(artifact_dir, REVIEW_ID)

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("transport must not run")

    with pytest.raises(RunnerError, match="BUDGET_STOP"):
        run_role(
            artifact_dir=artifact_dir, review_id=REVIEW_ID, role=ROLE,
            usage_attestation_path=attestation(tmp_path / "usage.json", remaining=70),
            run_command=forbidden,
        )
    output, receipt = output_paths(artifact_dir, REVIEW_ID, ROLE)
    assert not output.exists()
    assert not receipt.exists()
