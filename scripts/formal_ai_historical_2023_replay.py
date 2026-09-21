"""Validate and replay the 2023 formal AI V1/V2 decision ledgers.

This module deliberately contains no candidate classifier, score, scenario
router, or judgement function.  AI-authored ledgers decide whether and when a
signal exists.  The code below only validates causality, converts approved
signals to execution records, and performs deterministic accounting.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
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

from scripts import course_tg_enlightenment_ai_v2_backtest as execution  # noqa: E402


AS_OF = os.environ.get("FORMAL_AI_AS_OF", "2023-09-04")
MONITOR_FLOOR = os.environ.get("FORMAL_AI_MONITOR_FLOOR", "2023-05-02")
SELECTION_START = os.environ.get("FORMAL_AI_SELECTION_START", "2023-01-01")
SELECTION_END = os.environ.get("FORMAL_AI_SELECTION_END", "2023-05-03")
EXPECTED_STOCKS = int(os.environ.get("FORMAL_AI_EXPECTED_STOCKS", "889"))
RUN = Path(
    os.environ.get(
        "FORMAL_AI_RUN",
        str(ROOT / "reports/course_backtest/2023-09-04/historical_scan_formal_ai_v1_v2"),
    )
).expanduser().resolve()
REPORT_LABEL = os.environ.get("FORMAL_AI_REPORT_LABEL", "2023時間保留期")
FACT_MANIFEST = RUN / "input_manifest.json"
MERGED_LEDGER = RUN / "ai_decisions_merged.jsonl"
VALIDATION_PATH = RUN / "decision_validation.json"
RESULT_PATH = RUN / "backtest.json"
REPORT_PATH = RUN / "comparison.md"
DECISION_REPORT_PATH = RUN / "ai_decisions.md"
V1_REPORT_PATH = RUN / "v1/backtest.md"
V2_REPORT_PATH = RUN / "v2/backtest.md"
V1_DOC = ROOT / "docs/enlightenment-ai-judgement-v1.md"
V2_DOC = ROOT / "docs/enlightenment-ai-judgement-v2.md"

ALLOWED_SCENARIOS = frozenset({
    "MATURE_TREND_PULLBACK",
    "MACRO_COPY_RESONANCE",
    "BEAR_REVERSAL_LEFT_RIGHT",
    "FRESH_Q1_EXPANSION",
})

CANONICAL_TAIJI_GENERATIONS = frozenset({
    "ANCHOR_LEG_1",
    "COPY_LEG_3",
    "COPY_LEG_5",
    "LATER_GENERATION",
})

V2_REQUIRED_GATE_IDS = {
    "MATURE_TREND_PULLBACK": frozenset({
        "ACTIVE_LARGE_UPTREND", "LONG_TREND_PERSISTENCE", "LONG_MA_HABIT",
        "CORRECTION_WITHIN_CAMPAIGN", "TAIJI_GENERATION_MAPPED",
        "DYNAMIC_QUADRANTS_SUPPORT", "SMALL_UP_CONTROL_CAUSAL", "EPISODE_STOP_CAUSAL",
    }),
    "MACRO_COPY_RESONANCE": frozenset({
        "COMPLETED_PARENT_ANCHOR", "CORRECTION_INTACT", "TAIJI_GENERATION_MAPPED",
        "CORRECTION_BEAR_DOW_LINE_CAUSAL", "SMALL_UP_REANCHOR_BREAK",
        "DUAL_SCALE_LONG_ALIGNMENT", "NOT_Q3_OR_EXHAUSTED", "EPISODE_STOP_CAUSAL",
    }),
    "BEAR_REVERSAL_LEFT_RIGHT": frozenset({
        "ACTIVE_LARGE_BEAR_ANCHOR", "LARGE_BEAR_DOW_DEFENSE_CAUSAL",
        "BEAR_LATE_STAGE_EVIDENCE", "LEFT_RIGHT_PHASE_MAPPED",
        "DUAL_SCALE_SEPARATED", "PHASE_STOP_CAUSAL",
    }),
    "FRESH_Q1_EXPANSION": frozenset({
        "FRESH_UP_ANCHOR", "CLEAN_MEATY_DESTRUCTIVE_TRACEABLE", "DYNAMIC_Q1_EXPANSION",
        "EARLY_TAIJI_GENERATION", "MACD_SUPPORT_ONLY", "EARLY_LOCATION_WITH_SPACE",
        "FRESH_ANCHOR_STOP_CAUSAL",
    }),
}
V2_OPTIONAL_GATE_IDS = frozenset({"PROFIT_ONLY_ADD_ELIGIBILITY"})

BUY_COMMISSION = execution.BUY_COMMISSION
SELL_COMMISSION = execution.SELL_COMMISSION
SELL_TAX = execution.SELL_TAX


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _has_unicode_replacement(value: Any) -> bool:
    return "\ufffd" in json.dumps(value, ensure_ascii=False)


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _resolve_manifest_path(value: Any, fallback: Path) -> Path:
    if value:
        path = Path(str(value))
        return path if path.is_absolute() else ROOT / path
    return fallback


def _input_context() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    fact = _read(FACT_MANIFEST)
    source_path = _resolve_manifest_path(
        fact.get("source_manifest_path"),
        RUN / "source_manifest.json",
    )
    source = _read(source_path) if source_path.exists() else fact
    catalog_path = _resolve_manifest_path(
        fact.get("catalog_path") or source.get("catalog_path"),
        RUN / "catalog.json",
    )
    catalog = _read(catalog_path)
    fact_items = list(fact.get("items") or [])
    source_items = list(source.get("items") or fact_items)
    if not fact_items:
        fact_items = source_items
    return fact, source, catalog, fact_items


def _ledger_paths() -> list[Path]:
    paths = sorted(
        path for path in RUN.glob("*_ai_decisions.jsonl")
        if path.name != MERGED_LEDGER.name
    )
    if not paths:
        raise FileNotFoundError(f"expected at least one AI ledger file under {RUN}, found none")
    return paths


def _status(value: Any) -> str:
    if isinstance(value, str):
        return value.upper()
    if isinstance(value, dict):
        for key in ("status", "result", "value"):
            if key in value:
                return str(value[key]).upper()
    return "UNKNOWN"


def _v2_gate_quality(gates: Any, scenario: str) -> tuple[bool, str | None]:
    scenario_key = scenario.split("（", 1)[0]
    if scenario_key not in ALLOWED_SCENARIOS:
        return False, f"UNKNOWN_SCENARIO:{scenario_key}"
    minimum = {
        "MATURE_TREND_PULLBACK": 8,
        "MACRO_COPY_RESONANCE": 8,
        "BEAR_REVERSAL_LEFT_RIGHT": 6,
        "FRESH_Q1_EXPANSION": 7,
    }[scenario_key]
    if isinstance(gates, dict):
        rows = list(gates.items())
    elif isinstance(gates, list):
        rows = [
            (str(value.get("gate") or value.get("gate_id") or value.get("name") or index), value)
            if isinstance(value, dict) else (str(index), value)
            for index, value in enumerate(gates)
        ]
    else:
        return False, "NOT_A_COLLECTION"
    if len(rows) < minimum:
        return False, f"TOO_FEW_GATES:{len(rows)}<{minimum}"
    gate_ids = [gate_id for gate_id, _ in rows]
    if len(set(gate_ids)) != len(gate_ids):
        return False, "DUPLICATE_GATE_IDS"
    missing_ids = sorted(V2_REQUIRED_GATE_IDS[scenario_key] - set(gate_ids))
    if missing_ids:
        return False, f"MISSING_REQUIRED_GATE_IDS:{','.join(missing_ids)}"
    unexpected_ids = sorted(set(gate_ids) - V2_REQUIRED_GATE_IDS[scenario_key] - V2_OPTIONAL_GATE_IDS)
    if unexpected_ids:
        return False, f"UNEXPECTED_GATE_IDS:{','.join(unexpected_ids)}"
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


def _v2_taiji_quality(trigger: dict[str, Any]) -> tuple[bool, str | None]:
    generation = str(trigger.get("taiji_generation") or "")
    if generation not in CANONICAL_TAIJI_GENERATIONS:
        return False, f"NONCANONICAL_TAIJI_GENERATION:{generation or 'EMPTY'}"
    scenario = str(trigger.get("scenario") or "").split("（", 1)[0]
    path = str(trigger.get("trigger_path") or "")
    if scenario == "FRESH_Q1_EXPANSION":
        expected = {
            "INITIAL_DESTRUCTIVE_EXPANSION": "ANCHOR_LEG_1",
            "FIRST_SHALLOW_CORRECTION_RELAUNCH": "COPY_LEG_3",
        }.get(path)
        if expected and generation != expected:
            return False, f"FRESH_PATH_TAIJI_MISMATCH:{path}:{generation}!={expected}"
    if scenario == "MACRO_COPY_RESONANCE" and generation == "ANCHOR_LEG_1":
        return False, "MACRO_COPY_CANNOT_BE_ANCHOR_LEG_1"
    return True, None


_DATE_PATTERN = re.compile(r"(?<!\d)(20\d{2}-\d{2}-\d{2})(?!\d)")


def _future_dates(value: Any, signal_date: str, path: str = "trigger") -> list[dict[str, str]]:
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
        date = date or trigger["small_structure"].get("stop_date")
        price = trigger["small_structure"].get("stop_price")
    return (None if date is None else str(date), None if price is None else float(price))


def _matching_confirmed_pivots(
    pivots: list[dict[str, Any]],
    *,
    side: str,
    source_date: str | None,
    price: float | None,
    scale: str | None = None,
) -> list[dict[str, Any]]:
    """Return packet pivots that match a ledger date/price declaration."""
    if not source_date or price is None:
        return []
    tolerance = max(0.01, abs(float(price)) * 0.001)
    output: list[dict[str, Any]] = []
    for pivot in pivots:
        if str(pivot.get("side") or "").upper() != side.upper():
            continue
        if scale and str(pivot.get("scale") or "").upper() != scale.upper():
            continue
        if str(pivot.get("source_date") or "") != source_date:
            continue
        try:
            pivot_price = float(pivot.get("price"))
        except (TypeError, ValueError):
            continue
        if math.isclose(pivot_price, float(price), rel_tol=0.0, abs_tol=tolerance):
            output.append(pivot)
    return output


def _declared_pivot(value: Any) -> tuple[str | None, float | None, str | None]:
    if not isinstance(value, dict):
        return None, None, None
    source_date = value.get("date") or value.get("source_date")
    price = value.get("price")
    confirmed_on = value.get("confirmed_on") or value.get("confirmation_date")
    try:
        parsed_price = None if price is None else float(price)
    except (TypeError, ValueError):
        parsed_price = None
    return (
        None if source_date is None else str(source_date),
        parsed_price,
        None if confirmed_on is None else str(confirmed_on),
    )


def _pivot_timing_is_causal(source_date: str, confirmation_date: str, signal_date: str) -> bool:
    """A prior pivot may finish confirming at the signal close, never after it."""
    return source_date < signal_date and confirmation_date <= signal_date


def _intent_kind(value: Any) -> str | None:
    text = str(value or "").upper()
    if "MOTHER_OR_V2_VALID_ADD" in text:
        return "MOTHER_OR_ADD"
    if "ADD" in text or "加碼" in text:
        return "ADD"
    if "MOTHER" in text or "REENTRY" in text or "母單" in text or "再進" in text:
        return "MOTHER"
    return None


def _catalog_maps(catalog: dict[str, Any]) -> tuple[dict[str, dict[str, list[str]]], dict[str, dict[str, Any]]]:
    daily: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for event in catalog.get("events", []):
        if event.get("long_eligible") is False:
            continue
        day = str(event.get("date") or "")
        code = str(event.get("code") or "")
        if not code or not day or day > AS_OF:
            continue
        daily[code][day].append(str(event.get("strategy") or "UNKNOWN_SELECTION_SOURCE"))
    normalized = {
        code: {day: sorted(set(values)) for day, values in sorted(days.items())}
        for code, days in daily.items()
    }
    meta = {str(row["code"]): row for row in catalog.get("monitor_stocks", [])}
    return normalized, meta


def _first_selected(code: str, row: dict[str, Any], item: dict[str, Any], meta: dict[str, dict[str, Any]], daily: dict[str, dict[str, list[str]]]) -> str:
    # The 2023 study is a fixed cohort initialized on/after 2023-05-02.
    # Earlier selection dates describe how a stock entered the carry-in cohort,
    # not permission to backfill January-April trades.
    value = item.get("monitor_on") or item.get("first_selected_on")
    if value is None and code in meta:
        value = meta[code].get("monitor_on") or meta[code].get("first_selected_date") or meta[code].get("first_selected_on")
    if value is None:
        value = row.get("first_selected_on")
    if value is None and daily.get(code):
        value = min(daily[code])
    if value is None:
        raise ValueError(f"missing first monitoring date for {code}")
    return str(value)


def _lifecycle_for(version: str, row: dict[str, Any], first: str) -> list[dict[str, Any]]:
    section = row.get(version) or {}
    raw = section.get("watchlist_events") or row.get("watchlist_events") or []
    output = []
    for value in raw:
        if not isinstance(value, dict) or not value.get("date") or not value.get("event"):
            continue
        event = str(value["event"])
        if event.startswith("LEFT_CENSORED_CARRY_IN"):
            event = "WATCHING（LEFT_CENSORED_CARRY_IN／左截尾固定母體帶入）"
        output.append({**value, "date": str(value["date"]), "event": event})
    if not any(str(value["event"]).startswith("WATCHING") for value in output):
        output.append({"date": first, "event": "WATCHING（加入監控）", "campaign": 1})
    def order(value: dict[str, Any]) -> tuple[str, int, str]:
        event = str(value["event"])
        # Initial WATCHING occurs before a same-day AI rejection.  An explicit
        # RESELECTED event occurs after old-campaign removal and may reactivate.
        if event.startswith("WATCHING"):
            rank = 0
        elif event.startswith(("CAMPAIGN_INVALIDATED", "REMOVED_FROM_WATCHLIST")):
            rank = 1
        else:
            rank = 2
        return str(value["date"]), rank, event

    return sorted(output, key=order)


def _active_campaign(lifecycle: list[dict[str, Any]], day: str) -> tuple[bool, int]:
    active = False
    campaign = 0
    for event in lifecycle:
        if str(event["date"]) > day:
            break
        name = str(event["event"])
        if name.startswith("WATCHING") or name.startswith("RESELECTED"):
            campaign += 1
            active = True
        elif name.startswith("CAMPAIGN_INVALIDATED") or name.startswith("REMOVED_FROM_WATCHLIST"):
            active = False
    return active, campaign


def _split_ratio(row: dict[str, Any]) -> float | None:
    value = row.get("ratio")
    if value is None:
        value = row.get("split_ratio")
    if value is None:
        value = row.get("splitRatio")
    if value is None:
        value = row.get("amount")
    if value is None and row.get("numerator") is not None and row.get("denominator") is not None:
        denominator = float(row["denominator"])
        value = float(row["numerator"]) / denominator if denominator else None
    if isinstance(value, str) and ":" in value:
        numerator, denominator = value.split(":", 1)
        value = float(numerator) / float(denominator)
    try:
        ratio = float(value)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return ratio if math.isfinite(ratio) and ratio > 0 else None


def _corporate_actions(item: dict[str, Any]) -> tuple[dict[str, float], dict[str, float], list[Any]]:
    payload = _read(Path(item["event_path"]))
    dividends: dict[str, float] = defaultdict(float)
    for row in payload.get("dividends", []):
        day = str(row.get("date") or "")
        if day and day <= AS_OF:
            dividends[day] += float(row["amount"])
    splits: dict[str, float] = defaultdict(lambda: 1.0)
    unresolved: list[Any] = []
    for row in payload.get("splits", []):
        day = str(row.get("date") or "")
        ratio = _split_ratio(row)
        if not day or ratio is None:
            unresolved.append({"type": "SPLIT_PARSE_FAILED", "value": row})
        elif day <= AS_OF:
            splits[day] *= ratio
    for key in ("unresolved_actions", "unresolved_corporate_actions", "ambiguous_actions"):
        value = payload.get(key)
        if value:
            unresolved.extend(value if isinstance(value, list) else [value])
        item_value = item.get(key)
        if item_value:
            unresolved.extend(item_value if isinstance(item_value, list) else [item_value])
    # Producers may expose an aggregate resolution flag instead of a row list.
    # Treat every explicit negative/ambiguous status as blocking; a replay must
    # never silently turn an unresolved corporate action into ordinary P&L.
    for owner, label in ((payload, "event_payload"), (item, "manifest_item")):
        if owner.get("corporate_actions_resolved") is False:
            unresolved.append({"type": "CORPORATE_ACTIONS_NOT_RESOLVED", "source": label})
        if owner.get("has_unresolved_corporate_action") is True or owner.get("unresolved_action") is True:
            unresolved.append({"type": "UNRESOLVED_CORPORATE_ACTION_FLAG", "source": label})
        status = str(owner.get("corporate_action_status") or "").upper()
        if status in {"UNRESOLVED", "AMBIGUOUS", "ERROR", "FAILED"}:
            unresolved.append({"type": "CORPORATE_ACTION_STATUS", "source": label, "status": status})
    action_audit = item.get("corporate_action_audit") or {}
    if action_audit.get("corporate_action_unresolved") is True:
        unresolved.append({
            "type": "MANIFEST_CORPORATE_ACTION_AUDIT_UNRESOLVED",
            "reasons": action_audit.get("unresolved_reasons") or [],
        })
    if action_audit.get("requires_share_count_replay") is True and not splits:
        unresolved.append({"type": "SHARE_COUNT_REPLAY_REQUIRED_BUT_NO_SPLIT_EVENT"})
    return dict(dividends), dict(splits), unresolved


def validate_and_merge() -> dict[str, Any]:
    fact, source, catalog, expected_items = _input_context()
    source_items = {str(row["code"]): row for row in (source.get("items") or expected_items)}
    packet_manifest_path = RUN / "packet_manifest.json"
    packet_items = {
        str(row["code"]): row
        for row in (
            _read(packet_manifest_path).get("items", [])
            if packet_manifest_path.exists()
            else []
        )
    }
    daily, meta = _catalog_maps(catalog)
    expected = [str(row["code"]) for row in expected_items]
    paths = _ledger_paths()
    rows: list[dict[str, Any]] = []
    provenance: dict[str, str] = {}
    duplicate_codes: list[str] = []
    corrupted_text_codes: list[str] = []
    for path in paths:
        for row in _load_jsonl(path):
            code = str(row.get("code") or "")
            if _has_unicode_replacement(row):
                corrupted_text_codes.append(code)
            if code in provenance:
                duplicate_codes.append(code)
            else:
                provenance[code] = str(path.resolve())
            rows.append(row)
    by_code = {str(row.get("code") or ""): row for row in rows}
    missing = [code for code in expected if code not in by_code]
    expected_set = set(expected)
    extra = [code for code in by_code if code not in expected_set]
    errors: list[dict[str, Any]] = []
    for code in sorted(set(corrupted_text_codes)):
        errors.append({"code": code, "version": "stock", "error": "CORRUPTED_UNICODE_REPLACEMENT_CHARACTER"})
    factor_audit = fact.get("factor_jump_audit") or source.get("factor_jump_audit") or {}
    if int(factor_audit.get("corporate_action_unresolved_count") or 0) != 0 or factor_audit.get("unresolved_codes"):
        errors.append({
            "version": "run",
            "error": "MANIFEST_HAS_UNRESOLVED_CORPORATE_ACTIONS",
            "count": factor_audit.get("corporate_action_unresolved_count"),
            "codes": factor_audit.get("unresolved_codes") or [],
        })
    if len(expected) != EXPECTED_STOCKS:
        errors.append({"version": "run", "error": "EXPECTED_MANIFEST_COUNT_MISMATCH", "found": len(expected), "required": EXPECTED_STOCKS})
    if len(rows) != EXPECTED_STOCKS:
        errors.append({"version": "run", "error": "LEDGER_ROW_COUNT_MISMATCH", "found": len(rows), "required": EXPECTED_STOCKS})
    required_trigger_fields = {
        "signal_date", "scenario", "trigger_path", "episode_or_add_candidate",
        "macro_anchor", "small_structure", "taiji_generation", "large_quadrant",
        "small_quadrant", "large_dow", "small_dow", "stop_date", "stop_price",
        "left_right", "evidence", "invalidation",
    }
    trigger_counts = {"v1": 0, "v2": 0}
    intent_counts = {"v1": Counter(), "v2": Counter()}
    audited_rows = 0
    carry_in_stocks = 0
    split_events = 0
    dividend_events = 0
    for index, code in enumerate(expected):
        row = by_code.get(code)
        if row is None:
            continue
        declared = row.get("index")
        if declared is None or int(declared) != index:
            errors.append({"code": code, "version": "stock", "error": "INDEX_MISMATCH", "declared": declared, "expected": index})
        packet_item = packet_items.get(code)
        audit = row.get("audit")
        if not isinstance(audit, dict) or not audit:
            errors.append({"code": code, "version": "stock", "error": "MISSING_PER_STOCK_AUDIT"})
        else:
            audited_rows += 1
            if audit.get("method") != "FORMAL_AI_FACTS_ONLY_CAUSAL_REVIEW":
                errors.append({"code": code, "version": "stock", "error": "INVALID_AI_REVIEW_METHOD"})
            if not str(audit.get("outcome_blindness") or "").startswith("TEMPORAL_HOLDOUT_CAUSAL_REPLAY"):
                errors.append({"code": code, "version": "stock", "error": "MISSING_TEMPORAL_HOLDOUT_CAUSAL_REPLAY_ATTESTATION"})
            if audit.get("future_outcomes_used_in_final_evidence") is not False:
                errors.append({"code": code, "version": "stock", "error": "FINAL_EVIDENCE_NOT_ATTESTED_ASOF_ONLY"})
            if audit.get("outcome_visible_to_ai") is not False:
                errors.append({"code": code, "version": "stock", "error": "OUTCOME_WAS_VISIBLE_TO_AI"})
            if packet_item and str(audit.get("packet_sha256") or "") != str(packet_item.get("packet_sha256") or ""):
                errors.append({"code": code, "version": "stock", "error": "AI_AUDIT_PACKET_HASH_MISMATCH"})
        item = source_items.get(code)
        if item is None:
            errors.append({"code": code, "version": "stock", "error": "MISSING_SOURCE_ITEM"})
            continue
        first = _first_selected(code, row, expected_items[index], meta, daily)
        declared_first = row.get("first_selected_on")
        if declared_first is not None and str(declared_first) != first:
            errors.append({"code": code, "version": "stock", "error": "LEDGER_MONITOR_START_MISMATCH", "declared": declared_first, "expected": first})
        original_first = str(meta.get(code, {}).get("first_selected_date") or first)
        is_carry_in = original_first < first
        carry_in_stocks += int(is_carry_in)
        dividends, splits, unresolved = _corporate_actions(item)
        relevant_dividends = {day: amount for day, amount in dividends.items() if first <= day <= AS_OF}
        relevant_splits = {day: ratio for day, ratio in splits.items() if first <= day <= AS_OF}
        dividend_events += len(relevant_dividends)
        split_events += len(relevant_splits)
        if unresolved:
            errors.append({"code": code, "version": "stock", "error": "UNRESOLVED_CORPORATE_ACTION", "details": unresolved})
        action_audit = item.get("corporate_action_audit") or {}
        declared_adjustments = {
            str(value.get("date") or ""): _split_ratio(value)
            for value in action_audit.get("share_count_adjustments") or []
            if isinstance(value, dict)
        }
        if bool(action_audit.get("requires_share_count_replay")) != bool(relevant_splits):
            errors.append({
                "code": code,
                "version": "stock",
                "error": "SHARE_COUNT_REPLAY_FLAG_MISMATCH",
                "declared": bool(action_audit.get("requires_share_count_replay")),
                "event_splits": relevant_splits,
            })
        if declared_adjustments and (
            set(declared_adjustments) != set(relevant_splits)
            or any(
                declared_adjustments[day] is None
                or not math.isclose(float(declared_adjustments[day]), float(relevant_splits[day]), rel_tol=1e-9, abs_tol=1e-12)
                for day in declared_adjustments if day in relevant_splits
            )
        ):
            errors.append({
                "code": code,
                "version": "stock",
                "error": "MANIFEST_SPLIT_LEDGER_MISMATCH",
                "declared": declared_adjustments,
                "events": relevant_splits,
            })
        price_path = Path(item["price_path"])
        event_path = Path(item["event_path"])
        if item.get("price_sha256") and _digest(price_path) != str(item["price_sha256"]):
            errors.append({"code": code, "version": "stock", "error": "PRICE_SOURCE_HASH_MISMATCH"})
        if item.get("event_sha256") and _digest(event_path) != str(item["event_sha256"]):
            errors.append({"code": code, "version": "stock", "error": "EVENT_SOURCE_HASH_MISMATCH"})
        market = pd.read_csv(price_path, usecols=["date", "high", "low", "close"])
        future_market_dates = sorted({str(value) for value in market["date"] if str(value) > AS_OF})
        if future_market_dates:
            errors.append({"code": code, "version": "stock", "error": "FUTURE_PRICE_FACTS_IN_SOURCE", "dates": future_market_dates[:10]})
        event_payload = _read(event_path)
        future_action_dates = sorted({
            str(value.get("date"))
            for key in ("dividends", "splits")
            for value in (event_payload.get(key) or [])
            if isinstance(value, dict) and value.get("date") and str(value["date"]) > AS_OF
        })
        if future_action_dates:
            errors.append({"code": code, "version": "stock", "error": "FUTURE_CORPORATE_ACTION_FACTS_IN_SOURCE", "dates": future_action_dates[:10]})
        market_by_date = {
            str(value["date"]): {key: float(value[key]) for key in ("high", "low", "close")}
            for _, value in market.iterrows() if str(value["date"]) <= AS_OF
        }
        packet_pivots: list[dict[str, Any]] = []
        if packet_item and packet_item.get("packet"):
            packet_path = Path(str(packet_item["packet"]))
            if packet_path.exists():
                packet_pivots = list(_read(packet_path).get("confirmed_pivots") or [])
        for version in ("v1", "v2"):
            section = row.get(version)
            if not isinstance(section, dict):
                errors.append({"code": code, "version": version, "error": "MISSING_VERSION_SECTION"})
                continue
            triggers = section.get("triggers") or []
            if not triggers and not section.get("no_trade_reason"):
                errors.append({"code": code, "version": version, "error": "NO_TRADE_WITHOUT_REASON"})
            no_trade_future = _future_dates(section.get("no_trade_reason"), AS_OF, "no_trade_reason")
            if no_trade_future:
                errors.append({"code": code, "version": version, "error": "FUTURE_DATE_IN_NO_TRADE_REASON", "leaks": no_trade_future})
            raw_lifecycle = section.get("watchlist_events") or row.get("watchlist_events") or []
            if not raw_lifecycle:
                errors.append({"code": code, "version": version, "error": "MISSING_AI_WATCHLIST_LIFECYCLE"})
            lifecycle = _lifecycle_for(version, row, first)
            raw_initial = [
                event for event in raw_lifecycle
                if isinstance(event, dict) and str(event.get("date") or "") == first
            ]
            if is_carry_in:
                if not any(
                    "LEFT_CENSORED_CARRY_IN" in " ".join(str(event.get(key) or "") for key in ("event", "type", "status", "reason"))
                    for event in raw_initial
                ):
                    errors.append({"code": code, "version": version, "error": "MISSING_LEFT_CENSORED_CARRY_IN", "expected_date": first})
            elif not any(str(event.get("event") or "").startswith("WATCHING") for event in raw_initial):
                errors.append({"code": code, "version": version, "error": "MISSING_INITIAL_WATCHING_EVENT", "expected_date": first})
            for event in lifecycle:
                event_day, event_name = str(event["date"]), str(event["event"])
                if event_day < first or event_day > AS_OF:
                    errors.append({"code": code, "version": version, "error": "WATCHLIST_EVENT_OUTSIDE_WINDOW", "date": event_day})
                elif event_day not in market_by_date:
                    errors.append({"code": code, "version": version, "error": "WATCHLIST_EVENT_NOT_IN_MARKET_DATA", "date": event_day})
                event_leaks = _future_dates(event, event_day, "watchlist_event")
                if event_leaks:
                    errors.append({"code": code, "version": version, "error": "FUTURE_DATE_IN_WATCHLIST_EVIDENCE", "date": event_day, "leaks": event_leaks})
                if event_name.startswith("RESELECTED") and event_day not in daily.get(code, {}):
                    errors.append({"code": code, "version": version, "error": "RESELECTED_WITHOUT_UPSTREAM_EVENT", "date": event_day})
            removed_days = sorted({
                str(event["date"])
                for event in lifecycle
                if str(event.get("event") or "").startswith(("CAMPAIGN_INVALIDATED", "REMOVED_FROM_WATCHLIST", "REMOVED"))
            })
            declared_reselected = {
                str(event["date"])
                for event in lifecycle
                if str(event.get("event") or "").startswith("RESELECTED")
            }
            upstream_days = sorted(str(day) for day in daily.get(code, {}))
            for removed_day in removed_days:
                next_upstream = next((day for day in upstream_days if day > removed_day), None)
                if next_upstream and next_upstream not in declared_reselected:
                    errors.append({
                        "code": code,
                        "version": version,
                        "error": "MISSING_FIRST_TRUE_RESELECTION_AFTER_REMOVAL",
                        "removed_on": removed_day,
                        "expected_reselected_on": next_upstream,
                    })
            seen_dates: set[str] = set()
            mother_seen_by_campaign: dict[int, bool] = defaultdict(bool)
            for trigger in sorted(triggers, key=lambda value: str(value.get("signal_date") or "")):
                trigger_counts[version] += 1
                missing_fields = sorted(required_trigger_fields - set(trigger))
                if missing_fields:
                    errors.append({"code": code, "version": version, "error": "INCOMPLETE_AI_TRIGGER", "missing_fields": missing_fields})
                day = str(trigger.get("signal_date") or "")
                scenario_key = str(trigger.get("scenario") or "").split("（", 1)[0]
                if scenario_key not in ALLOWED_SCENARIOS:
                    errors.append({
                        "code": code,
                        "version": version,
                        "error": "UNKNOWN_AI_SCENARIO",
                        "date": day,
                        "scenario": trigger.get("scenario"),
                    })
                if not day or day in seen_dates:
                    errors.append({"code": code, "version": version, "error": "MISSING_OR_DUPLICATE_SIGNAL_DATE", "date": day})
                seen_dates.add(day)
                active, campaign = _active_campaign(lifecycle, day)
                if day < first or day > AS_OF:
                    errors.append({"code": code, "version": version, "error": "SIGNAL_OUTSIDE_MONITOR_WINDOW", "date": day})
                if not active:
                    errors.append({"code": code, "version": version, "error": "SIGNAL_WHILE_WATCHLIST_INACTIVE", "date": day})
                intent = _intent_kind(trigger.get("episode_or_add_candidate"))
                if intent is None:
                    errors.append({"code": code, "version": version, "error": "INVALID_AI_MOTHER_ADD_INTENT", "date": day, "intent": trigger.get("episode_or_add_candidate")})
                else:
                    intent_counts[version][intent] += 1
                    if intent == "ADD" and not mother_seen_by_campaign[campaign]:
                        errors.append({"code": code, "version": version, "error": "ADD_INTENT_WITHOUT_PRIOR_MOTHER", "date": day, "campaign": campaign})
                    if intent == "MOTHER":
                        mother_seen_by_campaign[campaign] = True
                stop_date, stop_price = _stop(trigger)
                if stop_date is None or stop_price is None or stop_price <= 0 or stop_date >= day:
                    errors.append({"code": code, "version": version, "error": "INVALID_CAUSAL_STOP", "date": day, "stop_date": stop_date, "stop_price": stop_price})
                elif day not in market_by_date:
                    errors.append({"code": code, "version": version, "error": "SIGNAL_DATE_NOT_IN_MARKET_DATA", "date": day})
                elif stop_price >= market_by_date[day]["close"]:
                    errors.append({"code": code, "version": version, "error": "STOP_NOT_BELOW_SIGNAL_CLOSE", "date": day, "stop_price": stop_price, "signal_close": market_by_date[day]["close"]})
                if stop_date in market_by_date and stop_price is not None:
                    source_bar = market_by_date[stop_date]
                    tolerance = max(0.01, abs(source_bar["low"]) * 0.001)
                    if stop_price < source_bar["low"] - tolerance or stop_price > source_bar["high"] + tolerance:
                        errors.append({"code": code, "version": version, "error": "STOP_PRICE_NOT_FROM_DECLARED_SOURCE_BAR", "date": day, "stop_date": stop_date, "stop_price": stop_price, "source_low": source_bar["low"], "source_high": source_bar["high"]})
                elif stop_date is not None:
                    errors.append({"code": code, "version": version, "error": "STOP_SOURCE_DATE_NOT_IN_MARKET_DATA", "date": day, "stop_date": stop_date})
                if stop_date is not None and stop_price is not None:
                    stop_matches = _matching_confirmed_pivots(
                        packet_pivots,
                        side="LOW",
                        source_date=stop_date,
                        price=stop_price,
                    )
                    if not stop_matches:
                        errors.append({
                            "code": code,
                            "version": version,
                            "error": "STOP_NOT_FROM_CONFIRMED_LOW_PIVOT",
                            "date": day,
                            "stop_date": stop_date,
                            "stop_price": stop_price,
                        })
                    elif not any(
                        _pivot_timing_is_causal(
                            str(value.get("source_date") or ""),
                            str(value.get("confirmation_date") or ""),
                            day,
                        )
                        for value in stop_matches
                    ):
                        errors.append({
                            "code": code,
                            "version": version,
                            "error": "STOP_PIVOT_CONFIRMED_AFTER_SIGNAL",
                            "date": day,
                            "stop_date": stop_date,
                            "stop_price": stop_price,
                            "confirmation_dates": sorted({str(value.get("confirmation_date") or "") for value in stop_matches}),
                        })
                small_structure = trigger.get("small_structure")
                if not isinstance(small_structure, dict):
                    errors.append({"code": code, "version": version, "error": "MISSING_SMALL_STRUCTURE", "date": day})
                else:
                    for field, side in (("confirmed_pivot_high", "HIGH"), ("confirmed_pivot_low", "LOW")):
                        source_date, pivot_price, confirmed_on = _declared_pivot(small_structure.get(field))
                        if source_date is None or pivot_price is None or confirmed_on is None:
                            errors.append({
                                "code": code,
                                "version": version,
                                "error": "INCOMPLETE_DECLARED_CONFIRMED_PIVOT",
                                "date": day,
                                "field": field,
                            })
                            continue
                        if source_date >= day:
                            errors.append({
                                "code": code,
                                "version": version,
                                "error": "DECLARED_PIVOT_SOURCE_NOT_BEFORE_SIGNAL",
                                "date": day,
                                "field": field,
                                "source_date": source_date,
                            })
                        if confirmed_on > day:
                            errors.append({
                                "code": code,
                                "version": version,
                                "error": "DECLARED_PIVOT_CONFIRMED_AFTER_SIGNAL",
                                "date": day,
                                "field": field,
                                "confirmation_date": confirmed_on,
                            })
                        pivot_matches = _matching_confirmed_pivots(
                            packet_pivots,
                            side=side,
                            source_date=source_date,
                            price=pivot_price,
                            scale="SMALL",
                        )
                        if not any(str(value.get("confirmation_date") or "") == confirmed_on for value in pivot_matches):
                            errors.append({
                                "code": code,
                                "version": version,
                                "error": "DECLARED_PIVOT_NOT_IN_PACKET",
                                "date": day,
                                "field": field,
                                "source_date": source_date,
                                "price": pivot_price,
                                "confirmation_date": confirmed_on,
                            })
                anchor = trigger.get("macro_anchor")
                if isinstance(anchor, dict):
                    anchor_start = anchor.get("start_date") or anchor.get("start")
                    anchor_end = anchor.get("end_date") or anchor.get("end")
                    if anchor_start and anchor_end and str(anchor_start) > str(anchor_end):
                        errors.append({"code": code, "version": version, "error": "ANCHOR_DATES_REVERSED", "date": day})
                leaked = _future_dates(trigger, day)
                if leaked:
                    errors.append({"code": code, "version": version, "error": "FUTURE_DATE_IN_AI_EVIDENCE", "date": day, "leaks": leaked})
                if version == "v2":
                    gate_ok, gate_error = _v2_gate_quality(trigger.get("required_gates"), str(trigger.get("scenario") or ""))
                    if not gate_ok:
                        errors.append({"code": code, "version": version, "error": "V2_GATES_NOT_FULLY_EVIDENCED", "date": day, "detail": gate_error})
                    taiji_ok, taiji_error = _v2_taiji_quality(trigger)
                    if not taiji_ok:
                        errors.append({"code": code, "version": version, "error": "V2_TAIJI_NOT_CANONICAL", "date": day, "detail": taiji_error})
        for split_day, ratio in relevant_splits.items():
            if split_day not in market_by_date:
                errors.append({"code": code, "version": "stock", "error": "SPLIT_DATE_NOT_IN_MARKET_DATA", "date": split_day, "ratio": ratio})
    expected_carry_in = int(catalog.get("left_censored_carry_in_count") or 0)
    if carry_in_stocks != expected_carry_in:
        errors.append({
            "version": "run",
            "error": "LEFT_CENSORED_CARRY_IN_COUNT_MISMATCH",
            "found": carry_in_stocks,
            "expected": expected_carry_in,
        })
    ordered = [by_code[code] for code in expected if code in by_code]
    MERGED_LEDGER.write_text("".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in ordered), encoding="utf-8")
    # A structurally labelled ADD is only executable when the causal replay
    # still has an open, profitable mother position at that signal close.
    # Validate that state before any performance report is allowed to run so
    # a stopped episode cannot silently turn a legitimate re-entry into an
    # ignored add candidate.
    if not errors and len(ordered) == EXPECTED_STOCKS:
        for row in ordered:
            code = str(row["code"])
            item = source_items[code]
            frame = _load_frame_asof(item)
            first = _first_selected(code, row, item, meta, daily)
            for version in ("v1", "v2"):
                signals = _signals_for(version, row, frame, daily.get(code, {}), first)
                if not signals:
                    continue
                lifecycle = _lifecycle_for(version, row, first)
                simulated = _simulate_stock(item, signals, lifecycle, allow_adds=True)
                for event in simulated.get("audit", []):
                    name = str(event.get("event") or "")
                    reason = str(event.get("reason") or "")
                    if name.startswith("ADD_SKIPPED"):
                        errors.append({
                            "code": code,
                            "version": version,
                            "error": "AI_ADD_NOT_PROFITABLE_AT_SIGNAL",
                            "date": event.get("signal_date") or event.get("date"),
                        })
                    elif name.startswith("SIGNAL_EVIDENCE_ONLY") and "AI僅核准加碼" in reason:
                        errors.append({
                            "code": code,
                            "version": version,
                            "error": "AI_ADD_WITHOUT_ACTIVE_MOTHER",
                            "date": event.get("signal_date") or event.get("date"),
                        })
                    elif name.startswith("SIGNAL_EVIDENCE_ONLY") and "AI核准母單／再進場，但當時已有持倉" in reason:
                        errors.append({
                            "code": code,
                            "version": version,
                            "error": "AI_MOTHER_WITH_ACTIVE_POSITION",
                            "date": event.get("signal_date") or event.get("date"),
                        })
    result = {
        "as_of": AS_OF,
        "expected_stocks": EXPECTED_STOCKS,
        "manifest_stocks": len(expected),
        "reviewed_stocks": len(rows),
        "audited_rows": audited_rows,
        "left_censored_carry_in_stocks": carry_in_stocks,
        "newly_selected_on_or_after_monitor_floor": len(expected) - carry_in_stocks,
        "corporate_actions": {"dividend_dates": dividend_events, "split_dates": split_events, "unresolved_stocks": sum(error["error"] == "UNRESOLVED_CORPORATE_ACTION" for error in errors)},
        "missing_codes": missing,
        "extra_codes": extra,
        "duplicate_codes": sorted(set(duplicate_codes)),
        "trigger_counts": trigger_counts,
        "intent_counts": {version: dict(counts) for version, counts in intent_counts.items()},
        "validation_errors": errors,
        "valid": len(rows) == EXPECTED_STOCKS and len(expected) == EXPECTED_STOCKS and not missing and not extra and not duplicate_codes and not errors,
        "ledger_files": [{"path": str(path.resolve()), "sha256": _digest(path)} for path in paths],
        "input_manifest": {"path": str(FACT_MANIFEST.resolve()), "sha256": _digest(FACT_MANIFEST)},
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
    for event in lifecycle:
        lifecycle_by_date[str(event["date"])].append(str(event["event"]))
    # Carry-in stocks were already selected before the 2023-05-02 cohort
    # boundary.  Preserve those historical upstream origins for attribution,
    # while still prohibiting any trade before the monitoring boundary.
    initial_sources = sorted({
        source
        for source_day, sources in daily_sources.items()
        if source_day <= first
        for source in sources
    })
    output: list[dict[str, Any]] = []
    for trigger in sorted((row.get(version) or {}).get("triggers") or [], key=lambda value: str(value["signal_date"])):
        day = str(trigger["signal_date"])
        market = indexed.loc[day]
        if isinstance(market, pd.DataFrame):
            market = market.iloc[-1]
        _, stop_price = _stop(trigger)
        accumulated: set[str] = set()
        pre_trigger: set[str] = set()
        active = False
        campaign = 0
        for source_day in sorted(set(daily_sources) | set(lifecycle_by_date)):
            if source_day > day:
                break
            events = lifecycle_by_date.get(source_day, [])
            for name in events:
                if name.startswith("WATCHING") or name.startswith("RESELECTED"):
                    accumulated.clear()
                    active = True
                    campaign += 1
                elif name.startswith("CAMPAIGN_INVALIDATED") or name.startswith("REMOVED_FROM_WATCHLIST"):
                    accumulated.clear()
                    active = False
            if active and source_day < day:
                accumulated.update(daily_sources.get(source_day, []))
            if source_day == day:
                pre_trigger = set(accumulated)
                if active:
                    accumulated.update(daily_sources.get(source_day, []))
        output.append({
            "signal_date": day,
            "family": _trigger_path(trigger),
            "scenario": _scenario(trigger),
            "stop": float(stop_price),
            "signal_close": float(market["close"]),
            "signal_atr": float(market["atr14"]),
            "risk_pct_at_signal": (float(market["close"]) - float(stop_price)) / float(market["close"]) * 100,
            "risk_atr_at_signal": (float(market["close"]) - float(stop_price)) / float(market["atr14"]),
            "score": None,
            "code": str(row["code"]),
            "name": str(row.get("name") or ""),
            "campaign": int(trigger.get("campaign") or campaign or 1),
            "intent": str(trigger.get("episode_or_add_candidate") or ""),
            "monitor_on": first,
            "initial_monitor_strategies": initial_sources,
            "pre_trigger_selection_strategies": sorted(pre_trigger),
            "active_selection_strategies": sorted(accumulated),
            "selected_today_strategies": sorted(set(daily_sources.get(day, []))),
            "ai_evidence": trigger.get("evidence"),
            "ai_invalidation": trigger.get("invalidation"),
        })
    return output


def _load_frame_asof(item: dict[str, Any]) -> pd.DataFrame:
    """Load raw facts and keep the deterministic engine inside the holdout."""
    frame = pd.read_csv(item["price_path"], parse_dates=["date"])
    frame = frame[frame["date"] <= pd.Timestamp(AS_OF)].copy().reset_index(drop=True)
    return execution.add_indicators(frame)


def _shares_text(value: float) -> str:
    rounded = round(float(value))
    return str(rounded) if math.isclose(float(value), rounded, abs_tol=1e-8) else f"{float(value):.6f}".rstrip("0").rstrip(".")


def _apply_split(current: dict[str, Any], day: str, ratio: float, audit: list[dict[str, Any]]) -> None:
    """Apply an ex-date split before cash flows and executions on that date.

    Technical comparisons are performed in the adjusted-price coordinate
    system, so their adjusted values remain unchanged.  The raw per-share
    entry/stop bases are divided by the split ratio for an explicit audit trail.
    """
    adjustments = []
    for tranche in current["tranches"]:
        before_shares = float(tranche["shares"])
        before_entry_basis = float(tranche.get("raw_entry_basis", tranche["entry_price_raw"]))
        before_stop_basis = float(tranche.get("raw_stop_basis", tranche["stop_adjusted"]))
        after_shares = before_shares * ratio
        tranche["shares"] = after_shares
        tranche["raw_entry_basis"] = before_entry_basis / ratio
        tranche["raw_stop_basis"] = before_stop_basis / ratio
        detail = {
            "date": day,
            "ratio": ratio,
            "shares_before": before_shares,
            "shares_after": after_shares,
            "raw_entry_basis_before": before_entry_basis,
            "raw_entry_basis_after": tranche["raw_entry_basis"],
            "raw_stop_basis_before": before_stop_basis,
            "raw_stop_basis_after": tranche["raw_stop_basis"],
            "adjusted_technical_basis_unchanged": True,
        }
        tranche.setdefault("share_adjustments", []).append(detail)
        adjustments.append({"role": tranche["role"], **detail})
    audit.append({
        "date": day,
        "event": "SPLIT_ADJUSTED（除權／分割調整）",
        "episode": current["episode_id"],
        "ratio": ratio,
        "tranches": adjustments,
    })


def _simulate_stock(
    item: dict[str, Any],
    signals: list[dict[str, Any]],
    lifecycle: list[dict[str, Any]],
    *,
    allow_adds: bool,
) -> dict[str, Any]:
    """Deterministically execute AI-approved signals with corporate actions."""
    frame = _load_frame_asof(item)
    frame["latest_small_pivot"] = frame.latest_pivot_low
    signal_map = {str(value["signal_date"]): value for value in signals}
    lifecycle_by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for value in lifecycle:
        lifecycle_by_date[str(value["date"])].append(value)
    distributions, splits, unresolved = _corporate_actions(item)
    if unresolved:
        raise ValueError(f"unresolved corporate action for {item['code']}: {unresolved}")
    start_day = min([str(value["monitor_on"]) for value in signals], default=None)
    if start_day is None:
        return {"code": item["code"], "name": item["name"], "episodes": [], "audit": []}
    start_rows = frame.index[frame.date.dt.strftime("%Y-%m-%d") >= start_day]
    if len(start_rows) == 0:
        return {"code": item["code"], "name": item["name"], "episodes": [], "audit": []}

    watch_active = False
    pending_buy: dict[str, Any] | None = None
    pending_exit: dict[str, Any] | None = None
    current: dict[str, Any] | None = None
    episodes: list[dict[str, Any]] = []
    audit: list[dict[str, Any]] = []
    episode_number = 0
    campaign_structure_keys: set[str] = set()
    for _, bar in frame.iloc[int(start_rows[0]):].iterrows():
        day = bar.date.date().isoformat()

        # Cash dividends belong to the shares held immediately before ex-date.
        # The split/right adjustment then changes the shares and raw per-share
        # basis before this session's exits or new entries.
        if current and day in distributions:
            amount = float(distributions[day]) * sum(float(t["shares"]) for t in current["tranches"])
            current["dividend_cash"] += amount
            audit.append({"date": day, "event": "CASH_DIVIDEND（現金股利）", "episode": current["episode_id"], "cash": amount})
        if current and day in splits:
            _apply_split(current, day, float(splits[day]), audit)

        if current and pending_exit:
            raw_price = float(bar.raw_open)
            for tranche in current["tranches"]:
                tranche["sell_price"] = raw_price
                tranche["sell_date"] = day
                tranche["sell_shares"] = float(tranche["shares"])
                tranche["sell_proceeds"] = execution._sell_proceeds(float(tranche["shares"]), raw_price)
            current["exit_date"] = day
            current["exit_price_raw"] = raw_price
            current["exit_price_adjusted"] = float(bar.open)
            current["exit_reason"] = pending_exit["reason"]
            current["status"] = "CLOSED（交易已結束）"
            current["realized_pnl"] = (
                sum(float(t["sell_proceeds"]) - float(t["buy_cost"]) for t in current["tranches"])
                + float(current["dividend_cash"])
            )
            current["net_pnl"] = current["realized_pnl"]
            episodes.append(current)
            audit.append({"date": day, "event": "SELL（賣出）", "episode": current["episode_id"]})
            current = None
            pending_exit = None

        if pending_buy:
            adjusted_open = float(bar.open)
            raw_open = float(bar.raw_open)
            cap = float(pending_buy["signal_close"]) + 0.5 * float(pending_buy["signal_atr"])
            factor = adjusted_open / raw_open
            tick_tolerance = execution._tw_stock_tick(cap / factor) * factor
            if adjusted_open <= float(pending_buy["stop"]):
                audit.append({"date": day, "event": "BUY_SKIPPED（取消進場）", "signal_date": pending_buy["signal_date"], "reason": "開盤跌破防線"})
            elif adjusted_open > cap + tick_tolerance:
                audit.append({"date": day, "event": "BUY_SKIPPED（取消進場）", "signal_date": pending_buy["signal_date"], "reason": "開盤超過0.5ATR追價線及一個最小跳動單位"})
            else:
                shares = math.floor(execution.NOMINAL_PER_TRANCHE / (raw_open * (1 + BUY_COMMISSION)))
                if shares > 0:
                    raw_stop_basis = float(pending_buy["stop"]) / factor
                    tranche = {
                        "role": pending_buy["role"],
                        "signal_date": pending_buy["signal_date"],
                        "entry_date": day,
                        "entry_price_raw": raw_open,
                        "entry_price_adjusted": adjusted_open,
                        "entry_shares": shares,
                        "shares": float(shares),
                        "buy_cost": execution._buy_cost(shares, raw_open),
                        "stop_adjusted": float(pending_buy["stop"]),
                        "raw_entry_basis": raw_open,
                        "raw_stop_basis": raw_stop_basis,
                        "share_adjustments": [],
                        "family": pending_buy["family"],
                        "scenario": pending_buy["scenario"],
                        "score": pending_buy["score"],
                        "active_selection_strategies": pending_buy["active_selection_strategies"],
                        "independent_structure_key": pending_buy.get("independent_structure_key"),
                    }
                    if current is None:
                        episode_number += 1
                        current = {
                            "code": item["code"],
                            "name": item["name"],
                            "episode_id": f"{item['code']}-E{episode_number}",
                            "campaign": pending_buy["campaign"],
                            "entry_date": day,
                            "mother_entry_adjusted": adjusted_open,
                            "initial_defense": float(pending_buy["stop"]),
                            "dynamic_defense": float(pending_buy["stop"]),
                            "small_pivot_defense": None,
                            "last_warned_small_pivot": None,
                            "small_pivot_warning_count": 0,
                            "initial_risk": adjusted_open - float(pending_buy["stop"]),
                            "profit_protect": False,
                            "profit_protect_activation_date": None,
                            "below_ma21_streak": 0,
                            "tranches": [],
                            "dividend_cash": 0.0,
                            "mfe_high_adjusted": adjusted_open,
                            "mae_low_adjusted": adjusted_open,
                            "peak_net_liquidation_return_pct": -100.0,
                            "exit_signal_date": None,
                        }
                    else:
                        current["dynamic_defense"] = max(float(current["dynamic_defense"]), float(pending_buy["stop"]))
                    current["tranches"].append(tranche)
                    if tranche.get("independent_structure_key"):
                        campaign_structure_keys.add(str(tranche["independent_structure_key"]))
                    if current["profit_protect"]:
                        weighted = sum(
                            float(t["entry_price_adjusted"]) * float(t["shares"])
                            for t in current["tranches"]
                        ) / sum(float(t["shares"]) for t in current["tranches"])
                        current["dynamic_defense"] = max(float(current["dynamic_defense"]), weighted, float(pending_buy["stop"]))
                    audit.append({"date": day, "event": f"BUY_{tranche['role']}（買進）", "episode": current["episode_id"], "signal_date": pending_buy["signal_date"]})
            pending_buy = None

        if current:
            current["mfe_high_adjusted"] = max(float(current["mfe_high_adjusted"]), float(bar.high))
            current["mae_low_adjusted"] = min(float(current["mae_low_adjusted"]), float(bar.low))
            close = float(bar.close)
            current["below_ma21_streak"] = int(current["below_ma21_streak"]) + 1 if close < float(bar.ma21) else 0
            activation = float(current["mother_entry_adjusted"]) + 2 * float(current["initial_risk"])
            if not current["profit_protect"] and float(bar.high) >= activation:
                current["profit_protect"] = True
                current["profit_protect_activation_date"] = day
                weighted = sum(
                    float(t["entry_price_adjusted"]) * float(t["shares"])
                    for t in current["tranches"]
                ) / sum(float(t["shares"]) for t in current["tranches"])
                current["dynamic_defense"] = max(float(current["dynamic_defense"]), weighted)
            reason = None
            if close < float(current["dynamic_defense"]):
                reason = "DYNAMIC_OR_POSITION_DEFENSE（動態或部位防線失守）"
            elif current["profit_protect"] and int(current["below_ma21_streak"]) >= 2:
                reason = "MA21_TWO_CLOSES（波段期連續兩日跌破21MA）"
            if reason:
                current["exit_signal_date"] = day
                pending_exit = {"reason": reason}
                audit.append({"date": day, "event": "EXIT_TRIGGERED（已觸發出場）", "episode": current["episode_id"], "reason": reason})
            else:
                small = current.get("small_pivot_defense")
                if (
                    current["profit_protect"] and small is not None and close < float(small)
                    and current.get("last_warned_small_pivot") != float(small)
                ):
                    current["small_pivot_warning_count"] += 1
                    current["last_warned_small_pivot"] = float(small)
                    audit.append({"date": day, "event": "SMALL_PIVOT_BROKEN（小級樞紐失守警告）", "episode": current["episode_id"], "defense": small})
                if current["profit_protect"] and pd.notna(bar.latest_small_pivot) and float(bar.latest_small_pivot) < close:
                    previous = current.get("small_pivot_defense")
                    current["small_pivot_defense"] = float(bar.latest_small_pivot) if previous is None else max(float(previous), float(bar.latest_small_pivot))
            live_deployed = sum(float(t["buy_cost"]) for t in current["tranches"])
            hypothetical_pnl = (
                sum(
                    execution._sell_proceeds(float(t["shares"]), float(bar.raw_close)) - float(t["buy_cost"])
                    for t in current["tranches"]
                )
                + float(current["dividend_cash"])
            )
            current["peak_net_liquidation_return_pct"] = max(
                float(current["peak_net_liquidation_return_pct"]),
                hypothetical_pnl / live_deployed * 100 if live_deployed else 0.0,
            )

        for lifecycle_event in lifecycle_by_date.get(day, []):
            event_name = str(lifecycle_event["event"])
            if event_name.startswith("WATCHING") or event_name.startswith("RESELECTED"):
                watch_active = True
                if event_name.startswith("RESELECTED") or not campaign_structure_keys:
                    campaign_structure_keys.clear()
            elif event_name.startswith("CAMPAIGN_INVALIDATED") or event_name.startswith("REMOVED_FROM_WATCHLIST"):
                watch_active = False
                pending_buy = None
                audit.append({"date": day, "event": "CAMPAIGN_INVALIDATED（大結構交易週期失效）", "source_event": event_name})
                if current and pending_exit is None:
                    current["exit_signal_date"] = day
                    pending_exit = {"reason": event_name}

        decision = signal_map.get(day)
        if decision and watch_active and pending_exit is None:
            intent = _intent_kind(decision.get("intent"))
            if current is None:
                if intent == "ADD":
                    audit.append({"date": day, "event": "SIGNAL_EVIDENCE_ONLY（僅保存訊號證據）", "signal_date": day, "reason": "AI僅核准加碼，但當時無母單持倉"})
                    continue
                structure_key = decision.get("independent_structure_key")
                if structure_key and str(structure_key) in campaign_structure_keys:
                    audit.append({
                        "date": day, "event": "REENTRY_SKIPPED_DUPLICATE_STRUCTURE（取消舊結構再進）",
                        "signal_date": day, "independent_structure_key": structure_key,
                        "reason": "同一個控制樞紐已在本campaign成交，停損／出場後必須等待新的獨立結構",
                    })
                    continue
                role = "REENTRY（再進母單）" if campaign_structure_keys else "MOTHER（母單）"
                if role.startswith("REENTRY"):
                    audit.append({
                        "date": day, "event": "POST_STOP_NEW_STRUCTURE（出場後新結構再進訊號）",
                        "signal_date": day, "independent_structure_key": structure_key,
                    })
            elif allow_adds and len(current["tranches"]) < 3:
                if intent not in {"ADD", "MOTHER_OR_ADD"}:
                    audit.append({"date": day, "event": "SIGNAL_EVIDENCE_ONLY（僅保存訊號證據）", "signal_date": day, "reason": "AI核准母單／再進場，但當時已有持倉"})
                    continue
                structure_key = decision.get("independent_structure_key")
                used_structure_keys = {
                    str(tranche["independent_structure_key"])
                    for tranche in current["tranches"]
                    if tranche.get("independent_structure_key")
                }
                if structure_key and str(structure_key) in used_structure_keys:
                    audit.append({
                        "date": day, "event": "ADD_SKIPPED_DUPLICATE_STRUCTURE（取消重複結構加碼）",
                        "signal_date": day, "independent_structure_key": structure_key,
                        "reason": "同一個已確認控制樞紐已被本episode使用，不構成新的獨立結構",
                    })
                    continue
                hypothetical_pnl = (
                    sum(
                        execution._sell_proceeds(float(t["shares"]), float(bar.raw_close)) - float(t["buy_cost"])
                        for t in current["tranches"]
                    )
                    + float(current["dividend_cash"])
                )
                if hypothetical_pnl <= 0:
                    audit.append({"date": day, "event": "ADD_SKIPPED（取消加碼）", "signal_date": day, "reason": "扣除成本後持倉尚未獲利"})
                    continue
                role = f"ADD_{len(current['tranches'])}（第{len(current['tranches'])}次加碼）"
            else:
                audit.append({"date": day, "event": "SIGNAL_EVIDENCE_ONLY（僅保存訊號證據）", "signal_date": day})
                continue
            pending_buy = {**decision, "role": role}

    if pending_buy:
        audit.append({
            "date": AS_OF,
            "event": "UNEXECUTED_END_OF_WINDOW（期末無下一交易日）",
            "signal_date": pending_buy["signal_date"],
            "intent": pending_buy.get("intent"),
            "reason": "訊號日為回放期最後交易日，窗內沒有下一開盤",
        })
    if current:
        final = frame.iloc[-1]
        if pending_exit:
            audit.append({
                "date": AS_OF,
                "event": "UNEXECUTED_EXIT_END_OF_WINDOW（期末無下一交易日出場）",
                "signal_date": current.get("exit_signal_date"),
                "reason": pending_exit["reason"],
            })
        current["status"] = "OPEN（持有中）"
        current["exit_date"] = None
        current["exit_price_raw"] = None
        current["exit_price_adjusted"] = None
        current["exit_reason"] = "AS_OF（截至回測日）"
        current["mark_date"] = final.date.date().isoformat()
        current["mark_price_raw"] = float(final.raw_close)
        current["mark_price_adjusted"] = float(final.close)
        current["net_pnl"] = (
            sum(execution._sell_proceeds(float(t["shares"]), float(final.raw_close)) - float(t["buy_cost"]) for t in current["tranches"])
            + float(current["dividend_cash"])
        )
        episodes.append(current)
    for episode in episodes:
        episode["tranche_count"] = len(episode["tranches"])
        episode["deployed_cash"] = sum(float(t["buy_cost"]) for t in episode["tranches"])
        episode["net_return_on_deployed_pct"] = float(episode["net_pnl"]) / float(episode["deployed_cash"]) * 100
        episode["mfe_pct_from_mother"] = (float(episode["mfe_high_adjusted"]) / float(episode["mother_entry_adjusted"]) - 1) * 100
        episode["mae_pct_from_mother"] = (float(episode["mae_low_adjusted"]) / float(episode["mother_entry_adjusted"]) - 1) * 100
        end = episode.get("exit_date") or AS_OF
        episode["holding_sessions"] = int(
            ((frame.date >= pd.Timestamp(episode["entry_date"])) & (frame.date <= pd.Timestamp(end))).sum()
        )
    return {"code": item["code"], "name": item["name"], "episodes": episodes, "audit": audit}


def _simulate_enriched(
    item: dict[str, Any],
    signals: list[dict[str, Any]],
    lifecycle: list[dict[str, Any]],
    *,
    allow_adds: bool,
) -> dict[str, Any]:
    stock = _simulate_stock(item, signals, lifecycle, allow_adds=allow_adds)
    signal_by_date = {str(signal["signal_date"]): signal for signal in signals}
    for episode in stock.get("episodes", []):
        for tranche in episode.get("tranches", []):
            signal = signal_by_date[str(tranche["signal_date"])]
            tranche["initial_monitor_strategies"] = signal["initial_monitor_strategies"]
            tranche["pre_trigger_selection_strategies"] = signal["pre_trigger_selection_strategies"]
            tranche["selected_today_strategies"] = signal["selected_today_strategies"]
            tranche["ai_intent"] = signal["intent"]
    return stock


def _shares_on(tranche: dict[str, Any], day: str) -> float:
    shares = float(tranche.get("entry_shares", tranche["shares"]))
    for adjustment in tranche.get("share_adjustments") or []:
        if str(adjustment.get("date") or "") <= day:
            shares *= float(adjustment["ratio"])
    return shares


def _shares_before(tranche: dict[str, Any], day: str) -> float:
    shares = float(tranche.get("entry_shares", tranche["shares"]))
    for adjustment in tranche.get("share_adjustments") or []:
        if str(adjustment.get("date") or "") < day:
            shares *= float(adjustment["ratio"])
    return shares


def _mark_to_market_drawdown(result: dict[str, Any], items: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Build a split-aware daily liquidation-value curve.

    The base is peak concurrent deployed cash, matching the prior comparison
    report.  It is a research risk view and not an account CAGR.
    """
    episodes = [episode for stock in result["stocks"] for episode in stock["episodes"]]
    if not episodes:
        return {
            "base_cash": 0.0,
            "max_drawdown_twd": 0.0,
            "max_drawdown_pct": 0.0,
            "peak_date": None,
            "trough_date": None,
            "method": "無成交回合。",
        }
    start = min(str(episode["entry_date"]) for episode in episodes)
    price_by_code: dict[str, pd.Series] = {}
    dividend_by_code: dict[str, dict[str, float]] = {}
    all_dates: set[str] = set()
    for stock in result["stocks"]:
        code = str(stock["code"])
        frame = pd.read_csv(items[code]["price_path"], usecols=["date", "raw_close"])
        frame["date"] = frame["date"].astype(str)
        frame = frame[(frame["date"] >= start) & (frame["date"] <= AS_OF)]
        price_by_code[code] = frame.set_index("date")["raw_close"].astype(float)
        all_dates.update(frame["date"].tolist())
        dividends, _, unresolved = _corporate_actions(items[code])
        if unresolved:
            raise ValueError(f"unresolved corporate action for {code}: {unresolved}")
        dividend_by_code[code] = dividends

    base_cash = float(result["summary"]["peak_concurrent_deployed_cash"])
    peak_equity = base_cash
    peak_date = start
    maximum_amount = 0.0
    maximum_pct = 0.0
    trough_date = start
    for day in sorted(all_dates):
        portfolio_pnl = 0.0
        for episode in episodes:
            entry_date = str(episode["entry_date"])
            if day < entry_date:
                continue
            exit_date = episode.get("exit_date")
            if exit_date and day >= str(exit_date):
                portfolio_pnl += float(episode["net_pnl"])
                continue
            series = price_by_code[str(episode["code"])]
            eligible = series.loc[:day]
            if eligible.empty:
                continue
            mark = float(eligible.iloc[-1])
            live_tranches = [value for value in episode["tranches"] if str(value["entry_date"]) <= day]
            portfolio_pnl += sum(
                execution._sell_proceeds(_shares_on(tranche, day), mark) - float(tranche["buy_cost"])
                for tranche in live_tranches
            )
            for ex_date, amount in dividend_by_code[str(episode["code"])].items():
                if entry_date <= ex_date <= day:
                    eligible_shares = sum(
                        _shares_before(tranche, ex_date)
                        for tranche in live_tranches
                        if str(tranche["entry_date"]) <= ex_date
                    )
                    portfolio_pnl += float(amount) * eligible_shares
        equity = base_cash + portfolio_pnl
        if equity > peak_equity:
            peak_equity = equity
            peak_date = day
        drawdown = peak_equity - equity
        drawdown_pct = drawdown / peak_equity * 100 if peak_equity else 0.0
        if drawdown > maximum_amount:
            maximum_amount = drawdown
            maximum_pct = drawdown_pct
            trough_date = day
    return {
        "base_cash": round(base_cash, 2),
        "max_drawdown_twd": round(maximum_amount, 2),
        "max_drawdown_pct": round(maximum_pct, 4),
        "peak_date": peak_date,
        "trough_date": trough_date,
        "method": "以尖峰投入資金作基準本金，逐日按估計可變現淨值計算；分割日依比例重建當日股數，未設資金上限。",
    }


def _source_breakdown(variant: dict[str, Any], field: str, *, by_scenario: bool = False) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str | None], list[dict[str, Any]]] = defaultdict(list)
    for stock in variant["stocks"]:
        for episode in stock.get("episodes", []):
            mother = episode["tranches"][0]
            sources = mother.get(field) or ["UNKNOWN_SELECTION_SOURCE"]
            scenario = str(mother["scenario"]) if by_scenario else None
            for source in sources:
                grouped[(str(source), scenario)].append(episode)
    output = []
    for (source, scenario), episodes in grouped.items():
        pnl = [float(row["net_pnl"]) for row in episodes]
        returns = [float(row["net_return_on_deployed_pct"]) for row in episodes]
        mfe = [float(row["mfe_pct_from_mother"]) for row in episodes]
        output.append({
            "strategy": source,
            "scenario": scenario,
            "episodes": len(episodes),
            "open": sum(str(row["status"]).startswith("OPEN") for row in episodes),
            "net_pnl_full_overlap_attribution": round(sum(pnl), 2),
            "average_return_pct": statistics.fmean(returns),
            "average_mfe_pct": statistics.fmean(mfe),
            "win_rate_pct": sum(value > 0 for value in pnl) / len(pnl) * 100,
        })
    return sorted(output, key=lambda row: (-row["episodes"], -row["net_pnl_full_overlap_attribution"], row["strategy"], str(row["scenario"])))


def replay() -> dict[str, Any]:
    validation = validate_and_merge()
    fact, source, catalog, expected_items = _input_context()
    items = {str(row["code"]): row for row in (source.get("items") or expected_items)}
    daily, meta = _catalog_maps(catalog)
    ledgers = {str(row["code"]): row for row in _load_jsonl(MERGED_LEDGER)}
    output: dict[str, Any] = {
        "method_version": "formal-ai-historical-ledger-replay-v2",
        "judgement_scope": "FORMAL_CODEX_AI_PER_STOCK（Codex AI逐檔判讀；非分數代理）",
        "outcome_blindness": f"TEMPORAL_HOLDOUT_CAUSAL_REPLAY（規則於2026凍結後回放至{AS_OF}；最終證據按每個訊號日裁切）",
        "as_of": AS_OF,
        "validation": validation,
        "ai_decision_ledger": {"path": str(MERGED_LEDGER.resolve()), "sha256": _digest(MERGED_LEDGER)},
        "rules": {"v1": str(V1_DOC.resolve()), "v2": str(V2_DOC.resolve())},
        "rule_sha256": {"v1": _digest(V1_DOC), "v2": _digest(V2_DOC)},
        "costs": {"buy_commission": BUY_COMMISSION, "sell_commission": SELL_COMMISSION, "sell_tax": SELL_TAX},
        "selection_window": catalog.get("selection_window") or catalog.get("window"),
        "monitoring_policy": {
            "type": "DYNAMIC_ENTRY_BY_FIRST_SELECTION（依實際首次入選日加入監控）",
            "left_censored_carry_in": validation["left_censored_carry_in_stocks"],
            "new_on_or_after_floor": validation["newly_selected_on_or_after_monitor_floor"],
            "monitor_floor": MONITOR_FLOOR,
            "selection_window": {"start": SELECTION_START, "end": SELECTION_END},
        },
        "variants": {},
    }
    expected_by_code = {str(row["code"]): row for row in expected_items}
    for version in ("v1", "v2"):
        signals_by_code: dict[str, list[dict[str, Any]]] = {}
        lifecycle_by_code: dict[str, list[dict[str, Any]]] = {}
        for code, row in ledgers.items():
            item = items[code]
            frame = _load_frame_asof(item)
            first = _first_selected(code, row, expected_by_code[code], meta, daily)
            signals = _signals_for(version, row, frame, daily.get(code, {}), first)
            lifecycle_by_code[code] = _lifecycle_for(version, row, first)
            if signals:
                signals_by_code[code] = signals
        for position_key, allow_adds in (("MOTHER_ONLY_10K", False), ("MOTHER_PLUS_2", True)):
            key = f"{version.upper()}_{position_key}"
            result = {"name": key, "stocks": []}
            for code, signals in sorted(signals_by_code.items()):
                result["stocks"].append(
                    _simulate_enriched(items[code], signals, lifecycle_by_code[code], allow_adds=allow_adds)
                )
            result["summary"] = execution._summary(result)
            replay_audit = [event for stock in result["stocks"] for event in stock.get("audit", [])]
            result["summary"]["unexecuted_end_of_window"] = sum(
                str(event.get("event") or "").startswith("UNEXECUTED_END_OF_WINDOW")
                for event in replay_audit
            )
            result["summary"]["split_adjustment_events"] = sum(
                str(event.get("event") or "").startswith("SPLIT_ADJUSTED")
                for event in replay_audit
            )
            result["summary"]["cash_dividend_events"] = sum(
                str(event.get("event") or "").startswith("CASH_DIVIDEND")
                for event in replay_audit
            )
            result["summary"]["mark_to_market_drawdown"] = _mark_to_market_drawdown(result, items)
            result["summary"]["initial_monitor_source_breakdown_overlap"] = _source_breakdown(result, "initial_monitor_strategies")
            result["summary"]["pre_trigger_source_breakdown_overlap"] = _source_breakdown(result, "pre_trigger_selection_strategies")
            # Most of this historical cohort was selected before monitoring began
            # on 2023-05-02.  Cross-tab scenarios against the actual carry-in
            # selection sources, not the usually-empty post-floor source list.
            result["summary"]["strategy_scenario_breakdown_overlap"] = _source_breakdown(result, "initial_monitor_strategies", by_scenario=True)
            output["variants"][key] = result
    _write_json(RESULT_PATH, output)
    return output


def _money(value: float) -> str:
    return f"{value:+,.0f}"


def _pct(value: float) -> str:
    return f"{value:+.2f}%"


def _pf(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f}"


def _cell(value: Any) -> str:
    return str(value if value is not None else "—").replace("|", "／").replace("\n", " ")


def _portfolio_table(result: dict[str, Any]) -> list[str]:
    lines = [
        "| 組合 | 股票 | 回合 | 已出場／持有 | 買進份數 | 期末未成交 | 已實現 | 未實現* | 淨損益 | 尖峰資金報酬 | 勝率 | PF | 最大持股／份數 | 尖峰資金 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for key, variant in result["variants"].items():
        summary = variant["summary"]
        lines.append(
            f"| {key} | {summary['stocks_traded']} | {summary['trade_episodes']} | {summary['closed']}／{summary['open']} | {summary['buy_fills']} | {summary.get('unexecuted_end_of_window', 0)} | "
            f"{_money(summary['realized_net_pnl'])} | {_money(summary['unrealized_net_pnl_after_estimated_exit_cost'])} | {_money(summary['net_pnl'])} | "
            f"{_pct(summary['return_on_peak_capital_pct'])} | {summary['win_rate_pct']:.2f}% | {_pf(summary['profit_factor'])} | "
            f"{summary['maximum_concurrent_stocks']}／{summary['maximum_concurrent_tranches']} | {summary['peak_concurrent_deployed_cash']:,.0f} |"
        )
    return lines


def _signal_cross(rows: list[dict[str, Any]]) -> dict[str, int]:
    events: dict[str, set[tuple[str, str]]] = {"v1": set(), "v2": set()}
    stocks: dict[str, set[str]] = {"v1": set(), "v2": set()}
    scenarios: dict[str, dict[tuple[str, str], str]] = {"v1": {}, "v2": {}}
    for row in rows:
        code = str(row["code"])
        for version in ("v1", "v2"):
            for trigger in (row.get(version) or {}).get("triggers") or []:
                event = (code, str(trigger["signal_date"]))
                events[version].add(event)
                stocks[version].add(code)
                scenarios[version][event] = _scenario(trigger)
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
        "same_date_scenario_changed": sum(scenarios["v1"].get(event) != scenarios["v2"].get(event) for event in shared),
    }


def _anchor_brief(value: Any) -> str:
    if not isinstance(value, dict):
        return _cell(value)
    start = value.get("start_date") or value.get("start")
    end = value.get("end_date") or value.get("end") or "FORMING"
    status = value.get("status") or ""
    return _cell(f"{start or '?'}→{end} {status}".strip())


def _write_decision_audit(rows: list[dict[str, Any]]) -> Path:
    _, _, catalog, expected_items = _input_context()
    daily, meta = _catalog_maps(catalog)
    items = {str(row["code"]): row for row in expected_items}
    lines = [
        f"# {REPORT_LABEL}正式Codex AI決策稽核表（{EXPECTED_STOCKS}檔）",
        "",
        "> 每股均由正式AI ledger提供判讀；本表不重新分類、不計分、不新增訊號。完整機器可讀內容見 `ai_decisions_merged.jsonl`。",
        "",
        "| Index | 股票 | 首次監控／來源 | 規格 | AI結果 | 訊號／母加碼意圖 | 不交易原因 | 監控生命週期 | Audit |",
        "|---:|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        code = str(row["code"])
        first = _first_selected(code, row, items[code], meta, daily)
        first_sources = "、".join(sorted({
            source
            for source_day, sources in daily.get(code, {}).items()
            if source_day <= first
            for source in sources
        })) or "UNKNOWN_SELECTION_SOURCE"
        audit = row.get("audit") or {}
        audit_text = f"{audit.get('outcome_blindness')}／future={audit.get('future_outcomes_used_in_final_evidence')}"
        for version in ("v1", "v2"):
            section = row.get(version) or {}
            triggers = section.get("triggers") or []
            trigger_text = "；".join(
                f"{trigger.get('signal_date')} {_scenario(trigger)}／{trigger.get('trigger_path')}／{trigger.get('episode_or_add_candidate')}；"
                f"防線{trigger.get('stop_price')}({trigger.get('stop_date')})；錨{_anchor_brief(trigger.get('macro_anchor'))}"
                for trigger in triggers
            )
            lifecycle = section.get("watchlist_events") or row.get("watchlist_events") or []
            lifecycle_text = "；".join(f"{value.get('date')} {value.get('event')}" for value in lifecycle if isinstance(value, dict))
            lines.append(
                f"| {row.get('index')} | {_cell(code + ' ' + str(row.get('name') or ''))} | {_cell(first + '／' + first_sources)} | {version.upper()} | "
                f"{'TRIGGERED（有觸發）' if triggers else 'NO_TRADE（無交易）'} | {_cell(trigger_text)} | "
                f"{_cell(section.get('no_trade_reason'))} | {_cell(lifecycle_text)} | {_cell(audit_text)} |"
            )
    DECISION_REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return DECISION_REPORT_PATH


def _append_source_table(lines: list[str], title: str, rows: list[dict[str, Any]], *, scenario: bool = False) -> None:
    lines += ["", f"### {title}", "", "> 同一交易可同時歸屬多個上游來源，以下為重疊歸因，不可直接加總。", ""]
    if scenario:
        lines += ["| 策略 | AI情境 | 回合／持有 | 淨損益 | 平均報酬 | 勝率 | 平均MFE |", "|---|---|---:|---:|---:|---:|---:|"]
    else:
        lines += ["| 策略 | 回合／持有 | 淨損益 | 平均報酬 | 勝率 | 平均MFE |", "|---|---:|---:|---:|---:|---:|"]
    for row in rows:
        prefix = f"| {_cell(row['strategy'])} | {_cell(row['scenario'])} |" if scenario else f"| {_cell(row['strategy'])} |"
        lines.append(
            f"{prefix} {row['episodes']}／{row['open']} | {_money(row['net_pnl_full_overlap_attribution'])} | "
            f"{_pct(row['average_return_pct'])} | {row['win_rate_pct']:.2f}% | {_pct(row['average_mfe_pct'])} |"
        )


def _append_variant_analysis(lines: list[str], key: str, variant: dict[str, Any]) -> None:
    summary = variant["summary"]
    lines += [
        "", f"## {key}", "",
        f"- 平均／中位報酬：{_pct(summary['average_return_pct'])}／{_pct(summary['median_return_pct'])}；平均／中位MFE：{_pct(summary['average_mfe_pct'])}／{_pct(summary['median_mfe_pct'])}。",
        f"- 已出場平均／勝率／PF：{_pct(summary['closed_average_return_pct'])}／{summary['closed_win_rate_pct']:.2f}%／{_pf(summary['closed_profit_factor'])}。",
        f"- 持有中：{summary['open']}回合、{summary['current_open_tranches']}份、平均{_pct(summary['open_average_return_pct'])}、勝率{summary['open_win_rate_pct']:.2f}%。",
        f"- MFE≥20%：{summary['mfe_20_plus_count']}筆；最終仍正報酬{summary['mfe_20_plus_final_positive_count']}筆；已出場平均峰值回吐{summary['closed_average_giveback_points']:.2f}個百分點。",
        f"- 最大同時持有{summary['maximum_concurrent_stocks']}檔／{summary['maximum_concurrent_tranches']}份，尖峰資金{summary['peak_concurrent_deployed_cash']:,.0f}元（{summary['peak_date']}）。",
        f"- 最大回撤-{summary['mark_to_market_drawdown']['max_drawdown_twd']:,.0f}元／-{summary['mark_to_market_drawdown']['max_drawdown_pct']:.2f}%。",
        f"- 期末無下一開盤而未成交：{summary.get('unexecuted_end_of_window', 0)}筆；持有期間套用分割調整{summary.get('split_adjustment_events', 0)}次、現金股利{summary.get('cash_dividend_events', 0)}次。",
        "", "### 情境績效", "",
        "| 情境 | 回合／持有 | 淨損益 | 平均報酬 | 勝率 | PF |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for row in summary["scenario_breakdown"]:
        lines.append(f"| {_cell(row['scenario'])} | {row['episodes']}／{row['open']} | {_money(row['net_pnl'])} | {_pct(row['average_return_pct'])} | {row['win_rate_pct']:.2f}% | {_pf(row['profit_factor'])} |")
    lines += ["", "### 損益分布", "", "| 區間 | 全部 | 已出場 | 持有中 | MFE |", "|---|---:|---:|---:|---:|"]
    for label in summary["return_distribution"]:
        lines.append(f"| {label} | {summary['return_distribution'][label]} | {summary['closed_return_distribution'][label]} | {summary['open_return_distribution'][label]} | {summary['mfe_distribution'][label]} |")
    _append_source_table(lines, "首次監控來源", summary["initial_monitor_source_breakdown_overlap"])
    _append_source_table(lines, "觸發前累積來源", summary["pre_trigger_source_breakdown_overlap"])
    _append_source_table(lines, "首次監控來源 × AI情境", summary["strategy_scenario_breakdown_overlap"], scenario=True)
    lines += ["", "### 前十大獲利／虧損", "", "| 類型 | 股票 | 回合 | 淨損益 | 報酬 |", "|---|---|---|---:|---:|"]
    for label, rows in (("獲利", summary["top_winners"]), ("虧損", summary["top_losers"])):
        for row in rows:
            lines.append(f"| {label} | {_cell(str(row['code']) + ' ' + str(row['name']))} | {row['episode_id']} | {_money(row['net_pnl'])} | {_pct(row['return_pct'])} |")


def _write_version_report(version: str, result: dict[str, Any]) -> Path:
    path = V1_REPORT_PATH if version == "V1" else V2_REPORT_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    subset = {key: value for key, value in result["variants"].items() if key.startswith(version + "_")}
    lines = [
        f"# {REPORT_LABEL} {version}正式AI逐筆交易",
        "",
        "> 訊號由Codex AI逐檔判讀；成交、公司行動、出場與統計由確定性回放器處理。",
        "",
        *_portfolio_table({"variants": subset}),
        "",
        f"\\* 未實現損益以{AS_OF}收盤假設賣出並扣估計成本。",
    ]
    for key, variant in subset.items():
        lines += [
            "", f"## {key}", "",
            "| 股票 | 回合 | 部位 | AI意圖 | 情境／觸發型態 | 首次監控來源 | 觸發前來源 | 訊號日 | 成交日 | 調整後／原始進價 | 股數 | 初始防線 | 出場觸發 | 實際出場／截至 | 原始出價／截至價 | 狀態 | MFE | MAE | 淨報酬 | 淨損益 |",
            "|---|---|---|---|---|---|---|---|---|---:|---:|---:|---|---|---:|---|---:|---:|---:|---:|",
        ]
        episodes = [episode for stock in variant["stocks"] for episode in stock["episodes"]]
        for episode in sorted(episodes, key=lambda row: (row["entry_date"], str(row["code"]), row["episode_id"])):
            for tranche_index, tranche in enumerate(episode["tranches"]):
                exit_or_mark = tranche.get("sell_date") or episode.get("mark_date")
                raw_exit_or_mark = tranche.get("sell_price") if tranche.get("sell_price") is not None else episode.get("mark_price_raw")
                lines.append(
                    f"| {_cell(str(episode['code']) + ' ' + str(episode['name']))} | {episode['episode_id']} | {_cell(tranche['role'])} | {_cell(tranche.get('ai_intent'))} | "
                    f"{_cell(str(tranche['scenario']) + '／' + str(tranche['family']))} | {_cell('、'.join(tranche.get('initial_monitor_strategies') or []))} | "
                    f"{_cell('、'.join(tranche.get('pre_trigger_selection_strategies') or []))} | {tranche['signal_date']} | {tranche['entry_date']} | "
                    f"{float(tranche['entry_price_adjusted']):.4f}／{float(tranche['entry_price_raw']):.4f} | {_shares_text(float(tranche['shares']))} | {float(tranche['stop_adjusted']):.4f} | "
                    f"{_cell(episode.get('exit_signal_date'))} | {_cell(exit_or_mark)} | {float(raw_exit_or_mark):.4f} | {_cell(episode['status'])} | "
                    f"{_pct(episode['mfe_pct_from_mother'])} | {_pct(episode['mae_pct_from_mother'])} | {_pct(episode['net_return_on_deployed_pct'])} | "
                    f"{_money(episode['net_pnl']) if tranche_index == 0 else '（計入回合）'} |"
                )
        lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def write_reports() -> Path:
    result = _read(RESULT_PATH)
    validation = _read(VALIDATION_PATH)
    preperformance_qa_path = RUN / "preperformance_qa.json"
    preperformance_qa = _read(preperformance_qa_path) if preperformance_qa_path.exists() else None
    decisions = _load_jsonl(MERGED_LEDGER)
    cross = _signal_cross(decisions)
    lines = [
        f"# {REPORT_LABEL}：正式Codex AI V1／V2交叉回測",
        "",
        f"> `TEMPORAL_HOLDOUT_CAUSAL_REPLAY（時間保留期因果回放）`：V1／V2規則於2026年凍結後，回放截至{AS_OF}的資料；每筆AI證據只能看到訊號日收盤以前。",
        "",
        "> 這不是即時前瞻交易：2023選股名單是在2026年以歷史資料重建，且母體只包含目前仍可辨識的股票，存在目前股票母體的倖存者偏誤。結果可檢查時間外推性，不能視為無偏的即時實盤績效。",
        "",
        "## 完整性與口徑",
        "",
        f"- AI逐檔判讀：{validation['reviewed_stocks']}/{validation['expected_stocks']}檔；逐股audit {validation['audited_rows']}檔。",
        f"- AI核准事件：V1 {validation['trigger_counts']['v1']}筆；V2 {validation['trigger_counts']['v2']}筆。",
        f"- 母單／再進意圖：V1 {validation['intent_counts']['v1'].get('MOTHER', 0)}筆、V2 {validation['intent_counts']['v2'].get('MOTHER', 0)}筆；加碼意圖：V1 {validation['intent_counts']['v1'].get('ADD', 0)}筆、V2 {validation['intent_counts']['v2'].get('ADD', 0)}筆。",
        f"- {EXPECTED_STOCKS}檔覆蓋、index、監控窗、OHLC防線、未來日期、V2 gates、母單／加碼意圖與audit驗證：{'通過' if validation['valid'] else '未通過'}。",
        f"- 監控生命週期：{validation['left_censored_carry_in_stocks']}檔在{MONITOR_FLOOR}以前已入選並以 `LEFT_CENSORED_CARRY_IN（左截尾帶入監控）` 初始化；另{validation['newly_selected_on_or_after_monitor_floor']}檔依實際首次入選日加入。",
        f"- 上游選股窗為{SELECTION_START}至{SELECTION_END}；股票不得早於實際首次入選日交易，且{SELECTION_END}後沒有上游重選事件可恢復已移除的campaign。",
        "- 進場：訊號日收盤成立，下一交易日開盤；開盤跌破防線或超過訊號收盤＋0.5ATR加一跳則取消。每份約一萬元。",
        "- 出場：+2R前使用部位防線；+2R後成本防線只升不降，小級樞紐跌破先警告，連續兩日收盤跌破21MA才全出。",
        f"- 公司行動：{validation['corporate_actions']['dividend_dates']}個股利日期、{validation['corporate_actions']['split_dates']}個分割日期、未解決公司行動{validation['corporate_actions']['unresolved_stocks']}檔。分割於除權日開盤前調整既有股數與原始每股基準；調整後技術座標保持可比。",
        *(
            [
                f"- 績效解封前 QA：{preperformance_qa['status']}；V2必要gate {preperformance_qa['v2_gate_instances']}個，缺少日期＋價位事實{preperformance_qa['v2_gates_without_dated_price_fact']}個，循環論證{preperformance_qa['circular_evidence_occurrences']}個，測試{preperformance_qa['tests_passed']}項通過。",
                f"- 監控窗行情修復：依櫃買中心官方行情修復{preperformance_qa['monitor_window_tpex_ohlc_repairs']['bars']}根不可能OHLC，涉及{preperformance_qa['monitor_window_tpex_ohlc_repairs']['stocks']}檔；修正時未讀績效。",
            ]
            if preperformance_qa
            else []
        ),
        f"- 帳務：原始成交價格、調整後技術價格、現金股利、股票分割、買賣手續費各0.1425%、賣出交易稅0.3%。{AS_OF}訊號若窗內沒有下一開盤，列為 `UNEXECUTED_END_OF_WINDOW（期末無下一交易日）`，不虛構成交。",
        "",
        "## 四組交叉結果",
        "",
        *_portfolio_table(result),
        "",
        f"\\* 未實現損益以{AS_OF}收盤假設賣出並扣估計成本。",
        "",
        "## V1／V2 AI決策差異",
        "",
        f"- V1觸發{cross['v1_events']}筆／{cross['v1_stocks']}檔；V2觸發{cross['v2_events']}筆／{cross['v2_stocks']}檔。",
        f"- 同股同日重疊{cross['shared_events']}筆；V1獨有{cross['v1_only_events']}筆；V2獨有{cross['v2_only_events']}筆；兩版共同股票{cross['shared_stocks']}檔。",
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
    for key, variant in result["variants"].items():
        _append_variant_analysis(lines, key, variant)
    lines += [
        "", "## 研究限制", "",
        "- 規則於2026年凍結，因此2023是時間保留期，AI證據按訊號日因果裁切。",
        "- 2023選股事件是在2026年重建，不等於2023當時真的收到完全相同的清單。",
        "- 股票母體以目前資料源可取得的股票為基礎，可能漏掉下市、合併或代碼失效股票，存在倖存者偏誤。",
        *(
            [
                f"- 監控日前仍有{preperformance_qa['pre_monitor_unrepaired_material_ohlc_anomalies']['bars']}根舊資料不可能OHLC，涉及{preperformance_qa['pre_monitor_unrepaired_material_ohlc_anomalies']['stocks']}檔；未被最終訊號、停損或明列樞紐直接引用，但可能間接影響長週期指標。",
                f"- {preperformance_qa['preferred_750_bars_not_met']}檔未達偏好的750根監控前日K；分群解讀時需另行標示。",
            ]
            if preperformance_qa
            else []
        ),
        "- 未設總資金上限；尖峰資金與尖峰資金報酬是研究性資金需求，不是帳戶CAGR。",
    ]
    REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _write_version_report("V1", result)
    _write_version_report("V2", result)
    _write_decision_audit(decisions)
    return REPORT_PATH


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("validate", "replay", "report", "all"))
    args = parser.parse_args()
    if args.command == "validate":
        print(json.dumps(validate_and_merge(), ensure_ascii=False))
    elif args.command == "replay":
        result = replay()
        print({key: value["summary"]["net_pnl"] for key, value in result["variants"].items()})
    elif args.command == "report":
        print(write_reports())
    else:
        result = replay()
        print({key: value["summary"]["net_pnl"] for key, value in result["variants"].items()})
        print(write_reports())


if __name__ == "__main__":
    main()
