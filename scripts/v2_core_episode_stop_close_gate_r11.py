"""Deterministic daily-close validity of a frozen episode stop.

This gate checks execution semantics only. It does not select a structural
stop, anchor, scenario, or AI judgement, and it never reads future bars.
"""

from __future__ import annotations

from typing import Any


VERSION = "v2-core-episode-stop-close-gate-r11-candidate-r1"


def evaluate_stop_close_asof(
    daily_rows: list[dict[str, Any]], *, stop_price: float,
    stop_confirmation_date: str, signal_date: str,
) -> dict[str, Any]:
    if stop_price <= 0 or stop_confirmation_date > signal_date:
        raise ValueError("stop price/confirmation invalid")
    dates = [row["date"] for row in daily_rows]
    if len(dates) != len(set(dates)) or dates != sorted(dates):
        raise ValueError("daily bars must have unique ascending dates")
    if not daily_rows or daily_rows[-1]["date"] != signal_date:
        raise ValueError("packet must terminate on signal date")
    if stop_confirmation_date not in dates:
        raise ValueError("stop confirmation bar missing")
    active = [row for row in daily_rows if stop_confirmation_date < row["date"] <= signal_date]
    prior_close_breaches = [row for row in active if row["date"] < signal_date and row["close"] < stop_price]
    prior_intraday_breaches_without_close = [
        row for row in active
        if row["date"] < signal_date and row["low"] < stop_price <= row["close"]
    ]
    signal = daily_rows[-1]
    if prior_close_breaches:
        status = "PRIOR_CLOSE_BREACH_REQUIRES_NEW_EPISODE"
    elif signal["close"] < stop_price:
        status = "SIGNAL_CLOSE_BREACH"
    else:
        status = "ACTIVE_BY_DAILY_CLOSE"
    return {
        "gate_version": VERSION,
        "status": status,
        "stop_price": stop_price,
        "stop_confirmation_date": stop_confirmation_date,
        "signal_date": signal_date,
        "first_prior_close_breach": (
            {"date": prior_close_breaches[0]["date"], "close": prior_close_breaches[0]["close"]}
            if prior_close_breaches else None
        ),
        "prior_intraday_breach_without_close_dates": [row["date"] for row in prior_intraday_breaches_without_close],
        "signal_close": signal["close"],
        "signal_low": signal["low"],
        "trade_permission_granted": False,
        "anchor_or_stop_selected_by_program": False,
    }
