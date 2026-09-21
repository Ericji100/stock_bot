from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pandas as pd

import stock_ai_bot.selection.laoxiao_scan_service as lx
from stock_ai_bot.market.market_risk_service import MarketRiskResult


def _history(rows: int = 260, *, base: float = 100.0, daily_step: float = 0.3, volume_lots: float = 1000.0) -> pd.DataFrame:
    dates = pd.bdate_range(end="2026-05-20", periods=rows)
    close = pd.Series([base + index * daily_step for index in range(rows)], dtype=float)
    return pd.DataFrame(
        {
            "date": dates,
            "open": close - 0.2,
            "high": close,
            "low": close - 0.5,
            "close": close,
            "adj_close": close,
            "volume": volume_lots * 1000.0,
        }
    )


def test_hard_filter_has_no_maximum_price_and_no_revenue_gate():
    history = _history(base=500.0, volume_lots=600.0)
    hard = {
        "min_price": 10,
        "max_price": None,
        "min_avg_volume_20d_lots": 500,
        "min_daily_rows": 120,
    }

    assert lx._hard_filter_failure(history, hard) is None


def test_adjusted_close_is_used_for_relative_return_across_split():
    history = _history(daily_step=0)
    history.loc[:159, "close"] = 100.0
    history.loc[160:, "close"] = 50.0
    history["open"] = history["close"]
    history["high"] = history["close"]
    history["low"] = history["close"]
    history["adj_close"] = 50.0

    features = lx._technical_features(history, lx.load_laoxiao_config()["thresholds"])

    assert features["adjusted_price_status"] == "adjusted"
    assert features["return_120d_pct"] == 0.0


def test_consecutive_new_highs_keep_first_valid_breakout_date():
    history = _history(rows=100, base=50, daily_step=0.5)

    context = lx._breakout_context(history, window=20, lookback=20, confirmation_days=3)

    assert context["holding"] is True
    assert context["confirmed"] is True
    assert context["days_since"] >= 2
    assert context["current_breakout"] is True


def test_peer_ranking_and_breakout_setup_require_formal_peer_sample():
    candidates = []
    for index, code in enumerate(("1111", "2222", "3333", "4444")):
        returns = 40 - index * 10
        candidate = lx.LaoXiaoCandidate(
            code=code,
            symbol=f"{code}.TW",
            market="TWSE",
            name=code,
            industry="半導體業",
            price=100,
            avg_volume_20d=1000,
            history_rows=260,
            price_source="unit",
            adjusted_price_status="adjusted",
            features={
                "return_20d_pct": returns,
                "return_60d_pct": returns,
                "return_120d_pct": returns,
                "drawdown_252_pct": index * 5,
                "above_ma20": True,
                "above_ma60": True,
                "ma20_above_ma60": True,
                "ma20_rising": True,
                "price_up": True,
                "volume_ratio": 1.5,
                "up_down_volume_ratio": 1.4,
                "breakouts": {
                    "60": {
                        "holding": index == 0,
                        "confirmed": index == 0,
                        "current_breakout": False,
                        "breakout_date": "2026-05-15" if index == 0 else None,
                        "window": 60,
                    }
                },
            },
        )
        candidates.append(candidate)

    lx._attach_peer_metrics(candidates, min_peer_count=3)
    config = lx.load_laoxiao_config()
    risk = MarketRiskResult(date(2026, 5, 20), {}, {"all": "ok"})
    lx._score_market_stage(candidates[0], config["hard_filters"], config["thresholds"], config["weights"], risk)

    assert candidates[0].peer_count == 3
    assert candidates[0].features["relative_strength_percentile"] == 100.0
    assert candidates[0].setup_type == "strong_breakout"
    assert candidates[0].component_scores["sector_leadership"] >= 10


def test_build_scan_prefetches_only_formal_shortlist_and_returns_messages(monkeypatch):
    universe = [
        SimpleNamespace(code=code, symbol=f"{code}.TW", market="TWSE", name=f"公司{code}", industry="半導體業")
        for code in ("1111", "2222", "3333", "4444")
    ]
    histories = {
        entry.symbol: _history(base=50 + index * 5, daily_step=0.25 + index * 0.05)
        for index, entry in enumerate(universe)
    }
    captured: list[str] = []

    monkeypatch.setattr(lx, "load_stock_universe", lambda *_args, **_kwargs: universe)
    monkeypatch.setattr(lx, "_load_formal_peer_groups", lambda: {})
    monkeypatch.setattr(lx, "fetch_daily_history", lambda symbol, _date: (histories[symbol], "unit"))
    monkeypatch.setattr(
        lx,
        "load_market_risk_map",
        lambda target: MarketRiskResult(target, {}, {"twse_attention": "ok", "twse_disposition": "ok", "tpex_attention": "ok", "tpex_disposition": "ok"}),
    )

    def fake_attach(candidates, *_args, **_kwargs):
        captured.extend(item.code for item in candidates)
        for item in candidates:
            item.component_scores["fundamental_support"] = 20
            item.component_scores["theme_catalyst"] = 10

    monkeypatch.setattr(lx, "_attach_fundamental_and_theme_scores", fake_attach)

    result = lx.build_laoxiao_scan_result({}, date(2026, 5, 20))

    assert captured
    assert set(result.selected_codes).issubset(set(captured))
    assert result.diagnostics["hard_filter_passed"] == 4
    assert result.report_messages
    assert all(len(message) <= lx.REPORT_MESSAGE_MAX_CHARS for message in result.report_messages)
    assert "不設最高價" in result.report_text


def test_partial_history_applies_score_cap_and_missing_adjusted_warning():
    item = lx.LaoXiaoCandidate(
        code="1111",
        symbol="1111.TW",
        market="TWSE",
        name="測試",
        industry="半導體業",
        price=100,
        avg_volume_20d=1000,
        history_rows=150,
        price_source="unit",
        adjusted_price_status="raw_fallback",
        features={"drawdown_252_pct": 10},
        component_scores={
            "technical_stage": 30,
            "sector_leadership": 20,
            "volume_liquidity": 15,
            "fundamental_support": 20,
            "theme_catalyst": 10,
        },
    )
    config = lx.load_laoxiao_config()
    risk = MarketRiskResult(date(2026, 5, 20), {}, {"all": "ok"})

    item.component_scores["risk_data_quality"] = lx._score_risk_quality(item, config["hard_filters"], config["thresholds"], risk)
    lx._finalize_candidate_score(item, config["hard_filters"], config["thresholds"], config["weights"])

    assert item.total_score == 75
    assert any("缺少還原價" in risk_text for risk_text in item.risks)
