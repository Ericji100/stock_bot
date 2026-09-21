import pytest

from scripts.v2_core_taiji_leg_table_r11 import validate_taiji_leg_table_asof


REFS = {"PRICE:2023-02-14", "PRICE:2023-03-06", "PRICE:2023-03-24", "PRICE:2023-04-12"}


def _leg(index, role, start, end, confirmed, refs):
    return {
        "index": index, "role": role, "scale": "LARGE", "parent_anchor_id": "ANCHOR-A",
        "start_date": start, "end_date": end, "confirmed_on": confirmed,
        "evidence_refs": refs,
        "same_scale_reason": "Same parent and comparable working-scale swing, subject to AI review.",
    }


def _check(legs, **kwargs):
    return validate_taiji_leg_table_asof(
        as_of="2023-04-12", parent_anchor_id="ANCHOR-A", controlling_scale="LARGE",
        counted_legs=legs, available_evidence_refs=REFS, **kwargs,
    )


def test_first_forming_leg_is_not_automatically_promoted_by_internal_pivots():
    first = _leg(1, "ADVANCE", "2023-02-14", None, None, ["PRICE:2023-02-14"])
    result = _check([first])
    assert result["counted_leg_count"] == 1
    assert result["last_confirmed_leg"] is None
    assert result["terminal_leg_forming"]
    assert not result["trade_permission_granted"]


def test_three_same_scale_legs_require_explicit_confirmed_table():
    legs = [
        _leg(1, "ADVANCE", "2023-02-14", "2023-03-06", "2023-03-06", ["PRICE:2023-02-14", "PRICE:2023-03-06"]),
        _leg(2, "CORRECTION", "2023-03-06", "2023-03-24", "2023-03-24", ["PRICE:2023-03-06", "PRICE:2023-03-24"]),
        _leg(3, "ADVANCE", "2023-03-24", None, None, ["PRICE:2023-03-24"]),
    ]
    result = _check(legs)
    assert result["counted_leg_count"] == 3
    assert result["last_confirmed_leg"] == 2


@pytest.mark.parametrize("edit", [
    lambda row: row.update(scale="SMALL"),
    lambda row: row.update(parent_anchor_id="OTHER"),
    lambda row: row.update(end_date="2023-04-15", confirmed_on="2023-04-17"),
    lambda row: row.update(evidence_refs=["PRICE:2023-06-01"]),
    lambda row: row.update(index=5),
    lambda row: row.update(role="CORRECTION"),
])
def test_rejects_scale_parent_future_evidence_or_sequence_error(edit):
    row = _leg(1, "ADVANCE", "2023-02-14", "2023-03-06", "2023-03-06", ["PRICE:2023-02-14"])
    edit(row)
    with pytest.raises(ValueError):
        _check([row])


def test_unresolved_scale_is_not_forced_into_a_generation():
    result = validate_taiji_leg_table_asof(
        as_of="2023-04-12", parent_anchor_id="ANCHOR-A", controlling_scale="UNRESOLVED",
        counted_legs=[], available_evidence_refs=REFS,
    )
    assert result["status"] == "SCALE_UNRESOLVED"
    assert result["counted_leg_count"] == 0
