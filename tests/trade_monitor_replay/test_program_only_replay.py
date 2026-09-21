from __future__ import annotations

import asyncio
import json
from datetime import date
from pathlib import Path

import pandas as pd

from trade_monitor_replay.config import ReplayConfig
from trade_monitor_replay.data_source import ReplayDataset
from trade_monitor_replay.program_analyzer import build_program_semantic_envelope
from trade_monitor_replay.reproducibility import compare_program_fingerprints
from trade_monitor_replay.runner import ReplayRunner


ROOT = Path(__file__).parents[2]
V11_MANIFEST = (
    ROOT
    / "trade_monitor_replay"
    / "rules"
    / "course-state-v2.1.8-replay-adapter-v31"
    / "rule-manifest.json"
)
V34_MANIFEST = (
    ROOT
    / "trade_monitor_replay"
    / "rules"
    / "course-state-v2.1.8-replay-adapter-v34-long-only-q2-q4"
    / "rule-manifest.json"
)


def _frame(times: list[str], prices: list[int]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "bar_time": pd.to_datetime(times).tz_localize("Asia/Taipei"),
            "open": prices,
            "high": [value + 4 for value in prices],
            "low": [value - 4 for value in prices],
            "close": [value + 1 for value in prices],
            "volume": [10] * len(times),
            "sma21": [None] * len(times),
            "sma105": [None] * len(times),
            "atr14": [None] * len(times),
        }
    )


def _dataset() -> ReplayDataset:
    return ReplayDataset(
        target_date=date(2026, 8, 21),
        instrument="TMF",
        expiry_month="202609",
        night_bars=_frame(
            ["2026-08-20 15:00", "2026-08-20 15:01", "2026-08-20 15:02"],
            [100, 101, 102],
        ),
        day_bars=_frame(
            ["2026-08-21 08:45", "2026-08-21 08:46"],
            [103, 104],
        ),
        source_sha256={"2026-08-21": "a" * 64},
    )


class _ExplodingAnalyzer:
    def analyze(self, _prompt: str):
        raise AssertionError("program-only must not call an AI analyzer")


def _run(tmp_path: Path, *, model: str) -> tuple[dict, Path]:
    runner = ReplayRunner(
        ReplayConfig(
            rule_manifest_path=V11_MANIFEST,
            ai_provider="codex",
            ai_model=model,
        ),
        analyzer=_ExplodingAnalyzer(),
        dataset_loader=lambda *_args, **_kwargs: _dataset(),
        runtime_root=tmp_path,
    )
    result = asyncio.run(
        runner.run(
            target_date=date(2026, 8, 21),
            max_bars=2,
            analysis_every_bars=1,
            program_only=True,
        )
    )
    return result, Path(result["run_directory"])


def test_program_only_runner_never_calls_ai_and_records_mode(tmp_path: Path) -> None:
    result, run_dir = _run(tmp_path, model="model-a")
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]

    assert result["status"] == "completed"
    assert result["analysis_mode"] == "program_only"
    assert result["ai_call_count"] == 0
    assert manifest["program_only"] is True
    assert manifest["ai_provider"] is None
    assert not (run_dir / "ai-usage.jsonl").exists()
    assert all(item["analysis_source"] == "program" for item in events)
    assert (run_dir / "constitution-state.json").exists()
    checkpoint = json.loads(
        max((run_dir / "analysis").glob("day-*-state-checkpoint.json"), key=lambda p: p.stat().st_mtime_ns).read_text(
            encoding="utf-8"
        )
    )
    assert checkpoint["constitution_state"]["trading_day"] == "2026-08-21"


def test_v34_program_only_uses_program_decision_authority_not_ai_hybrid(tmp_path: Path) -> None:
    runner = ReplayRunner(
        ReplayConfig(
            rule_manifest_path=V34_MANIFEST,
            ai_provider="codex",
            ai_model="unused",
        ),
        analyzer=_ExplodingAnalyzer(),
        dataset_loader=lambda *_args, **_kwargs: _dataset(),
        runtime_root=tmp_path,
    )
    result = asyncio.run(
        runner.run(
            target_date=date(2026, 8, 21),
            max_bars=2,
            analysis_every_bars=1,
            program_only=True,
        )
    )
    run_dir = Path(result["run_directory"])
    checkpoint = json.loads(
        max(
            (run_dir / "analysis").glob("day-*-state-checkpoint.json"),
            key=lambda path: path.stat().st_mtime_ns,
        ).read_text(encoding="utf-8")
    )

    policy = checkpoint["deterministic_evidence_ledger"]["program_trade_policy"]
    assert policy["decision_authority"] == "PROGRAM"
    assert policy["trade_setup_policy"] == "LONG_Q2_Q4_ONLY"


def test_fixed_cadence_does_not_precompute_event_timeline(tmp_path: Path) -> None:
    runner = ReplayRunner(
        ReplayConfig(
            rule_manifest_path=V34_MANIFEST,
            ai_provider="codex",
            ai_model="unused",
        ),
        analyzer=_ExplodingAnalyzer(),
        dataset_loader=lambda *_args, **_kwargs: _dataset(),
        runtime_root=tmp_path,
    )

    def unexpected_timeline(*_args, **_kwargs):
        raise AssertionError("fixed cadence must not precompute the event-driven timeline")

    runner._cached_course_timeline = unexpected_timeline  # type: ignore[method-assign]
    result = asyncio.run(
        runner.run(
            target_date=date(2026, 8, 21),
            max_bars=2,
            analysis_every_bars=1,
            program_only=True,
        )
    )
    run_dir = Path(result["run_directory"])
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["deterministic_timeline_mode"] == "not_required_fixed_cadence"
    assert manifest["deterministic_event_time_count"] == 0
    assert not (run_dir / "deterministic-timeline.json").exists()


def test_program_only_fingerprint_is_independent_of_configured_ai_model(tmp_path: Path) -> None:
    _, first = _run(tmp_path / "first", model="model-a")
    _, second = _run(tmp_path / "second", model="completely-different-model")

    compared = compare_program_fingerprints(first, second)
    assert compared["ok"] is True
    assert compared["status"] == "identical"


def test_two_bar_presentation_still_advances_program_on_every_one_minute_bar(
    tmp_path: Path,
) -> None:
    dataset = ReplayDataset(
        target_date=date(2026, 8, 21),
        instrument="TMF",
        expiry_month="202609",
        night_bars=_frame(
            ["2026-08-20 15:00", "2026-08-20 15:01", "2026-08-20 15:02"],
            [100, 101, 102],
        ),
        day_bars=_frame(
            [
                "2026-08-21 08:45",
                "2026-08-21 08:46",
                "2026-08-21 08:47",
                "2026-08-21 08:48",
            ],
            [103, 104, 105, 106],
        ),
        source_sha256={"2026-08-21": "b" * 64},
    )
    runner = ReplayRunner(
        ReplayConfig(
            rule_manifest_path=V11_MANIFEST,
            ai_provider="codex",
            ai_model="unused",
        ),
        analyzer=_ExplodingAnalyzer(),
        dataset_loader=lambda *_args, **_kwargs: dataset,
        runtime_root=tmp_path,
    )

    result = asyncio.run(
        runner.run(
            target_date=date(2026, 8, 21),
            analysis_every_bars=2,
            program_only=True,
        )
    )
    run_dir = Path(result["run_directory"])
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    events = [
        json.loads(line)
        for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    day_events = [item for item in events if item["stage"] == "day"]
    messages = [
        json.loads(line)
        for line in (run_dir / "messages.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    day_messages = [item for item in messages if item["stage"] == "day"]

    assert result["completed_program_ticks"] == 4
    assert result["completed_presentation_bars"] == 2
    assert result["completed_day_bars"] == 2
    assert len(manifest["selected_bar_times"]) == 4
    assert [item["bar_time"][11:16] for item in day_events] == [
        "08:45", "08:46", "08:47", "08:48"
    ]
    assert [item["scheduled_output"] for item in day_events] == [
        False, True, False, True
    ]
    assert [item["bar_time"][11:16] for item in day_messages] == ["08:46", "08:48"]
    assert all(item["scheduled_output"] is True for item in day_messages)
    ledger = json.loads(
        (run_dir / "deterministic-evidence-ledger.json").read_text(encoding="utf-8")
    )
    assert ledger["as_of"] == "2026-08-21T08:48:00+08:00"


def test_filtered_false_break_event_renders_observation_without_fake_preparation() -> None:
    event = {
        "id": "event-1",
        "event_type": "FALSE_BREAK_RECLAIM",
        "direction": "BULL",
        "first_seen_at": "2026-08-21T09:30:00+08:00",
    }
    envelope = build_program_semantic_envelope(
        ledger={
            "latest_closed_k": {"close": 100},
            "anchor_lifecycle": {
                "background_anchor": {"direction": "BULL", "status": "ACTIVE"},
                "quadrant_context": {},
            },
            "anchor_control": {},
            "course_method_state": {},
            "structure_events": [event],
            "trade_levels": {
                "continuation_arm_candidate": None,
                "course_filtered_candidate": {"setup_key": "filtered"},
            },
        },
        expected_as_of="2026-08-21T09:30:00+08:00",
        expected_session_key="2026-08-21:DAY",
        preopen=False,
        position={"status": "FLAT", "pending_entry": None},
        previous_memory=None,
        entry_gate={"status": "NONE"},
        evidence_events=[{"event_id": "event-1"}],
    )

    assert envelope["analysis"]["message_type"] == "OBSERVATION"
    assert envelope["analysis"]["course_reading"]["setup_stage"] == "NONE"
