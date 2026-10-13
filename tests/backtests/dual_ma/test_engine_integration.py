import json
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from backtests.dual_ma import (
    AddVariant,
    Bar,
    DualMABacktestEngine,
    ExitReason,
    ExitVariant,
    RunManifest,
    SPEC_SHA256,
    STRATEGY_VERSION,
    StrategyKind,
    run_matrix,
)


FIXTURE = (
    Path(__file__).parents[3]
    / "backtests"
    / "dual_ma"
    / "fixtures"
    / "causal_replay_golden.json"
)


def bar(day: date, close: Decimal, low: Decimal | None = None) -> Bar:
    actual_low = low if low is not None else close - Decimal("0.2")
    return Bar.from_values(day, close, close + Decimal("0.2"), actual_low, close)


def golden_bars() -> tuple[dict, list[Bar]]:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    start = date.fromisoformat(fixture["start_date"])
    warmup = fixture["warmup"]
    first = Decimal(warmup["first_close"])
    increment = Decimal(warmup["increment"])
    bars = [
        bar(start + timedelta(days=index), first + increment * index)
        for index in range(warmup["count"])
    ]
    for offset, row in enumerate(fixture["tail"], start=warmup["count"]):
        bars.append(
            bar(
                start + timedelta(days=offset),
                Decimal(row["close"]),
                Decimal(row["low"]),
            )
        )
    return fixture, bars


def engine() -> DualMABacktestEngine:
    return DualMABacktestEngine(AddVariant.SIGNAL_ONLY, ExitVariant.DOW_TRAIL)


def manifest(
    bars: list[Bar],
    *,
    matrix_id: str = "M4",
    slippage_bps: int = 0,
    run_id: str = "ARTIFICIAL-GOLDEN",
) -> RunManifest:
    return RunManifest(
        run_id=run_id,
        strategy_version=STRATEGY_VERSION,
        spec_sha256=SPEC_SHA256,
        git_commit="artificial-test-commit",
        universe_id="ARTIFICIAL-UNIVERSE",
        data_snapshot_id="ARTIFICIAL-SNAPSHOT",
        data_start=bars[0].date,
        data_end=bars[-1].date,
        data_hash="artificial-data-hash",
        execution_model="EOD_CLOSE_PROXY_V1",
        cost_model="CATHAY_WEB_028_TW_V1",
        matrix_id=matrix_id,
        slippage_bps=slippage_bps,
    )


def test_artificial_golden_and_medium_long_independence() -> None:
    fixture, bars = golden_bars()
    result = engine().run("FAKE", bars, run_manifest=manifest(bars))
    actual = [
        {
            key: value.value if hasattr(value, "value") else value.isoformat() if isinstance(value, date) else value
            for key, value in transaction.items()
            if key in {"date", "strategy", "role", "status", "exit_reason"}
        }
        for transaction in result.transactions
    ]

    assert actual == fixture["expected_transactions"]
    medium, long = result.legs
    assert medium.exit_reason == ExitReason.REGIME_EXIT
    assert long.strategy == StrategyKind.LONG
    assert long.is_open
    assert long.strategy.value == fixture["expected_open_strategy"]


def test_truncated_replay_is_identical_through_every_cutoff() -> None:
    _, bars = golden_bars()
    full = engine().run("FAKE", bars, run_manifest=manifest(bars)).to_dict()

    for cutoff in (143, 145, 146, 148, len(bars)):
        prefix_bars = bars[:cutoff]
        prefix = engine().run(
            "FAKE",
            prefix_bars,
            run_manifest=manifest(prefix_bars, run_id=f"PREFIX-{cutoff}"),
        ).to_dict()
        assert prefix["decisions"] == full["decisions"][: cutoff * 2]
        last_date = bars[cutoff - 1].date.isoformat()
        expected_transactions = [
            transaction
            for transaction in full["transactions"]
            if transaction["date"] <= last_date
        ]
        assert prefix["transactions"] == expected_transactions


def test_signal_is_non_executable_when_even_one_share_exceeds_budget() -> None:
    start = date(2025, 1, 1)
    bars = [
        bar(start + timedelta(days=index), Decimal("10000") + Decimal(index * 10))
        for index in range(144)
    ]
    bars.extend(
        [
            bar(start + timedelta(days=144), Decimal("11200"), Decimal("11150")),
            bar(start + timedelta(days=145), Decimal("11500"), Decimal("11400")),
        ]
    )

    result = engine().run("EXPENSIVE_FAKE", bars, run_manifest=manifest(bars))
    decisions = [
        event
        for decision in result.decisions
        for event in decision["events"]
        if event.get("type") == "ENTRY_DECISION"
    ]

    assert len(decisions) == 2
    assert {event["status"] for event in decisions} == {"NON_EXECUTABLE"}
    assert not result.legs


def test_required_matrix_has_six_variants_at_each_slippage_level() -> None:
    _, bars = golden_bars()
    results = run_matrix(
        "FAKE",
        bars,
        manifest_factory=lambda matrix_id, bps: manifest(
            bars,
            matrix_id=matrix_id,
            slippage_bps=bps,
            run_id=f"MATRIX-{matrix_id}-{bps}",
        ),
    )

    assert len(results) == 18
    assert {result.slippage_bps for result in results.values()} == {0, 10, 20}
    assert {result.matrix_group for result in results.values()} == {
        "M1",
        "M2",
        "M3",
        "M4",
        "M5",
        "M6",
    }


def test_warmup_signals_cannot_create_positions_or_transactions() -> None:
    _, bars = golden_bars()
    first_trading_day = bars[146].date  # joint reclaim/entry occurred at 145
    result = engine().run(
        "FAKE",
        bars,
        run_manifest=manifest(bars, run_id="WARMUP-NO-TRADE"),
        backtest_start=first_trading_day,
        backtest_end=bars[-1].date,
    )

    assert result.transactions == []
    assert result.legs == []
    assert result.decisions
    assert min(decision["date"] for decision in result.decisions) == first_trading_day


def test_first_trading_day_indicators_include_warmup_history() -> None:
    _, bars = golden_bars()
    first_trading_day = bars[143].date
    expected_sma144 = sum((bar.close for bar in bars[:144]), Decimal("0")) / 144
    result = engine().run(
        "FAKE",
        bars,
        run_manifest=manifest(bars, run_id="WARMUP-INDICATORS"),
        backtest_start=first_trading_day,
        backtest_end=bars[-1].date,
        required_warmup_bars=143,
    )

    first_medium = result.decisions[0]
    first_long = result.decisions[1]
    assert first_medium["date"] == first_trading_day
    assert first_long["date"] == first_trading_day
    assert first_medium["sma144"] == expected_sma144
    assert result.input_audit["warmup_bar_count"] == 143
    assert result.input_audit["has_required_warmup"] is True


def test_backtest_end_is_inclusive_and_future_rows_do_not_enter_result() -> None:
    _, bars = golden_bars()
    start = bars[143].date
    end = bars[145].date  # includes joint entry, excludes later medium exit
    shared_manifest = manifest(bars, run_id="END-INCLUSIVE-FULL-INPUT")
    full_input = engine().run(
        "FAKE",
        bars,
        run_manifest=shared_manifest,
        backtest_start=start,
        backtest_end=end,
    )
    truncated_input = engine().run(
        "FAKE",
        bars[:146],
        run_manifest=RunManifest.from_mapping(
            {
                **shared_manifest.payload(),
                "run_id": "END-INCLUSIVE-TRUNCATED-INPUT",
            }
        ),
        backtest_start=start,
        backtest_end=end,
    )

    assert max(decision["date"] for decision in full_input.decisions) == end
    assert all(transaction["date"] <= end for transaction in full_input.transactions)
    assert {transaction["role"] for transaction in full_input.transactions} == {"MOTHER"}
    assert len(full_input.legs) == 2 and all(leg.is_open for leg in full_input.legs)
    assert full_input.input_audit["post_backtest_bar_count"] == len(bars) - 146
    assert full_input.input_fingerprint == truncated_input.input_fingerprint
    assert (
        full_input.semantic_result_fingerprint
        == truncated_input.semantic_result_fingerprint
    )
