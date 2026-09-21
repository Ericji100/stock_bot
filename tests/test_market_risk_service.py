from __future__ import annotations

from datetime import date, datetime, timedelta

import market_risk_service as risk


def test_official_market_risk_feeds_are_merged_and_filtered(monkeypatch, tmp_path):
    target = date(2026, 5, 20)

    def fake_fetch(_client, url):
        if url.endswith("/notice"):
            return [{"Date": "1150520", "Code": "2330", "TradingInfoForAttention": "量價異常"}]
        if url.endswith("/punish"):
            return [{"Date": "1150519", "Code": "2317", "DispositionPeriod": "115/05/20～115/05/26", "ReasonsOfDisposition": "連續三次"}]
        if url.endswith("tpex_trading_warning_information"):
            return [{"Date": "1150520", "SecuritiesCompanyCode": "5425", "TradingInformation": "注意"}]
        return [{"Date": "1150519", "SecuritiesCompanyCode": "6488", "DispositionPeriod": "1150520~1150526", "DispositionReasons": "處置"}]

    monkeypatch.setattr(risk, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(risk, "_fetch_json", fake_fetch)

    result = risk.load_market_risk_map(target)

    assert result.complete is True
    assert result.by_code["2330"]["attention"] is True
    assert result.by_code["2317"]["disposition"] is True
    assert result.by_code["5425"]["attention"] is True
    assert result.by_code["6488"]["disposition"] is True


def test_source_failure_is_not_treated_as_complete(monkeypatch, tmp_path):
    def fake_fetch(_client, url):
        if "twse" in url and url.endswith("notice"):
            raise RuntimeError("offline")
        return []

    monkeypatch.setattr(risk, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(risk, "_fetch_json", fake_fetch)

    result = risk.load_market_risk_map(date(2026, 5, 20))

    assert result.complete is False
    assert result.source_status["twse_attention"].startswith("unavailable")


def test_roc_date_supports_slash_and_compact_formats():
    assert risk._roc_date("115/05/20") == date(2026, 5, 20)
    assert risk._roc_date("1150520") == date(2026, 5, 20)


def test_twse_disposition_period_supports_chinese_separator():
    assert risk._disposition_period({"DispositionPeriod": "115/05/20至115/05/26"}) == (
        date(2026, 5, 20),
        date(2026, 5, 26),
    )


def test_today_cache_expires_so_after_close_announcements_can_refresh():
    stale = {"generated_at": (datetime.now().astimezone() - timedelta(hours=2)).isoformat()}
    fresh = {"generated_at": datetime.now().astimezone().isoformat()}

    assert risk._cache_is_usable(stale, date.today()) is False
    assert risk._cache_is_usable(fresh, date.today()) is True
