"""Fail-closed wrapper for the frozen MATURE S1 repeatability evaluator.

V2 deliberately leaves the V1 metrics, atomic policy, reducer, prompt, schema,
and frozen artifacts untouched.  It validates the sample and the complete
runner/attempt provenance first, calls V1 only after those checks pass, and
then adds raw V3 permission/route diagnostics to the V1 report.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import date
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from . import hybrid_v3_mature_s1_consistency_v1 as v1
    from . import hybrid_v3_codex_reviewer_v3 as reviewer_v3
    from .hybrid_v3_atomic_runner_v2 import (
        ExecutionIntegrityError,
        RUNNER_VERSION,
        _validate_merge_envelope,
    )
    from .hybrid_v3_sharding_v2 import (
        build_run_case_key,
        canonical_json_bytes,
        canonical_sha256,
        file_sha256,
    )
except ImportError:  # pragma: no cover - direct script execution
    from scripts import hybrid_v3_mature_s1_consistency_v1 as v1
    from scripts import hybrid_v3_codex_reviewer_v3 as reviewer_v3
    from scripts.hybrid_v3_atomic_runner_v2 import (
        ExecutionIntegrityError,
        RUNNER_VERSION,
        _validate_merge_envelope,
    )
    from scripts.hybrid_v3_sharding_v2 import (
        build_run_case_key,
        canonical_json_bytes,
        canonical_sha256,
        file_sha256,
    )


REPORT_VERSION = "hybrid-v3-mature-s1-consistency-v2"
BASE_REPORT_VERSION = v1.REPORT_VERSION
EXPECTED_CASES = v1.EXPECTED_CASES
EXPECTED_RUNS = v1.EXPECTED_RUNS

_SAMPLING_PACKET_KEYS = frozenset(
    {"eligible_sampling_strata", "primary_sampling_focus", "sampling_stratum", "sampling_focus"}
)
_ENVELOPE_FIELDS = frozenset(
    {
        "runner_version",
        "status",
        "run_number",
        "source_ordinal",
        "review_id",
        "case_key",
        "run_case_key",
        "packet_sha256",
        "source_manifest_sha256",
        "protocol_sha256",
        "prompt_sha256",
        "schema_sha256",
        "execution_contract_sha256",
        "model",
        "reasoning_effort",
        "successful_attempt",
        "output_sha256",
        "reviewer_audit_sha256",
        "reviewer_audit",
        "output",
    }
)
_VALIDATED_ATTEMPT_FIELDS = frozenset(
    {
        "runner_version",
        "case_key",
        "run_case_key",
        "run_number",
        "attempt",
        "status",
        "technical_failure",
        "started_at",
        "finished_at",
        "elapsed_seconds",
        "output_sha256",
        "reviewer_audit_sha256",
        "reviewer_audit",
        "output",
    }
)


class MatureS1ConsistencyV2Error(ValueError):
    """A sample, run envelope, or attempt artifact is not exactly frozen."""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MatureS1ConsistencyV2Error(f"cannot read JSON artifact: {path}") from exc
    if not isinstance(value, dict):
        raise MatureS1ConsistencyV2Error(f"expected JSON object: {path}")
    return value


def _walk_keys(value: Any):
    if isinstance(value, Mapping):
        for key, nested in value.items():
            yield str(key)
            yield from _walk_keys(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk_keys(nested)


def _reviewer_code_sha256(execution_freeze: Path) -> str:
    freeze = _read_json(execution_freeze)
    rows = freeze.get("v2_components")
    if not isinstance(rows, list):
        raise MatureS1ConsistencyV2Error("execution freeze has no v2_components")
    matches = [row for row in rows if isinstance(row, dict) and row.get("name") == "reviewer"]
    if len(matches) != 1:
        raise MatureS1ConsistencyV2Error("execution freeze must contain exactly one reviewer component")
    row = matches[0]
    reviewer_path = Path(reviewer_v3.__file__).resolve()
    frozen_path = (v1.ROOT / str(row.get("relative_path") or "")).resolve()
    expected_sha = str(row.get("sha256") or "")
    if (
        row.get("status") != "FINAL"
        or frozen_path != reviewer_path
        or file_sha256(reviewer_path) != expected_sha
    ):
        raise MatureS1ConsistencyV2Error("reviewer V3 component identity differs from the freeze")
    return expected_sha


def validate_sample_invariants(
    records: Sequence[Mapping[str, Any]],
    stage: Mapping[str, Any],
    packet_manifest: Mapping[str, Any],
) -> dict[str, Any]:
    """Independently recompute every frozen S1 sample constraint."""

    sample = stage.get("consistency_sample")
    if not isinstance(sample, Mapping):
        raise MatureS1ConsistencyV2Error("stage has no consistency_sample")
    if int(sample.get("cases", -1)) != EXPECTED_CASES or int(sample.get("runs", -1)) != EXPECTED_RUNS:
        raise MatureS1ConsistencyV2Error("stage case/run count differs from the frozen S1 design")
    if sample.get("one_case_per_anonymous_stock") is not True:
        raise MatureS1ConsistencyV2Error("one-case-per-anonymous-stock is not frozen true")

    focus_order = list(sample.get("focus_order") or [])
    quota_value = sample.get("quotas")
    if not isinstance(quota_value, Mapping):
        raise MatureS1ConsistencyV2Error("stage has no focus quotas")
    quotas = {str(key): int(value) for key, value in quota_value.items()}
    if list(quotas) != focus_order or sum(quotas.values()) != EXPECTED_CASES:
        raise MatureS1ConsistencyV2Error("stage focus quotas do not exactly cover 36 cases")

    mapping = packet_manifest.get("mapping")
    if not isinstance(mapping, list) or len(mapping) != EXPECTED_CASES or len(records) != EXPECTED_CASES:
        raise MatureS1ConsistencyV2Error("sample does not contain exactly 36 records and mappings")
    for key, expected in {
        "sampling_metadata_inside_packet": False,
        "identity_visible_inside_packet": False,
        "future_or_performance_visible_inside_packet": False,
    }.items():
        if packet_manifest.get(key) is not expected:
            raise MatureS1ConsistencyV2Error(f"packet manifest changed causal flag: {key}")

    seen_reviews: set[str] = set()
    seen_stocks: set[str] = set()
    focus_counts: Counter[str] = Counter()
    month_counts: Counter[str] = Counter()
    for index, (record, mapped) in enumerate(zip(records, mapping)):
        if not isinstance(mapped, Mapping) or mapped.get("selected_ordinal") != index:
            raise MatureS1ConsistencyV2Error("external sample mapping order is not exact")
        packet = record.get("packet")
        if not isinstance(packet, Mapping):
            raise MatureS1ConsistencyV2Error("assigned sample record has no packet")
        review_id = str(record.get("review_id") or "")
        stock_id = str(record.get("anonymous_stock_id") or "")
        if not review_id or review_id in seen_reviews:
            raise MatureS1ConsistencyV2Error("sample review IDs are empty or duplicated")
        if not stock_id or stock_id in seen_stocks:
            raise MatureS1ConsistencyV2Error("sample repeats an anonymous stock")
        seen_reviews.add(review_id)
        seen_stocks.add(stock_id)
        if (
            mapped.get("review_id") != review_id
            or mapped.get("anonymous_stock_id") != stock_id
            or packet.get("review_id") != review_id
            or packet.get("anonymous_stock_id") != stock_id
            or mapped.get("packet_sha256") != record.get("packet_sha256")
            or canonical_sha256(dict(packet)) != record.get("packet_sha256")
        ):
            raise MatureS1ConsistencyV2Error(f"sample mapping/packet identity differs at ordinal {index}")
        eligible = mapped.get("eligible_stage_focuses")
        focus = str(mapped.get("sampling_focus") or "")
        if (
            not isinstance(eligible, list)
            or not eligible
            or len(eligible) != len(set(eligible))
            or focus not in eligible
            or focus not in quotas
        ):
            raise MatureS1ConsistencyV2Error(f"sample focus is not eligible at ordinal {index}")
        focus_counts[focus] += 1
        if _SAMPLING_PACKET_KEYS.intersection(_walk_keys(packet)):
            raise MatureS1ConsistencyV2Error(f"sampling metadata entered AI packet at ordinal {index}")
        as_of = packet.get("as_of")
        try:
            parsed = date.fromisoformat(str(as_of))
        except ValueError as exc:
            raise MatureS1ConsistencyV2Error(f"sample as_of is not an ISO date at ordinal {index}") from exc
        if parsed.isoformat() != as_of:
            raise MatureS1ConsistencyV2Error(f"sample as_of is not canonical at ordinal {index}")
        month_counts[parsed.strftime("%Y-%m")] += 1

    if dict(focus_counts) != quotas:
        raise MatureS1ConsistencyV2Error(
            f"sample focus quotas differ: actual={dict(focus_counts)} expected={quotas}"
        )
    minimum_months = int(sample.get("minimum_distinct_months", -1))
    maximum_per_month = int(sample.get("maximum_cases_per_month", -1))
    if len(month_counts) < minimum_months:
        raise MatureS1ConsistencyV2Error("sample has insufficient distinct-month coverage")
    if max(month_counts.values(), default=0) > maximum_per_month:
        raise MatureS1ConsistencyV2Error("sample exceeds the maximum cases per month")
    return {
        "status": "PASS",
        "cases": len(records),
        "unique_anonymous_stocks": len(seen_stocks),
        "focus_counts": dict(sorted(focus_counts.items())),
        "month_counts": dict(sorted(month_counts.items())),
        "distinct_months": len(month_counts),
        "maximum_cases_in_one_month": max(month_counts.values(), default=0),
        "sampling_metadata_inside_packet": False,
    }


def _validate_attempt_chain(
    *,
    record: Mapping[str, Any],
    envelope: Mapping[str, Any],
    envelope_path: Path,
    run_number: int,
) -> int:
    successful = envelope.get("successful_attempt")
    if isinstance(successful, bool) or not isinstance(successful, int) or successful < 1:
        raise MatureS1ConsistencyV2Error(f"invalid successful_attempt: {envelope_path}")
    run_case_key = str(envelope["run_case_key"])
    attempt_dir = envelope_path.parent.parent / "attempts" / run_case_key
    attempt_paths = sorted(attempt_dir.glob("attempt_*.json")) if attempt_dir.is_dir() else []
    expected_names = [f"attempt_{number:03d}.json" for number in range(1, successful + 1)]
    if [path.name for path in attempt_paths] != expected_names:
        raise MatureS1ConsistencyV2Error(f"attempt chain is missing, non-contiguous, or has trailing rows: {run_case_key}")
    for attempt_number, path in enumerate(attempt_paths, 1):
        attempt = _read_json(path)
        for field, expected in {
            "runner_version": RUNNER_VERSION,
            "case_key": record["case_key"],
            "run_case_key": run_case_key,
            "run_number": run_number,
            "attempt": attempt_number,
        }.items():
            if attempt.get(field) != expected:
                raise MatureS1ConsistencyV2Error(f"attempt identity differs for {run_case_key}: {field}")
        if attempt_number < successful:
            if attempt.get("status") != "RETRYABLE_ERROR" or attempt.get("technical_failure") is not True:
                raise MatureS1ConsistencyV2Error(f"pre-success attempt is not retryable: {path}")
            continue
        if set(attempt) != _VALIDATED_ATTEMPT_FIELDS:
            raise MatureS1ConsistencyV2Error(f"validated attempt fields are not exact: {path}")
        if attempt.get("status") != "VALIDATED" or attempt.get("technical_failure") is not False:
            raise MatureS1ConsistencyV2Error(f"successful attempt is not VALIDATED: {path}")
        for field in ("output_sha256", "reviewer_audit_sha256", "output", "reviewer_audit"):
            if attempt.get(field) != envelope.get(field):
                raise MatureS1ConsistencyV2Error(f"envelope differs from successful attempt: {field}")
    return successful


def validate_run_artifacts(
    records: Sequence[Mapping[str, Any]],
    run_roots: Sequence[Path],
    *,
    reviewer_code_sha256: str,
) -> list[dict[str, Any]]:
    """Validate exact runner envelopes and their successful attempt artifacts."""

    if len(run_roots) != EXPECTED_RUNS:
        raise MatureS1ConsistencyV2Error("S1 repeatability requires exactly three run roots")
    resolved_roots = [Path(root).resolve() for root in run_roots]
    if len(set(resolved_roots)) != EXPECTED_RUNS:
        raise MatureS1ConsistencyV2Error("the three run roots must be distinct")
    by_review = {str(record.get("review_id") or ""): record for record in records}
    if len(by_review) != EXPECTED_CASES or "" in by_review:
        raise MatureS1ConsistencyV2Error("expected record identities are not exact")

    summaries: list[dict[str, Any]] = []
    for run_number, root in enumerate(resolved_roots, 1):
        found: set[str] = set()
        attempts = 0
        paths = sorted(root.glob("s*/cases/*.json"))
        for path in paths:
            envelope = _read_json(path)
            review_id = str(envelope.get("review_id") or "")
            if review_id not in by_review or review_id in found:
                raise MatureS1ConsistencyV2Error(f"unexpected or duplicate run review ID: {review_id}")
            record = by_review[review_id]
            expected_run_key = build_run_case_key(str(record["case_key"]), run_number)
            if path.stem != expected_run_key or envelope.get("run_case_key") != expected_run_key:
                raise MatureS1ConsistencyV2Error(f"envelope filename/run_case_key differs: {path}")
            if set(envelope) != _ENVELOPE_FIELDS:
                raise MatureS1ConsistencyV2Error(f"run envelope fields are not exact: {path}")
            try:
                _validate_merge_envelope(dict(record), envelope, run_number)
            except ExecutionIntegrityError as exc:
                raise MatureS1ConsistencyV2Error(f"runner envelope validation failed: {path}: {exc}") from exc
            audit = envelope.get("reviewer_audit") or {}
            if (
                audit.get("adapter_version") != reviewer_v3.ADAPTER_VERSION
                or audit.get("adapter_status") != reviewer_v3.ADAPTER_STATUS
                or audit.get("adapter_code_sha256") != reviewer_code_sha256
            ):
                raise MatureS1ConsistencyV2Error(f"reviewer V3 adapter identity differs: {path}")
            attempts += _validate_attempt_chain(
                record=record,
                envelope=envelope,
                envelope_path=path,
                run_number=run_number,
            )
            found.add(review_id)
        missing = sorted(set(by_review) - found)
        if len(paths) != EXPECTED_CASES or missing:
            first = missing[0] if missing else None
            raise MatureS1ConsistencyV2Error(
                f"run {run_number} is not exact {EXPECTED_CASES}-case coverage; first_missing={first}"
            )
        summaries.append(
            {
                "run_number": run_number,
                "status": "PASS",
                "cases": len(found),
                "attempt_artifacts": attempts,
                "reviewer_adapter_version": reviewer_v3.ADAPTER_VERSION,
                "reviewer_adapter_status": reviewer_v3.ADAPTER_STATUS,
                "reviewer_adapter_code_sha256": reviewer_code_sha256,
            }
        )
    return summaries


def raw_permission_route_diagnostics(report: Mapping[str, Any]) -> dict[str, Any]:
    """Measure raw V3 permission/route stability without changing V1 gates."""

    rows = report.get("per_case")
    if not isinstance(rows, list) or len(rows) != EXPECTED_CASES:
        raise MatureS1ConsistencyV2Error("V1 report does not contain exact per-case diagnostics")
    permission_exact = route_exact = pair_exact = masked = 0
    run_permissions: list[Counter[str]] = [Counter() for _ in range(EXPECTED_RUNS)]
    run_routes: list[Counter[str]] = [Counter() for _ in range(EXPECTED_RUNS)]
    per_case: list[dict[str, Any]] = []
    for row in rows:
        decisions = row.get("run_decisions") if isinstance(row, Mapping) else None
        if not isinstance(decisions, list) or len(decisions) != EXPECTED_RUNS or any(
            not isinstance(value, Mapping) for value in decisions
        ):
            raise MatureS1ConsistencyV2Error("V1 per-case raw decisions are incomplete")
        permissions = [value.get("permission") for value in decisions]
        routes = [value.get("route") for value in decisions]
        permission_is_exact = len(set(permissions)) == 1
        route_is_exact = len(set(routes)) == 1
        pair_is_exact = len(set(zip(permissions, routes))) == 1
        stage_exact = bool(row.get("stage_permission_exact"))
        permission_exact += int(permission_is_exact)
        route_exact += int(route_is_exact)
        pair_exact += int(pair_is_exact)
        masked += int(stage_exact and not pair_is_exact)
        for index, (permission, route) in enumerate(zip(permissions, routes)):
            run_permissions[index]["<NONE>" if permission is None else str(permission)] += 1
            run_routes[index]["<NONE>" if route is None else str(route)] += 1
        row["raw_permission_exact"] = permission_is_exact
        row["raw_route_exact"] = route_is_exact
        row["raw_permission_route_exact"] = pair_is_exact
        per_case.append(
            {
                "review_id": row.get("review_id"),
                "raw_permissions": permissions,
                "raw_routes": routes,
                "raw_permission_exact": permission_is_exact,
                "raw_route_exact": route_is_exact,
                "raw_permission_route_exact": pair_is_exact,
                "stage_agreement_masks_raw_pair_disagreement": stage_exact and not pair_is_exact,
            }
        )

    def metric(exact: int) -> dict[str, Any]:
        return {"exact": exact, "total": EXPECTED_CASES, "rate": exact / EXPECTED_CASES}

    return {
        "status": "DIAGNOSTIC_ONLY_NOT_AN_ACCEPTANCE_GATE",
        "raw_permission": metric(permission_exact),
        "raw_route": metric(route_exact),
        "raw_permission_and_route": metric(pair_exact),
        "stage_agreement_masked_raw_pair_disagreement_cases": masked,
        "permission_distributions_by_run": [
            {"run_number": index + 1, "counts": dict(sorted(values.items()))}
            for index, values in enumerate(run_permissions)
        ],
        "route_distributions_by_run": [
            {"run_number": index + 1, "counts": dict(sorted(values.items()))}
            for index, values in enumerate(run_routes)
        ],
        "per_case": per_case,
    }


def evaluate(
    *,
    source: Path,
    source_manifest: Path,
    track_manifest: Path,
    assignment_manifest: Path,
    execution_freeze: Path,
    stage_protocol: Path,
    research_protocol: Path,
    schema: Path,
    run_roots: Sequence[Path],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Validate all frozen inputs first, then call the unchanged V1 metrics."""

    records, stage, _, _ = v1.verify_track(
        source=source,
        source_manifest=source_manifest,
        track_manifest=track_manifest,
        assignment_manifest=assignment_manifest,
        execution_freeze=execution_freeze,
        stage_protocol=stage_protocol,
        research_protocol=research_protocol,
        schema=schema,
    )
    sample_validation = validate_sample_invariants(records, stage, _read_json(source_manifest))
    reviewer_sha = _reviewer_code_sha256(execution_freeze)
    run_validation = validate_run_artifacts(
        records,
        run_roots,
        reviewer_code_sha256=reviewer_sha,
    )

    report, ledger = v1.evaluate(
        source=source,
        source_manifest=source_manifest,
        track_manifest=track_manifest,
        assignment_manifest=assignment_manifest,
        execution_freeze=execution_freeze,
        stage_protocol=stage_protocol,
        research_protocol=research_protocol,
        schema=schema,
        run_roots=run_roots,
    )
    report.pop("report_sha256", None)
    report["report_version"] = REPORT_VERSION
    report["base_metrics_version"] = BASE_REPORT_VERSION
    report["performance_or_future_fields_read"] = False
    report["fail_closed_pre_metric_validation"] = {
        "status": "PASS",
        "sample": sample_validation,
        "runs": run_validation,
    }
    report["raw_v3_permission_route_diagnostics"] = raw_permission_route_diagnostics(report)
    report["report_sha256"] = canonical_sha256(report)
    return report, ledger


def render_markdown(report: Mapping[str, Any], ledger_path: Path | None) -> str:
    base = v1.render_markdown(report, ledger_path).replace(
        "# V3 情境1三輪一致性報告", "# V3 情境1三輪一致性報告（V2 fail-closed）", 1
    )
    diagnostics = report["raw_v3_permission_route_diagnostics"]
    raw_permission = diagnostics["raw_permission"]
    raw_route = diagnostics["raw_route"]
    raw_pair = diagnostics["raw_permission_and_route"]
    section = [
        "",
        "## V2 fail-closed 與原始 V3 診斷",
        "",
        "- 樣本配額、股票唯一、月份與 eligible focus：已獨立重算通過。",
        "- 每輪 envelope、run_case_key、成功 attempt 與 reviewer V3 identity/hash：已逐案驗證通過。",
        f"- 原始 V3 permission 一致率：{raw_permission['rate']:.2%}（{raw_permission['exact']}/{raw_permission['total']}）。",
        f"- 原始 V3 route 一致率：{raw_route['rate']:.2%}（{raw_route['exact']}/{raw_route['total']}）。",
        f"- 原始 V3 permission+route 一致率：{raw_pair['rate']:.2%}（{raw_pair['exact']}/{raw_pair['total']}）。",
        "- 上述原始 V3 指標為診斷，不改動既有 V1 驗收門檻。",
    ]
    marker = "\n報告SHA-256："
    if marker in base:
        head, tail = base.rsplit(marker, 1)
        return head + "\n" + "\n".join(section) + marker + tail
    return base + "\n" + "\n".join(section) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--track-manifest", type=Path, required=True)
    parser.add_argument("--assignment-manifest", type=Path, required=True)
    parser.add_argument("--execution-freeze", type=Path, required=True)
    parser.add_argument("--stage-protocol", type=Path, required=True)
    parser.add_argument("--research-protocol", type=Path, required=True)
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--run-root", type=Path, action="append", required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-md", type=Path, required=True)
    parser.add_argument("--merged-ledger", type=Path, required=True)
    args = parser.parse_args(argv)
    report, ledger = evaluate(
        source=args.source,
        source_manifest=args.source_manifest,
        track_manifest=args.track_manifest,
        assignment_manifest=args.assignment_manifest,
        execution_freeze=args.execution_freeze,
        stage_protocol=args.stage_protocol,
        research_protocol=args.research_protocol,
        schema=args.schema,
        run_roots=args.run_root,
    )
    ledger_path: Path | None = None
    if report["acceptance"]["passed"]:
        ledger_payload = v1._canonical_jsonl(ledger)
        v1._publish(args.merged_ledger, ledger_payload)
        ledger_path = args.merged_ledger
        report["merged_ledger"] = {
            "status": "LOCKED_OUTCOME_BLIND_MATURE_S1_COMMON_LEDGER",
            "rows": len(ledger),
            "path": str(args.merged_ledger.resolve()),
            "sha256": __import__("hashlib").sha256(ledger_payload).hexdigest(),
        }
        report.pop("report_sha256", None)
        report["report_sha256"] = canonical_sha256(report)
    v1._publish(args.output_json, canonical_json_bytes(report) + b"\n")
    v1._publish(args.output_md, render_markdown(report, ledger_path).encode("utf-8"))
    print(json.dumps({"status": report["status"], "report_sha256": report["report_sha256"]}))
    return 0 if report["acceptance"]["passed"] else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
