"""Compare frozen hybrid V3 behavior with the earlier pure-AI V3 reference.

This is a post-replay audit.  It never feeds reference decisions or performance
back into the frozen semantic review or trading engine.
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
OLD_ROOT = ROOT / "reports/course_backtest/2024-02-02/historical_scan_2023h2_formal_ai_v1_v2_v3_full_codex"
NEW_ROOT = ROOT / "reports/course_backtest/2024-02-02/historical_scan_2023h2_formal_ai_v3_daily_scan/hybrid_monitoring_v1"
DEFAULT_OLD_LEDGER = OLD_ROOT / "v3_ai_decisions.jsonl"
DEFAULT_OLD_BACKTEST = OLD_ROOT / "backtest.json"
DEFAULT_NEW_LEDGER = NEW_ROOT / "v3_stock_lifecycle_and_triggers.jsonl"
DEFAULT_NEW_BACKTEST = NEW_ROOT / "v3_backtest.json"
DEFAULT_JSON = NEW_ROOT / "behavioral_equivalence.json"
DEFAULT_MD = NEW_ROOT / "behavioral_equivalence.md"


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _num_equal(left: Any, right: Any, tolerance: float) -> bool:
    if left is None or right is None:
        return left is right
    try:
        return math.isclose(float(left), float(right), rel_tol=0.0, abs_tol=tolerance)
    except (TypeError, ValueError):
        return left == right


def _clean_scenario(value: Any) -> str | None:
    if value is None:
        return None
    return str(value).split("（", 1)[0]


def _signal(code: str, name: str | None, trigger: dict[str, Any], source: str) -> dict[str, Any]:
    return {
        "source": source,
        "code": str(code),
        "name": name or "",
        "signal_date": str(trigger["signal_date"]),
        "route": trigger.get("v3_route") or trigger.get("trigger_path"),
        "scenario": _clean_scenario(trigger.get("scenario")),
        "phase": trigger.get("v3_phase") or trigger.get("left_right"),
        "stop_date": trigger.get("stop_date"),
        "stop_price": trigger.get("stop_price"),
        "review_id": trigger.get("semantic_review_id") or trigger.get("v3_review_id"),
    }


def load_old_signals(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for stock in _jsonl(path):
        for trigger in ((stock.get("v3") or {}).get("triggers") or []):
            rows.append(_signal(str(stock["code"]), stock.get("name"), trigger, "PURE_AI_REFERENCE"))
    return rows


def load_new_signals(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for stock in _jsonl(path):
        for trigger in ((stock.get("v3") or {}).get("triggers") or []):
            rows.append(_signal(str(stock["code"]), stock.get("name"), trigger, "HYBRID_V3"))
    return rows


def _signal_key(row: dict[str, Any]) -> tuple[str, str]:
    return str(row["code"]), str(row["signal_date"])


def _signal_differences(old: dict[str, Any], new: dict[str, Any], price_tolerance: float) -> list[str]:
    differences = []
    for field in ("route", "scenario", "phase", "stop_date"):
        if old.get(field) != new.get(field):
            differences.append(field)
    if not _num_equal(old.get("stop_price"), new.get("stop_price"), price_tolerance):
        differences.append("stop_price")
    return differences


def compare_signals(
    old_rows: list[dict[str, Any]],
    new_rows: list[dict[str, Any]],
    *,
    price_tolerance: float = 1e-4,
    date_shift_days: int = 7,
) -> dict[str, Any]:
    old_by_key = {_signal_key(row): row for row in old_rows}
    new_by_key = {_signal_key(row): row for row in new_rows}
    if len(old_by_key) != len(old_rows):
        raise ValueError("duplicate pure-AI signal key")
    if len(new_by_key) != len(new_rows):
        raise ValueError("duplicate hybrid-V3 signal key")

    details: list[dict[str, Any]] = []
    common = sorted(set(old_by_key) & set(new_by_key))
    exact = 0
    detail_diff = 0
    for key in common:
        old, new = old_by_key[key], new_by_key[key]
        differences = _signal_differences(old, new, price_tolerance)
        category = "EXACT_MATCH" if not differences else "ACTION_MATCH_DETAIL_DIFF"
        exact += category == "EXACT_MATCH"
        detail_diff += category == "ACTION_MATCH_DETAIL_DIFF"
        details.append({"category": category, "key": list(key), "differences": differences, "old": old, "new": new})

    old_only = [old_by_key[key] for key in sorted(set(old_by_key) - set(new_by_key))]
    new_only = [new_by_key[key] for key in sorted(set(new_by_key) - set(old_by_key))]
    unused_new = set(range(len(new_only)))
    shifted_old: set[int] = set()
    shifted_new: set[int] = set()
    candidates: list[tuple[int, int, int, int]] = []
    for old_index, old in enumerate(old_only):
        old_day = date.fromisoformat(old["signal_date"])
        for new_index, new in enumerate(new_only):
            if old["code"] != new["code"]:
                continue
            gap = abs((date.fromisoformat(new["signal_date"]) - old_day).days)
            if 0 < gap <= date_shift_days:
                route_penalty = 0 if old.get("route") == new.get("route") else 1
                candidates.append((route_penalty, gap, old_index, new_index))
    for _, gap, old_index, new_index in sorted(candidates):
        if old_index in shifted_old or new_index not in unused_new:
            continue
        shifted_old.add(old_index)
        shifted_new.add(new_index)
        unused_new.remove(new_index)
        old, new = old_only[old_index], new_only[new_index]
        details.append({
            "category": "DATE_SHIFT",
            "key": [old["code"], old["signal_date"], new["signal_date"]],
            "calendar_day_gap": gap,
            "differences": _signal_differences(old, new, price_tolerance),
            "old": old,
            "new": new,
        })
    for index, old in enumerate(old_only):
        if index not in shifted_old:
            details.append({"category": "OLD_ONLY", "key": list(_signal_key(old)), "old": old, "new": None})
    for index, new in enumerate(new_only):
        if index not in shifted_new:
            details.append({"category": "NEW_ONLY", "key": list(_signal_key(new)), "old": None, "new": new})

    counts = Counter(row["category"] for row in details)
    return {
        "old_signal_count": len(old_rows),
        "new_signal_count": len(new_rows),
        "same_day_action_count": len(common),
        "same_day_action_recall_vs_old_pct": round(len(common) / len(old_rows) * 100, 4) if old_rows else None,
        "same_day_action_precision_vs_old_pct": round(len(common) / len(new_rows) * 100, 4) if new_rows else None,
        "exact_detail_rate_within_same_day_pct": round(exact / len(common) * 100, 4) if common else None,
        "counts": dict(sorted(counts.items())),
        "details": sorted(details, key=lambda row: tuple(str(value) for value in row["key"])),
    }


def _episodes(backtest: dict[str, Any], variant: str, source: str) -> list[dict[str, Any]]:
    rows = []
    for stock in backtest["variants"][variant]["stocks"]:
        for episode in stock.get("episodes") or []:
            mother = episode["tranches"][0]
            rows.append({
                "source": source,
                "code": str(stock["code"]),
                "name": stock.get("name") or "",
                "signal_date": str(mother["signal_date"]),
                "entry_date": episode.get("entry_date"),
                "entry_price_raw": mother.get("entry_price_raw"),
                "entry_price_adjusted": mother.get("entry_price_adjusted"),
                "entry_shares": mother.get("entry_shares"),
                "status": episode.get("status"),
                "exit_signal_date": episode.get("exit_signal_date"),
                "exit_date": episode.get("exit_date"),
                "exit_price_raw": episode.get("exit_price_raw"),
                "net_pnl": episode.get("net_pnl"),
                "route": mother.get("v3_route"),
                "scenario": _clean_scenario(mother.get("scenario")),
            })
    return rows


def _trade_key(row: dict[str, Any]) -> tuple[str, str]:
    return str(row["code"]), str(row["signal_date"])


def _trade_differences(old: dict[str, Any], new: dict[str, Any]) -> tuple[list[str], list[str]]:
    entry = []
    exit_fields = []
    for field in ("entry_date", "entry_shares"):
        if old.get(field) != new.get(field):
            entry.append(field)
    for field in ("entry_price_raw", "entry_price_adjusted"):
        if not _num_equal(old.get(field), new.get(field), 1e-6):
            entry.append(field)
    for field in ("status", "exit_signal_date", "exit_date"):
        if old.get(field) != new.get(field):
            exit_fields.append(field)
    if not _num_equal(old.get("exit_price_raw"), new.get("exit_price_raw"), 1e-6):
        exit_fields.append("exit_price_raw")
    if not _num_equal(old.get("net_pnl"), new.get("net_pnl"), 0.01):
        exit_fields.append("net_pnl")
    return entry, exit_fields


def compare_trades(old_rows: list[dict[str, Any]], new_rows: list[dict[str, Any]]) -> dict[str, Any]:
    old_by_key = {_trade_key(row): row for row in old_rows}
    new_by_key = {_trade_key(row): row for row in new_rows}
    if len(old_by_key) != len(old_rows) or len(new_by_key) != len(new_rows):
        raise ValueError("duplicate mother-trade signal key")
    details = []
    for key in sorted(set(old_by_key) | set(new_by_key)):
        old, new = old_by_key.get(key), new_by_key.get(key)
        if old is None:
            category, entry, exit_fields = "NEW_TRADE_ONLY", [], []
        elif new is None:
            category, entry, exit_fields = "OLD_TRADE_ONLY", [], []
        else:
            entry, exit_fields = _trade_differences(old, new)
            category = "EXECUTION_EXACT" if not entry and not exit_fields else "ENTRY_DIFF" if entry else "ENTRY_EXACT_EXIT_DIFF"
        details.append({"category": category, "key": list(key), "entry_differences": entry, "exit_differences": exit_fields, "old": old, "new": new})
    counts = Counter(row["category"] for row in details)
    common_count = len(set(old_by_key) & set(new_by_key))
    exact_count = counts["EXECUTION_EXACT"]
    return {
        "old_trade_count": len(old_rows),
        "new_trade_count": len(new_rows),
        "common_mother_signal_trade_count": common_count,
        "observed_record_exact_count": exact_count,
        "observed_record_exact_rate_pct": round(exact_count / common_count * 100, 4) if common_count else None,
        "observed_execution_parity_100": bool(common_count) and exact_count == common_count,
        "counts": dict(sorted(counts.items())),
        "details": details,
    }


def _summary(backtest: dict[str, Any], variant: str) -> dict[str, Any]:
    source = backtest["variants"][variant]["summary"]
    fields = (
        "trade_episodes", "closed", "open", "buy_fills", "realized_net_pnl",
        "unrealized_net_pnl_after_estimated_exit_cost", "net_pnl", "profit_factor",
        "median_return_pct", "average_mfe_pct", "average_mae_pct",
        "maximum_concurrent_stocks", "maximum_concurrent_tranches",
        "peak_concurrent_deployed_cash", "return_on_peak_capital_pct",
    )
    return {field: source.get(field) for field in fields}


def compare(
    old_ledger: Path,
    old_backtest: Path,
    new_ledger: Path,
    new_backtest: Path,
) -> dict[str, Any]:
    for path in (old_ledger, old_backtest, new_ledger, new_backtest):
        if not path.exists():
            raise FileNotFoundError(path)
    old_signals = load_old_signals(old_ledger)
    new_signals = load_new_signals(new_ledger)
    old_bt, new_bt = _json(old_backtest), _json(new_backtest)
    old_trades = _episodes(old_bt, "V3_MOTHER_ONLY_10K", "PURE_AI_REFERENCE")
    new_trades = _episodes(new_bt, "V3_FIXED_10K", "HYBRID_V3")
    return {
        "audit_version": "hybrid-v3-behavioral-equivalence-v1",
        "reference_is_not_ground_truth": True,
        "causal_note": "Reference decisions and performance are read only after the frozen hybrid semantic ledger and replay have completed.",
        "inputs": {"old_ledger": str(old_ledger.resolve()), "old_backtest": str(old_backtest.resolve()), "new_ledger": str(new_ledger.resolve()), "new_backtest": str(new_backtest.resolve())},
        "signal_comparison": compare_signals(old_signals, new_signals),
        "fixed_trade_comparison": compare_trades(old_trades, new_trades),
        "performance_reference": {
            "pure_ai_fixed": _summary(old_bt, "V3_MOTHER_ONLY_10K"),
            "hybrid_v3_fixed": _summary(new_bt, "V3_FIXED_10K"),
        },
    }


def _fmt(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:,.4f}"
    return str(value)


def write_report(path: Path, result: dict[str, Any]) -> None:
    signals = result["signal_comparison"]
    trades = result["fixed_trade_comparison"]
    lines = [
        "# 混合V3與純AI V3行為等價稽核", "",
        "> 純AI結果是參考基準，不是假定正確的標準答案。本報告在混合V3語意帳本鎖定及交易回放完成後才讀取舊結果，不會回饋改寫新V3。", "",
        "## 訊號層", "",
        "| 指標 | 結果 |", "|---|---:|",
        f"| 純AI訊號 | {signals['old_signal_count']} |",
        f"| 混合V3訊號 | {signals['new_signal_count']} |",
        f"| 同股同日皆觸發 | {signals['same_day_action_count']} |",
        f"| 舊訊號同日保留率 | {_fmt(signals['same_day_action_recall_vs_old_pct'])}% |",
        f"| 新訊號對舊同日重合率 | {_fmt(signals['same_day_action_precision_vs_old_pct'])}% |",
        f"| 同日訊號細節完全一致率 | {_fmt(signals['exact_detail_rate_within_same_day_pct'])}% |", "",
        "### 訊號分類", "", "| 類別 | 筆數 |", "|---|---:|",
    ]
    lines.extend(f"| `{key}` | {value} |" for key, value in signals["counts"].items())
    lines.extend(["", "## 固定一萬元交易層", "", "| 指標 | 結果 |", "|---|---:|",
                  f"| 純AI交易 | {trades['old_trade_count']} |",
                  f"| 混合V3交易 | {trades['new_trade_count']} |",
                  f"| 共同母單訊號交易 | {trades['common_mother_signal_trade_count']} |",
                  f"| 逐欄完全一致 | {trades['observed_record_exact_count']} |",
                  f"| 逐欄完全一致率 | {_fmt(trades['observed_record_exact_rate_pct'])}% |",
                  f"| 觀察到的交易紀錄達100%一致 | {'是' if trades['observed_execution_parity_100'] else '否'} |", "",
                  "### 交易分類", "", "| 類別 | 筆數 |", "|---|---:|"])
    lines.extend(f"| `{key}` | {value} |" for key, value in trades["counts"].items())
    lines.extend(["", "## 績效參考", "", "| 指標 | 純AI固定版 | 混合V3固定版 |", "|---|---:|---:|"])
    old_perf = result["performance_reference"]["pure_ai_fixed"]
    new_perf = result["performance_reference"]["hybrid_v3_fixed"]
    for field in old_perf:
        lines.append(f"| `{field}` | {_fmt(old_perf[field])} | {_fmt(new_perf[field])} |")
    lines.extend(["", "## 訊號差異逐筆", "", "| 類別 | 股票 | 舊日期 | 新日期 | 差異欄位 |", "|---|---|---|---|---|"])
    for row in signals["details"]:
        old, new = row.get("old") or {}, row.get("new") or {}
        code = old.get("code") or new.get("code")
        name = old.get("name") or new.get("name") or ""
        differences = "、".join(row.get("differences") or []) or "—"
        lines.append(f"| `{row['category']}` | {code} {name} | {old.get('signal_date') or '—'} | {new.get('signal_date') or '—'} | {differences} |")
    lines.extend(["", "## 交易差異逐筆", "", "| 類別 | 股票 | 訊號日 | 進場差異 | 出場／損益差異 |", "|---|---|---|---|---|"])
    for row in trades["details"]:
        old, new = row.get("old") or {}, row.get("new") or {}
        code = old.get("code") or new.get("code")
        name = old.get("name") or new.get("name") or ""
        lines.append(f"| `{row['category']}` | {code} {name} | {old.get('signal_date') or new.get('signal_date')} | {'、'.join(row['entry_differences']) or '—'} | {'、'.join(row['exit_differences']) or '—'} |")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--old-ledger", type=Path, default=DEFAULT_OLD_LEDGER)
    parser.add_argument("--old-backtest", type=Path, default=DEFAULT_OLD_BACKTEST)
    parser.add_argument("--new-ledger", type=Path, default=DEFAULT_NEW_LEDGER)
    parser.add_argument("--new-backtest", type=Path, default=DEFAULT_NEW_BACKTEST)
    parser.add_argument("--output-json", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--output-md", type=Path, default=DEFAULT_MD)
    args = parser.parse_args(list(argv) if argv is not None else None)
    result = compare(args.old_ledger, args.old_backtest, args.new_ledger, args.new_backtest)
    _write_json(args.output_json, result)
    write_report(args.output_md, result)
    print(json.dumps({"signal_comparison": {key: value for key, value in result["signal_comparison"].items() if key != "details"}, "fixed_trade_comparison": {key: value for key, value in result["fixed_trade_comparison"].items() if key != "details"}}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
