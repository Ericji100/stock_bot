"""Compare outcome-blind reviews of the six cross-batch V2 conflicts.

This is a calibration comparator, not the three-run production-consistency
test.  It deliberately does not open legacy answers, sealed identities, or
performance files.  Its purpose is to expose which semantic fields still
depend on reviewer interpretation before R1 can be frozen.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from itertools import combinations
from pathlib import Path
from typing import Any, Iterable


VERSION = "v2-core-blind-review-compare-v1"
EXPECTED_MODEL = "gpt-5.6-sol"
EXPECTED_EFFORT = "xhigh"
SCENARIOS = {
    "MATURE_TREND_PULLBACK",
    "MACRO_COPY_RESONANCE",
    "BEAR_REVERSAL_LEFT_RIGHT",
    "FRESH_Q1_EXPANSION",
    "UNRESOLVED_NO_TRADE",
}
PERMISSION_ALIASES = {
    "TRADE": "TRADE_APPROVED",
    "TRADE_APPROVED": "TRADE_APPROVED",
    "WAIT": "WAIT",
    "UNKNOWN": "UNKNOWN",
    "REMOVE": "REMOVE",
}
DIRECTION_ALIASES = {"UP": "BULL", "BULL": "BULL", "DOWN": "BEAR", "BEAR": "BEAR"}
ROUTE_ALIASES = {
    ("MATURE_TREND_PULLBACK", "Q4_TO_Q1_RELAUNCH"): "MATURE_Q4_TO_Q1_RELAUNCH",
    ("MATURE_TREND_PULLBACK", "SMALL_DOW_REVERSAL"): "MATURE_SMALL_DOW_REVERSAL",
    ("MATURE_TREND_PULLBACK", "MA_HABIT_RECLAIM_WITH_STRUCTURE"): "MATURE_MA_HABIT_RECLAIM_WITH_STRUCTURE",
    ("MATURE_TREND_PULLBACK", "FIRST_SHALLOW_PULLBACK"): "MATURE_FIRST_SHALLOW_PULLBACK",
    ("MACRO_COPY_RESONANCE", "BREAK_THEN_RETEST"): "MACRO_BREAK_THEN_RETEST",
    ("MACRO_COPY_RESONANCE", "SMALL_REANCHOR_RELAUNCH"): "MACRO_SMALL_REANCHOR_RELAUNCH",
    ("MACRO_COPY_RESONANCE", "DIRECT_TO_RIGHT"): "MACRO_DIRECT_TO_RIGHT",
    ("BEAR_REVERSAL_LEFT_RIGHT", "LR_BREAK"): "BEAR_LR_BREAK",
    ("BEAR_REVERSAL_LEFT_RIGHT", "RL_PULLBACK"): "BEAR_RL_PULLBACK",
    ("BEAR_REVERSAL_LEFT_RIGHT", "RR_REBREAK"): "BEAR_RR_REBREAK",
    ("BEAR_REVERSAL_LEFT_RIGHT", "DIRECT_TO_RIGHT"): "BEAR_DIRECT_TO_RIGHT",
    ("FRESH_Q1_EXPANSION", "INITIAL_DESTRUCTIVE_EXPANSION"): "FRESH_INITIAL_DESTRUCTIVE_EXPANSION",
    ("FRESH_Q1_EXPANSION", "FIRST_SHALLOW_CORRECTION_RELAUNCH"): "FRESH_FIRST_SHALLOW_CORRECTION_RELAUNCH",
    ("FRESH_Q1_EXPANSION", "CONTINUATION_ADD"): "FRESH_CONTINUATION_ADD",
    ("UNRESOLVED_NO_TRADE", "UNKNOWN_ROUTE"): "UNRESOLVED_ROUTE",
}
FORBIDDEN_KEY_TOKENS = {
    "mfe",
    "mae",
    "pnl",
    "profit",
    "return_pct",
    "exit_date",
    "exit_price",
    "future_outcome",
    "legacy_answer",
    "stock_code",
    "stock_name",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
        if not isinstance(row, dict):
            raise ValueError(f"{path}:{line_number}: row is not an object")
        rows.append(row)
    return rows


def nested_keys(value: Any, prefix: str = "") -> Iterable[str]:
    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            yield path
            yield from nested_keys(child, path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from nested_keys(child, f"{prefix}[{index}]")


def _normal_permission(value: Any) -> str | None:
    if value is None:
        return None
    return PERMISSION_ALIASES.get(str(value), str(value))


def _normal_direction(value: Any) -> str | None:
    if value is None:
        return None
    return DIRECTION_ALIASES.get(str(value), str(value))


def _normal_route(value: Any, scenario: Any) -> str | None:
    if value is None:
        return None
    raw = str(value)
    return ROUTE_ALIASES.get((str(scenario), raw), raw)


def normalize(row: dict[str, Any]) -> dict[str, Any]:
    anchor_start = row.get("anchor_start") or row.get("controlling_anchor") or {}
    anchor_end = row.get("anchor_end_or_forming") or row.get("controlling_anchor") or {}
    scenario = row.get("primary_scenario")
    trigger_state = row.get("trigger_state") or row.get("route_state") or row.get("trigger_status")
    if isinstance(trigger_state, str) and trigger_state.startswith("TRIGGERED"):
        trigger_state = "TRIGGERED"
    return {
        "review_id": row.get("review_id"),
        "as_of": row.get("as_of"),
        "model": row.get("model"),
        "reasoning_effort": row.get("reasoning_effort"),
        "review_scope": row.get("review_scope") or "BLIND_BOUNDARY_REVIEW",
        "primary_scenario": scenario,
        "canonical_trigger_route": _normal_route(row.get("canonical_trigger_route") or row.get("canonical_route"), scenario),
        "trigger_state": trigger_state,
        "permission": _normal_permission(row.get("permission")),
        "anchor_start_date": anchor_start.get("date") or anchor_start.get("start_date"),
        "anchor_start_price": anchor_start.get("price") if anchor_start.get("price") is not None else anchor_start.get("start_price"),
        "anchor_start_direction": _normal_direction(anchor_start.get("direction")),
        "anchor_end_status": anchor_end.get("status"),
        "anchor_end_date": anchor_end.get("date") or anchor_end.get("end_date"),
        "anchor_end_price": anchor_end.get("price") if anchor_end.get("price") is not None else anchor_end.get("end_price"),
        "campaign_maturity": row.get("campaign_maturity"),
        "campaign_generation": row.get("campaign_generation"),
        "taiji_phase": row.get("taiji_phase"),
        "taiji_leg_index": row.get("taiji_leg_index"),
        "large_direction": _normal_direction(row.get("large_direction")),
        "small_direction": _normal_direction(row.get("small_direction")),
        "episode_stop_date": row.get("episode_stop_date"),
        "episode_stop_price": row.get("episode_stop_price"),
        "position_role": row.get("position_role"),
    }


def validate_review(
    label: str,
    path: Path,
    rows: list[dict[str, Any]],
    manifest_rows: dict[str, dict[str, Any]],
) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, row in enumerate(rows, 1):
        review_id = str(row.get("review_id") or "")
        if not review_id:
            issues.append({"reviewer": label, "review_id": f"row:{index}", "issue": "MISSING_REVIEW_ID"})
            continue
        if review_id in seen:
            issues.append({"reviewer": label, "review_id": review_id, "issue": "DUPLICATE_REVIEW_ID"})
        seen.add(review_id)
        expected = manifest_rows.get(review_id)
        if expected is None:
            issues.append({"reviewer": label, "review_id": review_id, "issue": "ID_NOT_IN_MANIFEST"})
        elif row.get("as_of") != expected.get("as_of"):
            issues.append({"reviewer": label, "review_id": review_id, "issue": "AS_OF_MISMATCH"})
        if row.get("model") != EXPECTED_MODEL:
            issues.append({"reviewer": label, "review_id": review_id, "issue": "MODEL_MISMATCH"})
        if row.get("reasoning_effort") != EXPECTED_EFFORT:
            issues.append({"reviewer": label, "review_id": review_id, "issue": "EFFORT_MISMATCH"})
        if row.get("primary_scenario") not in SCENARIOS:
            issues.append({"reviewer": label, "review_id": review_id, "issue": "INVALID_PRIMARY_SCENARIO"})
        permission = _normal_permission(row.get("permission"))
        if permission not in set(PERMISSION_ALIASES.values()):
            issues.append({"reviewer": label, "review_id": review_id, "issue": "INVALID_PERMISSION"})
        required_exact = {
            "canonical_trigger_route",
            "anchor_start",
            "anchor_end_or_forming",
            "parent_correction_replication_relationship",
            "campaign_maturity",
            "taiji_phase",
            "taiji_leg_index",
            "large_direction",
            "small_direction",
            "episode_stop_date",
            "episode_stop_price",
            "permission",
            "evidence",
            "uncertainties",
        }
        for missing_key in sorted(required_exact - set(row)):
            issues.append({"reviewer": label, "review_id": review_id, "issue": f"MISSING_REQUIRED_KEY:{missing_key}"})
        if not any(key in row for key in ("trigger_state", "route_state", "trigger_status")):
            issues.append({"reviewer": label, "review_id": review_id, "issue": "MISSING_REQUIRED_KEY:trigger_state"})
        for key_path in nested_keys(row):
            leaf = key_path.rsplit(".", 1)[-1].split("[", 1)[0].lower()
            if leaf in FORBIDDEN_KEY_TOKENS:
                issues.append({"reviewer": label, "review_id": review_id, "issue": f"FORBIDDEN_KEY:{key_path}"})
    missing = sorted(set(manifest_rows) - seen)
    extra = sorted(seen - set(manifest_rows))
    for review_id in missing:
        issues.append({"reviewer": label, "review_id": review_id, "issue": "MISSING_MANIFEST_CASE"})
    for review_id in extra:
        issues.append({"reviewer": label, "review_id": review_id, "issue": "EXTRA_CASE"})
    if not path.is_file():
        issues.append({"reviewer": label, "review_id": "FILE", "issue": "FILE_NOT_FOUND"})
    return issues


def _equal(field: str, left: Any, right: Any) -> bool:
    if field.endswith("_price") and isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return abs(float(left) - float(right)) <= 0.0001
    return left == right


def compare_reviews(
    manifest_path: Path,
    review_paths: dict[str, Path],
) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_rows = {str(row["review_id"]): row for row in manifest["rows"]}
    normalized: dict[str, dict[str, dict[str, Any]]] = {}
    issues: list[dict[str, str]] = []
    inputs: dict[str, Any] = {}
    for label, path in review_paths.items():
        rows = read_jsonl(path)
        issues.extend(validate_review(label, path, rows, manifest_rows))
        normalized[label] = {str(row["review_id"]): normalize(row) for row in rows if row.get("review_id")}
        inputs[label] = {"path": str(path), "sha256": sha256(path), "rows": len(rows)}

    fields = [
        "primary_scenario",
        "canonical_trigger_route",
        "trigger_state",
        "permission",
        "anchor_start_date",
        "anchor_start_price",
        "anchor_end_status",
        "anchor_end_date",
        "anchor_end_price",
        "campaign_maturity",
        "campaign_generation",
        "taiji_phase",
        "taiji_leg_index",
        "large_direction",
        "small_direction",
        "episode_stop_date",
        "episode_stop_price",
        "position_role",
    ]
    pairwise: dict[str, Any] = {}
    for left_label, right_label in combinations(review_paths, 2):
        key = f"{left_label}__vs__{right_label}"
        field_stats: dict[str, Any] = {}
        for field in fields:
            comparable = 0
            matches = 0
            for review_id in manifest_rows:
                left = normalized[left_label].get(review_id, {}).get(field)
                right = normalized[right_label].get(review_id, {}).get(field)
                if left is None or right is None:
                    continue
                comparable += 1
                matches += int(_equal(field, left, right))
            field_stats[field] = {
                "comparable": comparable,
                "matches": matches,
                "agreement_pct": round(matches / comparable * 100.0, 2) if comparable else None,
            }
        pairwise[key] = field_stats

    cases: list[dict[str, Any]] = []
    unanimous_counts: Counter[str] = Counter()
    for review_id, manifest_row in manifest_rows.items():
        reviewers = {label: normalized[label].get(review_id) for label in review_paths}
        unanimity: dict[str, bool | None] = {}
        for field in fields:
            values = [row.get(field) for row in reviewers.values() if row and row.get(field) is not None]
            unanimity[field] = None if len(values) != len(review_paths) else all(_equal(field, values[0], value) for value in values[1:])
            if unanimity[field] is True:
                unanimous_counts[field] += 1
        cases.append({
            "review_id": review_id,
            "anonymous_stock_id": manifest_row.get("anonymous_stock_id"),
            "as_of": manifest_row.get("as_of"),
            "reviewers": reviewers,
            "unanimity": unanimity,
        })

    return {
        "version": VERSION,
        "status": "CALIBRATION_COMPARISON（校準比較）",
        "scope_warning": "這不是正式三輪一致性測試；route審核者的任務範圍與兩位boundary審核者不同。",
        "future_performance_used": False,
        "legacy_answers_opened": False,
        "manifest": {"path": str(manifest_path), "sha256": sha256(manifest_path), "cases": len(manifest_rows)},
        "inputs": inputs,
        "validation": {"passed": not issues, "issues": issues},
        "unanimous_counts": {field: unanimous_counts[field] for field in fields},
        "pairwise": pairwise,
        "cases": cases,
    }


def render_markdown(result: dict[str, Any]) -> str:
    lines = [
        "# V2 四情境跨批次衝突盲化審核比較",
        "",
        f"- 狀態：`{result['status']}`",
        f"- 案例數：{result['manifest']['cases']}",
        f"- 審核輸入：{len(result['inputs'])}",
        f"- 驗證：{'PASS（通過）' if result['validation']['passed'] else 'FAIL（未通過）'}",
        "- 未來績效：未使用",
        "- 舊AI答案：未解封",
        f"- 範圍提醒：{result['scope_warning']}",
        "",
        "## 全體一致案例數",
        "",
        "| 欄位 | 全體一致／總案例 |",
        "|---|---:|",
    ]
    for field, count in result["unanimous_counts"].items():
        lines.append(f"| `{field}` | {count}/{result['manifest']['cases']} |")
    lines.extend(["", "## 逐案比較", ""])
    labels = list(result["inputs"])
    lines.append("| 案例 | 日期 | 審核者 | 主要情境 | 路徑 | 觸發 | 權限 | 定錨起點 | 停損 |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for case in result["cases"]:
        first = True
        for label in labels:
            row = case["reviewers"].get(label) or {}
            stop = f"{row.get('episode_stop_date') or '-'} @ {row.get('episode_stop_price') if row.get('episode_stop_price') is not None else '-'}"
            anchor = f"{row.get('anchor_start_date') or '-'} @ {row.get('anchor_start_price') if row.get('anchor_start_price') is not None else '-'}"
            lines.append(
                "| {case_id} | {as_of} | `{label}` | `{scenario}` | `{route}` | `{trigger}` | `{permission}` | {anchor} | {stop} |".format(
                    case_id=case["review_id"] if first else "",
                    as_of=case["as_of"] if first else "",
                    label=label,
                    scenario=row.get("primary_scenario") or "-",
                    route=row.get("canonical_trigger_route") or "-",
                    trigger=row.get("trigger_state") or "-",
                    permission=row.get("permission") or "-",
                    anchor=anchor,
                    stop=stop,
                )
            )
            first = False
    lines.extend(["", "## 兩兩一致率", ""])
    for pair, stats in result["pairwise"].items():
        lines.append(f"### `{pair}`")
        lines.append("")
        lines.append("| 欄位 | 可比 | 相同 | 一致率 |")
        lines.append("|---|---:|---:|---:|")
        for field, stat in stats.items():
            rate = "-" if stat["agreement_pct"] is None else f"{stat['agreement_pct']:.2f}%"
            lines.append(f"| `{field}` | {stat['comparable']} | {stat['matches']} | {rate} |")
        lines.append("")
    if result["validation"]["issues"]:
        lines.extend(["## 驗證問題", ""])
        for issue in result["validation"]["issues"]:
            lines.append(f"- `{issue['reviewer']}` / `{issue['review_id']}`：`{issue['issue']}`")
        lines.append("")
    lines.extend([
        "## 解讀限制",
        "",
        "- 本報告只用來修正四情境語意邊界與資料封包，不可當成正式績效或正式三輪一致性結果。",
        "- 結構分類、交易權限與觸發路徑必須分開看；其中一項一致，不代表整筆交易判讀已一致。",
        "- 若分歧源自封包缺少長期日線或長均線慣性證據，應建立新版封包後重新盲審，不應直接以多數決凍結答案。",
    ])
    return "\n".join(lines) + "\n"


def parse_review(value: str) -> tuple[str, Path]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("review must be LABEL=PATH")
    label, raw_path = value.split("=", 1)
    if not label or not raw_path:
        raise argparse.ArgumentTypeError("review must be LABEL=PATH")
    return label, Path(raw_path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--review", action="append", type=parse_review, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-md", type=Path, required=True)
    args = parser.parse_args()
    review_paths = dict(args.review)
    if len(review_paths) < 2:
        parser.error("at least two distinct --review inputs are required")
    result = compare_reviews(args.manifest, review_paths)
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.output_md.write_text(render_markdown(result), encoding="utf-8")


if __name__ == "__main__":
    main()
