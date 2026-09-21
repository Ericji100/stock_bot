"""Normalize Telegram stock-selection messages into an auditable event catalog.

The parser is deliberately limited to messages emitted by known selection
reports.  Research reports, user portfolios and monitor notifications are not
silently treated as new selections.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


DEFAULT_EXPORT = Path(r"D:\agent_workspace\inbox\telegram\output\personal\我的股票偵測器\result.json")
DEFAULT_OUTPUT = ROOT / "reports/course_backtest/2026-09-05/tg_selection_catalog_v1"
START = "2026-05-01"
END = "2026-09-04"

STRATEGY_LABELS = {
    "TECH_MA5_BREAKOUT": "突破5MA",
    "TECH_MA13_BREAKOUT": "突破13MA",
    "TECH_MA21_BREAKOUT": "突破21MA",
    "TECH_MA55_BREAKOUT": "突破55MA",
    "TECH_MA105_BREAKOUT": "突破105MA",
    "TECH_MA144_BREAKOUT": "突破144MA",
    "TECH_MA5_RECLAIM": "跌破後收復5MA",
    "TECH_MA13_RECLAIM": "跌破後收復13MA",
    "TECH_MA21_RECLAIM": "跌破後收復21MA",
    "TECH_MA55_RECLAIM": "跌破後收復55MA",
    "TECH_MA105_RECLAIM": "跌破後收復105MA",
    "TECH_MA144_RECLAIM": "跌破後收復144MA",
    "TECH_MACD_RETEST_BREAKOUT": "MACD回測突破",
    "TECH_MACD_GOLDEN": "MACD黃金交叉",
    "TECH_KD_GOLDEN": "KD黃金交叉",
    "TECH_MACD_LOW_DIVERGENCE": "MACD低檔背離",
    "TECH_KD_LOW_DIVERGENCE": "KD低檔背離",
    "TECH_A_PULLBACK_BREAKOUT": "策略A：多頭延續回檔突破",
    "TECH_B_STRONG_RETEST": "策略B：強勢紅柱回測突破",
    "TECH_C_LOW_REVERSAL": "策略C：低檔背離反轉突破",
    "TECH_D_DROP_RECLAIM": "策略D：動能背景短線轉強",
    "TECH_SELECTED": "技術訊號精選",
    "CHIP_INST_60D": "60日法人動態",
    "CHIP_TRUST_ADOPTION": "投信認養股",
    "CHIP_INST_HOLDING_UP": "法人持股比例增加",
    "CHIP_LARGE_HOLDER_WEEKLY": "每週大戶持股",
    "FUND_REVENUE_G1": "營收G1：連4月穩健成長",
    "FUND_REVENUE_G2": "營收G2：動能轉強",
    "CURATED_CROSS_HIT": "精選選股交叉命中",
    "RADAR_TOP": "每日選股雷達訊息可見名單",
}

TECH_SECTION_MAP = {
    "突破 5MA": "TECH_MA5_BREAKOUT",
    "突破 13MA": "TECH_MA13_BREAKOUT",
    "突破 21MA": "TECH_MA21_BREAKOUT",
    "突破 55MA": "TECH_MA55_BREAKOUT",
    "突破 105MA": "TECH_MA105_BREAKOUT",
    "突破 144MA": "TECH_MA144_BREAKOUT",
    "跌破後收復 5MA": "TECH_MA5_RECLAIM",
    "跌破後收復 13MA": "TECH_MA13_RECLAIM",
    "跌破後收復 21MA": "TECH_MA21_RECLAIM",
    "跌破後收復 55MA": "TECH_MA55_RECLAIM",
    "跌破後收復 105MA": "TECH_MA105_RECLAIM",
    "跌破後收復 144MA": "TECH_MA144_RECLAIM",
    "MACD 回測突破": "TECH_MACD_RETEST_BREAKOUT",
    "MACD 黃金交叉": "TECH_MACD_GOLDEN",
    "KD 黃金交叉": "TECH_KD_GOLDEN",
    "MACD 低檔背離": "TECH_MACD_LOW_DIVERGENCE",
    "KD 低檔背離": "TECH_KD_LOW_DIVERGENCE",
    "策略 A：多頭延續回檔突破": "TECH_A_PULLBACK_BREAKOUT",
    "策略 B：強勢紅柱回測突破": "TECH_B_STRONG_RETEST",
    "策略 C：低檔背離反轉突破": "TECH_C_LOW_REVERSAL",
    "策略 D：強勢股急跌收復": "TECH_D_DROP_RECLAIM",
    "策略 D：中長均線背景短線轉強": "TECH_D_DROP_RECLAIM",
    "策略 D：動能背景短線轉強": "TECH_D_DROP_RECLAIM",
    "技術訊號精選": "TECH_SELECTED",
}

NEGATIVE_TECH_SECTIONS = {
    "MACD 死亡交叉", "KD 死亡交叉", "MACD 高檔背離", "KD 高檔背離",
}

CHIP_REPORTS = {
    "🔍 今日 60 日法人動態選股掃描報告": "CHIP_INST_60D",
    "🔍 今日投信認養股掃描報告": "CHIP_TRUST_ADOPTION",
    "🔍 今日法人持股比例增加掃描報告": "CHIP_INST_HOLDING_UP",
    "🔍 本週大戶持股選股掃描報告": "CHIP_LARGE_HOLDER_WEEKLY",
}

DATE_RE = re.compile(r"(?:資料日期|📅\s*日期)[：:]\s*(20\d{2}-\d{2}-\d{2})")
TITLE_DATE_RE = re.compile(r"(20\d{2}-\d{2}-\d{2})")
CODE_RE = re.compile(r"(?<![\d-])(\d{4,6})\s+([^|｜()（）,，\s*]+)")
GRADE_RE = re.compile(r"(?:🥇|🥈|🥉|▫️)?\s*([SABCD])級")
HIT_RE = re.compile(r"【命中\s*(\d+)\s*個策略】")
RADAR_ROW_RE = re.compile(r"(?m)^\s*(\d+)\.\s+\*{0,2}(\d{4,6})\s+([^｜|\n*]+)\*{0,2}｜(\d+)分")


def _stock_map() -> dict[str, str]:
    payload = json.loads((ROOT / "stock_list.json").read_text(encoding="utf-8-sig"))
    return {
        str(row["code"]): str(row.get("name") or row["code"])
        for row in payload.get("stocks", []) if row.get("code")
    }


def _plain_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(str(part.get("text", "")) if isinstance(part, dict) else str(part) for part in value)
    return ""


def _report_date(text: str, message_date: str) -> str:
    match = DATE_RE.search(text)
    if match:
        return match.group(1)
    first_line = text.strip().splitlines()[0] if text.strip() else ""
    match = TITLE_DATE_RE.search(first_line)
    if match:
        return match.group(1)
    return message_date[:10]


def _mentions(line: str, stocks: dict[str, str]) -> list[tuple[str, str]]:
    clean = line.replace("**", "").replace("__", "")
    found: list[tuple[str, str]] = []
    for match in CODE_RE.finditer(clean):
        code = match.group(1)
        if code in stocks:
            found.append((code, stocks[code]))
    return found


def _event(
    *, day: str, strategy: str, code: str, name: str, message: dict[str, Any],
    grade: str | None = None, detail: str | None = None, long_eligible: bool = True,
) -> dict[str, Any]:
    return {
        "date": day,
        "strategy": strategy,
        "strategy_label": STRATEGY_LABELS[strategy],
        "code": code,
        "name": name,
        "grade": grade,
        "detail": detail,
        "long_eligible": long_eligible,
        "message_id": int(message["id"]),
        "message_datetime": str(message["date"]),
    }


def _normalize_section(value: str) -> str:
    value = value.strip().replace("📂", "").strip()
    value = re.sub(r"｜共.*$", "", value).strip()
    value = value.replace("【", "").replace("】", "").strip()
    return re.sub(r"\s+", " ", value)


def parse_technical(message: dict[str, Any], text: str, stocks: dict[str, str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    day = _report_date(text, str(message["date"]))
    current: str | None = None
    unknown: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("📂"):
            section = _normalize_section(stripped)
            current = TECH_SECTION_MAP.get(section)
            if current is None and section not in NEGATIVE_TECH_SECTIONS and not section.startswith("【策略"):
                unknown.append({"message_id": message["id"], "section": section})
            continue
        if stripped.startswith("策略 "):
            section = _normalize_section(stripped)
            current = TECH_SECTION_MAP.get(section)
            continue
        if current is None:
            continue
        for code, name in _mentions(stripped, stocks):
            events.append(_event(day=day, strategy=current, code=code, name=name, message=message))
    return events, unknown


def parse_chip(message: dict[str, Any], text: str, stocks: dict[str, str], strategy: str) -> list[dict[str, Any]]:
    day = _report_date(text, str(message["date"]))
    grade: str | None = None
    events = []
    for line in text.splitlines():
        match = GRADE_RE.search(line)
        if match:
            grade = match.group(1)
        for code, name in _mentions(line, stocks):
            events.append(_event(day=day, strategy=strategy, code=code, name=name, message=message, grade=grade))
    return events


def parse_fundamental(message: dict[str, Any], text: str, stocks: dict[str, str]) -> list[dict[str, Any]]:
    day = _report_date(text, str(message["date"]))
    strategy: str | None = None
    grade: str | None = None
    events = []
    for line in text.splitlines():
        if "營收第一組" in line:
            strategy = "FUND_REVENUE_G1"
            grade = None
            continue
        if "營收第二組" in line:
            strategy = "FUND_REVENUE_G2"
            grade = None
            continue
        match = GRADE_RE.search(line)
        if match:
            grade = match.group(1)
        if strategy is None:
            continue
        for code, name in _mentions(line, stocks):
            events.append(_event(day=day, strategy=strategy, code=code, name=name, message=message, grade=grade))
    return events


def parse_curated(message: dict[str, Any], text: str, stocks: dict[str, str]) -> list[dict[str, Any]]:
    day = _report_date(text, str(message["date"]))
    hit_count: str | None = None
    events = []
    for line in text.splitlines():
        match = HIT_RE.search(line)
        if match:
            hit_count = match.group(1)
            continue
        for code, name in _mentions(line, stocks):
            events.append(_event(
                day=day, strategy="CURATED_CROSS_HIT", code=code, name=name,
                message=message, grade=hit_count, detail=None if hit_count is None else f"命中{hit_count}策略",
            ))
    return events


def parse_radar(message: dict[str, Any], text: str, stocks: dict[str, str]) -> list[dict[str, Any]]:
    day = _report_date(text, str(message["date"]))
    events = []
    for match in RADAR_ROW_RE.finditer(text):
        rank, code, _, score = match.groups()
        if code not in stocks:
            continue
        events.append(_event(
            day=day, strategy="RADAR_TOP", code=code, name=stocks[code], message=message,
            grade=score, detail=f"訊息排名{rank}；分數{score}",
        ))
    return events


def parse_export(path: Path, start: str = START, end: str = END) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    stocks = _stock_map()
    raw_events: list[dict[str, Any]] = []
    unknown_sections: list[dict[str, Any]] = []
    corrupt = []
    classified_messages: Counter[str] = Counter()
    for message in payload.get("messages", []):
        message_day = str(message.get("date") or "")[:10]
        if not (start <= message_day <= end):
            continue
        text = _plain_text(message.get("text"))
        if "\ufffd" in text:
            corrupt.append({"id": message.get("id"), "date": message.get("date")})
        first = text.strip().splitlines()[0] if text.strip() else ""
        events: list[dict[str, Any]] = []
        if first.startswith("🔍 今日技術面選股掃描報告"):
            events, unknown = parse_technical(message, text, stocks)
            unknown_sections.extend(unknown)
            classified_messages["TECHNICAL_FULL"] += 1
        elif first.startswith("📂") and _normalize_section(first) in TECH_SECTION_MAP:
            events, unknown = parse_technical(message, text, stocks)
            unknown_sections.extend(unknown)
            classified_messages["TECHNICAL_PAGE"] += 1
        elif first in CHIP_REPORTS:
            events = parse_chip(message, text, stocks, CHIP_REPORTS[first])
            classified_messages[CHIP_REPORTS[first]] += 1
        elif first.startswith("🔍 今日財報營收選股掃描報告"):
            events = parse_fundamental(message, text, stocks)
            classified_messages["FUNDAMENTAL"] += 1
        elif first.startswith("⭐ 精選選股交叉命中報告"):
            events = parse_curated(message, text, stocks)
            classified_messages["CURATED"] += 1
        elif first.startswith("📡 每日選股雷達"):
            events = parse_radar(message, text, stocks)
            classified_messages["RADAR"] += 1
        raw_events.extend(event for event in events if start <= event["date"] <= end)

    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for event in raw_events:
        grouped[(event["date"], event["strategy"], event["code"])].append(event)
    events = []
    for key, rows in sorted(grouped.items()):
        first = rows[0]
        grades = sorted({str(row["grade"]) for row in rows if row.get("grade")})
        details = sorted({str(row["detail"]) for row in rows if row.get("detail")})
        events.append({
            **{k: first[k] for k in ("date", "strategy", "strategy_label", "code", "name", "long_eligible")},
            "grades": grades,
            "details": details,
            "message_ids": sorted({int(row["message_id"]) for row in rows}),
            "duplicate_observation_count": len(rows),
        })
    eligible = [row for row in events if row["long_eligible"]]
    unique_monitor = sorted({row["code"] for row in eligible})
    monitor_stocks = []
    for code in unique_monitor:
        stock_rows = [row for row in eligible if row["code"] == code]
        strategies = sorted({row["strategy"] for row in stock_rows})
        monitor_stocks.append({
            "code": code,
            "name": stock_rows[0]["name"],
            "first_selected_date": min(row["date"] for row in stock_rows),
            "last_selected_date": max(row["date"] for row in stock_rows),
            "selection_event_count": len(stock_rows),
            "selection_date_count": len({row["date"] for row in stock_rows}),
            "strategy_count": len(strategies),
            "strategies": strategies,
        })
    return {
        "method_version": "course-tg-selection-catalog-v1",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_path": str(path.resolve()),
        "source_chat": payload.get("name"),
        "window": {"start": start, "end": end},
        "message_count": len(payload.get("messages", [])),
        "classified_message_counts": dict(classified_messages),
        "corrupt_text_messages": corrupt,
        "unknown_technical_sections": unknown_sections,
        "raw_event_count": len(raw_events),
        "event_count": len(events),
        "duplicate_event_count": len(raw_events) - len(events),
        "strategy_counts": dict(Counter(row["strategy"] for row in events)),
        "strategy_date_counts": {
            strategy: len({row["date"] for row in events if row["strategy"] == strategy})
            for strategy in sorted({row["strategy"] for row in events})
        },
        "monitor_stock_count": len(unique_monitor),
        "monitor_codes": unique_monitor,
        "monitor_stocks": monitor_stocks,
        "events": events,
    }


def render(payload: dict[str, Any]) -> str:
    lines = [
        "# Telegram 全策略選股母表", "",
        f"期間：{payload['window']['start']}～{payload['window']['end']}", "",
        f"- TG原始訊息：{payload['message_count']:,}則。",
        f"- 解析後原始命中：{payload['raw_event_count']:,}筆。",
        f"- 同日同策略同股票去重後：{payload['event_count']:,}筆。",
        f"- 新監控股票聯集：{payload['monitor_stock_count']:,}檔。",
        f"- 文字損壞訊息：{len(payload['corrupt_text_messages'])}則。", "",
        "## 各策略覆蓋", "",
        "| 策略 | 日期數 | 去重命中數 |",
        "|---|---:|---:|",
    ]
    for strategy, count in sorted(payload["strategy_counts"].items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"| {strategy}（{STRATEGY_LABELS[strategy]}） | {payload['strategy_date_counts'][strategy]} | {count:,} |")
    lines += [
        "", "## 口徑", "",
        "- 每一筆保留日期、股票、來源策略、等級及原始Telegram訊息ID。",
        "- 完整技術報告與後續分頁可能重複；以日期＋策略＋股票去重，但保留所有訊息ID。",
        "- 財報營收分為G1、G2，A/B/C/D保留在grades欄；籌碼策略同樣保留等級。",
        "- 精選交叉與雷達保留為獨立聚合策略，不能假裝是新的原始訊號。",
        "- AI研究、個人持股、12:30監控通知、死亡交叉及高檔背離不加入做多監控母表。",
    ]
    return "\n".join(lines) + "\n"


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    columns = [
        "date", "strategy", "strategy_label", "code", "name", "long_eligible",
        "grades", "details", "message_ids", "duplicate_observation_count",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({**row, "grades": "|".join(row["grades"]), "details": "|".join(row["details"]), "message_ids": "|".join(map(str, row["message_ids"]))})


def write_monitor_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    columns = [
        "code", "name", "first_selected_date", "last_selected_date",
        "selection_event_count", "selection_date_count", "strategy_count", "strategies",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({**row, "strategies": "|".join(row["strategies"])})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=DEFAULT_EXPORT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--start", default=START)
    parser.add_argument("--end", default=END)
    args = parser.parse_args()
    payload = parse_export(args.input, args.start, args.end)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "catalog.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (args.output_dir / "catalog.md").write_text(render(payload), encoding="utf-8")
    write_csv(args.output_dir / "selection_events.csv", payload["events"])
    write_monitor_csv(args.output_dir / "monitor_list.csv", payload["monitor_stocks"])
    (args.output_dir / "monitor_codes.txt").write_text("\n".join(payload["monitor_codes"]) + "\n", encoding="utf-8")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(render(payload))


if __name__ == "__main__":
    main()
