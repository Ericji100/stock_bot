from typing import Any

import pandas as pd
import pytest

from scripts import course_tg_enlightenment_ai_v2_backtest as replay_engine
from scripts.formal_ai_v1_v2_replay import (
    _all_v2_gates_pass,
    _future_dates,
    _lifecycle_for,
    _scenario,
    _signals_for,
    _stop,
    _v2_gate_quality,
)


def _market_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    """Return the smallest adjusted/raw daily frame accepted by the replay engine."""
    normalized = []
    for row in rows:
        open_price = float(row["open"])
        close_price = float(row["close"])
        normalized.append(
            {
                "date": pd.Timestamp(row["date"]),
                "open": open_price,
                "high": float(row.get("high", max(open_price, close_price))),
                "low": float(row.get("low", min(open_price, close_price))),
                "close": close_price,
                "raw_open": open_price,
                "raw_close": close_price,
                "ma21": float(row.get("ma21", close_price)),
                "atr14": float(row.get("atr14", 20.0)),
                "latest_pivot_low": row.get("latest_pivot_low", float("nan")),
            }
        )
    return pd.DataFrame(normalized)


def _signal(
    day: str,
    *,
    stop: float,
    close: float,
    intent: str,
    atr: float = 20.0,
) -> dict[str, Any]:
    return {
        "signal_date": day,
        "family": "SMALL_DOW_REVERSAL（小級道氏轉強）",
        "scenario": "MACRO_COPY_RESONANCE（大定錨複製共振）",
        "stop": stop,
        "signal_close": close,
        "signal_atr": atr,
        "risk_pct_at_signal": (close - stop) / close * 100,
        "risk_atr_at_signal": (close - stop) / atr,
        "score": 0,
        "code": "TEST",
        "name": "測試股",
        "campaign": 1,
        "monitor_on": day,
        "active_selection_strategies": ["TEST_STRATEGY"],
        "selected_today_strategies": ["TEST_STRATEGY"],
        "episode_or_add_candidate": intent,
    }


def _simulate(
    monkeypatch: pytest.MonkeyPatch,
    frame: pd.DataFrame,
    signals: list[dict[str, Any]],
    *,
    allow_adds: bool,
) -> dict[str, Any]:
    monkeypatch.setattr(replay_engine, "_load_frame", lambda _item: frame.copy())
    monkeypatch.setattr(replay_engine, "_dividends", lambda _item: {})
    return replay_engine._simulate_stock(
        {"code": "TEST", "name": "測試股"},
        signals,
        [{"date": str(frame.iloc[0]["date"].date()), "event": "WATCHING（納入監控）"}],
        allow_adds=allow_adds,
    )


def test_v2_gate_validation_rejects_unknown_or_missing() -> None:
    assert _all_v2_gates_pass({"anchor": "PASS", "dow": {"status": "PASS"}})
    assert not _all_v2_gates_pass({"anchor": "PASS", "dow": "UNKNOWN"})
    assert not _all_v2_gates_pass({})
    assert not _all_v2_gates_pass(None)


def test_v2_gate_quality_requires_each_gate_to_have_its_own_evidence() -> None:
    gates = {
        f"gate_{index}": {"result": "PASS", "evidence": [f"evidence {index}"]}
        for index in range(8)
    }
    assert _v2_gate_quality(gates, "MACRO_COPY_RESONANCE") == (True, None)
    gates["gate_0"] = {"result": "PASS", "evidence": []}
    assert not _v2_gate_quality(gates, "MACRO_COPY_RESONANCE")[0]


def test_future_dates_detected_anywhere_inside_trigger() -> None:
    trigger = {
        "signal_date": "2026-06-01",
        "macro_anchor": {"start": "2026-04-01", "end": "2026-07-01"},
        "evidence": ["only facts through 2026-06-01"],
    }
    assert _future_dates(trigger, "2026-06-01") == [
        {"path": "trigger.macro_anchor.end", "date": "2026-07-01"}
    ]


def test_nested_stop_is_normalized() -> None:
    assert _stop({"stop": {"source_date": "2026-05-01", "price": 25.5}}) == (
        "2026-05-01",
        25.5,
    )


def test_scenario_gets_chinese_explanation() -> None:
    assert _scenario({"scenario": "MACRO_COPY_RESONANCE"}) == (
        "MACRO_COPY_RESONANCE（大定錨複製共振）"
    )


def test_lifecycle_uses_ai_events_and_adds_initial_watch_when_missing() -> None:
    events = _lifecycle_for(
        "v2",
        {"watchlist_events": [{"date": "2026-06-01", "event": "CAMPAIGN_INVALIDATED"}]},
        "2026-05-01",
    )
    assert events[0]["event"].startswith("WATCHING")
    assert events[1]["event"].startswith("CAMPAIGN_INVALIDATED")


def test_add_candidate_cannot_open_a_new_mother_while_flat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An AI ADD_CANDIDATE is evidence-only after the prior episode has closed."""
    frame = _market_frame(
        [
            {"date": "2026-01-01", "open": 100, "high": 101, "low": 99, "close": 100},
            {"date": "2026-01-02", "open": 100, "high": 102, "low": 99, "close": 101},
            {"date": "2026-01-05", "open": 100, "high": 101, "low": 88, "close": 89},
            {"date": "2026-01-06", "open": 89, "high": 102, "low": 88, "close": 101},
            {"date": "2026-01-07", "open": 101, "high": 103, "low": 100, "close": 102},
        ]
    )
    ledger = {
        "code": "TEST",
        "name": "測試股",
        "v1": {
            "triggers": [
                {
                    "signal_date": "2026-01-01",
                    "scenario": "MACRO_COPY_RESONANCE",
                    "trigger_path": "SMALL_DOW_REVERSAL",
                    "stop_price": 90,
                    "episode_or_add_candidate": "MOTHER_OR_REENTRY",
                },
                {
                    "signal_date": "2026-01-06",
                    "scenario": "MACRO_COPY_RESONANCE",
                    "trigger_path": "CONTINUATION_ADD",
                    "stop_price": 95,
                    "episode_or_add_candidate": "ADD_CANDIDATE",
                },
            ]
        },
    }
    signals = _signals_for(
        "v1",
        ledger,
        frame,
        {"2026-01-01": ["TEST_STRATEGY"]},
        "2026-01-01",
    )

    result = _simulate(monkeypatch, frame, signals, allow_adds=True)

    assert len(result["episodes"]) == 1
    assert all(
        tranche["signal_date"] != "2026-01-06"
        for episode in result["episodes"]
        for tranche in episode["tranches"]
    )


def test_two_r_activation_rechecks_same_close_against_cost_defense(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A +2R intraday touch must not survive a same-day close below the new defense."""
    frame = _market_frame(
        [
            {"date": "2026-02-02", "open": 100, "high": 101, "low": 99, "close": 100},
            {"date": "2026-02-03", "open": 100, "high": 105, "low": 99, "close": 100},
            {"date": "2026-02-04", "open": 110, "high": 121, "low": 98, "close": 99},
            {"date": "2026-02-05", "open": 101, "high": 102, "low": 100, "close": 101},
        ]
    )

    result = _simulate(
        monkeypatch,
        frame,
        [_signal("2026-02-02", stop=90, close=100, intent="MOTHER_OR_REENTRY")],
        allow_adds=False,
    )

    assert len(result["episodes"]) == 1
    episode = result["episodes"][0]
    assert episode["profit_protect_activation_date"] == "2026-02-04"
    assert episode["exit_signal_date"] == "2026-02-04"
    assert episode["exit_date"] == "2026-02-05"
    assert episode["status"].startswith("CLOSED")


def test_add_after_profit_protect_raises_defense_to_new_weighted_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A later fill must not leave cost protection at the pre-add mother cost."""
    frame = _market_frame(
        [
            {"date": "2026-04-01", "open": 100, "high": 101, "low": 99, "close": 100},
            {"date": "2026-04-02", "open": 100, "high": 105, "low": 99, "close": 102},
            {"date": "2026-04-03", "open": 110, "high": 121, "low": 109, "close": 110},
            {"date": "2026-04-06", "open": 120, "high": 122, "low": 114, "close": 115},
            {"date": "2026-04-07", "open": 116, "high": 118, "low": 115, "close": 117},
        ]
    )
    signals = [
        _signal("2026-04-01", stop=90, close=100, intent="MOTHER_OR_REENTRY"),
        _signal("2026-04-03", stop=95, close=110, intent="ADD_CANDIDATE", atr=30),
    ]

    result = _simulate(monkeypatch, frame, signals, allow_adds=True)

    assert len(result["episodes"]) == 1
    episode = result["episodes"][0]
    assert episode["profit_protect"] is True
    assert len(episode["tranches"]) == 2
    weighted_cost = sum(
        float(tranche["entry_price_adjusted"]) * int(tranche["shares"])
        for tranche in episode["tranches"]
    ) / sum(int(tranche["shares"]) for tranche in episode["tranches"])
    assert float(episode["dynamic_defense"]) == pytest.approx(weighted_cost)


def _summary_episode(
    code: str,
    entry_date: str,
    exit_date: str | None,
    deployed_cash: float,
) -> dict[str, Any]:
    status = "OPEN（持有中）" if exit_date is None else "CLOSED（交易已結束）"
    tranche = {
        "role": "MOTHER（母單）",
        "entry_date": entry_date,
        "entry_price_adjusted": 100.0,
        "stop_adjusted": 90.0,
        "shares": 1,
        "buy_cost": deployed_cash,
        "scenario": "TEST_SCENARIO",
        "active_selection_strategies": ["TEST_STRATEGY"],
    }
    if exit_date is not None:
        tranche["sell_date"] = exit_date
    return {
        "code": code,
        "name": code,
        "episode_id": f"{code}-E1",
        "status": status,
        "exit_reason": "AS_OF（截至回測日）" if exit_date is None else "TEST_EXIT",
        "tranches": [tranche],
        "tranche_count": 1,
        "deployed_cash": deployed_cash,
        "net_pnl": 0.0,
        "net_return_on_deployed_pct": 0.0,
        "mfe_pct_from_mother": 0.0,
        "peak_net_liquidation_return_pct": 0.0,
        "mae_pct_from_mother": 0.0,
        "holding_sessions": 1,
    }


def test_concurrent_stock_and_tranche_maxima_are_independent_of_cash_peak() -> None:
    """The most names/fills can occur on a lower-cash day than peak capital."""
    episodes = [
        _summary_episode("A", "2026-03-02", "2026-03-03", 10_000),
        _summary_episode("B", "2026-03-02", "2026-03-03", 10_000),
        _summary_episode("C", "2026-03-04", None, 6_000),
        _summary_episode("D", "2026-03-04", None, 6_000),
        _summary_episode("E", "2026-03-04", None, 6_000),
    ]

    summary = replay_engine._summary({"stocks": [{"episodes": episodes, "audit": []}]})

    assert summary["peak_concurrent_deployed_cash"] == 20_000
    assert summary["peak_date"] == "2026-03-02"
    assert summary["maximum_concurrent_stocks"] == 3
    assert summary["maximum_concurrent_tranches"] == 3
