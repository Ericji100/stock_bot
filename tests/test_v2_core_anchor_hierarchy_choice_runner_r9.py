"""R9B hierarchy decision is evidence-bound and cannot grant a trade."""

from __future__ import annotations

from pathlib import Path
import shutil

import pytest

from scripts.v2_core_anchor_hierarchy_choice_r9 import ATOMS, VERSION, validate_and_choose
from scripts.v2_core_anchor_hierarchy_choice_runner_r9 import RunnerError, prepare, run_choice, validate_existing
from scripts.v2_core_episode_defense_shortlist_r8 import INPUT_DIRECTORY, OUTPUT_DIRECTORY as DEFENSE_SHORTLIST_DIRECTORY
from scripts.v2_core_legacy_role_shortlist_r7 import ARTIFACT_DIR, load_json
from tests.test_v2_core_anchor_family_retrieval_runner_r9 import REVIEW_ID, workspace as retrieval_workspace
from tests.test_v2_core_legacy_role_qualification_runner_r7 import attestation, fake_command_for


def workspace(tmp_path: Path) -> Path:
    artifact_dir = retrieval_workspace(tmp_path)
    files = [
        "v2_core_anchor_hierarchy_choice.prompt.candidate_r9.md",
        f"anchor_family_retrieval_manifests_candidate_r9/{REVIEW_ID}.json",
    ]
    for suffix in (".json", ".raw.json", ".receipt.json"):
        files.append(f"anchor_family_retrieval_runs_candidate_r9/{REVIEW_ID}{suffix}")
    for name in files:
        destination = artifact_dir / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ARTIFACT_DIR / name, destination)
    return artifact_dir


def answer(packet: dict, retrieval: dict, defense_id: str) -> dict:
    pool = {row["candidate_id"]: row for row in packet["candidate_pool"]}
    reps = list(retrieval["group_representatives"].values())
    selected = next(row for row in reps if row["direction"] == "UP" and row["status"] == "FORMING" and row["relevance_judgement"] != "FAIL")
    context = next(row for row in reps if row["candidate_id"] != selected["candidate_id"] and row["direction"] == "UP")
    return {
        "schema_version": VERSION,
        "review_id": packet["review_id"],
        "as_of": packet["as_of"],
        "bound_defense_candidate_id": defense_id,
        "representative_assessments": {
            row["candidate_id"]: {
                "atoms": {atom: "PASS" if row["candidate_id"] in (selected["candidate_id"], context["candidate_id"]) else "FAIL" for atom in ATOMS},
                "supporting_evidence_refs": [pool[row["candidate_id"]]["source_evidence_refs"][0]],
                "explanation": "The frozen representative was examined for execution role, context role, defense control and price evidence.",
            }
            for row in reps
        },
        "selected_working_anchor_id": selected["candidate_id"],
        "selected_campaign_context_id": context["candidate_id"],
        "relationship_class": "FRESH_FORMING_UP_ANCHOR",
        "role_comparison_reason": "The selected forming UP representative directly controls this trade episode while the separate context representative supplies a broader direction; the other families remain alternative evidence rather than final anchors.",
        "causal_attestation": {
            "as_of_only": True, "no_identity": True, "no_teacher_answer": True,
            "no_future_performance": True, "defense_not_changed": True,
            "context_and_working_anchor_separate": True, "no_trade_permission": True,
        },
    }


def test_fake_hierarchy_choice_and_receipt(tmp_path: Path) -> None:
    artifact_dir = workspace(tmp_path)
    manifest = prepare(artifact_dir, REVIEW_ID)
    packet = load_json(artifact_dir / INPUT_DIRECTORY / f"{REVIEW_ID}.json")
    defense = load_json(artifact_dir / DEFENSE_SHORTLIST_DIRECTORY / f"{REVIEW_ID}.json")
    from scripts.v2_core_anchor_family_retrieval_runner_r9 import validate_existing as validate_retrieval
    retrieval = validate_retrieval(artifact_dir=artifact_dir, review_id=REVIEW_ID)
    response = answer(packet, retrieval, manifest["bound_defense_candidate_id"])
    assert validate_and_choose(response, packet=packet, defense=defense, defense_id=manifest["bound_defense_candidate_id"], retrieval=retrieval)["status"] == "VALID"
    result = run_choice(
        artifact_dir=artifact_dir, review_id=REVIEW_ID,
        usage_attestation_path=attestation(tmp_path / "usage.json"),
        run_command=fake_command_for(response),
    )
    assert result["resolution_status"] == "SELECTED"
    assert result["trade_permission_granted"] is False
    assert result["selected_campaign_context_id"] != result["selected_working_anchor_id"]
    assert validate_existing(artifact_dir=artifact_dir, review_id=REVIEW_ID) == result


def test_context_working_collapse_rejected(tmp_path: Path) -> None:
    artifact_dir = workspace(tmp_path)
    manifest = prepare(artifact_dir, REVIEW_ID)
    packet = load_json(artifact_dir / INPUT_DIRECTORY / f"{REVIEW_ID}.json")
    defense = load_json(artifact_dir / DEFENSE_SHORTLIST_DIRECTORY / f"{REVIEW_ID}.json")
    from scripts.v2_core_anchor_family_retrieval_runner_r9 import validate_existing as validate_retrieval
    retrieval = validate_retrieval(artifact_dir=artifact_dir, review_id=REVIEW_ID)
    response = answer(packet, retrieval, manifest["bound_defense_candidate_id"])
    response["selected_campaign_context_id"] = response["selected_working_anchor_id"]
    result = validate_and_choose(response, packet=packet, defense=defense, defense_id=manifest["bound_defense_candidate_id"], retrieval=retrieval)
    assert result["status"] == "INVALID"
    assert "context and working anchor collapsed" in result["errors"][0]


def test_budget_stop_before_choice_transport(tmp_path: Path) -> None:
    artifact_dir = workspace(tmp_path)
    prepare(artifact_dir, REVIEW_ID)

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("transport must not run")

    with pytest.raises(RunnerError, match="BUDGET_STOP"):
        run_choice(
            artifact_dir=artifact_dir, review_id=REVIEW_ID,
            usage_attestation_path=attestation(tmp_path / "usage.json", remaining=70),
            run_command=forbidden,
        )
