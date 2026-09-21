"""R8 fixed-key defense contract, fake transport and budget guard."""

from __future__ import annotations

from pathlib import Path
import shutil

import pytest

from scripts.v2_core_episode_defense_judgement_r8 import ATOMS, VERSION, transport_for, validate_and_resolve
from scripts.v2_core_episode_defense_runner_r8 import RunnerError, prepare, run_defense, validate_existing
from scripts.v2_core_episode_defense_shortlist_r8 import INPUT_DIRECTORY, OUTPUT_DIRECTORY as SHORTLIST_DIRECTORY
from scripts.v2_core_legacy_role_qualification_manifest_r7 import BUDGET_FILE
from scripts.v2_core_legacy_role_shortlist_r7 import ARTIFACT_DIR, load_json
from tests.test_v2_core_legacy_role_qualification_runner_r7 import attestation, fake_command_for


REVIEW_ID = "FP-18b86f08f05563ff5886097b"
PROMPT_FILE = "v2_core_episode_defense.prompt.candidate_r8.md"


def workspace(tmp_path: Path) -> Path:
    for name in (BUDGET_FILE, PROMPT_FILE):
        shutil.copyfile(ARTIFACT_DIR / name, tmp_path / name)
    for directory in (INPUT_DIRECTORY, SHORTLIST_DIRECTORY):
        destination = tmp_path / directory
        destination.mkdir()
        shutil.copyfile(ARTIFACT_DIR / directory / f"{REVIEW_ID}.json", destination / f"{REVIEW_ID}.json")
    return tmp_path


def answer(packet: dict, shortlist: dict) -> dict:
    selected = shortlist["candidate_rows"][0]["candidate_id"]
    return {
        "schema_version": VERSION,
        "review_id": packet["review_id"],
        "as_of": packet["as_of"],
        "candidate_assessments": {
            row["candidate_id"]: {
                "atoms": {atom: "PASS" if row["candidate_id"] == selected or atom == "CONFIRMED_AS_OF_SIGNAL_CLOSE" else "FAIL" for atom in ATOMS},
                "supporting_evidence_refs": [row["evidence_ref"]],
                "explanation": "The confirmed small-scale low has been assessed against the current price break and invalidation.",
            }
            for row in shortlist["candidate_rows"]
        },
        "selected_candidate_id": selected,
        "compared_candidate_ids": [row["candidate_id"] for row in shortlist["candidate_rows"][:2]],
        "selection_reason": "The selected low best represents the causal episode defense; the compared alternative is internal structure.",
        "causal_attestation": {
            "as_of_only": True,
            "no_identity": True,
            "no_teacher_answer": True,
            "no_future_performance": True,
            "no_scenario_or_trade_permission": True,
        },
    }


def test_fixed_key_contract_and_fake_runner(tmp_path: Path) -> None:
    artifact_dir = workspace(tmp_path)
    packet = load_json(artifact_dir / INPUT_DIRECTORY / f"{REVIEW_ID}.json")
    shortlist = load_json(artifact_dir / SHORTLIST_DIRECTORY / f"{REVIEW_ID}.json")
    schema = transport_for(packet, shortlist)
    assert "$ref" not in str(schema)
    assert set(schema["properties"]["candidate_assessments"]["required"]) == {row["candidate_id"] for row in shortlist["candidate_rows"]}
    response = answer(packet, shortlist)
    assert validate_and_resolve(response, packet=packet, shortlist=shortlist)["status"] == "VALID"
    prepare(artifact_dir, REVIEW_ID)
    result = run_defense(
        artifact_dir=artifact_dir, review_id=REVIEW_ID,
        usage_attestation_path=attestation(tmp_path / "usage.json"),
        run_command=fake_command_for(response),
    )
    assert result["resolution_status"] == "SELECTED"
    assert result["trade_permission_granted"] is False
    assert validate_existing(artifact_dir=artifact_dir, review_id=REVIEW_ID) == result


def test_invalid_selected_or_missing_key_is_rejected() -> None:
    packet = load_json(ARTIFACT_DIR / INPUT_DIRECTORY / f"{REVIEW_ID}.json")
    shortlist = load_json(ARTIFACT_DIR / SHORTLIST_DIRECTORY / f"{REVIEW_ID}.json")
    response = answer(packet, shortlist)
    selected = response["selected_candidate_id"]
    response["candidate_assessments"][selected]["atoms"]["CAUSES_CURRENT_ENTRY_INVALIDATION"] = "FAIL"
    assert validate_and_resolve(response, packet=packet, shortlist=shortlist)["status"] == "INVALID"
    response = answer(packet, shortlist)
    response["candidate_assessments"].pop(selected)
    assert validate_and_resolve(response, packet=packet, shortlist=shortlist)["status"] == "INVALID"


def test_budget_stop_before_transport(tmp_path: Path) -> None:
    artifact_dir = workspace(tmp_path)
    prepare(artifact_dir, REVIEW_ID)

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("transport must not run")

    with pytest.raises(RunnerError, match="BUDGET_STOP"):
        run_defense(
            artifact_dir=artifact_dir, review_id=REVIEW_ID,
            usage_attestation_path=attestation(tmp_path / "usage.json", remaining=70),
            run_command=forbidden,
        )
