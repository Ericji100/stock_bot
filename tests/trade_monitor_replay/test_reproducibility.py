import json
from pathlib import Path

from trade_monitor_replay.reproducibility import (
    build_execution_decision_trace,
    build_program_fingerprint,
    compare_program_fingerprints,
    verify_triplicate_runs,
)


AT = "2026-08-21T09:00:00+08:00"


def _write_run(
    root: Path,
    *,
    prose: str,
    stop: float = 90.0,
    quadrant: str = "Q4",
    grade_relation: str = "SAME_DIRECTION",
    bull_probability: int = 60,
    event_prose: str | None = None,
) -> Path:
    run = root
    analysis = run / "analysis"
    analysis.mkdir(parents=True)
    manifest = {
        "run_id": run.name,
        "status": "completed",
        "target_date": "2026-08-21",
        "instrument": "TMF",
        "expiry_month": "202609",
        "source_sha256": {"2026-08-21": "a" * 64},
        "rule_version": "rules-v1",
        "rule_sha256": "b" * 64,
        "schema_sha256": "c" * 64,
        "execution_version": "engine-v1",
        "selected_bar_times": [AT],
        "next_day_index": 1,
    }
    (run / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    validated = {
        "analysis": {
            "notification_reason": prose,
            "course_reading": {
                "background_quadrant": quadrant,
                "working_quadrant": quadrant,
                "background_trend_dynamics": "INCREASING",
                "background_volatility_dynamics": "CONTRACTING",
                "working_trend_dynamics": "INCREASING",
                "working_volatility_dynamics": "CONTRACTING",
                "main_strategy_family": "Q4",
                "grade_relation": grade_relation,
                "cclass_mode": "TAIJI_ORDERED",
                "x_stage": "ENTRY_EXECUTION",
                "setup_stage": "ENTRY_ELIGIBLE",
            },
            "scenario": {
                "bull_probability": bull_probability,
                "range_probability": 25,
                "bear_probability": 15,
            },
            "action": {
                "position_action": "ENTER",
                "direction": "LONG",
                "entry_role": "INITIAL",
                "setup_key": "q4-long",
                "stop_price": stop,
                "max_wait_bars": 5,
                "entry_rejection_reason": "NONE",
            },
        }
    }
    checkpoint = {
        "entry_eligibility": {
            "status": "ENTRY_ELIGIBLE",
            "event_id": "entry-1",
            "setup_key": "q4-long",
            "signal_time": AT,
            "eligible_from": "2026-08-21T09:01:00+08:00",
            "expires_at": "2026-08-21T09:03:00+08:00",
            "decision_authority": "AI_HYBRID",
            "required_candidate_source": "Q4_AGGRESSIVE_PULLBACK_REVERSAL",
            "required_entry_strategy": "Q4_AGGRESSIVE_PULLBACK_REVERSAL",
            "required_facts_hash": "FACTS-test",
            "required_stop_price": stop,
            "required_entry_rejection_reason": "NONE",
        },
        "position_state": {
            "status": "FLAT",
            "entry_time": None,
            "entry_price": None,
            "stop_price": None,
            "direction": None,
            "active_setup_key": None,
            "last_stop_time": None,
            "reentry_count": 0,
            "last_action": "ENTRY_QUEUED",
            "pending_entry": {
                "event_id": "entry-1",
                "setup_key": "q4-long",
                "direction": "LONG",
                "entry_role": "INITIAL",
                "signal_time": AT,
                "eligible_from": "2026-08-21T09:01:00+08:00",
                "expires_at": "2026-08-21T09:03:00+08:00",
                "stop_price": stop,
                "behavior_max_wait_bars": 5,
            },
        },
        "constitution_state": {
            "trading_day": "2026-08-21",
            "simulated_entry_count": 0,
            "consecutive_simulated_stops": 0,
            "cooldown_until": None,
            "requalification_required": False,
            "simulated_position": None,
            "setup_entry_counts": {},
        },
    }
    (analysis / "day-20260821-0900-validated.json").write_text(
        json.dumps(validated, ensure_ascii=False), encoding="utf-8"
    )
    (analysis / "day-20260821-0900-state-checkpoint.json").write_text(
        json.dumps(checkpoint, ensure_ascii=False), encoding="utf-8"
    )
    if event_prose is not None:
        event = {
            "event_type": "ENTRY_FILLED",
            "event_id": "entry-1",
            "setup_key": "q4-long",
            "direction": "LONG",
            "entry_role": "INITIAL",
            "signal_time": AT,
            "fill_time": AT,
            "fill_price": 100.0,
            "stop_price": stop,
            "trigger": event_prose,
            "expected_behavior": event_prose,
            "behavior_obstacles": [
                {
                    "role": "CHECKPOINT",
                    "price": 110.0,
                    "label": event_prose,
                    "reaction": event_prose,
                }
            ],
            "candidate_source": "Q4_AGGRESSIVE_PULLBACK_REVERSAL",
            "entry_strategy": "Q4_AGGRESSIVE_PULLBACK_REVERSAL",
            "facts_hash": "FACTS-test",
            "main_strategy_family": "Q4",
        }
        (run / "execution-events.jsonl").write_text(
            json.dumps(event, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    return run


def test_model_prose_does_not_change_program_fingerprint(tmp_path: Path) -> None:
    left = _write_run(tmp_path / "model-a", prose="模型A的說明。")
    right = _write_run(tmp_path / "model-b", prose="模型B使用完全不同措辭。")
    compared = compare_program_fingerprints(left, right)
    assert compared["ok"] is True
    assert compared["status"] == "identical"


def test_program_stop_change_changes_fingerprint(tmp_path: Path) -> None:
    left = _write_run(tmp_path / "run-a", prose="一樣。", stop=90.0)
    right = _write_run(tmp_path / "run-b", prose="一樣。", stop=91.0)
    assert build_program_fingerprint(left)["program_fingerprint_sha256"] != (
        build_program_fingerprint(right)["program_fingerprint_sha256"]
    )


def test_program_quadrant_change_changes_fingerprint(tmp_path: Path) -> None:
    left = _write_run(tmp_path / "run-a", prose="一樣。", quadrant="Q4")
    right = _write_run(tmp_path / "run-b", prose="一樣。", quadrant="Q1")
    assert build_program_fingerprint(left)["program_fingerprint_sha256"] != (
        build_program_fingerprint(right)["program_fingerprint_sha256"]
    )


def test_pair_comparison_fails_closed_for_matching_partial_prefixes(tmp_path: Path) -> None:
    left = _write_run(tmp_path / "partial-a", prose="一樣。")
    right = _write_run(tmp_path / "partial-b", prose="一樣。")
    for run in (left, right):
        manifest_path = run / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["status"] = "running"
        manifest["next_day_index"] = 0
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    compared = compare_program_fingerprints(left, right)

    assert compared["ok"] is False
    assert compared["status"] == "incomplete_or_invalid"


def test_triplicate_requires_three_distinct_complete_same_spec_runs(tmp_path: Path) -> None:
    runs = [
        _write_run(tmp_path / f"run-{index}", prose=f"措辭{index}")
        for index in range(3)
    ]

    verified = verify_triplicate_runs(runs)

    assert verified["ok"] is True
    assert verified["status"] == "triplicate_identical"
    assert len(verified["run_ids"]) == 3
    assert len(verified["decision_trace_sha256"]) == 64
    assert verify_triplicate_runs(runs[:2])["status"] == "invalid_run_count"
    assert verify_triplicate_runs([runs[0], runs[0], runs[2]])["status"] == "duplicate_run"


def test_triplicate_reports_first_material_decision_difference(tmp_path: Path) -> None:
    runs = [
        _write_run(tmp_path / "run-a", prose="不同措辭A", stop=90.0),
        _write_run(tmp_path / "run-b", prose="不同措辭B", stop=90.0),
        _write_run(tmp_path / "run-c", prose="不同措辭C", stop=91.0),
    ]

    verified = verify_triplicate_runs(runs)

    assert verified["ok"] is False
    assert verified["status"] == "hard_fact_trace_mismatch"
    assert verified["first_difference"]["field"] == "entry_eligibility"


def test_execution_event_prose_does_not_change_decision_trace(tmp_path: Path) -> None:
    left = _write_run(tmp_path / "event-a", prose="分析A", event_prose="模型措辭A")
    right = _write_run(tmp_path / "event-b", prose="分析B", event_prose="完全不同措辭B")

    first = build_execution_decision_trace(left)
    second = build_execution_decision_trace(right)

    assert first["decision_trace_sha256"] == second["decision_trace_sha256"]


def test_triplicate_separates_stable_trade_decisions_from_course_state_drift(
    tmp_path: Path,
) -> None:
    runs = [
        _write_run(tmp_path / "run-a", prose="A", quadrant="Q4"),
        _write_run(tmp_path / "run-b", prose="B", quadrant="Q4"),
        _write_run(tmp_path / "run-c", prose="C", quadrant="Q1"),
    ]

    verified = verify_triplicate_runs(runs)

    assert verified["ok"] is True
    assert verified["status"] == "triplicate_trade_decisions_identical_course_state_varied"
    assert verified["course_state_consistent"] is False
    assert verified["first_course_difference"]["bar_time"] == AT


def test_course_trace_includes_grade_relation_and_scenario_weights(tmp_path: Path) -> None:
    runs = [
        _write_run(tmp_path / "run-a", prose="A"),
        _write_run(tmp_path / "run-b", prose="B"),
        _write_run(
            tmp_path / "run-c",
            prose="C",
            grade_relation="CONFLICT",
            bull_probability=55,
        ),
    ]

    verified = verify_triplicate_runs(runs)

    assert verified["ok"] is True
    assert verified["course_state_consistent"] is False
    differing = verified["first_course_difference"]
    assert differing["left"]["course_state"]["grade_relation"] == "SAME_DIRECTION"
    assert differing["right"]["course_state"]["grade_relation"] == "CONFLICT"


def test_hard_fact_trace_hashes_deterministic_artifacts(tmp_path: Path) -> None:
    left = _write_run(tmp_path / "run-a", prose="A")
    right = _write_run(tmp_path / "run-b", prose="B")
    (left / "bars.csv").write_text("time,close\n09:00,100\n", encoding="utf-8")
    (right / "bars.csv").write_text("time,close\n09:00,101\n", encoding="utf-8")

    first = build_execution_decision_trace(left)
    second = build_execution_decision_trace(right)

    assert first["decision_trace_sha256"] == second["decision_trace_sha256"]
    assert first["hard_fact_trace_sha256"] != second["hard_fact_trace_sha256"]


def test_execution_trace_distinguishes_wait_from_hold(tmp_path: Path) -> None:
    run = _write_run(tmp_path / "trace", prose="說明。")
    trace = build_execution_decision_trace(run)

    assert trace["trace"][0]["decision"] == "ENTER"


def test_flat_wait_ignores_non_executable_explanatory_setup_key(tmp_path: Path) -> None:
    runs = [
        _write_run(tmp_path / f"run-{index}", prose=f"說明{index}")
        for index in range(3)
    ]
    for index, run in enumerate(runs):
        validated_path = next((run / "analysis").glob("*-validated.json"))
        validated = json.loads(validated_path.read_text(encoding="utf-8"))
        validated["analysis"]["action"] = {
            "position_action": "NONE",
            "direction": "NONE",
            "setup_key": f"observed-setup-{index}",
            "stop_price": 90.0 + index,
            "max_wait_bars": 3 + index,
            "entry_rejection_reason": "NONE",
        }
        validated_path.write_text(
            json.dumps(validated, ensure_ascii=False), encoding="utf-8"
        )

        checkpoint_path = next((run / "analysis").glob("*-state-checkpoint.json"))
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        checkpoint["entry_eligibility"] = {
            "status": "NONE",
            "event_id": None,
            "setup_key": None,
        }
        checkpoint["position_state"].update(
            {
                "status": "FLAT",
                "active_setup_key": None,
                "pending_entry": None,
            }
        )
        checkpoint_path.write_text(
            json.dumps(checkpoint, ensure_ascii=False), encoding="utf-8"
        )

    verified = verify_triplicate_runs(runs)

    assert verified["ok"] is True
    assert verified["status"] == "triplicate_identical"
    assert verified["course_state_consistent"] is True
    assert verified["decision_trace_sha256"]


def test_flat_wait_strategy_family_variation_is_course_state_not_trade_decision(
    tmp_path: Path,
) -> None:
    runs = [
        _write_run(tmp_path / f"run-{index}", prose=f"說明{index}")
        for index in range(3)
    ]
    for run, family in zip(runs, ("NONE", "Q4", "NONE"), strict=True):
        validated_path = next((run / "analysis").glob("*-validated.json"))
        validated = json.loads(validated_path.read_text(encoding="utf-8"))
        validated["analysis"]["course_reading"]["main_strategy_family"] = family
        validated["analysis"]["action"].update(
            {
                "position_action": "NONE",
                "direction": "NONE",
                "setup_key": None,
                "stop_price": None,
                "entry_rejection_reason": "NONE",
            }
        )
        validated_path.write_text(
            json.dumps(validated, ensure_ascii=False), encoding="utf-8"
        )

        checkpoint_path = next((run / "analysis").glob("*-state-checkpoint.json"))
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        checkpoint["entry_eligibility"] = {"status": "NONE"}
        checkpoint["position_state"].update(
            {"status": "FLAT", "active_setup_key": None, "pending_entry": None}
        )
        checkpoint_path.write_text(
            json.dumps(checkpoint, ensure_ascii=False), encoding="utf-8"
        )

    verified = verify_triplicate_runs(runs)

    assert verified["ok"] is True
    assert verified["status"] == (
        "triplicate_trade_decisions_identical_course_state_varied"
    )
    assert verified["course_state_consistent"] is False


def test_entry_eligible_wait_keeps_setup_key_strict(tmp_path: Path) -> None:
    left = _write_run(tmp_path / "left", prose="說明A")
    right = _write_run(tmp_path / "right", prose="說明B")
    for run, setup_key in ((left, "eligible-a"), (right, "eligible-b")):
        validated_path = next((run / "analysis").glob("*-validated.json"))
        validated = json.loads(validated_path.read_text(encoding="utf-8"))
        validated["analysis"]["action"].update(
            {
                "position_action": "NONE",
                "direction": "NONE",
                "setup_key": setup_key,
                "entry_rejection_reason": "HARD_OBSTACLE_TOO_CLOSE",
            }
        )
        validated_path.write_text(
            json.dumps(validated, ensure_ascii=False), encoding="utf-8"
        )

    first = build_execution_decision_trace(left)
    second = build_execution_decision_trace(right)

    assert first["decision_trace_sha256"] != second["decision_trace_sha256"]


def test_held_position_ignores_non_executable_max_wait_echo(tmp_path: Path) -> None:
    runs = [
        _write_run(tmp_path / f"run-{index}", prose=f"說明{index}")
        for index in range(3)
    ]
    for run, max_wait in zip(runs, (None, 3, 5), strict=True):
        validated_path = next((run / "analysis").glob("*-validated.json"))
        validated = json.loads(validated_path.read_text(encoding="utf-8"))
        validated["analysis"]["action"].update(
            {
                "position_action": "NONE",
                "direction": "NONE",
                "entry_role": "NOT_APPLICABLE",
                "setup_key": "held-setup",
                "stop_price": 90.0,
                "max_wait_bars": max_wait,
                "entry_rejection_reason": "NONE",
            }
        )
        validated_path.write_text(
            json.dumps(validated, ensure_ascii=False), encoding="utf-8"
        )

        checkpoint_path = next((run / "analysis").glob("*-state-checkpoint.json"))
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        checkpoint["entry_eligibility"] = {"status": "NONE"}
        checkpoint["position_state"].update(
            {
                "status": "LONG",
                "active_setup_key": "held-setup",
                "pending_entry": None,
            }
        )
        checkpoint_path.write_text(
            json.dumps(checkpoint, ensure_ascii=False), encoding="utf-8"
        )

    verified = verify_triplicate_runs(runs)

    assert verified["ok"] is True
    assert verified["status"] == "triplicate_identical"


def test_held_trade_family_comes_from_accepted_entry_not_later_ai_echo(
    tmp_path: Path,
) -> None:
    runs = [
        _write_run(
            tmp_path / f"run-{index}",
            prose=f"說明{index}",
            event_prose="同一個已成交Q4 setup",
        )
        for index in range(3)
    ]
    for run, family in zip(runs, ("Q4", "NONE", "Q2"), strict=True):
        validated_path = next((run / "analysis").glob("*-validated.json"))
        validated = json.loads(validated_path.read_text(encoding="utf-8"))
        validated["analysis"]["course_reading"]["main_strategy_family"] = family
        validated["analysis"]["action"].update(
            {
                "position_action": "NONE",
                "direction": "NONE",
                "entry_role": "NOT_APPLICABLE",
                "setup_key": "q4-long",
                "stop_price": 90.0,
            }
        )
        validated_path.write_text(
            json.dumps(validated, ensure_ascii=False), encoding="utf-8"
        )

        checkpoint_path = next((run / "analysis").glob("*-state-checkpoint.json"))
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        checkpoint["entry_eligibility"] = {"status": "NONE"}
        checkpoint["position_state"].update(
            {
                "status": "LONG",
                "active_setup_key": "q4-long",
                "pending_entry": None,
            }
        )
        checkpoint_path.write_text(
            json.dumps(checkpoint, ensure_ascii=False), encoding="utf-8"
        )

    verified = verify_triplicate_runs(runs)

    assert verified["ok"] is True
    assert verified["status"] == (
        "triplicate_trade_decisions_identical_course_state_varied"
    )


def test_v34_strict_comparison_requires_content_hash_identity(tmp_path: Path) -> None:
    left = _write_run(tmp_path / "v34-a", prose="一樣。")
    right = _write_run(tmp_path / "v34-b", prose="一樣。")
    for run in (left, right):
        manifest_path = run / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest.update(
            {
                "analysis_mode": "AI_HYBRID",
                "trade_direction_policy": "LONG_ONLY",
                "trade_setup_policy": "LONG_Q2_Q4_ONLY",
                "preopen_completed": True,
            }
        )
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    compared = compare_program_fingerprints(left, right)

    assert compared["ok"] is False
    assert compared["status"] == "incomplete_or_invalid"
    assert "execution_implementation_sha256" in compared["error"]
