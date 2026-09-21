import json
from pathlib import Path

import pytest

from trade_monitor_replay.performance import (
    _canonical_execution_events,
    _setup_family,
    account_trades,
    aggregate_stage_one_run_reports,
    load_run_report,
)


def at(minute):
    return f"2026-08-25T09:{minute:02d}:00+08:00"


def bar(minute, price):
    return {"bar_time":at(minute), "open":price, "high":price+2, "low":price-2, "close":price+1}


def fill(direction="LONG", price=44000):
    return {"event_type":"ENTRY_FILLED", "fill_time":at(1), "fill_price":price,
            "stop_price":price-20 if direction=="LONG" else price+20, "direction":direction, "setup_key":"s1"}


def test_next_open_not_seen_exit_close_and_costs():
    result = account_trades([bar(1,44000),bar(2,44020),bar(3,44025)], [fill()],
                            [{"bar_time":at(2),"action":"EXIT"}], cutoff=at(3),
                            fee_per_side=10,slippage_points_per_side=1)
    trade=result["trades"][0]
    assert trade["exit_price"]==44025
    assert trade["gross_points"]==25
    assert trade["net_ntd"]==pytest.approx(250-20-20-(44000+44025)*10*0.00002)
    assert trade["mfe_points"]==25  # not exit bar high 44027


def test_short_loss_and_drawdown():
    result=account_trades([bar(1,44000),bar(2,44020),bar(3,44030)],[fill("SHORT")],
                         [{"bar_time":at(2),"action":"STOP"}],cutoff=at(3),fee_per_side=0,slippage_points_per_side=0)
    assert result["gross_points"]==-30
    assert result["losses_after_cost"]==1
    assert result["closed_equity_max_drawdown_ntd"]==pytest.approx(-result["net_ntd"])


def test_cutoff_leaves_pending_exit_open_and_ignores_future():
    result=account_trades([bar(1,44000),bar(2,44020),bar(3,45000)],[fill()],
                         [{"bar_time":at(2),"action":"EXIT"}],cutoff=at(2),fee_per_side=10,slippage_points_per_side=1)
    assert result["closed_count"]==0
    assert result["open_positions"][0]["unrealized_points"]==21
    assert result["mean_net_per_closed_trade"] is None


def test_no_fill_no_trade():
    result=account_trades([bar(1,44000)],[],[],cutoff=at(1),fee_per_side=10,slippage_points_per_side=1)
    assert result["entry_count"]==0
    assert result["win_rate_after_cost"] is None


def test_missing_exit_is_not_fabricated():
    second=fill(); second["fill_time"]=at(3)
    with pytest.raises(ValueError,match="Overlapping"):
        account_trades([bar(1,44000),bar(3,44020)],[fill(),second],[],cutoff=at(3),fee_per_side=0,slippage_points_per_side=0)


def test_duplicate_fill_is_not_a_second_trade_and_fee_is_per_side():
    result=account_trades([bar(1,44000),bar(2,44020),bar(3,44025)],[fill(),fill()],
                         [{"bar_time":at(2),"action":"EXIT"}],cutoff=at(3),fee_per_side=12,slippage_points_per_side=0)
    assert result["entry_count"] == 1
    assert result["trades"][0]["fee_ntd"] == 24
    assert result["trades"][0]["net_ntd"] == pytest.approx(250-24-17.605)


def test_conflicting_duplicate_fill_must_be_audited():
    changed=fill(price=44010)
    with pytest.raises(ValueError,match="Conflicting"):
        account_trades([bar(1,44000)],[fill(),changed],[],cutoff=at(1),fee_per_side=12,slippage_points_per_side=0)


def test_programmatic_protective_stop_uses_stop_fill_not_later_open():
    stop = {
        "event_type": "STOP_FILLED",
        "event_id": "stop-1",
        "setup_key": "s1",
        "direction": "LONG",
        "trigger_time": at(2),
        "fill_time": at(2),
        "fill_price": 43990.5,
        "stop_price": 43990.5,
    }
    decisions = [
        {"bar_time": at(3), "action": "STOP", "stop_price": 43990.5, "reason": "已觸發"}
    ]
    result = account_trades(
        [bar(1, 44000), bar(2, 43990), bar(3, 44020)],
        [fill(), stop, dict(stop)],
        decisions,
        cutoff=at(3),
        fee_per_side=0,
        slippage_points_per_side=0,
    )

    trade = result["trades"][0]
    assert trade["exit_time"] == at(2)
    assert trade["exit_signal_time"] == at(2)
    assert trade["exit_price"] == 43990.5
    assert trade["gross_points"] == -9.5
    assert trade["exit_source"] == "PROGRAMMATIC_PROTECTIVE_STOP"


def test_programmatic_behavior_exit_uses_locked_next_open_fill():
    exit_fill = {
        "event_type": "EXIT_FILLED",
        "event_id": "exit-1",
        "setup_key": "s1",
        "direction": "LONG",
        "entry_time": at(1),
        "signal_time": at(2),
        "fill_time": at(3),
        "fill_price": 44025,
        "reason_code": "EXPECTED_BEHAVIOR_EXPIRED_NO_PROGRESS",
    }
    result = account_trades(
        [bar(1, 44000), bar(2, 44020), bar(3, 44025), bar(4, 45000)],
        [fill(), exit_fill, dict(exit_fill)],
        [{"bar_time": at(4), "action": "EXIT", "setup_key": "s1"}],
        cutoff=at(4),
        fee_per_side=0,
        slippage_points_per_side=0,
    )
    trade = result["trades"][0]
    assert trade["exit_signal_time"] == at(2)
    assert trade["exit_time"] == at(3)
    assert trade["exit_price"] == 44025
    assert trade["exit_source"] == "PROGRAMMATIC_BEHAVIOR_EXIT"


def test_rewind_branch_fill_is_excluded_when_checkpoint_does_not_carry_it():
    canonical = {
        "event_type": "ENTRY_FILLED",
        "event_id": "canonical",
        "setup_key": "new-short",
        "fill_time": at(3),
    }
    superseded = {
        "event_type": "ENTRY_FILLED",
        "event_id": "old-branch",
        "setup_key": "old-short",
        "fill_time": at(2),
    }

    filtered = _canonical_execution_events(
        [superseded, canonical],
        [
            {
                "bar_time": at(2),
                "analysis": {"action": {"position_action": "NONE", "setup_key": None}},
                "position_state": {"status": "FLAT", "active_setup_key": None},
            },
            {
                "bar_time": at(3),
                "analysis": {"action": {"position_action": "NONE", "setup_key": "new-short"}},
                "position_state": {
                    "status": "SHORT",
                    "active_setup_key": "new-short",
                    "entry_time": at(3),
                },
            },
        ],
    )

    assert [item["event_id"] for item in filtered] == ["canonical"]


def test_pending_exit_fill_is_canonical_on_the_following_checkpoint():
    exit_event = {
        "event_type": "EXIT_FILLED",
        "event_id": "exit-canonical",
        "setup_key": "s1",
        "direction": "LONG",
        "entry_time": at(1),
        "signal_time": at(2),
        "fill_time": at(3),
        "fill_price": 44025,
        "reason_code": "EXPECTED_BEHAVIOR_EXPIRED_NO_PROGRESS",
    }
    filtered = _canonical_execution_events(
        [exit_event],
        [
            {
                "bar_time": at(2),
                "analysis": {"action": {"position_action": "EXIT", "setup_key": "s1"}},
                "position_state": {
                    "status": "LONG",
                    "active_setup_key": "s1",
                    "pending_exit": {
                        "setup_key": "s1",
                        "direction": "LONG",
                        "signal_time": at(2),
                        "eligible_from": at(3),
                    },
                },
            },
            {
                "bar_time": at(3),
                "analysis": {"action": {"position_action": "NONE", "setup_key": None}},
                "position_state": {
                    "status": "FLAT",
                    "active_setup_key": None,
                    "pending_exit": None,
                },
            },
        ],
    )

    assert [item["event_id"] for item in filtered] == ["exit-canonical"]


def test_stage_one_performance_is_split_into_q2_q4_and_combined() -> None:
    q2_fill = {
        **fill(price=44000),
        "setup_key": "q2",
        "setup_name": "多方假跌破收復",
        "candidate_source": "FALSE_BREAK_RECLAIM",
        "entry_strategy": "Q2_FALSE_BREAK_RECLAIM",
        "entry_role": "INITIAL",
    }
    q4_fill = {
        **fill(price=44020),
        "fill_time": at(4),
        "setup_key": "q4",
        "setup_name": "多方修正後複製",
        "candidate_source": "CONFIRMED_PULLBACK_ENDPOINT_N2",
        "entry_strategy": "Q4_PULLBACK_CONTINUATION",
        "entry_role": "REENTRY",
    }
    result = account_trades(
        [
            bar(1, 44000),
            bar(2, 44005),
            bar(3, 44010),
            bar(4, 44020),
            bar(5, 44015),
            bar(6, 44010),
        ],
        [q2_fill, q4_fill],
        [
            {"bar_time": at(2), "action": "EXIT", "setup_key": "q2"},
            {"bar_time": at(5), "action": "EXIT", "setup_key": "q4"},
        ],
        cutoff=at(6),
        fee_per_side=0,
        slippage_points_per_side=0,
    )

    breakdown = result["strategy_breakdown"]
    assert breakdown["Q2"]["trade_count"] == 1
    assert breakdown["Q2"]["net_ntd"] > 0
    assert breakdown["Q4"]["trade_count"] == 1
    assert breakdown["Q4"]["net_ntd"] < 0
    assert breakdown["Q4"]["reentry_trade_count"] == 1
    assert breakdown["Q2_Q4_COMBINED"]["trade_count"] == 2
    assert breakdown["OTHER"]["trade_count"] == 0
    assert {item["setup_family"] for item in result["trades"]} == {"Q2", "Q4"}


@pytest.mark.parametrize(
    ("strategy", "source", "family"),
    [
        (
            "Q2_SLOW_OUTER_EXPANSION_FAILURE",
            "Q2_SLOW_OUTER_EXPANSION_FAILURE",
            "Q2",
        ),
        (
            "Q4_AGGRESSIVE_PULLBACK_REVERSAL",
            "Q4_AGGRESSIVE_PULLBACK_REVERSAL",
            "Q4",
        ),
    ],
)
def test_stage_one_new_course_paths_keep_stable_performance_family(
    strategy: str,
    source: str,
    family: str,
) -> None:
    assert _setup_family(
        {"entry_strategy": strategy, "candidate_source": source}
    ) == family


def _write_stage_one_report_run(
    root: Path,
    *,
    status: str = "completed",
    target_date: str = "2026-08-25",
) -> Path:
    analysis = root / "analysis"
    analysis.mkdir(parents=True)
    run_at = f"{target_date}T09:01:00+08:00"
    date_key = target_date.replace("-", "")
    manifest = {
        "run_id": root.name,
        "status": status,
        "target_date": target_date,
        "instrument": "TMF",
        "expiry_month": "202609",
        "source_sha256": {target_date: "a" * 64},
        "rule_version": "v34",
        "rule_sha256": "b" * 64,
        "model_rules_sha256": "c" * 64,
        "schema_sha256": "d" * 64,
        "execution_version": "v182",
        "execution_prompt_sha256": "e" * 64,
        "execution_implementation_sha256": "f" * 64,
        "deterministic_timeline_engine_version": "timeline-v1",
        "deterministic_timeline_engine_sha256": "1" * 64,
        "analysis_mode": "AI_HYBRID",
        "decision_authority": "AI_WITH_PROGRAM_GUARDRAILS",
        "execution_profile": "ai-hybrid-long-only-q2-q4-v1",
        "ai_input_view_version": "evidence-only-v1",
        "trade_direction_policy": "LONG_ONLY",
        "trade_setup_policy": "LONG_Q2_Q4_ONLY",
        "ai_provider": "codex",
        "ai_model": "model-a",
        "ai_reasoning_effort": "medium",
        "preopen_completed": True,
        "selected_bar_times": [run_at],
        "presentation_bar_times": [run_at],
        "next_day_index": 1 if status == "completed" else 0,
    }
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (root / "bars.csv").write_text(
        "bar_time,open,high,low,close,volume\n"
        f"{run_at},44000,44002,43998,44001,1\n",
        encoding="utf-8",
    )
    (analysis / f"day-{date_key}-0901-validated.json").write_text(
        json.dumps(
            {
                "analysis": {
                    "notification_reason": "無交易。",
                    "course_reading": {"main_strategy_family": "NONE"},
                    "scenario": {},
                    "action": {
                        "position_action": "NONE",
                        "setup_key": None,
                        "stop_price": None,
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    (analysis / f"day-{date_key}-0901-state-checkpoint.json").write_text(
        json.dumps(
            {
                "entry_eligibility": {"status": "NOT_ELIGIBLE"},
                "position_state": {"status": "FLAT", "last_action": "NONE"},
                "constitution_state": {},
            }
        ),
        encoding="utf-8",
    )
    return root


def test_stage_one_performance_rejects_partial_run(tmp_path: Path) -> None:
    run = _write_stage_one_report_run(tmp_path / "partial", status="running")

    with pytest.raises(ValueError, match="status.*completed"):
        load_run_report(run, fee=12, slippage=0)


def test_stage_one_performance_reports_frozen_execution_identity(tmp_path: Path) -> None:
    run = _write_stage_one_report_run(tmp_path / "complete")

    report = load_run_report(run, fee=12, slippage=0)

    assert report["complete"] is True
    assert report["target_date"] == "2026-08-25"
    assert len(report["execution_spec_sha256"]) == 64
    assert len(report["input_plan_sha256"]) == 64
    assert len(report["decision_trace_sha256"]) == 64
    assert len(report["course_state_trace_sha256"]) == 64


def test_stage_one_multi_run_aggregate_requires_same_spec_and_distinct_dates(
    tmp_path: Path,
) -> None:
    first = _write_stage_one_report_run(
        tmp_path / "day-a",
        target_date="2026-08-25",
    )
    second = _write_stage_one_report_run(
        tmp_path / "day-b",
        target_date="2026-08-26",
    )

    aggregate = aggregate_stage_one_run_reports(
        [first, second],
        fee=12,
        slippage=0.4,
    )

    assert aggregate["complete"] is True
    assert aggregate["run_count"] == 2
    assert aggregate["target_dates"] == ["2026-08-25", "2026-08-26"]
    assert aggregate["closed_trade_metrics"]["trade_count"] == 0
    assert aggregate["strategy_breakdown"]["Q2_Q4_COMBINED"]["trade_count"] == 0

    duplicate_date = _write_stage_one_report_run(
        tmp_path / "day-c",
        target_date="2026-08-25",
    )
    with pytest.raises(ValueError, match="distinct target_date"):
        aggregate_stage_one_run_reports(
            [first, duplicate_date],
            fee=12,
            slippage=0.4,
        )

    manifest_path = second / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["ai_model"] = "model-b"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="different execution"):
        aggregate_stage_one_run_reports(
            [first, second],
            fee=12,
            slippage=0.4,
        )
