from __future__ import annotations

import asyncio
import json
from datetime import datetime, time
from pathlib import Path

import pytest

from trade_monitor_replay.config import ReplayConfig
from trade_monitor_replay.deterministic_state import (
    _false_break_arm_candidate,
    _select_program_entry_candidate,
)
from trade_monitor_replay.execution_gate import (
    derive_analysis_exit_audit,
    derive_position_behavior_audit,
    fill_pending_exit,
    schedule_pending_exit,
)
from trade_monitor_replay.minimax_analyzer import (
    AI_HYBRID_EXECUTION_VERSION,
    AI_HYBRID_LONG_ONLY_EXECUTION_VERSION,
    AI_HYBRID_LONG_ONLY_Q2_Q4_EXECUTION_VERSION,
    build_replay_prompt,
)
from trade_monitor_replay.presentation import _v3_structure_event_line
from trade_monitor_replay.rules import ReplayRuleError, load_rule_package
from trade_monitor_replay.runner import ReplayRunError, ReplayRunner
from trade_monitor_replay.semantic_contract import (
    SemanticReplayError,
    _canonicalize_long_only_latest_material_event_ref,
    _canonicalize_long_only_upgrade_references,
    _is_highest_active_anchor_aligned_entry,
    _large_grade_upgrade_quadrant_authority,
    _long_only_persisted_large_anchor_ref,
    _long_only_promoted_large_trend,
    _previous_ai_hybrid_large_anchor_ref,
    _reading_v3,
    _validate_trade_direction_policy,
)


ROOT = Path(__file__).parents[2]
V32_MANIFEST = (
    ROOT
    / "trade_monitor_replay"
    / "rules"
    / "course-state-v2.1.8-replay-adapter-v32"
    / "rule-manifest.json"
)
V33_MANIFEST = (
    ROOT
    / "trade_monitor_replay"
    / "rules"
    / "course-state-v2.1.8-replay-adapter-v33-long-only"
    / "rule-manifest.json"
)
V34_MANIFEST = (
    ROOT
    / "trade_monitor_replay"
    / "rules"
    / "course-state-v2.1.8-replay-adapter-v34-long-only-q2-q4"
    / "rule-manifest.json"
)


def _policy_ledger(policy: str = "LONG_ONLY") -> dict[str, object]:
    return {
        "program_trade_policy": {
            "actionable_setups": "PROGRAM_CANDIDATES_AI_DECISION",
            "decision_authority": "AI_HYBRID",
            "trade_direction_policy": policy,
        }
    }


def _flat_action(direction: str = "NONE") -> dict[str, object]:
    return {"action": {"position_action": "NONE", "direction": direction}}


def test_v33_is_distinct_long_only_hybrid_package() -> None:
    v32 = load_rule_package(V32_MANIFEST)
    v33 = load_rule_package(V33_MANIFEST)

    assert v32.trade_direction_policy == "BOTH"
    assert v33.trade_direction_policy == "LONG_ONLY"
    assert v33.analysis_mode == "AI_HYBRID"
    assert v33.decision_authority == "AI_WITH_PROGRAM_GUARDRAILS"
    assert v33.execution_profile == "ai-hybrid-long-only-v1"
    assert v33.model_rules_sha256 == v32.model_rules_sha256


def test_v33_runner_has_distinct_execution_fingerprint_without_changing_v32() -> None:
    old_runner = ReplayRunner(ReplayConfig(rule_manifest_path=V32_MANIFEST))
    new_runner = ReplayRunner(ReplayConfig(rule_manifest_path=V33_MANIFEST))

    assert old_runner.long_only is False
    assert old_runner.execution_version == AI_HYBRID_EXECUTION_VERSION
    assert new_runner.long_only is True
    assert new_runner.execution_version == AI_HYBRID_LONG_ONLY_EXECUTION_VERSION
    assert new_runner.execution_prompt_sha256 != old_runner.execution_prompt_sha256


def test_v34_q2_q4_has_its_own_execution_version_without_relabeling_v33() -> None:
    v33 = ReplayRunner(ReplayConfig(rule_manifest_path=V33_MANIFEST))
    v34 = ReplayRunner(ReplayConfig(rule_manifest_path=V34_MANIFEST))

    assert v33.execution_version == AI_HYBRID_LONG_ONLY_EXECUTION_VERSION
    assert v34.execution_version == AI_HYBRID_LONG_ONLY_Q2_Q4_EXECUTION_VERSION
    assert v34.execution_version != v33.execution_version


def test_v34_never_migrates_an_old_execution_version_inside_one_run(tmp_path: Path) -> None:
    runner = ReplayRunner(
        ReplayConfig(rule_manifest_path=V34_MANIFEST),
        runtime_root=tmp_path,
    )
    run_id = "old-v34-run"
    run_dir = tmp_path / "runs" / run_id
    run_dir.mkdir(parents=True)
    manifest = {
        "rule_version": runner.rules.version,
        "rule_sha256": runner.rules.prompt_sha256,
        "schema_sha256": runner.rules.schema_sha256,
        "execution_prompt_sha256": runner.execution_prompt_sha256,
        "execution_implementation_sha256": runner.execution_implementation_sha256,
        "trade_direction_policy": runner.rules.trade_direction_policy,
        "trade_setup_policy": runner.rules.trade_setup_policy,
        "detail_bar_count": runner.config.detail_bar_count,
        "execution_version": "older-q2-q4-execution",
    }
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False),
        encoding="utf-8",
    )

    with pytest.raises(ReplayRunError, match="必須建立新run"):
        asyncio.run(runner.resume(run_id, rewind_to=time(9, 0)))


def test_v34_refuses_same_named_version_when_execution_code_hash_changed(tmp_path: Path) -> None:
    runner = ReplayRunner(
        ReplayConfig(rule_manifest_path=V34_MANIFEST),
        runtime_root=tmp_path,
    )
    run_id = "same-version-old-code"
    run_dir = tmp_path / "runs" / run_id
    run_dir.mkdir(parents=True)
    manifest = {
        "rule_version": runner.rules.version,
        "rule_sha256": runner.rules.prompt_sha256,
        "schema_sha256": runner.rules.schema_sha256,
        "execution_prompt_sha256": runner.execution_prompt_sha256,
        "execution_implementation_sha256": "0" * 64,
        "trade_direction_policy": runner.rules.trade_direction_policy,
        "trade_setup_policy": runner.rules.trade_setup_policy,
        "detail_bar_count": runner.config.detail_bar_count,
        "execution_version": runner.execution_version,
    }
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False),
        encoding="utf-8",
    )

    with pytest.raises(ReplayRunError, match="不能接續舊run"):
        asyncio.run(runner.resume(run_id))


def test_long_only_prompt_preserves_bearish_diagnosis_but_forbids_short_execution() -> None:
    package = load_rule_package(V33_MANIFEST)
    prompt = build_replay_prompt(
        rules_text=package.model_rules,
        schema_text=package.schema_text,
        runtime_context={},
        deterministic_contract=True,
        course_chain_contract=True,
        ai_hybrid=True,
        trade_direction_policy=package.trade_direction_policy,
    )

    assert "市場結構仍須雙向辨識" in prompt
    assert "交易執行只有LONG或空手" in prompt
    assert "空方定錨、空方道氏" in prompt
    assert "不得建立SHORT setup" in prompt
    assert "large_trend.classification必須為盤整" in prompt
    assert "background_quadrant必須為UNDEFINED" in prompt
    assert "這不限制working_quadrant" in prompt


def test_program_candidate_selection_filters_short_before_recency_priority() -> None:
    long_candidate = {
        "setup_key": "long-q4",
        "direction": "LONG",
        "candidate_source": "CONFIRMED_PULLBACK_ENDPOINT_N2",
        "first_seen_at": "2026-08-11T09:10:00+08:00",
    }
    newer_short_candidate = {
        "setup_key": "short-yizhi",
        "direction": "SHORT",
        "candidate_source": "YIZHI_EARLY_BREAKOUT",
        "first_seen_at": "2026-08-11T09:11:00+08:00",
    }

    both = _select_program_entry_candidate(long_candidate, newer_short_candidate)
    long_only = _select_program_entry_candidate(
        long_candidate,
        newer_short_candidate,
        allowed_directions={"LONG"},
    )

    assert both["setup_key"] == "short-yizhi"
    assert long_only["setup_key"] == "long-q4"


def test_long_only_semantics_accept_bearish_market_diagnosis_while_flat() -> None:
    analysis = _flat_action()
    analysis.update(
        {
            "message_direction": "BEAR",
            "large_trend": {"classification": "偏空"},
            "current_trend": {"classification": "空方延續"},
        }
    )
    memory = {"thesis_bias": "BEAR", "active_setups": []}

    _validate_trade_direction_policy(
        analysis,
        memory,
        ledger=_policy_ledger(),
        position={"status": "FLAT"},
        entry_gate={"status": "NONE"},
    )


@pytest.mark.parametrize(
    ("analysis", "memory", "position", "entry_gate", "error"),
    [
        (
            _flat_action("SHORT"),
            {"active_setups": []},
            {"status": "FLAT"},
            {"status": "NONE"},
            "action.direction不得為SHORT",
        ),
        (
            _flat_action(),
            {"active_setups": [{"setup_key": "short-q4", "direction": "SHORT"}]},
            {"status": "FLAT"},
            {"status": "NONE"},
            "不得保存SHORT setup",
        ),
        (
            _flat_action(),
            {"active_setups": []},
            {"status": "SHORT"},
            {"status": "NONE"},
            "不得承接SHORT持倉",
        ),
        (
            _flat_action(),
            {"active_setups": []},
            {"status": "FLAT"},
            {"status": "ENTRY_ELIGIBLE", "direction": "SHORT"},
            "不得產生SHORT進場資格",
        ),
    ],
)
def test_long_only_semantics_fail_closed_on_short_execution_state(
    analysis: dict[str, object],
    memory: dict[str, object],
    position: dict[str, object],
    entry_gate: dict[str, object],
    error: str,
) -> None:
    with pytest.raises(SemanticReplayError, match=error):
        _validate_trade_direction_policy(
            analysis,
            memory,
            ledger=_policy_ledger(),
            position=position,
            entry_gate=entry_gate,
        )


def test_bidirectional_policy_preserves_existing_short_execution_behavior() -> None:
    _validate_trade_direction_policy(
        _flat_action("SHORT"),
        {"active_setups": [{"setup_key": "short-q4", "direction": "SHORT"}]},
        ledger=_policy_ledger("BOTH"),
        position={"status": "SHORT"},
        entry_gate={"status": "ENTRY_ELIGIBLE", "direction": "SHORT"},
    )


def test_manifest_rejects_unknown_trade_direction_policy(tmp_path: Path) -> None:
    raw = V33_MANIFEST.read_text(encoding="utf-8").replace(
        '"trade_direction_policy": "LONG_ONLY"',
        '"trade_direction_policy": "LONG_AND_SHORT_SOMETIMES"',
    )
    path = tmp_path / "rule-manifest.json"
    path.write_text(raw, encoding="utf-8")

    with pytest.raises(ReplayRuleError, match="trade_direction_policy"):
        load_rule_package(path)


def test_long_only_requires_ai_hybrid_authority(tmp_path: Path) -> None:
    raw = V33_MANIFEST.read_text(encoding="utf-8").replace(
        '"analysis_mode": "AI_HYBRID"',
        '"analysis_mode": "PROGRAM_ONLY"',
    ).replace(
        '"decision_authority": "AI_WITH_PROGRAM_GUARDRAILS"',
        '"decision_authority": "PROGRAM"',
    )
    path = tmp_path / "rule-manifest.json"
    path.write_text(raw, encoding="utf-8")

    with pytest.raises(ReplayRuleError, match="LONG_ONLY"):
        load_rule_package(path)


def test_long_only_bear_upgrade_distinguishes_structure_cross_from_dow_break() -> None:
    ledger = {
        **_policy_ledger(),
        "pivots": [
            {"id": "parent", "kind": "HIGH", "bar_time": "2026-08-11T09:15:00+08:00", "price": 44992},
            {"id": "absorbed", "kind": "HIGH", "bar_time": "2026-08-11T09:24:00+08:00", "price": 44916},
            {"id": "boundary", "kind": "LOW", "bar_time": "2026-08-11T09:26:00+08:00", "price": 44775},
        ],
        "working_pivots": [
            {"id": "replacement", "kind": "HIGH", "bar_time": "2026-08-11T09:29:00+08:00", "price": 44929},
        ],
        "defenses": [],
        "structure_events": [
            {
                "id": "upgrade",
                "event_type": "GRADE_UPGRADE",
                "direction": "BEAR",
                "parent_origin_pivot_id": "parent",
                "absorbed_defense_pivot_id": "absorbed",
                "reclaimed_boundary_pivot_id": "boundary",
                "replacement_defense_pivot_id": "replacement",
                "parent_origin_price": 44992,
                "absorbed_defense_price": 44916,
                "reclaimed_boundary_price": 44775,
                "replacement_defense_price": 44929,
            }
        ],
    }

    line = _v3_structure_event_line({"structure_event_ref": "upgrade"}, ledger)

    assert line == (
        "升級：原小級空方結構09:24高44,916點雖被09:29高44,929點穿越，但"
        "父級09:15高44,992點守住；本輪收盤跌破09:26低44,775點，空方級數升級，"
        "09:29高44,929點成大級防線候選。"
    )
    assert "小級防線09:24高44,916點失守" not in line


def test_long_only_ai_exit_waits_for_next_bar_open() -> None:
    position = {
        "version": 2,
        "as_of": "2026-08-11T09:52:00+08:00",
        "status": "LONG",
        "entry_time": "2026-08-11T09:51:00+08:00",
        "entry_price": 44836.0,
        "stop_price": 44676.8,
        "direction": "LONG",
        "active_setup_key": "long-copy",
        "pending_entry": None,
        "pending_exit": None,
    }
    analysis = {"action": {"position_action": "EXIT", "direction": "NONE"}}

    audit = derive_analysis_exit_audit(
        position,
        analysis,
        as_of="2026-08-11T09:52:00+08:00",
    )
    queued = schedule_pending_exit(
        position,
        analysis,
        audit,
        as_of="2026-08-11T09:52:00+08:00",
    )

    assert queued["status"] == "LONG"
    assert queued["pending_exit"]["eligible_from"].endswith("09:53:00+08:00")

    exited, event = fill_pending_exit(
        queued,
        [
            {
                "time": "2026-08-11T09:53:00+08:00",
                "open": 44767.0,
            }
        ],
        as_of="2026-08-11T09:53:00+08:00",
    )

    assert exited["status"] == "FLAT"
    assert event is not None
    assert event["event_type"] == "EXIT_FILLED"
    assert event["fill_price"] == 44767.0
    assert event["reason_code"] == "AI_COURSE_EXIT"


def test_long_only_ai_selected_bear_upgrade_rejects_inconsistent_trend_without_rewrite() -> None:
    ledger = {
        **_policy_ledger(),
        "pivots": [
            {
                "id": "parent",
                "kind": "HIGH",
                "bar_time": "2026-08-11T09:15:00+08:00",
                "price": 44992.0,
            },
            {
                "id": "replacement",
                "kind": "HIGH",
                "bar_time": "2026-08-11T09:29:00+08:00",
                "price": 44929.0,
            },
        ],
        "working_pivots": [],
        "anchor_lifecycle": {
            "background_anchor": None,
            "dow_context": {"large_state": "UNDEFINED"},
        },
        "structure_events": [
            {
                "id": "bear-upgrade",
                "event_type": "GRADE_UPGRADE",
                "direction": "BEAR",
                "from_level": "SMALL",
                "to_level": "LARGE",
                "first_seen_at": "2026-08-11T09:36:00+08:00",
                "parent_origin_pivot_id": "parent",
                "parent_origin_price": 44992.0,
                "replacement_defense_pivot_id": "replacement",
                "replacement_defense_price": 44929.0,
            }
        ],
    }
    reading = {
        "structure_event_ref": "bear-upgrade",
        "controlling_grade": "LARGE",
        "background_trend_dynamics": "INCREASING",
    }

    ai_trend = {
        "classification": "偏多但回檔",
        "details": ["錯誤地把小級反彈當成大級多方背景。"],
    }

    with pytest.raises(SemanticReplayError, match="不會代替AI改寫"):
        _long_only_promoted_large_trend(
            ai_trend,
            reading,
            ledger=ledger,
        )

    assert ai_trend["classification"] == "偏多但回檔"
    assert ai_trend["details"] == ["錯誤地把小級反彈當成大級多方背景。"]


def test_long_only_ai_selected_upgrade_keeps_valid_ai_wording_unchanged() -> None:
    ledger = {
        **_policy_ledger(),
        "structure_events": [
            {
                "id": "bear-upgrade",
                "event_type": "GRADE_UPGRADE",
                "direction": "BEAR",
            }
        ],
    }
    ai_trend = {
        "classification": "強勢偏空",
        "details": ["AI依課程判斷這次空方結構升級有效。"],
    }

    result = _long_only_promoted_large_trend(
        ai_trend,
        {
            "structure_event_ref": "bear-upgrade",
            "controlling_grade": "LARGE",
            "background_trend_dynamics": "INCREASING",
        },
        ledger=ledger,
    )

    assert result == ai_trend


def test_long_only_ai_selected_upgrade_derives_same_direction_strength_label() -> None:
    ledger = {
        **_policy_ledger(),
        "structure_events": [
            {
                "id": "bull-upgrade",
                "event_type": "GRADE_UPGRADE",
                "direction": "BULL",
            }
        ],
    }
    ai_trend = {
        "classification": "偏多但回檔",
        "details": ["AI已採用多方升級，Q1趨勢性增強。"],
    }

    result = _long_only_promoted_large_trend(
        ai_trend,
        {
            "structure_event_ref": "bull-upgrade",
            "controlling_grade": "LARGE",
            "background_trend_dynamics": "INCREASING",
        },
        ledger=ledger,
    )

    assert result["classification"] == "強勢偏多"
    assert result["details"] == ai_trend["details"]


def test_long_only_unselected_program_upgrade_does_not_rewrite_ai_trend() -> None:
    ledger = {
        **_policy_ledger(),
        "structure_events": [
            {
                "id": "bear-upgrade-candidate",
                "event_type": "GRADE_UPGRADE",
                "direction": "BEAR",
                "from_level": "SMALL",
                "to_level": "LARGE",
                "first_seen_at": "2026-08-11T09:36:00+08:00",
            }
        ],
    }
    ai_trend = {
        "classification": "盤整",
        "details": ["AI判定該程式候選仍不足以取得大級控制意義。"],
    }

    result = _long_only_promoted_large_trend(
        ai_trend,
        {
            "structure_event_ref": None,
            "controlling_grade": "SMALL",
            "background_trend_dynamics": "UNCLEAR",
        },
        ledger=ledger,
    )

    assert result == ai_trend


def test_long_only_promoted_grade_outranks_opposite_child_for_entry_alignment() -> None:
    ledger = {
        **_policy_ledger(),
        "structure_events": [
            {
                "id": "bear-upgrade",
                "event_type": "GRADE_UPGRADE",
                "direction": "BEAR",
                "from_level": "SMALL",
                "to_level": "LARGE",
                "first_seen_at": "2026-08-11T09:36:00+08:00",
            }
        ],
        "anchor_lifecycle": {
            "background_anchor": None,
            "child_anchor": {
                "status": "ACTIVE",
                "direction": "BULL",
                "defense": {"state": "ACTIVE"},
            },
            "dow_context": {"large_state": "UNDEFINED"},
        },
    }

    aligned = _is_highest_active_anchor_aligned_entry(
        ledger=ledger,
        entry_gate={"direction": "LONG"},
    )

    assert aligned is False


def test_long_only_explicit_null_anchor_ref_is_not_silently_restored() -> None:
    ledger = {
        **_policy_ledger(),
        "anchor_lifecycle": {
            "background_anchor": None,
            "dow_context": {"large_state": "UNDEFINED"},
        },
        "structure_events": [
            {
                "id": "bear-upgrade",
                "event_type": "GRADE_UPGRADE",
                "direction": "BEAR",
                "from_level": "SMALL",
                "to_level": "LARGE",
                "first_seen_at": "2026-08-11T09:36:00+08:00",
            }
        ],
    }

    assert (
        _long_only_persisted_large_anchor_ref(
            None,
            "L-existing-bear-anchor",
            ledger=ledger,
        )
        is None
    )
    assert (
        _long_only_persisted_large_anchor_ref(
            "L-new-explicit-anchor",
            "L-existing-bear-anchor",
            ledger=ledger,
        )
        == "L-new-explicit-anchor"
    )
    assert (
        _long_only_persisted_large_anchor_ref(
            None,
            "L-existing-bear-anchor",
            ledger=_policy_ledger("BOTH"),
        )
        is None
    )


def test_long_only_previous_large_anchor_requires_unsuperseded_owned_promotion() -> None:
    previous_memory = {
        "session_key": "2026-08-11:DAY",
        "structure_control": {
            "controlling_grade": "LARGE",
            "active_large_anchor_ref": "L-adopted",
        },
    }
    upgrade = {
        "id": "S-upgrade",
        "event_type": "GRADE_UPGRADE",
        "direction": "BULL",
        "from_level": "SMALL",
        "to_level": "LARGE",
        "source_anchor_id": "L-adopted",
    }
    ledger = {
        **_policy_ledger(),
        "session_key": "2026-08-11:DAY",
        "structure_events": [upgrade],
    }
    refs = {"legs": {"L-adopted"}}

    assert _previous_ai_hybrid_large_anchor_ref(
        previous_memory,
        ledger=ledger,
        refs=refs,
    ) == "L-adopted"

    released = {
        **ledger,
        "structure_events": [
            upgrade,
            {
                "id": "S-down",
                "event_type": "GRADE_DOWNGRADE",
                "source_upgrade_event_id": "S-upgrade",
                "source_anchor_id": "L-adopted",
            },
        ],
    }
    assert _previous_ai_hybrid_large_anchor_ref(
        previous_memory,
        ledger=released,
        refs=refs,
    ) is None


def test_long_only_large_grade_authority_requires_adopted_anchor_identity() -> None:
    ledger = {
        **_policy_ledger(),
        "structure_events": [
            {
                "id": "S-upgrade",
                "event_type": "GRADE_UPGRADE",
                "direction": "BULL",
                "from_level": "SMALL",
                "to_level": "LARGE",
                "source_anchor_id": "L-adopted",
            }
        ],
    }

    assert not _large_grade_upgrade_quadrant_authority(
        {
            "controlling_grade": "LARGE",
            "large_anchor_ref": None,
            "structure_event_ref": None,
        },
        ledger=ledger,
    )
    assert _large_grade_upgrade_quadrant_authority(
        {
            "controlling_grade": "LARGE",
            "large_anchor_ref": "L-adopted",
            "structure_event_ref": "S-upgrade",
        },
        ledger=ledger,
    )


def test_long_only_upgrade_event_validates_atomic_analysis_and_memory_choice() -> None:
    ledger = {
        **_policy_ledger(),
        "anchor_control": {"active_background_anchor_ref": None},
        "structure_events": [
            {
                "id": "new-bear-upgrade",
                "event_type": "GRADE_UPGRADE",
                "direction": "BEAR",
                "source_anchor_id": "L-new-bear-anchor",
            }
        ],
    }
    analysis = {
        "message_type": "STRUCTURE_UPGRADE",
        "course_reading": {
            "structure_event_ref": "new-bear-upgrade",
            "large_anchor_ref": "L-new-bear-anchor",
            "controlling_grade": "LARGE",
        },
    }
    memory = {
        "structure_control": {
            "active_large_anchor_ref": "L-new-bear-anchor",
            "controlling_grade": "LARGE",
        }
    }

    selected = _canonicalize_long_only_upgrade_references(
        analysis,
        ledger=ledger,
        memory=memory,
    )

    assert selected == "L-new-bear-anchor"
    assert analysis["course_reading"]["large_anchor_ref"] == "L-new-bear-anchor"
    assert analysis["course_reading"]["controlling_grade"] == "LARGE"
    assert memory["structure_control"]["active_large_anchor_ref"] == "L-new-bear-anchor"
    assert memory["structure_control"]["controlling_grade"] == "LARGE"


def test_ai_hybrid_accepts_current_upgrade_source_even_when_not_in_generic_leg_refs() -> None:
    ledger = {
        **_policy_ledger(),
        "anchor_control": {
            "active_background_anchor_ref": None,
            "active_child_anchor_ref": "ANCHOR-child",
            "working_leg_ref": None,
            "reverse_anchor_candidate_ref": None,
            "large_defense_ref": None,
            "small_defense_ref": None,
        },
        "anchor_records": [{"id": "ANCHOR-child"}],
        "structure_events": [
            {
                "id": "S-current-upgrade",
                "event_type": "GRADE_UPGRADE",
                "direction": "BULL",
                "from_level": "SMALL",
                "to_level": "LARGE",
                "source_anchor_id": "L-upgrade-source-not-in-current-legs",
            }
        ],
    }
    reading = {
        "large_anchor_ref": "L-upgrade-source-not-in-current-legs",
        "small_anchor_ref": "ANCHOR-child",
        "working_anchor_ref": None,
        "reverse_anchor_candidate_ref": None,
        "large_defense_ref": None,
        "small_defense_ref": None,
        "structure_event_ref": "S-current-upgrade",
        "controlling_grade": "LARGE",
        "grade_relation": "ALIGNED",
        "background_quadrant": "Q1",
        "working_quadrant": "Q1",
        "primary_quadrant_candidate": "Q1",
        "secondary_quadrant_candidate": "Q4",
        "background_trend_dynamics": "INCREASING",
        "background_volatility_dynamics": "EXPANDING",
        "working_trend_dynamics": "INCREASING",
        "working_volatility_dynamics": "EXPANDING",
        "focus_methods": ["TAIJI", "DOW"],
        "cclass_mode": "TAIJI_ORDERED",
        "taiji": "升級後的多方父代與新複製維持有序。",
        "yizhi": "尚未由異常動能接管。",
        "left_right": "多方右側結構已取得控制。",
        "dow": "新升級來源仍待建立大級防線。",
        "x_stage": "ENTRY_EXECUTION",
        "x_process": "升級與進場資格同時出現，先依執行契約處理。",
        "primary_lens": "多方結構升級",
        "main_strategy": "多方假跌破收復",
        "setup_stage": "ENTRY_ELIGIBLE",
        "strategy_reason": ["本輪升級事件與來源錨為同一組因果證據。"],
    }

    validated = _reading_v3(reading, ledger=ledger, ai_generated=True)

    assert validated["structure_event_ref"] == "S-current-upgrade"
    assert validated["large_anchor_ref"] == "L-upgrade-source-not-in-current-legs"


def test_long_only_upgrade_prefers_installed_background_anchor_over_source_leg() -> None:
    ledger = {
        **_policy_ledger(),
        "anchor_control": {"active_background_anchor_ref": "ANCHOR-background"},
        "structure_events": [
            {
                "id": "new-bull-upgrade",
                "event_type": "GRADE_UPGRADE",
                "direction": "BULL",
                "source_anchor_id": "L-upgrade-source",
            }
        ],
    }
    analysis = {
        "message_type": "STRUCTURE_UPGRADE",
        "course_reading": {
            "structure_event_ref": "new-bull-upgrade",
            "large_anchor_ref": "ANCHOR-background",
            "controlling_grade": "LARGE",
        },
    }
    memory = {
        "structure_control": {
            "active_large_anchor_ref": "ANCHOR-background",
            "controlling_grade": "LARGE",
        }
    }

    selected = _canonicalize_long_only_upgrade_references(
        analysis,
        ledger=ledger,
        memory=memory,
    )

    assert selected == "ANCHOR-background"


def test_long_only_upgrade_rejects_source_leg_after_background_is_installed() -> None:
    ledger = {
        **_policy_ledger(),
        "anchor_control": {"active_background_anchor_ref": "ANCHOR-background"},
        "structure_events": [
            {
                "id": "new-bull-upgrade",
                "event_type": "GRADE_UPGRADE",
                "direction": "BULL",
                "source_anchor_id": "L-upgrade-source",
            }
        ],
    }
    analysis = {
        "message_type": "STRUCTURE_UPGRADE",
        "course_reading": {
            "structure_event_ref": "new-bull-upgrade",
            "large_anchor_ref": "L-upgrade-source",
            "controlling_grade": "LARGE",
        },
    }

    with pytest.raises(SemanticReplayError, match="作用中背景大錨"):
        _canonicalize_long_only_upgrade_references(analysis, ledger=ledger)


def test_long_only_upgrade_event_rejects_mismatch_instead_of_rewriting() -> None:
    ledger = {
        **_policy_ledger(),
        "anchor_control": {"active_background_anchor_ref": None},
        "structure_events": [
            {
                "id": "new-bear-upgrade",
                "event_type": "GRADE_UPGRADE",
                "direction": "BEAR",
                "source_anchor_id": "L-new-bear-anchor",
            }
        ],
    }
    analysis = {
        "message_type": "STRUCTURE_UPGRADE",
        "course_reading": {
            "structure_event_ref": "new-bear-upgrade",
            "large_anchor_ref": "L-old-bear-anchor",
            "controlling_grade": "SMALL",
        },
    }

    with pytest.raises(SemanticReplayError, match="不會代替AI改寫"):
        _canonicalize_long_only_upgrade_references(analysis, ledger=ledger)

    assert analysis["course_reading"]["large_anchor_ref"] == "L-old-bear-anchor"
    assert analysis["course_reading"]["controlling_grade"] == "SMALL"


def test_long_only_same_close_sibling_event_keeps_ai_selected_reference() -> None:
    ledger = {
        **_policy_ledger(),
        "anchor_control": {"active_background_anchor_ref": None},
        "structure_events": [
            {
                "id": "bull-upgrade",
                "event_type": "GRADE_UPGRADE",
                "direction": "BULL",
                "first_seen_at": "2026-08-11T10:28:00+08:00",
                "source_anchor_id": "L-bull-upgrade",
            },
            {
                "id": "bull-false-break",
                "event_type": "FALSE_BREAK_RECLAIM",
                "direction": "BULL",
                "first_seen_at": "2026-08-11T10:28:00+08:00",
            },
        ],
    }
    reading = {"structure_event_ref": "bull-false-break"}
    evidence = [
        {"event_id": "bull-upgrade"},
        {"event_id": "bull-false-break"},
    ]

    selected = _canonicalize_long_only_latest_material_event_ref(
        reading,
        ledger=ledger,
        evidence_events=evidence,
    )

    assert selected == "bull-false-break"
    assert reading["structure_event_ref"] == "bull-false-break"


def test_long_only_false_break_plan_uses_causal_pivot_as_behavior_checkpoint() -> None:
    bars = [
        {"time": "2026-08-11T10:18:00+08:00", "open": 101, "high": 103, "low": 98, "close": 100},
        {"time": "2026-08-11T10:19:00+08:00", "open": 100, "high": 104, "low": 98, "close": 102},
        {"time": "2026-08-11T10:20:00+08:00", "open": 100, "high": 105, "low": 99, "close": 101},
        {"time": "2026-08-11T10:21:00+08:00", "open": 101, "high": 110, "low": 100, "close": 108},
        {"time": "2026-08-11T10:22:00+08:00", "open": 108, "high": 109, "low": 103, "close": 104},
        {"time": "2026-08-11T10:23:00+08:00", "open": 104, "high": 106, "low": 98, "close": 99},
        {"time": "2026-08-11T10:24:00+08:00", "open": 99, "high": 107, "low": 95, "close": 105},
    ]
    events = [
        {
            "id": "false-break",
            "event_type": "FALSE_BREAK_RECLAIM",
            "direction": "BULL",
            "level_price": 100,
            "breach_extreme": 95,
            "breach_time": "2026-08-11T10:23:00+08:00",
            "reclaim_close": 105,
            "first_seen_at": "2026-08-11T10:24:00+08:00",
        }
    ]

    baseline = _false_break_arm_candidate(
        events,
        expected=datetime.fromisoformat("2026-08-11T10:24:00+08:00"),
        bars=bars,
    )
    long_only = _false_break_arm_candidate(
        events,
        expected=datetime.fromisoformat("2026-08-11T10:24:00+08:00"),
        bars=bars,
        include_causal_checkpoint=True,
    )

    assert baseline is not None
    assert long_only is not None
    assert [item["role"] for item in baseline["behavior_obstacles"]] == ["TRIGGER"]
    assert baseline["trigger_level"] == 107.0
    assert baseline["reclaim_boundary_level"] == 100.0
    assert long_only["behavior_obstacles"][-1] == {
        "price": 110.0,
        "role": "CHECKPOINT",
        "source_time": "2026-08-11T10:21:00+08:00",
        "source_field": "high",
    }
    assert long_only["behavior_policy"] == "FALSE_BREAK_RECLAIM_CAUSAL_CHECKPOINT"


def test_false_break_plan_freezes_stop_and_facts_at_reclaim_close() -> None:
    bars = [
        {
            "time": f"2026-08-11T10:{minute:02d}:00+08:00",
            "open": 100,
            "high": 101,
            "low": 99,
            "close": 100,
        }
        for minute in range(18)
    ]
    bars.extend(
        [
            {
                "time": "2026-08-11T10:18:00+08:00",
                "open": 100,
                "high": 101,
                "low": 95,
                "close": 99,
            },
            {
                "time": "2026-08-11T10:19:00+08:00",
                "open": 99,
                "high": 107,
                "low": 98,
                "close": 105,
            },
        ]
    )
    event = {
        "id": "false-break-frozen",
        "event_type": "FALSE_BREAK_RECLAIM",
        "direction": "BULL",
        "level_price": 100,
        "breach_extreme": 95,
        "breach_time": "2026-08-11T10:18:00+08:00",
        "reclaim_close": 105,
        "first_seen_at": "2026-08-11T10:19:00+08:00",
    }
    future_wide_bar = {
        "time": "2026-08-11T10:20:00+08:00",
        "open": 105,
        "high": 200,
        "low": 101,
        "close": 106,
    }

    at_reclaim = _false_break_arm_candidate(
        [event],
        expected=datetime.fromisoformat("2026-08-11T10:19:00+08:00"),
        bars=bars,
    )
    reviewed_later = _false_break_arm_candidate(
        [event],
        expected=datetime.fromisoformat("2026-08-11T10:20:00+08:00"),
        bars=[*bars, future_wide_bar],
    )

    assert at_reclaim is not None and reviewed_later is not None
    assert reviewed_later["setup_key"] == at_reclaim["setup_key"]
    assert reviewed_later["facts_cutoff"] == at_reclaim["facts_cutoff"]
    assert reviewed_later["stop_buffer_points"] == at_reclaim["stop_buffer_points"]
    assert reviewed_later["stop_price"] == at_reclaim["stop_price"]
    assert reviewed_later["facts_hash"] == at_reclaim["facts_hash"]


def test_long_only_false_break_checkpoint_is_managed_by_closed_bar_extreme() -> None:
    position = {
        "status": "LONG",
        "entry_time": "2026-08-11T10:29:00+08:00",
        "entry_price": 44833.0,
        "stop_price": 44700.5,
        "behavior_plan": {
            "initial_stop_price": 44700.5,
            "max_wait_bars": 3,
            "trigger_level": 44757.0,
            "policy": "FALSE_BREAK_RECLAIM_CAUSAL_CHECKPOINT",
            "obstacles": [
                {"price": 44757.0, "role": "TRIGGER"},
                {"price": 44840.0, "role": "CHECKPOINT"},
            ],
        },
    }
    bars = [
        {"time": "2026-08-11T10:29:00+08:00", "open": 44833, "high": 44839, "low": 44796, "close": 44811},
        {"time": "2026-08-11T10:30:00+08:00", "open": 44810, "high": 44855, "low": 44807, "close": 44825},
        {"time": "2026-08-11T10:31:00+08:00", "open": 44821, "high": 44870, "low": 44803, "close": 44839},
    ]

    audit = derive_position_behavior_audit(
        position,
        bars,
        as_of="2026-08-11T10:31:00+08:00",
    )

    assert audit["status"] == "ACHIEVED"
    assert audit["achieved_at"].endswith("10:31:00+08:00")
    assert audit["checkpoint_price"] == 44866.125
    assert audit["checkpoint_label"] == "進場後最低0.25R行為檢查"
    assert audit["checkpoint_evidence"] == "CLOSED_BAR_FAVORABLE_EXTREME"
