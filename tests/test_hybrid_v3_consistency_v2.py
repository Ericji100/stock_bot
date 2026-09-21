from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts/hybrid_v3_consistency_v2.py"
SPEC = importlib.util.spec_from_file_location("hybrid_v3_consistency_v2_test", MODULE_PATH)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


ConsistencyError = module.ConsistencyError
FormalReadinessError = module.FormalReadinessError


def test_consistency_tool_is_final_but_acceptance_still_depends_on_data_gates():
    assert module.CONSISTENCY_STATUS == "FINAL"


def protocol(*, strata: dict[str, int], **overrides):
    consistency = {
        "sample_size": sum(strata.values()),
        "runs": 3,
        "seed": "FIXED_TEST_SEED",
        "strata": strata,
        "schema_causality_evidence_rate": 1.0,
        "permission_rate": 1.0,
        "scenario_phase_rate": 1.0,
        "critical_atom_rate": 1.0,
        "critical_question_ids": ["ATOM_A", "ATOM_B"],
        "trade_action_signature_rate": 1.0,
        "minimum_conservative_trade_cases": 2,
        "minimum_trade_routes": 2,
        "minimum_wait_cases": 1,
        "minimum_remove_cases_when_eligible": 1,
    }
    consistency.update(overrides)
    return {
        "consistency": consistency,
        "policy": {
            "material_action_signature_fields": [
                "direction",
                "signal_event_ref",
                "episode_stop_ref",
                "campaign_stop_ref",
                "position_role",
            ]
        },
    }


def source_record(ordinal: int, stratum: str, *, stock: str | None = None, review: str | None = None):
    as_of = "2023-06-01"
    questions = {
        "global_question_ids": ["ATOM_A", "ATOM_B"],
        "anchor_candidates": [],
        "relation_candidates": [],
        "stop_candidates": [],
    }
    evidence = [{"ref": f"BAR:{as_of}", "kind": "BAR", "date": as_of}]
    objective = {
        "causal_cutoff_as_of": as_of,
        "ai_visible_evidence_sha256": module.canonical_sha256(evidence),
    }
    packet = {
        "review_id": review or f"D-{ordinal:024x}",
        "anonymous_stock_id": stock or f"S-{ordinal:016x}",
        "as_of": as_of,
        "question_manifest": questions,
        "evidence": evidence,
        "objective_facts": objective,
        "question_manifest_sha256": module.canonical_sha256(questions),
        "evidence_catalog_sha256": module.canonical_sha256(evidence),
    }
    packet["input_packet_sha256"] = module.canonical_sha256(packet)
    return {
        "source_ordinal": ordinal,
        "review_id": packet["review_id"],
        "anonymous_stock_id": packet["anonymous_stock_id"],
        "sampling_stratum": stratum,
        "packet_sha256": module.canonical_sha256(packet),
        "packet": packet,
    }


def verdict(value: str):
    return {
        "result": value,
        "supporting_evidence_refs": ["BAR:2023-06-01"],
        "contradicting_evidence_refs": [],
        "missing_evidence_codes": [],
        "reason_code": "TEST",
    }


def semantic(review_id: str, *, permission="TRADE", route="R1", atom="PASS", scenario="S1"):
    return {
        "review_id": review_id,
        "candidate_answers": {
            "anchor_candidates": [],
            "relation_candidates": [],
            "stop_candidates": [],
        },
        "global_answers": {"ATOM_A": verdict(atom), "ATOM_B": verdict("PASS")},
        "test_policy": {
            "permission": permission,
            "route": route,
            "scenario": scenario,
            "phase": "EARLY",
            "action_signature": {
                "direction": "UP",
                "signal_event_ref": review_id,
                "episode_stop_ref": "STOP:EPISODE",
                "campaign_stop_ref": "STOP:CAMPAIGN",
                "position_role": "MOTHER",
            },
        },
    }


def validate(_packet, output):
    return [] if output.get("review_id") and output.get("valid", True) else ["invalid"]


def reduce(_packet, output):
    decision = dict(output["test_policy"])
    if output["global_answers"]["ATOM_A"]["result"] == "UNKNOWN":
        decision.update(permission="WAIT", route="NO_TRADE", action_signature=None)
    return decision


def research_gate_report():
    core = {
        "version": "hybrid-v2-research-calibration-v1",
        "status": "FINAL",
        "classification": "RESEARCH_CALIBRATION_NOT_PERFORMANCE_HOLDOUT",
        "case_count": 20,
        "human_frozen_rubric_count": 20,
        "classification_counts": {
            "positive_or_constructive": 10,
            "negative_or_ambiguous": 10,
        },
        "rubrics_frozen_before_ai": True,
        "separated_from_future_performance": True,
        "pass_rate": 1.0,
        "research_calibration_pass": True,
    }
    return {**core, "evaluation_sha256": module.canonical_sha256(core)}


def blinded_gold_report():
    core = {
        "course_gold_version": "hybrid-v3-course-gold-v2",
        "course_gold_status": "FINAL",
        "classification": "BLINDED_HISTORICAL_COURSE_GOLD_NOT_PERFORMANCE_HOLDOUT",
        "case_count": 20,
        "stratum_counts": {"A": 5, "B": 5, "C": 5, "D": 5},
        "provenance_pass": True,
        "formal_source": True,
        "selection_plan_sha256": "a" * 64,
        "bundle_manifest_sha256": "b" * 64,
        "protocol_sha256": "c" * 64,
        "atomic_assertion_agreement_rate": 0.9,
        "material_permission_agreement_rate": 1.0,
        "course_gold_pass": True,
    }
    return {**core, "evaluation_sha256": module.canonical_sha256(core)}


def envelope(identity, run_number, output, *, attempt=1):
    return {
        "status": "VALID",
        "run_number": run_number,
        "source_ordinal": identity["source_ordinal"],
        "review_id": identity["review_id"],
        "packet_sha256": identity["packet_sha256"],
        "successful_attempt": attempt,
        "elapsed_seconds": 0.5 + run_number,
        "output_sha256": module.canonical_sha256(output),
        "output": output,
    }


def test_holdout_is_deterministic_stratified_and_disjoint_from_v1():
    rows = []
    ordinal = 0
    for stratum in ("A", "B"):
        for index in range(10):
            stock = "S-v1-stock" if index == 0 else f"S-{stratum}-{index}"
            rows.append(source_record(ordinal, stratum, stock=stock))
            ordinal += 1
    excluded_review = rows[1]["review_id"]
    v1 = [
        {"review_id": excluded_review, "anonymous_stock_id": "S-other"},
        {"review_id": "D-not-present", "anonymous_stock_id": "S-v1-stock"},
    ]
    settings = protocol(strata={"A": 3, "B": 3})
    first = module.build_holdout_plan(
        rows,
        protocol=settings,
        v1_rows=v1,
        outcome_blind_locked=True,
        source_sha256="a" * 64,
        reserve_block_count=1,
    )
    second = module.build_holdout_plan(
        list(reversed(rows)),
        protocol=settings,
        v1_rows=list(reversed(v1)),
        outcome_blind_locked=True,
        source_sha256="a" * 64,
        reserve_block_count=1,
    )
    assert first["plan_sha256"] == second["plan_sha256"]
    assert first["primary"]["stratum_counts"] == {"A": 3, "B": 3}
    assert first["reserve_blocks"][0]["stratum_counts"] == {"A": 3, "B": 3}
    selected = first["primary"]["rows"] + first["reserve_blocks"][0]["rows"]
    assert excluded_review not in {row["review_id"] for row in selected}
    assert "S-v1-stock" not in {row["anonymous_stock_id"] for row in selected}
    assert [row["source_ordinal"] for row in first["primary"]["rows"]] == sorted(
        row["source_ordinal"] for row in first["primary"]["rows"]
    )


def test_holdout_uses_v1_stock_only_as_a_recorded_fallback():
    rows = [
        source_record(0, "A", stock="S-new"),
        source_record(1, "A", stock="S-old"),
    ]
    plan = module.build_holdout_plan(
        rows,
        protocol=protocol(strata={"A": 1}),
        v1_rows=[{"review_id": "D-other", "anonymous_stock_id": "S-old"}],
        outcome_blind_locked=True,
        reserve_block_count=1,
    )
    assert plan["stock_disjoint_fallback_counts"] == {"PRIMARY": 0, "RESERVE_01": 1}


def test_holdout_rejects_unlocked_or_future_visible_source():
    row = source_record(0, "A")
    with pytest.raises(ConsistencyError, match="LOCKED_OUTCOME_BLIND"):
        module.build_holdout_plan(
            [row], protocol=protocol(strata={"A": 1}), outcome_blind_locked=False
        )
    row["packet"]["future_data_visible"] = True
    row["packet_sha256"] = module.canonical_sha256(row["packet"])
    with pytest.raises(ConsistencyError, match="future_data_visible"):
        module.build_holdout_plan(
            [row],
            protocol=protocol(strata={"A": 1}),
            outcome_blind_locked=True,
            reserve_block_count=1,
        )


def test_holdout_rejects_nested_outcome_even_when_visibility_flags_are_absent():
    row = source_record(0, "A")
    row["packet"]["objective_facts"]["nested"] = {"mfe": 99.0}
    core = dict(row["packet"])
    core.pop("input_packet_sha256")
    row["packet"]["input_packet_sha256"] = module.canonical_sha256(core)
    row["packet_sha256"] = module.canonical_sha256(row["packet"])
    with pytest.raises(ConsistencyError, match="forbidden identity/performance key"):
        module.build_holdout_plan(
            [row],
            protocol=protocol(strata={"A": 1}),
            outcome_blind_locked=True,
            reserve_block_count=1,
        )


def test_holdout_rejects_insufficient_complete_reserve_block():
    rows = [source_record(index, "A") for index in range(3)]
    with pytest.raises(ConsistencyError, match="4 required"):
        module.build_holdout_plan(
            rows,
            protocol=protocol(strata={"A": 2}),
            outcome_blind_locked=True,
            reserve_block_count=1,
        )


def test_select_records_detects_post_sample_mutation():
    rows = [source_record(index, "A") for index in range(2)]
    plan = module.build_holdout_plan(
        rows,
        protocol=protocol(strata={"A": 1}),
        outcome_blind_locked=True,
        reserve_block_count=1,
    )
    selected = module.select_records_for_block(rows, plan)
    assert len(selected) == 1
    chosen = selected[0]
    chosen["source_ordinal"] += 99
    with pytest.raises(ConsistencyError, match="changed"):
        module.select_records_for_block(rows, plan)


def test_exact_coverage_rejects_missing_duplicate_and_hash_change():
    identity = module._normalized_case(source_record(0, "A"))
    output = semantic(identity["review_id"])
    valid = [envelope(identity, run_number, output) for run_number in (1, 2, 3)]
    module.assert_exact_three_run_coverage([identity], [[valid[0]], [valid[1]], [valid[2]]])
    with pytest.raises(ConsistencyError, match="coverage mismatch"):
        module.assert_exact_three_run_coverage([identity], [[valid[0]], [], [valid[2]]])
    with pytest.raises(ConsistencyError, match="duplicates"):
        module.assert_exact_three_run_coverage(
            [identity], [[valid[0], valid[0]], [valid[1]], [valid[2]]]
        )
    changed = dict(valid[2], packet_sha256="f" * 64)
    with pytest.raises(ConsistencyError, match="packet_sha256 mismatch"):
        module.assert_exact_three_run_coverage(
            [identity], [[valid[0]], [valid[1]], [changed]]
        )


def test_conservative_merge_turns_each_disagreeing_atom_unknown_without_majority():
    outputs = [
        semantic("D-x", atom="PASS"),
        semantic("D-x", atom="PASS"),
        semantic("D-x", atom="FAIL"),
    ]
    merged, metrics = module.conservative_merge_atomic_outputs(
        outputs, critical_ids={"ATOM_A", "ATOM_B"}
    )
    assert merged["global_answers"]["ATOM_A"]["result"] == "UNKNOWN"
    assert merged["global_answers"]["ATOM_A"]["missing_evidence_codes"] == [
        "CONFLICTING_VISIBLE_EVIDENCE"
    ]
    assert metrics["critical_atom_fields"] == 2
    assert metrics["critical_atom_exact_fields"] == 1
    assert metrics["critical_atom_rate"] == 0.5
    assert metrics["disagreement_paths"] == ["global_answers.ATOM_A"]


def test_noncritical_stability_cannot_dilute_one_critical_disagreement():
    outputs = []
    for run_number in range(3):
        output = semantic("D-x")
        output["global_answers"] = {
            **{f"NONCRITICAL_{index}": verdict("PASS") for index in range(99)},
            "DIRECTION_CRITICAL": verdict("FAIL" if run_number == 2 else "PASS"),
        }
        outputs.append(output)
    _, metrics = module.conservative_merge_atomic_outputs(
        outputs, critical_ids={"DIRECTION_CRITICAL"}
    )
    assert metrics["critical_atom_fields"] == 1
    assert metrics["critical_atom_exact_fields"] == 0
    assert metrics["critical_atom_rate"] == 0.0
    assert metrics["critical_by_question"]["DIRECTION_CRITICAL"] == {
        "fields": 1,
        "exact_fields": 0,
        "rate": 0.0,
    }
    assert metrics["critical_disagreement_paths"] == [
        "global_answers.DIRECTION_CRITICAL"
    ]


def test_missing_frozen_critical_metadata_fails_closed():
    settings = protocol(strata={"A": 1})
    del settings["consistency"]["critical_question_ids"]
    with pytest.raises(ConsistencyError, match="critical question metadata is missing"):
        module.critical_question_ids(settings, atomic_schema={})


def _evaluation_fixture():
    definitions = [
        ("TRADE", "R1", "A"),
        ("TRADE", "R2", "B"),
        ("WAIT", "NO_TRADE", "C"),
        ("REMOVE", "NO_TRADE", "MACRO_DEFENSE_REMOVE_PROXY"),
    ]
    records = [source_record(index, stratum) for index, (_, _, stratum) in enumerate(definitions)]
    identities = [module._normalized_case(row) for row in records]
    block = module._block(identities, "PRIMARY")
    runs = [[], [], []]
    for identity, (permission, route, _) in zip(identities, definitions):
        output = semantic(identity["review_id"], permission=permission, route=route)
        for run_number in range(1, 4):
            runs[run_number - 1].append(
                envelope(identity, run_number, output, attempt=2 if run_number == 2 else 1)
            )
    return records, block, runs


def test_evaluation_passes_stable_mixed_actions_and_reports_operations_separately():
    records, block, runs = _evaluation_fixture()
    report = module.evaluate_consistency(
        records=records,
        sample_block=block,
        runs=runs,
        protocol=protocol(strata={"unused": 4}),
        validate_fn=validate,
        reduce_fn=reduce,
    )
    assert report["passed"] is False
    assert report["repeatability_pass"] is True
    assert report["overall_formal_pass"] is False
    assert report["three_run_repeatability"]["passed"] is True
    assert report["conservative_permission_counts"] == {"REMOVE": 1, "TRADE": 2, "WAIT": 1}
    assert report["positive_route_counts"] == {"R1": 1, "R2": 1}
    assert report["operational_reliability"]["retry_count"] == 4
    assert report["course_correctness"]["status"].startswith("N/A_")
    assert report["gold_case_agreement"]["status"].startswith("N/A_")
    assert report["future_performance"]["status"] == "LOCKED_PENDING_FULL_SEMANTIC_LEDGER"


def test_course_invariants_and_gold_cases_are_reported_but_do_not_replace_repeatability():
    records, block, runs = _evaluation_fixture()
    first_review = block["rows"][0]["review_id"]

    def invariant(_packet, _merged, decision):
        return ["course invariant failed"] if decision["permission"] == "REMOVE" else []

    # Expected fields are deliberately a small, independently authored rubric.
    first_output = next(row for row in runs[0] if row["review_id"] == first_review)["output"]
    expected_decision = reduce(records[[row["review_id"] for row in block["rows"]].index(first_review)]["packet"], first_output)
    gold = {
        first_review: {
            "permission": expected_decision["permission"],
            "route": expected_decision["route"],
        }
    }
    report = module.evaluate_consistency(
        records=records,
        sample_block=block,
        runs=runs,
        protocol=protocol(strata={"unused": 4}),
        validate_fn=validate,
        reduce_fn=reduce,
        course_invariant_fn=invariant,
        gold_cases=gold,
    )
    assert report["passed"] is False
    assert report["repeatability_pass"] is True
    assert report["course_correctness_pass"] is False
    assert report["course_correctness"]["passing_cases"] == 3
    assert report["gold_case_agreement"]["exact_rate"] == 1.0


def test_all_wait_guard_fails_even_when_all_three_runs_agree():
    records = [source_record(index, "WAIT_POLICY_BOUNDARY") for index in range(4)]
    identities = [module._normalized_case(row) for row in records]
    block = module._block(identities, "PRIMARY")
    runs = [[], [], []]
    for identity in identities:
        output = semantic(identity["review_id"], permission="WAIT", route="NO_TRADE")
        for run_number in range(1, 4):
            runs[run_number - 1].append(envelope(identity, run_number, output))
    settings = protocol(
        strata={"unused": 4},
        minimum_conservative_trade_cases=0,
        minimum_trade_routes=0,
        minimum_wait_cases=1,
        minimum_remove_cases_when_eligible=0,
    )
    report = module.evaluate_consistency(
        records=records,
        sample_block=block,
        runs=runs,
        protocol=settings,
        validate_fn=validate,
        reduce_fn=reduce,
    )
    assert report["rates"]["permission_rate"] == 1.0
    assert report["checks"]["not_all_wait"] is False
    assert report["passed"] is False


def test_course_invariant_alone_cannot_create_overall_formal_pass():
    records, block, runs = _evaluation_fixture()
    report = module.evaluate_consistency(
        records=records,
        sample_block=block,
        runs=runs,
        protocol=protocol(strata={"unused": 4}),
        validate_fn=validate,
        reduce_fn=reduce,
        course_invariant_fn=lambda _packet, _semantic, _decision: [],
    )
    assert report["repeatability_pass"] is True
    assert report["course_correctness_pass"] is True
    assert report["research_calibration_pass"] is False
    assert report["blinded_course_gold_pass"] is False
    assert report["overall_formal_pass"] is False
    assert report["passed"] is False


def test_overall_formal_pass_requires_all_four_correctness_gates(monkeypatch):
    monkeypatch.setattr(module, "CONSISTENCY_STATUS", "FINAL")
    records, block, runs = _evaluation_fixture()
    report = module.evaluate_consistency(
        records=records,
        sample_block=block,
        runs=runs,
        protocol=protocol(strata={"unused": 4}),
        validate_fn=validate,
        reduce_fn=reduce,
        course_invariant_fn=lambda _packet, _semantic, _decision: [],
        research_calibration_report=research_gate_report(),
        blinded_course_gold_report=blinded_gold_report(),
    )
    assert report["repeatability_pass"] is True
    assert report["course_correctness_pass"] is True
    assert report["research_calibration_pass"] is True
    assert report["blinded_course_gold_pass"] is True
    assert report["formal_acceptance_inputs_pass"] is True
    assert report["overall_formal_pass"] is True
    assert report["correctness_layers"]["L5_FORWARD_PERFORMANCE_VALIDATION"]["passed"] is None


def test_disagreement_lowers_atom_rate_and_conservative_permission():
    record = source_record(0, "A")
    identity = module._normalized_case(record)
    block = module._block([identity], "PRIMARY")
    outputs = [
        semantic(identity["review_id"], atom="PASS"),
        semantic(identity["review_id"], atom="PASS"),
        semantic(identity["review_id"], atom="FAIL"),
    ]
    runs = [[envelope(identity, index + 1, output)] for index, output in enumerate(outputs)]
    settings = protocol(
        strata={"A": 1},
        critical_atom_rate=0.75,
        permission_rate=0,
        scenario_phase_rate=0,
        trade_action_signature_rate=0,
        minimum_conservative_trade_cases=0,
        minimum_trade_routes=0,
        minimum_wait_cases=1,
        minimum_remove_cases_when_eligible=0,
    )
    report = module.evaluate_consistency(
        records=[record],
        sample_block=block,
        runs=runs,
        protocol=settings,
        validate_fn=validate,
        reduce_fn=reduce,
    )
    assert report["rates"]["critical_atom_rate"] == 0.5
    assert report["checks"]["critical_atoms"] is False
    assert report["conservative_permission_counts"]["WAIT"] == 1


def test_formal_dependency_gate_requires_exact_version_and_final_status(tmp_path):
    policy = tmp_path / "policy.py"
    packet = tmp_path / "packet.py"
    policy.write_text(
        'POLICY_VERSION = "hybrid-v3-atomic-policy-v2"\nPOLICY_STATUS = "DRAFT"\n', encoding="utf-8"
    )
    packet.write_text(
        'BUILDER_VERSION = "hybrid-v3-atomic-packets-v2"\nBUILDER_STATUS = "FINAL"\n',
        encoding="utf-8",
    )
    with pytest.raises(FormalReadinessError, match="status must be FINAL"):
        module.assert_formal_dependencies(policy_path=policy, packet_builder_path=packet)
    policy.write_text(
        'POLICY_VERSION = "hybrid-v3-atomic-policy-v2"\nPOLICY_STATUS = "FINAL"\n', encoding="utf-8"
    )
    result = module.assert_formal_dependencies(policy_path=policy, packet_builder_path=packet)
    assert result["policy.py"]["status"] == "FINAL"


def test_formal_dependency_gate_rejects_missing_component(tmp_path):
    with pytest.raises(FormalReadinessError, match="missing"):
        module.assert_formal_dependencies(
            policy_path=tmp_path / "missing_policy.py",
            packet_builder_path=tmp_path / "missing_packet.py",
        )
