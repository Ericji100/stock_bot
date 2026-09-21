from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from trade_monitor.pivot_replay import Candle, compare_parameters, detect_local_pivots, load_candles_from_html


TAIPEI = ZoneInfo("Asia/Taipei")


def _candles(highs: list[float], lows: list[float], closes: list[float] | None = None) -> list[Candle]:
    start = datetime(2026, 9, 2, 8, 45, tzinfo=TAIPEI)
    close_values = closes or [(high + low) / 2 for high, low in zip(highs, lows)]
    return [
        Candle(start + timedelta(minutes=index), close, high, low, close)
        for index, (high, low, close) in enumerate(zip(highs, lows, close_values))
    ]


def test_n2_pivot_requires_two_strict_right_bars_and_close_breach() -> None:
    rows = _candles(
        [10, 11, 15, 13, 12, 11],
        [8, 9, 10, 9, 8, 7],
        [9, 10, 12, 11, 9, 8],
    )
    pivots, stats = detect_local_pivots(rows, 2)
    high = next(item for item in pivots if item.kind == "HIGH")

    assert high.bar_index == 2
    assert high.confirmation_index == 4
    assert high.delay_bars == 2
    assert stats["candidates"] >= 1


def test_equal_high_plateau_is_not_a_strict_pivot() -> None:
    rows = _candles([10, 11, 15, 15, 12, 11], [8, 9, 10, 10, 8, 7], [9, 10, 12, 11, 9, 8])
    pivots, _ = detect_local_pivots(rows, 2)
    assert not any(item.kind == "HIGH" and item.bar_index in {2, 3} for item in pivots)


def test_embedded_payload_uses_exported_utc_clock_as_taiwan_session_clock(tmp_path: Path) -> None:
    timestamp = int(datetime(2026, 8, 3, 8, 45, tzinfo=ZoneInfo("UTC")).timestamp())
    payload = {"meta": {"barCount": 1}, "candles": [{"time": timestamp, "open": 1, "high": 2, "low": 0.5, "close": 1.5}]}
    html = tmp_path / "chart.html"
    html.write_text(f"<script>const payload = {json.dumps(payload)};</script>", encoding="utf-8")

    _, candles = load_candles_from_html(html)
    assert candles[0].at.isoformat() == "2026-08-03T08:45:00+08:00"


def test_comparison_reports_day_first_hour_and_general_segments() -> None:
    rows = _candles(
        [10 + (index % 5) for index in range(80)],
        [8 + (index % 5) for index in range(80)],
    )
    report = compare_parameters(rows)
    assert "DAY_FIRST_HOUR" in report["segments"]
    assert "DAY_GENERAL" in report["segments"]
    assert set(report["aggregate"]) == {"2", "3"}
