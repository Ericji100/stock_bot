"""R9C trigger gate binds AI evidence to a deterministic signal candidate."""

from __future__ import annotations

from pathlib import Path
import shutil

import pytest

from scripts.v2_core_anchor_hierarchy_choice_runner_r9 import validate_existing as validate_hierarchy
from scripts.v2_core_episode_defense_shortlist_r8 import INPUT_DIRECTORY
from scripts.v2_core_legacy_role_shortlist_r7 import ARTIFACT_DIR, load_json
from scripts.v2_core_trigger_judgement_r9 import GATES, VERSION, confirmed_high_options, validate_and_gate
from scripts.v2_core_trigger_runner_r9 import RunnerError, prepare, run_trigger, validate_existing
from tests.test_v2_core_anchor_hierarchy_choice_runner_r9 import REVIEW_ID, workspace as hierarchy_workspace
from tests.test_v2_core_legacy_role_qualification_runner_r7 import attestation, fake_command_for


def workspace(tmp_path: Path) -> Path:
    artifact_dir = hierarchy_workspace(tmp_path)
    for name in (
        "v2_core_trigger.prompt.candidate_r9_fresh.md",
        "v2_core_trigger.prompt.candidate_r9_macro.md",
        f"anchor_hierarchy_choice_manifests_candidate_r9/{REVIEW_ID}.json",
        f"anchor_hierarchy_choice_runs_candidate_r9/{REVIEW_ID}.json",
        f"anchor_hierarchy_choice_runs_candidate_r9/{REVIEW_ID}.raw.json",
        f"anchor_hierarchy_choice_runs_candidate_r9/{REVIEW_ID}.receipt.json",
    ):
        destination = artifact_dir / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ARTIFACT_DIR / name, destination)
    return artifact_dir


def answer(packet: dict, defense_id: str, working_id: str, scenario: str) -> dict:
    options = confirmed_high_options(packet)
    latest = max(options, key=lambda item: options[item]["source_date"])
    ref = packet["candidate_pool"][0]["source_evidence_refs"][0]
    return {
        "schema_version": VERSION,
        "review_id": packet["review_id"],
        "as_of": packet["as_of"],
        "bound_defense_candidate_id": defense_id,
        "bound_working_anchor_id": working_id,
        "program_scenario": scenario,
        "gate_assessments": {
            gate: {
                "judgement": "PASS",
                "supporting_evidence_refs": [ref],
                "explanation": "AS-OF evidence was examined for the frozen V2 scenario and its causal structural gate.",
            }
            for gate in GATES[scenario]
        },
        "proposed_trigger_path": "INITIAL_DESTRUCTIVE_EXPANSION",
        "break_pivot_id": latest,
        "trigger_evidence_refs": [options[latest]["evidence_ref"]],
        "trigger_reason": "The AS-OF close breaks the most recent confirmed small-scale high after the frozen defense, with the required structural gates passed.",
        "causal_attestation": {
            "as_of_only": True, "no_identity": True, "no_teacher_answer": True,
            "no_future_performance": True, "defense_and_anchor_not_changed": True,
            "no_actual_fill_or_portfolio_claim": True,
        },
    }


def test_fake_trigger_and_receipt(tmp_path: Path) -> None:
    artifact_dir = workspace(tmp_path)
    manifest = prepare(artifact_dir, REVIEW_ID)
    packet = load_json(artifact_dir / INPUT_DIRECTORY / f"{REVIEW_ID}.json")
    hierarchy = validate_hierarchy(artifact_dir=artifact_dir, review_id=REVIEW_ID)
    assert manifest["bound_working_anchor_id"] == hierarchy["selected_working_anchor_id"]
    response = answer(packet, manifest["bound_defense_candidate_id"], manifest["bound_working_anchor_id"], manifest["program_scenario"])
    result = run_trigger(
        artifact_dir=artifact_dir, review_id=REVIEW_ID,
        usage_attestation_path=attestation(tmp_path / "usage.json"),
        run_command=fake_command_for(response),
    )
    assert result["signal_disposition"] == "TRIGGERED_SIGNAL_CANDIDATE"
    assert result["actual_entry_or_fill_computed"] is False
    assert validate_existing(artifact_dir=artifact_dir, review_id=REVIEW_ID) == result


def test_fail_gate_blocks_signal(tmp_path: Path) -> None:
    artifact_dir = workspace(tmp_path)
    manifest = prepare(artifact_dir, REVIEW_ID)
    packet = load_json(artifact_dir / INPUT_DIRECTORY / f"{REVIEW_ID}.json")
    response = answer(packet, manifest["bound_defense_candidate_id"], manifest["bound_working_anchor_id"], manifest["program_scenario"])
    response["gate_assessments"][GATES[manifest["program_scenario"]][0]]["judgement"] = "FAIL"
    assert validate_and_gate(
        response, packet=packet, defense_id=manifest["bound_defense_candidate_id"],
        working_id=manifest["bound_working_anchor_id"], scenario=manifest["program_scenario"],
    )["signal_disposition"] == "NO_TRADE_GATE_FAIL"


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
