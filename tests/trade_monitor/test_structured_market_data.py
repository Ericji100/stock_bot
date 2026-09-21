from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from trade_monitor.structured_market_data import load_structured_market_snapshot


TAIPEI = ZoneInfo("Asia/Taipei")


def _bars(count: int = 120) -> list[dict[str, object]]:
    start = datetime(2026, 9, 2, 8, 45, tzinfo=TAIPEI)
    result = []
    for index in range(count):
        open_ = 46000 + index
        result.append(
            {
                "time": (start + timedelta(minutes=index)).isoformat(),
                "open": open_,
                "high": open_ + 3,
                "low": open_ - 2,
                "close": open_ + 1,
                "volume": 100 + index,
            }
        )
    return result


def _write(path: Path, bars: list[dict[str, object]]) -> None:
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "source": "read-only-test-feed",
                "instrument": "TMF",
                "timezone": "Asia/Taipei",
                "bars": bars,
            }
        ),
        encoding="utf-8",
    )


def test_fresh_closed_bar_is_exact_numeric_authority(tmp_path: Path) -> None:
    path = tmp_path / "bars.json"
    bars = _bars()
    _write(path, bars)
    expected = str(bars[-1]["time"])

    result = load_structured_market_snapshot(
        path,
        expected_latest_closed_k_iso=expected,
        max_age_seconds=90,
    )

    assert result["status"] == "FRESH"
    assert result["latest_closed_k"]["close"] == 46120.0
    assert result["latest_closed_k"]["volume"] == 219.0
    assert result["indicators"]["sma21"] == 46110.0
    assert result["indicators"]["sma105"] == 46068.0
    assert result["indicators"]["atr14"] == 5.0
    assert result["opening_ranges"]["or5"] == {
        "start": "2026-09-02T08:45:00+08:00",
        "end": "2026-09-02T08:49:00+08:00",
        "high": 46007.0,
        "low": 45998.0,
    }


def test_stale_source_never_exposes_exact_values_as_fresh(tmp_path: Path) -> None:
    path = tmp_path / "bars.json"
    bars = _bars(20)
    _write(path, bars)
    expected = (datetime.fromisoformat(str(bars[-1]["time"])) + timedelta(minutes=2)).isoformat()

    result = load_structured_market_snapshot(
        path,
        expected_latest_closed_k_iso=expected,
        max_age_seconds=90,
    )

    assert result["status"] == "STALE"
    assert "latest_closed_k" not in result
    assert result["provenance"]["age_seconds"] == 120


def test_future_source_fails_safe(tmp_path: Path) -> None:
    path = tmp_path / "bars.json"
    bars = _bars(10)
    _write(path, bars)
    expected = str(bars[-2]["time"])

    result = load_structured_market_snapshot(path, expected_latest_closed_k_iso=expected)

    assert result["status"] == "FUTURE"
    assert "latest_closed_k" not in result


def test_invalid_ohlc_fails_safe_without_partial_numbers(tmp_path: Path) -> None:
    path = tmp_path / "bars.json"
    bars = _bars(10)
    bars[-1]["high"] = float(bars[-1]["low"]) - 1
    _write(path, bars)

    result = load_structured_market_snapshot(
        path,
        expected_latest_closed_k_iso=str(bars[-1]["time"]),
    )

    assert result["status"] == "INVALID"
    assert "latest_closed_k" not in result


def test_missing_optional_feed_is_explicitly_unavailable(tmp_path: Path) -> None:
    result = load_structured_market_snapshot(
        tmp_path / "missing.json",
        expected_latest_closed_k_iso="2026-09-02T10:00:00+08:00",
    )
    assert result["status"] == "UNAVAILABLE"
