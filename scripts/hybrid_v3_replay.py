"""Lock, unseal, and replay the formal hybrid V3 semantic ledger."""
from __future__ import annotations

import hashlib
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterator


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import formal_ai_historical_2023_replay as base  # noqa: E402
from scripts.hybrid_v3_policy import reduce_v3, validate_semantic  # noqa: E402


PARENT = ROOT / "reports/course_backtest/2024-02-02/historical_scan_2023h2_formal_ai_v1_v2"
SOURCE = ROOT / "reports/course_backtest/2024-02-02/historical_scan_2023h2_formal_ai_v3_daily_scan"
RUN = SOURCE / "hybrid_monitoring_v1"
PACKETS = RUN / "anonymous_policy_boundary_packets.jsonl"
PACKET_MANIFEST = RUN / "policy_boundary_manifest.json"
SEMANTICS = RUN / "semantic/full_ai_semantic_ledger.jsonl"
SEMANTIC_MANIFEST = SEMANTICS.with_suffix(".manifest.json")
IDENTITIES = SOURCE / "sealed_identity_map.json"
RESELECTIONS = RUN / "reselection_candidates.jsonl"
PARENT_MANIFEST = PARENT / "input_manifest.json"
PROTOCOL = ROOT / "config/hybrid_monitoring_protocol_v1.json"
PROMPT = ROOT / "config/hybrid_semantic_prompt_v1.md"
SCHEMA = ROOT / "config/hybrid_common_structure_v1.schema.json"
DECISION_LEDGER = RUN / "v3_program_decision_ledger.jsonl"
STOCK_LEDGER = RUN / "v3_stock_lifecycle_and_triggers.jsonl"
VALIDATION = RUN / "v3_replay_validation.json"
RESULT = RUN / "v3_backtest.json"
REPORT = RUN / "v3_backtest.md"
FIXED_REPORT = RUN / "fixed_trades.md"
ADD2_REPORT = RUN / "add2_trades.md"
STOCK_REPORT = RUN / "v3_stock_lifecycle_and_triggers.md"
DECISION_REPORT = RUN / "v3_policy_boundary_decisions.md"
FOCUS_REPORT = RUN / "v3_focus_stock_audit.md"
FREEZE = RUN / "protocol_freeze_manifest.json"
EXECUTION_FREEZE = RUN / "execution_freeze_manifest.json"

AS_OF = "2024-02-02"
EXPECTED_STOCKS = 1029
EXPECTED_STOCK_DAYS = 151804

base.AS_OF = AS_OF
base.MONITOR_FLOOR = "2023-06-01"
base.SELECTION_START = "2023-06-01"
base.SELECTION_END = "2023-12-31"
base.EXPECTED_STOCKS = EXPECTED_STOCKS
base.RUN = PARENT
base.FACT_MANIFEST = PARENT / "input_manifest.json"
base.VALIDATION_PATH = PARENT / "decision_validation.json"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n" for row in rows), encoding="utf-8")


def _assert_freeze() -> dict[str, Any]:
    freeze = _read(FREEZE)
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
    freeze = _read(EXECUTION_FREEZE)
    for item in freeze.get("files") or []:
        path = ROOT / item["relative_path"]
        if not path.exists() or _sha(path) != item["sha256"]:
            raise ValueError(f"execution-frozen file changed: {item['relative_path']}")
    for item in freeze.get("artifacts") or []:
        path = Path(item["absolute_path"])
        if not path.exists() or _sha(path) != item["sha256"]:
            raise ValueError(f"execution-frozen artifact changed: {path}")
    return freeze


def _ref_values(packet: dict[str, Any], ref: str) -> dict[str, Any]:
    return next(item["values"] for item in packet["evidence"] if item["ref"] == ref)


def _trigger(packet: dict[str, Any], semantic: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    stop_ref = str(policy["stop_ref"])
    stop = _ref_values(packet, stop_ref)
    anchors = [row for row in semantic["anchors"] if row["role"] in {"PRIMARY", "PARENT"}]
    anchor = anchors[0] if anchors else semantic["anchors"][0]
    refs = {item["ref"]: item for item in packet["evidence"]}
    start_values = refs[anchor["start_ref"]]["values"]
    end_values = refs[anchor["end_ref"]]["values"] if anchor.get("end_ref") else {}
    pivots = [item for item in packet["evidence"] if item["kind"] == "CONFIRMED_PIVOT" and item["values"]["scale"] == "SMALL"]
    highs = [item for item in pivots if item["values"]["side"] == "HIGH"]
    lows = [item for item in pivots if item["values"]["side"] == "LOW"]
    latest_high = highs[-1]["values"] if highs else None
    latest_low = lows[-1]["values"] if lows else None
    broken_control_refs = []
    for scale, event_type in (("SMALL", "SMALL_CONTROL_BREAK"), ("LARGE", "LARGE_CONTROL_BREAK")):
        if event_type not in (packet.get("event_types") or []):
            continue
        candidates = [
            item for item in packet["evidence"]
            if item["kind"] == "CONFIRMED_PIVOT"
            and item["values"]["scale"] == scale and item["values"]["side"] == "HIGH"
        ]
        if candidates:
            chosen = max(candidates, key=lambda item: (str(item["values"]["confirmation_date"]), str(item["values"]["source_date"])))
            broken_control_refs.append(chosen["ref"])
    if not broken_control_refs:
        raise ValueError(f"trigger boundary has no causal broken-control ref: {packet['review_id']}")
    gates = {row["gate_id"]: {"result": row["result"], "evidence": row["evidence_refs"], "reason": row["reason"]} for row in semantic["semantic_gates"]}
    return {
        "signal_date": packet["as_of"],
        "scenario": semantic["primary_scenario"],
        "trigger_path": policy["route"],
        "episode_or_add_candidate": "MOTHER_OR_V2_VALID_ADD",
        "macro_anchor": {
            "start_date": start_values.get("source_date") or start_values.get("low_date") or start_values.get("start") or refs[anchor["start_ref"]]["date"],
            "end_date": end_values.get("source_date") or end_values.get("high_date") or end_values.get("end") or (refs[anchor["end_ref"]]["date"] if anchor.get("end_ref") else packet["as_of"]),
            "direction": anchor["direction"], "status": anchor["status"],
        },
        "small_structure": {
            "confirmed_pivot_high": None if latest_high is None else {"date": latest_high["source_date"], "price": latest_high["price"], "confirmed_on": latest_high["confirmation_date"]},
            "confirmed_pivot_low": None if latest_low is None else {"date": latest_low["source_date"], "price": latest_low["price"], "confirmed_on": latest_low["confirmation_date"]},
        },
        "taiji_generation": semantic["taiji"]["generation"],
        "large_quadrant": semantic["scales"]["large"]["quadrant"],
        "small_quadrant": semantic["scales"]["small"]["quadrant"],
        "large_dow": semantic["scales"]["large"]["dow_state"],
        "small_dow": semantic["scales"]["small"]["dow_state"],
        "stop_date": stop["source_date"], "stop_price": float(stop["price"]),
        "left_right": semantic["left_right_phase"],
        "required_gates": gates,
        "evidence": [{"gate_id": row["gate_id"], "refs": row["evidence_refs"]} for row in semantic["semantic_gates"]],
        "invalidation": {"scope": "TRADE_EPISODE", "price": float(stop["price"]), "source_date": stop["source_date"], "confirmation_date": stop["confirmation_date"], "rule": "收盤失守程式選定的因果小級低點，本episode失效。"},
        "v3_layer": "V2_CORE" if policy["route"] == "V2_CORE" else "BEAR_REVERSAL_PROBE" if policy["route"] == "BEAR_REVERSAL_PROBE" else "NEAR_PASS",
        "v3_route": policy["route"], "v3_unknown_gate": policy.get("unknown_gate"),
        "semantic_review_id": packet["review_id"],
        "independent_structure_key": "+".join(sorted(broken_control_refs)),
        "broken_control_refs": sorted(broken_control_refs),
    }


def build_program_decisions() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    freeze = _assert_freeze()
    execution_freeze = _assert_execution_freeze()
    if execution_freeze.get("model") != freeze.get("model") or execution_freeze.get("reasoning_effort") != freeze.get("reasoning_effort"):
        raise ValueError("semantic and execution freeze model settings differ")
    semantic_manifest = _read(SEMANTIC_MANIFEST)
    boundary_manifest = _read(PACKET_MANIFEST)
    if not semantic_manifest.get("complete"):
        raise ValueError("formal semantic ledger is incomplete")
    if semantic_manifest["source_sha256"] != _sha(PACKETS):
        raise ValueError("semantic source hash differs from policy-boundary packets")
    if semantic_manifest["output_sha256"] != _sha(SEMANTICS):
        raise ValueError("semantic ledger changed after lock")
    if semantic_manifest.get("model") != freeze["model"] or semantic_manifest.get("reasoning_effort") != freeze["reasoning_effort"]:
        raise ValueError("semantic model setting differs from frozen protocol")
    for path, key in ((PROTOCOL, "protocol_sha256"), (PROMPT, "prompt_sha256"), (SCHEMA, "schema_sha256")):
        if semantic_manifest[key] != _sha(path):
            raise ValueError(f"frozen {path.name} changed after semantic review")
    semantic_locked_sha = _sha(SEMANTICS)  # identity map is not opened before this line

    identities = _read(IDENTITIES)
    anon_to_identity = {row["anonymous_id"]: row for row in identities["mapping"]}
    source_items = {str(row["code"]): row for row in (_read(PARENT_MANIFEST).get("items") or [])}
    if set(source_items) != {str(row["code"]) for row in identities["mapping"]}:
        raise ValueError("sealed identities and source manifest stock universe differ")
    reselections: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in _jsonl(RESELECTIONS):
        reselections[str(row["anonymous_stock_id"])].append(row)

    state: dict[str, dict[str, Any]] = defaultdict(lambda: {"active": True, "removed_on": None, "next_reselection_index": 0, "campaign": 1})
    lifecycles: dict[str, list[dict[str, Any]]] = defaultdict(list)
    triggers: dict[str, list[dict[str, Any]]] = defaultdict(list)
    decisions: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    permission_counts: Counter[str] = Counter()
    route_counts: Counter[str] = Counter()

    packet_iter = _jsonl(PACKETS)
    semantic_iter = _jsonl(SEMANTICS)
    paired = 0
    for packet, semantic in zip(packet_iter, semantic_iter):
        paired += 1
        if packet["review_id"] != semantic.get("review_id"):
            errors.append({"review_id": packet["review_id"], "error": "SEMANTIC_ORDER_OR_ID_MISMATCH"})
            continue
        validation_errors = validate_semantic(packet, semantic)
        if validation_errors:
            errors.append({"review_id": packet["review_id"], "error": "INVALID_SEMANTIC", "details": validation_errors})
            continue
        anonymous_id = str(packet["anonymous_stock_id"])
        identity = anon_to_identity[anonymous_id]
        code = str(identity["code"])
        current = state[anonymous_id]
        if not lifecycles[code]:
            first = str(source_items[code]["monitor_on"])
            lifecycles[code].append({"date": first, "event": "WATCHING（加入監控）", "campaign": 1})

        if not current["active"]:
            candidates = reselections.get(anonymous_id) or []
            next_index = int(current["next_reselection_index"])
            while next_index < len(candidates) and str(candidates[next_index]["as_of"]) <= str(current["removed_on"]):
                next_index += 1
            if next_index < len(candidates) and str(candidates[next_index]["as_of"]) <= str(packet["as_of"]):
                day = str(candidates[next_index]["as_of"])
                current.update({"active": True, "removed_on": None, "next_reselection_index": next_index + 1, "campaign": int(current["campaign"]) + 1})
                lifecycles[code].append({"date": day, "event": "RESELECTED（重新入選監控）", "campaign": current["campaign"], "sources": candidates[next_index]["selected_today"]})

        policy_packet = json.loads(json.dumps(packet))
        policy_packet["selection_asof"]["selected_before_or_on_day"] = bool(current["active"])
        policy = reduce_v3(policy_packet, semantic)
        permission_counts[policy["permission"]] += 1
        route_counts[policy["route"]] += 1
        decision_row = {
            "review_id": packet["review_id"], "anonymous_stock_id": anonymous_id,
            "as_of": packet["as_of"], "event_types": packet.get("event_types") or [],
            "permission": policy["permission"], "route": policy["route"],
            "reason_codes": policy.get("reason_codes") or [],
            "primary_scenario": semantic["primary_scenario"],
            "left_right_phase": semantic["left_right_phase"],
            "stage_location": semantic["stage_location"],
            "semantic_flags": semantic["semantic_flags"],
            "gate_results": {gate["gate_id"]: gate["result"] for gate in semantic["semantic_gates"]},
            "semantic_sha256": hashlib.sha256(json.dumps(semantic, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
        }
        decisions.append(decision_row)
        if policy["permission"] == "REMOVE" and current["active"]:
            current.update({"active": False, "removed_on": packet["as_of"]})
            lifecycles[code].append({"date": packet["as_of"], "event": "CAMPAIGN_INVALIDATED（大結構交易週期失效）", "campaign": current["campaign"], "semantic_review_id": packet["review_id"]})
        elif policy["permission"] == "TRADE" and current["active"]:
            triggers[code].append(_trigger(packet, semantic, policy))

    if next(packet_iter, None) is not None or next(semantic_iter, None) is not None:
        errors.append({"error": "PACKET_SEMANTIC_ROW_COUNT_MISMATCH"})
    if paired != int(boundary_manifest["policy_boundary_rows"]):
        errors.append({"error": "BOUNDARY_ROW_COUNT_MISMATCH", "paired": paired, "expected": boundary_manifest["policy_boundary_rows"]})

    stock_rows = []
    for identity in sorted(identities["mapping"], key=lambda row: int(row["index"])):
        code = str(identity["code"])
        source_item = source_items[code]
        stock_rows.append({
            "index": int(identity["index"]), "code": code, "name": identity.get("name") or source_item.get("name"),
            "first_selected_on": source_item["monitor_on"],
            "v3": {
                "watchlist_events": sorted(lifecycles[code], key=lambda row: (row["date"], 0 if row["event"].startswith("WATCHING") else 1)),
                "triggers": sorted(triggers[code], key=lambda row: row["signal_date"]),
                "no_trade_reason": None if triggers[code] else "混合V3政策未核准交易。",
            },
        })
    _write_jsonl(DECISION_LEDGER, decisions)
    _write_jsonl(STOCK_LEDGER, stock_rows)
    validation = {
        "valid": not errors, "errors": errors, "semantic_locked_before_identity_unseal": True,
        "semantic_ledger_sha256": semantic_locked_sha, "identity_map_sha256": _sha(IDENTITIES),
        "policy_boundary_rows": paired, "program_decision_rows": len(decisions),
        "stocks": len(stock_rows), "trigger_events": sum(len(values) for values in triggers.values()),
        "trigger_stocks": sum(bool(values) for values in triggers.values()),
        "permission_counts": dict(permission_counts), "route_counts": dict(route_counts),
        "program_decision_ledger": {"path": str(DECISION_LEDGER.resolve()), "sha256": _sha(DECISION_LEDGER)},
        "stock_lifecycle_and_triggers": {"path": str(STOCK_LEDGER.resolve()), "sha256": _sha(STOCK_LEDGER)},
        "protocol_sha256": _sha(PROTOCOL), "prompt_sha256": _sha(PROMPT), "schema_sha256": _sha(SCHEMA),
    }
    _write_json(VALIDATION, validation)
    if errors:
        raise ValueError(f"hybrid V3 validation failed with {len(errors)} errors")
    return stock_rows, validation


def _prepare_signals(rows: list[dict[str, Any]]):
    _, source, catalog, expected_items = base._input_context()
    items = {str(row["code"]): row for row in (source.get("items") or expected_items)}
    expected = {str(row["code"]): row for row in expected_items}
    daily, meta = base._catalog_maps(catalog)
    signals_by_code = {}
    lifecycle_by_code = {}
    for row in rows:
        code = str(row["code"])
        frame = base._load_frame_asof(items[code])
        first = base._first_selected(code, row, expected[code], meta, daily)
        lifecycle = base._lifecycle_for("v3", row, first)
        signals = base._signals_for("v3", row, frame, daily.get(code, {}), first)
        if signals:
            trigger_by_day = {value["signal_date"]: value for value in row["v3"]["triggers"]}
            for signal in signals:
                trigger = trigger_by_day[signal["signal_date"]]
                signal.update({key: trigger.get(key) for key in ("v3_layer", "v3_route", "v3_unknown_gate", "semantic_review_id", "independent_structure_key", "broken_control_refs")})
            signals_by_code[code] = signals
        lifecycle_by_code[code] = lifecycle
    return items, signals_by_code, lifecycle_by_code


def _run_variant(name: str, items: dict[str, dict[str, Any]], signals_by_code: dict[str, list[dict[str, Any]]], lifecycle_by_code: dict[str, list[dict[str, Any]]], allow_adds: bool) -> dict[str, Any]:
    result = {"name": name, "stocks": []}
    for code, signals in sorted(signals_by_code.items()):
        stock = base._simulate_enriched(items[code], signals, lifecycle_by_code[code], allow_adds=allow_adds)
        signal_by_date = {row["signal_date"]: row for row in signals}
        for episode in stock.get("episodes", []):
            for tranche in episode["tranches"]:
                signal = signal_by_date[tranche["signal_date"]]
                tranche.update({key: signal.get(key) for key in ("v3_layer", "v3_route", "v3_unknown_gate", "semantic_review_id")})
        result["stocks"].append(stock)
    summary = base.execution._summary(result)
    replay_audit = [event for stock in result["stocks"] for event in stock.get("audit", [])]
    summary["unexecuted_end_of_window"] = sum(
        str(event.get("event") or "").startswith("UNEXECUTED_END_OF_WINDOW")
        for event in replay_audit
    )
    summary["split_adjustment_events"] = sum(
        str(event.get("event") or "").startswith("SPLIT_ADJUSTED")
        for event in replay_audit
    )
    summary["cash_dividend_events"] = sum(
        str(event.get("event") or "").startswith("CASH_DIVIDEND")
        for event in replay_audit
    )
    episodes = [episode for stock in result["stocks"] for episode in stock.get("episodes", [])]
    mae_values = [float(episode["mae_pct_from_mother"]) for episode in episodes]
    summary["average_mae_pct"] = statistics.fmean(mae_values) if mae_values else 0.0
    summary["median_mae_pct"] = statistics.median(mae_values) if mae_values else 0.0
    summary["mark_to_market_drawdown"] = base._mark_to_market_drawdown(result, items)
    summary["initial_monitor_source_breakdown_overlap"] = base._source_breakdown(result, "initial_monitor_strategies")
    summary["strategy_scenario_breakdown_overlap"] = base._source_breakdown(result, "initial_monitor_strategies", by_scenario=True)
    result["summary"] = summary
    return result


def _distribution(variant: dict[str, Any]) -> dict[str, int]:
    buckets = Counter()
    for stock in variant["stocks"]:
        for episode in stock.get("episodes", []):
            value = float(episode["net_return_on_deployed_pct"])
            label = "<=-10%" if value <= -10 else "-10~-5%" if value < -5 else "-5~0%" if value < 0 else "0~5%" if value < 5 else "5~10%" if value < 10 else "10~20%" if value < 20 else ">=20%"
            buckets[label] += 1
    return dict(buckets)


def _route_breakdown(variant: dict[str, Any]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for stock in variant["stocks"]:
        for episode in stock.get("episodes", []):
            grouped[str(episode["tranches"][0].get("v3_route") or "UNKNOWN")].append(episode)
    rows = []
    for route, episodes in sorted(grouped.items()):
        pnl = [float(row["net_pnl"]) for row in episodes]
        deployed = sum(float(row["deployed_cash"]) for row in episodes)
        wins = sum(value > 0 for value in pnl)
        gross_win = sum(value for value in pnl if value > 0)
        gross_loss = -sum(value for value in pnl if value < 0)
        rows.append({"route": route, "episodes": len(episodes), "net_pnl": round(sum(pnl), 2), "return_on_deployed_pct": round(sum(pnl) / deployed * 100, 4) if deployed else 0, "win_rate_pct": round(wins / len(pnl) * 100, 2), "profit_factor": round(gross_win / gross_loss, 4) if gross_loss else None})
    return rows


def _source_route_breakdown(variant: dict[str, Any]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for stock in variant["stocks"]:
        for episode in stock.get("episodes", []):
            mother = episode["tranches"][0]
            route = str(mother.get("v3_route") or "UNKNOWN")
            for source in mother.get("initial_monitor_strategies") or ["UNKNOWN_SELECTION_SOURCE"]:
                grouped[(str(source), route)].append(episode)
    rows = []
    for (source, route), episodes in grouped.items():
        pnl = [float(row["net_pnl"]) for row in episodes]
        returns = [float(row["net_return_on_deployed_pct"]) for row in episodes]
        mfe = [float(row["mfe_pct_from_mother"]) for row in episodes]
        gross_win = sum(value for value in pnl if value > 0)
        gross_loss = -sum(value for value in pnl if value < 0)
        rows.append({
            "strategy": source, "route": route, "episodes": len(episodes),
            "open": sum(str(row["status"]).startswith("OPEN") for row in episodes),
            "net_pnl_full_overlap_attribution": round(sum(pnl), 2),
            "average_return_pct": statistics.fmean(returns),
            "average_mfe_pct": statistics.fmean(mfe),
            "win_rate_pct": sum(value > 0 for value in pnl) / len(pnl) * 100,
            "profit_factor": round(gross_win / gross_loss, 4) if gross_loss else None,
        })
    return sorted(rows, key=lambda row: (-row["episodes"], -row["net_pnl_full_overlap_attribution"], row["strategy"], row["route"]))


def replay() -> dict[str, Any]:
    rows, validation = build_program_decisions()
    items, signals, lifecycles = _prepare_signals(rows)
    variants = {
        "V3_FIXED_10K": _run_variant("V3_FIXED_10K", items, signals, lifecycles, False),
        "V3_ADD2_10K": _run_variant("V3_ADD2_10K", items, signals, lifecycles, True),
    }
    for variant in variants.values():
        variant["summary"]["return_distribution"] = _distribution(variant)
        variant["summary"]["route_breakdown"] = _route_breakdown(variant)
        variant["summary"]["strategy_route_breakdown_overlap"] = _source_route_breakdown(variant)
    fixed = variants["V3_FIXED_10K"]["summary"]
    add2 = variants["V3_ADD2_10K"]["summary"]
    feasibility = {
        "net_profit_positive": float(fixed["net_pnl"]) > 0,
        "profit_factor_gte_1_20": fixed.get("profit_factor") is not None and float(fixed["profit_factor"]) >= 1.2,
        "fixed_median_return_nonnegative": float(fixed["median_return_pct"]) >= 0,
        "add2_improves_net_profit_or_peak_capital_efficiency": float(add2["net_pnl"]) > float(fixed["net_pnl"]) or float(add2["return_on_peak_capital_pct"]) > float(fixed["return_on_peak_capital_pct"]),
    }
    feasibility["v3_fixed_preliminarily_feasible"] = all(feasibility[key] for key in ("net_profit_positive", "profit_factor_gte_1_20", "fixed_median_return_nonnegative"))
    output = {
        "method_version": "formal-hybrid-ai-v3-replay-v1",
        "as_of": AS_OF, "stocks": EXPECTED_STOCKS, "stock_days": EXPECTED_STOCK_DAYS,
        "validation": validation, "feasibility": feasibility,
        "focus_stock_audit": {
            row["code"]: {"name": row.get("name"), "watchlist_events": row["v3"]["watchlist_events"], "triggers": row["v3"]["triggers"]}
            for row in rows if row["code"] in {"8054", "6535", "6140", "8096", "8059"}
        },
        "variants": variants,
    }
    _write_json(RESULT, output)
    _write_reports(output)
    return output


def _money(value: Any) -> str:
    return f"{float(value or 0):+,.0f}"


def _pct(value: Any) -> str:
    return f"{float(value or 0):+.2f}%"


def _write_trade_report(path: Path, name: str, variant: dict[str, Any]) -> None:
    lines = [f"# {name} 逐筆交易", "", "| 股票 | 情境／路徑 | 母單訊號 | 進場 | 出場／截至 | 狀態 | 份數 | MFE | MAE | 淨損益 | 報酬 |", "|---|---|---|---|---|---|---:|---:|---:|---:|---:|"]
    for stock in variant["stocks"]:
        for episode in stock.get("episodes", []):
            first = episode["tranches"][0]
            lines.append(f"| {stock['code']} {stock['name']} | {first['scenario']}／{first.get('v3_route')} | {first['signal_date']} | {episode['entry_date']} | {episode.get('exit_date') or AS_OF} | {episode['status']} | {episode['tranche_count']} | {_pct(episode['mfe_pct_from_mother'])} | {_pct(episode['mae_pct_from_mother'])} | {_money(episode['net_pnl'])} | {_pct(episode['net_return_on_deployed_pct'])} |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_reports(output: dict[str, Any]) -> None:
    feasibility = output["feasibility"]
    lines = ["# V3 混合式正式回測", "", "本報表只使用已鎖定的匿名 AI 共同結構 ledger；股票身分與績效在 ledger 雜湊完成後才解封。", "", "## 初步可行性", "", f"- 固定版結果：**{'PASS（通過）' if feasibility['v3_fixed_preliminarily_feasible'] else 'FAIL（未通過）'}**。", f"- 淨利 > 0：{feasibility['net_profit_positive']}；PF >= 1.20：{feasibility['profit_factor_gte_1_20']}；固定版中位報酬 >= 0：{feasibility['fixed_median_return_nonnegative']}。", f"- 最多兩次加碼是否改善淨利或尖峰資金效率：{feasibility['add2_improves_net_profit_or_peak_capital_efficiency']}。", "", "通過只表示本歷史窗初步可行，仍不代表已證明未來穩定獲利。", "", "## 總覽", "", "| 版本 | 交易episode | 已結束／持有中 | 買進份數 | 期末未成交 | 已實現 | 未實現 | 淨損益 | 勝率 | PF | 最大同時持倉 | 尖峰資金 |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for key, variant in output["variants"].items():
        summary = variant["summary"]
        lines.append(f"| `{key}` | {summary['trade_episodes']} | {summary['closed']}／{summary['open']} | {summary['buy_fills']} | {summary['unexecuted_end_of_window']} | {_money(summary['realized_net_pnl'])} | {_money(summary['unrealized_net_pnl_after_estimated_exit_cost'])} | {_money(summary['net_pnl'])} | {summary['win_rate_pct']:.2f}% | {summary.get('profit_factor') if summary.get('profit_factor') is not None else '—'} | {summary['maximum_concurrent_stocks']} | {summary['peak_concurrent_deployed_cash']:,.0f} |")
    for key, variant in output["variants"].items():
        lines.extend(["", f"## {key} 路徑績效", "", "| 路徑 | 筆數 | 淨損益 | 部署資金報酬 | 勝率 | PF |", "|---|---:|---:|---:|---:|---:|"])
        for row in variant["summary"]["route_breakdown"]:
            lines.append(f"| `{row['route']}` | {row['episodes']} | {_money(row['net_pnl'])} | {_pct(row['return_on_deployed_pct'])} | {row['win_rate_pct']:.2f}% | {row['profit_factor'] if row['profit_factor'] is not None else '—'} |")
        lines.extend(["", "### Episode 報酬分布", "", "| 報酬區間 | 筆數 |", "|---|---:|"])
        distribution = variant["summary"]["return_distribution"]
        for bucket in ("<=-10%", "-10~-5%", "-5~0%", "0~5%", "5~10%", "10~20%", ">=20%"):
            lines.append(f"| {bucket} | {distribution.get(bucket, 0)} |")
        summary = variant["summary"]
        lines.extend([
            "",
            f"平均／中位報酬：{_pct(summary['average_return_pct'])}／{_pct(summary['median_return_pct'])}；平均／中位 MFE：{_pct(summary['average_mfe_pct'])}／{_pct(summary['median_mfe_pct'])}；平均／中位 MAE：{_pct(summary['average_mae_pct'])}／{_pct(summary['median_mae_pct'])}；平均已出場回吐：{summary['closed_average_giveback_points']:.2f} 個百分點。",
            f"Top1／Top3 正獲利集中度：{summary['top1_profit_concentration_pct']:.2f}%／{summary['top3_profit_concentration_pct']:.2f}%；扣除 Top1 後淨損益：{_money(summary['net_without_top1'])}。",
            f"期末持有份數／投入：{summary['current_open_tranches']}／{summary['current_open_deployed_cash']:,.0f}；最大同時持股／份數：{summary['maximum_concurrent_stocks']}／{summary['maximum_concurrent_tranches']}。",
            f"每份平均／最大初始風險：{_pct(summary['average_initial_risk_pct_per_fill'])}／{_pct(summary['maximum_initial_risk_pct_per_fill'])}；尖峰靜態停損風險：{summary['peak_static_initial_stop_risk_twd']:,.0f} 元（{summary['peak_static_initial_stop_risk_date']}）。",
            f"逐日估計最大回撤：{summary['mark_to_market_drawdown']['max_drawdown_twd']:,.0f} 元（{summary['mark_to_market_drawdown']['max_drawdown_pct']:.2f}%）。",
            f"公司行動現金帳：分割／除權股數調整 {summary['split_adjustment_events']} 次；現金股利 {summary['cash_dividend_events']} 次。",
            "",
            "### 情境績效",
            "",
            "| 情境 | 筆數 | 持有中 | 淨損益 | 部署資金報酬 | 勝率 | PF |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ])
        for row in summary["scenario_breakdown"]:
            lines.append(f"| {row['scenario']} | {row['episodes']} | {row['open']} | {_money(row['net_pnl'])} | {_pct(row['return_on_deployed_cash_pct'])} | {row['win_rate_pct']:.2f}% | {row['profit_factor'] if row['profit_factor'] is not None else '—'} |")
        lines.extend(["", "### 初始選股來源績效（重疊歸因，不可加總）", "", "| 選股來源 | 筆數 | 持有中 | 完整重疊歸因損益 | 平均報酬 | 平均MFE | 勝率 |", "|---|---:|---:|---:|---:|---:|---:|"])
        for row in summary["initial_monitor_source_breakdown_overlap"]:
            lines.append(f"| `{row['strategy']}` | {row['episodes']} | {row['open']} | {_money(row['net_pnl_full_overlap_attribution'])} | {_pct(row['average_return_pct'])} | {_pct(row['average_mfe_pct'])} | {row['win_rate_pct']:.2f}% |")
        lines.extend(["", "### 初始選股來源 × AI情境（重疊歸因，不可加總）", "", "| 選股來源 | 情境 | 筆數 | 持有中 | 完整重疊歸因損益 | 平均報酬 | 平均MFE | 勝率 |", "|---|---|---:|---:|---:|---:|---:|---:|"])
        for row in summary["strategy_scenario_breakdown_overlap"]:
            lines.append(f"| `{row['strategy']}` | `{row['scenario']}` | {row['episodes']} | {row['open']} | {_money(row['net_pnl_full_overlap_attribution'])} | {_pct(row['average_return_pct'])} | {_pct(row['average_mfe_pct'])} | {row['win_rate_pct']:.2f}% |")
        lines.extend(["", "### 初始選股來源 × V3路徑（重疊歸因，不可加總）", "", "| 選股來源 | V3路徑 | 筆數 | 持有中 | 完整重疊歸因損益 | 平均報酬 | 平均MFE | 勝率 | PF |", "|---|---|---:|---:|---:|---:|---:|---:|---:|"])
        for row in summary["strategy_route_breakdown_overlap"]:
            lines.append(f"| `{row['strategy']}` | `{row['route']}` | {row['episodes']} | {row['open']} | {_money(row['net_pnl_full_overlap_attribution'])} | {_pct(row['average_return_pct'])} | {_pct(row['average_mfe_pct'])} | {row['win_rate_pct']:.2f}% | {row['profit_factor'] if row['profit_factor'] is not None else '—'} |")
    lines.extend(["", "## 五檔爭議股票稽核", "", "下列股票不作事前排除；此處只列程式在匿名 ledger 解封後還原的事件數。", "", "| 股票 | 監控生命週期事件 | V3核准觸發 |", "|---|---:|---:|"])
    for code, row in sorted(output["focus_stock_audit"].items()):
        lines.append(f"| {code} {row['name']} | {len(row['watchlist_events'])} | {len(row['triggers'])} |")
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _write_trade_report(FIXED_REPORT, "V3_FIXED_10K", output["variants"]["V3_FIXED_10K"])
    _write_trade_report(ADD2_REPORT, "V3_ADD2_10K", output["variants"]["V3_ADD2_10K"])
    stock_lines = ["# V3 全股票監控生命週期與觸發", ""]
    for row in _jsonl(STOCK_LEDGER):
        stock_lines.extend([f"## {row['code']} {row.get('name') or ''}", "", "### 監控事件", ""])
        stock_lines.extend(f"- {event['date']}：{event['event']}" for event in row["v3"]["watchlist_events"])
        stock_lines.extend(["", "### 核准觸發", ""])
        if row["v3"]["triggers"]:
            stock_lines.extend(f"- {trigger['signal_date']}：`{trigger['v3_route']}`／{trigger['scenario']}；防線 {trigger['stop_date']} @ {trigger['stop_price']}" for trigger in row["v3"]["triggers"])
        else:
            stock_lines.append("- 無")
        stock_lines.append("")
    STOCK_REPORT.write_text("\n".join(stock_lines) + "\n", encoding="utf-8")
    _write_decision_reports()


def _write_decision_reports() -> None:
    identities = _read(IDENTITIES)
    identity_by_anon = {str(row["anonymous_id"]): row for row in identities["mapping"]}
    parents = {str(row["code"]): row for row in (_read(PARENT_MANIFEST).get("items") or [])}
    rows = list(_jsonl(DECISION_LEDGER))
    lines = [
        "# V3 政策邊界完整決策紀錄", "",
        "本表在共同結構 ledger 鎖定後才還原股票身分；`WAIT（繼續監控）`、`TRADE（核准交易）`、`REMOVE（移出監控）` 均完整保留。", "",
        "| 股票 | 日期 | 客觀事件 | AI主要情境 | 左右／位階 | 權限 | V3路徑 | 原因碼 | 語意審核ID |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    unsealed: list[tuple[str, str, dict[str, Any]]] = []
    for row in rows:
        identity = identity_by_anon[str(row["anonymous_stock_id"])]
        code = str(identity["code"])
        name = str(parents.get(code, {}).get("name") or identity.get("name") or "")
        unsealed.append((code, name, row))
        events = "、".join(row.get("event_types") or []) or "—"
        reasons = "、".join(row.get("reason_codes") or []) or "—"
        lines.append(
            f"| {code} {name} | {row['as_of']} | {events} | `{row['primary_scenario']}` | "
            f"`{row['left_right_phase']}`／`{row['stage_location']}` | `{row['permission']}` | `{row['route']}` | {reasons} | `{row['review_id']}` |"
        )
    DECISION_REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")

    focus_codes = {"8054", "6535", "6140", "8096", "8059"}
    focus_lines = [
        "# V3 五檔爭議股票個別判讀稽核", "",
        "這些股票沒有被事前排除；以下逐一顯示所有政策邊界的 AI 結構與程式權限。", "",
    ]
    for code in sorted(focus_codes):
        selected = [(name, row) for item_code, name, row in unsealed if item_code == code]
        name = selected[0][0] if selected else str(parents.get(code, {}).get("name") or "")
        focus_lines.extend([
            f"## {code} {name}", "",
            "| 日期 | 事件 | 情境 | 左右／位階 | Q3／耗竭／衝突 | 權限／路徑 | 原因碼 | Gate結果 | 審核ID |",
            "|---|---|---|---|---|---|---|---|---|",
        ])
        if not selected:
            focus_lines.append("| — | — | — | — | — | — | 無政策邊界 | — | — |")
        for _, row in selected:
            flags = row["semantic_flags"]
            flag_text = f"{flags['q3']}／{flags['exhausted']}／{flags['scale_conflict']}"
            gates = "；".join(f"{key}={value}" for key, value in sorted(row["gate_results"].items()))
            focus_lines.append(
                f"| {row['as_of']} | {'、'.join(row.get('event_types') or [])} | `{row['primary_scenario']}` | "
                f"`{row['left_right_phase']}`／`{row['stage_location']}` | {flag_text} | "
                f"`{row['permission']}`／`{row['route']}` | {'、'.join(row.get('reason_codes') or []) or '—'} | {gates} | `{row['review_id']}` |"
            )
        focus_lines.append("")
    FOCUS_REPORT.write_text("\n".join(focus_lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    result = replay()
    print(json.dumps({key: value["summary"] for key, value in result["variants"].items()}, ensure_ascii=False, indent=2))
