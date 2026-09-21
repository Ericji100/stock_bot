import pandas as pd

from scripts.course_tg_strategy_entry_matrix import (
    _active_context,
    _map_selections_to_sessions,
    _return_distribution,
    fixed_horizon_return,
    render_trade_report,
)


def test_nontrading_selection_maps_to_next_market_session():
    raw = pd.DataFrame({"date": pd.to_datetime(["2026-05-01", "2026-05-04", "2026-05-05"])})
    rows = [{"date": "2026-05-02", "strategy": "A"}]
    mapped = _map_selections_to_sessions(raw, rows)
    assert list(mapped) == [1]


def test_active_strategy_window_is_independent_per_strategy():
    active, dates = _active_context({"A": (10, "2026-05-01"), "B": (80, "2026-08-01")}, 81, 60)
    assert active == ["B"]
    assert dates == {"B": "2026-08-01"}


def test_fixed_horizon_uses_twentieth_daily_net_liquidation():
    trade = {
        "status": "OPEN",
        "daily": [{"net_liquidation_pnl": float(index)} for index in range(20)],
    }
    assert fixed_horizon_return(trade, 20) == 0.19


def test_fixed_horizon_keeps_earlier_realized_exit():
    trade = {"status": "CLOSED", "net_pnl": 250.0, "daily": [{"net_liquidation_pnl": 100.0}]}
    assert fixed_horizon_return(trade, 20) == 2.5


def test_immature_open_trade_is_excluded_from_rule_mapping():
    trade = {"status": "OPEN", "daily": [{"net_liquidation_pnl": 100.0}]}
    assert fixed_horizon_return(trade, 20) is None


def test_return_distribution_has_mutually_exclusive_buckets():
    trades = [
        {"net_return_on_campaign_budget_pct": value}
        for value in [-12.0, -10.0, -0.1, 0.0, 9.9, 10.0, 19.9, 20.0]
    ]
    assert _return_distribution(trades) == [
        ("低於 -10%", 1),
        ("-10%～低於 0%", 2),
        ("0%～低於 10%", 2),
        ("10%～低於 20%", 2),
        ("20%以上", 1),
    ]


def test_trade_report_separates_realized_and_open_rows():
    base = {
        "trade_id": "T1", "code": "1101", "name": "台泥",
        "entry_date": "2026-05-05", "entry_price": 10.0, "initial_defense": 9.0,
        "net_pnl": 100.0, "net_return_on_campaign_budget_pct": 1.0,
        "max_campaign_gross_return_pct": 5.0, "max_campaign_date": "2026-05-06",
        "entry_variant": "OLD_MA_RECLAIM", "trigger_family": "OLD_MA_RECLAIM",
        "entry_proposal": {"signal_date": "2026-05-04", "active_strategies": ["TECH_MA5_RECLAIM"]},
    }
    closed = {
        **base, "status": "CLOSED", "status_label": "CLOSED（交易已結束）",
        "exit_date": "2026-05-07", "exit_price": 10.1,
        "exit_reason_label": "FIXED_DEFENSE（跌破初始結構防線）",
        "performance_date": "2026-05-07", "daily": [],
    }
    opened = {
        **base, "trade_id": "T2", "status": "OPEN", "status_label": "OPEN（持有中）",
        "exit_date": None, "exit_price": None, "exit_reason_label": "AS_OF（截至日估值）",
        "performance_date": "2026-09-04", "daily": [{"raw_close": 10.2}],
    }
    payload = {
        "as_of": "2026-09-04", "method_version": "test",
        "results": {"OLD_MA_RECLAIM": {"summary": {
            "net_pnl": 200.0, "return_on_500k_pct": 0.04,
            "max_concurrent_positions": 2, "average_mfe_pct": 5.0, "mfe20_count": 0,
        }, "trades": [closed, opened]}},
    }
    report = render_trade_report(payload, "OLD_MA_RECLAIM")
    assert "已實現損益" in report
    assert "OPEN（持有中／未實現）" in report
    assert "2026-09-04／10.20" in report
