"""R10 separates a tactical generation clock from the frozen broad campaign."""

from __future__ import annotations

from pathlib import Path
import shutil

import pytest

from scripts.v2_core_legacy_role_shortlist_r7 import ARTIFACT_DIR
from scripts.v2_core_tactical_cycle_anchor_r10 import ATOMS, VERSION, candidate_options, validate_and_select
from scripts.v2_core_tactical_cycle_anchor_runner_r10 import RunnerError, _inputs, prepare, run_selection, validate_existing
from tests.test_v2_core_legacy_role_qualification_runner_r7 import attestation, fake_command_for
from tests.test_v2_core_trigger_runner_r9 import REVIEW_ID, workspace as trigger_workspace


def workspace(tmp_path: Path) -> Path:
    artifact_dir = trigger_workspace(tmp_path)
    for name in (
        "v2_core_tactical_cycle_anchor.prompt.candidate_r10.md",
        "R10_SOURCE_BOUND_REPAIR_PLAN_CANDIDATE.md",
    ):
        shutil.copyfile(ARTIFACT_DIR / name, artifact_dir / name)
    return artifact_dir


def answer(packet: dict, retrieval: dict, hierarchy: dict, defense_id: str) -> dict:
    options = candidate_options(packet, hierarchy, retrieval)
    broad = hierarchy["selected_working_anchor_id"]
    chosen = next(candidate_id for candidate_id in options if candidate_id != broad)
    assessments = {}
    for candidate_id, item in options.items():
        refs = list(item["candidate"]["source_evidence_refs"])
        if candidate_id == chosen and not any(ref.startswith(("PRICE:", "PIVOT:")) for ref in refs):
            refs.append(next(ref for ref in packet["candidate_pool"][0]["source_evidence_refs"] if ref.startswith(("PRICE:", "PIVOT:"))))
        assessments[candidate_id] = {
            "atoms": {atom: "PASS" if candidate_id == chosen else "UNKNOWN" for atom in ATOMS},
            "supporting_evidence_refs": refs,
            "explanation": "This AS-OF candidate was compared with the frozen broad campaign for cycle fit, nesting, independent price support and Taiji generation usefulness.",
        }
    return {
        "schema_version": VERSION,
        "review_id": packet["review_id"],
        "as_of": packet["as_of"],
        "bound_defense_candidate_id": defense_id,
        "bound_broad_anchor_id": broad,
        "program_scenario": "FRESH_Q1_EXPANSION",
        "option_assessments": assessments,
        "selected_tactical_cycle_anchor_id": chosen,
        "broad_vs_tactical_reason": "The fixed broad UP price campaign supplies direction and space, while the selected nested current-cycle candidate supplies a separate generation clock. No selected evidence changes the broad anchor, defense or scenario, and all candidates were compared on AS-OF facts.",
        "causal_attestation": {
            "as_of_only": True, "no_identity": True, "no_teacher_answer": True,
            "no_future_performance": True, "broad_anchor_unchanged": True,
            "episode_defense_unchanged": True, "no_trade_permission": True,
        },
    }


def test_tactical_cycle_fake_transport_and_receipt(tmp_path: Path) -> None:
    artifact_dir = workspace(tmp_path)
    manifest = prepare(artifact_dir, REVIEW_ID)
    packet, retrieval, hierarchy, defense_id, _defense_row = _inputs(artifact_dir, REVIEW_ID)
    response = answer(packet, retrieval, hierarchy, defense_id)
    assert validate_and_select(response, packet=packet, hierarchy=hierarchy, retrieval=retrieval, defense_id=defense_id)["status"] == "VALID"
    result = run_selection(
        artifact_dir=artifact_dir, review_id=REVIEW_ID,
        usage_attestation_path=attestation(tmp_path / "usage.json"),
        run_command=fake_command_for(response),
    )
    assert result["resolution_status"] == "SELECTED"
    assert result["selected_tactical_cycle_anchor_id"] != manifest["bound_broad_anchor_id"]
    assert result["trade_permission_granted"] is False
    assert validate_existing(artifact_dir=artifact_dir, review_id=REVIEW_ID) == result


def test_selected_candidate_must_have_price_reference(tmp_path: Path) -> None:
    artifact_dir = workspace(tmp_path)
    packet, retrieval, hierarchy, defense_id, _defense_row = _inputs(artifact_dir, REVIEW_ID)
    response = answer(packet, retrieval, hierarchy, defense_id)
    selected = response["selected_tactical_cycle_anchor_id"]
    response["option_assessments"][selected]["supporting_evidence_refs"] = [
        ref for ref in response["option_assessments"][selected]["supporting_evidence_refs"] if not ref.startswith(("PRICE:", "PIVOT:"))
    ]
    result = validate_and_select(response, packet=packet, hierarchy=hierarchy, retrieval=retrieval, defense_id=defense_id)
    assert result["status"] == "INVALID"


def test_budget_stop_before_transport(tmp_path: Path) -> None:
    artifact_dir = workspace(tmp_path)
    prepare(artifact_dir, REVIEW_ID)

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("transport must not run")

    with pytest.raises(RunnerError, match="BUDGET_STOP"):
        run_selection(
            artifact_dir=artifact_dir, review_id=REVIEW_ID,
            usage_attestation_path=attestation(tmp_path / "usage.json", remaining=70),
            run_command=forbidden,
        )
