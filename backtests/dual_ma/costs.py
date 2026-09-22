from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP

from .models import InstrumentType, decimal


TWD = Decimal("1")
BPS = Decimal("10000")


@dataclass(frozen=True)
class OrderCost:
    side: str
    reference_price: Decimal
    fill_price: Decimal
    shares: int
    gross: Decimal
    commission: Decimal
    tax: Decimal
    cash_amount: Decimal


@dataclass(frozen=True)
class SizedEntry:
    shares: int
    buy: OrderCost
    estimated_stop_sale: OrderCost
    initial_r: Decimal


@dataclass(frozen=True)
class CostModel:
    model_id: str = "CATHAY_WEB_028_TW_V1"
    commission_rate: Decimal = Decimal("0.000399")
    stock_sell_tax_rate: Decimal = Decimal("0.003")
    etf_sell_tax_rate: Decimal = Decimal("0.001")
    slippage_bps: int = 0
    minimum_commission_twd: Decimal = Decimal("0")
    rounding: str = ROUND_HALF_UP

    @property
    def minimum_fee_assumption(self) -> str:
        return (
            "minimum commission unavailable in frozen spec; baseline uses TWD-level "
            "rounded percentage commission with no minimum"
        )

    def with_slippage(self, bps: int) -> "CostModel":
        if bps not in (0, 10, 20):
            raise ValueError("v0.4 scenarios require 0, 10, or 20 bps per side")
        return replace(self, slippage_bps=bps)

    def _commission(self, gross: Decimal) -> Decimal:
        fee = (gross * self.commission_rate).quantize(TWD, rounding=self.rounding)
        return max(fee, self.minimum_commission_twd)

    def _fill_price(self, reference_price: Decimal, side: str) -> Decimal:
        direction = Decimal("1") if side == "BUY" else Decimal("-1")
        return reference_price * (
            Decimal("1") + direction * decimal(self.slippage_bps) / BPS
        )

    def buy(self, reference_price: Decimal | int | float | str, shares: int) -> OrderCost:
        return self._order(reference_price, shares, "BUY", InstrumentType.STOCK)

    def sell(
        self,
        reference_price: Decimal | int | float | str,
        shares: int,
        instrument_type: InstrumentType,
    ) -> OrderCost:
        return self._order(reference_price, shares, "SELL", instrument_type)

    def _order(
        self,
        reference_price: Decimal | int | float | str,
        shares: int,
        side: str,
        instrument_type: InstrumentType,
    ) -> OrderCost:
        if shares < 1:
            raise ValueError("shares must be positive")
        ref = decimal(reference_price)
        if ref <= 0:
            raise ValueError("reference price must be positive")
        fill = self._fill_price(ref, side)
        gross = fill * shares
        commission = self._commission(gross)
        tax_rate = Decimal("0")
        if side == "SELL":
            tax_rate = (
                self.stock_sell_tax_rate
                if instrument_type == InstrumentType.STOCK
                else self.etf_sell_tax_rate
            )
        tax = (gross * tax_rate).quantize(TWD, rounding=self.rounding)
        cash = gross + commission if side == "BUY" else gross - commission - tax
        return OrderCost(side, ref, fill, shares, gross, commission, tax, cash)

    def immediate_sale_pnl(
        self,
        reference_price: Decimal,
        shares: int,
        buy_total: Decimal,
        instrument_type: InstrumentType,
    ) -> Decimal:
        return self.sell(reference_price, shares, instrument_type).cash_amount - buy_total

    def breakeven_reference_price(
        self,
        shares: int,
        buy_total: Decimal,
        instrument_type: InstrumentType,
    ) -> Decimal:
        """Smallest close proxy (to 1e-8 TWD) whose simulated sale covers buy cost."""
        low = Decimal("0")
        high = buy_total / shares * Decimal("2")
        while self.sell(high, shares, instrument_type).cash_amount < buy_total:
            high *= 2
        for _ in range(96):
            middle = (low + high) / 2
            if self.sell(middle, shares, instrument_type).cash_amount >= buy_total:
                high = middle
            else:
                low = middle
        return high.quantize(Decimal("0.00000001"), rounding=ROUND_CEILING)


CATHAY_WEB_028_TW_V1 = CostModel()


def size_entry(
    entry_reference_close: Decimal | int | float | str,
    initial_stop: Decimal | int | float | str,
    instrument_type: InstrumentType,
    cost_model: CostModel = CATHAY_WEB_028_TW_V1,
    max_total_cost: Decimal = Decimal("10000"),
    max_initial_r: Decimal = Decimal("2000"),
    max_shares: int = 999,
) -> SizedEntry | None:
    """Descend from the largest possible integer quantity to the first valid size."""
    entry = decimal(entry_reference_close)
    stop = decimal(initial_stop)
    if stop >= entry or stop <= 0:
        return None
    upper = min(max_shares, int(max_total_cost / cost_model._fill_price(entry, "BUY")))
    for shares in range(upper, 0, -1):
        buy = cost_model.buy(entry, shares)
        if buy.cash_amount > max_total_cost:
            continue
        stop_sale = cost_model.sell(stop, shares, instrument_type)
        risk = buy.cash_amount - stop_sale.cash_amount
        if Decimal("0") < risk <= max_initial_r:
            return SizedEntry(shares, buy, stop_sale, risk)
    return None
