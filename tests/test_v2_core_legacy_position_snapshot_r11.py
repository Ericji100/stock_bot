from copy import deepcopy
import json

from scripts.v2_core_legacy_position_snapshot_r11 import (
    ARTIFACT, build_calibration_snapshots, snapshot_from_executed_stock,
)
from scripts.v2_core_trade_role_preflight_r11 import preflight_trade_role_asof


def _stock():
    return {
        "episodes": [{
            "episode_id": "EP-1", "entry_date": "2023-06-14", "exit_date": "2023-08-10",
            "exit_signal_date": "2023-08-09", "mfe_pct_from_mother": 9999,
            "tranches": [
                {
                    "signal_date": "2023-06-13", "entry_date": "2023-06-14",
                    "entry_shares": 100, "buy_cost": 1000.0,
                    "share_adjustments": [{"date": "2023-07-01", "ratio": 1.1}],
                    "sell_price": 1000.0,  # Future result must not enter the snapshot.
                },
                {
                    "signal_date": "2023-07-14", "entry_date": "2023-07-17",
                    "entry_shares": 100, "buy_cost": 1000.0,
                    "share_adjustments": [],
                },
            ],
        }],
        "audit": [{"date": "2023-07-06", "event": "CASH_DIVIDEND（現金股利）", "episode": "EP-1", "cash": 50.0}],
    }


def _snapshot(stock):
    return snapshot_from_executed_stock(
        stock, as_of="2023-07-14", raw_close=10.0,
        sell_commission=0.001425, sell_tax=0.003, watchlist_active=True,
    )


def test_current_day_teacher_add_and_future_exit_do_not_change_prior_role():
    stock = _stock()
    before = _snapshot(stock)
    changed = deepcopy(stock)
    changed["episodes"][0]["mfe_pct_from_mother"] = -9999
    changed["episodes"][0]["tranches"][0]["sell_price"] = 0.01
    changed["episodes"][0]["tranches"][1]["signal_date"] = "2023-07-14"
    changed["episodes"][0]["tranches"][1]["entry_date"] = "2023-07-18"
    assert _snapshot(changed) == before
    assert before["open_position"]["filled_legs"] == [{"role": "MOTHER", "fill_date": "2023-06-14"}]
    assert before["open_position"]["net_liquidation_pnl_asof_close"] > 0
    assert preflight_trade_role_asof("2023-07-14", before)["review_role"] == "ADD_1"


def test_prior_mother_skipped_means_flat_even_if_old_ai_named_add():
    stock = {"episodes": [], "audit": [
        {"date": "2023-07-04", "event": "BUY_SKIPPED（取消進場）", "signal_date": "2023-07-03"},
        {"date": "2023-07-26", "event": "SIGNAL_EVIDENCE_ONLY（僅保存訊號證據）", "signal_date": "2023-07-26"},
    ]}
    result = snapshot_from_executed_stock(
        stock, as_of="2023-07-26", raw_close=20.0,
        sell_commission=0.001425, sell_tax=0.003, watchlist_active=True,
    )
    assert result["open_position"] is None
    assert preflight_trade_role_asof("2023-07-26", result)["review_role"] == "MOTHER_OR_REENTRY"


def test_all_fourteen_offline_packets_replay_and_role_discrepancy_is_preserved():
    manifest = build_calibration_snapshots()
    assert manifest["case_count"] == 14
    roles = {row["review_id"]: row["review_role"] for row in manifest["rows"]}
    assert sum(role == "MOTHER_OR_REENTRY" for role in roles.values()) == 12
    assert roles["FP-7a92f125f3c66ad4e26d7943"] == "ADD_1"
    assert roles["FP-f773ac4ff295214dfbea04ac"] == "ADD_2"
    assert roles["FP-cf4bd836219ec291eb979cc8"] == "MOTHER_OR_REENTRY"
    packet = json.loads((ARTIFACT / "legacy_position_snapshots_candidate_r11" / "FP-cf4bd836219ec291eb979cc8.json").read_text(encoding="utf-8"))
    assert packet["position_snapshot_as_of"]["open_position"] is None
    assert not packet["end_to_end_new_policy_ledger"]
    for row in manifest["rows"]:
        candidate = json.loads((ARTIFACT / "legacy_position_snapshots_candidate_r11" / row["packet_file"]).read_text(encoding="utf-8"))
        assert set(candidate) == {
            "packet_version", "review_id", "as_of", "calibration_only_prior_legacy_fills",
            "end_to_end_new_policy_ledger", "position_snapshot_as_of", "role_preflight",
        }
        snapshot = candidate["position_snapshot_as_of"]
        assert set(snapshot) == {
            "as_of", "source", "watchlist_active", "campaign_state", "open_position",
            "closed_episode_count", "pending_order",
        }
        if snapshot["open_position"]:
            assert all(leg["fill_date"] <= row["as_of"] for leg in snapshot["open_position"]["filled_legs"])
