from decimal import Decimal

from backtests.dual_ma.costs import CostModel, size_entry
from backtests.dual_ma.models import InstrumentType


def test_per_order_twd_rounding_and_instrument_tax() -> None:
    model = CostModel()
    buy = model.buy(Decimal("100"), 10)
    stock_sale = model.sell(Decimal("100"), 10, InstrumentType.STOCK)
    etf_sale = model.sell(Decimal("100"), 10, InstrumentType.ETF)

    assert buy.commission == Decimal("0")
    assert stock_sale.tax == Decimal("3")
    assert etf_sale.tax == Decimal("1")
    assert "no minimum" in model.minimum_fee_assumption


def test_sizing_descends_and_obeys_notional_risk_and_share_caps() -> None:
    sized = size_entry(Decimal("100"), Decimal("90"), InstrumentType.STOCK)

    assert sized is not None
    assert 1 <= sized.shares <= 999
    assert sized.buy.cash_amount <= Decimal("10000")
    assert Decimal("0") < sized.initial_r <= Decimal("2000")
    assert size_entry(Decimal("10000"), Decimal("9000"), InstrumentType.STOCK) is None


def test_slippage_is_applied_on_both_sides() -> None:
    model = CostModel().with_slippage(20)
    assert model.buy(100, 1).fill_price == Decimal("100.200")
    assert model.sell(100, 1, InstrumentType.STOCK).fill_price == Decimal("99.800")


def test_breakeven_defense_really_covers_rounded_costs() -> None:
    model = CostModel().with_slippage(10)
    buy = model.buy(Decimal("87.3"), 113)
    defense = model.breakeven_reference_price(
        113, buy.cash_amount, InstrumentType.BENEFICIARY
    )

    assert model.sell(defense, 113, InstrumentType.BENEFICIARY).cash_amount >= buy.cash_amount
