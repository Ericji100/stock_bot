from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts/hybrid_v3_course_gold_v2.py"
SPEC = importlib.util.spec_from_file_location("hybrid_v3_course_gold_v2_test", MODULE_PATH)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


CourseGoldError = module.CourseGoldError
FormalUniverseError = module.FormalUniverseError
GoldEvaluationError = module.GoldEvaluationError


def test_course_gold_tool_is_final_without_forging_human_gold_data():
    assert module.COURSE_GOLD_STATUS == "FINAL"
    assert module.ANNOTATION_MANIFEST_VERSION.endswith("-v2")


def gold_protocol(*, minimum_cases=1, minimum_strata=1, atomic_rate=0.9):
    return {
        "correctness_gate": {
            "blinded_course_gold_holdout": {
                "minimum_cases": minimum_cases,
                "minimum_distinct_objective_strata": minimum_strata,
                "required_atomic_assertion_agreement_rate": atomic_rate,
                "required_material_permission_agreement_rate": 1.0,
            }
        }
    }


def packet(review_id: str, stock_id: str, as_of: str = "2023-06-01"):
    questions = {
        "global_question_ids": ["Q1"],
        "anchor_candidates": [],
        "relation_candidates": [],
        "stop_candidates": [],
    }
    evidence = [{"ref": f"BAR:{as_of}", "kind": "BAR", "date": as_of}]
    objective = {
        "causal_cutoff_as_of": as_of,
        "ai_visible_evidence_sha256": module.canonical_sha256(evidence),
    }
    value = {
        "review_id": review_id,
        "anonymous_stock_id": stock_id,
        "as_of": as_of,
        "question_manifest": questions,
        "evidence": evidence,
        "objective_facts": objective,
        "question_manifest_sha256": module.canonical_sha256(questions),
        "evidence_catalog_sha256": module.canonical_sha256(evidence),
    }
    value["input_packet_sha256"] = module.canonical_sha256(value)
    return value


def source_row(ordinal: int, stratum: str, *, stock_id: str | None = None):
    review_id = f"D-{ordinal:024x}"
    stock = stock_id or f"S-{ordinal:016x}"
    value = packet(review_id, stock)
    return {
        "source_ordinal": ordinal,
        "review_id": review_id,
        "anonymous_stock_id": stock,
        "sampling_stratum": stratum,
        "packet_sha256": module.canonical_sha256(value),
        "packet": value,
    }


def selection_config(quotas: dict[str, int]):
    return {
        "config_version": module.SELECTION_CONFIG_VERSION,
        "status": "LOCKED_BEFORE_SAMPLE",
        "seed": "FIXED_COURSE_GOLD_TEST_SEED",
        "objective_stratum_quotas": quotas,
        "minimum_cases": sum(quotas.values()),
        "strata_basis": "OBJECTIVE_ONLY_AS_OF",
        "future_performance_used": False,
        "old_ai_output_used": False,
        "real_identity_used": False,
    }


def exclusion(scope: str, universe_sha: str, rows=None):
    rows = list(rows or [])
    return {
        "manifest_version": module.EXCLUSION_MANIFEST_VERSION,
        "status": "LOCKED_COMPLETE_BEFORE_SAMPLE",
        "scope": scope,
        "universe_manifest_sha256": universe_sha,
        "contains_ai_outputs": False,
        "contains_real_identity": False,
        "contains_future_or_performance": False,
        "rows": rows,
        "rows_sha256": module.canonical_sha256(rows),
    }


def exclusions(universe_sha: str, **rows_by_scope):
    return {
        scope: exclusion(scope, universe_sha, rows_by_scope.get(scope, []))
        for scope in module.REQUIRED_EXCLUSION_SCOPES
    }


def plan(rows, quotas, *, excluded=None, protocol=None):
    universe_sha = "a" * 64
    protocol = protocol or gold_protocol(
        minimum_cases=sum(quotas.values()), minimum_strata=len(quotas)
    )
    return module.build_selection_plan(
        rows,
        selection_config=selection_config(quotas),
        exclusion_manifests=excluded or exclusions(universe_sha),
        universe_manifest_sha256=universe_sha,
        protocol_sha256=module.canonical_sha256(protocol),
    )


def annotation_bundle(rows, quotas, *, protocol=None):
    protocol = protocol or gold_protocol(
        minimum_cases=sum(quotas.values()), minimum_strata=len(quotas)
    )
    selection = plan(rows, quotas, protocol=protocol)
    return module.build_annotation_bundle(rows, selection, protocol=protocol), protocol


def test_selection_is_same_for_same_seed_regardless_of_input_order():
    rows = [source_row(index, "A" if index % 2 == 0 else "B") for index in range(20)]
    first = plan(rows, {"A": 3, "B": 3})
    second = plan(list(reversed(rows)), {"A": 3, "B": 3})
    assert first["selection_plan_sha256"] == second["selection_plan_sha256"]
    assert first["cases"] == second["cases"]
    assert first["stratum_counts"] == {"A": 3, "B": 3}


def test_v1_v2_and_research_calibration_stocks_are_hard_excluded():
    rows = [source_row(index, "A", stock_id=f"S-{index}") for index in range(8)]
    universe_sha = "a" * 64
    excluded = exclusions(
        universe_sha,
        V1_AI_REVIEWED=[
            {"review_id": rows[0]["review_id"], "anonymous_stock_id": rows[0]["anonymous_stock_id"]}
        ],
        V2_AI_REVIEWED=[
            {"review_id": rows[1]["review_id"], "anonymous_stock_id": rows[1]["anonymous_stock_id"]}
        ],
        RESEARCH_CALIBRATION=[
            {"review_id": "", "anonymous_stock_id": rows[2]["anonymous_stock_id"]}
        ],
    )
    selected = module.build_selection_plan(
        rows,
        selection_config=selection_config({"A": 3}),
        exclusion_manifests=excluded,
        universe_manifest_sha256=universe_sha,
        protocol_sha256=module.canonical_sha256(gold_protocol(minimum_cases=3)),
    )["cases"]
    selected_stocks = {row["anonymous_stock_id"] for row in selected}
    assert selected_stocks.isdisjoint({rows[0]["anonymous_stock_id"], rows[1]["anonymous_stock_id"], rows[2]["anonymous_stock_id"]})


@pytest.mark.parametrize(
    ("location", "key", "value"),
    [
        ("row", "stock_code", "2330"),
        ("row", "old_ai_output", {"permission": "TRADE"}),
        ("row", "future_return", 2.0),
        ("packet", "company_name", "REAL NAME"),
        ("packet", "prior_ai_output", {"route": "R1"}),
        ("packet", "mfe", 100.0),
    ],
)
def test_contaminated_identity_ai_or_future_fields_are_rejected(location, key, value):
    row = source_row(0, "A")
    row if location == "row" else row["packet"]
    target = row if location == "row" else row["packet"]
    target[key] = value
    if location == "packet":
        row["packet_sha256"] = module.canonical_sha256(row["packet"])
    with pytest.raises(CourseGoldError):
        plan([row], {"A": 1})


def test_future_evidence_date_is_rejected_even_without_outcome_field():
    row = source_row(0, "A")
    row["packet"]["evidence"].append(
        {"ref": "BAR:2023-06-02", "kind": "BAR", "date": "2023-06-02"}
    )
    row["packet"]["evidence_catalog_sha256"] = module.canonical_sha256(row["packet"]["evidence"])
    row["packet"]["objective_facts"]["ai_visible_evidence_sha256"] = module.canonical_sha256(
        row["packet"]["evidence"]
    )
    core = dict(row["packet"])
    core.pop("input_packet_sha256")
    row["packet"]["input_packet_sha256"] = module.canonical_sha256(core)
    row["packet_sha256"] = module.canonical_sha256(row["packet"])
    with pytest.raises(CourseGoldError, match="future date"):
        plan([row], {"A": 1})


def test_formal_selection_cannot_use_fixture_without_full_universe_manifest():
    rows = [source_row(index, "A") for index in range(2)]
    universe_sha = "a" * 64
    with pytest.raises(FormalUniverseError, match="full V2 universe manifest"):
        module.build_selection_plan(
            rows,
            selection_config=selection_config({"A": 2}),
            exclusion_manifests=exclusions(universe_sha),
            universe_manifest_sha256=universe_sha,
            protocol_sha256=module.canonical_sha256(gold_protocol(minimum_cases=2)),
            formal=True,
        )


def test_formal_selection_config_needs_at_least_twenty_cases():
    with pytest.raises(CourseGoldError, match="at least 20"):
        module.validate_selection_config(selection_config({"A": 2}), formal=True)


def test_formal_selection_config_needs_four_objective_strata():
    with pytest.raises(CourseGoldError, match="four objective strata"):
        module.validate_selection_config(selection_config({"A": 20}), formal=True)


def test_formal_universe_rejects_fixture_or_partial_scope():
    manifest = {
        "manifest_version": "hybrid-v3-review-points-v2",
        "status": "LOCKED_OUTCOME_BLIND",
        "universe_scope": "TEST_FIXTURE",
        **module.REQUIRED_FORMAL_UNIVERSE_ATTESTATIONS,
        "sampling_strata_basis": "OBJECTIVE_ONLY_AS_OF",
        "stocks": module.EXPECTED_STOCKS,
        "stock_days_scanned": module.EXPECTED_STOCK_DAYS,
        "review_points": 20,
        "review_points_artifact": {"rows": 20},
    }
    with pytest.raises(FormalUniverseError, match="fixture"):
        module.validate_formal_universe_manifest(manifest)


def test_formal_universe_requires_objective_only_strata_and_full_counts():
    manifest = {
        "manifest_version": "hybrid-v3-review-points-v2",
        "status": "LOCKED_OUTCOME_BLIND",
        "universe_scope": "FULL_V2_REVIEW_POINT_UNIVERSE",
        **module.REQUIRED_FORMAL_UNIVERSE_ATTESTATIONS,
        "sampling_strata_basis": "MODEL_OUTCOME",
        "stocks": module.EXPECTED_STOCKS,
        "stock_days_scanned": module.EXPECTED_STOCK_DAYS,
        "review_points": 20,
        "review_points_artifact": {"rows": 20},
    }
    with pytest.raises(FormalUniverseError, match="objective-only"):
        module.validate_formal_universe_manifest(manifest)


def test_annotation_surface_removes_all_join_ids_and_strata():
    rows = [source_row(index, "A") for index in range(3)]
    bundle, _ = annotation_bundle(rows, {"A": 2})
    assert bundle["annotation_manifest"]["gold_labels_present"] is False
    for row in bundle["annotation_packets"]:
        visible = row["packet"]
        assert "review_id" not in visible
        assert "anonymous_stock_id" not in visible
        assert "source_ordinal" not in visible
        assert "sampling_stratum" not in visible
        assert visible["isolation"] == {
            "real_identity_visible": False,
            "old_ai_visible": False,
            "future_or_performance_visible": False,
            "sampling_stratum_visible": False,
        }
        assert row["annotation_packet_sha256"] == module.canonical_sha256(visible)
    assert all(item["status"] == "DRAFT_UNREVIEWED" for item in bundle["rubric_templates"])
    assert all(not item["assertions"] for item in bundle["rubric_templates"])


def test_immutable_bundle_replays_identically_and_rejects_mutation(tmp_path):
    rows = [source_row(index, "A") for index in range(3)]
    bundle, _ = annotation_bundle(rows, {"A": 2})
    first = module.write_annotation_bundle(bundle, tmp_path)
    second = module.write_annotation_bundle(bundle, tmp_path)
    assert first == second
    mutated = copy.deepcopy(bundle)
    mutated["annotation_manifest"]["case_count"] = 999
    with pytest.raises(CourseGoldError, match="overwrite immutable"):
        module.write_annotation_bundle(mutated, tmp_path)


def frozen_rubric(annotation_row, *, classification="POSITIVE_OR_CONSTRUCTIVE"):
    value = {
        "rubric_version": module.RUBRIC_VERSION,
        "status": "HUMAN_REVIEWED_FROZEN",
        "gold_case_id": annotation_row["gold_case_id"],
        "annotation_packet_sha256": module.canonical_sha256(annotation_row["packet"]),
        "classification": classification,
        "reviewer_attestations": [
            {
                "reviewer_ref": "COURSE_REVIEWER_1",
                "reviewed_at_utc": "2026-09-08T00:00:00Z",
                "course_and_as_of_only": True,
                "old_ai_and_outcomes_not_viewed": True,
            }
        ],
        "course_citations": [
            {
                "citation_id": "C1",
                "source_path": "course_knowledge_base/example.md",
                "section": "S1",
                "proposition": "Only as-of evidence may establish the state.",
            }
        ],
        "assertions": [
            {
                "assertion_id": "A1",
                "target_artifact": "POLICY_DECISION",
                "json_pointer": "/permission",
                "allowed_values": ["WAIT"],
                "course_citation_ids": ["C1"],
                "as_of_evidence_refs": ["BAR:2023-06-01"],
            }
        ],
        "adjudication_complete": True,
        "frozen_at_utc": "2026-09-08T00:01:00Z",
    }
    value["rubric_sha256"] = module.canonical_sha256(value)
    return value


def test_evaluator_fails_closed_on_generated_unreviewed_template(tmp_path):
    rows = [source_row(0, "A")]
    bundle, protocol = annotation_bundle(rows, {"A": 1})
    bundle_manifest = module.write_annotation_bundle(bundle, tmp_path)
    result = {
        "gold_case_id": bundle["annotation_packets"][0]["gold_case_id"],
        "semantic_output": {},
        "policy_decision": {"permission": "WAIT"},
    }
    with pytest.raises(GoldEvaluationError, match="HUMAN_REVIEWED_FROZEN"):
        module.evaluate_gold(
            bundle["annotation_packets"],
            bundle["rubric_templates"],
            [result],
            selection_plan=bundle["selection_plan"],
            annotation_manifest=bundle["annotation_manifest"],
            bundle_manifest=bundle_manifest,
            protocol=protocol,
        )


def test_evaluator_requires_complete_human_assertions_and_hash(tmp_path):
    rows = [source_row(0, "A")]
    bundle, protocol = annotation_bundle(rows, {"A": 1})
    bundle_manifest = module.write_annotation_bundle(bundle, tmp_path)
    annotation = bundle["annotation_packets"][0]
    rubric = frozen_rubric(annotation)
    rubric["assertions"] = []
    core = dict(rubric)
    core.pop("rubric_sha256")
    rubric["rubric_sha256"] = module.canonical_sha256(core)
    with pytest.raises(GoldEvaluationError, match="no machine-readable assertion"):
        module.evaluate_gold(
            [annotation],
            [rubric],
            [{"gold_case_id": annotation["gold_case_id"], "semantic_output": {}, "policy_decision": {}}],
            selection_plan=bundle["selection_plan"],
            annotation_manifest=bundle["annotation_manifest"],
            bundle_manifest=bundle_manifest,
            protocol=protocol,
        )


def test_evaluator_scores_frozen_human_rubrics_without_research_balance(tmp_path):
    rows = [source_row(0, "A"), source_row(1, "B")]
    bundle, protocol = annotation_bundle(rows, {"A": 1, "B": 1})
    bundle_manifest = module.write_annotation_bundle(bundle, tmp_path)
    rubrics = [
        frozen_rubric(bundle["annotation_packets"][0], classification="POSITIVE_OR_CONSTRUCTIVE"),
        frozen_rubric(bundle["annotation_packets"][1], classification="NEGATIVE_OR_AMBIGUOUS"),
    ]
    results = [
        {
            "gold_case_id": row["gold_case_id"],
            "semantic_output": {},
            "policy_decision": {"permission": "WAIT"},
        }
        for row in bundle["annotation_packets"]
    ]
    report = module.evaluate_gold(
        bundle["annotation_packets"],
        rubrics,
        results,
        selection_plan=bundle["selection_plan"],
        annotation_manifest=bundle["annotation_manifest"],
        bundle_manifest=bundle_manifest,
        protocol=protocol,
    )
    assert report["course_gold_pass"] is True
    assert report["passed_cases"] == 2
    assert report["research_balance_rule_applied"] is False
    assert report["atomic_assertion_agreement_rate"] == 1.0
    assert report["material_permission_agreement_rate"] == 1.0


def test_evaluator_rejects_gold_without_material_permission_assertion(tmp_path):
    rows = [source_row(0, "A")]
    bundle, protocol = annotation_bundle(rows, {"A": 1})
    bundle_manifest = module.write_annotation_bundle(bundle, tmp_path)
    annotation = bundle["annotation_packets"][0]
    rubric = frozen_rubric(annotation)
    rubric["assertions"][0].update(
        target_artifact="SEMANTIC_OUTPUT",
        json_pointer="/state",
        allowed_values=["OK"],
    )
    core = dict(rubric)
    core.pop("rubric_sha256")
    rubric["rubric_sha256"] = module.canonical_sha256(core)
    result = {
        "gold_case_id": annotation["gold_case_id"],
        "semantic_output": {"state": "OK"},
        "policy_decision": {"permission": "WAIT"},
    }
    with pytest.raises(GoldEvaluationError, match="material /permission assertion"):
        module.evaluate_gold(
            [annotation],
            [rubric],
            [result],
            selection_plan=bundle["selection_plan"],
            annotation_manifest=bundle["annotation_manifest"],
            bundle_manifest=bundle_manifest,
            protocol=protocol,
        )


def test_evaluator_rejects_annotation_not_bound_to_bundle(tmp_path):
    rows = [source_row(0, "A")]
    bundle, protocol = annotation_bundle(rows, {"A": 1})
    bundle_manifest = module.write_annotation_bundle(bundle, tmp_path)
    annotation = bundle["annotation_packets"][0]
    rubric = frozen_rubric(annotation)
    changed_manifest = copy.deepcopy(bundle["annotation_manifest"])
    changed_manifest["case_count"] = 99
    core = dict(changed_manifest)
    core.pop("annotation_manifest_sha256")
    changed_manifest["annotation_manifest_sha256"] = module.canonical_sha256(core)
    with pytest.raises(GoldEvaluationError, match="case count"):
        module.evaluate_gold(
            [annotation],
            [rubric],
            [{
                "gold_case_id": annotation["gold_case_id"],
                "semantic_output": {},
                "policy_decision": {"permission": "WAIT"},
            }],
            selection_plan=bundle["selection_plan"],
            annotation_manifest=changed_manifest,
            bundle_manifest=bundle_manifest,
            protocol=protocol,
        )


def test_gold_uses_ninety_percent_atomic_rate_but_one_hundred_percent_permission(tmp_path):
    rows = [source_row(0, "A")]
    protocol = gold_protocol(minimum_cases=1, minimum_strata=1, atomic_rate=0.9)
    bundle, _ = annotation_bundle(rows, {"A": 1}, protocol=protocol)
    bundle_manifest = module.write_annotation_bundle(bundle, tmp_path)
    annotation = bundle["annotation_packets"][0]
    rubric = frozen_rubric(annotation)
    rubric["assertions"] = [
        {
            "assertion_id": "PERMISSION",
            "target_artifact": "POLICY_DECISION",
            "json_pointer": "/permission",
            "allowed_values": ["WAIT"],
            "course_citation_ids": ["C1"],
            "as_of_evidence_refs": ["BAR:2023-06-01"],
        }
    ] + [
        {
            "assertion_id": f"SEMANTIC_{index}",
            "target_artifact": "SEMANTIC_OUTPUT",
            "json_pointer": f"/answer_{index}",
            "allowed_values": ["PASS"],
            "course_citation_ids": ["C1"],
            "as_of_evidence_refs": ["BAR:2023-06-01"],
        }
        for index in range(1, 10)
    ]
    core = dict(rubric)
    core.pop("rubric_sha256")
    rubric["rubric_sha256"] = module.canonical_sha256(core)
    semantic = {f"answer_{index}": "PASS" for index in range(1, 10)}
    semantic["answer_9"] = "FAIL"
    report = module.evaluate_gold(
        [annotation],
        [rubric],
        [{
            "gold_case_id": annotation["gold_case_id"],
            "semantic_output": semantic,
            "policy_decision": {"permission": "WAIT"},
        }],
        selection_plan=bundle["selection_plan"],
        annotation_manifest=bundle["annotation_manifest"],
        bundle_manifest=bundle_manifest,
        protocol=protocol,
    )
    assert report["atomic_assertion_agreement_rate"] == 0.9
    assert report["material_permission_agreement_rate"] == 1.0
    assert report["course_gold_pass"] is True
    assert report["passed_cases"] == 0
