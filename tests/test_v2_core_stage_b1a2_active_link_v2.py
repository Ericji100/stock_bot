from pathlib import Path
from scripts.v2_core_stage_b1a2_active_link_input_v2 import generate_all
from scripts.v2_core_stage_b1a2_active_link_manifest_v2 import generate
from scripts.v2_core_stage_b1a2_active_link_manifest_v2a import generate as generate_r2a
from scripts.v2_core_stage_b1a2_active_link_validator_v1 import load_json

ROOT=Path(__file__).resolve().parents[1]
ARTIFACT_DIR=ROOT/"reports"/"course_backtest"/"2026-09-10"/"v2_core_reproducible_goal_v1"

def test_r2_has_five_cases_and_no_future_or_prior_answers():
    m=generate_all(ARTIFACT_DIR)
    assert m["case_count"]==5
    assert m["future_performance_used"] is False and m["prior_role_answers_used"] is False

def test_r2_paths_are_forward_and_non_auxiliary():
    m=generate_all(ARTIFACT_DIR)
    for row in m["rows"]:
        packet=load_json(ARTIFACT_DIR/m["input_packet_directory"]/row["input_packet_file"])
        segments={x["candidate_id"]:x for x in packet["path_context_segments"]}
        for option in packet["role_link_evidence_options"]:
            for path in option["fixed_relation_paths"]:
                assert all(segments[x]["scale"] in {"LARGE","SMALL"} for x in path["segment_ids"])
                for left,right,rel in zip(path["segment_ids"],path["segment_ids"][1:],path["relations"]):
                    if rel["relation_basis"]=="SHARED_BOUNDARY":
                        assert left==rel["left_segment_id"] and right==rel["right_segment_id"]

def test_regression_candidate_has_no_causal_path():
    m=generate_all(ARTIFACT_DIR); rid="FP-d79297cd1f01c1600d27cb75"
    row=next(x for x in m["rows"] if x["review_id"]==rid)
    packet=load_json(ARTIFACT_DIR/m["input_packet_directory"]/row["input_packet_file"])
    option=next(x for x in packet["role_link_evidence_options"] if x["candidate_id"]=="SEG-54010644172260f8ae4a")
    assert option["no_fixed_path_found"] is True and option["fixed_relation_paths"]==[]

def test_r2_execution_manifest_is_frozen_to_same_model():
    m=generate(ARTIFACT_DIR)
    assert m["expected_case_rounds"]==15
    assert (m["formal_model"],m["reasoning_effort"])==("gpt-5.6-sol","xhigh")
    assert len(m["bindings"])==8

def test_r2a_changes_only_transport_schema_binding():
    m=generate_r2a(ARTIFACT_DIR)
    assert m["technical_delta_from_r2"]=="ADD_TRANSPORT_DEFS_ONLY"
    schema=load_json(ARTIFACT_DIR/m["schema_file"])
    assert schema["$defs"]["evidenceRef"]=={"type":"string"}
    assert m["expected_case_rounds"]==15
