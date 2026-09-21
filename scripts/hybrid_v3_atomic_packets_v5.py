"""Candidate4 packet builder: preserve V4 and reject one-bar comparison legs.

V4 remains byte-for-byte frozen.  This wrapper pins that exact dependency,
changes only the working-comparison-window constructor, and delegates all
other causal packet construction and CLI behavior to V4.  A pivot leg whose
two endpoints occur on the same trading date is not a measurable comparison
segment and is excluded before the latest-two-same-direction pair is chosen.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Sequence

import pandas as pd

try:
    from . import hybrid_v3_atomic_packets_v4 as _v4
except ImportError:  # direct script import
    from scripts import hybrid_v3_atomic_packets_v4 as _v4


BUILDER_VERSION = "hybrid-v3-atomic-packets-v5"
BUILDER_STATUS = "FINAL"
BASE_BUILDER_VERSION = "hybrid-v3-atomic-packets-v4"
BASE_BUILDER_SHA256 = "580016b01c47a81404c46749ef0b4670990e5305195b5f35620f2c5ae1d3c648"
FIX_CODE = "EXCLUDE_COMPARISON_LEGS_WITH_FEWER_THAN_TWO_VISIBLE_BARS"
_WRAPPER_FILE = Path(__file__).resolve()
_BASE_FILE = Path(_v4.__file__).resolve()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def assert_base_builder_frozen() -> None:
    if _v4.BUILDER_VERSION not in {BASE_BUILDER_VERSION, BUILDER_VERSION}:
        raise RuntimeError("V4 base builder version changed")
    if _file_sha256(_BASE_FILE) != BASE_BUILDER_SHA256:
        raise RuntimeError("V4 base builder bytes changed")


def build_working_comparison_windows(
    *,
    objective: dict[str, Any],
    confirmed_pivots: Sequence[dict[str, Any]],
    visible: pd.DataFrame,
    as_of: str,
) -> dict[str, dict[str, Any] | None]:
    """Choose two causal same-direction legs, each spanning at least two bars."""

    output: dict[str, dict[str, Any] | None] = {"LARGE": None, "SMALL": None}
    for scale in ("LARGE", "SMALL"):
        pivots = sorted(
            (row for row in confirmed_pivots if row["scale"] == scale),
            key=lambda row: (
                str(row["source_date"]),
                str(row["confirmation_date"]),
                str(row["ref"]),
            ),
        )
        legs: list[dict[str, Any]] = []
        for start, end in zip(pivots, pivots[1:]):
            if (
                start["side"] == end["side"]
                or str(start["source_date"]) >= str(end["source_date"])
                or str(end["source_date"]) > as_of
            ):
                continue
            leg = {
                "direction": "UP" if start["side"] == "LOW" else "DOWN",
                "start": start,
                "end": end,
                "available_on": max(
                    str(start["confirmation_date"]), str(end["confirmation_date"])
                ),
            }
            measured = _v4._comparison_leg(start=start, end=end, visible=visible)
            if int(measured["bar_count"]) < 2:
                continue
            leg["measured"] = measured
            legs.append(leg)
        control_state = str(
            (objective.get("controls", {}).get(scale.lower()) or {}).get("state") or ""
        )
        direction = {"UP_CONTROL": "UP", "DOWN_CONTROL": "DOWN"}.get(control_state)
        if direction is None and legs:
            direction = str(legs[-1]["direction"])
        comparable = [row for row in legs if row["direction"] == direction]
        if len(comparable) < 2:
            continue
        baseline, current = comparable[-2:]
        available_on = max(str(baseline["available_on"]), str(current["available_on"]))
        if available_on > as_of:
            raise _v4.AtomicPacketError("comparison window is not causally available at as_of")
        output[scale] = {
            "scale": scale,
            "direction": direction,
            "selection_policy": "LATEST_TWO_COMPLETED_SAME_DIRECTION_LEGACY_PIVOT_LEGS",
            "baseline": baseline["measured"],
            "current": current["measured"],
            "baseline_available_on": str(baseline["available_on"]),
            "current_available_on": str(current["available_on"]),
            "available_on": available_on,
        }
    return output


def activate() -> None:
    """Install the one scoped V5 fix into the pinned V4 implementation."""

    assert_base_builder_frozen()
    _v4.build_working_comparison_windows = build_working_comparison_windows
    _v4.BUILDER_VERSION = BUILDER_VERSION
    _v4.BUILDER_STATUS = BUILDER_STATUS
    # V4's manifest helpers hash their module ``__file__``.  Under this
    # wrapper the auditable top-level builder is V5; V5 itself pins V4 bytes.
    _v4.__file__ = str(_WRAPPER_FILE)


def __getattr__(name: str) -> Any:
    return getattr(_v4, name)


def main() -> int:
    activate()
    return int(_v4._main())


if __name__ == "__main__":
    raise SystemExit(main())
