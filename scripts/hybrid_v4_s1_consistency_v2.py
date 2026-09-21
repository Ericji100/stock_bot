"""Technical amendment to the frozen V4-S1 repeatability evaluator.

V1 correctly fails closed for mismatched atomic manifests, but it also raises
when all three valid outputs contain an intentionally empty atomic manifest.
Such packets occur when the frozen input exposes no answerable candidate.  V2
handles only that zero-atom case; every non-empty case delegates unchanged to
the frozen V1 merge and the rest of the V1 evaluator remains authoritative.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import hybrid_v4_s1_consistency_v1 as v1
from .hybrid_v3_consistency_v2 import _atomic_verdicts
from .hybrid_v3_conservative_merge_v1 import conservative_merge_atomic_outputs


REPORT_VERSION = "hybrid-v4-s1-consistency-v2-empty-manifest-amendment"
AMENDMENT_CODE = "ALLOW_IDENTICAL_VALID_EMPTY_ATOMIC_MANIFEST"


def merge_empty_manifest_aware(
    outputs: Sequence[Mapping[str, Any]],
    *,
    critical_ids: set[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Merge like V1, except that three identical empty manifests are valid."""

    maps = [_atomic_verdicts(output) for output in outputs]
    if len(outputs) == 3 and all(not mapping for mapping in maps):
        return copy.deepcopy(dict(outputs[0])), {
            "merge_version": REPORT_VERSION,
            "merge_status": "FINAL_TECHNICAL_AMENDMENT",
            "critical_atom_fields": 0,
            "critical_atom_exact_fields": 0,
            "critical_atom_rate": None,
            "disagreement_paths": [],
            "critical_disagreement_paths": [],
            "critical_by_question": {},
            "empty_manifest_handling": AMENDMENT_CODE,
        }
    return conservative_merge_atomic_outputs(outputs, critical_ids=critical_ids)


def evaluate(**kwargs: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Run frozen V1 with the narrowly scoped empty-manifest merge amendment."""

    original = v1.conservative_merge_atomic_outputs
    v1.conservative_merge_atomic_outputs = merge_empty_manifest_aware
    try:
        report, ledger = v1.evaluate(**kwargs)
    finally:
        v1.conservative_merge_atomic_outputs = original
    report["report_version"] = REPORT_VERSION
    report["technical_amendment"] = {
        "code": AMENDMENT_CODE,
        "scope": "IDENTICAL_SCHEMA_VALID_ZERO_ATOM_OUTPUTS_ONLY",
        "rule_or_ai_output_changed": False,
        "non_empty_merge_delegates_to_frozen_v1": True,
        "frozen_v1_evaluator_sha256": v1.file_sha256(Path(v1.__file__)),
    }
    report.pop("report_sha256", None)
    report["report_sha256"] = v1.canonical_sha256(report)
    return report, ledger


def render_markdown(report: Mapping[str, Any], ledger_path: Path | None = None) -> str:
    text = v1.render_markdown(report, ledger_path)
    note = (
        "\n## 技術修正說明\n\n"
        "- 本報告使用 `CONSISTENCY_EVALUATOR_V2（第二版一致率計算器）`。\n"
        "- 只允許三輪皆為合法空原子清單的案例通過合併；非空案例完全沿用凍結V1算法。\n"
        "- 未修改V4-S1規則、AI答案、交易權限或績效資料。\n"
    )
    return text.rstrip() + note


def _publish(path: Path, payload: bytes) -> None:
    v1._publish(path, payload)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--track-manifest", type=Path, required=True)
    parser.add_argument("--assignment-manifest", type=Path, required=True)
    parser.add_argument("--execution-freeze", type=Path, required=True)
    parser.add_argument("--stage-protocol", type=Path, required=True)
    parser.add_argument("--research-protocol", type=Path, required=True)
    parser.add_argument("--prompt", type=Path, required=True)
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
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
        prompt=args.prompt,
        schema=args.schema,
        policy=args.policy,
        run_roots=args.run_root,
    )
    ledger_path: Path | None = None
    if report["acceptance"]["passed"]:
        ledger_payload = v1._canonical_jsonl(ledger)
        _publish(args.merged_ledger, ledger_payload)
        ledger_path = args.merged_ledger
        report["merged_ledger"] = {
            "status": "LOCKED_OUTCOME_BLIND_COMMON_LEDGER",
            "status_zh": "結果盲化共同帳本已鎖定",
            "rows": len(ledger),
            "path": str(args.merged_ledger.resolve()),
            "sha256": hashlib.sha256(ledger_payload).hexdigest(),
        }
        report.pop("report_sha256", None)
        report["report_sha256"] = v1.canonical_sha256(report)
    _publish(args.output_json, v1.canonical_json_bytes(report) + b"\n")
    _publish(args.output_md, render_markdown(report, ledger_path).encode("utf-8"))
    print(json.dumps({
        "status": report["status"],
        "status_zh": report["status_zh"],
        "report_sha256": report["report_sha256"],
    }, ensure_ascii=False))
    return 0 if report["acceptance"]["passed"] else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
