from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.hybrid_v3_freeze_v2 import (
    DEFAULT_COMPONENTS,
    FREEZE_STATUS,
    FreezeInputs,
    FreezeValidationError,
    ImmutableFreezeError,
    PIVOT_DEFINITION,
    file_sha256,
    freeze,
    main,
    pre_freeze_readiness_audit,
    validate_freeze,
)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write_json(path: Path, value: dict) -> None:
    write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def with_self_hash(core: dict, field: str) -> dict:
    import hashlib

    result = dict(core)
    result[field] = hashlib.sha256(
        json.dumps(core, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return result


def final_markdown(version: str) -> str:
    return f"# fixture\n\n版本：`{version}`  \n狀態：`FINAL`\n"


def protocol_payload(*, stocks: int = 1029, stock_days: int = 151804, review_points: int = 3) -> dict:
    return {
        "protocol_version": "hybrid-monitoring-protocol-v2",
        "status": "FINAL",
        "monitoring_window": {
            "expected_stocks": stocks,
            "expected_stock_days": stock_days,
            "expected_v2_review_points": review_points,
            "expected_v2_review_points_policy": "SET_FROM_OUTCOME_BLIND_DAILY_BUILDER_BEFORE_FORMAL_AI",
        },
        "data": {
            "pivot_definition": PIVOT_DEFINITION,
            "course_l1_l2_status": "DISABLED_EXPERIMENTAL_ONLY",
            "same_day_new_pivot_control_break": "FORBIDDEN",
        },
        "execution": {
            "model": "gpt-test-final",
            "reasoning_effort": "xhigh",
            "case_atomic": True,
            "consistency_run_isolation": True,
            "technical_max_attempts": 3,
            "valid_semantic_response_may_be_redrawn": False,
            "full_shard_count": 3,
            "merge_order": "SOURCE_ORDINAL",
        },
        "consistency": {
            "sample_size": 6,
            "runs": 3,
            "seed": "DISJOINT_HOLDOUT",
            "exclude_v1_review_ids": True,
            "prefer_stock_disjoint_from_v1": True,
            "schema_causality_evidence_rate": 1.0,
            "permission_rate": 0.95,
            "scenario_phase_rate": 0.9,
            "critical_atom_rate": 0.95,
            "trade_action_signature_rate": 1.0,
            "minimum_conservative_trade_cases": 1,
            "minimum_trade_routes": 1,
            "minimum_wait_cases": 1,
            "minimum_remove_cases_when_eligible": 1,
            "strata": {"TRADE_PROXY": 2, "WAIT": 2, "REMOVE": 2},
        },
        "correctness_gate": {
            "deterministic_course_invariant": {
                "required": True,
                "required_pass_rate": 1.0,
            },
            "repeatability_alone_is_sufficient": False,
            "overall_formal_pass_requires": [
                "THREE_RUN_REPEATABILITY",
                "DETERMINISTIC_COURSE_INVARIANT",
                "RESEARCH_CALIBRATION",
                "BLINDED_COURSE_GOLD_HOLDOUT",
            ],
            "research_calibration": {
                "required": True,
                "classification": "RESEARCH_CALIBRATION_NOT_PERFORMANCE_HOLDOUT",
                "separated_from_future_performance": True,
                "minimum_cases": 20,
                "required_balance": {
                    "positive_or_constructive": 10,
                    "negative_or_ambiguous": 10,
                },
                "human_rubric_must_be_frozen_before_ai": True,
                "incomplete_or_unreviewed_case_result": "NOT_APPLICABLE_FAIL_CLOSED",
                "required_pass_rate": 1.0,
            },
            "blinded_course_gold_holdout": {
                "required": True,
                "classification": "BLINDED_HISTORICAL_COURSE_GOLD_NOT_PERFORMANCE_HOLDOUT",
                "minimum_cases": 20,
                "minimum_distinct_objective_strata": 4,
                "fixed_sampling_before_human_labels": True,
                "human_labels_frozen_before_ai": True,
                "identity_future_old_ai_hidden_during_annotation": True,
                "required_atomic_assertion_agreement_rate": 0.9,
                "required_material_permission_agreement_rate": 1.0,
                "separated_from_future_performance": True,
            },
            "future_performance": {
                "read_before_consistency_ledger_locked": False,
                "read_before_full_semantic_ledger_locked": False,
                "part_of_repeatability": False,
            },
        },
        "performance_lock": {
            "future_prices_hidden_until_ledger_sha256": True,
            "identity_hidden_until_ledger_sha256": True,
            "old_pure_ai_reference_hidden_until_ledger_sha256": True,
            "full_v3_requires_consistency_pass": True,
        },
    }


def build_fixture(
    root: Path,
    *,
    source_stocks: int = 1029,
    source_stock_days: int = 151804,
    review_locked: bool = True,
) -> FreezeInputs:
    strategy_files = (
        "docs/enlightenment-ai-judgement-v1.md",
        "docs/enlightenment-ai-judgement-v2.md",
        "docs/enlightenment-ai-judgement-v3.md",
        "config/enlightenment_ai_rules_v2.json",
        "config/enlightenment_ai_rules_v3.json",
    )
    for relative in strategy_files:
        write(root / relative, f"protected:{relative}\n")

    source = root / "run/packet_manifest.json"
    write_json(source, {"stocks": source_stocks, "stock_days": source_stock_days})

    v1_protocol = root / "run/hybrid_monitoring_v1/protocol_freeze_manifest.json"
    protocol_manifest = {
        "status": "LOCKED_BEFORE_TEST",
        "files": [
            {"relative_path": relative, "sha256": file_sha256(root / relative)}
            for relative in strategy_files
        ],
    }
    write_json(v1_protocol, protocol_manifest)

    v1_exec_file = root / "scripts/frozen_v1_executor.py"
    write(v1_exec_file, "V1 = True\n")
    v1_execution = root / "run/hybrid_monitoring_v1/execution_freeze_manifest.json"
    write_json(
        v1_execution,
        {
            "status": "FROZEN_BEFORE_TEST",
            "files": [
                {"relative_path": "scripts/frozen_v1_executor.py", "sha256": file_sha256(v1_exec_file)}
            ],
            "artifacts": [
                {"absolute_path": str(v1_protocol.resolve()), "sha256": file_sha256(v1_protocol)},
                {"absolute_path": str(source.resolve()), "sha256": file_sha256(source)},
            ],
        },
    )

    write(root / "docs/hybrid-monitoring-protocol-v2.md", final_markdown("hybrid-monitoring-protocol-v2"))
    write_json(root / "config/hybrid_monitoring_protocol_v2.json", protocol_payload())
    write_json(
        root / "config/hybrid_atomic_semantics_v2.schema.json",
        {
            "x-schema-version": "hybrid-atomic-semantics-v2",
            "x-contract-status": "FINAL",
            "x-question-groups": {
                "global": ["DIRECTION_SUPPORTED", "STOP_BELONGS_TO_CAMPAIGN"],
                "candidate": ["ANCHOR_CONTROLS_CONTEXT"],
            },
            "x-critical-question-ids": [
                "DIRECTION_SUPPORTED",
                "STOP_BELONGS_TO_CAMPAIGN",
            ],
        },
    )
    write(root / "config/hybrid_semantic_prompt_v2.md", final_markdown("hybrid-semantic-prompt-v2"))

    python_metadata = {
        "scripts/hybrid_v3_objective_state_v2.py": (
            "ENGINE_VERSION",
            "hybrid-v3-objective-state-v2",
            "ENGINE_STATUS",
        ),
        "scripts/hybrid_v3_atomic_policy_v2.py": (
            "POLICY_VERSION",
            "hybrid-v3-atomic-policy-v2",
            "POLICY_STATUS",
        ),
        "scripts/hybrid_v3_atomic_packets_v2.py": (
            "BUILDER_VERSION",
            "hybrid-v3-atomic-packets-v2",
            "BUILDER_STATUS",
        ),
        "scripts/hybrid_v3_atomic_runner_v2.py": (
            "RUNNER_VERSION",
            "hybrid-v3-atomic-runner-v2",
            "RUNNER_STATUS",
        ),
        "scripts/hybrid_v3_sharding_v2.py": (
            "SHARDING_VERSION",
            "hybrid-v3-sharding-v2",
            "SHARDING_STATUS",
        ),
        "scripts/hybrid_v3_consistency_v2.py": (
            "CONSISTENCY_VERSION",
            "hybrid-v3-consistency-v2",
            "CONSISTENCY_STATUS",
        ),
        "scripts/hybrid_v3_codex_reviewer_v2.py": (
            "ADAPTER_VERSION",
            "hybrid-v3-codex-reviewer-v2",
            "ADAPTER_STATUS",
        ),
        "scripts/hybrid_v2_research_calibration.py": (
            "CALIBRATION_VERSION",
            "hybrid-v2-research-calibration-v1",
            "TOOL_STATUS",
        ),
        "scripts/hybrid_v3_course_gold_v2.py": (
            "COURSE_GOLD_VERSION",
            "hybrid-v3-course-gold-v2",
            "COURSE_GOLD_STATUS",
        ),
    }
    for relative, (version_key, version, status_key) in python_metadata.items():
        extra = 'FORMAL_SOURCE_MANIFEST_FILE = "review_point_manifest.json"\n' if relative.endswith("atomic_packets_v2.py") else ""
        write(root / relative, f'{version_key} = "{version}"\n{status_key} = "FINAL"\n{extra}')

    review_artifact = root / "run/hybrid_monitoring_v2/review_points.jsonl"
    write(review_artifact, "".join(json.dumps({"source_ordinal": value}) + "\n" for value in range(3)))
    review_manifest = root / "run/hybrid_monitoring_v2/review_point_manifest.json"
    write_json(
        review_manifest,
        {
            "manifest_version": "hybrid-v3-review-points-v2",
            "status": "LOCKED_OUTCOME_BLIND" if review_locked else "DRAFT",
            "outcome_blind": True,
            "identity_visible": False,
            "performance_visible": False,
            "future_data_visible": False,
            "source_order_locked": True,
            "outcome_excluded_codes": [],
            "stocks": source_stocks,
            "stock_days_scanned": source_stock_days,
            "review_points": 3,
            "source_manifest": {"path": str(source.resolve()), "sha256": file_sha256(source)},
            "review_points_artifact": {
                "path": str(review_artifact.resolve()),
                "sha256": file_sha256(review_artifact),
                "rows": 3,
            },
        },
    )
    research_manifest = root / "run/hybrid_monitoring_v2/research_calibration/human_frozen_manifest.json"
    write_json(
        research_manifest,
        with_self_hash(
            {
                "manifest_version": "hybrid-v2-research-calibration-freeze-v1",
                "status": "HUMAN_FROZEN",
                "case_count": 20,
                "human_frozen_rubric_count": 20,
                "rubrics_frozen_before_ai": True,
            },
            "manifest_sha256",
        ),
    )
    course_bundle = root / "run/hybrid_monitoring_v2/course_gold/bundle_manifest.json"
    write_json(
        course_bundle,
        with_self_hash(
            {
                "manifest_version": "hybrid-v3-course-gold-bundle-v2",
                "status": "DRAFT_UNLABELED",
                "gold_labels_present": False,
            },
            "bundle_manifest_sha256",
        ),
    )
    course_rubrics = root / "run/hybrid_monitoring_v2/course_gold/human_frozen_rubrics_manifest.json"
    write_json(
        course_rubrics,
        with_self_hash(
            {
                "manifest_version": "hybrid-v3-course-gold-rubrics-v2",
                "status": "HUMAN_REVIEWED_FROZEN",
                "case_count": 20,
                "human_frozen_rubric_count": 20,
                "rubrics_frozen_before_ai": True,
                "bundle_manifest_sha256": file_sha256(course_bundle),
            },
            "manifest_sha256",
        ),
    )
    consistency_sample = root / "run/hybrid_monitoring_v2/consistency/holdout_plan.json"
    write_json(
        consistency_sample,
        with_self_hash(
            {
                "holdout_plan_version": "hybrid-v3-holdout-plan-v2",
                "status": "LOCKED_OUTCOME_BLIND_SAMPLE_PLAN",
                "sample_size": 6,
                "primary": {"block_id": "PRIMARY", "count": 6},
                "reserve_blocks": [{"block_id": "RESERVE_1", "count": 6}],
            },
            "plan_sha256",
        ),
    )
    execution_manifest = root / "run/hybrid_monitoring_v2/execution_manifest.json"
    execution_core = {
        "manifest_version": "hybrid-v3-execution-manifest-v2",
        "status": "LOCKED_BEFORE_AI",
        "model": "gpt-test-final",
        "reasoning_effort": "xhigh",
        "review_point_manifest_sha256": file_sha256(review_manifest),
        "research_calibration_manifest_sha256": file_sha256(research_manifest),
        "course_gold_bundle_manifest_sha256": file_sha256(course_bundle),
        "course_gold_rubrics_manifest_sha256": file_sha256(course_rubrics),
        "consistency_sample_manifest_sha256": file_sha256(consistency_sample),
        "protocol_sha256": file_sha256(root / "config/hybrid_monitoring_protocol_v2.json"),
        "schema_sha256": file_sha256(root / "config/hybrid_atomic_semantics_v2.schema.json"),
        "prompt_sha256": file_sha256(root / "config/hybrid_semantic_prompt_v2.md"),
        "policy_sha256": file_sha256(root / "scripts/hybrid_v3_atomic_policy_v2.py"),
        "runner_sha256": file_sha256(root / "scripts/hybrid_v3_atomic_runner_v2.py"),
        "sharding_sha256": file_sha256(root / "scripts/hybrid_v3_sharding_v2.py"),
        "reviewer_sha256": file_sha256(root / "scripts/hybrid_v3_codex_reviewer_v2.py"),
    }
    write_json(execution_manifest, with_self_hash(execution_core, "manifest_sha256"))
    return FreezeInputs(
        root=root,
        v1_protocol_manifest=v1_protocol,
        v1_execution_manifest=v1_execution,
        source_manifest=source,
        v2_review_point_manifest=review_manifest,
        output=root / "run/hybrid_monitoring_v2/freeze_manifest.json",
        expected_v1_protocol_manifest_sha256=file_sha256(v1_protocol),
        expected_v1_execution_manifest_sha256=file_sha256(v1_execution),
        components=DEFAULT_COMPONENTS,
        research_calibration_manifest=research_manifest,
        course_gold_bundle_manifest=course_bundle,
        course_gold_rubrics_manifest=course_rubrics,
        consistency_sample_manifest=consistency_sample,
        execution_manifest=execution_manifest,
    )


def test_complete_final_fixture_freezes_once_and_is_idempotent(tmp_path: Path) -> None:
    inputs = build_fixture(tmp_path)
    payload = freeze(inputs)
    first_bytes = inputs.output.read_bytes()
    repeated = freeze(inputs)
    assert inputs.output.read_bytes() == first_bytes
    assert payload == repeated
    assert payload["status"] == FREEZE_STATUS
    assert payload["strategy_rules_unchanged"] is True
    assert len(payload["v2_components"]) == 13
    assert payload["review_points"]["review_points"] == 3
    assert set(payload["pre_ai_hash_chain"]) == {
        "research_calibration", "course_gold_bundle", "course_gold_rubrics",
        "consistency_sample", "execution",
    }
    assert payload["execution_contract"]["critical_questions"]["count"] == 2
    assert payload["execution_contract"]["correctness_gate"][
        "deterministic_course_invariant_required_pass_rate"
    ] == 1.0
    assert set(
        payload["execution_contract"]["correctness_gate"]["overall_formal_pass_requires"]
    ) == {
        "THREE_RUN_REPEATABILITY",
        "DETERMINISTIC_COURSE_INVARIANT",
        "RESEARCH_CALIBRATION",
        "BLINDED_COURSE_GOLD_HOLDOUT",
    }


def test_validate_does_not_write_manifest(tmp_path: Path) -> None:
    inputs = build_fixture(tmp_path)
    payload = validate_freeze(inputs)
    assert payload["status"] == FREEZE_STATUS
    assert not inputs.output.exists()


def test_complete_fixture_pre_freeze_readiness_audit_is_ready(tmp_path: Path) -> None:
    audit = pre_freeze_readiness_audit(build_fixture(tmp_path))
    assert audit["ready"] is True
    assert audit["unresolved"] == []
    assert audit["checks"]["components"]["expected"] == 13
    assert audit["checks"]["components"]["metadata_parseable"] == 13
    assert audit["checks"]["pre_ai_hash_chain"]["ready"] is True
    lifecycle = audit["checks"]["lifecycle"]
    assert lifecycle["circular_dependency"] is False
    assert lifecycle["topological_order"].index("LOCKED_REVIEW_POINT_UNIVERSE") < lifecycle[
        "topological_order"
    ].index("PROTOCOL_COUNT_LOCK_AND_COMPONENT_FINALIZATION")
    assert lifecycle["topological_order"].index("FORMAL_MASTER_FREEZE") < lifecycle[
        "topological_order"
    ].index("AI_EXECUTION")
    assert lifecycle["review_point_builder_depends_on_protocol_frozen_count"] is False
    assert lifecycle["master_freeze_depends_on_ai_outputs"] is False


def test_pre_ai_hash_chain_is_mandatory_and_execution_links_are_exact(tmp_path: Path) -> None:
    inputs = build_fixture(tmp_path)
    assert inputs.research_calibration_manifest is not None
    inputs.research_calibration_manifest.unlink()
    with pytest.raises(FreezeValidationError, match="hash chain artifact is missing"):
        validate_freeze(inputs)

    inputs = build_fixture(tmp_path / "links")
    assert inputs.execution_manifest is not None
    execution = json.loads(inputs.execution_manifest.read_text(encoding="utf-8"))
    execution.pop("manifest_sha256")
    execution["review_point_manifest_sha256"] = "0" * 64
    write_json(inputs.execution_manifest, with_self_hash(execution, "manifest_sha256"))
    with pytest.raises(FreezeValidationError, match="does not pin review_point_manifest_sha256"):
        validate_freeze(inputs)


def test_actual_repo_validate_only_reports_all_expected_pre_freeze_blockers(capsys) -> None:
    root = Path(__file__).resolve().parents[1]
    audit = pre_freeze_readiness_audit(__import__(
        "scripts.hybrid_v3_freeze_v2", fromlist=["default_inputs"]
    ).default_inputs(root))
    assert audit["ready"] is False
    assert audit["checks"]["components"]["expected"] == 13
    assert audit["checks"]["components"]["inspected"] == 13
    assert audit["checks"]["source_manifest"]["scope_ready"] is True
    codes = {row["code"] for row in audit["unresolved"]}
    assert {
        "COMPONENT_STATUS_NOT_FINAL",
        "EXPECTED_V2_REVIEW_POINTS_UNSET",
        "PRE_AI_HASH_CHAIN_ARTIFACT_MISSING",
        "RESEARCH_CALIBRATION_CASE_CAPACITY_BELOW_PROTOCOL",
    } <= codes
    if not audit["checks"]["review_points"]["exists"]:
        assert "REVIEW_POINT_MANIFEST_MISSING" in codes
    else:
        assert audit["checks"]["review_points"]["status"] == "LOCKED_OUTCOME_BLIND"
    assert audit["checks"]["research_calibration_source"]["declared_cases"] == 8
    assert audit["checks"]["research_calibration_source"]["minimum_total"] == 20
    existing = audit["checks"]["existing_research_calibration_artifact"]
    assert existing["status"] == "DRAFT/INCOMPLETE_RESEARCH_CALIBRATION"
    assert existing["case_count"] == 8
    assert existing["human_frozen_rubric_count"] == 0
    assert existing["immutable_self_hash_valid"] is True
    assert existing["eligible_as_human_frozen_manifest"] is False
    assert audit["checks"]["lifecycle"]["circular_dependency"] is False
    component_status = {
        row["name"]: row["status"] for row in audit["checks"]["components"]["items"]
    }
    assert component_status["consistency"] == "FINAL"
    assert component_status["course_gold"] == "FINAL"
    assert component_status["research_calibration"] == "FINAL"
    assert all(row["priority"] == "P0" for row in audit["unresolved"])
    assert main(["--root", str(root), "--validate-only"]) == 2
    emitted = json.loads(capsys.readouterr().out)
    assert emitted["status"] == "BLOCKED_PRE_FREEZE"
    assert emitted["unresolved_count"] == len(emitted["unresolved"])


@pytest.mark.parametrize("component", ["policy", "packet", "consistency"])
def test_missing_required_v2_component_is_never_waived(tmp_path: Path, component: str) -> None:
    inputs = build_fixture(tmp_path)
    spec = next(row for row in DEFAULT_COMPONENTS if row.name == component)
    (tmp_path / spec.relative_path).unlink()
    with pytest.raises(FreezeValidationError, match="component is missing"):
        freeze(inputs)
    assert not inputs.output.exists()


def test_draft_component_is_rejected(tmp_path: Path) -> None:
    inputs = build_fixture(tmp_path)
    write(tmp_path / "docs/hybrid-monitoring-protocol-v2.md", final_markdown("hybrid-monitoring-protocol-v2").replace("FINAL", "DRAFT"))
    with pytest.raises(FreezeValidationError, match="status must be FINAL"):
        freeze(inputs)


def test_wrong_v2_component_version_is_rejected(tmp_path: Path) -> None:
    inputs = build_fixture(tmp_path)
    write_json(
        tmp_path / "config/hybrid_atomic_semantics_v2.schema.json",
        {"x-schema-version": "hybrid-atomic-semantics-v2-draft", "x-contract-status": "FINAL"},
    )
    with pytest.raises(FreezeValidationError, match="version is not final"):
        freeze(inputs)


@pytest.mark.parametrize("critical", [None, [], ["NOT_IN_QUESTION_GROUPS"]])
def test_schema_requires_nonempty_known_critical_question_ids(
    tmp_path: Path, critical
) -> None:
    inputs = build_fixture(tmp_path)
    path = tmp_path / "config/hybrid_atomic_semantics_v2.schema.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    if critical is None:
        value.pop("x-critical-question-ids")
    else:
        value["x-critical-question-ids"] = critical
    write_json(path, value)
    with pytest.raises(FreezeValidationError, match="critical question ids|x-critical-question-ids"):
        freeze(inputs)
    assert not inputs.output.exists()


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (
            lambda gate: gate["deterministic_course_invariant"].update(required=False),
            "course invariant must be required",
        ),
        (
            lambda gate: gate["deterministic_course_invariant"].update(required_pass_rate=0.99),
            "required_pass_rate must be 1.0",
        ),
        (
            lambda gate: gate.update(repeatability_alone_is_sufficient=True),
            "cannot be based on repeatability alone",
        ),
        (
            lambda gate: gate.update(overall_formal_pass_requires=["THREE_RUN_REPEATABILITY"]),
            "overall formal pass must require",
        ),
        (
            lambda gate: gate["overall_formal_pass_requires"].append("FUTURE_PERFORMANCE"),
            "overall formal pass must require exactly",
        ),
    ],
)
def test_formal_correctness_requires_repeatability_and_perfect_course_invariants(
    tmp_path: Path, mutator, message: str
) -> None:
    inputs = build_fixture(tmp_path)
    path = tmp_path / "config/hybrid_monitoring_protocol_v2.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    mutator(value["correctness_gate"])
    write_json(path, value)
    with pytest.raises(FreezeValidationError, match=message):
        freeze(inputs)
    assert not inputs.output.exists()


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (
            lambda gate: gate["research_calibration"].update(required=False),
            "research calibration must be required",
        ),
        (
            lambda gate: gate["research_calibration"].update(minimum_cases=19),
            "at least 20 course cases",
        ),
        (
            lambda gate: gate["research_calibration"].update(required_pass_rate=0.875),
            "required_pass_rate must be 1.0",
        ),
        (
            lambda gate: gate["research_calibration"].update(
                classification="PERFORMANCE_HOLDOUT"
            ),
            "must not be classified as a performance holdout",
        ),
        (
            lambda gate: gate["research_calibration"].update(
                separated_from_future_performance=False
            ),
            "research calibration and future performance must be separated",
        ),
        (
            lambda gate: gate["research_calibration"]["required_balance"].update(
                positive_or_constructive=9
            ),
            "at least 10 constructive and 10 negative/ambiguous cases",
        ),
        (
            lambda gate: gate["research_calibration"].update(
                human_rubric_must_be_frozen_before_ai=False
            ),
            "human rubric must be frozen before AI",
        ),
        (
            lambda gate: gate["research_calibration"].update(
                incomplete_or_unreviewed_case_result="PASS"
            ),
            "incomplete research calibration cases must fail closed",
        ),
        (
            lambda gate: gate["future_performance"].update(
                read_before_consistency_ledger_locked=True
            ),
            "must stay hidden",
        ),
        (
            lambda gate: gate["future_performance"].update(
                read_before_full_semantic_ledger_locked=True
            ),
            "full semantic ledger",
        ),
        (
            lambda gate: gate["future_performance"].update(part_of_repeatability=True),
            "cannot be part of repeatability",
        ),
    ],
)
def test_research_calibration_and_future_performance_are_strictly_separated(
    tmp_path: Path, mutator, message: str
) -> None:
    inputs = build_fixture(tmp_path)
    path = tmp_path / "config/hybrid_monitoring_protocol_v2.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    mutator(value["correctness_gate"])
    write_json(path, value)
    with pytest.raises(FreezeValidationError, match=message):
        freeze(inputs)
    assert not inputs.output.exists()


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (
            lambda gold: gold.update(required=False),
            "gold holdout must be required",
        ),
        (
            lambda gold: gold.update(minimum_cases=19),
            "at least 20 cases",
        ),
        (
            lambda gold: gold.update(minimum_distinct_objective_strata=3),
            "at least 4 objective strata",
        ),
        (
            lambda gold: gold.update(human_labels_frozen_before_ai=False),
            "human labels must be frozen before AI",
        ),
        (
            lambda gold: gold.update(identity_future_old_ai_hidden_during_annotation=False),
            "must hide identity, future data, and old AI",
        ),
        (
            lambda gold: gold.update(required_atomic_assertion_agreement_rate=0.89),
            "atomic assertion agreement must be at least 0.90",
        ),
        (
            lambda gold: gold.update(required_material_permission_agreement_rate=0.95),
            "material permission agreement must be 1.0",
        ),
    ],
)
def test_blinded_course_gold_is_required_and_outcome_blind(
    tmp_path: Path, mutator, message: str
) -> None:
    inputs = build_fixture(tmp_path)
    path = tmp_path / "config/hybrid_monitoring_protocol_v2.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    mutator(value["correctness_gate"]["blinded_course_gold_holdout"])
    write_json(path, value)
    with pytest.raises(FreezeValidationError, match=message):
        freeze(inputs)
    assert not inputs.output.exists()


def test_any_protected_strategy_mutation_is_rejected(tmp_path: Path) -> None:
    inputs = build_fixture(tmp_path)
    write(tmp_path / "docs/enlightenment-ai-judgement-v2.md", "mutated\n")
    with pytest.raises(FreezeValidationError, match="protected entry hash changed"):
        freeze(inputs)


def test_v1_manifest_itself_is_pinned(tmp_path: Path) -> None:
    inputs = build_fixture(tmp_path)
    value = json.loads(inputs.v1_protocol_manifest.read_text(encoding="utf-8"))
    value["unexpected"] = True
    write_json(inputs.v1_protocol_manifest, value)
    with pytest.raises(FreezeValidationError, match="manifest hash changed"):
        freeze(inputs)


def test_wrong_source_scope_is_rejected_even_when_fixture_is_self_consistent(tmp_path: Path) -> None:
    inputs = build_fixture(tmp_path, source_stocks=1028)
    with pytest.raises(FreezeValidationError, match="1,029 stocks / 151,804"):
        freeze(inputs)


def test_review_points_must_be_outcome_blind_and_locked(tmp_path: Path) -> None:
    inputs = build_fixture(tmp_path, review_locked=False)
    with pytest.raises(FreezeValidationError, match="LOCKED_OUTCOME_BLIND"):
        freeze(inputs)


def test_review_artifact_hash_and_rows_are_locked(tmp_path: Path) -> None:
    inputs = build_fixture(tmp_path)
    artifact = tmp_path / "run/hybrid_monitoring_v2/review_points.jsonl"
    write(artifact, artifact.read_text(encoding="utf-8") + json.dumps({"source_ordinal": 3}) + "\n")
    with pytest.raises(FreezeValidationError, match="artifact hash changed"):
        freeze(inputs)


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (lambda value: value["execution"].update(model=""), "model and reasoning_effort"),
        (lambda value: value["execution"].update(full_shard_count=0), "full_shard_count"),
        (lambda value: value["execution"].update(merge_order="COMPLETION_ORDER"), "SOURCE_ORDINAL"),
        (lambda value: value["consistency"]["strata"].update(WAIT=3), "differs from sample_size"),
        (lambda value: value["consistency"].update(exclude_v1_review_ids=False), "exclude V1 review ids"),
    ],
)
def test_incomplete_model_holdout_or_shard_settings_are_rejected(
    tmp_path: Path, mutator, message: str
) -> None:
    inputs = build_fixture(tmp_path)
    path = tmp_path / "config/hybrid_monitoring_protocol_v2.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    mutator(value)
    write_json(path, value)
    with pytest.raises(FreezeValidationError, match=message):
        freeze(inputs)


def test_existing_different_freeze_manifest_is_never_overwritten(tmp_path: Path) -> None:
    inputs = build_fixture(tmp_path)
    write_json(inputs.output, {"different": True})
    before = inputs.output.read_bytes()
    with pytest.raises(ImmutableFreezeError, match="already exists"):
        freeze(inputs)
    assert inputs.output.read_bytes() == before
