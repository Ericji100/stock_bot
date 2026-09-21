"""R10B binds tactical and broad anchors with true episode-stop risk."""

from __future__ import annotations

from pathlib import Path
import shutil

import pytest

from scripts.v2_core_legacy_role_shortlist_r7 import ARTIFACT_DIR
from scripts.v2_core_trigger_judgement_r10b import VERSION, obstacle_high_options, validate_and_gate
from scripts.v2_core_trigger_runner_r10b import RunnerError, _inputs, prepare, run_trigger, validate_existing
from tests.test_v2_core_legacy_role_qualification_runner_r7 import attestation, fake_command_for
from tests.test_v2_core_tactical_cycle_anchor_runner_r10 import REVIEW_ID, workspace as tactical_workspace
from tests.test_v2_core_trigger_runner_r9 import answer as old_answer


def workspace(tmp_path: Path) -> Path:
    artifact_dir = tactical_workspace(tmp_path)
    for name in (
        "v2_core_trigger.prompt.candidate_r10b_fresh.md",
        "v2_core_trigger.prompt.candidate_r10b_macro.md",
        f"tactical_cycle_anchor_manifests_candidate_r10/{REVIEW_ID}.json",
        f"tactical_cycle_anchor_runs_candidate_r10/{REVIEW_ID}.json",
        f"tactical_cycle_anchor_runs_candidate_r10/{REVIEW_ID}.raw.json",
        f"tactical_cycle_anchor_runs_candidate_r10/{REVIEW_ID}.receipt.json",
    ):
        destination = artifact_dir / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ARTIFACT_DIR / name, destination)
    return artifact_dir


def answer(packet: dict, defense_id: str, broad_id: str, tactical_id: str, scenario: str) -> dict:
    response = old_answer(packet, defense_id, broad_id, scenario)
    response["schema_version"] = VERSION
    response.update({
        "bound_tactical_cycle_anchor_id": tactical_id,
        "taiji_generation": "ANCHOR_LEG_1",
        "large_quadrant": "Q1",
        "small_quadrant": "Q1",
        "correction_pressure_pivot_id": "NONE",
        "nearest_meaningful_obstacle_id": next(iter(obstacle_high_options(packet))),
        "space_and_generation_reason": "The AS-OF close is assessed against the previously confirmed episode low and the nearest same-level obstacle, while the tactical cycle starts after the broad campaign and provides an independent generation clock; no fixed reward-to-risk threshold is imposed.",
    })
    return response


def test_fake_trigger_and_receipt(tmp_path: Path) -> None:
    artifact_dir = workspace(tmp_path)
    manifest = prepare(artifact_dir, REVIEW_ID)
    packet, _hierarchy, defense_row, _role, defense_id, broad_id, tactical_id = _inputs(artifact_dir, REVIEW_ID)
    response = answer(packet, defense_id, broad_id, tactical_id, manifest["program_scenario"])
    result = run_trigger(
        artifact_dir=artifact_dir, review_id=REVIEW_ID,
        usage_attestation_path=attestation(tmp_path / "usage.json"),
        run_command=fake_command_for(response),
    )
    assert result["signal_disposition"] == "TRIGGERED_SIGNAL_CANDIDATE"
    assert result["objective_risk_context"]["frozen_episode_stop"] == defense_row["price"]
    assert result["fixed_reward_to_risk_threshold_applied"] is False
    assert result["actual_entry_or_fill_computed"] is False
    assert validate_existing(artifact_dir=artifact_dir, review_id=REVIEW_ID) == result


def test_q3_cannot_trigger(tmp_path: Path) -> None:
    artifact_dir = workspace(tmp_path)
    manifest = prepare(artifact_dir, REVIEW_ID)
    packet, _hierarchy, defense_row, _role, defense_id, broad_id, tactical_id = _inputs(artifact_dir, REVIEW_ID)
    response = answer(packet, defense_id, broad_id, tactical_id, manifest["program_scenario"])
    response["large_quadrant"] = "Q3"
    result = validate_and_gate(
        response, packet=packet, defense_id=defense_id, defense_row=defense_row,
        broad_id=broad_id, tactical_id=tactical_id, scenario=manifest["program_scenario"],
    )
    assert result["status"] == "INVALID"
    assert "Q3 cannot trigger" in result["errors"][0]


def test_budget_stop_before_transport(tmp_path: Path) -> None:
    artifact_dir = workspace(tmp_path)
    prepare(artifact_dir, REVIEW_ID)

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("transport must not run")

    with pytest.raises(RunnerError, match="BUDGET_STOP"):
        run_trigger(
            artifact_dir=artifact_dir, review_id=REVIEW_ID,
            usage_attestation_path=attestation(tmp_path / "usage.json", remaining=70),
            run_command=forbidden,
        )
