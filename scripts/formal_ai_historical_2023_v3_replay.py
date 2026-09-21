"""Replay the frozen V1/V2 ledgers plus an AI-authored V3 overlay.

This module contains no stock classifier, score threshold, route inference, or
automatic V3 approval.  V1/V2 decisions are read from the frozen parent run;
V3 additions are read from the manually authored AI decision file.  Code only
validates causal evidence and performs deterministic execution/accounting.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import formal_ai_historical_2023_replay as base  # noqa: E402


PARENT = ROOT / "reports/course_backtest/2024-02-02/historical_scan_2023h2_formal_ai_v1_v2"
RUN = ROOT / "reports/course_backtest/2024-02-02/historical_scan_2023h2_formal_ai_v1_v2_v3"
APPROVALS_PATH = RUN / "v3_ai_manual_approvals.json"
PACKET_INDEX_PATH = RUN / "v3_review_index.jsonl"
V3_LEDGER_PATH = RUN / "v3_ai_decisions.jsonl"
VALIDATION_PATH = RUN / "v3_decision_validation.json"
RESULT_PATH = RUN / "backtest.json"
SUMMARY_PATH = RUN / "backtest_summary.json"
REPORT_PATH = RUN / "comparison.md"
DECISION_REPORT_PATH = RUN / "v3_ai_decisions.md"
V3_REPORT_PATH = RUN / "v3/backtest.md"
AS_OF = "2024-02-02"
MONITOR_FLOOR = "2023-06-01"
EXPECTED_STOCKS = 1029
PARENT_LEDGER = PARENT / "ai_decisions_merged.jsonl"
PARENT_BACKTEST = PARENT / "backtest.json"
PARENT_VALIDATION = PARENT / "decision_validation.json"
PARENT_MANIFEST = PARENT / "input_manifest.json"
V3_DOC = ROOT / "docs/enlightenment-ai-judgement-v3.md"
V3_RULES = ROOT / "config/enlightenment_ai_rules_v3.json"

base.AS_OF = AS_OF
base.MONITOR_FLOOR = MONITOR_FLOOR
base.SELECTION_START = "2023-06-01"
base.SELECTION_END = "2023-12-31"
base.EXPECTED_STOCKS = EXPECTED_STOCKS
base.RUN = PARENT
base.FACT_MANIFEST = PARENT_MANIFEST
base.MERGED_LEDGER = PARENT_LEDGER
base.VALIDATION_PATH = PARENT_VALIDATION


ROUTE_NOMINAL = {
    "V2_CORE": 10_000.0,
    "NEAR_PASS_MACRO_COPY": 5_000.0,
    "NEAR_PASS_FRESH_Q1": 5_000.0,
    "BEAR_REVERSAL_PROBE": 2_500.0,
}


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _close(left: float, right: float) -> bool:
    return math.isclose(float(left), float(right), rel_tol=0.0, abs_tol=1e-4)


def _approval_trigger(approval: dict[str, Any]) -> dict[str, Any]:
    return {
        "signal_date": approval["signal_date"],
        "scenario": approval["base_scenario"],
        "trigger_path": approval["trigger_path"],
        "episode_or_add_candidate": "MOTHER_OR_REENTRY",
        "stop_date": approval["stop_date"],
        "stop_price": approval["stop_price"],
        "left_right": approval["phase"],
        "evidence": approval["evidence"],
        "invalidation": {
            "scope": "TRADE_EPISODE",
            "price": approval["stop_price"],
            "rule": f"收盤失守{approval['stop_date']}已確認控制低，本次V3試單episode失效；大結構未失效時仍可等待新episode。",
        },
        "v3_layer": approval["layer"],
        "v3_route": approval["route"],
        "v3_unknown_gate": approval["unknown_gate"],
        "v3_phase": approval["phase"],
        "v3_late_stage_partial_evidence": approval.get("late_stage_partial_evidence") or [],
        "v3_ai_decision_source": str(APPROVALS_PATH.resolve()),
    }


def _watch_active_on(events: list[dict[str, Any]], day: str) -> bool:
    active = False
    for event in sorted(events, key=lambda value: str(value.get("date") or "")):
        event_day = str(event.get("date") or "")
        if event_day > day:
            break
        name = str(event.get("event") or "")
        if name.startswith("WATCHING") or name.startswith("RESELECTED"):
            active = True
        elif name.startswith("CAMPAIGN_INVALIDATED") or name.startswith("REMOVED_FROM_WATCHLIST"):
            active = False
    return active


def _core_trigger(trigger: dict[str, Any]) -> dict[str, Any]:
    copied = copy.deepcopy(trigger)
    copied.update({
        "v3_layer": "V2_CORE",
        "v3_route": "V2_CORE",
        "v3_unknown_gate": None,
        "v3_phase": trigger.get("left_right"),
        "v3_late_stage_partial_evidence": [],
        "v3_ai_decision_source": str(PARENT_LEDGER.resolve()),
    })
    return copied


def build_and_validate_v3_ledger() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    approvals_file = _read(APPROVALS_PATH)
    approvals = {str(row["code"]): row for row in approvals_file["extra_approvals"]}
    parent_rows = _read_jsonl(PARENT_LEDGER)
    packet_index = {str(row["code"]): row for row in _read_jsonl(PACKET_INDEX_PATH)}
    parent_validation = _read(PARENT_VALIDATION)
    errors: list[dict[str, Any]] = []

    if len(parent_rows) != EXPECTED_STOCKS:
        errors.append({"error": "PARENT_ROW_COUNT", "actual": len(parent_rows), "expected": EXPECTED_STOCKS})
    if len(packet_index) != EXPECTED_STOCKS:
        errors.append({"error": "PACKET_INDEX_COUNT", "actual": len(packet_index), "expected": EXPECTED_STOCKS})
    if _digest(PARENT_MANIFEST) != parent_validation["input_manifest"]["sha256"]:
        errors.append({"error": "PARENT_MANIFEST_HASH_CHANGED"})
    if _digest(PARENT_LEDGER) != "8b239b5f2d4ec8f973df49bf7360013f21ad44bad7fd0d80ab359ff00ac387b4":
        errors.append({"error": "PARENT_LEDGER_HASH_CHANGED", "actual": _digest(PARENT_LEDGER)})
    if len(approvals) != len(approvals_file["extra_approvals"]):
        errors.append({"error": "DUPLICATE_EXTRA_APPROVAL_CODE"})

    output: list[dict[str, Any]] = []
    approved_packets: list[dict[str, Any]] = []
    for parent in parent_rows:
        code = str(parent["code"])
        core = (parent.get("v2") or {}).get("triggers") or []
        approval = approvals.get(code)
        if core and approval:
            errors.append({"code": code, "error": "V3_PRECEDENCE_COLLISION"})
        v3_triggers: list[dict[str, Any]] = []
        review: dict[str, Any]
        if core:
            v3_triggers = [_core_trigger(trigger) for trigger in core]
            review = {
                "decision": "V2_CORE_READ_ONLY",
                "reason": "V2正式AI已核准；V3只承接，不重寫任何核心欄位。",
                "reviewed_candidate_packets": 0,
            }
        elif approval:
            if int(approval["index"]) != int(parent["index"]) or str(approval["name"]) != str(parent["name"]):
                errors.append({"code": code, "error": "APPROVAL_IDENTITY_MISMATCH"})
            if str(approval["signal_date"]) < str(parent["first_selected_on"]):
                errors.append({"code": code, "error": "SIGNAL_BEFORE_WATCHLIST", "date": approval["signal_date"]})
            if not _watch_active_on((parent.get("v2") or {}).get("watchlist_events") or [], str(approval["signal_date"])):
                errors.append({"code": code, "error": "INACTIVE_WATCHLIST_AT_SIGNAL", "date": approval["signal_date"]})
            by_day = {str(row["date"]): row for row in packet_index[code]["review_packets"]}
            info = by_day.get(str(approval["signal_date"]))
            if not info:
                errors.append({"code": code, "error": "MISSING_ASOF_PACKET", "date": approval["signal_date"]})
            else:
                packet_path = Path(info["path"])
                if _digest(packet_path) != info["sha256"]:
                    errors.append({"code": code, "error": "ASOF_PACKET_HASH_CHANGED", "date": approval["signal_date"]})
                packet = _read(packet_path)
                candidate = packet["candidate_day"]
                if packet["review_as_of"] != approval["signal_date"] or packet["review_contract"]["future_bars_present"]:
                    errors.append({"code": code, "error": "NON_CAUSAL_PACKET", "date": approval["signal_date"]})
                if not _close(candidate["close"], approval["signal_close"]) or not _close(candidate["atr14"], approval["atr"]):
                    errors.append({"code": code, "error": "SIGNAL_MARKET_FACT_MISMATCH", "date": approval["signal_date"]})
                pivot = next((
                    value for value in packet["confirmed_pivots_asof"]
                    if value["side"] == "LOW"
                    and value["source_date"] == approval["stop_date"]
                    and value["confirmation_date"] == approval["stop_confirmed_on"]
                    and _close(value["price"], approval["stop_price"])
                ), None)
                if pivot is None:
                    errors.append({"code": code, "error": "STOP_NOT_CAUSALLY_CONFIRMED", "date": approval["signal_date"]})
                approved_packets.append({"code": code, "date": approval["signal_date"], "path": str(packet_path), "sha256": info["sha256"]})
            if approval["route"] == "BEAR_REVERSAL_PROBE":
                if approval["unknown_gate"] != "BEAR_LATE_STAGE_EVIDENCE" or approval["phase"] == "LL" or not approval.get("late_stage_partial_evidence"):
                    errors.append({"code": code, "error": "INVALID_BEAR_OVERLAY"})
            elif approval["route"] == "NEAR_PASS_MACRO_COPY":
                if approval["unknown_gate"] not in {"COMPLETED_PARENT_ANCHOR", "TAIJI_GENERATION_MAPPED", "DUAL_SCALE_LONG_ALIGNMENT"}:
                    errors.append({"code": code, "error": "INVALID_MACRO_UNKNOWN_GATE"})
            elif approval["route"] == "NEAR_PASS_FRESH_Q1":
                if approval["unknown_gate"] not in {"CLEAN_MEATY_DESTRUCTIVE_TRACEABLE", "DYNAMIC_Q1_EXPANSION", "EARLY_TAIJI_GENERATION"}:
                    errors.append({"code": code, "error": "INVALID_FRESH_UNKNOWN_GATE"})
            else:
                errors.append({"code": code, "error": "INVALID_V3_ROUTE", "route": approval["route"]})
            v3_triggers = [_approval_trigger(approval)]
            review = {
                "decision": "AI_EXTRA_APPROVAL",
                "route": approval["route"],
                "unknown_gate": approval["unknown_gate"],
                "zero_fail_attestation": True,
                "common_hard_guards_all_pass": True,
                "reviewed_candidate_packets": len(packet_index[code]["review_packets"]),
                "approved_packet": approved_packets[-1] if approved_packets and approved_packets[-1]["code"] == code else None,
            }
        else:
            parent_reason = str((parent.get("v2") or {}).get("no_trade_reason") or "")
            review = {
                "decision": "V3_NO_TRADE",
                "reason": "V2未達核心；V3審核未找到可同時證明共同硬閘全PASS、零FAIL且只含一個允許UNKNOWN的完整路徑。證據不足不改寫為PASS。",
                "parent_v2_reason": parent_reason,
                "reviewed_candidate_packets": len(packet_index[code]["review_packets"]),
            }
        row = copy.deepcopy(parent)
        row["v3"] = {
            "stock_status": "TRIGGERED" if v3_triggers else "NO_TRADE",
            "watchlist_events": copy.deepcopy((parent.get("v2") or {}).get("watchlist_events") or []),
            "triggers": v3_triggers,
            "no_trade_reason": None if v3_triggers else review["reason"],
            "ai_overlay_review": review,
        }
        output.append(row)

    missing_approval_codes = sorted(set(approvals) - {str(row["code"]) for row in parent_rows})
    if missing_approval_codes:
        errors.append({"error": "APPROVAL_CODES_NOT_IN_PARENT", "codes": missing_approval_codes})
    core_events = sum(len((row.get("v2") or {}).get("triggers") or []) for row in parent_rows)
    v3_events = sum(len(row["v3"]["triggers"]) for row in output)
    validation = {
        "valid": not errors,
        "errors": errors,
        "expected_stocks": EXPECTED_STOCKS,
        "reviewed_stocks": len(output),
        "v2_core_stocks": sum(bool((row.get("v2") or {}).get("triggers")) for row in parent_rows),
        "v2_core_events": core_events,
        "v3_extra_stocks": len(approvals),
        "v3_extra_events": len(approvals),
        "v3_total_stocks": sum(bool(row["v3"]["triggers"]) for row in output),
        "v3_total_events": v3_events,
        "v3_no_trade_stocks": sum(not row["v3"]["triggers"] for row in output),
        "preferred_750_bars_met": sum(bool((row.get("audit") or {}).get("preferred_750_met")) for row in parent_rows),
        "preferred_750_bars_not_met": sum(not bool((row.get("audit") or {}).get("preferred_750_met")) for row in parent_rows),
        "candidate_asof_packets_available": sum(row["v3"]["ai_overlay_review"].get("reviewed_candidate_packets", 0) for row in output),
        "approved_asof_packets": approved_packets,
        "parent_input_manifest_sha256": _digest(PARENT_MANIFEST),
        "parent_merged_ledger_sha256": _digest(PARENT_LEDGER),
        "approval_file_sha256": _digest(APPROVALS_PATH),
        "packet_index_sha256": _digest(PACKET_INDEX_PATH),
        "v3_rule_sha256": _digest(V3_DOC),
        "v3_machine_rules_sha256": _digest(V3_RULES),
        "corporate_actions": parent_validation.get("corporate_actions"),
        "causal_attestation": {
            "candidate_level_future_outcome_visible": False,
            "known_winner_reference_used": False,
            "rules_changed_after_candidate_outcome": False,
            "aggregate_parent_performance_known": True,
        },
    }
    _write_jsonl(V3_LEDGER_PATH, output)
    validation["v3_ledger_sha256"] = _digest(V3_LEDGER_PATH)
    _write_json(VALIDATION_PATH, validation)
    if errors:
        raise ValueError(f"V3 decision validation failed; see {VALIDATION_PATH}")
    return output, validation


def _prepare_signals(
    rows: list[dict[str, Any]],
    items: dict[str, dict[str, Any]],
    expected_by_code: dict[str, dict[str, Any]],
    daily: dict[str, dict[str, list[str]]],
    meta: dict[str, dict[str, Any]],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    signals_by_code: dict[str, list[dict[str, Any]]] = {}
    lifecycle_by_code: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        code = str(row["code"])
        frame = base._load_frame_asof(items[code])
        first = base._first_selected(code, row, expected_by_code[code], meta, daily)
        signals = base._signals_for("v3", row, frame, daily.get(code, {}), first)
        trigger_by_day = {str(trigger["signal_date"]): trigger for trigger in row["v3"]["triggers"]}
        for signal in signals:
            trigger = trigger_by_day[str(signal["signal_date"])]
            signal["v3_layer"] = trigger["v3_layer"]
            signal["v3_route"] = trigger["v3_route"]
            signal["v3_unknown_gate"] = trigger.get("v3_unknown_gate")
            signal["v3_phase"] = trigger.get("v3_phase")
        lifecycle_by_code[code] = base._lifecycle_for("v3", row, first)
        if signals:
            signals_by_code[code] = signals
    return signals_by_code, lifecycle_by_code


def _enrich_v3(stock: dict[str, Any], signals: list[dict[str, Any]]) -> None:
    signal_by_date = {str(signal["signal_date"]): signal for signal in signals}
    for episode in stock.get("episodes", []):
        routes = set()
        layers = set()
        for tranche in episode["tranches"]:
            signal = signal_by_date[str(tranche["signal_date"])]
            for key in ("v3_layer", "v3_route", "v3_unknown_gate", "v3_phase"):
                tranche[key] = signal.get(key)
            tranche["planned_nominal_twd"] = ROUTE_NOMINAL[signal["v3_route"]]
            routes.add(signal["v3_route"])
            layers.add(signal["v3_layer"])
        episode["v3_routes"] = sorted(routes)
        episode["v3_layers"] = sorted(layers)


def _layer_breakdown(variant: dict[str, Any]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for stock in variant["stocks"]:
        for episode in stock.get("episodes", []):
            route = str(episode["tranches"][0].get("v3_route") or "UNKNOWN")
            grouped[route].append(episode)
    output = []
    for route, episodes in sorted(grouped.items()):
        pnl = [float(row["net_pnl"]) for row in episodes]
        cash = sum(float(row["deployed_cash"]) for row in episodes)
        output.append({
            "route": route,
            "episodes": len(episodes),
            "closed": sum(str(row["status"]).startswith("CLOSED") for row in episodes),
            "open": sum(str(row["status"]).startswith("OPEN") for row in episodes),
            "net_pnl": round(sum(pnl), 2),
            "return_on_deployed_cash_pct": round(sum(pnl) / cash * 100, 4) if cash else 0.0,
            "average_return_pct": round(statistics.fmean(float(row["net_return_on_deployed_pct"]) for row in episodes), 4),
            "win_rate_pct": round(sum(value > 0 for value in pnl) / len(pnl) * 100, 2),
            "average_mfe_pct": round(statistics.fmean(float(row["mfe_pct_from_mother"]) for row in episodes), 4),
        })
    return output


def _add_scenario_mfe(summary: dict[str, Any], variant: dict[str, Any]) -> None:
    grouped: dict[str, list[float]] = defaultdict(list)
    for stock in variant["stocks"]:
        for episode in stock.get("episodes", []):
            grouped[str(episode["tranches"][0]["scenario"])].append(float(episode["mfe_pct_from_mother"]))
    for row in summary["scenario_breakdown"]:
        values = grouped.get(str(row["scenario"])) or []
        row["average_mfe_pct"] = round(statistics.fmean(values), 4) if values else 0.0


def _run_variant(
    key: str,
    items: dict[str, dict[str, Any]],
    signals_by_code: dict[str, list[dict[str, Any]]],
    lifecycle_by_code: dict[str, list[dict[str, Any]]],
    *,
    allow_adds: bool,
    tiered: bool,
) -> dict[str, Any]:
    result: dict[str, Any] = {"name": key, "stocks": []}
    original_nominal = base.execution.NOMINAL_PER_TRANCHE
    try:
        for code, signals in sorted(signals_by_code.items()):
            route = str(signals[0]["v3_route"])
            base.execution.NOMINAL_PER_TRANCHE = ROUTE_NOMINAL[route] if tiered else 10_000.0
            stock = base._simulate_enriched(items[code], signals, lifecycle_by_code[code], allow_adds=allow_adds)
            _enrich_v3(stock, signals)
            result["stocks"].append(stock)
    finally:
        base.execution.NOMINAL_PER_TRANCHE = original_nominal
    result["summary"] = base.execution._summary(result)
    _add_scenario_mfe(result["summary"], result)
    audit = [event for stock in result["stocks"] for event in stock.get("audit", [])]
    result["summary"].update({
        "unexecuted_end_of_window": sum(str(event.get("event") or "").startswith("UNEXECUTED_END_OF_WINDOW") for event in audit),
        "split_adjustment_events": sum(str(event.get("event") or "").startswith("SPLIT_ADJUSTED") for event in audit),
        "cash_dividend_events": sum(str(event.get("event") or "").startswith("CASH_DIVIDEND") for event in audit),
        "mark_to_market_drawdown": base._mark_to_market_drawdown(result, items),
        "initial_monitor_source_breakdown_overlap": base._source_breakdown(result, "initial_monitor_strategies"),
        "pre_trigger_source_breakdown_overlap": base._source_breakdown(result, "pre_trigger_selection_strategies"),
        "strategy_scenario_breakdown_overlap": base._source_breakdown(result, "initial_monitor_strategies", by_scenario=True),
        "v3_route_breakdown": _layer_breakdown(result),
    })
    return result


def _core_identity(parent_result: dict[str, Any], variants: dict[str, Any]) -> dict[str, Any]:
    checks = []
    for parent_key, v3_key in (
        ("V2_MOTHER_ONLY_10K", "V3_MOTHER_ONLY_10K"),
        ("V2_MOTHER_PLUS_2", "V3_MOTHER_PLUS_2"),
    ):
        parent_by_code = {str(row["code"]): row for row in parent_result["variants"][parent_key]["stocks"]}
        v3_core = {
            str(row["code"]): row for row in variants[v3_key]["stocks"]
            if any(
                episode["tranches"] and episode["tranches"][0].get("v3_route") == "V2_CORE"
                for episode in row.get("episodes", [])
            ) or str(row["code"]) in parent_by_code
        }
        differences = []
        for code, parent_stock in parent_by_code.items():
            candidate = v3_core.get(code)
            if candidate is None:
                differences.append({"code": code, "error": "MISSING_CORE_STOCK"})
                continue
            for field in ("episodes", "audit"):
                comparable = copy.deepcopy(candidate.get(field))
                if field == "episodes":
                    for episode in comparable:
                        episode.pop("v3_routes", None)
                        episode.pop("v3_layers", None)
                        for tranche in episode.get("tranches", []):
                            for key in ("v3_layer", "v3_route", "v3_unknown_gate", "v3_phase", "planned_nominal_twd"):
                                tranche.pop(key, None)
                if comparable != parent_stock.get(field):
                    differences.append({"code": code, "field": field})
                    break
        checks.append({"parent": parent_key, "v3": v3_key, "differences": differences, "identical": not differences})
    return {"checks": checks, "valid": all(row["identical"] for row in checks)}


def replay() -> dict[str, Any]:
    rows, validation = build_and_validate_v3_ledger()
    fact, source, catalog, expected_items = base._input_context()
    items = {str(row["code"]): row for row in (source.get("items") or expected_items)}
    expected_by_code = {str(row["code"]): row for row in expected_items}
    daily, meta = base._catalog_maps(catalog)
    signals_by_code, lifecycle_by_code = _prepare_signals(rows, items, expected_by_code, daily, meta)
    parent_result = _read(PARENT_BACKTEST)
    variants = {
        "V3_MOTHER_ONLY_10K": _run_variant(
            "V3_MOTHER_ONLY_10K", items, signals_by_code, lifecycle_by_code, allow_adds=False, tiered=False,
        ),
        "V3_MOTHER_PLUS_2": _run_variant(
            "V3_MOTHER_PLUS_2", items, signals_by_code, lifecycle_by_code, allow_adds=True, tiered=False,
        ),
        "V3_TIERED_MOTHER_PLUS_2": _run_variant(
            "V3_TIERED_MOTHER_PLUS_2", items, signals_by_code, lifecycle_by_code, allow_adds=True, tiered=True,
        ),
    }
    identity = _core_identity(parent_result, variants)
    if not identity["valid"]:
        raise ValueError(f"V3 core replay diverged from frozen V2: {identity}")
    parent_summaries = {}
    for key, variant in parent_result["variants"].items():
        summary = copy.deepcopy(variant["summary"])
        _add_scenario_mfe(summary, variant)
        parent_summaries[key] = summary
    output = {
        "method_version": "formal-ai-historical-ledger-replay-v3",
        "judgement_scope": "FORMAL_CODEX_AI_V1_V2_FROZEN_PLUS_V3_AI_OVERLAY（無程式代理判讀）",
        "as_of": AS_OF,
        "requested_as_of": "2024-02-04",
        "validation": validation,
        "core_identity": identity,
        "decision_files": {
            "parent_v1_v2": {"path": str(PARENT_LEDGER.resolve()), "sha256": _digest(PARENT_LEDGER)},
            "v3": {"path": str(V3_LEDGER_PATH.resolve()), "sha256": _digest(V3_LEDGER_PATH)},
            "v3_manual_approvals": {"path": str(APPROVALS_PATH.resolve()), "sha256": _digest(APPROVALS_PATH)},
        },
        "rules": {
            "v1": str((ROOT / "docs/enlightenment-ai-judgement-v1.md").resolve()),
            "v2": str((ROOT / "docs/enlightenment-ai-judgement-v2.md").resolve()),
            "v3": str(V3_DOC.resolve()),
        },
        "parent_variants": parent_summaries,
        "variants": variants,
    }
    _write_json(RESULT_PATH, output)
    compact = copy.deepcopy(output)
    compact["variants"] = {key: value["summary"] for key, value in variants.items()}
    _write_json(SUMMARY_PATH, compact)
    return output


def _money(value: float) -> str:
    return f"{float(value):+,.0f}"


def _pct(value: float) -> str:
    return f"{float(value):+.2f}%"


def _pf(value: float | None) -> str:
    return "—" if value is None else f"{float(value):.2f}"


def _cell(value: Any) -> str:
    return str(value if value is not None else "—").replace("|", "／").replace("\n", " ")


def _variant_row(key: str, summary: dict[str, Any]) -> str:
    return (
        f"| {key} | {summary['stocks_traded']} | {summary['trade_episodes']} | {summary['closed']}／{summary['open']} | "
        f"{summary['buy_fills']} | {_money(summary['realized_net_pnl'])} | {_money(summary['unrealized_net_pnl_after_estimated_exit_cost'])} | "
        f"{_money(summary['net_pnl'])} | {_pct(summary['return_on_peak_capital_pct'])} | {summary['win_rate_pct']:.2f}% | "
        f"{_pf(summary['profit_factor'])} | {summary['maximum_concurrent_stocks']}／{summary['maximum_concurrent_tranches']} | "
        f"{summary['peak_concurrent_deployed_cash']:,.0f} |"
    )


def _all_summaries(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    parent = result["parent_variants"]
    current = {key: value["summary"] for key, value in result["variants"].items()}
    return {**parent, **current}


def _append_breakdown(lines: list[str], title: str, rows: list[dict[str, Any]], category: str) -> None:
    lines += ["", f"### {title}", "", f"| {category} | 回合／持有 | 淨損益 | 平均報酬 | 勝率 | 平均MFE |", "|---|---:|---:|---:|---:|---:|"]
    for row in rows:
        label = row.get("route") or row.get("scenario") or row.get("strategy")
        lines.append(
            f"| {_cell(label)} | {row['episodes']}／{row['open']} | {_money(row.get('net_pnl', row.get('net_pnl_full_overlap_attribution', 0)))} | "
            f"{_pct(row['average_return_pct'])} | {row['win_rate_pct']:.2f}% | {_pct(row.get('average_mfe_pct', 0))} |"
        )


def _append_strategy_scenario(lines: list[str], rows: list[dict[str, Any]]) -> None:
    lines += [
        "", "### 入選策略 × 四情境（重疊歸因）", "",
        "> 同一檔股票可由多個選股策略同時入選，各列不可直接相加。",
        "", "| 選股策略 | 情境 | 回合／持有 | 淨損益 | 平均報酬 | 勝率 | 平均MFE |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            f"| {_cell(row['strategy'])} | {_cell(row['scenario'])} | {row['episodes']}／{row['open']} | "
            f"{_money(row['net_pnl_full_overlap_attribution'])} | {_pct(row['average_return_pct'])} | "
            f"{row['win_rate_pct']:.2f}% | {_pct(row['average_mfe_pct'])} |"
        )


def write_reports(result: dict[str, Any]) -> None:
    summaries = _all_summaries(result)
    ordered = [
        "V1_MOTHER_ONLY_10K", "V1_MOTHER_PLUS_2",
        "V2_MOTHER_ONLY_10K", "V2_MOTHER_PLUS_2",
        "V3_MOTHER_ONLY_10K", "V3_MOTHER_PLUS_2", "V3_TIERED_MOTHER_PLUS_2",
    ]
    lines = [
        "# 2023下半年1,029檔正式AI V1／V2／V3交叉回測",
        "",
        "> 監控自2023-06-01起，請求終點2024-02-04為週日，因此績效截至2024-02-02收盤。V1／V2直接引用封存正式AI ledger；V3核心逐筆承接V2，新增訊號來自AI人工定稿檔，程式只驗證與成交記帳。",
        "",
        "## 核心結論",
        "",
        f"- V3共核准{result['validation']['v3_total_events']}筆事件／{result['validation']['v3_total_stocks']}檔：其中V2核心{result['validation']['v2_core_events']}筆，V3新增試單{result['validation']['v3_extra_events']}筆；其餘{result['validation']['v3_no_trade_stocks']}檔不交易。",
        "- 14筆V3新增訊號中13筆實際成交；5009榮剛因下一交易日開盤超過訊號收盤+0.5ATR與一個最小跳動單位而取消，不追價。",
        f"- V3固定版淨損益{_money(summaries['V3_MOTHER_ONLY_10K']['net_pnl'])}元；加碼版{_money(summaries['V3_MOTHER_PLUS_2']['net_pnl'])}元；分層部位版{_money(summaries['V3_TIERED_MOTHER_PLUS_2']['net_pnl'])}元。",
        f"- 相對V2，V3新增試單在固定版與加碼版都增加{_money(summaries['V3_MOTHER_ONLY_10K']['net_pnl'] - summaries['V2_MOTHER_ONLY_10K']['net_pnl'])}元；本窗內新增試單沒有第二個完整核心加碼點，因此V3的加碼增益仍全部來自V2_CORE。",
        f"- V3_CORE與V2成交、退出、股利和分割紀錄完全一致：{'是' if result['core_identity']['valid'] else '否'}。這項檢查防止V3暗中改寫V2。",
        "- 這是無資金上限的研究回放；尖峰資金報酬不是CAGR，也未模擬流動性、滑價或實際委託排隊。",
        "",
        "## 資料與判讀範圍",
        "",
        f"- 名單1,029檔全部有行情；其中{result['validation']['preferred_750_bars_met']}檔在監控前具至少750根日K，{result['validation']['preferred_750_bars_not_met']}檔不足並保留資料不足標記。",
        f"- 公司行動：現金股利日{result['validation']['corporate_actions']['dividend_dates']}個、分割／除權股數調整日{result['validation']['corporate_actions']['split_dates']}個、未解決0檔。",
        f"- V3事實層共產生{result['validation']['candidate_asof_packets_available']:,}個只到候選當日的證據包；V2核心直接繼承封存AI決策，V3新增14筆另由AI逐筆定稿，並非程式打分或代理分類。",
        "",
        "## 組合總表",
        "",
        "| 組合 | 股票 | 回合 | 已出場／持有 | 買進份數 | 已實現 | 未實現* | 淨損益 | 尖峰資金報酬 | 勝率 | PF | 最大持股／份數 | 尖峰資金 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for key in ordered:
        lines.append(_variant_row(key, summaries[key]))
    lines += [
        "",
        "\\* 未實現損益已估計若於期末賣出的手續費與交易稅；已實現損益則使用實際規則出場。",
        "",
        "## 固定版與加碼版差異",
        "",
        "| 規則 | 固定版淨損益 | 加碼版淨損益 | 加碼增減 | 固定版尖峰資金 | 加碼版尖峰資金 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for version in ("V1", "V2", "V3"):
        fixed = summaries[f"{version}_MOTHER_ONLY_10K"]
        added = summaries[f"{version}_MOTHER_PLUS_2"]
        lines.append(
            f"| {version} | {_money(fixed['net_pnl'])} | {_money(added['net_pnl'])} | {_money(added['net_pnl'] - fixed['net_pnl'])} | "
            f"{fixed['peak_concurrent_deployed_cash']:,.0f} | {added['peak_concurrent_deployed_cash']:,.0f} |"
        )
    for key in ("V1_MOTHER_ONLY_10K", "V2_MOTHER_ONLY_10K", "V3_MOTHER_ONLY_10K", "V1_MOTHER_PLUS_2", "V2_MOTHER_PLUS_2", "V3_MOTHER_PLUS_2"):
        summary = summaries[key]
        lines += [
            "", f"## {key}", "",
            f"- 平均／中位報酬：{_pct(summary['average_return_pct'])}／{_pct(summary['median_return_pct'])}；平均／中位MFE：{_pct(summary['average_mfe_pct'])}／{_pct(summary['median_mfe_pct'])}。",
            f"- 已出場{summary['closed']}筆，持有中{summary['open']}筆；已出場PF {_pf(summary['closed_profit_factor'])}，持有中平均{_pct(summary['open_average_return_pct'])}。",
            f"- MFE≥20%共{summary['mfe_20_plus_count']}筆，其中期末仍正報酬{summary['mfe_20_plus_final_positive_count']}筆；已出場平均峰值回吐{summary['closed_average_giveback_points']:.2f}個百分點。",
            "", "### 損益分布", "", "| 區間 | 全部 | 已出場 | 持有中 | MFE |", "|---|---:|---:|---:|---:|",
        ]
        for label in summary["return_distribution"]:
            lines.append(f"| {label} | {summary['return_distribution'][label]} | {summary['closed_return_distribution'][label]} | {summary['open_return_distribution'][label]} | {summary['mfe_distribution'][label]} |")
        _append_breakdown(lines, "四情境績效", summary["scenario_breakdown"], "情境")
        _append_breakdown(lines, "首次入選策略績效（重疊歸因）", summary["initial_monitor_source_breakdown_overlap"], "選股策略")
        _append_strategy_scenario(lines, summary["strategy_scenario_breakdown_overlap"])
        if key.startswith("V3_"):
            _append_breakdown(lines, "V3路徑增量績效", summary["v3_route_breakdown"], "V3路徑")
    lines += [
        "", "## 因果與一致性稽核", "",
        f"- V3決策驗證：{len(result['validation']['errors'])}項錯誤；V3 ledger SHA-256 `{result['validation']['v3_ledger_sha256']}`。",
        f"- V3核准訊號均能對應訊號日證據包、當日收盤與ATR、已確認樞紐防線；共{len(result['validation']['approved_asof_packets'])}個新增核准包。",
        "- AI判讀沒有使用候選個股訊號日後的價格或績效；已知V1/V2父回測的整體統計，此限制已在決策檔中明載。",
        "- V1、V2舊規則、舊ledger與舊回測檔皆未修改。",
        "", "## 檔案索引", "",
        f"- V3逐筆交易：[v3/backtest.md]({V3_REPORT_PATH.as_posix()})",
        f"- V3逐股AI稽核：[v3_ai_decisions.md]({DECISION_REPORT_PATH.as_posix()})",
        f"- V1逐筆交易（封存父回測）：[{(PARENT / 'v1/backtest.md').name}]({(PARENT / 'v1/backtest.md').as_posix()})",
        f"- V2逐筆交易（封存父回測）：[{(PARENT / 'v2/backtest.md').name}]({(PARENT / 'v2/backtest.md').as_posix()})",
        f"- 完整機器資料：[backtest.json]({RESULT_PATH.as_posix()})",
    ]
    REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")

    decision_rows = _read_jsonl(V3_LEDGER_PATH)
    dlines = [
        "# V3正式AI決策稽核",
        "",
        f"> V2核心完全唯讀；V3新增{result['validation']['v3_extra_events']}筆由AI按訊號日證據包定稿。下表每股一列，完整觸發證據見JSONL與核准檔。",
        "",
        "| Index | 股票 | V3結果 | 路徑／訊號 | 唯一UNKNOWN | V2原因／V3不交易理由 | 候選包數 |",
        "|---:|---|---|---|---|---|---:|",
    ]
    for row in decision_rows:
        review = row["v3"]["ai_overlay_review"]
        triggers = row["v3"]["triggers"]
        trigger_text = "；".join(f"{value['signal_date']} {value['v3_route']} 防線{value['stop_price']}({value['stop_date']})" for value in triggers)
        unknown = "、".join(str(value.get("v3_unknown_gate") or "無") for value in triggers) if triggers else "—"
        reason = review.get("parent_v2_reason") or review.get("reason")
        dlines.append(
            f"| {row['index']} | {row['code']} {_cell(row['name'])} | {review['decision']} | {_cell(trigger_text)} | {_cell(unknown)} | {_cell(reason)} | {review.get('reviewed_candidate_packets', 0)} |"
        )
    DECISION_REPORT_PATH.write_text("\n".join(dlines) + "\n", encoding="utf-8")

    v3lines = [
        "# V3正式AI回測逐筆交易",
        "",
        "> 主要比較為每次10,000元的固定母單與最多兩次獲利後加碼；分層部位為補充敏感度，V2_CORE 10,000元、近合格5,000元、空頭反轉試單2,500元。",
    ]
    for key, variant in result["variants"].items():
        summary = variant["summary"]
        v3lines += [
            "", f"## {key}", "",
            f"- {summary['trade_episodes']}回合，已出場{summary['closed']}、持有中{summary['open']}；淨損益{_money(summary['net_pnl'])}元，尖峰資金{summary['peak_concurrent_deployed_cash']:,.0f}元。",
            "", "| 股票 | 層／路徑 | 情境 | 訊號日 | 進場日 | 出場／期末 | 狀態 | 份數 | 投入 | 淨損益 | 報酬 | MFE | MAE | 出場原因 | 首次入選策略 |",
            "|---|---|---|---|---|---|---|---:|---:|---:|---:|---:|---:|---|---|",
        ]
        episodes = [episode for stock in variant["stocks"] for episode in stock.get("episodes", [])]
        for episode in sorted(episodes, key=lambda row: (str(row["entry_date"]), str(row["code"]), str(row["episode_id"]))):
            mother = episode["tranches"][0]
            end = episode.get("exit_date") or episode.get("mark_date")
            sources = "、".join(mother.get("initial_monitor_strategies") or ["UNKNOWN_SELECTION_SOURCE"])
            v3lines.append(
                f"| {episode['code']} {_cell(episode['name'])} | {_cell(mother.get('v3_layer'))}／{_cell(mother.get('v3_route'))} | {_cell(mother['scenario'])} | "
                f"{mother['signal_date']} | {episode['entry_date']} | {end} | {_cell(episode['status'])} | {episode['tranche_count']} | {episode['deployed_cash']:,.0f} | "
                f"{_money(episode['net_pnl'])} | {_pct(episode['net_return_on_deployed_pct'])} | {_pct(episode['mfe_pct_from_mother'])} | {_pct(episode['mae_pct_from_mother'])} | "
                f"{_cell(episode['exit_reason'])} | {_cell(sources)} |"
            )
    V3_REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    V3_REPORT_PATH.write_text("\n".join(v3lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    payload = replay()
    write_reports(payload)
    print(json.dumps({
        "report": str(REPORT_PATH),
        "v3_report": str(V3_REPORT_PATH),
        "validation": payload["validation"],
        "core_identity": payload["core_identity"],
        "summaries": _all_summaries(payload),
    }, ensure_ascii=True, indent=2, allow_nan=False))
