from __future__ import annotations

import pytest

from scripts.hybrid_v3_atomic_packets_v7_range_worker_v1 import (
    select_assigned_range,
)


def _inventory() -> list[dict[str, int]]:
    return [{"stock_source_ordinal": value} for value in range(12)]


def test_selects_disjoint_assigned_positions() -> None:
    left = select_assigned_range(
        _inventory(), shard_id=1, assigned_start=0, assigned_end=2
    )
    right = select_assigned_range(
        _inventory(), shard_id=1, assigned_start=2, assigned_end=4
    )
    assert [row["stock_source_ordinal"] for row in left] == [1, 4]
    assert [row["stock_source_ordinal"] for row in right] == [7, 10]
    assert {row["stock_source_ordinal"] for row in left}.isdisjoint(
        {row["stock_source_ordinal"] for row in right}
    )


@pytest.mark.parametrize(
    ("shard_id", "start", "end"),
    [(-1, 0, 1), (3, 0, 1), (0, -1, 1), (0, 1, 1), (0, 0, 5)],
)
def test_rejects_invalid_or_empty_ranges(shard_id: int, start: int, end: int) -> None:
    with pytest.raises(ValueError):
        select_assigned_range(
            _inventory(),
            shard_id=shard_id,
            assigned_start=start,
            assigned_end=end,
        )
