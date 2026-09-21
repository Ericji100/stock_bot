from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import trade_monitor.analysis_adapter as adapter
from trade_monitor.analysis_contract import (
    DISCLAIMER,
    AnalysisValidationError,
    render_analysis_markdown,
    render_quiet_status_markdown,
)
from trade_monitor.market_structure_state import empty_market_structure_state


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
        constitution_state_path=tmp_path / "constitution-state.json",
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
            constitution_state_path=tmp_path / "constitution-state.json",
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


def test_material_scenario_change_forces_full_notify(tmp_path: Path) -> None:
    prior_as_of = "2026-09-02T19:10:00+08:00"
    state_path = tmp_path / "analysis-state.json"
    state_path.write_text(
        json.dumps({
            "version": 1,
            "pending": {},
            "market_structure_state": empty_market_structure_state(
                as_of=prior_as_of,
                session_key="2026-09-02-NIGHT",
            ),
        }),
        encoding="utf-8",
    )
    prepared = prepare(tmp_path, "2026-09-02T19:12:05+08:00", b"new-chart")
    as_of = str(prepared["context"]["expected_latest_closed_k_iso"])
    payload = sample_analysis_payload(decision="DONT_NOTIFY")
    payload["constitution_event"] = {
        "event_type": "NONE", "event_id": f"NONE|{as_of}", "setup_id": None,
        "position_id": None, "direction": None, "entry_price_estimate": None,
        "stop_price_estimate": None, "risk_points": None,
        "latest_closed_bar_time": as_of, "reason": "沒有交易憲法異動。",
    }
    structure = empty_market_structure_state(as_of=as_of, session_key="2026-09-02-NIGHT")
    structure["scenario_context"].update({
        "wave_phase": "PULLBACK",
        "locations": ["NEAR_SUPPORT"],
        "grade_alignment": "ALIGNED",
        "hold_scenario": "支撐守住後等待多方重新發動。",
        "break_scenario": "收盤跌破支撐後候選失效。",
    })
    setup = structure["scenario_context"]["setup"]
    setup.update({
        "setup_id": "TREND_PULLBACK_LONG|1911",
        "pattern": "TREND_PULLBACK_CONTINUATION",
        "direction": "LONG",
        "stage": "ARMED",
        "first_seen_at": as_of,
        "stage_changed_at": as_of,
    })
    payload["market_structure_state"] = structure

    result = asyncio.run(
        adapter.finalize_analysis(
            context_id=str(prepared["context_id"]),
            analysis_payload=payload,
            force_notify=False,
            run_id=None,
            state_path=state_path,
            config_path=write_config(tmp_path / "config.json"),
            delivery_state_path=tmp_path / "delivery.json",
            runtime_state_path=tmp_path / "runtime.json",
            constitution_state_path=tmp_path / "constitution-state.json",
            dry_run=True,
        )
    )
    assert result["original_decision"] == "NOTIFY"
    assert result["decision"] == "NOTIFY"
    assert "**時間／最新已收盤 K**" in result["message"]
    assert result["message"].count("**") >= 18


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
            constitution_state_path=tmp_path / "constitution-state.json",
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
        "**一倍初始風險與最近障礙**",
        "**單口管理／禁止原因**",
    ]

    assert [message.index(heading) for heading in headings] == sorted(message.index(heading) for heading in headings)
    assert message.count("23:10") == 1
    assert message.count(DISCLAIMER) == 1


def test_renderer_adds_confirmed_small_bear_defense_from_structured_state() -> None:
    as_of = "2026-09-03T10:06:00+08:00"
    structure = empty_market_structure_state(
        as_of=as_of,
        session_key="2026-09-03-DAY",
        version=6,
    )
    structure["primary_pivots"] = [
        {
            "pivot_id": "P1-H-0956",
            "kind": "HIGH",
            "state": "PAIRED_CONFIRMED",
            "bar_time": "2026-09-03T09:56:00+08:00",
            "price_estimate": 46182.0,
            "first_seen_at": as_of,
            "locally_confirmed_at": as_of,
            "paired_confirmed_at": as_of,
            "terminal_at": None,
            "replaced_by": None,
            "proportionality": "NORMAL",
        }
    ]
    structure["dow_state_small"] = "BEAR"
    structure["defense_lines"]["small_bear"] = {
        "direction": "BEAR",
        "pivot_id": "P1-H-0956",
        "price_estimate": 46182.0,
        "qualified_at": as_of,
        "status": "ACTIVE",
        "ended_at": None,
    }
    payload = sample_analysis_payload()
    payload["large_trend"] = {"classification": "強勢偏空", "details": ["大級持續向下。"]}
    payload["current_trend"] = {"classification": "偏空", "details": ["小級持續向下。"]}
    payload["market_structure_state"] = structure
    message = render_analysis_markdown(
        payload,
        {
            "expected_latest_closed_k_hhmm": "10:06",
            "current_unclosed_k_hhmm": "10:07",
            "new_closed_bar_count": 1,
        },
        resumed=False,
    )

    assert "小級空頭防線已成立" in message
    assert "09:56 的樞紐高點" in message
    assert "約46182點（圖面估計）" in message


def test_renderer_rejects_aligned_strong_trend_that_downgrades_dow_state_to_undefined() -> None:
    as_of = "2026-09-03T10:06:00+08:00"
    payload = sample_analysis_payload()
    payload["large_trend"] = {"classification": "強勢偏空", "details": ["大級持續向下。"]}
    payload["current_trend"] = {"classification": "偏空", "details": ["小級持續向下。"]}
    payload["market_structure_state"] = empty_market_structure_state(
        as_of=as_of,
        session_key="2026-09-03-DAY",
        version=6,
    )

    with pytest.raises(AnalysisValidationError, match="active same-direction Dow defense"):
        render_analysis_markdown(
            payload,
            {
                "expected_latest_closed_k_hhmm": "10:06",
                "current_unclosed_k_hhmm": "10:07",
                "new_closed_bar_count": 1,
            },
            resumed=False,
        )


def test_renderer_explains_gap_rebuilds_full_visible_context_without_retroactive_entry() -> None:
    context = {
        "expected_latest_closed_k_hhmm": "23:10",
        "current_unclosed_k_hhmm": "23:11",
        "new_closed_bar_count": 16,
    }

    message = render_analysis_markdown(sample_analysis_payload(), context, resumed=True)

    assert "已使用畫面中可見的歷史 K 完整重建大趨勢、當前趨勢、定錨、防線與目前情境" in message
    assert "僅不把中斷期間已走完的歷史訊號追認為模擬成交" in message
    assert "只重建目前結構" not in message


def test_quiet_renderer_is_short_canonical_status() -> None:
    context = {
        "expected_latest_closed_k_hhmm": "23:10",
        "current_unclosed_k_hhmm": "23:11",
        "new_closed_bar_count": 1,
    }

    message = render_quiet_status_markdown(
        sample_analysis_payload(decision="DONT_NOTIFY"),
        context,
    )

    assert message.startswith("23:10｜大趨勢：偏多但回檔｜當前趨勢：轉換中｜條件式偏多：趨勢拉回延續。")
    assert "**時間／最新已收盤 K**" not in message
    assert message.count(DISCLAIMER) == 1
    assert len(message) < 300


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
            constitution_state_path=tmp_path / "constitution-state.json",
        )
    )

    assert result["original_decision"] == "DONT_NOTIFY"
    assert result["decision"] == "NOTIFY"
    assert "**監控已恢復**" in result["message"]
    assert result["resume_delivered"] is True
    assert len(calls) == 1
    assert calls[0]["message"] == result["message"]


def test_finalize_notify_sends_same_full_message_to_telegram(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = prepare(tmp_path, "2026-09-01T23:11:05+08:00")
    calls: list[dict[str, object]] = []

    async def fake_bridge(**kwargs):
        calls.append(kwargs)
        return 0, {"ok": True, "status": "sent", "event_id": kwargs["event_id"]}

    monkeypatch.setattr(adapter, "execute_bridge", fake_bridge)
    result = asyncio.run(
        adapter.finalize_analysis(
            context_id=str(prepared["context_id"]),
            analysis_payload=sample_analysis_payload(decision="NOTIFY"),
            force_notify=False,
            run_id=None,
            state_path=tmp_path / "analysis-state.json",
            config_path=write_config(tmp_path / "config.json"),
            delivery_state_path=tmp_path / "delivery.json",
            runtime_state_path=tmp_path / "runtime.json",
            constitution_state_path=tmp_path / "constitution-state.json",
        )
    )

    assert result["decision"] == "NOTIFY"
    assert result["telegram_status"] == "sent"
    assert len(calls) == 1
    assert calls[0]["message"] == result["message"]
    assert "**時間／最新已收盤 K**" in result["message"]
    assert result["message"].count(DISCLAIMER) == 1


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
            constitution_state_path=tmp_path / "constitution-state.json",
        )
    )

    assert result["decision"] == "DONT_NOTIFY"
    assert result["telegram_status"] == "not_called"
    assert result["message"].startswith(
        "23:10｜大趨勢：偏多但回檔｜當前趨勢：轉換中｜條件式偏多：趨勢拉回延續。"
    )
    assert "**時間／最新已收盤 K**" not in result["message"]


def test_finalize_quiet_mode_sends_same_short_message_to_telegram(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
            force_notify=False,
            run_id=None,
            state_path=tmp_path / "analysis-state.json",
            config_path=write_config(tmp_path / "config.json", quiet=True),
            delivery_state_path=tmp_path / "delivery.json",
            runtime_state_path=tmp_path / "runtime.json",
            constitution_state_path=tmp_path / "constitution-state.json",
        )
    )

    assert result["decision"] == "DONT_NOTIFY"
    assert result["telegram_status"] == "sent"
    assert len(calls) == 1
    assert calls[0]["decision"] == "DONT_NOTIFY"
    assert calls[0]["message"] == result["message"]
    assert "**時間／最新已收盤 K**" not in result["message"]


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
            constitution_state_path=tmp_path / "constitution-state.json",
            dry_run=True,
        )
    )

    assert result["analysis_valid"] is False
    assert result["decision"] == "NOTIFY"
    assert "圖表擷取正常，但內部結構化狀態未通過一致性驗證" in result["message"]
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
            constitution_state_path=tmp_path / "constitution-state.json",
            dry_run=True,
        )
    )
    serialized = json.dumps(result, ensure_ascii=False)

    assert secret_token not in serialized
    assert secret_chat not in serialized


def test_renderer_rejects_duplicate_adapter_owned_times() -> None:
    context = {
        "expected_latest_closed_k_hhmm": "23:10",
        "current_unclosed_k_hhmm": "23:11",
        "new_closed_bar_count": 1,
    }
    payload = sample_analysis_payload()
    payload["latest_closed_k_details"] = ["23:10 已收盤。"]

    with pytest.raises(AnalysisValidationError):
        render_analysis_markdown(payload, context, resumed=False)


def test_finalize_applies_valid_internal_constitution_event_without_tenth_section(tmp_path: Path) -> None:
    prepared = prepare(tmp_path, "2026-09-02T15:02:05+08:00")
    latest = str(prepared["context"]["expected_latest_closed_k_iso"])
    payload = sample_analysis_payload()
    payload["constitution_event"] = {
        "event_type": "SIM_ENTER",
        "event_id": "SIM_ENTER|2026-09-02T15:01:00+08:00|setup-a|p1",
        "setup_id": "setup-a",
        "position_id": "p1",
        "direction": "LONG",
        "entry_price_estimate": 46800,
        "stop_price_estimate": 46780,
        "risk_points": 20,
        "latest_closed_bar_time": latest,
        "reason": "已完成允許的模擬進場觸發。",
    }

    result = asyncio.run(
        adapter.finalize_analysis(
            context_id=str(prepared["context_id"]),
            analysis_payload=payload,
            force_notify=False,
            run_id=None,
            state_path=tmp_path / "analysis-state.json",
            config_path=write_config(tmp_path / "config.json"),
            delivery_state_path=tmp_path / "delivery.json",
            runtime_state_path=tmp_path / "runtime.json",
            constitution_state_path=tmp_path / "constitution-state.json",
            dry_run=True,
        )
    )

    state = json.loads((tmp_path / "constitution-state.json").read_text(encoding="utf-8"))
    assert result["analysis_valid"] is True
    assert result["constitution_status"] == "applied"
    assert state["simulated_entry_count"] == 1
    assert state["simulated_position"]["position_id"] == "p1"
    assert "constitution_event" not in result["message"]


def test_constitution_state_change_cannot_be_hidden_by_dont_notify() -> None:
    payload = sample_analysis_payload(decision="DONT_NOTIFY")
    payload["constitution_event"] = {
        "event_type": "SIM_ENTER",
        "event_id": "SIM_ENTER|2026-09-02T15:01:00+08:00|setup-a|p1",
        "setup_id": "setup-a",
        "position_id": "p1",
        "direction": "LONG",
        "entry_price_estimate": 46800,
        "stop_price_estimate": 46780,
        "risk_points": 20,
        "latest_closed_bar_time": "2026-09-02T15:01:00+08:00",
        "reason": "test",
    }

    with pytest.raises(AnalysisValidationError, match="require original_decision"):
        render_analysis_markdown(
            payload,
            {
                "expected_latest_closed_k_hhmm": "15:01",
                "current_unclosed_k_hhmm": "15:02",
                "new_closed_bar_count": 1,
            },
            resumed=False,
        )

def test_prepare_can_upgrade_v2_structure_to_v3_without_writing_it_early(tmp_path: Path) -> None:
    image = tmp_path / "chart.png"
    image.write_bytes(b"png")
    state_path = tmp_path / "analysis_state.json"
    v2 = empty_market_structure_state(
        as_of="2026-09-02T09:59:00+08:00",
        session_key="2026-09-02-DAY",
    )
    state_path.write_text(
        json.dumps({"version": 1, "pending": {}, "market_structure_state": v2}),
        encoding="utf-8",
    )

    prepared = adapter.prepare_capture(
        image,
        datetime.fromisoformat("2026-09-02T10:01:05+08:00"),
        state_path=state_path,
        constitution_state_path=tmp_path / "constitution.json",
        target_market_structure_version=3,
    )

    assert prepared["market_structure_state"]["version"] == 3
    assert prepared["market_structure_state"]["anchor_context"]["anchors"] == []
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted["market_structure_state"]["version"] == 2


def test_prepare_can_upgrade_v3_structure_to_v4_without_writing_it_early(tmp_path: Path) -> None:
    image = tmp_path / "chart.png"
    image.write_bytes(b"png")
    state_path = tmp_path / "analysis_state.json"
    v3 = empty_market_structure_state(
        as_of="2026-09-02T09:59:00+08:00",
        session_key="2026-09-02-DAY",
        version=3,
    )
    state_path.write_text(
        json.dumps({"version": 1, "pending": {}, "market_structure_state": v3}),
        encoding="utf-8",
    )

    prepared = adapter.prepare_capture(
        image,
        datetime.fromisoformat("2026-09-02T10:01:05+08:00"),
        state_path=state_path,
        constitution_state_path=tmp_path / "constitution.json",
        target_market_structure_version=4,
    )

    assert prepared["market_structure_state"]["version"] == 4
    assert prepared["market_structure_state"]["cclass_context"]["engine_mode"] == "UNDEFINED"
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted["market_structure_state"]["version"] == 3


def test_prepare_can_upgrade_v4_structure_to_v5_without_writing_it_early(tmp_path: Path) -> None:
    image = tmp_path / "chart.png"
    image.write_bytes(b"png")
    state_path = tmp_path / "analysis_state.json"
    v4 = empty_market_structure_state(
        as_of="2026-09-02T09:59:00+08:00",
        session_key="2026-09-02-DAY",
        version=4,
    )
    state_path.write_text(
        json.dumps({"version": 1, "pending": {}, "market_structure_state": v4}),
        encoding="utf-8",
    )

    prepared = adapter.prepare_capture(
        image,
        datetime.fromisoformat("2026-09-02T10:01:05+08:00"),
        state_path=state_path,
        constitution_state_path=tmp_path / "constitution.json",
        target_market_structure_version=5,
    )

    assert prepared["market_structure_state"]["version"] == 5
    decision = prepared["market_structure_state"]["decision_chain_context"]
    assert decision["process_stage"] == "UNDEFINED"
    assert decision["opening_context"]["first_endpoint_side"] == "NONE"
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted["market_structure_state"]["version"] == 4
