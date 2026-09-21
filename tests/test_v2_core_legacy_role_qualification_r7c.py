"""R7C fixed-key transport prevents silent candidate omission or duplication."""

from __future__ import annotations

import json

import pytest

from scripts.v2_core_legacy_role_qualification_r7c import (
    VERSION, build_schema, parse_raw_without_duplicate_keys, transport_for,
    validate_and_resolve,
)
from scripts.v2_core_legacy_role_qualification_runner_r7 import _bound_inputs
from scripts.v2_core_legacy_role_qualification_runner_r7b import frozen_episode
from scripts.v2_core_legacy_role_shortlist_r7 import ARTIFACT_DIR
from tests.test_v2_core_legacy_role_qualification_r7b import bound_answer


REVIEW_ID = "FP-38178c2820dd71aa800fd8d7"
ROLE = "CONTROLLING_MATURE_CAMPAIGN"


def context() -> tuple[dict, dict, dict]:
    _manifest, _row, packet, shortlist = _bound_inputs(ARTIFACT_DIR, REVIEW_ID, ROLE)
    episode, _paths = frozen_episode(ARTIFACT_DIR, REVIEW_ID)
    return packet, shortlist, episode


def keyed_answer(packet: dict, shortlist: dict, episode: dict) -> dict:
    response = bound_answer(packet, shortlist, ROLE)
    response["schema_version"] = VERSION
    response["bound_current_episode_candidate_id"] = episode["selected_candidate_id"]
    response["bound_current_episode_evidence_option_id"] = episode["selected_evidence_option_id"]
    response["candidate_assessments"] = {
        row["candidate_id"]: {key: value for key, value in row.items() if key != "candidate_id"}
        for row in response["candidate_assessments"]
    }
    return response


def test_all_candidate_ids_are_required_properties() -> None:
    packet, shortlist, episode = context()
    schema = build_schema(packet, shortlist, ROLE, episode)
    expected = {row["candidate_id"] for row in shortlist["roles"][ROLE]["candidate_rows"]}
    assert set(schema["properties"]["candidate_assessments"]["required"]) == expected
    assert set(schema["properties"]["candidate_assessments"]["properties"]) == expected
    assert "$ref" not in json.dumps(transport_for(packet, shortlist, ROLE, episode))
    result = validate_and_resolve(keyed_answer(packet, shortlist, episode), packet=packet, shortlist=shortlist, role=ROLE, episode_result=episode)
    assert result["status"] == "VALID"
    assert result["transport_version"] == VERSION


def test_missing_or_extra_candidate_key_is_invalid() -> None:
    packet, shortlist, episode = context()
    response = keyed_answer(packet, shortlist, episode)
    removed_key = next(iter(response["candidate_assessments"]))
    removed = response["candidate_assessments"].pop(removed_key)
    assert validate_and_resolve(response, packet=packet, shortlist=shortlist, role=ROLE, episode_result=episode)["status"] == "INVALID"
    response["candidate_assessments"][removed_key] = removed
    response["candidate_assessments"]["CAMSEG-fake"] = removed
    assert validate_and_resolve(response, packet=packet, shortlist=shortlist, role=ROLE, episode_result=episode)["status"] == "INVALID"


def test_duplicate_raw_json_key_is_rejected_before_normalization() -> None:
    with pytest.raises(ValueError, match="duplicate JSON key"):
        parse_raw_without_duplicate_keys(b'{"candidate_assessments":{"CAMSEG-a":1,"CAMSEG-a":2}}')
