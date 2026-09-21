"""R7B relative-role runner keeps the episode and source outputs immutable."""

from __future__ import annotations

from pathlib import Path
import shutil

import pytest

from scripts.v2_core_legacy_role_adjudication_runner_r7b import RunnerError, prepare, run_adjudication, validate_existing
from scripts.v2_core_legacy_role_shortlist_r7 import ARTIFACT_DIR
from tests.test_v2_core_legacy_role_adjudication_r7b import REVIEW_ID, ROLE, answer, context
from tests.test_v2_core_legacy_role_qualification_runner_r7 import attestation, fake_command_for, workspace


def adjudication_workspace(tmp_path: Path) -> Path:
    artifact_dir = workspace(tmp_path)
    files = [
        "v2_core_legacy_role_qualification.prompt.candidate_r7b.md",
        "v2_core_legacy_role_adjudication.prompt.candidate_r7b.md",
        f"legacy_anchor_alignment_input_packets_candidate_r1/{REVIEW_ID}.json",
        f"legacy_role_shortlists_candidate_r7_v2/{REVIEW_ID}.json",
        f"legacy_role_qualification_runs_candidate_r7/CURRENT_EPISODE_UP/{REVIEW_ID}.json",
        f"legacy_role_qualification_runs_candidate_r7/CURRENT_EPISODE_UP/{REVIEW_ID}.raw.json",
        f"legacy_role_qualification_runs_candidate_r7/CURRENT_EPISODE_UP/{REVIEW_ID}.receipt.json",
        f"legacy_role_qualification_manifests_candidate_r7b/{REVIEW_ID}.json",
        f"legacy_role_qualification_runs_candidate_r7b/{ROLE}/{REVIEW_ID}.json",
        f"legacy_role_qualification_runs_candidate_r7b/{ROLE}/{REVIEW_ID}.raw.json",
        f"legacy_role_qualification_runs_candidate_r7b/{ROLE}/{REVIEW_ID}.receipt.json",
    ]
    for name in files:
        destination = artifact_dir / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ARTIFACT_DIR / name, destination)
    return artifact_dir


def test_fake_adjudication_revalidates_receipt(tmp_path: Path) -> None:
    artifact_dir = adjudication_workspace(tmp_path)
    prepare(artifact_dir, REVIEW_ID, ROLE)
    packet, shortlist, qualification, episode = context()
    result = run_adjudication(
        artifact_dir=artifact_dir,
        review_id=REVIEW_ID,
        role=ROLE,
        usage_attestation_path=attestation(tmp_path / "usage.json"),
        run_command=fake_command_for(answer(packet, shortlist, qualification, episode)),
    )
    assert result["status"] == "VALID"
    assert result["bound_current_episode_candidate_id"] == episode["selected_candidate_id"]
    assert validate_existing(artifact_dir=artifact_dir, review_id=REVIEW_ID, role=ROLE) == result


def test_budget_stop_prevents_adjudication_call(tmp_path: Path) -> None:
    artifact_dir = adjudication_workspace(tmp_path)
    prepare(artifact_dir, REVIEW_ID, ROLE)

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("formal transport should not run")

    with pytest.raises(RunnerError, match="BUDGET_STOP"):
        run_adjudication(
            artifact_dir=artifact_dir,
            review_id=REVIEW_ID,
            role=ROLE,
            usage_attestation_path=attestation(tmp_path / "usage.json", remaining=70),
            run_command=forbidden,
        )
