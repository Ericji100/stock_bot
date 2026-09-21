"""R7B relation fake transport binds prior role outputs and route receipt."""

from __future__ import annotations

from pathlib import Path
import shutil

import pytest

from scripts.v2_core_legacy_relation_runner_r7b import RunnerError, prepare, run_relation, validate_existing
from scripts.v2_core_legacy_role_shortlist_r7 import ARTIFACT_DIR
from tests.test_v2_core_legacy_relation_r7b import answer
from tests.test_v2_core_legacy_role_qualification_r7 import REVIEW_ID, data
from tests.test_v2_core_legacy_role_qualification_runner_r7 import attestation, fake_command_for
from tests.test_v2_core_legacy_role_qualification_runner_r7b import bound_workspace


def relation_workspace(tmp_path: Path) -> Path:
    artifact_dir = bound_workspace(tmp_path)
    files = [
        "v2_core_legacy_relation.prompt.candidate_r7b.md",
        f"legacy_role_qualification_manifests_candidate_r7b/{REVIEW_ID}.json",
        f"legacy_role_cross_object_preflight_candidate_r7b/{REVIEW_ID}.json",
    ]
    for role in ("ACTIVE_DOWN_CONTROLLER", "CONTROLLING_MATURE_CAMPAIGN", "IMMEDIATE_COMPLETED_UP_PARENT", "UP_CONTROL_CHALLENGER"):
        for suffix in (".json", ".raw.json", ".receipt.json"):
            files.append(f"legacy_role_qualification_runs_candidate_r7b/{role}/{REVIEW_ID}{suffix}")
    for name in files:
        destination = artifact_dir / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ARTIFACT_DIR / name, destination)
    return artifact_dir


def test_fake_relation_transport_routes_without_trade_permission(tmp_path: Path) -> None:
    artifact_dir = relation_workspace(tmp_path)
    prepare(artifact_dir, REVIEW_ID)
    packet, _shortlist = data()
    from scripts.v2_core_legacy_role_cross_object_preflight_r7b import build_report
    report = build_report(artifact_dir, REVIEW_ID)
    result = run_relation(
        artifact_dir=artifact_dir,
        review_id=REVIEW_ID,
        usage_attestation_path=attestation(tmp_path / "usage.json"),
        run_command=fake_command_for(answer(report, packet)),
    )
    assert result["program_derived_scenario"] == "FRESH_Q1_EXPANSION"
    assert result["trade_permission_granted"] is False
    assert validate_existing(artifact_dir=artifact_dir, review_id=REVIEW_ID) == result


def test_budget_stop_prevents_relation_call(tmp_path: Path) -> None:
    artifact_dir = relation_workspace(tmp_path)
    prepare(artifact_dir, REVIEW_ID)

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("formal transport must not run")

    with pytest.raises(RunnerError, match="BUDGET_STOP"):
        run_relation(
            artifact_dir=artifact_dir,
            review_id=REVIEW_ID,
            usage_attestation_path=attestation(tmp_path / "usage.json", remaining=70),
            run_command=forbidden,
        )
