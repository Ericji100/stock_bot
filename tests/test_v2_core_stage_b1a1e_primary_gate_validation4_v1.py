from pathlib import Path

from scripts import v2_core_stage_b1a1e_primary_gate_validation2_input_v1 as base
from scripts.v2_core_stage_b1a1e_primary_gate_validation4_input_v1 import generate_all
from scripts.v2_core_stage_b1a1e_primary_gate_validation4_manifest_v1 import generate
from scripts.v2_core_stage_b1a1e_primary_gate_validator_v1 import load_json


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_DIR = ROOT / "reports" / "course_backtest" / "2026-09-10" / "v2_core_reproducible_goal_v1"


def test_fifth_batch_is_fresh_and_does_not_mutate_base_configuration():
    before = (base.SELECTION_SEED, base.EXCLUSION_MANIFESTS, base.OUTPUT_DIRECTORY, base.PROGRAM_GATE_DIRECTORY, base.OUTPUT_MANIFEST, base.OUTPUT_SELECTION)
    source = generate_all(ARTIFACT_DIR)
    after = (base.SELECTION_SEED, base.EXCLUSION_MANIFESTS, base.OUTPUT_DIRECTORY, base.PROGRAM_GATE_DIRECTORY, base.OUTPUT_MANIFEST, base.OUTPUT_SELECTION)
    prior = set()
    for name in (
        "stage_b1a1e_smoke_execution_manifest_candidate_r2.json",
        "stage_b1a1e_validation_execution_manifest_r1.json",
        "stage_b1a1e_primary_execution_manifest_candidate_r3.json",
        "stage_b1a1e_primary_gate_execution_manifest_candidate_r4.json",
        "stage_b1a1e_primary_gate_validation_execution_manifest_r1.json",
        "stage_b1a1e_primary_gate_validation2_execution_manifest_r1.json",
        "stage_b1a1e_primary_gate_validation3_execution_manifest_r1.json",
    ):
        prior.update(str(row["review_id"]) for row in load_json(ARTIFACT_DIR / name)["rows"])
    ids = {str(row["review_id"]) for row in source["rows"]}
    assert len(ids) == 4 and ids.isdisjoint(prior)
    assert before == after
    assert source["future_performance_used"] is False
    assert source["prior_answers_present_in_ai_packets"] is False


def test_fifth_batch_manifest_freezes_existing_r4_gate_and_budget_policy():
    manifest = generate(ARTIFACT_DIR)
    assert manifest["expected_case_rounds"] == 12
    assert manifest["formal_model"] == "gpt-5.6-sol"
    assert manifest["reasoning_effort"] == "xhigh"
    assert manifest["stop_remaining_percent_lte"] == 60
    assert manifest["operational_policy_file"] == "operational_budget_override_v13.json"
