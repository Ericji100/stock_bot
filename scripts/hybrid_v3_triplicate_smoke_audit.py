"""Audit three independent AI reviews of the same outcome-blind V3 case.

This is an operational smoke audit, not the frozen 120-case formal consistency
gate and not a performance report.  It verifies each atomic output, applies the
unchanged deterministic V3 reducer, and reports exact agreement without using
stock identity or future prices.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from .hybrid_v3_atomic_policy_v3 import reduce_atomic_v3, validate_atomic
    from .hybrid_v3_consistency_v2 import (
        _atomic_verdicts,
        atom_path,
        critical_question_ids,
    )
    from .hybrid_v3_conservative_merge_v1 import conservative_merge_atomic_outputs
    from .hybrid_v3_sharding_v2 import canonical_sha256
except ImportError:  # direct script execution
    from scripts.hybrid_v3_atomic_policy_v3 import reduce_atomic_v3, validate_atomic
    from scripts.hybrid_v3_consistency_v2 import (
        _atomic_verdicts,
        atom_path,
        critical_question_ids,
    )
    from scripts.hybrid_v3_conservative_merge_v1 import conservative_merge_atomic_outputs
    from scripts.hybrid_v3_sharding_v2 import canonical_sha256


AUDIT_VERSION = "hybrid-v3-triplicate-smoke-audit-v1"
AUDIT_STATUS = "RESEARCH_SMOKE_NOT_FORMAL_ACCEPTANCE"


class SmokeAuditError(ValueError):
    """The requested triplicate evidence is incomplete or changed."""


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise SmokeAuditError(f"expected JSON object: {path}")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = [
        json.loads(line)
        for line in Path(path).read_text(encoding="utf-8-sig").splitlines()
        if line.strip()
    ]
    if any(not isinstance(row, dict) for row in rows):
        raise SmokeAuditError(f"expected JSON objects in: {path}")
    return rows


def _source_record(records: Sequence[Mapping[str, Any]], review_id: str) -> dict[str, Any]:
    matches = [dict(row) for row in records if row.get("review_id") == review_id]
    if len(matches) != 1:
        raise SmokeAuditError(f"expected one source record for {review_id}, got {len(matches)}")
    record = matches[0]
    packet = record.get("packet")
    if not isinstance(packet, dict):
        raise SmokeAuditError("source record packet is missing")
    if canonical_sha256(packet) != record.get("packet_sha256"):
        raise SmokeAuditError("source packet SHA-256 changed")
    return record


def _run_envelope(run_dir: Path, review_id: str) -> dict[str, Any]:
    matches: list[dict[str, Any]] = []
    for path in sorted((Path(run_dir) / "cases").glob("*.json")):
        envelope = _read_json(path)
        if envelope.get("review_id") == review_id:
            matches.append(envelope)
    if len(matches) != 1:
        raise SmokeAuditError(
            f"expected one validated envelope for {review_id} in {run_dir}, got {len(matches)}"
        )
    envelope = matches[0]
    if envelope.get("status") != "VALID":
        raise SmokeAuditError(f"run envelope is not VALID: {run_dir}")
    output = envelope.get("output")
    if not isinstance(output, dict) or canonical_sha256(output) != envelope.get("output_sha256"):
        raise SmokeAuditError(f"run output SHA-256 changed: {run_dir}")
    return envelope


def _attempt_summary(run_dir: Path, run_case_key: str) -> dict[str, Any]:
    rows = [
        _read_json(path)
        for path in sorted((Path(run_dir) / "attempts" / run_case_key).glob("attempt_*.json"))
    ]
    elapsed = [float(row["elapsed_seconds"]) for row in rows if row.get("elapsed_seconds") is not None]
    return {
        "attempt_count": len(rows),
        "attempt_status_counts": dict(sorted(Counter(str(row.get("status")) for row in rows).items())),
        "elapsed_seconds_total": sum(elapsed),
        "elapsed_seconds_by_attempt": elapsed,
    }


def result_map(semantic: Mapping[str, Any]) -> dict[str, str]:
    """Return canonical atom paths and only their PASS/FAIL/UNKNOWN result."""

    return {
        atom_path(key): str(verdict.get("result"))
        for key, verdict in _atomic_verdicts(semantic).items()
    }


def decision_signature(decision: Mapping[str, Any]) -> dict[str, Any]:
    derived = decision.get("derived_structure") or {}
    return {
        "permission": decision.get("permission"),
        "route": decision.get("route"),
        "scenario": decision.get("scenario", derived.get("primary_scenario")),
        "stage": derived.get("stage"),
        "left_right_phase": derived.get("left_right_phase"),
        "reason_codes": list(decision.get("reason_codes") or []),
    }


def exact_rate(maps: Sequence[Mapping[str, str]], paths: Sequence[str]) -> dict[str, Any]:
    exact = sum(len({mapping.get(path) for mapping in maps}) == 1 for path in paths)
    return {
        "exact": exact,
        "total": len(paths),
        "rate": exact / len(paths) if paths else 0.0,
        "disagreements": [
            {"path": path, "results": [mapping.get(path) for mapping in maps]}
            for path in paths
            if len({mapping.get(path) for mapping in maps}) != 1
        ],
    }


def audit(
    *,
    case_records: Path,
    run_dirs: Sequence[Path],
    review_id: str,
    protocol_path: Path,
    schema_path: Path,
) -> dict[str, Any]:
    if len(run_dirs) != 3:
        raise SmokeAuditError("triplicate smoke audit requires exactly three run directories")
    record = _source_record(_read_jsonl(case_records), review_id)
    packet = record["packet"]
    protocol = _read_json(protocol_path)
    schema = _read_json(schema_path)
    critical_ids = critical_question_ids(protocol, schema)

    envelopes = [_run_envelope(path, review_id) for path in run_dirs]
    identity_fields = (
        "review_id",
        "case_key",
        "packet_sha256",
        "source_manifest_sha256",
        "protocol_sha256",
        "prompt_sha256",
        "schema_sha256",
        "execution_contract_sha256",
        "model",
        "reasoning_effort",
    )
    for field in identity_fields:
        values = {str(envelope.get(field)) for envelope in envelopes}
        if len(values) != 1:
            raise SmokeAuditError(f"triplicate run identity differs for {field}")
    if envelopes[0].get("packet_sha256") != record.get("packet_sha256"):
        raise SmokeAuditError("run packet SHA-256 differs from source record")

    outputs = [envelope["output"] for envelope in envelopes]
    validation = [list(validate_atomic(packet, output)) for output in outputs]
    if any(validation):
        raise SmokeAuditError(f"atomic validation failed: {validation}")
    decisions = [reduce_atomic_v3(packet, output) for output in outputs]
    maps = [result_map(output) for output in outputs]
    all_paths = sorted(set().union(*(set(mapping) for mapping in maps)))
    if any(set(mapping) != set(all_paths) for mapping in maps):
        raise SmokeAuditError("triplicate atomic manifests differ")
    critical_paths = [path for path in all_paths if path.rsplit(".", 1)[-1] in critical_ids]

    merged, merge_metrics = conservative_merge_atomic_outputs(outputs, critical_ids=critical_ids)
    merged_validation = list(validate_atomic(packet, merged))
    if merged_validation:
        raise SmokeAuditError(f"conservative merged output is invalid: {merged_validation}")
    merged_decision = reduce_atomic_v3(packet, merged)
    signatures = [decision_signature(value) for value in decisions]
    permission_exact = len({value["permission"] for value in signatures}) == 1
    scenario_phase_fields = ("scenario", "stage", "left_right_phase")
    scenario_phase_exact = all(
        len({value[field] for value in signatures}) == 1 for field in scenario_phase_fields
    )

    runs: list[dict[str, Any]] = []
    for run_dir, envelope, output, signature in zip(run_dirs, envelopes, outputs, signatures):
        counts = Counter(result_map(output).values())
        runs.append(
            {
                "run_number": envelope.get("run_number"),
                "run_dir": str(Path(run_dir).resolve()),
                "successful_attempt": envelope.get("successful_attempt"),
                "output_sha256": envelope.get("output_sha256"),
                "answer_count": len(result_map(output)),
                "verdict_counts": dict(sorted(counts.items())),
                "decision": signature,
                "operations": _attempt_summary(Path(run_dir), str(envelope["run_case_key"])),
            }
        )
    report = {
        "audit_version": AUDIT_VERSION,
        "status": AUDIT_STATUS,
        "classification": "RESEARCH_REPEATABILITY_SMOKE_NO_PERFORMANCE",
        "review_id": review_id,
        "source_ordinal": record.get("source_ordinal"),
        "identity_visible": False,
        "future_performance_visible": False,
        "run_count": 3,
        "model": envelopes[0].get("model"),
        "reasoning_effort": envelopes[0].get("reasoning_effort"),
        "runs": runs,
        "agreement": {
            "all_atoms": exact_rate(maps, all_paths),
            "critical_atoms": exact_rate(maps, critical_paths),
            "permission_exact": permission_exact,
            "scenario_phase_exact": scenario_phase_exact,
            "decision_signatures": signatures,
        },
        "conservative_merge": {
            "critical_atom_rate": merge_metrics["critical_atom_rate"],
            "critical_disagreement_paths": merge_metrics["critical_disagreement_paths"],
            "decision": decision_signature(merged_decision),
        },
        "formal_acceptance": {
            "evaluated": False,
            "reason": "ONE_CASE_SMOKE_CANNOT_REPLACE_FROZEN_120_CASE_HOLDOUT",
        },
        "performance": {
            "evaluated": False,
            "reason": "FUTURE_RESULTS_REMAIN_LOCKED_PENDING_COMMON_LEDGER",
        },
    }
    report["report_sha256"] = canonical_sha256(report)
    return report


def render_markdown(report: Mapping[str, Any]) -> str:
    agreement = report["agreement"]
    merged = report["conservative_merge"]
    lines = [
        "# V3 三次判讀單案例冒煙稽核",
        "",
        f"- 狀態：`{report['status']}`",
        "- 性質：研究一致性冒煙測試；不是正式120案例驗收，也不是績效回測。",
        f"- Review ID：`{report['review_id']}`",
        f"- 模型／推理：`{report['model']}`／`{report['reasoning_effort']}`",
        f"- 全部原子答案一致率：{agreement['all_atoms']['rate']:.2%}（{agreement['all_atoms']['exact']}/{agreement['all_atoms']['total']}）",
        f"- 關鍵原子答案一致率：{agreement['critical_atoms']['rate']:.2%}（{agreement['critical_atoms']['exact']}/{agreement['critical_atoms']['total']}）",
        f"- 最終權限完全一致：{'是' if agreement['permission_exact'] else '否'}",
        f"- 情境／位階／左右階段完全一致：{'是' if agreement['scenario_phase_exact'] else '否'}",
        f"- 保守合併結果：`{merged['decision']['permission']}`／`{merged['decision']['route']}`／`{merged['decision']['scenario']}`",
        "",
        "| 輪次 | 成功嘗試 | 原子答案 | PASS | FAIL | UNKNOWN | 程式權限 | 路徑 | 情境 |",
        "| ---: | ---: | ---: | ---: | ---: | ---: | --- | --- | --- |",
    ]
    for row in report["runs"]:
        counts = row["verdict_counts"]
        decision = row["decision"]
        lines.append(
            f"| {row['run_number']} | {row['successful_attempt']} | {row['answer_count']} | "
            f"{counts.get('PASS', 0)} | {counts.get('FAIL', 0)} | {counts.get('UNKNOWN', 0)} | "
            f"`{decision['permission']}` | `{decision['route']}` | `{decision['scenario']}` |"
        )
    lines.extend(
        [
            "",
            "## 限制",
            "",
            "這份結果只證明同一個盲化案例的傳輸、驗證與程式裁決是否可重複。正式一致性仍須完成凍結的120案例三輪，績效仍須等共同結構ledger鎖定後才可解盲計算。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case-records", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, action="append", required=True)
    parser.add_argument("--review-id", required=True)
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-md", type=Path, required=True)
    args = parser.parse_args()
    report = audit(
        case_records=args.case_records,
        run_dirs=args.run_dir,
        review_id=args.review_id,
        protocol_path=args.protocol,
        schema_path=args.schema,
    )
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_md.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    args.output_md.write_text(render_markdown(report), encoding="utf-8")
    print(
        json.dumps(
            {
                "status": report["status"],
                "report_sha256": report["report_sha256"],
                "output_json": str(args.output_json.resolve()),
                "output_md": str(args.output_md.resolve()),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
