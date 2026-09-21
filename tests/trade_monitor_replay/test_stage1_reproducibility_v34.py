from __future__ import annotations

import json
from pathlib import Path

import pytest

from trade_monitor_replay.reproducibility import build_program_fingerprint
from trade_monitor_replay.rules import load_rule_package
from trade_monitor_replay.semantic_contract import (
    SemanticReplayError,
    _validate_trade_setup_policy,
)


ROOT = Path(__file__).parents[2]
V31_MANIFEST = (
    ROOT
    / "trade_monitor_replay"
    / "rules"
    / "course-state-v2.1.8-replay-adapter-v31"
    / "rule-manifest.json"
)
AT = "2026-08-21T09:00:00+08:00"


def _fingerprint_run(root: Path, **identity_overrides: object) -> Path:
    analysis_dir = root / "analysis"
    analysis_dir.mkdir(parents=True)
    manifest = {
        "run_id": root.name,
        "target_date": "2026-08-21",
        "instrument": "TMF",
        "expiry_month": "202609",
        "source_sha256": {"2026-08-21": "a" * 64},
        "rule_version": "v34",
        "rule_sha256": "b" * 64,
        "model_rules_sha256": "c" * 64,
        "schema_sha256": "d" * 64,
        "execution_version": "engine-v34",
        "execution_prompt_sha256": "e" * 64,
        "execution_implementation_sha256": "9" * 64,
        "analysis_mode": "AI_HYBRID",
        "decision_authority": "AI_WITH_PROGRAM_GUARDRAILS",
        "execution_profile": "ai-hybrid-long-only-q2-q4-v1",
        "trade_direction_policy": "LONG_ONLY",
        "trade_setup_policy": "LONG_Q2_Q4_ONLY",
        "ai_input_view_version": "ai-hybrid-evidence-only-v2-actionable-stage",
        "ai_provider": "codex",
        "ai_model": "model-a",
        "ai_reasoning_effort": "low",
        "selected_bar_times": [AT],
        "next_day_index": 1,
        **identity_overrides,
    }
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (analysis_dir / "day-20260821-0900-validated.json").write_text(
        json.dumps({"analysis": {"course_reading": {}, "scenario": {}, "action": {}}}),
        encoding="utf-8",
    )
    (analysis_dir / "day-20260821-0900-state-checkpoint.json").write_text(
        "{}",
        encoding="utf-8",
    )
    return root


def test_legacy_v31_keeps_pre_v34_defaults() -> None:
    package = load_rule_package(V31_MANIFEST)

    assert package.analysis_mode == "PROGRAM_ONLY"
    assert package.decision_authority == "PROGRAM"
    assert package.trade_direction_policy == "BOTH"
    assert package.trade_setup_policy == "ALL"


@pytest.mark.parametrize(
    ("field", "different_value"),
    [
        ("ai_model", "model-b"),
        ("execution_prompt_sha256", "f" * 64),
        ("execution_implementation_sha256", "8" * 64),
        ("rule_sha256", "1" * 64),
        ("model_rules_sha256", "2" * 64),
        ("decision_authority", "PROGRAM"),
        ("trade_direction_policy", "BOTH"),
        ("trade_setup_policy", "ALL"),
    ],
)
def test_fingerprint_changes_with_model_prompt_rules_or_policy(
    tmp_path: Path,
    field: str,
    different_value: str,
) -> None:
    baseline = _fingerprint_run(tmp_path / "baseline")
    changed = _fingerprint_run(tmp_path / "changed", **{field: different_value})

    first = build_program_fingerprint(baseline)
    second = build_program_fingerprint(changed)

    assert first["payload"]["input_identity"][field] != second["payload"]["input_identity"][field]
    assert first["program_fingerprint_sha256"] != second["program_fingerprint_sha256"]


@pytest.mark.parametrize(
    ("field", "different_value"),
    [
        ("execution_profile", "different-execution-policy"),
        ("ai_input_view_version", "different-ai-input-view"),
    ],
)
def test_fingerprint_changes_with_remaining_execution_identity(
    tmp_path: Path,
    field: str,
    different_value: str,
) -> None:
    baseline = _fingerprint_run(tmp_path / "baseline")
    changed = _fingerprint_run(tmp_path / "changed", **{field: different_value})

    assert (
        build_program_fingerprint(baseline)["program_fingerprint_sha256"]
        != build_program_fingerprint(changed)["program_fingerprint_sha256"]
    )


@pytest.mark.parametrize(
    ("source", "working", "primary", "trend", "volatility"),
    [
        ("FALSE_BREAK_RECLAIM", "Q2", "Q4", "INCREASING", "CONTRACTING"),
        ("ANCHOR_LEG_SEQUENCE", "Q4", "Q2", "DECREASING", "EXPANDING"),
    ],
)
def test_direct_q2_q4_entry_requires_its_own_course_axes(
    source: str,
    working: str,
    primary: str,
    trend: str,
    volatility: str,
) -> None:
    with pytest.raises(SemanticReplayError, match="交易候選"):
        _validate_trade_setup_policy(
            {
                "course_reading": {
                    "working_quadrant": working,
                    "primary_quadrant_candidate": primary,
                    "working_trend_dynamics": trend,
                    "working_volatility_dynamics": volatility,
                },
                "action": {"position_action": "ENTER"},
            },
            ledger={
                "program_trade_policy": {
                    "trade_setup_policy": "LONG_Q2_Q4_ONLY",
                }
            },
            position={"status": "FLAT"},
            entry_gate={
                "status": "ENTRY_ELIGIBLE",
                "required_candidate_source": source,
            },
        )
