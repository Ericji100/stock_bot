"""Evaluate and lock the three-run hybrid semantic consistency sample."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from hybrid_v3_policy import consistency_metrics, merge_three, read_json, reduce_v3


ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "reports/course_backtest/2024-02-02/historical_scan_2023h2_formal_ai_v3_daily_scan/hybrid_monitoring_v1"
SAMPLE = RUN / "consistency_sample.jsonl"
CONSISTENCY = RUN / "consistency"
PROTOCOL = ROOT / "config/hybrid_monitoring_protocol_v1.json"
POLICY = ROOT / "scripts/hybrid_v3_policy.py"
FREEZE = RUN / "protocol_freeze_manifest.json"
EXECUTION_FREEZE = RUN / "execution_freeze_manifest.json"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n" for row in rows), encoding="utf-8")


def _assert_freeze() -> dict[str, Any]:
    freeze = read_json(FREEZE)
    for item in freeze.get("files") or []:
        path = ROOT / item["relative_path"]
        if not path.exists() or _sha(path) != item["sha256"]:
            raise ValueError(f"frozen file changed: {item['relative_path']}")
    for key in ("event_manifest", "policy_boundary_manifest"):
        item = freeze[key]
        path = Path(item["path"])
        if not path.exists() or _sha(path) != item["sha256"]:
            raise ValueError(f"frozen artifact changed: {key}")
    return freeze


def _assert_execution_freeze() -> dict[str, Any]:
    freeze = read_json(EXECUTION_FREEZE)
    for item in freeze.get("files") or []:
        path = ROOT / item["relative_path"]
        if not path.exists() or _sha(path) != item["sha256"]:
            raise ValueError(f"execution-frozen file changed: {item['relative_path']}")
    for item in freeze.get("artifacts") or []:
        path = Path(item["absolute_path"])
        if not path.exists() or _sha(path) != item["sha256"]:
            raise ValueError(f"execution-frozen artifact changed: {path}")
    return freeze


def evaluate() -> dict[str, Any]:
    freeze = _assert_freeze()
    execution_freeze = _assert_execution_freeze()
    if execution_freeze.get("model") != freeze.get("model") or execution_freeze.get("reasoning_effort") != freeze.get("reasoning_effort"):
        raise ValueError("semantic and execution freeze model settings differ")
    packets = {row["review_id"]: row for row in _jsonl(SAMPLE)}
    run_paths = [CONSISTENCY / f"run_{number}.jsonl" for number in (1, 2, 3)]
    for path in run_paths:
        if not path.exists():
            raise FileNotFoundError(path)
    runs = []
    expected_ids = set(packets)
    for path in run_paths:
        manifest_path = path.with_suffix(".manifest.json")
        if not manifest_path.exists():
            raise FileNotFoundError(manifest_path)
        manifest = read_json(manifest_path)
        if not manifest.get("complete") or int(manifest.get("completed_rows", -1)) != len(packets):
            raise ValueError(f"incomplete consistency run: {path.name}")
        if manifest.get("source_sha256") != _sha(SAMPLE) or manifest.get("output_sha256") != _sha(path):
            raise ValueError(f"consistency run hash mismatch: {path.name}")
        if manifest.get("model") != freeze["model"] or manifest.get("reasoning_effort") != freeze["reasoning_effort"]:
            raise ValueError(f"consistency model setting mismatch: {path.name}")
        rows = _jsonl(path)
        ids = [row.get("review_id") for row in rows]
        if len(ids) != len(set(ids)) or set(ids) != expected_ids:
            raise ValueError(f"consistency review ids mismatch: {path.name}")
        runs.append({row["review_id"]: row for row in rows})
    metrics = consistency_metrics(packets, runs)
    thresholds = read_json(PROTOCOL)["consistency_thresholds"]
    passed = all(float(metrics[key]) >= float(thresholds[key]) for key in ("schema_and_causality", "permission", "scenario_and_phase"))

    merged = []
    permission_counts: dict[str, int] = {}
    for review_id, packet in packets.items():
        semantic = merge_three(packet, [run[review_id] for run in runs])
        policy = reduce_v3(packet, semantic)
        semantic["consistency_policy_preview"] = policy  # written only to audit envelope below
        preview = semantic.pop("consistency_policy_preview")
        merged.append({"review_id": review_id, "semantic": semantic, "policy_preview": preview})
        key = preview["permission"] + "/" + preview["route"]
        permission_counts[key] = permission_counts.get(key, 0) + 1

    MERGED = CONSISTENCY / "merged_sample_ledger.jsonl"
    _write_jsonl(MERGED, merged)
    result = {
        "protocol_version": "hybrid-monitoring-protocol-v1",
        "passed": passed,
        "thresholds": thresholds,
        "metrics": {key: value for key, value in metrics.items() if key != "details"},
        "permission_counts_after_conservative_merge": dict(sorted(permission_counts.items())),
        "sample": {"path": str(SAMPLE.resolve()), "sha256": _sha(SAMPLE)},
        "policy_reducer": {"path": str(POLICY.resolve()), "sha256": _sha(POLICY)},
        "runs": [{"path": str(path.resolve()), "sha256": _sha(path), "rows": len(runs[index])} for index, path in enumerate(run_paths)],
        "merged": {"path": str(MERGED.resolve()), "sha256": _sha(MERGED), "rows": len(merged)},
        "details": metrics["details"],
    }
    output_json = CONSISTENCY / "consistency_result.json"
    output_json.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# HYBRID_MONITORING_PROTOCOL_V1 一致性測試",
        "",
        f"- 結果：**{'PASS（通過）' if passed else 'FAIL（未通過）'}**",
        f"- 匿名案例：{len(packets)} 筆；相同輸入獨立執行 3 次。",
        f"- Schema／因果／證據合法率：{metrics['schema_and_causality']:.2%}（門檻 {thresholds['schema_and_causality']:.0%}）",
        f"- 交易／等待／移除權限一致率：{metrics['permission']:.2%}（門檻 {thresholds['permission']:.0%}）",
        f"- 主要情境＋左右階段一致率：{metrics['scenario_and_phase']:.2%}（門檻 {thresholds['scenario_and_phase']:.0%}）",
        "",
        "不一致的語意 gate 在合併樣本中一律降為 `UNKNOWN`，不由程式補成 `PASS`。若本測試未通過，不得開始正式 V3 績效回放。",
        "",
        "## 保守合併後權限",
        "",
        "| 權限／路徑 | 筆數 |",
        "|---|---:|",
    ]
    lines.extend(f"| `{key}` | {value} |" for key, value in sorted(permission_counts.items()))
    (CONSISTENCY / "consistency_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return result


if __name__ == "__main__":
    payload = evaluate()
    print(json.dumps({key: payload[key] for key in ("passed", "thresholds", "metrics", "permission_counts_after_conservative_merge")}, ensure_ascii=False, indent=2))
