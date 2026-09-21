from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from trade_monitor.market_structure_state import empty_market_structure_state
from trade_monitor_replay.presentation import (
    ARMED,
    ENTRY,
    MANAGEMENT,
    SNAPSHOT,
    STOP,
    UNCHANGED,
    _select_playbook,
    _v3_course_method_lines,
    _v3_position_lines,
    _v3_public_text,
    _v3_taiji_public_text,
    render_replay_event_card,
)
from trade_monitor_replay.runner import _quiet_status_delivery_due, _recent_validation_errors


TAIPEI = ZoneInfo("Asia/Taipei")
AS_OF = "2026-08-27T09:17:00+08:00"


def test_public_text_does_not_duplicate_existing_bar_time_and_role() -> None:
    ledger = {
        "recent_bar_levels": [
            {
                "time": "2026-08-24T08:45:00+08:00",
                "open": 45045,
                "high": 45117,
                "low": 45030,
                "close": 45100,
            }
        ]
    }

    rendered = _v3_public_text(
        "父代為08:45開盤價45,045點至同根高點45,117點。",
        ledger,
        150,
    )

    assert rendered == "父代為08:45開盤價45,045點至同根高點45,117點。"


def test_taiji_public_text_distinguishes_current_generation_from_dynasty_anchor() -> None:
    ledger = {
        "anchor_lifecycle": {
            "background_anchor": {
                "origin_price": 44467.0,
                "first_extreme_price": 44176.0,
            },
            "taiji_context": {
                "parent_start_price": 44405.0,
                "parent_end_price": 44150.0,
            },
        }
    }

    rendered = _v3_taiji_public_text(
        "大級空方父代為44405.0至44150.0；其後修正擴大。",
        ledger,
        150,
    )

    assert "本代空方父代" in rendered
    assert "大級空方父代" not in rendered


def test_course_method_lines_do_not_repeat_taiji_label() -> None:
    reading = {
        "taiji": "太極：父代推進後進入複製。",
        "yizhi": "動能尚未接管。",
        "x_stage": "SCENARIO_RANKING",
        "x_process": "依序選擇主鏡頭。",
    }

    lines = _v3_course_method_lines(reading, ledger={})

    assert lines[0] == "太極：父代推進後進入複製。"


def test_exit_card_labels_causal_close_as_signal_not_fill() -> None:
    lines = _v3_position_lines(
        "EXIT",
        {"status": "LONG", "entry_price": 44461.0},
        {"status": "FLAT"},
        {"management": "依動機失效退出。"},
        ledger={
            "latest_closed_k": {
                "time": "2026-08-25T11:40:00+08:00",
                "close": 44490.0,
            }
        },
    )

    assert "出場訊號：11:40收44,490點｜按下一根1分K第一個可成交價執行" in lines
    assert not any("模擬出場" in line for line in lines)
    assert not any("+29點" in line for line in lines)


def test_stop_card_uses_protective_fill_instead_of_bar_close() -> None:
    lines = _v3_position_lines(
        "STOP",
        {"status": "LONG", "entry_price": 45183.0},
        {"status": "FLAT"},
        {"management": "既有保護價已觸及。", "stop_price": 45813.8},
        ledger={
            "latest_closed_k": {
                "time": "2026-08-26T11:28:00+08:00",
                "close": 45814.0,
            }
        },
    )

    assert "模擬停損：11:28 45,814點｜+631點" in lines
    assert "11:28收" not in "\n".join(lines)


def _payload(*, decision: str = "NOTIFY", event_type: str = "NONE") -> dict:
    state = empty_market_structure_state(
        as_of=AS_OF,
        session_key="2026-08-27-DAY",
        version=6,
    )
    state["primary_pivots"] = [
        {
            "pivot_id": "p-low-0901",
            "kind": "LOW",
            "bar_time": "2026-08-27T09:01:00+08:00",
            "price_estimate": 46077,
            "state": "PAIRED_CONFIRMED",
        },
        {
            "pivot_id": "p-high-0907",
            "kind": "HIGH",
            "bar_time": "2026-08-27T09:07:00+08:00",
            "price_estimate": 46417,
            "state": "LOCAL_CONFIRMED",
        },
    ]
    state["defense_lines"]["small_bull"] = {
        "direction": "BULL",
        "pivot_id": "p-low-0901",
        "price_estimate": 46077,
        "status": "ACTIVE",
    }
    state["anchor_context"] = {
        "anchors": [
            {
                "anchor_id": "a-small-bull-0901",
                "parent_anchor_id": None,
                "direction": "BULL",
                "start_bar_time": "2026-08-27T09:01:00+08:00",
                "start_price_estimate": 46077,
                "extreme_bar_time": "2026-08-27T09:07:00+08:00",
                "extreme_price_estimate": 46417,
                "status": "CONFIRMED",
                "quality": "CLEAN",
            }
        ],
        "active_large_anchor_id": None,
        "active_small_anchor_id": "a-small-bull-0901",
        "controlling_grade": "SMALL",
        "grade_relation": "ONLY_SMALL",
        "control_reason": "小級多方定錨暫時取得控制。",
        "trend_dynamics": "DECREASING",
        "volatility_dynamics": "CONTRACTING",
        "working_quadrant": "Q4",
        "quadrant_reason": "多方推進後進入波動收縮的回檔觀察。",
    }
    state["cclass_context"] = {
        "engine_mode": "TAIJI_ORDERED",
        "order_state": "ORDERED",
        "order_reason": "多方複製與修正仍可辨識。",
        "taiji_context": {
            "active_leg_id": "leg-2",
            "anchor_time_status": "FRESH",
            "previous_context_alignment": "NEUTRAL",
            "legs": [
                {
                    "leg_id": "leg-2",
                    "sequence": "CORRECTION_2",
                    "role": "CORRECTION",
                    "correction_quality": "CAUTION",
                }
            ],
        },
        "momentum_context": {"stage": "NONE"},
    }
    state["decision_chain_context"] = {
        "process_stage": "SETUP_EVALUATION",
        "structure_context": {
            "confirmed_leg_count": 2,
            "analysis_lens": "COMBINED_CONFIRMATION",
            "family_dna": "CHANGING",
            "confluences": ["COPY_CORRECTION"],
        },
        "opportunity_context": {
            "course_grade": "B_CANDIDATE",
            "probability_evidence": "MEDIUM",
            "payoff_evidence": "HIGH",
        },
        "opening_context": {"cash_gap_source": "UNAVAILABLE"},
    }
    state["scenario_context"] = {
        "setup": {
            "pattern": "QUADRANT_Q4_TREND_ZONE",
            "direction": "LONG",
            "stage": "ARMED",
        }
    }
    playbook = {
        "status": "AVAILABLE",
        "execution_style": "STANDARD_STRUCTURAL",
        "observation_zone": {"low": 46190, "high": 46225},
        "trigger_zone": {"low": 46230, "high": 46245},
        "invalidation_zone": {"low": 46070, "high": 46077},
        "first_obstacle_zone": {"low": 46390, "high": 46417},
        "required_k_behavior": "回檔速度放慢並形成修正低點。",
        "close_trigger": "收盤重新站回46,230點上方。",
        "next_bar_entry": "下一根不跌回觸發區下方才評估。",
        "structural_stop": "跌破46,077點的多方防線。",
        "no_chase": "直接拉離46,245點不追。",
        "expected_behavior": "觸發後兩根內應重新挑戰前高。",
        "max_wait_bars": 2,
        "behavior_invalidation": "兩根內無法推進或再破修正低點。",
        "exit_plan": "應有行為失敗即退出。",
        "switch_condition": "小級多頭防線遭已收盤K破壞。",
    }
    state["prospective_context"] = {
        "primary_hypothesis": {
            "direction": "BULL",
            "status": "STRENGTHENING",
            "confidence": "MEDIUM",
            "title": "多方定錨後的良性修正",
            "thesis": "若守住原突破區，第四象限仍有延續條件",
            "next_scenario": "轉為盤整並等待重新定錨",
        },
        "alternative_hypothesis": {
            "direction": "BEAR",
            "status": "POTENTIAL",
            "confidence": "LOW",
            "title": "修正擴大為反向定錨",
            "thesis": "若跌破多方防線，空方才接手",
            "next_scenario": "改看空方結構建立",
        },
        "long_playbook": playbook,
        "short_playbook": {
            **playbook,
            "status": "FORMING",
            "close_trigger": "收盤跌破46,077點且反彈不回。",
        },
    }
    return {
        "original_decision": decision,
        "notification_reason": "第四象限回檔進入觀察區，等待收盤觸發。",
        "latest_closed_k_price_estimate": "約46,205點",
        "latest_closed_k_details": [
            "本根開46,220、高46,230、低46,190、收46,205，成交量3,308口。",
            "未收盤即時K為46,210點。",
        ],
        "large_trend": {"classification": "盤整", "details": ["大級仍在寬幅整理。"]},
        "current_trend": {"classification": "偏多", "details": ["小級多方定錨後回檔。"]},
        "market_state": ["一號盤；箱型S／S／T／V品質仍待確認。"],
        "pattern_observation": {
            "status": "條件式偏多",
            "pattern": "第四象限順勢區域",
            "details": ["太極複製尚未完成。"],
        },
        "missing_conditions_or_trigger": ["等待修正低點與已收盤突破。"],
        "entry_and_structural_stop": ["尚未進場。"],
        "risk_and_nearest_obstacle": ["最近障礙為09:07高點46,417點。"],
        "single_contract_management_or_prohibition": ["觸發前維持空手，不預判進場。"],
        "constitution_event": {
            "event_type": event_type,
            "direction": "LONG" if event_type == "SIM_ENTER" else None,
            "entry_price_estimate": 46235 if event_type == "SIM_ENTER" else None,
            "stop_price_estimate": 46075 if event_type == "SIM_ENTER" else None,
            "risk_points": 160 if event_type == "SIM_ENTER" else None,
            "reason": "條件事件測試。",
        },
        "market_structure_state": state,
    }


def _context() -> dict:
    return {
        "expected_latest_closed_k_iso": AS_OF,
        "expected_latest_closed_k_hhmm": "09:17",
    }


def test_observation_card_preserves_course_structure_without_raw_bar_noise() -> None:
    rendered = render_replay_event_card(_payload(), _context(), stage="day")
    message = rendered.body

    assert rendered.kind == ARMED
    assert "小錨：多方 09:01 46,077點 → 09:07 46,417點｜+340點／6分鐘" in message
    assert "09:07 高點46,417點（局部成立）" in message
    assert "小級多頭防線：46,077點（源自09:01低點；有效）" in message
    assert "四象限：第四象限" in message
    assert "目前第二段修正" in message
    assert "主鏡頭為太極＋四象限共同確認" in message
    assert "質性條件，不是統計勝率" in message
    assert "觀察區：46,190～46,225點" in message
    assert "未收盤即時K" not in message
    assert "成交量3,308" not in message
    assert "kind=" not in message
    assert "bar_time=" not in message
    assert "Q4" not in message
    assert len(message) < 4096


def test_quiet_card_is_short_and_does_not_dump_every_internal_field() -> None:
    rendered = render_replay_event_card(
        _payload(decision="DONT_NOTIFY"),
        _context(),
        stage="day",
    )
    assert rendered.kind == UNCHANGED
    assert "條件不變，繼續觀望" in rendered.body
    assert "定錨／樞紐／道氏防線" not in rendered.body
    assert "未收盤即時K" not in rendered.body
    assert len(rendered.body.splitlines()) <= 7


def test_preopen_entry_management_and_stop_have_distinct_message_kinds() -> None:
    assert render_replay_event_card(_payload(), _context(), stage="preopen").kind == SNAPSHOT

    entry = render_replay_event_card(
        _payload(event_type="SIM_ENTER"),
        _context(),
        stage="day",
    )
    assert entry.kind == ENTRY
    assert "模擬多方：進場約46,235點" in entry.body
    assert "一倍初始風險位置：約46,395點" in entry.body

    snapshot = {
        "simulated_position": {
            "direction": "LONG",
            "entry_price_estimate": 46235,
            "stop_price_estimate": 46075,
            "risk_points": 160,
        }
    }
    management = render_replay_event_card(
        _payload(),
        _context(),
        stage="day",
        constitution_snapshot=snapshot,
    )
    assert management.kind == MANAGEMENT
    assert "應有行為" in management.body

    stopped_payload = _payload(event_type="SIM_STOP")
    stopped_payload["constitution_event"]["reason"] = "已收盤K跌破原結構停損。"
    stopped = render_replay_event_card(
        stopped_payload,
        _context(),
        stage="day",
        constitution_snapshot=snapshot,
    )
    assert stopped.kind == STOP
    assert "出場原因：已收盤K跌破原結構停損" in stopped.body


def test_neutral_primary_does_not_fall_back_to_long_playbook() -> None:
    payload = _payload()
    state = payload["market_structure_state"]
    state["scenario_context"]["setup"]["direction"] = None
    state["prospective_context"]["primary_hypothesis"]["direction"] = "UNDEFINED"
    assert _select_playbook(payload, state) is None


def test_quiet_delivery_is_due_only_after_interval_since_success(tmp_path: Path) -> None:
    path = tmp_path / "deliveries.jsonl"
    path.write_text(
        json.dumps(
            {
                "bar_time": "2026-08-27T09:10:00+08:00",
                "status": "sent",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert not _quiet_status_delivery_due(
        path,
        bar_time=datetime(2026, 8, 27, 9, 17, tzinfo=TAIPEI),
        interval_minutes=10,
    )
    assert _quiet_status_delivery_due(
        path,
        bar_time=datetime(2026, 8, 27, 9, 20, tzinfo=TAIPEI),
        interval_minutes=10,
    )


def test_only_same_causal_point_contract_errors_are_returned(tmp_path: Path) -> None:
    path = tmp_path / "validation-errors.jsonl"
    rows = [
        {"stage": "day", "bar_time": AS_OF, "error": "first invariant"},
        {"stage": "preopen", "bar_time": AS_OF, "error": "wrong stage"},
        {"stage": "day", "bar_time": "2026-08-27T09:18:00+08:00", "error": "future point"},
        {"stage": "day", "bar_time": AS_OF, "error": "second invariant"},
        {"stage": "day", "bar_time": AS_OF, "error": "first invariant"},
    ]
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )
    assert _recent_validation_errors(
        path,
        stage="day",
        bar_time=datetime.fromisoformat(AS_OF),
    ) == ["first invariant", "second invariant"]
