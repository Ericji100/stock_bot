from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

import scripts.course_radar_trigger_backtest as backtest


def test_parse_radar_artifact_supports_legacy_and_modern_rows(tmp_path: Path) -> None:
    path = tmp_path / "radar_summary.md"
    path.write_text(
        "來源：技術面選股結果｜AI短評：略過\n"
        "1. 1234 測試股｜88分｜策略 B/D｜轉機波段\n"
        "2. 5678 第二股｜70分｜技術策略：A（多頭延續）、D（急跌收復）\n",
        encoding="utf-8",
    )

    source, rows = backtest.parse_radar_artifact(path, date(2026, 5, 21))

    assert source == "技術面選股結果"
    assert [(row["code"], row["score"]) for row in rows] == [("1234", 88), ("5678", 70)]
    assert rows[0]["strategy_codes"] == ["B", "D"]
    assert rows[1]["strategy_codes"] == ["A", "D"]


def test_replay_enters_on_trigger_and_separates_mfe_from_exit_returns(
    monkeypatch, tmp_path: Path
) -> None:
    frame = pd.DataFrame(
        [
            {"date": "2026-05-21", "open": 10.0, "high": 10.4, "low": 9.8, "close": 10.2, "ATR14": 1.0},
            {"date": "2026-05-22", "open": 10.2, "high": 10.8, "low": 9.8, "close": 10.6, "ATR14": 1.0},
            {"date": "2026-05-25", "open": 10.7, "high": 12.0, "low": 8.8, "close": 8.9, "ATR14": 1.0},
            {"date": "2026-05-26", "open": 8.7, "high": 20.0, "low": 8.5, "close": 19.0, "ATR14": 1.0},
        ]
    )
    frame["date"] = pd.to_datetime(frame["date"])
    source = tmp_path / "1234_TW.csv"
    monkeypatch.setattr(backtest, "load_full_frame", lambda symbol, as_of: (frame, source))

    def snapshot(truncated: pd.DataFrame, thresholds=backtest.THRESHOLDS):
        return {
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
                "date": date(2026, 5, 21),
                "code": "1234",
                "name": "測試股",
                "score": 88,
                "strategy_codes": ["B"],
                "source_labels": ["技術面選股結果"],
            }
        ],
        as_of=date(2026, 5, 26),
        max_monitor_bars=20,
    )

    trade = result["trades"][0]
    assert trade["armed_date"] == "2026-05-21"
    assert trade["entry_date"] == "2026-05-22"
    assert trade["entry_price"] == 10.5
    assert trade["max_favorable_price"] == 12.0
    assert trade["mfe_pct"] == 14.2857
    assert trade["exit_trigger_date"] == "2026-05-25"
    assert trade["exit_trigger_return_pct"] == -15.2381
    assert trade["exit_date"] == "2026-05-26"
    assert trade["exit_price"] == 8.7
    assert trade["realized_return_pct"] == -17.1429
    assert trade["status_label"] == "CLOSED（交易已結束）"


def test_trigger_above_chase_cap_is_not_entered(monkeypatch, tmp_path: Path) -> None:
    frame = pd.DataFrame(
        [
            {"date": "2026-05-21", "open": 10.0, "high": 10.4, "low": 9.8, "close": 10.2, "ATR14": 1.0},
            {"date": "2026-05-22", "open": 10.2, "high": 11.5, "low": 10.0, "close": 11.2, "ATR14": 1.0},
        ]
    )
    frame["date"] = pd.to_datetime(frame["date"])
    source = tmp_path / "1234_TW.csv"
    monkeypatch.setattr(backtest, "load_full_frame", lambda symbol, as_of: (frame, source))
    monkeypatch.setattr(
        backtest,
        "enlightenment_snapshot",
        lambda truncated, thresholds=backtest.THRESHOLDS: {
            "structural_invalid": False,
            "armed": True,
            "setup": {"trigger_price": 11.2, "pattern_code": "BREAKOUT"},
            "risk": {"chase_cap": 11.0, "defense": 9.0},
            "structural_lens": "QUADRANT_PRIMARY",
            "taiji": {"sequence": "NONE", "state": "UNDEFINED"},
        },
    )
    result = backtest.replay_stock(
        stock={"code": "1234", "name": "測試股", "symbol": "1234.TW", "market": "TWSE"},
        selections=[
            {
                "date": date(2026, 5, 21),
                "code": "1234",
                "name": "測試股",
                "score": 88,
                "strategy_codes": ["B"],
                "source_labels": ["技術面選股結果"],
            }
        ],
        as_of=date(2026, 5, 22),
        max_monitor_bars=20,
    )

    assert result["trades"] == []
    assert result["state_counts"]["NO_CHASE"] == 1
