from copy import deepcopy

import pandas as pd
import pytest

from scripts import course_ai_manual_replay as engine
from scripts.course_ai_replay_report import accounting, cost_sensitivity, initial_state


def frame(rows):
    return pd.DataFrame([{"date": pd.Timestamp(f"2026-07-{20+i:02d}"), "open": o,
        "high": h, "low": l, "close": c, "MA21": ma, "MA55": 8., "ATR14": 1., "volume": v}
        for i, (o, h, l, c, ma, v) in enumerate(rows)])


def plan(trigger=10., cap=10.1, defense=9.):
    return {"action": "ARM", "trigger": trigger, "cap": cap, "defense": defense,
            "signal_date": "2026-07-17", "reason": "人工AI測試計畫，先決策後執行"}


def enter(rows, entry_plan=None):
    f = frame(rows)
    s = initial_state()
    s["plans"] = {"S01": entry_plan or plan()}
    engine.execute_next(s, {"S01": f}, "2026-07-20")
    return s, {"S01": f}


def close(s, frames, day, **extra):
    engine.apply_close(s, {"date": day, "judgments": {"S01": {"action": "HOLD",
        "reason": "測試持倉判讀的足夠長理由", **extra}}}, frames)


@pytest.mark.parametrize("row", [
    (10.2, 10.5, 10.1, 10.3, 9.5, 100),  # Gap above cap.
    (9., 10.3, 8.8, 10., 9.5, 100),  # Open at defense: cancel.
    (9.7, 9.9, 9.5, 9.8, 9.5, 100),  # No trigger.
    (10., 10.5, 9.5, 10.2, 9.5, 0),  # No volume.
])
def test_non_executable_plans_expire(row):
    s, _ = enter([row])
    assert not s["trades"] and not s["plans"]
    assert s["cash"] == 500000


def test_signal_does_not_buy_until_next_session_and_gaps_fill_at_open():
    s = initial_state()
    f = {"S01": frame([(9.5, 9.8, 9., 9.7, 9., 100), (10.05, 10.2, 9.9, 10.1, 9., 100)])}
    engine.apply_close(s, {"date": "2026-07-20", "judgments": {"S01": plan()}}, f)
    assert not s["trades"] and s["cash"] == 500000
    engine.execute_next(s, f, "2026-07-21")
    t = s["trades"]["S01"]
    assert t["entry_price"] == 10.05
    assert t["entry_date"] == "2026-07-21"
    assert t["buy_outflow"] <= 10000 < engine.buy(t["quantity"]+1, 10.05)


def test_close_stop_executes_next_open_not_signal_close():
    s, f = enter([(10, 10.2, 8.8, 8.9, 8.5, 100), (8.2, 8.5, 8.1, 8.3, 8.4, 100)])
    close(s, f, "2026-07-20")
    t = s["trades"]["S01"]
    assert t["pending"] == "SELL" and t["remaining"] > 0
    engine.execute_next(s, f, "2026-07-21")
    assert t["exit_price"] == 8.2 and t["remaining"] == 0
    assert t["sale_proceeds"] == pytest.approx(t["quantity"] * 8.2 * .995575)


def test_half_and_recovery_keep_remaining_quantity():
    s, f = enter([(10, 10.1, 9.4, 9.6, 9.8, 100), (9.7, 10.4, 9.5, 10.2, 10, 100),
                  (10.1, 10.2, 9.3, 9.5, 9.8, 100), (9.6, 10, 9.4, 9.8, 9.7, 100)],
                 plan(trigger=10.01, cap=10.1))
    t = s["trades"]["S01"]
    original = t["quantity"]
    assert original % 2 == 1
    close(s, f, "2026-07-20")
    assert t["pending"] == "HALF"
    engine.execute_next(s, f, "2026-07-21")
    assert t["remaining"] == original - original // 2
    close(s, f, "2026-07-21")
    engine.execute_next(s, f, "2026-07-22")
    close(s, f, "2026-07-22")
    engine.execute_next(s, f, "2026-07-23")
    assert [x["event"] for x in t["fills"]] == ["BUY", "HALF"]
    assert t["remaining"] == original - original // 2


def test_two_r_touch_activates_cost_floor_same_close():
    s, f = enter([(10, 12.1, 9.6, 9.8, 9.5, 100), (9.4, 9.6, 9.2, 9.5, 9.3, 100)])
    close(s, f, "2026-07-20")
    t = s["trades"]["S01"]
    assert t["runner"] and t["dynamic_defense"] == 10.
    assert t["pending"] == "SELL"
    engine.execute_next(s, f, "2026-07-21")
    assert t["exit_price"] == 9.4


def test_runner_is_irreversible_and_needs_two_below_ma_closes():
    s, f = enter([(10, 12.1, 10, 11.8, 11.9, 100), (11.6, 11.7, 10.3, 10.5, 11., 100),
                  (10.4, 10.6, 10.2, 10.3, 10.8, 100)])
    close(s, f, "2026-07-20")
    t = s["trades"]["S01"]
    assert t["runner"] and t["pending"] is None and not t["half_used"]
    engine.execute_next(s, f, "2026-07-21")
    close(s, f, "2026-07-21")
    assert t["runner"] and t["pending"] == "SELL"
    assert t["exit_reason"] == "波段期連兩日跌破21MA"
    engine.execute_next(s, f, "2026-07-22")
    assert t["exit_date"] == "2026-07-22"


def test_ai_raised_structure_used_next_session_and_never_lowered():
    s, f = enter([(10, 11, 9.6, 10.8, 10, 100), (10.7, 10.8, 10.2, 10.3, 10.5, 100)])
    close(s, f, "2026-07-20", raise_defense=10.4)
    t = s["trades"]["S01"]
    assert t["pending"] is None and t["dynamic_defense"] == 10.4
    engine.execute_next(s, f, "2026-07-21")
    close(s, f, "2026-07-21", raise_defense=9.8)
    assert t["pending"] == "SELL" and t["dynamic_defense"] == 10.4
    assert t["exit_reason"] == "21MA及AI確認結構同步失守"


def test_expired_unfilled_plan_is_not_reused():
    s, f = enter([(9.7, 9.9, 9.5, 9.8, 9.5, 100), (10., 11., 9.8, 10.5, 9.8, 100)])
    engine.apply_close(s, {"date": "2026-07-20", "judgments": {"S01": {"action": "WAIT"}}}, f)
    engine.execute_next(s, f, "2026-07-21")
    assert not s["trades"]


def packet_fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "RUN", tmp_path)
    packet = {"as_of": "2026-07-20", "items": [{"alias": "S01", "position": None,
        "bars": [{"date": "2026-07-17", "low": 9.4, "close": 9.8, "ATR14": 1.}]}]}
    path = tmp_path / "packets/2026-07-20.json"
    engine.save(path, packet)
    payload = {"date": packet["as_of"], "packet_sha256": engine.digest(path),
               "judgments": {"S01": {**plan(defense=9.4), "defense_date": "2026-07-17"}}}
    return packet, payload


@pytest.mark.parametrize("mutation,match", [
    ("missing", "all candidates"), ("hash", "exact packet"),
    ("future", "invisible data"), ("price", "quoted low"), ("risk", "risk cap"),
])
def test_invalid_or_untraceable_ai_judgments_rejected(tmp_path, monkeypatch, mutation, match):
    packet, payload = packet_fixture(tmp_path, monkeypatch)
    if mutation == "missing": payload["judgments"] = {}
    if mutation == "hash": payload["packet_sha256"] = "wrong"
    if mutation == "future": payload["judgments"]["S01"]["defense_date"] = "2026-07-21"
    if mutation == "price": payload["judgments"]["S01"]["defense"] = 9.5
    if mutation == "risk": payload["judgments"]["S01"]["cap"] = 12
    with pytest.raises(ValueError, match=match):
        engine.validate_judgments(payload, packet, {}, initial_state())


def test_valid_ai_plan_and_immutable_seal(tmp_path, monkeypatch):
    packet, payload = packet_fixture(tmp_path, monkeypatch)
    engine.validate_judgments(payload, packet, {}, initial_state())
    path = tmp_path / "sealed.json"
    engine.save(path, payload, immutable=True)
    engine.save(path, payload, immutable=True)
    changed = deepcopy(payload)
    changed["date"] = "2026-07-21"
    with pytest.raises(ValueError, match="immutable"):
        engine.save(path, changed, immutable=True)


def test_open_partial_pnl_reconciles_and_cost_stress_lowers_result():
    s, f = enter([(10, 10.1, 9.4, 9.6, 9.8, 100), (9.7, 10.4, 9.5, 10.2, 10., 100)])
    close(s, f, "2026-07-20")
    engine.execute_next(s, f, "2026-07-21")
    close(s, f, "2026-07-21")
    m = {"initial_cash": 500000, "as_of": "2026-07-21",
         "cohort": [{"alias": "S01", "code": "1234", "name": "測試"}]}
    trades, stats = accounting(m, s)
    t = s["trades"]["S01"]
    net_value = t["remaining"] * 10.2 * .995575
    expected = t["sale_proceeds"] + net_value - t["buy_outflow"]
    assert stats["total_net_pnl"] == pytest.approx(expected)
    assert stats["all_realized_pnl"] + stats["open_unrealized_net_pnl"] == pytest.approx(expected)
    assert stats["total_net_pnl"] == pytest.approx(stats["ending_equity"] - 500000)
    assert trades[0]["mfe_pct"] == pytest.approx(4.)
    assert cost_sensitivity(s)["net_pnl"] == pytest.approx(expected)
    assert cost_sensitivity(s, 20, .001)["net_pnl"] < expected
