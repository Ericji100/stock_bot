from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

import scripts.course_watchlist_backtest as backtest


def test_parse_radar_summary_reads_strategy_multilabel(tmp_path: Path) -> None:
    path = tmp_path / "radar_summary.md"
    path.write_text(
        "1. 1234 測試股｜88分｜技術策略：A（多頭延續）、D（急跌收復）｜AI短評信心：高\n"
        "2. 5678 第二股｜70分｜技術策略：B（紅柱突破）\n",
        encoding="utf-8",
    )

    rows = backtest.parse_radar_summary(path, date(2026, 8, 7))

    assert [(row["code"], row["score"]) for row in rows] == [("1234", 88), ("5678", 70)]
    assert rows[0]["strategy_codes"] == ["A", "D"]
    assert rows[1]["strategy_codes"] == ["B"]


def test_replay_requires_close_confirmation_then_following_open(monkeypatch, tmp_path: Path) -> None:
    frame = pd.DataFrame(
        [
            {"date": "2026-08-07", "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2, "ATR14": 1.0},
            {"date": "2026-08-10", "open": 10.3, "high": 10.8, "low": 10.1, "close": 10.6, "ATR14": 1.0},
            {"date": "2026-08-11", "open": 10.7, "high": 11.1, "low": 10.4, "close": 11.0, "ATR14": 1.0},
            {"date": "2026-08-12", "open": 11.0, "high": 11.3, "low": 10.8, "close": 11.2, "ATR14": 1.0},
        ]
    )
    frame["date"] = pd.to_datetime(frame["date"])
    source = tmp_path / "1234_TW.csv"
    monkeypatch.setattr(backtest, "load_full_frame", lambda symbol, as_of: (frame, source))

    def snapshot(truncated: pd.DataFrame, thresholds=backtest.THRESHOLDS):
        day = truncated.iloc[-1]
        return {
            "date": day["date"].date().isoformat(),
            "structural_invalid": False,
            "armed": True,
            "setup": {"trigger_price": 10.5, "pattern_code": "RECLAIM"},
            "risk": {"chase_cap": 11.0, "defense": 9.0},
            "structural_lens": "QUADRANT_PRIMARY",
            "taiji": {"sequence": "NONE", "state": "UNDEFINED"},
        }

    monkeypatch.setattr(backtest, "enlightenment_snapshot", snapshot)
    result = backtest.replay_stock(
        stock={"code": "1234", "name": "測試股", "symbol": "1234.TW", "market": "TWSE"},
        selections=[
            {
                "date": date(2026, 8, 7),
                "code": "1234",
                "name": "測試股",
                "score": 88,
                "strategy_codes": ["A"],
            }
        ],
        as_of=date(2026, 8, 12),
        max_monitor_bars=20,
    )

    assert result["trade"]["armed_date"] == "2026-08-07"
    assert result["trade"]["confirmation_date"] == "2026-08-10"
    assert result["trade"]["entry_date"] == "2026-08-11"
    assert result["trade"]["entry_price"] == 10.7
    assert result["trade"]["return_pct"] > 0
