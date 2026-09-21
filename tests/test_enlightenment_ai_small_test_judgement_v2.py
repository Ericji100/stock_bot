from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.enlightenment_ai_small_test_judgement_v2 import (
    DECISIONS_PATH,
    RUN,
    validate_decisions,
)


def _result() -> dict:
    return json.loads((RUN / "backtest.json").read_text(encoding="utf-8"))


def _variant(name: str) -> dict:
    return _result()["variants"][name]


def test_v2_ai_decision_audit_passes() -> None:
    audit = validate_decisions()
    assert audit["passed"]
    assert audit["approved_event_count"] == 39
    assert audit["reviewed_non_trigger_count"] == 13


def test_two_position_variants_have_expected_accounting_totals() -> None:
    fixed = _variant("MOTHER_ONLY_10K（固定母單一萬元）")["summary"]
    adds = _variant("MOTHER_PLUS_2（母單＋最多兩次獲利後加碼）")["summary"]
    assert (fixed["trade_episodes"], fixed["tranches"], fixed["open"]) == (19, 19, 0)
    assert (adds["trade_episodes"], adds["tranches"], adds["open"]) == (20, 30, 0)
    assert fixed["net_pnl"] == 30435.86
    assert adds["net_pnl"] == 41418.65
    assert fixed["maximum_concurrent_tranches"] == 4
    assert adds["maximum_concurrent_tranches"] == 7


def test_adds_are_profitable_before_signal_and_never_exceed_two() -> None:
    variant = _variant("MOTHER_PLUS_2（母單＋最多兩次獲利後加碼）")
    for stock in variant["stocks"]:
        for episode in stock["episodes"]:
            assert episode["tranche_count"] <= 3
            mother = episode["tranches"][0]
            assert mother["role"].startswith("MOTHER")
            for position, tranche in enumerate(episode["tranches"][1:], start=1):
                assert tranche["role"].startswith(f"ADD_{position}")
                assert tranche["signal_date"] > mother["signal_date"]


def test_key_v2_reclassifications_and_causal_blocks_are_preserved() -> None:
    decisions = json.loads(DECISIONS_PATH.read_text(encoding="utf-8"))
    assert next(row for row in decisions["approved_events"]["8234"] if row["signal_date"] == "2025-04-29")
    assert next(row for row in decisions["approved_events"]["8358"] if row["signal_date"] == "2026-04-13")
    assert next(row for row in decisions["approved_events"]["8358"] if row["signal_date"] == "2026-08-27")
    assert decisions["campaign_invalidations"]["6189"]["date"] == "2024-12-16"
    assert decisions["campaign_invalidations"]["6125"]["date"] == "2026-07-28"

    fixed = _variant("MOTHER_ONLY_10K（固定母單一萬元）")
    skip_signals = {
        event["signal"]["signal_date"]
        for stock in fixed["stocks"]
        for event in stock["audit"]
        if event["event"].startswith("BUY_SKIPPED")
    }
    assert {"2026-04-13", "2026-08-27"}.issubset(skip_signals)


def test_report_contains_full_tables_and_limitations() -> None:
    text = (RUN / "backtest.md").read_text(encoding="utf-8")
    for heading in (
        "## 兩種部位版本統計",
        "## 最終損益漲跌幅分布",
        "## MOTHER_ONLY_10K（固定母單一萬元）：完整逐筆回合",
        "## MOTHER_PLUS_2（母單＋最多兩次獲利後加碼）：完整逐筆回合",
        "## V2 重要未觸發／受阻事件",
        "## 研究限制",
    ):
        assert heading in text
    assert "RETROSPECTIVE_NOT_BLIND（事後研究、不是樣本外盲測）" in text
    assert "固定版是固定名目金額，不是固定風險金額" in text
