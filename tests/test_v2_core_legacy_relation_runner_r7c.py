"""R7C mixed diagnostic relation fake transport and immutable receipt."""

from __future__ import annotations

from pathlib import Path
import shutil

import pytest

from scripts.v2_core_legacy_relation_runner_r7c import RunnerError, prepare, run_relation, validate_existing
from scripts.v2_core_legacy_role_cross_object_preflight_r7c import build_report
from scripts.v2_core_legacy_role_qualification_runner_r7 import _bound_inputs
from scripts.v2_core_legacy_role_shortlist_r7 import ARTIFACT_DIR
from tests.test_v2_core_legacy_relation_r7b import answer
from tests.test_v2_core_legacy_role_qualification_r7c import REVIEW_ID
from tests.test_v2_core_legacy_role_qualification_runner_r7 import attestation, fake_command_for
from tests.test_v2_core_legacy_role_qualification_runner_r7c import r7c_workspace


def relation_workspace(tmp_path: Path) -> Path:
    artifact_dir = r7c_workspace(tmp_path)
    files = [
        "v2_core_legacy_relation.prompt.candidate_r7b.md",
        "v2_core_legacy_role_adjudication.prompt.candidate_r7b.md",
        f"legacy_role_qualification_manifests_candidate_r7b/{REVIEW_ID}.json",
        f"legacy_role_qualification_manifests_candidate_r7c/{REVIEW_ID}.json",
        f"legacy_role_cross_object_preflight_candidate_r7c/{REVIEW_ID}.json",
        f"legacy_role_adjudication_manifests_candidate_r7b/UP_CONTROL_CHALLENGER/{REVIEW_ID}.json",
    ]
    for role in ("ACTIVE_DOWN_CONTROLLER", "IMMEDIATE_COMPLETED_UP_PARENT", "UP_CONTROL_CHALLENGER"):
        for suffix in (".json", ".raw.json", ".receipt.json"):
            files.append(f"legacy_role_qualification_runs_candidate_r7b/{role}/{REVIEW_ID}{suffix}")
    for role, directory in (
        ("UP_CONTROL_CHALLENGER", "legacy_role_adjudication_runs_candidate_r7b"),
        ("CONTROLLING_MATURE_CAMPAIGN", "legacy_role_qualification_runs_candidate_r7c"),
    ):
        for suffix in (".json", ".raw.json", ".receipt.json"):
            files.append(f"{directory}/{role}/{REVIEW_ID}{suffix}")
    for name in files:
        destination = artifact_dir / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ARTIFACT_DIR / name, destination)
    return artifact_dir


def test_mixed_relation_fake_transport_and_revalidation(tmp_path: Path) -> None:
    artifact_dir = relation_workspace(tmp_path)
    manifest = prepare(artifact_dir, REVIEW_ID)
    _source, _row, packet, _shortlist = _bound_inputs(artifact_dir, REVIEW_ID, "CURRENT_EPISODE_UP")
    report = build_report(artifact_dir, REVIEW_ID)
    result = run_relation(
        artifact_dir=artifact_dir, review_id=REVIEW_ID,
        usage_attestation_path=attestation(tmp_path / "usage.json"),
        run_command=fake_command_for(answer(report, packet)),
    )
    assert manifest["mixed_version_diagnostic_only"] is True
    assert result["status"] == "VALID"
    assert result["trade_permission_granted"] is False
    assert validate_existing(artifact_dir=artifact_dir, review_id=REVIEW_ID) == result


def test_mixed_relation_budget_stop(tmp_path: Path) -> None:
    artifact_dir = relation_workspace(tmp_path)
    prepare(artifact_dir, REVIEW_ID)

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("transport must not run")

    with pytest.raises(RunnerError, match="BUDGET_STOP"):
        run_relation(
            artifact_dir=artifact_dir, review_id=REVIEW_ID,
            usage_attestation_path=attestation(tmp_path / "usage.json", remaining=70),
            run_command=forbidden,
        )
