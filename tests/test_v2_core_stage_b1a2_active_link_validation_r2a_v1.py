from pathlib import Path
from scripts.v2_core_stage_b1a2_active_link_validation_r2a_input_v1 import generate_all
from scripts.v2_core_stage_b1a2_active_link_validation_r2a_manifest_v1 import generate
from scripts.v2_core_stage_b1a2_active_link_validator_v1 import load_json
ROOT=Path(__file__).resolve().parents[1];ARTIFACT_DIR=ROOT/"reports"/"course_backtest"/"2026-09-10"/"v2_core_reproducible_goal_v1"
def test_cases_are_fresh_relative_to_r1_batches():
    m=generate_all(ARTIFACT_DIR);ids={x["review_id"] for x in m["rows"]};prior=set()
    for name in ("stage_b1a2_active_link_input_manifest_candidate_r1.json","stage_b1a2_active_link_validation_input_manifest_r1.json"):
        prior.update(x["review_id"] for x in load_json(ARTIFACT_DIR/name)["rows"])
    assert len(ids)==4 and ids.isdisjoint(prior) and m["r1_or_r2a_answers_used"] is False
def test_frozen_r2a_contract():
    m=generate(ARTIFACT_DIR);r2a=load_json(ARTIFACT_DIR/"stage_b1a2_active_link_execution_manifest_candidate_r2a.json")
    for key in ("prompt_sha256","schema_sha256","runner_sha256","validator_sha256","comparator_sha256"):
        assert m["bindings"][key]==r2a["bindings"][key]
    assert m["expected_case_rounds"]==12
