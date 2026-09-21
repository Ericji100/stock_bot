"""R8 working-anchor contract binds defense and checks cross-basis evidence."""

from __future__ import annotations

from pathlib import Path
import shutil

import pytest

from scripts.v2_core_episode_defense_shortlist_r8 import INPUT_DIRECTORY, OUTPUT_DIRECTORY as DEFENSE_SHORTLIST_DIRECTORY
from scripts.v2_core_legacy_role_shortlist_r7 import ARTIFACT_DIR, load_json
from scripts.v2_core_working_anchor_judgement_r8 import ATOMS, VERSION, validate_and_resolve
from scripts.v2_core_working_anchor_runner_r8 import RunnerError, prepare, run_anchor, validate_existing
from tests.test_v2_core_episode_defense_runner_r8 import REVIEW_ID, workspace as defense_workspace
from tests.test_v2_core_legacy_role_qualification_runner_r7 import attestation, fake_command_for


def workspace(tmp_path: Path) -> Path:
    artifact_dir = defense_workspace(tmp_path)
    for name in (
        "v2_core_working_anchor.prompt.candidate_r8.md",
        f"episode_defense_manifests_candidate_r8/{REVIEW_ID}.json",
        f"episode_defense_runs_candidate_r8/{REVIEW_ID}.json",
        f"episode_defense_runs_candidate_r8/{REVIEW_ID}.raw.json",
        f"episode_defense_runs_candidate_r8/{REVIEW_ID}.receipt.json",
    ):
        destination = artifact_dir / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ARTIFACT_DIR / name, destination)
    return artifact_dir


def answer(packet: dict, defense_id: str) -> dict:
    candidates = packet["candidate_pool"]
    selected = next(row for row in candidates if row["direction"] == "UP" and row["status"] == "FORMING")
    other_basis = next(row for row in candidates if row["candidate_id"] != selected["candidate_id"] and row["basis"] != selected["basis"])
    third = next(row for row in candidates if row["candidate_id"] not in {selected["candidate_id"], other_basis["candidate_id"]})
    compared = (selected, other_basis, third)
    return {
        "schema_version": VERSION,
        "review_id": packet["review_id"],
        "as_of": packet["as_of"],
        "bound_defense_candidate_id": defense_id,
        "selected_anchor_candidate_id": selected["candidate_id"],
        "relationship_class": "FRESH_FORMING_UP_ANCHOR",
        "selected_anchor_atoms": {atom: "PASS" for atom in ATOMS},
        "selected_anchor_evidence_refs": [selected["source_evidence_refs"][0]],
        "comparison_rows": [{
            "candidate_id": row["candidate_id"],
            "control_judgement": "PASS" if row is selected else "FAIL",
            "supporting_evidence_refs": [row["source_evidence_refs"][0]],
            "explanation": "This candidate was compared using its own visible price and structural evidence at AS-OF.",
        } for row in compared],
        "selection_reason": "The selected forming upward anchor most directly controls this defense episode; competing basis and scale candidates were considered but lack direct control.",
        "causal_attestation": {
            "as_of_only": True, "no_identity": True, "no_teacher_answer": True,
            "no_future_performance": True, "defense_not_changed": True,
            "no_scenario_or_trade_permission": True,
        },
    }


def test_fake_anchor_transport_binds_defense(tmp_path: Path) -> None:
    artifact_dir = workspace(tmp_path)
    manifest = prepare(artifact_dir, REVIEW_ID)
    packet = load_json(artifact_dir / INPUT_DIRECTORY / f"{REVIEW_ID}.json")
    defense = load_json(artifact_dir / DEFENSE_SHORTLIST_DIRECTORY / f"{REVIEW_ID}.json")
    response = answer(packet, manifest["bound_defense_candidate_id"])
    assert validate_and_resolve(response, packet=packet, defense=defense, defense_id=manifest["bound_defense_candidate_id"])["status"] == "VALID"
    result = run_anchor(
        artifact_dir=artifact_dir, review_id=REVIEW_ID,
        usage_attestation_path=attestation(tmp_path / "usage.json"),
        run_command=fake_command_for(response),
    )
    assert result["resolution_status"] == "SELECTED"
    assert result["trade_permission_granted"] is False
    assert validate_existing(artifact_dir=artifact_dir, review_id=REVIEW_ID) == result


def test_wrong_defense_or_no_cross_basis_is_invalid() -> None:
    packet = load_json(ARTIFACT_DIR / INPUT_DIRECTORY / f"{REVIEW_ID}.json")
    defense = load_json(ARTIFACT_DIR / DEFENSE_SHORTLIST_DIRECTORY / f"{REVIEW_ID}.json")
    manifest = load_json(ARTIFACT_DIR / "episode_defense_manifests_candidate_r8" / f"{REVIEW_ID}.json")
    defense_id = manifest["review_id"]  # deliberately not a defense ID
    with pytest.raises(ValueError, match="selected defense not in shortlist"):
        validate_and_resolve(answer(packet, defense_id), packet=packet, defense=defense, defense_id=defense_id)


def test_budget_stop_before_anchor_transport(tmp_path: Path) -> None:
    artifact_dir = workspace(tmp_path)
    prepare(artifact_dir, REVIEW_ID)

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("transport must not run")

    with pytest.raises(RunnerError, match="BUDGET_STOP"):
        run_anchor(
            artifact_dir=artifact_dir, review_id=REVIEW_ID,
            usage_attestation_path=attestation(tmp_path / "usage.json", remaining=70),
            run_command=forbidden,
        )
