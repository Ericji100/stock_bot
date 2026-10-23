"""Causal MA21/MA144-regime entry strategies for the accepted 0050 engine."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from . import etf_0050_ma21_macd_v2 as moving
from .etf_0050_base_v1 import StrategyRunSlice, StrategySpec
from .etf_0050_ten_year_same_method_v1 import _d
from .models import BacktestResult, StrategyKind


MOVING_ID = "MA21_MA144_REGIME_MOVING_DOW_MACD_21_55_55"
FIXED_ID = "MA21_MA144_REGIME_FIXED_INITIAL_STOP_MACD_21_55_55"
MOVING_LABEL = "MA21/MA144 分流進場＋移動道氏防線＋MACD(21,55,55) 停利"
FIXED_LABEL = "MA21/MA144 分流進場＋固定初始防守＋MACD(21,55,55) 停利"
SPECS = {
    MOVING_ID: StrategySpec(MOVING_ID, MOVING_LABEL, StrategyKind.LONG, "CUSTOM"),
    FIXED_ID: StrategySpec(FIXED_ID, FIXED_LABEL, StrategyKind.LONG, "CUSTOM"),
}


def build_indicator_rows(
    raw_rows: Sequence[Mapping[str, Any]], events: Sequence[Any]
) -> list[dict[str, Any]]:
    """Reuse the accepted MACD/Dow calculation and add causal SMA144."""

    return list(iter_indicator_rows(raw_rows, events))


def iter_indicator_rows(
    raw_rows: Iterable[Mapping[str, Any]], events: Sequence[Any]
) -> Iterator[dict[str, Any]]:
    """Yield one accepted regime row without requesting the next raw bar."""

    closes: list[Decimal] = []
    for source in moving.iter_indicator_rows(raw_rows, events):
        row = dict(source)
        factor = _d(row["price_rebase_factor"])
        if factor != 1:
            closes = [value * factor for value in closes]
        closes.append(_d(row["technical_close"]))
        row["sma144"] = (
            sum(closes[-144:], Decimal("0")) / Decimal("144")
            if len(closes) >= 144 else None
        )
        yield row


def _entry(
    *, day: date, leg_id: str, raw_close: Decimal,
    technical_close: Decimal, stop: Decimal,
) -> dict[str, Any]:
    return {
        "date": day, "role": "MOTHER", "status": "FILLED",
        "leg_id": leg_id, "reference_price": raw_close,
        "entry_reference_close": technical_close,
        "initial_stop": stop, "exit_reason": None,
    }


def build_regime_run(
    *, indicator_rows: Iterable[Mapping[str, Any]], raw_by_date: Mapping[date, Any],
    strategy_id: str, moving_stop: bool, start: date, end: date,
    slippage_bps: int,
    on_day: Callable[
        [Mapping[str, Any], tuple[Mapping[str, Any], ...], tuple[Mapping[str, Any], ...]],
        None,
    ] | None = None,
) -> tuple[StrategyRunSlice, list[dict[str, Any]], list[dict[str, Any]]]:
    """Freeze one mutually-exclusive regime strategy without sizing or PnL."""

    if strategy_id not in SPECS or moving_stop != (strategy_id == MOVING_ID):
        raise ValueError("strategy id / stop mode mismatch")
    transactions: list[dict[str, Any]] = []
    daily: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    active: dict[str, Any] | None = None
    phase_low: Decimal | None = None
    previous: Mapping[str, Any] | None = None
    episode = 0
    active_dow: Decimal | None = None
    active_dow_version: int | None = None

    for row in indicator_rows:
        day = row["date"]
        if day > end:
            break
        factor = _d(row["price_rebase_factor"])
        if factor != 1:
            if phase_low is not None:
                phase_low *= factor
            if active is not None:
                active["stop"] *= factor
            if active_dow is not None:
                active_dow *= factor
            events.append({
                "strategy_id": strategy_id, "episode_id": active["episode_id"] if active else "",
                "leg_id": active["leg_id"] if active else "", "date": day,
                "event": "CORPORATE_ACTION_REBASE", "factor": factor,
                "valid": True, "reason": "技術座標與既有防守價等比例換算",
            })

        sma21, sma144 = row.get("sma21"), row.get("sma144")
        histogram = _d(row["histogram"])
        prior_hist = None if previous is None else _d(previous["histogram"])
        death_cross = prior_hist is not None and prior_hist >= 0 and histogram < 0
        breakout = bool(
            day >= start and previous is not None
            and previous.get("sma21") is not None and sma21 is not None
            and _d(previous["technical_close"]) <= _d(previous["sma21"])
            and _d(row["technical_close"]) > _d(sma21)
        )
        green_to_red = bool(
            day >= start and prior_hist is not None
            and prior_hist <= 0 and histogram > 0
        )
        regime = (
            "WARMUP" if sma21 is None or sma144 is None
            else "SMA21_GE_SMA144" if _d(sma21) >= _d(sma144)
            else "SMA21_LT_SMA144"
        )
        previous_regime = "WARMUP"
        if previous is not None and previous.get("sma21") is not None and previous.get("sma144") is not None:
            previous_regime = (
                "SMA21_GE_SMA144"
                if _d(previous["sma21"]) >= _d(previous["sma144"])
                else "SMA21_LT_SMA144"
            )
        selected = (
            "MA21_BREAKOUT" if regime == "SMA21_GE_SMA144" and breakout
            else "MACD_GREEN_TO_RED" if regime == "SMA21_LT_SMA144" and green_to_red
            else ""
        )
        active_before = active is not None
        exited_today = False
        day_events: list[dict[str, Any]] = []

        if moving_stop:
            for event in row.get("dow_events", ()) if day >= start else ():
                defense = _d(event.defense)
                if active_dow is None or defense > active_dow:
                    old = active_dow
                    active_dow, active_dow_version = defense, event.version
                    events.append({
                        "strategy_id": strategy_id,
                        "episode_id": active["episode_id"] if active else "",
                        "leg_id": active["leg_id"] if active else "", "date": day,
                        "event": "DOW_DEFENSE_ESTABLISHED" if old is None else "DOW_DEFENSE_RAISED",
                        "previous_stop": old, "stop": defense,
                        "defense_version": event.version, "valid": True,
                        "reason": "因果 n=2 道氏防線只上移",
                    })
            if active is not None and active_dow is not None and active_dow > _d(active["stop"]):
                old = _d(active["stop"])
                active["stop"] = active_dow
                events.append({
                    "strategy_id": strategy_id, "episode_id": active["episode_id"],
                    "leg_id": active["leg_id"], "date": day,
                    "event": "DOW_DEFENSE_APPLIED_TO_LEG", "previous_stop": old,
                    "stop": active_dow, "defense_version": active_dow_version,
                    "valid": True, "reason": "有效防守只上移、不下移",
                })

        stop_hit_today = False
        suppressed_stop_hit = False
        stop_enforcement = "NO_POSITION"
        if day >= start and active is not None:
            if not active["armed"] and histogram >= 0:
                active["armed"] = True
                events.append({
                    "strategy_id": strategy_id, "episode_id": active["episode_id"],
                    "leg_id": active["leg_id"], "date": day, "event": "MACD_ARMED",
                    "histogram": histogram, "valid": True,
                    "reason": "買進後已觀察到柱狀值非負",
                })
            stop_hit_today = _d(row["technical_close"]) < _d(active["stop"])
            stop_enforcement = (
                "INACTIVE_LT_REGIME" if regime == "SMA21_LT_SMA144"
                else "ACTIVE_GE_REGIME"
            )
            if regime == "SMA21_LT_SMA144":
                events.append({
                    "strategy_id": strategy_id, "episode_id": active["episode_id"],
                    "leg_id": active["leg_id"], "date": day,
                    "event": "STOP_ENFORCEMENT_INACTIVE_LOW_REGIME",
                    "regime": regime, "stop": active["stop"],
                    "technical_close": row["technical_close"], "stop_hit": stop_hit_today,
                    "valid": True,
                    "reason": "SMA21<SMA144：維護 stop state，但只允許 armed 後 MACD 紅翻綠出場",
                })
                if stop_hit_today:
                    suppressed_stop_hit = True
                    events.append({
                        "strategy_id": strategy_id, "episode_id": active["episode_id"],
                        "leg_id": active["leg_id"], "date": day,
                        "event": "STOP_EXIT_SUPPRESSED_LOW_REGIME",
                        "regime": regime, "stop": active["stop"],
                        "technical_close": row["technical_close"], "stop_hit": True,
                        "valid": True,
                        "reason": "價格已跌破 stop，但低 regime 禁止價格停損；持倉續抱等待 MACD",
                    })
            elif previous_regime == "SMA21_LT_SMA144":
                events.append({
                    "strategy_id": strategy_id, "episode_id": active["episode_id"],
                    "leg_id": active["leg_id"], "date": day,
                    "event": "STOP_ENFORCEMENT_REACTIVATED_GE_REGIME",
                    "regime": regime, "stop": active["stop"],
                    "technical_close": row["technical_close"], "stop_hit": stop_hit_today,
                    "valid": True,
                    "reason": "SMA21>=SMA144：恢復使用持續因果維護的既有 stop state",
                })
            enforced_stop_hit = stop_hit_today and regime != "SMA21_LT_SMA144"
            if enforced_stop_hit or (death_cross and active["armed"]):
                if enforced_stop_hit:
                    reason = "STRUCTURAL_STOP" if moving_stop else "INITIAL_PROTECTIVE_STOP"
                    event_name = (
                        "STOP_PRIORITY_OVER_MACD" if death_cross
                        else "STRUCTURAL_STOP_EXIT" if moving_stop
                        else "INITIAL_STOP_EXIT"
                    )
                else:
                    reason, event_name = "MACD_RED_TO_GREEN", "VALID_RED_TO_GREEN_EXIT"
                transactions.append({
                    "date": day, "role": "EXIT", "status": "FILLED",
                    "leg_id": active["leg_id"], "reference_price": raw_by_date[day].close,
                    "exit_reason": reason,
                })
                events.append({
                    "strategy_id": strategy_id, "episode_id": active["episode_id"],
                    "leg_id": active["leg_id"], "date": day, "event": event_name,
                    "histogram": histogram, "previous_histogram": prior_hist,
                    "stop": active["stop"], "valid": True,
                    "reason": "同日防守優先" if enforced_stop_hit and death_cross else reason,
                })
                day_events.append({"type": "EXIT", "reason": reason})
                active = None
                exited_today = True

        if moving_stop and active_dow is not None and _d(row["technical_close"]) < active_dow:
            events.append({
                "strategy_id": strategy_id, "episode_id": active["episode_id"] if active else "",
                "leg_id": active["leg_id"] if active else "", "date": day,
                "event": "DOW_DEFENSE_INVALIDATED", "stop": active_dow,
                "defense_version": active_dow_version, "valid": True,
                "reason": "CLOSE_BREAK",
            })
            active_dow = active_dow_version = None
        if moving_stop and exited_today and active_dow is not None:
            events.append({
                "strategy_id": strategy_id, "episode_id": "", "leg_id": "", "date": day,
                "event": "DOW_DEFENSE_INVALIDATED", "stop": active_dow,
                "defense_version": active_dow_version, "valid": True,
                "reason": "EPISODE_END",
            })
            active_dow = active_dow_version = None

        entry_status = ""
        entry_leg = ""
        candidate_stop: Decimal | None = None
        effective_stop: Decimal | None = None
        if selected:
            candidate_stop = min(
                value for value in (
                    phase_low,
                    None if previous is None else _d(previous["technical_low"]),
                    _d(row["technical_low"]),
                ) if value is not None
            )
            if exited_today:
                entry_status = "SKIPPED_SAME_DAY_EXIT"
            elif active is not None:
                entry_status = "SKIPPED_ALREADY_HOLDING"
            elif candidate_stop >= _d(row["technical_close"]):
                entry_status = "SKIPPED_INVALID_STOP"
                events.append({
                    "strategy_id": strategy_id, "episode_id": "", "leg_id": "",
                    "date": day, "event": "ENTRY_SKIPPED_INVALID_STOP",
                    "entry_type": selected, "regime": regime,
                    "histogram": histogram, "initial_stop": candidate_stop,
                    "valid": True,
                    "reason": "候選防守價未低於進場參考價，沿用已驗收 sizing 約束不進場",
                })
            else:
                episode += 1
                entry_leg = f"REGIME-{'MOV' if moving_stop else 'FIX'}-L{episode}"
                episode_id = f"REGIME-{'MOV' if moving_stop else 'FIX'}-E{episode}"
                inherited = active_dow if moving_stop else None
                entry_fill = _d(raw_by_date[day].close) * (
                    Decimal("1") + Decimal(slippage_bps) / Decimal("10000")
                )
                if inherited is not None and inherited > entry_fill:
                    events.append({
                        "strategy_id": strategy_id, "episode_id": episode_id,
                        "leg_id": entry_leg, "date": day,
                        "event": "DOW_DEFENSE_INVALIDATED", "stop": inherited,
                        "defense_version": active_dow_version, "entry_fill_price": entry_fill,
                        "valid": True, "reason": "ABOVE_NEW_ENTRY_FILL",
                    })
                    active_dow = active_dow_version = inherited = None
                effective_stop = max(
                    value for value in (candidate_stop, inherited) if value is not None
                )
                transactions.append(_entry(
                    day=day, leg_id=entry_leg, raw_close=raw_by_date[day].close,
                    technical_close=_d(row["technical_close"]), stop=candidate_stop,
                ))
                armed = selected == "MACD_GREEN_TO_RED" or histogram >= 0
                active = {
                    "episode_id": episode_id, "leg_id": entry_leg,
                    "stop": effective_stop, "armed": armed,
                }
                entry_status = "FILLED"
                events.append({
                    "strategy_id": strategy_id, "episode_id": episode_id,
                    "leg_id": entry_leg, "date": day, "event": "ENTRY",
                    "entry_type": selected, "regime": regime, "histogram": histogram,
                    "armed_on_entry": armed, "initial_stop": candidate_stop,
                    "inherited_dow_stop": inherited, "effective_stop": effective_stop,
                    "valid": True,
                    "reason": "MACD 綠翻紅入場當日立即 armed" if selected == "MACD_GREEN_TO_RED" else "MA21 新突破入場",
                })
                day_events.append({"type": "ENTRY", "leg_id": entry_leg})
            phase_low = None
        elif sma21 is not None and _d(row["technical_close"]) <= _d(sma21):
            phase_low = min(value for value in (phase_low, _d(row["technical_low"])) if value is not None)
        elif sma21 is not None:
            phase_low = None

        if day >= start:
            daily_row = {
                "strategy_id": strategy_id, "date": day,
                "technical_close": row["technical_close"], "sma21": sma21,
                "sma144": sma144, "regime": regime,
                "previous_histogram": prior_hist, "histogram": histogram,
                "ma21_breakout": breakout, "macd_green_to_red": green_to_red,
                "selected_entry_type": selected, "active_before": active_before,
                "exited_today": exited_today, "entry_status": entry_status,
                "leg_id": entry_leg, "candidate_stop": candidate_stop,
                "effective_stop": effective_stop,
                "stop_enforcement": stop_enforcement,
                "stop_hit": stop_hit_today,
                "suppressed_stop_hit": suppressed_stop_hit,
            }
            daily.append(daily_row)
            decisions.append({
                "date": day, "strategy": StrategyKind.LONG.value,
                "close": row["technical_close"], "sma21": sma21,
                "histogram": histogram, "events": day_events,
            })
            if on_day is not None:
                on_day(daily_row, tuple(transactions), tuple(events))
        previous = row

    if active is not None:
        events.append({
            "strategy_id": strategy_id, "episode_id": active["episode_id"],
            "leg_id": active["leg_id"], "date": end, "event": "OPEN_AT_CUTOFF",
            "histogram": previous["histogram"] if previous else None,
            "valid": True, "reason": "截止仍持有，不強平",
        })
    result = object.__new__(BacktestResult)
    return (
        StrategyRunSlice(SPECS[strategy_id], result, tuple(decisions), tuple(transactions), ()),
        daily, events,
    )
