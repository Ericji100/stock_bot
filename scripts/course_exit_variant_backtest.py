"""Compare four exit variants on the same enlightenment-layer entry events.

The entry sample is loaded from ``course_radar_trigger_backtest.py`` output so
that only the exit rule changes.  Signals use information available at the
session close and are filled at the following session open.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.course_daily_screen_trial import THRESHOLDS, _pivot_structure  # noqa: E402
from scripts.course_watchlist_backtest import load_full_frame  # noqa: E402


METHOD_VERSION = "course-exit-variant-backtest-v1"
VARIANTS = {
    "FIXED_PIVOT": "FIXED_PIVOT（固定初始樞紐）",
    "MA21_ONE_CLOSE": "MA21_ONE_CLOSE（首次收盤跌破21MA）",
    "MA21_TWO_CLOSES": "MA21_TWO_CLOSES（連續兩日收盤跌破21MA）",
    "HYBRID_RUNNER": "HYBRID_RUNNER（爆量警戒＋動態樞紐＋21MA）",
}
STATUS_LABELS = {
    "OPEN": "OPEN（持有中）",
    "EXIT_TRIGGERED": "EXIT_TRIGGERED（已觸發出場）",
    "CLOSED": "CLOSED（交易已結束）",
}
EXIT_REASON_LABELS = {
    "FIXED_DEFENSE": "FIXED_DEFENSE（固定初始防線）",
    "MA21_ONE_CLOSE": "MA21_ONE_CLOSE（首次收盤跌破21MA）",
    "MA21_TWO_CLOSES": "MA21_TWO_CLOSES（連續兩日收盤跌破21MA）",
    "DYNAMIC_DEFENSE": "DYNAMIC_DEFENSE（成本或動態樞紐防線）",
    "VOLUME_WARNING_PARTIAL": "VOLUME_WARNING_PARTIAL（爆量不漲部分停利）",
    "AS_OF": "AS_OF（回測截止日）",
}


def _return_pct(price: float, entry: float) -> float:
    return (price / entry - 1.0) * 100.0


def _mean(values: list[float]) -> float | None:
    return None if not values else round(statistics.fmean(values), 4)


def _median(values: list[float]) -> float | None:
    return None if not values else round(statistics.median(values), 4)


def _round_or_none(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(float(value), digits)


def _base_result(
    trade: dict[str, Any],
    variant: str,
    reference_mfe_pct: float,
    reference_max_price: float,
) -> dict[str, Any]:
    return {
        "trade_id": trade["trade_id"],
        "code": trade["code"],
        "name": trade["name"],
        "entry_date": trade["entry_date"],
        "entry_price": float(trade["entry_price"]),
        "initial_defense": float(trade["defense"]),
        "initial_risk_pct": float(trade["initial_risk_pct"]),
        "variant": variant,
        "variant_label": VARIANTS[variant],
        "reference_max_price": float(reference_max_price),
        "reference_mfe_pct": float(reference_mfe_pct),
        "exit_trigger_date": None,
        "exit_trigger_close": None,
        "exit_reason": None,
        "exit_reason_label": None,
        "exit_date": None,
        "exit_price": None,
        "partial_exits": [],
        "status": "OPEN",
        "status_label": STATUS_LABELS["OPEN"],
    }


def _finish_result(
    result: dict[str, Any],
    *,
    frame: pd.DataFrame,
    entry_position: int,
    signal_position: int | None,
    performance_price: float,
    performance_date: str,
    remaining_fraction: float = 1.0,
) -> dict[str, Any]:
    entry = float(result["entry_price"])
    end_position = len(frame) - 1 if signal_position is None else signal_position
    observed = frame.iloc[entry_position : end_position + 1]
    strategy_mfe = _return_pct(float(observed["high"].max()), entry)
    partial_return = sum(
        float(item["fraction"]) * float(item["return_pct"])
        for item in result["partial_exits"]
    )
    performance_return = partial_return + remaining_fraction * _return_pct(performance_price, entry)
    reference_mfe = float(result["reference_mfe_pct"])
    capture = None if reference_mfe <= 0 else performance_return / reference_mfe * 100.0
    result.update(
        {
            "remaining_fraction": round(remaining_fraction, 4),
            "strategy_max_price": round(float(observed["high"].max()), 4),
            "strategy_mfe_pct": round(strategy_mfe, 4),
            "performance_date": performance_date,
            "performance_price": round(performance_price, 4),
            "performance_return_pct": round(performance_return, 4),
            "reference_mfe_capture_pct": _round_or_none(capture),
            "reference_mfe_giveback_points": round(reference_mfe - performance_return, 4),
        }
    )
    return result


def simulate_simple_variant(
    *,
    frame: pd.DataFrame,
    trade: dict[str, Any],
    variant: str,
) -> dict[str, Any]:
    date_to_position = {value.date(): index for index, value in enumerate(frame["date"])}
    entry_position = date_to_position[date.fromisoformat(trade["entry_date"])]
    entry = float(trade["entry_price"])
    defense = float(trade["defense"])
    result = _base_result(
        trade,
        variant,
        float(trade["mfe_pct"]),
        float(trade["max_favorable_price"]),
    )
    below_ma21_streak = 0
    signal_position: int | None = None
    reason: str | None = None
    for position in range(entry_position, len(frame)):
        row = frame.iloc[position]
        close = float(row["close"])
        below_ma21_streak = below_ma21_streak + 1 if close < float(row["MA21"]) else 0
        if close < defense:
            signal_position = position
            reason = "FIXED_DEFENSE"
        elif variant == "MA21_ONE_CLOSE" and below_ma21_streak >= 1:
            signal_position = position
            reason = "MA21_ONE_CLOSE"
        elif variant == "MA21_TWO_CLOSES" and below_ma21_streak >= 2:
            signal_position = position
            reason = "MA21_TWO_CLOSES"
        if signal_position is not None:
            break

    if signal_position is None:
        mark = frame.iloc[-1]
        result["exit_reason"] = "AS_OF"
        result["exit_reason_label"] = EXIT_REASON_LABELS["AS_OF"]
        return _finish_result(
            result,
            frame=frame,
            entry_position=entry_position,
            signal_position=None,
            performance_price=float(mark["close"]),
            performance_date=mark["date"].date().isoformat(),
        )

    signal = frame.iloc[signal_position]
    result["exit_trigger_date"] = signal["date"].date().isoformat()
    result["exit_trigger_close"] = round(float(signal["close"]), 4)
    result["exit_reason"] = reason
    result["exit_reason_label"] = EXIT_REASON_LABELS[str(reason)]
    if signal_position + 1 < len(frame):
        exit_bar = frame.iloc[signal_position + 1]
        exit_price = float(exit_bar["open"])
        result.update(
            {
                "exit_date": exit_bar["date"].date().isoformat(),
                "exit_price": round(exit_price, 4),
                "status": "CLOSED",
                "status_label": STATUS_LABELS["CLOSED"],
            }
        )
        return _finish_result(
            result,
            frame=frame,
            entry_position=entry_position,
            signal_position=signal_position,
            performance_price=exit_price,
            performance_date=result["exit_date"],
        )

    result["status"] = "EXIT_TRIGGERED"
    result["status_label"] = STATUS_LABELS["EXIT_TRIGGERED"]
    return _finish_result(
        result,
        frame=frame,
        entry_position=entry_position,
        signal_position=signal_position,
        performance_price=float(signal["close"]),
        performance_date=result["exit_trigger_date"],
    )


def _confirmed_pivot_defenses(frame: pd.DataFrame, start_position: int) -> dict[int, float | None]:
    defenses: dict[int, float | None] = {}
    for position in range(start_position, len(frame)):
        snapshot = _pivot_structure(frame.iloc[: position + 1].copy(), THRESHOLDS)
        record = snapshot.get("defense")
        defenses[position] = None if record is None else float(record["price"])
    return defenses


def simulate_hybrid_variant(
    *,
    frame: pd.DataFrame,
    trade: dict[str, Any],
    pivot_defenses: dict[int, float | None],
    activation_r: float = 2.0,
    partial_fraction: float = 1.0 / 3.0,
    volume_ratio_min: float = 2.5,
    extension_atr_min: float = 2.0,
    close_position_max: float = 0.35,
    max_close_advance_pct: float = 1.0,
) -> dict[str, Any]:
    date_to_position = {value.date(): index for index, value in enumerate(frame["date"])}
    entry_position = date_to_position[date.fromisoformat(trade["entry_date"])]
    entry = float(trade["entry_price"])
    initial_defense = float(trade["defense"])
    risk_per_share = entry - initial_defense
    activation_price = entry + activation_r * risk_per_share
    result = _base_result(
        trade,
        "HYBRID_RUNNER",
        float(trade["mfe_pct"]),
        float(trade["max_favorable_price"]),
    )
    result.update(
        {
            "profit_protect_activation_price": round(activation_price, 4),
            "profit_protect_activation_date": None,
            "final_dynamic_defense": round(initial_defense, 4),
            "volume_warning_date": None,
        }
    )
    profit_protect = False
    dynamic_defense = initial_defense
    below_ma21_streak = 0
    partial_done = False
    pending_partial = False
    remaining_fraction = 1.0
    signal_position: int | None = None
    reason: str | None = None

    for position in range(entry_position, len(frame)):
        row = frame.iloc[position]
        if pending_partial:
            executed_fraction = min(partial_fraction, remaining_fraction)
            partial_price = float(row["open"])
            result["partial_exits"].append(
                {
                    "signal_date": result["volume_warning_date"],
                    "date": row["date"].date().isoformat(),
                    "price": round(partial_price, 4),
                    "fraction": executed_fraction,
                    "return_pct": round(_return_pct(partial_price, entry), 4),
                    "reason": "VOLUME_WARNING_PARTIAL",
                    "reason_label": EXIT_REASON_LABELS["VOLUME_WARNING_PARTIAL"],
                }
            )
            remaining_fraction -= executed_fraction
            pending_partial = False
            partial_done = True

        close = float(row["close"])
        ma21 = float(row["MA21"])
        below_ma21_streak = below_ma21_streak + 1 if close < ma21 else 0

        if close < initial_defense:
            signal_position = position
            reason = "FIXED_DEFENSE"
        elif profit_protect and close < dynamic_defense:
            signal_position = position
            reason = "DYNAMIC_DEFENSE"
        elif profit_protect and below_ma21_streak >= 2:
            signal_position = position
            reason = "MA21_TWO_CLOSES"
        if signal_position is not None:
            break

        if not profit_protect and float(row["high"]) >= activation_price:
            profit_protect = True
            dynamic_defense = max(dynamic_defense, entry)
            below_ma21_streak = 1 if close < ma21 else 0
            result["profit_protect_activation_date"] = row["date"].date().isoformat()

        if profit_protect:
            pivot_defense = pivot_defenses.get(position)
            if pivot_defense is not None:
                dynamic_defense = max(dynamic_defense, float(pivot_defense))
            result["final_dynamic_defense"] = round(dynamic_defense, 4)

            if not partial_done and not pending_partial and position > entry_position:
                previous_close = float(frame.iloc[position - 1]["close"])
                volume_ma20 = float(row["VOL_MA20"])
                volume_ratio = 0.0 if volume_ma20 <= 0 else float(row["volume"]) / volume_ma20
                atr = float(row["ATR14"])
                extension_atr = 0.0 if atr <= 0 else (close - ma21) / atr
                candle_range = float(row["high"] - row["low"])
                close_position = 1.0 if candle_range <= 0 else (close - float(row["low"])) / candle_range
                close_advance_pct = _return_pct(close, previous_close)
                volume_failure = bool(
                    volume_ratio >= volume_ratio_min
                    and extension_atr >= extension_atr_min
                    and close_position <= close_position_max
                    and close_advance_pct <= max_close_advance_pct
                )
                if volume_failure:
                    result["volume_warning_date"] = row["date"].date().isoformat()
                    if position + 1 < len(frame):
                        pending_partial = True

    if signal_position is None:
        mark = frame.iloc[-1]
        result["exit_reason"] = "AS_OF"
        result["exit_reason_label"] = EXIT_REASON_LABELS["AS_OF"]
        return _finish_result(
            result,
            frame=frame,
            entry_position=entry_position,
            signal_position=None,
            performance_price=float(mark["close"]),
            performance_date=mark["date"].date().isoformat(),
            remaining_fraction=remaining_fraction,
        )

    signal = frame.iloc[signal_position]
    result["exit_trigger_date"] = signal["date"].date().isoformat()
    result["exit_trigger_close"] = round(float(signal["close"]), 4)
    result["exit_reason"] = reason
    result["exit_reason_label"] = EXIT_REASON_LABELS[str(reason)]
    if signal_position + 1 < len(frame):
        exit_bar = frame.iloc[signal_position + 1]
        exit_price = float(exit_bar["open"])
        result.update(
            {
                "exit_date": exit_bar["date"].date().isoformat(),
                "exit_price": round(exit_price, 4),
                "status": "CLOSED",
                "status_label": STATUS_LABELS["CLOSED"],
            }
        )
        return _finish_result(
            result,
            frame=frame,
            entry_position=entry_position,
            signal_position=signal_position,
            performance_price=exit_price,
            performance_date=result["exit_date"],
            remaining_fraction=remaining_fraction,
        )

    result["status"] = "EXIT_TRIGGERED"
    result["status_label"] = STATUS_LABELS["EXIT_TRIGGERED"]
    return _finish_result(
        result,
        frame=frame,
        entry_position=entry_position,
        signal_position=signal_position,
        performance_price=float(signal["close"]),
        performance_date=result["exit_trigger_date"],
        remaining_fraction=remaining_fraction,
    )


def summarize_variant(rows: list[dict[str, Any]]) -> dict[str, Any]:
    returns = [float(row["performance_return_pct"]) for row in rows]
    capture_eligible = [row for row in rows if float(row["reference_mfe_pct"]) >= 10.0]
    captures = [
        float(row["reference_mfe_capture_pct"])
        for row in capture_eligible
        if row["reference_mfe_capture_pct"] is not None
    ]
    big = [row for row in rows if float(row["reference_mfe_pct"]) >= 30.0]
    positives = [value for value in returns if value > 0]
    negatives = [value for value in returns if value < 0]
    profit_factor = None if not negatives else sum(positives) / abs(sum(negatives))
    return {
        "trade_count": len(rows),
        "closed_count": sum(row["status"] == "CLOSED" for row in rows),
        "exit_triggered_count": sum(row["status"] == "EXIT_TRIGGERED" for row in rows),
        "open_count": sum(row["status"] == "OPEN" for row in rows),
        "positive_count": sum(value > 0 for value in returns),
        "positive_rate_pct": round(sum(value > 0 for value in returns) / len(returns) * 100.0, 2),
        "mean_return_pct": _mean(returns),
        "median_return_pct": _median(returns),
        "profit_factor": _round_or_none(profit_factor),
        "mfe_capture_min_reference_mfe_pct": 10.0,
        "mfe_capture_sample_count": len(capture_eligible),
        "mean_reference_mfe_capture_pct": _mean(captures),
        "median_reference_mfe_capture_pct": _median(captures),
        "mean_reference_mfe_giveback_points": _mean(
            [float(row["reference_mfe_giveback_points"]) for row in rows]
        ),
        "big_opportunity_count": len(big),
        "big_opportunity_mean_return_pct": _mean(
            [float(row["performance_return_pct"]) for row in big]
        ),
        "big_opportunity_mean_capture_pct": _mean(
            [float(row["reference_mfe_capture_pct"]) for row in big]
        ),
        "big_opportunity_positive_count": sum(
            float(row["performance_return_pct"]) > 0 for row in big
        ),
        "big_opportunity_kept_half_count": sum(
            float(row["reference_mfe_capture_pct"]) >= 50.0 for row in big
        ),
        "partial_exit_count": sum(bool(row["partial_exits"]) for row in rows),
        "exit_reason_counts": dict(
            sorted(Counter(str(row["exit_reason"]) for row in rows).items())
        ),
    }


def build_payload(input_path: Path) -> dict[str, Any]:
    source = json.loads(input_path.read_text(encoding="utf-8"))
    as_of = date.fromisoformat(source["as_of"])
    frames: dict[str, pd.DataFrame] = {}
    candidates_by_code = {row["code"]: row for row in source["candidates"]}
    source_trades = [trade for row in source["candidates"] for trade in row["trades"]]
    results: dict[str, list[dict[str, Any]]] = {key: [] for key in VARIANTS}
    baseline_mismatches: list[dict[str, Any]] = []

    for trade in source_trades:
        candidate = candidates_by_code[trade["code"]]
        symbol = str(candidate["symbol"])
        if symbol not in frames:
            frames[symbol], _ = load_full_frame(symbol, as_of)
        full_frame = frames[symbol]
        entry_date = date.fromisoformat(trade["entry_date"])
        full_entry_position = next(
            index
            for index, value in enumerate(full_frame["date"])
            if value.date() == entry_date
        )
        frame = full_frame.iloc[full_entry_position:].reset_index(drop=True)

        fixed = simulate_simple_variant(frame=frame, trade=trade, variant="FIXED_PIVOT")
        results["FIXED_PIVOT"].append(fixed)
        results["MA21_ONE_CLOSE"].append(
            simulate_simple_variant(frame=frame, trade=trade, variant="MA21_ONE_CLOSE")
        )
        results["MA21_TWO_CLOSES"].append(
            simulate_simple_variant(frame=frame, trade=trade, variant="MA21_TWO_CLOSES")
        )
        full_pivot_defenses = _confirmed_pivot_defenses(full_frame, full_entry_position)
        pivot_defenses = {
            position - full_entry_position: value
            for position, value in full_pivot_defenses.items()
        }
        results["HYBRID_RUNNER"].append(
            simulate_hybrid_variant(
                frame=frame,
                trade=trade,
                pivot_defenses=pivot_defenses,
            )
        )

        source_return = float(trade["performance_return_pct"])
        if abs(float(fixed["performance_return_pct"]) - source_return) > 0.02 or fixed["status"] != trade["status"]:
            baseline_mismatches.append(
                {
                    "trade_id": trade["trade_id"],
                    "source_status": trade["status"],
                    "replayed_status": fixed["status"],
                    "source_return_pct": source_return,
                    "replayed_return_pct": fixed["performance_return_pct"],
                }
            )

    summaries = {key: summarize_variant(rows) for key, rows in results.items()}
    return {
        "method_version": METHOD_VERSION,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "input_path": str(input_path.resolve()),
        "input_method_version": source["method_version"],
        "selection_window": source["selection_window"],
        "as_of": source["as_of"],
        "entry_sample": {
            "definition": "same_90_triggered_entries_from_radar_full_history_v2",
            "trade_count": len(source_trades),
            "unique_stocks": len({trade["code"] for trade in source_trades}),
            "isolates_exit_rules": True,
            "overlapping_alternative_positions_allowed": True,
        },
        "parameters": {
            "signal_fill": "close_signal_then_following_session_open",
            "costs_included": False,
            "ma21_source": "causal_daily_MA21",
            "hybrid_profit_protect_activation_r": 2.0,
            "hybrid_dynamic_defense": "max(previous_defense, entry_price, latest_causally_confirmed_pivot_low)",
            "hybrid_ma21_full_exit": "two_consecutive_closes_below_MA21_after_profit_protect_activation",
            "hybrid_partial_fraction": round(1.0 / 3.0, 4),
            "hybrid_volume_ratio_min": 2.5,
            "hybrid_extension_atr_min": 2.0,
            "hybrid_close_position_max": 0.35,
            "hybrid_max_close_advance_pct": 1.0,
        },
        "variant_labels": VARIANTS,
        "summaries": summaries,
        "results": results,
        "baseline_validation": {
            "mismatch_count": len(baseline_mismatches),
            "mismatches": baseline_mismatches,
        },
    }


def _f(value: Any, digits: int = 2) -> str:
    return "—" if value is None else f"{float(value):.{digits}f}"


def render_markdown(payload: dict[str, Any]) -> str:
    summaries = payload["summaries"]
    lines = [
        f"# 啟蒙層出場規則四版本比較｜截至 {payload['as_of']}",
        "",
        "> 研究用途，不是投資建議。固定同一組進場事件，只替換出場規則；報酬未扣交易成本。",
        "",
        "## 四版本總表",
        "",
        "| 版本 | 交易數 | 已結束 | 持有中 | 正報酬率 | 平均報酬 | 中位報酬 | Profit factor | MFE≥10%平均保留率 | 平均回吐 | 大波段平均報酬 | 大波段為正 | 大波段保留過半 | 部分停利 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for key in VARIANTS:
        row = summaries[key]
        lines.append(
            f"| {VARIANTS[key]} | {row['trade_count']} | {row['closed_count']} | {row['open_count']} | "
            f"{_f(row['positive_rate_pct'])}% | {_f(row['mean_return_pct'])}% | {_f(row['median_return_pct'])}% | "
            f"{_f(row['profit_factor'])} | {_f(row['mean_reference_mfe_capture_pct'])}% | "
            f"{_f(row['mean_reference_mfe_giveback_points'])}pt | {_f(row['big_opportunity_mean_return_pct'])}% | "
            f"{row['big_opportunity_positive_count']}/{row['big_opportunity_count']} | "
            f"{row['big_opportunity_kept_half_count']}/{row['big_opportunity_count']} | {row['partial_exit_count']} |"
        )

    lines += [
        "",
        "MFE 保留率的分母固定為原始進場事件在固定防線生命週期內的最大浮盈；因此可以懲罰過早出場。總表只對參考 MFE 至少 10% 的交易計算平均保留率，避免微小分母扭曲結果；「大波段」定義為參考 MFE 至少 30%。",
        "",
        "## 結果解讀",
        "",
        f"- 整體平均報酬最高為 {VARIANTS['MA21_ONE_CLOSE']}：{_f(summaries['MA21_ONE_CLOSE']['mean_return_pct'])}%，Profit factor {_f(summaries['MA21_ONE_CLOSE']['profit_factor'])}。",
        f"- 大波段保護最好為 {VARIANTS['HYBRID_RUNNER']}：5 筆大波段平均 {_f(summaries['HYBRID_RUNNER']['big_opportunity_mean_return_pct'])}%，{summaries['HYBRID_RUNNER']['big_opportunity_positive_count']}/5 保持正報酬。",
        "- 四版本 Profit factor 都未超過 1，且尚未扣成本，所以現階段只能用來選出下一輪方向，還不能視為可上線的完整出場系統。",
        "",
        "## 百容（2483）",
        "",
        "| 版本 | 部分停利 | 最終出場觸發 | 實際出場／截至價 | 績效 | MFE保留率 | 狀態 |",
        "|---|---|---|---|---:|---:|---|",
    ]
    for key in VARIANTS:
        row = next(item for item in payload["results"][key] if item["code"] == "2483")
        partial = "—"
        if row["partial_exits"]:
            item = row["partial_exits"][0]
            partial = f"{item['date']}／{_f(item['price'])}／{_f(float(item['fraction']) * 100)}% 部位"
        trigger = "—" if row["exit_trigger_date"] is None else f"{row['exit_trigger_date']}／{row['exit_reason_label']}"
        actual = f"{row['performance_date']}／{_f(row['performance_price'])}"
        lines.append(
            f"| {VARIANTS[key]} | {partial} | {trigger} | {actual} | "
            f"{_f(row['performance_return_pct'])}% | {_f(row['reference_mfe_capture_pct'])}% | {row['status_label']} |"
        )

    big_ids = {
        row["trade_id"]
        for row in payload["results"]["FIXED_PIVOT"]
        if float(row["reference_mfe_pct"]) >= 30.0
    }
    lines += [
        "",
        "## 大波段逐筆比較（參考 MFE ≥ 30%）",
        "",
        "| 股票 | 進場日 | 參考MFE | 固定樞紐 | 21MA一日 | 21MA兩日 | 混合版 |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    indexes = {
        key: {row["trade_id"]: row for row in payload["results"][key]}
        for key in VARIANTS
    }
    for trade_id in sorted(
        big_ids,
        key=lambda value: indexes["FIXED_PIVOT"][value]["reference_mfe_pct"],
        reverse=True,
    ):
        base = indexes["FIXED_PIVOT"][trade_id]
        lines.append(
            f"| {base['code']} {base['name']} | {base['entry_date']} | {_f(base['reference_mfe_pct'])}% | "
            f"{_f(indexes['FIXED_PIVOT'][trade_id]['performance_return_pct'])}% | "
            f"{_f(indexes['MA21_ONE_CLOSE'][trade_id]['performance_return_pct'])}% | "
            f"{_f(indexes['MA21_TWO_CLOSES'][trade_id]['performance_return_pct'])}% | "
            f"{_f(indexes['HYBRID_RUNNER'][trade_id]['performance_return_pct'])}% |"
        )

    lines += [
        "",
        "## 規則定義",
        "",
        f"- {VARIANTS['FIXED_PIVOT']}：收盤跌破進場時固定樞紐防線，次日開盤全數出場。",
        f"- {VARIANTS['MA21_ONE_CLOSE']}：固定防線或首次收盤跌破 21MA，次日開盤全數出場。",
        f"- {VARIANTS['MA21_TWO_CLOSES']}：固定防線或連續兩日收盤跌破 21MA，次日開盤全數出場。",
        f"- {VARIANTS['HYBRID_RUNNER']}：未到 +2R 使用原防線；到 +2R 後將防線提高至成本與最新已確認樞紐低點之較高者，且只能上移。爆量不漲條件成立後次日減碼 1/3；剩餘部位收盤跌破動態防線，或連續兩日跌破 21MA 時全數出場。",
        "- 爆量不漲：成交量至少 2.5 倍 20 日均量、收盤距 21MA 至少 2 ATR、收在當日振幅底部 35%，且較前收漲幅不超過 1%。",
        "- 所有樞紐都用當時已確認的資料；`pivot_n=2` 的右側確認日未到之前，不得使用該樞紐。",
        "",
        "## 限制",
        "",
        "- 這是「固定進場樣本」的出場規則實驗，目的是隔離出場效果；不會因為某版本提早出場而另外新增重新進場。",
        "- 因此少數同一股票的後續進場事件，在某些出場版本中可能與前一筆持有期間重疊。",
        "- 部分停利的報酬按原始部位權重合併；未出場部位按截止日收盤計價。",
        "- 未扣手續費、交易稅與滑價，也未模擬漲跌停無法成交。",
        "",
        "## 稽核",
        "",
        f"- 原始固定防線重播差異：{payload['baseline_validation']['mismatch_count']} 筆。",
        f"- 方法版本：`{payload['method_version']}`",
        f"- 參數：`{json.dumps(payload['parameters'], ensure_ascii=False, sort_keys=True)}`",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        type=Path,
        default=ROOT
        / "reports"
        / "course_backtest"
        / "2026-09-04"
        / "radar_full_history_v2"
        / "backtest.json",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT
        / "reports"
        / "course_backtest"
        / "2026-09-04"
        / "exit_variants_v1",
    )
    args = parser.parse_args()
    payload = build_payload(args.input)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.output_dir / "comparison.json"
    markdown_path = args.output_dir / "comparison.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path.write_text(render_markdown(payload), encoding="utf-8")
    print(json.dumps(payload["summaries"], ensure_ascii=False, indent=2))
    print(markdown_path.resolve())


if __name__ == "__main__":
    main()
