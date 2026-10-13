from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import Decimal

import pytest

from backtests.dual_ma import etf_0050_base_v1 as adapter
from backtests.dual_ma.costs import CATHAY_WEB_028_TW_V1
from backtests.dual_ma.engine import (
    _StrategyState,
    _apply_signal_price_rebase_to_strategy_state,
    _apply_split_to_strategy_state,
    _split_adjusted_bar,
)
from backtests.dual_ma.etf_0050_base_v1 import (
    STOP_WAIT_01_PACKAGE,
    STRATEGY_SPECS,
    CashDividendEvent,
    ETF0050Error,
    ETFPackageNotReady,
    SplitAwarePosition,
    SplitEvent,
    eligibility_sessions,
    evaluate_split_acceptance,
    fixed_detail_sample,
    mark_open_position,
    prepare_wait_checkpoint,
    render_year_report,
    render_overview_report,
    simulate_buy_and_hold,
    validate_primary_split_evidence,
    validate_ready_package,
)
from backtests.dual_ma.models import Bar, InstrumentType, Leg, Pullback, StrategyKind
from backtests.dual_ma.etf_0050_formal_v1 import (
    END,
    build_corporate_ledgers,
    compute_buy_hold,
    load_package,
)


def _sha(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _leg() -> Leg:
    return Leg(
        leg_id="0050-L1",
        symbol="0050",
        strategy=StrategyKind.LONG,
        entry_date=date(2025, 6, 17),
        entry_reference_close=Decimal("100"),
        entry_fill_price=Decimal("100"),
        shares=100,
        buy_gross=Decimal("10000"),
        buy_commission=Decimal("4"),
        buy_total=Decimal("10004"),
        initial_stop=Decimal("90"),
        initial_r=Decimal("1010"),
        effective_stop=Decimal("92"),
        breakeven_stop=Decimal("100.1"),
        dow_stop=Decimal("91"),
    )


def test_every_official_session_is_eligible_and_2026_cutoff_is_inclusive() -> None:
    sessions = [date(2026, 1, 2), date(2026, 9, 30), date(2026, 10, 1)]
    assert eligibility_sessions(2026, sessions, cutoff=date(2026, 9, 30)) == tuple(
        sessions[:2]
    )


def test_five_baselines_are_exact_and_have_no_kd_variant() -> None:
    assert [spec.strategy_id for spec in STRATEGY_SPECS] == [
        "A_MEDIUM",
        "A_LONG",
        "B_MEDIUM",
        "B_LONG",
        "C_LONG",
    ]
    assert all("KD" not in spec.strategy_id for spec in STRATEGY_SPECS)


def test_2025_split_rebases_all_live_state_and_preserves_value() -> None:
    state = _StrategyState(
        StrategyKind.LONG,
        pullback=Pullback(10, date(2025, 6, 16), Decimal("88")),
        open_legs=[_leg()],
        active_dow_defense=Decimal("91"),
    )
    before_cost = state.open_legs[0].buy_total
    before_value = Decimal(state.open_legs[0].shares) * Decimal("100")
    _apply_split_to_strategy_state(state, Decimal("4"))
    leg = state.open_legs[0]
    assert leg.shares == 400
    assert leg.entry_fill_price == Decimal("25")
    assert leg.initial_stop == Decimal("22.5")
    assert leg.effective_stop == Decimal("23")
    assert leg.breakeven_stop == Decimal("25.025")
    assert leg.dow_stop == Decimal("22.75")
    assert state.pullback.low == Decimal("22")
    assert state.active_dow_defense == Decimal("22.75")
    assert leg.buy_total == before_cost
    assert Decimal(leg.shares) * Decimal("25") == before_value


def test_pre_adjusted_signal_coordinate_is_not_divided_twice_at_split() -> None:
    state = _StrategyState(
        StrategyKind.LONG,
        pullback=Pullback(10, date(2025, 6, 10), Decimal("45")),
        open_legs=[_leg()],
        active_dow_defense=Decimal("44"),
    )
    state.open_legs[0].initial_stop = Decimal("43")
    _apply_split_to_strategy_state(
        state, Decimal("4"), signal_coordinate_already_adjusted=True
    )
    assert state.open_legs[0].shares == 400
    assert state.open_legs[0].entry_fill_price == Decimal("25")
    assert state.open_legs[0].initial_stop == Decimal("43")
    assert state.pullback.low == Decimal("45")
    assert state.active_dow_defense == Decimal("44")


def test_cash_action_rebases_signal_state_without_changing_raw_economics() -> None:
    state = _StrategyState(
        StrategyKind.LONG,
        pullback=Pullback(10, date(2024, 1, 16), Decimal("88")),
        open_legs=[_leg()],
        active_dow_defense=Decimal("91"),
    )
    before = (state.open_legs[0].shares, state.open_legs[0].entry_fill_price, state.open_legs[0].buy_total)
    _apply_signal_price_rebase_to_strategy_state(state, Decimal("0.98"))
    leg = state.open_legs[0]
    assert (leg.shares, leg.entry_fill_price, leg.buy_total) == before
    assert state.pullback.low == Decimal("86.24")
    assert state.active_dow_defense == Decimal("89.18")
    assert leg.entry_reference_close == Decimal("98")
    assert leg.initial_stop == Decimal("88.2")
    assert leg.effective_stop == Decimal("90.16")


def test_event_minus_one_event_and_plus_one_fixture_has_zero_fake_stop() -> None:
    prior = Bar.from_values(date(2025, 6, 17), 99, 101, 98, 100)
    event = Bar.from_values(date(2025, 6, 18), 24.8, 25.3, 24.6, 25)
    after = Bar.from_values(date(2025, 6, 19), 25, 25.5, 24.9, 25.2)
    adjusted_prior = _split_adjusted_bar(prior, Decimal("4"))
    assert event.close < Decimal("90")  # the broken raw-coordinate engine's fake stop
    adjusted_stop = Decimal("90") / Decimal("4")
    assert adjusted_prior.close == event.close
    assert event.close >= adjusted_stop
    assert after.close >= adjusted_stop


def test_true_split_gate_requires_exact_first_party_terms_and_zero_false_stops() -> None:
    event = SplitEvent(
        date(2025, 6, 18),
        Decimal("4"),
        official_pre_split_reference=Decimal("100"),
        official_post_split_reference=Decimal("25"),
        primary_source_url="https://example.invalid/first-party-fixture",
        primary_source_sha256="a" * 64,
    )
    validate_primary_split_evidence(event)
    acceptance = evaluate_split_acceptance(
        event=event,
        official_sessions=[
            date(2025, 6, 17),
            date(2025, 6, 18),
            date(2025, 6, 19),
        ],
        transactions=[],
        false_stop_count=0,
    )
    assert acceptance.status == "PASS"
    assert acceptance.false_stop_count == 0
    with pytest.raises(ETF0050Error, match="first-party"):
        validate_primary_split_evidence(
            SplitEvent(date(2025, 6, 18), Decimal("4"))
        )
    actual = SplitEvent(
        date(2025, 6, 18),
        Decimal("4"),
        official_pre_split_reference=Decimal("188.65"),
        official_post_split_reference=Decimal("47.16"),
        exact_post_split_reference=Decimal("47.1625"),
        primary_source_url="https://www.twse.com.tw/fixture",
        primary_source_sha256="b" * 64,
    )
    validate_primary_split_evidence(actual)
    assert actual.exact_post_split_reference == Decimal("188.65") / 4
    broken = SplitEvent(
        date(2025, 6, 18),
        Decimal("4"),
        official_pre_split_reference=Decimal("188.65"),
        official_post_split_reference=Decimal("47.57"),
        exact_post_split_reference=Decimal("47.1625"),
        primary_source_url="https://www.twse.com.tw/fixture",
        primary_source_sha256="b" * 64,
    )
    with pytest.raises(ETF0050Error, match="tick rounding"):
        validate_primary_split_evidence(broken)


def test_split_ledger_is_not_a_trade_dividend_or_pnl_event() -> None:
    position = SplitAwarePosition(
        shares=Decimal("100"),
        total_cost=Decimal("10004"),
        per_share_cost=Decimal("100.04"),
        initial_stop=Decimal("90"),
        effective_stop=Decimal("92"),
        add_price=Decimal("110"),
        r_per_share=Decimal("10.1"),
        pending_thresholds={"two_r": Decimal("120")},
    )
    before = position.market_value(Decimal("100"))
    position.apply_split(SplitEvent(date(2025, 6, 18), Decimal("4")))
    assert position.market_value(Decimal("25")) == before
    assert position.total_cost == Decimal("10004")
    assert position.cash_dividends == 0
    assert position.transactions == []
    assert position.pending_thresholds["two_r"] == Decimal("30")
    assert position.action_audit[0]["type"] == "SPLIT_REBASE_ONLY"


def test_cash_dividend_and_buyhold_reinvestment_use_raw_execution_price() -> None:
    prices = {
        date(2025, 1, 2): Decimal("100"),
        date(2025, 6, 18): Decimal("25"),
        date(2025, 7, 15): Decimal("26"),
        date(2025, 12, 31): Decimal("30"),
    }
    common = dict(
        raw_close_by_date=prices,
        initial_cash=Decimal("100000"),
        splits=[SplitEvent(date(2025, 6, 18), Decimal("4"))],
        dividends=[
            CashDividendEvent(
                date(2025, 7, 10), date(2025, 7, 15), Decimal("0.5")
            )
        ],
    )
    cash = simulate_buy_and_hold(**common, reinvest=False)
    reinvest = simulate_buy_and_hold(**common, reinvest=True)
    assert cash.dividends_received > 0
    assert reinvest.shares > cash.shares
    order = next(row for row in reinvest.transactions if row["role"] == "DIVIDEND_REINVEST")
    assert order["cash"] == CATHAY_WEB_028_TW_V1.buy(Decimal("26"), order["shares"]).cash_amount


def test_dividend_right_is_locked_on_ex_date_not_payment_date() -> None:
    event = CashDividendEvent(
        date(2025, 7, 21), date(2025, 8, 8), Decimal("0.36")
    )
    split = SplitEvent(date(2025, 6, 18), Decimal("4"))
    sold_after_ex = [
        {
            "date": date(2025, 7, 1),
            "role": "MOTHER",
            "status": "FILLED",
            "shares": 400,
            "leg_id": "L1",
        },
        {
            "date": date(2025, 7, 22),
            "role": "EXIT",
            "shares": 400,
            "leg_id": "L1",
        },
    ]
    ledger = build_corporate_ledgers(
        transactions=sold_after_ex,
        dividends=[event],
        split=split,
        start=date(2025, 1, 1),
        end=date(2025, 12, 31),
    )
    assert ledger.dividend_cash_by_leg["L1"] == Decimal("144")
    bought_after_ex = [
        {
            "date": date(2025, 7, 22),
            "role": "MOTHER",
            "status": "FILLED",
            "shares": 400,
            "leg_id": "L2",
        }
    ]
    ledger = build_corporate_ledgers(
        transactions=bought_after_ex,
        dividends=[event],
        split=split,
        start=date(2025, 1, 1),
        end=date(2025, 12, 31),
    )
    assert ledger.dividend_cash_by_leg == {}


def test_pre_split_holding_gets_four_times_shares_before_post_split_dividend() -> None:
    event = CashDividendEvent(
        date(2025, 7, 21), date(2025, 8, 8), Decimal("0.36")
    )
    ledger = build_corporate_ledgers(
        transactions=[
            {
                "date": date(2025, 6, 10),
                "role": "MOTHER",
                "status": "FILLED",
                "shares": 1000,
                "leg_id": "L1",
            }
        ],
        dividends=[event],
        split=SplitEvent(date(2025, 6, 18), Decimal("4")),
        start=date(2025, 1, 1),
        end=date(2025, 12, 31),
    )
    assert ledger.splits[0]["shares_after"] == 4000
    assert ledger.dividend_cash_by_leg["L1"] == Decimal("1440")
    assert ledger.splits[0]["transaction"] is False


def test_real_v3_buyhold_reinvestment_and_2026_cutoff_mark() -> None:
    package = load_package(adapter.PACKAGE_ROOT_V3)
    cash, _, _, cash_curve, _ = compute_buy_hold(
        package=package, year=2026, slippage_bps=20, reinvest=False
    )
    reinvest, _, _, reinvest_curve, _ = compute_buy_hold(
        package=package, year=2026, slippage_bps=20, reinvest=True
    )
    assert cash_curve[-1]["date"] == END
    assert reinvest_curve[-1]["date"] == END
    assert cash["open_rounds"] == reinvest["open_rounds"] == 1
    assert cash["cutoff_estimated_pnl"] is not None
    assert reinvest["cumulative_buy"] >= cash["cumulative_buy"]


def test_etf_sell_tax_is_one_tenth_percent_and_raw_mark_is_separate() -> None:
    sale = CATHAY_WEB_028_TW_V1.sell(Decimal("25"), 400, InstrumentType.ETF)
    assert sale.tax == Decimal("10")
    assert sale.reference_price == Decimal("25")
    assert sale.fill_price == Decimal("25")
    mark = mark_open_position(
        raw_reference_price=Decimal("25"),
        shares=400,
        total_cost=Decimal("9000"),
        cash_dividends=Decimal("100"),
    )
    assert mark.hypothetical_sell_net == sale.cash_amount
    assert mark.unrealized_total_return == sale.cash_amount + 100 - 9000


def test_ready_gate_checks_real_adopted_v3_artifacts_sources_and_readback() -> None:
    marker = validate_ready_package(adapter.PACKAGE_ROOT_V3)
    assert marker["ready"] is True
    assert marker["data_version"] == "etf-research-package-v3"
    assert len(marker["ready_checks"]) == 4


def test_rejected_v1_and_v2_roots_can_never_authorize_a_run() -> None:
    for rejected in adapter.REJECTED_PACKAGE_ROOTS:
        with pytest.raises(ETFPackageNotReady, match="rejected package root"):
            validate_ready_package(rejected)


def test_wait_checkpoint_does_not_touch_old_run_or_claim_performance(tmp_path) -> None:
    package = tmp_path / "package"
    output = tmp_path / "etf-output"
    old = tmp_path / "old-run"
    old.mkdir()
    frozen = old / "report.md"
    frozen.write_text("old", encoding="utf-8")
    before = _sha(frozen)
    checkpoint = prepare_wait_checkpoint(
        package_root=package, output_root=output, old_run_root=old
    )
    assert checkpoint["status"] == STOP_WAIT_01_PACKAGE
    assert checkpoint["performance_reports_generated"] is False
    assert checkpoint["old_run_unchanged"] is True
    assert _sha(frozen) == before
    assert not any(output.glob("20??-report.md"))


def test_report_and_sampling_are_fixed_and_require_verified_payload() -> None:
    rows = list(range(20))
    assert fixed_detail_sample(rows) == fixed_detail_sample(rows)
    assert len(fixed_detail_sample(rows)) == 12
    with pytest.raises(ETF0050Error, match="unverified"):
        render_year_report(2025, {"status": "WAIT"})
    payload = {
        "status": "PASS",
        "plain_conclusion": "fixture",
        "comparison_table": "fixture",
        "pnl_table": "fixture",
        "risk_table": "fixture",
        "cost_table": "fixture",
        "detail_summary": "fixture",
        "causal_notes": "fixture",
    }
    report = render_year_report(2025, payload)
    assert "買進持有、股利再投入、五個策略" in report
    assert "實現損益、未平倉市值與合計" in report
    overview = render_overview_report(
        {
            "status": "PASS",
            "plain_conclusion": "fixture",
            "comparison_table": "fixture",
            "defense_2022": "fixture",
            "bull_participation": "fixture",
            "stability_table": "fixture",
            "source_groups": "fixture",
        }
    )
    assert "2022 防守能力" in overview
    assert "多頭參與率" in overview
