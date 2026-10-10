"""Research variant engine: ONE_ENTRY_PER_ACTIVATED_EPISODE + EXIT_DOW_TRAIL.

Authority (read-only, 00-frozen):

* `docs/strategies/dual_ma/2022-h1-single-entry-no-add-exploration-v1.md`
  raw SHA-256 `937D6585FD3727AC227542FEDCBB5266D2DBE98BE7CBBAB375469CFFA9BF4405`
* `docs/strategies/dual_ma/versions/v0.4.1-frozen/spec.md`
  raw SHA-256 `5A9E3A573178AA135FFBD960871D1ACEE20049F9ACF2DD7C117E1E2390B53DDA`
* `docs/strategies/dual_ma/2022-h1-selection-episode-lifecycle-v1.md`
  raw SHA-256 `361D5F35456FCA9B8BF00412CACBA4E2EF4173175ECDBB813D7600A977EB755E`

The variant keeps every frozen v0.4.1-frozen rule for the mother order -- the
`close < fast` / `close > fast` reclaim entry signal, the pullback low as the
initial stop, the 999-share / 10,000 TWD sizing rule, the 2,000 TWD initial-risk
cap, the `CATHAY_WEB_028_TW_V1` cost model, the `EOD_CLOSE_PROXY_V1` fill, the
structural stop, `EXIT_DOW_TRAIL` and the SMA regime exit -- and changes exactly
two things:

1. **No additions.** While a leg of the activated selection episode is open,
   every further entry signal inside the episode window is rejected.
2. **One mother order per activated selection episode.** Once a mother order has
   filled inside this episode window, a later entry signal in the same window is
   rejected even after that leg has already exited.

A selection episode that ends because the regime failed (or that never opened a
position) is re-driven by its own engine run from a fresh D -> E selection
event, so "old episode invalidated, then a new D/E/signal may enter" still
works exactly as the frozen lifecycle ruling requires.

Both rejections are appended as named `ENTRY_DECISION` events, so a run can
count them.  The variant is a genuine re-run over the frozen price bars; it is
never produced by filtering another matrix's `ADD` rows.

An entry signal that cannot be sized is left to the frozen engine, which records
`NON_EXECUTABLE` and creates no leg.  Because "one mother order" is defined on
the order that actually filled, an unfilled signal does not consume the
single-entry slot -- exactly as it does not create the mother leg of `M1`.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Mapping, Sequence

from .engine import DualMABacktestEngine
from .models import (
    AddVariant,
    EntryVariant,
    ExitVariant,
    Leg,
    SignalStatus,
    StrategyKind,
)


VARIANT_ID = "ONE_ENTRY_PER_ACTIVATED_EPISODE+EXIT_DOW_TRAIL"

#: The frozen six-matrix id whose exit arm this variant reuses.  The engine's
#: `RunManifest` contract only accepts `M1`..`M6`, so `M1` (`EXIT_DOW_TRAIL`)
#: is carried as the vehicle id; every emitted row also carries `VARIANT_ID`, and
#: the variant is never reported as one of the six frozen matrices.
ENGINE_MATRIX_VEHICLE = "M1"

STATUS_FILLED = SignalStatus.FILLED.value
STATUS_NON_EXECUTABLE = SignalStatus.NON_EXECUTABLE.value
STATUS_ADD_REJECTED_SINGLE_ENTRY = "ADD_REJECTED_SINGLE_ENTRY_VARIANT"
STATUS_REENTRY_REJECTED_SAME_EPISODE = "REENTRY_REJECTED_SAME_SELECTION_EPISODE"

#: Every `ENTRY_DECISION` status the variant can emit, for reconciliation.
ENTRY_DECISION_STATUSES: tuple[str, ...] = (
    STATUS_FILLED,
    STATUS_NON_EXECUTABLE,
    STATUS_ADD_REJECTED_SINGLE_ENTRY,
    STATUS_REENTRY_REJECTED_SAME_EPISODE,
)


class SingleEntryNoAddEngine(DualMABacktestEngine):
    """`v0.4.1-frozen` engine pinned to one mother order per episode window.

    Only the entry decision is overridden.  Stop handling, the daily Dow defense,
    the `EXIT_DOW_TRAIL` effective stop, the regime exit, sizing and the cost
    model all stay the frozen engine's own code paths.
    """

    single_entry_only = True

    def __init__(
        self,
        *,
        slippage_bps: int = 0,
        entry_variant: EntryVariant = EntryVariant.RECLAIM_ONLY,
    ) -> None:
        super().__init__(
            AddVariant.R1_AGGREGATE,
            ExitVariant.DOW_TRAIL,
            slippage_bps=slippage_bps,
            entry_variant=entry_variant,
        )

    def _handle_entry_signal(
        self,
        result: Any,
        state: Any,
        bar: Any,
        signal_stop: Any,
        pullback_event_id: str | None,
        events: list[dict[str, object]],
        instrument_type: Any,
        entry_evidence: Mapping[str, Any] | None = None,
    ) -> None:
        evidence = dict(entry_evidence or {})
        if state.open_legs:
            events.append(
                {
                    "type": "ENTRY_DECISION",
                    "role": "ADD",
                    "status": STATUS_ADD_REJECTED_SINGLE_ENTRY,
                    "variant_id": VARIANT_ID,
                    "pullback_event_id": pullback_event_id,
                    "initial_stop": signal_stop,
                    "active_episode_id": state.active_episode_id,
                    "open_leg_count": len(state.open_legs),
                    **evidence,
                }
            )
            return
        if state.episode_sequence >= 1:
            events.append(
                {
                    "type": "ENTRY_DECISION",
                    "role": "MOTHER",
                    "status": STATUS_REENTRY_REJECTED_SAME_EPISODE,
                    "variant_id": VARIANT_ID,
                    "pullback_event_id": pullback_event_id,
                    "initial_stop": signal_stop,
                    "mother_orders_already_filled": state.episode_sequence,
                    **evidence,
                }
            )
            return
        super()._handle_entry_signal(
            result,
            state,
            bar,
            signal_stop,
            pullback_event_id,
            events,
            instrument_type,
            entry_evidence=entry_evidence,
        )


def tally_entry_decisions(
    decisions: Sequence[Mapping[str, Any]], *, strategy: StrategyKind | str
) -> dict[str, int]:
    """Count `ENTRY_DECISION` statuses for one strategy inside one engine run."""

    wanted = strategy.value if isinstance(strategy, StrategyKind) else str(strategy)
    counter: Counter[str] = Counter()
    for decision in decisions:
        if str(decision.get("strategy")) != wanted:
            continue
        for event in decision.get("events") or ():
            if not isinstance(event, Mapping):
                continue
            if event.get("type") != "ENTRY_DECISION":
                continue
            counter[str(event.get("status"))] += 1
    return dict(sorted(counter.items()))


def pullback_event_by_leg(
    transactions: Sequence[Mapping[str, Any]], *, strategy: StrategyKind | str
) -> dict[str, str]:
    """Map each filled entry leg of one strategy to its pullback event id."""

    wanted = strategy.value if isinstance(strategy, StrategyKind) else str(strategy)
    mapping: dict[str, str] = {}
    for row in transactions:
        if str(row.get("strategy")) != wanted:
            continue
        if row.get("role") not in ("MOTHER", "ADD"):
            continue
        leg_id = row.get("leg_id")
        event_id = row.get("pullback_event_id")
        if leg_id is None or event_id is None:
            continue
        mapping.setdefault(str(leg_id), str(event_id))
    return mapping


def open_leg_ids(legs: Sequence[Leg]) -> tuple[str, ...]:
    return tuple(leg.leg_id for leg in legs if leg.exit_date is None)
