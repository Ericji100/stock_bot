from scripts.course_profit_only_stack_backtest import (
    _known_pnl,
    select_profit_only_stack,
)


def _trade(trade_id, family, code, entry_date, signal_date, daily, risk=5.0):
    return {
        "trade_id": trade_id,
        "entry_variant": family,
        "code": code,
        "name": code,
        "entry_date": entry_date,
        "exit_date": None,
        "initial_risk_pct": risk,
        "daily": daily,
        "transactions": [{"type": "BUY", "outflow": 9_980.0}],
        "entry_proposal": {"signal_date": signal_date},
        "net_pnl": 0.0,
    }


def test_known_pnl_uses_only_rows_available_by_signal_close():
    trade = _trade("a", "OLD_MA_RECLAIM", "1", "2026-05-02", "2026-05-01", [
        {"date": "2026-05-02", "net_liquidation_pnl": -10.0},
        {"date": "2026-05-03", "net_liquidation_pnl": 20.0},
        {"date": "2026-05-04", "net_liquidation_pnl": 100.0},
    ])
    assert _known_pnl(trade, "2026-05-03") == 20.0


def test_same_day_dedup_and_profitable_later_add_cap_at_two():
    mother_a = _trade("a", "OLD_MA_RECLAIM", "1", "2026-05-02", "2026-05-01", [
        {"date": "2026-05-02", "net_liquidation_pnl": 10.0},
        {"date": "2026-05-03", "net_liquidation_pnl": 20.0},
    ])
    same_day_b = _trade("b", "OLD_DUAL_RESONANCE", "1", "2026-05-02", "2026-05-01", [
        {"date": "2026-05-02", "net_liquidation_pnl": 10.0},
        {"date": "2026-05-03", "net_liquidation_pnl": 20.0},
    ])
    later_add = _trade("c", "OLD_PULLBACK_RELAUNCH", "1", "2026-05-04", "2026-05-03", [])
    third = _trade("d", "OLD_LARGE_BREAKOUT", "1", "2026-05-05", "2026-05-04", [])
    accepted, skipped, _ = select_profit_only_stack(
        [mother_a, same_day_b, later_add, third], capital_limit=None,
    )
    assert [row["trade_id"] for row in accepted] == ["b", "c"]
    assert accepted[0]["same_day_confirmations"] == ["OLD_DUAL_RESONANCE", "OLD_MA_RECLAIM"]
    assert accepted[1]["stack_role"] == "ADD_1（第1次獲利後加碼）"
    assert skipped[0]["reason"] == "MAX_POSITIONS（單股已有2個部位）"


def test_unprofitable_existing_position_blocks_add():
    mother = _trade("a", "OLD_MA_RECLAIM", "1", "2026-05-02", "2026-05-01", [
        {"date": "2026-05-02", "net_liquidation_pnl": -1.0},
    ])
    add = _trade("b", "OLD_DUAL_RESONANCE", "1", "2026-05-03", "2026-05-02", [])
    accepted, skipped, _ = select_profit_only_stack([mother, add], capital_limit=None)
    assert [row["trade_id"] for row in accepted] == ["a"]
    assert skipped[0]["reason"] == "EXISTING_CAMPAIGN_NOT_PROFITABLE（現有整體部位尚未獲利）"


def test_max_three_allows_second_add_only_when_campaign_is_profitable():
    mother = _trade("a", "OLD_MA_RECLAIM", "1", "2026-05-02", "2026-05-01", [
        {"date": "2026-05-02", "net_liquidation_pnl": 100.0},
        {"date": "2026-05-03", "net_liquidation_pnl": 100.0},
        {"date": "2026-05-04", "net_liquidation_pnl": 100.0},
    ])
    add_one = _trade("b", "OLD_DUAL_RESONANCE", "1", "2026-05-03", "2026-05-02", [
        {"date": "2026-05-03", "net_liquidation_pnl": -20.0},
        {"date": "2026-05-04", "net_liquidation_pnl": -20.0},
    ])
    add_two = _trade("c", "OLD_PULLBACK_RELAUNCH", "1", "2026-05-05", "2026-05-04", [])
    accepted, skipped, _ = select_profit_only_stack(
        [mother, add_one, add_two], capital_limit=None, max_positions_per_stock=3,
    )
    assert [row["trade_id"] for row in accepted] == ["a", "b", "c"]
    assert accepted[-1]["stack_role"] == "ADD_2（第2次獲利後加碼）"
    assert accepted[-1]["existing_campaign_known_pnl_at_add_signal"] == 80.0
    assert skipped == []
