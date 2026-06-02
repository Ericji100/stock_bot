from datetime import date

import pandas as pd


def test_technical_history_duplicate_numeric_columns_do_not_crash():
    from technical_scanner import _standardize_history

    frame = pd.DataFrame(
        [
            [date(2026, 5, 1), 10, 99, 11, 9, 10.5, 88, 1000, 9999],
            [date(2026, 5, 2), 11, 98, 12, 10, 11.5, 87, 2000, 9998],
        ],
        columns=["Date", "Open", "Open", "High", "Low", "Close", "Close", "Volume", "Volume"],
    )

    result = _standardize_history(frame)

    assert len(result) == 2
    assert list(result.columns) == ["date", "open", "high", "low", "close", "volume"]
    assert result.iloc[0]["close"] == 10.5


def test_stock_price_metric_duplicate_columns_do_not_crash():
    from stock_scanner import _extract_price_metric

    rows = []
    for index in range(25):
        rows.append([100 + index, 900 + index, 1000 + index, 9999 + index])
    frame = pd.DataFrame(rows, columns=["Close", "Close", "Volume", "Volume"])

    metric = _extract_price_metric(frame)

    assert metric is not None
    assert metric["price"] == 124


def test_research_technical_snapshot_duplicate_columns_do_not_crash():
    from research_center.data_services import _technical_snapshot

    rows = []
    for index in range(25):
        rows.append([date(2026, 5, index % 28 + 1), 100 + index, 900 + index, 1000 + index, 9999 + index])
    frame = pd.DataFrame(rows, columns=["Date", "Close", "Close", "Volume_Lots", "Volume_Lots"])

    snapshot = _technical_snapshot(frame)

    assert snapshot["latest_close"] == 124
    assert "avg_volume_20d" in snapshot
