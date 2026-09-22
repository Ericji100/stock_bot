from __future__ import annotations

from datetime import date
import json

import pandas as pd

from stock_ai_bot.data_sources import historical_price_service
from stock_ai_bot.selection import historical_selection_data_backfill_service as service
from stock_ai_bot.selection.historical_universe_service import (
    HistoricalUniverse,
    MarketMembershipInterval,
)


def _universe() -> HistoricalUniverse:
    return HistoricalUniverse(
        coverage_start=date(2021, 1, 1),
        coverage_end=date(2026, 12, 31),
        source_id="official-test-source",
        intervals=(
            MarketMembershipInterval(
                code="2330",
                symbol="2330.TW",
                market="TWSE",
                name="台積電",
                industry="半導體業",
                listed_on=date(1994, 9, 5),
                delisted_on=None,
                listed_on_basis="TEST",
                source_keys=("test",),
            ),
            MarketMembershipInterval(
                code="6488",
                symbol="6488.TWO",
                market="TPEX",
                name="環球晶",
                industry="半導體業",
                listed_on=date(2011, 10, 18),
                delisted_on=None,
                listed_on_basis="TEST",
                source_keys=("test",),
            ),
        ),
    )


def test_parse_twse_monthly_price_payload_uses_share_volume():
    frame = service.parse_twse_monthly_price_payload(
        {
            "stat": "OK",
            "data": [
                ["111/01/03", "1,363,372", "31,375,539", "23.35", "23.40", "22.75", "22.80"],
                ["111/01/04", "0", "0", "--", "--", "--", "--"],
            ],
        }
    )

    assert len(frame) == 1
    assert frame.iloc[0]["date"].date() == date(2022, 1, 3)
    assert frame.iloc[0]["volume"] == 1_363_372
    assert frame.iloc[0]["close"] == 22.8


def test_parse_tpex_monthly_price_payload_converts_thousand_shares():
    frame = service.parse_tpex_monthly_price_payload(
        {
            "tables": [
                {
                    "data": [
                        ["111/01/03", "11", "213", "19.40", "19.40", "19.35", "19.35", "0.55", "9"]
                    ]
                }
            ]
        }
    )

    assert len(frame) == 1
    assert frame.iloc[0]["date"].date() == date(2022, 1, 3)
    assert frame.iloc[0]["volume"] == 11_000


def test_parse_finmind_price_payload_maps_ohlcv_fields():
    frame = service.parse_finmind_price_payload(
        {
            "status": 200,
            "data": [
                {
                    "date": "2022-01-03",
                    "stock_id": "2358",
                    "Trading_Volume": 1_363_372,
                    "open": 23.35,
                    "max": 23.40,
                    "min": 22.75,
                    "close": 22.80,
                }
            ],
        }
    )

    assert len(frame) == 1
    assert frame.iloc[0]["date"].date() == date(2022, 1, 3)
    assert frame.iloc[0]["high"] == 23.40
    assert frame.iloc[0]["low"] == 22.75
    assert frame.iloc[0]["volume"] == 1_363_372


def test_parse_official_institutional_payloads_use_expected_net_columns():
    target = date(2022, 1, 3)
    twse_row = ["2330", "台積電", "0", "0", "12,000", "0", "0", "0", "0", "0", "-3,000"]
    twse = service.parse_twse_daily_institutional_payload(
        {"stat": "OK", "data": [twse_row]}, target, {"2330"}
    )
    tpex_row = ["6488", "環球晶"] + ["0"] * 22
    tpex_row[4] = "-557,849"
    tpex_row[13] = "81,000"
    tpex = service.parse_tpex_daily_institutional_payload(
        {"tables": [{"date": "111/01/03", "data": [tpex_row]}]}, target, {"6488"}
    )

    assert twse.iloc[0]["foreign_net_lots"] == 12.0
    assert twse.iloc[0]["trust_net_lots"] == -3.0
    assert tpex.iloc[0]["foreign_net_lots"] == -557.849
    assert tpex.iloc[0]["trust_net_lots"] == 81.0


def test_tpex_institutional_rejects_silent_date_fallback():
    try:
        service.parse_tpex_daily_institutional_payload(
            {"tables": [{"date": "115/09/21", "data": []}]},
            date(2022, 1, 3),
            {"6488"},
        )
    except ValueError as exc:
        assert "date mismatch" in str(exc)
    else:  # pragma: no cover - assertion branch
        raise AssertionError("mismatched TPEx response date must not be accepted")


def test_price_targets_are_isolated_and_skip_existing_files(monkeypatch, tmp_path):
    monkeypatch.setattr(historical_price_service, "HISTORICAL_PRICE_CACHE_DIR", tmp_path)
    historical_price_service._read_cached_history.cache_clear()
    historical_price_service.save_history(
        "2330.TW",
        pd.DataFrame(
            [
                {
                    "date": "2022-01-03",
                    "open": 1,
                    "high": 1,
                    "low": 1,
                    "close": 1,
                    "volume": 1,
                }
            ]
        ),
    )

    targets = service._price_targets(
        _universe(), date(2021, 5, 1), date(2025, 12, 31), None, True
    )

    assert [target[0].symbol for target in targets] == ["6488.TWO"]


def test_price_window_targets_detect_partial_calendar_gap(monkeypatch, tmp_path):
    monkeypatch.setattr(historical_price_service, "HISTORICAL_PRICE_CACHE_DIR", tmp_path)
    historical_price_service._read_cached_history.cache_clear()

    def history(dates):
        return pd.DataFrame(
            [
                {
                    "date": value,
                    "open": 1,
                    "high": 1,
                    "low": 1,
                    "close": 1,
                    "volume": 1,
                }
                for value in dates
            ]
        )

    historical_price_service.save_history("^TWII", history(["2024-01-02", "2024-01-03"]))
    historical_price_service.save_history("2330.TW", history(["2024-01-02"]))
    historical_price_service.save_history("6488.TWO", history(["2024-01-02", "2024-01-03"]))

    targets = service._price_window_targets(
        _universe(),
        date(2024, 1, 1),
        date(2024, 12, 31),
        minimum_coverage_ratio=0.75,
        force_all=False,
    )

    assert [(item[0].symbol, item[3], item[4]) for item in targets] == [
        ("2330.TW", 1, 2)
    ]


def test_backfill_manifest_is_content_addressed(tmp_path):
    run = service.BackfillRun(
        kind="official-prices",
        requested_start=date(2021, 5, 1),
        requested_end=date(2025, 12, 31),
        universe_source_id="official-test-source",
        results=({"symbol": "2330.TW", "status": "SUCCESS"},),
    )

    first = service.freeze_backfill_run(run, root=tmp_path)
    second = service.freeze_backfill_run(run, root=tmp_path)
    payload = json.loads(first.read_text(encoding="utf-8"))

    assert first == second
    assert run.content_sha256[:16] in first.name
    assert payload["content_sha256"] == run.content_sha256
