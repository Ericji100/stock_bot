from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from trade_monitor_replay.posthoc_adjudication import (
    PosthocAdjudicationError,
    adjudicate_run,
)


def _write_fixture_run(root: Path, *, status: str = "paused") -> str:
    run_id = "replay-test-posthoc"
    run_dir = root / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                "status": status,
                "rule_version": "ai-hybrid-test",
                "execution_version": "execution-test",
                "ai_model": "test-model",
                "analysis_mode": "AI_HYBRID",
                "human_intervention": True,
            }
        ),
        encoding="utf-8",
    )
    comparison = {
        "version": 1,
        "comparison_id": "SC-one",
        "stage": "day",
        "bar_time": "2026-08-11T09:00:00+08:00",
        "causal_cutoff": "2026-08-11T09:00:00+08:00",
        "differences": [{"field": "working_quadrant", "verdict": "UNRESOLVED"}],
        "winner": None,
        "posthoc_outcome": None,
    }
    (run_dir / "semantic-comparisons.jsonl").write_text(
        json.dumps(comparison) + "\n",
        encoding="utf-8",
    )
    with (run_dir / "bars.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["session", "bar_time", "open", "high", "low", "close"],
        )
        writer.writeheader()
        writer.writerows(
            [
                {
                    "session": "day",
                    "bar_time": "2026-08-11T09:00:00+08:00",
                    "open": 99,
                    "high": 102,
                    "low": 99,
                    "close": 100,
                },
                {
                    "session": "day",
                    "bar_time": "2026-08-11T09:01:00+08:00",
                    "open": 100,
                    "high": 104,
                    "low": 99,
                    "close": 103,
                },
                {
                    "session": "day",
                    "bar_time": "2026-08-11T09:02:00+08:00",
                    "open": 103,
                    "high": 106,
                    "low": 101,
                    "close": 105,
                },
            ]
        )
    return run_id


def test_posthoc_run_writes_separate_read_only_artifact(tmp_path: Path) -> None:
    run_id = _write_fixture_run(tmp_path)

    result = adjudicate_run(run_id, runtime_root=tmp_path, horizons=(2,))

    assert result["comparison_count"] == 1
    assert result["unresolved_difference_count"] == 1
    assert result["winner_count"] == 0
    outcome = result["comparisons"][0]["posthoc_outcome"]["horizons"]["2"]
    assert outcome["end_close"] == 105
    assert (tmp_path / "runs" / run_id / "posthoc-semantic-adjudication.json").exists()
    original = json.loads(
        (tmp_path / "runs" / run_id / "semantic-comparisons.jsonl")
        .read_text(encoding="utf-8")
        .strip()
    )
    assert original["posthoc_outcome"] is None
    assert original["winner"] is None


def test_posthoc_run_refuses_live_running_state(tmp_path: Path) -> None:
    run_id = _write_fixture_run(tmp_path, status="running")

    with pytest.raises(PosthocAdjudicationError, match="仍在執行"):
        adjudicate_run(run_id, runtime_root=tmp_path)
