from __future__ import annotations

from datetime import date, datetime, timezone
import json

import pytest

import stock_ai_bot.selection.historical_universe_service as service


def _sources(
    *,
    twse_current=(),
    tpex_current=(),
    twse_delisted=(),
    tpex_delisted=(),
    twse_new=(),
    tpex_new=(),
):
    return service.OfficialUniverseSources(
        collected_at=datetime(2026, 9, 21, tzinfo=timezone.utc).isoformat(),
        coverage_start=date(2021, 1, 1),
        coverage_end=date(2025, 12, 31),
        twse_current=tuple(twse_current),
        tpex_current=tuple(tpex_current),
        twse_delisted=tuple(twse_delisted),
        tpex_delisted=tuple(tpex_delisted),
        twse_new_listings=tuple(twse_new),
        tpex_new_listings=tuple(tpex_new),
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("111年09月26日", date(2022, 9, 26)),
        ("111/09/26", date(2022, 9, 26)),
        ("111-09-26", date(2022, 9, 26)),
        ("2022/09/26", date(2022, 9, 26)),
        ("20220926", date(2022, 9, 26)),
        ("1110926", date(2022, 9, 26)),
    ],
)
def test_parse_exchange_date_supports_roc_and_gregorian(raw, expected):
    assert service.parse_exchange_date(raw) == expected


def test_trade_date_parser_accepts_tpex_spaced_field_name():
    assert service._trade_dates([{"日 期": "109/12/01"}]) == [date(2020, 12, 1)]


def test_as_of_universe_excludes_before_listing_and_on_delisting_date():
    sources = _sources(
        twse_current=[
            {"公司代號": "2330", "公司簡稱": "台積電", "產業別": "24", "上市日期": "19940905"},
            {"公司代號": "6863", "公司簡稱": "永道", "產業別": "28", "上市日期": "20230321"},
        ],
        twse_delisted=[{"上市編號": "2443", "公司名稱": "昶虹", "終止上市日期": "113/11/19"}],
        twse_new=[{"公司代號": "2443", "股票上市買賣日期": "90.01.03"}],
    )
    universe = service.build_historical_universe(sources)

    before_new_listing = {item.code for item in universe.members_as_of(date(2023, 3, 20))}
    after_new_listing = {item.code for item in universe.members_as_of(date(2023, 3, 21))}
    before_delisting = {item.code for item in universe.members_as_of(date(2024, 11, 18))}
    on_delisting = {item.code for item in universe.members_as_of(date(2024, 11, 19))}

    assert "6863" not in before_new_listing
    assert "6863" in after_new_listing
    assert next(item for item in universe.members_as_of(date(2023, 3, 21)) if item.code == "2330").industry != "24"
    assert "2443" in before_delisting
    assert "2443" not in on_delisting


def test_missing_delisted_listing_evidence_blocks_strict_replay():
    sources = _sources(
        twse_delisted=[{"上市編號": "2358", "公司名稱": "廷鑫", "終止上市日期": "113/11/19"}],
    )
    universe = service.build_historical_universe(sources)

    assert universe.formal_backtest_ready is False
    with pytest.raises(service.IncompleteHistoricalUniverseError):
        universe.members_as_of(date(2022, 1, 3))
    assert universe.members_as_of(date(2024, 11, 19)) == []


def test_precoverage_price_observation_resolves_old_delisted_member():
    sources = _sources(
        tpex_delisted=[{"股票代號": "5820", "公司名稱": "日盛金", "終止上櫃日期": "111-11-11"}],
    )
    universe = service.build_historical_universe(
        sources,
        first_trade_dates={"5820.TWO": date(2020, 1, 2)},
    )

    assert universe.formal_backtest_ready is True
    member = universe.members_as_of(date(2022, 1, 3))[0]
    assert member.symbol == "5820.TWO"
    interval = universe.intervals[0]
    assert interval.listed_on == date(2021, 1, 1)
    assert interval.listed_on_basis == "PRICE_OBSERVED_ON_OR_BEFORE_COVERAGE_START"
    assert "membership-evidence#" in universe.source_id


def test_transfer_intervals_do_not_overlap_and_use_historical_market_symbol():
    sources = _sources(
        twse_current=[
            {"公司代號": "3652", "公司簡稱": "精聯", "產業別": "25", "上市日期": "20220921"},
        ],
        tpex_delisted=[
            {"股票代號": "3652", "公司名稱": "精聯電子", "終止上櫃日期": "111-09-21"},
        ],
        tpex_new=[
            {"股票代號": "3652", "公司名稱": "精聯電子", "上櫃日期": "100/03/21"},
        ],
    )
    universe = service.build_historical_universe(sources)

    before = universe.members_as_of(date(2022, 9, 20))[0]
    after = universe.members_as_of(date(2022, 9, 21))[0]
    assert (before.market, before.symbol) == ("TPEX", "3652.TWO")
    assert (after.market, after.symbol) == ("TWSE", "3652.TW")


def test_source_and_daily_snapshot_freezes_are_content_addressed(tmp_path):
    sources = _sources(
        twse_current=[
            {"公司代號": "2330", "公司簡稱": "台積電", "產業別": "24", "上市日期": "19940905"},
        ],
    )
    source_path_1 = service.freeze_official_universe_sources(sources, tmp_path / "sources")
    source_path_2 = service.freeze_official_universe_sources(sources, tmp_path / "sources")
    assert source_path_1 == source_path_2
    source_payload = json.loads(source_path_1.read_text(encoding="utf-8"))
    assert source_payload["content_sha256"] == sources.content_sha256
    assert service.load_official_universe_sources(source_path_1) == sources

    universe = service.build_historical_universe(sources)
    snapshot_path_1 = service.freeze_daily_universe_snapshot(
        universe, date(2022, 1, 3), tmp_path / "snapshots"
    )
    snapshot_path_2 = service.freeze_daily_universe_snapshot(
        universe, date(2022, 1, 3), tmp_path / "snapshots"
    )
    assert snapshot_path_1 == snapshot_path_2
    snapshot = json.loads(snapshot_path_1.read_text(encoding="utf-8"))
    assert snapshot["member_count"] == 1
    assert snapshot["members"][0]["code"] == "2330"


def test_official_membership_evidence_freeze_and_date_mapping(tmp_path):
    evidence = service.OfficialMembershipEvidence(
        collected_at=datetime(2026, 9, 21, tzinfo=timezone.utc).isoformat(),
        evidence_month=date(2020, 12, 1),
        observations=(
            {
                "symbol": "2358.TW",
                "code": "2358",
                "market": "TWSE",
                "requested_month": "2020-12-01",
                "status": "FOUND",
                "first_trade_date": "2020-12-01",
                "row_count": 23,
            },
        ),
    )

    path = service.freeze_official_membership_evidence(evidence, tmp_path)
    assert evidence.first_trade_dates == {"2358.TW": date(2020, 12, 1)}
    assert json.loads(path.read_text(encoding="utf-8"))["status_counts"] == {"FOUND": 1}
    assert service.load_official_membership_evidence(path) == evidence


def test_official_listing_supplement_resolves_suspended_delisted_member(tmp_path):
    sources = _sources(
        tpex_delisted=[
            {"股票代號": "911613", "公司名稱": "特藝石油", "終止上櫃日期": "110-09-03"},
        ],
    )
    supplement = service.OfficialListingSupplement(
        collected_at=datetime(2026, 9, 21, tzinfo=timezone.utc).isoformat(),
        observations=(
            {
                "symbol": "911613.TWO",
                "listed_on": "2011-02-25",
                "source_url": "https://www.tpex.org.tw/storage/publish/annual/100/100.pdf",
                "source_title": "100年度有價證券上櫃異動",
            },
        ),
    )

    path = service.freeze_official_listing_supplement(supplement, tmp_path)
    assert service.load_official_listing_supplement(path) == supplement
    universe = service.build_historical_universe(
        sources,
        supplemental_listing_dates=supplement,
    )
    assert universe.formal_backtest_ready is True
    assert universe.members_as_of(date(2021, 1, 4))[0].symbol == "911613.TWO"
    assert universe.intervals[0].listed_on_basis == "OFFICIAL_SUPPLEMENTAL_LISTING_RECORD"
    assert "listing-supplement#" in universe.source_id
