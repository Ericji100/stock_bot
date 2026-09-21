from __future__ import annotations

from copy import deepcopy

from scripts.v2_core_stage_b1a1e_candidate_validator_v1 import (
    load_json,
    validate_stage_b1a1e_response,
)
from scripts.v2_core_stage_b1a1e_focus_input_v1 import (
    ARTIFACT_DIR,
    OUTPUT_DIRECTORY,
    generate_all,
)


SCHEMA_PATH = ARTIFACT_DIR / "v2_core_stage_b1a1e.schema.candidate_r1.json"


def _fixture():
    manifest = generate_all(ARTIFACT_DIR)
    row = manifest["rows"][0]
    packet = load_json(ARTIFACT_DIR / OUTPUT_DIRECTORY / row["input_packet_file"])
    schema = load_json(SCHEMA_PATH)
    options = {}
    for option in packet["evidence_options"]:
        options.setdefault(option["candidate_id"], {}).setdefault(
            option["atom_name"], []
        ).append(option)
    response_rows = []
    for segment in packet["selected_focus_segments"]:
        candidate_id = segment["candidate_id"]
        directional = options[candidate_id]["directional_coherence"][0]
        targets = options[candidate_id]["structural_challenge_or_break"]
        response_rows.append(
            {
                "candidate_id": candidate_id,
                "directional_path": {
                    "evidence_option_id": directional["evidence_option_id"],
                    "judgement": "INSUFFICIENT",
                    "reason_code": "REQUIRED_EVIDENCE_INSUFFICIENT",
                },
                "structural_targets": [
                    {
                        "evidence_option_id": option["evidence_option_id"],
                        "same_level_meaningful_target": {
                            "judgement": "INSUFFICIENT",
                            "reason_code": "REQUIRED_EVIDENCE_INSUFFICIENT",
                        },
                        "challenge_or_break_realized": {
                            "judgement": (
                                "INSUFFICIENT"
                                if option["target"]["price_reached_or_crossed_objective"]
                                else "CONTRADICTS"
                            ),
                            "reason_code": (
                                "REQUIRED_EVIDENCE_INSUFFICIENT"
                                if option["target"]["price_reached_or_crossed_objective"]
                                else "VISIBLE_EVIDENCE_CONTRADICTS"
                            ),
                        },
                    }
                    for option in targets
                ],
            }
        )
    response = {
        "schema_version": "v2-core-stage-b1a1e-focus-r1-candidate",
        "candidate_perceptions": response_rows,
    }
    return packet, schema, response


def test_complete_insufficient_response_is_valid_and_derives_unknown():
    packet, schema, response = _fixture()
    result = validate_stage_b1a1e_response(
        response=response, schema=schema, packet=packet
    )
    assert result["status"] == "VALID"
    assert result["submitted_candidate_count"] == len(packet["selected_focus_segments"])
    assert result["partial_eligible_ids"] == []
    assert all(
        row["directional_coherence"]["result"] == "UNKNOWN"
        for row in result["derived_candidate_results"]
    )
    assert result["validator_contract"][
        "aggregate_results_derived_by_frozen_program"
    ] is True


def test_missing_structural_option_is_invalid():
    packet, schema, response = _fixture()
    response["candidate_perceptions"][0]["structural_targets"].pop()
    result = validate_stage_b1a1e_response(
        response=response, schema=schema, packet=packet
    )
    assert result["status"] == "INVALID"
    assert any("structural option set differs" in error for error in result["errors"])


def test_unreached_target_cannot_be_supports():
    packet, schema, response = _fixture()
    target = next(
        value
        for row in response["candidate_perceptions"]
        for value in row["structural_targets"]
        if not next(
            option
            for option in packet["evidence_options"]
            if option["evidence_option_id"] == value["evidence_option_id"]
        )["target"]["price_reached_or_crossed_objective"]
    )
    target["challenge_or_break_realized"] = {
        "judgement": "SUPPORTS",
        "reason_code": "VISIBLE_EVIDENCE_SUPPORTS",
    }
    result = validate_stage_b1a1e_response(
        response=response, schema=schema, packet=packet
    )
    assert result["status"] == "INVALID"
    assert any("unreached target must be CONTRADICTS" in error for error in result["errors"])


def test_truth_table_derives_pass_and_fixed_primary_without_repair():
    packet, schema, response = _fixture()
    option_index = {
        option["evidence_option_id"]: option for option in packet["evidence_options"]
    }
    first = next(
        row
        for row in response["candidate_perceptions"]
        if any(
            option_index[value["evidence_option_id"]]["target"][
                "price_reached_or_crossed_objective"
            ]
            for value in row["structural_targets"]
        )
    )
    first["directional_path"]["judgement"] = "SUPPORTS"
    first["directional_path"]["reason_code"] = "VISIBLE_EVIDENCE_SUPPORTS"
    target = next(
        value
        for value in first["structural_targets"]
        if option_index[value["evidence_option_id"]]["target"][
            "price_reached_or_crossed_objective"
        ]
    )
    target["same_level_meaningful_target"] = {
        "judgement": "SUPPORTS",
        "reason_code": "VISIBLE_EVIDENCE_SUPPORTS",
    }
    target["challenge_or_break_realized"] = {
        "judgement": "SUPPORTS",
        "reason_code": "VISIBLE_EVIDENCE_SUPPORTS",
    }
    result = validate_stage_b1a1e_response(
        response=response, schema=schema, packet=packet
    )
    assert result["status"] == "VALID"
    derived = next(
        row
        for row in result["derived_candidate_results"]
        if row["candidate_id"] == first["candidate_id"]
    )
    assert derived["directional_coherence"]["result"] == "PASS"
    assert derived["structural_challenge_or_break"]["result"] == "PASS"
    assert derived["partial_eligible"] is True
    assert result["validator_contract"]["ai_evidence_perceptions_repaired"] is False


def test_schema_forbids_free_explanation_fields():
    packet, schema, response = _fixture()
    modified = deepcopy(response)
    modified["candidate_perceptions"][0]["explanation"] = "free text"
    result = validate_stage_b1a1e_response(
        response=modified, schema=schema, packet=packet
    )
    assert result["status"] == "INVALID"
    assert any("Additional properties" in error for error in result["errors"])
