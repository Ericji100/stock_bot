"""Replay V1/V2 plus the complete, batch-authored Codex V3 review.

The batch JSON files are AI decisions.  This module never infers a route or
approves a candidate: it validates the frozen review inputs, materializes the
approved events, and performs deterministic execution/accounting.
"""
from __future__ import annotations

import copy
import hashlib
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import formal_ai_historical_2023_replay as base
from scripts import formal_ai_historical_2023_v3_replay as prior_v3


ROOT = Path(__file__).resolve().parents[1]
PARENT = ROOT / "reports/course_backtest/2024-02-02/historical_scan_2023h2_formal_ai_v1_v2"
REVIEW_RUN = ROOT / "reports/course_backtest/2024-02-02/historical_scan_2023h2_formal_ai_v3_full_review"
RUN = ROOT / "reports/course_backtest/2024-02-02/historical_scan_2023h2_formal_ai_v1_v2_v3_full_codex"
REVIEWS = REVIEW_RUN / "ai_reviews"
BATCH_MANIFEST = REVIEW_RUN / "ai_review_batch_manifest.json"
AI_REVIEW_QUEUE = REVIEW_RUN / "ai_review_queue.jsonl"
PARENT_LEDGER = PARENT / "ai_decisions_merged.jsonl"
PARENT_BACKTEST = PARENT / "backtest.json"
LEDGER_PATH = RUN / "v3_ai_decisions.jsonl"
VALIDATION_PATH = RUN / "v3_decision_validation.json"
RESULT_PATH = RUN / "backtest.json"
SUMMARY_PATH = RUN / "backtest_summary.json"
REPORT_PATH = RUN / "comparison.md"
DECISION_REPORT_PATH = RUN / "v3_ai_decisions.md"
PREPERFORMANCE_LOCK_PATH = RUN / "preperformance_decision_lock.json"
EXPECTED_PARENT_SHA = "8b239b5f2d4ec8f973df49bf7360013f21ad44bad7fd0d80ab359ff00ac387b4"
EXPECTED_QUEUE_ROWS = 4_871
EXPECTED_STOCKS = 1_029


ROUTE_SCENARIO = {
    "NEAR_PASS_MACRO_COPY": "MACRO_COPY_RESONANCE",
    "NEAR_PASS_FRESH_Q1": "FRESH_Q1_EXPANSION",
    "BEAR_REVERSAL_PROBE": "BEAR_REVERSAL_LEFT_RIGHT",
}
ALLOWED_UNKNOWN = {
    "NEAR_PASS_MACRO_COPY": {
        "COMPLETED_PARENT_ANCHOR", "TAIJI_GENERATION_MAPPED", "DUAL_SCALE_LONG_ALIGNMENT",
    },
    "NEAR_PASS_FRESH_Q1": {
        "CLEAN_MEATY_DESTRUCTIVE_TRACEABLE", "DYNAMIC_Q1_EXPANSION", "EARLY_TAIJI_GENERATION",
    },
    "BEAR_REVERSAL_PROBE": {"BEAR_LATE_STAGE_EVIDENCE"},
}
ALLOWED_PHASES = {
    "NEAR_PASS_MACRO_COPY": {"RR", "DIRECT_TO_RIGHT"},
    "NEAR_PASS_FRESH_Q1": {"DIRECT_TO_RIGHT", "RR"},
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
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _trigger_path(route: str, phase: str) -> str:
    if route == "NEAR_PASS_MACRO_COPY":
        return "CORRECTION_SMALL_REANCHOR_BREAK"
    if route == "NEAR_PASS_FRESH_Q1":
        return "FRESH_ANCHOR_DESTRUCTIVE_BREAK"
    return "BEAR_DIRECT_RIGHT_DUAL_BREAK" if phase == "DIRECT_TO_RIGHT" else f"BEAR_{phase}_SMALL_DEFENSE_BREAK"


def validate_batch_reviews(*, require_complete: bool = True) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Validate authored reviews and return blind-primary approvals only."""
    manifest = _read(BATCH_MANIFEST)
    errors: list[dict[str, Any]] = []
    approvals: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    reviewed_ids: set[str] = set()
    batch_stats: list[dict[str, Any]] = []
    route_counts: Counter[str] = Counter()

    for item in manifest["files"]:
        round_no, batch_no = int(item["round"]), int(item["batch"])
        stem = f"round_{round_no:02d}_batch_{batch_no:02d}"
        batch_path = Path(item["jsonl"])
        review_path = REVIEWS / f"{stem}.json"
        batch_rows = _jsonl(batch_path)
        by_id = {str(row["review_id"]): row for row in batch_rows}
        if _sha(batch_path) != str(item["jsonl_sha256"]):
            errors.append({"batch": stem, "error": "BATCH_HASH_CHANGED"})
        if len(by_id) != len(batch_rows) or len(batch_rows) != int(item["rows"]):
            errors.append({"batch": stem, "error": "BATCH_ROW_OR_ID_MISMATCH"})
        if not review_path.exists():
            if require_complete:
                errors.append({"batch": stem, "error": "MISSING_AI_REVIEW"})
            continue
        review = _read(review_path)
        if review.get("review_protocol") != "FORMAL_CODEX_AI_V3_CAUSAL_BATCH_REVIEW_V1":
            errors.append({"batch": stem, "error": "INVALID_REVIEW_PROTOCOL"})
        if str(review.get("reviewer") or "").upper().find("MINIMAX") >= 0 or str(review.get("reviewer") or "").upper().find("M3") >= 0:
            errors.append({"batch": stem, "error": "DISALLOWED_REVIEWER", "reviewer": review.get("reviewer")})
        if not str(review.get("reviewer") or "").upper().startswith("CODEX"):
            errors.append({"batch": stem, "error": "NON_CODEX_REVIEWER", "reviewer": review.get("reviewer")})
        if review.get("future_performance_visible_in_input") is not False:
            errors.append({"batch": stem, "error": "FUTURE_PERFORMANCE_NOT_BLIND"})
        if review.get("same_stock_future_checkpoint_visible_in_batch") is not False:
            errors.append({"batch": stem, "error": "SAME_STOCK_FUTURE_VISIBLE"})
        if review.get("default_decision_for_unlisted_review_ids") != "NO_TRADE":
            errors.append({"batch": stem, "error": "INVALID_DEFAULT_DECISION"})
        if int(review.get("round", -1)) != round_no or int(review.get("batch", -1)) != batch_no:
            errors.append({"batch": stem, "error": "REVIEW_BATCH_ID_MISMATCH"})
        if int(review.get("input_rows", -1)) != len(batch_rows):
            errors.append({"batch": stem, "error": "REVIEW_INPUT_COUNT_MISMATCH"})
        if str(review.get("input_jsonl_sha256") or "") != _sha(batch_path):
            errors.append({"batch": stem, "error": "REVIEW_INPUT_HASH_MISMATCH"})

        local_ids: set[str] = set()
        local_approved = 0
        local_diagnostic = 0
        for authored in review.get("approvals") or []:
            review_id = str(authored.get("review_id") or "")
            if review_id in local_ids or review_id not in by_id:
                errors.append({"batch": stem, "review_id": review_id, "error": "DUPLICATE_OR_FOREIGN_APPROVAL_ID"})
                continue
            local_ids.add(review_id)
            row = by_id[review_id]
            route = str(authored.get("route") or "")
            gate = str(authored.get("unknown_gate") or "")
            phase = str(authored.get("phase") or "")
            if route not in ALLOWED_UNKNOWN or gate not in ALLOWED_UNKNOWN.get(route, set()):
                errors.append({"batch": stem, "review_id": review_id, "error": "INVALID_ROUTE_OR_UNKNOWN_GATE"})
                continue
            if phase not in ALLOWED_PHASES[route]:
                errors.append({"batch": stem, "review_id": review_id, "error": "INVALID_PHASE"})
                continue
            if not str(authored.get("reason") or "").strip():
                errors.append({"batch": stem, "review_id": review_id, "error": "MISSING_AI_REASON"})
                continue
            combined = {**row, **authored, "review_file": str(review_path.resolve())}
            if row.get("outcome_exposure") != "BLIND_PRIMARY":
                diagnostics.append(combined)
                local_diagnostic += 1
            else:
                approvals.append(combined)
                reviewed_ids.add(review_id)
                route_counts[route] += 1
                local_approved += 1
        batch_stats.append({
            "batch": stem,
            "rows": len(batch_rows),
            "blind_approvals": local_approved,
            "diagnostic_approvals_excluded": local_diagnostic,
            "review_sha256": _sha(review_path),
        })

    if int(manifest.get("rows", -1)) != EXPECTED_QUEUE_ROWS:
        errors.append({"error": "QUEUE_SIZE_CHANGED", "actual": manifest.get("rows")})
    unique_approval_ids = {str(row["review_id"]) for row in approvals}
    if len(unique_approval_ids) != len(approvals):
        errors.append({"error": "DUPLICATE_APPROVAL_ACROSS_BATCHES"})
    audit = {
        "valid": not errors,
        "errors": errors,
        "expected_batches": len(manifest["files"]),
        "reviewed_batches": len(batch_stats),
        "expected_queue_rows": EXPECTED_QUEUE_ROWS,
        "blind_primary_approvals": len(approvals),
        "diagnostic_approvals_excluded": len(diagnostics),
        "route_counts": dict(route_counts),
        "batch_stats": batch_stats,
        "batch_manifest": {"path": str(BATCH_MANIFEST.resolve()), "sha256": _sha(BATCH_MANIFEST)},
        "review_protocol": "FORMAL_CODEX_AI_V3_CAUSAL_BATCH_REVIEW_V1",
        "decision_boundary": "AI authored route/NO_TRADE; code validated and replayed only",
    }
    if require_complete and errors:
        raise ValueError(f"V3 batch review validation failed; missing/invalid items: {len(errors)}")
    return approvals, audit


def _materialize_trigger(approval: dict[str, Any]) -> dict[str, Any]:
    route = str(approval["route"])
    phase = str(approval["phase"])
    risk = approval["risk"]
    reason = str(approval["reason"])
    late = ["FAILED_CONTINUATION_LOW_OR_PARTIAL_EXHAUSTION"] if route == "BEAR_REVERSAL_PROBE" else []
    return {
        "signal_date": str(approval["review_as_of"]),
        "scenario": ROUTE_SCENARIO[route],
        "trigger_path": _trigger_path(route, phase),
        "episode_or_add_candidate": "MOTHER_OR_REENTRY",
        "stop_date": str(risk["stop_date"]),
        "stop_price": float(risk["stop_price"]),
        "left_right": phase,
        "evidence": [
            reason,
            "本事件由Codex以截至訊號日的因果封包核准；共同硬閘全PASS、零FAIL，且只有所列gate為UNKNOWN。",
        ],
        "invalidation": {
            "scope": "TRADE_EPISODE",
            "price": float(risk["stop_price"]),
            "rule": f"收盤失守{risk['stop_date']}已確認控制低，本次episode失效；若大結構未失效，等待新的獨立觸發可再進場。",
        },
        "v3_layer": "BEAR_REVERSAL_PROBE" if route == "BEAR_REVERSAL_PROBE" else "NEAR_PASS",
        "v3_route": route,
        "v3_unknown_gate": str(approval["unknown_gate"]),
        "v3_phase": phase,
        "v3_late_stage_partial_evidence": late,
        "v3_ai_decision_source": str(approval["review_file"]),
        "v3_review_id": str(approval["review_id"]),
        "v3_packet_sha256": str(approval["packet_sha256"]),
    }


def build_v3_ledger() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    approvals, review_audit = validate_batch_reviews(require_complete=True)
    if _sha(PARENT_LEDGER) != EXPECTED_PARENT_SHA:
        raise ValueError("Frozen V1/V2 parent ledger hash changed")
    parent_rows = _jsonl(PARENT_LEDGER)
    if len(parent_rows) != EXPECTED_STOCKS:
        raise ValueError(f"Expected {EXPECTED_STOCKS} parent rows, got {len(parent_rows)}")
    approvals_by_code: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for approval in approvals:
        approvals_by_code[str(approval["code"])].append(approval)
    reviewed_candidate_counts = Counter(str(row["code"]) for row in _jsonl(AI_REVIEW_QUEUE))
    output: list[dict[str, Any]] = []
    approved_packet_errors: list[dict[str, Any]] = []
    for parent in parent_rows:
        code = str(parent["code"])
        core = (parent.get("v2") or {}).get("triggers") or []
        extra = sorted(approvals_by_code.get(code, []), key=lambda row: str(row["review_as_of"]))
        if core and extra:
            approved_packet_errors.append({"code": code, "error": "V2_CORE_PRECEDENCE_COLLISION"})
            extra = []
        triggers = [prior_v3._core_trigger(trigger) for trigger in core]
        for approval in extra:
            packet_path = Path(str(approval["packet_path"]))
            if _sha(packet_path) != str(approval["packet_sha256"]):
                approved_packet_errors.append({"review_id": approval["review_id"], "error": "PACKET_HASH_CHANGED"})
                continue
            packet = _read(packet_path)
            risk = approval["risk"]
            candidate = packet["candidate_day"]
            pivot = next((
                item for item in packet["confirmed_pivots_asof"]
                if item["side"] == "LOW"
                and str(item["source_date"]) == str(risk["stop_date"])
                and str(item["confirmation_date"]) == str(risk["stop_confirmed_on"])
                and abs(float(item["price"]) - float(risk["stop_price"])) <= 1e-4
            ), None)
            if (
                packet.get("review_as_of") != approval["review_as_of"]
                or packet["review_contract"].get("future_bars_present")
                or packet["review_contract"].get("performance_present")
                or float(risk["stop_price"]) >= float(candidate["close"])
                or pivot is None
            ):
                approved_packet_errors.append({"review_id": approval["review_id"], "error": "NON_CAUSAL_PACKET_OR_STOP"})
                continue
            triggers.append(_materialize_trigger(approval))
        review = {
            "decision": "V2_CORE_READ_ONLY" if core else ("CODEX_V3_APPROVALS" if triggers else "V3_NO_TRADE"),
            "zero_fail_attestation": bool(triggers),
            "common_hard_guards_all_pass": bool(triggers),
            "reviewed_candidate_packets": reviewed_candidate_counts.get(code, 0),
            "reason": None if triggers else "完整Codex V3候選日審核未找到共同硬閘全PASS、零FAIL且恰一個允許UNKNOWN的交易路徑。",
        }
        row = copy.deepcopy(parent)
        row["v3"] = {
            "stock_status": "TRIGGERED" if triggers else "NO_TRADE",
            "watchlist_events": copy.deepcopy((parent.get("v2") or {}).get("watchlist_events") or []),
            "triggers": sorted(triggers, key=lambda value: str(value["signal_date"])),
            "no_trade_reason": None if triggers else review["reason"],
            "ai_overlay_review": review,
        }
        output.append(row)
    if approved_packet_errors:
        raise ValueError(f"V3 packet/precedence validation failed: {approved_packet_errors[:5]}")
    validation = {
        **review_audit,
        "valid": True,
        "expected_stocks": EXPECTED_STOCKS,
        "reviewed_stocks": len(output),
        "v2_core_stocks": sum(bool((row.get("v2") or {}).get("triggers")) for row in parent_rows),
        "v2_core_events": sum(len((row.get("v2") or {}).get("triggers") or []) for row in parent_rows),
        "v3_extra_stocks": len({str(row["code"]) for row in approvals}),
        "v3_extra_events": len(approvals),
        "v3_total_stocks": sum(bool(row["v3"]["triggers"]) for row in output),
        "v3_total_events": sum(len(row["v3"]["triggers"]) for row in output),
        "v3_no_trade_stocks": sum(not row["v3"]["triggers"] for row in output),
        "parent_merged_ledger_sha256": _sha(PARENT_LEDGER),
        "diagnostic_codes_excluded_from_main_result": _read(BATCH_MANIFEST)["outcome_exposed_codes"],
    }
    _write_jsonl(LEDGER_PATH, output)
    validation["v3_ledger_sha256"] = _sha(LEDGER_PATH)
    _write_json(VALIDATION_PATH, validation)
    return output, validation


def write_preperformance_lock(validation: dict[str, Any]) -> dict[str, Any]:
    """Freeze all AI decisions before the replay opens any performance artifact."""
    lock = {
        "status": "LOCKED_BEFORE_PERFORMANCE_REPLAY",
        "performance_visible_when_decisions_authored": False,
        "reviewed_batches": validation["reviewed_batches"],
        "expected_batches": validation["expected_batches"],
        "expected_queue_rows": validation["expected_queue_rows"],
        "blind_primary_approvals": validation["blind_primary_approvals"],
        "diagnostic_approvals_excluded": validation["diagnostic_approvals_excluded"],
        "route_counts": validation["route_counts"],
        "parent_merged_ledger": {
            "path": str(PARENT_LEDGER.resolve()),
            "sha256": _sha(PARENT_LEDGER),
        },
        "candidate_queue": {
            "path": str(AI_REVIEW_QUEUE.resolve()),
            "sha256": _sha(AI_REVIEW_QUEUE),
        },
        "batch_manifest": validation["batch_manifest"],
        "v3_decision_ledger": {
            "path": str(LEDGER_PATH.resolve()),
            "sha256": validation["v3_ledger_sha256"],
        },
        "review_files": validation["batch_stats"],
    }
    _write_json(PREPERFORMANCE_LOCK_PATH, lock)
    return lock


def replay() -> dict[str, Any]:
    rows, validation = build_v3_ledger()
    preperformance_lock = write_preperformance_lock(validation)
    prior_v3.RUN = RUN
    prior_v3.V3_LEDGER_PATH = LEDGER_PATH
    prior_v3.VALIDATION_PATH = VALIDATION_PATH
    prior_v3.RESULT_PATH = RESULT_PATH
    prior_v3.SUMMARY_PATH = SUMMARY_PATH
    fact, source, catalog, expected_items = base._input_context()
    items = {str(row["code"]): row for row in (source.get("items") or expected_items)}
    expected_by_code = {str(row["code"]): row for row in expected_items}
    daily, meta = base._catalog_maps(catalog)
    signals, lifecycle = prior_v3._prepare_signals(rows, items, expected_by_code, daily, meta)
    variants = {
        "V3_MOTHER_ONLY_10K": prior_v3._run_variant(
            "V3_MOTHER_ONLY_10K", items, signals, lifecycle, allow_adds=False, tiered=False,
        ),
        "V3_MOTHER_PLUS_2": prior_v3._run_variant(
            "V3_MOTHER_PLUS_2", items, signals, lifecycle, allow_adds=True, tiered=False,
        ),
    }
    parent = _read(PARENT_BACKTEST)
    identity = prior_v3._core_identity(parent, variants)
    if not identity["valid"]:
        raise ValueError("V3 core execution diverged from frozen V2")
    parent_summaries: dict[str, Any] = {}
    for key, variant in parent["variants"].items():
        summary = copy.deepcopy(variant["summary"])
        prior_v3._add_scenario_mfe(summary, variant)
        parent_summaries[key] = summary
    result = {
        "method_version": "formal-codex-ai-v3-full-causal-review-replay-v1",
        "as_of": prior_v3.AS_OF,
        "validation": validation,
        "core_identity": identity,
        "parent_variants": parent_summaries,
        "variants": variants,
        "decision_files": {
            "parent_v1_v2": {"path": str(PARENT_LEDGER.resolve()), "sha256": _sha(PARENT_LEDGER)},
            "v3": {"path": str(LEDGER_PATH.resolve()), "sha256": _sha(LEDGER_PATH)},
            "batch_manifest": {"path": str(BATCH_MANIFEST.resolve()), "sha256": _sha(BATCH_MANIFEST)},
            "preperformance_lock": {
                "path": str(PREPERFORMANCE_LOCK_PATH.resolve()),
                "sha256": _sha(PREPERFORMANCE_LOCK_PATH),
            },
        },
        "preperformance_lock": preperformance_lock,
    }
    _write_json(RESULT_PATH, result)
    compact = copy.deepcopy(result)
    compact["variants"] = {key: value["summary"] for key, value in variants.items()}
    _write_json(SUMMARY_PATH, compact)
    return result


def _money(value: float) -> str:
    return f"{float(value):+,.0f}"


def _pct(value: float) -> str:
    return f"{float(value):+.2f}%"


def _pf(value: float | None) -> str:
    return "—" if value is None else f"{float(value):.2f}"


def _cell(value: Any) -> str:
    return str(value if value is not None else "—").replace("|", "／").replace("\n", " ")


def _variant_line(key: str, summary: dict[str, Any]) -> str:
    return (
        f"| {key} | {summary['stocks_traded']} | {summary['trade_episodes']} | {summary['closed']}／{summary['open']} | "
        f"{summary['buy_fills']} | {_money(summary['realized_net_pnl'])} | {_money(summary['unrealized_net_pnl_after_estimated_exit_cost'])} | "
        f"{_money(summary['net_pnl'])} | {_pct(summary['return_on_peak_capital_pct'])} | {summary['win_rate_pct']:.2f}% | "
        f"{_pf(summary['profit_factor'])} | {summary['maximum_concurrent_stocks']}／{summary['maximum_concurrent_tranches']} | "
        f"{summary['peak_concurrent_deployed_cash']:,.0f} |"
    )


def _breakdown(lines: list[str], title: str, rows: list[dict[str, Any]], key: str) -> None:
    lines += ["", f"### {title}", "", "| 分類 | 回合／持有 | 淨損益 | 平均報酬 | 勝率 | 平均MFE |", "|---|---:|---:|---:|---:|---:|"]
    for row in rows:
        pnl = row.get("net_pnl", row.get("net_pnl_full_overlap_attribution", 0))
        lines.append(
            f"| {_cell(row[key])} | {row['episodes']}／{row['open']} | {_money(pnl)} | {_pct(row['average_return_pct'])} | "
            f"{row['win_rate_pct']:.2f}% | {_pct(row.get('average_mfe_pct', 0))} |"
        )


def _strategy_scenario(lines: list[str], rows: list[dict[str, Any]]) -> None:
    lines += [
        "", "### 選股策略 × 四情境（重疊歸因）", "",
        "> 同一檔可同時屬於多個選股策略，各列不能直接相加。",
        "", "| 選股策略 | 情境 | 回合／持有 | 淨損益 | 平均報酬 | 勝率 | 平均MFE |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            f"| {_cell(row['strategy'])} | {_cell(row['scenario'])} | {row['episodes']}／{row['open']} | "
            f"{_money(row['net_pnl_full_overlap_attribution'])} | {_pct(row['average_return_pct'])} | "
            f"{row['win_rate_pct']:.2f}% | {_pct(row.get('average_mfe_pct', 0))} |"
        )


def _write_trade_report(key: str, variant: dict[str, Any]) -> Path:
    path = RUN / key.lower() / "backtest.md"
    summary = variant["summary"]
    lines = [
        f"# {key}逐筆交易",
        "",
        f"- {summary['trade_episodes']}回合；已出場{summary['closed']}、持有中{summary['open']}；淨損益{_money(summary['net_pnl'])}元。",
        f"- 最大同時持股{summary['maximum_concurrent_stocks']}檔／{summary['maximum_concurrent_tranches']}份；尖峰資金{summary['peak_concurrent_deployed_cash']:,.0f}元。",
        "",
        "| 股票 | V3路徑 | 情境 | 訊號日 | 進場日 | 出場／期末 | 狀態 | 份數 | 投入 | 淨損益 | 報酬 | MFE | MAE | 出場原因 |",
        "|---|---|---|---|---|---|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    episodes = [episode for stock in variant["stocks"] for episode in stock.get("episodes", [])]
    for episode in sorted(episodes, key=lambda row: (str(row["entry_date"]), str(row["code"]), str(row["episode_id"]))):
        mother = episode["tranches"][0]
        end = episode.get("exit_date") or episode.get("mark_date")
        lines.append(
            f"| {episode['code']} {_cell(episode['name'])} | {_cell(mother.get('v3_route'))} | {_cell(mother['scenario'])} | "
            f"{mother['signal_date']} | {episode['entry_date']} | {end} | {_cell(episode['status'])} | {episode['tranche_count']} | "
            f"{episode['deployed_cash']:,.0f} | {_money(episode['net_pnl'])} | {_pct(episode['net_return_on_deployed_pct'])} | "
            f"{_pct(episode['mfe_pct_from_mother'])} | {_pct(episode['mae_pct_from_mother'])} | {_cell(episode['exit_reason'])} |"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def write_reports(result: dict[str, Any]) -> None:
    summaries = {**result["parent_variants"], **{key: value["summary"] for key, value in result["variants"].items()}}
    ordered = [
        "V1_MOTHER_ONLY_10K", "V1_MOTHER_PLUS_2",
        "V2_MOTHER_ONLY_10K", "V2_MOTHER_PLUS_2",
        "V3_MOTHER_ONLY_10K", "V3_MOTHER_PLUS_2",
    ]
    lines = [
        "# 2023下半年1,029檔正式Codex AI V1／V2／V3完整交叉回測",
        "",
        "> 監控自2023-06-01起；2024-02-04為週日，績效截至2024-02-02收盤。V1/V2引用雜湊鎖定的正式AI ledger；V3核心唯讀承接V2，新增事件來自4,871個因果候選日的Codex逐批判讀。程式只驗證、成交與記帳。",
        "",
        "## 判讀與一致性",
        "",
        f"- V3完整AI候選日：{result['validation']['expected_queue_rows']:,}；批次：{result['validation']['reviewed_batches']}／{result['validation']['expected_batches']}。",
        f"- V3新增盲審核准：{result['validation']['v3_extra_events']}筆、{result['validation']['v3_extra_stocks']}檔；診斷樣本排除{result['validation']['diagnostic_approvals_excluded']}筆。",
        f"- V3路徑分布：{result['validation']['route_counts']}。",
        f"- V2核心與V3內的核心成交完全一致：{'是' if result['core_identity']['valid'] else '否'}。",
        "- 本回放是回溯因果研究，不是未來未知期間的真正樣本外驗證；沒有使用候選日後價格、MFE或個股損益來決定訊號。",
        "",
        "## 組合總表",
        "",
        "| 組合 | 股票 | 回合 | 已出場／持有 | 買進份數 | 已實現 | 未實現* | 淨損益 | 尖峰資金報酬 | 勝率 | PF | 最大持股／份數 | 尖峰資金 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for key in ordered:
        lines.append(_variant_line(key, summaries[key]))
    lines += ["", "\\* 未實現損益已估計期末賣出成本。", "", "## 固定版與加碼版", "", "| 規則 | 固定版淨損益 | 加碼版淨損益 | 加碼增減 | 固定版尖峰資金 | 加碼版尖峰資金 |", "|---|---:|---:|---:|---:|---:|"]
    for version in ("V1", "V2", "V3"):
        fixed, added = summaries[f"{version}_MOTHER_ONLY_10K"], summaries[f"{version}_MOTHER_PLUS_2"]
        lines.append(
            f"| {version} | {_money(fixed['net_pnl'])} | {_money(added['net_pnl'])} | {_money(added['net_pnl'] - fixed['net_pnl'])} | "
            f"{fixed['peak_concurrent_deployed_cash']:,.0f} | {added['peak_concurrent_deployed_cash']:,.0f} |"
        )
    for key in ordered:
        summary = summaries[key]
        lines += [
            "", f"## {key}", "",
            f"- 平均／中位報酬：{_pct(summary['average_return_pct'])}／{_pct(summary['median_return_pct'])}；平均／中位MFE：{_pct(summary['average_mfe_pct'])}／{_pct(summary['median_mfe_pct'])}。",
            f"- 已出場{summary['closed']}筆，持有中{summary['open']}筆；MFE≥20%共{summary['mfe_20_plus_count']}筆。",
            "", "### 損益分布", "", "| 區間 | 全部 | 已出場 | 持有中 | MFE |", "|---|---:|---:|---:|---:|",
        ]
        for label in summary["return_distribution"]:
            lines.append(f"| {label} | {summary['return_distribution'][label]} | {summary['closed_return_distribution'][label]} | {summary['open_return_distribution'][label]} | {summary['mfe_distribution'][label]} |")
        _breakdown(lines, "四情境", summary["scenario_breakdown"], "scenario")
        _breakdown(lines, "首次入選策略（重疊歸因）", summary["initial_monitor_source_breakdown_overlap"], "strategy")
        _strategy_scenario(lines, summary["strategy_scenario_breakdown_overlap"])
        if key.startswith("V3_"):
            _breakdown(lines, "V3路徑", summary["v3_route_breakdown"], "route")
    trade_paths = {key: _write_trade_report(key, result["variants"][key]) for key in result["variants"]}
    lines += [
        "", "## 稽核與檔案", "",
        f"- V3 ledger SHA-256：`{result['validation']['v3_ledger_sha256']}`。",
        f"- 績效開封前決策鎖：[preperformance_decision_lock.json]({PREPERFORMANCE_LOCK_PATH.as_posix()})",
        f"- V3固定版逐筆：[backtest.md]({trade_paths['V3_MOTHER_ONLY_10K'].as_posix()})",
        f"- V3加碼版逐筆：[backtest.md]({trade_paths['V3_MOTHER_PLUS_2'].as_posix()})",
        f"- V1逐筆（封存）：[backtest.md]({(PARENT / 'v1/backtest.md').as_posix()})",
        f"- V2逐筆（封存）：[backtest.md]({(PARENT / 'v2/backtest.md').as_posix()})",
        f"- 完整JSON：[backtest.json]({RESULT_PATH.as_posix()})",
    ]
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")

    dlines = [
        "# V3完整Codex AI決策稽核", "",
        "| Index | 股票 | 結果 | V3訊號 | 唯一UNKNOWN | 候選核准數 |", "|---:|---|---|---|---|---:|",
    ]
    for row in _jsonl(LEDGER_PATH):
        triggers = row["v3"]["triggers"]
        text = "；".join(f"{item['signal_date']} {item['v3_route']} 防線{item['stop_price']}" for item in triggers)
        unknown = "、".join(str(item.get("v3_unknown_gate") or "無") for item in triggers) if triggers else "—"
        dlines.append(f"| {row['index']} | {row['code']} {_cell(row['name'])} | {row['v3']['ai_overlay_review']['decision']} | {_cell(text)} | {_cell(unknown)} | {len(triggers)} |")
    DECISION_REPORT_PATH.write_text("\n".join(dlines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    payload = replay()
    write_reports(payload)
    print(json.dumps({
        "report": str(REPORT_PATH),
        "validation": payload["validation"],
        "summaries": {**payload["parent_variants"], **{key: value["summary"] for key, value in payload["variants"].items()}},
    }, ensure_ascii=True, indent=2, allow_nan=False))
