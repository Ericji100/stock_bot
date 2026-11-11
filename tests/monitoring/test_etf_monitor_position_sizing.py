from decimal import Decimal as D

import pytest

from backtests.dual_ma.costs import CostModel
from backtests.dual_ma.models import InstrumentType
from stock_ai_bot.monitoring.etf_monitor.position_sizing import (
    SizingInputError, estimate_position, parse_request, render_estimate, split_orders,
)


def test_delegated_831_share_example_is_exact():
    value = estimate_position(D("100000"), D("120"), D("108"))
    assert value.shares == 831
    assert value.buy_total == D("99959.44")
    assert value.initial_risk == D("10516.936")
    assert [(order.market, order.quantity, order.count) for order in value.orders] == [("零股", 831, 1)]
    text = render_estimate(value, quote={"trading_date": "2026-11-11", "regime": "SMA21_GE_SMA144", "confirmed": True}, cash=value.cash, override=False)
    for required in ("831 股", "20,000.00", "前一晚參考", "未核實你的券商合約", "可能未成交", "可超過估值", "不下單"):
        assert required in text


def test_board_and_odd_split_costs_both_sides_per_order():
    model = CostModel(slippage_bps=20, minimum_commission_twd=D("20"))
    orders = split_orders(1001, D("1"), D("0.9"), model)
    assert [(order.market, order.quantity) for order in orders] == [("整張", 1000), ("零股", 1)]
    assert sum(order.buy.commission for order in orders) == 40
    assert sum(order.stop_sale.commission for order in orders) == 40
    # Treating both markets as a single order would miss a minimum on each side.
    assert sum(order.buy.cash_amount for order in orders) == model.buy(D(1), 1001).cash_amount + 20
    value = estimate_position(D("250000"), D("120"), D("108"))
    assert value.shares > 2000 and value.shares % 1000
    assert {order.market for order in value.orders} == {"整張", "零股"}
    assert value.buy_total <= value.cash and value.initial_risk <= value.risk_limit


@pytest.mark.parametrize("cash,price,stop", [
    ("1022", "1", "0.9"), ("2030", "1", "0.9"), ("5010", "1", "0.4"),
    ("100000", "120", "108"), ("100000", "120", "50"), ("50", "50", "49"),
])
def test_maximum_quantity_matches_independent_integer_enumeration(cash, price, stop):
    cash, price, stop = D(cash), D(price), D(stop)
    model = CostModel(slippage_bps=20, minimum_commission_twd=D("20"))
    best = 0
    for shares in range(1, int(cash / (price * D("1.002"))) + 1):
        lots, odd = divmod(shares, 1000)
        amounts = [n for n in (lots * 1000, odd) if n]
        buy = sum(model.buy(price, n).cash_amount for n in amounts)
        sell = sum(model.sell(stop, n, InstrumentType.ETF).cash_amount for n in amounts)
        if buy <= cash and 0 < buy - sell <= cash * D("0.2"):
            best = shares
    result = estimate_position(cash, price, stop)
    assert (0 if result is None else result.shares) == best


def test_risk_cap_binds_before_cash_and_cash_never_resets():
    value = estimate_position(D("100000"), D("120"), D("50"))
    assert value.buy_total < D("50000")
    assert value.initial_risk <= D("20000")
    more = split_orders(value.shares + 1, D("120"), D("50"))
    assert sum(order.buy.cash_amount - order.stop_sale.cash_amount for order in more) > value.risk_limit
    assert estimate_position(D("200000"), D("120"), D("108")).shares > estimate_position(D("100000"), D("120"), D("108")).shares


def test_large_board_orders_are_batched_without_per_share_or_per_order_loops():
    orders = split_orders(1_000_999, D("120"), D("108"))
    assert [(order.quantity, order.count) for order in orders] == [(499000, 2), (2000, 1), (999, 1)]
    assert sum(order.quantity * order.count for order in orders) == 1_000_999
    value = estimate_position(D("1000000000000"), D("0.01"), D("0.009"))
    assert value.shares > 1_000_000 and len(value.orders) <= 3
    assert value.buy_total <= value.cash and value.initial_risk <= value.risk_limit


@pytest.mark.parametrize("text", ["0", "-100", "NaN", "Infinity", "1e9", "1,00", "100 120 108", "1000000000001", "9" * 80])
def test_invalid_cash_does_not_leak_value(text):
    with pytest.raises(SizingInputError) as caught:
        parse_request(text)
    assert text not in str(caught.value) or text == "0"


def test_price_override_tick_stop_and_missing_cash_cases():
    assert parse_request("100,000 123.4") == (D("100000"), D("123.4"))
    before = estimate_position(D("100000"), D("120"), D("108"))
    after = estimate_position(D("100000"), D("123.4"), D("108"))
    assert after.shares < before.shares
    for price, stop in (("120.03", "108"), ("120", "120"), ("120", "0"), ("NaN", "108"), ("120", "NaN")):
        with pytest.raises(SizingInputError):
            estimate_position(D("100000"), D(price), D(stop))
    assert estimate_position(D("10"), D("120"), D("108")) is None


def test_low_regime_does_not_promise_price_stop_execution():
    value = estimate_position(D("100000"), D("120"), D("108"))
    text = render_estimate(value, quote={"trading_date": "2026-11-11", "regime": "SMA21_LT_SMA144", "confirmed": False}, cash=value.cash, override=True)
    assert "此分流不執行價格防守" in text and "待晚間完整資料確認" in text
