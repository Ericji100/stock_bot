from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from scripts import hybrid_v3_mature_s1_course_audit_v1 as audit


SECRET = "course-audit-rekey-secret-that-is-longer-than-32-bytes"


def _gate(
    records: list[dict] | None = None, *, status: str = "REPEATABILITY_PASS"
) -> dict:
    records = records or _records()
    core = {
        "receipt_version": audit.GATE_RECEIPT_VERSION,
        "report_version": audit.CONSISTENCY_REPORT_VERSION,
        "status": status,
        "expected_cases": 36,
        "run_count": 3,
        "course_correctness_evaluated": False,
        "performance_evaluated": False,
        "future_or_performance_visible": False,
        "report_sha256": "a" * 64,
        "track_manifest_sha256": "b" * 64,
        "execution_freeze_sha256": "c" * 64,
        "source_case_keys_sha256": audit.canonical_sha256(
            sorted(row["case_key"] for row in records)
        ),
        "source_packets_sha256": audit.canonical_sha256(
            sorted(
                (
                    {
                        "case_key": row["case_key"],
                        "packet_sha256": row["packet_sha256"],
                    }
                    for row in records
                ),
                key=lambda row: row["case_key"],
            )
        ),
    }
    return {**core, "receipt_sha256": audit.canonical_sha256(core)}


def _records() -> list[dict]:
    rows = []
    for ordinal in range(36):
        as_of = f"2023-{6 + ordinal % 6:02d}-01"
        packet = {
            "review_id": f"SOURCE-REVIEW-{ordinal:02d}",
            "anonymous_stock_id": f"SOURCE-STOCK-{ordinal:02d}",
            "as_of": as_of,
            "question_manifest": {"global_question_ids": ["Q1", "Q3"]},
            "evidence": [{"ref": f"BAR:{as_of}", "kind": "BAR", "date": as_of}],
            "objective_facts": {"causal_cutoff_as_of": as_of},
        }
        packet["question_manifest_sha256"] = audit.canonical_sha256(
            packet["question_manifest"]
        )
        packet["evidence_catalog_sha256"] = audit.canonical_sha256(packet["evidence"])
        packet["objective_facts"]["ai_visible_evidence_sha256"] = audit.canonical_sha256(
            packet["evidence"]
        )
        packet["input_packet_sha256"] = audit.canonical_sha256(packet)
        rows.append(
            {
                "case_key": f"SOURCE-CASE-{ordinal:02d}",
                "review_id": packet["review_id"],
                "anonymous_stock_id": packet["anonymous_stock_id"],
                "packet_sha256": audit.canonical_sha256(packet),
                "packet": packet,
                "model": "gpt-5.6-sol",
                "reasoning_effort": "xhigh",
            }
        )
    return rows


def _rehash_packet(record: dict) -> None:
    packet = record["packet"]
    packet["question_manifest_sha256"] = audit.canonical_sha256(
        packet["question_manifest"]
    )
    packet["evidence_catalog_sha256"] = audit.canonical_sha256(packet["evidence"])
    packet["objective_facts"]["ai_visible_evidence_sha256"] = audit.canonical_sha256(
        packet["evidence"]
    )
    core = dict(packet)
    core.pop("input_packet_sha256", None)
    packet["input_packet_sha256"] = audit.canonical_sha256(core)
    record["packet_sha256"] = audit.canonical_sha256(packet)


def _coverage_plan(records: list[dict], *, omit: str | None = None) -> dict:
    required = [tag for tag in audit.REQUIRED_COVERAGE_TAGS if tag != omit]
    assigned = [tag for tag in required for _ in range(audit.MIN_COVERAGE_CASES_PER_TAG)]
    rows = []
    for ordinal, record in enumerate(records):
        tags = [assigned[ordinal]] if ordinal < len(assigned) else []
        rows.append(
            {
                "source_case_key": record["case_key"],
                "coverage_claims": [
                    {
                        "tag": tag,
                        "as_of_evidence_refs": [record["packet"]["evidence"][0]["ref"]],
                    }
                    for tag in tags
                ],
            }
        )
    core = {
        "plan_version": audit.COVERAGE_PLAN_VERSION,
        "status": "LOCKED_OBJECTIVE_ONLY_AS_OF",
        "basis": "OBJECTIVE_ONLY_AS_OF",
        "old_ai_output_used": False,
        "common_ledger_used": False,
        "future_or_performance_used": False,
        "rows": rows,
        "rows_sha256": audit.canonical_sha256(rows),
    }
    return {**core, "plan_sha256": audit.canonical_sha256(core)}


def _course(tmp_path: Path) -> tuple[Path, list[dict]]:
    root = tmp_path / "course_knowledge_base"
    root.mkdir(parents=True)
    source = root / "lesson.md"
    section = "Mature trend pullback"
    proposition = "Use only causal structure visible on the review date."
    source.write_text(
        f"# Course reference\n{section}\n{proposition}\n", encoding="utf-8"
    )
    excerpt = f"# Course reference\n{section}\n{proposition}"
    citations = [
        {
            "citation_id": "COURSE-1",
            "source_path": "lesson.md",
            "source_sha256": audit.file_sha256(source),
            "start_line": 1,
            "end_line": 3,
            "excerpt_sha256": audit.hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
            "section": section,
            "proposition": proposition,
            "criterion_ids": list(audit.CRITERION_IDS),
        }
    ]
    return root, citations


def _bundle(
    tmp_path: Path,
    *,
    records: list[dict] | None = None,
    coverage_plan: dict | None = None,
    gate: dict | None = None,
    secret: str = SECRET,
) -> dict:
    records = records or _records()
    effective_gate = gate or _gate(records)
    effective_plan = coverage_plan or _coverage_plan(records)
    root, citations = _course(tmp_path)
    return audit.build_blind_course_audit_bundle(
        records,
        consistency_gate=effective_gate,
        trusted_gate_receipt_sha256=audit.canonical_sha256(effective_gate),
        coverage_plan=effective_plan,
        trusted_coverage_plan_sha256=audit.canonical_sha256(effective_plan),
        course_citations=citations,
        trusted_course_citations_sha256=audit.canonical_sha256(citations),
        course_root=root,
        rekey_secret=secret,
    )


def _trust(bundle: dict, course_root: Path) -> dict:
    manifest = bundle["bundle_manifest"]
    return {
        "trusted_gate_receipt_sha256": manifest["consistency_gate_receipt_sha256"],
        "trusted_course_citations_sha256": manifest["course_citations_sha256"],
        "trusted_reviewer_inputs_sha256": manifest["reviewer_inputs_sha256"],
        "trusted_coverage_plan_sha256": manifest["coverage_plan_sha256"],
        "trusted_coverage_manifest_sha256": manifest["coverage_manifest_sha256"],
        "course_root": course_root,
    }


def test_all_36_cases_are_rekeyed_and_reviewer_surface_is_blank_and_blind(
    tmp_path: Path,
) -> None:
    records = _records()
    bundle = _bundle(tmp_path, records=records)
    inputs = bundle["reviewer_inputs"]

    assert len(inputs) == len({row["audit_case_id"] for row in inputs}) == 36
    assert bundle["bundle_manifest"]["status"] == audit.READY_STATUS
    assert bundle["bundle_manifest"]["target_scenario"] == "MATURE_TREND_PULLBACK"
    assert bundle["bundle_manifest"]["target_route"] == "V2_CORE"
    assert bundle["bundle_manifest"]["performance_sealed"] is True
    assert bundle["bundle_manifest"]["course_fidelity_conclusion"] is None
    encoded = audit.canonical_json_bytes(inputs).decode("utf-8")
    assert SECRET not in encoded
    for record in records:
        assert record["case_key"] not in encoded
        assert record["review_id"] not in encoded
        assert record["anonymous_stock_id"] not in encoded
        assert record["packet_sha256"] not in encoded
    for row in inputs:
        assert set(row) == {
            "input_version",
            "audit_case_id",
            "as_of_packet",
            "course_citations",
            "blank_rubric",
            "reviewer_input_sha256",
        }
        packet = row["as_of_packet"]
        assert set(packet) == {"as_of", "question_manifest", "evidence", "objective_facts"}
        rubric = row["blank_rubric"]
        assert rubric["status"] == "DRAFT_UNREVIEWED"
        assert rubric["evaluation_layer"] == audit.INDEPENDENT_AUDIT_LAYER
        assert rubric["overall_s1_course_fit"] is None
        assert rubric["auditor_attestation"] is None
        assert [item["criterion_id"] for item in rubric["criteria"]] == list(
            audit.CRITERION_IDS
        )
        assert all(item["verdict"] is None for item in rubric["criteria"])
        assert all(not item["course_citation_ids"] for item in rubric["criteria"])
        assert all(not item["as_of_evidence_refs"] for item in rubric["criteria"])


def test_rekey_is_deterministic_for_same_secret_and_unlinkable_across_secrets(
    tmp_path: Path,
) -> None:
    records = _records()
    first = _bundle(tmp_path / "a", records=records)
    second = _bundle(tmp_path / "b", records=records)
    third = _bundle(tmp_path / "c", records=records, secret="z" * 40)
    first_ids = [row["audit_case_id"] for row in first["reviewer_inputs"]]

    assert first_ids == [row["audit_case_id"] for row in second["reviewer_inputs"]]
    assert set(first_ids).isdisjoint(
        {row["audit_case_id"] for row in third["reviewer_inputs"]}
    )


@pytest.mark.parametrize(
    ("location", "key", "value"),
    [
        ("record", "output", {"permission": "TRADE"}),
        ("record", "model_output", {"permission": "TRADE"}),
        ("record", "common_ledger", {"joined": True}),
        ("packet", "future_return", 0.2),
        ("packet", "future_return_not_viewed", 0.2),
        ("packet", "runs", [{"text": "old model output"}] * 3),
        ("packet", "ledger", [{"joined": True}]),
        ("packet", "asset", "2330.TW"),
        ("packet", "source_id", "OLD-SOURCE-ID"),
        ("packet", "company_name", "REAL COMPANY"),
    ],
)
def test_ai_ledger_future_performance_and_real_identity_contamination_is_rejected(
    tmp_path: Path, location: str, key: str, value: object
) -> None:
    records = _records()
    target = records[0] if location == "record" else records[0]["packet"]
    target[key] = value
    if location == "packet":
        _rehash_packet(records[0])

    with pytest.raises(audit.MatureS1CourseAuditError):
        _bundle(tmp_path, records=records)


def test_future_dated_evidence_is_rejected_even_without_performance_field(
    tmp_path: Path,
) -> None:
    records = _records()
    records[0]["packet"]["evidence"].append(
        {"ref": "BAR:2024-01-01", "kind": "BAR", "date": "2024-01-01"}
    )
    _rehash_packet(records[0])

    with pytest.raises(audit.MatureS1CourseAuditError, match="future date"):
        _bundle(tmp_path, records=records)


@pytest.mark.parametrize(
    "mutation",
    [
        {"status": "REPEATABILITY_FAIL"},
        {"report_version": "hybrid-v3-mature-s1-consistency-v2"},
        {"performance_evaluated": True},
        {"future_or_performance_visible": True},
    ],
)
def test_nonpass_or_unblinded_consistency_gate_is_rejected(
    tmp_path: Path, mutation: dict
) -> None:
    gate = {**_gate(), **mutation}
    core = dict(gate)
    core.pop("receipt_sha256")
    gate["receipt_sha256"] = audit.canonical_sha256(core)

    with pytest.raises(audit.MatureS1CourseAuditError, match="only after"):
        _bundle(tmp_path, gate=gate)


def test_valid_detached_gate_receipt_requires_an_external_trusted_pin(
    tmp_path: Path,
) -> None:
    records = _records()
    root, citations = _course(tmp_path)
    gate = _gate()

    with pytest.raises(audit.MatureS1CourseAuditError, match="only after"):
        audit.build_blind_course_audit_bundle(
            records,
            consistency_gate=gate,
            trusted_gate_receipt_sha256="b" * 64,
            coverage_plan=(plan := _coverage_plan(records)),
            trusted_coverage_plan_sha256=audit.canonical_sha256(plan),
            course_citations=citations,
            trusted_course_citations_sha256=audit.canonical_sha256(citations),
            course_root=root,
            rekey_secret=SECRET,
        )


def test_rehashed_coverage_plan_rejects_a_different_external_pin(tmp_path: Path) -> None:
    records = _records()
    original = _coverage_plan(records)
    replacement = copy.deepcopy(original)
    replacement["rows"] = list(reversed(replacement["rows"]))
    replacement["rows_sha256"] = audit.canonical_sha256(replacement["rows"])
    core = dict(replacement)
    core.pop("plan_sha256")
    replacement["plan_sha256"] = audit.canonical_sha256(core)
    root, citations = _course(tmp_path)
    gate = _gate(records)

    with pytest.raises(audit.MatureS1CourseAuditError, match="coverage plan"):
        audit.build_blind_course_audit_bundle(
            records,
            consistency_gate=gate,
            trusted_gate_receipt_sha256=audit.canonical_sha256(gate),
            coverage_plan=replacement,
            trusted_coverage_plan_sha256=audit.canonical_sha256(original),
            course_citations=citations,
            trusted_course_citations_sha256=audit.canonical_sha256(citations),
            course_root=root,
            rekey_secret=SECRET,
        )


def test_valid_citation_replacement_rejects_a_different_external_pin(
    tmp_path: Path,
) -> None:
    records = _records()
    root, citations = _course(tmp_path)
    original_pin = audit.canonical_sha256(citations)
    citations[0]["citation_id"] = "COURSE-RENAMED"
    plan = _coverage_plan(records)
    gate = _gate(records)

    with pytest.raises(audit.MatureS1CourseAuditError, match="external trust pin"):
        audit.build_blind_course_audit_bundle(
            records,
            consistency_gate=gate,
            trusted_gate_receipt_sha256=audit.canonical_sha256(gate),
            coverage_plan=plan,
            trusted_coverage_plan_sha256=audit.canonical_sha256(plan),
            course_citations=citations,
            trusted_course_citations_sha256=original_pin,
            course_root=root,
            rekey_secret=SECRET,
        )


def test_trusted_pass_receipt_rejects_a_substituted_36_case_set(tmp_path: Path) -> None:
    original = _records()
    trusted_gate = _gate(original)
    substituted = copy.deepcopy(original)
    substituted[0]["case_key"] = "SUBSTITUTED-CASE"

    with pytest.raises(audit.MatureS1CourseAuditError, match="another case set"):
        _bundle(
            tmp_path,
            records=substituted,
            coverage_plan=_coverage_plan(substituted),
            gate=trusted_gate,
        )


def test_missing_required_coverage_is_explicit_and_keeps_performance_sealed(
    tmp_path: Path,
) -> None:
    records = _records()
    missing = "NO_CAUSAL_DEFENSE"
    bundle = _bundle(
        tmp_path,
        records=records,
        coverage_plan=_coverage_plan(records, omit=missing),
    )
    manifest = bundle["bundle_manifest"]
    coverage = bundle["governance_coverage_manifest"]

    assert manifest["status"] == audit.INSUFFICIENT_STATUS
    assert manifest["performance_sealed"] is True
    assert manifest["performance_unseal_authorized"] is False
    assert coverage["summary"]["missing_tags"] == [missing]
    assert audit.validate_completed_audit_set(
        bundle["reviewer_inputs"],
        [],
        bundle_manifest=manifest,
        governance_coverage_manifest=coverage,
        **_trust(bundle, tmp_path / "course_knowledge_base"),
    ) == {
        "status": audit.INSUFFICIENT_STATUS,
        "validated_rubrics": 0,
        "course_fidelity_conclusion": None,
        "performance_sealed": True,
        "performance_unseal_authorized": False,
    }


def test_completed_audit_validator_cannot_rehash_insufficient_manifest_to_ready(
    tmp_path: Path,
) -> None:
    records = _records()
    bundle = _bundle(
        tmp_path,
        records=records,
        coverage_plan=_coverage_plan(records, omit="NO_CAUSAL_DEFENSE"),
    )
    forged = copy.deepcopy(bundle["bundle_manifest"])
    forged["status"] = audit.READY_STATUS
    core = dict(forged)
    core.pop("bundle_manifest_sha256")
    forged["bundle_manifest_sha256"] = audit.canonical_sha256(core)

    with pytest.raises(audit.MatureS1CourseAuditError, match="bundle manifest"):
        audit.validate_completed_audit_set(
            bundle["reviewer_inputs"],
            [],
            bundle_manifest=forged,
            governance_coverage_manifest=bundle["governance_coverage_manifest"],
            **_trust(bundle, tmp_path / "course_knowledge_base"),
        )


@pytest.mark.parametrize(
    "defect", ["hash", "unknown_tag", "bad_evidence_ref", "overloaded_case", "missing_case"]
)
def test_forged_coverage_plan_is_rejected(tmp_path: Path, defect: str) -> None:
    records = _records()
    plan = _coverage_plan(records)
    if defect == "hash":
        plan["rows_sha256"] = "0" * 64
    elif defect == "unknown_tag":
        plan["rows"][0]["coverage_claims"] = [
            {"tag": "MODEL_SAID_TRADE", "as_of_evidence_refs": ["BAR:2023-06-01"]}
        ]
        plan["rows_sha256"] = audit.canonical_sha256(plan["rows"])
        core = dict(plan)
        core.pop("plan_sha256")
        plan["plan_sha256"] = audit.canonical_sha256(core)
    elif defect == "bad_evidence_ref":
        plan["rows"][0]["coverage_claims"][0]["as_of_evidence_refs"] = [
            "BAR:2099-01-01"
        ]
        plan["rows_sha256"] = audit.canonical_sha256(plan["rows"])
        core = dict(plan)
        core.pop("plan_sha256")
        plan["plan_sha256"] = audit.canonical_sha256(core)
    elif defect == "overloaded_case":
        plan["rows"][0]["coverage_claims"] = [
            {
                "tag": tag,
                "as_of_evidence_refs": [records[0]["packet"]["evidence"][0]["ref"]],
            }
            for tag in audit.REQUIRED_COVERAGE_TAGS
        ]
        plan["rows_sha256"] = audit.canonical_sha256(plan["rows"])
        core = dict(plan)
        core.pop("plan_sha256")
        plan["plan_sha256"] = audit.canonical_sha256(core)
    else:
        plan["rows"].pop()
        plan["rows_sha256"] = audit.canonical_sha256(plan["rows"])
        core = dict(plan)
        core.pop("plan_sha256")
        plan["plan_sha256"] = audit.canonical_sha256(core)

    with pytest.raises(audit.MatureS1CourseAuditError):
        _bundle(tmp_path, records=records, coverage_plan=plan)


@pytest.mark.parametrize("defect", ["escape", "hash", "proposition", "criterion"])
def test_forged_or_incomplete_course_citation_is_rejected(
    tmp_path: Path, defect: str
) -> None:
    records = _records()
    root, citations = _course(tmp_path)
    if defect == "escape":
        outside = tmp_path / "outside.md"
        outside.write_text("not course\n", encoding="utf-8")
        citations[0]["source_path"] = "../outside.md"
        citations[0]["source_sha256"] = audit.file_sha256(outside)
    elif defect == "hash":
        citations[0]["source_sha256"] = "0" * 64
    elif defect == "proposition":
        citations[0]["proposition"] = "A proposition absent from the cited line span."
    else:
        citations[0]["criterion_ids"].remove("CAUSAL_DEFENSE")

    with pytest.raises(audit.MatureS1CourseAuditError):
        audit.build_blind_course_audit_bundle(
            records,
            consistency_gate=_gate(),
            trusted_gate_receipt_sha256=audit.canonical_sha256(_gate()),
            coverage_plan=(plan := _coverage_plan(records)),
            trusted_coverage_plan_sha256=audit.canonical_sha256(plan),
            course_citations=citations,
            trusted_course_citations_sha256=audit.canonical_sha256(citations),
            course_root=root,
            rekey_secret=SECRET,
        )


def _completed_rubric(reviewer_input: dict) -> dict:
    rubric = copy.deepcopy(reviewer_input["blank_rubric"])
    rubric["status"] = "COURSE_REVIEWED_FROZEN"
    rubric["overall_s1_course_fit"] = "SUPPORTED"
    rubric["auditor_attestation"] = {
        "reviewer_ref": "INDEPENDENT-COURSE-REVIEWER",
        "reviewer_role": "INDEPENDENT_COURSE_AUDITOR",
        "original_three_run_reviewer": False,
        "only_reviewer_input_viewed": True,
        "three_run_outputs_not_viewed": True,
        "common_ledger_not_viewed": True,
        "real_stock_identity_not_viewed": True,
        "future_or_performance_not_viewed": True,
    }
    evidence_ref = reviewer_input["as_of_packet"]["evidence"][0]["ref"]
    for criterion in rubric["criteria"]:
        criterion["verdict"] = "SUPPORTED"
        criterion["course_citation_ids"] = ["COURSE-1"]
        criterion["as_of_evidence_refs"] = [evidence_ref]
        criterion["rationale"] = "Supported by the cited course rule and as-of evidence."
    core = dict(rubric)
    core.pop("rubric_sha256")
    rubric["rubric_sha256"] = audit.canonical_sha256(core)
    return rubric


def _independent_review_receipt(bundle: dict, completed: list[dict]) -> dict:
    manifest = bundle["bundle_manifest"]
    core = {
        "receipt_version": audit.REVIEW_RECEIPT_VERSION,
        "evaluation_layer": audit.INDEPENDENT_AUDIT_LAYER,
        "reviewer_ref": "INDEPENDENT-COURSE-REVIEWER",
        "reviewer_credential_sha256": "d" * 64,
        "reviewer_inputs_sha256": manifest["reviewer_inputs_sha256"],
        "coverage_manifest_sha256": manifest["coverage_manifest_sha256"],
        "course_citations_sha256": manifest["course_citations_sha256"],
        "completed_rubrics_sha256": audit.canonical_sha256(
            sorted(completed, key=lambda row: row["audit_case_id"])
        ),
        "ai_self_review_used": False,
        "original_three_run_reviewer": False,
        "only_reviewer_inputs_viewed": True,
        "future_or_performance_viewed": False,
    }
    return {**core, "receipt_sha256": audit.canonical_sha256(core)}


def test_completed_audit_records_can_be_validated_without_publishing_a_conclusion(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path)
    completed = [_completed_rubric(row) for row in bundle["reviewer_inputs"]]
    receipt = _independent_review_receipt(bundle, completed)

    report = audit.validate_completed_audit_set(
        bundle["reviewer_inputs"],
        completed,
        bundle_manifest=bundle["bundle_manifest"],
        governance_coverage_manifest=bundle["governance_coverage_manifest"],
        independent_review_receipt=receipt,
        trusted_independent_review_receipt_sha256=audit.canonical_sha256(receipt),
        **_trust(bundle, tmp_path / "course_knowledge_base"),
    )

    assert report["validated_rubrics"] == 36
    assert report["course_fidelity_conclusion"] is None
    assert report["independent_review_provenance_verified"] is True
    assert report["performance_sealed"] is True
    assert report["performance_unseal_authorized"] is False


def test_completed_audit_cannot_claim_independence_from_self_attestation_alone(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path)
    completed = [_completed_rubric(row) for row in bundle["reviewer_inputs"]]

    with pytest.raises(audit.MatureS1CourseAuditError, match="externally pinned"):
        audit.validate_completed_audit_set(
            bundle["reviewer_inputs"],
            completed,
            bundle_manifest=bundle["bundle_manifest"],
            governance_coverage_manifest=bundle["governance_coverage_manifest"],
            **_trust(bundle, tmp_path / "course_knowledge_base"),
        )


@pytest.mark.parametrize(
    "defect", ["missing_evidence", "bad_attestation", "overall_conflict", "hash"]
)
def test_completed_rubric_fails_closed_on_blindness_or_evidence_defect(
    tmp_path: Path, defect: str
) -> None:
    bundle = _bundle(tmp_path)
    reviewer_input = bundle["reviewer_inputs"][0]
    rubric = _completed_rubric(reviewer_input)
    if defect == "missing_evidence":
        rubric["criteria"][0]["as_of_evidence_refs"] = []
    elif defect == "bad_attestation":
        rubric["auditor_attestation"]["three_run_outputs_not_viewed"] = False
    elif defect == "overall_conflict":
        rubric["overall_s1_course_fit"] = "NOT_SUPPORTED"
    else:
        rubric["rubric_sha256"] = "0" * 64
    if defect != "hash":
        core = dict(rubric)
        core.pop("rubric_sha256")
        rubric["rubric_sha256"] = audit.canonical_sha256(core)

    with pytest.raises(audit.MatureS1CourseAuditError):
        audit.validate_completed_rubric(reviewer_input, rubric)


def test_writer_is_immutable_and_publishes_no_source_identity_or_secret(
    tmp_path: Path,
) -> None:
    records = _records()
    bundle = _bundle(tmp_path / "fixture", records=records)
    output = tmp_path / "published"
    trust = _trust(bundle, tmp_path / "fixture" / "course_knowledge_base")
    result = audit.write_bundle(bundle, output, **trust)
    published = b"".join(path.read_bytes() for path in output.iterdir() if path.is_file())

    assert result["status"] == audit.READY_STATUS
    assert SECRET.encode() not in published
    assert all(record["case_key"].encode() not in published for record in records)
    assert all(record["review_id"].encode() not in published for record in records)
    assert all(record["anonymous_stock_id"].encode() not in published for record in records)
    with pytest.raises(audit.MatureS1CourseAuditError, match="overwrite"):
        audit.write_bundle(bundle, output, **trust)


@pytest.mark.parametrize("injection", ["company_name", "nested_issuer"])
def test_writer_revalidates_bundle_and_rejects_recomputed_identity_injection(
    tmp_path: Path, injection: str
) -> None:
    bundle = _bundle(tmp_path / "fixture")
    forged = copy.deepcopy(bundle)
    reviewer_input = forged["reviewer_inputs"][0]
    if injection == "company_name":
        reviewer_input["as_of_packet"]["company_name"] = "REAL COMPANY"
    else:
        reviewer_input["as_of_packet"]["objective_facts"]["issuer"] = "2330.TW"
    rubric = reviewer_input["blank_rubric"]
    rubric["as_of_packet_sha256"] = audit.canonical_sha256(
        reviewer_input["as_of_packet"]
    )
    rubric_core = dict(rubric)
    rubric_core.pop("rubric_sha256")
    rubric["rubric_sha256"] = audit.canonical_sha256(rubric_core)
    input_core = dict(reviewer_input)
    input_core.pop("reviewer_input_sha256")
    reviewer_input["reviewer_input_sha256"] = audit.canonical_sha256(input_core)
    manifest = forged["bundle_manifest"]
    manifest["reviewer_inputs_sha256"] = audit.canonical_sha256(
        forged["reviewer_inputs"]
    )
    manifest_core = dict(manifest)
    manifest_core.pop("bundle_manifest_sha256")
    manifest["bundle_manifest_sha256"] = audit.canonical_sha256(manifest_core)

    with pytest.raises(audit.MatureS1CourseAuditError):
        audit.write_bundle(
            forged,
            tmp_path / "forged-output",
            **_trust(bundle, tmp_path / "fixture" / "course_knowledge_base"),
        )


def test_writer_recomputes_coverage_instead_of_trusting_rehashed_pass_flag(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path / "fixture")
    forged = copy.deepcopy(bundle)
    coverage = forged["governance_coverage_manifest"]
    missing_tag = "NO_CAUSAL_DEFENSE"
    for row in coverage["rows"]:
        row["coverage_claims"] = [
            claim for claim in row["coverage_claims"] if claim["tag"] != missing_tag
        ]
    coverage["rows_sha256"] = audit.canonical_sha256(coverage["rows"])
    coverage_core = dict(coverage)
    coverage_core.pop("coverage_manifest_sha256")
    coverage["coverage_manifest_sha256"] = audit.canonical_sha256(coverage_core)
    manifest = forged["bundle_manifest"]
    manifest["coverage_manifest_sha256"] = coverage["coverage_manifest_sha256"]
    manifest_core = dict(manifest)
    manifest_core.pop("bundle_manifest_sha256")
    manifest["bundle_manifest_sha256"] = audit.canonical_sha256(manifest_core)

    with pytest.raises(audit.MatureS1CourseAuditError, match="recomputed"):
        audit.write_bundle(
            forged,
            tmp_path / "forged-output",
            **_trust(bundle, tmp_path / "fixture" / "course_knowledge_base"),
        )


def test_writer_rejects_reordered_and_rehashed_coverage_without_new_external_pin(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path / "fixture")
    forged = copy.deepcopy(bundle)
    coverage = forged["governance_coverage_manifest"]
    coverage["rows"] = list(reversed(coverage["rows"]))
    coverage["rows_sha256"] = audit.canonical_sha256(coverage["rows"])
    coverage_core = dict(coverage)
    coverage_core.pop("coverage_manifest_sha256")
    coverage["coverage_manifest_sha256"] = audit.canonical_sha256(coverage_core)
    manifest = forged["bundle_manifest"]
    manifest["coverage_manifest_sha256"] = coverage["coverage_manifest_sha256"]
    manifest_core = dict(manifest)
    manifest_core.pop("bundle_manifest_sha256")
    manifest["bundle_manifest_sha256"] = audit.canonical_sha256(manifest_core)

    with pytest.raises(audit.MatureS1CourseAuditError, match="bundle manifest"):
        audit.write_bundle(
            forged,
            tmp_path / "forged-output",
            **_trust(bundle, tmp_path / "fixture" / "course_knowledge_base"),
        )


def test_writer_rejects_extra_manifest_payload_even_when_self_hash_is_recomputed(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path / "fixture")
    forged = copy.deepcopy(bundle)
    manifest = forged["bundle_manifest"]
    manifest["ai_output"] = {"permission": "TRADE"}
    manifest_core = dict(manifest)
    manifest_core.pop("bundle_manifest_sha256")
    manifest["bundle_manifest_sha256"] = audit.canonical_sha256(manifest_core)

    with pytest.raises(audit.MatureS1CourseAuditError, match="bundle manifest"):
        audit.write_bundle(
            forged,
            tmp_path / "forged-output",
            **_trust(bundle, tmp_path / "fixture" / "course_knowledge_base"),
        )


def test_cli_first_phase_emits_candidate_pins_without_publishing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    records = _records()
    gate = _gate(records)
    plan = _coverage_plan(records)
    course_root, citations = _course(tmp_path)
    cases_path = tmp_path / "cases.jsonl"
    gate_path = tmp_path / "gate.json"
    plan_path = tmp_path / "plan.json"
    citations_path = tmp_path / "citations.json"
    secret_path = tmp_path / "secret.bin"
    output = tmp_path / "must-not-exist"
    cases_path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in records),
        encoding="utf-8",
    )
    gate_path.write_text(json.dumps(gate), encoding="utf-8")
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    citations_path.write_text(json.dumps({"citations": citations}), encoding="utf-8")
    secret_path.write_text(SECRET, encoding="utf-8")

    code = audit.main(
        [
            "--source-case",
            str(cases_path),
            "--consistency-gate",
            str(gate_path),
            "--trusted-gate-receipt-sha256",
            audit.canonical_sha256(gate),
            "--coverage-plan",
            str(plan_path),
            "--trusted-coverage-plan-sha256",
            audit.canonical_sha256(plan),
            "--course-citations",
            str(citations_path),
            "--trusted-course-citations-sha256",
            audit.canonical_sha256(citations),
            "--course-root",
            str(course_root),
            "--rekey-secret-file",
            str(secret_path),
            "--output-dir",
            str(output),
        ]
    )
    emitted = json.loads(capsys.readouterr().out)

    assert code == 3
    assert emitted["status"] == "PIN_REQUIRED_BEFORE_PUBLISH"
    assert emitted["course_audit_executed"] is False
    assert emitted["performance_sealed"] is True
    assert not output.exists()
