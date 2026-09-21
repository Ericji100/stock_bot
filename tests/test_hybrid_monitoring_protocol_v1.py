from __future__ import annotations

import copy
import json
from pathlib import Path

from scripts.hybrid_v3_policy import consistency_metrics, merge_three, reduce_v3, validate_semantic
from scripts.hybrid_v3_event_packets import _event_types
from scripts.hybrid_v3_ai_review import _assert_execution_freeze, _assert_freeze


ROOT = Path(__file__).resolve().parents[1]


def packet() -> dict:
    return {
        "review_id": "D-" + "1" * 24,
        "anonymous_stock_id": "S-" + "2" * 16,
        "as_of": "2023-06-01",
        "event_types": ["INITIAL_SELECTION", "SMALL_CONTROL_BREAK"],
        "selection_asof": {"selected_before_or_on_day": True},
        "data_quality": {"preferred_750_met": True, "pre_monitor_bars": 800},
        "candidate_stop_refs": ["PIVOT:SMALL:LOW:2023-05-20:2023-05-25"],
        "evidence": [
            {"ref": "BAR:2023-06-01", "kind": "BAR", "date": "2023-06-01", "values": {"close": 100, "atr14": 2}},
            {"ref": "PIVOT:SMALL:LOW:2023-05-20:2023-05-25", "kind": "CONFIRMED_PIVOT", "date": "2023-05-25", "values": {"price": 95}},
            {"ref": "PIVOT:LARGE:LOW:2023-04-01:2023-04-20", "kind": "CONFIRMED_PIVOT", "date": "2023-04-20", "values": {"price": 80}},
            {"ref": "PIVOT:LARGE:HIGH:2023-05-01:2023-05-15", "kind": "CONFIRMED_PIVOT", "date": "2023-05-15", "values": {"price": 110}},
        ],
    }


def semantic() -> dict:
    gate_ids = [
        "COMPLETED_PARENT_ANCHOR", "CORRECTION_INTACT", "TAIJI_GENERATION_MAPPED",
        "CORRECTION_BEAR_DOW_LINE_CAUSAL", "SMALL_UP_REANCHOR_BREAK",
        "DUAL_SCALE_LONG_ALIGNMENT", "NOT_Q3_OR_EXHAUSTED", "EPISODE_STOP_CAUSAL",
    ]
    return {
        "schema_version": "hybrid-common-structure-v1",
        "protocol_version": "hybrid-monitoring-protocol-v1",
        "review_id": "D-" + "1" * 24,
        "anonymous_stock_id": "S-" + "2" * 16,
        "as_of": "2023-06-01",
        "primary_scenario": "MACRO_COPY_RESONANCE",
        "alternative_scenarios": [],
        "anchors": [{"anchor_id": "A1", "role": "PRIMARY", "scale": "LARGE", "direction": "UP", "start_ref": "PIVOT:LARGE:LOW:2023-04-01:2023-04-20", "end_ref": "PIVOT:LARGE:HIGH:2023-05-01:2023-05-15", "status": "CORRECTING", "clean": "PASS", "meaty": "PASS", "destructive": "PASS", "traceable": "PASS", "reason": "已完成可追溯父級向上定錨。"}],
        "scales": {
            "large": {"direction": "UP", "quadrant": "Q4", "dow_state": "BULL", "dow_defense_ref": "PIVOT:LARGE:LOW:2023-04-01:2023-04-20", "reason": "大級多頭修正仍守住防線。"},
            "small": {"direction": "UP", "quadrant": "Q1", "dow_state": "BULL", "dow_defense_ref": "PIVOT:SMALL:LOW:2023-05-20:2023-05-25", "reason": "小級完成低不破低與高過高。"},
            "relationship": "STRATEGY_RESONANCE",
        },
        "taiji": {"phase": "REPLICATION", "generation": "COPY_LEG_3", "parent_anchor_id": "A1", "same_direction_attack_number": 1, "copy_to_correction": False, "reason": "父代修正後開始第一代向上複製。"},
        "left_right_phase": "RR",
        "stage_location": "EARLY",
        "semantic_flags": {"q3": False, "exhausted": False, "scale_conflict": False, "single_indicator_only": False, "bear_late_stage_clues": []},
        "semantic_gates": [{"gate_id": gate_id, "result": "PASS", "evidence_refs": ["PIVOT:SMALL:LOW:2023-05-20:2023-05-25"], "reason": "具有當日以前可核對結構證據。"} for gate_id in gate_ids],
        "reason": "父代向上定錨完成，修正未失效且小級重新取得向上控制權。",
        "causal_attestation": {"latest_visible_bar": "2023-06-01", "used_future_data": False, "identity_visible": False, "performance_visible": False, "invented_evidence_ref": False},
    }


def test_frozen_files_exist_and_original_v3_unchanged() -> None:
    assert (ROOT / "docs/hybrid-monitoring-protocol-v1.md").exists()
    expected = "4e78b5ddeea93d6469eca1fbd6e5309679bfdfc4193f1358cfde09789b201ca6"
    import hashlib
    assert hashlib.sha256((ROOT / "config/enlightenment_ai_rules_v3.json").read_bytes()).hexdigest() == expected


def test_hybrid_freeze_manifest_matches_current_authoritative_files() -> None:
    _assert_freeze()
    _assert_execution_freeze()


def test_valid_core_is_reduced_by_program() -> None:
    assert validate_semantic(packet(), semantic()) == []
    result = reduce_v3(packet(), semantic())
    assert result["permission"] == "TRADE"
    assert result["route"] == "V2_CORE"


def test_ai_cannot_output_trade_key() -> None:
    value = semantic()
    value["trade"] = "yes"
    assert validate_semantic(packet(), value)


def test_unknown_evidence_ref_is_rejected() -> None:
    value = semantic()
    value["semantic_gates"][0]["evidence_refs"] = ["PIVOT:INVENTED"]
    assert any("unknown evidence_ref" in error for error in validate_semantic(packet(), value))


def test_one_allowed_macro_unknown_becomes_probe() -> None:
    value = semantic()
    next(g for g in value["semantic_gates"] if g["gate_id"] == "DUAL_SCALE_LONG_ALIGNMENT")["result"] = "UNKNOWN"
    result = reduce_v3(packet(), value)
    assert result["permission"] == "TRADE"
    assert result["route"] == "NEAR_PASS_MACRO_COPY"


def test_two_unknowns_do_not_trade() -> None:
    value = semantic()
    for gate_id in ("DUAL_SCALE_LONG_ALIGNMENT", "TAIJI_GENERATION_MAPPED"):
        next(g for g in value["semantic_gates"] if g["gate_id"] == gate_id)["result"] = "UNKNOWN"
    assert reduce_v3(packet(), value)["permission"] == "WAIT"


def test_three_run_disagreement_is_downgraded() -> None:
    rows = [semantic(), copy.deepcopy(semantic()), copy.deepcopy(semantic())]
    next(g for g in rows[2]["semantic_gates"] if g["gate_id"] == "DUAL_SCALE_LONG_ALIGNMENT")["result"] = "UNKNOWN"
    merged = merge_three(packet(), rows)
    gate = next(g for g in merged["semantic_gates"] if g["gate_id"] == "DUAL_SCALE_LONG_ALIGNMENT")
    assert gate["result"] == "UNKNOWN"


def test_consistency_metrics_require_exact_permissions() -> None:
    value = semantic()
    packets = {packet()["review_id"]: packet()}
    run = {value["review_id"]: value}
    metrics = consistency_metrics(packets, [run, copy.deepcopy(run), copy.deepcopy(run)])
    assert metrics["schema_and_causality"] == 1
    assert metrics["permission"] == 1
    assert metrics["scenario_and_phase"] == 1


def test_upstream_repeat_is_not_automatically_reselection() -> None:
    row = {
        "stock_day_ordinal": 3,
        "candidate_day": {"close": 100, "ma21": 99, "ma55": 98, "ma105": 97, "ma144": 96, "facts": ["UPSTREAM_SELECTED_TODAY"]},
        "selection_asof": {"selected_today": ["TECH_MA5_RECLAIM"]},
        "confirmed_pivots_asof": [],
    }
    events, _ = _event_types(row, {"long_regime": "ABOVE_BOTH", "working_regime": "ABOVE_BOTH", "below_macro": False})
    assert "RESELECTION" not in events


def test_v2_gate_ids_match_frozen_formal_replay() -> None:
    protocol = json.loads((ROOT / "config/hybrid_monitoring_protocol_v1.json").read_text(encoding="utf-8"))
    from scripts.formal_ai_historical_2023_replay import V2_REQUIRED_GATE_IDS
    for scenario, ids in protocol["v2_required_semantic_gates"].items():
        assert set(ids) == set(V2_REQUIRED_GATE_IDS[scenario])
