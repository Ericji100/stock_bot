from __future__ import annotations

import asyncio
import json
from datetime import date
from pathlib import Path

import pandas as pd

from trade_monitor.market_structure_state import empty_market_structure_state
from trade_monitor_replay.config import DEFAULT_RULE_MANIFEST_PATH, ReplayConfig
from trade_monitor_replay.full_fidelity import (
    humanize_visible_analysis,
    validate_full_fidelity_payload,
)
from trade_monitor_replay.minimax_analyzer import MiniMaxReplayResult
from trade_monitor_replay.data_source import ReplayDataset
from trade_monitor_replay.rules import load_rule_package
from trade_monitor_replay.runner import ReplayRunner

ROOT = Path(__file__).parents[2]
FORMAL_SCHEMA = (
    ROOT
    / "trade_monitor"
    / "rules"
    / "versions"
    / "enlightenment-integrated-v2.1.8-anchor-origin"
    / "analysis-schema.json"
)


def _frame(times: list[str], prices: list[int]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "bar_time": pd.to_datetime(times).tz_localize("Asia/Taipei"),
            "open": prices,
            "high": [value + 2 for value in prices],
            "low": [value - 2 for value in prices],
            "close": [value + 1 for value in prices],
            "volume": [10] * len(times),
            "sma21": [None] * len(times),
            "sma105": [None] * len(times),
            "atr14": [None] * len(times),
        }
    )


def _dataset() -> ReplayDataset:
    return ReplayDataset(
        target_date=date(2026, 8, 27),
        instrument="TMF",
        expiry_month="202609",
        night_bars=_frame(["2026-08-26 15:00", "2026-08-26 15:01"], [100, 101]),
        day_bars=_frame(["2026-08-27 08:45", "2026-08-27 08:46"], [102, 103]),
        source_sha256={"2026-08-26": "a" * 64, "2026-08-27": "b" * 64},
    )


def _full_payload(expected: str, session_key: str, *, preopen: bool) -> dict:
    return {
        "original_decision": "NOTIFY" if preopen else "DONT_NOTIFY",
        "notification_reason": "盤前完整快照" if preopen else "條件不變，繼續觀望",
        "latest_closed_k_price_estimate": "約103點",
        "latest_closed_k_details": ["已使用歷史結構化一分K。"],
        "large_trend": {"classification": "盤整", "details": ["大級結構尚未確認方向。"]},
        "current_trend": {"classification": "盤整", "details": ["等待已收盤K完成有效突破。"]},
        "market_state": ["歷史資料依時間逐根揭露。"],
        "pattern_observation": {
            "status": "觀望",
            "pattern": "尚無主控戰法",
            "details": ["條件尚未完成。"],
        },
        "missing_conditions_or_trigger": ["等待結構確認。"],
        "entry_and_structural_stop": ["目前沒有可執行進場。"],
        "risk_and_nearest_obstacle": ["尚未建立風險計畫。"],
        "single_contract_management_or_prohibition": ["維持空手。"],
        "constitution_event": {
            "event_type": "NONE",
            "event_id": f"NONE|{expected}",
            "setup_id": None,
            "position_id": None,
            "direction": None,
            "entry_price_estimate": None,
            "stop_price_estimate": None,
            "risk_points": None,
            "latest_closed_bar_time": expected,
            "reason": "本輪沒有模擬持倉狀態異動。",
        },
        "market_structure_state": empty_market_structure_state(
            as_of=expected,
            session_key=session_key,
            version=6,
        ),
    }


class FullFidelityFakeAnalyzer:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def analyze(self, prompt: str) -> MiniMaxReplayResult:
        self.prompts.append(prompt)
        expected = prompt.split('"expected_latest_closed_k_iso":"', 1)[1].split('"', 1)[0]
        session_key = prompt.split('"expected_session_key":"', 1)[1].split('"', 1)[0]
        preopen = '"preopen_snapshot":true' in prompt
        payload = _full_payload(expected, session_key, preopen=preopen)
        raw = json.dumps(payload, ensure_ascii=False)
        return MiniMaxReplayResult(
            payload=payload,
            raw_text=raw,
            diagnostics={"model": "fake", "usage": {}, "elapsed_seconds": 0.01},
        )


def test_default_full_fidelity_manifest_references_formal_schema_directly() -> None:
    manifest = json.loads(DEFAULT_RULE_MANIFEST_PATH.read_text(encoding="utf-8"))
    rules = load_rule_package(DEFAULT_RULE_MANIFEST_PATH)
    assert rules.contract_version == 8
    assert rules.memory_version == 6
    assert rules.schema_text == FORMAL_SCHEMA.read_text(encoding="utf-8")
    assert manifest["analysis_schema_path"] == manifest["replay_schema_path"]
    assert manifest["analysis_schema_sha256"] == manifest["replay_schema_sha256"]


def test_full_fidelity_runner_persists_v6_state_only_inside_replay_run(tmp_path: Path) -> None:
    fake = FullFidelityFakeAnalyzer()
    runtime_root = tmp_path / "replay-runtime"
    runner = ReplayRunner(
        ReplayConfig(telegram_enabled=False, analysis_every_bars=1),
        analyzer=fake,
        dataset_loader=lambda *_args, **_kwargs: _dataset(),
        runtime_root=runtime_root,
    )
    result = asyncio.run(runner.run(target_date=date(2026, 8, 27), max_bars=1))
    run_dir = Path(result["run_directory"])
    state = json.loads((run_dir / "market-structure-state.json").read_text(encoding="utf-8"))
    validated = json.loads(
        max((run_dir / "analysis").glob("day-*-validated.json")).read_text(encoding="utf-8")
    )
    assert result["status"] == "completed"
    assert state["version"] == 6
    assert state["session_key"] == "2026-08-27-DAY"
    assert "constitution_event" in validated
    assert "market_structure_state" in validated
    assert not (tmp_path / "trade_monitor").exists()
    assert "完整market_structure_state v6" in fake.prompts[0]
    assert "previous_cash_close、cash_open、gap_observed_at全部為null" in fake.prompts[0]
    assert "previous_replay_memory" not in fake.prompts[0]


def test_visible_machine_pivot_is_humanized_without_touching_internal_state() -> None:
    payload = {
        "market_state": [
            "kind=低、bar_time=2026-08-27T09:01:00+08:00、price=46077.0，"
            "kind=高、bar_time=2026-08-27T09:07:00+08:00、price=46417.0"
        ],
        "market_structure_state": {
            "notes": ["kind=LOW,bar_time=2026-08-27T09:01:00+08:00,price=46077.0"]
        },
    }
    result = humanize_visible_analysis(payload)
    assert result["market_state"] == ["09:01 低點 46,077點，09:07 高點 46,417點"]
    assert result["market_structure_state"] == payload["market_structure_state"]


def test_inactive_taiji_bookkeeping_timestamp_is_cleared_before_validation() -> None:
    expected = "2026-08-27T08:45:00+08:00"
    payload = _full_payload(expected, "2026-08-27-DAY", preopen=False)
    taiji = payload["market_structure_state"]["cclass_context"]["taiji_context"]
    assert taiji["active_leg_id"] is None and taiji["legs"] == []
    taiji["assessment_changed_at"] = expected

    validated, _ = validate_full_fidelity_payload(
        payload,
        previous_market_structure=None,
        expected_as_of=expected,
        expected_session_key="2026-08-27-DAY",
        preopen=False,
    )

    assert (
        validated["market_structure_state"]["cclass_context"]["taiji_context"]
        ["assessment_changed_at"]
        is None
    )


def test_resetting_taiji_clears_only_terminal_history() -> None:
    expected = "2026-08-27T08:45:00+08:00"
    payload = _full_payload(expected, "2026-08-27-DAY", preopen=False)
    cclass = payload["market_structure_state"]["cclass_context"]
    cclass["engine_mode"] = "RESETTING"
    cclass["engine_changed_at"] = expected
    cclass["order_state"] = "TRANSITION"
    taiji = cclass["taiji_context"]
    taiji.update(
        {
            "dynasty_id": "old-dynasty",
            "anchor_id": "old-anchor",
            "active_leg_id": None,
            "legs": [
                {
                    "leg_id": "old-leg",
                    "status": "COMPLETED",
                }
            ],
            "assessment_changed_at": expected,
        }
    )
    evolution = payload["market_structure_state"]["prospective_context"]["taiji_evolution"]
    evolution["copy_outcomes"] = ["STRONG"]
    evolution["correction_outcomes"] = ["ACCEPTABLE"]

    validated, _ = validate_full_fidelity_payload(
        payload,
        previous_market_structure=None,
        expected_as_of=expected,
        expected_session_key="2026-08-27-DAY",
        preopen=False,
    )
    normalized = validated["market_structure_state"]["cclass_context"]["taiji_context"]
    assert normalized["dynasty_id"] is None
    assert normalized["anchor_id"] is None
    assert normalized["legs"] == []
    assert normalized["assessment_changed_at"] is None
    normalized_evolution = validated["market_structure_state"]["prospective_context"]["taiji_evolution"]
    assert normalized_evolution["copy_outcomes"] == []
    assert normalized_evolution["correction_outcomes"] == []


def test_inactive_setup_bookkeeping_timestamp_is_cleared_before_validation() -> None:
    expected = "2026-08-27T08:45:00+08:00"
    payload = _full_payload(expected, "2026-08-27-DAY", preopen=False)
    setup = payload["market_structure_state"]["scenario_context"]["setup"]
    assert setup["stage"] == "NONE" and setup["setup_id"] is None
    setup["stage_changed_at"] = expected

    validated, _ = validate_full_fidelity_payload(
        payload,
        previous_market_structure=None,
        expected_as_of=expected,
        expected_session_key="2026-08-27-DAY",
        preopen=False,
    )

    assert (
        validated["market_structure_state"]["scenario_context"]["setup"]
        ["stage_changed_at"]
        is None
    )


def test_non_gate_momentum_clears_irrelevant_gate_balance() -> None:
    expected = "2026-08-27T09:00:00+08:00"
    payload = _full_payload(expected, "2026-08-27-DAY", preopen=False)
    cclass = payload["market_structure_state"]["cclass_context"]
    cclass["engine_mode"] = "YIZHI_MOMENTUM"
    cclass["engine_changed_at"] = expected
    cclass["order_state"] = "TRANSITION"
    momentum = cclass["momentum_context"]
    momentum.update(
        {
            "episode_id": "episode-1",
            "stage": "EXHAUSTION_WARNING",
            "direction": "BEAR",
            "first_seen_at": expected,
            "stage_changed_at": expected,
            "gate_balance": "REVERSAL_STRONGER",
        }
    )

    validated, _ = validate_full_fidelity_payload(
        payload,
        previous_market_structure=None,
        expected_as_of=expected,
        expected_session_key="2026-08-27-DAY",
        preopen=False,
    )
    assert (
        validated["market_structure_state"]["cclass_context"]["momentum_context"]
        ["gate_balance"]
        == "NOT_APPLICABLE"
    )


def test_inactive_momentum_bookkeeping_timestamp_is_cleared() -> None:
    expected = "2026-08-27T09:25:00+08:00"
    payload = _full_payload(expected, "2026-08-27-DAY", preopen=False)
    momentum = payload["market_structure_state"]["cclass_context"]["momentum_context"]
    assert momentum["stage"] == "NONE" and momentum["episode_id"] is None
    momentum["stage_changed_at"] = expected

    validated, _ = validate_full_fidelity_payload(
        payload,
        previous_market_structure=None,
        expected_as_of=expected,
        expected_session_key="2026-08-27-DAY",
        preopen=False,
    )
    assert (
        validated["market_structure_state"]["cclass_context"]["momentum_context"]
        ["stage_changed_at"]
        is None
    )
