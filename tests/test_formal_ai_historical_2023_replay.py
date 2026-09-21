from __future__ import annotations

import ast
from pathlib import Path

import pandas as pd

from scripts import formal_ai_historical_2023_replay as replay


def test_no_old_classifier_or_judge_is_called() -> None:
    source_path = Path(replay.__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    forbidden = {
        "candidate_reason",
        "_judge_candidate",
        "judge_candidate",
        "classifier",
        "classify",
        "score_candidate",
    }
    called: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            called.add(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            called.add(node.func.attr)
    assert not (called & forbidden)


def test_future_date_scan_is_recursive() -> None:
    value = {
        "macro_anchor": {"start": "2023-01-03", "end": "2023-02-01"},
        "evidence": ["截至2023-02-02已知", {"bad": "2023-02-03"}],
    }
    leaks = replay._future_dates(value, "2023-02-02")
    assert leaks == [{"path": "trigger.evidence[1].bad", "date": "2023-02-03"}]


def test_unicode_replacement_character_is_rejected() -> None:
    assert replay._has_unicode_replacement({"evidence": ["正常中文"]}) is False
    assert replay._has_unicode_replacement({"evidence": ["損壞�文字"]}) is True


def test_intent_requires_explicit_mother_or_add() -> None:
    assert replay._intent_kind("MOTHER_OR_REENTRY") == "MOTHER"
    assert replay._intent_kind("ADD_CANDIDATE") == "ADD"
    assert replay._intent_kind("WATCH_ONLY") is None


def test_confirmed_pivot_matching_requires_side_scale_date_and_price() -> None:
    pivots = [
        {
            "confirmation_date": "2023-01-10",
            "source_date": "2023-01-05",
            "scale": "SMALL",
            "side": "LOW",
            "price": 10.0,
        },
        {
            "confirmation_date": "2023-01-12",
            "source_date": "2023-01-05",
            "scale": "LARGE",
            "side": "LOW",
            "price": 10.0,
        },
    ]
    matches = replay._matching_confirmed_pivots(
        pivots,
        side="LOW",
        source_date="2023-01-05",
        price=10.005,
        scale="SMALL",
    )
    assert matches == [pivots[0]]
    assert replay._matching_confirmed_pivots(
        pivots,
        side="HIGH",
        source_date="2023-01-05",
        price=10.0,
        scale="SMALL",
    ) == []


def test_declared_pivot_requires_explicit_confirmation_fields() -> None:
    assert replay._declared_pivot({
        "date": "2023-01-05",
        "price": "10.5",
        "confirmed_on": "2023-01-10",
    }) == ("2023-01-05", 10.5, "2023-01-10")
    assert replay._declared_pivot({"date": "2023-01-05", "price": None}) == (
        "2023-01-05",
        None,
        None,
    )


def test_pivot_may_confirm_at_signal_close_but_must_form_earlier() -> None:
    assert replay._pivot_timing_is_causal("2023-01-05", "2023-01-10", "2023-01-10") is True
    assert replay._pivot_timing_is_causal("2023-01-10", "2023-01-10", "2023-01-10") is False
    assert replay._pivot_timing_is_causal("2023-01-05", "2023-01-11", "2023-01-10") is False


def test_v2_gates_require_individual_pass_evidence() -> None:
    good = [
        {"gate": gate, "result": "PASS", "evidence": [f"fact {gate}"]}
        for gate in sorted(replay.V2_REQUIRED_GATE_IDS["FRESH_Q1_EXPANSION"])
    ]
    assert replay._v2_gate_quality(good, "FRESH_Q1_EXPANSION") == (True, None)
    bad = [*good[:-1], {**good[-1], "evidence": []}]
    valid, error = replay._v2_gate_quality(bad, "FRESH_Q1_EXPANSION")
    assert valid is False
    assert error == f"EMPTY_INDIVIDUAL_EVIDENCE:{good[-1]['gate']}"


def test_v2_gate_ids_and_taiji_generation_are_canonical() -> None:
    missing = [
        {"gate": gate, "result": "PASS", "evidence": ["fact"]}
        for gate in sorted(
            replay.V2_REQUIRED_GATE_IDS["FRESH_Q1_EXPANSION"]
            - {"EARLY_LOCATION_WITH_SPACE"}
        )
    ]
    valid, error = replay._v2_gate_quality(missing, "FRESH_Q1_EXPANSION")
    assert valid is False
    assert error == "TOO_FEW_GATES:6<7"

    valid, error = replay._v2_taiji_quality({
        "scenario": "FRESH_Q1_EXPANSION",
        "trigger_path": "FIRST_SHALLOW_CORRECTION_RELAUNCH",
        "taiji_generation": "COPY_LEG_2",
    })
    assert valid is False
    assert error == "NONCANONICAL_TAIJI_GENERATION:COPY_LEG_2"

    assert replay._v2_taiji_quality({
        "scenario": "FRESH_Q1_EXPANSION",
        "trigger_path": "FIRST_SHALLOW_CORRECTION_RELAUNCH",
        "taiji_generation": "COPY_LEG_3",
    }) == (True, None)


def test_unknown_scenario_is_rejected_instead_of_using_a_one_gate_fallback() -> None:
    gates = [{"gate": "g0", "result": "PASS", "evidence": ["fact"]}]
    assert replay._v2_gate_quality(gates, "FIRST_QUALITY_EXPANSION") == (
        False,
        "UNKNOWN_SCENARIO:FIRST_QUALITY_EXPANSION",
    )


def test_signal_source_attribution_separates_first_prior_and_same_day() -> None:
    frame = pd.DataFrame(
        [
            {"date": pd.Timestamp("2023-01-03"), "close": 10.0, "atr14": 1.0},
            {"date": pd.Timestamp("2023-01-05"), "close": 11.0, "atr14": 1.0},
        ]
    )
    row = {
        "code": "TEST",
        "name": "測試",
        "watchlist_events": [{"date": "2023-01-03", "event": "WATCHING"}],
        "v1": {
            "triggers": [{
                "signal_date": "2023-01-05",
                "scenario": "FRESH_Q1_EXPANSION",
                "trigger_path": "INITIAL_DESTRUCTIVE_EXPANSION",
                "episode_or_add_candidate": "MOTHER_OR_REENTRY",
                "stop_date": "2023-01-03",
                "stop_price": 9.0,
                "evidence": ["causal"],
                "invalidation": "below 9",
            }]
        },
    }
    daily = {
        "2023-01-03": ["FIRST_A"],
        "2023-01-04": ["PRIOR_B"],
        "2023-01-05": ["TODAY_C"],
    }
    signals = replay._signals_for("v1", row, frame, daily, "2023-01-03")
    assert signals[0]["initial_monitor_strategies"] == ["FIRST_A"]
    assert signals[0]["pre_trigger_selection_strategies"] == ["FIRST_A", "PRIOR_B"]
    assert signals[0]["active_selection_strategies"] == ["FIRST_A", "PRIOR_B", "TODAY_C"]
    assert signals[0]["selected_today_strategies"] == ["TODAY_C"]


def test_same_day_invalidation_then_reselection_reactivates() -> None:
    lifecycle = replay._lifecycle_for(
        "v1",
        {
            "watchlist_events": [
                {"date": "2023-01-03", "event": "WATCHING"},
                {"date": "2023-02-01", "event": "RESELECTED"},
                {"date": "2023-02-01", "event": "CAMPAIGN_INVALIDATED"},
            ],
            "v1": {},
        },
        "2023-01-03",
    )
    active, campaign = replay._active_campaign(lifecycle, "2023-02-01")
    assert active is True
    assert campaign == 2


def test_same_day_initial_carry_in_rejection_stays_inactive() -> None:
    lifecycle = replay._lifecycle_for(
        "v1",
        {
            "watchlist_events": [
                {"date": "2023-05-02", "event": "LEFT_CENSORED_CARRY_IN"},
                {"date": "2023-05-02", "event": "REMOVED_FROM_WATCHLIST（AI初審拒絕）"},
            ],
            "v1": {},
        },
        "2023-05-02",
    )
    active, campaign = replay._active_campaign(lifecycle, "2023-05-02")
    assert active is False
    assert campaign == 1


def test_split_adjusts_existing_shares_and_raw_per_share_basis() -> None:
    current = {
        "episode_id": "TEST-E1",
        "tranches": [{
            "role": "MOTHER（母單）",
            "entry_price_raw": 20.0,
            "entry_shares": 100,
            "shares": 100.0,
            "stop_adjusted": 18.0,
            "raw_entry_basis": 20.0,
            "raw_stop_basis": 18.0,
            "share_adjustments": [],
        }],
    }
    audit: list[dict] = []
    replay._apply_split(current, "2023-06-01", 2.0, audit)
    tranche = current["tranches"][0]
    assert tranche["shares"] == 200.0
    assert tranche["raw_entry_basis"] == 10.0
    assert tranche["raw_stop_basis"] == 9.0
    assert replay._shares_on(tranche, "2023-05-31") == 100.0
    assert replay._shares_on(tranche, "2023-06-01") == 200.0
    assert replay._shares_before(tranche, "2023-06-01") == 100.0
    assert audit[0]["event"].startswith("SPLIT_ADJUSTED")


def test_last_day_signal_is_audited_as_unexecuted(monkeypatch) -> None:
    frame = pd.DataFrame([
        {
            "date": pd.Timestamp("2023-09-01"), "open": 10.0, "raw_open": 10.0,
            "high": 10.5, "low": 9.5, "close": 10.0, "raw_close": 10.0,
            "ma21": 9.8, "latest_pivot_low": 9.0,
        },
        {
            "date": pd.Timestamp("2023-09-04"), "open": 10.2, "raw_open": 10.2,
            "high": 10.8, "low": 10.0, "close": 10.6, "raw_close": 10.6,
            "ma21": 10.0, "latest_pivot_low": 9.5,
        },
    ])
    monkeypatch.setattr(replay, "_load_frame_asof", lambda item: frame.copy())
    monkeypatch.setattr(replay, "_corporate_actions", lambda item: ({}, {}, []))
    stock = replay._simulate_stock(
        {"code": "TEST", "name": "測試"},
        [{
            "signal_date": "2023-09-04", "monitor_on": "2023-09-01",
            "signal_close": 10.6, "signal_atr": 1.0, "stop": 9.5,
            "family": "AI", "scenario": "FRESH_Q1_EXPANSION", "score": None,
            "campaign": 1, "intent": "MOTHER_OR_REENTRY",
            "active_selection_strategies": ["TEST_SOURCE"],
        }],
        [{"date": "2023-09-01", "event": "WATCHING（加入監控）"}],
        allow_adds=False,
    )
    assert stock["episodes"] == []
    end_events = [event for event in stock["audit"] if event["event"].startswith("UNEXECUTED_END_OF_WINDOW")]
    assert len(end_events) == 1
    assert end_events[0]["signal_date"] == "2023-09-04"


def test_add_requires_an_unused_independent_structure_key(monkeypatch) -> None:
    frame = pd.DataFrame([
        {"date": pd.Timestamp("2023-06-01"), "open": 100.0, "raw_open": 100.0, "high": 102.0, "low": 99.0, "close": 101.0, "raw_close": 101.0, "ma21": 95.0, "latest_pivot_low": 90.0},
        {"date": pd.Timestamp("2023-06-02"), "open": 101.0, "raw_open": 101.0, "high": 107.0, "low": 100.0, "close": 106.0, "raw_close": 106.0, "ma21": 96.0, "latest_pivot_low": 91.0},
        {"date": pd.Timestamp("2023-06-05"), "open": 106.0, "raw_open": 106.0, "high": 110.0, "low": 105.0, "close": 109.0, "raw_close": 109.0, "ma21": 97.0, "latest_pivot_low": 92.0},
        {"date": pd.Timestamp("2023-06-06"), "open": 109.0, "raw_open": 109.0, "high": 111.0, "low": 108.0, "close": 110.0, "raw_close": 110.0, "ma21": 98.0, "latest_pivot_low": 93.0},
    ])
    monkeypatch.setattr(replay, "_load_frame_asof", lambda item: frame.copy())
    monkeypatch.setattr(replay, "_corporate_actions", lambda item: ({}, {}, []))

    def signal(day: str, key: str) -> dict:
        return {
            "signal_date": day, "monitor_on": "2023-06-01", "signal_close": float(frame.loc[frame.date == pd.Timestamp(day), "close"].iloc[0]),
            "signal_atr": 20.0, "stop": 90.0, "family": "AI", "scenario": "MACRO_COPY_RESONANCE", "score": None,
            "campaign": 1, "intent": "MOTHER_OR_V2_VALID_ADD", "active_selection_strategies": ["TEST_SOURCE"],
            "independent_structure_key": key,
        }

    stock = replay._simulate_stock(
        {"code": "TEST", "name": "測試"},
        [signal("2023-06-01", "CONTROL-0"), signal("2023-06-02", "CONTROL-1"), signal("2023-06-05", "CONTROL-1")],
        [{"date": "2023-06-01", "event": "WATCHING（加入監控）"}], allow_adds=True,
    )
    assert len(stock["episodes"]) == 1
    assert len(stock["episodes"][0]["tranches"]) == 2
    skipped = [event for event in stock["audit"] if event["event"].startswith("ADD_SKIPPED_DUPLICATE_STRUCTURE")]
    assert len(skipped) == 1
    assert skipped[0]["independent_structure_key"] == "CONTROL-1"


def test_reentry_rejects_an_old_structure_key_but_accepts_a_new_one(monkeypatch) -> None:
    frame = pd.DataFrame([
        {"date": pd.Timestamp("2023-07-03"), "open": 100.0, "raw_open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "raw_close": 100.0, "ma21": 98.0, "latest_pivot_low": 95.0},
        {"date": pd.Timestamp("2023-07-04"), "open": 100.0, "raw_open": 100.0, "high": 101.0, "low": 93.0, "close": 94.0, "raw_close": 94.0, "ma21": 98.0, "latest_pivot_low": 95.0},
        {"date": pd.Timestamp("2023-07-05"), "open": 94.0, "raw_open": 94.0, "high": 101.0, "low": 93.0, "close": 100.0, "raw_close": 100.0, "ma21": 97.0, "latest_pivot_low": 94.0},
        {"date": pd.Timestamp("2023-07-06"), "open": 100.0, "raw_open": 100.0, "high": 103.0, "low": 99.0, "close": 102.0, "raw_close": 102.0, "ma21": 98.0, "latest_pivot_low": 96.0},
    ])
    monkeypatch.setattr(replay, "_load_frame_asof", lambda item: frame.copy())
    monkeypatch.setattr(replay, "_corporate_actions", lambda item: ({}, {}, []))

    def signal(day: str, key: str) -> dict:
        close = float(frame.loc[frame.date == pd.Timestamp(day), "close"].iloc[0])
        return {
            "signal_date": day, "monitor_on": "2023-07-03", "signal_close": close,
            "signal_atr": 20.0, "stop": 95.0 if day == "2023-07-03" else 94.0,
            "family": "AI", "scenario": "MACRO_COPY_RESONANCE", "score": None,
            "campaign": 1, "intent": "MOTHER_OR_V2_VALID_ADD",
            "active_selection_strategies": ["TEST_SOURCE"], "independent_structure_key": key,
        }

    duplicate = replay._simulate_stock(
        {"code": "TEST", "name": "測試"},
        [signal("2023-07-03", "CONTROL-0"), signal("2023-07-05", "CONTROL-0")],
        [{"date": "2023-07-03", "event": "WATCHING（加入監控）"}], allow_adds=False,
    )
    assert len(duplicate["episodes"]) == 1
    assert any(event["event"].startswith("REENTRY_SKIPPED_DUPLICATE_STRUCTURE") for event in duplicate["audit"])

    fresh = replay._simulate_stock(
        {"code": "TEST", "name": "測試"},
        [signal("2023-07-03", "CONTROL-0"), signal("2023-07-05", "CONTROL-1")],
        [{"date": "2023-07-03", "event": "WATCHING（加入監控）"}], allow_adds=False,
    )
    assert len(fresh["episodes"]) == 2
    assert fresh["episodes"][1]["tranches"][0]["role"].startswith("REENTRY")
    assert any(event["event"].startswith("POST_STOP_NEW_STRUCTURE") for event in fresh["audit"])
