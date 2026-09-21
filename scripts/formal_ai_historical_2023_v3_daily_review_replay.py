"""Validate, lock, unseal, and replay the every-monitored-day Codex V3 review."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from scripts import formal_ai_historical_2023_replay as base
from scripts import formal_ai_historical_2023_v3_replay as v3exec


ROOT = Path(__file__).resolve().parents[1]
PARENT = ROOT / "reports/course_backtest/2024-02-02/historical_scan_2023h2_formal_ai_v1_v2"
SOURCE = ROOT / "reports/course_backtest/2024-02-02/historical_scan_2023h2_formal_ai_v3_daily_scan"
REVIEW = SOURCE / "ai_daily_review_v2"
RUN = ROOT / "reports/course_backtest/2024-02-02/historical_scan_2023h2_formal_ai_v1_v2_v3_daily_codex"
BATCH_MANIFEST = REVIEW / "batch_manifest.json"
REVIEWS = REVIEW / "reviews"
IDENTITY_MAP = SOURCE / "sealed_identity_map.json"
PARENT_LEDGER = PARENT / "ai_decisions_merged.jsonl"
PARENT_BACKTEST = PARENT / "backtest.json"
ANON_DAILY_LEDGER = RUN / "anonymous_daily_ai_decisions.jsonl"
PREPERFORMANCE_LOCK = RUN / "preperformance_decision_lock.json"
V3_LEDGER = RUN / "v3_ai_decisions.jsonl"
VALIDATION = RUN / "decision_validation.json"
RESULT = RUN / "backtest.json"
SUMMARY = RUN / "backtest_summary.json"
REPORT = RUN / "comparison.md"
DECISION_REPORT = RUN / "v3_ai_decisions.md"
TRADE_REPORTS = {
    "V3_DAILY_MOTHER_ONLY_10K": RUN / "v3_daily_fixed_trades.md",
    "V3_DAILY_MOTHER_PLUS_2": RUN / "v3_daily_add_trades.md",
}
EXPECTED_STOCKS = 1_029
EXPECTED_STOCK_DAYS = 151_804
EXPECTED_PARENT_SHA = "8b239b5f2d4ec8f973df49bf7360013f21ad44bad7fd0d80ab359ff00ac387b4"

ROUTE_SCENARIO = {
    "NEAR_PASS_MACRO_COPY": "MACRO_COPY_RESONANCE",
    "NEAR_PASS_FRESH_Q1": "FRESH_Q1_EXPANSION",
    "BEAR_REVERSAL_PROBE": "BEAR_REVERSAL_LEFT_RIGHT",
}
V2_SCENARIOS = {
    "MATURE_TREND_PULLBACK",
    "MACRO_COPY_RESONANCE",
    "FRESH_Q1_EXPANSION",
    "BEAR_REVERSAL_LEFT_RIGHT",
}
ALLOWED_UNKNOWN = {
    "NEAR_PASS_MACRO_COPY": {"COMPLETED_PARENT_ANCHOR", "TAIJI_GENERATION_MAPPED", "DUAL_SCALE_LONG_ALIGNMENT"},
    "NEAR_PASS_FRESH_Q1": {"CLEAN_MEATY_DESTRUCTIVE_TRACEABLE", "DYNAMIC_Q1_EXPANSION", "EARLY_TAIJI_GENERATION"},
    "BEAR_REVERSAL_PROBE": {"BEAR_LATE_STAGE_EVIDENCE"},
}
ALLOWED_PHASES = {
    "V2_CORE": {"LR", "RL", "RR", "DIRECT_TO_RIGHT"},
    "NEAR_PASS_MACRO_COPY": {"RR", "DIRECT_TO_RIGHT"},
    "NEAR_PASS_FRESH_Q1": {"RR", "DIRECT_TO_RIGHT"},
    "BEAR_REVERSAL_PROBE": {"LR", "RL", "RR", "DIRECT_TO_RIGHT"},
}


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n" for row in rows), encoding="utf-8")


def _close(left: Any, right: Any) -> bool:
    try:
        return math.isclose(float(left), float(right), rel_tol=0.0, abs_tol=1e-4)
    except (TypeError, ValueError):
        return False


def _trigger_path(route: str, phase: str) -> str:
    if route == "V2_CORE":
        return "DAILY_V2_CORE_STRUCTURE_BREAK"
    if route == "NEAR_PASS_MACRO_COPY":
        return "CORRECTION_SMALL_REANCHOR_BREAK"
    if route == "NEAR_PASS_FRESH_Q1":
        return "FRESH_ANCHOR_DESTRUCTIVE_BREAK"
    return "BEAR_DIRECT_RIGHT_DUAL_BREAK" if phase == "DIRECT_TO_RIGHT" else f"BEAR_{phase}_SMALL_DEFENSE_BREAK"


def _decode(row: list[Any]) -> dict[str, Any]:
    """Decode the compact transport array without adding any judgement."""
    pivots = row[17] if len(row) > 17 else (row[12] if len(row) > 12 else [])
    stop = row[13] if len(row) > 13 else None
    return {
        "review_id": row[0],
        "anonymous_stock_id": row[1],
        "review_as_of": row[2],
        "stock_day_ordinal": row[3],
        "selection_today": row[4],
        "trigger_completed": bool(row[6]),
        "market": {"close": row[8][3] if len(row) > 8 else None},
        "causal_stop_candidate": None if stop is None else {
            "source_date": stop[0], "confirmation_date": stop[1], "price": stop[2],
        },
        "confirmed_pivots_asof": [
            {
                "scale": "LARGE" if item[0] == "L" else "SMALL",
                "side": "HIGH" if item[1] == "H" else "LOW",
                "source_date": item[2], "confirmation_date": item[3], "price": item[4],
            }
            for item in pivots
        ],
    }


def validate_and_expand(*, require_complete: bool = True) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    manifest = _read(BATCH_MANIFEST)
    errors: list[dict[str, Any]] = []
    expanded: list[dict[str, Any]] = []
    review_hashes: list[dict[str, Any]] = []
    event_counts: Counter[str] = Counter()
    route_counts: Counter[str] = Counter()
    active: dict[str, bool] = defaultdict(lambda: True)
    last_trigger_structure: dict[str, tuple[Any, ...]] = {}

    for item in sorted(manifest["files"], key=lambda row: (int(row["round"]), int(row["shard"]))):
        batch_path = Path(item["path"])
        if _sha(batch_path) != str(item["sha256"]):
            errors.append({"batch": batch_path.name, "error": "BATCH_HASH_CHANGED"})
        batch_rows = [_decode(row) for row in _jsonl(batch_path)]
        by_id = {str(row["review_id"]): row for row in batch_rows}
        review_path = REVIEWS / f"shard_{int(item['shard'])}" / f"{batch_path.stem}.json"
        if not review_path.exists():
            if require_complete:
                errors.append({"batch": batch_path.name, "error": "MISSING_REVIEW"})
            continue
        review = _read(review_path)
        expected = {
            "review_protocol": "FORMAL_CODEX_AI_V3_EVERY_DAY_REVIEW_V2",
            "reviewer": "CODEX_SAME_MODEL",
            "shard": int(item["shard"]),
            "round": int(item["round"]),
            "date": str(item["date"]),
            "input_sha256": _sha(batch_path),
            "input_rows": len(batch_rows),
            "identity_visible": False,
            "future_performance_visible": False,
            "same_stock_future_day_visible": False,
            "reviewed_every_row": True,
            "default_decision_for_unlisted_review_ids": "WATCHING_NO_EVENT",
        }
        for key, value in expected.items():
            if review.get(key) != value:
                errors.append({"batch": batch_path.name, "error": f"REVIEW_HEADER_MISMATCH:{key}"})
        if str(Path(str(review.get("input_path") or "")).resolve()) != str(batch_path.resolve()):
            errors.append({"batch": batch_path.name, "error": "REVIEW_HEADER_MISMATCH:input_path"})
        local: dict[str, dict[str, Any]] = {}
        for decision in review.get("decisions") or []:
            review_id = str(decision.get("review_id") or "")
            if review_id not in by_id or review_id in local:
                errors.append({"batch": batch_path.name, "review_id": review_id, "error": "DUPLICATE_OR_FOREIGN_ID"})
                continue
            local[review_id] = decision
        for review_id, row in by_id.items():
            anonymous_id = str(row["anonymous_stock_id"])
            selection_today = list(row.get("selection_today") or [])
            if not active[anonymous_id] and selection_today:
                active[anonymous_id] = True
                last_trigger_structure.pop(anonymous_id, None)
            decision = local.get(review_id)
            if decision is None:
                state = "WATCHING_NO_EVENT" if active[anonymous_id] else "REMOVED_CARRY"
                output = {
                    "review_id": review_id, "anonymous_stock_id": anonymous_id,
                    "review_as_of": row["review_as_of"], "state": state,
                    "route": "NO_TRADE", "scenario": None, "phase": None, "unknown_gate": None,
                    "reason": "AI逐列審核後套用該批明示的預設無事件決策。",
                    "selection_today": selection_today,
                }
            else:
                state = str(decision.get("state") or "")
                route = str(decision.get("route") or "")
                reason = str(decision.get("reason") or "").strip()
                if state not in {"ARMED", "TRIGGERED", "REMOVED"}:
                    errors.append({"review_id": review_id, "error": "INVALID_EVENT_STATE"})
                if not reason:
                    errors.append({"review_id": review_id, "error": "MISSING_REASON"})
                if state == "REMOVED":
                    if route != "NO_TRADE" or any(decision.get(key) is not None for key in ("scenario", "phase", "unknown_gate", "stop_date", "stop_confirmed_on", "stop_price")):
                        errors.append({"review_id": review_id, "error": "INVALID_REMOVAL_FIELDS"})
                    active[anonymous_id] = False
                else:
                    if not active[anonymous_id]:
                        errors.append({"review_id": review_id, "error": "EVENT_WHILE_INACTIVE"})
                    scenario = str(decision.get("scenario") or "")
                    phase = str(decision.get("phase") or "")
                    unknown = decision.get("unknown_gate")
                    route_valid = route == "V2_CORE" or route in ROUTE_SCENARIO
                    scenario_valid = scenario in V2_SCENARIOS if route == "V2_CORE" else scenario == ROUTE_SCENARIO.get(route)
                    if not route_valid or not scenario_valid or phase not in ALLOWED_PHASES.get(route, set()):
                        errors.append({"review_id": review_id, "error": "INVALID_ROUTE_SCENARIO_PHASE"})
                    if route == "V2_CORE":
                        if unknown is not None:
                            errors.append({"review_id": review_id, "error": "V2_CORE_HAS_UNKNOWN"})
                    elif str(unknown or "") not in ALLOWED_UNKNOWN.get(route, set()):
                        errors.append({"review_id": review_id, "error": "INVALID_UNIQUE_UNKNOWN"})
                    if state == "TRIGGERED":
                        stop = row.get("causal_stop_candidate")
                        if not row.get("trigger_completed"):
                            errors.append({"review_id": review_id, "error": "TRIGGER_EVENT_NOT_COMPLETED"})
                        if not stop or not all(_close(decision.get(key), stop.get(source)) for key, source in (("stop_price", "price"),)) or str(decision.get("stop_date")) != str((stop or {}).get("source_date")) or str(decision.get("stop_confirmed_on")) != str((stop or {}).get("confirmation_date")):
                            errors.append({"review_id": review_id, "error": "NON_CAUSAL_STOP"})
                        elif float(decision["stop_price"]) >= float(row["market"]["close"]):
                            errors.append({"review_id": review_id, "error": "STOP_NOT_BELOW_CLOSE"})
                        pivots = row.get("confirmed_pivots_asof") or []
                        key = (
                            route,
                            next((x.get("source_date") for x in reversed(pivots) if x["scale"] == "SMALL" and x["side"] == "HIGH"), None),
                            next((x.get("source_date") for x in reversed(pivots) if x["scale"] == "SMALL" and x["side"] == "LOW"), None),
                            next((x.get("source_date") for x in reversed(pivots) if x["scale"] == "LARGE" and x["side"] == "HIGH"), None),
                            next((x.get("source_date") for x in reversed(pivots) if x["scale"] == "LARGE" and x["side"] == "LOW"), None),
                        )
                        if last_trigger_structure.get(anonymous_id) == key:
                            errors.append({"review_id": review_id, "error": "DUPLICATE_TRIGGER_SAME_STRUCTURE"})
                        last_trigger_structure[anonymous_id] = key
                output = {
                    **copy.deepcopy(decision), "anonymous_stock_id": anonymous_id,
                    "review_as_of": row["review_as_of"], "selection_today": selection_today,
                }
                output.setdefault("state", state or "INVALID")
                output.setdefault("route", route or "INVALID")
            event_counts[str(output["state"])] += 1
            if output.get("route") == "V2_CORE" or output.get("route") in ROUTE_SCENARIO:
                route_counts[str(output["route"])] += int(output["state"] == "TRIGGERED")
            expanded.append(output)
        review_hashes.append({"path": str(review_path.resolve()), "sha256": _sha(review_path), "rows": len(batch_rows), "events": len(local)})

    if manifest.get("stock_days") != EXPECTED_STOCK_DAYS or len(expanded) != (EXPECTED_STOCK_DAYS if require_complete else len(expanded)):
        errors.append({"error": "STOCK_DAY_COUNT_MISMATCH", "manifest": manifest.get("stock_days"), "expanded": len(expanded)})
    if manifest.get("stocks") != EXPECTED_STOCKS or manifest.get("outcome_excluded_codes"):
        errors.append({"error": "UNIVERSE_OR_EXCLUSION_MISMATCH"})
    if len({row["review_id"] for row in expanded}) != len(expanded):
        errors.append({"error": "DUPLICATE_DAILY_REVIEW_ID"})
    audit = {
        "valid": not errors, "errors": errors,
        "expected_stocks": EXPECTED_STOCKS, "stock_days": len(expanded),
        "expected_batches": len(manifest["files"]), "reviewed_batches": len(review_hashes),
        "outcome_excluded_codes": [], "event_counts": dict(event_counts), "trigger_route_counts": dict(route_counts),
        "batch_manifest": {"path": str(BATCH_MANIFEST.resolve()), "sha256": _sha(BATCH_MANIFEST)},
        "review_files": review_hashes,
        "decision_boundary": "Codex AI reviewed every row; code validated, expanded explicit batch defaults, executed and accounted only",
    }
    if require_complete and errors:
        raise ValueError(f"daily V3 review validation failed with {len(errors)} errors")
    return expanded, audit


def lock_anonymous_decisions(expanded: list[dict[str, Any]], audit: dict[str, Any]) -> dict[str, Any]:
    _write_jsonl(ANON_DAILY_LEDGER, expanded)
    lock = {
        "status": "LOCKED_BEFORE_IDENTITY_UNSEAL_AND_PERFORMANCE",
        "identity_visible_when_authored": False,
        "future_performance_visible_when_authored": False,
        "outcome_excluded_codes": [],
        "stock_days": len(expanded), "reviewed_batches": audit["reviewed_batches"],
        "event_counts": audit["event_counts"], "trigger_route_counts": audit["trigger_route_counts"],
        "anonymous_daily_ledger": {"path": str(ANON_DAILY_LEDGER.resolve()), "sha256": _sha(ANON_DAILY_LEDGER)},
        "batch_manifest": audit["batch_manifest"], "review_files": audit["review_files"],
        "v3_rule": {"path": str((ROOT / 'docs/enlightenment-ai-judgement-v3.md').resolve()), "sha256": _sha(ROOT / 'docs/enlightenment-ai-judgement-v3.md')},
    }
    _write_json(PREPERFORMANCE_LOCK, lock)
    return lock


def build_unsealed_ledger(expanded: list[dict[str, Any]], audit: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    identity = _read(IDENTITY_MAP)
    code_by_anonymous = {str(row["anonymous_id"]): str(row["code"]) for row in identity["mapping"]}
    parent_rows = _jsonl(PARENT_LEDGER)
    parent_by_code = {str(row["code"]): row for row in parent_rows}
    daily_by_code: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in expanded:
        daily_by_code[code_by_anonymous[str(row["anonymous_stock_id"])]].append(row)
    output = []
    for code, parent in parent_by_code.items():
        daily = sorted(daily_by_code.get(code, []), key=lambda row: str(row["review_as_of"]))
        triggers = []
        lifecycle = [{"date": str(parent["first_selected_on"]), "event": "WATCHING（監控中）"}]
        was_active = True
        for decision in daily:
            day = str(decision["review_as_of"])
            if not was_active and decision.get("selection_today"):
                lifecycle.append({"date": day, "event": "RESELECTED（重新入選監控）"})
                was_active = True
            state = str(decision["state"])
            if state == "REMOVED" and was_active:
                lifecycle.append({"date": day, "event": "CAMPAIGN_INVALIDATED（大結構交易週期失效）", "reason": decision["reason"]})
                was_active = False
            if state != "TRIGGERED" or not was_active:
                continue
            route = str(decision["route"])
            triggers.append({
                "signal_date": day,
                "scenario": str(decision["scenario"]),
                "trigger_path": _trigger_path(route, str(decision["phase"])),
                "episode_or_add_candidate": "MOTHER_OR_V2_VALID_ADD" if route == "V2_CORE" else "MOTHER_OR_REENTRY",
                "stop_date": str(decision["stop_date"]), "stop_price": float(decision["stop_price"]),
                "left_right": str(decision["phase"]), "evidence": [str(decision["reason"])],
                "invalidation": {"scope": "TRADE_EPISODE", "price": float(decision["stop_price"]), "rule": "收盤失守因果控制低，本次episode失效；大結構未失效可等待獨立再觸發。"},
                "v3_layer": "V2_CORE" if route == "V2_CORE" else ("BEAR_REVERSAL_PROBE" if route == "BEAR_REVERSAL_PROBE" else "NEAR_PASS"),
                "v3_route": route, "v3_unknown_gate": decision.get("unknown_gate"), "v3_phase": decision["phase"],
                "v3_ai_decision_source": str(ANON_DAILY_LEDGER.resolve()), "v3_review_id": decision["review_id"],
            })
        row = copy.deepcopy(parent)
        row["v3"] = {
            "stock_status": "TRIGGERED" if triggers else "NO_TRADE",
            "watchlist_events": lifecycle, "triggers": triggers,
            "no_trade_reason": None if triggers else "V3逐日Codex AI審核未核准交易事件。",
            "ai_overlay_review": {"decision": "FULL_DAILY_CODEX_AI_REVIEW", "reviewed_stock_days": len(daily)},
        }
        output.append(row)
    _write_jsonl(V3_LEDGER, output)
    validation = {
        **audit, "valid": True, "unsealed_stocks": len(output),
        "v3_trigger_events": sum(len(row["v3"]["triggers"]) for row in output),
        "v3_trigger_stocks": sum(bool(row["v3"]["triggers"]) for row in output),
        "v3_ledger_sha256": _sha(V3_LEDGER), "identity_map_sha256": _sha(IDENTITY_MAP),
        "parent_ledger_sha256": _sha(PARENT_LEDGER), "preperformance_lock_sha256": _sha(PREPERFORMANCE_LOCK),
    }
    _write_json(VALIDATION, validation)
    return output, validation


def replay(rows: list[dict[str, Any]], validation: dict[str, Any]) -> dict[str, Any]:
    v3exec.RUN = RUN
    v3exec.V3_LEDGER_PATH = V3_LEDGER
    v3exec.VALIDATION_PATH = VALIDATION
    v3exec.RESULT_PATH = RESULT
    v3exec.SUMMARY_PATH = SUMMARY
    _, source, catalog, expected_items = base._input_context()
    items = {str(row["code"]): row for row in (source.get("items") or expected_items)}
    expected_by_code = {str(row["code"]): row for row in expected_items}
    daily, meta = base._catalog_maps(catalog)
    signals, lifecycle = v3exec._prepare_signals(rows, items, expected_by_code, daily, meta)
    variants = {
        "V3_DAILY_MOTHER_ONLY_10K": v3exec._run_variant("V3_DAILY_MOTHER_ONLY_10K", items, signals, lifecycle, allow_adds=False, tiered=False),
        "V3_DAILY_MOTHER_PLUS_2": v3exec._run_variant("V3_DAILY_MOTHER_PLUS_2", items, signals, lifecycle, allow_adds=True, tiered=False),
    }
    parent = _read(PARENT_BACKTEST)
    result = {
        "method_version": "formal-codex-ai-v3-every-monitored-day-replay-v1",
        "as_of": v3exec.AS_OF, "validation": validation,
        "parent_variants": {key: value["summary"] for key, value in parent["variants"].items()},
        "variants": variants,
    }
    _write_json(RESULT, result)
    compact = copy.deepcopy(result)
    compact["variants"] = {key: value["summary"] for key, value in variants.items()}
    _write_json(SUMMARY, compact)
    return result


def _money(value: Any) -> str:
    return f"{float(value):+,.0f}"


def _pct(value: Any) -> str:
    return f"{float(value):+.2f}%"


def _pf(value: Any) -> str:
    return "N/A" if value is None else f"{float(value):.2f}"


def _episodes(variant: dict[str, Any]) -> list[dict[str, Any]]:
    return [episode for stock in variant.get("stocks") or [] for episode in stock.get("episodes") or []]


def _write_trade_report(key: str, variant: dict[str, Any]) -> None:
    summary = variant["summary"]
    lines = [
        f"# {key} 逐筆交易", "",
        f"> 共 {summary['trade_episodes']:,} 個 episode；持有中以 2024-02-02 收盤估值，未實現損益已估計賣出成本。", "",
        "| 股票 | Episode | 狀態 | 進場日 | 份數 | 情境／V3路徑 | 投入 | 出場／估值日 | 淨損益 | 報酬 | MFE | MAE | 持有日 | 出場原因 |",
        "|---|---|---|---|---:|---|---:|---|---:|---:|---:|---:|---:|---|",
    ]
    for ep in sorted(_episodes(variant), key=lambda row: (str(row["entry_date"]), str(row["code"]), str(row["episode_id"]))):
        scenario = str((ep.get("tranches") or [{}])[0].get("scenario") or "")
        routes = "、".join(ep.get("v3_routes") or [])
        status = str(ep.get("status") or "")
        close_date = ep.get("exit_date") or ep.get("mark_date") or ""
        reason = str(ep.get("exit_reason") or "").replace("|", "／")
        lines.append(
            f"| {ep['code']} {ep.get('name','')} | {ep['episode_id']} | {status} | {ep['entry_date']} | {ep['tranche_count']} | "
            f"{scenario}／{routes} | {ep['deployed_cash']:,.0f} | {close_date} | {_money(ep['net_pnl'])} | "
            f"{_pct(ep['net_return_on_deployed_pct'])} | {_pct(ep['mfe_pct_from_mother'])} | {_pct(ep['mae_pct_from_mother'])} | "
            f"{ep['holding_sessions']} | {reason} |"
        )
    TRADE_REPORTS[key].write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_reports(result: dict[str, Any]) -> None:
    parent = result["parent_variants"]
    variants = {key: value["summary"] for key, value in result["variants"].items()}
    all_summaries = {**parent, **variants}
    ordered = ["V1_MOTHER_ONLY_10K", "V1_MOTHER_PLUS_2", "V2_MOTHER_ONLY_10K", "V2_MOTHER_PLUS_2", "V3_DAILY_MOTHER_ONLY_10K", "V3_DAILY_MOTHER_PLUS_2"]
    lines = [
        "# 2023下半年1,029檔V3全監控日Codex AI回測", "",
        "> 1,029檔、151,804股票日全數匿名逐日審核；沒有結果排除名單。AI決策在股票身份還原與績效開封前完成雜湊鎖定。", "",
        "## 判讀完整性", "",
        f"- 批次：{result['validation']['reviewed_batches']}／{result['validation']['expected_batches']}；股票日：{result['validation']['stock_days']:,}。",
        f"- V3觸發：{result['validation']['v3_trigger_events']:,}筆、{result['validation']['v3_trigger_stocks']:,}檔；路徑：{result['validation']['trigger_route_counts']}。",
        "- 結果排除：0檔；包含原先標成結果已知的五檔。", "",
        "## 六組比較", "",
        "| 組合 | 股票 | 回合 | 已出場／持有 | 買進份數 | 已實現 | 未實現* | 淨損益 | 尖峰資金報酬 | 勝率 | PF | 最大持股／份數 | 尖峰資金 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for key in ordered:
        s = all_summaries[key]
        lines.append(
            f"| {key} | {s['stocks_traded']} | {s['trade_episodes']} | {s['closed']}／{s['open']} | {s['buy_fills']} | "
            f"{_money(s['realized_net_pnl'])} | {_money(s['unrealized_net_pnl_after_estimated_exit_cost'])} | {_money(s['net_pnl'])} | "
            f"{_pct(s['return_on_peak_capital_pct'])} | {s['win_rate_pct']:.2f}% | {_pf(s['profit_factor'])} | "
            f"{s['maximum_concurrent_stocks']}／{s['maximum_concurrent_tranches']} | {s['peak_concurrent_deployed_cash']:,.0f} |"
        )
    lines += ["", "\\* 未實現損益已估計期末賣出成本。"]
    for key, variant in result["variants"].items():
        s = variant["summary"]
        lines += [
            "", f"## {key}", "",
            f"- 平均／中位報酬：{_pct(s['average_return_pct'])}／{_pct(s['median_return_pct'])}；平均／中位MFE：{_pct(s['average_mfe_pct'])}／{_pct(s['median_mfe_pct'])}。",
            "", "### 四情境", "", "| 情境 | 回合／持有 | 淨損益 | 平均報酬 | 勝率 | 平均MFE |", "|---|---:|---:|---:|---:|---:|",
        ]
        for row in s["scenario_breakdown"]:
            lines.append(f"| {row['scenario']} | {row['episodes']}／{row['open']} | {_money(row['net_pnl'])} | {_pct(row['average_return_pct'])} | {row['win_rate_pct']:.2f}% | {_pct(row.get('average_mfe_pct', 0))} |")
        lines += ["", "### V3路徑", "", "| 路徑 | 回合／持有 | 淨損益 | 平均報酬 | 勝率 | 平均MFE |", "|---|---:|---:|---:|---:|---:|"]
        for row in s["v3_route_breakdown"]:
            lines.append(f"| {row['route']} | {row['episodes']}／{row['open']} | {_money(row['net_pnl'])} | {_pct(row['average_return_pct'])} | {row['win_rate_pct']:.2f}% | {_pct(row['average_mfe_pct'])} |")
        lines += ["", "### 選股策略（重疊歸因）", "", "| 策略 | 回合／持有 | 淨損益 | 平均報酬 | 勝率 |", "|---|---:|---:|---:|---:|"]
        for row in s["initial_monitor_source_breakdown_overlap"]:
            lines.append(f"| {row['strategy']} | {row['episodes']}／{row['open']} | {_money(row['net_pnl_full_overlap_attribution'])} | {_pct(row['average_return_pct'])} | {row['win_rate_pct']:.2f}% |")
        lines += ["", "### 報酬分布", "", "| 區間 | 全部 | 已出場 | 持有中 |", "|---|---:|---:|---:|"]
        buckets = list(s["return_distribution"].keys())
        for bucket in buckets:
            lines.append(f"| {bucket} | {s['return_distribution'].get(bucket, 0)} | {s['closed_return_distribution'].get(bucket, 0)} | {s['open_return_distribution'].get(bucket, 0)} |")
        dd = s["mark_to_market_drawdown"]
        lines += [
            "", "### 集中度與風險", "",
            f"- 最大回撤：{dd['max_drawdown_twd']:,.0f} 元（{dd['max_drawdown_pct']:.2f}%），{dd['peak_date']} 至 {dd['trough_date']}。",
            f"- 前1／前3大獲利集中度：{s['top1_profit_concentration_pct']:.2f}%／{s['top3_profit_concentration_pct']:.2f}%；扣除最大贏家後淨損益 {_money(s['net_without_top1'])} 元。",
            f"- MFE ≥20%：{s['mfe_20_plus_count']} 筆，其中期末仍正報酬 {s['mfe_20_plus_final_positive_count']} 筆。",
            "", "### 選股策略 × 情境（重疊歸因）", "",
            "| 策略 | 情境 | 回合／持有 | 淨損益 | 平均報酬 | 平均MFE | 勝率 |", "|---|---|---:|---:|---:|---:|---:|",
        ]
        for row in s["strategy_scenario_breakdown_overlap"]:
            lines.append(
                f"| {row['strategy']} | {row['scenario']} | {row['episodes']}／{row['open']} | "
                f"{_money(row['net_pnl_full_overlap_attribution'])} | {_pct(row['average_return_pct'])} | "
                f"{_pct(row['average_mfe_pct'])} | {row['win_rate_pct']:.2f}% |"
            )
        lines += ["", f"[本版本逐筆交易]({TRADE_REPORTS[key].as_posix()})"]
    lines += [
        "", "## 稽核檔案", "",
        f"- 績效開封前鎖：[{PREPERFORMANCE_LOCK.name}]({PREPERFORMANCE_LOCK.as_posix()})",
        f"- 匿名逐日決策：[{ANON_DAILY_LEDGER.name}]({ANON_DAILY_LEDGER.as_posix()})",
        f"- 完整V3決策：[{V3_LEDGER.name}]({V3_LEDGER.as_posix()})",
        f"- 完整JSON：[{RESULT.name}]({RESULT.as_posix()})",
    ]
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")

    dlines = ["# V3逐日AI觸發決策", "", "| 股票 | 訊號日 | 路徑 | 情境 | 防線 | 理由 |", "|---|---|---|---|---:|---|"]
    for row in _jsonl(V3_LEDGER):
        for trigger in row["v3"]["triggers"]:
            reason = str((trigger.get("evidence") or [""])[0]).replace("|", "／").replace("\n", " ")
            dlines.append(f"| {row['code']} {row.get('name','')} | {trigger['signal_date']} | {trigger['v3_route']} | {trigger['scenario']} | {trigger['stop_price']} | {reason} |")
    DECISION_REPORT.write_text("\n".join(dlines) + "\n", encoding="utf-8")
    for key, variant in result["variants"].items():
        _write_trade_report(key, variant)


def main() -> dict[str, Any]:
    expanded, audit = validate_and_expand(require_complete=True)
    lock_anonymous_decisions(expanded, audit)
    rows, validation = build_unsealed_ledger(expanded, audit)
    payload = replay(rows, validation)
    write_reports(payload)
    return payload


if __name__ == "__main__":
    payload = main()
    print(json.dumps({"report": str(REPORT), "validation": payload["validation"], "summaries": {key: value["summary"] for key, value in payload["variants"].items()}}, ensure_ascii=True, indent=2, allow_nan=False))
