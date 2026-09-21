"""Apply the final, outcome-blind semantic QA decisions to the 2023-H2 AI ledgers.

This is not a signal generator.  Every edit below records a specific AI review
decision made before the performance replay was unsealed.  The script exists so
the corrections are explicit, reviewable, and repeatable instead of being hidden
inside an ad-hoc JSONL rewrite.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
RUN = Path(
    os.environ.get(
        "FORMAL_AI_RUN",
        ROOT / "reports/course_backtest/2024-02-02/historical_scan_2023h2_formal_ai_v1_v2",
    )
).resolve()


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def replace_text(value: Any, replacements: dict[str, str]) -> Any:
    if isinstance(value, str):
        for before, after in replacements.items():
            value = value.replace(before, after)
        return value
    if isinstance(value, list):
        return [replace_text(item, replacements) for item in value]
    if isinstance(value, dict):
        return {key: replace_text(item, replacements) for key, item in value.items()}
    return value


def write_jsonl_atomic(path: Path, rows: list[dict[str, Any]]) -> None:
    temp = path.with_suffix(path.suffix + ".qa_tmp")
    temp.write_text(
        "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )
    temp.replace(path)


def mcr_3663(trigger: dict[str, Any]) -> dict[str, Any]:
    trigger["scenario"] = "MACRO_COPY_RESONANCE"
    trigger["trigger_path"] = "SMALL_REANCHOR_RELAUNCH"
    trigger["taiji_generation"] = "COPY_LEG_3"
    trigger["large_quadrant"] = "Q4"
    trigger["small_quadrant"] = "Q1"
    trigger["large_dow"] = "BULL"
    trigger["small_dow"] = "BULL"
    trigger["left_right"] = "NONE"
    trigger["macro_anchor"] = {
        "start": "2023-07-07",
        "end": "2023-08-02",
        "status": "CONFIRMED",
        "direction": "UP",
        "ai_basis": (
            "截至2023-09-19，2023-07-07低28.701至2023-08-02高42.1012構成已完成向上父代；"
            "父代後修正至2023-08-16低30.4117，仍守父代起點。2023-09-08較高低33.3317確認後，"
            "9月19日收35.3904突破9月4日修正段高35.3414，判為第一代複製的大小級共振。"
        ),
    }
    trigger["evidence"] = [
        "父代2023-07-07@28.701至2023-08-02@42.1012在訊號前完成；修正低2023-08-16@30.4117未破父代起點。",
        "2023-09-19 adjusted OHLC=34.8512/36.7629/34.7532/35.3904，收盤突破2023-09-04@35.3414；未使用期後資料。",
        "小級防線2023-09-08@33.3317於2023-09-13確認，早於訊號且位於來源K棒OHLC內。",
    ]
    trigger["required_gates"] = {
        "COMPLETED_PARENT_ANCHOR": {
            "result": "PASS",
            "evidence": [
                "向上父代由2023-07-07低28.701開始，至2023-08-02高42.1012完成；父高在2023-08-17確認，訊號日2023-09-19以前已可見。"
            ],
        },
        "CORRECTION_INTACT": {
            "result": "PASS",
            "evidence": [
                "父代後於2023-08-16形成修正低30.4117並在2023-08-30確認；30.4117仍高於父代起點28.701，父代未失效。"
            ],
        },
        "TAIJI_GENERATION_MAPPED": {
            "result": "PASS",
            "evidence": [
                "2023-07-07低28.701至08-02高42.1012為ANCHOR_LEG_1，08-02後為修正；2023-09-19收35.3904啟動的向上回合標記COPY_LEG_3（第一代複製）。"
            ],
        },
        "CORRECTION_BEAR_DOW_LINE_CAUSAL": {
            "result": "PASS",
            "evidence": [
                "修正段控制高2023-09-04@35.3414的小級確認日為2023-09-07、大級確認日為2023-09-18，均不晚於2023-09-19訊號收盤。"
            ],
        },
        "SMALL_UP_REANCHOR_BREAK": {
            "result": "PASS",
            "evidence": [
                "2023-09-08低33.3317於09-13確認後未再破低；09-19收35.3904高於09-04控制高35.3414，完成小級低不破低、高過高。"
            ],
        },
        "DUAL_SCALE_LONG_ALIGNMENT": {
            "result": "PASS",
            "evidence": [
                "大級父代低28.701→高42.1012仍有效且等待修正後向上複製；小級在2023-09-19收35.3904重取向上控制，兩級下一可交易方向一致向上。"
            ],
        },
        "NOT_Q3_OR_EXHAUSTED": {
            "result": "PASS",
            "evidence": [
                "2023-09-19日內低34.7532至高36.7629且收35.3904，突破已確認高35.3414；目前為父代後第一代COPY_LEG_3，不是第五段、第三次攻擊或Q3殘餘盤整。"
            ],
        },
        "EPISODE_STOP_CAUSAL": {
            "result": "PASS",
            "evidence": [
                "本episode防線採2023-09-08低33.3317，來源早於訊號且在2023-09-13完成確認；不用遠端父代低28.701放寬風險。"
            ],
        },
    }
    return trigger


def remove_gate_evidence(trigger: dict[str, Any], gate_id: str, exact_text: str) -> None:
    """Remove one duplicated source-fact sentence without changing the AI decision."""
    gate = trigger.get("required_gates", {}).get(gate_id)
    if not gate:
        return
    gate["evidence"] = [item for item in gate.get("evidence", []) if item != exact_text]


def main() -> None:
    packet_manifest = read_json(RUN / "packet_manifest.json")
    packet_hashes = {str(item["code"]): str(item["packet_sha256"]) for item in packet_manifest["items"]}
    paths = sorted(RUN.glob("agent_*_ai_decisions.jsonl"))
    if len(paths) != 5:
        raise RuntimeError(f"expected five formal ledgers, found {len(paths)}")

    before = {path.name: digest(path) for path in paths}
    touched: list[str] = []
    for path in paths:
        rows = read_jsonl(path)
        for row in rows:
            code = str(row["code"])
            row["audit"]["packet_sha256"] = packet_hashes[code]

            # V2 太極中的奇數是攻擊段；2、4是修正段，不能當交易世代名稱。
            row["v2"] = replace_text(
                row["v2"],
                {
                    "COPY_LEG_2": "COPY_LEG_3",
                    "COPY_LEG_4": "COPY_LEG_5",
                    "NEW_ANCHOR_GEN_1": "COPY_LEG_3",
                },
            )

            if code == "2611":
                trigger = row["v2"]["triggers"][0]
                trigger["macro_anchor"]["ai_basis"] = (
                    "截至2023-11-28，2023-10-31低10.0048後形成新生向上定錨；11月22日首次淺修正低"
                    "11.1576已確認，11月28日收11.6516再越11月17日高11.5281。這是V2第一筆母單，"
                    "太極位置為第一次修正後COPY_LEG_3早期。"
                )
                trigger["evidence"][0] = trigger["macro_anchor"]["ai_basis"]
                gate = trigger["required_gates"]["EARLY_TAIJI_GENERATION"]
                gate["evidence"] = [
                    "2023-10-31低10.0048啟動ANCHOR_LEG_1；2023-11-22低11.1576為首次淺修正，11月28日再發動標記COPY_LEG_3，且是V2首次母單而非借用V1 episode。"
                ]
                touched.append("2611:v2:first-mother-and-taiji")

            if code in {"3019", "3031"}:
                original = list(row["v2"].get("triggers") or [])
                if original:
                    row["v2"]["triggers"] = []
                    row["v2"]["stock_status"] = "WATCHING"
                    if code == "3019":
                        row["v2"]["no_trade_reason"] = (
                            "V2_INDEPENDENT_REVIEW_REJECTED：2023-11-22收59.7634雖突破近端已確認高59.2126，"
                            "但上方仍有2023-09-18@61.1405及2023-07-13@64.0782同級壓力；當時只能證明局部"
                            "控制轉強，不能同時證成新生定錨的充分破壞性與剩餘空間。FRESH_Q1_EXPANSION的"
                            "CLEAN_MEATY_DESTRUCTIVE_TRACEABLE及EARLY_LOCATION_WITH_SPACE未全數PASS，V2不交易、續監控。"
                        )
                    else:
                        row["v2"]["no_trade_reason"] = (
                            "V2_INDEPENDENT_REVIEW_REJECTED：2023-11-20收15.5415突破2023-10-11@15.0586，"
                            "但仍低於2023-09-05@15.8928及2023-07-05@18.103；八月至十一月仍是多樞紐"
                            "整理／空方壓力帶，沒有足夠證據判為乾淨、有肉且具破壞性的新生Q1。"
                            "FRESH_Q1_EXPANSION必要條件未全數PASS，V2不交易、續監控。"
                        )
                    touched.append(f"{code}:v2:reject-fqe")

            if code == "3663":
                trigger = row["v2"]["triggers"][0]
                row["v2"]["triggers"][0] = mcr_3663(trigger)
                touched.append("3663:v2:reclassify-mcr")

            if code == "4160":
                row["v2"]["stock_status"] = "WATCHING"
                row["v2"]["triggers"] = []
                row["v2"]["no_trade_reason"] = (
                    "V2_FACTS_REVIEW_REJECTED_AFTER_OFFICIAL_OHLC_REPAIR：櫃買中心2023-12-05官方行情為"
                    "開42.00、高42.00、低41.15、收41.20；換算回凍結資料尺度後收37.7369，低於"
                    "2023-11-29小高38.9276及2023-11-09大高39.0192，因此當日沒有結構突破。"
                    "原FRESH_Q1_EXPANSION觸發撤銷，V2維持監控且未使用績效結果。"
                )
                touched.append("4160:v2:reject-after-official-ohlc-repair")

            if code == "5464":
                for trigger in row["v1"].get("triggers") or []:
                    if trigger.get("signal_date") == "2023-11-14":
                        trigger["evidence"][1] = (
                            "5464 2023-11-14 adjusted OHLC=[28.7689,29.2843,28.1597,28.2066]、"
                            "ATR14=0.6894、MA21/55/105/144=[26.348,26.1756,26.4301,26.5953]、"
                            "MACD hist=0.44014。"
                        )
                    elif trigger.get("signal_date") == "2023-12-04":
                        trigger["evidence"][1] = (
                            "5464 2023-12-04 adjusted OHLC=[28.5814,31.1584,28.5814,31.1584]、"
                            "ATR14=0.6593、MA21/55/105/144=[28.2155,26.8325,26.4884,26.7991]、"
                            "MACD hist=0.66434。"
                        )
                touched.append("5464:v1:indicator-evidence-after-official-repair")

            if code == "5328":
                for version in ("v1", "v2"):
                    for event in row[version].get("watchlist_events") or []:
                        if event.get("event") == "RESELECTED" and event.get("date") == "2023-08-11":
                            event["date"] = "2023-08-14"
                            event["reason"] = (
                                "2023-08-14出現大級campaign失效後第一筆真實上游選股事件，收盤後建立campaign 2；"
                                "2023-08-11移除後至此日收盤前不得觸發。"
                            )
                touched.append("5328:v1-v2:true-reselection-date")

            # The following sentences repeated the same causal pivot fact in two
            # gates of one trigger.  Each destination gate already contains its
            # own dated/priced evidence, so remove only the redundant copy.
            if code == "1442":
                remove_gate_evidence(
                    row["v2"]["triggers"][0],
                    "DYNAMIC_Q1_EXPANSION",
                    "For SMALL scale, 2023-06-14 high 27.9927 confirmed on 2023-06-19 and 2023-06-19 low 26.6457 confirmed on 2023-06-26; both precede the 2023-07-06 close 28.3066.",
                )
                touched.append("1442:v2:deduplicate-gate-evidence")

            if code == "1477":
                remove_gate_evidence(
                    row["v2"]["triggers"][0],
                    "DYNAMIC_QUADRANTS_SUPPORT",
                    "The 2023-07-31 small high 262.7701 has packet SMALL confirmation 2023-08-04; the 2023-08-15 date is its LARGE-scale confirmation and is kept separate from this small-control claim.",
                )
                touched.append("1477:v2:deduplicate-gate-evidence")

            if code == "1612":
                remove_gate_evidence(
                    row["v2"]["triggers"][0],
                    "DYNAMIC_QUADRANTS_SUPPORT",
                    "Packet SMALL scale confirms 2023-06-20 high 22.1423 on 2023-06-27; at 2023-07-17 close 22.5069 this level was already causal and distinct from the large high's confirmation.",
                )
                touched.append("1612:v2:deduplicate-gate-evidence")

        write_jsonl_atomic(path, rows)

    after = {path.name: digest(path) for path in paths}
    print(json.dumps({"before": before, "after": after, "touched": touched}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
