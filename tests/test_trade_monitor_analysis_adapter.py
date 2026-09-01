from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import trade_monitor_analysis_adapter as adapter
from trade_monitor_analysis_contract import DISCLAIMER, render_analysis_markdown


def sample_analysis_payload(*, decision: str = "NOTIFY") -> dict[str, object]:
    return {
        "original_decision": decision,
        "notification_reason": "當前趨勢改變。",
        "latest_closed_k_price_estimate": "收盤約 46,820～46,825（圖面估計）。",
        "latest_closed_k_details": ["屬夜盤、美股開盤後時段。"],
        "large_trend": {
            "classification": "偏多但回檔",
            "details": ["價格仍在緩升的均價105上方。"],
        },
        "current_trend": {
            "classification": "轉換中",
            "details": ["價格位於均價21附近。"],
        },
        "market_state": ["多頭結構中的高檔回檔。"],
        "pattern_observation": {
            "status": "條件式偏多",
            "pattern": "趨勢拉回延續",
            "details": ["尚未完成重新轉多觸發。"],
        },
        "missing_conditions_or_trigger": ["等待已收盤K突破拉回小區間。"],
        "entry_and_structural_stop": ["觸發完成後下一根第一個可成交價格評估。"],
        "risk_and_nearest_obstacle": ["尚無進場價，無法可靠計算1R。"],
        "single_contract_management_or_prohibition": ["目前觀望，不建立持倉。"],
    }


def write_config(path: Path, *, quiet: bool = False) -> Path:
    path.write_text(
        json.dumps(
            {
                "trade_monitor_telegram_enabled": False,
                "trade_monitor_send_quiet_status": quiet,
            }
        ),
        encoding="utf-8",
    )
    return path


def prepare(tmp_path: Path, timestamp: str, content: bytes = b"chart") -> dict[str, object]:
    image = tmp_path / f"chart-{timestamp[14:19].replace(':', '-')}.png"
    image.write_bytes(content)
    return adapter.prepare_capture(
        image,
        datetime.fromisoformat(timestamp),
        state_path=tmp_path / "analysis-state.json",
    )


def test_prepare_anchors_latest_closed_bar_to_previous_minute(tmp_path: Path) -> None:
    result = prepare(tmp_path, "2026-09-01T23:11:26+08:00")

    assert result["status"] == "FRESH"
    assert result["requires_analysis"] is True
    assert result["context"]["expected_latest_closed_k_iso"] == "2026-09-01T23:10:00+08:00"
    assert result["context"]["expected_latest_closed_k_hhmm"] == "23:10"
    assert result["context"]["current_unclosed_k_hhmm"] == "23:11"


def test_prepare_detects_same_bar_after_successful_finalize(tmp_path: Path) -> None:
    first = prepare(tmp_path, "2026-09-01T23:11:05+08:00")
    result = asyncio.run(
        adapter.finalize_analysis(
            context_id=str(first["context_id"]),
            analysis_payload=sample_analysis_payload(),
            force_notify=False,
            run_id=None,
            state_path=tmp_path / "analysis-state.json",
            config_path=write_config(tmp_path / "config.json"),
            delivery_state_path=tmp_path / "delivery.json",
            runtime_state_path=tmp_path / "runtime.json",
            dry_run=True,
        )
    )
    duplicate = prepare(tmp_path, "2026-09-01T23:11:55+08:00", b"changed-chart")

    assert result["analysis_valid"] is True
    assert duplicate["status"] == "SAME_BAR"
    assert duplicate["requires_analysis"] is False


def test_prepare_detects_stale_unchanged_chart(tmp_path: Path) -> None:
    prepare(tmp_path, "2026-09-01T23:11:05+08:00", b"frozen")
    stale = prepare(tmp_path, "2026-09-01T23:13:05+08:00", b"frozen")

    assert stale["status"] == "STALE"
    assert stale["context"]["unchanged_image_seconds"] == 120


def test_prepare_detects_time_regression(tmp_path: Path) -> None:
    first = prepare(tmp_path, "2026-09-01T23:11:05+08:00")
    asyncio.run(
        adapter.finalize_analysis(
            context_id=str(first["context_id"]),
            analysis_payload=sample_analysis_payload(),
            force_notify=False,
            run_id=None,
            state_path=tmp_path / "analysis-state.json",
            config_path=write_config(tmp_path / "config.json"),
            delivery_state_path=tmp_path / "delivery.json",
            dry_run=True,
        )
    )
    regression = prepare(tmp_path, "2026-09-01T23:10:05+08:00", b"older")

    assert regression["status"] == "REGRESSION"


def test_renderer_uses_fixed_heading_order_and_one_authoritative_time() -> None:
    context = {
        "expected_latest_closed_k_hhmm": "23:10",
        "current_unclosed_k_hhmm": "23:11",
        "new_closed_bar_count": 1,
    }
    message = render_analysis_markdown(sample_analysis_payload(), context, resumed=False)
    headings = [
        "**時間／最新已收盤 K**",
        "**大趨勢**",
        "**當前趨勢**",
        "**市場狀態**",
        "**觀察型態與狀態**",
        "**尚缺條件／觸發**",
        "**進場與結構停損**",
        "**1R與最近障礙**",
        "**單口管理／禁止原因**",
    ]

    assert [message.index(heading) for heading in headings] == sorted(message.index(heading) for heading in headings)
    assert message.count("23:10") == 1
    assert message.count(DISCLAIMER) == 1


def test_finalize_force_notify_keeps_original_decision_and_adds_resume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prepared = prepare(tmp_path, "2026-09-01T23:11:05+08:00")
    calls: list[dict[str, object]] = []

    async def fake_bridge(**kwargs):
        calls.append(kwargs)
        return 0, {"ok": True, "status": "sent", "event_id": kwargs["event_id"]}

    monkeypatch.setattr(adapter, "execute_bridge", fake_bridge)
    result = asyncio.run(
        adapter.finalize_analysis(
            context_id=str(prepared["context_id"]),
            analysis_payload=sample_analysis_payload(decision="DONT_NOTIFY"),
            force_notify=True,
            run_id=None,
            state_path=tmp_path / "analysis-state.json",
            config_path=write_config(tmp_path / "config.json"),
            delivery_state_path=tmp_path / "delivery.json",
            runtime_state_path=tmp_path / "runtime.json",
        )
    )

    assert result["original_decision"] == "DONT_NOTIFY"
    assert result["decision"] == "NOTIFY"
    assert "**監控已恢復**" in result["message"]
    assert result["resume_delivered"] is True
    assert len(calls) == 1
    assert calls[0]["message"] == result["message"]


def test_finalize_dont_notify_never_calls_bridge_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prepared = prepare(tmp_path, "2026-09-01T23:11:05+08:00")

    async def unexpected_bridge(**_kwargs):
        raise AssertionError("bridge must not be called")

    monkeypatch.setattr(adapter, "execute_bridge", unexpected_bridge)
    result = asyncio.run(
        adapter.finalize_analysis(
            context_id=str(prepared["context_id"]),
            analysis_payload=sample_analysis_payload(decision="DONT_NOTIFY"),
            force_notify=False,
            run_id=None,
            state_path=tmp_path / "analysis-state.json",
            config_path=write_config(tmp_path / "config.json"),
            delivery_state_path=tmp_path / "delivery.json",
            runtime_state_path=tmp_path / "runtime.json",
        )
    )

    assert result["decision"] == "DONT_NOTIFY"
    assert result["telegram_status"] == "not_called"


def test_invalid_structured_analysis_fails_closed_without_direction(tmp_path: Path) -> None:
    prepared = prepare(tmp_path, "2026-09-01T23:11:05+08:00")
    invalid = sample_analysis_payload()
    invalid.pop("large_trend")
    result = asyncio.run(
        adapter.finalize_analysis(
            context_id=str(prepared["context_id"]),
            analysis_payload=invalid,
            force_notify=False,
            run_id=None,
            state_path=tmp_path / "analysis-state.json",
            config_path=write_config(tmp_path / "config.json"),
            delivery_state_path=tmp_path / "delivery.json",
            runtime_state_path=tmp_path / "runtime.json",
            dry_run=True,
        )
    )

    assert result["analysis_valid"] is False
    assert result["decision"] == "NOTIFY"
    assert "本輪不採用任何方向或價位" in result["message"]
    assert "禁止依本輪資料建立真實或模擬持倉" in result["message"]


def test_output_never_contains_configured_credentials(tmp_path: Path) -> None:
    prepared = prepare(tmp_path, "2026-09-01T23:11:05+08:00")
    config = tmp_path / "config.json"
    secret_token = "123456:super-secret-token"
    secret_chat = "-1000000000000"
    config.write_text(
        json.dumps(
            {
                "api_token": secret_token,
                "trade_monitor_chat_id": secret_chat,
                "trade_monitor_telegram_enabled": False,
                "trade_monitor_send_quiet_status": False,
            }
        ),
        encoding="utf-8",
    )
    result = asyncio.run(
        adapter.finalize_analysis(
            context_id=str(prepared["context_id"]),
            analysis_payload=sample_analysis_payload(),
            force_notify=False,
            run_id=None,
            state_path=tmp_path / "analysis-state.json",
            config_path=config,
            delivery_state_path=tmp_path / "delivery.json",
            runtime_state_path=tmp_path / "runtime.json",
            dry_run=True,
        )
    )
    serialized = json.dumps(result, ensure_ascii=False)

    assert secret_token not in serialized
    assert secret_chat not in serialized
