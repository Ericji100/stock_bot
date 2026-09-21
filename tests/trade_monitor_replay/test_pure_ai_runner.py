from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
from jsonschema import Draft202012Validator

from trade_monitor_replay.pure_ai_runner import (
    PureAIConfig,
    PureAIReplayRunner,
    PureAIRunError,
    _analysis_positions,
    _build_prompt,
    _build_pure_ai_continuation_prompt,
)
from trade_monitor_replay.pure_ai_source import DAY, load_chart_payload, load_session


SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "trade_monitor_replay"
    / "rules"
    / "course-pure-ai-v1-long-only"
    / "analysis-schema.json"
)


def _write_chart(path: Path) -> None:
    start = datetime(2026, 7, 1, 15, 0)
    moments = [start + timedelta(minutes=index) for index in range(840)]
    moments += [datetime(2026, 7, 2, 8, 45) + timedelta(minutes=index) for index in range(300)]
    candles = []
    volume = []
    ma21 = []
    ma105 = []
    for index, moment in enumerate(moments):
        timestamp = int(moment.replace(tzinfo=timezone.utc).timestamp())
        price = 45_000 + index
        candles.append(
            {"time": timestamp, "open": price, "high": price + 3, "low": price - 2, "close": price + 1}
        )
        volume.append({"time": timestamp, "value": 10 + index, "color": "#fff"})
        ma21.append({"time": timestamp, "value": price - 1})
        ma105.append({"time": timestamp, "value": price - 4})
    payload = {
        "meta": {"title": "test", "session": "全日盤", "frequency": "1m", "barCount": len(candles)},
        "candles": candles,
        "volume": volume,
        "ma21": ma21,
        "ma105": ma105,
    }
    path.write_text(
        "<script>const payload = " + json.dumps(payload, ensure_ascii=False) + ";\n</script>",
        encoding="utf-8",
    )


def _strategy(status: str = "NOT_APPLICABLE") -> dict:
    return {
        "status": status,
        "direction": "NONE",
        "relation": "INACTIVE",
        "evidence": "目前無足夠證據。",
        "missing_or_rejection": "尚未形成。",
    }


def _valid_payload(as_of: str, session_key: str, *, phase: str = "REPLAY") -> dict:
    quadrant = {
        "trend_axis": "UNCLEAR",
        "volatility_axis": "UNCLEAR",
        "current": "UNKNOWN",
        "primary_candidate": "UNKNOWN",
        "secondary_candidate": "UNKNOWN",
        "evidence": "時段剛開始，尚無可比同級波段。",
        "change_condition": "等待更多已收盤K形成可比結構。",
    }
    structure = {
        "direction": "UNKNOWN",
        "bull_anchor": None,
        "bear_anchor": None,
        "working_segment": None,
        "bull_defense": None,
        "bear_defense": None,
        "quadrant": quadrant,
        "narrative": "目前只有開盤證據。",
    }
    names = (
        "q1_momentum",
        "q2_outer_reversal",
        "q3_box_edge",
        "q4_pullback",
        "taiji_copy",
        "anchor_copy",
        "new_bull_anchor",
        "parent_anchor_next_leg",
        "dow_continuation",
        "dow_reversal",
        "left_right_takeover",
        "or_breakout_retest",
        "box_lower_edge_reversal",
        "box_breakout_retest",
        "compression_breakout",
        "centrifugal",
        "one_dragon",
        "life_death_gate",
    )
    return {
        "schema_version": "tmf-enlightenment-pure-ai-v1",
        "as_of": as_of,
        "session_key": session_key,
        "phase": phase,
        "data_quality": {"status": "COMPLETE", "issue": None},
        "market_state": {
            "background_summary": "盤前資料完整，但新時段仍須重建。",
            "large": structure,
            "small": structure,
            "control": {
                "direction": "UNKNOWN",
                "grade": "NONE",
                "grade_relation": "UNDEFINED",
                "event": "NONE",
                "handoff_trigger": "等待方向結構。",
            },
        },
        "course_state": {
            "trend_number": "UNKNOWN",
            "taiji": {
                "mode": "UNDEFINED",
                "anchor_id": None,
                "parent_segment_id": None,
                "phase": "UNDEFINED",
                "generation": None,
                "quality": "UNKNOWN",
                "comparison": "尚無父代與複製可比較。",
                "thesis_effect": "UNKNOWN",
            },
            "yizhi": {
                "stage": "NONE",
                "direction": "NONE",
                "quality": "UNKNOWN",
                "location": "尚未形成。",
                "evidence": "沒有連續異常動能。",
                "failure_condition": "不適用。",
            },
            "left_right": {
                "stage": "NONE",
                "direction": "NONE",
                "reversal_type": "NONE",
                "evidence": "尚無反轉成熟度。",
            },
            "x_flow": {
                "stage": "OPENING_EVIDENCE" if phase == "REPLAY" else "SESSION_OPEN_PENDING",
                "first_endpoint_style": "NOT_AVAILABLE",
                "primary_lens": "OPENING",
                "family_dna": "INSUFFICIENT",
                "evidence": "先等待本時段開盤資料。",
            },
            "box_state": {
                "status": "NONE",
                "lower": None,
                "upper": None,
                "quality": "UNKNOWN",
                "location": "NONE",
                "evidence": "尚無箱型。",
            },
        },
        "scenarios": {
            "primary": "UNKNOWN",
            "backup": "UNKNOWN",
            "bull": {"probability": 33, "thesis": "待證據。", "confirmation": "待突破。", "invalidation": "不適用。"},
            "range": {"probability": 34, "thesis": "待證據。", "confirmation": "待重疊。", "invalidation": "出現方向。"},
            "bear": {"probability": 33, "thesis": "待證據。", "confirmation": "待跌破。", "invalidation": "不適用。"},
        },
        "strategy_scan": {name: _strategy() for name in names},
        "primary_setup": None,
        "decision": {
            "action": "OBSERVE",
            "setup_id": None,
            "strategy": None,
            "reason": "本時段結構尚未形成。",
            "new_stop_price": None,
            "allow_reentry": False,
        },
        "audit_summary": {
            "new_evidence": ["只增加已收盤K。"],
            "state_changes": [],
            "uncertainties": ["控制級數未定。"],
            "course_basis": ["時段獨立與定錨控制意義。"],
        },
    }


class _FakeAnalyzer:
    def analyze(self, prompt: str):
        runtime_text = prompt.split("<RUNTIME_CONTEXT>\n", 1)[1].split("\n</RUNTIME_CONTEXT>", 1)[0]
        runtime = json.loads(runtime_text)
        payload = _valid_payload(runtime["as_of"], runtime["session_key"], phase=runtime["phase"])
        return SimpleNamespace(payload=payload, raw_text=json.dumps(payload), diagnostics={"provider": "fake"})


def test_schema_is_valid_and_accepts_minimal_observation() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(
        _valid_payload("2026-07-02T08:46:00+08:00", "2026-07-02:DAY")
    )


def test_chart_source_extracts_complete_day_and_prior_night(tmp_path: Path) -> None:
    path = tmp_path / "chart.html"
    _write_chart(path)
    payload = load_chart_payload(path)
    assert payload["meta"]["barCount"] == 1140
    data = load_session(path, session_date=date(2026, 7, 2), session_kind=DAY)
    assert len(data.background_bars) == 840
    assert len(data.session_bars) == 300
    assert data.session_bars.iloc[0]["bar_time"].strftime("%H:%M") == "08:45"
    assert data.session_bars.iloc[-1]["bar_time"].strftime("%H:%M") == "13:44"


def test_completed_overview_preserves_exact_extreme_times(tmp_path: Path) -> None:
    from trade_monitor_replay.pure_ai_source import completed_overview

    path = tmp_path / "chart.html"
    _write_chart(path)
    data = load_session(path, session_date=date(2026, 7, 2), session_kind=DAY)
    overview = completed_overview(data.session_bars.head(15))
    indexes = {name: index for index, name in enumerate(overview["columns"])}
    row = overview["rows"][0]
    source = data.session_bars.head(15)
    expected_high = source.loc[source["high"].idxmax()]
    expected_low = source.loc[source["low"].idxmin()]
    assert row[indexes["high_time"]] == expected_high["bar_time"].isoformat()
    assert row[indexes["low_time"]] == expected_low["bar_time"].isoformat()
    assert row[indexes["close_time"]] == source.iloc[-1]["bar_time"].isoformat()


def test_analysis_cadence_is_anchored_to_session_start_and_includes_close() -> None:
    positions = _analysis_positions(300, 2)
    assert min(positions) == 1
    assert 299 in positions
    assert len(positions) == 150
    night = _analysis_positions(210, 2)
    assert min(night) == 1
    assert 209 in night
    assert len(night) == 105


def test_persistent_continuation_keeps_only_causal_delta() -> None:
    previous = _valid_payload("2026-07-02T08:46:00+08:00", "2026-07-02:DAY")
    rows = [
        [f"2026-07-02T08:{minute:02d}:00+08:00", 100, 110, 90, 105, 1, None, None, None]
        for minute in range(45, 51)
    ]
    runtime = {
        "phase": "REPLAY",
        "as_of": "2026-07-02T08:50:00+08:00",
        "session_key": "2026-07-02:DAY",
        "session_kind": "DAY",
        "causal_rule": "No future bars",
        "background": {"facts": {"latest_close": 99}, "preopen_ai_snapshot": {"large": "FULL"}},
        "current_session": {
            "facts": {"latest_close": 105},
            "completed_15m": {"columns": [], "rows": []},
            "recent_1m": {"columns": ["time", "open", "high", "low", "close"], "rows": rows},
        },
        "previous_validated_analysis": previous,
        "position": {"status": "FLAT"},
        "analysis_every_bars": 2,
    }
    result = _build_pure_ai_continuation_prompt(_build_prompt("IMMUTABLE CONTRACT", runtime))
    compact_text = result.split("<RUNTIME_CONTEXT>\n", 1)[1].split("\n</RUNTIME_CONTEXT>", 1)[0]
    compact = json.loads(compact_text)
    assert "IMMUTABLE CONTRACT" not in result
    assert "preopen_ai_snapshot" not in result
    assert "market_state" not in result
    assert compact["as_of"] == "2026-07-02T08:50:00+08:00"
    assert len(compact["current_session"]["new_and_overlap_1m"]["rows"]) == 6


def test_point_validator_rejects_future_reference() -> None:
    runner = object.__new__(PureAIReplayRunner)
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    runner.validator = Draft202012Validator(schema)
    payload = _valid_payload("2026-07-02T08:46:00+08:00", "2026-07-02:DAY")
    payload["market_state"]["small"]["working_segment"] = {
        "segment_id": "future",
        "direction": "BULL",
        "kind": "IMPULSE",
        "status": "FORMING",
        "start": {"time": "2026-07-02T08:45:00+08:00", "role": "LOW", "price": 100},
        "end": {"time": "2026-07-02T08:47:00+08:00", "role": "HIGH", "price": 110},
        "relation_to_anchor": "測試",
    }
    runtime = {
        "phase": "REPLAY",
        "as_of": "2026-07-02T08:46:00+08:00",
        "session_key": "2026-07-02:DAY",
        "current_session": {
            "facts": {"latest_close": 105},
            "recent_1m": {
                "rows": [
                    ["2026-07-02T08:45:00+08:00", 101, 108, 100, 105, 1, None, None, None],
                    ["2026-07-02T08:46:00+08:00", 105, 109, 104, 108, 1, None, None, None],
                ]
            },
        },
        "background": {"preopen_ai_snapshot": {}, "facts": {}},
        "previous_validated_analysis": {},
        "position": {"status": "FLAT"},
    }
    with pytest.raises(PureAIRunError, match="未來點位"):
        runner._validate_output(payload, runtime)


def test_fake_analyzer_run_pauses_without_telegram(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    chart = tmp_path / "chart.html"
    _write_chart(chart)
    contract = tmp_path / "contract.md"
    contract.write_text("固定課程契約", encoding="utf-8")
    schema = tmp_path / "schema.json"
    schema.write_text(SCHEMA_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    contract_hash = __import__("hashlib").sha256(contract.read_bytes()).hexdigest()
    schema_hash = __import__("hashlib").sha256(schema.read_bytes()).hexdigest()
    source_hash = __import__("hashlib").sha256(chart.read_bytes()).hexdigest()
    plan = tmp_path / "plan.json"
    sessions = [
        {
            "session_key": "2026-07-02:DAY",
            "date": "2026-07-02",
            "session_kind": "DAY",
            "source_month": "2026-07",
            "source_sha256": source_hash,
            "classification": "DEVELOPMENT",
        }
    ]
    plan.write_text(
        json.dumps(
            {
                "contract_sha256": contract_hash,
                "schema_sha256": schema_hash,
                "sessions": sessions,
            }
        ),
        encoding="utf-8",
    )
    config = PureAIConfig(
        model="fake",
        reasoning_effort="medium",
        timeout_seconds=1,
        analysis_every_bars=2,
        current_bar_tail=180,
        background_bar_tail=180,
        contract_path=contract,
        schema_path=schema,
        rule_manifest_path=plan,
        cohort_plan_path=plan,
        source_paths={"2026-07": chart},
        point_value_ntd=10,
        round_trip_cost_ntd=50,
        slippage_scenarios_points_per_side=(0, 1, 2),
    )
    monkeypatch.setattr("trade_monitor_replay.pure_ai_runner.validate_plan", lambda _config: {"ok": True})
    runtime = tmp_path / "runs"
    runner = PureAIReplayRunner(config, analyzer=_FakeAnalyzer(), runtime_root=runtime)
    result = runner.run_session("2026-07-02:DAY", max_ai_calls=2)
    assert result["status"] == "paused"
    assert result["ai_call_count"] == 2
    run_dir = Path(result["run_directory"])
    assert (run_dir / "validated" / "preopen.json").exists()
    assert len(list((run_dir / "validated").glob("replay-*.json"))) == 1
    assert not (run_dir / "delivery-state.json").exists()
