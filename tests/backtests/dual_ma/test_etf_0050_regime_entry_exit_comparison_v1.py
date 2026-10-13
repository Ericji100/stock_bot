from datetime import date, timedelta
from decimal import Decimal

from backtests.dual_ma import etf_0050_regime_entry_exit_comparison_v1 as subject
from backtests.dual_ma.costs import CATHAY_WEB_028_TW_V1, size_entry
from backtests.dual_ma.models import Bar, InstrumentType
from backtests.dual_ma.pivots import DowDefenseTracker


def _row(*, ma21="10", ma144="10", close="10", low="9", k="20", d="20"):
    return {
        "date": date(2024, 1, 2), "sma21": Decimal(ma21), "sma144": Decimal(ma144),
        "technical_close": Decimal(close), "technical_low": Decimal(low),
        "entry_k": Decimal(k), "entry_d": Decimal(d),
    }


def test_regime_equality_is_high_and_strict_low_is_low():
    assert subject._regime(_row(ma21="10", ma144="10")) == "HIGH"
    assert subject._regime(_row(ma21="9.999", ma144="10")) == "LOW"


def test_ma21_breakout_is_strict_on_signal_day():
    previous = _row(close="10", ma21="10")
    assert not subject._breakout(previous, _row(close="10", ma21="10"))
    assert subject._breakout(previous, _row(close="10.01", ma21="10"))


def test_relation_equality_is_not_death_or_golden_side():
    assert subject._relation(_row(k="20", d="20")) == "EQ"
    assert subject._relation(_row(k="19.99", d="20")) == "LT"
    assert subject._relation(_row(k="20.01", d="20")) == "GT"


def test_fee_adjusted_position_sizing_respects_cash_and_twenty_percent_risk():
    model = CATHAY_WEB_028_TW_V1.with_research_slippage(20)
    sized = size_entry("100", "80", InstrumentType.ETF, model,
                       max_total_cost=Decimal("500000"),
                       max_initial_r=Decimal("100000"), max_shares=10_000_000)
    assert sized is not None
    assert sized.buy.cash_amount <= Decimal("500000")
    assert Decimal("0") < sized.initial_r <= Decimal("100000")


def test_sealed_hash_constants_are_lowercase_sha256():
    assert len(subject.SEALED_RUN_MANIFEST_SHA256) == 64
    assert len(subject.SEALED_REPORT_MANIFEST_SHA256) == 64
    assert len(subject.SEALED_READBACK_SHA256) == 64
    assert subject.SEALED_RUN_MANIFEST_SHA256 == subject.SEALED_RUN_MANIFEST_SHA256.lower()


def test_stop_is_strict_and_has_priority_over_e1_macd():
    position = {"exit_rule": "E1", "regime": "HIGH", "effective_stop": Decimal("90"),
                "macd_armed": True, "two_r_armed": False}
    assert subject.resolve_exit_reason(close=Decimal("90"), position=position,
                                       macd_death=False, kd_death=False) is None
    assert subject.resolve_exit_reason(close=Decimal("89.99"), position=position,
                                       macd_death=True, kd_death=False) == "FIXED_STOP"


def test_e1_requires_arm_and_e2_requires_2r_arm_plus_new_cross():
    e1 = {"exit_rule": "E1", "regime": "HIGH", "effective_stop": Decimal("90"),
          "macd_armed": False, "two_r_armed": False}
    assert subject.resolve_exit_reason(close=Decimal("100"), position=e1,
                                       macd_death=True, kd_death=False) is None
    e1["macd_armed"] = True
    assert subject.resolve_exit_reason(close=Decimal("100"), position=e1,
                                       macd_death=True, kd_death=False) == "E1_MACD_DEATH"
    e2 = {"exit_rule": "E2", "regime": "LOW", "effective_stop": Decimal("90"),
          "macd_armed": True, "two_r_armed": False}
    assert subject.resolve_exit_reason(close=Decimal("100"), position=e2,
                                       macd_death=False, kd_death=True) is None
    e2["two_r_armed"] = True
    assert subject.resolve_exit_reason(close=Decimal("100"), position=e2,
                                       macd_death=False, kd_death=True) == "E2_POST_2R_KD_DEATH"


def test_e3_defense_only_rises_and_legacy_low_suppression_is_explicit():
    assert subject.raised_defense(Decimal("90"), Decimal("80")) == Decimal("90")
    assert subject.raised_defense(Decimal("90"), Decimal("95")) == Decimal("95")
    low = {"exit_rule": "E1", "regime": "LOW", "effective_stop": Decimal("90"),
           "macd_armed": False, "two_r_armed": False}
    assert subject.resolve_exit_reason(close=Decimal("80"), position=low,
                                       macd_death=False, kd_death=False) == "FIXED_STOP"
    assert subject.resolve_exit_reason(close=Decimal("80"), position=low,
                                       macd_death=False, kd_death=False,
                                       legacy_low_stop_inactive=True) is None


def test_e3_n2_pivot_requires_confirmation_then_later_close_breakout():
    values = [(10, 8, 9), (11, 9, 10), (15, 10, 14), (12, 9, 10),
              (11, 8, 9), (10, 5, 6), (11, 7, 9), (12, 8, 10), (16, 9, 16)]
    bars = []
    tracker = DowDefenseTracker()
    emitted = []
    for index, (high, low, close) in enumerate(values):
        day = date(2024, 1, 1) + timedelta(days=index)
        bars.append(Bar.from_values(day, close, high, low, close))
        _, _, new = tracker.update(bars)
        if index < 8:
            assert not new
        emitted.extend(new)
    assert len(emitted) == 1
    assert emitted[0].pivot_low.confirmed_date == date(2024, 1, 8)
    assert emitted[0].activated_date == date(2024, 1, 9)
    assert emitted[0].defense == Decimal("5")
