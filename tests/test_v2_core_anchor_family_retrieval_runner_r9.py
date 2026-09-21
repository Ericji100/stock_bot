"""R9A group-level retrieval returns one auditable representative per family."""

from __future__ import annotations

from pathlib import Path
import shutil

import pytest

from scripts.v2_core_anchor_basis_groups_r9 import OUTPUT_DIRECTORY as GROUP_DIRECTORY, build_for_file
from scripts.v2_core_anchor_family_retrieval_r9 import VERSION, validate_and_retrieve
from scripts.v2_core_anchor_family_retrieval_runner_r9 import RunnerError, prepare, run_retrieval, validate_existing
from scripts.v2_core_episode_defense_shortlist_r8 import INPUT_DIRECTORY
from scripts.v2_core_legacy_role_shortlist_r7 import ARTIFACT_DIR, load_json
from tests.test_v2_core_episode_defense_runner_r8 import REVIEW_ID
from tests.test_v2_core_legacy_role_qualification_runner_r7 import attestation, fake_command_for
from tests.test_v2_core_working_anchor_runner_r8 import workspace as anchor_workspace


def workspace(tmp_path: Path) -> Path:
    artifact_dir = anchor_workspace(tmp_path)
    shutil.copyfile(
        ARTIFACT_DIR / "v2_core_anchor_family_retrieval.prompt.candidate_r9.md",
        artifact_dir / "v2_core_anchor_family_retrieval.prompt.candidate_r9.md",
    )
    directory = artifact_dir / GROUP_DIRECTORY
    directory.mkdir()
    shutil.copyfile(ARTIFACT_DIR / GROUP_DIRECTORY / f"{REVIEW_ID}.json", directory / f"{REVIEW_ID}.json")
    return artifact_dir


def answer(packet: dict, groups: dict, defense_id: str) -> dict:
    candidates = {row["candidate_id"]: row for row in packet["candidate_pool"]}
    return {
        "schema_version": VERSION,
        "review_id": packet["review_id"],
        "as_of": packet["as_of"],
        "bound_defense_candidate_id": defense_id,
        "group_representatives": {
            group["group_id"]: {
                "representative_candidate_id": group["candidate_ids"][0],
                "compared_candidate_ids": group["candidate_ids"][:2],
                "relevance_judgement": "UNKNOWN",
                "supporting_evidence_refs": [candidates[group["candidate_ids"][0]]["source_evidence_refs"][0]],
                "selection_reason": "This source-family representative has the strongest visible connection to the fixed episode defense among this group's candidates.",
            }
            for group in groups["groups"]
        },
        "retrieval_attestation": {
            "as_of_only": True, "no_identity": True, "no_teacher_answer": True,
            "no_future_performance": True, "defense_not_changed": True,
            "no_final_anchor_or_trade_permission": True,
        },
    }


def test_group_generator_and_fake_retrieval(tmp_path: Path) -> None:
    artifact_dir = workspace(tmp_path)
    packet_path = artifact_dir / INPUT_DIRECTORY / f"{REVIEW_ID}.json"
    packet = load_json(packet_path)
    groups = load_json(artifact_dir / GROUP_DIRECTORY / f"{REVIEW_ID}.json")
    assert groups == build_for_file(packet_path)
    assert groups["candidate_count"] == len(packet["candidate_pool"])
    manifest = prepare(artifact_dir, REVIEW_ID)
    response = answer(packet, groups, manifest["bound_defense_candidate_id"])
    assert validate_and_retrieve(response, packet=packet, groups=groups, defense_id=manifest["bound_defense_candidate_id"])["status"] == "VALID"
    result = run_retrieval(
        artifact_dir=artifact_dir, review_id=REVIEW_ID,
        usage_attestation_path=attestation(tmp_path / "usage.json"),
        run_command=fake_command_for(response),
    )
    assert result["group_count"] == groups["group_count"]
    assert result["final_anchor_selected"] is False
    assert result["trade_permission_granted"] is False
    assert validate_existing(artifact_dir=artifact_dir, review_id=REVIEW_ID) == result


def test_missing_group_or_outside_group_rejected() -> None:
    packet = load_json(ARTIFACT_DIR / INPUT_DIRECTORY / f"{REVIEW_ID}.json")
    groups = load_json(ARTIFACT_DIR / GROUP_DIRECTORY / f"{REVIEW_ID}.json")
    defense_id = "DEF-b0d98e503c98104e2b3d"
    response = answer(packet, groups, defense_id)
    response["group_representatives"].pop(groups["groups"][0]["group_id"])
    assert validate_and_retrieve(response, packet=packet, groups=groups, defense_id=defense_id)["status"] == "INVALID"


def test_budget_stop_before_retrieval_transport(tmp_path: Path) -> None:
    artifact_dir = workspace(tmp_path)
    prepare(artifact_dir, REVIEW_ID)

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("transport must not run")

    with pytest.raises(RunnerError, match="BUDGET_STOP"):
        run_retrieval(
            artifact_dir=artifact_dir, review_id=REVIEW_ID,
            usage_attestation_path=attestation(tmp_path / "usage.json", remaining=70),
            run_command=forbidden,
        )
