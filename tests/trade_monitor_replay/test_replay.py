from __future__ import annotations

import json
import asyncio
import zipfile
from datetime import date, datetime
from io import BytesIO
from pathlib import Path

import pandas as pd
import pytest

from stock_ai_bot.telegram.telegram_push_service import TelegramPushResult
from trade_monitor_replay.config import ReplayConfig, ReplayConfigError, load_replay_config
from trade_monitor_replay.codex_analyzer import (
    CodexReplayAnalyzer,
    _command_failure_text,
    _compact_continuation_runtime,
)
from trade_monitor_replay.contract import empty_replay_memory
from trade_monitor_replay.data_source import (
    ReplayDataset,
    _aggregate_one_minute,
    compact_bar_table,
    compact_completed_overview,
    completed_overview,
    load_replay_dataset,
)
from trade_monitor_replay.minimax_analyzer import (
    MiniMaxReplayAnalyzer,
    MiniMaxReplayError,
    MiniMaxReplayResult,
    build_replay_prompt,
    parse_json_object,
    normalize_tool_arguments,
    replay_rule_text,
)
from trade_monitor_replay.notifier import ReplayNotifier
from trade_monitor_replay.runner import (
    ReplayRunError,
    ReplayRunner,
    _apply_replay_delivery_policy,
    _deterministic_constraints,
    _select_day_bars,
    _select_day_bars_at_times,
    _validate_grounded_level_claims,
    _normalize_session_open_role_labels,
    _validate_replay_semantics,
)
from trade_monitor_replay.state import create_run_directory
from trade_monitor_replay.twse_spot_source import (
    DAILY_URL,
    load_twse_spot_replay_data,
    spot_context,
)


TAIPEI = "Asia/Taipei"
ROOT = Path(__file__).parents[2]
LEGACY_RULE_MANIFEST = (
    ROOT
    / "trade_monitor_replay"
    / "rules"
    / "baseline-v2.1.8-replay-v2"
    / "rule-manifest.json"
)


def _legacy_config(**overrides) -> ReplayConfig:
    return ReplayConfig(rule_manifest_path=LEGACY_RULE_MANIFEST, **overrides)


def _zip(rows: list[tuple[str, str, str, str, str, str]]) -> bytes:
    csv = "日期,商品,到期,時間,價格,成交量\n" + "\n".join(",".join(row) for row in rows) + "\n"
    output = BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("Daily.csv", csv.encode("cp950"))
    return output.getvalue()


def _frame(times: list[str], prices: list[int]) -> pd.DataFrame:
    frame = pd.DataFrame(
        {
            "bar_time": pd.to_datetime(times).tz_localize(TAIPEI),
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
    return frame


def _dataset() -> ReplayDataset:
    return ReplayDataset(
        target_date=date(2026, 8, 27),
        instrument="TMF",
        expiry_month="202609",
        night_bars=_frame(["2026-08-26 15:00", "2026-08-26 15:01"], [100, 101]),
        day_bars=_frame(["2026-08-27 08:45", "2026-08-27 08:46"], [102, 99999]),
        source_sha256={"2026-08-26": "a" * 64, "2026-08-27": "b" * 64},
    )


def _analysis_payload(expected: str, *, preopen: bool) -> dict:
    session = "NIGHT" if preopen else "DAY"
    memory = empty_replay_memory(
        as_of=expected,
        session_key=f"2026-08-27:{session}",
    )
    analysis = {
        "original_decision": "NOTIFY" if preopen else "DONT_NOTIFY",
        "notification_reason": "盤前快照" if preopen else "條件不變，繼續觀望",
        "latest_closed_k_price_estimate": "約103點",
        "latest_closed_k_details": ["已使用歷史結構化一分K。"],
        "large_trend": {"classification": "盤整", "details": ["方向尚未確認。"]},
        "current_trend": {"classification": "盤整", "details": ["等待有效突破。"]},
        "market_state": ["歷史回放狀態正常。"],
        "pattern_observation": {"status": "觀望", "pattern": "尚無主控戰法", "details": ["條件未完成。"]},
        "missing_conditions_or_trigger": ["等待已收盤K確認。"],
        "entry_and_structural_stop": ["目前不提供進場。"],
        "risk_and_nearest_obstacle": ["沒有可執行風險計畫。"],
        "single_contract_management_or_prohibition": ["維持空手觀察。"],
    }
    return {"analysis": analysis, "memory": memory}


class FakeAnalyzer:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def analyze(self, prompt: str) -> MiniMaxReplayResult:
        self.prompts.append(prompt)
        marker = '"expected_latest_closed_k_iso":"'
        expected = prompt.split(marker, 1)[1].split('"', 1)[0]
        preopen = '"preopen_snapshot":true' in prompt
        payload = _analysis_payload(expected, preopen=preopen)
        raw = json.dumps(payload, ensure_ascii=False)
        return MiniMaxReplayResult(
            payload=payload,
            raw_text=raw,
            diagnostics={"model": "fake", "usage": {}, "elapsed_seconds": 0.01},
        )


def test_replay_window_does_not_shift_session_anchored_two_bar_cadence() -> None:
    frame = _frame(
        [f"2026-08-27 08:{minute:02d}" for minute in range(45, 53)],
        list(range(100, 108)),
    )

    selected = _select_day_bars(
        frame,
        start_time=datetime.strptime("08:48", "%H:%M").time(),
        end_time=datetime.strptime("08:52", "%H:%M").time(),
        max_bars=None,
        analysis_every_bars=2,
    )

    assert [value.strftime("%H:%M") for value in selected["bar_time"]] == [
        "08:48",
        "08:50",
        "08:52",
    ]


def test_data_source_uses_previous_night_and_merges_exact_close() -> None:
    target = _zip(
        [
            ("20260826", "TMF", "202609", "150000", "100", "1"),
            ("20260826", "TMF", "202609", "150059", "105", "2"),
            ("20260826", "TMF", "202610", "150000", "999", "1"),
            ("20260827", "TMF", "202609", "045959", "103", "1"),
            ("20260827", "TMF", "202609", "084500", "110", "1"),
            ("20260827", "TMF", "202609", "084559", "112", "2"),
            ("20260827", "TMF", "202609", "134459", "120", "1"),
            ("20260827", "TMF", "202609", "134500", "121", "2"),
            ("20260827", "TMF", "202610", "084500", "999", "1"),
        ]
    )
    by_date = {date(2026, 8, 27): target}
    result = load_replay_dataset(date(2026, 8, 27), zip_loader=by_date.get, spot_loader=None)
    assert result.expiry_month == "202609"
    assert result.night_bars.iloc[0]["bar_time"].isoformat() == "2026-08-26T15:00:00+08:00"
    assert result.night_bars.iloc[-1]["bar_time"].isoformat() == "2026-08-27T04:59:00+08:00"
    assert result.day_bars.iloc[0]["bar_time"].isoformat() == "2026-08-27T08:45:00+08:00"
    assert result.day_bars.iloc[-1]["bar_time"].isoformat() == "2026-08-27T13:44:00+08:00"
    assert result.day_bars.iloc[-1]["close"] == 121
    assert result.day_bars.iloc[-1]["volume"] == 3


def test_data_source_uses_trading_date_zip_for_monday_night_session() -> None:
    monday_file = _zip(
        [
            ("20260821", "TMF", "202609", "150000", "100", "1"),
            ("20260822", "TMF", "202609", "045959", "105", "2"),
            ("20260824", "TMF", "202609", "084500", "110", "1"),
            ("20260824", "TMF", "202609", "134500", "115", "1"),
        ]
    )
    requested: list[date] = []

    def loader(calendar_date: date) -> bytes | None:
        requested.append(calendar_date)
        return monday_file if calendar_date == date(2026, 8, 24) else None

    result = load_replay_dataset(date(2026, 8, 24), zip_loader=loader, spot_loader=None)

    assert requested == [date(2026, 8, 24)]
    assert result.night_bars.iloc[0]["bar_time"].isoformat() == "2026-08-21T15:00:00+08:00"
    assert result.night_bars.iloc[-1]["bar_time"].isoformat() == "2026-08-22T04:59:00+08:00"
    assert result.day_bars.iloc[0]["bar_time"].isoformat() == "2026-08-24T08:45:00+08:00"
    assert result.day_bars.iloc[-1]["bar_time"].isoformat() == "2026-08-24T13:44:00+08:00"
    assert set(result.source_sha256) == {"2026-08-24"}


def test_one_minute_aggregation_fills_only_isolated_no_trade_minute() -> None:
    ticks = pd.DataFrame(
        {
            "actual_datetime": pd.to_datetime(
                [
                    "2026-08-13 02:52:10",
                    "2026-08-13 02:52:50",
                    "2026-08-13 02:54:05",
                    "2026-08-13 02:57:05",
                ]
            ),
            "price": [100, 102, 103, 104],
            "volume": [1, 2, 1, 1],
        }
    )

    result = _aggregate_one_minute(
        ticks,
        session_end=datetime.fromisoformat("2026-08-13T05:00:00"),
    )

    assert [value.strftime("%H:%M") for value in result["bar_time"]] == [
        "02:52",
        "02:53",
        "02:54",
        "02:57",
    ]
    synthetic = result[result["bar_time"].dt.strftime("%H:%M") == "02:53"].iloc[0]
    assert synthetic[["open", "high", "low", "close"]].tolist() == [102, 102, 102, 102]
    assert synthetic["volume"] == 0


def test_completed_overview_excludes_partial_bar() -> None:
    frame = _frame(
        [f"2026-08-27 08:{minute:02d}" for minute in range(45, 60)],
        list(range(100, 115)),
    )
    before = completed_overview(frame.head(14), minutes=15, available_at=pd.Timestamp("2026-08-27 08:59", tz=TAIPEI).to_pydatetime())
    after = completed_overview(frame, minutes=15, available_at=pd.Timestamp("2026-08-27 09:00", tz=TAIPEI).to_pydatetime())
    assert before == []
    assert len(after) == 1


def test_exact_analysis_times_are_sorted_deduplicated_and_must_exist() -> None:
    frame = _frame(
        ["2026-08-27 08:45", "2026-08-27 08:46", "2026-08-27 08:47"],
        [100, 101, 102],
    )
    selected = _select_day_bars_at_times(
        frame,
        [datetime.strptime("08:47", "%H:%M").time(), datetime.strptime("08:45", "%H:%M").time(), datetime.strptime("08:47", "%H:%M").time()],
    )
    assert [value.strftime("%H:%M") for value in selected["bar_time"]] == ["08:45", "08:47"]
    with pytest.raises(ReplayRunError, match="08:48"):
        _select_day_bars_at_times(frame, [datetime.strptime("08:48", "%H:%M").time()])


def test_compact_tables_preserve_all_bar_values_without_repeated_keys() -> None:
    frame = _frame(
        ["2026-08-27 08:45", "2026-08-27 08:46"],
        [100, 110],
    )
    detail = compact_bar_table(frame)
    assert detail["columns"] == ["time", "open", "high", "low", "close", "volume", "sma21", "sma105", "atr14"]
    assert detail["rows"][0] == ["2026-08-27T08:45:00+08:00", 100, 102, 98, 101, 10, None, None, None]

    overview_frame = _frame(
        [f"2026-08-27 08:{minute:02d}" for minute in range(45, 60)],
        list(range(100, 115)),
    )
    overview = compact_completed_overview(
        overview_frame,
        minutes=15,
        available_at=pd.Timestamp("2026-08-27 09:00", tz=TAIPEI).to_pydatetime(),
    )
    assert overview["columns"] == ["time", "open", "high", "low", "close", "volume"]
    assert overview["rows"] == [["2026-08-27T08:45:00+08:00", 100, 116, 98, 115, 150]]


def test_twse_spot_source_excludes_previous_close_tick_and_is_causal() -> None:
    daily = {
        "stat": "OK",
        "data": [
            ["115/08/26", "99", "101", "98", "100"],
            ["115/08/27", "102", "106", "101", "104"],
        ],
    }
    intraday = {
        "stat": "OK",
        "fields": ["時間", "發行量加權股價指數"],
        "data": [
            ["09:00:00", "100"],
            ["09:00:05", "102"],
            ["09:00:55", "103"],
            ["09:01:00", "101"],
            ["09:01:55", "106"],
            ["13:30:00", "104"],
        ],
    }

    def loader(url: str, params: dict[str, str]):
        payload = daily if url == DAILY_URL else intraday
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return payload, raw

    data = load_twse_spot_replay_data(date(2026, 8, 27), json_loader=loader)
    assert data.previous_close == 100
    assert data.bars.iloc[0]["open"] == 102
    assert data.bars.iloc[-1]["bar_time"].strftime("%H:%M") == "13:29"
    preopen = spot_context(data, latest_futures_bar=pd.Timestamp("2026-08-27 08:59", tz=TAIPEI).to_pydatetime())
    first = spot_context(data, latest_futures_bar=pd.Timestamp("2026-08-27 09:00", tz=TAIPEI).to_pydatetime())
    assert preopen["status"] == "PREOPEN"
    assert preopen["current_session"] is None
    assert first["status"] == "FRESH"
    assert len(first["bars"]) == 1
    assert first["current_session"]["open"] == 102


def test_json_parser_accepts_object_and_ignores_non_executable_tail() -> None:
    assert parse_json_object('```json\n{"ok": true}\n```') == {"ok": True}
    assert parse_json_object('{"ok": true}\nextra') == {"ok": True}
    normalized, stats = normalize_tool_arguments(
        {
            "details": {"item": ["a", "b"]},
            "kept": {"item": "text"},
            "memory": {"position": {"entry_time": "null", "entry_price": "null", "stop_price": "null"}},
        }
    )
    assert normalized["details"] == ["a", "b"]
    assert normalized["kept"] == {"item": "text"}
    assert normalized["memory"]["position"] == {"entry_time": None, "entry_price": None, "stop_price": None}
    assert stats == {
        "array_wrappers": 1,
        "null_fields": 3,
        "trailing_serialization_suffixes": 0,
    }


def test_tool_argument_normalizer_repairs_only_safe_complete_sentence_suffix() -> None:
    normalized, stats = normalize_tool_arguments(
        {
            "safe": "曾收回後已再站上；再度收回則複製降溫。},{",
            "unfinished": "正常說明。陶},{",
            "embedded": "前段。},{後段",
        }
    )

    assert normalized["safe"] == "曾收回後已再站上；再度收回則複製降溫。"
    assert normalized["unfinished"] == "正常說明。陶},{"
    assert normalized["embedded"] == "前段。},{後段"
    assert stats == {
        "array_wrappers": 0,
        "null_fields": 0,
        "trailing_serialization_suffixes": 1,
    }


def test_grounded_level_validator_rejects_wrong_named_night_low() -> None:
    with pytest.raises(ReplayRunError, match="夜盤低"):
        _validate_grounded_level_claims(
            {"text": "停損參考45670（夜盤低）"},
            structured={"causal_structure_n2": {}},
            night_summary={"high": 46139, "low": 45770, "close": 45995},
        )


def test_grounded_level_validator_does_not_confuse_preclose_phrase_with_close_price() -> None:
    _validate_grounded_level_claims(
        {"text": "夜盤收盤前形成更高低點45814；夜盤收盤45995。"},
        structured={"causal_structure_n2": {}},
        night_summary={"high": 46139, "low": 45770, "close": 45995},
    )


def test_grounded_pivot_validator_uses_direct_time_label_price_claims_only() -> None:
    structured = {
        "causal_structure_n2": {
            "recent_confirmed_pivots": [
                {"bar_time": "2026-08-27T04:50:00+08:00", "kind": "HIGH", "price": 45963},
                {"bar_time": "2026-08-27T04:43:00+08:00", "kind": "LOW", "price": 45875},
            ]
        }
    }
    _validate_grounded_level_claims(
        {
            "text": [
                "04:50所形成45963高點、接近45996壓力。",
                "04:50所形成小級高點45963。",
                "04:22深跌後新高，逼近45996。",
                "15:00開盤45987後衝至16:42高點46139。",
            ]
        },
        structured=structured,
        night_summary={"high": 46139, "low": 45770, "close": 45995},
    )
    with pytest.raises(ReplayRunError, match="因果樞紐"):
        _validate_grounded_level_claims(
            {"text": "04:50小級高點45996。"},
            structured=structured,
            night_summary={"high": 46139, "low": 45770, "close": 45995},
        )


def test_grounded_bar_level_validator_rejects_wrong_ohlc_role() -> None:
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
    _validate_grounded_level_claims(
        {
            "text": [
                "父代起點為08:45開盤價45,045，終點為08:45高點45,117。",
                "現貨08:45收盤45,186.03。",
            ]
        },
        structured={"causal_structure_n2": {}},
        night_summary={"high": 45475, "low": 44983, "close": 45069},
        ledger=ledger,
    )
    with pytest.raises(ReplayRunError, match="開高低收角色"):
        _validate_grounded_level_claims(
            {"text": "父代為08:45低點45,045至08:45高點45,117。"},
            structured={"causal_structure_n2": {}},
            night_summary={"high": 45475, "low": 44983, "close": 45069},
            ledger=ledger,
        )


def test_session_open_role_normalizer_changes_only_proven_wrong_role() -> None:
    ledger = {
        "recent_bar_levels": [
            {
                "time": "2026-08-24T08:45:00+08:00",
                "open": 45045,
                "high": 45117,
                "low": 45030,
                "close": 45100,
            }
        ],
        "anchor_records": [
            {
                "anchor_origin_kind": "SESSION_OPEN",
                "origin_time": "2026-08-24T08:45:00+08:00",
                "origin_price": 45045,
            }
        ],
    }
    payload = {
        "wrong": "父代為08:45低點45,045至09:01高點45,234。",
        "right": "真正低點是08:45低點45,030，高點是08:45高點45,117。",
    }

    count = _normalize_session_open_role_labels(payload, ledger=ledger)

    assert count == 1
    assert payload["wrong"] == "父代為08:45開盤價45,045至09:01高點45,234。"
    assert payload["right"] == "真正低點是08:45低點45,030，高點是08:45高點45,117。"


def test_preopen_delivery_policy_promotes_snapshot_without_changing_analysis() -> None:
    payload = {
        "original_decision": "DONT_NOTIFY",
        "notification_reason": "條件不變。",
        "large_trend": {"classification": "盤整", "details": ["方向未定。"]},
    }
    promoted = _apply_replay_delivery_policy(payload, stage="preopen")
    assert promoted is True
    assert payload["original_decision"] == "NOTIFY"
    assert payload["notification_reason"].startswith("歷史回放開始")
    assert payload["large_trend"] == {"classification": "盤整", "details": ["方向未定。"]}


def test_preopen_semantics_enforce_closed_session_and_authoritative_structure() -> None:
    expected = "2026-08-27T04:59:00+08:00"
    envelope = _analysis_payload(expected, preopen=True)
    structured = {"causal_structure_n2": {"dow_small": "BULL", "dow_large": "TRANSITION"}}
    envelope["analysis"]["current_trend"]["classification"] = "偏多"
    envelope["memory"]["small_structure"]["direction"] = "BULL"
    envelope["memory"]["large_structure"]["direction"] = "RANGE"
    _validate_replay_semantics(envelope, structured=structured, stage="preopen")
    constraints = _deterministic_constraints(structured, stage="preopen")
    assert constraints["next_valid_futures_bar"] == "08:45"
    assert constraints["allowed_large_trend"] == ["盤整"]

    envelope["analysis"]["market_state"] = ["下一根05:00仍是未收盤K。"]
    with pytest.raises(ReplayRunError, match="05:00"):
        _validate_replay_semantics(envelope, structured=structured, stage="preopen")


def test_preopen_semantics_rejects_directional_large_trend_and_active_setup() -> None:
    expected = "2026-08-27T04:59:00+08:00"
    envelope = _analysis_payload(expected, preopen=True)
    structured = {"causal_structure_n2": {"dow_small": "TRANSITION", "dow_large": "TRANSITION"}}
    envelope["analysis"]["current_trend"]["classification"] = "轉換中"
    envelope["analysis"]["large_trend"]["classification"] = "偏多但回檔"
    with pytest.raises(ReplayRunError, match="大趨勢"):
        _validate_replay_semantics(envelope, structured=structured, stage="preopen")


def test_semantics_allows_conservative_current_trend_but_rejects_opposite_direction() -> None:
    expected = "2026-08-27T08:46:00+08:00"
    envelope = _analysis_payload(expected, preopen=False)
    structured = {"causal_structure_n2": {"dow_small": "BULL", "dow_large": "TRANSITION"}}
    envelope["memory"]["small_structure"]["direction"] = "BULL"
    envelope["analysis"]["current_trend"]["classification"] = "轉換中"
    _validate_replay_semantics(envelope, structured=structured, stage="day")

    envelope["analysis"]["current_trend"]["classification"] = "偏空"
    with pytest.raises(ReplayRunError, match="當前趨勢"):
        _validate_replay_semantics(envelope, structured=structured, stage="day")


def test_semantics_rejects_directionally_inverted_thesis_flip() -> None:
    expected = "2026-08-27T08:46:00+08:00"
    envelope = _analysis_payload(expected, preopen=False)
    structured = {"causal_structure_n2": {"dow_small": "TRANSITION", "dow_large": "TRANSITION"}}
    envelope["memory"]["thesis"]["flip"] = "若正式突破夜盤高46139，工作看法翻空。"
    with pytest.raises(ReplayRunError, match="向上突破"):
        _validate_replay_semantics(envelope, structured=structured, stage="day")

    envelope["memory"]["thesis"]["flip"] = "若突破夜盤高46139後無法守住並跌回，工作看法翻空。"
    _validate_replay_semantics(envelope, structured=structured, stage="day")


def test_minimax_analyzer_requires_structured_function_call() -> None:
    captured: dict = {}

    def fake_post(url: str, headers: dict, payload: dict, timeout: float) -> dict:
        captured.update(payload)
        return {
            "model": "MiniMax-M3",
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "content": "",
                        "tool_calls": [
                            {
                                "type": "function",
                                "function": {
                                    "name": "submit_replay_analysis",
                                    "arguments": '{"ok":true}',
                                },
                            }
                        ],
                    },
                }
            ],
            "usage": {},
        }

    analyzer = MiniMaxReplayAnalyzer(
        api_key="test-key",
        model="MiniMax-M3",
        base_url="https://example.invalid/v1",
        timeout_seconds=30,
        max_output_tokens=100,
        output_schema={"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}},
        post_json=fake_post,
    )
    result = analyzer.analyze("PROMPT")
    assert result.payload == {"ok": True}
    assert result.diagnostics["used_function_call"] is True
    assert captured["tools"][0]["function"]["name"] == "submit_replay_analysis"
    assert captured["tool_choice"] == {
        "type": "function",
        "function": {"name": "submit_replay_analysis"},
    }


def test_codex_analyzer_uses_isolated_structured_cli(tmp_path: Path) -> None:
    captured: dict = {}

    def fake_run(command: list[str], **kwargs) -> object:
        captured["command"] = command
        captured["input"] = kwargs["input"]
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text('{"ok":true}', encoding="utf-8")
        return type(
            "Completed",
            (),
            {
                "returncode": 0,
                "stdout": '{"type":"thread.started","thread_id":"test-thread"}\n'
                '{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":2}}\n',
                "stderr": "",
            },
        )()

    analyzer = CodexReplayAnalyzer(
        model="gpt-5.6-sol",
        reasoning_effort="medium",
        timeout_seconds=30,
        output_schema={"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}},
        run_command=fake_run,
    )
    result = analyzer.analyze("PROMPT")
    assert result.payload == {"ok": True}
    assert result.diagnostics["usage"]["input_tokens"] == 10
    assert "--ephemeral" in captured["command"]
    assert "--ignore-user-config" in captured["command"]
    assert "--sandbox" in captured["command"]
    assert "所有分析內容使用繁體中文" in captured["input"]


def test_codex_analyzer_reuses_one_causal_session_with_incremental_prompt(tmp_path: Path) -> None:
    calls: list[dict[str, object]] = []

    def fake_run(command: list[str], **kwargs) -> object:
        calls.append({"command": command, "input": kwargs["input"]})
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text('{"ok":true}', encoding="utf-8")
        return type(
            "Completed",
            (),
            {
                "returncode": 0,
                "stdout": '{"type":"thread.started","thread_id":"causal-thread"}\n'
                '{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":2}}\n',
                "stderr": "",
            },
        )()

    analyzer = CodexReplayAnalyzer(
        model="gpt-5.6-sol",
        reasoning_effort="medium",
        timeout_seconds=30,
        output_schema={"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}},
        run_command=fake_run,
        persistent_session=True,
    )
    analyzer.bind_run(tmp_path)
    full = (
        "<MONITOR_RULES>LOCKED RULES</MONITOR_RULES>\n"
        '<RUNTIME_CONTEXT>{"bar":"08:46"}</RUNTIME_CONTEXT>'
    )
    first = analyzer.analyze(full)
    second = analyzer.analyze(full.replace("08:46", "08:48"))

    assert "--ephemeral" not in calls[0]["command"]
    assert "resume" in calls[1]["command"]
    assert "causal-thread" in calls[1]["command"]
    assert "LOCKED RULES" in calls[0]["input"]
    assert "LOCKED RULES" not in calls[1]["input"]
    assert '"bar":"08:48"' in calls[1]["input"]
    assert first.diagnostics["session_mode"] == "new"
    assert second.diagnostics["session_mode"] == "resumed"
    assert analyzer.session_turn_count == 2


def test_codex_continuation_sends_only_causal_tail_and_current_ledger() -> None:
    runtime = {
        "replay": {"newly_revealed_bar_count": 2, "detail_bar_count": 90},
        "detail_bars": {"columns": ["time", "close"], "rows": [[f"08:{i:02d}", i] for i in range(20)]},
        "overview_bars": {"columns": ["time", "close"], "rows": [[f"{i:02d}:00", i] for i in range(8)]},
        "spot_market_context": {"status": "FRESH", "bars": [{"time": i} for i in range(12)]},
        "deterministic_evidence_ledger": {
            "as_of": "2026-08-11T09:04:00+08:00",
            "latest_closed_k": {"close": 100},
            "anchor_lifecycle": {"child_anchor": {"id": "A-1"}},
            "reference_anchor_lifecycle": {"immutable": "already sent"},
            "reference_anchor_records": [{"id": "old"}],
            "trade_levels": {"continuation_arm_candidate": None},
            "position_behavior_audit": {"checkpoint_price": 102.5},
        },
        "previous_semantic_memory": {"as_of": "2026-08-11T09:02:00+08:00"},
    }

    compact_text, mode = _compact_continuation_runtime(
        json.dumps(runtime, ensure_ascii=False, separators=(",", ":"))
    )
    compact = json.loads(compact_text)

    assert mode == "PERSISTENT_CAUSAL_DELTA"
    assert len(compact["detail_bars"]["rows"]) == 6
    assert compact["detail_bars"]["rows"][0][0] == "08:14"
    assert len(compact["overview_bars"]["rows"]) == 2
    assert len(compact["spot_market_context"]["bars"]) == 6
    assert compact["deterministic_evidence_ledger"]["anchor_lifecycle"]["child_anchor"]["id"] == "A-1"
    assert compact["deterministic_evidence_ledger"]["position_behavior_audit"]["checkpoint_price"] == 102.5
    assert "reference_anchor_lifecycle" not in compact["deterministic_evidence_ledger"]
    assert "reference_anchor_records" not in compact["deterministic_evidence_ledger"]
    assert compact["previous_semantic_memory"]["as_of"].endswith("09:02:00+08:00")


def test_codex_session_rotates_before_context_can_grow_without_bound(tmp_path: Path) -> None:
    commands: list[list[str]] = []

    def fake_run(command: list[str], **kwargs) -> object:
        commands.append(command)
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text('{"ok":true}', encoding="utf-8")
        thread_id = f"thread-{len(commands)}"
        return type(
            "Completed",
            (),
            {
                "returncode": 0,
                "stdout": f'{{"type":"thread.started","thread_id":"{thread_id}"}}\n',
                "stderr": "",
            },
        )()

    analyzer = CodexReplayAnalyzer(
        model="gpt-5.6-sol",
        reasoning_effort="medium",
        timeout_seconds=30,
        output_schema={"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}},
        run_command=fake_run,
        persistent_session=True,
        max_session_turns=2,
    )
    analyzer.bind_run(tmp_path)
    prompt = '<MONITOR_RULES>R</MONITOR_RULES><RUNTIME_CONTEXT>{"bar":1}</RUNTIME_CONTEXT>'

    analyzer.analyze(prompt)
    analyzer.analyze(prompt)
    third = analyzer.analyze(prompt)

    assert "resume" not in commands[0]
    assert "resume" in commands[1]
    assert "resume" not in commands[2]
    assert third.diagnostics["session_mode"] == "new"
    assert third.diagnostics["rotated_from_session_id"] == "thread-2"


def test_codex_failure_prefers_json_error_over_powershell_snapshot_warning() -> None:
    text = _command_failure_text(
        "WARN codex_core::shell_snapshot: Shell snapshot not supported yet for PowerShell\n",
        '{"type":"error","message":"invalid_json_schema"}\n',
    )
    assert "shell_snapshot" not in text
    assert "invalid_json_schema" in text


def test_replay_rule_text_removes_only_production_io_chapters() -> None:
    original = "# R\n\n## 1. 讀圖與操作安全\nChrome\n\n## 2. 資料紀律與時間錨定\nTrading\n\n## 16. 結構化 adapter 固定介面\nHeartbeat\n\n## 17. 執行期 fail-safe 與相容性\nState"
    result = replay_rule_text(original)
    assert "Chrome" not in result
    assert "Heartbeat" not in result
    assert "Trading" in result
    assert "State" in result


def test_prompt_contains_override_and_only_supplied_runtime() -> None:
    prompt = build_replay_prompt(
        rules_text="RULES",
        schema_text='{"type":"object"}',
        runtime_context={"bars": [{"time": "08:45", "close": 100}]},
    )
    assert "REPLAY_EXECUTION_OVERRIDE" in prompt
    assert "08:45" in prompt
    assert "99999" not in prompt
    assert prompt.index("<MONITOR_RULES>") < prompt.index("<RUNTIME_CONTEXT>")


def test_deterministic_prompt_uses_compact_market_notation_and_exact_ledger_prices() -> None:
    prompt = build_replay_prompt(
        rules_text="RULES",
        schema_text='{"type":"object"}',
        runtime_context={"deterministic_evidence_ledger": {"latest_closed_k": {"close": 44957}}},
        deterministic_contract=True,
    )
    assert "`1分K`、`15分`、`21MA`、`105MA`" in prompt
    assert "必須逐字複製deterministic_evidence_ledger中的數值" in prompt


def test_v3_prompt_requires_grade_upgrade_dual_quadrants_and_event_cards() -> None:
    prompt = build_replay_prompt(
        rules_text="RULES",
        schema_text='{"type":"object","properties":{"message_type":{"type":"string"}}}',
        runtime_context={"deterministic_evidence_events": [{"event_type": "GRADE_UPGRADE"}]},
        deterministic_contract=True,
    )
    assert "background_quadrant與working_quadrant" in prompt
    assert "forming_small_anchor_ref" in prompt
    assert "message_type=STRUCTURE_UPGRADE" in prompt
    assert "TRIGGER、CHECKPOINT、HARD_TARGET" in prompt
    assert "同一setup最多再進場1次" in prompt


def test_resume_rejects_ai_provider_or_model_change(tmp_path: Path) -> None:
    fake = FakeAnalyzer()
    dataset = _dataset()

    def loader(target_date: date, *, instrument: str) -> ReplayDataset:
        return dataset

    original = ReplayRunner(
        _legacy_config(ai_provider="codex", ai_model="gpt-5.6-sol", codex_reasoning_effort="medium"),
        analyzer=fake,
        dataset_loader=loader,
        runtime_root=tmp_path,
    )
    result = asyncio.run(original.run(target_date=date(2026, 8, 27), preopen_only=True))
    changed = ReplayRunner(
        _legacy_config(ai_provider="codex", ai_model="gpt-5.6-sol", codex_reasoning_effort="high"),
        analyzer=fake,
        dataset_loader=loader,
        runtime_root=tmp_path,
    )
    with pytest.raises(ReplayRunError, match="reasoning effort"):
        asyncio.run(changed.resume(result["run_id"]))

    attempts_changed = ReplayRunner(
        _legacy_config(
            ai_provider="codex",
            ai_model="gpt-5.6-sol",
            codex_reasoning_effort="medium",
            max_ai_attempts=3,
        ),
        analyzer=fake,
        dataset_loader=loader,
        runtime_root=tmp_path,
    )
    with pytest.raises(ReplayRunError, match="重試上限"):
        asyncio.run(attempts_changed.resume(result["run_id"]))


def test_resume_can_extend_end_time_and_pause_by_batch(tmp_path: Path) -> None:
    fake = FakeAnalyzer()
    dataset = ReplayDataset(
        target_date=date(2026, 8, 27),
        instrument="TMF",
        expiry_month="202609",
        night_bars=_frame(["2026-08-26 15:00", "2026-08-26 15:01"], [100, 101]),
        day_bars=_frame(
            [
                "2026-08-27 08:45",
                "2026-08-27 08:46",
                "2026-08-27 08:47",
                "2026-08-27 08:48",
                "2026-08-27 08:49",
                "2026-08-27 08:50",
            ],
            [102, 103, 104, 105, 106, 107],
        ),
        source_sha256={"2026-08-26": "a" * 64, "2026-08-27": "b" * 64},
    )

    def loader(target_date: date, *, instrument: str) -> ReplayDataset:
        return dataset

    runner = ReplayRunner(
        _legacy_config(telegram_enabled=False, analysis_every_bars=2),
        analyzer=fake,
        dataset_loader=loader,
        runtime_root=tmp_path,
    )
    first = asyncio.run(
        runner.run(
            target_date=date(2026, 8, 27),
            start_time=datetime.strptime("08:45", "%H:%M").time(),
            end_time=datetime.strptime("08:47", "%H:%M").time(),
            analysis_every_bars=2,
        )
    )
    assert first["completed_day_bars"] == 2
    resumed = asyncio.run(
        runner.resume(
            first["run_id"],
            continue_day=True,
            end_time=datetime.strptime("08:50", "%H:%M").time(),
            batch_size=1,
        )
    )
    assert resumed["status"] == "paused"
    assert resumed["completed_day_bars"] == 3
    manifest = json.loads((Path(first["run_directory"]) / "manifest.json").read_text(encoding="utf-8"))
    assert [datetime.fromisoformat(value).strftime("%H:%M:%S") for value in manifest["presentation_bar_times"]] == [
        "08:46:00",
        "08:47:00",
        "08:48:00",
        "08:50:00",
    ]
    assert manifest["selected_bar_times"][-2].endswith("08:48:00+08:00")
    assert manifest["selected_bar_times"][-1].endswith("08:50:00+08:00")


def test_notifier_is_deduplicated(tmp_path: Path) -> None:
    calls: list[tuple[str, str, str]] = []

    async def fake_send(token: str, chat_id: str, message: str) -> TelegramPushResult:
        calls.append((token, chat_id, message))
        return TelegramPushResult(ok=True, status="sent", chunk_count=1, sent_chunk_count=1, message_ids=[321])

    notifier = ReplayNotifier(
        bot_token="123456:abcdefghijklmnopqrstuvwxyzABCDE",
        chat_id="-100000000001",
        state_path=tmp_path / "delivery.json",
        send_function=fake_send,
    )
    first = asyncio.run(notifier.send(event_id="event-1", message="🧪測試"))
    second = asyncio.run(notifier.send(event_id="event-1", message="🧪測試"))
    assert first["status"] == "sent"
    assert second["status"] == "duplicate"
    assert len(calls) == 1


def test_replay_config_never_falls_back_to_production_chat(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text('{"telegram_enabled":true,"chat_id":"PRODUCTION"}', encoding="utf-8")
    config = load_replay_config(path)
    assert config.telegram_chat_id is None
    assert config.message_profile == "course_event_cards"
    assert config.quiet_status_interval_minutes == 10


def test_replay_config_validates_message_profile_and_quiet_interval(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        '{"message_profile":"formal_nine_sections","quiet_status_interval_minutes":15}',
        encoding="utf-8",
    )
    config = load_replay_config(path)
    assert config.message_profile == "formal_nine_sections"
    assert config.quiet_status_interval_minutes == 15

    path.write_text('{"message_profile":"unknown"}', encoding="utf-8")
    with pytest.raises(ReplayConfigError, match="message_profile"):
        load_replay_config(path)

    path.write_text('{"quiet_status_interval_minutes":61}', encoding="utf-8")
    with pytest.raises(ReplayConfigError, match="最多60分鐘"):
        load_replay_config(path)


def test_replay_config_allows_three_bounded_ai_attempts(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text('{"max_ai_attempts":3}', encoding="utf-8")
    assert load_replay_config(path).max_ai_attempts == 3

    path.write_text('{"max_ai_attempts":4}', encoding="utf-8")
    with pytest.raises(ReplayConfigError, match="最多只能為 3"):
        load_replay_config(path)


def test_runner_is_causal_and_writes_only_replay_runtime(tmp_path: Path) -> None:
    fake = FakeAnalyzer()
    dataset = _dataset()

    def loader(target_date: date, *, instrument: str) -> ReplayDataset:
        assert target_date == date(2026, 8, 27)
        assert instrument == "TMF"
        return dataset

    config = _legacy_config(telegram_enabled=False, analysis_every_bars=1)
    runner = ReplayRunner(
        config,
        analyzer=fake,
        dataset_loader=loader,
        runtime_root=tmp_path / "replay-runtime",
        step_callback=lambda _: True,
    )
    result = asyncio.run(runner.run(target_date=date(2026, 8, 27), max_bars=1))
    assert result["status"] == "completed"
    assert result["completed_day_bars"] == 1
    assert len(fake.prompts) == 2
    assert "99999" not in fake.prompts[-1]
    assert "Codex" in fake.prompts[-1]
    assert not (tmp_path / "trade_monitor").exists()
    messages = (Path(result["run_directory"]) / "messages.jsonl").read_text(encoding="utf-8")
    assert "🧪【歷史回放" in messages
    assert "這是歷史資料模擬，不是即時交易訊號" in messages
    manifest = json.loads((Path(result["run_directory"]) / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["execution_version"] == "replay-execution-v5-provider-neutral-structured-output"
    assert len(manifest["execution_prompt_sha256"]) == 64


def test_runner_throttles_only_repeated_quiet_telegram_cards(tmp_path: Path) -> None:
    fake = FakeAnalyzer()
    calls: list[str] = []

    async def fake_send(token: str, chat_id: str, message: str) -> TelegramPushResult:
        calls.append(message)
        return TelegramPushResult(
            ok=True,
            status="sent",
            chunk_count=1,
            sent_chunk_count=1,
            message_ids=[100 + len(calls)],
        )

    runner = ReplayRunner(
        _legacy_config(
            telegram_enabled=True,
            telegram_chat_id="-100000000001",
            analysis_every_bars=1,
            quiet_status_interval_minutes=10,
        ),
        analyzer=fake,
        dataset_loader=lambda *_args, **_kwargs: _dataset(),
        runtime_root=tmp_path / "replay-runtime",
    )

    def notifier(run_dir: Path) -> ReplayNotifier:
        return ReplayNotifier(
            bot_token="123456:abcdefghijklmnopqrstuvwxyzABCDE",
            chat_id="-100000000001",
            state_path=run_dir / "delivery-state.json",
            send_function=fake_send,
        )

    runner._get_notifier = notifier  # type: ignore[method-assign]
    result = asyncio.run(
        runner.run(target_date=date(2026, 8, 27), max_bars=2, telegram=True)
    )
    events = [
        json.loads(line)
        for line in (Path(result["run_directory"]) / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]

    assert len(calls) == 2  # 盤前快照及日盤第一則安靜狀態
    assert events[-1]["decision"] == "DONT_NOTIFY"
    assert events[-1]["delivery"]["status"] == "quiet_throttled"
    assert events[-1]["message_kind"] == "UNCHANGED"


def test_run_directory_uses_short_rule_hash_but_manifest_keeps_logical_version(tmp_path: Path) -> None:
    run_dir = create_run_directory(
        target_date="2026-08-27",
        instrument="TMF",
        rule_version="baseline-v2.1.8-replay-v2-with-an-intentionally-very-long-version-label",
        runtime_root=tmp_path,
    )
    assert "baseline" not in run_dir.name
    assert "-r" in run_dir.name
    assert len(run_dir.name) < 80


def test_default_two_bar_cadence_keeps_both_one_minute_bars(tmp_path: Path) -> None:
    fake = FakeAnalyzer()
    dataset = _dataset()

    def loader(target_date: date, *, instrument: str) -> ReplayDataset:
        return dataset

    runner = ReplayRunner(
        _legacy_config(telegram_enabled=False),
        analyzer=fake,
        dataset_loader=loader,
        runtime_root=tmp_path / "replay-runtime",
    )
    result = asyncio.run(runner.run(target_date=date(2026, 8, 27), max_bars=1))
    assert result["completed_day_bars"] == 1
    assert '"new_closed_bar_count":2' in fake.prompts[-1]
    assert "2026-08-27T08:45:00+08:00" in fake.prompts[-1]
    assert "2026-08-27T08:46:00+08:00" in fake.prompts[-1]
    event = json.loads((Path(result["run_directory"]) / "events.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert event["bar_time"] == "2026-08-27T08:46:00+08:00"


def test_completed_preopen_run_can_continue_without_repeating_preopen(tmp_path: Path) -> None:
    fake = FakeAnalyzer()
    dataset = _dataset()

    def loader(target_date: date, *, instrument: str) -> ReplayDataset:
        return dataset

    runner = ReplayRunner(
        _legacy_config(telegram_enabled=False),
        analyzer=fake,
        dataset_loader=loader,
        runtime_root=tmp_path / "replay-runtime",
    )
    preopen = asyncio.run(runner.run(target_date=date(2026, 8, 27), preopen_only=True))
    assert len(fake.prompts) == 1
    continued = asyncio.run(runner.resume(preopen["run_id"], continue_day=True, max_bars=1))
    assert continued["status"] == "completed"
    assert continued["completed_day_bars"] == 1
    assert len(fake.prompts) == 2
    rerun = asyncio.run(runner.resume(preopen["run_id"], rerun_last=True))
    assert rerun["completed_day_bars"] == 1
    assert len(fake.prompts) == 3
    assert (Path(rerun["run_directory"]) / "rewinds.jsonl").exists()


def test_run_can_rewind_to_completed_analysis_point_without_ai_call(tmp_path: Path) -> None:
    fake = FakeAnalyzer()
    dataset = _dataset()
    runner = ReplayRunner(
        _legacy_config(telegram_enabled=False),
        analyzer=fake,
        dataset_loader=lambda *_args, **_kwargs: dataset,
        runtime_root=tmp_path / "replay-runtime",
    )
    completed = asyncio.run(
        runner.run(target_date=date(2026, 8, 27), max_bars=2, analysis_every_bars=1)
    )
    calls_before = len(fake.prompts)
    selected = json.loads((Path(completed["run_directory"]) / "manifest.json").read_text(encoding="utf-8"))[
        "selected_bar_times"
    ]
    target = datetime.fromisoformat(selected[1]).time().replace(second=0, microsecond=0)

    rewound = asyncio.run(runner.resume(completed["run_id"], rewind_to=target))

    assert rewound["status"] == "paused"
    assert rewound["completed_day_bars"] == 1
    assert len(fake.prompts) == calls_before


def test_guard_only_version_migration_requires_explicit_rewind(tmp_path: Path) -> None:
    fake = FakeAnalyzer()
    dataset = _dataset()
    runner = ReplayRunner(
        _legacy_config(telegram_enabled=False),
        analyzer=fake,
        dataset_loader=lambda *_args, **_kwargs: dataset,
        runtime_root=tmp_path / "replay-runtime",
    )
    completed = asyncio.run(
        runner.run(target_date=date(2026, 8, 27), max_bars=2, analysis_every_bars=1)
    )
    manifest_path = Path(completed["run_directory"]) / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["execution_version"] = "older-guard-only-version"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    target = datetime.fromisoformat(manifest["selected_bar_times"][1]).time().replace(second=0, microsecond=0)

    with pytest.raises(ReplayRunError, match="必須明確--rewind-to"):
        asyncio.run(runner.resume(completed["run_id"], continue_day=True))

    rewound = asyncio.run(runner.resume(completed["run_id"], rewind_to=target))
    migrated = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert rewound["status"] == "paused"
    assert migrated["execution_version"] == runner.execution_version
    assert migrated["execution_version_migrations"][-1]["from"] == "older-guard-only-version"


def test_rewound_range_can_reuse_causal_saved_raw_without_ai_calls(tmp_path: Path) -> None:
    fake = FakeAnalyzer()
    dataset = _dataset()
    runner = ReplayRunner(
        _legacy_config(telegram_enabled=False, analysis_every_bars=1),
        analyzer=fake,
        dataset_loader=lambda *_args, **_kwargs: dataset,
        runtime_root=tmp_path / "replay-runtime",
    )
    completed = asyncio.run(
        runner.run(target_date=date(2026, 8, 27), max_bars=2, analysis_every_bars=1)
    )
    calls_before = len(fake.prompts)
    manifest_path = Path(completed["run_directory"]) / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    target = datetime.fromisoformat(manifest["selected_bar_times"][1]).time().replace(second=0, microsecond=0)

    rewound = asyncio.run(runner.resume(completed["run_id"], rewind_to=target))
    assert rewound["completed_day_bars"] == 1
    replayed = asyncio.run(
        runner.resume(completed["run_id"], reuse_saved_raw=True, batch_size=1)
    )

    assert replayed["completed_day_bars"] == 2
    assert len(fake.prompts) == calls_before
    run_dir = Path(replayed["run_directory"])
    latest_event = json.loads((run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert latest_event["analysis_source"] == "saved_raw"


def test_reuse_saved_raw_falls_back_to_an_older_valid_attempt(tmp_path: Path) -> None:
    fake = FakeAnalyzer()
    dataset = _dataset()
    runner = ReplayRunner(
        _legacy_config(telegram_enabled=False, analysis_every_bars=1),
        analyzer=fake,
        dataset_loader=lambda *_args, **_kwargs: dataset,
        runtime_root=tmp_path / "replay-runtime",
    )
    completed = asyncio.run(
        runner.run(target_date=date(2026, 8, 27), max_bars=2, analysis_every_bars=1)
    )
    calls_before = len(fake.prompts)
    run_dir = Path(completed["run_directory"])
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    target_dt = datetime.fromisoformat(manifest["selected_bar_times"][1])
    target = target_dt.time().replace(second=0, microsecond=0)
    key = f"day-{target_dt.strftime('%Y%m%d-%H%M')}"
    invalid_newest = run_dir / "analysis" / f"{key}-resume-999-attempt-1-raw.txt"
    invalid_newest.write_text("{}", encoding="utf-8")

    asyncio.run(runner.resume(completed["run_id"], rewind_to=target))
    replayed = asyncio.run(
        runner.resume(completed["run_id"], reuse_saved_raw=True, batch_size=1)
    )

    assert replayed["completed_day_bars"] == 2
    assert len(fake.prompts) == calls_before
    revalidations = [
        json.loads(line)
        for line in (run_dir / "revalidations.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert revalidations[-1]["source_file"] != invalid_newest.name


def test_completed_day_plan_can_be_extended_without_changing_two_bar_cadence(tmp_path: Path) -> None:
    fake = FakeAnalyzer()
    dataset = ReplayDataset(
        target_date=date(2026, 8, 27),
        instrument="TMF",
        expiry_month="202609",
        night_bars=_frame(["2026-08-26 15:00", "2026-08-26 15:01"], [100, 101]),
        day_bars=_frame(
            ["2026-08-27 08:45", "2026-08-27 08:46", "2026-08-27 08:47", "2026-08-27 08:48"],
            [102, 103, 104, 105],
        ),
        source_sha256={"2026-08-26": "a" * 64, "2026-08-27": "b" * 64},
    )

    runner = ReplayRunner(
        _legacy_config(telegram_enabled=False),
        analyzer=fake,
        dataset_loader=lambda *_args, **_kwargs: dataset,
        runtime_root=tmp_path / "replay-runtime",
    )
    first = asyncio.run(runner.run(target_date=date(2026, 8, 27), max_bars=1))
    assert first["completed_day_bars"] == 1
    extended = asyncio.run(runner.resume(first["run_id"], continue_day=True, max_bars=2))
    assert extended["completed_day_bars"] == 2
    assert len(fake.prompts) == 3
    assert '"new_closed_bar_count":2' in fake.prompts[-1]
    assert "2026-08-27T08:48:00+08:00" in fake.prompts[-1]


def test_saved_raw_can_be_revalidated_without_another_ai_call(tmp_path: Path) -> None:
    dataset = _dataset()

    class InvalidPreopenAnalyzer(FakeAnalyzer):
        def analyze(self, prompt: str) -> MiniMaxReplayResult:
            result = super().analyze(prompt)
            result.payload["analysis"]["market_state"] = ["下一根05:00仍是未收盤K。"]
            raw = json.dumps(result.payload, ensure_ascii=False)
            return MiniMaxReplayResult(payload=result.payload, raw_text=raw, diagnostics=result.diagnostics)

    fake = InvalidPreopenAnalyzer()

    def loader(target_date: date, *, instrument: str) -> ReplayDataset:
        return dataset

    runtime_root = tmp_path / "replay-runtime"
    runner = ReplayRunner(
        _legacy_config(telegram_enabled=False),
        analyzer=fake,
        dataset_loader=loader,
        runtime_root=runtime_root,
    )
    with pytest.raises(ReplayRunError, match="05:00"):
        asyncio.run(runner.run(target_date=date(2026, 8, 27), preopen_only=True))
    assert len(fake.prompts) == 2
    run_dir = next((runtime_root / "runs").iterdir())
    latest_raw = max((run_dir / "analysis").glob("preopen-*-raw.txt"), key=lambda path: path.stat().st_mtime_ns)
    corrected = _analysis_payload("2026-08-26T15:01:00+08:00", preopen=True)
    latest_raw.write_text(json.dumps(corrected, ensure_ascii=False), encoding="utf-8")

    result = asyncio.run(runner.resume(run_dir.name, revalidate_latest_raw=True))
    assert result["status"] == "completed"
    assert result["preopen_completed"] is True
    assert len(fake.prompts) == 2
    revalidation = json.loads((run_dir / "revalidations.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert revalidation["status"] == "accepted"


def test_revalidate_day_stops_after_saved_point_without_new_ai(tmp_path: Path) -> None:
    class InvalidDay(FakeAnalyzer):
        def analyze(self, prompt):
            result = super().analyze(prompt)
            if '"preopen_snapshot":false' in prompt:
                result.payload["analysis"]["original_decision"] = "INVALID"
            return MiniMaxReplayResult(payload=result.payload, raw_text=json.dumps(result.payload), diagnostics=result.diagnostics)
    fake = InvalidDay()
    runner = ReplayRunner(_legacy_config(telegram_enabled=False, analysis_every_bars=1), analyzer=fake,
        dataset_loader=lambda *args, **kwargs: _dataset(), runtime_root=tmp_path/"runtime")
    with pytest.raises(ReplayRunError):
        asyncio.run(runner.run(target_date=date(2026,8,27), analysis_every_bars=1))
    run_dir = next((tmp_path/"runtime/runs").iterdir())
    raw = max((run_dir/"analysis").glob("day-*-raw.txt"), key=lambda p:p.stat().st_mtime_ns)
    raw.write_text(json.dumps(_analysis_payload("2026-08-27T08:45:00+08:00", preopen=False)), encoding="utf-8")
    before = len(fake.prompts)
    result = asyncio.run(runner.resume(run_dir.name, revalidate_latest_raw=True))
    assert result["status"] == "paused"
    assert result["completed_day_bars"] == 1
    assert len(fake.prompts) == before


def test_latest_completed_day_raw_can_be_revalidated_without_advancing(tmp_path: Path) -> None:
    fake = FakeAnalyzer()
    runner = ReplayRunner(
        _legacy_config(telegram_enabled=False, analysis_every_bars=1),
        analyzer=fake,
        dataset_loader=lambda *args, **kwargs: _dataset(),
        runtime_root=tmp_path / "runtime",
    )
    first = asyncio.run(runner.run(target_date=date(2026, 8, 27), max_bars=1))
    before = len(fake.prompts)

    result = asyncio.run(runner.resume(first["run_id"], revalidate_latest_raw=True))

    assert result["status"] == "completed"
    assert result["completed_day_bars"] == 1
    assert len(fake.prompts) == before
    run_dir = Path(result["run_directory"])
    latest_event = json.loads((run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert latest_event["analysis_source"] == "saved_raw"
    assert latest_event["bar_time"].endswith("08:45:00+08:00")


def test_deliver_latest_reuses_saved_canonical_message_and_deduplicates(tmp_path: Path) -> None:
    fake = FakeAnalyzer()
    dataset = _dataset()
    calls: list[str] = []

    def loader(target_date: date, *, instrument: str) -> ReplayDataset:
        return dataset

    async def fake_send(token: str, chat_id: str, message: str) -> TelegramPushResult:
        calls.append(message)
        return TelegramPushResult(ok=True, status="sent", chunk_count=1, sent_chunk_count=1, message_ids=[456])

    runner = ReplayRunner(
        _legacy_config(telegram_enabled=True, telegram_chat_id="-100000000001"),
        analyzer=fake,
        dataset_loader=loader,
        runtime_root=tmp_path / "replay-runtime",
    )
    preopen = asyncio.run(runner.run(target_date=date(2026, 8, 27), preopen_only=True))

    def notifier(run_dir: Path) -> ReplayNotifier:
        return ReplayNotifier(
            bot_token="123456:abcdefghijklmnopqrstuvwxyzABCDE",
            chat_id="-100000000001",
            state_path=run_dir / "delivery-state.json",
            send_function=fake_send,
        )

    runner._get_notifier = notifier  # type: ignore[method-assign]
    first = asyncio.run(runner.deliver_latest(preopen["run_id"]))
    second = asyncio.run(runner.deliver_latest(preopen["run_id"]))
    assert first["status"] == "sent"
    assert first["message_ids"] == [456]
    assert second["status"] == "duplicate"
    assert len(calls) == 1
    saved = json.loads((Path(preopen["run_directory"]) / "messages.jsonl").read_text(encoding="utf-8"))
    assert calls[0] == saved["message"]


def test_telegram_connectivity_test_is_deduplicated(tmp_path: Path) -> None:
    calls: list[str] = []

    async def fake_send(token: str, chat_id: str, message: str) -> TelegramPushResult:
        calls.append(message)
        return TelegramPushResult(ok=True, status="sent", chunk_count=1, sent_chunk_count=1, message_ids=[789])

    runner = ReplayRunner(
        _legacy_config(telegram_enabled=True, telegram_chat_id="-100000000001"),
        analyzer=FakeAnalyzer(),
        dataset_loader=lambda *_args, **_kwargs: _dataset(),
        runtime_root=tmp_path / "replay-runtime",
    )

    def notifier(state_path: Path) -> ReplayNotifier:
        return ReplayNotifier(
            bot_token="123456:abcdefghijklmnopqrstuvwxyzABCDE",
            chat_id="-100000000001",
            state_path=state_path,
            send_function=fake_send,
        )

    runner._notifier_for_state = notifier  # type: ignore[method-assign]
    first = asyncio.run(runner.test_telegram())
    second = asyncio.run(runner.test_telegram())
    assert first["status"] == "sent"
    assert second["status"] == "duplicate"
    assert first["message_ids"] == [789]
    assert len(calls) == 1
    assert "不是交易訊號" in calls[0]
