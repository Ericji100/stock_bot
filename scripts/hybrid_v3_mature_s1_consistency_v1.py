"""Evaluate three-run repeatability for the frozen MATURE S1 track only.

The evaluator is outcome blind.  It preserves the full V3 semantic decision
for diagnostics, but stage trade permission is granted only when the unchanged
V3 reducer returns TRADE + V2_CORE + MATURE_TREND_PULLBACK.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

try:
    from .hybrid_v3_atomic_policy_v3 import reduce_atomic_v3, validate_atomic
    from .hybrid_v3_consistency_v2 import critical_question_ids
    from .hybrid_v3_conservative_merge_v1 import conservative_merge_atomic_outputs
    from .hybrid_v3_sharding_v2 import canonical_json_bytes, canonical_sha256, file_sha256
    from .hybrid_v3_triplicate_smoke_audit import decision_signature, result_map
except ImportError:  # pragma: no cover
    from scripts.hybrid_v3_atomic_policy_v3 import reduce_atomic_v3, validate_atomic
    from scripts.hybrid_v3_consistency_v2 import critical_question_ids
    from scripts.hybrid_v3_conservative_merge_v1 import conservative_merge_atomic_outputs
    from scripts.hybrid_v3_sharding_v2 import canonical_json_bytes, canonical_sha256, file_sha256
    from scripts.hybrid_v3_triplicate_smoke_audit import decision_signature, result_map


ROOT = Path(__file__).resolve().parents[1]
REPORT_VERSION = "hybrid-v3-mature-s1-consistency-v1"
TRACK_VERSION = "hybrid-v3-mature-s1-research-track-v1"
FREEZE_VERSION = "hybrid-v3-mature-s1-execution-freeze-v1"
EXPECTED_CASES = 36
EXPECTED_RUNS = 3
TARGET_SCENARIO = "MATURE_TREND_PULLBACK"
TARGET_ROUTE = "V2_CORE"


class MatureS1ConsistencyError(ValueError):
    """The frozen S1 inputs, outputs, or hash chain are incomplete or changed."""


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise MatureS1ConsistencyError(f"expected JSON object: {path}")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = [
        json.loads(line)
        for line in Path(path).read_text(encoding="utf-8-sig").splitlines()
        if line.strip()
    ]
    if any(not isinstance(row, dict) for row in rows):
        raise MatureS1ConsistencyError(f"expected JSON objects: {path}")
    return rows


def _self_hash(value: Mapping[str, Any], field: str) -> bool:
    core = dict(value)
    supplied = core.pop(field, None)
    return supplied == canonical_sha256(core)


def _verify_component_rows(freeze: Mapping[str, Any], field: str) -> None:
    rows = freeze.get(field)
    if not isinstance(rows, list) or not rows:
        raise MatureS1ConsistencyError(f"freeze has no {field}")
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or not row.get("name"):
            raise MatureS1ConsistencyError(f"invalid {field} entry")
        name = str(row["name"])
        if name in seen:
            raise MatureS1ConsistencyError(f"duplicate component: {name}")
        seen.add(name)
        if row.get("status") != "FINAL":
            raise MatureS1ConsistencyError(f"component is not FINAL: {name}")
        path = (ROOT / str(row.get("relative_path"))).resolve()
        if not path.is_file() or file_sha256(path) != row.get("sha256"):
            raise MatureS1ConsistencyError(f"frozen component changed: {name}")


def verify_track(
    *,
    source: Path,
    source_manifest: Path,
    track_manifest: Path,
    assignment_manifest: Path,
    execution_freeze: Path,
    stage_protocol: Path,
    research_protocol: Path,
    schema: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any], dict[str, str]]:
    track = _read_json(track_manifest)
    freeze = _read_json(execution_freeze)
    packet_manifest = _read_json(source_manifest)
    stage = _read_json(stage_protocol)
    protocol = _read_json(research_protocol)
    assignment = _read_json(assignment_manifest)
    if track.get("track_version") != TRACK_VERSION or not _self_hash(
        track, "track_manifest_sha256"
    ):
        raise MatureS1ConsistencyError("S1 track manifest changed")
    if track.get("status") != "FROZEN_OUTCOME_BLIND_SINGLE_ROUTE_RESEARCH_ONLY":
        raise MatureS1ConsistencyError("S1 track is not frozen")
    if freeze.get("freeze_version") != FREEZE_VERSION:
        raise MatureS1ConsistencyError("S1 execution freeze version changed")
    if freeze.get("status") != "FROZEN_IMMUTABLE_BEFORE_AI_EXECUTION":
        raise MatureS1ConsistencyError("S1 execution is not frozen")
    for key, expected in {
        "outcome_blind": True,
        "identity_visible": False,
        "future_data_visible": False,
        "performance_visible": False,
        "old_ai_output_visible": False,
        "strategy_or_gate_change": False,
    }.items():
        if freeze.get(key) is not expected:
            raise MatureS1ConsistencyError(f"freeze causal flag changed: {key}")
    scope = freeze.get("single_route_scope") or {}
    if scope.get("primary_scenario") != TARGET_SCENARIO or scope.get("trade_route") != TARGET_ROUTE:
        raise MatureS1ConsistencyError("freeze target route changed")
    if stage.get("stage_protocol_version") != "hybrid-v3-mature-s1-stage-v2" or stage.get(
        "status"
    ) != "FINAL_RESEARCH_LOCKED":
        raise MatureS1ConsistencyError("stage protocol is not frozen")
    if protocol.get("status") != "FINAL_RESEARCH_LOCKED":
        raise MatureS1ConsistencyError("research protocol is not frozen")
    if file_sha256(stage_protocol) != freeze.get("stage_protocol_sha256"):
        raise MatureS1ConsistencyError("stage protocol hash changed")
    _verify_component_rows(freeze, "v2_components")
    _verify_component_rows(freeze, "stage_components")

    linked = {
        "source": (source, "packet_path", "packet_sha256"),
        "source_manifest": (source_manifest, "packet_manifest_path", "packet_manifest_sha256"),
        "execution_freeze": (execution_freeze, "execution_freeze_path", "execution_freeze_sha256"),
        "assignment": (
            assignment_manifest,
            "assignment_manifest_path",
            "assignment_manifest_sha256",
        ),
    }
    for name, (path, path_field, hash_field) in linked.items():
        if Path(str(track.get(path_field) or "")).resolve() != Path(path).resolve():
            raise MatureS1ConsistencyError(f"track {name} path changed")
        if file_sha256(path) != track.get(hash_field):
            raise MatureS1ConsistencyError(f"track {name} hash changed")
    if file_sha256(source) != freeze.get("primary_packet_sha256"):
        raise MatureS1ConsistencyError("freeze source hash changed")
    if file_sha256(source_manifest) != freeze.get("primary_packet_manifest_sha256"):
        raise MatureS1ConsistencyError("freeze source manifest hash changed")
    if file_sha256(execution_freeze) != assignment.get("execution_contract_sha256"):
        raise MatureS1ConsistencyError("assignment uses another execution freeze")
    if file_sha256(source) != assignment.get("source_manifest_sha256"):
        raise MatureS1ConsistencyError("assignment uses another source")

    packets = _read_jsonl(source)
    if len(packets) != EXPECTED_CASES or int(packet_manifest.get("rows", -1)) != EXPECTED_CASES:
        raise MatureS1ConsistencyError("S1 packet count is not exact")
    if packet_manifest.get("status") != "LOCKED_OUTCOME_BLIND" or not _self_hash(
        packet_manifest, "manifest_sha256"
    ):
        raise MatureS1ConsistencyError("S1 packet manifest changed")
    if file_sha256(source) != packet_manifest.get("packets_canonical_jsonl_sha256"):
        raise MatureS1ConsistencyError("S1 packet artifact changed")
    mapping = packet_manifest.get("mapping") or []
    if len(mapping) != EXPECTED_CASES or canonical_sha256(mapping) != packet_manifest.get(
        "mapping_sha256"
    ):
        raise MatureS1ConsistencyError("S1 external mapping changed")
    focus_by_review: dict[str, str] = {}
    for index, (packet, mapped) in enumerate(zip(packets, mapping)):
        if mapped.get("selected_ordinal") != index:
            raise MatureS1ConsistencyError("S1 mapping order changed")
        if packet.get("review_id") != mapped.get("review_id"):
            raise MatureS1ConsistencyError("S1 mapped review differs")
        if canonical_sha256(packet) != mapped.get("packet_sha256"):
            raise MatureS1ConsistencyError("S1 mapped packet hash differs")
        focus_by_review[str(packet["review_id"])] = str(mapped.get("sampling_focus"))

    if (
        int(assignment.get("expected_rows", -1)) != EXPECTED_CASES
        or int((assignment.get("coverage") or {}).get("covered", -1)) != EXPECTED_CASES
        or int(assignment.get("shard_count", -1)) != len(assignment.get("shards") or [])
    ):
        raise MatureS1ConsistencyError("S1 shard coverage is incomplete")
    assigned: list[dict[str, Any]] = []
    pairs: list[dict[str, Any]] = []
    for shard in assignment["shards"]:
        path = Path(str(shard.get("path"))).resolve()
        if file_sha256(path) != shard.get("sha256"):
            raise MatureS1ConsistencyError("S1 shard artifact changed")
        rows = _read_jsonl(path)
        if len(rows) != int(shard.get("rows", -1)):
            raise MatureS1ConsistencyError("S1 shard row count changed")
        assigned.extend(rows)
        pairs.extend(
            {"case_key": row["case_key"], "shard_id": int(shard["shard_id"])} for row in rows
        )
    if len(assigned) != EXPECTED_CASES or canonical_sha256(pairs) != assignment.get(
        "assignment_sha256"
    ):
        raise MatureS1ConsistencyError("S1 shard assignment changed")
    by_review = {str(row["review_id"]): row for row in assigned}
    if len(by_review) != EXPECTED_CASES or set(by_review) != set(focus_by_review):
        raise MatureS1ConsistencyError("S1 assigned identities differ from packets")
    ordered = [by_review[str(packet["review_id"])] for packet in packets]
    return ordered, stage, protocol, focus_by_review


def _load_run(root: Path, run_number: int) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for path in sorted(Path(root).glob("s*/cases/*.json")):
        envelope = _read_json(path)
        review_id = str(envelope.get("review_id") or "")
        if not review_id or review_id in rows:
            raise MatureS1ConsistencyError(f"duplicate run review id: {review_id}")
        if envelope.get("status") != "VALID" or int(envelope.get("run_number", -1)) != run_number:
            raise MatureS1ConsistencyError(f"invalid run envelope: {path}")
        output = envelope.get("output")
        if not isinstance(output, dict) or canonical_sha256(output) != envelope.get("output_sha256"):
            raise MatureS1ConsistencyError(f"run output hash changed: {path}")
        rows[review_id] = envelope
    return rows


def stage_permission(signature: Mapping[str, Any]) -> str:
    if signature.get("permission") == "REMOVE":
        return "REMOVE"
    if (
        signature.get("permission") == "TRADE"
        and signature.get("route") == TARGET_ROUTE
        and signature.get("scenario") == TARGET_SCENARIO
    ):
        return "TRADE"
    return "WAIT"


def _rate(exact: int, total: int, threshold: float | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"exact": exact, "total": total, "rate": exact / total if total else 0.0}
    if threshold is not None:
        result.update({"threshold": threshold, "passed": result["rate"] >= threshold})
    return result


def _counter(values: Iterable[str]) -> dict[str, int]:
    return dict(sorted(Counter(values).items()))


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
    if len(run_roots) != EXPECTED_RUNS:
        raise MatureS1ConsistencyError("S1 repeatability requires exactly three runs")
    records, stage, protocol, focus_by_review = verify_track(
        source=source,
        source_manifest=source_manifest,
        track_manifest=track_manifest,
        assignment_manifest=assignment_manifest,
        execution_freeze=execution_freeze,
        stage_protocol=stage_protocol,
        research_protocol=research_protocol,
        schema=schema,
    )
    expected_ids = {str(row["review_id"]) for row in records}
    runs = [_load_run(root, number) for number, root in enumerate(run_roots, 1)]
    coverage: list[dict[str, Any]] = []
    for number, run in enumerate(runs, 1):
        missing = expected_ids - set(run)
        unexpected = set(run) - expected_ids
        coverage.append(
            {"run_number": number, "cases": len(run), "missing": len(missing), "unexpected": len(unexpected)}
        )
        if missing or unexpected or len(run) != EXPECTED_CASES:
            raise MatureS1ConsistencyError(f"run {number} is not exact {EXPECTED_CASES}-case coverage")

    freeze = _read_json(execution_freeze)
    expected_identity = {
        "source_manifest_sha256": file_sha256(source),
        "protocol_sha256": file_sha256(research_protocol),
        "prompt_sha256": file_sha256(ROOT / "config/hybrid_semantic_prompt_v3.md"),
        "schema_sha256": file_sha256(schema),
        "execution_contract_sha256": file_sha256(execution_freeze),
        "model": freeze["execution_contract"]["model"],
        "reasoning_effort": freeze["execution_contract"]["reasoning_effort"],
    }
    critical_ids = critical_question_ids(protocol, _read_json(schema))
    permission_exact = scenario_lr_exact = schema_exact = 0
    pooled_exact = pooled_total = critical_exact_total = critical_total = 0
    unanimous_trade = unanimous_nontrade = 0
    run_permissions: list[list[str]] = [[] for _ in range(EXPECTED_RUNS)]
    run_scenarios: list[list[str]] = [[] for _ in range(EXPECTED_RUNS)]
    focus_totals: dict[str, dict[str, int]] = defaultdict(
        lambda: {"cases": 0, "permission_exact": 0, "scenario_lr_exact": 0}
    )
    per_case: list[dict[str, Any]] = []
    merged_ledger: list[dict[str, Any]] = []

    for record in records:
        review_id = str(record["review_id"])
        packet = record["packet"]
        envelopes = [run[review_id] for run in runs]
        for field, expected in expected_identity.items():
            if any(envelope.get(field) != expected for envelope in envelopes):
                raise MatureS1ConsistencyError(f"frozen identity changed for {review_id}: {field}")
        if any(envelope.get("case_key") != record.get("case_key") for envelope in envelopes):
            raise MatureS1ConsistencyError(f"case key changed for {review_id}")
        outputs = [envelope["output"] for envelope in envelopes]
        errors = [validate_atomic(packet, output) for output in outputs]
        if any(errors):
            raise MatureS1ConsistencyError(f"atomic validation failed for {review_id}: {errors}")
        schema_exact += 1
        signatures = [decision_signature(reduce_atomic_v3(packet, output)) for output in outputs]
        stage_values = [stage_permission(signature) for signature in signatures]
        permission_is_exact = len(set(stage_values)) == 1
        scenario_lr_is_exact = (
            len({signature.get("scenario") for signature in signatures}) == 1
            and len({signature.get("left_right_phase") for signature in signatures}) == 1
        )
        permission_exact += int(permission_is_exact)
        scenario_lr_exact += int(scenario_lr_is_exact)
        if permission_is_exact and stage_values[0] == "TRADE":
            unanimous_trade += 1
        if permission_is_exact and stage_values[0] != "TRADE":
            unanimous_nontrade += 1
        for index, value in enumerate(stage_values):
            run_permissions[index].append(value)
            run_scenarios[index].append(str(signatures[index].get("scenario")))

        maps = [result_map(output) for output in outputs]
        paths = sorted(set().union(*(set(mapping) for mapping in maps)))
        if any(set(mapping) != set(paths) for mapping in maps):
            raise MatureS1ConsistencyError(f"atomic manifest differs for {review_id}")
        critical_paths = [path for path in paths if path.rsplit(".", 1)[-1] in critical_ids]
        atom_exact = sum(len({mapping[path] for mapping in maps}) == 1 for path in paths)
        critical_atom_exact = sum(
            len({mapping[path] for mapping in maps}) == 1 for path in critical_paths
        )
        pooled_exact += atom_exact
        pooled_total += len(paths)
        critical_exact_total += critical_atom_exact
        critical_total += len(critical_paths)

        merged, merge_metrics = conservative_merge_atomic_outputs(outputs, critical_ids=critical_ids)
        if validate_atomic(packet, merged):
            raise MatureS1ConsistencyError(f"merged output invalid for {review_id}")
        merged_signature = decision_signature(reduce_atomic_v3(packet, merged))
        focus = focus_by_review[review_id]
        focus_totals[focus]["cases"] += 1
        focus_totals[focus]["permission_exact"] += int(permission_is_exact)
        focus_totals[focus]["scenario_lr_exact"] += int(scenario_lr_is_exact)
        per_case.append(
            {
                "review_id": review_id,
                "sampling_focus": focus,
                "atom_agreement": _rate(atom_exact, len(paths)),
                "critical_atom_agreement": _rate(critical_atom_exact, len(critical_paths)),
                "stage_permission_exact": permission_is_exact,
                "scenario_left_right_exact": scenario_lr_is_exact,
                "run_decisions": signatures,
                "run_stage_permissions": stage_values,
                "conservative_merge_decision": merged_signature,
                "conservative_merge_stage_permission": stage_permission(merged_signature),
                "critical_disagreement_count": len(merge_metrics["critical_disagreement_paths"]),
            }
        )
        merged_ledger.append(
            {
                "review_id": review_id,
                "sampling_focus": focus,
                "packet_sha256": record["packet_sha256"],
                "merge_policy": "UNANIMOUS_ELSE_UNKNOWN",
                "atomic_semantics": merged,
                "full_v3_decision": merged_signature,
                "s1_stage_permission": stage_permission(merged_signature),
                "critical_disagreement_paths": merge_metrics["critical_disagreement_paths"],
            }
        )

    thresholds = stage["repeatability_acceptance"]
    schema_metric = _rate(schema_exact, EXPECTED_CASES, float(thresholds["schema_and_causal_fields"]))
    permission_metric = _rate(
        permission_exact, EXPECTED_CASES, float(thresholds["material_permission"])
    )
    scenario_metric = _rate(
        scenario_lr_exact,
        EXPECTED_CASES,
        float(thresholds["scenario_and_left_right_phase"]),
    )
    coverage_checks = {
        "unanimous_s1_trade_cases": unanimous_trade
        >= int(thresholds["minimum_unanimous_s1_trade_cases"]),
        "unanimous_s1_nontrade_cases": unanimous_nontrade
        >= int(thresholds["minimum_unanimous_s1_nontrade_cases"]),
    }
    checks = {
        "schema_and_causal_fields": bool(schema_metric["passed"]),
        "material_permission": bool(permission_metric["passed"]),
        "scenario_and_left_right_phase": bool(scenario_metric["passed"]),
        **coverage_checks,
    }
    passed = all(checks.values())
    focus_metrics = {
        focus: {
            "cases": values["cases"],
            "material_permission": _rate(values["permission_exact"], values["cases"]),
            "scenario_and_left_right_phase": _rate(values["scenario_lr_exact"], values["cases"]),
        }
        for focus, values in sorted(focus_totals.items())
    }
    report = {
        "report_version": REPORT_VERSION,
        "status": "REPEATABILITY_PASS" if passed else "REPEATABILITY_FAIL",
        "classification": "OUTCOME_BLIND_MATURE_S1_REPEATABILITY_ONLY",
        "course_correctness_evaluated": False,
        "performance_evaluated": False,
        "identity_visible": False,
        "future_or_performance_visible": False,
        "target_scenario": TARGET_SCENARIO,
        "target_route": TARGET_ROUTE,
        "expected_cases": EXPECTED_CASES,
        "run_count": EXPECTED_RUNS,
        "run_coverage": coverage,
        "model": expected_identity["model"],
        "reasoning_effort": expected_identity["reasoning_effort"],
        "metrics": {
            "schema_and_causal_fields": schema_metric,
            "material_permission": permission_metric,
            "scenario_and_left_right_phase": scenario_metric,
            "all_atomic_answers_pooled": _rate(pooled_exact, pooled_total),
            "critical_atomic_answers_pooled": _rate(critical_exact_total, critical_total),
            "unanimous_s1_trade_cases": unanimous_trade,
            "unanimous_s1_nontrade_cases": unanimous_nontrade,
        },
        "acceptance": {"checks": checks, "passed": passed},
        "stage_permission_distributions_by_run": [
            {"run_number": index + 1, "counts": _counter(values)}
            for index, values in enumerate(run_permissions)
        ],
        "scenario_distributions_by_run": [
            {"run_number": index + 1, "counts": _counter(values)}
            for index, values in enumerate(run_scenarios)
        ],
        "sampling_focus_metrics": focus_metrics,
        "per_case": per_case,
        "next_step": (
            "LOCK_COMMON_LEDGER_AND_RUN_OUTCOME_BLIND_COURSE_FIDELITY_AUDIT"
            if passed
            else "KEEP_PERFORMANCE_SEALED_AND_VERSION_BEFORE_RETEST"
        ),
        "limitations": [
            "REPEATABILITY_IS_NOT_COURSE_CORRECTNESS",
            "REPEATABILITY_IS_NOT_TRADING_PERFORMANCE",
            "OTHER_SIX_V3_ROUTES_REMAIN_SUSPENDED",
        ],
    }
    report["report_sha256"] = canonical_sha256(report)
    return report, merged_ledger


def _canonical_jsonl(rows: Sequence[Mapping[str, Any]]) -> bytes:
    return b"".join(canonical_json_bytes(dict(row)) + b"\n" for row in rows)


def render_markdown(report: Mapping[str, Any], ledger_path: Path | None) -> str:
    metrics = report["metrics"]
    lines = [
        "# V3 情境1三輪一致性報告",
        "",
        f"- 結果：`{report['status']}`",
        "- 路徑：`MATURE_TREND_PULLBACK／V2_CORE（長多慣性拉回再發動完整合格）`",
        f"- 案例：{report['expected_cases']}例 × {report['run_count']}輪",
        "- 性質：結果盲化一致性驗收；不是課程正確性或交易績效。",
        "- 股票身分、未來行情與績效：未讀取。",
        "",
        "| 驗收項目 | 實際 | 門檻 | 結果 |",
        "| --- | ---: | ---: | --- |",
    ]
    for key, label in (
        ("schema_and_causal_fields", "Schema與因果欄位"),
        ("material_permission", "情境1交易／等待／移除權限"),
        ("scenario_and_left_right_phase", "主要情境與左右階段"),
    ):
        value = metrics[key]
        lines.append(
            f"| {label} | {value['rate']:.2%}（{value['exact']}/{value['total']}） | "
            f"{value['threshold']:.0%} | {'通過' if value['passed'] else '未通過'} |"
        )
    lines.extend(
        [
            "",
            "## 覆蓋門檻",
            "",
            f"- 三輪一致的情境1交易案例：{metrics['unanimous_s1_trade_cases']}例。",
            f"- 三輪一致的不交易／移除案例：{metrics['unanimous_s1_nontrade_cases']}例。",
            "",
            "## 診斷",
            "",
            f"- 全部原子答案一致率：{metrics['all_atomic_answers_pooled']['rate']:.2%}。",
            f"- 關鍵原子答案一致率：{metrics['critical_atomic_answers_pooled']['rate']:.2%}。",
            "",
            "## 後續",
            "",
        ]
    )
    if report["acceptance"]["passed"]:
        lines.append("一致性與覆蓋門檻通過；可鎖定共同ledger並進入結果盲化課程符合度稽核，仍不得解封績效。")
        if ledger_path is not None:
            lines.append(f"共同ledger：`{Path(ledger_path).resolve()}`")
    else:
        lines.append("一致性或覆蓋門檻未通過；維持績效封存，不得在本輪邊跑邊修改。")
    lines.extend(["", f"報告SHA-256：`{report['report_sha256']}`", ""])
    return "\n".join(lines)


def _publish(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != payload:
            raise MatureS1ConsistencyError(f"refusing to overwrite immutable artifact: {path}")
        return
    path.write_bytes(payload)


def main() -> int:
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
    args = parser.parse_args()
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
        ledger_payload = _canonical_jsonl(ledger)
        _publish(args.merged_ledger, ledger_payload)
        ledger_path = args.merged_ledger
        report["merged_ledger"] = {
            "status": "LOCKED_OUTCOME_BLIND_MATURE_S1_COMMON_LEDGER",
            "rows": len(ledger),
            "path": str(args.merged_ledger.resolve()),
            "sha256": hashlib.sha256(ledger_payload).hexdigest(),
        }
        report.pop("report_sha256", None)
        report["report_sha256"] = canonical_sha256(report)
    _publish(args.output_json, canonical_json_bytes(report) + b"\n")
    _publish(args.output_md, render_markdown(report, ledger_path).encode("utf-8"))
    print(json.dumps({"status": report["status"], "report_sha256": report["report_sha256"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
