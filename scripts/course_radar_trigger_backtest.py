"""Replay every retained non-empty radar candidate through the enlightenment layer.

Selection is known after the radar session closes.  An ARMED plan may trigger
from the next session onward.  TRIGGERED means an immediate simulated fill;
there is no additional close-confirmation delay.
"""
from __future__ import annotations

import argparse
import json
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

from scripts.course_daily_screen_trial import THRESHOLDS, TrialThresholds  # noqa: E402
from scripts.course_watchlist_backtest import (  # noqa: E402
    enlightenment_snapshot,
    load_full_frame,
    load_stock_map,
    resolve_stock,
)


METHOD_VERSION = "course-radar-trigger-backtest-v2-intraday-trigger"
RADAR_ROW_RE = re.compile(
    r"(?m)^\s*\d+\.\s+(?P<code>\d{4,6})\s+(?P<name>[^｜\r\n]+?)"
    r"｜(?P<score>\d+)分｜(?P<detail>[^\r\n]+)"
)
SOURCE_RE = re.compile(r"(?m)^來源：(?P<source>[^｜\r\n]+)")
MODERN_STRATEGY_RE = re.compile(r"([A-D])（")
LEGACY_STRATEGY_RE = re.compile(r"策略\s+([A-D](?:/[A-D])*)")

STATUS_LABELS = {
    "FORMING": "FORMING（條件形成中）",
    "ARMED": "ARMED（已建立進場計畫）",
    "TRIGGERED": "TRIGGERED（已觸發進場）",
    "OPEN": "OPEN（持有中）",
    "NO_CHASE": "NO_CHASE（不可追價）",
    "INVALIDATED": "INVALIDATED（結構失效）",
    "EXPIRED": "EXPIRED（監控逾期）",
    "AMBIGUOUS": "AMBIGUOUS（同日觸發與破防，順序不明）",
    "EXIT_TRIGGERED": "EXIT_TRIGGERED（已觸發出場）",
    "CLOSED": "CLOSED（交易已結束）",
}


def status_label(status: str) -> str:
    return STATUS_LABELS.get(status, f"{status}（未定義）")


def parse_radar_artifact(path: Path, report_date: date) -> tuple[str, list[dict[str, Any]]]:
    text = path.read_text(encoding="utf-8-sig")
    source_match = SOURCE_RE.search(text)
    source = source_match.group("source").strip() if source_match else "雷達來源未標示"
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for match in RADAR_ROW_RE.finditer(text):
        values = match.groupdict()
        code = values["code"]
        if code in seen:
            continue
        seen.add(code)
        detail = values["detail"].strip()
        strategies = set(MODERN_STRATEGY_RE.findall(detail))
        legacy = LEGACY_STRATEGY_RE.search(detail)
        if legacy:
            strategies.update(legacy.group(1).split("/"))
        rows.append(
            {
                "date": report_date,
                "code": code,
                "name": values["name"].strip(),
                "score": int(values["score"]),
                "strategy_codes": sorted(strategies),
                "source_labels": [source],
                "source_paths": [str(path.resolve())],
            }
        )
    return source, rows


def discover_radar_union(start: date, end: date) -> tuple[dict[date, list[dict[str, Any]]], list[dict[str, Any]]]:
    """Union every retained non-empty radar artifact within each report date."""
    radar_root = ROOT / "reports" / "radar"
    by_date: dict[date, list[dict[str, Any]]] = {}
    coverage: list[dict[str, Any]] = []
    for day_dir in sorted(radar_root.iterdir() if radar_root.exists() else []):
        if not day_dir.is_dir():
            continue
        try:
            report_date = date.fromisoformat(day_dir.name)
        except ValueError:
            continue
        if report_date < start or report_date > end:
            continue
        merged: dict[str, dict[str, Any]] = {}
        nonempty_files: list[dict[str, Any]] = []
        for path in sorted(day_dir.glob("radar_*/radar_summary.md")):
            source, rows = parse_radar_artifact(path, report_date)
            if not rows:
                continue
            nonempty_files.append(
                {"path": str(path.resolve()), "source": source, "candidate_count": len(rows)}
            )
            for row in rows:
                existing = merged.get(row["code"])
                if existing is None:
                    merged[row["code"]] = row
                    continue
                existing["score"] = max(int(existing["score"]), int(row["score"]))
                existing["strategy_codes"] = sorted(
                    set(existing["strategy_codes"]) | set(row["strategy_codes"])
                )
                existing["source_labels"] = sorted(
                    set(existing["source_labels"]) | set(row["source_labels"])
                )
                existing["source_paths"] = sorted(
                    set(existing["source_paths"]) | set(row["source_paths"])
                )
        if not merged:
            continue
        rows = sorted(merged.values(), key=lambda item: item["code"])
        by_date[report_date] = rows
        coverage.append(
            {
                "date": report_date.isoformat(),
                "union_candidate_count": len(rows),
                "nonempty_artifact_count": len(nonempty_files),
                "artifacts": nonempty_files,
            }
        )
    return by_date, coverage


def _index_selections(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        result[row["code"]].append(row)
    return result


def _return_pct(price: float, entry: float) -> float:
    return round((price / entry - 1.0) * 100.0, 4)


def _new_episode(selection: dict[str, Any], bar_date: date, position: int, sequence: int) -> dict[str, Any]:
    return {
        "sequence": sequence,
        "added_date": bar_date.isoformat(),
        "last_selected_date": bar_date.isoformat(),
        "last_selected_position": position,
        "selection_count": 1,
        "strategy_codes": set(selection["strategy_codes"]),
        "source_labels": set(selection["source_labels"]),
    }


def _merge_selection(episode: dict[str, Any], selection: dict[str, Any], bar_date: date, position: int) -> None:
    episode["last_selected_date"] = bar_date.isoformat()
    episode["last_selected_position"] = position
    episode["selection_count"] += 1
    episode["strategy_codes"].update(selection["strategy_codes"])
    episode["source_labels"].update(selection["source_labels"])


def _make_trade(
    *,
    code: str,
    name: str,
    episode: dict[str, Any],
    plan: dict[str, Any],
    bar_date: date,
    entry_price: float,
    entry_position: int,
) -> dict[str, Any]:
    defense = float(plan["defense"])
    return {
        "trade_id": f"{code}-{bar_date.isoformat()}-{episode['sequence']}",
        "code": code,
        "name": name,
        "episode_sequence": episode["sequence"],
        "monitor_added_date": episode["added_date"],
        "last_selected_date_before_entry": episode["last_selected_date"],
        "selection_count_before_entry": episode["selection_count"],
        "source_strategy_codes": sorted(episode["strategy_codes"]),
        "source_labels": sorted(episode["source_labels"]),
        "armed_date": plan["armed_date"],
        "trigger_date": bar_date.isoformat(),
        "entry_date": bar_date.isoformat(),
        "entry_price": round(entry_price, 4),
        "entry_position": entry_position,
        "trigger_price": round(float(plan["trigger"]), 4),
        "chase_cap": round(float(plan["chase_cap"]), 4),
        "defense": round(defense, 4),
        "initial_risk_pct": round((entry_price - defense) / entry_price * 100.0, 4),
        "pattern": plan["pattern"],
        "structural_lens": plan["structural_lens"],
        "taiji_sequence": plan["taiji_sequence"],
        "taiji_state": plan["taiji_state"],
        "status": "OPEN",
        "status_label": status_label("OPEN"),
        "exit_trigger_date": None,
        "exit_trigger_position": None,
        "exit_trigger_close": None,
        "exit_trigger_return_pct": None,
        "exit_date": None,
        "exit_price": None,
        "realized_return_pct": None,
    }


def _finalize_trade_metrics(trade: dict[str, Any], frame: pd.DataFrame, end_position: int) -> None:
    entry = float(trade["entry_price"])
    if trade["exit_trigger_position"] is not None:
        excursion_end = int(trade["exit_trigger_position"])
    else:
        excursion_end = end_position
    observed = frame.iloc[int(trade["entry_position"]) : excursion_end + 1]
    max_high = float(observed["high"].max())
    min_low = float(observed["low"].min())
    risk_per_share = entry - float(trade["defense"])
    trade["max_favorable_price"] = round(max_high, 4)
    trade["mfe_pct"] = _return_pct(max_high, entry)
    trade["max_adverse_price"] = round(min_low, 4)
    trade["mae_pct"] = _return_pct(min_low, entry)
    trade["mfe_r"] = None if risk_per_share <= 0 else round((max_high - entry) / risk_per_share, 4)
    trade["mae_r"] = None if risk_per_share <= 0 else round((min_low - entry) / risk_per_share, 4)
    if trade["status"] == "CLOSED":
        trade["performance_date"] = trade["exit_date"]
        trade["performance_price"] = trade["exit_price"]
        trade["performance_return_pct"] = trade["realized_return_pct"]
    else:
        mark = float(frame.iloc[end_position]["close"])
        trade["performance_date"] = frame.iloc[end_position]["date"].date().isoformat()
        trade["performance_price"] = round(mark, 4)
        trade["performance_return_pct"] = _return_pct(mark, entry)
    trade.pop("entry_position", None)
    trade.pop("exit_trigger_position", None)


def replay_stock(
    *,
    stock: dict[str, Any],
    selections: list[dict[str, Any]],
    as_of: date,
    max_monitor_bars: int,
    thresholds: TrialThresholds = THRESHOLDS,
) -> dict[str, Any]:
    code = str(stock["code"])
    name = str(stock.get("name") or selections[0]["name"])
    frame, source_path = load_full_frame(str(stock["symbol"]), as_of)
    date_to_position = {value.date(): index for index, value in enumerate(frame["date"])}
    usable = [row for row in selections if row["date"] in date_to_position]
    if not usable:
        raise ValueError("no selection date has a matching market bar")
    selected_by_date = {row["date"]: row for row in usable}
    start_position = min(date_to_position[row["date"]] for row in usable)
    end_position = max(index for index, value in enumerate(frame["date"]) if value.date() <= as_of)

    episode: dict[str, Any] | None = None
    episode_sequence = 0
    pending_plan: dict[str, Any] | None = None
    current_trade: dict[str, Any] | None = None
    pending_exit = False
    trades: list[dict[str, Any]] = []
    ambiguities: list[dict[str, Any]] = []
    state_counts: Counter[str] = Counter()
    final_state = "FORMING"
    armed_count = 0

    for position in range(start_position, end_position + 1):
        bar = frame.iloc[position]
        bar_date = bar["date"].date()
        open_price = float(bar["open"])
        high = float(bar["high"])
        low = float(bar["low"])
        close = float(bar["close"])

        if current_trade is not None and pending_exit:
            current_trade["exit_date"] = bar_date.isoformat()
            current_trade["exit_price"] = round(open_price, 4)
            current_trade["realized_return_pct"] = _return_pct(open_price, float(current_trade["entry_price"]))
            current_trade["status"] = "CLOSED"
            current_trade["status_label"] = status_label("CLOSED")
            trades.append(current_trade)
            current_trade = None
            pending_exit = False
            episode = None
            pending_plan = None

        # A plan created at a prior close is executable during this session.
        if current_trade is None and episode is not None and pending_plan is not None:
            trigger = float(pending_plan["trigger"])
            chase_cap = float(pending_plan["chase_cap"])
            defense = float(pending_plan["defense"])
            if open_price > chase_cap:
                state_counts["NO_CHASE"] += 1
                final_state = "NO_CHASE"
            elif open_price <= defense:
                state_counts["INVALIDATED"] += 1
                final_state = "INVALIDATED"
            elif trigger > chase_cap:
                state_counts["NO_CHASE"] += 1
                final_state = "NO_CHASE"
            elif high >= trigger:
                # Entry is intraday, but the fixed-defense exit is evaluated
                # at the close.  A low below defense therefore does not create
                # an unknown ordering between the two executable rules.
                entry = max(open_price, trigger)
                current_trade = _make_trade(
                    code=code,
                    name=name,
                    episode=episode,
                    plan=pending_plan,
                    bar_date=bar_date,
                    entry_price=entry,
                    entry_position=position,
                )
                state_counts["TRIGGERED"] += 1
                final_state = "TRIGGERED"
            pending_plan = None

        selection = selected_by_date.get(bar_date)

        if current_trade is not None:
            if close < float(current_trade["defense"]):
                current_trade["exit_trigger_date"] = bar_date.isoformat()
                current_trade["exit_trigger_position"] = position
                current_trade["exit_trigger_close"] = round(close, 4)
                current_trade["exit_trigger_return_pct"] = _return_pct(
                    close, float(current_trade["entry_price"])
                )
                current_trade["status"] = "EXIT_TRIGGERED"
                current_trade["status_label"] = status_label("EXIT_TRIGGERED")
                state_counts["EXIT_TRIGGERED"] += 1
                final_state = "EXIT_TRIGGERED"
                if position < end_position:
                    pending_exit = True
            continue

        if selection is not None:
            if episode is None:
                episode_sequence += 1
                episode = _new_episode(selection, bar_date, position, episode_sequence)
            else:
                _merge_selection(episode, selection, bar_date, position)

        if episode is None:
            continue
        if position - int(episode["last_selected_position"]) > max_monitor_bars:
            state_counts["EXPIRED"] += 1
            final_state = "EXPIRED"
            episode = None
            pending_plan = None
            continue

        snapshot = enlightenment_snapshot(frame.iloc[: position + 1].copy(), thresholds)
        if snapshot["structural_invalid"]:
            state_counts["INVALIDATED"] += 1
            final_state = "INVALIDATED"
            episode = None
            pending_plan = None
            continue

        if snapshot["armed"]:
            pending_plan = {
                "armed_date": bar_date.isoformat(),
                "trigger": float(snapshot["setup"]["trigger_price"]),
                "chase_cap": float(snapshot["risk"]["chase_cap"]),
                "defense": float(snapshot["risk"]["defense"]),
                "pattern": snapshot["setup"]["pattern_code"],
                "structural_lens": snapshot["structural_lens"],
                "taiji_sequence": snapshot["taiji"]["sequence"],
                "taiji_state": snapshot["taiji"]["state"],
            }
            armed_count += 1
            state_counts["ARMED"] += 1
            final_state = "ARMED"
        else:
            state_counts["FORMING"] += 1
            final_state = "FORMING"

    if current_trade is not None:
        trades.append(current_trade)
    for trade in trades:
        _finalize_trade_metrics(trade, frame, end_position)
    return {
        "code": code,
        "name": name,
        "symbol": stock["symbol"],
        "market": stock.get("market"),
        "source_path": str(source_path.resolve()),
        "first_selected_date": min(row["date"] for row in usable).isoformat(),
        "last_selected_date": max(row["date"] for row in usable).isoformat(),
        "selection_count": len(usable),
        "strategy_codes": sorted({value for row in usable for value in row["strategy_codes"]}),
        "source_labels": sorted({value for row in usable for value in row["source_labels"]}),
        "armed_count": armed_count,
        "trades": trades,
        "ambiguities": ambiguities,
        "state_counts": dict(state_counts),
        "final_state": final_state,
        "final_state_label": status_label(final_state),
    }


def _mean(values: list[float]) -> float | None:
    return None if not values else round(statistics.fmean(values), 4)


def _median(values: list[float]) -> float | None:
    return None if not values else round(statistics.median(values), 4)


def summarize(
    results: list[dict[str, Any]],
    errors: list[dict[str, str]],
    selection_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    trades = [trade for result in results for trade in result["trades"]]
    ambiguities = [item for result in results for item in result["ambiguities"]]
    performance = [float(trade["performance_return_pct"]) for trade in trades]
    exit_trigger_returns = [
        float(trade["exit_trigger_return_pct"])
        for trade in trades
        if trade["exit_trigger_return_pct"] is not None
    ]
    realized = [
        float(trade["realized_return_pct"])
        for trade in trades
        if trade["realized_return_pct"] is not None
    ]
    mfes = [float(trade["mfe_pct"]) for trade in trades]
    return {
        "selection_rows_after_daily_union": len(selection_rows),
        "unique_candidates": len({row["code"] for row in selection_rows}),
        "analyzable_candidates": len(results),
        "data_error_candidates": len(errors),
        "armed_candidates": sum(result["armed_count"] > 0 for result in results),
        "triggered_trades": len(trades),
        "ambiguous_events_excluded": len(ambiguities),
        "open_trades": sum(trade["status"] == "OPEN" for trade in trades),
        "exit_triggered_pending_trades": sum(trade["status"] == "EXIT_TRIGGERED" for trade in trades),
        "closed_trades": sum(trade["status"] == "CLOSED" for trade in trades),
        "positive_at_exit_or_as_of": sum(value > 0 for value in performance),
        "non_positive_at_exit_or_as_of": sum(value <= 0 for value in performance),
        "positive_rate_pct": None if not performance else round(sum(value > 0 for value in performance) / len(performance) * 100.0, 2),
        "mean_performance_return_pct": _mean(performance),
        "median_performance_return_pct": _median(performance),
        "mean_mfe_pct": _mean(mfes),
        "median_mfe_pct": _median(mfes),
        "mean_exit_trigger_return_pct": _mean(exit_trigger_returns),
        "median_exit_trigger_return_pct": _median(exit_trigger_returns),
        "mean_realized_return_pct": _mean(realized),
        "median_realized_return_pct": _median(realized),
        "strategy_trade_counts_multilabel": dict(
            sorted(Counter(value for trade in trades for value in trade["source_strategy_codes"]).items())
        ),
        "source_selection_row_counts_multilabel": dict(
            sorted(Counter(value for row in selection_rows for value in row["source_labels"]).items())
        ),
    }


def _f(value: Any, digits: int = 2) -> str:
    return "—" if value is None else f"{float(value):.{digits}f}"


def render_markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    trades = [trade for result in payload["candidates"] for trade in result["trades"]]
    closed = [trade for trade in trades if trade["status"] == "CLOSED"]
    pending_exit = [trade for trade in trades if trade["status"] == "EXIT_TRIGGERED"]
    opened = [trade for trade in trades if trade["status"] == "OPEN"]
    closed_positive = sum(float(trade["realized_return_pct"]) > 0 for trade in closed)
    open_positive = sum(float(trade["performance_return_pct"]) > 0 for trade in opened)
    closed_givebacks = [
        float(trade["mfe_pct"]) - float(trade["exit_trigger_return_pct"])
        for trade in closed
    ]
    lines = [
        f"# 啟蒙層雷達全歷史觸發回測｜{payload['selection_window']['start']}～{payload['as_of']}",
        "",
        "> 研究用途，不是投資建議。候選採本機保存的雷達檔；所有指標只使用當時以前的日 K。報酬未扣交易成本。",
        "",
        "## 結論",
        "",
        f"- 最早非空雷達：{payload['selection_window']['start']}；雷達最後選股日：{payload['selection_window']['end']}；績效截至 {payload['as_of']}。",
        f"- 保存雷達日 {payload['source_coverage']['report_days']} 日；同日多檔聯集後 {summary['selection_rows_after_daily_union']} 筆，去重 {summary['unique_candidates']} 檔。",
        f"- 可分析 {summary['analyzable_candidates']} 檔，資料錯誤 {summary['data_error_candidates']} 檔；曾進入 ARMED（已建立進場計畫）{summary['armed_candidates']} 檔。",
        f"- TRIGGERED（已觸發進場）{summary['triggered_trades']} 筆。",
        f"- CLOSED（交易已結束）{summary['closed_trades']} 筆；EXIT_TRIGGERED（已觸發出場）待下次開盤 {summary['exit_triggered_pending_trades']} 筆；OPEN（持有中）{summary['open_trades']} 筆。",
        f"- 已結束交易實現報酬為正 {closed_positive}/{len(closed)} 筆；持有中截至今日為正 {open_positive}/{len(opened)} 筆。",
        f"- 每筆最大浮盈 MFE 平均 {_f(summary['mean_mfe_pct'])}%、中位數 {_f(summary['median_mfe_pct'])}%；出場觸發當刻平均 {_f(summary['mean_exit_trigger_return_pct'])}%、中位數 {_f(summary['median_exit_trigger_return_pct'])}%。",
        f"- 已結束交易從 MFE 到出場觸發平均回吐 {_f(_mean(closed_givebacks))} 個百分點，顯示固定防線尚缺移動停利規則。",
        "",
        "MFE（持有期間最大浮盈）使用進場後至出場觸發日／9 月 4 日之間的最高價。出場觸發損益使用跌破防線當日收盤；實際模擬出場則使用下一交易日開盤。",
        "",
    ]

    def add_trade_table(title: str, rows: list[dict[str, Any]]) -> None:
        lines.extend(
            [
                f"## {title}",
                "",
                "| 股票 | 監控加入 | 來源策略 | ARMED | TRIGGERED／進場 | 進場價 | 防線 | 最大價 | MFE% | 出場觸發 | 觸發時損益% | 實際出場 | 實現損益% | 截至績效% | 狀態 |",
                "|---|---|---|---|---|---:|---:|---:|---:|---|---:|---|---:|---:|---|",
            ]
        )
        if not rows:
            lines.append("| — | — | — | — | — | — | — | — | — | — | — | — | — | — | — |")
            lines.append("")
            return
        for trade in sorted(rows, key=lambda item: (item["entry_date"], item["code"], item["trade_id"])):
            lines.append(
                "| "
                + " | ".join(
                    [
                        f"{trade['code']} {trade['name']}",
                        trade["monitor_added_date"],
                        ",".join(trade["source_strategy_codes"]) or "雷達非技術來源",
                        trade["armed_date"],
                        trade["entry_date"],
                        _f(trade["entry_price"]),
                        _f(trade["defense"]),
                        _f(trade["max_favorable_price"]),
                        _f(trade["mfe_pct"]),
                        trade["exit_trigger_date"] or "—",
                        _f(trade["exit_trigger_return_pct"]),
                        (f"{trade['exit_date']}／{_f(trade['exit_price'])}" if trade["exit_date"] else "—"),
                        _f(trade["realized_return_pct"]),
                        _f(trade["performance_return_pct"]),
                        trade["status_label"],
                    ]
                )
                + " |"
            )
        lines.append("")

    add_trade_table("CLOSED（交易已結束）", closed)
    add_trade_table("EXIT_TRIGGERED（已觸發出場，等待下一開盤）", pending_exit)
    add_trade_table("OPEN（持有中）", opened)

    lines += [
        "",
        "## 規則與限制",
        "",
        "- 每日將同日期下所有非空雷達產物聯集、依股票代號去重；早期雷達包含技術、籌碼與跨來源模式，並非每一日都使用相同來源模式。",
        "- 入選日收盤後加入監控；啟蒙層通過後為 ARMED（已建立進場計畫）。下一交易日最高價達觸發價且未超過追價上限，即為 TRIGGERED（已觸發進場）並立即模擬成交。",
        "- 開盤低於觸發價，以觸發價成交；開盤高於觸發價但未超過追價上限，以開盤價成交；開盤超過追價上限則 NO_CHASE（不可追價）。",
        "- 進場是盤中觸及價成交；固定防線則用收盤價判定。若同日進場後收盤破防，當日即列為 EXIT_TRIGGERED（已觸發出場）。",
        "- 固定防線採 ARMED（已建立進場計畫）時最近的已確認樞紐。日 K 收盤跌破為 EXIT_TRIGGERED（已觸發出場），下一交易日開盤為 CLOSED（交易已結束）。",
        f"- 候選最後一次被雷達選中後最多監控 {payload['parameters']['max_monitor_bars']} 根交易日；出場後必須再次入選才能建立下一案。",
        "- 目前沒有移動停利或分批停利。因此最大浮盈不等於實現獲利；這正是本報告同時呈現 MFE 與出場觸發損益的原因。",
        "- 每一檔非空雷達檔都納入，包含早期試跑或不同來源模式；結果代表『已保存雷達產物聯集』，不是固定單一雷達版本的乾淨實驗。",
        "",
        "## 稽核資訊",
        "",
        f"- 方法版本：`{payload['method_version']}`",
        f"- 參數：`{json.dumps(payload['parameters'], ensure_ascii=False, sort_keys=True)}`",
    ]
    if payload["errors"]:
        lines += ["", "## 資料錯誤", "", "| 股票 | 原因 |", "|---|---|"]
        for error in payload["errors"]:
            lines.append(f"| {error['code']} {error['name']} | {error['error'].replace('|', '/')} |")
    return "\n".join(lines) + "\n"


def build_payload(start: date, selection_end: date, as_of: date, max_monitor_bars: int) -> dict[str, Any]:
    by_date, coverage = discover_radar_union(start, selection_end)
    selection_rows = [row for report_date in sorted(by_date) for row in by_date[report_date]]
    if not selection_rows:
        raise ValueError("no non-empty radar candidates found")
    stock_map = load_stock_map()
    by_code = _index_selections(selection_rows)
    results: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for code in sorted(by_code):
        stock = resolve_stock(code, by_code[code][0]["name"], stock_map)
        if stock is None:
            errors.append({"code": code, "name": by_code[code][0]["name"], "error": "missing stock mapping and price cache"})
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
        except Exception as exc:
            errors.append({"code": code, "name": by_code[code][0]["name"], "error": str(exc)})
    return {
        "method_version": METHOD_VERSION,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "selection_window": {"start": start.isoformat(), "end": selection_end.isoformat()},
        "as_of": as_of.isoformat(),
        "source_coverage": {
            "report_days": len(coverage),
            "available_dates": [item["date"] for item in coverage],
            "daily_artifacts": coverage,
        },
        "parameters": {
            **asdict(THRESHOLDS),
            "max_monitor_bars": max_monitor_bars,
            "selection_source": "union_of_all_retained_nonempty_radar_artifacts_per_date",
            "entry_model": "next_session_intraday_trigger_immediate_fill",
            "exit_model": "close_below_fixed_pivot_defense_then_following_open",
            "same_bar_execution": "intraday_entry_then_close_based_defense_evaluation",
            "reentry_requires_new_selection_after_exit": True,
            "costs_included": False,
            "elson_laoxiao_entry_gates": False,
        },
        "summary": summarize(results, errors, selection_rows),
        "candidates": results,
        "errors": errors,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2026-05-21")
    parser.add_argument("--selection-end", default="2026-09-03")
    parser.add_argument("--as-of", default="2026-09-04")
    parser.add_argument("--max-monitor-bars", type=int, default=20)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "reports" / "course_backtest" / "2026-09-04" / "radar_full_history_v2",
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
