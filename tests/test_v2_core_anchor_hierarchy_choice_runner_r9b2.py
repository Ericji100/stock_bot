"""R9B2 is a new transport contract; original R9B invalid raw stays invalid."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from scripts import v2_core_anchor_hierarchy_choice_r9 as r9b
from scripts import v2_core_anchor_hierarchy_choice_runner_r9 as r9b_runner
from scripts.v2_core_anchor_hierarchy_choice_r9b2 import VERSION, build_schema, validate_and_choose
from scripts.v2_core_anchor_hierarchy_choice_runner_r9b2 import (
    RunnerError, prepare, run_choice, validate_existing,
)
from scripts.v2_core_episode_defense_shortlist_r8 import INPUT_DIRECTORY
from scripts.v2_core_legacy_role_shortlist_r7 import ARTIFACT_DIR, load_json, sha256_path
from tests.test_v2_core_anchor_hierarchy_choice_runner_r9 import REVIEW_ID, answer, workspace
from tests.test_v2_core_legacy_role_qualification_runner_r7 import attestation, fake_command_for


def test_duplicate_ref_kept_in_raw_but_only_one_evidence_weight(tmp_path: Path) -> None:
    artifact_dir = workspace(tmp_path)
    manifest = prepare(artifact_dir, REVIEW_ID)
    packet, defense, defense_id, _defense_row, retrieval = r9b_runner._inputs(artifact_dir, REVIEW_ID)
    response = answer(packet, retrieval, defense_id)
    target = next(iter(response["representative_assessments"].values()))
    target["supporting_evidence_refs"].append(target["supporting_evidence_refs"][0])
    assert r9b.validate_and_choose(
        response, packet=packet, defense=defense, defense_id=defense_id, retrieval=retrieval,
    )["status"] == "INVALID"
    response["schema_version"] = VERSION
    assert "uniqueItems" not in json.dumps(build_schema(packet, defense, defense_id, retrieval))
    expected = validate_and_choose(
        response, packet=packet, defense=defense, defense_id=defense_id, retrieval=retrieval,
    )
    assert expected["status"] == "VALID"
    assert expected["duplicate_evidence_reference_count"] == 1
    assert expected["repeated_refs_count_as_distinct_evidence"] is False
    assert manifest["transport_only_change_from_r9b"] == "ALLOW_REPEATED_EVIDENCE_REFS_WITH_ONE_EVIDENCE_WEIGHT"
    result = run_choice(
        artifact_dir=artifact_dir, review_id=REVIEW_ID,
        usage_attestation_path=attestation(tmp_path / "usage.json"),
        run_command=fake_command_for(response),
    )
    assert result == expected
    assert validate_existing(artifact_dir=artifact_dir, review_id=REVIEW_ID) == result
    output = artifact_dir / "anchor_hierarchy_choice_runs_candidate_r9b2" / f"{REVIEW_ID}.raw.json"
    assert json.loads(output.read_text(encoding="utf-8")) == response


def test_original_invalid_raw_is_not_rewritten_or_reclassified() -> None:
    review_id = "FP-a46bf89ae6077ab9e326306c"
    packet, defense, defense_id, _defense_row, retrieval = r9b_runner._inputs(ARTIFACT_DIR, review_id)
    old_output, _receipt = r9b_runner.output_paths(ARTIFACT_DIR, review_id)
    invalid = old_output.with_suffix(".invalid.raw")
    before = sha256_path(invalid)
    old_response = json.loads(invalid.read_text(encoding="utf-8"))
    assert r9b.validate_and_choose(
        old_response, packet=packet, defense=defense,
        defense_id=defense_id, retrieval=retrieval,
    )["status"] == "INVALID"
    counterfactual = copy.deepcopy(old_response)
    counterfactual["schema_version"] = VERSION
    result = validate_and_choose(
        counterfactual, packet=packet, defense=defense,
        defense_id=defense_id, retrieval=retrieval,
    )
    assert result["status"] == "VALID"
    assert result["duplicate_evidence_reference_count"] == 1
    assert sha256_path(invalid) == before


def test_budget_stop_before_r9b2_transport(tmp_path: Path) -> None:
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
