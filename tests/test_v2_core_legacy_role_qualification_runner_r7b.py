"""R7B frozen-episode transport and receipt checks."""

from __future__ import annotations

from pathlib import Path
import shutil

import pytest

from scripts.v2_core_legacy_role_qualification_runner_r7b import (
    RunnerError,
    prepare,
    run_role,
    validate_existing,
)
from scripts.v2_core_legacy_role_shortlist_r7 import ARTIFACT_DIR
from tests.test_v2_core_legacy_role_qualification_r7 import REVIEW_ID, data
from tests.test_v2_core_legacy_role_qualification_r7b import bound_answer
from tests.test_v2_core_legacy_role_qualification_runner_r7 import attestation, fake_command_for, workspace


def bound_workspace(tmp_path: Path) -> Path:
    artifact_dir = workspace(tmp_path)
    files = [
        "v2_core_legacy_role_adjudication.prompt.candidate_r7.md",
        "v2_core_legacy_role_qualification.prompt.candidate_r7b.md",
        f"legacy_role_qualification_runs_candidate_r7/CURRENT_EPISODE_UP/{REVIEW_ID}.json",
        f"legacy_role_qualification_runs_candidate_r7/CURRENT_EPISODE_UP/{REVIEW_ID}.raw.json",
        f"legacy_role_qualification_runs_candidate_r7/CURRENT_EPISODE_UP/{REVIEW_ID}.receipt.json",
        f"legacy_role_adjudication_manifests_candidate_r7/CURRENT_EPISODE_UP/{REVIEW_ID}.json",
        f"legacy_role_adjudication_runs_candidate_r7/CURRENT_EPISODE_UP/{REVIEW_ID}.json",
        f"legacy_role_adjudication_runs_candidate_r7/CURRENT_EPISODE_UP/{REVIEW_ID}.raw.json",
        f"legacy_role_adjudication_runs_candidate_r7/CURRENT_EPISODE_UP/{REVIEW_ID}.receipt.json",
    ]
    for name in files:
        destination = artifact_dir / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ARTIFACT_DIR / name, destination)
    return artifact_dir


def test_r7b_fake_transport_binds_episode_and_revalidates(tmp_path: Path) -> None:
    artifact_dir = bound_workspace(tmp_path)
    manifest = prepare(artifact_dir, REVIEW_ID)
    packet, shortlist = data()
    role = "ACTIVE_DOWN_CONTROLLER"
    response = bound_answer(packet, shortlist, role)
    result = run_role(
        artifact_dir=artifact_dir,
        review_id=REVIEW_ID,
        role=role,
        usage_attestation_path=attestation(tmp_path / "usage.json"),
        run_command=fake_command_for(response),
    )
    assert result["status"] == "VALID"
    assert result["bound_current_episode_candidate_id"] == manifest["frozen_episode_candidate_id"]
    assert validate_existing(artifact_dir=artifact_dir, review_id=REVIEW_ID, role=role) == result


def test_r7b_budget_stop_prevents_transport(tmp_path: Path) -> None:
    artifact_dir = bound_workspace(tmp_path)
    prepare(artifact_dir, REVIEW_ID)

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("transport must not run at stop line")

    with pytest.raises(RunnerError, match="BUDGET_STOP"):
        run_role(
            artifact_dir=artifact_dir,
            review_id=REVIEW_ID,
            role="ACTIVE_DOWN_CONTROLLER",
            usage_attestation_path=attestation(tmp_path / "usage.json", remaining=70),
            run_command=forbidden,
        )
