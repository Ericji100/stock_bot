from pathlib import Path

from backtests.dual_ma import etf_0050_base_v1 as base
from backtests.dual_ma import etf_0050_final_strategy_v1 as final
from backtests.dual_ma import etf_0050_ma21_fixed_stop_v1 as fixed
from backtests.dual_ma import etf_0050_ma21_ma144_regime_v1 as regime
from backtests.dual_ma import etf_0050_ma21_macd_v2 as moving


RULEBOOK = Path("docs/strategies/dual_ma/0050-strategy-rulebook-v1.md")


def _text() -> str:
    return RULEBOOK.read_text(encoding="utf-8-sig")


def test_rulebook_covers_every_implemented_strategy_and_comparison_id() -> None:
    text = _text()
    required = {
        *(spec.strategy_id for spec in base.STRATEGY_SPECS),
        moving.STRATEGY_ID,
        fixed.STRATEGY_ID,
        regime.MOVING_ID,
        regime.FIXED_ID,
        *final.MAIN_IDS,
    }
    assert all(f"`{strategy_id}`" in text for strategy_id in required)


def test_rulebook_preserves_critical_semantic_boundaries() -> None:
    text = _text()
    assert "技術分析用價格" in text
    assert "未還原價格" in text
    assert "不得在同一交易日重進" in text
    assert "價格防守／停損與 MACD 出場同日成立時，價格防守優先" in text
    assert "等號歸入高市場狀態" in text
    assert "不存在「收盤進場、隔日開盤出場」的混合第三版" in text


def test_rulebook_distinguishes_strategy_timing_and_benchmark() -> None:
    text = _text()
    assert "成交版本" in text
    assert "最終報告只保留 5 條正式比較線" in text
    assert "否，只是市場基準" in text
    assert "最終定版不是第八套訊號策略" in text
