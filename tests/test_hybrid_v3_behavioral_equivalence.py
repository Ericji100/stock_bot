from scripts.hybrid_v3_behavioral_equivalence import compare_signals, compare_trades


def _signal(code, day, *, route="V2_CORE", phase="RR", stop=10.0):
    return {
        "source": "X", "code": code, "name": code, "signal_date": day,
        "route": route, "scenario": "MACRO_COPY_RESONANCE", "phase": phase,
        "stop_date": "2023-01-01", "stop_price": stop, "review_id": day,
    }


def _trade(code, day, *, entry_day="2023-01-03", exit_day="2023-01-10", pnl=100.0):
    return {
        "source": "X", "code": code, "name": code, "signal_date": day,
        "entry_date": entry_day, "entry_price_raw": 11.0,
        "entry_price_adjusted": 10.5, "entry_shares": 909,
        "status": "CLOSED", "exit_signal_date": "2023-01-09",
        "exit_date": exit_day, "exit_price_raw": 12.0, "net_pnl": pnl,
        "route": "V2_CORE", "scenario": "MACRO_COPY_RESONANCE",
    }


def test_signal_categories_include_exact_detail_shift_and_sided_only():
    old = [_signal("A", "2023-01-02"), _signal("B", "2023-01-02"), _signal("C", "2023-01-02"), _signal("D", "2023-01-02")]
    new = [_signal("A", "2023-01-02"), _signal("B", "2023-01-02", phase="LR"), _signal("C", "2023-01-05"), _signal("E", "2023-01-02")]
    result = compare_signals(old, new)
    assert result["counts"] == {
        "ACTION_MATCH_DETAIL_DIFF": 1,
        "DATE_SHIFT": 1,
        "EXACT_MATCH": 1,
        "NEW_ONLY": 1,
        "OLD_ONLY": 1,
    }
    assert result["same_day_action_count"] == 2
    assert result["exact_detail_rate_within_same_day_pct"] == 50.0


def test_trade_comparison_requires_entry_exit_and_pnl_equality():
    old = [_trade("A", "2023-01-02"), _trade("B", "2023-01-02"), _trade("C", "2023-01-02")]
    new = [_trade("A", "2023-01-02"), _trade("B", "2023-01-02", pnl=101.0), _trade("D", "2023-01-02")]
    result = compare_trades(old, new)
    assert result["counts"] == {
        "ENTRY_EXACT_EXIT_DIFF": 1,
        "EXECUTION_EXACT": 1,
        "NEW_TRADE_ONLY": 1,
        "OLD_TRADE_ONLY": 1,
    }
    assert result["observed_record_exact_rate_pct"] == 50.0
    assert result["observed_execution_parity_100"] is False
