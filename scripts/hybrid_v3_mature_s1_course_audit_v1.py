"""Prepare and validate a blinded MATURE S1 course-fidelity audit.

This tool is intentionally separated from repeatability outputs.  It accepts
only frozen as-of case packets, an objective-only coverage plan, immutable
course citations, and a small PASS gate attestation.  It never reads a run
root, successful attempt, common ledger, stock identity map, or performance
artifact, and it does not call a reviewer or publish a course conclusion.
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from datetime import date
import hashlib
import hmac
import json
from pathlib import Path
import shutil
import tempfile
from typing import Any, Iterable, Mapping, Sequence


TOOL_VERSION = "hybrid-v3-mature-s1-course-audit-v1"
INPUT_VERSION = "hybrid-v3-mature-s1-course-audit-input-v1"
RUBRIC_VERSION = "hybrid-v3-mature-s1-course-audit-rubric-v1"
COVERAGE_PLAN_VERSION = "hybrid-v3-mature-s1-course-audit-coverage-v1"
COVERAGE_MANIFEST_VERSION = "hybrid-v3-mature-s1-course-audit-coverage-manifest-v1"
BUNDLE_MANIFEST_VERSION = "hybrid-v3-mature-s1-course-audit-bundle-v1"
CONSISTENCY_REPORT_VERSION = "hybrid-v3-mature-s1-consistency-v3-integrity-complete"
GATE_RECEIPT_VERSION = "hybrid-v3-mature-s1-consistency-pass-receipt-v1"
REVIEW_RECEIPT_VERSION = "hybrid-v3-mature-s1-independent-review-receipt-v1"
EXPECTED_CASES = 36
EXPECTED_RUNS = 3
TARGET_SCENARIO = "MATURE_TREND_PULLBACK"
TARGET_ROUTE = "V2_CORE"
INDEPENDENT_AUDIT_LAYER = "INDEPENDENT_BLINDED_COURSE_FIDELITY_AUDIT"
OBJECTIVE_VERIFICATION_SCOPE = "SCHEMA_HASH_AS_OF_AND_COVERAGE_ONLY"

READY_STATUS = "READY_FOR_BLINDED_COURSE_AUDIT"
INSUFFICIENT_STATUS = "INSUFFICIENT_COURSE_AUDIT_COVERAGE"

REQUIRED_COVERAGE_TAGS = (
    "Q3_PULLBACK_POSITION",
    "EXHAUSTION_OR_TERMINAL_ADVANCE",
    "DIRECTION_CONFLICT",
    "NO_CAUSAL_DEFENSE",
    "COMPETING_MACRO_COPY",
    "COMPETING_FRESH_Q1",
    "COMPETING_BEAR_REVERSAL",
)
MIN_COVERAGE_CASES_PER_TAG = 2
MAX_COVERAGE_TAGS_PER_CASE = 2

AUDIT_CRITERIA = (
    (
        "MATURE_BULL_TREND_VALID",
        "As of the review date, is the mature bullish trend still valid?",
    ),
    (
        "WITHIN_DYNASTY_CORRECTION",
        "Is the move a correction inside the same bullish dynasty rather than a regime change?",
    ),
    (
        "PULLBACK_POSITION",
        "Is the pullback at a course-valid structural position, including Q3 risk where relevant?",
    ),
    (
        "TAIJI_GENERATION",
        "Does the Taiji generation and copy/correction relationship support this setup?",
    ),
    (
        "LARGE_SMALL_QUADRANTS",
        "Do the large and small quadrant relationships support, rather than contradict, the setup?",
    ),
    (
        "DOW_STRUCTURE",
        "Does the causal Dow structure support the mature-trend pullback interpretation?",
    ),
    (
        "AS_OF_STRENGTHENING",
        "Was renewed strength actually provable on the review date without future bars?",
    ),
    (
        "CAUSAL_DEFENSE",
        "Was a valid causal defense/stop boundary available on the review date?",
    ),
    (
        "NOT_TERMINAL_ADVANCE",
        "Is exhaustion or a terminal advance sufficiently excluded?",
    ),
    (
        "EXCLUDES_MACRO_COPY",
        "Is the macro-copy competing scenario excluded?",
    ),
    (
        "EXCLUDES_FRESH_Q1",
        "Is the fresh-Q1 competing scenario excluded?",
    ),
    (
        "EXCLUDES_BEAR_REVERSAL",
        "Is the bear-reversal competing scenario excluded?",
    ),
)
CRITERION_IDS = tuple(row[0] for row in AUDIT_CRITERIA)

_CONSISTENCY_GATE_FIELDS = frozenset(
    {
        "report_version",
        "receipt_version",
        "status",
        "expected_cases",
        "run_count",
        "course_correctness_evaluated",
        "performance_evaluated",
        "future_or_performance_visible",
        "report_sha256",
        "track_manifest_sha256",
        "execution_freeze_sha256",
        "source_case_keys_sha256",
        "source_packets_sha256",
        "receipt_sha256",
    }
)
_CITATION_FIELDS = frozenset(
    {
        "citation_id",
        "source_path",
        "source_sha256",
        "start_line",
        "end_line",
        "excerpt_sha256",
        "section",
        "proposition",
        "criterion_ids",
    }
)
_SOURCE_REQUIRED_FIELDS = frozenset(
    {"case_key", "review_id", "anonymous_stock_id", "packet_sha256", "packet"}
)
_SOURCE_PACKET_FIELDS = frozenset(
    {
        "review_id",
        "anonymous_stock_id",
        "as_of",
        "question_manifest",
        "evidence",
        "objective_facts",
        "question_manifest_sha256",
        "evidence_catalog_sha256",
        "input_packet_sha256",
    }
)
_IDENTITY_OR_LINKAGE_KEYS = frozenset(
    {
        "review_id",
        "anonymous_stock_id",
        "stock_id",
        "source_ordinal",
        "case_key",
        "run_case_key",
        "packet_sha256",
        "input_packet_sha256",
        "identity_map",
        "private_code",
        "stock_code",
        "symbol",
        "ticker",
        "company",
        "company_name",
    }
)
_EXECUTION_OR_OUTCOME_KEYS = frozenset(
    {
        "output",
        "reviewer_output",
        "ai_output",
        "model_output",
        "old_ai_output",
        "prior_ai_output",
        "atomic_semantics",
        "policy_decision",
        "decision",
        "run_decisions",
        "reviewer_audit",
        "successful_attempt",
        "attempt",
        "common_ledger",
        "merged_ledger",
        "performance",
        "pnl",
        "profit",
        "mfe",
        "mae",
        "future_return",
        "forward_return",
        "return_after",
        "realized_return",
        "unrealized_return",
        "outcome",
        "exit_date",
        "exit_price",
        "future_open",
        "future_high",
        "future_low",
        "future_close",
    }
)
_DATE_KEYS = frozenset(
    {
        "date",
        "as_of",
        "start",
        "end",
        "low_date",
        "high_date",
        "source_date",
        "available_on",
        "confirmed_on",
        "confirmation_date",
        "breached_on",
        "origin_pivot_source_date",
        "parent_available_on",
        "correction_available_on",
        "current_available_on",
        "causal_cutoff_as_of",
    }
)


class MatureS1CourseAuditError(ValueError):
    """An input is contaminated, incomplete, mutable, or incorrectly bound."""


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def canonical_jsonl_bytes(rows: Iterable[Mapping[str, Any]]) -> bytes:
    return b"".join(canonical_json_bytes(dict(row)) + b"\n" for row in rows)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _is_sha256(value: Any) -> bool:
    text = str(value or "")
    return len(text) == 64 and all(char in "0123456789abcdef" for char in text)


def _self_hashed(value: Mapping[str, Any], field: str) -> bool:
    core = dict(value)
    supplied = core.pop(field, None)
    return supplied == canonical_sha256(core)


def _is_identity_or_linkage_key(key: Any) -> bool:
    normalized = str(key).lower()
    return normalized in _IDENTITY_OR_LINKAGE_KEYS or normalized.endswith(
        ("_stock_id", "_stock_code", "_company_name", "_ticker", "_symbol")
    )


def _walk(value: Any, *, path: str = "$"):
    if isinstance(value, Mapping):
        for key, nested in value.items():
            child = f"{path}/{key}"
            yield str(key), nested, child
            yield from _walk(nested, path=child)
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            yield from _walk(nested, path=f"{path}/{index}")


def _assert_no_execution_or_outcome(value: Any) -> None:
    for key, _nested, path in _walk(value):
        normalized = key.lower()
        if (
            normalized in _EXECUTION_OR_OUTCOME_KEYS
            or normalized in {
                "future",
                "forward",
                "ledger",
                "runs",
                "outputs",
                "identity",
                "asset",
                "security",
                "instrument",
                "source_id",
                "legacy_id",
                "legacy_source",
                "issuer",
                "responses",
                "consensus",
                "projection",
                "returns",
            }
            or any(
                token in normalized
                for token in (
                    "future_",
                    "forward_",
                    "old_ai_",
                    "prior_ai_",
                    "run_output",
                    "common_ledger",
                    "merged_ledger",
                )
            )
            or normalized.endswith(("_pnl", "_performance", "_outcome"))
        ):
            raise MatureS1CourseAuditError(
                f"AI output, ledger, future, or performance field is forbidden: {path}"
            )


def _scrub_packet(packet: Mapping[str, Any]) -> dict[str, Any]:
    """Remove source join keys while rejecting, rather than hiding, contamination."""

    _assert_no_execution_or_outcome(packet)
    for key, _nested, path in _walk(packet):
        normalized = key.lower()
        if (
            _is_identity_or_linkage_key(normalized)
            and not normalized.endswith("_sha256")
            and path not in {"$/review_id", "$/anonymous_stock_id"}
        ):
            raise MatureS1CourseAuditError(f"unexpected identity/linkage in source packet: {path}")

    def scrub(value: Any) -> Any:
        if isinstance(value, Mapping):
            result: dict[str, Any] = {}
            for key, nested in value.items():
                normalized = str(key).lower()
                if _is_identity_or_linkage_key(normalized) or normalized.endswith("_sha256"):
                    continue
                result[str(key)] = scrub(nested)
            return result
        if isinstance(value, list):
            return [scrub(item) for item in value]
        return deepcopy(value)

    visible = scrub(packet)
    as_of = str(visible.get("as_of") or "")
    try:
        cutoff = date.fromisoformat(as_of)
    except ValueError as exc:
        raise MatureS1CourseAuditError("as-of packet needs a canonical ISO as_of date") from exc
    if cutoff.isoformat() != as_of:
        raise MatureS1CourseAuditError("as-of packet has a non-canonical as_of date")
    for key, nested, path in _walk(visible):
        normalized = key.lower()
        if _is_identity_or_linkage_key(normalized) or normalized.endswith("_sha256"):
            raise MatureS1CourseAuditError(f"source identity/linkage survived rekey: {path}")
        temporal_key = (
            normalized in _DATE_KEYS
            or normalized in {"time", "timestamp", "datetime"}
            or normalized.endswith(("_date", "_time", "_timestamp", "_at"))
        )
        if temporal_key and nested is not None:
            text = str(nested)
            try:
                observed = date.fromisoformat(text[:10])
            except ValueError as exc:
                raise MatureS1CourseAuditError(f"invalid causal date at {path}") from exc
            if observed > cutoff:
                raise MatureS1CourseAuditError(f"future date at {path}: {text} > {as_of}")
    return visible


def validate_consistency_gate(
    gate: Mapping[str, Any], *, trusted_receipt_sha256: str
) -> dict[str, Any]:
    value = dict(gate)
    if set(value) != _CONSISTENCY_GATE_FIELDS:
        raise MatureS1CourseAuditError("consistency gate fields are not exact")
    trusted = str(trusted_receipt_sha256 or "").lower()
    if (
        value.get("receipt_version") != GATE_RECEIPT_VERSION
        or not _self_hashed(value, "receipt_sha256")
        or not _is_sha256(trusted)
        or canonical_sha256(value) != trusted
        or value.get("report_version") != CONSISTENCY_REPORT_VERSION
        or value.get("status") != "REPEATABILITY_PASS"
        or value.get("expected_cases") != EXPECTED_CASES
        or value.get("run_count") != EXPECTED_RUNS
        or value.get("course_correctness_evaluated") is not False
        or value.get("performance_evaluated") is not False
        or value.get("future_or_performance_visible") is not False
        or not _is_sha256(value.get("report_sha256"))
        or not _is_sha256(value.get("track_manifest_sha256"))
        or not _is_sha256(value.get("execution_freeze_sha256"))
        or not _is_sha256(value.get("source_case_keys_sha256"))
        or not _is_sha256(value.get("source_packets_sha256"))
    ):
        raise MatureS1CourseAuditError(
            "course audit may be prepared only after the exact blinded consistency PASS"
        )
    return value


def validate_course_citations(
    citations: Sequence[Mapping[str, Any]], *, course_root: Path
) -> list[dict[str, Any]]:
    root = Path(course_root).resolve()
    if not root.is_dir() or not citations:
        raise MatureS1CourseAuditError("course citations and course root are required")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    covered: set[str] = set()
    for row in citations:
        if set(row) != _CITATION_FIELDS:
            raise MatureS1CourseAuditError("course citation fields are not exact")
        citation_id = str(row.get("citation_id") or "")
        if not citation_id or citation_id in seen:
            raise MatureS1CourseAuditError("course citation IDs are empty or duplicated")
        relative = Path(str(row.get("source_path") or ""))
        source = (root / relative).resolve()
        try:
            source.relative_to(root)
        except ValueError as exc:
            raise MatureS1CourseAuditError("course citation escaped course root") from exc
        if (
            relative.is_absolute()
            or not source.is_file()
            or file_sha256(source) != row.get("source_sha256")
        ):
            raise MatureS1CourseAuditError("course citation source/hash changed")
        try:
            lines = source.read_text(encoding="utf-8-sig").splitlines()
            start_line = int(row.get("start_line"))
            end_line = int(row.get("end_line"))
        except (OSError, TypeError, ValueError) as exc:
            raise MatureS1CourseAuditError("course citation locator is invalid") from exc
        if start_line < 1 or end_line < start_line or end_line > len(lines):
            raise MatureS1CourseAuditError("course citation line span is outside the source")
        excerpt = "\n".join(lines[start_line - 1 : end_line])
        if (
            hashlib.sha256(excerpt.encode("utf-8")).hexdigest()
            != row.get("excerpt_sha256")
            or str(row.get("section") or "") not in excerpt
            or str(row.get("proposition") or "") not in excerpt
        ):
            raise MatureS1CourseAuditError(
                "course citation proposition/section is not bound to its line span"
            )
        criterion_ids = row.get("criterion_ids")
        if (
            not isinstance(criterion_ids, list)
            or not criterion_ids
            or len(criterion_ids) != len(set(criterion_ids))
            or not set(criterion_ids).issubset(CRITERION_IDS)
            or not str(row.get("section") or "").strip()
            or not str(row.get("proposition") or "").strip()
        ):
            raise MatureS1CourseAuditError("course citation scope/content is incomplete")
        seen.add(citation_id)
        covered.update(str(item) for item in criterion_ids)
        normalized.append(
            {
                "citation_id": citation_id,
                "source_path": relative.as_posix(),
                "source_sha256": str(row["source_sha256"]),
                "start_line": start_line,
                "end_line": end_line,
                "excerpt_sha256": str(row["excerpt_sha256"]),
                "section": str(row["section"]),
                "proposition": str(row["proposition"]),
                "criterion_ids": list(criterion_ids),
            }
        )
    if covered != set(CRITERION_IDS):
        raise MatureS1CourseAuditError("course citations do not cover every S1 criterion")
    return normalized


def _normalize_records(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if len(records) != EXPECTED_CASES:
        raise MatureS1CourseAuditError("course audit requires exactly 36 frozen cases")
    normalized: list[dict[str, Any]] = []
    case_keys: set[str] = set()
    review_ids: set[str] = set()
    stock_ids: set[str] = set()
    for source in records:
        if not _SOURCE_REQUIRED_FIELDS.issubset(source):
            raise MatureS1CourseAuditError("source case lacks frozen identity/packet fields")
        _assert_no_execution_or_outcome(source)
        case_key = str(source.get("case_key") or "")
        review_id = str(source.get("review_id") or "")
        stock_id = str(source.get("anonymous_stock_id") or "")
        packet = source.get("packet")
        if (
            not case_key
            or not review_id
            or not stock_id
            or not isinstance(packet, Mapping)
            or case_key in case_keys
            or review_id in review_ids
            or stock_id in stock_ids
        ):
            raise MatureS1CourseAuditError("source cases are empty, duplicated, or malformed")
        if set(packet) != _SOURCE_PACKET_FIELDS:
            raise MatureS1CourseAuditError("source as-of packet fields are not exact")
        objective = packet.get("objective_facts")
        packet_core = dict(packet)
        supplied_packet_hash = packet_core.pop("input_packet_sha256", None)
        if (
            packet.get("review_id") != review_id
            or packet.get("anonymous_stock_id") != stock_id
            or packet.get("question_manifest_sha256")
            != canonical_sha256(packet.get("question_manifest"))
            or packet.get("evidence_catalog_sha256")
            != canonical_sha256(packet.get("evidence"))
            or not isinstance(objective, Mapping)
            or objective.get("ai_visible_evidence_sha256")
            != canonical_sha256(packet.get("evidence"))
            or supplied_packet_hash != canonical_sha256(packet_core)
            or canonical_sha256(packet) != source.get("packet_sha256")
        ):
            raise MatureS1CourseAuditError("source packet identity/hash changed")
        case_keys.add(case_key)
        review_ids.add(review_id)
        stock_ids.add(stock_id)
        normalized.append(
            {
                "case_key": case_key,
                "review_id": review_id,
                "anonymous_stock_id": stock_id,
                "packet_sha256": str(source["packet_sha256"]),
                "packet": deepcopy(dict(packet)),
            }
        )
    return normalized


def _validate_coverage_plan(
    plan: Mapping[str, Any],
    *,
    records_by_case: Mapping[str, Mapping[str, Any]],
    trusted_plan_sha256: str,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    value = dict(plan)
    if (
        not _is_sha256(trusted_plan_sha256)
        or canonical_sha256(value) != trusted_plan_sha256
        or value.get("plan_version") != COVERAGE_PLAN_VERSION
        or value.get("status") != "LOCKED_OBJECTIVE_ONLY_AS_OF"
        or value.get("old_ai_output_used") is not False
        or value.get("common_ledger_used") is not False
        or value.get("future_or_performance_used") is not False
        or value.get("basis") != "OBJECTIVE_ONLY_AS_OF"
        or not _self_hashed(value, "plan_sha256")
    ):
        raise MatureS1CourseAuditError("coverage plan is not frozen objective-only as-of")
    rows = value.get("rows")
    if (
        not isinstance(rows, list)
        or len(rows) != EXPECTED_CASES
        or value.get("rows_sha256") != canonical_sha256(rows)
    ):
        raise MatureS1CourseAuditError("coverage plan row/hash coverage is not exact")
    by_case: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != {"source_case_key", "coverage_claims"}:
            raise MatureS1CourseAuditError("coverage row fields are not exact")
        case_key = str(row.get("source_case_key") or "")
        claims = row.get("coverage_claims")
        if (
            case_key in by_case
            or case_key not in records_by_case
            or not isinstance(claims, list)
            or len(claims) > MAX_COVERAGE_TAGS_PER_CASE
        ):
            raise MatureS1CourseAuditError("coverage plan case/tags are invalid")
        allowed_refs = _evidence_refs(_scrub_packet(records_by_case[case_key]["packet"]))
        normalized_claims: list[dict[str, Any]] = []
        seen_tags: set[str] = set()
        for claim in claims:
            if not isinstance(claim, Mapping) or set(claim) != {"tag", "as_of_evidence_refs"}:
                raise MatureS1CourseAuditError("coverage claim fields are not exact")
            tag = str(claim.get("tag") or "")
            refs = claim.get("as_of_evidence_refs")
            if (
                tag not in REQUIRED_COVERAGE_TAGS
                or tag in seen_tags
                or not isinstance(refs, list)
                or not refs
                or len(refs) != len(set(refs))
                or not set(refs).issubset(allowed_refs)
            ):
                raise MatureS1CourseAuditError("coverage claim is not bound to as-of evidence")
            seen_tags.add(tag)
            normalized_claims.append({"tag": tag, "as_of_evidence_refs": list(refs)})
        by_case[case_key] = normalized_claims
    if set(by_case) != set(records_by_case):
        raise MatureS1CourseAuditError("coverage plan does not bind all and only 36 source cases")
    counts = Counter(claim["tag"] for claims in by_case.values() for claim in claims)
    missing = [
        tag for tag in REQUIRED_COVERAGE_TAGS if counts[tag] < MIN_COVERAGE_CASES_PER_TAG
    ]
    summary = {
        "status": INSUFFICIENT_STATUS if missing else "PASS",
        "required_tags": list(REQUIRED_COVERAGE_TAGS),
        "minimum_per_tag": MIN_COVERAGE_CASES_PER_TAG,
        "counts": {tag: counts[tag] for tag in REQUIRED_COVERAGE_TAGS},
        "missing_tags": missing,
    }
    return by_case, summary


def _audit_case_id(secret: bytes, record: Mapping[str, Any]) -> str:
    material = "|".join(
        (
            TOOL_VERSION,
            str(record["case_key"]),
            str(record["review_id"]),
            str(record["packet_sha256"]),
        )
    ).encode("utf-8")
    return "S1A-" + hmac.new(secret, material, hashlib.sha256).hexdigest()[:32]


def _blank_rubric(
    *, audit_case_id: str, packet_sha256: str, citations_sha256: str
) -> dict[str, Any]:
    core = {
        "rubric_version": RUBRIC_VERSION,
        "status": "DRAFT_UNREVIEWED",
        "evaluation_layer": INDEPENDENT_AUDIT_LAYER,
        "audit_case_id": audit_case_id,
        "as_of_packet_sha256": packet_sha256,
        "course_citations_sha256": citations_sha256,
        "criteria": [
            {
                "criterion_id": criterion_id,
                "question": question,
                "verdict": None,
                "course_citation_ids": [],
                "as_of_evidence_refs": [],
                "rationale": None,
            }
            for criterion_id, question in AUDIT_CRITERIA
        ],
        "overall_s1_course_fit": None,
        "auditor_attestation": None,
    }
    return {**core, "rubric_sha256": canonical_sha256(core)}


def validate_reviewer_input(value: Mapping[str, Any]) -> dict[str, Any]:
    expected_fields = {
        "input_version",
        "audit_case_id",
        "as_of_packet",
        "course_citations",
        "blank_rubric",
        "reviewer_input_sha256",
    }
    result = deepcopy(dict(value))
    if set(result) != expected_fields or not _self_hashed(result, "reviewer_input_sha256"):
        raise MatureS1CourseAuditError("reviewer input fields/hash are not exact")
    if result.get("input_version") != INPUT_VERSION:
        raise MatureS1CourseAuditError("reviewer input version changed")
    audit_case_id = str(result.get("audit_case_id") or "")
    if not audit_case_id.startswith("S1A-"):
        raise MatureS1CourseAuditError("reviewer input is not S1 rekeyed")
    packet = result.get("as_of_packet")
    citations = result.get("course_citations")
    rubric = result.get("blank_rubric")
    if not isinstance(packet, Mapping) or not isinstance(citations, list) or not isinstance(rubric, Mapping):
        raise MatureS1CourseAuditError("reviewer input sections are malformed")
    if set(packet) != {"as_of", "question_manifest", "evidence", "objective_facts"}:
        raise MatureS1CourseAuditError("reviewer as-of packet fields are not exact")
    _assert_no_execution_or_outcome(result)
    for key, _nested, path in _walk(packet):
        normalized = key.lower()
        if _is_identity_or_linkage_key(normalized) or normalized.endswith("_sha256"):
            raise MatureS1CourseAuditError(f"reviewer packet contains source linkage: {path}")
    if (
        rubric.get("status") != "DRAFT_UNREVIEWED"
        or rubric.get("evaluation_layer") != INDEPENDENT_AUDIT_LAYER
        or rubric.get("audit_case_id") != audit_case_id
        or rubric.get("as_of_packet_sha256") != canonical_sha256(packet)
        or rubric.get("course_citations_sha256") != canonical_sha256(citations)
        or not _self_hashed(rubric, "rubric_sha256")
        or rubric.get("overall_s1_course_fit") is not None
        or rubric.get("auditor_attestation") is not None
    ):
        raise MatureS1CourseAuditError("blank rubric is prefilled or incorrectly bound")
    criteria = rubric.get("criteria")
    if not isinstance(criteria, list) or [row.get("criterion_id") for row in criteria] != list(
        CRITERION_IDS
    ):
        raise MatureS1CourseAuditError("blank rubric criteria are incomplete or reordered")
    for row in criteria:
        if (
            row.get("verdict") is not None
            or row.get("course_citation_ids") != []
            or row.get("as_of_evidence_refs") != []
            or row.get("rationale") is not None
        ):
            raise MatureS1CourseAuditError("rubric must be completely blank before review")
    return result


def build_blind_course_audit_bundle(
    records: Sequence[Mapping[str, Any]],
    *,
    consistency_gate: Mapping[str, Any],
    trusted_gate_receipt_sha256: str,
    coverage_plan: Mapping[str, Any],
    trusted_coverage_plan_sha256: str,
    course_citations: Sequence[Mapping[str, Any]],
    trusted_course_citations_sha256: str,
    course_root: Path,
    rekey_secret: str | bytes,
) -> dict[str, Any]:
    """Build 36 reviewer inputs without consulting any repeatability output."""

    gate = validate_consistency_gate(
        consistency_gate, trusted_receipt_sha256=trusted_gate_receipt_sha256
    )
    normalized = _normalize_records(records)
    case_key_commitment = canonical_sha256(sorted(row["case_key"] for row in normalized))
    packet_commitment = canonical_sha256(
        sorted(
            (
                {"case_key": row["case_key"], "packet_sha256": row["packet_sha256"]}
                for row in normalized
            ),
            key=lambda row: row["case_key"],
        )
    )
    if (
        gate.get("source_case_keys_sha256") != case_key_commitment
        or gate.get("source_packets_sha256") != packet_commitment
    ):
        raise MatureS1CourseAuditError("detached PASS receipt is bound to another case set")
    citations = validate_course_citations(course_citations, course_root=course_root)
    if (
        not _is_sha256(trusted_course_citations_sha256)
        or canonical_sha256(citations) != trusted_course_citations_sha256
    ):
        raise MatureS1CourseAuditError("course citations differ from the external trust pin")
    by_case, coverage = _validate_coverage_plan(
        coverage_plan,
        records_by_case={row["case_key"]: row for row in normalized},
        trusted_plan_sha256=trusted_coverage_plan_sha256,
    )
    secret = rekey_secret.encode("utf-8") if isinstance(rekey_secret, str) else bytes(rekey_secret)
    if len(secret) < 32:
        raise MatureS1CourseAuditError("rekey secret must contain at least 32 bytes")
    citations_sha = canonical_sha256(citations)
    reviewer_inputs: list[dict[str, Any]] = []
    governance_rows: list[dict[str, Any]] = []
    source_identifiers: set[str] = set()
    audit_ids: set[str] = set()
    for record in normalized:
        source_identifiers.update(
            {
                record["case_key"],
                record["review_id"],
                record["anonymous_stock_id"],
                record["packet_sha256"],
            }
        )
        audit_id = _audit_case_id(secret, record)
        if audit_id in audit_ids or audit_id in source_identifiers:
            raise MatureS1CourseAuditError("S1 audit rekey collision")
        audit_ids.add(audit_id)
        packet = _scrub_packet(record["packet"])
        rubric = _blank_rubric(
            audit_case_id=audit_id,
            packet_sha256=canonical_sha256(packet),
            citations_sha256=citations_sha,
        )
        core = {
            "input_version": INPUT_VERSION,
            "audit_case_id": audit_id,
            "as_of_packet": packet,
            "course_citations": deepcopy(citations),
            "blank_rubric": rubric,
        }
        reviewer_input = {**core, "reviewer_input_sha256": canonical_sha256(core)}
        reviewer_inputs.append(validate_reviewer_input(reviewer_input))
        governance_rows.append(
            {
                "audit_case_id": audit_id,
                "coverage_claims": deepcopy(by_case[record["case_key"]]),
            }
        )
    serialized = canonical_json_bytes(reviewer_inputs)
    for source_identifier in source_identifiers:
        if source_identifier and source_identifier.encode("utf-8") in serialized:
            raise MatureS1CourseAuditError("source identifier leaked into reviewer input")
    if secret in serialized:
        raise MatureS1CourseAuditError("rekey secret leaked into reviewer input")

    coverage_core = {
        "manifest_version": COVERAGE_MANIFEST_VERSION,
        "status": coverage["status"],
        "reviewer_visible": False,
        "basis": "OBJECTIVE_ONLY_AS_OF",
        "performance_sealed": True,
        "performance_unseal_authorized": False,
        "rows": governance_rows,
        "rows_sha256": canonical_sha256(governance_rows),
        "summary": coverage,
    }
    coverage_manifest = {
        **coverage_core,
        "coverage_manifest_sha256": canonical_sha256(coverage_core),
    }
    bundle_status = INSUFFICIENT_STATUS if coverage["missing_tags"] else READY_STATUS
    manifest_core = {
        "manifest_version": BUNDLE_MANIFEST_VERSION,
        "status": bundle_status,
        "tool_version": TOOL_VERSION,
        "target_scenario": TARGET_SCENARIO,
        "target_route": TARGET_ROUTE,
        "case_count": len(reviewer_inputs),
        "all_cases_rekeyed": True,
        "source_identifiers_present_in_reviewer_inputs": False,
        "three_run_outputs_present": False,
        "common_ledger_present": False,
        "real_stock_identity_present": False,
        "future_or_performance_present": False,
        "rubrics_blank": True,
        "course_audit_executed": False,
        "ai_self_review_used": False,
        "independent_course_audit_executed": False,
        "objective_rule_verification_scope": OBJECTIVE_VERIFICATION_SCOPE,
        "course_fidelity_conclusion": None,
        "performance_sealed": True,
        "performance_unseal_authorized": False,
        "consistency_report_sha256": gate["report_sha256"],
        "consistency_gate_receipt_sha256": trusted_gate_receipt_sha256,
        "course_citations_sha256": citations_sha,
        "reviewer_inputs_sha256": canonical_sha256(reviewer_inputs),
        "coverage_manifest_sha256": coverage_manifest["coverage_manifest_sha256"],
        "coverage_plan_sha256": trusted_coverage_plan_sha256,
    }
    bundle_manifest = {
        **manifest_core,
        "bundle_manifest_sha256": canonical_sha256(manifest_core),
    }
    return {
        "reviewer_inputs": reviewer_inputs,
        "governance_coverage_manifest": coverage_manifest,
        "bundle_manifest": bundle_manifest,
    }


def _evidence_refs(packet: Mapping[str, Any]) -> set[str]:
    refs: set[str] = set()
    for key, nested, _path in _walk(packet):
        if isinstance(nested, str) and (key.lower() == "ref" or key.lower().endswith("_ref")):
            refs.add(nested)
    return refs


def validate_completed_rubric(
    reviewer_input: Mapping[str, Any], rubric: Mapping[str, Any]
) -> dict[str, Any]:
    """Validate a future course review without aggregating a PASS/FAIL conclusion."""

    source = validate_reviewer_input(reviewer_input)
    value = deepcopy(dict(rubric))
    template = source["blank_rubric"]
    expected_fields = set(template)
    if set(value) != expected_fields or not _self_hashed(value, "rubric_sha256"):
        raise MatureS1CourseAuditError("completed rubric fields/hash are not exact")
    if (
        value.get("rubric_version") != RUBRIC_VERSION
        or value.get("status") != "COURSE_REVIEWED_FROZEN"
        or value.get("evaluation_layer") != INDEPENDENT_AUDIT_LAYER
        or value.get("audit_case_id") != source["audit_case_id"]
        or value.get("as_of_packet_sha256") != canonical_sha256(source["as_of_packet"])
        or value.get("course_citations_sha256") != canonical_sha256(source["course_citations"])
        or value.get("overall_s1_course_fit")
        not in {"SUPPORTED", "NOT_SUPPORTED", "INDETERMINATE"}
    ):
        raise MatureS1CourseAuditError("completed rubric is not frozen/bound/complete")
    attestation = value.get("auditor_attestation")
    required_attestations = {
        "reviewer_role": "INDEPENDENT_COURSE_AUDITOR",
        "original_three_run_reviewer": False,
        "only_reviewer_input_viewed": True,
        "three_run_outputs_not_viewed": True,
        "common_ledger_not_viewed": True,
        "real_stock_identity_not_viewed": True,
        "future_or_performance_not_viewed": True,
    }
    if (
        not isinstance(attestation, Mapping)
        or set(attestation) != {"reviewer_ref", *required_attestations}
        or not str(attestation.get("reviewer_ref") or "").strip()
        or any(
            attestation.get(key) is not expected
            for key, expected in required_attestations.items()
        )
    ):
        raise MatureS1CourseAuditError("auditor blind attestation is incomplete")
    citations = {row["citation_id"]: row for row in source["course_citations"]}
    evidence_refs = _evidence_refs(source["as_of_packet"])
    criteria = value.get("criteria")
    if not isinstance(criteria, list) or len(criteria) != len(AUDIT_CRITERIA):
        raise MatureS1CourseAuditError("completed rubric criteria are incomplete")
    for (expected_id, expected_question), row in zip(AUDIT_CRITERIA, criteria):
        if (
            not isinstance(row, Mapping)
            or set(row)
            != {
                "criterion_id",
                "question",
                "verdict",
                "course_citation_ids",
                "as_of_evidence_refs",
                "rationale",
            }
            or row.get("criterion_id") != expected_id
            or row.get("question") != expected_question
            or row.get("verdict") not in {"SUPPORTED", "NOT_SUPPORTED", "INDETERMINATE"}
            or not str(row.get("rationale") or "").strip()
        ):
            raise MatureS1CourseAuditError(f"completed criterion is invalid: {expected_id}")
        citation_ids = row.get("course_citation_ids")
        refs = row.get("as_of_evidence_refs")
        if (
            not isinstance(citation_ids, list)
            or not citation_ids
            or len(citation_ids) != len(set(citation_ids))
            or any(item not in citations for item in citation_ids)
            or any(expected_id not in citations[item]["criterion_ids"] for item in citation_ids)
            or not isinstance(refs, list)
            or len(refs) != len(set(refs))
            or not set(refs).issubset(evidence_refs)
            or (row.get("verdict") != "INDETERMINATE" and not refs)
        ):
            raise MatureS1CourseAuditError(f"criterion citations/evidence are invalid: {expected_id}")
    verdicts = [row["verdict"] for row in criteria]
    derived_overall = (
        "NOT_SUPPORTED"
        if "NOT_SUPPORTED" in verdicts
        else "INDETERMINATE"
        if "INDETERMINATE" in verdicts
        else "SUPPORTED"
    )
    if value.get("overall_s1_course_fit") != derived_overall:
        raise MatureS1CourseAuditError("overall S1 verdict contradicts criterion verdicts")
    return value


def validate_bundle(
    bundle: Mapping[str, Any],
    *,
    trusted_gate_receipt_sha256: str,
    trusted_course_citations_sha256: str,
    trusted_reviewer_inputs_sha256: str,
    trusted_coverage_plan_sha256: str,
    trusted_coverage_manifest_sha256: str,
    course_root: Path,
) -> dict[str, Any]:
    """Validate a prepared bundle before any file is published."""

    if set(bundle) != {
        "reviewer_inputs",
        "governance_coverage_manifest",
        "bundle_manifest",
    }:
        raise MatureS1CourseAuditError("course audit bundle fields are not exact")
    reviewer_inputs = bundle.get("reviewer_inputs")
    coverage = bundle.get("governance_coverage_manifest")
    manifest = bundle.get("bundle_manifest")
    if (
        not isinstance(reviewer_inputs, list)
        or len(reviewer_inputs) != EXPECTED_CASES
        or not isinstance(coverage, Mapping)
        or not isinstance(manifest, Mapping)
    ):
        raise MatureS1CourseAuditError("course audit bundle sections are incomplete")
    validated_inputs = [validate_reviewer_input(row) for row in reviewer_inputs]
    audit_ids = [row["audit_case_id"] for row in validated_inputs]
    inputs_by_id = {row["audit_case_id"]: row for row in validated_inputs}
    if len(set(audit_ids)) != EXPECTED_CASES:
        raise MatureS1CourseAuditError("reviewer input rekeys are not unique")
    trusted_values = (
        trusted_gate_receipt_sha256,
        trusted_course_citations_sha256,
        trusted_reviewer_inputs_sha256,
        trusted_coverage_plan_sha256,
        trusted_coverage_manifest_sha256,
    )
    if not all(_is_sha256(value) for value in trusted_values):
        raise MatureS1CourseAuditError("external audit trust pins are missing or invalid")
    normalized_citations = validate_course_citations(
        validated_inputs[0]["course_citations"], course_root=course_root
    )
    if (
        set(coverage)
        != {
            "manifest_version",
            "status",
            "reviewer_visible",
            "basis",
            "performance_sealed",
            "performance_unseal_authorized",
            "rows",
            "rows_sha256",
            "summary",
            "coverage_manifest_sha256",
        }
        or
        coverage.get("manifest_version") != COVERAGE_MANIFEST_VERSION
        or not _self_hashed(coverage, "coverage_manifest_sha256")
        or coverage.get("reviewer_visible") is not False
        or coverage.get("basis") != "OBJECTIVE_ONLY_AS_OF"
        or coverage.get("performance_sealed") is not True
        or coverage.get("performance_unseal_authorized") is not False
        or coverage.get("rows_sha256") != canonical_sha256(coverage.get("rows"))
    ):
        raise MatureS1CourseAuditError("governance coverage manifest changed")
    coverage_rows = coverage.get("rows")
    if not isinstance(coverage_rows, list) or len(coverage_rows) != EXPECTED_CASES:
        raise MatureS1CourseAuditError("governance coverage rows are incomplete")
    coverage_ids: list[str] = []
    coverage_counts: Counter[str] = Counter()
    for row in coverage_rows:
        if (
            not isinstance(row, Mapping)
            or set(row) != {"audit_case_id", "coverage_claims"}
            or not isinstance(row.get("coverage_claims"), list)
            or len(row["coverage_claims"]) > MAX_COVERAGE_TAGS_PER_CASE
        ):
            raise MatureS1CourseAuditError("governance coverage row is invalid")
        seen_tags: set[str] = set()
        audit_id = str(row.get("audit_case_id") or "")
        allowed_refs = (
            _evidence_refs(inputs_by_id[audit_id]["as_of_packet"])
            if audit_id in inputs_by_id
            else set()
        )
        for claim in row["coverage_claims"]:
            if (
                not isinstance(claim, Mapping)
                or set(claim) != {"tag", "as_of_evidence_refs"}
                or claim.get("tag") not in REQUIRED_COVERAGE_TAGS
                or claim.get("tag") in seen_tags
                or not isinstance(claim.get("as_of_evidence_refs"), list)
                or not claim["as_of_evidence_refs"]
                or len(claim["as_of_evidence_refs"])
                != len(set(claim["as_of_evidence_refs"]))
                or not set(claim["as_of_evidence_refs"]).issubset(allowed_refs)
            ):
                raise MatureS1CourseAuditError("governance coverage claim is invalid")
            seen_tags.add(str(claim["tag"]))
        coverage_ids.append(audit_id)
        coverage_counts.update(seen_tags)
    if set(coverage_ids) != set(audit_ids) or len(set(coverage_ids)) != EXPECTED_CASES:
        raise MatureS1CourseAuditError("governance coverage rekeys differ from reviewer inputs")
    missing_tags = [
        tag
        for tag in REQUIRED_COVERAGE_TAGS
        if coverage_counts[tag] < MIN_COVERAGE_CASES_PER_TAG
    ]
    expected_coverage_status = INSUFFICIENT_STATUS if missing_tags else "PASS"
    expected_summary = {
        "status": expected_coverage_status,
        "required_tags": list(REQUIRED_COVERAGE_TAGS),
        "minimum_per_tag": MIN_COVERAGE_CASES_PER_TAG,
        "counts": {tag: coverage_counts[tag] for tag in REQUIRED_COVERAGE_TAGS},
        "missing_tags": missing_tags,
    }
    if coverage.get("status") != expected_coverage_status or coverage.get("summary") != expected_summary:
        raise MatureS1CourseAuditError("governance coverage status/summary is not recomputed")
    expected_status = INSUFFICIENT_STATUS if missing_tags else READY_STATUS
    required_manifest_values = {
        "manifest_version": BUNDLE_MANIFEST_VERSION,
        "status": expected_status,
        "tool_version": TOOL_VERSION,
        "target_scenario": TARGET_SCENARIO,
        "target_route": TARGET_ROUTE,
        "case_count": EXPECTED_CASES,
        "all_cases_rekeyed": True,
        "source_identifiers_present_in_reviewer_inputs": False,
        "three_run_outputs_present": False,
        "common_ledger_present": False,
        "real_stock_identity_present": False,
        "future_or_performance_present": False,
        "rubrics_blank": True,
        "course_audit_executed": False,
        "ai_self_review_used": False,
        "independent_course_audit_executed": False,
        "objective_rule_verification_scope": OBJECTIVE_VERIFICATION_SCOPE,
        "course_fidelity_conclusion": None,
        "performance_sealed": True,
        "performance_unseal_authorized": False,
    }
    expected_manifest_fields = {
        *required_manifest_values,
        "consistency_report_sha256",
        "consistency_gate_receipt_sha256",
        "course_citations_sha256",
        "reviewer_inputs_sha256",
        "coverage_manifest_sha256",
        "coverage_plan_sha256",
        "bundle_manifest_sha256",
    }
    if (
        set(manifest) != expected_manifest_fields
        or not _self_hashed(manifest, "bundle_manifest_sha256")
        or any(manifest.get(key) != expected for key, expected in required_manifest_values.items())
        or manifest.get("reviewer_inputs_sha256") != canonical_sha256(validated_inputs)
        or manifest.get("consistency_gate_receipt_sha256")
        != trusted_gate_receipt_sha256
        or manifest.get("course_citations_sha256")
        != trusted_course_citations_sha256
        or manifest.get("reviewer_inputs_sha256")
        != trusted_reviewer_inputs_sha256
        or manifest.get("coverage_plan_sha256")
        != trusted_coverage_plan_sha256
        or manifest.get("coverage_manifest_sha256")
        != coverage.get("coverage_manifest_sha256")
        or manifest.get("coverage_manifest_sha256")
        != trusted_coverage_manifest_sha256
        or manifest.get("course_citations_sha256")
        != canonical_sha256(normalized_citations)
        or any(
            canonical_sha256(row["course_citations"])
            != manifest.get("course_citations_sha256")
            for row in validated_inputs
        )
    ):
        raise MatureS1CourseAuditError("course audit bundle manifest changed")
    return {
        "reviewer_inputs": validated_inputs,
        "governance_coverage_manifest": deepcopy(dict(coverage)),
        "bundle_manifest": deepcopy(dict(manifest)),
    }


def validate_completed_audit_set(
    reviewer_inputs: Sequence[Mapping[str, Any]],
    completed_rubrics: Sequence[Mapping[str, Any]],
    *,
    bundle_manifest: Mapping[str, Any],
    governance_coverage_manifest: Mapping[str, Any],
    trusted_gate_receipt_sha256: str,
    trusted_course_citations_sha256: str,
    trusted_reviewer_inputs_sha256: str,
    trusted_coverage_plan_sha256: str,
    trusted_coverage_manifest_sha256: str,
    course_root: Path,
    independent_review_receipt: Mapping[str, Any] | None = None,
    trusted_independent_review_receipt_sha256: str | None = None,
) -> dict[str, Any]:
    """Validate future review records while retaining the performance seal."""

    validated_bundle = validate_bundle(
        {
            "reviewer_inputs": list(reviewer_inputs),
            "governance_coverage_manifest": dict(governance_coverage_manifest),
            "bundle_manifest": dict(bundle_manifest),
        },
        trusted_gate_receipt_sha256=trusted_gate_receipt_sha256,
        trusted_course_citations_sha256=trusted_course_citations_sha256,
        trusted_reviewer_inputs_sha256=trusted_reviewer_inputs_sha256,
        trusted_coverage_plan_sha256=trusted_coverage_plan_sha256,
        trusted_coverage_manifest_sha256=trusted_coverage_manifest_sha256,
        course_root=course_root,
    )
    manifest = validated_bundle["bundle_manifest"]
    if manifest.get("status") == INSUFFICIENT_STATUS:
        return {
            "status": INSUFFICIENT_STATUS,
            "validated_rubrics": 0,
            "course_fidelity_conclusion": None,
            "performance_sealed": True,
            "performance_unseal_authorized": False,
        }
    if manifest.get("status") != READY_STATUS:
        raise MatureS1CourseAuditError("course audit bundle is not ready")
    inputs_by_id = {str(row.get("audit_case_id") or ""): row for row in reviewer_inputs}
    rubrics_by_id = {str(row.get("audit_case_id") or ""): row for row in completed_rubrics}
    if (
        len(inputs_by_id) != EXPECTED_CASES
        or "" in inputs_by_id
        or len(rubrics_by_id) != EXPECTED_CASES
        or "" in rubrics_by_id
        or set(inputs_by_id) != set(rubrics_by_id)
    ):
        raise MatureS1CourseAuditError("completed rubric coverage is not exactly 36 rekeyed cases")
    validated = [
        validate_completed_rubric(inputs_by_id[case_id], rubrics_by_id[case_id])
        for case_id in sorted(inputs_by_id)
    ]
    ordered_completed = sorted(validated, key=lambda row: row["audit_case_id"])
    receipt = dict(independent_review_receipt or {})
    expected_receipt_fields = {
        "receipt_version",
        "evaluation_layer",
        "reviewer_ref",
        "reviewer_credential_sha256",
        "reviewer_inputs_sha256",
        "coverage_manifest_sha256",
        "course_citations_sha256",
        "completed_rubrics_sha256",
        "ai_self_review_used",
        "original_three_run_reviewer",
        "only_reviewer_inputs_viewed",
        "future_or_performance_viewed",
        "receipt_sha256",
    }
    reviewer_refs = {
        str(row["auditor_attestation"]["reviewer_ref"]) for row in ordered_completed
    }
    trusted_review_receipt = str(trusted_independent_review_receipt_sha256 or "")
    if (
        set(receipt) != expected_receipt_fields
        or not _self_hashed(receipt, "receipt_sha256")
        or not _is_sha256(trusted_review_receipt)
        or canonical_sha256(receipt) != trusted_review_receipt
        or receipt.get("receipt_version") != REVIEW_RECEIPT_VERSION
        or receipt.get("evaluation_layer") != INDEPENDENT_AUDIT_LAYER
        or len(reviewer_refs) != 1
        or receipt.get("reviewer_ref") not in reviewer_refs
        or not _is_sha256(receipt.get("reviewer_credential_sha256"))
        or receipt.get("reviewer_inputs_sha256")
        != manifest.get("reviewer_inputs_sha256")
        or receipt.get("coverage_manifest_sha256")
        != manifest.get("coverage_manifest_sha256")
        or receipt.get("course_citations_sha256")
        != manifest.get("course_citations_sha256")
        or receipt.get("completed_rubrics_sha256")
        != canonical_sha256(ordered_completed)
        or receipt.get("ai_self_review_used") is not False
        or receipt.get("original_three_run_reviewer") is not False
        or receipt.get("only_reviewer_inputs_viewed") is not True
        or receipt.get("future_or_performance_viewed") is not False
    ):
        raise MatureS1CourseAuditError(
            "completed rubrics lack an externally pinned independent-review receipt"
        )
    return {
        "status": "COURSE_AUDIT_RECORDS_VALIDATED_NO_AGGREGATE_CONCLUSION",
        "validated_rubrics": len(validated),
        "overall_verdict_counts": dict(
            sorted(Counter(row["overall_s1_course_fit"] for row in validated).items())
        ),
        "course_fidelity_conclusion": None,
        "independent_review_provenance_verified": True,
        "performance_sealed": True,
        "performance_unseal_authorized": False,
    }


def write_bundle(
    bundle: Mapping[str, Any],
    output_dir: Path,
    *,
    trusted_gate_receipt_sha256: str,
    trusted_course_citations_sha256: str,
    trusted_reviewer_inputs_sha256: str,
    trusted_coverage_plan_sha256: str,
    trusted_coverage_manifest_sha256: str,
    course_root: Path,
) -> dict[str, Any]:
    """Publish a new bundle directory; refuse any overwrite or resume."""

    bundle = validate_bundle(
        bundle,
        trusted_gate_receipt_sha256=trusted_gate_receipt_sha256,
        trusted_course_citations_sha256=trusted_course_citations_sha256,
        trusted_reviewer_inputs_sha256=trusted_reviewer_inputs_sha256,
        trusted_coverage_plan_sha256=trusted_coverage_plan_sha256,
        trusted_coverage_manifest_sha256=trusted_coverage_manifest_sha256,
        course_root=course_root,
    )
    output = Path(output_dir)
    if output.exists():
        raise MatureS1CourseAuditError(f"refusing to overwrite course audit bundle: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    files = {
        "reviewer_inputs.jsonl": canonical_jsonl_bytes(bundle["reviewer_inputs"]),
        "governance_coverage_manifest.json": canonical_json_bytes(
            bundle["governance_coverage_manifest"]
        )
        + b"\n",
        "bundle_manifest.json": canonical_json_bytes(bundle["bundle_manifest"]) + b"\n",
    }
    temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}.tmp-", dir=output.parent))
    try:
        for name, payload in files.items():
            with (temporary / name).open("xb") as handle:
                handle.write(payload)
        temporary.rename(output)
    except Exception as exc:
        shutil.rmtree(temporary, ignore_errors=True)
        raise MatureS1CourseAuditError(
            f"cannot atomically publish new course audit bundle: {output}"
        ) from exc
    return {
        "status": bundle["bundle_manifest"]["status"],
        "output_dir": str(output.resolve()),
        "files": {
            name: {"sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload)}
            for name, payload in files.items()
        },
    }


def _read_json(path: Path) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MatureS1CourseAuditError(f"cannot read JSON: {path}") from exc


def _read_jsonl(paths: Sequence[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in paths:
        try:
            lines = Path(path).read_text(encoding="utf-8-sig").splitlines()
        except OSError as exc:
            raise MatureS1CourseAuditError(f"cannot read case JSONL: {path}") from exc
        for line_number, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise MatureS1CourseAuditError(
                    f"invalid case JSONL at {path}:{line_number}"
                ) from exc
            if not isinstance(row, dict):
                raise MatureS1CourseAuditError(f"case JSONL row is not an object: {path}")
            rows.append(row)
    return rows


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-case", type=Path, action="append", required=True)
    parser.add_argument("--consistency-gate", type=Path, required=True)
    parser.add_argument("--trusted-gate-receipt-sha256", required=True)
    parser.add_argument("--coverage-plan", type=Path, required=True)
    parser.add_argument("--trusted-coverage-plan-sha256", required=True)
    parser.add_argument("--course-citations", type=Path, required=True)
    parser.add_argument("--trusted-course-citations-sha256", required=True)
    parser.add_argument("--trusted-reviewer-inputs-sha256")
    parser.add_argument("--trusted-coverage-manifest-sha256")
    parser.add_argument("--course-root", type=Path, required=True)
    parser.add_argument("--rekey-secret-file", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    citation_document = _read_json(args.course_citations)
    if not isinstance(citation_document, Mapping) or set(citation_document) != {"citations"}:
        raise MatureS1CourseAuditError("course citation document must contain only citations")
    try:
        secret = args.rekey_secret_file.read_bytes().strip()
    except OSError as exc:
        raise MatureS1CourseAuditError("cannot read rekey secret") from exc
    bundle = build_blind_course_audit_bundle(
        _read_jsonl(args.source_case),
        consistency_gate=_read_json(args.consistency_gate),
        trusted_gate_receipt_sha256=args.trusted_gate_receipt_sha256,
        coverage_plan=_read_json(args.coverage_plan),
        trusted_coverage_plan_sha256=args.trusted_coverage_plan_sha256,
        course_citations=citation_document["citations"],
        trusted_course_citations_sha256=args.trusted_course_citations_sha256,
        course_root=args.course_root,
        rekey_secret=secret,
    )
    candidate_pins = {
        "status": "PIN_REQUIRED_BEFORE_PUBLISH",
        "consistency_gate_receipt_sha256": bundle["bundle_manifest"][
            "consistency_gate_receipt_sha256"
        ],
        "coverage_plan_sha256": bundle["bundle_manifest"]["coverage_plan_sha256"],
        "course_citations_sha256": bundle["bundle_manifest"][
            "course_citations_sha256"
        ],
        "reviewer_inputs_sha256": bundle["bundle_manifest"][
            "reviewer_inputs_sha256"
        ],
        "coverage_manifest_sha256": bundle["bundle_manifest"][
            "coverage_manifest_sha256"
        ],
        "course_audit_executed": False,
        "performance_sealed": True,
        "performance_unseal_authorized": False,
    }
    if (
        args.trusted_reviewer_inputs_sha256 is None
        or args.trusted_coverage_manifest_sha256 is None
    ):
        print(json.dumps(candidate_pins, ensure_ascii=False, sort_keys=True))
        return 3
    result = write_bundle(
        bundle,
        args.output_dir,
        trusted_gate_receipt_sha256=args.trusted_gate_receipt_sha256,
        trusted_course_citations_sha256=args.trusted_course_citations_sha256,
        trusted_reviewer_inputs_sha256=args.trusted_reviewer_inputs_sha256,
        trusted_coverage_plan_sha256=args.trusted_coverage_plan_sha256,
        trusted_coverage_manifest_sha256=args.trusted_coverage_manifest_sha256,
        course_root=args.course_root,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == READY_STATUS else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
