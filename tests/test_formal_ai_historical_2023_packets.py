import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from scripts import formal_ai_historical_2023_packets as packets


def test_historical_event_source_contains_exact_889_stock_catalog() -> None:
    source = packets.read_json(packets.SOURCE_EVENTS)
    assert len({str(row["code"]) for row in source["events"]}) == 889


def test_strategy_normalization_preserves_each_technical_signal() -> None:
    assert packets.normalize_strategy(
        {"strategy": "technical", "signal": "突破 21MA"}
    ) == "TECH_MA21_BREAKOUT"
    assert packets.normalize_strategy(
        {"strategy": "technical", "signal": "跌破後收復 105MA"}
    ) == "TECH_MA105_RECLAIM"
    assert packets.normalize_strategy(
        {"strategy": "technical", "signal": "自訂技術訊號"}
    ) == "TECHNICAL::自訂技術訊號"
    assert packets.normalize_strategy({"strategy": "financial", "signal": "G1/毛利率A"}) == "FINANCIAL_G1_G2"
    assert packets.normalize_strategy({"strategy": "financial", "signal": "G2/毛利率D"}) == "FINANCIAL_G1_G2"
    assert packets.normalize_strategy({"strategy": "chip_1", "signal": "S級"}) == "CHIP"
    assert packets.normalize_strategy({"strategy": "chip_3", "signal": "B級"}) == "CHIP"
    assert packets.normalize_strategy({"strategy": "curated", "signal": "組合"}) == "CURATED"
    assert packets.normalize_strategy({"strategy": "laoxiao", "signal": "強勢"}) == "LAOXIAO"


def test_catalog_monitor_date_and_nontechnical_collapse() -> None:
    raw_events = [
        {"code": "1101", "name": "甲", "date": "2023-01-03", "strategy": "chip_1", "signal": "A級"},
        {"code": "1101", "name": "甲", "date": "2023-01-03", "strategy": "chip_3", "signal": "B級"},
        {"code": "1101", "name": "甲", "date": "2023-01-03", "strategy": "technical", "signal": "突破 5MA"},
        {"code": "1102", "name": "乙", "date": "2023-05-03", "strategy": "financial", "signal": "G2/毛利率B"},
    ]
    universe = {
        "1101": {"code": "1101", "name": "甲", "symbol": "1101.TW", "market": "TWSE"},
        "1102": {"code": "1102", "name": "乙", "symbol": "1102.TW", "market": "TWSE"},
    }

    catalog = packets.build_catalog_payload(raw_events, universe, expected_stocks=None)

    assert catalog["monitor_stock_count"] == 2
    assert catalog["raw_event_count"] == 4
    assert catalog["event_count"] == 3
    by_code = {row["code"]: row for row in catalog["monitor_stocks"]}
    assert by_code["1101"]["monitor_on"] == "2023-05-02"
    assert by_code["1102"]["monitor_on"] == "2023-05-03"
    assert by_code["1101"]["watchlist_entry_type"] == "LEFT_CENSORED_CARRY_IN"
    assert by_code["1102"]["watchlist_entry_type"] == "NEW_UPSTREAM_SELECTION"
    assert by_code["1101"]["strategies"] == ["CHIP", "TECH_MA5_BREAKOUT"]


def test_yahoo_payload_keeps_raw_and_adjusted_ohlc_and_dividends() -> None:
    timestamp = int(datetime(2023, 5, 2, tzinfo=timezone.utc).timestamp())
    payload = {
        "chart": {
            "result": [
                {
                    "timestamp": [timestamp],
                    "indicators": {
                        "quote": [
                            {
                                "open": [100.0],
                                "high": [110.0],
                                "low": [90.0],
                                "close": [100.0],
                                "volume": [1_000_000],
                            }
                        ],
                        "adjclose": [{"adjclose": [80.0]}],
                    },
                    "events": {
                        "dividends": {
                            str(timestamp): {"date": timestamp, "amount": 2.5}
                        },
                        "splits": {
                            str(timestamp): {
                                "date": timestamp,
                                "splitRatio": "2:1",
                                "numerator": 2.0,
                                "denominator": 1.0,
                            }
                        },
                    },
                }
            ]
        }
    }

    frame, dividends = packets.payload_to_frame(payload)

    assert frame.loc[0, "raw_high"] == 110.0
    assert frame.loc[0, "high"] == pytest.approx(88.0)
    assert frame.loc[0, "close"] == pytest.approx(80.0)
    assert dividends == [{"date": "2023-05-02", "amount": 2.5}]
    events = packets.payload_events(payload)
    assert events["splits"][0]["splitRatio"] == "2:1"
    assert events["splits"][0]["numerator"] == 2.0


def test_factor_jump_audit_exposes_share_adjustment_and_unresolved_jump() -> None:
    frame = pd.DataFrame(
        {
            "date": ["2023-05-02", "2023-05-03", "2023-05-04"],
            "raw_close": [100.0, 50.0, 50.0],
            "adj_close": [50.0, 50.0, 45.0],
        }
    )
    actions = {
        "splits": [
            {
                "date": "2023-05-03",
                "splitRatio": "2:1",
                "numerator": 2.0,
                "denominator": 1.0,
            }
        ]
    }

    audit = packets.corporate_action_audit(frame, actions, "2023-05-02")

    assert audit["requires_share_count_replay"] is True
    assert audit["share_count_adjustments"][0]["ratio"] == 2.0
    # The May-04 factor jump is within four calendar days of the split and is
    # therefore explicitly reconciled rather than silently accepted.
    assert audit["corporate_action_unresolved"] is False


def _write_market_fixture(tmp_path: Path) -> tuple[dict, dict, dict]:
    dates = pd.bdate_range("2020-01-02", "2023-09-04")
    close = pd.Series(range(len(dates)), dtype=float) / 20.0 + 50.0
    frame = pd.DataFrame(
        {
            "date": dates,
            "raw_open": close + 0.1,
            "raw_high": close + 1.0,
            "raw_low": close - 1.0,
            "raw_close": close,
            "adj_close": close * 0.9,
            "volume": 1_000_000,
            "open": (close + 0.1) * 0.9,
            "high": (close + 1.0) * 0.9,
            "low": (close - 1.0) * 0.9,
            "close": close * 0.9,
        }
    )
    price_path = tmp_path / "1101.csv"
    event_path = tmp_path / "1101.json"
    frame.to_csv(price_path, index=False)
    event_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "dividends": [{"date": "2023-06-01", "amount": 1.0}],
                "all_events": {
                    "dividends": [{"date": "2023-06-01", "amount": 1.0}]
                },
            }
        ),
        encoding="utf-8",
    )
    item = {
        "code": "1101",
        "name": "甲",
        "symbol": "1101.TW",
        "market": "TWSE",
        "monitor_on": "2023-05-02",
        "price_path": str(price_path),
        "event_path": str(event_path),
        "price_sha256": packets.digest(price_path),
        "event_sha256": packets.digest(event_path),
        "source_origin": "TEST",
        "corporate_action_audit": {
            "factor_jumps": [],
            "requires_share_count_replay": False,
            "corporate_action_unresolved": False,
        },
    }
    meta = {
        "code": "1101",
        "first_selected_date": "2023-01-03",
        "monitor_on": "2023-05-02",
        "watchlist_entry_type": "LEFT_CENSORED_CARRY_IN",
        "last_selected_date": "2023-05-03",
        "selection_event_count": 2,
        "raw_selection_event_count": 2,
        "selection_date_count": 2,
        "strategies": ["TECH_MA21_BREAKOUT"],
    }
    selections = {
        "2023-01-03": ["TECH_MA21_BREAKOUT"],
        "2023-05-03": ["TECH_MA21_BREAKOUT"],
    }
    return item, meta, selections


def test_packet_and_asof_are_causal_facts_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item, meta, selections = _write_market_fixture(tmp_path)
    packet = packets.build_packet(item, selections, meta)

    assert packet["monitor_on"] == "2023-05-02"
    assert packet["data_quality"]["preferred_750_met"] is True
    assert packet["data_quality"]["raw_and_adjusted_ohlc_present"] is True
    assert packet["boundary"]["contains_classification_score_or_approval"] is False
    assert max(row["date"] for row in packet["daily_visible_facts_from_monitoring"]) == "2023-09-04"
    assert "raw_close" in packet["daily_visible_facts_from_monitoring"][0]

    catalog_path = tmp_path / "catalog.json"
    manifest_path = tmp_path / "manifest.json"
    packets.write_json(
        catalog_path,
        {
            "monitor_stocks": [meta],
            "events": [
                {"code": "1101", "date": day, "strategy": source}
                for day, sources in selections.items()
                for source in sources
            ],
        },
    )
    packets.write_json(manifest_path, {"items": [item]})
    monkeypatch.setattr(packets, "CATALOG", catalog_path)
    monkeypatch.setattr(packets, "SOURCE_MANIFEST", manifest_path)

    view = packets.asof_view("1101", "2023-06-02")

    assert view["boundary"]["facts_only"] is True
    assert view["boundary"]["future_bars_loaded_into_view"] is False
    assert max(row["date"] for row in view["recent_daily_facts"]) == "2023-06-02"
    assert view["dividends_to_date"] == [{"date": "2023-06-01", "amount": 1.0}]
