"""Attach read-only future outcome evidence to frozen semantic comparisons."""

from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from .semantic_disagreement import DEFAULT_FORWARD_HORIZONS, attach_forward_outcomes
from .state import DEFAULT_RUNTIME_ROOT, read_json, resolve_run_directory, write_json_atomic


class PosthocAdjudicationError(RuntimeError):
    pass


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise PosthocAdjudicationError(
                f"{path.name}第{line_number}行不是有效JSON。"
            ) from exc
        if not isinstance(value, dict):
            raise PosthocAdjudicationError(
                f"{path.name}第{line_number}行必須是JSON object。"
            )
        rows.append(value)
    return rows


def _read_bars(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise PosthocAdjudicationError("找不到回放bars.csv。")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    required = {"session", "bar_time", "high", "low", "close"}
    if not rows or not required.issubset(rows[0]):
        raise PosthocAdjudicationError("bars.csv缺少盤後結果計算所需欄位。")
    return rows


def adjudicate_run(
    run_id: str,
    *,
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
    horizons: Sequence[int] = DEFAULT_FORWARD_HORIZONS,
) -> dict[str, Any]:
    """Create a post-hoc artifact without mutating any causal replay state."""

    run_dir = resolve_run_directory(run_id, runtime_root=runtime_root)
    manifest = read_json(run_dir / "manifest.json")
    if not isinstance(manifest, Mapping):
        raise PosthocAdjudicationError("回放manifest不存在或格式無效。")
    if manifest.get("status") == "running":
        raise PosthocAdjudicationError("回放仍在執行；必須先凍結或暫停才能進行盤後審核。")
    comparisons = _read_jsonl(run_dir / "semantic-comparisons.jsonl")
    if not comparisons:
        raise PosthocAdjudicationError("此run沒有AI／程式語意比較紀錄。")
    bars = _read_bars(run_dir / "bars.csv")

    reviewed: list[dict[str, Any]] = []
    for comparison in comparisons:
        stage = str(comparison.get("stage") or "")
        session = "day" if stage == "day" else "night"
        stage_bars = [item for item in bars if item.get("session") == session]
        reviewed.append(
            attach_forward_outcomes(
                comparison,
                stage_bars,
                horizons=horizons,
            )
        )
    payload = {
        "version": 1,
        "run_id": run_dir.name,
        "source_run_status": manifest.get("status"),
        "rule_version": manifest.get("rule_version"),
        "execution_version": manifest.get("execution_version"),
        "ai_model": manifest.get("ai_model"),
        "analysis_mode": manifest.get("analysis_mode"),
        "human_intervention": manifest.get("human_intervention"),
        "generated_at": datetime.now().astimezone().isoformat(),
        "horizons": [int(value) for value in horizons],
        "comparison_count": len(reviewed),
        "unresolved_difference_count": sum(
            len(item.get("differences") or []) for item in reviewed
        ),
        "winner_count": 0,
        "comparisons": reviewed,
        "policy": (
            "未來K棒只作盤後結果證據；本工具不修改原分析、memory、持倉、"
            "交易統計或Telegram訊息，也不以單次漲跌自動裁定課程判讀勝負。"
        ),
    }
    write_json_atomic(run_dir / "posthoc-semantic-adjudication.json", payload)
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="為已凍結的AI_HYBRID回放附加盤後未來K棒結果證據。"
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--horizons",
        default=",".join(str(value) for value in DEFAULT_FORWARD_HORIZONS),
        help="以逗號分隔的未來K棒根數，預設3,5,10,20。",
    )
    args = parser.parse_args(argv)
    try:
        horizons = tuple(int(value.strip()) for value in args.horizons.split(",") if value.strip())
        if not horizons:
            raise ValueError
        result = adjudicate_run(args.run_id, horizons=horizons)
    except (ValueError, PosthocAdjudicationError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    print(
        json.dumps(
            {
                "ok": True,
                "run_id": result["run_id"],
                "comparison_count": result["comparison_count"],
                "unresolved_difference_count": result["unresolved_difference_count"],
                "winner_count": result["winner_count"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
