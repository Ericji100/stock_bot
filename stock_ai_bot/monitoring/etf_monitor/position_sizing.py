"""Cash-specific estimates; no strategy state, account access or order execution."""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from backtests.dual_ma.costs import CostModel, OrderCost
from backtests.dual_ma.models import InstrumentType


RISK_FRACTION = Decimal("0.20")
# Explicit research assumptions, not the user's verified brokerage contract.
ESTIMATE_COST = CostModel(slippage_bps=20, minimum_commission_twd=Decimal("20"))
BOARD_ORDER_SHARES = 499_000
_AMOUNT = re.compile(r"(?:[0-9]{1,13}|[0-9]{1,3}(?:,[0-9]{3}){1,4})(?:\.[0-9]{1,2})?\Z")


class SizingInputError(ValueError):
    """Only fixed public reasons: never include the supplied cash in exceptions."""


@dataclass(frozen=True)
class SplitOrder:
    market: str
    quantity: int
    count: int
    buy: OrderCost
    stop_sale: OrderCost


@dataclass(frozen=True)
class PositionEstimate:
    cash: Decimal
    price: Decimal
    stop: Decimal
    shares: int
    orders: tuple[SplitOrder, ...]
    buy_total: Decimal
    stop_proceeds: Decimal
    initial_risk: Decimal

    @property
    def risk_limit(self):
        return self.cash * RISK_FRACTION


def parse_request(text: str) -> tuple[Decimal, Decimal | None]:
    if len(text) > 60:
        raise SizingInputError("請輸入「可用現金」或「可用現金 委託價」，例如 100000 123.4。")
    parts = text.strip().split()
    if len(parts) not in {1, 2} or any(not _AMOUNT.fullmatch(value) for value in parts):
        raise SizingInputError("請輸入「可用現金」或「可用現金 委託價」，例如 100000 123.4。")
    cash = Decimal(parts[0].replace(",", ""))
    price = None if len(parts) == 1 else Decimal(parts[1].replace(",", ""))
    if not 0 < cash <= Decimal("1000000000000"):
        raise SizingInputError("可用現金須大於零且不超過一兆元。")
    return cash, price


def validate_prices(price: Decimal, stop: Decimal) -> None:
    if not price.is_finite() or not stop.is_finite() or not 0 < stop < price <= Decimal("1000000"):
        raise SizingInputError("參考價或初始防守價缺失／不合法，無法試算股數。")
    tick = Decimal("0.01") if price < 50 else Decimal("0.05")
    if price % tick:
        raise SizingInputError("委託價不符合 0050 跳動單位：未滿 50 元為 0.01 元，50 元以上為 0.05 元。")


def split_orders(shares: int, price: Decimal, stop: Decimal,
                 model: CostModel = ESTIMATE_COST) -> tuple[SplitOrder, ...]:
    board, odd = divmod(shares, 1000)
    batches, remainder = divmod(board * 1000, BOARD_ORDER_SHARES)
    quantities = (("整張", BOARD_ORDER_SHARES, batches), ("整張", remainder, 1), ("零股", odd, 1))
    return tuple(SplitOrder(market, quantity, count, model.buy(price, quantity),
                           model.sell(stop, quantity, InstrumentType.ETF))
                 for market, quantity, count in quantities if quantity and count)


def estimate_position(cash: Decimal, price: Decimal, stop: Decimal,
                      *, model: CostModel = ESTIMATE_COST) -> PositionEstimate | None:
    validate_prices(price, stop)
    if not cash.is_finite() or not 0 < cash <= Decimal("1000000000000"):
        raise SizingInputError("可用現金須大於零且不超過一兆元。")

    def evaluate(shares):
        orders = split_orders(shares, price, stop, model)
        buy = sum((order.buy.cash_amount * order.count for order in orders), Decimal(0))
        sell = sum((order.stop_sale.cash_amount * order.count for order in orders), Decimal(0))
        return PositionEstimate(cash, price, stop, shares, orders, buy, sell, buy - sell)

    def fits(shares):
        if shares == 0:
            return True
        value = evaluate(shares)
        return value.buy_total <= cash and 0 < value.initial_risk <= value.risk_limit

    def largest(low, high, to_shares):
        while low < high:
            middle = (low + high + 1) // 2
            if fits(to_shares(middle)):
                low = middle
            else:
                high = middle - 1
        return low

    # Sizes across a whole-lot boundary are not monotone: an odd-order minimum
    # disappears. First maximize board lots, then maximize the separate odd order.
    lots = largest(0, int(cash / model._fill_price(price, "BUY")) // 1000, lambda n: n * 1000)
    odd = largest(0, 999, lambda n: lots * 1000 + n)
    shares = lots * 1000 + odd
    return None if shares == 0 else evaluate(shares)


def _money(value, *, floor=False):
    return f"{value.quantize(Decimal('0.01'), rounding=ROUND_FLOOR if floor else ROUND_CEILING):,.2f}"


def render_estimate(value: PositionEstimate | None, *, quote, cash: Decimal, override: bool) -> str:
    if value is None:
        return "0050 股數試算\n依本次現金、分筆費用及 20% 估計風險限制，無可買股數。請確認現金與價格。"
    board, odd = divmod(value.shares, 1000)
    board_orders = sum(order.count for order in value.orders if order.market == "整張")
    buy_fee = sum((order.buy.commission * order.count for order in value.orders), Decimal(0))
    sell_fee = sum((order.stop_sale.commission * order.count for order in value.orders), Decimal(0))
    sell_tax = sum((order.stop_sale.tax * order.count for order in value.orders), Decimal(0))
    arrangement = []
    if board:
        arrangement.append(f"整張：{board:,} 張（{board * 1000:,} 股，{board_orders} 筆，每筆最多 499 張）")
    if odd:
        arrangement.append(f"零股：{odd} 股（1 筆）")
    low_regime = quote["regime"] == "SMA21_LT_SMA144"
    return (
        f"0050 股數試算｜{quote['trading_date']} 進場通知\n"
        f"本次可用現金：{_money(cash)} 元\n"
        f"{'自填委託價' if override else '訊號參考價'}：{value.price:f} 元\n"
        f"初始防守價：{value.stop:f} 元\n"
        f"可買股數：{value.shares:,} 股\n" + "\n".join(arrangement) + "\n"
        "本次獨立試算，與先前股數不相加，也不是加碼訊號。\n"
        f"預估買進支出：{_money(value.buy_total)} 元（含手續費 {_money(buy_fee)} 元）\n"
        f"剩餘現金：{_money(cash - value.buy_total, floor=True)} 元\n"
        f"預估防守價賣出實收：{_money(value.stop_proceeds, floor=True)} 元"
        f"（費 {_money(sell_fee)}、稅 {_money(sell_tax)} 元）\n"
        f"預估初始風險：{_money(value.initial_risk)} 元\n"
        f"本次現金 20% 上限：{_money(value.risk_limit, floor=True)} 元\n\n"
        + ("使用訊號價時僅供前一晚參考；價格改變請回覆原進場通知，輸入「現金 委託價」重算。\n" if not override
           else "依自填價格重算，未取得即時報價或驗證當日漲跌幅。\n")
        + ("本訊號仍待晚間完整資料確認。\n" if not quote["confirmed"] else "")
        + ("此分流不執行價格防守，防守價只作初始風險估計。\n" if low_regime else "")
        + "試算假設：雙邊各預留 0.2% 滑價、手續費 0.0399% 且每筆最低 20 元、賣出稅 0.1%；"
          "整張與零股分筆計費，費稅依原模型取整元。未核實你的券商合約，實際費用可能不同。\n"
          "零股成交價可能不同於普通市場開盤，限價委託也可能未成交。20% 僅是估計初始損失上限，"
          "策略依收盤條件出場，跳空等實際損失可超過估值。本功能只試算，不讀帳戶、不下單。"
    )
