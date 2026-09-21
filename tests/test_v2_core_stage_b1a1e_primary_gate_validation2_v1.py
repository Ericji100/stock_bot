from pathlib import Path
from scripts.v2_core_stage_b1a1e_primary_gate_validation2_input_v1 import generate_all
from scripts.v2_core_stage_b1a1e_primary_gate_validation2_manifest_v1 import generate
from scripts.v2_core_stage_b1a1e_primary_gate_validator_v1 import load_json
ROOT=Path(__file__).resolve().parents[1]
ARTIFACT_DIR=ROOT/"reports"/"course_backtest"/"2026-09-10"/"v2_core_reproducible_goal_v1"
def test_fresh_cases_exclude_both_prior_r4_batches():
    m=generate_all(ARTIFACT_DIR); ids={x["review_id"] for x in m["rows"]}
    prior=set()
    for name in ("stage_b1a1e_primary_gate_execution_manifest_candidate_r4.json","stage_b1a1e_primary_gate_validation_execution_manifest_r1.json"):
        prior.update(x["review_id"] for x in load_json(ARTIFACT_DIR/name)["rows"])
    assert len(ids)==4 and ids.isdisjoint(prior)
def test_execution_contract_reuses_r4_exactly():
    m=generate(ARTIFACT_DIR)
    old=load_json(ARTIFACT_DIR/"stage_b1a1e_primary_gate_validation_execution_manifest_r1.json")
    for key in ("prompt_sha256","schema_sha256","runner_sha256","validator_sha256","comparator_sha256"):
        assert m[key]==old[key]
    assert m["expected_case_rounds"]==12
