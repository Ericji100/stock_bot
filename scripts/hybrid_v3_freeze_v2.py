"""Validate and immutably freeze the complete hybrid monitoring V2 contract.

The formal freeze is intentionally fail-closed.  It does not build packets,
select a holdout, call an AI model, or run a backtest.  Every component and
outcome-blind input must already be final and locked before this module will
create ``hybrid_monitoring_v2/freeze_manifest.json``.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


FREEZE_VERSION = "hybrid-monitoring-v2-freeze-v1"
FREEZE_STATUS = "FROZEN_IMMUTABLE_BEFORE_AI_EXECUTION"
READINESS_AUDIT_VERSION = "hybrid-monitoring-v2-pre-freeze-readiness-v1"
PROTOCOL_VERSION = "hybrid-monitoring-protocol-v2"
PIVOT_DEFINITION = "LEGACY_RADIUS_3_10_NOT_COURSE_L1_L2"
EXPECTED_STOCKS = 1029
EXPECTED_STOCK_DAYS = 151804

DEFAULT_V1_PROTOCOL_MANIFEST_SHA256 = "aa69b49b100f96d5c7f7248a4305a99d2270ca86f28381bc98a4e2fbd6c95421"
DEFAULT_V1_EXECUTION_MANIFEST_SHA256 = "b9e9f0cfa6f21277042b78add2f1e6de1be43e99f146fbac10b33809f4e69ae1"

PROTECTED_STRATEGY_FILES = (
    "docs/enlightenment-ai-judgement-v1.md",
    "docs/enlightenment-ai-judgement-v2.md",
    "docs/enlightenment-ai-judgement-v3.md",
    "config/enlightenment_ai_rules_v2.json",
    "config/enlightenment_ai_rules_v3.json",
)


class FreezeValidationError(ValueError):
    """The proposed freeze is incomplete, mutable, or semantically unsafe."""


class ImmutableFreezeError(FreezeValidationError):
    """A different freeze artifact already exists at the immutable target."""


@dataclass(frozen=True)
class ComponentSpec:
    name: str
    relative_path: str
    kind: str
    expected_version: str
    version_keys: tuple[str, ...] = ()
    status_keys: tuple[str, ...] = ()


DEFAULT_COMPONENTS = (
    ComponentSpec("protocol_doc", "docs/hybrid-monitoring-protocol-v2.md", "markdown", PROTOCOL_VERSION),
    ComponentSpec(
        "protocol",
        "config/hybrid_monitoring_protocol_v2.json",
        "json",
        PROTOCOL_VERSION,
        ("protocol_version",),
        ("status",),
    ),
    ComponentSpec(
        "schema",
        "config/hybrid_atomic_semantics_v2.schema.json",
        "json",
        "hybrid-atomic-semantics-v2",
        ("x-schema-version", "schema_version", "$id"),
        ("x-contract-status", "status"),
    ),
    ComponentSpec("prompt", "config/hybrid_semantic_prompt_v2.md", "markdown", "hybrid-semantic-prompt-v2"),
    ComponentSpec(
        "objective",
        "scripts/hybrid_v3_objective_state_v2.py",
        "python",
        "hybrid-v3-objective-state-v2",
        ("ENGINE_VERSION", "OBJECTIVE_ENGINE_VERSION", "COMPONENT_VERSION"),
        ("ENGINE_STATUS", "OBJECTIVE_ENGINE_STATUS", "COMPONENT_STATUS"),
    ),
    ComponentSpec(
        "policy",
        "scripts/hybrid_v3_atomic_policy_v2.py",
        "python",
        "hybrid-v3-atomic-policy-v2",
        ("POLICY_VERSION", "COMPONENT_VERSION"),
        ("POLICY_STATUS", "COMPONENT_STATUS"),
    ),
    ComponentSpec(
        "packet",
        "scripts/hybrid_v3_atomic_packets_v2.py",
        "python",
        "hybrid-v3-atomic-packets-v2",
        ("BUILDER_VERSION", "PACKET_BUILDER_VERSION", "COMPONENT_VERSION"),
        ("BUILDER_STATUS", "PACKET_BUILDER_STATUS", "COMPONENT_STATUS"),
    ),
    ComponentSpec(
        "runner",
        "scripts/hybrid_v3_atomic_runner_v2.py",
        "python",
        "hybrid-v3-atomic-runner-v2",
        ("RUNNER_VERSION", "COMPONENT_VERSION"),
        ("RUNNER_STATUS", "COMPONENT_STATUS"),
    ),
    ComponentSpec(
        "sharding",
        "scripts/hybrid_v3_sharding_v2.py",
        "python",
        "hybrid-v3-sharding-v2",
        ("SHARDING_VERSION", "COMPONENT_VERSION"),
        ("SHARDING_STATUS", "COMPONENT_STATUS"),
    ),
    ComponentSpec(
        "consistency",
        "scripts/hybrid_v3_consistency_v2.py",
        "python",
        "hybrid-v3-consistency-v2",
        ("CONSISTENCY_VERSION", "COMPONENT_VERSION"),
        ("CONSISTENCY_STATUS", "COMPONENT_STATUS"),
    ),
    ComponentSpec(
        "reviewer",
        "scripts/hybrid_v3_codex_reviewer_v2.py",
        "python",
        "hybrid-v3-codex-reviewer-v2",
        ("ADAPTER_VERSION", "COMPONENT_VERSION"),
        ("ADAPTER_STATUS", "COMPONENT_STATUS"),
    ),
    ComponentSpec(
        "research_calibration",
        "scripts/hybrid_v2_research_calibration.py",
        "python",
        "hybrid-v2-research-calibration-v1",
        ("CALIBRATION_VERSION", "COMPONENT_VERSION"),
        ("TOOL_STATUS", "COMPONENT_STATUS"),
    ),
    ComponentSpec(
        "course_gold",
        "scripts/hybrid_v3_course_gold_v2.py",
        "python",
        "hybrid-v3-course-gold-v2",
        ("COURSE_GOLD_VERSION", "COMPONENT_VERSION"),
        ("COURSE_GOLD_STATUS", "COMPONENT_STATUS"),
    ),
)


@dataclass(frozen=True)
class FreezeInputs:
    root: Path
    v1_protocol_manifest: Path
    v1_execution_manifest: Path
    source_manifest: Path
    v2_review_point_manifest: Path
    output: Path
    expected_v1_protocol_manifest_sha256: str = DEFAULT_V1_PROTOCOL_MANIFEST_SHA256
    expected_v1_execution_manifest_sha256: str = DEFAULT_V1_EXECUTION_MANIFEST_SHA256
    components: tuple[ComponentSpec, ...] = DEFAULT_COMPONENTS
    protocol_relative_path: str = "config/hybrid_monitoring_protocol_v2.json"
    builder_relative_path: str = "scripts/hybrid_v3_atomic_packets_v2.py"
    protocol_version: str = PROTOCOL_VERSION
    review_point_manifest_version: str = "hybrid-v3-review-points-v2"
    consistency_plan_version_field: str = "holdout_plan_version"
    consistency_plan_version: str = "hybrid-v3-holdout-plan-v2"
    consistency_plan_locked_status: str = "LOCKED_OUTCOME_BLIND_SAMPLE_PLAN"
    consistency_plan_sample_size_field: str = "sample_size"
    course_gold_bundle_version: str = "hybrid-v3-course-gold-bundle-v2"
    course_gold_rubrics_version: str = "hybrid-v3-course-gold-rubrics-v2"
    research_calibration_manifest: Path | None = None
    course_gold_bundle_manifest: Path | None = None
    course_gold_rubrics_manifest: Path | None = None
    consistency_sample_manifest: Path | None = None
    execution_manifest: Path | None = None


def default_inputs(root: Path | None = None) -> FreezeInputs:
    root = (root or Path(__file__).resolve().parents[1]).resolve()
    source_run = root / "reports/course_backtest/2024-02-02/historical_scan_2023h2_formal_ai_v3_daily_scan"
    v1_run = source_run / "hybrid_monitoring_v1"
    v2_run = source_run / "hybrid_monitoring_v2"
    return FreezeInputs(
        root=root,
        v1_protocol_manifest=v1_run / "protocol_freeze_manifest.json",
        v1_execution_manifest=v1_run / "execution_freeze_manifest.json",
        source_manifest=source_run / "packet_manifest.json",
        v2_review_point_manifest=v2_run / "review_point_manifest.json",
        output=v2_run / "freeze_manifest.json",
        research_calibration_manifest=v2_run / "research_calibration/human_frozen_manifest.json",
        course_gold_bundle_manifest=v2_run / "course_gold/bundle_manifest.json",
        course_gold_rubrics_manifest=v2_run / "course_gold/human_frozen_rubrics_manifest.json",
        consistency_sample_manifest=v2_run / "consistency/holdout_plan.json",
        execution_manifest=v2_run / "execution_manifest.json",
    )


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FreezeValidationError(f"required file is missing: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FreezeValidationError(f"invalid JSON file: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise FreezeValidationError(f"expected JSON object: {path}")
    return value


def _assert_sha(path: Path, expected: str, label: str) -> str:
    if not path.is_file():
        raise FreezeValidationError(f"{label} is missing: {path}")
    actual = file_sha256(path)
    if actual.lower() != str(expected).lower():
        raise FreezeValidationError(f"{label} hash changed: expected {expected}, got {actual}: {path}")
    return actual


def _entry_path(entry: Mapping[str, Any], root: Path, base: Path) -> Path:
    relative = entry.get("relative_path")
    if relative:
        path = (root / str(relative)).resolve()
        try:
            path.relative_to(root.resolve())
        except ValueError as exc:
            raise FreezeValidationError(f"relative frozen path escapes root: {relative}") from exc
        return path
    raw = entry.get("absolute_path") or entry.get("path")
    if not raw:
        raise FreezeValidationError(f"frozen hash entry has no path: {entry}")
    path = Path(str(raw))
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def _hash_entries(manifest: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    for field in ("files", "artifacts"):
        rows = manifest.get(field) or []
        if not isinstance(rows, list):
            raise FreezeValidationError(f"V1 manifest field {field} must be an array")
        yield from rows
    for field in ("event_manifest", "policy_boundary_manifest"):
        row = manifest.get(field)
        if row is not None:
            if not isinstance(row, dict):
                raise FreezeValidationError(f"V1 manifest field {field} must be an object")
            yield row


def _validate_v1_manifest(
    *, path: Path, expected_manifest_sha256: str, root: Path, label: str
) -> dict[str, Any]:
    manifest_sha = _assert_sha(path, expected_manifest_sha256, label)
    manifest = _read_json(path)
    status = str(manifest.get("status") or "")
    if "LOCKED" not in status and "FROZEN" not in status:
        raise FreezeValidationError(f"{label} is not locked/frozen: {status!r}")
    checked: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in _hash_entries(manifest):
        expected = str(entry.get("sha256") or "")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", expected):
            raise FreezeValidationError(f"{label} has invalid sha256 entry: {entry}")
        target = _entry_path(entry, root, path.parent)
        key = str(target).casefold()
        if key in seen:
            continue
        seen.add(key)
        actual = _assert_sha(target, expected, f"{label} protected entry")
        checked.append({"path": str(target), "sha256": actual})
    if not checked:
        raise FreezeValidationError(f"{label} protects no files")
    return {"path": str(path.resolve()), "sha256": manifest_sha, "entries_checked": len(checked)}


def _validate_strategy_coverage(protocol_manifest_path: Path) -> list[dict[str, str]]:
    manifest = _read_json(protocol_manifest_path)
    indexed = {
        str(entry.get("relative_path")): entry
        for entry in manifest.get("files") or []
        if isinstance(entry, dict) and entry.get("relative_path")
    }
    output: list[dict[str, str]] = []
    for relative in PROTECTED_STRATEGY_FILES:
        entry = indexed.get(relative)
        if entry is None:
            raise FreezeValidationError(f"V1 protocol freeze does not protect strategy file: {relative}")
        output.append({"relative_path": relative, "sha256": str(entry["sha256"]).lower()})
    return output


_MARKDOWN_VERSION = re.compile(r"^\s*(?:版本|version)\s*[:：]\s*`?([A-Za-z0-9._-]+)`?", re.IGNORECASE | re.MULTILINE)
_MARKDOWN_STATUS = re.compile(r"^\s*(?:狀態|status)\s*[:：]\s*`?([A-Za-z0-9._-]+)`?", re.IGNORECASE | re.MULTILINE)


def _normalize_version(value: Any) -> str:
    text = str(value).strip().rstrip("/")
    if "://" in text:
        text = text.rsplit("/", 1)[-1]
    if text.endswith(".json"):
        text = text[:-5]
    return text


def _first_value(mapping: Mapping[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def _python_constants(path: Path) -> dict[str, Any]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    except (OSError, SyntaxError) as exc:
        raise FreezeValidationError(f"invalid Python component: {path}: {exc}") from exc
    values: dict[str, Any] = {}
    for node in tree.body:
        name: str | None = None
        value_node: ast.AST | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name, value_node = node.targets[0].id, node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            name, value_node = node.target.id, node.value
        if name is None or value_node is None:
            continue
        try:
            values[name] = ast.literal_eval(value_node)
        except (ValueError, TypeError):
            continue
    return values


def _component_metadata(root: Path, spec: ComponentSpec) -> dict[str, str]:
    path = (root / spec.relative_path).resolve()
    if not path.is_file():
        raise FreezeValidationError(f"V2 component is missing: {spec.name}: {path}")
    if spec.kind == "markdown":
        text = path.read_text(encoding="utf-8-sig")
        version_match = _MARKDOWN_VERSION.search(text)
        status_match = _MARKDOWN_STATUS.search(text)
        version = version_match.group(1) if version_match else None
        status = status_match.group(1) if status_match else None
    elif spec.kind == "json":
        value = _read_json(path)
        version = _first_value(value, spec.version_keys)
        status = _first_value(value, spec.status_keys)
    elif spec.kind == "python":
        value = _python_constants(path)
        version = _first_value(value, spec.version_keys)
        status = _first_value(value, spec.status_keys)
    else:
        raise FreezeValidationError(f"unsupported component kind: {spec.kind}")
    normalized_version = _normalize_version(version) if version is not None else ""
    normalized_status = str(status or "").strip().upper()
    if normalized_version != spec.expected_version:
        raise FreezeValidationError(
            f"V2 component {spec.name} version is not final {spec.expected_version!r}: {normalized_version!r}"
        )
    if normalized_status != "FINAL":
        raise FreezeValidationError(f"V2 component {spec.name} status must be FINAL: {normalized_status!r}")
    return {
        "name": spec.name,
        "relative_path": spec.relative_path,
        "version": normalized_version,
        "status": normalized_status,
        "sha256": file_sha256(path),
    }


def _positive_integer(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise FreezeValidationError(f"{label} must be a positive integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise FreezeValidationError(f"{label} must be a positive integer") from exc
    if number <= 0 or number != value:
        raise FreezeValidationError(f"{label} must be a positive integer")
    return number


def _validate_critical_question_metadata(schema: Mapping[str, Any]) -> list[str]:
    """Return the frozen critical set; never let all questions be an implicit fallback."""

    values = schema.get("x-critical-question-ids")
    if not isinstance(values, list) or not values:
        raise FreezeValidationError("schema x-critical-question-ids must be a nonempty frozen list")
    critical = [str(value).strip() for value in values]
    if any(not value for value in critical) or len(critical) != len(set(critical)):
        raise FreezeValidationError("schema x-critical-question-ids must contain unique nonempty ids")
    groups = schema.get("x-question-groups")
    if not isinstance(groups, Mapping) or not groups:
        raise FreezeValidationError("schema x-question-groups is required to validate critical ids")
    known: set[str] = set()
    for name, rows in groups.items():
        if not isinstance(rows, list) or any(not isinstance(value, str) or not value for value in rows):
            raise FreezeValidationError(f"schema question group is invalid: {name}")
        known.update(rows)
    unknown = sorted(set(critical) - known)
    if unknown:
        raise FreezeValidationError(f"schema critical question ids are absent from question groups: {unknown}")
    return critical


def _validate_correctness_gate(protocol: Mapping[str, Any]) -> dict[str, Any]:
    """Freeze correctness separately from repeatability and future performance."""

    gate = protocol.get("correctness_gate")
    if not isinstance(gate, Mapping):
        raise FreezeValidationError("protocol correctness_gate is required")
    invariant = gate.get("deterministic_course_invariant")
    if not isinstance(invariant, Mapping) or invariant.get("required") is not True:
        raise FreezeValidationError("deterministic course invariant must be required")
    try:
        pass_rate = float(invariant.get("required_pass_rate"))
    except (TypeError, ValueError) as exc:
        raise FreezeValidationError("deterministic course invariant required_pass_rate must be 1.0") from exc
    if pass_rate != 1.0:
        raise FreezeValidationError("deterministic course invariant required_pass_rate must be 1.0")
    if gate.get("repeatability_alone_is_sufficient") is not False:
        raise FreezeValidationError("formal correctness cannot be based on repeatability alone")
    requirements = gate.get("overall_formal_pass_requires")
    required = {
        "THREE_RUN_REPEATABILITY",
        "DETERMINISTIC_COURSE_INVARIANT",
        "RESEARCH_CALIBRATION",
        "BLINDED_COURSE_GOLD_HOLDOUT",
    }
    if (
        not isinstance(requirements, list)
        or len(requirements) != len(set(requirements))
        or set(requirements) != required
    ):
        raise FreezeValidationError(
            "overall formal pass must require exactly repeatability, deterministic course invariant, research calibration, and blinded course gold holdout"
        )

    calibration = gate.get("research_calibration")
    if not isinstance(calibration, Mapping):
        raise FreezeValidationError("research calibration contract is required")
    if calibration.get("required") is not True:
        raise FreezeValidationError("research calibration must be required for overall formal pass")
    if calibration.get("classification") != "RESEARCH_CALIBRATION_NOT_PERFORMANCE_HOLDOUT":
        raise FreezeValidationError("research calibration must not be classified as a performance holdout")
    if calibration.get("separated_from_future_performance") is not True:
        raise FreezeValidationError("research calibration and future performance must be separated")
    minimum_cases = _positive_integer(
        calibration.get("minimum_cases"), "correctness_gate.research_calibration.minimum_cases"
    )
    if minimum_cases < 20:
        raise FreezeValidationError("research calibration must require at least 20 course cases")
    balance = calibration.get("required_balance")
    if not isinstance(balance, Mapping):
        raise FreezeValidationError("research calibration required_balance is required")
    constructive_cases = _positive_integer(
        balance.get("positive_or_constructive"),
        "correctness_gate.research_calibration.required_balance.positive_or_constructive",
    )
    negative_cases = _positive_integer(
        balance.get("negative_or_ambiguous"),
        "correctness_gate.research_calibration.required_balance.negative_or_ambiguous",
    )
    if constructive_cases < 10 or negative_cases < 10:
        raise FreezeValidationError(
            "research calibration must require at least 10 constructive and 10 negative/ambiguous cases"
        )
    if constructive_cases + negative_cases > minimum_cases:
        raise FreezeValidationError("research calibration balance cannot exceed minimum_cases")
    if calibration.get("human_rubric_must_be_frozen_before_ai") is not True:
        raise FreezeValidationError("research calibration human rubric must be frozen before AI")
    if calibration.get("incomplete_or_unreviewed_case_result") != "NOT_APPLICABLE_FAIL_CLOSED":
        raise FreezeValidationError("incomplete research calibration cases must fail closed")
    try:
        calibration_rate = float(calibration.get("required_pass_rate"))
    except (TypeError, ValueError) as exc:
        raise FreezeValidationError("research calibration required_pass_rate must be 1.0") from exc
    if calibration_rate != 1.0:
        raise FreezeValidationError("research calibration required_pass_rate must be 1.0")

    blinded_gold = gate.get("blinded_course_gold_holdout")
    if not isinstance(blinded_gold, Mapping):
        raise FreezeValidationError("blinded course gold holdout contract is required")
    if blinded_gold.get("required") is not True:
        raise FreezeValidationError("blinded course gold holdout must be required for overall formal pass")
    if blinded_gold.get("classification") != "BLINDED_HISTORICAL_COURSE_GOLD_NOT_PERFORMANCE_HOLDOUT":
        raise FreezeValidationError("blinded course gold must not be classified as a performance holdout")
    if blinded_gold.get("fixed_sampling_before_human_labels") is not True:
        raise FreezeValidationError("blinded course gold sampling must be frozen before human labels")
    if blinded_gold.get("human_labels_frozen_before_ai") is not True:
        raise FreezeValidationError("blinded course gold human labels must be frozen before AI")
    if blinded_gold.get("identity_future_old_ai_hidden_during_annotation") is not True:
        raise FreezeValidationError("blinded course gold annotation must hide identity, future data, and old AI")
    if blinded_gold.get("separated_from_future_performance") is not True:
        raise FreezeValidationError("blinded course gold and future performance must be separated")
    blinded_minimum_cases = _positive_integer(
        blinded_gold.get("minimum_cases"),
        "correctness_gate.blinded_course_gold_holdout.minimum_cases",
    )
    if blinded_minimum_cases < 20:
        raise FreezeValidationError("blinded course gold must require at least 20 cases")
    blinded_minimum_strata = _positive_integer(
        blinded_gold.get("minimum_distinct_objective_strata"),
        "correctness_gate.blinded_course_gold_holdout.minimum_distinct_objective_strata",
    )
    if blinded_minimum_strata < 4:
        raise FreezeValidationError("blinded course gold must require at least 4 objective strata")
    try:
        blinded_atomic_rate = float(blinded_gold.get("required_atomic_assertion_agreement_rate"))
        blinded_permission_rate = float(blinded_gold.get("required_material_permission_agreement_rate"))
    except (TypeError, ValueError) as exc:
        raise FreezeValidationError("blinded course gold agreement rates are invalid") from exc
    if not 0.9 <= blinded_atomic_rate <= 1.0:
        raise FreezeValidationError("blinded course gold atomic assertion agreement must be at least 0.90")
    if blinded_permission_rate != 1.0:
        raise FreezeValidationError("blinded course gold material permission agreement must be 1.0")

    future = gate.get("future_performance")
    if not isinstance(future, Mapping):
        raise FreezeValidationError("future performance separation contract is required")
    if future.get("read_before_consistency_ledger_locked") is not False:
        raise FreezeValidationError("future performance must stay hidden until the consistency ledger is locked")
    if future.get("read_before_full_semantic_ledger_locked") is not False:
        raise FreezeValidationError("future performance must stay hidden until the full semantic ledger is locked")
    if future.get("part_of_repeatability") is not False:
        raise FreezeValidationError("future performance cannot be part of repeatability acceptance")
    return {
        "deterministic_course_invariant_required": True,
        "deterministic_course_invariant_required_pass_rate": 1.0,
        "repeatability_alone_is_sufficient": False,
        "overall_formal_pass_requires": sorted(required),
        "research_calibration_classification": calibration["classification"],
        "research_calibration_required": True,
        "research_calibration_minimum_cases": minimum_cases,
        "research_calibration_required_balance": {
            "positive_or_constructive": constructive_cases,
            "negative_or_ambiguous": negative_cases,
        },
        "research_calibration_human_rubric_must_be_frozen_before_ai": True,
        "research_calibration_incomplete_or_unreviewed_case_result": "NOT_APPLICABLE_FAIL_CLOSED",
        "research_calibration_required_pass_rate": 1.0,
        "research_calibration_separated_from_future_performance": True,
        "blinded_course_gold_classification": blinded_gold["classification"],
        "blinded_course_gold_required": True,
        "blinded_course_gold_minimum_cases": blinded_minimum_cases,
        "blinded_course_gold_minimum_distinct_objective_strata": blinded_minimum_strata,
        "blinded_course_gold_required_atomic_assertion_agreement_rate": blinded_atomic_rate,
        "blinded_course_gold_required_material_permission_agreement_rate": blinded_permission_rate,
        "blinded_course_gold_sampling_and_labels_frozen_before_ai": True,
        "blinded_course_gold_identity_future_old_ai_hidden": True,
        "blinded_course_gold_separated_from_future_performance": True,
        "future_performance_read_before_consistency_ledger_locked": False,
        "future_performance_read_before_full_semantic_ledger_locked": False,
        "future_performance_part_of_repeatability": False,
    }


def _validate_protocol_settings(
    protocol: Mapping[str, Any], schema: Mapping[str, Any]
) -> dict[str, Any]:
    monitoring = protocol.get("monitoring_window") or {}
    if monitoring.get("expected_stocks") != EXPECTED_STOCKS or monitoring.get("expected_stock_days") != EXPECTED_STOCK_DAYS:
        raise FreezeValidationError("V2 monitoring source must be exactly 1,029 stocks / 151,804 stock-days")
    expected_review_points = _positive_integer(
        monitoring.get("expected_v2_review_points"), "monitoring_window.expected_v2_review_points"
    )
    data = protocol.get("data") or {}
    if data.get("pivot_definition") != PIVOT_DEFINITION or data.get("course_l1_l2_status") not in {
        "DISABLED_EXPERIMENTAL_ONLY",
        "DISABLED_NOT_IMPLEMENTED",
    }:
        raise FreezeValidationError("formal V2 must use the disclosed legacy pivot baseline with course L1/L2 disabled")
    if data.get("same_day_new_pivot_control_break") != "FORBIDDEN":
        raise FreezeValidationError("same-day newly confirmed pivot control break must be FORBIDDEN")

    execution = protocol.get("execution") or {}
    model = str(execution.get("model") or "").strip()
    reasoning = str(execution.get("reasoning_effort") or "").strip()
    if not model or not reasoning:
        raise FreezeValidationError("execution model and reasoning_effort must both be frozen")
    if execution.get("case_atomic") is not True or execution.get("consistency_run_isolation") is not True:
        raise FreezeValidationError("V2 execution must be case-atomic with isolated consistency runs")
    _positive_integer(execution.get("technical_max_attempts"), "execution.technical_max_attempts")
    if execution.get("valid_semantic_response_may_be_redrawn") is not False:
        raise FreezeValidationError("a valid semantic response must never be redrawn")
    shard_count = _positive_integer(execution.get("full_shard_count"), "execution.full_shard_count")
    if execution.get("merge_order") != "SOURCE_ORDINAL":
        raise FreezeValidationError("execution.merge_order must be SOURCE_ORDINAL")

    holdout = protocol.get("consistency") or {}
    sample_size = _positive_integer(holdout.get("sample_size"), "consistency.sample_size")
    runs = _positive_integer(holdout.get("runs"), "consistency.runs")
    if runs != 3 or not str(holdout.get("seed") or "").strip():
        raise FreezeValidationError("holdout requires exactly three runs and a non-empty seed")
    if holdout.get("exclude_v1_review_ids") is not True or holdout.get("prefer_stock_disjoint_from_v1") is not True:
        raise FreezeValidationError("holdout must exclude V1 review ids and prefer stock-disjoint cases")
    strata = holdout.get("strata")
    if not isinstance(strata, dict) or not strata:
        raise FreezeValidationError("holdout strata must be complete")
    stratum_total = sum(_positive_integer(value, f"consistency.strata.{key}") for key, value in strata.items())
    if stratum_total != sample_size:
        raise FreezeValidationError(f"holdout strata total {stratum_total} differs from sample_size {sample_size}")
    for key in (
        "schema_causality_evidence_rate",
        "permission_rate",
        "scenario_phase_rate",
        "critical_atom_rate",
        "trade_action_signature_rate",
    ):
        try:
            rate = float(holdout[key])
        except (KeyError, TypeError, ValueError) as exc:
            raise FreezeValidationError(f"holdout rate is missing or invalid: consistency.{key}") from exc
        if not 0.0 <= rate <= 1.0:
            raise FreezeValidationError(f"holdout rate must be within [0,1]: consistency.{key}")
    for key in (
        "minimum_conservative_trade_cases",
        "minimum_trade_routes",
        "minimum_wait_cases",
        "minimum_remove_cases_when_eligible",
    ):
        _positive_integer(holdout.get(key), f"consistency.{key}")

    lock = protocol.get("performance_lock") or {}
    for key in (
        "future_prices_hidden_until_ledger_sha256",
        "identity_hidden_until_ledger_sha256",
        "old_pure_ai_reference_hidden_until_ledger_sha256",
        "full_v3_requires_consistency_pass",
    ):
        if lock.get(key) is not True:
            raise FreezeValidationError(f"performance lock must be true: {key}")
    critical_questions = _validate_critical_question_metadata(schema)
    correctness = _validate_correctness_gate(protocol)
    return {
        "model": model,
        "reasoning_effort": reasoning,
        "holdout": {
            "sample_size": sample_size,
            "runs": runs,
            "seed": str(holdout["seed"]),
            "exclude_v1_review_ids": True,
            "prefer_stock_disjoint_from_v1": True,
            "strata": dict(sorted(strata.items())),
        },
        "sharding": {"full_shard_count": shard_count, "merge_order": "SOURCE_ORDINAL"},
        "expected_v2_review_points": expected_review_points,
        "critical_questions": {
            "count": len(critical_questions),
            "ids": critical_questions,
            "sha256": hashlib.sha256(
                json.dumps(
                    critical_questions,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
        },
        "correctness_gate": correctness,
    }


def _jsonl_rows(path: Path) -> int:
    rows = 0
    with path.open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise FreezeValidationError(f"invalid review point JSONL at line {line_number}: {path}") from exc
            if not isinstance(value, dict):
                raise FreezeValidationError(f"review point line {line_number} is not an object: {path}")
            rows += 1
    return rows


def _validate_source_and_review_points(
    *,
    source_path: Path,
    review_manifest_path: Path,
    expected_review_points: int,
    expected_manifest_version: str = "hybrid-v3-review-points-v2",
) -> tuple[dict[str, Any], dict[str, Any]]:
    source = _read_json(source_path)
    if source.get("stocks") != EXPECTED_STOCKS or source.get("stock_days") != EXPECTED_STOCK_DAYS:
        raise FreezeValidationError("source manifest must contain exactly 1,029 stocks / 151,804 stock-days")
    source_sha = file_sha256(source_path)

    review = _read_json(review_manifest_path)
    if _normalize_version(review.get("manifest_version")) != _normalize_version(expected_manifest_version):
        raise FreezeValidationError(f"review point manifest version must be {expected_manifest_version}")
    if str(review.get("status") or "").upper() != "LOCKED_OUTCOME_BLIND":
        raise FreezeValidationError("V2 review point manifest must be LOCKED_OUTCOME_BLIND")
    required_blind = {
        "outcome_blind": True,
        "identity_visible": False,
        "performance_visible": False,
        "future_data_visible": False,
        "source_order_locked": True,
    }
    for key, expected in required_blind.items():
        if review.get(key) is not expected:
            raise FreezeValidationError(f"V2 review point manifest violates outcome-blind lock: {key}")
    if review.get("outcome_excluded_codes") != []:
        raise FreezeValidationError("outcome-blind review points must explicitly lock an empty outcome_excluded_codes list")
    if review.get("stocks") != EXPECTED_STOCKS or review.get("stock_days_scanned") != EXPECTED_STOCK_DAYS:
        raise FreezeValidationError("review point manifest source scope differs from 1,029 / 151,804")
    if review.get("review_points") != expected_review_points:
        raise FreezeValidationError("review point count differs from frozen V2 protocol")

    source_entry = review.get("source_manifest") or {}
    linked_source = _entry_path(source_entry, source_path.parent, review_manifest_path.parent)
    if linked_source != source_path.resolve() or str(source_entry.get("sha256") or "").lower() != source_sha:
        raise FreezeValidationError("review point manifest does not pin the exact source manifest")

    artifact_entry = review.get("review_points_artifact") or {}
    artifact = _entry_path(artifact_entry, review_manifest_path.parent, review_manifest_path.parent)
    artifact_sha = _assert_sha(
        artifact,
        str(artifact_entry.get("sha256") or ""),
        "outcome-blind V2 review point artifact",
    )
    rows = _jsonl_rows(artifact)
    if rows != expected_review_points or artifact_entry.get("rows") != rows:
        raise FreezeValidationError("review point artifact rows differ from its locked manifest")
    return (
        {"path": str(source_path.resolve()), "sha256": source_sha, "stocks": EXPECTED_STOCKS, "stock_days": EXPECTED_STOCK_DAYS},
        {
            "path": str(review_manifest_path.resolve()),
            "sha256": file_sha256(review_manifest_path),
            "status": "LOCKED_OUTCOME_BLIND",
            "review_points": rows,
            "artifact": {"path": str(artifact), "sha256": artifact_sha},
        },
    )


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    ).hexdigest()


def _required_pre_ai_path(path: Path | None, label: str) -> Path:
    if path is None:
        raise FreezeValidationError(f"pre-AI hash chain path is not configured: {label}")
    resolved = Path(path).resolve()
    if not resolved.is_file():
        raise FreezeValidationError(f"pre-AI hash chain artifact is missing: {label}: {resolved}")
    return resolved


def _validate_self_hash(manifest: Mapping[str, Any], field: str, label: str) -> str:
    supplied = str(manifest.get(field) or "").lower()
    core = dict(manifest)
    core.pop(field, None)
    if not re.fullmatch(r"[0-9a-f]{64}", supplied) or supplied != _canonical_sha256(core):
        raise FreezeValidationError(f"{label} immutable self-hash is missing or changed")
    return supplied


def _validate_pre_ai_hash_chain(inputs: FreezeInputs, settings: Mapping[str, Any]) -> dict[str, Any]:
    """Require correctness, sample, and execution artifacts in one freeze chain."""
    paths = {
        "research_calibration": _required_pre_ai_path(
            inputs.research_calibration_manifest, "research calibration HUMAN_FROZEN manifest"
        ),
        "course_gold_bundle": _required_pre_ai_path(
            inputs.course_gold_bundle_manifest, "blinded course-gold bundle manifest"
        ),
        "course_gold_rubrics": _required_pre_ai_path(
            inputs.course_gold_rubrics_manifest, "blinded course-gold HUMAN_REVIEWED_FROZEN rubrics manifest"
        ),
        "consistency_sample": _required_pre_ai_path(
            inputs.consistency_sample_manifest, "120-case primary/reserve sample manifest"
        ),
        "execution": _required_pre_ai_path(inputs.execution_manifest, "formal execution/run manifest"),
    }
    manifests = {name: _read_json(path) for name, path in paths.items()}

    research = manifests["research_calibration"]
    if research.get("manifest_version") != "hybrid-v2-research-calibration-freeze-v1":
        raise FreezeValidationError("research calibration manifest version is invalid")
    if research.get("status") != "HUMAN_FROZEN" or research.get("rubrics_frozen_before_ai") is not True:
        raise FreezeValidationError("research calibration rubric manifest must be HUMAN_FROZEN before AI")
    research_cases = _positive_integer(research.get("case_count"), "research calibration case_count")
    if research_cases < int(settings["correctness_gate"]["research_calibration_minimum_cases"]):
        raise FreezeValidationError("research calibration has fewer cases than the frozen protocol")
    if research.get("human_frozen_rubric_count") != research_cases:
        raise FreezeValidationError("research calibration HUMAN_FROZEN rubric coverage is incomplete")
    _validate_self_hash(research, "manifest_sha256", "research calibration manifest")

    bundle = manifests["course_gold_bundle"]
    if bundle.get("manifest_version") != inputs.course_gold_bundle_version:
        raise FreezeValidationError("course-gold bundle manifest version is invalid")
    if bundle.get("status") != "DRAFT_UNLABELED" or bundle.get("gold_labels_present") is not False:
        raise FreezeValidationError("course-gold bundle must preserve the original unlabeled freeze")
    _validate_self_hash(bundle, "bundle_manifest_sha256", "course-gold bundle manifest")

    rubrics = manifests["course_gold_rubrics"]
    if rubrics.get("manifest_version") != inputs.course_gold_rubrics_version:
        raise FreezeValidationError("course-gold rubric manifest version is invalid")
    if rubrics.get("status") != "HUMAN_REVIEWED_FROZEN" or rubrics.get("rubrics_frozen_before_ai") is not True:
        raise FreezeValidationError("course-gold rubrics must be HUMAN_REVIEWED_FROZEN before AI")
    gold_cases = _positive_integer(rubrics.get("case_count"), "course-gold rubric case_count")
    if gold_cases < int(settings["correctness_gate"]["blinded_course_gold_minimum_cases"]):
        raise FreezeValidationError("course-gold rubrics have fewer cases than the frozen protocol")
    if rubrics.get("human_frozen_rubric_count") != gold_cases:
        raise FreezeValidationError("course-gold HUMAN_REVIEWED_FROZEN rubric coverage is incomplete")
    if rubrics.get("bundle_manifest_sha256") != file_sha256(paths["course_gold_bundle"]):
        raise FreezeValidationError("course-gold rubrics do not pin the exact unlabeled bundle file")
    _validate_self_hash(rubrics, "manifest_sha256", "course-gold rubric manifest")

    sample = manifests["consistency_sample"]
    if _normalize_version(sample.get(inputs.consistency_plan_version_field)) != _normalize_version(
        inputs.consistency_plan_version
    ):
        raise FreezeValidationError("consistency sample manifest version is invalid")
    if sample.get("status") != inputs.consistency_plan_locked_status:
        raise FreezeValidationError("consistency sample/reserve manifest is not outcome-blind locked")
    sample_size = int(settings["holdout"]["sample_size"])
    if sample.get(inputs.consistency_plan_sample_size_field) != sample_size:
        raise FreezeValidationError("consistency primary sample size differs from the frozen protocol")
    primary = sample.get("primary") or {}
    reserves = sample.get("reserve_blocks")
    if primary.get("count") != sample_size or not isinstance(reserves, list) or not reserves:
        raise FreezeValidationError("consistency sample must pin the complete primary and reserve blocks")
    _validate_self_hash(sample, "plan_sha256", "consistency sample manifest")

    execution = manifests["execution"]
    if execution.get("manifest_version") != "hybrid-v3-execution-manifest-v2":
        raise FreezeValidationError("formal execution manifest version is invalid")
    if execution.get("status") != "LOCKED_BEFORE_AI":
        raise FreezeValidationError("formal execution manifest must be LOCKED_BEFORE_AI")
    if execution.get("model") != settings["model"] or execution.get("reasoning_effort") != settings["reasoning_effort"]:
        raise FreezeValidationError("formal execution model/reasoning differs from protocol")
    linked_hashes = {
        "review_point_manifest_sha256": file_sha256(inputs.v2_review_point_manifest.resolve()),
        "research_calibration_manifest_sha256": file_sha256(paths["research_calibration"]),
        "course_gold_bundle_manifest_sha256": file_sha256(paths["course_gold_bundle"]),
        "course_gold_rubrics_manifest_sha256": file_sha256(paths["course_gold_rubrics"]),
        "consistency_sample_manifest_sha256": file_sha256(paths["consistency_sample"]),
        "protocol_sha256": file_sha256(inputs.root.resolve() / inputs.protocol_relative_path),
        "schema_sha256": file_sha256(inputs.root.resolve() / "config/hybrid_atomic_semantics_v2.schema.json"),
        "prompt_sha256": file_sha256(inputs.root.resolve() / "config/hybrid_semantic_prompt_v2.md"),
        "policy_sha256": file_sha256(inputs.root.resolve() / "scripts/hybrid_v3_atomic_policy_v2.py"),
        "runner_sha256": file_sha256(inputs.root.resolve() / "scripts/hybrid_v3_atomic_runner_v2.py"),
        "sharding_sha256": file_sha256(inputs.root.resolve() / "scripts/hybrid_v3_sharding_v2.py"),
        "reviewer_sha256": file_sha256(inputs.root.resolve() / "scripts/hybrid_v3_codex_reviewer_v2.py"),
    }
    for field, expected in linked_hashes.items():
        if str(execution.get(field) or "").lower() != expected:
            raise FreezeValidationError(f"formal execution manifest does not pin {field}")
    _validate_self_hash(execution, "manifest_sha256", "formal execution manifest")

    return {
        name: {"path": str(path), "sha256": file_sha256(path), "status": manifests[name].get("status")}
        for name, path in paths.items()
    }


def validate_freeze(inputs: FreezeInputs) -> dict[str, Any]:
    root = inputs.root.resolve()
    v1_protocol = _validate_v1_manifest(
        path=inputs.v1_protocol_manifest.resolve(),
        expected_manifest_sha256=inputs.expected_v1_protocol_manifest_sha256,
        root=root,
        label="V1 protocol freeze manifest",
    )
    v1_execution = _validate_v1_manifest(
        path=inputs.v1_execution_manifest.resolve(),
        expected_manifest_sha256=inputs.expected_v1_execution_manifest_sha256,
        root=root,
        label="V1 execution freeze manifest",
    )
    strategies = _validate_strategy_coverage(inputs.v1_protocol_manifest.resolve())
    components = [_component_metadata(root, spec) for spec in inputs.components]
    protocol_path = root / inputs.protocol_relative_path
    protocol = _read_json(protocol_path)
    schema = _read_json(root / "config/hybrid_atomic_semantics_v2.schema.json")
    settings = _validate_protocol_settings(protocol, schema)
    source, review_points = _validate_source_and_review_points(
        source_path=inputs.source_manifest.resolve(),
        review_manifest_path=inputs.v2_review_point_manifest.resolve(),
        expected_review_points=settings["expected_v2_review_points"],
        expected_manifest_version=inputs.review_point_manifest_version,
    )
    pre_ai_hash_chain = _validate_pre_ai_hash_chain(inputs, settings)
    return {
        "freeze_version": FREEZE_VERSION,
        "status": FREEZE_STATUS,
        "protocol_version": inputs.protocol_version,
        "pivot_definition": PIVOT_DEFINITION,
        "strategy_rules_unchanged": True,
        "protected_strategy_files": strategies,
        "v1_frozen_manifests": {"protocol": v1_protocol, "execution": v1_execution},
        "v2_components": components,
        "source": source,
        "review_points": review_points,
        "pre_ai_hash_chain": pre_ai_hash_chain,
        "execution_contract": settings,
        "identity_visible": False,
        "performance_visible": False,
        "future_data_visible": False,
    }


def _inspect_component_readiness(root: Path, spec: ComponentSpec) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Inspect the same metadata freeze uses without failing at the first draft."""
    path = (root / spec.relative_path).resolve()
    row: dict[str, Any] = {
        "name": spec.name,
        "relative_path": spec.relative_path,
        "kind": spec.kind,
        "expected_version": spec.expected_version,
        "exists": path.is_file(),
        "version": None,
        "status": None,
        "version_parseable": False,
        "status_parseable": False,
        "version_matches": False,
        "status_final": False,
        "sha256": file_sha256(path) if path.is_file() else None,
    }
    issues: list[dict[str, str]] = []

    def issue(code: str, message: str, action: str) -> None:
        issues.append({"code": code, "scope": f"component:{spec.name}", "message": message, "action": action})

    if not path.is_file():
        issue("COMPONENT_MISSING", f"required component is missing: {spec.relative_path}", "Create and validate the component before freeze.")
        return row, issues
    try:
        if spec.kind == "markdown":
            text = path.read_text(encoding="utf-8-sig")
            version_match = _MARKDOWN_VERSION.search(text)
            status_match = _MARKDOWN_STATUS.search(text)
            version = version_match.group(1) if version_match else None
            status = status_match.group(1) if status_match else None
        elif spec.kind == "json":
            value = _read_json(path)
            version = _first_value(value, spec.version_keys)
            status = _first_value(value, spec.status_keys)
        elif spec.kind == "python":
            value = _python_constants(path)
            version = _first_value(value, spec.version_keys)
            status = _first_value(value, spec.status_keys)
        else:
            raise FreezeValidationError(f"unsupported component kind: {spec.kind}")
    except Exception as exc:
        issue("COMPONENT_METADATA_PARSE_ERROR", str(exc), "Repair the component metadata so the freeze parser can read it.")
        return row, issues
    normalized_version = _normalize_version(version) if version is not None else ""
    normalized_status = str(status or "").strip().upper()
    row.update(
        {
            "version": normalized_version or None,
            "status": normalized_status or None,
            "version_parseable": bool(normalized_version),
            "status_parseable": bool(normalized_status),
            "version_matches": normalized_version == spec.expected_version,
            "status_final": normalized_status == "FINAL",
        }
    )
    if not normalized_version:
        issue(
            "COMPONENT_VERSION_UNPARSEABLE",
            f"freeze cannot parse a version from {spec.relative_path}",
            f"Declare one of the supported version keys or markdown headers: {spec.version_keys or ('版本', 'version')}.",
        )
    elif normalized_version != spec.expected_version:
        issue(
            "COMPONENT_VERSION_NOT_FINAL_TARGET",
            f"parsed version {normalized_version!r}; freeze expects {spec.expected_version!r}",
            "Complete validation, then update the declared version to the exact final target.",
        )
    if not normalized_status:
        issue(
            "COMPONENT_STATUS_UNPARSEABLE",
            f"freeze cannot parse a status from {spec.relative_path}",
            f"Declare one of the supported status keys or markdown headers: {spec.status_keys or ('狀態', 'status')}.",
        )
    elif normalized_status != "FINAL":
        issue(
            "COMPONENT_STATUS_NOT_FINAL",
            f"parsed status {normalized_status!r}; freeze requires 'FINAL'",
            "Do not rename to FINAL until that component's tests and acceptance gates pass.",
        )
    return row, issues


def pre_freeze_readiness_audit(inputs: FreezeInputs) -> dict[str, Any]:
    """Collect every pre-freeze blocker without writing or changing any status."""
    root = inputs.root.resolve()
    issues: list[dict[str, str]] = []
    checks: dict[str, Any] = {}

    def issue(code: str, scope: str, message: str, action: str) -> None:
        issues.append({"code": code, "scope": scope, "message": message, "action": action})

    v1_rows: dict[str, Any] = {}
    for label, path, expected in (
        ("protocol", inputs.v1_protocol_manifest.resolve(), inputs.expected_v1_protocol_manifest_sha256),
        ("execution", inputs.v1_execution_manifest.resolve(), inputs.expected_v1_execution_manifest_sha256),
    ):
        try:
            v1_rows[label] = _validate_v1_manifest(
                path=path, expected_manifest_sha256=expected, root=root, label=f"V1 {label} manifest"
            )
        except Exception as exc:
            issue(
                "V1_FROZEN_MANIFEST_INVALID",
                f"v1:{label}",
                str(exc),
                "Restore the exact frozen V1 manifest and every protected hash entry.",
            )
    try:
        strategies = _validate_strategy_coverage(inputs.v1_protocol_manifest.resolve())
        checks["protected_strategy_files"] = {"ready": True, "files": strategies}
    except Exception as exc:
        checks["protected_strategy_files"] = {"ready": False, "error": str(exc)}
        issue(
            "PROTECTED_STRATEGY_COVERAGE_INVALID",
            "strategy",
            str(exc),
            "Restore V1 protection for every frozen V1/V2/V3 strategy file; do not edit strategy rules here.",
        )
    checks["v1_frozen_manifests"] = v1_rows

    component_rows: list[dict[str, Any]] = []
    for spec in inputs.components:
        row, component_issues = _inspect_component_readiness(root, spec)
        component_rows.append(row)
        issues.extend(component_issues)
    checks["components"] = {
        "expected": len(inputs.components),
        "inspected": len(component_rows),
        "metadata_parseable": sum(
            bool(row["version_parseable"] and row["status_parseable"]) for row in component_rows
        ),
        "final": sum(bool(row["version_matches"] and row["status_final"]) for row in component_rows),
        "items": component_rows,
    }

    source_summary: dict[str, Any] = {"path": str(inputs.source_manifest.resolve()), "exists": False}
    source: dict[str, Any] | None = None
    if inputs.source_manifest.is_file():
        try:
            source = _read_json(inputs.source_manifest.resolve())
            source_summary.update(
                {
                    "exists": True,
                    "sha256": file_sha256(inputs.source_manifest.resolve()),
                    "stocks": source.get("stocks"),
                    "stock_days": source.get("stock_days"),
                    "scope_ready": source.get("stocks") == EXPECTED_STOCKS
                    and source.get("stock_days") == EXPECTED_STOCK_DAYS,
                }
            )
            if not source_summary["scope_ready"]:
                issue(
                    "SOURCE_SCOPE_INVALID",
                    "source_manifest",
                    "source manifest does not contain exactly 1,029 stocks / 151,804 stock-days",
                    "Regenerate or restore the frozen outcome-blind source manifest.",
                )
        except Exception as exc:
            source_summary["error"] = str(exc)
            issue("SOURCE_MANIFEST_INVALID", "source_manifest", str(exc), "Repair the source manifest before building review points.")
    else:
        issue(
            "SOURCE_MANIFEST_MISSING",
            "source_manifest",
            f"source manifest is missing: {inputs.source_manifest}",
            "Restore the 1,029-stock / 151,804-stock-day source manifest.",
        )
    checks["source_manifest"] = source_summary

    protocol_path = root / inputs.protocol_relative_path
    schema_path = root / "config/hybrid_atomic_semantics_v2.schema.json"
    protocol_summary: dict[str, Any] = {"path": str(protocol_path.resolve())}
    protocol: dict[str, Any] | None = None
    protocol_settings: dict[str, Any] | None = None
    try:
        protocol = _read_json(protocol_path)
        monitoring = protocol.get("monitoring_window") or {}
        expected_review_points = monitoring.get("expected_v2_review_points")
        update_policy = monitoring.get("expected_v2_review_points_policy")
        protocol_summary.update(
            {
                "expected_v2_review_points": expected_review_points,
                "expected_v2_review_points_policy": update_policy,
                "count_locked": isinstance(expected_review_points, int)
                and not isinstance(expected_review_points, bool)
                and expected_review_points > 0,
            }
        )
        if not protocol_summary["count_locked"]:
            issue(
                "EXPECTED_V2_REVIEW_POINTS_UNSET",
                "protocol",
                "monitoring_window.expected_v2_review_points is not a positive frozen integer",
                "After the outcome-blind builder emits review_point_manifest.json, copy its review_points count once, then freeze the protocol before any AI run.",
            )
        if update_policy != "SET_FROM_OUTCOME_BLIND_DAILY_BUILDER_BEFORE_FORMAL_AI":
            issue(
                "EXPECTED_REVIEW_POINT_UPDATE_POLICY_INVALID",
                "protocol",
                f"unexpected count update policy: {update_policy!r}",
                "Require SET_FROM_OUTCOME_BLIND_DAILY_BUILDER_BEFORE_FORMAL_AI.",
            )
        # Probe the remaining protocol contract independently of the known
        # count placeholder, so another blocker cannot hide behind null.
        probe = json.loads(json.dumps(protocol))
        probe.setdefault("monitoring_window", {})["expected_v2_review_points"] = (
            expected_review_points if protocol_summary["count_locked"] else 1
        )
        protocol_settings = _validate_protocol_settings(probe, _read_json(schema_path))
        protocol_summary["remaining_contract_ready"] = True
    except Exception as exc:
        protocol_summary["remaining_contract_ready"] = False
        protocol_summary["error"] = str(exc)
        issue(
            "PROTOCOL_CONTRACT_INVALID",
            "protocol",
            str(exc),
            "Resolve this protocol/schema contract error before changing either status to FINAL.",
        )
    checks["protocol"] = protocol_summary

    builder_path = root / inputs.builder_relative_path
    builder_constants = _python_constants(builder_path) if builder_path.is_file() else {}
    expected_manifest_name = "review_point_manifest.json"
    builder_contract = {
        "path": str(builder_path.resolve()),
        "manifest_filename": builder_constants.get("FORMAL_SOURCE_MANIFEST_FILE"),
        "manifest_filename_matches_consumer": builder_constants.get("FORMAL_SOURCE_MANIFEST_FILE")
        == expected_manifest_name,
        "builder_version": builder_constants.get("BUILDER_VERSION"),
        "builder_status": builder_constants.get("BUILDER_STATUS"),
        "consumer_manifest_path": str(inputs.v2_review_point_manifest.resolve()),
    }
    if not builder_contract["manifest_filename_matches_consumer"] or inputs.v2_review_point_manifest.name != expected_manifest_name:
        issue(
            "REVIEW_POINT_MANIFEST_PATH_CONTRACT_MISMATCH",
            "review_points",
            "builder and freeze consumer do not agree on review_point_manifest.json",
            "Align the builder filename and FreezeInputs path before producing the full artifact.",
        )
    checks["review_point_builder_contract"] = builder_contract

    # This is an explicit, outcome-blind dependency graph.  In particular the
    # review-point universe does not depend on the protocol's eventual frozen
    # row count, while every AI-facing execution does depend on the final
    # master freeze.  Keeping those two facts machine-visible prevents the
    # apparent protocol-count cycle from being "solved" by running AI early.
    lifecycle_dependencies = {
        "SOURCE_AND_BUILDER_PREREQUISITES": [],
        "LOCKED_REVIEW_POINT_UNIVERSE": ["SOURCE_AND_BUILDER_PREREQUISITES"],
        "PROTOCOL_COUNT_LOCK_AND_COMPONENT_FINALIZATION": ["LOCKED_REVIEW_POINT_UNIVERSE"],
        "HUMAN_FROZEN_RESEARCH_CALIBRATION": ["PROTOCOL_COUNT_LOCK_AND_COMPONENT_FINALIZATION"],
        "UNLABELED_COURSE_GOLD_BUNDLE": ["PROTOCOL_COUNT_LOCK_AND_COMPONENT_FINALIZATION"],
        "HUMAN_FROZEN_COURSE_GOLD_RUBRICS": ["UNLABELED_COURSE_GOLD_BUNDLE"],
        "LOCKED_120_CASE_PRIMARY_AND_RESERVE_PLAN": ["PROTOCOL_COUNT_LOCK_AND_COMPONENT_FINALIZATION"],
        "LOCKED_EXECUTION_MANIFEST": [
            "HUMAN_FROZEN_RESEARCH_CALIBRATION",
            "HUMAN_FROZEN_COURSE_GOLD_RUBRICS",
            "LOCKED_120_CASE_PRIMARY_AND_RESERVE_PLAN",
        ],
        "FORMAL_MASTER_FREEZE": ["LOCKED_EXECUTION_MANIFEST"],
        "AI_EXECUTION": ["FORMAL_MASTER_FREEZE"],
    }
    resolved: set[str] = set()
    remaining = dict(lifecycle_dependencies)
    lifecycle_order: list[str] = []
    while remaining:
        available = sorted(name for name, dependencies in remaining.items() if set(dependencies) <= resolved)
        if not available:
            break
        for name in available:
            lifecycle_order.append(name)
            resolved.add(name)
            remaining.pop(name)
    checks["lifecycle"] = {
        "circular_dependency": bool(remaining),
        "topological_order": lifecycle_order,
        "unresolved_cycle_nodes": sorted(remaining),
        "review_point_builder_depends_on_protocol_frozen_count": False,
        "formal_ai_depends_on_master_freeze": True,
        "master_freeze_depends_on_ai_outputs": False,
    }
    if remaining:
        issue(
            "PRE_AI_LIFECYCLE_CYCLE",
            "lifecycle",
            f"pre-AI lifecycle contains a dependency cycle: {sorted(remaining)}",
            "Remove AI outputs and the protocol review-point count from review-universe prerequisites.",
        )

    calibration_source = root / "config/enlightenment_ai_calibration_cases_v1.json"
    if calibration_source.is_file():
        try:
            calibration_cases = _read_json(calibration_source)
            declared_cases = calibration_cases.get("cases") or []
            balance = ((calibration_cases.get("rules") or {}).get("required_balance_before_freeze") or {})
            minimum_total = int(balance.get("minimum_total") or 0)
            calibration_summary = {
                "path": str(calibration_source.resolve()),
                "declared_cases": len(declared_cases),
                "minimum_total": minimum_total,
                "capacity_ready": minimum_total >= 20 and len(declared_cases) >= minimum_total,
            }
            checks["research_calibration_source"] = calibration_summary
            if not calibration_summary["capacity_ready"]:
                issue(
                    "RESEARCH_CALIBRATION_CASE_CAPACITY_BELOW_PROTOCOL",
                    "research_calibration",
                    f"only {len(declared_cases)} declared calibration cases are available; protocol requires at least {minimum_total or 20}",
                    "A human must add and freeze at least 10 constructive and 10 negative/ambiguous causal cases before formal AI.",
                )
        except Exception as exc:
            checks["research_calibration_source"] = {
                "path": str(calibration_source.resolve()), "capacity_ready": False, "error": str(exc)
            }
            issue(
                "RESEARCH_CALIBRATION_SOURCE_INVALID",
                "research_calibration",
                str(exc),
                "Repair the human calibration source before freezing its rubrics.",
            )

    existing_calibration = (
        root
        / "reports/course_backtest/2026-09-08/hybrid_v2_research_calibration_v1/immutable_manifest.json"
    )
    if existing_calibration.is_file():
        try:
            manifest = _read_json(existing_calibration)
            supplied = str(manifest.get("manifest_sha256") or "").lower()
            core = dict(manifest)
            core.pop("manifest_sha256", None)
            checks["existing_research_calibration_artifact"] = {
                "path": str(existing_calibration.resolve()),
                "exists": True,
                "status": manifest.get("status"),
                "case_count": manifest.get("case_count"),
                "human_frozen_rubric_count": manifest.get("human_frozen_rubric_count"),
                "immutable_self_hash_valid": bool(
                    re.fullmatch(r"[0-9a-f]{64}", supplied) and supplied == _canonical_sha256(core)
                ),
                "eligible_as_human_frozen_manifest": False,
            }
        except Exception as exc:
            checks["existing_research_calibration_artifact"] = {
                "path": str(existing_calibration.resolve()), "exists": True, "error": str(exc)
            }

    review_summary: dict[str, Any] = {
        "path": str(inputs.v2_review_point_manifest.resolve()),
        "exists": inputs.v2_review_point_manifest.is_file(),
    }
    review_count: int | None = None
    if not inputs.v2_review_point_manifest.is_file():
        issue(
            "REVIEW_POINT_MANIFEST_MISSING",
            "review_points",
            f"review point manifest is missing: {inputs.v2_review_point_manifest}",
            "Run the outcome-blind full builder only after its DRAFT readiness gates pass.",
        )
    else:
        try:
            review = _read_json(inputs.v2_review_point_manifest.resolve())
            review_summary.update(
                {
                    "manifest_version": review.get("manifest_version"),
                    "status": review.get("status"),
                    "review_points": review.get("review_points"),
                    "source_manifest": review.get("source_manifest"),
                    "review_points_artifact": review.get("review_points_artifact"),
                }
            )
            review_count = review.get("review_points") if isinstance(review.get("review_points"), int) else None
            if _normalize_version(review.get("manifest_version")) != _normalize_version(
                inputs.review_point_manifest_version
            ):
                issue(
                    "REVIEW_POINT_MANIFEST_VERSION_INVALID",
                    "review_points",
                    f"review point manifest version is not {inputs.review_point_manifest_version}",
                    "Regenerate it with the current builder contract.",
                )
            if str(review.get("status") or "").upper() != "LOCKED_OUTCOME_BLIND":
                issue("REVIEW_POINT_MANIFEST_NOT_LOCKED", "review_points", f"review point status is {review.get('status')!r}", "Only a FINAL builder may publish LOCKED_OUTCOME_BLIND.")
            source_entry = review.get("source_manifest") or {}
            linked_source = _entry_path(source_entry, inputs.source_manifest.parent, inputs.v2_review_point_manifest.parent)
            if linked_source != inputs.source_manifest.resolve() or source_entry.get("sha256") != source_summary.get("sha256"):
                issue("REVIEW_POINT_SOURCE_LINK_INVALID", "review_points", "review manifest does not pin the exact configured source path/hash", "Regenerate the review manifest against the configured source manifest.")
            artifact_entry = review.get("review_points_artifact") or {}
            artifact = _entry_path(artifact_entry, inputs.v2_review_point_manifest.parent, inputs.v2_review_point_manifest.parent)
            if not artifact.is_file() or file_sha256(artifact) != artifact_entry.get("sha256"):
                issue("REVIEW_POINT_ARTIFACT_INVALID", "review_points", "review point JSONL is missing or its hash changed", "Restore or rebuild the exact anonymous review-point artifact.")
            elif _jsonl_rows(artifact) != artifact_entry.get("rows") or artifact_entry.get("rows") != review_count:
                issue("REVIEW_POINT_ARTIFACT_ROWS_INVALID", "review_points", "artifact row count differs from its manifest", "Rebuild and atomically republish the review-point bundle.")
        except Exception as exc:
            review_summary["error"] = str(exc)
            issue("REVIEW_POINT_MANIFEST_INVALID", "review_points", str(exc), "Repair or regenerate the review-point manifest.")
    checks["review_points"] = review_summary

    if protocol is not None:
        locked_count = (protocol.get("monitoring_window") or {}).get("expected_v2_review_points")
        if review_count is not None and locked_count != review_count:
            issue(
                "PROTOCOL_REVIEW_POINT_COUNT_MISMATCH",
                "protocol",
                f"protocol count {locked_count!r} differs from builder manifest count {review_count}",
                "Set the protocol count exactly once from the outcome-blind builder manifest before AI.",
            )

    chain_paths = {
        "research_calibration": inputs.research_calibration_manifest,
        "course_gold_bundle": inputs.course_gold_bundle_manifest,
        "course_gold_rubrics": inputs.course_gold_rubrics_manifest,
        "consistency_sample": inputs.consistency_sample_manifest,
        "execution": inputs.execution_manifest,
    }
    chain_summary: dict[str, Any] = {}
    chain_missing = False
    for name, configured in chain_paths.items():
        path = Path(configured).resolve() if configured is not None else None
        exists = bool(path and path.is_file())
        chain_summary[name] = {
            "path": str(path) if path else None,
            "exists": exists,
            "sha256": file_sha256(path) if exists and path is not None else None,
        }
        if not exists:
            chain_missing = True
            issue(
                "PRE_AI_HASH_CHAIN_ARTIFACT_MISSING",
                f"pre_ai_hash_chain:{name}",
                f"required pre-AI artifact is missing or unconfigured: {path}",
                "Produce and immutably hash this artifact before any formal AI execution.",
            )
    if not chain_missing and protocol_settings is not None and inputs.v2_review_point_manifest.is_file():
        try:
            chain_summary["validated"] = _validate_pre_ai_hash_chain(inputs, protocol_settings)
            chain_summary["ready"] = True
        except Exception as exc:
            chain_summary["ready"] = False
            chain_summary["error"] = str(exc)
            issue(
                "PRE_AI_HASH_CHAIN_INVALID",
                "pre_ai_hash_chain",
                str(exc),
                "Repair the linked HUMAN_FROZEN, course-gold, sample/reserve, and execution manifests.",
            )
    else:
        chain_summary["ready"] = False
    checks["pre_ai_hash_chain"] = chain_summary

    issues = sorted(issues, key=lambda row: (row["scope"], row["code"], row["message"]))
    for row in issues:
        row.setdefault("priority", "P0")
    return {
        "audit_version": READINESS_AUDIT_VERSION,
        "status": "READY_FOR_FORMAL_FREEZE" if not issues else "BLOCKED_PRE_FREEZE",
        "ready": not issues,
        "writes_performed": False,
        "strategies_modified": False,
        "components_expected": len(inputs.components),
        "checks": checks,
        "unresolved_count": len(issues),
        "unresolved": issues,
    }


def _manifest_bytes(payload: Mapping[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")


def _write_immutable(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() == payload:
            return
        raise ImmutableFreezeError(f"different immutable freeze manifest already exists: {path}")
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
    except FileExistsError:
        if path.read_bytes() == payload:
            return
        raise ImmutableFreezeError(f"different immutable freeze manifest won creation race: {path}")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def freeze(inputs: FreezeInputs) -> dict[str, Any]:
    payload = validate_freeze(inputs)
    _write_immutable(inputs.output.resolve(), _manifest_bytes(payload))
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--readiness-audit", action="store_true")
    args = parser.parse_args(argv)
    inputs = default_inputs(args.root)
    if args.validate_only or args.readiness_audit:
        audit = pre_freeze_readiness_audit(inputs)
        if args.readiness_audit or not audit["ready"]:
            print(json.dumps(audit, ensure_ascii=False, indent=2))
            return 0 if audit["ready"] else 2
    payload = validate_freeze(inputs) if args.validate_only else freeze(inputs)
    print(
        json.dumps(
            {
                "freeze_version": payload["freeze_version"],
                "status": payload["status"],
                "components": len(payload["v2_components"]),
                "review_points": payload["review_points"]["review_points"],
                "output": None if args.validate_only else str(inputs.output),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DEFAULT_COMPONENTS",
    "FREEZE_STATUS",
    "FREEZE_VERSION",
    "FreezeInputs",
    "FreezeValidationError",
    "ImmutableFreezeError",
    "PIVOT_DEFINITION",
    "READINESS_AUDIT_VERSION",
    "default_inputs",
    "file_sha256",
    "freeze",
    "pre_freeze_readiness_audit",
    "validate_freeze",
]
