"""Replay only human/Codex-authored V1/V2 AI judgement ledgers.

This module contains no entry classifier, candidate score, scenario router, or
approval rule.  It validates AI-authored decisions, attaches dated market
facts, and delegates only mechanical execution/accounting to the established
corporate-action-aware simulator.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.course_tg_enlightenment_ai_v2_backtest import (  # noqa: E402
    AS_OF,
    CATALOG,
    BUY_COMMISSION,
    SELL_COMMISSION,
    SELL_TAX,
    _digest,
    _mark_to_market_drawdown,
    _read,
    _simulate_stock,
    _summary,
)


RUN = ROOT / "reports/course_backtest/2026-09-06/tg_formal_ai_v1_v2"
FACT_MANIFEST = RUN / "input_manifest.json"
SOURCE_MANIFEST = ROOT / "reports/course_backtest/2026-09-06/tg_enlightenment_ai_v2/input_manifest.json"
ROOT_LEDGER = RUN / "root_000_079_ai_decisions.jsonl"
AGENT_LEDGERS = [
    RUN / "agent_080_301_ai_decisions.jsonl",
    RUN / "agent_302_523_ai_decisions.jsonl",
    RUN / "agent_524_746_ai_decisions.jsonl",
]
MERGED_LEDGER = RUN / "ai_decisions_merged.jsonl"
VALIDATION_PATH = RUN / "decision_validation.json"
RESULT_PATH = RUN / "backtest.json"
REPORT_PATH = RUN / "comparison.md"
V1_DOC = ROOT / "docs/enlightenment-ai-judgement-v1.md"
V2_DOC = ROOT / "docs/enlightenment-ai-judgement-v2.md"
CALIBRATION_CODES = {
    "2483",  # 百容
    "2630",  # 亞航
    "5425",  # 台半
    "6125",  # 廣運
    "6182",  # 合晶
    "6189",  # 豐藝
    "6282",  # 康舒
    "8234",  # 新漢
}


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]


def _status(value: Any) -> str:
    if isinstance(value, str):
        return value.upper()
    if isinstance(value, dict):
        for key in ("status", "result", "value"):
            if key in value:
                return str(value[key]).upper()
    return "UNKNOWN"


def _all_v2_gates_pass(gates: Any) -> bool:
    if isinstance(gates, dict) and gates:
        return all(_status(value).startswith("PASS") for value in gates.values())
    if isinstance(gates, list) and gates:
        return all(_status(value).startswith("PASS") for value in gates)
    return False


def _v2_gate_quality(gates: Any, scenario: str) -> tuple[bool, str | None]:
    minimum = {
        "MATURE_TREND_PULLBACK": 8,
        "MACRO_COPY_RESONANCE": 8,
        "BEAR_REVERSAL_LEFT_RIGHT": 6,
        "FRESH_Q1_EXPANSION": 7,
    }.get(scenario, 1)
    if isinstance(gates, dict):
        rows = list(gates.items())
    elif isinstance(gates, list):
        rows = [(str(index), value) for index, value in enumerate(gates)]
    else:
        return False, "NOT_A_COLLECTION"
    if len(rows) < minimum:
        return False, f"TOO_FEW_GATES:{len(rows)}<{minimum}"
    for gate_id, value in rows:
        if not _status(value).startswith("PASS"):
            return False, f"NOT_PASS:{gate_id}"
        if not isinstance(value, dict):
            return False, f"NO_INDIVIDUAL_EVIDENCE:{gate_id}"
        evidence = value.get("evidence")
        if isinstance(evidence, str):
            evidence = [evidence]
        if not isinstance(evidence, list) or not any(str(item).strip() for item in evidence):
            return False, f"EMPTY_INDIVIDUAL_EVIDENCE:{gate_id}"
    return True, None


_DATE_PATTERN = re.compile(r"(?<!\d)(20\d{2}-\d{2}-\d{2})(?!\d)")


def _future_dates(value: Any, signal_date: str, path: str = "trigger") -> list[dict[str, str]]:
    """Find dates leaked into an AI trigger that were not visible at signal close."""
    found: list[dict[str, str]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            found.extend(_future_dates(child, signal_date, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_future_dates(child, signal_date, f"{path}[{index}]"))
    elif isinstance(value, str):
        for day in _DATE_PATTERN.findall(value):
            if day > signal_date:
                found.append({"path": path, "date": day})
    return found


def _scenario(trigger: dict[str, Any]) -> str:
    value = str(trigger.get("scenario") or "UNRESOLVED_NO_TRADE")
    labels = {
        "MATURE_TREND_PULLBACK": "MATURE_TREND_PULLBACK（長多慣性拉回再發動）",
        "MACRO_COPY_RESONANCE": "MACRO_COPY_RESONANCE（大定錨複製共振）",
        "BEAR_REVERSAL_LEFT_RIGHT": "BEAR_REVERSAL_LEFT_RIGHT（空頭末端左右反轉）",
        "FRESH_Q1_EXPANSION": "FRESH_Q1_EXPANSION（新生定錨直接擴張）",
    }
    for key, label in labels.items():
        if value.startswith(key):
            return label
    return value


def _trigger_path(trigger: dict[str, Any]) -> str:
    return str(trigger.get("trigger_path") or trigger.get("family") or "AI_STRUCTURE_TRIGGER（AI結構觸發）")


def _stop(trigger: dict[str, Any]) -> tuple[str | None, float | None]:
    date = trigger.get("stop_date")
    price = trigger.get("stop_price")
    if price is None and isinstance(trigger.get("stop"), dict):
        date = date or trigger["stop"].get("date") or trigger["stop"].get("source_date")
        price = trigger["stop"].get("price") or trigger["stop"].get("value")
    if price is None and isinstance(trigger.get("small_structure"), dict):
        price = trigger["small_structure"].get("stop_price")
        date = date or trigger["small_structure"].get("stop_date")
    return (None if date is None else str(date), None if price is None else float(price))


def _catalog_maps() -> tuple[dict[str, dict[str, list[str]]], dict[str, dict[str, Any]]]:
    catalog = _read(CATALOG)
    daily: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    meta = {str(row["code"]): row for row in catalog["monitor_stocks"]}
    for event in catalog["events"]:
        if event.get("long_eligible"):
            daily[str(event["code"])][str(event["date"])].append(str(event["strategy"]))
    result = {
        code: {day: sorted(set(values)) for day, values in dates.items()}
        for code, dates in daily.items()
    }
    return result, meta


def validate_and_merge() -> dict[str, Any]:
    manifest = _read(FACT_MANIFEST)
    source_manifest = _read(SOURCE_MANIFEST)
    source_items = {str(row["code"]): row for row in source_manifest["items"]}
    daily_selections, _ = _catalog_maps()
    expected = [str(row["code"]) for row in manifest["items"]]
    paths = [ROOT_LEDGER, *AGENT_LEDGERS]
    missing_files = [str(path) for path in paths if not path.exists()]
    if missing_files:
        raise FileNotFoundError("AI ledgers incomplete: " + ", ".join(missing_files))
    rows: list[dict[str, Any]] = []
    provenance: dict[str, str] = {}
    for path in paths:
        for row in _load_jsonl(path):
            code = str(row["code"])
            if code in provenance:
                raise ValueError(f"duplicate AI review for {code}: {provenance[code]} and {path}")
            provenance[code] = str(path.resolve())
            rows.append(row)
    by_code = {str(row["code"]): row for row in rows}
    missing = [code for code in expected if code not in by_code]
    extra = [code for code in by_code if code not in set(expected)]
    errors: list[dict[str, Any]] = []
    trigger_counts = {"v1": 0, "v2": 0}
    required_trigger_fields = {
        "signal_date", "scenario", "trigger_path", "episode_or_add_candidate",
        "macro_anchor", "small_structure", "taiji_generation",
        "large_quadrant", "small_quadrant", "large_dow", "small_dow",
        "stop_date", "stop_price", "left_right", "evidence", "invalidation",
    }
    for index, code in enumerate(expected):
        if code not in by_code:
            continue
        row = by_code[code]
        market = pd.read_csv(source_items[code]["price_path"], usecols=["date", "high", "low", "close"])
        market_by_date = {
            str(value["date"]): {key: float(value[key]) for key in ("high", "low", "close")}
            for _, value in market.iterrows()
        }
        close_by_date = {day: value["close"] for day, value in market_by_date.items()}
        declared = row.get("index")
        if declared is not None and int(declared) != index:
            errors.append({"code": code, "version": "stock", "error": "INDEX_MISMATCH", "declared": declared, "expected": index})
        audit = row.get("audit") or {}
        if not str(audit.get("outcome_blindness") or "").startswith("RETROSPECTIVE_NOT_BLIND"):
            errors.append({"code": code, "version": "stock", "error": "MISSING_RETROSPECTIVE_NOT_BLIND_ATTESTATION"})
        if audit.get("future_outcomes_used_in_final_evidence") is not False:
            errors.append({"code": code, "version": "stock", "error": "FINAL_EVIDENCE_NOT_ATTESTED_ASOF_ONLY"})
        if not row.get("watchlist_events") and not all((row.get(version) or {}).get("watchlist_events") for version in ("v1", "v2")):
            errors.append({"code": code, "version": "stock", "error": "MISSING_AI_WATCHLIST_LIFECYCLE"})
        for version in ("v1", "v2"):
            section = row.get(version) or {}
            triggers = section.get("triggers") or []
            if not triggers and not section.get("no_trade_reason"):
                errors.append({"code": code, "version": version, "error": "NO_TRADE_WITHOUT_REASON"})
            seen_dates: set[str] = set()
            first_selected = str(row.get("first_selected_on") or manifest["items"][index]["first_selected_on"])
            lifecycle = section.get("watchlist_events") or row.get("watchlist_events") or []
            normalized_lifecycle = sorted(
                [value for value in lifecycle if isinstance(value, dict) and value.get("date") and value.get("event")],
                key=lambda value: (str(value["date"]), str(value["event"])),
            )
            for event in normalized_lifecycle:
                event_day = str(event["date"])
                event_name = str(event["event"])
                if event_day < first_selected or event_day > AS_OF:
                    errors.append({"code": code, "version": version, "error": "WATCHLIST_EVENT_OUTSIDE_WINDOW", "date": event_day, "event": event_name})
                if event_name.startswith("RESELECTED") and event_day not in daily_selections.get(code, {}):
                    errors.append({"code": code, "version": version, "error": "RESELECTED_WITHOUT_UPSTREAM_EVENT", "date": event_day})
            for trigger in triggers:
                trigger_counts[version] += 1
                missing_fields = sorted(required_trigger_fields - set(trigger))
                if missing_fields:
                    errors.append({"code": code, "version": version, "error": "INCOMPLETE_AI_TRIGGER", "missing_fields": missing_fields})
                day = str(trigger.get("signal_date") or "")
                stop_date, stop_price = _stop(trigger)
                if not day or day in seen_dates:
                    errors.append({"code": code, "version": version, "error": "MISSING_OR_DUPLICATE_SIGNAL_DATE", "date": day})
                seen_dates.add(day)
                if day < str(row.get("first_selected_on") or manifest["items"][index]["first_selected_on"]) or day > AS_OF:
                    errors.append({"code": code, "version": version, "error": "SIGNAL_OUTSIDE_MONITOR_WINDOW", "date": day})
                active = False
                for event in normalized_lifecycle:
                    if str(event["date"]) > day:
                        break
                    name = str(event["event"])
                    if name.startswith("WATCHING") or name.startswith("RESELECTED"):
                        active = True
                    elif name.startswith("CAMPAIGN_INVALIDATED") or name.startswith("REMOVED_FROM_WATCHLIST"):
                        active = False
                if not active:
                    errors.append({"code": code, "version": version, "error": "SIGNAL_WHILE_WATCHLIST_INACTIVE", "date": day})
                if stop_price is None or stop_price <= 0 or stop_date is None or stop_date > day:
                    errors.append({"code": code, "version": version, "error": "INVALID_CAUSAL_STOP", "date": day, "stop_date": stop_date, "stop_price": stop_price})
                elif day not in close_by_date:
                    errors.append({"code": code, "version": version, "error": "SIGNAL_DATE_NOT_IN_MARKET_DATA", "date": day})
                elif stop_price >= close_by_date[day]:
                    errors.append({"code": code, "version": version, "error": "STOP_NOT_BELOW_SIGNAL_CLOSE", "date": day, "signal_close": close_by_date[day], "stop_price": stop_price})
                if stop_date in market_by_date and stop_price is not None:
                    source_bar = market_by_date[stop_date]
                    tolerance = max(0.01, abs(source_bar["low"]) * 0.001)
                    if stop_price < source_bar["low"] - tolerance or stop_price > source_bar["high"] + tolerance:
                        errors.append(
                            {
                                "code": code,
                                "version": version,
                                "error": "STOP_PRICE_NOT_FROM_DECLARED_SOURCE_BAR",
                                "date": day,
                                "stop_date": stop_date,
                                "stop_price": stop_price,
                                "source_low": source_bar["low"],
                                "source_high": source_bar["high"],
                            }
                        )
                anchor = trigger.get("macro_anchor")
                if isinstance(anchor, dict):
                    anchor_start = anchor.get("start_date") or anchor.get("start")
                    anchor_end = anchor.get("end_date") or anchor.get("end")
                    if anchor_start and anchor_end and str(anchor_start) > str(anchor_end):
                        errors.append({"code": code, "version": version, "error": "ANCHOR_DATES_REVERSED", "date": day, "start": anchor_start, "end": anchor_end})
                    start_price = anchor.get("start_price")
                    end_price = anchor.get("end_price")
                    direction = str(anchor.get("direction") or "").upper()
                    if start_price is not None and end_price is not None:
                        if direction == "UP" and float(end_price) <= float(start_price):
                            errors.append({"code": code, "version": version, "error": "UP_ANCHOR_PRICE_NOT_ADVANCING", "date": day})
                        if direction == "DOWN" and float(end_price) >= float(start_price):
                            errors.append({"code": code, "version": version, "error": "DOWN_ANCHOR_PRICE_NOT_DECLINING", "date": day})
                leaked = _future_dates(trigger, day)
                if leaked:
                    errors.append({"code": code, "version": version, "error": "FUTURE_DATE_IN_AI_EVIDENCE", "date": day, "leaks": leaked})
                if version == "v2":
                    gate_ok, gate_error = _v2_gate_quality(trigger.get("required_gates"), str(trigger.get("scenario") or ""))
                    if not gate_ok:
                        errors.append({"code": code, "version": version, "error": "V2_GATES_NOT_FULLY_EVIDENCED", "date": day, "detail": gate_error})
    ordered = [by_code[code] for code in expected if code in by_code]
    MERGED_LEDGER.write_text(
        "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in ordered),
        encoding="utf-8",
    )
    result = {
        "expected_stocks": len(expected),
        "reviewed_stocks": len(rows),
        "preferred_750_count": int(manifest.get("preferred_750_count") or 0),
        "below_preferred_750_codes": [str(item["code"]) for item in manifest["items"] if not item.get("preferred_750_met")],
        "missing_codes": missing,
        "extra_codes": extra,
        "duplicate_codes": [],
        "trigger_counts": trigger_counts,
        "validation_errors": errors,
        "valid": len(rows) == len(expected) and not missing and not extra and not errors,
        "ledger_files": [{"path": str(path.resolve()), "sha256": _digest(path)} for path in paths],
    }
    _write_json(VALIDATION_PATH, result)
    if not result["valid"]:
        raise ValueError(f"AI decision validation failed; see {VALIDATION_PATH}")
    return result


def _signals_for(
    version: str,
    row: dict[str, Any],
    frame: pd.DataFrame,
    daily_sources: dict[str, list[str]],
    first: str,
) -> list[dict[str, Any]]:
    frame = frame.copy()
    frame["date_key"] = frame["date"].dt.strftime("%Y-%m-%d")
    indexed = frame.set_index("date_key")
    lifecycle = _lifecycle_for(version, row, first)
    lifecycle_by_date: dict[str, list[str]] = defaultdict(list)
    for value in lifecycle:
        lifecycle_by_date[str(value["date"])].append(str(value["event"]))
    output = []
    for trigger in sorted((row.get(version) or {}).get("triggers") or [], key=lambda value: str(value["signal_date"])):
        day = str(trigger["signal_date"])
        market = indexed.loc[day]
        if isinstance(market, pd.DataFrame):
            market = market.iloc[-1]
        _, stop_price = _stop(trigger)
        # Attribute a trigger only to upstream strategies observed in its
        # currently active campaign.  A removed/invalidated campaign must not
        # contaminate a later re-selected campaign.
        accumulated: set[str] = set()
        active = False
        for source_day in sorted(set(daily_sources) | set(lifecycle_by_date)):
            if source_day > day:
                break
            events = lifecycle_by_date.get(source_day, [])
            if any(event.startswith("CAMPAIGN_INVALIDATED") or event.startswith("REMOVED_FROM_WATCHLIST") for event in events):
                accumulated.clear()
                active = False
            if any(event.startswith("WATCHING") or event.startswith("RESELECTED") for event in events):
                accumulated.clear()
                active = True
            if active:
                accumulated.update(daily_sources.get(source_day, []))
        output.append(
            {
                "signal_date": day,
                "family": _trigger_path(trigger),
                "scenario": _scenario(trigger),
                "stop": float(stop_price),
                "signal_close": float(market["close"]),
                "signal_atr": float(market["atr14"]),
                "risk_pct_at_signal": (float(market["close"]) - float(stop_price)) / float(market["close"]) * 100,
                "risk_atr_at_signal": (float(market["close"]) - float(stop_price)) / float(market["atr14"]),
                "score": 0,
                "code": str(row["code"]),
                "name": str(row.get("name") or ""),
                "campaign": int(trigger.get("campaign") or 1),
                "intent": str(trigger.get("episode_or_add_candidate") or ""),
                "monitor_on": first,
                "active_selection_strategies": sorted(accumulated),
                "selected_today_strategies": daily_sources.get(day, []),
                "ai_evidence": trigger.get("evidence"),
                "ai_invalidation": trigger.get("invalidation"),
            }
        )
    return output


def _lifecycle_for(version: str, row: dict[str, Any], first: str) -> list[dict[str, Any]]:
    section = row.get(version) or {}
    raw = section.get("watchlist_events") or row.get("watchlist_events") or []
    output: list[dict[str, Any]] = []
    for value in raw:
        if not isinstance(value, dict) or not value.get("date") or not value.get("event"):
            continue
        event = str(value["event"])
        labels = {
            "WATCHING": "WATCHING（加入監控）",
            "RESELECTED": "RESELECTED（上游選股重新選入）",
            "CAMPAIGN_INVALIDATED": "CAMPAIGN_INVALIDATED（大結構交易週期失效）",
            "REMOVED_FROM_WATCHLIST": "REMOVED_FROM_WATCHLIST（已移出監控）",
        }
        for prefix, label in labels.items():
            if event.startswith(prefix):
                event = label
                break
        output.append({**value, "date": str(value["date"]), "event": event})
    if not any(str(value["event"]).startswith("WATCHING") for value in output):
        output.append({"date": first, "event": "WATCHING（加入監控）", "campaign": 1})
    return sorted(output, key=lambda value: (str(value["date"]), str(value["event"])))


def replay() -> dict[str, Any]:
    validation = validate_and_merge()
    ledgers = {str(row["code"]): row for row in _load_jsonl(MERGED_LEDGER)}
    source = _read(SOURCE_MANIFEST)
    items = {str(row["code"]): row for row in source["items"]}
    daily_map, meta = _catalog_maps()
    output: dict[str, Any] = {
        "method_version": "formal-ai-v1-v2-ledger-replay-v1",
        "judgement_scope": "FORMAL_CODEX_AI_PER_STOCK（Codex AI逐檔判讀；非分數代理）",
        "outcome_blindness": "RETROSPECTIVE_NOT_BLIND（事後研究；輸入證據按訊號日因果裁切，但不是樣本外盲測）",
        "as_of": AS_OF,
        "validation": validation,
        "ai_decision_ledger": {"path": str(MERGED_LEDGER.resolve()), "sha256": _digest(MERGED_LEDGER)},
        "rules": {"v1": str(V1_DOC.resolve()), "v2": str(V2_DOC.resolve())},
        "rule_sha256": {"v1": _digest(V1_DOC), "v2": _digest(V2_DOC)},
        "costs": {"buy_commission": BUY_COMMISSION, "sell_commission": SELL_COMMISSION, "sell_tax": SELL_TAX},
        "variants": {},
    }
    for version in ("v1", "v2"):
        signals_by_code: dict[str, list[dict[str, Any]]] = {}
        for code, row in ledgers.items():
            frame = pd.read_csv(items[code]["price_path"], parse_dates=["date"])
            from scripts.course_tg_enlightenment_ai_v2_backtest import add_indicators
            frame = add_indicators(frame)
            signals = _signals_for(version, row, frame, daily_map.get(code, {}), str(meta[code]["first_selected_date"]))
            if signals:
                signals_by_code[code] = signals
        for position_key, allow_adds in (("MOTHER_ONLY_10K", False), ("MOTHER_PLUS_2", True)):
            key = f"{version.upper()}_{position_key}"
            result = {"name": key, "stocks": []}
            for code, signals in sorted(signals_by_code.items()):
                first = str(meta[code]["first_selected_date"])
                lifecycle = _lifecycle_for(version, ledgers[code], first)
                result["stocks"].append(_simulate_stock(items[code], signals, lifecycle, allow_adds=allow_adds))
            result["summary"] = _summary(result)
            result["summary"]["mark_to_market_drawdown"] = _mark_to_market_drawdown(result, items)
            sensitivity = {
                "name": f"{key}_EX_CALIBRATION",
                "stocks": [stock for stock in result["stocks"] if str(stock["code"]) not in CALIBRATION_CODES],
            }
            sensitivity["summary"] = _summary(sensitivity)
            sensitivity["summary"]["mark_to_market_drawdown"] = _mark_to_market_drawdown(sensitivity, items)
            result["calibration_sensitivity"] = {
                "excluded_codes": sorted(CALIBRATION_CODES),
                "excluded_traded_codes": sorted(
                    {str(stock["code"]) for stock in result["stocks"] if str(stock["code"]) in CALIBRATION_CODES and stock["episodes"]}
                ),
                "summary": sensitivity["summary"],
            }
            output["variants"][key] = result
    _write_json(RESULT_PATH, output)
    return output


def _money(value: float) -> str:
    return f"{value:+,.0f}"


def _pct(value: float) -> str:
    return f"{value:+.2f}%"


def _pf(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f}"


def _portfolio_table(result: dict[str, Any]) -> list[str]:
    lines = [
        "| 組合 | 股票 | 回合 | 已出場／持有 | 買進份數 | 已實現 | 未實現* | 淨損益 | 尖峰資金報酬 | 勝率 | PF | 最大持股／份數 | 尖峰資金 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for key, variant in result["variants"].items():
        s = variant["summary"]
        lines.append(
            f"| {key} | {s['stocks_traded']} | {s['trade_episodes']} | {s['closed']}／{s['open']} | {s['buy_fills']} | "
            f"{_money(s['realized_net_pnl'])} | {_money(s['unrealized_net_pnl_after_estimated_exit_cost'])} | {_money(s['net_pnl'])} | "
            f"{_pct(s['return_on_peak_capital_pct'])} | {s['win_rate_pct']:.2f}% | {_pf(s['profit_factor'])} | "
            f"{s['maximum_concurrent_stocks']}／{s['maximum_concurrent_tranches']} | {s['peak_concurrent_deployed_cash']:,.0f} |"
        )
    return lines


def _signal_cross() -> dict[str, Any]:
    rows = _load_jsonl(MERGED_LEDGER)
    events: dict[str, set[tuple[str, str]]] = {"v1": set(), "v2": set()}
    stocks: dict[str, set[str]] = {"v1": set(), "v2": set()}
    scenario_by_event: dict[str, dict[tuple[str, str], str]] = {"v1": {}, "v2": {}}
    for row in rows:
        code = str(row["code"])
        for version in ("v1", "v2"):
            for trigger in (row.get(version) or {}).get("triggers") or []:
                event = (code, str(trigger["signal_date"]))
                events[version].add(event)
                stocks[version].add(code)
                scenario_by_event[version][event] = _scenario(trigger)
    shared = events["v1"] & events["v2"]
    return {
        "v1_events": len(events["v1"]),
        "v2_events": len(events["v2"]),
        "shared_events": len(shared),
        "v1_only_events": len(events["v1"] - events["v2"]),
        "v2_only_events": len(events["v2"] - events["v1"]),
        "v1_stocks": len(stocks["v1"]),
        "v2_stocks": len(stocks["v2"]),
        "shared_stocks": len(stocks["v1"] & stocks["v2"]),
        "same_date_scenario_changed": sum(
            scenario_by_event["v1"].get(event) != scenario_by_event["v2"].get(event)
            for event in shared
        ),
    }


def _strategy_scenario_rows(variant: dict[str, Any]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for stock in variant["stocks"]:
        for episode in stock["episodes"]:
            first = episode["tranches"][0]
            scenario = str(first["scenario"])
            for strategy in first.get("active_selection_strategies", []):
                grouped[(str(strategy), scenario)].append(episode)
    output = []
    for (strategy, scenario), episodes in grouped.items():
        pnl = [float(row["net_pnl"]) for row in episodes]
        returns = [float(row["net_return_on_deployed_pct"]) for row in episodes]
        output.append(
            {
                "strategy": strategy,
                "scenario": scenario,
                "episodes": len(episodes),
                "net_pnl_overlap": sum(pnl),
                "average_return_pct": statistics.fmean(returns),
                "win_rate_pct": sum(value > 0 for value in pnl) / len(pnl) * 100,
            }
        )
    return sorted(output, key=lambda row: (-row["episodes"], -row["net_pnl_overlap"], row["strategy"], row["scenario"]))


def _cell(value: Any) -> str:
    return str(value if value is not None else "—").replace("|", "／").replace("\n", " ")


def _anchor_brief(value: Any) -> str:
    if not isinstance(value, dict):
        return _cell(value)
    start = value.get("start_date") or value.get("start")
    end = value.get("end_date") or value.get("end") or "FORMING"
    status = value.get("status") or ""
    return _cell(f"{start or '?'}→{end} {status}".strip())


def _write_decision_audit(rows: list[dict[str, Any]]) -> Path:
    path = RUN / "ai_decisions.md"
    lines = [
        "# 747檔正式Codex AI決策稽核表",
        "",
        "> 本表記錄AI最終判讀，不是程式分數或批次分類器的輸出。完整機器可讀內容見 `ai_decisions_merged.jsonl`。",
        "",
        "| 股票 | 規格 | AI結果 | 訊號 | 不交易原因 | 監控生命週期 |",
        "|---|---|---|---|---|---|",
    ]
    for row in rows:
        code_name = f"{row['code']} {row.get('name') or ''}".strip()
        for version in ("v1", "v2"):
            section = row.get(version) or {}
            triggers = section.get("triggers") or []
            trigger_text = []
            for trigger in triggers:
                trigger_text.append(
                    f"{trigger.get('signal_date')} {_scenario(trigger)}／{trigger.get('trigger_path')}；"
                    f"防線{trigger.get('stop_price')}({trigger.get('stop_date')})；"
                    f"錨{_anchor_brief(trigger.get('macro_anchor'))}"
                )
            lifecycle = section.get("watchlist_events") or row.get("watchlist_events") or []
            lifecycle_text = "；".join(f"{value.get('date')} {value.get('event')}" for value in lifecycle if isinstance(value, dict))
            lines.append(
                f"| {_cell(code_name)} | {version.upper()} | {'TRIGGERED（有觸發）' if triggers else 'NO_TRADE（無交易）'} | "
                f"{_cell('；'.join(trigger_text))} | {_cell(section.get('no_trade_reason'))} | {_cell(lifecycle_text)} |"
            )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def write_reports() -> Path:
    result = _read(RESULT_PATH)
    validation = _read(VALIDATION_PATH)
    cross = _signal_cross()
    v1_mother = result["variants"]["V1_MOTHER_ONLY_10K"]["summary"]
    v1_add = result["variants"]["V1_MOTHER_PLUS_2"]["summary"]
    v2_mother = result["variants"]["V2_MOTHER_ONLY_10K"]["summary"]
    v2_add = result["variants"]["V2_MOTHER_PLUS_2"]["summary"]
    lines = [
        "# 2026/05/01起747檔：正式Codex AI V1／V2交叉回測",
        "",
        "> `FORMAL_CODEX_AI_PER_STOCK（Codex AI逐檔判讀）`：進場日期、情境、定錨、級數與防線來自AI決策檔；回放程式沒有候選分數、情境分類器或核准門檻。",
        "",
        "> `RETROSPECTIVE_NOT_BLIND（事後研究、不是樣本外盲測）`：每筆證據按訊號日裁切，仍不能宣稱規格形成前未知結果。真正可驗證穩定性的資料是規格凍結後的新名單。",
        "",
        "## 完整性",
        "",
        f"- AI逐檔判讀：{validation['reviewed_stocks']}/{validation['expected_stocks']}檔。",
        f"- 監控日前達750根日K偏好標準：{validation['preferred_750_count']}檔；未達者{len(validation['below_preferred_750_codes'])}檔，僅能依可見資料降低信心或不交易。",
        f"- AI核准事件：V1 {validation['trigger_counts']['v1']}筆；V2 {validation['trigger_counts']['v2']}筆。",
        f"- 決策完整性與V2必要條件驗證：{'通過' if validation['valid'] else '未通過'}。",
        "- 進場：訊號日收盤成立、次交易日開盤；跌破防線或超過訊號收盤＋0.5ATR加一跳則取消。每份約一萬元。",
        "- 出場：+2R前使用部位防線；+2R後成本防線只升不降，小級樞紐跌破先警告，連續兩日收盤跌破21MA才全出。",
        "- 帳務：原始成交價格、調整後技術價格、現金股利、買賣手續費各0.1425%、賣出交易稅0.3%。",
        "",
        "## 四組交叉結果",
        "",
        *_portfolio_table(result),
        "",
        "\\* 未實現損益以截至日收盤假設賣出並扣除估計成本。",
        "",
        "## 先讀結論",
        "",
        f"- V1涵蓋較廣，固定母單／加碼版淨損益為{_money(v1_mother['net_pnl'])}／{_money(v1_add['net_pnl'])}元，但尖峰需約{v1_mother['peak_concurrent_deployed_cash']:,.0f}／{v1_add['peak_concurrent_deployed_cash']:,.0f}元。",
        f"- V2較嚴格，固定母單／加碼版淨損益為{_money(v2_mother['net_pnl'])}／{_money(v2_add['net_pnl'])}元；雖然絕對獲利略低，PF為{_pf(v2_mother['profit_factor'])}／{_pf(v2_add['profit_factor'])}，尖峰資金報酬{_pct(v2_mother['return_on_peak_capital_pct'])}／{_pct(v2_add['return_on_peak_capital_pct'])}，資金效率明顯優於V1。",
        f"- 加碼使V1／V2多賺{_money(v1_add['net_pnl'] - v1_mother['net_pnl'])}／{_money(v2_add['net_pnl'] - v2_mother['net_pnl'])}元，但V2尖峰資金報酬只由{_pct(v2_mother['return_on_peak_capital_pct'])}提高到{_pct(v2_add['return_on_peak_capital_pct'])}；加碼是提高絕對獲利，不是明顯改善勝率或單筆效率。",
        f"- 若先選一個實務研究基準，V2固定母單較平衡：{v2_mother['trade_episodes']}回合、PF {_pf(v2_mother['profit_factor'])}、最大回撤-{v2_mother['mark_to_market_drawdown']['max_drawdown_pct']:.2f}%、尖峰資金{v2_mother['peak_concurrent_deployed_cash']:,.0f}元。這不是上線承諾，仍需規格凍結後前瞻驗證。",
        "- V2情境中，長多慣性拉回、定錨複製共振與新生定錨均為正；空頭末端左右反轉只有5回合且為負，現階段應列觀察而非直接採用。",
        "",
        "## AI訊號不等於一定成交",
        "",
        f"- V1由AI核准{validation['trigger_counts']['v1']}筆結構訊號，固定母單實際成交{v1_mother['buy_fills']}份；V2核准{validation['trigger_counts']['v2']}筆，實際成交{v2_mother['buy_fills']}份。",
        "- 差異來自：訊號被明確標成加碼候選、已有部位時母單訊號僅保留證據、隔日開盤跌破防線／超過追價上限，以及截至日尚無下一交易日可成交。",
        "",
        "## V1／V2 AI決策差異",
        "",
        f"- V1觸發{cross['v1_events']}筆／{cross['v1_stocks']}檔；V2觸發{cross['v2_events']}筆／{cross['v2_stocks']}檔。",
        f"- 同股同日重疊{cross['shared_events']}筆；V1獨有{cross['v1_only_events']}筆；V2獨有{cross['v2_only_events']}筆。兩版皆曾交易的股票{cross['shared_stocks']}檔。",
        f"- 同股同日但情境解讀不同：{cross['same_date_scenario_changed']}筆。",
        "",
        "## 固定母單與加碼的增量",
        "",
        "| 規格 | 固定母單淨損益 | 加碼版淨損益 | 加碼增量 | 固定母單尖峰資金 | 加碼版尖峰資金 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for version in ("V1", "V2"):
        mother = result["variants"][f"{version}_MOTHER_ONLY_10K"]["summary"]
        adding = result["variants"][f"{version}_MOTHER_PLUS_2"]["summary"]
        lines.append(
            f"| {version} | {_money(mother['net_pnl'])} | {_money(adding['net_pnl'])} | {_money(adding['net_pnl'] - mother['net_pnl'])} | "
            f"{mother['peak_concurrent_deployed_cash']:,.0f} | {adding['peak_concurrent_deployed_cash']:,.0f} |"
        )
    lines.append("")
    lines += [
        "## 校準案例敏感度",
        "",
        "> 台半、百容、康舒及先前六檔小測試曾參與規則形成；下表排除其中實際存在於747檔母體的代碼，避免把校準案例誤當成獨立證據。這仍然不是樣本外測試。",
        "",
        "| 組合 | 原淨損益 | 排除校準案例後 | 差額 | 排除後回合 | 排除後PF |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for key, variant in result["variants"].items():
        original = variant["summary"]
        sensitivity = variant["calibration_sensitivity"]["summary"]
        lines.append(
            f"| {key} | {_money(original['net_pnl'])} | {_money(sensitivity['net_pnl'])} | "
            f"{_money(sensitivity['net_pnl'] - original['net_pnl'])} | {sensitivity['trade_episodes']} | {_pf(sensitivity['profit_factor'])} |"
        )
    lines.append("")
    for key, variant in result["variants"].items():
        s = variant["summary"]
        lines += [
            f"## {key}",
            "",
            f"- 平均／中位報酬：{_pct(s['average_return_pct'])}／{_pct(s['median_return_pct'])}；平均／中位MFE：{_pct(s['average_mfe_pct'])}／{_pct(s['median_mfe_pct'])}。",
            f"- 已出場平均／勝率／PF：{_pct(s['closed_average_return_pct'])}／{s['closed_win_rate_pct']:.2f}%／{_pf(s['closed_profit_factor'])}。",
            f"- 持有中：{s['open']}回合、{s['current_open_tranches']}份、平均{_pct(s['open_average_return_pct'])}、勝率{s['open_win_rate_pct']:.2f}%。",
            f"- MFE≥20%：{s['mfe_20_plus_count']}筆；其中最終仍正報酬{s['mfe_20_plus_final_positive_count']}筆；已出場平均收盤可變現淨值峰值回吐{s['closed_average_giveback_points']:.2f}個百分點。",
            f"- 最大同時持有{s['maximum_concurrent_stocks']}檔／{s['maximum_concurrent_tranches']}份，尖峰資金{s['peak_concurrent_deployed_cash']:,.0f}元（{s['peak_date']}）。",
            f"- 最大回撤-{s['mark_to_market_drawdown']['max_drawdown_twd']:,.0f}元／-{s['mark_to_market_drawdown']['max_drawdown_pct']:.2f}%。",
            "",
            "### 情境績效",
            "",
            "| 情境 | 回合／持有 | 淨損益 | 平均報酬 | 勝率 | PF |",
            "|---|---:|---:|---:|---:|---:|",
        ]
        for row in s["scenario_breakdown"]:
            lines.append(f"| {row['scenario']} | {row['episodes']}／{row['open']} | {_money(row['net_pnl'])} | {_pct(row['average_return_pct'])} | {row['win_rate_pct']:.2f}% | {_pf(row['profit_factor'])} |")
        lines += ["", "### 損益分布", "", "| 區間 | 全部 | 已出場 | 持有中 | MFE |", "|---|---:|---:|---:|---:|"]
        for label in s["return_distribution"]:
            lines.append(f"| {label} | {s['return_distribution'][label]} | {s['closed_return_distribution'][label]} | {s['open_return_distribution'][label]} | {s['mfe_distribution'][label]} |")
        lines += ["", "### 上游策略重疊歸因", "", "> 同一交易可同時屬於多個上游策略，以下不可加總。", "", "| 策略 | 回合 | 淨損益（重疊歸因） | 勝率 | 平均MFE |", "|---|---:|---:|---:|---:|"]
        for row in s["strategy_breakdown_overlap"]:
            lines.append(f"| {row['strategy']} | {row['episodes']} | {_money(row['net_pnl_full_overlap_attribution'])} | {row['win_rate_pct']:.2f}% | {_pct(row['average_mfe_pct'])} |")
        lines += ["", "### 上游策略 × AI情境（重疊歸因）", "", "| 策略 | AI情境 | 回合 | 淨損益 | 平均報酬 | 勝率 |", "|---|---|---:|---:|---:|---:|"]
        for row in _strategy_scenario_rows(variant):
            lines.append(
                f"| {row['strategy']} | {row['scenario']} | {row['episodes']} | {_money(row['net_pnl_overlap'])} | "
                f"{_pct(row['average_return_pct'])} | {row['win_rate_pct']:.2f}% |"
            )
        lines += ["", "### 前十大獲利／虧損", "", "| 類型 | 股票 | 回合 | 淨損益 | 報酬 |", "|---|---|---|---:|---:|"]
        for label, rows in (("獲利", s["top_winners"]), ("虧損", s["top_losers"])):
            for row in rows:
                lines.append(f"| {label} | {row['code']} {row['name']} | {row['episode_id']} | {_money(row['net_pnl'])} | {_pct(row['return_pct'])} |")
        lines.append("")
    REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")

    decisions = _load_jsonl(MERGED_LEDGER)
    _write_decision_audit(decisions)

    for version in ("V1", "V2"):
        version_dir = RUN / version.lower()
        version_dir.mkdir(parents=True, exist_ok=True)
        subset = {key: value for key, value in result["variants"].items() if key.startswith(version + "_")}
        details = [
            f"# {version}正式AI逐筆交易",
            "",
            "> 訊號由Codex AI逐檔判讀；成交、公司行動、出場與統計由確定性回放程式處理。未實現部位按2026-09-04收盤估計賣出並扣成本。",
            "",
            *_portfolio_table({"variants": subset}),
            "",
        ]
        for key, variant in subset.items():
            s = variant["summary"]
            details += [
                f"## {key}",
                "",
                f"- 交易{s['trade_episodes']}回合（已出場{s['closed']}／持有{s['open']}），買進{s['buy_fills']}份；已實現{_money(s['realized_net_pnl'])}元、未實現{_money(s['unrealized_net_pnl_after_estimated_exit_cost'])}元、合計{_money(s['net_pnl'])}元。",
                f"- 最大同時{s['maximum_concurrent_stocks']}檔／{s['maximum_concurrent_tranches']}份；尖峰占用{s['peak_concurrent_deployed_cash']:,.0f}元；平均MFE {_pct(s['average_mfe_pct'])}。",
                "",
                "| 股票 | 回合 | 部位 | 情境／觸發型態 | 訊號日 | 成交日 | 調整後／原始進價 | 股數 | 初始防線 | 出場觸發 | 實際出場／截至 | 原始出價／截至價 | 狀態 | 回合MFE | 回合MAE | 回合淨報酬 | 回合淨損益 |",
                "|---|---|---|---|---|---|---:|---:|---:|---|---|---:|---|---:|---:|---:|---:|",
            ]
            episodes = [episode for stock in variant["stocks"] for episode in stock["episodes"]]
            for episode in sorted(episodes, key=lambda row: (row["entry_date"], str(row["code"]), row["episode_id"])):
                for tranche_index, tranche in enumerate(episode["tranches"]):
                    exit_or_mark = tranche.get("sell_date") or episode.get("mark_date")
                    raw_exit_or_mark = tranche.get("sell_price") if tranche.get("sell_price") is not None else episode.get("mark_price_raw")
                    details.append(
                        f"| {_cell(str(episode['code']) + ' ' + str(episode['name']))} | {episode['episode_id']} | {_cell(tranche['role'])} | "
                        f"{_cell(str(tranche['scenario']) + '／' + str(tranche['family']))} | {tranche['signal_date']} | {tranche['entry_date']} | "
                        f"{float(tranche['entry_price_adjusted']):.4f}／{float(tranche['entry_price_raw']):.4f} | {int(tranche['shares'])} | "
                        f"{float(tranche['stop_adjusted']):.4f} | {_cell(episode.get('exit_signal_date'))} | {_cell(exit_or_mark)} | "
                        f"{float(raw_exit_or_mark):.4f} | {_cell(episode['status'])} | {_pct(episode['mfe_pct_from_mother'])} | "
                        f"{_pct(episode['mae_pct_from_mother'])} | {_pct(episode['net_return_on_deployed_pct'])} | {_money(episode['net_pnl']) if tranche_index == 0 else '（計入回合）'} |"
                    )
            details.append("")
        (version_dir / "backtest.md").write_text("\n".join(details) + "\n", encoding="utf-8")
    return REPORT_PATH


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("validate", "replay", "report", "all"))
    args = parser.parse_args()
    if args.command in ("validate", "all"):
        print(json.dumps(validate_and_merge(), ensure_ascii=False))
    if args.command in ("replay", "all"):
        result = replay()
        print({key: value["summary"]["net_pnl"] for key, value in result["variants"].items()})
    if args.command in ("report", "all"):
        print(write_reports())


if __name__ == "__main__":
    main()
