"""Build outcome-blind evidence summaries for V2 four-scenario semantics.

The input is the already separated calibration case file.  This module never
opens the sealed reproduction answers or a performance/backtest artifact.  It
does not decide trades; it only makes the legacy calibration evidence auditable
before a new reproducible specification is written.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median
from typing import Any, Iterable


VERSION = "v2-core-semantic-evidence-v1"
POSITIVE_KIND = "POSITIVE_REFERENCE（舊V2核准正例）"
V1_ONLY_KIND = "HARD_NEGATIVE_V1_ONLY（V1核准但V2未核准）"
NEARBY_KIND = "HARD_NEGATIVE_NEARBY（同股相鄰未觸發日）"
OBJECTIVE_KIND = "HARD_NEGATIVE_OBJECTIVE_CHALLENGE（客觀觸發型不交易挑戰日）"
SCENARIOS = (
    "MATURE_TREND_PULLBACK",
    "MACRO_COPY_RESONANCE",
    "BEAR_REVERSAL_LEFT_RIGHT",
    "FRESH_Q1_EXPANSION",
)

# Multi-label, text-evidence index.  It is an inventory of legacy reason wording,
# not a new classifier and not a replacement for reading the source packet.
NEGATIVE_REASON_PATTERNS: dict[str, tuple[str, ...]] = {
    "ANCHOR_OR_PARENT_INCOMPLETE（定錨或父代不完整）": (
        r"父代.*(?:不完整|未完成|不足|沒有)",
        r"(?:缺|沒有|未有).{0,8}(?:父代|定錨)",
        r"定錨.{0,12}(?:不完整|不清|不足|未確認)",
    ),
    "CAMPAIGN_OR_LARGE_STRUCTURE_INVALID（朝代或大結構無效）": (
        r"(?:長多|多頭|父代|大結構).{0,14}(?:破壞|失效|未重新確認|尚未成立)",
        r"跌破.{0,12}(?:大級|父代|控制|防線|低點)",
        r"(?:空頭|下降).{0,12}(?:仍|延續|控制)",
    ),
    "LONG_MA_HABIT_MISSING（長均線慣性不足）": (
        r"(?:長均線|MA55|MA105|MA144).{0,18}(?:不足|未|仍低於|未收復|不支持|下彎)",
        r"長多慣性.{0,12}(?:不足|未|不成立|尚未)",
    ),
    "CORRECTION_OR_REPLICATION_UNCLEAR（修正或複製關係不清）": (
        r"(?:修正|複製|太極|世代).{0,14}(?:不清|不足|未完成|不成立|無法)",
        r"沒有.{0,12}(?:修正|複製)",
    ),
    "SMALL_TRIGGER_OR_DUAL_SCALE_INCOMPLETE（小級觸發或大小級未同向）": (
        r"(?:小級|小結構).{0,14}(?:未|不足|不完整|沒有|尚未)",
        r"(?:大小級|雙級).{0,14}(?:未|不同向|衝突|不足|不完整)",
        r"仍低於.{0,12}(?:大高|大級|控制高|壓力)",
    ),
    "BEAR_REVERSAL_EVIDENCE_INCOMPLETE（空頭反轉證據不足）": (
        r"(?:空頭|大空).{0,16}(?:防線|末段|左右|反轉).{0,16}(?:不足|未|沒有|不完整)",
        r"未突破.{0,12}(?:空頭|大空|下降).{0,8}(?:防線|壓力|結構)",
    ),
    "FRESH_ANCHOR_QUALITY_INCOMPLETE（新生定錨品質不足）": (
        r"(?:新生|新錨|Q1).{0,18}(?:不足|未|不完整|不成立|不能)",
        r"(?:CLEAN|MEATY|DESTRUCTIVE|TRACEABLE).{0,20}(?:不足|未|不全|FAIL)",
        r"(?:破壞性|有肉|乾淨度|可追溯).{0,16}(?:不足|未|不夠)",
    ),
    "LOCATION_SPACE_OR_RISK_POOR（位階、空間或風險不佳）": (
        r"(?:位階|空間|肉|追價|末端|過高|延伸|風險).{0,18}(?:不足|惡化|過大|不佳|太遠|有限|過高|已)",
        r"(?:EARLY_LOCATION_WITH_SPACE|NOT_Q3_OR_EXHAUSTED).{0,20}(?:未|不足|FAIL|不全)",
        r"(?:逾|超過|距離).{0,8}\d+(?:\.\d+)?%",
    ),
    "CAUSAL_STOP_MISSING_OR_TOO_FAR（因果防線缺失或過遠）": (
        r"(?:防線|失效點|stop).{0,14}(?:不存在|沒有|未|不足|過遠|太遠|不合理)",
        r"找不到.{0,8}(?:防線|失效)",
    ),
    "Q3_EXHAUSTION_OR_LATE_STAGE（Q3、耗竭或末段）": (
        r"Q3|耗竭|末升|末端加速|後代弱化|第三次攻擊|第五段|斜率.*(?:衰退|降低)",
    ),
    "SINGLE_INDICATOR_OR_BREAK_ONLY（只有指標或單次突破）": (
        r"只有.{0,16}(?:均線|MACD|紅K|突破|20日高|量)",
        r"僅.{0,16}(?:均線|MACD|紅K|突破|20日高|量|小級)",
    ),
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def counter_dict(values: Iterable[Any]) -> dict[str, int]:
    return dict(sorted(Counter(str(value) for value in values if value is not None).items()))


def distribution(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"count": 0, "min": None, "median": None, "max": None}
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "min": round(ordered[0], 4),
        "median": round(median(ordered), 4),
        "max": round(ordered[-1], 4),
    }


def scenario_summary(cases: list[dict[str, Any]], scenario: str) -> dict[str, Any]:
    selected = [case for case in cases if (case.get("legacy_v2_trigger") or {}).get("scenario") == scenario]
    triggers = [case["legacy_v2_trigger"] for case in selected]
    daily_rows = [case["daily_visible_fact"] for case in selected]
    gate_presence: Counter[str] = Counter()
    gate_results: dict[str, Counter[str]] = defaultdict(Counter)
    stop_risk: list[float] = []
    for case, trigger, daily in zip(selected, triggers, daily_rows):
        for gate, payload in (trigger.get("required_gates") or {}).items():
            gate_presence[gate] += 1
            gate_results[gate][str((payload or {}).get("result"))] += 1
        close = daily.get("close")
        stop = trigger.get("stop_price")
        if isinstance(close, (int, float)) and isinstance(stop, (int, float)) and close:
            stop_risk.append((float(close) - float(stop)) / float(close) * 100.0)

    ma_support = {}
    for ma in ("ma5", "ma13", "ma21", "ma55", "ma105", "ma144"):
        comparable = [row for row in daily_rows if isinstance(row.get("close"), (int, float)) and isinstance(row.get(ma), (int, float))]
        ma_support[ma] = {
            "comparable": len(comparable),
            "close_above": sum(float(row["close"]) > float(row[ma]) for row in comparable),
        }

    return {
        "count": len(selected),
        "trigger_paths": counter_dict(trigger.get("trigger_path") for trigger in triggers),
        "episode_roles": counter_dict(trigger.get("episode_or_add_candidate") for trigger in triggers),
        "anchor_status": counter_dict((trigger.get("macro_anchor") or {}).get("status") for trigger in triggers),
        "anchor_direction": counter_dict((trigger.get("macro_anchor") or {}).get("direction") for trigger in triggers),
        "taiji_generation": counter_dict(trigger.get("taiji_generation") for trigger in triggers),
        "large_quadrant": counter_dict(trigger.get("large_quadrant") for trigger in triggers),
        "small_quadrant": counter_dict(trigger.get("small_quadrant") for trigger in triggers),
        "large_dow": counter_dict(trigger.get("large_dow") for trigger in triggers),
        "small_dow": counter_dict(trigger.get("small_dow") for trigger in triggers),
        "left_right": counter_dict(trigger.get("left_right") for trigger in triggers),
        "daily_facts": counter_dict(fact for row in daily_rows for fact in (row.get("facts") or [])),
        "signal_return_1d_pct": distribution([float(row["return_1d_pct"]) for row in daily_rows if isinstance(row.get("return_1d_pct"), (int, float))]),
        "volume_ratio_20": distribution([float(row["volume_ratio_20"]) for row in daily_rows if isinstance(row.get("volume_ratio_20"), (int, float))]),
        "episode_stop_risk_pct": distribution(stop_risk),
        "ma_support": ma_support,
        "required_gate_presence": dict(sorted(gate_presence.items())),
        "required_gate_results": {gate: dict(sorted(results.items())) for gate, results in sorted(gate_results.items())},
        "case_ids": [case["case_id"] for case in selected],
    }


def classify_reason(reason: str) -> list[str]:
    labels = [label for label, patterns in NEGATIVE_REASON_PATTERNS.items() if any(re.search(pattern, reason, re.IGNORECASE) for pattern in patterns)]
    return labels or ["UNCLASSIFIED_LEGACY_WORDING（舊理由文字未歸類）"]


def summarize_negatives(cases: list[dict[str, Any]]) -> dict[str, Any]:
    kinds = counter_dict(case.get("case_kind") for case in cases if case.get("case_kind") != POSITIVE_KIND)
    v1_only = [case for case in cases if case.get("case_kind") == V1_ONLY_KIND]
    label_counts: Counter[str] = Counter()
    label_examples: dict[str, list[dict[str, str]]] = defaultdict(list)
    for case in v1_only:
        reason = str(case.get("legacy_v2_no_trade_reason") or "")
        for label in classify_reason(reason):
            label_counts[label] += 1
            if len(label_examples[label]) < 5:
                label_examples[label].append({
                    "case_id": str(case["case_id"]),
                    "batch_id": str(case["batch_id"]),
                    "code": str(case["code"]),
                    "as_of": str(case["as_of"]),
                    "reason": reason,
                })

    objective = [case for case in cases if case.get("case_kind") == OBJECTIVE_KIND]
    nearby = [case for case in cases if case.get("case_kind") == NEARBY_KIND]
    positives_by_stock: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for case in cases:
        if case.get("case_kind") == POSITIVE_KIND:
            positives_by_stock[(str(case["batch_id"]), str(case["code"]))].append(case)
    lost_facts: Counter[str] = Counter()
    gained_facts: Counter[str] = Counter()
    for case in nearby:
        stock_positives = positives_by_stock[(str(case["batch_id"]), str(case["code"]))]
        if not stock_positives:
            continue
        nearest = min(stock_positives, key=lambda item: abs((__import__("datetime").date.fromisoformat(str(item["as_of"])) - __import__("datetime").date.fromisoformat(str(case["as_of"]))).days))
        positive_facts = set((nearest.get("daily_visible_fact") or {}).get("facts") or [])
        nearby_facts = set((case.get("daily_visible_fact") or {}).get("facts") or [])
        lost_facts.update(positive_facts - nearby_facts)
        gained_facts.update(nearby_facts - positive_facts)

    return {
        "case_kinds": kinds,
        "v1_only_reason_categories_multilabel": dict(sorted(label_counts.items(), key=lambda item: (-item[1], item[0]))),
        "v1_only_reason_examples": dict(sorted(label_examples.items())),
        "objective_challenge_facts": counter_dict(fact for case in objective for fact in (case.get("daily_visible_fact") or {}).get("facts") or []),
        "nearby_facts_missing_vs_nearest_positive": dict(sorted(lost_facts.items(), key=lambda item: (-item[1], item[0]))),
        "nearby_facts_new_vs_nearest_positive": dict(sorted(gained_facts.items(), key=lambda item: (-item[1], item[0]))),
        "interpretation_limits": [
            "V1-only理由多為整段監控期的舊AI摘要；只有理由明確指向該日時才可作逐日原子標籤。",
            "相鄰未觸發日只證明舊V2沒有列入trigger，不等於舊AI逐欄提交完整WAIT。",
            "客觀挑戰日由可見flags選出，不代表舊AI明確選定該日作拒絕判斷。",
            "文字分類是多標籤索引，不是新交易規則。",
        ],
    }


def validate_positive_invariants(cases: list[dict[str, Any]]) -> dict[str, Any]:
    violations: list[dict[str, str]] = []
    positives = [case for case in cases if case.get("case_kind") == POSITIVE_KIND]
    for case in positives:
        trigger = case.get("legacy_v2_trigger") or {}
        as_of = str(case.get("as_of"))
        if trigger.get("signal_date") != as_of:
            violations.append({"case_id": case["case_id"], "rule": "SIGNAL_DATE_EQUALS_AS_OF"})
        for date_name, value in (
            ("ANCHOR_START_NOT_FUTURE", (trigger.get("macro_anchor") or {}).get("start")),
            ("ANCHOR_END_NOT_FUTURE", (trigger.get("macro_anchor") or {}).get("end")),
            ("STOP_DATE_NOT_FUTURE", trigger.get("stop_date")),
        ):
            if value and str(value) > as_of:
                violations.append({"case_id": case["case_id"], "rule": date_name})
        for gate, payload in (trigger.get("required_gates") or {}).items():
            if gate == "PROFIT_ONLY_ADD_ELIGIBILITY":
                continue
            if (payload or {}).get("result") != "PASS":
                violations.append({"case_id": case["case_id"], "rule": f"REQUIRED_GATE_{gate}_PASS"})
        close = (case.get("daily_visible_fact") or {}).get("close")
        stop = trigger.get("stop_price")
        if not isinstance(close, (int, float)) or not isinstance(stop, (int, float)) or stop >= close:
            violations.append({"case_id": case["case_id"], "rule": "STOP_BELOW_SIGNAL_CLOSE"})
    return {"positive_count": len(positives), "violation_count": len(violations), "violations": violations}


def duplicate_positive_analysis(cases: list[dict[str, Any]]) -> dict[str, Any]:
    """Expose cross-batch duplicate labels instead of silently overweighting them."""

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for case in cases:
        if case.get("case_kind") == POSITIVE_KIND:
            grouped[(str(case["code"]), str(case["as_of"]))].append(case)
    duplicates = {key: rows for key, rows in grouped.items() if len(rows) > 1}
    details: list[dict[str, Any]] = []
    for (code, as_of), rows in sorted(duplicates.items()):
        scenarios = [(row.get("legacy_v2_trigger") or {}).get("scenario") for row in rows]
        paths = [(row.get("legacy_v2_trigger") or {}).get("trigger_path") for row in rows]
        stop_dates = [(row.get("legacy_v2_trigger") or {}).get("stop_date") for row in rows]
        stop_prices = [(row.get("legacy_v2_trigger") or {}).get("stop_price") for row in rows]
        details.append(
            {
                "status": (
                    "LEGACY_JUDGEMENT_QUESTIONABLE（舊判讀存在疑義）"
                    if len(set(scenarios)) > 1
                    else "DUPLICATE_REFERENCE_DIFFERENT_DETAIL（重複參考、細節不同）"
                ),
                "code": code,
                "as_of": as_of,
                "case_ids": [str(row["case_id"]) for row in rows],
                "batch_ids": [str(row["batch_id"]) for row in rows],
                "scenarios": scenarios,
                "trigger_paths": paths,
                "stop_dates": stop_dates,
                "stop_prices": stop_prices,
                "scenario_conflict": len(set(scenarios)) > 1,
                "trigger_path_conflict": len(set(paths)) > 1,
                "stop_date_conflict": len(set(stop_dates)) > 1,
                "stop_price_conflict": len(set(stop_prices)) > 1,
            }
        )
    return {
        "row_level_positive_count": sum(len(rows) for rows in grouped.values()),
        "unique_stock_date_count": len(grouped),
        "duplicate_stock_date_groups": len(duplicates),
        "scenario_conflict_groups": sum(bool(item["scenario_conflict"]) for item in details),
        "trigger_path_conflict_groups": sum(bool(item["trigger_path_conflict"]) for item in details),
        "stop_date_conflict_groups": sum(bool(item["stop_date_conflict"]) for item in details),
        "stop_price_conflict_groups": sum(bool(item["stop_price_conflict"]) for item in details),
        "details": details,
    }


def build_summary(cases_path: Path) -> dict[str, Any]:
    cases = read_jsonl(cases_path)
    positives = [case for case in cases if case.get("case_kind") == POSITIVE_KIND]
    return {
        "version": VERSION,
        "status": "CALIBRATION_EVIDENCE（校準證據）",
        "input": {"path": str(cases_path.resolve()), "sha256": sha256(cases_path), "rows": len(cases)},
        "future_performance_used": False,
        "sealed_answers_opened": False,
        "positive_count": len(positives),
        "positive_scenario_counts": counter_dict((case.get("legacy_v2_trigger") or {}).get("scenario") for case in positives),
        "positive_invariants": validate_positive_invariants(cases),
        "duplicate_positive_analysis": duplicate_positive_analysis(cases),
        "scenarios": {scenario: scenario_summary(positives, scenario) for scenario in SCENARIOS},
        "negatives": summarize_negatives(cases),
    }


def render_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# V2四情境結果盲化語意證據摘要",
        "",
        f"版本：`{summary['version']}`  ",
        f"狀態：`{summary['status']}`  ",
        f"輸入SHA-256：`{summary['input']['sha256']}`  ",
        "未讀取sealed鎖定答案；未使用MFE、MAE、損益或出場結果。",
        "",
        "## 正例總覽",
        "",
        "| 情境 | 正例 | 觸發路徑 | 防線風險中位數 |",
        "|---|---:|---|---:|",
    ]
    for scenario in SCENARIOS:
        data = summary["scenarios"][scenario]
        paths = "、".join(f"{key}({value})" for key, value in data["trigger_paths"].items())
        risk = data["episode_stop_risk_pct"]["median"]
        lines.append(f"| `{scenario}` | {data['count']} | {paths} | {risk if risk is not None else '—'}% |")
    lines += [
        "",
        "## 正例因果不變量",
        "",
        f"- 正例：{summary['positive_invariants']['positive_count']}。",
        f"- 違反訊號日／定錨／防線日期／必要gate／防線價位的不變量：{summary['positive_invariants']['violation_count']}。",
        "- `PROFIT_ONLY_ADD_ELIGIBILITY`是加碼資格，不列入母單共通必要gate。",
        "",
        "## 跨批次重複正例",
        "",
        f"- 列層正例：{summary['duplicate_positive_analysis']['row_level_positive_count']}。",
        f"- 不同股票日：{summary['duplicate_positive_analysis']['unique_stock_date_count']}。",
        f"- 跨批次重複股票日：{summary['duplicate_positive_analysis']['duplicate_stock_date_groups']}。",
        f"- 主要情境衝突：{summary['duplicate_positive_analysis']['scenario_conflict_groups']}。",
        f"- 觸發路徑衝突：{summary['duplicate_positive_analysis']['trigger_path_conflict_groups']}。",
        "- 衝突案例不得以多數決或績效任選一個答案；須在結果盲化狀態重新做課程證據稽核。",
        "",
        "## V1-only舊拒絕理由索引",
        "",
        "以下為多標籤文字索引，不是新規則：",
        "",
    ]
    for label, count in summary["negatives"]["v1_only_reason_categories_multilabel"].items():
        lines.append(f"- `{label}`：{count}")
    lines += [
        "",
        "## 解讀限制",
        "",
    ]
    lines.extend(f"- {item}" for item in summary["negatives"]["interpretation_limits"])
    lines += [
        "",
        "完整逐情境gate、象限、太極、道氏、觸發flags與案例ID請見同名JSON。",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    summary = build_summary(args.cases)
    json_path = args.output_dir / "semantic_evidence_summary.json"
    md_path = args.output_dir / "semantic_evidence_summary.md"
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(summary), encoding="utf-8")
    print(json.dumps({"json": str(json_path), "markdown": str(md_path), "positive_count": summary["positive_count"], "violations": summary["positive_invariants"]["violation_count"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
