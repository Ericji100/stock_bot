"""R9D accepts duplicate refs without changing the course decision."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.v2_core_episode_defense_shortlist_r8 import INPUT_DIRECTORY
from scripts.v2_core_legacy_role_shortlist_r7 import load_json
from scripts.v2_core_trigger_judgement_r9d import VERSION, validate_and_gate
from scripts.v2_core_trigger_runner_r9d import RunnerError, prepare, run_trigger, validate_existing
from tests.test_v2_core_legacy_role_qualification_runner_r7 import attestation, fake_command_for
from tests.test_v2_core_trigger_runner_r9 import REVIEW_ID, answer, workspace


def test_duplicate_refs_kept_raw_and_do_not_change_signal(tmp_path: Path) -> None:
    artifact_dir = workspace(tmp_path)
    manifest = prepare(artifact_dir, REVIEW_ID)
    packet = load_json(artifact_dir / INPUT_DIRECTORY / f"{REVIEW_ID}.json")
    response = answer(packet, manifest["bound_defense_candidate_id"], manifest["bound_working_anchor_id"], manifest["program_scenario"])
    response["schema_version"] = VERSION
    gate = next(iter(response["gate_assessments"].values()))
    gate["supporting_evidence_refs"].append(gate["supporting_evidence_refs"][0])
    baseline = validate_and_gate(
        response, packet=packet, defense_id=manifest["bound_defense_candidate_id"],
        working_id=manifest["bound_working_anchor_id"], scenario=manifest["program_scenario"],
    )
    assert baseline["status"] == "VALID"
    assert baseline["duplicate_evidence_reference_count"] == 1
    result = run_trigger(
        artifact_dir=artifact_dir, review_id=REVIEW_ID,
        usage_attestation_path=attestation(tmp_path / "usage.json"),
        run_command=fake_command_for(response),
    )
    assert result["signal_disposition"] == "TRIGGERED_SIGNAL_CANDIDATE"
    assert result["raw_response_unchanged"] is True
    assert validate_existing(artifact_dir=artifact_dir, review_id=REVIEW_ID) == result


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
