"""Causal daily-K replay of radar candidates through the enlightenment layer.

The script deliberately keeps the upstream A/B/C/D stock-selection strategies
separate from the downstream course rules.  A candidate enters monitoring only
after the saved radar report's session has closed.  No same-session entry is
allowed and every feature snapshot is calculated from bars available on that
date.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.course_daily_screen_trial import (  # noqa: E402
    THRESHOLDS,
    TrialThresholds,
    _add_indicators,
    _pivot_structure,
    _quadrant,
    _resolve_direction,
    _setup_pattern,
    _sstv,
    _taiji,
)


METHOD_VERSION = "course-watchlist-backtest-v1-close-confirmed"
RADAR_LINE_RE = re.compile(
    r"(?m)^\s*\d+\.\s+(?P<code>\d{4,6})\s+(?P<name>[^｜\r\n]+?)"
    r"｜(?P<score>\d+)分｜技術策略：(?P<strategies>[^｜\r\n]+)"
)
STRATEGY_CODE_RE = re.compile(r"(?:^|、)([A-D])（")


def parse_radar_summary(path: Path, report_date: date) -> list[dict[str, Any]]:
    """Parse the complete persisted candidate list without loading huge JSON."""
    text = path.read_text(encoding="utf-8-sig")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for match in RADAR_LINE_RE.finditer(text):
        values = match.groupdict()
        code = values["code"]
        if code in seen:
            continue
        seen.add(code)
        rows.append(
            {
                "date": report_date,
                "code": code,
                "name": values["name"].strip(),
                "score": int(values["score"]),
                "strategy_codes": STRATEGY_CODE_RE.findall(values["strategies"]),
                "strategy_text": values["strategies"].strip(),
                "source_path": str(path.resolve()),
            }
        )
    return rows


def discover_selections(start: date, end: date) -> tuple[dict[date, list[dict[str, Any]]], list[dict[str, Any]]]:
    by_date: dict[date, list[dict[str, Any]]] = {}
    source_rows: list[dict[str, Any]] = []
    radar_root = ROOT / "reports" / "radar"
    for day_dir in sorted(radar_root.iterdir() if radar_root.exists() else []):
        if not day_dir.is_dir():
            continue
        try:
            report_date = date.fromisoformat(day_dir.name)
        except ValueError:
            continue
        if report_date < start or report_date > end:
            continue
        summaries = sorted(day_dir.glob("radar_*/radar_summary.md"))
        if not summaries:
            continue
        path = summaries[-1]
        rows = parse_radar_summary(path, report_date)
        if not rows:
            continue
        by_date[report_date] = rows
        source_rows.append(
            {
                "date": report_date.isoformat(),
                "candidate_count": len(rows),
                "path": str(path.resolve()),
            }
        )
    return by_date, source_rows


def load_stock_map() -> dict[str, dict[str, Any]]:
    payload = json.loads((ROOT / "stock_list.json").read_text(encoding="utf-8-sig"))
    return {
        str(item.get("code") or ""): item
        for item in payload.get("stocks", [])
        if isinstance(item, dict) and item.get("code")
    }


def resolve_stock(code: str, name: str, stock_map: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """Resolve delisted/renamed names from an unambiguous persisted price cache."""
    stock = stock_map.get(code)
    if stock is not None:
        return stock
    matches = sorted((ROOT / ".cache" / "technical_daily").glob(f"{code}_*.csv"))
    if len(matches) != 1:
        return None
    suffix = matches[0].stem[len(code) + 1 :]
    market = {"TW": "TWSE", "TWO": "TPEX"}.get(suffix)
    if market is None:
        return None
    return {"code": code, "name": name, "symbol": f"{code}.{suffix}", "market": market}


def load_full_frame(symbol: str, as_of: date) -> tuple[pd.DataFrame, Path]:
    path = ROOT / ".cache" / "technical_daily" / f"{symbol.replace('.', '_')}.csv"
    if not path.exists():
        raise FileNotFoundError(path)
    frame = pd.read_csv(path)
    required = {"date", "open", "high", "low", "close", "volume"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"{path.name} missing columns: {sorted(missing)}")
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    for column in ("open", "high", "low", "close", "volume"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = (
        frame.dropna(subset=["date", "open", "high", "low", "close"])
        .sort_values("date")
        .drop_duplicates("date", keep="last")
    )
    frame = frame[frame["date"].dt.date <= as_of].reset_index(drop=True)
    if frame.empty:
        raise ValueError(f"{symbol} has no bars through {as_of.isoformat()}")
    return _add_indicators(frame), path


def enlightenment_snapshot(frame: pd.DataFrame, thresholds: TrialThresholds = THRESHOLDS) -> dict[str, Any]:
    """Return one causal enlightenment-layer state from a truncated frame."""
    if len(frame) < 145:
        raise ValueError(f"only {len(frame)} daily bars; 145 required")
    structure = _pivot_structure(frame, thresholds)
    direction = _resolve_direction(frame, structure)
    quadrant = _quadrant(frame, thresholds)
    setup = _setup_pattern(frame, direction, thresholds)
    sstv = _sstv(frame, direction)
    taiji = _taiji(frame, direction, thresholds)
    latest = frame.iloc[-1]
    close = float(latest["close"])
    atr = float(latest["ATR14"])
    defense_record = structure.get("defense")
    defense = None if not defense_record else float(defense_record["price"])
    risk_pct = None if defense is None else max(0.0, (close - defense) / close * 100.0)
    risk_atr = None if defense is None or atr <= 0 else max(0.0, (close - defense) / atr)
    extension_atr = 0.0 if atr <= 0 else max(0.0, (close - float(latest["MA21"])) / atr)
    signal_range_atr = 0.0 if atr <= 0 else float((latest["high"] - latest["low"]) / atr)
    risk_manageable = bool(
        risk_pct is not None
        and risk_atr is not None
        and risk_pct <= thresholds.risk_max_pct
        and risk_atr <= thresholds.risk_max_atr
    )
    no_chase = bool(
        extension_atr > thresholds.no_chase_extension_atr
        or signal_range_atr > thresholds.no_chase_signal_range_atr
        or (risk_pct is not None and risk_pct > thresholds.no_chase_risk_pct)
        or (risk_atr is not None and risk_atr > thresholds.no_chase_risk_atr)
    )
    structural_invalid = bool(
        direction["relation"] == "空方共振"
        or (taiji["state"] == "CORRECTION_FAILED" and taiji["direction"] == "BULL")
        or (
            direction["large"] == "BEAR"
            and close < float(latest["MA55"])
            and structure["small_dow"] == "BEAR"
        )
    )
    taiji_primary = taiji["sequence"] in {"LEG_2", "LEG_3", "LEG_4"}
    taiji_blocks = taiji["state"] in {"COPY_FAILED", "CORRECTION_FAILED", "POST_5"}
    structural_lens = "TAIJI_PRIMARY" if taiji_primary else "QUADRANT_PRIMARY"
    structural_lens_ok = bool(
        taiji["entry_support"] if taiji_primary else quadrant["working_quadrant"] in {"Q1", "Q4"}
    )
    checks = {
        "direction": direction["large"] == "BULL" and direction["small"] != "BEAR",
        "structural_lens": structural_lens_ok,
        "taiji_not_failed": not taiji_blocks,
        "setup": bool(setup["confirmed"]),
        "sstv": sstv["quality"] != "不合格",
        "risk": risk_manageable,
        "not_no_chase": not no_chase,
        "not_invalid": not structural_invalid,
    }
    armed = all(checks.values())
    return {
        "date": latest["date"].date().isoformat(),
        "close": round(close, 4),
        "atr": round(atr, 4),
        "structure": structure,
        "direction": direction,
        "quadrant": quadrant,
        "setup": setup,
        "sstv": sstv,
        "taiji": taiji,
        "structural_lens": structural_lens,
        "checks": checks,
        "armed": armed,
        "structural_invalid": structural_invalid,
        "risk": {
            "defense": None if defense is None else round(defense, 4),
            "distance_pct": None if risk_pct is None else round(risk_pct, 4),
            "distance_atr": None if risk_atr is None else round(risk_atr, 4),
            "extension_atr": round(extension_atr, 4),
            "signal_range_atr": round(signal_range_atr, 4),
            "chase_cap": round(close + thresholds.next_session_chase_cap_atr * atr, 4),
        },
    }


def _selection_index(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        result[row["code"]].append(row)
    return result


def _pct(numerator: float, denominator: float) -> float:
    return round((numerator / denominator - 1.0) * 100.0, 4)


def replay_stock(
    *,
    stock: dict[str, Any],
    selections: list[dict[str, Any]],
    as_of: date,
    max_monitor_bars: int,
    thresholds: TrialThresholds = THRESHOLDS,
) -> dict[str, Any]:
    """Replay a stock until its first executable entry, then track the trade."""
    code = str(stock["code"])
    name = str(stock.get("name") or selections[0]["name"])
    frame, source_path = load_full_frame(str(stock["symbol"]), as_of)
    date_to_position = {value.date(): index for index, value in enumerate(frame["date"])}
    usable_selections = [row for row in selections if row["date"] in date_to_position]
    if not usable_selections:
        raise ValueError("no selection date has a matching market bar")
    selected_by_date = {row["date"]: row for row in usable_selections}
    start_position = min(date_to_position[row["date"]] for row in usable_selections)
    end_positions = [index for index, value in enumerate(frame["date"]) if value.date() <= as_of]
    end_position = end_positions[-1]

    active = False
    added_date: date | None = None
    last_selected_position: int | None = None
    episode_count = 0
    invalidations: list[str] = []
    armed_events: list[dict[str, Any]] = []
    rejected_confirmations = Counter()
    pending_plan: dict[str, Any] | None = None
    pending_entry: dict[str, Any] | None = None
    confirmation: dict[str, Any] | None = None
    trade: dict[str, Any] | None = None
    final_monitor_state = "NOT_TRIGGERED"
    last_snapshot: dict[str, Any] | None = None

    for position in range(start_position, end_position + 1):
        row = frame.iloc[position]
        bar_date = row["date"].date()

        # A confirmed signal is only executed on the following session open.
        if pending_entry is not None and trade is None:
            entry_cap = float(pending_entry["entry_cap"])
            defense = float(pending_entry["defense"])
            open_price = float(row["open"])
            if open_price > entry_cap:
                rejected_confirmations["open_above_chase_cap"] += 1
                pending_entry = None
                confirmation = None
            elif open_price <= defense:
                rejected_confirmations["open_below_defense"] += 1
                pending_entry = None
                confirmation = None
            else:
                risk_per_share = open_price - defense
                trade = {
                    "entry_date": bar_date.isoformat(),
                    "entry_price": round(open_price, 4),
                    "defense": round(defense, 4),
                    "initial_risk_pct": round(risk_per_share / open_price * 100.0, 4),
                    "armed_date": pending_entry["armed_date"],
                    "confirmation_date": pending_entry["confirmation_date"],
                    "pattern": pending_entry["pattern"],
                    "structural_lens": pending_entry["structural_lens"],
                    "taiji_sequence": pending_entry["taiji_sequence"],
                    "taiji_state": pending_entry["taiji_state"],
                    "source_strategy_codes": sorted(
                        {
                            strategy
                            for selection in usable_selections
                            if selection["date"] <= bar_date
                            for strategy in selection["strategy_codes"]
                        }
                    ),
                    "status": "OPEN",
                    "exit_signal_date": None,
                    "exit_date": None,
                    "exit_price": None,
                }
                pending_entry = None
                final_monitor_state = "TRIGGERED"

        # Execute a close-confirmed defense exit at the next session open.
        if trade is not None and trade.get("exit_pending"):
            trade["exit_date"] = bar_date.isoformat()
            trade["exit_price"] = round(float(row["open"]), 4)
            trade["status"] = "CLOSED_DEFENSE"
            trade.pop("exit_pending", None)

        selection = selected_by_date.get(bar_date)
        if selection is not None and trade is None:
            if not active:
                active = True
                added_date = bar_date
                episode_count += 1
            last_selected_position = position

        if trade is not None:
            if trade["status"] == "OPEN" and float(row["close"]) < float(trade["defense"]):
                trade["exit_signal_date"] = bar_date.isoformat()
                if position < end_position:
                    trade["exit_pending"] = True
                else:
                    trade["status"] = "EXIT_PENDING"
            if trade["status"] == "CLOSED_DEFENSE":
                break
            continue

        if not active:
            continue
        if last_selected_position is not None and position - last_selected_position > max_monitor_bars:
            active = False
            pending_plan = None
            final_monitor_state = "EXPIRED"
            continue

        snapshot = enlightenment_snapshot(frame.iloc[: position + 1].copy(), thresholds)
        last_snapshot = snapshot

        # Resolve yesterday's ARMED plan with today's close and today's causal
        # structural state.  A broken structure cancels the confirmation.
        if pending_plan is not None:
            close = float(row["close"])
            if snapshot["structural_invalid"]:
                rejected_confirmations["structure_invalid"] += 1
            elif close < float(pending_plan["trigger"]):
                rejected_confirmations["close_below_trigger"] += 1
            elif close > float(pending_plan["chase_cap"]):
                rejected_confirmations["close_above_chase_cap"] += 1
            elif close <= float(pending_plan["defense"]):
                rejected_confirmations["close_below_defense"] += 1
            else:
                confirmation = {
                    "date": bar_date.isoformat(),
                    "close": round(close, 4),
                    "armed_date": pending_plan["armed_date"],
                    "trigger": pending_plan["trigger"],
                }
                pending_entry = {
                    **pending_plan,
                    "confirmation_date": bar_date.isoformat(),
                    "confirmation_close": round(close, 4),
                    "entry_cap": round(
                        close + thresholds.next_session_chase_cap_atr * float(row["ATR14"]), 4
                    ),
                }
                final_monitor_state = "CONFIRMED_PENDING_ENTRY"
            pending_plan = None

        if snapshot["structural_invalid"]:
            invalidations.append(bar_date.isoformat())
            active = False
            pending_plan = None
            final_monitor_state = "INVALIDATED"
            # A fresh same-day upstream selection starts a new episode after
            # this close, but cannot reuse the invalid snapshot as an entry.
            if selection is not None:
                active = True
                added_date = bar_date
                last_selected_position = position
                episode_count += 1
            continue

        if pending_entry is None and snapshot["armed"]:
            plan = {
                "armed_date": bar_date.isoformat(),
                "trigger": float(snapshot["setup"]["trigger_price"]),
                "chase_cap": float(snapshot["risk"]["chase_cap"]),
                "defense": float(snapshot["risk"]["defense"]),
                "pattern": snapshot["setup"]["pattern_code"],
                "structural_lens": snapshot["structural_lens"],
                "taiji_sequence": snapshot["taiji"]["sequence"],
                "taiji_state": snapshot["taiji"]["state"],
            }
            pending_plan = plan
            armed_events.append(plan)
            final_monitor_state = "ARMED"
        elif pending_entry is None and final_monitor_state not in {"INVALIDATED", "EXPIRED"}:
            final_monitor_state = "FORMING"

    result: dict[str, Any] = {
        "code": code,
        "name": name,
        "symbol": stock["symbol"],
        "market": stock.get("market"),
        "source_path": str(source_path.resolve()),
        "first_selected_date": min(row["date"] for row in usable_selections).isoformat(),
        "last_selected_date": max(row["date"] for row in usable_selections).isoformat(),
        "selection_count": len(usable_selections),
        "selection_scores": [row["score"] for row in usable_selections],
        "strategy_codes": sorted(
            {strategy for row in usable_selections for strategy in row["strategy_codes"]}
        ),
        "episode_count": episode_count,
        "invalidations": invalidations,
        "armed_count": len(armed_events),
        "first_armed": None if not armed_events else armed_events[0],
        "last_armed": None if not armed_events else armed_events[-1],
        "confirmation": confirmation,
        "rejected_confirmations": dict(rejected_confirmations),
        "final_monitor_state": final_monitor_state,
        "last_snapshot": last_snapshot,
        "trade": trade,
    }
    if trade is not None:
        entry_position = date_to_position[date.fromisoformat(trade["entry_date"])]
        if trade["exit_date"]:
            mark_position = date_to_position[date.fromisoformat(trade["exit_date"])]
            mark_price = float(trade["exit_price"])
            performance_date = trade["exit_date"]
        else:
            mark_position = end_position
            mark_price = float(frame.iloc[end_position]["close"])
            performance_date = frame.iloc[end_position]["date"].date().isoformat()
        observed = frame.iloc[entry_position : mark_position + 1]
        entry_price = float(trade["entry_price"])
        risk_per_share = entry_price - float(trade["defense"])
        lifecycle_return = _pct(mark_price, entry_price)
        trade["performance_date"] = performance_date
        trade["mark_or_exit_price"] = round(mark_price, 4)
        trade["return_pct"] = lifecycle_return
        trade["mfe_pct"] = _pct(float(observed["high"].max()), entry_price)
        trade["mae_pct"] = _pct(float(observed["low"].min()), entry_price)
        trade["return_r"] = None if risk_per_share <= 0 else round((mark_price - entry_price) / risk_per_share, 4)
        trade["mfe_r"] = None if risk_per_share <= 0 else round(
            (float(observed["high"].max()) - entry_price) / risk_per_share, 4
        )
        trade["mae_r"] = None if risk_per_share <= 0 else round(
            (float(observed["low"].min()) - entry_price) / risk_per_share, 4
        )
    return result


def _safe_mean(values: list[float]) -> float | None:
    return None if not values else round(statistics.fmean(values), 4)


def _safe_median(values: list[float]) -> float | None:
    return None if not values else round(statistics.median(values), 4)


def summarize(results: list[dict[str, Any]], errors: list[dict[str, str]], selection_rows: list[dict[str, Any]]) -> dict[str, Any]:
    trades = [row["trade"] for row in results if row["trade"] is not None]
    returns = [float(trade["return_pct"]) for trade in trades]
    mfes = [float(trade["mfe_pct"]) for trade in trades]
    maes = [float(trade["mae_pct"]) for trade in trades]
    strategy_selection_counts = Counter(
        strategy for row in selection_rows for strategy in row["strategy_codes"]
    )
    strategy_trade_counts = Counter(
        strategy for trade in trades for strategy in trade["source_strategy_codes"]
    )
    return {
        "selection_rows": len(selection_rows),
        "unique_candidates": len({row["code"] for row in selection_rows}),
        "analyzable_candidates": len(results),
        "data_error_candidates": len(errors),
        "armed_candidates": sum(row["armed_count"] > 0 for row in results),
        "confirmed_candidates": sum(row["confirmation"] is not None for row in results),
        "executed_trades": len(trades),
        "open_trades": sum(trade["status"] == "OPEN" for trade in trades),
        "exit_pending_trades": sum(trade["status"] == "EXIT_PENDING" for trade in trades),
        "closed_defense_trades": sum(trade["status"] == "CLOSED_DEFENSE" for trade in trades),
        "positive_trades": sum(value > 0 for value in returns),
        "non_positive_trades": sum(value <= 0 for value in returns),
        "win_rate_pct": None if not returns else round(sum(value > 0 for value in returns) / len(returns) * 100.0, 2),
        "equal_weight_mean_return_pct": _safe_mean(returns),
        "median_return_pct": _safe_median(returns),
        "mean_mfe_pct": _safe_mean(mfes),
        "mean_mae_pct": _safe_mean(maes),
        "monitor_state_counts": dict(Counter(row["final_monitor_state"] for row in results)),
        "strategy_selection_row_counts": dict(sorted(strategy_selection_counts.items())),
        "strategy_executed_trade_counts_multilabel": dict(sorted(strategy_trade_counts.items())),
    }


def _f(value: Any, digits: int = 2) -> str:
    if value is None:
        return "—"
    return f"{float(value):.{digits}f}"


def render_markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    trades = [row for row in payload["candidates"] if row["trade"] is not None]
    pending = [
        row
        for row in payload["candidates"]
        if row["trade"] is None and row["final_monitor_state"] == "CONFIRMED_PENDING_ENTRY"
    ]
    lines = [
        f"# 啟蒙層監控清單四週回測｜截至 {payload['as_of']}",
        "",
        "> 研究用途；不是投資建議。這是固定規則的因果日 K 回放，不是事後看圖挑案例。報酬未扣手續費、交易稅與滑價。",
        "",
        "## 結論",
        "",
        f"- 來源期間：{payload['selection_window']['start']}～{payload['selection_window']['end']}，保存雷達 {payload['source_coverage']['report_days']} 日；選股列 {summary['selection_rows']} 筆、去重 {summary['unique_candidates']} 檔。",
        f"- 可分析 {summary['analyzable_candidates']} 檔；資料不足／對照失敗 {summary['data_error_candidates']} 檔。",
        f"- 曾進入 ARMED：{summary['armed_candidates']} 檔；完成下一根日 K 收盤確認：{summary['confirmed_candidates']} 檔；實際有下一交易日開盤可計入：{summary['executed_trades']} 筆。",
        f"- 截至回放終點：未停損仍開放 {summary['open_trades']} 筆、等待防線退出 {summary['exit_pending_trades']} 筆、已依防線退出 {summary['closed_defense_trades']} 筆。",
        f"- 正報酬 {summary['positive_trades']} 筆，非正報酬 {summary['non_positive_trades']} 筆，命中率 {_f(summary['win_rate_pct'])}%。",
        f"- 等權平均報酬 {_f(summary['equal_weight_mean_return_pct'])}%，中位數 {_f(summary['median_return_pct'])}%；平均 MFE {_f(summary['mean_mfe_pct'])}%，平均 MAE {_f(summary['mean_mae_pct'])}%。",
        "",
        "`命中率` 只表示截至 9/4 或防線退出時報酬大於 0 的比例，不是策略長期勝率。多策略標籤可重複計數。",
        "",
        "## 已觸發並完成入場",
        "",
        "| 股票 | 首次入選 | 策略 | ARMED | 收盤確認 | 入場 | 狀態 | 入場價 | 防線 | 截止／退出價 | 報酬% | MFE% | MAE% | R | 型態 |",
        "|---|---|---|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in sorted(trades, key=lambda item: item["trade"]["entry_date"]):
        trade = row["trade"]
        lines.append(
            "| "
            + " | ".join(
                [
                    f"{row['code']} {row['name']}",
                    row["first_selected_date"],
                    ",".join(trade["source_strategy_codes"]) or "—",
                    trade["armed_date"],
                    trade["confirmation_date"],
                    trade["entry_date"],
                    trade["status"],
                    _f(trade["entry_price"]),
                    _f(trade["defense"]),
                    _f(trade["mark_or_exit_price"]),
                    _f(trade["return_pct"]),
                    _f(trade["mfe_pct"]),
                    _f(trade["mae_pct"]),
                    _f(trade["return_r"]),
                    trade["pattern"],
                ]
            )
            + " |"
        )
    if not trades:
        lines.append("| — | — | — | — | — | — | — | — | — | — | — | — | — | — | — |")

    lines += ["", "## 已收盤確認但尚無下一交易日可入場", ""]
    if pending:
        lines += [
            "| 股票 | 首次入選 | ARMED | 確認日 | 確認收盤 |",
            "|---|---|---|---|---:|",
        ]
        for row in pending:
            confirmation = row["confirmation"]
            lines.append(
                f"| {row['code']} {row['name']} | {row['first_selected_date']} | "
                f"{confirmation['armed_date']} | {confirmation['date']} | {_f(confirmation['close'])} |"
            )
    else:
        lines.append("無。")

    lines += [
        "",
        "## 方法與限制",
        "",
        "- 上游只用當日已保存的雷達完整候選，保留 A/B/C/D 原策略標籤；Elson、老蕭不作為啟蒙層的進場門檻。",
        "- 啟蒙層為同一個決策：確認樞紐與大小方向、四象限；若太極處第 2～4 段則由太極主判；再檢查日 K 型態、SSTV、樞紐防線（10% 且 2.5 ATR 內）與不可追價。太極不是獨立投票。",
        "- 入選日收盤後才加入；ARMED 後下一根日 K 必須收在觸發價之上且不高於追價上限，再下一交易日開盤不跳過新上限才算入場。每檔只統計第一筆可執行交易。",
        f"- 未再次入選的候選最多續看 {payload['parameters']['max_monitor_bars']} 根交易日；完全走空可先失效，日後若再次入選才重建案件。",
        "- 出場只回放目前已明文化的樞紐防線：日 K 收盤跌破後，下一交易日開盤退出；尚未納入移動停利、分批出場與基本面事件。",
        "- 8/21 沒有保存雷達報告；9/4 尚無當日雷達，因此選股來源截至 9/3。9/4 只用來確認訊號與計算績效。",
        "- 本次是機械化代理規則；未來 AI 圖形判讀若加入，應與本結果做盲測差異，而不能事後改規則配合答案。",
        "",
        "## 稽核資訊",
        "",
        f"- 方法版本：`{payload['method_version']}`",
        f"- 參數：`{json.dumps(payload['parameters'], ensure_ascii=False, sort_keys=True)}`",
        f"- 各策略入選列數：`{json.dumps(summary['strategy_selection_row_counts'], ensure_ascii=False, sort_keys=True)}`",
        f"- 各策略觸發交易數（多標籤）：`{json.dumps(summary['strategy_executed_trade_counts_multilabel'], ensure_ascii=False, sort_keys=True)}`",
        f"- 監控終態：`{json.dumps(summary['monitor_state_counts'], ensure_ascii=False, sort_keys=True)}`",
    ]
    if payload["errors"]:
        lines += ["", "## 資料錯誤", "", "| 股票 | 原因 |", "|---|---|"]
        for error in payload["errors"]:
            lines.append(f"| {error['code']} {error['name']} | {error['error'].replace('|', '/')} |")
    return "\n".join(lines) + "\n"


def build_payload(start: date, selection_end: date, as_of: date, max_monitor_bars: int) -> dict[str, Any]:
    by_date, source_rows = discover_selections(start, selection_end)
    selection_rows = [row for report_date in sorted(by_date) for row in by_date[report_date]]
    if not selection_rows:
        raise ValueError("no persisted radar candidates found in requested window")
    stock_map = load_stock_map()
    by_code = _selection_index(selection_rows)
    results: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for code in sorted(by_code):
        stock = resolve_stock(code, by_code[code][0]["name"], stock_map)
        if stock is None:
            errors.append({"code": code, "name": by_code[code][0]["name"], "error": "missing stock_list mapping"})
            continue
        try:
            results.append(
                replay_stock(
                    stock=stock,
                    selections=by_code[code],
                    as_of=as_of,
                    max_monitor_bars=max_monitor_bars,
                )
            )
        except Exception as exc:  # keep the audit list complete
            errors.append({"code": code, "name": by_code[code][0]["name"], "error": str(exc)})
    summary = summarize(results, errors, selection_rows)
    return {
        "method_version": METHOD_VERSION,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "selection_window": {"start": start.isoformat(), "end": selection_end.isoformat()},
        "as_of": as_of.isoformat(),
        "source_coverage": {
            "report_days": len(source_rows),
            "available_dates": [row["date"] for row in source_rows],
            "known_missing_dates": ["2026-08-21", "2026-09-04"],
            "reports": source_rows,
        },
        "parameters": {
            **asdict(THRESHOLDS),
            "max_monitor_bars": max_monitor_bars,
            "entry_model": "armed_close_then_next_close_confirmation_then_following_open",
            "exit_model": "close_below_fixed_pivot_defense_then_following_open",
            "one_trade_per_stock": True,
            "costs_included": False,
            "elson_laoxiao_entry_gates": False,
        },
        "summary": summary,
        "candidates": sorted(results, key=lambda row: (row["first_selected_date"], row["code"])),
        "errors": errors,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2026-08-07")
    parser.add_argument("--selection-end", default="2026-09-03")
    parser.add_argument("--as-of", default="2026-09-04")
    parser.add_argument("--max-monitor-bars", type=int, default=20)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "reports" / "course_backtest" / "2026-09-04" / "four_week_v1",
    )
    args = parser.parse_args()
    payload = build_payload(
        date.fromisoformat(args.start),
        date.fromisoformat(args.selection_end),
        date.fromisoformat(args.as_of),
        args.max_monitor_bars,
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.output_dir / "backtest.json"
    markdown_path = args.output_dir / "backtest.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path.write_text(render_markdown(payload), encoding="utf-8")
    print(json.dumps(payload["summary"], ensure_ascii=False, indent=2))
    print(markdown_path.resolve())


if __name__ == "__main__":
    main()
