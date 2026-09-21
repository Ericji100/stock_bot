from pathlib import Path

from scripts import v2_core_stage_b1a2_active_link_validation_r2a_input_v1 as base
from scripts.v2_core_stage_b1a2_active_link_validation_r4_input_v1 import generate_all
from scripts.v2_core_stage_b1a2_active_link_validation_r4_manifest_v1 import generate
from scripts.v2_core_stage_b1a2_active_link_validator_v1 import load_json


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_DIR = ROOT / "reports" / "course_backtest" / "2026-09-10" / "v2_core_reproducible_goal_v1"


def test_r4_active_link_cases_are_fresh_and_base_configuration_is_restored():
    before = (base.SOURCE_EXECUTION_MANIFEST, base.SOURCE_FINAL_REPORT, base.OUTPUT_DIRECTORY, base.OUTPUT_MANIFEST)
    source = generate_all(ARTIFACT_DIR)
    after = (base.SOURCE_EXECUTION_MANIFEST, base.SOURCE_FINAL_REPORT, base.OUTPUT_DIRECTORY, base.OUTPUT_MANIFEST)
    prior = set()
    for name in (
        "stage_b1a2_active_link_execution_manifest_candidate_r2a.json",
        "stage_b1a2_active_link_validation_r2a_execution_manifest_r1.json",
        "stage_b1a2_active_link_validation_r3_execution_manifest_r1.json",
    ):
        prior.update(str(row["review_id"]) for row in load_json(ARTIFACT_DIR / name)["rows"])
    ids = {str(row["review_id"]) for row in source["rows"]}
    assert len(ids) == 4 and ids.isdisjoint(prior)
    assert before == after
    assert source["future_performance_used"] is False
    assert source["prior_role_answers_used"] is False


def test_r4_manifest_freezes_two_edge_escalation_before_ai_outputs():
    manifest = generate(ARTIFACT_DIR)
    assert manifest["expected_case_rounds"] == 12
    assert manifest["escalation_min_fixed_path_edges"] == 2
    assert manifest["escalation_requires_unanimous_support"] is True
    assert manifest["stop_remaining_percent_lte"] == 60.0
    assert manifest["policy_file"] == "operational_budget_override_v13.json"
