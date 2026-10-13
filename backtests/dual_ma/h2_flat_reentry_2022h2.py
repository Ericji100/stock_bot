"""2022 H2 flat re-entry after exit inside one activated episode (02 role).

Authority (read-only, 00-frozen):

* docs/strategies/dual_ma/2022-h2-flat-reentry-v-pivot-exploration-v1.md
  raw SHA-256 6376BDC1F45120A00AFA4468FCC9218E139BB58AADFF68ED22C268FE14469D87
  (4046 bytes)
* docs/strategies/dual_ma/2022-h2-single-entry-v-pivot-exploration-v1.md
* docs/strategies/dual_ma/versions/v0.4.1-frozen/spec.md
* docs/strategies/dual_ma/versions/v0.5.0-preregistered/spec.md
* docs/strategies/dual_ma/patterns/pivot-v-confirm-v1.md
* docs/strategies/dual_ma/2022-h1-selection-episode-lifecycle-v1.md
* docs/strategies/dual_ma/2022-h2-corporate-action-report-correction-v1.md

Question: with exactly the same 129 accepted official 2022 H2 selection days,
the same 2021-2022 Fugle price snapshot, the same D -> E -> T causal state
machine, the same cost/exit model and the same 2022-12-30 cut-off, what does the
single-entry variant look like when exactly one rule is relaxed -- after the
mother order of an activated selection episode has exited, the episode stays
valid and the *original* V-plus-reclaim state machine may fire a fresh, complete
entry signal on a later official session, in which case one new mother order is
bought inside the same episode.

Only that one rule changes.  Holding while a leg is open still rejects every
addition, a same-day exit-and-rebuy is refused, a past T is never reused, an
episode that ended on a regime failure still needs a new D/E and long/medium
stay independent.  Every new mother order re-sizes, re-pays commission and tax
and never rewrites, merges or makes an old leg free.
"""

from __future__ import annotations

import argparse
import csv
import subprocess
import sys
from collections import Counter
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import h1_real_pilot_2022_0419_0428 as pilot
from . import h2_corpaction_correction_2022h2 as corp
from . import h2_single_entry_v_pivot_2022h2 as h2
from .contracts import (
    COST_MODEL,
    EXECUTION_MODEL,
    SPEC_SHA256,
    STRATEGY_VERSION,
    RunManifest,
)
from .costs import CATHAY_WEB_028_TW_V1
from .engine import DualMABacktestEngine
from .models import (
    AddVariant,
    EntryVariant,
    ExitReason,
    ExitVariant,
    InstrumentType,
    StrategyKind,
)
from .single_entry_no_add import (
    ENGINE_MATRIX_VEHICLE,
    STATUS_ADD_REJECTED_SINGLE_ENTRY,
    STATUS_FILLED,
    STATUS_NON_EXECUTABLE,
)


MODULE_VERSION = "dual-ma-h2-flat-reentry-v-pivot-2022h2-v1"
RUN_ID_DEFAULT = "h2-flat-reentry-vpivot-2022h2-v1"
MANIFEST_SCHEMA_VERSION = "dual-ma-h2-flat-reentry-manifest-v1"
READBACK_SCHEMA_VERSION = "dual-ma-h2-flat-reentry-readback-v1"
FUNNEL_SCHEMA_VERSION = "dual-ma-h2-flat-reentry-funnel-v1"
STATS_SCHEMA_VERSION = "dual-ma-h2-flat-reentry-stats-v1"
REJECTIONS_SCHEMA_VERSION = "dual-ma-h2-flat-reentry-rejections-v1"
SPOT_CHECK_SCHEMA_VERSION = "dual-ma-h2-flat-reentry-spot-checks-v1"
COMPARISON_SCHEMA_VERSION = "dual-ma-h2-flat-reentry-comparison-v1"
REENTRY_SCHEMA_VERSION = "dual-ma-h2-flat-reentry-analysis-v1"
CORPACTION_SCHEMA_VERSION = "dual-ma-h2-flat-reentry-corpaction-v1"

# Report-only re-render (added 2026-10-04).  The same-basis difference table used
# to print the stored 0-1 win-rate ratio with ``already_percent=True``, which
# shrank every gap by 100x (0.0025 read as "-0.00%").  This path never re-runs
# the engine: it reads one finished run's frozen artifacts, re-renders the report
# with the corrected "勝率差（百分點）" column and rewrites nothing upstream.
WIN_RATE_DELTA_COLUMN = "勝率差（百分點）"
REPORT_ONLY_EDITION = "v2 報告顯示勘誤版"
REPORT_ONLY_RUN_ID_DEFAULT = "h2-flat-reentry-vpivot-2022h2-v2-report-factfix"
REPORT_ONLY_MANIFEST_SCHEMA_VERSION = (
    "dual-ma-h2-flat-reentry-report-only-manifest-v1"
)
REPORT_ONLY_READBACK_SCHEMA_VERSION = (
    "dual-ma-h2-flat-reentry-report-only-readback-v1"
)
REPORT_ONLY_OUTPUT_FILES: tuple[str, ...] = (
    "manifest.json",
    "readback.json",
    "report_zh.md",
)

VARIANT_ID = "FLAT_REENTRY_AFTER_EXIT_WITHIN_EPISODE+EXIT_DOW_TRAIL"
ENTRY_VARIANT = EntryVariant.RECLAIM_PIVOT_V_CONFIRMED

STATUS_REENTRY_REJECTED_SAME_DAY = "REENTRY_REJECTED_SAME_DAY_AS_EXIT"
STATUS_REENTRY_REJECTED_REGIME_INVALIDATED = (
    "REENTRY_REJECTED_REGIME_INVALIDATED_EPISODE"
)
ENTRY_DECISION_STATUSES: tuple[str, ...] = (
    STATUS_FILLED,
    STATUS_NON_EXECUTABLE,
    STATUS_ADD_REJECTED_SINGLE_ENTRY,
    STATUS_REENTRY_REJECTED_SAME_DAY,
    STATUS_REENTRY_REJECTED_REGIME_INVALIDATED,
)

SPEC_SHA256_REENTRY = (
    "6376BDC1F45120A00AFA4468FCC9218E139BB58AADFF68ED22C268FE14469D87"
)
SPEC_BYTE_SIZE_REENTRY = 4046

BASELINE_RUN_ID = h2.RUN_ID_DEFAULT
BASELINE_RUN_RELATIVE = f"02-causal-backtest/{BASELINE_RUN_ID}"
BASELINE_TRADES_SHA256 = (
    "8F757478A54E9D5340CD6FEC522A32920A216B52B5066294D8F1BD3554D78119"
)
BASELINE_OPEN_POSITIONS_SHA256 = (
    "B18D387A9270EC856897CE8B61E0D3694D49D81728399A7DC18383FA7C7E5FFC"
)

H2_FIRST_DAY = h2.H2_FIRST_DAY
H2_LAST_DAY = h2.H2_LAST_DAY
AS_OF_CUTOFF = h2.AS_OF_CUTOFF
EXPECTED_H2_SESSION_COUNT = h2.EXPECTED_H2_SESSION_COUNT
SLIPPAGE_SCENARIOS = h2.SLIPPAGE_SCENARIOS
PRIMARY_SLIPPAGE_BPS = h2.PRIMARY_SLIPPAGE_BPS
STRATEGY_ORDER = h2.STRATEGY_ORDER
DESCRIPTIVE_BOTH = h2.DESCRIPTIVE_BOTH
STRATEGY_DISPLAY_LABELS = h2.STRATEGY_DISPLAY_LABELS

# Corporate-action classification labels (frozen conservative taxonomy).
CA_UNAFFECTED = "UNAFFECTED_NO_2022_OFFICIAL_ACTION"
CA_CASH_ONLY = corp.SOLVED_CASH_ONLY
CA_WITH_STOCK = corp.SOLVED_WITH_STOCK_RECEIVABLE
CA_UNRESOLVED = corp.UNRESOLVED
CA_NOT_REPRODUCED = "HOLD_CORPORATE_ACTION_CORRECTION_NOT_REPRODUCED"
CA_NON_PRIMARY = "NOT_EVALUATED_NON_PRIMARY_BPS"


class H2FlatReentryError(RuntimeError):
    """A structural problem with the re-entry run or one of its inputs."""


# ---------------------------------------------------------------------------
# The one rule change: re-entry after an exit inside the same episode
# ---------------------------------------------------------------------------


class ReentryAfterExitEngine(DualMABacktestEngine):
    """`v0.4.1-frozen` engine where an exited leg may be replaced once more.

    Every other code path stays the frozen engine's own: pullback/pivot entry
    state machine, sizing, cost model, structural stop, `EXIT_DOW_TRAIL`, the
    daily Dow defense and the SMA regime exit.  Only `_handle_entry_signal` is
    overridden, and only the *rejection* of an already-exited mother order is
    removed.

    Guards kept verbatim from the frozen ruling:

    * a signal raised while a leg is open is an addition and is rejected;
    * a signal on the very session the previous leg exited is rejected
      (no same-day exit-and-rebuy);
    * after a regime-failure exit the episode is over, so the run refuses any
      further entry and a fresh D -> E -> T is required instead.

    The state machine itself already clears `pullback`/`pivot_v_tracker` on the
    session a complete entry signal fires, so every accepted re-entry is a
    genuinely new pullback plus a newly confirmed pivot: an old T is never
    reused and no extra fresh-pivot threshold is added.
    """

    def __init__(
        self,
        *,
        slippage_bps: int = 0,
        entry_variant: EntryVariant = ENTRY_VARIANT,
        kd_2r_death_cross_exit: bool = False,
        kd_values_by_date: Mapping[
            date, tuple[Decimal | None, Decimal | None]
        ] | None = None,
    ) -> None:
        super().__init__(
            AddVariant.R1_AGGREGATE,
            ExitVariant.DOW_TRAIL,
            slippage_bps=slippage_bps,
            entry_variant=entry_variant,
            kd_2r_death_cross_exit=kd_2r_death_cross_exit,
            kd_values_by_date=kd_values_by_date,
        )
        self._last_closed: dict[StrategyKind, tuple[date, ExitReason, str]] = {}

    def _close_leg(
        self,
        result: Any,
        state: Any,
        leg: Any,
        bar: Any,
        reason: ExitReason,
        instrument_type: InstrumentType,
    ) -> None:
        super()._close_leg(result, state, leg, bar, reason, instrument_type)
        self._last_closed[state.kind] = (bar.date, ExitReason(reason), leg.leg_id)

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
        previous = self._last_closed.get(state.kind)
        if previous is not None:
            prior_exit_date, prior_exit_reason, prior_leg_id = previous
            if bar.date <= prior_exit_date:
                events.append(
                    {
                        "type": "ENTRY_DECISION",
                        "role": "MOTHER",
                        "status": STATUS_REENTRY_REJECTED_SAME_DAY,
                        "variant_id": VARIANT_ID,
                        "pullback_event_id": pullback_event_id,
                        "initial_stop": signal_stop,
                        "prior_leg_id": prior_leg_id,
                        "prior_exit_date": prior_exit_date,
                        "prior_exit_reason": prior_exit_reason.value,
                        **evidence,
                    }
                )
                return
            if prior_exit_reason == ExitReason.REGIME_EXIT:
                events.append(
                    {
                        "type": "ENTRY_DECISION",
                        "role": "MOTHER",
                        "status": STATUS_REENTRY_REJECTED_REGIME_INVALIDATED,
                        "variant_id": VARIANT_ID,
                        "pullback_event_id": pullback_event_id,
                        "initial_stop": signal_stop,
                        "prior_leg_id": prior_leg_id,
                        "prior_exit_date": prior_exit_date,
                        "prior_exit_reason": prior_exit_reason.value,
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


def _run_manifest(
    *,
    code: str,
    bars: Sequence[Any],
    slippage_bps: int,
    run_id: str,
    runner_commit: str,
    snapshot_id: str,
    data_hash: str,
) -> RunManifest:
    return RunManifest(
        run_id=(
            f"{run_id}:{code}:{slippage_bps}:"
            f"{bars[0].date.isoformat()}:{bars[-1].date.isoformat()}"
        ),
        strategy_version=STRATEGY_VERSION,
        spec_sha256=SPEC_SHA256,
        git_commit=runner_commit,
        universe_id=snapshot_id,
        data_snapshot_id=snapshot_id,
        data_start=bars[0].date,
        data_end=bars[-1].date,
        data_hash=data_hash,
        execution_model=EXECUTION_MODEL,
        cost_model=COST_MODEL,
        matrix_id=ENGINE_MATRIX_VEHICLE,
        slippage_bps=slippage_bps,
        strategy_parameters={
            "entry_variant": ENTRY_VARIANT.value,
            "pattern_id": "PIVOT_V_CONFIRM_V1",
            "add_variant": "NONE_FLAT_REENTRY_VARIANT",
            "exit_variant": "EXIT_DOW_TRAIL",
            "variant_id": VARIANT_ID,
        },
        metadata={
            "source_code": code,
            "variant_id": VARIANT_ID,
            "engine_matrix_vehicle": ENGINE_MATRIX_VEHICLE,
            "lifecycle_contract": (
                "docs/strategies/dual_ma/2022-h1-selection-episode-lifecycle-v1.md"
            ),
            "variant_contract": (
                "docs/strategies/dual_ma/"
                "2022-h2-flat-reentry-v-pivot-exploration-v1.md"
            ),
        },
    )


def run_variant_episode(
    *,
    episode: Any,
    bars: Sequence[Any],
    slippage_bps: int,
    run_id: str,
    runner_commit: str,
    snapshot_id: str,
    data_hash: str,
    instrument_parameter: Any = pilot.ENGINE_INSTRUMENT_PARAMETER,
) -> tuple[list[Any], dict[str, int], dict[str, str], dict[str, Any]]:
    """Replay one activated episode with the flat re-entry engine."""

    window = episode.window
    run_bars = [bar for bar in bars if bar.date <= window.regime_window_end]
    manifest = _run_manifest(
        code=episode.code,
        bars=run_bars,
        slippage_bps=slippage_bps,
        run_id=run_id,
        runner_commit=runner_commit,
        snapshot_id=snapshot_id,
        data_hash=data_hash,
    )
    result = ReentryAfterExitEngine(
        slippage_bps=slippage_bps, entry_variant=ENTRY_VARIANT
    ).run(
        episode.symbol,
        run_bars,
        run_manifest=manifest,
        instrument_type=instrument_parameter,
        backtest_start=window.regime_window_start,
        backtest_end=window.regime_window_end,
    )
    legs = [leg for leg in result.legs if leg.strategy.value == episode.strategy]
    tally = tally_entry_decisions(result.decisions, strategy=episode.strategy)
    pullbacks = pullback_event_by_leg(result.transactions, strategy=episode.strategy)
    return legs, tally, pullbacks, result


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------


TRADE_ROW_FIELDS: tuple[str, ...] = (
    "variant_id",
    "run_id",
    "leg_key",
    "code",
    "symbol",
    "strategy",
    "slippage_bps",
    "engine_matrix_vehicle",
    "entry_variant",
    "leg_id",
    "leg_number",
    "reentry_ordinal",
    "is_reentry",
    "episode_ordinal",
    "episode_key",
    "episode_window_leg_count",
    "source_selection_date_D",
    "monitor_effective_date_E",
    "regime_window_start",
    "regime_window_end",
    "entry_signal_date_T",
    "entry_reference_close",
    "entry_fill_price",
    "entry_fast_sma",
    "shares",
    "initial_stop",
    "initial_r",
    "pivot_low",
    "breakout_level",
    "pivot_confirmed_date",
    "pivot_group_start_date",
    "pivot_group_end_date",
    "pivot_left_arm_date",
    "pivot_right_arm_distance",
    "first_reclaim_date",
    "prior_leg_id",
    "prior_exit_date",
    "prior_exit_reason",
    "same_episode_after_exit",
    "exit_date",
    "exit_reason",
    "exit_reference_close",
    "exit_fill_price",
    "buy_gross",
    "buy_commission",
    "buy_total",
    "sell_gross",
    "sell_commission",
    "sell_tax",
    "sell_net",
    "total_fees_and_tax",
    "gross_pnl",
    "costed_net_pnl",
    "net_return_pct",
    "open_censored",
    "selection_groups",
    "pullback_event_id",
    "corporate_action_status",
    "corporate_action_events",
    "corporate_action_reason",
    "corrected_exit_date",
    "corrected_exit_reason",
    "cash_dividend",
    "receivable_shares",
    "receivable_value_at_exit_close",
    "realized_net_pnl_corrected",
    "economic_total_corrected",
    "correction_delta_vs_raw",
    "corrected_sequence_shift",
)


def _episode_key(episode: Any) -> str:
    return h2._episode_key(episode)


def _leg_entry_evidence(result: Any, leg: Any) -> Mapping[str, Any]:
    return h2._leg_entry_evidence(result, leg)


def _leg_row(
    *,
    leg: Any,
    episode: Any,
    slippage_bps: int,
    run_id: str,
    pullback_event_id: str | None,
    entry_evidence: Mapping[str, Any],
    groups: Sequence[str],
    reentry_ordinal: int,
    episode_window_leg_count: int,
    prior_leg: Any | None,
) -> dict[str, Any]:
    window = episode.window
    costed_net = leg.net_pnl
    net_return = (
        None
        if costed_net is None or Decimal(leg.buy_total) == 0
        else (Decimal(costed_net) / Decimal(leg.buy_total)) * Decimal(100)
    )
    gross = (
        None
        if leg.sell_gross is None
        else Decimal(leg.sell_gross) - Decimal(leg.buy_gross)
    )
    total_fees = Decimal(leg.buy_commission)
    if leg.sell_commission is not None:
        total_fees += Decimal(leg.sell_commission)
    if leg.sell_tax is not None:
        total_fees += Decimal(leg.sell_tax)
    is_reentry = reentry_ordinal > 1
    row = {
        "variant_id": VARIANT_ID,
        "run_id": run_id,
        "code": episode.code,
        "symbol": episode.symbol,
        "strategy": episode.strategy,
        "slippage_bps": slippage_bps,
        "engine_matrix_vehicle": ENGINE_MATRIX_VEHICLE,
        "entry_variant": ENTRY_VARIANT.value,
        "leg_id": leg.leg_id,
        "leg_number": leg.leg_number,
        "reentry_ordinal": reentry_ordinal,
        "is_reentry": is_reentry,
        "episode_ordinal": window.ordinal,
        "episode_key": _episode_key(episode),
        "episode_window_leg_count": episode_window_leg_count,
        "source_selection_date_D": window.source_selection_date.isoformat(),
        "monitor_effective_date_E": window.monitor_effective_from.isoformat(),
        "regime_window_start": window.regime_window_start.isoformat(),
        "regime_window_end": window.regime_window_end.isoformat(),
        "entry_signal_date_T": leg.entry_date.isoformat(),
        "entry_reference_close": h2._dstr(leg.entry_reference_close),
        "entry_fill_price": h2._dstr(leg.entry_fill_price),
        "entry_fast_sma": h2._dstr(entry_evidence.get("entry_fast_sma")),
        "shares": int(leg.shares),
        "initial_stop": h2._dstr(leg.initial_stop),
        "initial_r": h2._dstr(leg.initial_r),
        "pivot_low": h2._dstr(entry_evidence.get("pivot_low")),
        "breakout_level": h2._dstr(entry_evidence.get("breakout_level")),
        "pivot_confirmed_date": (
            None
            if entry_evidence.get("confirmed_date") is None
            else str(entry_evidence.get("confirmed_date"))
        ),
        "pivot_group_start_date": (
            None
            if entry_evidence.get("group_start_date") is None
            else str(entry_evidence.get("group_start_date"))
        ),
        "pivot_group_end_date": (
            None
            if entry_evidence.get("group_end_date") is None
            else str(entry_evidence.get("group_end_date"))
        ),
        "pivot_left_arm_date": (
            None
            if entry_evidence.get("left_arm_date") is None
            else str(entry_evidence.get("left_arm_date"))
        ),
        "pivot_right_arm_distance": entry_evidence.get("right_arm_distance"),
        "first_reclaim_date": (
            None
            if entry_evidence.get("first_reclaim_date") is None
            else str(entry_evidence.get("first_reclaim_date"))
        ),
        "prior_leg_id": None if prior_leg is None else prior_leg.leg_id,
        "prior_exit_date": (
            None
            if prior_leg is None or prior_leg.exit_date is None
            else prior_leg.exit_date.isoformat()
        ),
        "prior_exit_reason": (
            None
            if prior_leg is None or prior_leg.exit_reason is None
            else str(prior_leg.exit_reason.value)
        ),
        "same_episode_after_exit": bool(is_reentry),
        "exit_date": None if leg.exit_date is None else leg.exit_date.isoformat(),
        "exit_reason": (
            None if leg.exit_reason is None else str(leg.exit_reason.value)
        ),
        "exit_reference_close": h2._dstr(leg.exit_reference_close),
        "exit_fill_price": h2._dstr(leg.exit_fill_price),
        "buy_gross": h2._dstr(leg.buy_gross),
        "buy_commission": h2._dstr(leg.buy_commission),
        "buy_total": h2._dstr(leg.buy_total),
        "sell_gross": h2._dstr(leg.sell_gross),
        "sell_commission": h2._dstr(leg.sell_commission),
        "sell_tax": h2._dstr(leg.sell_tax),
        "sell_net": h2._dstr(leg.sell_net),
        "total_fees_and_tax": h2._dstr(total_fees),
        "gross_pnl": h2._dstr(gross),
        "costed_net_pnl": h2._dstr(costed_net),
        "net_return_pct": h2._dstr(net_return, 6),
        "open_censored": leg.exit_date is None,
        "selection_groups": "|".join(groups),
        "pullback_event_id": pullback_event_id,
        "corporate_action_status": "PENDING_NOT_EVALUATED",
        "corporate_action_events": None,
        "corporate_action_reason": None,
        "corrected_exit_date": None,
        "corrected_exit_reason": None,
        "cash_dividend": None,
        "receivable_shares": None,
        "receivable_value_at_exit_close": None,
        "realized_net_pnl_corrected": None,
        "economic_total_corrected": None,
        "correction_delta_vs_raw": None,
        "corrected_sequence_shift": False,
    }
    row["leg_key"] = corp._leg_key(row)
    return row


def collect_trade_rows(
    *,
    plan: Any,
    snapshot: Any,
    code_programs_by_date: Mapping[str, Mapping[str, Sequence[str]]],
    run_id: str,
    runner_commit: str,
    progress: Callable[[str, int, int, Mapping[str, Any]], None] | None = None,
    episode_limit: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Replay every activated episode at 0/10/20 bps with the flat re-entry engine."""

    episodes = list(plan.episodes)
    truncated = False
    if episode_limit is not None and episode_limit < len(episodes):
        episodes = episodes[: int(episode_limit)]
        truncated = True

    rows: list[dict[str, Any]] = []
    decision_totals: Counter[str] = Counter()
    decision_first_example: dict[str, dict[str, Any]] = {}
    episodes_with_leg: Counter[str] = Counter()
    legs_per_episode: Counter[tuple] = Counter()
    max_open_legs = 0
    filled_add_events = 0
    reentry_episodes: Counter[str] = Counter()
    funnel_by_strategy: dict[str, Counter] = {
        strategy.value: Counter() for strategy in STRATEGY_ORDER
    }

    for index, episode in enumerate(episodes, start=1):
        bars = snapshot.bars_by_symbol[episode.symbol]
        groups_today = tuple(
            code_programs_by_date.get(
                episode.window.source_selection_date.isoformat(), {}
            ).get(episode.code, ())
        )
        for slippage_bps in SLIPPAGE_SCENARIOS:
            legs, tally, pullbacks, result = run_variant_episode(
                episode=episode,
                bars=bars,
                slippage_bps=slippage_bps,
                run_id=run_id,
                runner_commit=runner_commit,
                snapshot_id=snapshot.snapshot_id,
                data_hash=snapshot.content_sha256,
            )
            for decision in result.decisions:
                for key in ("before_actions", "after_actions"):
                    snap = decision.get(key) or {}
                    max_open_legs = max(
                        max_open_legs, int(snap.get("open_leg_count") or 0)
                    )
                for event in decision.get("events") or ():
                    if not isinstance(event, Mapping):
                        continue
                    if (
                        event.get("type") == "ENTRY_DECISION"
                        and event.get("status") == STATUS_FILLED
                        and event.get("role") == "ADD"
                    ):
                        filled_add_events += 1
            for status, count in tally.items():
                key = f"{episode.strategy}|{slippage_bps}|{status}"
                decision_totals[key] += count
                if status != STATUS_FILLED and key not in decision_first_example:
                    decision_first_example[key] = {
                        "code": episode.code,
                        "symbol": episode.symbol,
                        "strategy": episode.strategy,
                        "episode_ordinal": episode.window.ordinal,
                        "source_selection_date_D": (
                            episode.window.source_selection_date.isoformat()
                        ),
                        "monitor_effective_date_E": (
                            episode.window.monitor_effective_from.isoformat()
                        ),
                        "regime_window_end": (
                            episode.window.regime_window_end.isoformat()
                        ),
                    }
            if slippage_bps == PRIMARY_SLIPPAGE_BPS:
                evidence = h2._episode_evidence(result, episode.strategy)
                bucket = funnel_by_strategy[episode.strategy]
                bucket["episodes"] += 1
                if evidence["saw_pullback"]:
                    bucket["episodes_with_pullback"] += 1
                if evidence["saw_reclaim_before_pivot"]:
                    bucket["episodes_reclaim_before_pivot"] += 1
                if evidence["saw_pivot_confirmed"]:
                    bucket["episodes_with_pivot_confirmed"] += 1
                if evidence["saw_entry_signal"]:
                    bucket["episodes_with_entry_signal"] += 1
                if evidence["saw_pivot_confirmed"] and not evidence["saw_entry_signal"]:
                    bucket["episodes_pivot_only_no_entry"] += 1
                if (
                    evidence["saw_reclaim_before_pivot"]
                    and not evidence["saw_pivot_confirmed"]
                ):
                    bucket["episodes_reclaim_only_no_pivot"] += 1
            if legs:
                episodes_with_leg[f"{episode.strategy}|{slippage_bps}"] += 1
                if slippage_bps == PRIMARY_SLIPPAGE_BPS:
                    funnel_by_strategy[episode.strategy][
                        "episodes_with_a_filled_leg"
                    ] += 1
            legs_per_episode[
                (
                    episode.code,
                    episode.strategy,
                    episode.window.ordinal,
                    slippage_bps,
                )
            ] += len(legs)
            if len(legs) > 1:
                reentry_episodes[f"{episode.strategy}|{slippage_bps}"] += 1
            ordered_legs = sorted(legs, key=lambda item: (item.entry_date, item.leg_id))
            for ordinal, leg in enumerate(ordered_legs, start=1):
                prior_leg = ordered_legs[ordinal - 2] if ordinal > 1 else None
                rows.append(
                    _leg_row(
                        leg=leg,
                        episode=episode,
                        slippage_bps=slippage_bps,
                        run_id=run_id,
                        pullback_event_id=pullbacks.get(leg.leg_id),
                        entry_evidence=_leg_entry_evidence(result, leg),
                        groups=groups_today,
                        reentry_ordinal=ordinal,
                        episode_window_leg_count=len(legs),
                        prior_leg=prior_leg,
                    )
                )
        if progress is not None and index % 50 == 0:
            progress("episodes", index, len(episodes), {"leg_rows": len(rows)})

    stats = {
        "episode_count_run": len(episodes),
        "episode_count_truncated": truncated,
        "leg_row_count": len(rows),
        "episodes_with_a_leg_by_strategy_and_slippage": dict(
            sorted(episodes_with_leg.items())
        ),
        "episodes_with_reentry_by_strategy_and_slippage": dict(
            sorted(reentry_episodes.items())
        ),
        "max_open_legs_observed": max_open_legs,
        "filled_add_events": filled_add_events,
        "entry_decision_totals": dict(sorted(decision_totals.items())),
        "entry_decision_first_example": decision_first_example,
        "funnel_by_strategy_0bps": {
            key: dict(sorted(value.items()))
            for key, value in sorted(funnel_by_strategy.items())
        },
    }
    return rows, stats


# ---------------------------------------------------------------------------
# Re-entry analysis and the same-basis single-entry comparison
# ---------------------------------------------------------------------------


def _read_csv_rows(path: str | Path) -> list[dict[str, str]]:
    with Path(path).open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _money(value: Any) -> Decimal:
    if value is None or value == "":
        return Decimal(0)
    return Decimal(str(value))


def build_reentry_analysis(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Everything the report needs about the re-entry legs at the 0 bps basis."""

    primary = [
        row for row in rows if int(row["slippage_bps"]) == PRIMARY_SLIPPAGE_BPS
    ]
    reentries = [row for row in primary if row["is_reentry"]]
    by_strategy: dict[str, dict[str, Any]] = {}
    for strategy in (StrategyKind.MEDIUM.value, StrategyKind.LONG.value):
        subset = [row for row in reentries if row["strategy"] == strategy]
        closed = [row for row in subset if not row["open_censored"]]
        pnls = [_money(row["costed_net_pnl"]) for row in closed]
        wins = [value for value in pnls if value > 0]
        losses = [value for value in pnls if value < 0]
        by_strategy[strategy] = {
            "reentry_leg_count": len(subset),
            "open_reentry_leg_count": len(subset) - len(closed),
            "closed_reentry_leg_count": len(closed),
            "win_count": len(wins),
            "loss_count": len(losses),
            "win_rate": (
                None
                if not closed
                else str(
                    (Decimal(len(wins)) / Decimal(len(closed))).quantize(
                        Decimal("0.000001")
                    )
                )
            ),
            "costed_net_pnl_total": str(sum(pnls, Decimal(0)).quantize(Decimal("0.0001"))),
            "fee_and_tax_total": str(
                sum((_money(row["total_fees_and_tax"]) for row in subset), Decimal(0)).quantize(
                    Decimal("0.0001")
                )
            ),
            "recovered_follow_through_count": len(wins),
            "episodes_with_reentry": len({row["episode_key"] for row in subset}),
        }
    all_pnls = [_money(row["costed_net_pnl"]) for row in reentries if not row["open_censored"]]
    prior_reasons = Counter(
        str(row["prior_exit_reason"])
        for row in reentries
        if row["prior_exit_reason"] is not None
    )
    # How the re-entry leg compared with the leg it replaced inside the episode.
    by_episode: dict[tuple, list[Mapping[str, Any]]] = {}
    for row in primary:
        by_episode.setdefault(
            (row["episode_key"], int(row["slippage_bps"])), []
        ).append(row)
    recovered_after_washout = 0
    compounded_loss_episodes = 0
    for key, bucket in by_episode.items():
        ordered = sorted(bucket, key=lambda item: int(item["reentry_ordinal"]))
        for position in range(1, len(ordered)):
            previous = ordered[position - 1]
            current = ordered[position]
            if previous["open_censored"] or current["open_censored"]:
                continue
            if _money(current["costed_net_pnl"]) > 0:
                recovered_after_washout += 1
            else:
                compounded_loss_episodes += 1
    return {
        "schema_version": REENTRY_SCHEMA_VERSION,
        "primary_slippage_bps": PRIMARY_SLIPPAGE_BPS,
        "reentry_leg_count": len(reentries),
        "episodes_with_reentry": len({row["episode_key"] for row in reentries}),
        "open_reentry_leg_count": len([row for row in reentries if row["open_censored"]]),
        "closed_reentry_leg_count": len(
            [row for row in reentries if not row["open_censored"]]
        ),
        "win_count": len([value for value in all_pnls if value > 0]),
        "loss_count": len([value for value in all_pnls if value < 0]),
        "flat_count": len([value for value in all_pnls if value == 0]),
        "costed_net_pnl_total": str(sum(all_pnls, Decimal(0)).quantize(Decimal("0.0001"))),
        "fee_and_tax_total": str(
            sum(
                (_money(row["total_fees_and_tax"]) for row in reentries), Decimal(0)
            ).quantize(Decimal("0.0001"))
        ),
        "recovered_after_washout_count": recovered_after_washout,
        "compounded_loss_count": compounded_loss_episodes,
        "prior_exit_reason_counts": dict(sorted(prior_reasons.items())),
        "by_strategy": by_strategy,
    }


SUMMARY_KEYS: tuple[str, ...] = (
    "closed_leg_count",
    "open_leg_count",
    "win_count",
    "loss_count",
    "flat_count",
    "win_rate",
    "mean_costed_net_pnl",
    "median_costed_net_pnl",
    "gross_profit",
    "gross_loss",
    "costed_net_pnl_total",
)


def _summary_index(
    summary: Sequence[Mapping[str, Any]],
) -> dict[tuple[str, int], Mapping[str, Any]]:
    return {
        (str(row["strategy"]), int(row["slippage_bps"])): row for row in summary
    }


def build_comparison(
    *,
    new_rows: Sequence[Mapping[str, Any]],
    baseline_rows: Sequence[Mapping[str, str]],
    baseline_sha256: str,
) -> dict[str, Any]:
    """Old single-entry vs new flat-re-entry on the identical 0/10/20 bps basis."""

    # The baseline rows are read straight from CSV, so their numeric columns
    # are strings; the shared summarizer expects the in-memory types.
    normalized_baseline = [
        {
            **row,
            "slippage_bps": int(row["slippage_bps"]),
            "open_censored": str(row["open_censored"]).lower() == "true",
        }
        for row in baseline_rows
    ]
    new_summary = h2.summarize_rows(new_rows)
    baseline_summary = h2.summarize_rows(normalized_baseline)
    new_index = _summary_index(new_summary)
    old_index = _summary_index(baseline_summary)
    pairs: list[dict[str, Any]] = []
    for strategy in (
        StrategyKind.MEDIUM.value,
        StrategyKind.LONG.value,
        DESCRIPTIVE_BOTH,
    ):
        for slippage_bps in SLIPPAGE_SCENARIOS:
            old = old_index.get((strategy, slippage_bps))
            new = new_index.get((strategy, slippage_bps))
            if old is None or new is None:
                continue
            delta: dict[str, Any] = {}
            for field in SUMMARY_KEYS:
                old_value = old.get(field)
                new_value = new.get(field)
                if old_value is None or new_value is None:
                    delta[field] = None
                    continue
                delta[field] = h2._dstr(_money(new_value) - _money(old_value))
            pairs.append(
                {
                    "strategy": strategy,
                    "slippage_bps": slippage_bps,
                    "baseline": {field: old.get(field) for field in SUMMARY_KEYS},
                    "reentry": {field: new.get(field) for field in SUMMARY_KEYS},
                    "delta": delta,
                    "baseline_fill_count": int(old["closed_leg_count"])
                    + int(old["open_leg_count"]),
                    "reentry_fill_count": int(new["closed_leg_count"])
                    + int(new["open_leg_count"]),
                }
            )
    return {
        "schema_version": COMPARISON_SCHEMA_VERSION,
        "primary_slippage_bps": PRIMARY_SLIPPAGE_BPS,
        "baseline_run_id": BASELINE_RUN_ID,
        "baseline_trades_sha256": baseline_sha256,
        "note": (
            "both columns are re-derived from the leg rows with the same frozen "
            "cost/exit terms and the same leg-row summarizer; the only difference "
            "is the entry rule variant, so the delta is attributable to the "
            "re-entry rule alone"
        ),
        "pairs": pairs,
    }


def build_reentry_spot_checks(
    *, rows: Sequence[Mapping[str, Any]], limit_per_strategy: int = 2
) -> dict[str, Any]:
    """Two real D -> E -> T -> exit -> T2 chains plus hand-costed arithmetic."""

    primary = [
        row
        for row in rows
        if int(row["slippage_bps"]) == PRIMARY_SLIPPAGE_BPS
        and row["is_reentry"]
        and not row["open_censored"]
    ]
    by_leg = {
        (row["episode_key"], int(row["slippage_bps"]), row["leg_id"]): row
        for row in rows
    }
    checks: list[dict[str, Any]] = []
    for strategy in STRATEGY_ORDER:
        candidates = [row for row in primary if row["strategy"] == strategy.value]
        candidates.sort(key=lambda row: (row["code"], row["entry_signal_date_T"]))
        for row in candidates[:limit_per_strategy]:
            prior = by_leg.get(
                (row["episode_key"], int(row["slippage_bps"]), row["prior_leg_id"])
            )
            if prior is None:
                continue
            exit_date = date.fromisoformat(str(prior["exit_date"]))
            t2_date = date.fromisoformat(str(row["entry_signal_date_T"]))
            hand_calc = _money(row["sell_net"]) - _money(row["buy_total"])
            checks.append(
                {
                    "kind": "D_TO_E_TO_T_TO_EXIT_TO_T2_REENTRY",
                    "strategy": strategy.value,
                    "code": row["code"],
                    "symbol": row["symbol"],
                    "source_selection_date_D": row["source_selection_date_D"],
                    "monitor_effective_date_E": row["monitor_effective_date_E"],
                    "regime_window_end": row["regime_window_end"],
                    "episode_ordinal": row["episode_ordinal"],
                    "first_leg_id": prior["leg_id"],
                    "first_pivot_confirmed_date_V": prior["pivot_confirmed_date"],
                    "first_entry_signal_date_T": prior["entry_signal_date_T"],
                    "first_entry_reference_close": prior["entry_reference_close"],
                    "first_exit_date": prior["exit_date"],
                    "first_exit_reason": prior["exit_reason"],
                    "first_costed_net_pnl": prior["costed_net_pnl"],
                    "reentry_ordinal": row["reentry_ordinal"],
                    "reentry_leg_id": row["leg_id"],
                    "second_pivot_confirmed_date_V2": row["pivot_confirmed_date"],
                    "second_entry_signal_date_T2": row["entry_signal_date_T"],
                    "second_entry_reference_close": row["entry_reference_close"],
                    "second_entry_fast_sma": row["entry_fast_sma"],
                    "second_exit_date": row["exit_date"],
                    "second_exit_reason": row["exit_reason"],
                    "second_shares": row["shares"],
                    "second_buy_total": row["buy_total"],
                    "second_sell_net": row["sell_net"],
                    "second_total_fees_and_tax": row["total_fees_and_tax"],
                    "second_costed_net_pnl": row["costed_net_pnl"],
                    "hand_calc_sell_net_minus_buy_total": h2._dstr(hand_calc),
                    "costed_net_pnl_matches_hand_calc": (
                        _money(row["costed_net_pnl"]) == hand_calc
                    ),
                    "T2_is_strictly_after_first_exit": t2_date > exit_date,
                    "T2_is_strictly_after_first_T": (
                        t2_date > date.fromisoformat(str(prior["entry_signal_date_T"]))
                    ),
                    "V2_is_at_or_after_first_exit": (
                        date.fromisoformat(str(row["pivot_confirmed_date"])) >= exit_date
                    ),
                    "close_is_strictly_above_fast_at_T2": (
                        _money(row["entry_reference_close"])
                        > _money(row["entry_fast_sma"])
                    ),
                    "note": (
                        "the second leg is a genuine new mother order inside the "
                        "same selection episode: a fresh pullback, a freshly "
                        "confirmed pivot, its own sizing and its own commission "
                        "and tax, entered strictly after the first leg exited"
                    ),
                }
            )
    return {
        "schema_version": SPOT_CHECK_SCHEMA_VERSION,
        "check_count": len(checks),
        "checks": checks,
    }


# ---------------------------------------------------------------------------
# Corporate actions on the new legs (frozen conservative taxonomy)
# ---------------------------------------------------------------------------


def _episode_stub(row: Mapping[str, Any]) -> Any:
    return SimpleNamespace(
        code=row["code"],
        symbol=row["symbol"],
        strategy=row["strategy"],
        window=SimpleNamespace(
            ordinal=row["episode_ordinal"],
            source_selection_date=date.fromisoformat(row["source_selection_date_D"]),
            monitor_effective_from=date.fromisoformat(row["monitor_effective_date_E"]),
            regime_window_start=date.fromisoformat(row["regime_window_start"]),
            regime_window_end=date.fromisoformat(row["regime_window_end"]),
        ),
    )


def _closes_upto(bars: Sequence[Any], day: date) -> Decimal | None:
    close = None
    for bar in bars:
        if bar.date <= day:
            close = bar.close
        else:
            break
    return close


def enrich_corporate_actions(
    *,
    rows: list[dict[str, Any]],
    snapshot: Any,
    workdata_root: str | Path,
    run_id: str,
    runner_commit: str,
    progress: Callable[[str, int, int, Mapping[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Classify every 0 bps leg against the two frozen official 2022 indexes.

    A leg whose holding interval covers an official action and whose terms are
    exactly resolvable from the frozen data is *locally re-run* on the
    value-equivalent re-based series; everything else is either untouched (no
    action) or named as unresolved and left on the raw baseline only.
    """

    action_events = corp.load_official_action_index(workdata_root)
    action_index = corp.index_by_market_code(action_events)
    zero_rows = [row for row in rows if int(row["slippage_bps"]) == PRIMARY_SLIPPAGE_BPS]
    for row in rows:
        if int(row["slippage_bps"]) != PRIMARY_SLIPPAGE_BPS:
            row["corporate_action_status"] = CA_NON_PRIMARY
    for row in zero_rows:
        row["corporate_action_status"] = CA_UNAFFECTED
    inventory = corp.build_event_inventory(zero_rows, action_index)
    grouped = corp.group_inventory_by_leg(inventory)
    by_key = {corp._leg_key(row): row for row in zero_rows}
    raw_entry_dates_by_episode: dict[str, set[str]] = {}
    for row in zero_rows:
        raw_entry_dates_by_episode.setdefault(row["episode_key"], set()).add(
            row["entry_signal_date_T"]
        )

    corrected: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    not_reproduced: list[dict[str, Any]] = []
    for index, (leg_key, events) in enumerate(sorted(grouped.items()), start=1):
        row = by_key[leg_key]
        raw_events = [dict(item["event"]) for item in events]
        row["corporate_action_events"] = "|".join(
            str(event["date"]) for event in raw_events
        )
        classification = corp.classify_leg(raw_events)
        if classification["correction_class"] == corp.UNRESOLVED:
            row["corporate_action_status"] = CA_UNRESOLVED
            row["corporate_action_reason"] = classification["reason"]
            unresolved.append(
                {
                    "leg_key": leg_key,
                    "code": row["code"],
                    "symbol": row["symbol"],
                    "strategy": row["strategy"],
                    "entry_signal_date_T": row["entry_signal_date_T"],
                    "exit_date": row["exit_date"],
                    "shares": int(row["shares"]),
                    "open_censored": bool(row["open_censored"]),
                    "original_costed_net_pnl": row["costed_net_pnl"],
                    "reason": classification["reason"],
                    "detail": classification["detail"],
                    "events": [str(event["date"]) for event in raw_events],
                }
            )
            continue

        pairs = list(zip(raw_events, classification["terms"]))
        bars = snapshot.bars_by_symbol[row["symbol"]]
        rebased = corp.rebase_bars(bars, pairs)
        corrected_legs_raw, _, _, _ = run_variant_episode(
            episode=_episode_stub(row),
            bars=rebased,
            slippage_bps=PRIMARY_SLIPPAGE_BPS,
            run_id=f"{run_id}-corpaction",
            runner_commit=runner_commit,
            snapshot_id=snapshot.snapshot_id,
            data_hash=snapshot.content_sha256,
        )
        entry_iso = str(row["entry_signal_date_T"])
        matches = [
            leg for leg in corrected_legs_raw if leg.entry_date.isoformat() == entry_iso
        ]
        if len(matches) != 1:
            row["corporate_action_status"] = CA_NOT_REPRODUCED
            row["corporate_action_reason"] = "LEG_NOT_REPRODUCED_OR_DUPLICATED"
            not_reproduced.append({"leg_key": leg_key, "matches": len(matches)})
            continue
        corrected_leg = matches[0]
        window_end = date.fromisoformat(row["regime_window_end"])
        if corrected_leg.exit_date is None and window_end < AS_OF_CUTOFF:
            row["corporate_action_status"] = CA_UNRESOLVED
            row["corporate_action_reason"] = corp.UNRESOLVED_EXIT_OUTSIDE_WINDOW
            unresolved.append(
                {
                    "leg_key": leg_key,
                    "code": row["code"],
                    "symbol": row["symbol"],
                    "strategy": row["strategy"],
                    "entry_signal_date_T": row["entry_signal_date_T"],
                    "exit_date": row["exit_date"],
                    "shares": int(row["shares"]),
                    "open_censored": bool(row["open_censored"]),
                    "original_costed_net_pnl": row["costed_net_pnl"],
                    "reason": corp.UNRESOLVED_EXIT_OUTSIDE_WINDOW,
                    "detail": (
                        "after re-basing the leg is still open on the last "
                        "session of the frozen episode window; the corrected "
                        "exit is never extrapolated"
                    ),
                    "events": [str(event["date"]) for event in raw_events],
                }
            )
            continue
        if corrected_leg.exit_date is None:
            raw_exit_close = _closes_upto(bars, window_end)
        else:
            raw_exit_close = corp._raw_close_on(bars, corrected_leg.exit_date)
        flows = corp.corrected_flows(
            shares=int(row["shares"]),
            buy_total=_money(row["buy_total"]),
            events_with_terms=pairs,
            raw_exit_close=raw_exit_close,
        )
        rebased_entry_dates = {leg.entry_date.isoformat() for leg in corrected_legs_raw}
        raw_entry_dates = raw_entry_dates_by_episode.get(row["episode_key"], set())
        sequence_shift = rebased_entry_dates != raw_entry_dates
        row["corporate_action_status"] = classification["correction_class"]
        row["corporate_action_reason"] = None
        row["corrected_exit_date"] = (
            corrected_leg.exit_date.isoformat() if corrected_leg.exit_date else None
        )
        row["corrected_exit_reason"] = (
            corrected_leg.exit_reason.value if corrected_leg.exit_reason else None
        )
        row["cash_dividend"] = h2._dstr(flows["cash_dividend"])
        row["receivable_shares"] = h2._dstr(flows["receivable_shares"], 6)
        row["receivable_value_at_exit_close"] = h2._dstr(
            flows["receivable_value_at_exit_close"]
        )
        row["realized_net_pnl_corrected"] = h2._dstr(flows["realized_net_pnl"])
        row["economic_total_corrected"] = h2._dstr(flows["economic_total"])
        row["correction_delta_vs_raw"] = h2._dstr(
            flows["realized_net_pnl"] - _money(row["costed_net_pnl"])
        )
        row["corrected_sequence_shift"] = bool(sequence_shift)
        corrected.append(
            {
                "leg_key": leg_key,
                "code": row["code"],
                "symbol": row["symbol"],
                "strategy": row["strategy"],
                "source_selection_date_D": row["source_selection_date_D"],
                "monitor_effective_date_E": row["monitor_effective_date_E"],
                "entry_signal_date_T": entry_iso,
                "is_reentry": bool(row["is_reentry"]),
                "open_censored": bool(row["open_censored"]),
                "correction_class": classification["correction_class"],
                "original_exit_date": row["exit_date"],
                "original_exit_reason": row["exit_reason"],
                "original_costed_net_pnl": row["costed_net_pnl"],
                "corrected_exit_date": row["corrected_exit_date"],
                "corrected_exit_reason": row["corrected_exit_reason"],
                "corrected_sequence_shift": bool(sequence_shift),
                "shares": int(row["shares"]),
                "buy_total": row["buy_total"],
                "sale_net": h2._dstr(flows["sale_net"]),
                "cash_dividend": h2._dstr(flows["cash_dividend"]),
                "receivable_shares": h2._dstr(flows["receivable_shares"], 6),
                "receivable_value_at_exit_close": h2._dstr(
                    flows["receivable_value_at_exit_close"]
                ),
                "realized_net_pnl": h2._dstr(flows["realized_net_pnl"]),
                "economic_total": h2._dstr(flows["economic_total"]),
                "events": [
                    {
                        "event_date": event["date"],
                        "event_kind": event["kind"],
                        "market": event["market"],
                        "cash_per_share": h2._dstr(term["cash_per_share"], 6),
                        "stock_per_share": h2._dstr(term["stock_per_share"], 6),
                        "basis": term["basis"],
                    }
                    for event, term in pairs
                ],
            }
        )
        if progress is not None and index % 25 == 0:
            progress("corpaction", index, len(grouped), {"solved": len(corrected)})

    affected_leg_count = len(grouped)
    corrected_realized = sum(
        (_money(item["realized_net_pnl"]) for item in corrected), Decimal(0)
    )
    summary_counts = Counter(
        item["correction_class"] for item in corrected
    )
    reason_counts = Counter(item["reason"] for item in unresolved)
    unresolved_raw = sum(
        (_money(item["original_costed_net_pnl"]) for item in unresolved), Decimal(0)
    )
    sample_status = Counter(row["corporate_action_status"] for row in zero_rows)
    return {
        "schema_version": CORPACTION_SCHEMA_VERSION,
        "primary_slippage_bps": PRIMARY_SLIPPAGE_BPS,
        "action_index_event_count_2022": len(action_events),
        "zero_bps_leg_count": len(zero_rows),
        "affected_leg_count": affected_leg_count,
        "resolved_leg_count": len(corrected),
        "unresolved_leg_count": len(unresolved),
        "not_reproduced_leg_count": len(not_reproduced),
        "status_counts": dict(sorted(sample_status.items())),
        "resolved_class_counts": dict(sorted(summary_counts.items())),
        "unresolved_reason_counts": dict(sorted(reason_counts.items())),
        "sequence_shifted_leg_count": len(
            [item for item in corrected if item["corrected_sequence_shift"]]
        ),
        "corrected_flows": {
            "sale_cash_net_total": h2._dstr(
                sum((_money(item["sale_net"]) for item in corrected), Decimal(0))
            ),
            "cash_dividend_receivable_total": h2._dstr(
                sum((_money(item["cash_dividend"]) for item in corrected), Decimal(0))
            ),
            "stock_receivable_shares_total": h2._dstr(
                sum(
                    (_money(item["receivable_shares"]) for item in corrected),
                    Decimal(0),
                ),
                6,
            ),
            "stock_receivable_value_total": h2._dstr(
                sum(
                    (
                        _money(item["receivable_value_at_exit_close"])
                        for item in corrected
                    ),
                    Decimal(0),
                )
            ),
            "realized_net_pnl_total": h2._dstr(corrected_realized),
            "economic_total_including_stock_receivable": h2._dstr(
                sum((_money(item["economic_total"]) for item in corrected), Decimal(0))
            ),
        },
        "raw_baseline_of_unresolved_legs": h2._dstr(unresolved_raw),
        "unresolved_legs": unresolved,
        "corrected_legs": corrected,
        "not_reproduced_legs": not_reproduced,
        "note": (
            "receivable cash dividend and free shares are listed separately and "
            "are never counted as credited cash; the unresolved legs stay on the "
            "raw unadjusted baseline and are never mixed into a readable subtotal"
        ),
    }


def build_corpaction_open_positions(
    *, rows: Sequence[Mapping[str, Any]], snapshot: Any, cutoff: date = AS_OF_CUTOFF
) -> dict[str, Any]:
    """Year-end open legs, with corporate-action receivables kept separate."""

    open_rows = [
        row
        for row in rows
        if row["open_censored"] and int(row["slippage_bps"]) == PRIMARY_SLIPPAGE_BPS
    ]
    positions: list[dict[str, Any]] = []
    for row in open_rows:
        bars = snapshot.bars_by_symbol.get(row["symbol"], ())
        close = _closes_upto(bars, cutoff)
        if close is None:
            positions.append(
                {
                    "leg_key": row["leg_key"],
                    "code": row["code"],
                    "symbol": row["symbol"],
                    "strategy": row["strategy"],
                    "is_reentry": bool(row["is_reentry"]),
                    "entry_signal_date_T": row["entry_signal_date_T"],
                    "cutoff_close": None,
                    "mark_to_market_net_pnl": None,
                    "corporate_action_status": row["corporate_action_status"],
                    "cash_dividend_receivable": row["cash_dividend"],
                    "stock_receivable_shares": row["receivable_shares"],
                    "note": "HOLD_NO_CUTOFF_CLOSE",
                }
            )
            continue
        shares = int(row["shares"])
        liquidation = CATHAY_WEB_028_TW_V1.sell(
            close, shares, InstrumentType.STOCK
        )
        positions.append(
            {
                "leg_key": row["leg_key"],
                "code": row["code"],
                "symbol": row["symbol"],
                "strategy": row["strategy"],
                "is_reentry": bool(row["is_reentry"]),
                "entry_signal_date_T": row["entry_signal_date_T"],
                "shares": shares,
                "buy_total": row["buy_total"],
                "cutoff_close": h2._dstr(close),
                "liquidation_cash_at_cutoff": h2._dstr(liquidation.cash_amount),
                "mark_to_market_net_pnl": h2._dstr(
                    liquidation.cash_amount - _money(row["buy_total"])
                ),
                "corporate_action_status": row["corporate_action_status"],
                "cash_dividend_receivable": row["cash_dividend"],
                "stock_receivable_shares": row["receivable_shares"],
                "stock_receivable_value_at_cutoff": (
                    None
                    if row["receivable_shares"] is None
                    else h2._dstr(_money(row["receivable_shares"]) * close)
                ),
            }
        )
    return {
        "cutoff_date": cutoff.isoformat(),
        "cutoff_price_basis": "unadjusted vendor close proxy (2022-12-30 session)",
        "open_position_count": len(positions),
        "open_reentry_position_count": len(
            [item for item in positions if item["is_reentry"]]
        ),
        "positions": positions,
    }


def build_rejections(
    *, stats: Mapping[str, Any], plan: Any, baseline_leg_count: int
) -> dict[str, Any]:
    totals = dict(stats["entry_decision_totals"])
    by_strategy: dict[str, dict[str, int]] = {}
    for key, count in totals.items():
        strategy, _, status = key.partition("|")
        by_strategy.setdefault(strategy, {})[status] = count
    lifecycle_reason = Counter(
        str(record["reason"])
        for record in plan.selection_records
        if str(record["status"]) != "ACTIVATED"
    )
    return {
        "schema_version": REJECTIONS_SCHEMA_VERSION,
        "variant_id": VARIANT_ID,
        "entry_variant": ENTRY_VARIANT.value,
        "entry_decision_totals_by_strategy_slippage_status": totals,
        "entry_decision_totals_by_strategy": {
            key: dict(sorted(value.items()))
            for key, value in sorted(by_strategy.items())
        },
        "rejected_additions_total": sum(
            count
            for key, count in totals.items()
            if key.endswith(STATUS_ADD_REJECTED_SINGLE_ENTRY)
        ),
        "rejected_same_day_reentries_total": sum(
            count
            for key, count in totals.items()
            if key.endswith(STATUS_REENTRY_REJECTED_SAME_DAY)
        ),
        "rejected_regime_invalidated_reentries_total": sum(
            count
            for key, count in totals.items()
            if key.endswith(STATUS_REENTRY_REJECTED_REGIME_INVALIDATED)
        ),
        "non_executable_signals_total": sum(
            count
            for key, count in totals.items()
            if key.endswith(STATUS_NON_EXECUTABLE)
        ),
        "filled_mother_orders_total": sum(
            count for key, count in totals.items() if key.endswith(STATUS_FILLED)
        ),
        "filled_add_events_total": int(stats["filled_add_events"]),
        "max_open_legs_observed": int(stats["max_open_legs_observed"]),
        "first_example_by_status": dict(stats["entry_decision_first_example"]),
        "baseline_leg_count_0bps": int(baseline_leg_count),
        "lifecycle_named_holds": {
            "status_counts": dict(
                sorted(
                    Counter(
                        str(record["status"]) for record in plan.selection_records
                    ).items()
                )
            ),
            "reason_counts": dict(sorted(lifecycle_reason.items())),
            "hold_counts": {
                key: value
                for key, value in sorted(lifecycle_reason.items())
                if key.startswith("HOLD")
            },
            "skipped_counts": {
                key: value
                for key, value in sorted(lifecycle_reason.items())
                if key.startswith("SKIPPED")
            },
        },
    }


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def _cell(value: Any) -> str:
    if value is None or value == "":
        return "-"
    return str(value).replace("|", "/")


def _detail_table_lines(detail_rows: Sequence[Mapping[str, Any]]) -> list[str]:
    lines: list[str] = []
    lines.append(
        "| 唯一鍵 | 策略 | 代號 | 證券 | 組別 | D | E | V | T | 進場價 | 股數 | "
        "買進成本 | 費稅 | 出場日 | 出場價 | 出場原因 | 賣出淨額 | 成本後損益 | "
        "重進 | 前次出場 | 公司行動 |"
    )
    lines.append("|" + "---|" * 21)
    for row in detail_rows:
        lines.append(
            "| "
            + " | ".join(
                [
                    _cell(row["leg_key"]),
                    _cell(row["strategy"]),
                    _cell(row["code"]),
                    _cell(row["symbol"]),
                    _cell(row["selection_groups"]),
                    _cell(row["source_selection_date_D"]),
                    _cell(row["monitor_effective_date_E"]),
                    _cell(row["pivot_confirmed_date"]),
                    _cell(row["entry_signal_date_T"]),
                    _cell(row["entry_fill_price"]),
                    _cell(row["shares"]),
                    _cell(row["buy_total"]),
                    _cell(row["total_fees_and_tax"]),
                    _cell(row["exit_date"]),
                    _cell(row["exit_fill_price"]),
                    _cell(row["exit_reason"]),
                    _cell(row["sell_net"]),
                    _cell(row["costed_net_pnl"]),
                    "是" if row["is_reentry"] else "否",
                    _cell(
                        None
                        if row["prior_exit_date"] is None
                        else f"{row['prior_leg_id']}@{row['prior_exit_date']}({row['prior_exit_reason']})"
                    ),
                    _cell(row["corporate_action_status"]),
                ]
            )
            + " |"
        )
    return lines


def win_rate_delta_pp(
    baseline_win_rate: Any, reentry_win_rate: Any, places: int = 2
) -> str | None:
    """Signed gap between two 0-1 win rates, expressed in percentage points.

    ``reentry_comparison.json`` stores ``delta.win_rate`` as a *ratio* gap
    rounded to four places, so ``-0.0025`` is really -0.25 percentage points.
    Rendering that ratio with ``_pct(..., already_percent=True)`` printed
    "-0.00%" and shrank every gap by 100x.  The display therefore recomputes the
    gap from the two ratios that section two already prints and scales it by
    100, i.e. exactly ``(new - old) x 100``.
    """

    if baseline_win_rate in (None, "") or reentry_win_rate in (None, ""):
        return None
    gap = (
        Decimal(str(reentry_win_rate)) - Decimal(str(baseline_win_rate))
    ) * Decimal(100)
    return str(gap.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def comparison_delta_table_lines(comparison: Mapping[str, Any]) -> list[str]:
    """The same-basis difference table, win-rate gap in percentage points."""

    label = {DESCRIPTIVE_BOTH: "兩策略交易合計（描述性）"}
    lines = [
        "| 策略 | bps | 成交筆數差 | 已平倉差 | 未平倉差 | "
        + WIN_RATE_DELTA_COLUMN
        + " | 成本後淨損益差(元) |",
        "|---|---|---|---|---|---|---|",
    ]
    for pair in comparison["pairs"]:
        delta = pair["delta"]
        gap = win_rate_delta_pp(
            pair["baseline"].get("win_rate"), pair["reentry"].get("win_rate")
        )
        lines.append(
            "| {s} | {b} | {f} | {c} | {o} | {w} | {n} |".format(
                s=label.get(pair["strategy"], pair["strategy"]),
                b=pair["slippage_bps"],
                f=pair["reentry_fill_count"] - pair["baseline_fill_count"],
                c=delta["closed_leg_count"],
                o=delta["open_leg_count"],
                w="-" if gap is None else gap,
                n=delta["costed_net_pnl_total"],
            )
        )
    return lines


def render_report_zh(
    *,
    manifest: Mapping[str, Any],
    summary: Sequence[Mapping[str, Any]],
    comparison: Mapping[str, Any],
    reentry: Mapping[str, Any],
    funnel: Mapping[str, Any],
    groups: Mapping[str, Any],
    top_movers: Mapping[str, Any],
    exit_reasons: Mapping[str, Any],
    open_positions: Mapping[str, Any],
    corp_summary: Mapping[str, Any],
    corp_open: Mapping[str, Any],
    rejections: Mapping[str, Any],
    spot_checks: Mapping[str, Any],
    preflight: Mapping[str, Any],
    git_state: Mapping[str, Any],
    detail_rows: Sequence[Mapping[str, Any]],
) -> str:
    primary = {
        row["strategy"]: row
        for row in summary
        if int(row["slippage_bps"]) == PRIMARY_SLIPPAGE_BPS
    }
    lines: list[str] = []
    report_only = manifest.get("report_only_factfix")
    title = "# 2022 下半年「站回均線＋V 型樞紐」出場後可再進場探索回測 v1"
    if report_only:
        title = title + "（" + REPORT_ONLY_EDITION + "）"
    lines.append(title)
    lines.append("")
    lines.append(
        "本報告是 00 於績效計算前凍結的探索研究結果，research_only=true、"
        "point_in_time_verified=false。它回答的是：在 2022/07/01 到 2022/12/30 的 "
        "129 個官方選股日裡，沿用單筆版完全相同的 01 名單、價格快照、D→E→V→T "
        "因果狀態機、成本、出場與 2022-12-30 截點，只把「同一已啟動選股 episode "
        "內出場後不得再買」放寬為：持倉中仍不加碼，但前一筆已出場、原 episode 仍"
        "有效、快線仍大於慢線，且原 V＋站回快線狀態機在出場日之後的官方交易日再次"
        "發出完整新進場訊號時，可在同一 episode 再買一筆新的母單。這不是正式歷史可"
        "交易績效，也不是任何帳戶的報酬或最大回撤。"
    )
    lines.append("")
    lines.append("## 一、白話結論（0 bps 主基準）")
    lines.append("")
    medium_fills = (
        int(primary.get(StrategyKind.MEDIUM.value, {}).get("closed_leg_count", 0))
        + int(primary.get(StrategyKind.MEDIUM.value, {}).get("open_leg_count", 0))
    )
    long_fills = (
        int(primary.get(StrategyKind.LONG.value, {}).get("closed_leg_count", 0))
        + int(primary.get(StrategyKind.LONG.value, {}).get("open_leg_count", 0))
    )
    lines.append(
        "本次 129 個官方選股日、0 bps 主基準的實際成交：中期 5/21 共 {mc} 筆、"
        "長期 21/144 共 {lc} 筆（含已平倉與未平倉，成本已含手續費與證交稅）；"
        "其中 0 bps 的再進場母單共 {rc} 筆。下表先分別列中期與長期，合計列只是把"
        "兩策略交易描述性相加，不是投資組合或帳戶報酬。".format(
            mc=medium_fills, lc=long_fills, rc=reentry["reentry_leg_count"]
        )
    )
    lines.append("")
    lines.append(
        "| 策略 | 已平倉筆數 | 未平倉 | 勝率 | 平均成本後損益(元) | "
        "中位成本後損益(元) | 總獲利(元) | 總虧損(元) | 成本後淨損益(元) |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for strategy in (
        StrategyKind.MEDIUM.value,
        StrategyKind.LONG.value,
        DESCRIPTIVE_BOTH,
    ):
        row = primary.get(strategy)
        if row is None:
            continue
        lines.append(
            "| {s} | {c} | {o} | {w} | {m} | {md} | {gp} | {gl} | {n} |".format(
                s=STRATEGY_DISPLAY_LABELS.get(row["strategy"], row["strategy"]),
                c=h2._fmt(row["closed_leg_count"]),
                o=h2._fmt(row["open_leg_count"]),
                w=h2._pct(row["win_rate"]),
                m=h2._fmt(row["mean_costed_net_pnl"]),
                md=h2._fmt(row["median_costed_net_pnl"]),
                gp=h2._fmt(row["gross_profit"]),
                gl=h2._fmt(row["gross_loss"]),
                n=h2._fmt(row["costed_net_pnl_total"]),
            )
        )
    lines.append("")
    lines.append(
        "BOTH_DESCRIPTIVE_ONLY 只是把兩策略各自的交易描述性相加；本輪沒有共用資金、"
        "部位衝突或先後順序規則，因此它不是單一資金帳戶的總報酬，也不可與成本敏感度"
        "的三欄相加。"
    )
    lines.append("")
    lines.append("## 二、與舊單筆版的同口徑對照（0 / 10 / 20 bps）")
    lines.append("")
    lines.append(
        "兩欄都用同一份凍結輸入、同一組成本／出場條款與同一個逐筆統計函式重新"
        "產生；唯一差異就是進場規則，所以差值可直接歸因於「出場後可再進場」這一條。"
    )
    lines.append("")
    lines.append(
        "| 策略 | bps | 版本 | 成交筆數 | 已平倉 | 未平倉 | 勝率 | "
        "平均成本後損益(元) | 中位成本後損益(元) | 成本後淨損益(元) |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    label = {DESCRIPTIVE_BOTH: "兩策略交易合計（描述性）"}
    for pair in comparison["pairs"]:
        name = label.get(pair["strategy"], pair["strategy"])
        for version in ("baseline", "reentry"):
            payload = pair[version]
            lines.append(
                "| {s} | {b} | {v} | {f} | {c} | {o} | {w} | {m} | {md} | {n} |".format(
                    s=name,
                    b=pair["slippage_bps"],
                    v="舊單筆版" if version == "baseline" else "新可再進場版",
                    f=pair["baseline_fill_count"]
                    if version == "baseline"
                    else pair["reentry_fill_count"],
                    c=h2._fmt(payload["closed_leg_count"]),
                    o=h2._fmt(payload["open_leg_count"]),
                    w=h2._pct(payload["win_rate"]),
                    m=h2._fmt(payload["mean_costed_net_pnl"]),
                    md=h2._fmt(payload["median_costed_net_pnl"]),
                    n=h2._fmt(payload["costed_net_pnl_total"]),
                )
            )
    lines.append("")
    lines.append("### 新可再進場版減舊單筆版（同口徑差值）")
    lines.append("")
    lines.extend(comparison_delta_table_lines(comparison))
    lines.append("")
    lines.append("## 三、再進場分析（0 bps）")
    lines.append("")
    lines.append(
        "- 再進場母單筆數：{n}（已平倉 {c}、未平倉 {o}），分布在 {e} 個選股 "
        "episode。".format(
            n=reentry["reentry_leg_count"],
            c=reentry["closed_reentry_leg_count"],
            o=reentry["open_reentry_leg_count"],
            e=reentry["episodes_with_reentry"],
        )
    )
    lines.append(
        "- 已平倉再進場勝負：勝 {w}、負 {l}、平 {f}；成本後淨損益合計 {p} 元；"
        "這些筆數另外付出 {fee} 元手續費與證交稅。".format(
            w=reentry["win_count"],
            l=reentry["loss_count"],
            f=reentry["flat_count"],
            p=reentry["costed_net_pnl_total"],
            fee=reentry["fee_and_tax_total"],
        )
    )
    lines.append(
        "- 被洗出後再進場、且該筆轉為獲利（抓到後續波段）的有 {r} 筆；"
        "再進場後仍虧損（連續二次虧損）的有 {c} 筆。".format(
            r=reentry["recovered_after_washout_count"],
            c=reentry["compounded_loss_count"],
        )
    )
    lines.append(
        "- 再進場前一次的出場原因分布：{d}。".format(
            d="、".join(
                f"{key} {value} 筆"
                for key, value in reentry["prior_exit_reason_counts"].items()
            )
            or "無"
        )
    )
    lines.append(
        "- 再進場不得與前一次出場同日，也不得沿用舊 T；每筆皆為新的母單，重新"
        "定股數、風險與費稅，不可把原單損益改寫或合併。"
    )
    lines.append("")
    lines.append("| 策略 | 再進場筆數 | 已平倉 | 未平倉 | 勝 | 負 | 勝率 | 成本後淨損益(元) | 費稅合計(元) |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for strategy in (StrategyKind.MEDIUM.value, StrategyKind.LONG.value):
        item = reentry["by_strategy"][strategy]
        lines.append(
            "| {s} | {n} | {c} | {o} | {w} | {l} | {r} | {p} | {f} |".format(
                s=strategy,
                n=item["reentry_leg_count"],
                c=item["closed_reentry_leg_count"],
                o=item["open_reentry_leg_count"],
                w=item["win_count"],
                l=item["loss_count"],
                r=h2._pct(item["win_rate"]),
                p=item["costed_net_pnl_total"],
                f=item["fee_and_tax_total"],
            )
        )
    lines.append("")
    lines.append("## 四、成本敏感度（每邊 10 / 20 bps，僅敏感度）")
    lines.append("")
    lines.append(
        "| 策略 | bps | 已平倉 | 勝率 | 平均成本後損益(元) | 中位成本後損益(元) | "
        "總獲利(元) | 總虧損(元) | 成本後淨損益(元) |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for row in summary:
        lines.append(
            "| {s} | {b} | {c} | {w} | {m} | {md} | {gp} | {gl} | {n} |".format(
                s=STRATEGY_DISPLAY_LABELS.get(row["strategy"], row["strategy"]),
                b=row["slippage_bps"],
                c=h2._fmt(row["closed_leg_count"]),
                w=h2._pct(row["win_rate"]),
                m=h2._fmt(row["mean_costed_net_pnl"]),
                md=h2._fmt(row["median_costed_net_pnl"]),
                gp=h2._fmt(row["gross_profit"]),
                gl=h2._fmt(row["gross_loss"]),
                n=h2._fmt(row["costed_net_pnl_total"]),
            )
        )
    lines.append("")
    lines.append("### 出場原因分布（0 bps，已平倉）")
    lines.append("")
    lines.append("| 策略 | 出場原因 | 筆數 | 成本後淨損益(元) |")
    lines.append("|---|---|---|---|")
    for strategy in (StrategyKind.MEDIUM.value, StrategyKind.LONG.value):
        for item in exit_reasons["by_strategy"].get(strategy, []):
            lines.append(
                "| {s} | {r} | {c} | {n} |".format(
                    s=strategy,
                    r=item["exit_reason"],
                    c=item["count"],
                    n=item["costed_net_pnl_total"],
                )
            )
    lines.append("")
    lines.append("## 五、六個可用選股組別（重複標籤，不可相加）")
    lines.append("")
    lines.append(
        "| 組別 | 代號 | 成交腿(0 bps) | 已平倉 | 未平倉 | 勝率 | 成本後淨損益(元) |"
    )
    lines.append("|---|---|---|---|---|---|---|")
    for row in groups["available_group_rows"]:
        lines.append(
            "| {lbl} | {key} | {legs} | {cl} | {op} | {w} | {n} |".format(
                lbl=row["group_label_zh"],
                key=row["group_key"],
                legs=h2._fmt(row["leg_count_primary_bps"]),
                cl=h2._fmt(row["closed_leg_count"]),
                op=h2._fmt(row["open_leg_count"]),
                w=h2._pct(row["win_rate"]),
                n=h2._fmt(row["costed_net_pnl_total"]),
            )
        )
    for row in groups["unavailable_group_rows"]:
        status_zh = "缺期（UNAVAILABLE）" if row["status"] == "UNAVAILABLE" else "失敗（FAILED）"
        lines.append(
            "| {lbl} | {key} | 無 | 無 | 無 | 無 | 2022 H2 全期{st}，"
            "本次沒有任何選股候選；不以零交易呈現 |".format(
                lbl=row["group_label_zh"], key=row["group_key"], st=status_zh
            )
        )
    lines.append("")
    lines.append(
        "同一檔股票在 D 日可能同時被多個組別選中，會分別列在每個組別下一次；這是"
        "多標籤揭露，不可相加當成單一投組，也不可當成八組完整績效。chip_4 缺期、"
        "curated 失敗，本輪仍是每日 6/8 的部分探索樣本。"
    )
    lines.append("")
    lines.append("## 六、十大賺錢與十大虧損（0 bps，已平倉）")
    lines.append("")
    for title, key in (("十大賺錢", "winners"), ("十大虧損", "losers")):
        lines.append(f"### {title}")
        lines.append("")
        lines.append(
            "| 代號 | 證券 | 策略 | D 選股 | T 進場 | 出場 | 原因 | 股數 | "
            "成本後損益(元) | 報酬 |"
        )
        lines.append("|---|---|---|---|---|---|---|---|---|---|")
        for row in top_movers[key]:
            lines.append(
                "| {c} | {s} | {st} | {d} | {t} | {x} | {r} | {sh} | {p} | {ret} |".format(
                    c=row["code"],
                    s=row["symbol"],
                    st=row["strategy"],
                    d=row["source_selection_date_D"],
                    t=row["entry_signal_date_T"],
                    x=row["exit_date"],
                    r=row["exit_reason"],
                    sh=row["shares"],
                    p=row["costed_net_pnl"],
                    ret=h2._pct(row["net_return_pct"], already_percent=True),
                )
            )
        lines.append("")
    lines.append("## 七、公司行動狀態與未解列（0 bps）")
    lines.append("")
    lines.append(
        "沿用已核准的 2022 官方除權息事件與保守分類。應收現金股利、應收配股與賣出"
        "現金分開列示，不把應收當入帳現金；未解列不得混入可解讀小計，原始未還原結果"
        "只能當比較基準。"
    )
    lines.append("")
    lines.append(
        "- 0 bps 交易中，持有期命中 2022 官方除權息事件的有 {a} 筆；其中可局部還原"
        " {s} 筆、未解 {u} 筆、無法重現 {n} 筆。".format(
            a=corp_summary["affected_leg_count"],
            s=corp_summary["resolved_leg_count"],
            u=corp_summary["unresolved_leg_count"],
            n=corp_summary["not_reproduced_leg_count"],
        )
    )
    flows = corp_summary["corrected_flows"]
    lines.append(
        "- 可局部還原母體的現金流（僅 {s} 筆，未解列不列入）：賣出現金淨額 {sale} 元、"
        "應收現金股利 {cash} 元、應收配股 {shares} 股（以出場價估計 {stock} 元）；"
        "含應收現金股利的權益損益 {realized} 元；再加應收配股估值後的經濟合計 "
        "{econ} 元。".format(
            s=corp_summary["resolved_leg_count"],
            sale=flows["sale_cash_net_total"],
            cash=flows["cash_dividend_receivable_total"],
            shares=flows["stock_receivable_shares_total"],
            stock=flows["stock_receivable_value_total"],
            realized=flows["realized_net_pnl_total"],
            econ=flows["economic_total_including_stock_receivable"],
        )
    )
    lines.append(
        "- 未解交易維持在原始未還原價格上的成本後損益合計 {raw} 元，單獨列出，"
        "不併入上面的可解讀小計，也不得冒充完整修正後績效。".format(
            raw=corp_summary["raw_baseline_of_unresolved_legs"]
        )
    )
    if corp_summary["sequence_shifted_leg_count"]:
        lines.append(
            "- 另有 {n} 筆在還原價格後，同一 episode 的再進場序列日期會改變；本輪僅"
            "逐筆標示此平移風險，不把它當成已完成的整體修正。".format(
                n=corp_summary["sequence_shifted_leg_count"]
            )
        )
    lines.append("")
    lines.append(
        "| 代號 | 證券 | 策略 | T 進場 | 原始出場 | 原始損益(元) | 類別 | 修正後出場 | "
        "賣出現金(元) | 應收股利(元) | 應收配股(股) |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for item in corp_summary["corrected_legs"]:
        lines.append(
            "| {c} | {s} | {st} | {t} | {x} | {p} | {k} | {cx} | {sale} | {div} | {sh} |".format(
                c=item["code"],
                s=item["symbol"],
                st=item["strategy"],
                t=item["entry_signal_date_T"],
                x=item["original_exit_date"],
                p=item["original_costed_net_pnl"],
                k=item["correction_class"],
                cx=item["corrected_exit_date"],
                sale=item["sale_net"],
                div=item["cash_dividend"],
                sh=item["receivable_shares"],
            )
        )
    lines.append("")
    lines.append("### 未解列（具名，不含臆造）")
    lines.append("")
    lines.append("| 代號 | 證券 | 策略 | T 進場 | 出場 | 原始損益(元) | 未解原因 | 事件 |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for item in corp_summary["unresolved_legs"]:
        lines.append(
            "| {c} | {s} | {st} | {t} | {x} | {p} | {r} | {e} |".format(
                c=item["code"],
                s=item["symbol"],
                st=item["strategy"],
                t=item["entry_signal_date_T"],
                x=item["exit_date"],
                p=item["original_costed_net_pnl"],
                r=item["reason"],
                e="/".join(item["events"]),
            )
        )
    lines.append("")
    lines.append("## 八、2022/12/30 未平倉獨立估值")
    lines.append("")
    lines.append(
        "截至截點仍持有的部位不當作已平倉，也不以 2023 之後的價格補平倉。"
    )
    lines.append(
        "- 未平倉筆數：{n}（其中再進場母單 {r} 筆）".format(
            n=corp_open["open_position_count"],
            r=corp_open["open_reentry_position_count"],
        )
    )
    lines.append(
        "- 以 2022-12-30 收盤推估的未實現損益：中期 {m} 元、長期 {l} 元".format(
            m=open_positions["medium_mark_to_market_net_pnl"],
            l=open_positions["long_mark_to_market_net_pnl"],
        )
    )
    lines.append("")
    lines.append(
        "| 代號 | 證券 | 策略 | T 進場 | 股數 | 買進成本(元) | 截點收盤 | 未實現損益(元) | "
        "公司行動 | 應收股利(元) | 應收配股(股) |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for item in corp_open["positions"]:
        lines.append(
            "| {c} | {s} | {st} | {t} | {sh} | {buy} | {close} | {pnl} | {ca} | {div} | {rs} |".format(
                c=item.get("code"),
                s=item.get("symbol"),
                st=item.get("strategy"),
                t=item.get("entry_signal_date_T"),
                sh=item.get("shares"),
                buy=item.get("buy_total"),
                close=item.get("cutoff_close"),
                pnl=item.get("mark_to_market_net_pnl"),
                ca=item.get("corporate_action_status"),
                div=item.get("cash_dividend_receivable"),
                rs=item.get("stock_receivable_shares"),
            )
        )
    lines.append("")
    lines.append("## 九、典型序列抽查：D → E → T → 出場 → T2")
    lines.append("")
    chains = spot_checks["reentry_chains"]["checks"]
    if not chains:
        lines.append("本次 0 bps 樣本沒有可列示的再進場序列。")
    for check in chains:
        lines.append(
            "- {code} {symbol}（{strategy}，episode {ep}）：D={d} → E={e} → "
            "第一腿 T={t1}（V={v1}，收盤 {close1}）→ 出場 {x1}（{r1}）→ "
            "第二腿 T2={t2}（V2={v2}，收盤 {close2} > 快線 {fast2}）→ 出場 {x2}"
            "（{r2}）。第二腿股數 {sh}，買進總成本 {buy} 元，賣出淨額 {sell} 元，"
            "費稅 {fee} 元，成本後損益 {pnl} 元（手算相符={ok}；T2 嚴格晚於第一次"
            "出場={after}）。".format(
                code=check["code"],
                symbol=check["symbol"],
                strategy=check["strategy"],
                ep=check["episode_ordinal"],
                d=check["source_selection_date_D"],
                e=check["monitor_effective_date_E"],
                t1=check["first_entry_signal_date_T"],
                v1=check["first_pivot_confirmed_date_V"],
                close1=check["first_entry_reference_close"],
                x1=check["first_exit_date"],
                r1=check["first_exit_reason"],
                t2=check["second_entry_signal_date_T2"],
                v2=check["second_pivot_confirmed_date_V2"],
                close2=check["second_entry_reference_close"],
                fast2=check["second_entry_fast_sma"],
                x2=check["second_exit_date"],
                r2=check["second_exit_reason"],
                sh=check["second_shares"],
                buy=check["second_buy_total"],
                sell=check["second_sell_net"],
                fee=check["second_total_fees_and_tax"],
                pnl=check["second_costed_net_pnl"],
                ok=check["costed_net_pnl_matches_hand_calc"],
                after=check["T2_is_strictly_after_first_exit"],
            )
        )
    lines.append("")
    lines.append("## 十、選股 → 監控 → V 型 → 買入 → 出場漏斗（0 bps）")
    lines.append("")
    lines.append(
        "| 策略 | 選股事件 | 啟動監控 episode | 有再進場的 episode | 拉回事件 | "
        "V 型已確認 | 兩條件同日成立 | 實際成交腿 | 已平倉 | 未平倉 |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for strategy in STRATEGY_ORDER:
        bucket = funnel["by_strategy"].get(strategy.value, {})
        lines.append(
            "| {s} | {sel} | {act} | {re} | {pb} | {vc} | {es} | {fill} | {cl} | {op} |".format(
                s=strategy.value,
                sel=h2._fmt(bucket.get("selection_events_total")),
                act=h2._fmt(bucket.get("episodes")),
                re=h2._fmt(bucket.get("episodes_with_reentry")),
                pb=h2._fmt(bucket.get("episodes_with_pullback")),
                vc=h2._fmt(bucket.get("episodes_with_pivot_confirmed")),
                es=h2._fmt(bucket.get("episodes_with_entry_signal")),
                fill=h2._fmt(bucket.get("filled_legs")),
                cl=h2._fmt(bucket.get("filled_closed_legs")),
                op=h2._fmt(bucket.get("filled_open_legs")),
            )
        )
    lines.append("")
    lines.append(
        "被拒絕的紀錄：持倉中加碼被拒 {a} 筆、同一出場日重買被拒 {s} 筆、"
        "regime 失效後未經新 D/E 的再進場被拒 {r} 筆、無法成交的訊號 {n} 筆。".format(
            a=rejections["rejected_additions_total"],
            s=rejections["rejected_same_day_reentries_total"],
            r=rejections["rejected_regime_invalidated_reentries_total"],
            n=rejections["non_executable_signals_total"],
        )
    )
    lines.append("")
    lines.append("## 十一、完整 0 bps 逐筆明細（共 %d 筆）" % len(detail_rows))
    lines.append("")
    lines.append(
        "唯一鍵＝證券|策略|episode|T|leg_id；「重進」為是代表這是同一 selection "
        "episode 內、前一筆出場後重新買進的新母單；「前次出場」欄列出被取代的 leg "
        "與其出場日／原因。公司行動欄為 UNAFFECTED_NO_2022_OFFICIAL_ACTION、"
        "CORRECTED_* 或 CORPORATE_ACTION_UNRESOLVED。"
    )
    lines.append("")
    lines.extend(_detail_table_lines(detail_rows))
    lines.append("")
    lines.append("## 十二、檔案、SHA 與可重跑")
    lines.append("")
    lines.append("| 產物 | SHA-256 |")
    lines.append("|---|---|")
    for name, digest in sorted(manifest["artifact_sha256"].items()):
        lines.append(f"| {name} | {digest} |")
    lines.append(
        "（report_zh.md 與 manifest.json 自身的 SHA-256 記錄在 readback.json。）"
    )
    if report_only:
        lines.append(
            "- 本檔是報告專用再輸出（report-only）：資料產物逐位元沿用來源 run "
            "{s}，上表列出的就是那些凍結 v1 產物的 SHA-256；本輪沒有重跑引擎，"
            "沒有改動任何交易、股數、金額或勝率原值，只把同口徑差值表的勝率欄"
            "改成「{c}」並以 (新 − 舊) × 100 顯示。".format(
                s=report_only["source_run_id"], c=WIN_RATE_DELTA_COLUMN
            )
        )
        lines.append(
            "- 來源 v1 報告本體 SHA-256：{h}；本檔（v2）repo 內新檔的 SHA-256 "
            "記錄在 readback.json 的 new_file_sha256。".format(
                h=report_only["source_report_zh_sha256"]
            )
        )
    lines.append("")
    lines.append("- 規格 SHA-256（raw）：" + spec_line(manifest))
    lines.append(
        "- 價格快照 content SHA-256：{h}；最後官方日 {d}（未讀 2023/2024）".format(
            h=manifest["inputs"]["price_snapshot_content_sha256"],
            d=preflight["price_snapshot_last_session"],
        )
    )
    lines.append(
        "- 官方日曆 raw SHA-256：{h}".format(
            h=manifest["inputs"]["official_calendar"]["raw_sha256"]
        )
    )
    lines.append(
        "- 舊單筆 run（唯讀、未改動）trades.csv SHA-256：{h}（重跑前後一致={same}）".format(
            h=manifest["inputs"]["baseline_run"]["trades_sha256"],
            same=manifest["inputs"]["baseline_run"]["trades_sha256_unchanged"],
        )
    )
    lines.append("- 分支：{b}；HEAD：{c}".format(b=git_state["branch"], c=git_state["head_commit"]))
    lines.append(
        "- git diff --stat HEAD：{d}".format(
            d=git_state["diff_stat_vs_head"]
            or "（空；本次新模組與測試為未追蹤檔案，清單見下方 git status）"
        )
    )
    if report_only:
        lines.append(
            "- 產生這些資料的凍結 v1 執行模組 SHA-256：{h}".format(
                h=manifest.get("source_sha256", {}).get("runner_module_sha256")
            )
        )
        lines.append(
            "- 本報告由 v2 報告用模組以 report-only 重新輸出，沒有重跑引擎；"
            "以下分支／HEAD／git 狀態是本次重新輸出時的工作區狀態。"
        )
    else:
        lines.append(
            "- 執行模組 SHA-256：{h}".format(
                h=manifest.get("source_sha256", {}).get("runner_module_sha256")
            )
        )
    dirty = git_state["status_porcelain"] or "（clean）"
    lines.append("- 工作區 git status：")
    lines.append("")
    lines.append("```")
    lines.append(dirty)
    lines.append("```")
    lines.append("")
    lines.append("- 重現命令：")
    lines.append("")
    lines.append("```")
    if report_only:
        lines.append(
            "python -m backtests.dual_ma.h2_flat_reentry_2022h2 "
            "--report-only-from {s} --run-id {r}".format(
                s=report_only["source_run_dir"], r=manifest["run_id"]
            )
        )
    else:
        lines.append(
            "python -m backtests.dual_ma.h2_flat_reentry_2022h2 "
            "--workdata-root {w} --run-id {r}".format(
                w=manifest["workdata_root"], r=manifest["run_id"]
            )
        )
    lines.append("```")
    lines.append("")
    lines.append("## 十三、方法與邊界")
    lines.append("")
    lines.append(
        "- 唯一規則變因：持倉中仍禁止加碼；同一 selection episode 內前一筆已出場、"
        "episode 仍有效且快線仍大於慢線時，原 V＋站回快線狀態機在出場日之後再次發出"
        "完整新訊號才可再買一筆。不得同日出場重買、不得沿用舊 T、不額外新增樞紐門檻；"
        "regime 失效需新 D/E。中期 5/21、長期 21/144 各自獨立。"
    )
    lines.append(
        "- 逐日因果重播：訊號與成交都用訊號日收盤（EOD_CLOSE_PROXY_V1），不使用事後"
        "高低點或損益下單；每筆重新定股數／風險並各自計費稅（999 股、10,000 元名目、"
        "初始 R 上限 2,000 元、CATHAY_WEB_028_TW_V1）。"
    )
    lines.append(
        "- 價格為未還原供應商收盤代理，非官方收盤；只讀 2022 及必要 2021 暖機價，"
        "未讀 2023/2024 績效，2022-12-30 為截點。"
    )
    lines.append(
        "- 不按績效挑股或改參數；不進行 50 萬元組合淨值、資金佔用或最大回撤估算，"
        "那需要另定資金衝突規則，本輪不虛構。"
    )
    lines.append(
        "- 出場後再買進時，凍結引擎原本就在某策略所有部位出場當日結束該策略的 Dow "
        "防線生命週期（EPISODE_END），本輪未改動該行為；因此新母單的 Dow 防線是"
        "之後重新累積的，並非沿用前一筆的防線實例。這是既有凍結行為，不是本輪新增"
        "或移除的規則。"
    )
    lines.append(
        "- 公司行動的局部還原沿用凍結修正模組作法，只在 0 bps 主基準上依官方事件"
        "條款重算價位與權益；10 / 20 bps 僅作成本敏感度，不做公司行動還原。"
    )
    lines.append(
        "- 這是每日 6/8 的部分探索樣本：chip_4 缺期、curated 失敗，不是完整八組績效。"
    )
    holds = rejections.get("lifecycle_named_holds", {}).get("reason_counts", {})
    lines.append(
        "- 年底截斷與 HOLD：D=2022-12-30 且下一個官方日在 2023 的選股事件記為 "
        "HOLD_NO_NEXT_OFFICIAL_SESSION_AFTER_D {n} 筆、"
        "HOLD_MISSING_E_BAR_OR_UNVERIFIED_NON_TRADING {e} 筆、"
        "HOLD_SMA_WARMUP_INSUFFICIENT {w} 筆。".format(
            n=holds.get("HOLD_NO_NEXT_OFFICIAL_SESSION_AFTER_D", 0),
            e=holds.get("HOLD_MISSING_E_BAR_OR_UNVERIFIED_NON_TRADING", 0),
            w=holds.get("HOLD_SMA_WARMUP_INSUFFICIENT", 0),
        )
    )
    lines.append("- 完成本報告後停在 00 QA，不自行展開其他新規則或大定錨。")
    lines.append("")
    lines.append(
        "run_id：{r}／module：{m}／run 狀態：{st}".format(
            r=manifest["run_id"], m=MODULE_VERSION, st=manifest["status"]
        )
    )
    lines.append("")
    return "\n".join(lines)


def spec_line(manifest: Mapping[str, Any]) -> str:
    source = manifest.get("source_sha256", {})
    return "{s}／bytes={b}（{p}）".format(
        s=source.get("reentry_variant_contract_sha256"),
        b=source.get("reentry_variant_contract_bytes"),
        p=source.get("reentry_variant_contract_path"),
    )


def git_state(worktree: str | Path) -> dict[str, Any]:
    worktree = Path(worktree)

    def _run(args: Sequence[str]) -> str:
        try:
            completed = subprocess.run(
                ["git", "-C", str(worktree), *args],
                capture_output=True,
                text=True,
                check=False,
            )
            return completed.stdout.strip()
        except Exception as exc:  # pragma: no cover - non-git checkouts
            return f"ERROR:{exc}"

    return {
        "branch": _run(["rev-parse", "--abbrev-ref", "HEAD"]),
        "head_commit": _run(["rev-parse", "HEAD"]),
        "status_porcelain": _run(["status", "--porcelain"]),
        "diff_stat_vs_head": _run(["diff", "--stat", "HEAD"]),
    }


# ---------------------------------------------------------------------------
# Run driver
# ---------------------------------------------------------------------------


def build_funnel(
    *, stats: Mapping[str, Any], plan: Any, rows: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    funnel_by_strategy = {
        key: dict(value) for key, value in stats["funnel_by_strategy_0bps"].items()
    }
    selection_records = list(plan.selection_records)
    for strategy in STRATEGY_ORDER:
        bucket = funnel_by_strategy.setdefault(strategy.value, {})
        bucket["selection_events_total"] = sum(
            1
            for record in selection_records
            if record.get("strategy") == strategy.value
        )
        bucket["episodes_with_reentry"] = len(
            {
                row["episode_key"]
                for row in rows
                if row["strategy"] == strategy.value
                and int(row["slippage_bps"]) == PRIMARY_SLIPPAGE_BPS
                and row["is_reentry"]
            }
        )
        bucket["filled_legs"] = sum(
            1
            for row in rows
            if row["strategy"] == strategy.value
            and int(row["slippage_bps"]) == PRIMARY_SLIPPAGE_BPS
        )
        bucket["filled_closed_legs"] = sum(
            1
            for row in rows
            if row["strategy"] == strategy.value
            and int(row["slippage_bps"]) == PRIMARY_SLIPPAGE_BPS
            and not row["open_censored"]
        )
        bucket["filled_open_legs"] = sum(
            1
            for row in rows
            if row["strategy"] == strategy.value
            and int(row["slippage_bps"]) == PRIMARY_SLIPPAGE_BPS
            and row["open_censored"]
        )
    return {
        "schema_version": FUNNEL_SCHEMA_VERSION,
        "primary_slippage_bps": PRIMARY_SLIPPAGE_BPS,
        "note": (
            "counts are per activated selection episode at 0 bps; a stage is "
            "reached when the causal engine emitted the corresponding event"
        ),
        "by_strategy": funnel_by_strategy,
    }


def run_h2_flat_reentry(
    *,
    workdata_root: str | Path,
    output_root: str | Path | None = None,
    run_id: str = RUN_ID_DEFAULT,
    episode_limit: int | None = None,
    runner_commit: str | None = None,
    progress: Callable[[str, int, int, Mapping[str, Any]], None] | None = None,
) -> dict[str, Any]:
    workdata_root = Path(workdata_root)
    out_root = (
        Path(output_root)
        if output_root is not None
        else workdata_root / "02-causal-backtest"
    )
    run_dir = out_root / run_id
    if run_dir.exists():
        raise H2FlatReentryError(
            f"refusing to overwrite an existing run directory: {run_dir}"
        )

    inputs = h2.load_h2_inputs(workdata_root, progress=progress)
    calendar = inputs["calendar"]
    snapshot = inputs["snapshot"]
    plan = pilot.build_lifecycle_plan(
        reports=inputs["reports"],
        resolution=inputs["resolution"],
        snapshot=snapshot,
        calendar=calendar,
    )
    preflight = h2.build_h2_preflight(inputs)
    preflight["run_id"] = run_id
    preflight["module_version"] = MODULE_VERSION

    repo_root = workdata_root.parents[2]
    spec_path = Path(
        repo_root
        / ".worktrees/dual-ma-integration-v0.4.1/docs/strategies/dual_ma/"
        "2022-h2-flat-reentry-v-pivot-exploration-v1.md"
    )
    if not spec_path.is_file():
        raise H2FlatReentryError(f"frozen re-entry spec is missing: {spec_path}")
    spec_bytes = spec_path.read_bytes()
    spec_sha = pilot.sha256_bytes(spec_bytes).upper()
    if spec_sha != SPEC_SHA256_REENTRY:
        raise H2FlatReentryError(
            f"re-entry spec sha256 {spec_sha} != {SPEC_SHA256_REENTRY}"
        )
    if len(spec_bytes) != SPEC_BYTE_SIZE_REENTRY:
        raise H2FlatReentryError(
            f"re-entry spec byte size {len(spec_bytes)} != {SPEC_BYTE_SIZE_REENTRY}"
        )

    baseline_dir = workdata_root / BASELINE_RUN_RELATIVE
    baseline_trades_path = baseline_dir / "trades.csv"
    baseline_open_path = baseline_dir / "year_end_open_positions.json"
    baseline_sha = pilot.sha256_file(baseline_trades_path).upper()
    if baseline_sha != BASELINE_TRADES_SHA256:
        raise H2FlatReentryError(
            f"baseline trades.csv sha256 {baseline_sha} != {BASELINE_TRADES_SHA256}"
        )
    baseline_open_sha = pilot.sha256_file(baseline_open_path).upper()
    if baseline_open_sha != BASELINE_OPEN_POSITIONS_SHA256:
        raise H2FlatReentryError(
            "baseline year_end_open_positions.json sha256 "
            f"{baseline_open_sha} != {BASELINE_OPEN_POSITIONS_SHA256}"
        )
    baseline_rows = _read_csv_rows(baseline_trades_path)
    baseline_leg_count_0bps = len(
        [row for row in baseline_rows if str(row.get("slippage_bps")) == "0"]
    )

    worktree_root = Path(__file__).resolve().parents[2]
    commit_state = pilot.resolve_runner_commit(worktree_root)
    commit = runner_commit or commit_state["runner_commit"]
    state = git_state(worktree_root)

    run_dir.mkdir(parents=True, exist_ok=True)
    rows, stats = collect_trade_rows(
        plan=plan,
        snapshot=snapshot,
        code_programs_by_date=inputs["code_programs_by_date"],
        run_id=run_id,
        runner_commit=commit,
        progress=progress,
        episode_limit=episode_limit,
    )
    if progress is not None:
        progress(
            "engine_done",
            stats["episode_count_run"],
            stats["episode_count_run"],
            stats,
        )
    summary = h2.summarize_rows(rows)
    comparison = build_comparison(
        new_rows=rows,
        baseline_rows=baseline_rows,
        baseline_sha256=baseline_sha,
    )
    reentry = build_reentry_analysis(rows)
    corp_summary = enrich_corporate_actions(
        rows=rows,
        snapshot=snapshot,
        workdata_root=workdata_root,
        run_id=run_id,
        runner_commit=commit,
        progress=progress,
    )
    groups = h2.build_group_attribution(
        rows, code_programs_by_date=inputs["code_programs_by_date"]
    )
    top_movers = h2.build_top_movers(rows)
    open_positions = h2.build_year_end_open_positions(rows=rows, snapshot=snapshot)
    corp_open = build_corpaction_open_positions(rows=rows, snapshot=snapshot)
    spot_reentry = build_reentry_spot_checks(rows=rows)
    spot_baseline = h2.build_spot_checks(rows=rows, calendar=calendar, stats=stats)
    spot_checks = {
        "schema_version": SPOT_CHECK_SCHEMA_VERSION,
        "baseline_d_e_v_t": spot_baseline,
        "reentry_chains": spot_reentry,
    }
    exit_reasons = h2.build_exit_reason_breakdown(rows)
    funnel = build_funnel(stats=stats, plan=plan, rows=rows)
    rejections = build_rejections(
        stats=stats, plan=plan, baseline_leg_count=baseline_leg_count_0bps
    )
    rejections["run_id"] = run_id

    primary_rows = [
        row for row in rows if int(row["slippage_bps"]) == PRIMARY_SLIPPAGE_BPS
    ]
    early_entries = [
        row
        for row in primary_rows
        if date.fromisoformat(row["entry_signal_date_T"])
        < date.fromisoformat(row["monitor_effective_date_E"])
    ]
    late_entries = [
        row
        for row in primary_rows
        if date.fromisoformat(row["entry_signal_date_T"])
        > date.fromisoformat(row["regime_window_end"])
    ]
    year_end_leaks = [
        row
        for row in primary_rows
        if date.fromisoformat(row["entry_signal_date_T"]) > AS_OF_CUTOFF
        or (
            row["exit_date"] is not None
            and date.fromisoformat(row["exit_date"]) > AS_OF_CUTOFF
        )
    ]
    bad_sizing = [
        row
        for row in primary_rows
        if int(row["shares"]) > 999
        or _money(row["buy_total"]) > Decimal("10000")
        or _money(row["initial_r"]) > Decimal("2000")
    ]
    fill_mismatch = [
        row
        for row in primary_rows
        if row["entry_fill_price"] != row["entry_reference_close"]
    ]
    open_with_stats = [
        row
        for row in rows
        if row["open_censored"]
        and (row["sell_net"] is not None or row["costed_net_pnl"] is not None)
    ]
    entry_without_confirm = [
        row
        for row in primary_rows
        if row["pivot_confirmed_date"] is None or row["pivot_low"] is None
    ]
    entry_below_fast = [
        row
        for row in primary_rows
        if row["entry_fast_sma"] is None
        or _money(row["entry_reference_close"]) <= _money(row["entry_fast_sma"])
    ]
    stop_not_pivot = [
        row
        for row in primary_rows
        if row["pivot_low"] is None or row["initial_stop"] != row["pivot_low"]
    ]
    backfilled_confirmation = [
        row
        for row in primary_rows
        if date.fromisoformat(row["pivot_confirmed_date"])
        > date.fromisoformat(row["entry_signal_date_T"])
    ]
    bad_reentry_order = [
        row
        for row in primary_rows
        if row["is_reentry"]
        and (
            row["prior_exit_date"] is None
            or date.fromisoformat(row["entry_signal_date_T"])
            <= date.fromisoformat(row["prior_exit_date"])
        )
    ]
    reentry_after_regime = [
        row
        for row in primary_rows
        if row["is_reentry"] and row["prior_exit_reason"] == ExitReason.REGIME_EXIT.value
    ]
    ordinal_gaps: list[dict[str, Any]] = []
    buckets: dict[tuple, list[int]] = {}
    for row in primary_rows:
        buckets.setdefault((row["episode_key"], row["strategy"]), []).append(
            int(row["reentry_ordinal"])
        )
    for key, values in buckets.items():
        if sorted(values) != list(range(1, len(values) + 1)):
            ordinal_gaps.append({"episode_key": key[0], "ordinals": sorted(values)})

    h2_sessions = calendar.sessions_between(H2_FIRST_DAY, H2_LAST_DAY)
    gates = {
        "parent_reports_match_declared_sha256": (
            "PASS" if inputs["parent"]["all_matched"] else "FAIL"
        ),
        "coverage_is_exactly_129_official_h2_sessions": (
            "PASS"
            if len(inputs["reports"]) == EXPECTED_H2_SESSION_COUNT
            and len(h2_sessions) == EXPECTED_H2_SESSION_COUNT
            else "FAIL"
        ),
        "price_snapshot_never_reaches_2023": (
            "PASS" if snapshot.last_session <= AS_OF_CUTOFF else "FAIL"
        ),
        "universe_sources_match_declared_sha256": (
            "PASS"
            if inputs["universe_sha"] == h2.UNIVERSE_SOURCES_RAW_SHA256
            else "FAIL"
        ),
        "reentry_spec_matches_declared_sha256": (
            "PASS" if spec_sha == SPEC_SHA256_REENTRY else "FAIL"
        ),
        "baseline_run_trades_byte_unchanged": (
            "PASS"
            if pilot.sha256_file(baseline_trades_path).upper()
            == BASELINE_TRADES_SHA256
            else "FAIL"
        ),
        "at_most_one_open_leg_per_strategy_at_any_time": (
            "PASS" if int(stats["max_open_legs_observed"]) <= 1 else "FAIL"
        ),
        "no_additions_were_filled": (
            "PASS" if int(stats["filled_add_events"]) == 0 else "FAIL"
        ),
        "every_entry_is_at_or_after_monitor_effective_E": (
            "PASS" if not early_entries else "FAIL"
        ),
        "no_entry_after_its_episode_window_end": (
            "PASS" if not late_entries else "FAIL"
        ),
        "no_trade_or_exit_after_2022_12_30": (
            "PASS" if not year_end_leaks else "FAIL"
        ),
        "every_entry_has_a_confirmed_pivot_and_pivot_low": (
            "PASS" if not entry_without_confirm else "FAIL"
        ),
        "every_entry_close_is_strictly_above_fast_sma": (
            "PASS" if not entry_below_fast else "FAIL"
        ),
        "initial_stop_equals_confirmed_pivot_low": (
            "PASS" if not stop_not_pivot else "FAIL"
        ),
        "no_confirmation_backfilled_to_an_earlier_date": (
            "PASS" if not backfilled_confirmation else "FAIL"
        ),
        "every_reentry_is_strictly_after_its_prior_exit": (
            "PASS" if not bad_reentry_order else "FAIL"
        ),
        "no_reentry_after_a_regime_invalidated_episode": (
            "PASS" if not reentry_after_regime else "FAIL"
        ),
        "reentry_ordinals_are_contiguous": (
            "PASS" if not ordinal_gaps else "FAIL"
        ),
        "sizing_within_frozen_leg_limits": "PASS" if not bad_sizing else "FAIL",
        "zero_bps_fill_equals_signal_day_close": (
            "PASS" if not fill_mismatch else "FAIL"
        ),
        "no_open_leg_carries_a_closed_leg_result": (
            "PASS" if not open_with_stats else "FAIL"
        ),
        "reentry_chain_spot_checks_present": (
            "PASS" if spot_reentry["check_count"] >= 2 else "FAIL"
        ),
        "spot_checks_hand_calculation_matches": (
            "PASS"
            if all(
                check.get("costed_net_pnl_matches_hand_calc", True)
                for check in spot_baseline["checks"]
            )
            and all(
                check.get("costed_net_pnl_matches_hand_calc", True)
                for check in spot_reentry["checks"]
            )
            else "FAIL"
        ),
    }
    if episode_limit is not None:
        gates["full_129_day_sample"] = "FAIL"
    readback_status = (
        "PASS" if all(value == "PASS" for value in gates.values()) else "HOLD"
    )

    detail_rows = sorted(
        primary_rows,
        key=lambda row: (
            row["strategy"],
            row["code"],
            int(row["episode_ordinal"]),
            row["entry_signal_date_T"],
            row["leg_id"],
        ),
    )
    trade_rows = [[row.get(name) for name in TRADE_ROW_FIELDS] for row in rows]
    summary_rows = [[row.get(name) for name in h2.SUMMARY_FIELDS] for row in summary]
    payloads: dict[str, bytes] = {
        "trades.csv": h2._csv_bytes(TRADE_ROW_FIELDS, trade_rows),
        "strategy_summary.csv": h2._csv_bytes(h2.SUMMARY_FIELDS, summary_rows),
        "strategy_summary.json": pilot._json_bytes(
            {
                "schema_version": STATS_SCHEMA_VERSION,
                "run_id": run_id,
                "variant_id": VARIANT_ID,
                "entry_variant": ENTRY_VARIANT.value,
                "primary_slippage_bps": PRIMARY_SLIPPAGE_BPS,
                "slippage_scenarios": list(SLIPPAGE_SCENARIOS),
                "research_only": True,
                "point_in_time_verified": False,
                "rows": summary,
            }
        ),
        "funnel.json": pilot._json_bytes(funnel),
        "group_attribution.json": pilot._json_bytes(groups),
        "top_movers.json": pilot._json_bytes(top_movers),
        "exit_reason_breakdown.json": pilot._json_bytes(exit_reasons),
        "year_end_open_positions.json": pilot._json_bytes(open_positions),
        "corpaction_summary.json": pilot._json_bytes(corp_summary),
        "corpaction_open_positions.json": pilot._json_bytes(corp_open),
        "reentry_comparison.json": pilot._json_bytes(comparison),
        "reentry_analysis.json": pilot._json_bytes(reentry),
        "rejections.json": pilot._json_bytes(rejections),
        "spot_checks.json": pilot._json_bytes(spot_checks),
        "parent_runs.json": pilot._json_bytes(inputs["parent"]),
        "preflight.json": pilot._json_bytes(preflight),
    }
    artifact_hashes = {
        name: pilot.sha256_bytes(blob) for name, blob in payloads.items()
    }
    source_sha256 = {
        "reentry_variant_contract_path": str(spec_path.relative_to(repo_root)).replace("\\", "/"),
        "reentry_variant_contract_sha256": spec_sha,
        "reentry_variant_contract_bytes": len(spec_bytes),
        "baseline_single_entry_contract": (
            "A68B1DC77D2C40156088365635F34A07280055C9E338F4F2E715AF8DB7BE8AA9"
        ),
        "frozen_spec": (
            "5A9E3A573178AA135FFBD960871D1ACEE20049F9ACF2DD7C117E1E2390B53DDA"
        ),
        "v050_preregistered_spec": (
            "AF6E2781EC81E8A46A42DC87E147F8CED02FC0FB2B91E35B25BA45CF49F85DAC"
        ),
        "lifecycle_contract": (
            "361D5F35456FCA9B8BF00412CACBA4E2EF4173175ECDBB813D7600A977EB755E"
        ),
        "corporate_action_correction_contract": (
            corp.SPEC_SHA256
        ),
        "price_snapshot_content": snapshot.content_sha256,
        "official_calendar_raw": calendar.raw_sha256,
        "baseline_run_trades_sha256": baseline_sha,
        "runner_module_sha256": pilot.sha256_file(Path(__file__)).upper(),
    }
    manifest: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "module_version": MODULE_VERSION,
        "run_id": run_id,
        "variant_id": VARIANT_ID,
        "entry_variant": ENTRY_VARIANT.value,
        "engine_matrix_vehicle": ENGINE_MATRIX_VEHICLE,
        "note": "No wall-clock timestamp is recorded so the run stays reproducible.",
        "status": "PASS" if readback_status == "PASS" else "HOLD",
        "readback_status": readback_status,
        "workdata_root": str(workdata_root),
        "research_only": True,
        "point_in_time_verified": False,
        "formal_historical_performance": False,
        "performance_read_or_computed": True,
        "frozen_matrix_unchanged": True,
        "research_variant_applied": True,
        "variant_rule_delta": [
            "entry signal is ENTRY_RECLAIM_V_PIVOT_CONFIRMED: an entry needs a "
            "causally confirmed PIVOT_V_CONFIRM_V1 bottom and a strict close "
            "above the fast SMA on the same session",
            "no additions: a signal raised while a leg of the episode is open "
            "is rejected",
            "ONE change: after the mother order has exited inside the still "
            "valid episode, a later complete signal may open one new mother "
            "order on a session strictly after the exit; same-day re-entry, "
            "old-T reuse and a fresh-pivot threshold are all forbidden",
        ],
        "selection_program_modified": False,
        "sample_definition": (
            "the six accepted 01 selection runs for every official 2022 H2 "
            "session (2022-07-01 .. 2022-12-30); a 6/8 TDCC partial exploration "
            "sample; 2023 / 2024 data is never read"
        ),
        "episode_limit": episode_limit,
        "episode_count_run": stats["episode_count_run"],
        "data_labels": {
            "price_basis": pilot.C1_DECLARED_PRICE_BASIS,
            "price_basis_is_official_close": False,
            "engine_instrument_parameter": pilot.ENGINE_INSTRUMENT_PARAMETER.value,
            "instrument_is_a_current_registration_proxy": True,
            "dated_2022_instrument_identity_evidence": False,
            "cost_model": COST_MODEL,
            "execution_model": EXECUTION_MODEL,
            "strategy_version": STRATEGY_VERSION,
        },
        "runner_commit": commit,
        "runner_commit_state": commit_state,
        "git_state": state,
        "inputs": {
            "official_calendar": calendar.as_binding(),
            "price_snapshot_content_sha256": snapshot.content_sha256,
            "price_snapshot_manifest_file_sha256": snapshot.manifest_file_sha256,
            "price_basis_binding": inputs["binding"].as_dict(),
            "universe_sources": {
                "path": str(inputs["universe_sources_path"]),
                "raw_sha256": inputs["universe_sha"],
                "source_id": h2.UNIVERSE_SOURCES_SOURCE_ID,
            },
            "parent_runs": inputs["parent"],
            "baseline_run": {
                "run_id": BASELINE_RUN_ID,
                "trades_path": str(baseline_trades_path),
                "trades_sha256": baseline_sha,
                "trades_sha256_expected": BASELINE_TRADES_SHA256,
                "year_end_open_positions_sha256": baseline_open_sha,
                "year_end_open_positions_sha256_expected": (
                    BASELINE_OPEN_POSITIONS_SHA256
                ),
                "trades_sha256_unchanged": None,
            },
        },
        "source_sha256": source_sha256,
        "coverage": {
            "schema_version": h2.PREFLIGHT_SCHEMA_VERSION,
            "reported_date_count": len(inputs["reports"]),
            "official_h2_session_count": len(h2_sessions),
            "first_date": min(report.report_date for report in inputs["reports"]),
            "last_date": max(report.report_date for report in inputs["reports"]),
        },
        "lifecycle": plan.as_dict(),
        "engine": {
            "episode_windows_run": stats["episode_count_run"],
            "slippage_scenarios": list(SLIPPAGE_SCENARIOS),
            "engine_runs": stats["episode_count_run"] * len(SLIPPAGE_SCENARIOS),
            "leg_rows": stats["leg_row_count"],
            "max_open_legs_observed": int(stats["max_open_legs_observed"]),
            "filled_add_events": int(stats["filled_add_events"]),
            "episodes_with_reentry_by_strategy_and_slippage": stats[
                "episodes_with_reentry_by_strategy_and_slippage"
            ],
        },
        "stats": {
            "primary_slippage_bps": PRIMARY_SLIPPAGE_BPS,
            "slippage_scenarios": list(SLIPPAGE_SCENARIOS),
            "rows": summary,
        },
        "funnel": funnel,
        "group_attribution": groups,
        "top_movers": top_movers,
        "exit_reason_breakdown": exit_reasons,
        "reentry_comparison": comparison,
        "reentry_analysis": reentry,
        "corporate_action": {
            "zero_bps_leg_count": corp_summary["zero_bps_leg_count"],
            "affected_leg_count": corp_summary["affected_leg_count"],
            "resolved_leg_count": corp_summary["resolved_leg_count"],
            "unresolved_leg_count": corp_summary["unresolved_leg_count"],
            "not_reproduced_leg_count": corp_summary["not_reproduced_leg_count"],
            "status_counts": corp_summary["status_counts"],
            "resolved_class_counts": corp_summary["resolved_class_counts"],
            "unresolved_reason_counts": corp_summary["unresolved_reason_counts"],
            "sequence_shifted_leg_count": corp_summary[
                "sequence_shifted_leg_count"
            ],
            "corrected_flows": corp_summary["corrected_flows"],
            "raw_baseline_of_unresolved_legs": corp_summary[
                "raw_baseline_of_unresolved_legs"
            ],
        },
        "year_end_open_positions": {
            "open_position_count": open_positions["open_position_count"],
            "open_reentry_position_count": corp_open["open_reentry_position_count"],
            "medium_open_count": open_positions["medium_open_count"],
            "long_open_count": open_positions["long_open_count"],
            "medium_mark_to_market_net_pnl": open_positions[
                "medium_mark_to_market_net_pnl"
            ],
            "long_mark_to_market_net_pnl": open_positions[
                "long_mark_to_market_net_pnl"
            ],
        },
        "preflight": preflight,
        "gates": gates,
        "artifact_sha256": artifact_hashes,
    }

    baseline_unchanged = (
        pilot.sha256_file(baseline_trades_path).upper() == BASELINE_TRADES_SHA256
    )
    manifest["inputs"]["baseline_run"]["trades_sha256_unchanged"] = (
        baseline_unchanged
    )
    report = render_report_zh(
        manifest=manifest,
        summary=summary,
        comparison=comparison,
        reentry=reentry,
        funnel=funnel,
        groups=groups,
        top_movers=top_movers,
        exit_reasons=exit_reasons,
        open_positions=open_positions,
        corp_summary=corp_summary,
        corp_open=corp_open,
        rejections=rejections,
        spot_checks=spot_checks,
        preflight=preflight,
        git_state=state,
        detail_rows=detail_rows,
    )
    payloads["report_zh.md"] = report.encode("utf-8")
    for name, blob in payloads.items():
        (run_dir / name).write_bytes(blob)

    if pilot.sha256_file(baseline_trades_path).upper() != BASELINE_TRADES_SHA256:
        raise H2FlatReentryError("the baseline run changed during this run")
    (run_dir / "manifest.json").write_bytes(pilot._json_bytes(manifest))

    all_names = list(artifact_hashes) + ["report_zh.md", "manifest.json"]
    declared = dict(artifact_hashes)
    declared["report_zh.md"] = pilot.sha256_bytes(payloads["report_zh.md"])
    declared["manifest.json"] = pilot.sha256_file(run_dir / "manifest.json")
    verified = {name: pilot.sha256_file(run_dir / name) for name in all_names}
    readback = {
        "schema_version": READBACK_SCHEMA_VERSION,
        "module_version": MODULE_VERSION,
        "run_id": run_id,
        "variant_id": VARIANT_ID,
        "entry_variant": ENTRY_VARIANT.value,
        "status": readback_status,
        "research_only": True,
        "point_in_time_verified": False,
        "formal_historical_performance": False,
        "gates": gates,
        "coverage": manifest["coverage"],
        "lifecycle": manifest["lifecycle"],
        "engine": manifest["engine"],
        "stats": manifest["stats"],
        "funnel": funnel,
        "reentry_comparison": comparison,
        "reentry_analysis": reentry,
        "corporate_action": manifest["corporate_action"],
        "year_end_open_positions": manifest["year_end_open_positions"],
        "rejections": rejections,
        "spot_checks": spot_checks,
        "preflight": preflight,
        "git_state": state,
        "runner_commit_state": commit_state,
        "source_sha256": source_sha256,
        "artifact_sha256": declared,
        "artifact_sha256_recomputed": verified,
        "artifacts_verified": declared == verified,
        "checkpoint": {
            "kind": "ATOMIC_H2_FLAT_REENTRY_V_PIVOT",
            "state": "COMPLETE_AWAITING_00_QA",
            "resumable": False,
            "next_action": (
                "00 reviews the research_only H2 flat-re-entry variant and its "
                "same-basis comparison; no 2023/2024 performance is read and "
                "nothing is deployed"
            ),
        },
    }
    (run_dir / "readback.json").write_bytes(pilot._json_bytes(readback))
    return manifest


# ---------------------------------------------------------------------------
# Report-only re-render (display fix only, never re-runs the engine)
# ---------------------------------------------------------------------------


DETAIL_ROW_NULLABLE_FIELDS: tuple[str, ...] = (
    "pivot_confirmed_date",
    "exit_date",
    "exit_reason",
    "exit_fill_price",
    "exit_reference_close",
    "prior_leg_id",
    "prior_exit_date",
    "prior_exit_reason",
    "sell_net",
    "costed_net_pnl",
)


def _as_bool(value: Any) -> bool:
    return str(value).strip().lower() == "true"


def _normalize_detail_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Turn one trades.csv text row back into the shape the table expects."""

    normalized: dict[str, Any] = dict(row)
    normalized["is_reentry"] = _as_bool(row.get("is_reentry"))
    normalized["open_censored"] = _as_bool(row.get("open_censored"))
    for name in DETAIL_ROW_NULLABLE_FIELDS:
        if normalized.get(name) == "":
            normalized[name] = None
    return normalized


def load_report_only_state(source_run_dir: str | Path) -> dict[str, Any]:
    """Read the frozen artifacts one finished run needs to re-render its report."""

    source_run_dir = Path(source_run_dir)
    manifest = pilot._read_json(source_run_dir / "manifest.json")
    detail_rows = [
        _normalize_detail_row(row)
        for row in _read_csv_rows(source_run_dir / "trades.csv")
        if str(row.get("slippage_bps")) == str(PRIMARY_SLIPPAGE_BPS)
    ]
    detail_rows.sort(
        key=lambda row: (
            row["strategy"],
            row["code"],
            int(row["episode_ordinal"] or 0),
            row["entry_signal_date_T"],
            row["leg_id"],
        )
    )
    return {
        "manifest": manifest,
        "summary": manifest["stats"]["rows"],
        "comparison": pilot._read_json(source_run_dir / "reentry_comparison.json"),
        "reentry": pilot._read_json(source_run_dir / "reentry_analysis.json"),
        "funnel": pilot._read_json(source_run_dir / "funnel.json"),
        "groups": pilot._read_json(source_run_dir / "group_attribution.json"),
        "top_movers": pilot._read_json(source_run_dir / "top_movers.json"),
        "exit_reasons": pilot._read_json(
            source_run_dir / "exit_reason_breakdown.json"
        ),
        "open_positions": pilot._read_json(
            source_run_dir / "year_end_open_positions.json"
        ),
        "corp_summary": pilot._read_json(source_run_dir / "corpaction_summary.json"),
        "corp_open": pilot._read_json(
            source_run_dir / "corpaction_open_positions.json"
        ),
        "rejections": pilot._read_json(source_run_dir / "rejections.json"),
        "spot_checks": pilot._read_json(source_run_dir / "spot_checks.json"),
        "preflight": pilot._read_json(source_run_dir / "preflight.json"),
        "detail_rows": detail_rows,
    }


def _detail_table_row_count(report: str) -> int:
    """How many per-leg rows the rendered section eleven actually carries."""

    lines = report.split("\n")
    start = next(
        index
        for index, line in enumerate(lines)
        if line.startswith("## 十一、完整 0 bps 逐筆明細")
    )
    stop = next(
        index for index, line in enumerate(lines) if line.startswith("## 十二")
    )
    return sum(
        1
        for line in lines[start:stop]
        if line.startswith("|")
        and not line.startswith("|---")
        and "唯一鍵" not in line
    )


def report_sections(text: str) -> dict[str, list[str]]:
    """Slice a report into the regions a display-only re-render may touch.

    Only the head line, the same-basis difference table and the metadata block
    may differ.  Everything else -- the narrative sections and the complete
    0 bps per-leg detail table -- has to come back byte-for-byte.
    """

    lines = text.split("\n")

    def find(prefix: str) -> int:
        return next(
            index for index, line in enumerate(lines) if line.startswith(prefix)
        )

    delta_start = find("### 新可再進場版減舊單筆版")
    delta_stop = find("## 三、再進場分析")
    metadata_start = find("## 十二")
    metadata_stop = find("## 十三")
    footer_start = find("run_id：")
    return {
        "head": lines[:1],
        "body": lines[1:delta_start],
        "delta": lines[delta_start:delta_stop],
        "middle": lines[delta_stop:metadata_start],
        "metadata": lines[metadata_start:metadata_stop],
        "tail": lines[metadata_stop:footer_start],
        "footer": lines[footer_start:],
    }


def changed_report_regions(source_report: str, new_report: str) -> dict[str, bool]:
    """Which regions of the report the re-render actually changed."""

    source = report_sections(source_report)
    new = report_sections(new_report)
    return {name: source[name] != new[name] for name in source}


def unexpected_report_diff_regions(source_report: str, new_report: str) -> list[str]:
    """Fixed regions that a display-only re-render is not allowed to change."""

    changed = changed_report_regions(source_report, new_report)
    return [
        name for name in ("body", "middle", "tail") if changed.get(name)
    ]


def run_report_only(
    *,
    source_run_dir: str | Path,
    output_root: str | Path | None = None,
    run_id: str = REPORT_ONLY_RUN_ID_DEFAULT,
    runner_commit: str | None = None,
    write: bool = True,
    replace: bool = False,
) -> dict[str, Any]:
    """Re-render one finished run's report; the engine is never replayed.

    The frozen data artifacts are only ever read: no trade, share count, money
    figure or original win rate is recomputed or rewritten.  The one display
    change is the same-basis difference table, whose win-rate column is now
    named 勝率差（百分點） and rendered as (new - old) x 100.

    The replace flag may only rewrite a directory that already holds nothing but
    this report-only output triple, so a frozen run can never be overwritten.
    """

    source_run_dir = Path(source_run_dir)
    manifest_path = source_run_dir / "manifest.json"
    if not manifest_path.is_file():
        raise H2FlatReentryError(f"source run has no manifest.json: {source_run_dir}")
    out_root = (
        Path(output_root) if output_root is not None else source_run_dir.parent
    )
    run_dir = out_root / run_id
    if write and run_dir.exists():
        existing = sorted(item.name for item in run_dir.iterdir())
        if not replace or existing != list(REPORT_ONLY_OUTPUT_FILES):
            raise H2FlatReentryError(
                f"refusing to overwrite an existing run directory: {run_dir}"
            )

    state = load_report_only_state(source_run_dir)
    source_manifest = state["manifest"]
    source_report_path = source_run_dir / "report_zh.md"
    source_report_text = source_report_path.read_text(encoding="utf-8")

    source_shas: dict[str, dict[str, Any]] = {}
    sha_match = True
    for name, expected in sorted(
        (source_manifest.get("artifact_sha256") or {}).items()
    ):
        actual = pilot.sha256_file(source_run_dir / name).lower()
        match = actual == str(expected).lower()
        sha_match = sha_match and match
        source_shas[name] = {
            "expected": str(expected).lower(),
            "actual": actual,
            "match": match,
        }
    source_report_sha = pilot.sha256_file(source_report_path).lower()
    source_manifest_sha = pilot.sha256_file(manifest_path).lower()
    source_readback_sha = pilot.sha256_file(
        source_run_dir / "readback.json"
    ).lower()

    worktree_root = Path(__file__).resolve().parents[2]
    commit_state = pilot.resolve_runner_commit(worktree_root)
    commit = runner_commit or commit_state["runner_commit"]
    state_git = git_state(worktree_root)

    render_manifest = {
        **source_manifest,
        "run_id": run_id,
        "report_only_factfix": {
            "source_run_id": source_manifest.get("run_id"),
            "source_run_dir": str(source_run_dir),
            "source_report_zh_sha256": source_report_sha,
            "edition": REPORT_ONLY_EDITION,
            "engine_rerun": False,
        },
    }
    report = render_report_zh(
        manifest=render_manifest,
        summary=state["summary"],
        comparison=state["comparison"],
        reentry=state["reentry"],
        funnel=state["funnel"],
        groups=state["groups"],
        top_movers=state["top_movers"],
        exit_reasons=state["exit_reasons"],
        open_positions=state["open_positions"],
        corp_summary=state["corp_summary"],
        corp_open=state["corp_open"],
        rejections=state["rejections"],
        spot_checks=state["spot_checks"],
        preflight=state["preflight"],
        git_state=state_git,
        detail_rows=state["detail_rows"],
    )
    report_bytes = report.encode("utf-8")

    detail_rows = state["detail_rows"]
    leg_keys = [row["leg_key"] for row in detail_rows]
    expected_detail = int(source_manifest["engine"]["leg_rows"]) // len(
        SLIPPAGE_SCENARIOS
    )
    delta_lines = comparison_delta_table_lines(state["comparison"])
    detail_rendered = _detail_table_row_count(report)
    changed_regions = changed_report_regions(source_report_text, report)
    unexpected = [name for name, changed in changed_regions.items()
                  if changed and name not in ("head", "delta", "metadata", "footer")]
    source_untouched = all(
        pilot.sha256_file(source_run_dir / name).lower() == item["actual"]
        for name, item in source_shas.items()
    ) and pilot.sha256_file(source_report_path).lower() == source_report_sha

    gates = {
        "source_artifact_sha256_match": "PASS" if sha_match else "FAIL",
        "no_engine_rerun": "PASS",
        "source_run_untouched": "PASS" if source_untouched else "FAIL",
        "detail_rows_complete": (
            "PASS" if len(detail_rows) == expected_detail else "FAIL"
        ),
        "detail_leg_keys_unique": (
            "PASS" if len(set(leg_keys)) == len(leg_keys) else "FAIL"
        ),
        "report_renders_every_detail_row": (
            "PASS" if detail_rendered == len(detail_rows) else "FAIL"
        ),
        "win_rate_delta_column_is_percentage_points": (
            "PASS"
            if WIN_RATE_DELTA_COLUMN in delta_lines[0]
            and all(line in report for line in delta_lines)
            else "FAIL"
        ),
        "report_diff_limited_to_display_fix": (
            "PASS" if not unexpected else "FAIL"
        ),
    }
    status = "PASS" if all(value == "PASS" for value in gates.values()) else "HOLD"

    new_file_sha256 = {"report_zh.md": pilot.sha256_bytes(report_bytes)}
    old_file_sha256 = {
        "report_zh.md": source_report_sha,
        "manifest.json": source_manifest_sha,
        "readback.json": source_readback_sha,
    }
    old_file_sha256.update(
        {name: item["actual"] for name, item in source_shas.items()}
    )

    manifest = {
        "schema_version": REPORT_ONLY_MANIFEST_SCHEMA_VERSION,
        "module_version": MODULE_VERSION,
        "run_id": run_id,
        "run_kind": "report-only",
        "edition": REPORT_ONLY_EDITION,
        "status": status,
        "readback_status": status,
        "runner_commit": commit,
        "research_only": True,
        "formal_historical_performance": False,
        "point_in_time_verified": False,
        "engine_rerun": False,
        "source_run_id": source_manifest.get("run_id"),
        "source_run_dir": str(source_run_dir),
        "source_manifest_sha256": source_manifest_sha,
        "source_readback_sha256": source_readback_sha,
        "source_artifact_sha256": source_shas,
        "old_file_sha256": old_file_sha256,
        "new_file_sha256": new_file_sha256,
        "report_change": {
            "section": "### 新可再進場版減舊單筆版（同口徑差值）",
            "column": WIN_RATE_DELTA_COLUMN,
            "formula": "(new_win_rate - old_win_rate) * 100",
            "unit": "percentage points",
            "rows": delta_lines,
            "changed_regions": changed_regions,
            "unexpected_diff_regions": unexpected,
            "source_json_untouched": (
                "reentry_comparison.json keeps its frozen delta.win_rate ratio "
                "gap; the percentage-point column is derived for display from "
                "the two win rates the table already prints, and no data "
                "artifact is rewritten"
            ),
        },
        "detail_rows": {
            "count": len(detail_rows),
            "unique_leg_keys": len(set(leg_keys)),
            "expected_from_source_engine": expected_detail,
            "rendered_in_report": detail_rendered,
            "source": "trades.csv @ primary bps",
        },
        "gates": gates,
        "note": (
            "Report-only re-render: the frozen artifacts of "
            + str(source_manifest.get("run_id"))
            + " are read byte-for-byte and only the report text is rebuilt, "
            "with the same-basis win-rate column renamed to 勝率差（百分點） "
            "and printed as (new - old) x 100."
        ),
    }
    readback = {
        "schema_version": REPORT_ONLY_READBACK_SCHEMA_VERSION,
        "module_version": MODULE_VERSION,
        "run_id": run_id,
        "run_kind": "report-only",
        "status": status,
        "research_only": True,
        "formal_historical_performance": False,
        "point_in_time_verified": False,
        "engine_rerun": False,
        "engine_artifacts_rewritten": [],
        "source_run_id": source_manifest.get("run_id"),
        "source_run_dir": str(source_run_dir),
        "source_artifact_sha256": source_shas,
        "source_artifacts_unchanged_after_render": source_untouched,
        "new_file_sha256": new_file_sha256,
        "old_file_sha256": old_file_sha256,
        "detail_rows": manifest["detail_rows"],
        "win_rate_delta_rows": delta_lines,
        "changed_regions": changed_regions,
        "unexpected_diff_regions": unexpected,
        "gates": gates,
        "runner_commit_state": commit_state,
        "git_state": state_git,
        "checkpoint": {
            "kind": "ATOMIC_H2_FLAT_REENTRY_REPORT_ONLY_FACTFIX",
            "state": "COMPLETE_AWAITING_00_QA",
            "resumable": False,
            "next_action": (
                "00 reviews the display-only 勝率差（百分點） correction; the "
                "frozen v1 run and the old single-entry run stay untouched"
            ),
        },
    }
    if write:
        run_dir.mkdir(parents=True, exist_ok=replace)
        (run_dir / "report_zh.md").write_bytes(report_bytes)
        (run_dir / "manifest.json").write_bytes(pilot._json_bytes(manifest))
        (run_dir / "readback.json").write_bytes(pilot._json_bytes(readback))
    return {
        "manifest": manifest,
        "readback": readback,
        "report": report,
        "run_dir": run_dir,
        "source_run_dir": source_run_dir,
    }


def _progress_printer(
    stage: str, done: int, total: int, extra: Mapping[str, Any]
) -> None:
    print(f"[{stage}] {done}/{total} {dict(extra)}", file=sys.stderr, flush=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdata-root", type=Path, default=None)
    parser.add_argument("--output-root", type=Path, default=None)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--episode-limit", type=int, default=None)
    parser.add_argument("--runner-commit", default=None)
    parser.add_argument(
        "--report-only-from",
        type=Path,
        default=None,
        help=(
            "re-render the report of an existing run directory without replaying "
            "the engine; the frozen data artifacts stay byte-for-byte unchanged"
        ),
    )
    parser.add_argument(
        "--replace-report-only",
        action="store_true",
        help=(
            "allow --report-only-from to refresh a directory that already holds "
            "only a report-only output triple (report_zh.md, manifest.json, "
            "readback.json); any other directory is still refused"
        ),
    )
    args = parser.parse_args(argv)
    if args.report_only_from is not None:
        result = run_report_only(
            source_run_dir=args.report_only_from,
            output_root=args.output_root,
            run_id=args.run_id or REPORT_ONLY_RUN_ID_DEFAULT,
            runner_commit=args.runner_commit,
            replace=args.replace_report_only,
        )
        manifest = result["manifest"]
        print(
            "run {r} status={s} detail_rows={d} source={o}".format(
                r=manifest["run_id"],
                s=manifest["status"],
                d=manifest["detail_rows"]["count"],
                o=manifest["source_run_id"],
            )
        )
        return 0 if manifest["status"] == "PASS" else 1
    if args.workdata_root is None:
        parser.error("--workdata-root is required unless --report-only-from is given")
    manifest = run_h2_flat_reentry(
        workdata_root=args.workdata_root,
        output_root=args.output_root,
        run_id=args.run_id or RUN_ID_DEFAULT,
        episode_limit=args.episode_limit,
        runner_commit=args.runner_commit,
        progress=_progress_printer,
    )
    print(
        "run {r} status={s} episodes={e} leg_rows={l} reentries={n}".format(
            r=manifest["run_id"],
            s=manifest["status"],
            e=manifest["episode_count_run"],
            l=manifest["engine"]["leg_rows"],
            n=manifest["reentry_analysis"]["reentry_leg_count"],
        )
    )
    return 0 if manifest["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
