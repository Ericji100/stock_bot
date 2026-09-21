import pytest

from scripts.v2_core_trade_role_preflight_r11 import preflight_trade_role_asof


DAY = "2023-07-14"


def _snapshot(*, legs=None, pnl=10.0, closed=0, pending="NONE", active=True):
    return {
        "as_of": DAY,
        "source": "CAUSAL_EXECUTION_LEDGER",
        "watchlist_active": active,
        "campaign_state": "ACTIVE",
        "open_position": None if legs is None else {
            "episode_id": "EP-1", "filled_legs": legs,
            "net_liquidation_pnl_asof_close": pnl,
        },
        "closed_episode_count": closed,
        "pending_order": pending,
    }


MOTHER = {"role": "MOTHER", "fill_date": "2023-07-03"}
ADD_1 = {"role": "ADD_1", "fill_date": "2023-07-10"}
ADD_2 = {"role": "ADD_2", "fill_date": "2023-07-12"}


def test_missing_state_is_unknown_not_assumed_mother():
    result = preflight_trade_role_asof(DAY, None)
    assert result["review_role"] == "ROLE_UNKNOWN"
    assert result["status"] == "MISSING_ASOF_POSITION_SNAPSHOT"
    assert not result["trade_permission_granted"]


def test_flat_first_trade_and_reentry_need_mother_review():
    first = preflight_trade_role_asof(DAY, _snapshot())
    reentry = preflight_trade_role_asof(DAY, _snapshot(closed=1))
    assert first["review_role"] == reentry["review_role"] == "MOTHER_OR_REENTRY"
    assert not first["requires_new_episode"]
    assert reentry["requires_new_episode"]
    assert not reentry["structural_scenario_selected_by_program"]


def test_open_profitable_position_routes_adds_not_mother():
    first_add = preflight_trade_role_asof(DAY, _snapshot(legs=[MOTHER]))
    second_add = preflight_trade_role_asof(DAY, _snapshot(legs=[MOTHER, ADD_1]))
    capped = preflight_trade_role_asof(DAY, _snapshot(legs=[MOTHER, ADD_1, ADD_2]))
    assert (first_add["review_role"], first_add["status"]) == ("ADD_1", "READY_FOR_ADD_REVIEW")
    assert (second_add["review_role"], second_add["status"]) == ("ADD_2", "READY_FOR_ADD_REVIEW")
    assert capped["status"] == "ADD_CAP_REACHED"


def test_loss_same_day_and_pending_orders_block_duplicate_entry():
    assert preflight_trade_role_asof(DAY, _snapshot(legs=[MOTHER], pnl=0))["status"] == "NONPROFITABLE_POSITION_BLOCKS_ADD"
    today_mother = {"role": "MOTHER", "fill_date": DAY}
    assert preflight_trade_role_asof(DAY, _snapshot(legs=[today_mother]))["status"] == "SAME_DAY_FILL_BLOCKS_ADD"
    assert preflight_trade_role_asof(DAY, _snapshot(pending="BUY"))["status"] == "PENDING_ORDER_BLOCKS_NEW_SIGNAL"
    assert preflight_trade_role_asof(DAY, _snapshot(legs=[MOTHER], pending="SELL"))["status"] == "PENDING_ORDER_BLOCKS_NEW_SIGNAL"


def test_inactive_watch_and_fixed_only_variant_do_not_open_new_role():
    assert preflight_trade_role_asof(DAY, _snapshot(active=False))["status"] == "WATCH_OR_CAMPAIGN_INACTIVE"
    assert preflight_trade_role_asof(DAY, _snapshot(legs=[MOTHER]), max_adds=0)["status"] == "ADD_CAP_REACHED"


@pytest.mark.parametrize("mutation", [
    lambda s: s.update(as_of="2023-07-15"),
    lambda s: s.update(source="OLD_AI_LABEL"),
    lambda s: s.update(teacher_answer="ADD_1"),
    lambda s: s["open_position"]["filled_legs"][0].update(future_mfe=100),
    lambda s: s["open_position"]["filled_legs"].append({"role": "ADD_1", "fill_date": "2023-07-15"}),
    lambda s: s["open_position"]["filled_legs"].append({"role": "ADD_2", "fill_date": "2023-07-10"}),
])
def test_rejects_noncausal_or_invalid_snapshot(mutation):
    snapshot = _snapshot(legs=[MOTHER])
    mutation(snapshot)
    with pytest.raises(ValueError):
        preflight_trade_role_asof(DAY, snapshot)
