from datetime import date, timedelta
from decimal import Decimal

import pytest

from backtests.dual_ma import (
    AddVariant,
    Bar,
    ContractValidationError,
    CostModel,
    DualMABacktestEngine,
    ExitVariant,
    InstrumentType,
    RunManifest,
    SPEC_SHA256,
    STRATEGY_VERSION,
    run_matrix,
    validate_daily_inputs,
)


START = date(2025, 1, 1)


def bars(count: int = 150) -> list[Bar]:
    return [
        Bar.from_values(
            START + timedelta(days=index),
            Decimal("100") + Decimal(index) / 10,
            Decimal("101") + Decimal(index) / 10,
            Decimal("99") + Decimal(index) / 10,
            Decimal("100") + Decimal(index) / 10,
        )
        for index in range(count)
    ]


def manifest_values(series: list[Bar], **overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "run_id": "CONTRACT-TEST-RUN",
        "strategy_version": STRATEGY_VERSION,
        "spec_sha256": SPEC_SHA256,
        "git_commit": "0123456789abcdef",
        "universe_id": "ARTIFICIAL-UNIVERSE",
        "data_snapshot_id": "ARTIFICIAL-SNAPSHOT",
        "data_start": series[0].date,
        "data_end": series[-1].date,
        "data_hash": "artificial-snapshot-hash",
        "execution_model": "EOD_CLOSE_PROXY_V1",
        "cost_model": "CATHAY_WEB_028_TW_V1",
        "matrix_id": "M4",
        "slippage_bps": 0,
        "strategy_parameters": {"entry": "ENTRY_RECLAIM_ONLY"},
    }
    values.update(overrides)
    return values


def make_manifest(series: list[Bar], **overrides: object) -> RunManifest:
    return RunManifest.from_mapping(manifest_values(series, **overrides))


def test_manifest_rejects_missing_and_empty_required_fields() -> None:
    series = bars(3)
    missing = manifest_values(series)
    missing.pop("git_commit")
    with pytest.raises(ContractValidationError, match="git_commit"):
        RunManifest.from_mapping(missing)

    with pytest.raises(ContractValidationError, match="universe_id"):
        make_manifest(series, universe_id=" ")


def test_manifest_is_pinned_to_final_v041_spec_hash() -> None:
    series = bars(3)
    manifest = make_manifest(series)

    assert manifest.strategy_version == "v0.4.1-frozen"
    assert manifest.spec_sha256 == (
        "17f3e15b360eba03d9cbc47a5c1013f6ab4e86352cac8beaa87ad1d94fdc5c1a"
    )
    with pytest.raises(ContractValidationError, match="spec_sha256"):
        make_manifest(series, spec_sha256="0" * 64)


def test_daily_input_rejects_duplicate_dates_and_nonpositive_prices() -> None:
    series = bars(3)
    manifest = make_manifest(series)
    duplicate = [series[0], Bar.from_values(series[0].date, 101, 102, 100, 101)]
    with pytest.raises(ContractValidationError, match="strictly increasing"):
        validate_daily_inputs("FAKE", InstrumentType.STOCK, duplicate, manifest)

    invalid = [
        Bar(
            date=START,
            open=Decimal("-1"),
            high=Decimal("-1"),
            low=Decimal("-1"),
            close=Decimal("-1"),
        )
    ]
    invalid_manifest = make_manifest(invalid)
    with pytest.raises(ContractValidationError, match="positive"):
        validate_daily_inputs("FAKE", InstrumentType.STOCK, invalid, invalid_manifest)


def test_symbol_instrument_and_warmup_coverage_are_auditable() -> None:
    series = bars()
    manifest = make_manifest(series)
    audit = validate_daily_inputs(
        "FAKE",
        InstrumentType.ETF,
        series,
        manifest,
        backtest_start=series[143].date,
        backtest_end=series[-1].date,
        required_warmup_bars=143,
    )

    assert audit.symbol == "FAKE"
    assert audit.instrument_type == InstrumentType.ETF
    assert audit.warmup_bar_count == 143
    assert audit.has_required_warmup
    assert audit.backtest_bar_count == 7
    assert audit.covers_backtest_start and audit.covers_backtest_end

    with pytest.raises(ContractValidationError, match="symbol"):
        validate_daily_inputs("", InstrumentType.STOCK, series, manifest)
    with pytest.raises(ContractValidationError, match="instrument_type"):
        validate_daily_inputs("FAKE", "UNKNOWN", series, manifest)


def test_manifest_fingerprint_is_stable_and_tracks_metadata_and_parameters() -> None:
    series = bars(3)
    first = make_manifest(series)
    repeated = make_manifest(series)
    changed_metadata = make_manifest(series, universe_id="OTHER-UNIVERSE")
    changed_parameters = make_manifest(
        series,
        strategy_parameters={"entry": "ENTRY_RECLAIM_ONLY", "test_knob": 1},
    )

    assert first.fingerprint == repeated.fingerprint
    assert changed_metadata.fingerprint != first.fingerprint
    assert changed_parameters.fingerprint != first.fingerprint


def test_manifest_parameters_and_metadata_are_immutable_snapshots() -> None:
    series = bars(3)
    parameters = {"nested": {"threshold": 1}}
    metadata = {"worker": {"host": "A"}}
    manifest = make_manifest(
        series,
        strategy_parameters=parameters,
        metadata=metadata,
    )
    original_fingerprint = manifest.fingerprint

    parameters["nested"]["threshold"] = 999
    metadata["worker"]["host"] = "B"
    assert manifest.fingerprint == original_fingerprint
    assert manifest.strategy_parameters["nested"]["threshold"] == 1
    assert manifest.metadata["worker"]["host"] == "A"
    with pytest.raises(TypeError):
        manifest.strategy_parameters["new"] = "value"
    with pytest.raises(TypeError):
        manifest.strategy_parameters["nested"]["threshold"] = 2


def test_result_and_input_fingerprints_are_deterministic_and_parameter_sensitive() -> None:
    series = bars(30)
    first_manifest = make_manifest(
        series,
        run_id="SEMANTIC-RUN-A",
        git_commit="commit-a",
        metadata={"worker": "A"},
    )
    repeated_manifest = make_manifest(
        series,
        run_id="SEMANTIC-RUN-B",
        git_commit="commit-b",
        metadata={"worker": "B"},
    )
    changed_manifest = make_manifest(
        series,
        strategy_parameters={"entry": "ENTRY_RECLAIM_ONLY", "test_knob": 1},
    )
    engine = DualMABacktestEngine(AddVariant.SIGNAL_ONLY, ExitVariant.DOW_TRAIL)

    first = engine.run(
        "FAKE",
        series,
        run_manifest=first_manifest,
        required_warmup_bars=0,
    )
    repeated = engine.run(
        "FAKE",
        series,
        run_manifest=repeated_manifest,
        required_warmup_bars=0,
    )
    changed = engine.run(
        "FAKE",
        series,
        run_manifest=changed_manifest,
        required_warmup_bars=0,
    )
    slipped = DualMABacktestEngine(
        AddVariant.SIGNAL_ONLY,
        ExitVariant.DOW_TRAIL,
        slippage_bps=10,
    ).run(
        "FAKE",
        series,
        run_manifest=make_manifest(
            series,
            run_id="SEMANTIC-SLIPPAGE-10",
            slippage_bps=10,
        ),
        required_warmup_bars=0,
    )

    assert first.manifest_fingerprint != repeated.manifest_fingerprint
    assert first.input_fingerprint == repeated.input_fingerprint
    assert first.semantic_result_fingerprint == repeated.semantic_result_fingerprint
    assert changed.manifest_fingerprint != first.manifest_fingerprint
    assert changed.semantic_result_fingerprint != first.semantic_result_fingerprint
    assert slipped.input_fingerprint == first.input_fingerprint
    assert slipped.semantic_result_fingerprint != first.semantic_result_fingerprint


def test_manifest_must_match_engine_matrix_and_slippage() -> None:
    series = bars(30)
    engine = DualMABacktestEngine(AddVariant.SIGNAL_ONLY, ExitVariant.DOW_TRAIL)

    with pytest.raises(ContractValidationError, match="matrix_id"):
        engine.run(
            "FAKE",
            series,
            run_manifest=make_manifest(series, matrix_id="M1"),
        )
    with pytest.raises(ContractValidationError, match="slippage_bps"):
        engine.run(
            "FAKE",
            series,
            run_manifest=make_manifest(series, slippage_bps=10),
        )


def test_same_cost_model_id_with_changed_terms_is_rejected() -> None:
    series = bars(30)
    disguised = CostModel(commission_rate=Decimal("0.0004"))
    engine = DualMABacktestEngine(
        AddVariant.SIGNAL_ONLY,
        ExitVariant.DOW_TRAIL,
        cost_model=disguised,
    )

    with pytest.raises(ContractValidationError, match="cost model terms"):
        engine.run("FAKE", series, run_manifest=make_manifest(series))


def test_matrix_batch_rejects_duplicate_run_ids() -> None:
    series = bars(30)

    with pytest.raises(ContractValidationError, match="run_id must be unique"):
        run_matrix(
            "FAKE",
            series,
            manifest_factory=lambda matrix_id, bps: make_manifest(
                series,
                run_id="DUPLICATE-RUN-ID",
                matrix_id=matrix_id,
                slippage_bps=bps,
            ),
            required_warmup_bars=0,
        )
