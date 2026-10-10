"""2022 H2 single-entry plus causal V-pivot entry exploration run (02 role).

Authority (read-only, 00-frozen):

* docs/strategies/dual_ma/2022-h2-single-entry-v-pivot-exploration-v1.md
  raw SHA-256 A68B1DC77D2C40156088365635F34A07280055C9E338F4F2E715AF8DB7BE8AA9
* docs/strategies/dual_ma/versions/v0.5.0-preregistered/spec.md
  raw SHA-256 AF6E2781EC81E8A46A42DC87E147F8CED02FC0FB2B91E35B25BA45CF49F85DAC
* docs/strategies/dual_ma/patterns/pivot-v-confirm-v1.md
* docs/strategies/dual_ma/2022-h1-single-entry-no-add-exploration-v1.md
  raw SHA-256 937D6585FD3727AC227542FEDCBB5266D2DBE98BE7CBBAB375469CFFA9BF4405
* docs/strategies/dual_ma/2022-h1-selection-episode-lifecycle-v1.md
  raw SHA-256 361D5F35456FCA9B8BF00412CACBA4E2EF4173175ECDBB813D7600A977EB755E

Question: with the 129 accepted official 2022 H2 selection days, the same
2021-2022 Fugle price snapshot and the same CATHAY_WEB_028_TW_V1 cost model,
what do the trades look like when each strategy keeps at most one mother order
per activated selection episode (no additions) AND a mother order may only be
filled on the first T session whose close is strictly above the fast SMA while
a causal PIVOT_V_CONFIRM_V1 bottom is confirmed and has not been breached?

The variant is re-run from the frozen selection life cycle and the frozen price
bars (see single_entry_no_add.SingleEntryNoAddEngine with
EntryVariant.RECLAIM_PIVOT_V_CONFIRMED).  It is never produced by filtering an
existing run.  Only the new run directory under <workdata_root>/02-causal-backtest
is written; no frozen input, matrix, 01 selection product or 00-owned doc changes.
"""

from __future__ import annotations

import argparse
import csv
import io
import statistics
import sys
from collections import Counter
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import h1_real_pilot_2022_0419_0428 as pilot
from .contracts import (
    COST_MODEL,
    EXECUTION_MODEL,
    SPEC_SHA256,
    STRATEGY_VERSION,
    RunManifest,
)
from .costs import CATHAY_WEB_028_TW_V1
from .h1_full_exploration_2022h1 import (
    UNIVERSE_SOURCES_RELATIVE_PATH,
    UNIVERSE_SOURCES_RAW_SHA256,
    UNIVERSE_SOURCES_SOURCE_ID,
)
from .models import EntryVariant, InstrumentType, StrategyKind
from .single_entry_no_add import (
    ENGINE_MATRIX_VEHICLE,
    STATUS_ADD_REJECTED_SINGLE_ENTRY,
    STATUS_FILLED,
    STATUS_NON_EXECUTABLE,
    STATUS_REENTRY_REJECTED_SAME_EPISODE,
    VARIANT_ID,
    SingleEntryNoAddEngine,
    pullback_event_by_leg,
    tally_entry_decisions,
)


MODULE_VERSION = "dual-ma-h2-single-entry-v-pivot-2022h2-v1"
RUN_ID_DEFAULT = "h2-vpivot-single-entry-2022h2-v1"
MANIFEST_SCHEMA_VERSION = "dual-ma-h2-single-entry-v-pivot-manifest-v1"
READBACK_SCHEMA_VERSION = "dual-ma-h2-single-entry-v-pivot-readback-v1"
PREFLIGHT_SCHEMA_VERSION = "dual-ma-h2-single-entry-v-pivot-preflight-v1"
FUNNEL_SCHEMA_VERSION = "dual-ma-h2-single-entry-v-pivot-funnel-v1"
GROUP_SCHEMA_VERSION = "dual-ma-h2-single-entry-v-pivot-groups-v1"
STATS_SCHEMA_VERSION = "dual-ma-h2-single-entry-v-pivot-stats-v1"
REJECTIONS_SCHEMA_VERSION = "dual-ma-h2-single-entry-v-pivot-rejections-v1"
SPOT_CHECK_SCHEMA_VERSION = "dual-ma-h2-single-entry-v-pivot-spot-checks-v1"
OPEN_POSITIONS_SCHEMA_VERSION = "dual-ma-h2-single-entry-v-pivot-open-positions-v1"

H2_FIRST_DAY = date(2022, 7, 1)
H2_LAST_DAY = date(2022, 12, 30)
AS_OF_CUTOFF = date(2022, 12, 30)
EXPECTED_H2_SESSION_COUNT = 129
LATEST_READABLE_YEAR = 2022
SLIPPAGE_SCENARIOS: tuple[int, ...] = (0, 10, 20)
PRIMARY_SLIPPAGE_BPS = 0
STRATEGY_ORDER: tuple[StrategyKind, ...] = (StrategyKind.MEDIUM, StrategyKind.LONG)
DESCRIPTIVE_BOTH = "BOTH_DESCRIPTIVE_ONLY"
STRATEGY_DISPLAY_LABELS: Mapping[str, str] = {
    DESCRIPTIVE_BOTH: "兩策略交易合計（僅交易描述，非投資組合或帳戶報酬）",
}

ENTRY_VARIANT = EntryVariant.RECLAIM_PIVOT_V_CONFIRMED

# The six accepted 01 selection runs that together cover every official 2022 H2
# session exactly once (2022-07-01 .. 2022-12-30).
H2_PARENT_RUNS: tuple[tuple[str, str], ...] = (
    (
        "run-2026-10-03-d2022-07-v1-isolated",
        "2d29edeeac62abff9dd6336a7e11c650dd97bbdd687568859ccbdae1702ab9da",
    ),
    (
        "run-2026-10-03-d2022-08-v1-isolated",
        "b305a1937086b94767a26a962455a40b699b6cc6066f4e27085069cb0b15a028",
    ),
    (
        "run-2026-10-03-d2022-09-v1-isolated",
        "f60224716431f22bb431d7beeea25280d789e602d868918dcd14f534bcd3a390",
    ),
    (
        "run-2026-10-03-d2022-10-v1-isolated",
        "832a37887aa78c5d7b607d7fbb732ccdc960badaa8f096acf321bf46dad9886d",
    ),
    (
        "run-2026-10-03-d2022-11-v1-isolated",
        "3bdc9137bf8cf90e29f15f759862a8882e9ae0cbdfa23a9d4648605945e68ad9",
    ),
    (
        "run-2026-10-03-d2022-12-v1-isolated",
        "dba0ee21ea21d6f79b62ac06ef71c7c1eaa22c51fe0cebf3f4a06a7043219880",
    ),
)
H2_PARENT_RUN_IDS: tuple[str, ...] = tuple(run for run, _ in H2_PARENT_RUNS)
H2_PARENT_SHA256: Mapping[str, str] = {run: sha for run, sha in H2_PARENT_RUNS}

GROUP_ORDER: tuple[str, ...] = (
    "financial",
    "chip_1",
    "chip_2",
    "chip_3",
    "chip_4",
    "technical",
    "curated",
    "laoxiao",
)
AVAILABLE_GROUPS: tuple[str, ...] = (
    "financial",
    "chip_1",
    "chip_2",
    "chip_3",
    "technical",
    "laoxiao",
)
GROUP_LABELS: Mapping[str, str] = {
    "financial": "財務",
    "chip_1": "籌碼1",
    "chip_2": "籌碼2",
    "chip_3": "籌碼3",
    "chip_4": "籌碼4",
    "technical": "技術",
    "curated": "精選",
    "laoxiao": "老蕭",
}
EXPECTED_PROGRAM_STATUS_COUNTS: Mapping[str, int] = {
    "SUCCESS": 6,
    "UNAVAILABLE": 1,
    "FAILED": 1,
}

SEMANTIC_LABELS: Mapping[str, Any] = {
    "entry_variant": "ENTRY_RECLAIM_V_PIVOT_CONFIRMED",
    "pattern_id": "PIVOT_V_CONFIRM_V1",
    "exit_variant": "EXIT_DOW_TRAIL",
    "down_exit_basis": "FROZEN_N2_SINGLE_LEVEL_ENGINEERING_BASELINE",
    "down_defense_n": 2,
    "down_defense_description": (
        "fixed daily n=2 single-level defense: two completed right bars, equal "
        "extremes treated as one zone, usable only after the zone ends"
    ),
    "no_exit_008_degree_L1_L2": True,
    "course_style_degree_dow_semantics": "HOLD_NOT_VALIDATED",
    "pivot_pattern_does_not_feed_dow_or_exit": True,
    "disclosure_only_no_rule_change": True,
}

TRADE_ROW_FIELDS: tuple[str, ...] = (
    "variant_id",
    "run_id",
    "code",
    "symbol",
    "strategy",
    "slippage_bps",
    "engine_matrix_vehicle",
    "entry_variant",
    "leg_id",
    "leg_number",
    "episode_ordinal",
    "episode_key",
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
    "exit_date",
    "exit_reason",
    "exit_reference_close",
    "exit_fill_price",
    "buy_total",
    "sell_net",
    "gross_pnl",
    "costed_net_pnl",
    "net_return_pct",
    "open_censored",
    "selection_groups",
    "pullback_event_id",
)

SUMMARY_FIELDS: tuple[str, ...] = (
    "strategy",
    "slippage_bps",
    "closed_leg_count",
    "open_leg_count",
    "win_count",
    "loss_count",
    "flat_count",
    "win_rate",
    "mean_costed_net_pnl",
    "median_costed_net_pnl",
    "mean_return_pct",
    "median_return_pct",
    "gross_profit",
    "gross_loss",
    "costed_net_pnl_total",
    "note",
)


class H2VPivotError(RuntimeError):
    """A structural problem with the H2 run or one of its frozen inputs."""


def _dstr(value: Decimal | None, places: int = 4) -> str | None:
    if value is None:
        return None
    return str(value.quantize(Decimal(1).scaleb(-places)))


def _csv_bytes(header: Sequence[str], rows: Iterable[Sequence[Any]]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(list(header))
    for row in rows:
        writer.writerow(["" if value is None else value for value in row])
    return buffer.getvalue().encode("utf-8")


def _fmt(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _pct(value: Any, places: int = 2, *, already_percent: bool = False) -> str:
    """Render a ratio as a fixed two-decimal percentage (0.249789 -> 24.98%)."""

    if value is None or value == "":
        return ""
    number = Decimal(str(value))
    if not already_percent:
        number = number * Decimal(100)
    return str(number.quantize(Decimal(1).scaleb(-places))) + "%"


def _episode_key(episode: Any) -> str:
    window = episode.window
    return "|".join(
        [
            str(episode.symbol),
            str(episode.strategy),
            "EP" + str(window.ordinal),
            window.source_selection_date.isoformat(),
        ]
    )


def verify_h2_parent_runs(
    pilot_root: str | Path,
    *,
    expectations: pilot.FrozenExpectations = pilot.FROZEN_EXPECTATIONS,
) -> dict[str, Any]:
    """Read the six accepted 01 monthly reports and check their SHA-256."""

    pilot_root = Path(pilot_root)
    rows: list[dict[str, Any]] = []
    all_matched = True
    for run, expected in H2_PARENT_RUNS:
        path = pilot_root / run / pilot.PILOT_REPORT_NAME
        if not path.is_file():
            raise H2VPivotError(f"parent pilot report is missing: {path}")
        digest = pilot.sha256_file(path)
        matched = digest == expected
        all_matched = all_matched and matched
        blocks = pilot._date_reports(pilot_root / run)
        rows.append(
            {
                "run_dir": run,
                "report_path": str(path),
                "report_sha256": digest,
                "expected_sha256": expected,
                "matched": matched,
                "date_block_count": len(blocks),
                "report_dates": [str(item.get("report_date")) for item in blocks],
            }
        )
    return {
        "schema_version": "dual-ma-h2-single-entry-v-pivot-parent-runs-v1",
        "run_count": len(rows),
        "all_matched": all_matched,
        "runs": rows,
    }


def build_h2_run_by_date(pilot_root: str | Path) -> dict[str, str]:
    """Map every reported H2 selection date to the accepted run that produced it."""

    pilot_root = Path(pilot_root)
    mapping: dict[str, str] = {}
    for run, _ in H2_PARENT_RUNS:
        for item in pilot._date_reports(pilot_root / run):
            report_date = str(item.get("report_date"))
            if not report_date or report_date == "None":
                raise H2VPivotError(f"{run}: date block without report_date")
            previous = mapping.get(report_date)
            if previous is not None and previous != run:
                raise H2VPivotError(
                    f"{report_date} is claimed by both {previous} and {run}"
                )
            mapping[report_date] = run
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
            "add_variant": "NONE_SINGLE_ENTRY_VARIANT",
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
                "2022-h2-single-entry-v-pivot-exploration-v1.md"
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
    """Re-run the V-pivot single-entry engine once for one activated episode."""

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
    result = SingleEntryNoAddEngine(
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


def _episode_evidence(result: Any, strategy: str) -> dict[str, Any]:
    """Causal V-pivot funnel facts for one activated selection episode."""

    events: list[Mapping[str, Any]] = []
    for decision in result.decisions:
        if str(decision.get("strategy")) != strategy:
            continue
        for event in decision.get("events") or ():
            if isinstance(event, Mapping):
                events.append(event)
    type_counts = Counter(str(event.get("type")) for event in events)
    confirmations = [
        event for event in events if event.get("type") == "PIVOT_V_CONFIRMED"
    ]
    entry_signals = [
        event for event in events if event.get("type") == "PIVOT_V_ENTRY_SIGNAL"
    ]
    reclaims = [event for event in events if event.get("type") == "RECLAIM_SIGNAL"]
    deferred_reclaims = [
        event for event in reclaims if event.get("entry_deferred_for_pivot")
    ]
    return {
        "event_type_counts": dict(sorted(type_counts.items())),
        "saw_pullback": type_counts.get("PULLBACK_STARTED", 0) > 0,
        "pivot_confirmed_count": len(confirmations),
        "pivot_expired_count": type_counts.get("PIVOT_V_EXPIRED", 0),
        "pivot_invalidated_count": type_counts.get("PIVOT_V_INVALIDATED", 0),
        "saw_pivot_confirmed": bool(confirmations),
        "saw_reclaim_before_pivot": bool(deferred_reclaims),
        "entry_signal_count": len(entry_signals),
        "saw_entry_signal": bool(entry_signals),
    }


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
    """Replay every activated episode at 0 / 10 / 20 bps and collect its legs."""

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
                evidence = _episode_evidence(result, episode.strategy)
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
            for leg in legs:
                legs_per_episode[
                    (
                        episode.code,
                        episode.strategy,
                        episode.window.ordinal,
                        slippage_bps,
                    )
                ] += 1
                rows.append(
                    _leg_row(
                        leg=leg,
                        episode=episode,
                        slippage_bps=slippage_bps,
                        run_id=run_id,
                        pullback_event_id=pullbacks.get(leg.leg_id),
                        entry_evidence=_leg_entry_evidence(result, leg),
                        groups=groups_today,
                    )
                )
        if progress is not None and index % 50 == 0:
            progress("episodes", index, len(episodes), {"leg_rows": len(rows)})

    duplicate_episode_keys = [
        key for key, count in legs_per_episode.items() if count > 1
    ]
    stats = {
        "episode_count_run": len(episodes),
        "episode_count_truncated": truncated,
        "leg_row_count": len(rows),
        "episodes_with_a_leg_by_strategy_and_slippage": dict(
            sorted(episodes_with_leg.items())
        ),
        "single_leg_per_episode": not duplicate_episode_keys,
        "duplicate_episode_leg_keys": [
            {
                "code": key[0],
                "strategy": key[1],
                "episode_ordinal": key[2],
                "slippage_bps": key[3],
                "leg_count": legs_per_episode[key],
            }
            for key in duplicate_episode_keys[:50]
        ],
        "entry_decision_totals": dict(sorted(decision_totals.items())),
        "entry_decision_first_example": decision_first_example,
        "funnel_by_strategy_0bps": {
            key: dict(sorted(value.items()))
            for key, value in sorted(funnel_by_strategy.items())
        },
    }
    return rows, stats


def _leg_entry_evidence(result: Any, leg: Any) -> Mapping[str, Any]:
    for transaction in result.transactions:
        if (
            str(transaction.get("leg_id")) == leg.leg_id
            and transaction.get("role") == "MOTHER"
        ):
            return transaction
    return {}


def _leg_row(
    *,
    leg: Any,
    episode: Any,
    slippage_bps: int,
    run_id: str,
    pullback_event_id: str | None,
    entry_evidence: Mapping[str, Any],
    groups: Sequence[str] = (),
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
    return {
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
        "episode_ordinal": window.ordinal,
        "episode_key": _episode_key(episode),
        "source_selection_date_D": window.source_selection_date.isoformat(),
        "monitor_effective_date_E": window.monitor_effective_from.isoformat(),
        "regime_window_start": window.regime_window_start.isoformat(),
        "regime_window_end": window.regime_window_end.isoformat(),
        "entry_signal_date_T": leg.entry_date.isoformat(),
        "entry_reference_close": _dstr(leg.entry_reference_close),
        "entry_fill_price": _dstr(leg.entry_fill_price),
        "entry_fast_sma": _dstr(entry_evidence.get("entry_fast_sma")),
        "shares": int(leg.shares),
        "initial_stop": _dstr(leg.initial_stop),
        "initial_r": _dstr(leg.initial_r),
        "pivot_low": _dstr(entry_evidence.get("pivot_low")),
        "breakout_level": _dstr(entry_evidence.get("breakout_level")),
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
        "exit_date": None if leg.exit_date is None else leg.exit_date.isoformat(),
        "exit_reason": (
            None if leg.exit_reason is None else str(leg.exit_reason.value)
        ),
        "exit_reference_close": _dstr(leg.exit_reference_close),
        "exit_fill_price": _dstr(leg.exit_fill_price),
        "buy_total": _dstr(leg.buy_total),
        "sell_net": _dstr(leg.sell_net),
        "gross_pnl": _dstr(gross),
        "costed_net_pnl": _dstr(costed_net),
        "net_return_pct": _dstr(net_return, 6),
        "open_censored": leg.exit_date is None,
        "selection_groups": "|".join(groups),
        "pullback_event_id": pullback_event_id,
    }


def summarize_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Per-strategy closed-leg statistics, plus one descriptive combined row."""

    out: list[dict[str, Any]] = []
    groups: list[tuple[str, str]] = [
        (strategy.value, "per strategy; medium and long are independent")
        for strategy in STRATEGY_ORDER
    ]
    groups.append(
        (
            DESCRIPTIVE_BOTH,
            (
                "descriptive only: the two independent strategies' trades added "
                "together; NOT a single-account return, no shared capital or "
                "position-conflict rule exists in this round"
            ),
        )
    )
    for label, note in groups:
        for slippage_bps in SLIPPAGE_SCENARIOS:
            subset = [
                row
                for row in rows
                if row["slippage_bps"] == slippage_bps
                and (label == DESCRIPTIVE_BOTH or row["strategy"] == label)
            ]
            closed = [row for row in subset if not row["open_censored"]]
            open_count = len(subset) - len(closed)
            pnls = [Decimal(str(row["costed_net_pnl"])) for row in closed]
            returns = [Decimal(str(row["net_return_pct"])) for row in closed]
            wins = [value for value in pnls if value > 0]
            losses = [value for value in pnls if value < 0]
            flats = [value for value in pnls if value == 0]
            out.append(
                {
                    "strategy": label,
                    "slippage_bps": slippage_bps,
                    "closed_leg_count": len(closed),
                    "open_leg_count": open_count,
                    "win_count": len(wins),
                    "loss_count": len(losses),
                    "flat_count": len(flats),
                    "win_rate": (
                        None
                        if not closed
                        else _dstr(Decimal(len(wins)) / Decimal(len(closed)), 6)
                    ),
                    "mean_costed_net_pnl": (
                        None if not closed else _dstr(Decimal(statistics.mean(pnls)))
                    ),
                    "median_costed_net_pnl": (
                        None
                        if not closed
                        else _dstr(Decimal(statistics.median(pnls)))
                    ),
                    "mean_return_pct": (
                        None
                        if not closed
                        else _dstr(Decimal(statistics.mean(returns)), 6)
                    ),
                    "median_return_pct": (
                        None
                        if not closed
                        else _dstr(Decimal(statistics.median(returns)), 6)
                    ),
                    "gross_profit": (
                        None if not closed else _dstr(sum(wins, Decimal("0")))
                    ),
                    "gross_loss": (
                        None if not closed else _dstr(sum(losses, Decimal("0")))
                    ),
                    "costed_net_pnl_total": (
                        None if not closed else _dstr(sum(pnls, Decimal("0")))
                    ),
                    "note": note,
                }
            )
    return out


def build_group_attribution(
    rows: Sequence[Mapping[str, Any]],
    *,
    code_programs_by_date: Mapping[str, Mapping[str, Sequence[str]]],
) -> dict[str, Any]:
    """Multi-label attribution of legs to the six available selection groups."""

    primary = [
        row for row in rows if row["slippage_bps"] == PRIMARY_SLIPPAGE_BPS
    ]
    group_rows: list[dict[str, Any]] = []
    for group in AVAILABLE_GROUPS:
        selected = [
            row for row in primary if group in str(row["selection_groups"]).split("|")
        ]
        closed = [row for row in selected if not row["open_censored"]]
        pnls = [Decimal(str(row["costed_net_pnl"])) for row in closed]
        wins = [value for value in pnls if value > 0]
        group_rows.append(
            {
                "group_key": group,
                "group_label_zh": GROUP_LABELS.get(group, group),
                "leg_count_primary_bps": len(selected),
                "closed_leg_count": len(closed),
                "open_leg_count": len(selected) - len(closed),
                "win_count": len(wins),
                "win_rate": (
                    None
                    if not closed
                    else _dstr(Decimal(len(wins)) / Decimal(len(closed)), 6)
                ),
                "costed_net_pnl_total": (
                    None if not closed else _dstr(sum(pnls, Decimal("0")))
                ),
            }
        )
    unavailable = [
        {
            "group_key": group,
            "group_label_zh": GROUP_LABELS.get(group, group),
            "status": "UNAVAILABLE" if group == "chip_4" else "FAILED",
            "leg_count_primary_bps": None,
            "note": (
                "no candidates were produced for this group on any H2 selection "
                "day; it is never reported as zero trades"
            ),
        }
        for group in ("chip_4", "curated")
    ]
    multi = [
        row
        for row in primary
        if len([part for part in str(row["selection_groups"]).split("|") if part]) > 1
    ]
    return {
        "schema_version": GROUP_SCHEMA_VERSION,
        "primary_slippage_bps": PRIMARY_SLIPPAGE_BPS,
        "available_group_rows": group_rows,
        "unavailable_group_rows": unavailable,
        "multi_label_leg_count": len(multi),
        "multi_label_note": (
            "a stock selected by several groups on the same D is counted once "
            "under each group; the group column must never be summed into one "
            "portfolio total"
        ),
    }


def build_top_movers(
    rows: Sequence[Mapping[str, Any]], *, limit: int = 10
) -> dict[str, Any]:
    primary = [
        row
        for row in rows
        if row["slippage_bps"] == PRIMARY_SLIPPAGE_BPS
        and not row["open_censored"]
    ]
    ordered = sorted(primary, key=lambda row: Decimal(str(row["costed_net_pnl"])))
    fields = (
        "code",
        "symbol",
        "strategy",
        "source_selection_date_D",
        "monitor_effective_date_E",
        "entry_signal_date_T",
        "exit_date",
        "exit_reason",
        "shares",
        "costed_net_pnl",
        "net_return_pct",
    )
    return {
        "schema_version": "dual-ma-h2-single-entry-v-pivot-top-movers-v1",
        "winners": [
            {name: row.get(name) for name in fields}
            for row in reversed(ordered[-limit:])
        ],
        "losers": [
            {name: row.get(name) for name in fields} for row in ordered[:limit]
        ],
    }


def build_year_end_open_positions(
    *,
    rows: Sequence[Mapping[str, Any]],
    snapshot: Any,
    cutoff: date = AS_OF_CUTOFF,
) -> dict[str, Any]:
    """Value every still-open leg at the 2022-12-30 close; never a closed trade."""

    open_rows = [
        row
        for row in rows
        if row["open_censored"] and row["slippage_bps"] == PRIMARY_SLIPPAGE_BPS
    ]
    positions: list[dict[str, Any]] = []
    for row in open_rows:
        bars = snapshot.bars_by_symbol.get(row["symbol"], ())
        close = None
        for bar in bars:
            if bar.date <= cutoff:
                close = bar.close
            else:
                break
        if close is None:
            positions.append(
                {
                    **{
                        name: row.get(name)
                        for name in (
                            "code",
                            "symbol",
                            "strategy",
                            "source_selection_date_D",
                            "monitor_effective_date_E",
                            "entry_signal_date_T",
                            "shares",
                            "buy_total",
                        )
                    },
                    "cutoff_close": None,
                    "mark_to_market_net_pnl": None,
                    "note": "HOLD_NO_CUTOFF_CLOSE",
                }
            )
            continue
        shares = int(row["shares"])
        liquidation = CATHAY_WEB_028_TW_V1.sell(close, shares, InstrumentType.STOCK)
        buy_total = Decimal(str(row["buy_total"]))
        positions.append(
            {
                **{
                    name: row.get(name)
                    for name in (
                        "code",
                        "symbol",
                        "strategy",
                        "source_selection_date_D",
                        "monitor_effective_date_E",
                        "entry_signal_date_T",
                        "shares",
                        "initial_stop",
                    )
                },
                "buy_total": _dstr(buy_total),
                "cutoff_close": _dstr(close),
                "liquidation_cash_at_cutoff": _dstr(liquidation.cash_amount),
                "mark_to_market_net_pnl": _dstr(liquidation.cash_amount - buy_total),
            }
        )
    medium = [
        item for item in positions if item["strategy"] == StrategyKind.MEDIUM.value
    ]
    long = [
        item for item in positions if item["strategy"] == StrategyKind.LONG.value
    ]

    def _total(items: Sequence[Mapping[str, Any]]) -> str | None:
        values = [
            Decimal(str(item["mark_to_market_net_pnl"]))
            for item in items
            if item.get("mark_to_market_net_pnl") is not None
        ]
        return None if not values else _dstr(sum(values, Decimal("0")))

    return {
        "schema_version": OPEN_POSITIONS_SCHEMA_VERSION,
        "cutoff_date": cutoff.isoformat(),
        "cutoff_price_basis": "unadjusted vendor close proxy (2022-12-30 session)",
        "valuation_note": (
            "open legs are marked to the 2022-12-30 close with the frozen "
            "CATHAY_WEB_028_TW_V1 sell commission and stock tax; this is an "
            "unrealized study valuation, not a realized trade, and 2023 prices "
            "are never read"
        ),
        "open_position_count": len(positions),
        "medium_open_count": len(medium),
        "long_open_count": len(long),
        "medium_mark_to_market_net_pnl": _total(medium),
        "long_mark_to_market_net_pnl": _total(long),
        "positions": positions,
    }


def build_rejections(*, stats: Mapping[str, Any], plan: Any) -> dict[str, Any]:
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
        "run_id": None,
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
        "rejected_same_episode_reentries_total": sum(
            count
            for key, count in totals.items()
            if key.endswith(STATUS_REENTRY_REJECTED_SAME_EPISODE)
        ),
        "non_executable_signals_total": sum(
            count
            for key, count in totals.items()
            if key.endswith(STATUS_NON_EXECUTABLE)
        ),
        "filled_mother_orders_total": sum(
            count for key, count in totals.items() if key.endswith(STATUS_FILLED)
        ),
        "first_example_by_status": dict(stats["entry_decision_first_example"]),
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


def build_spot_checks(
    *,
    rows: Sequence[Mapping[str, Any]],
    calendar: Any,
    stats: Mapping[str, Any],
    limit: int = 3,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    for strategy in STRATEGY_ORDER:
        candidates = [
            row
            for row in rows
            if row["strategy"] == strategy.value
            and row["slippage_bps"] == PRIMARY_SLIPPAGE_BPS
            and not row["open_censored"]
            and row["pivot_confirmed_date"] is not None
        ]
        for row in candidates[:limit]:
            day_d = date.fromisoformat(row["source_selection_date_D"])
            day_e = date.fromisoformat(row["monitor_effective_date_E"])
            day_t = date.fromisoformat(row["entry_signal_date_T"])
            day_v = date.fromisoformat(row["pivot_confirmed_date"])
            day_x = date.fromisoformat(row["exit_date"])
            checks.append(
                {
                    "kind": "D_TO_E_TO_V_TO_T_CLOSED_LEG",
                    "strategy": strategy.value,
                    "code": row["code"],
                    "symbol": row["symbol"],
                    "selection_date_D": row["source_selection_date_D"],
                    "monitor_effective_date_E": row["monitor_effective_date_E"],
                    "E_is_next_official_session_after_D": (
                        calendar.next_session(day_d) == day_e
                    ),
                    "pivot_confirmed_date_V": row["pivot_confirmed_date"],
                    "entry_signal_date_T": row["entry_signal_date_T"],
                    "V_is_at_or_after_E": day_v >= day_e,
                    "T_is_at_or_after_V": day_t >= day_v,
                    "T_is_inside_episode_window": (
                        date.fromisoformat(row["regime_window_start"])
                        <= day_t
                        <= date.fromisoformat(row["regime_window_end"])
                    ),
                    "entry_requires_close_above_fast": (
                        row["entry_fast_sma"] is not None
                        and Decimal(str(row["entry_reference_close"]))
                        > Decimal(str(row["entry_fast_sma"]))
                    ),
                    "pillar_stop_equals_pivot_low": (
                        row["pivot_low"] is not None
                        and row["initial_stop"] == row["pivot_low"]
                    ),
                    "exit_date": row["exit_date"],
                    "exit_is_at_or_after_T": day_x >= day_t,
                    "exit_reason": row["exit_reason"],
                    "shares": row["shares"],
                    "entry_fill_equals_reference_at_0bps": (
                        row["entry_fill_price"] == row["entry_reference_close"]
                    ),
                    "entry_reference_close": row["entry_reference_close"],
                    "entry_fast_sma": row["entry_fast_sma"],
                    "pivot_low": row["pivot_low"],
                    "breakout_level": row["breakout_level"],
                    "buy_total": row["buy_total"],
                    "sell_net": row["sell_net"],
                    "costed_net_pnl": row["costed_net_pnl"],
                    "hand_calc_sell_net_minus_buy_total": _dstr(
                        Decimal(str(row["sell_net"]))
                        - Decimal(str(row["buy_total"]))
                    ),
                    "costed_net_pnl_matches_hand_calc": (
                        Decimal(str(row["costed_net_pnl"]))
                        == Decimal(str(row["sell_net"]))
                        - Decimal(str(row["buy_total"]))
                    ),
                    "note": (
                        "signal and fill share the T session close "
                        "(EOD_CLOSE_PROXY_V1); sell_net already nets the sell "
                        "commission and the STOCK sell tax, buy_total already "
                        "nets the buy commission"
                    ),
                }
            )
    for status in (
        STATUS_ADD_REJECTED_SINGLE_ENTRY,
        STATUS_REENTRY_REJECTED_SAME_EPISODE,
    ):
        candidates = [
            (key, example)
            for key, example in stats["entry_decision_first_example"].items()
            if key.endswith(status)
        ]
        preferred = [
            item for item in candidates if f"|{PRIMARY_SLIPPAGE_BPS}|" in item[0]
        ]
        for key, example in (preferred or candidates)[:1]:
            checks.append(
                {
                    "kind": "VARIANT_REJECTION_OBSERVED",
                    "status": status,
                    "key": key,
                    **example,
                    "note": (
                        "the variant engine recorded this rejection as a named "
                        "ENTRY_DECISION event instead of creating a leg"
                    ),
                }
            )
    return {
        "schema_version": SPOT_CHECK_SCHEMA_VERSION,
        "check_count": len(checks),
        "checks": checks,
    }


def build_exit_reason_breakdown(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Closed-leg exit reason distribution at the 0 bps baseline."""

    by_strategy: dict[str, list[dict[str, Any]]] = {}
    for strategy in (StrategyKind.MEDIUM.value, StrategyKind.LONG.value):
        closed = [
            row
            for row in rows
            if row["strategy"] == strategy
            and row["slippage_bps"] == PRIMARY_SLIPPAGE_BPS
            and not row["open_censored"]
        ]
        counter = Counter(str(row["exit_reason"]) for row in closed)
        items: list[dict[str, Any]] = []
        for reason, count in sorted(counter.items(), key=lambda item: (-item[1], item[0])):
            net = sum(
                (
                    Decimal(str(row["costed_net_pnl"]))
                    for row in closed
                    if str(row["exit_reason"]) == reason
                ),
                Decimal("0"),
            )
            items.append(
                {
                    "exit_reason": reason,
                    "count": count,
                    "costed_net_pnl_total": _dstr(net),
                }
            )
        by_strategy[strategy] = items
    return {
        "schema_version": (
            "dual-ma-h2-single-entry-v-pivot-exit-reasons-v1"
        ),
        "primary_slippage_bps": PRIMARY_SLIPPAGE_BPS,
        "by_strategy": by_strategy,
    }


def render_report_zh(
    *,
    manifest: Mapping[str, Any],
    summary: Sequence[Mapping[str, Any]],
    funnel: Mapping[str, Any],
    groups: Mapping[str, Any],
    top_movers: Mapping[str, Any],
    exit_reasons: Mapping[str, Any],
    open_positions: Mapping[str, Any],
    rejections: Mapping[str, Any],
    spot_checks: Mapping[str, Any],
    preflight: Mapping[str, Any],
) -> str:
    primary = {
        row["strategy"]: row
        for row in summary
        if row["slippage_bps"] == PRIMARY_SLIPPAGE_BPS
    }
    lines: list[str] = []
    lines.append("# 2022 下半年「站回均線＋V 型樞紐」單筆進場研究回測")
    lines.append("")
    lines.append(
        "本報告是 00 於績效計算前凍結的探索研究結果，research_only=true、"
        "point_in_time_verified=false。它回答的是：在 2022/07/01 到 2022/12/30 "
        "的 129 個官方選股日裡，中期 5/21 與長期 21/144 兩套策略，每個已啟動的選股 "
        "episode 最多一張母單、不加碼，而且只有在「收盤站回快線」與「因果 V 型樞紐"
        "已確認」同一天成立的第一個 T 日收盤才進場，成本後長相如何。這不是正式"
        "歷史可交易績效，也不是任何帳戶的報酬或最大回撤。"
    )
    lines.append("")
    lines.append("## 一、白話結論（0 bps 主基準）")
    lines.append("")
    lines.append(
        "本次 129 個官方選股日、0 bps 主基準的實際成交：中期 5/21 共 {mc} 筆、"
        "長期 21/144 共 {lc} 筆（成交筆數含已平倉與未平倉，成本已含手續費與證交稅）。"
        "兩個策略在本次樣本內的成本後淨損益皆為負值，下表先分別列中期與長期，"
        "合計列只是把兩策略交易描述性相加，不是投資組合或帳戶報酬。".format(
            mc=(
                int(primary.get(StrategyKind.MEDIUM.value, {}).get("closed_leg_count", 0))
                + int(primary.get(StrategyKind.MEDIUM.value, {}).get("open_leg_count", 0))
            ),
            lc=(
                int(primary.get(StrategyKind.LONG.value, {}).get("closed_leg_count", 0))
                + int(primary.get(StrategyKind.LONG.value, {}).get("open_leg_count", 0))
            ),
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
                c=_fmt(row["closed_leg_count"]),
                o=_fmt(row["open_leg_count"]),
                w=_pct(row["win_rate"]),
                m=_fmt(row["mean_costed_net_pnl"]),
                md=_fmt(row["median_costed_net_pnl"]),
                gp=_fmt(row["gross_profit"]),
                gl=_fmt(row["gross_loss"]),
                n=_fmt(row["costed_net_pnl_total"]),
            )
        )
    lines.append("")
    lines.append(
        "BOTH_DESCRIPTIVE_ONLY 只是把兩策略各自的交易描述性相加；本輪沒有共用資金、"
        "部位衝突或先後順序規則，因此它不是單一資金帳戶的總報酬，也不可與成本敏感度"
        "的三欄相加。"
    )
    lines.append("")
    lines.append("## 二、選股 → 監控 → V 型 → 買入 → 出場漏斗（0 bps）")
    lines.append("")
    lines.append(
        "| 策略 | 選股事件 | 啟動監控 episode | 拉回事件 | 曾站回快線但樞紐未到 | "
        "V 型已確認 | 兩條件同日成立 | 實際成交 | 已平倉 | 未平倉 |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for strategy in STRATEGY_ORDER:
        bucket = funnel["by_strategy"].get(strategy.value, {})
        lines.append(
            "| {s} | {sel} | {act} | {pb} | {ro} | {vc} | {es} | {fill} | {cl} | {op} |".format(
                s=strategy.value,
                sel=_fmt(bucket.get("selection_events_total")),
                act=_fmt(bucket.get("episodes")),
                pb=_fmt(bucket.get("episodes_with_pullback")),
                ro=_fmt(bucket.get("episodes_reclaim_only_no_pivot")),
                vc=_fmt(bucket.get("episodes_with_pivot_confirmed")),
                es=_fmt(bucket.get("episodes_with_entry_signal")),
                fill=_fmt(bucket.get("episodes_with_a_filled_leg")),
                cl=_fmt(bucket.get("filled_closed_legs")),
                op=_fmt(bucket.get("filled_open_legs")),
            )
        )
    lines.append("")
    lines.append(
        "只有「快線站回」而樞紐未確認，或只有「樞紐確認」而收盤還沒站上快線，"
        "都不會成交；條件先後成立時，一律等到第一個兩者同時成立的 T 日收盤才進場，"
        "不會回填到較早的日期。"
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
    lines.append(
        "STRUCTURAL_STOP 是跌破當時有效的結構防線（起始是 V 樞紐低點，"
        "之後可能隨已確認防線上移，例如日線 n=2 道氏防線）；"
        "REGIME_EXIT 是快線不再高於慢線的均線趨勢終止。"
    )
    lines.append("")
    lines.append("## 三、成本敏感度（每邊 10 / 20 bps，僅敏感度）")
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
                c=_fmt(row["closed_leg_count"]),
                w=_pct(row["win_rate"]),
                m=_fmt(row["mean_costed_net_pnl"]),
                md=_fmt(row["median_costed_net_pnl"]),
                gp=_fmt(row["gross_profit"]),
                gl=_fmt(row["gross_loss"]),
                n=_fmt(row["costed_net_pnl_total"]),
            )
        )
    lines.append("")
    lines.append("## 四、六個可用選股組別（重複標籤，不可相加）")
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
                legs=_fmt(row["leg_count_primary_bps"]),
                cl=_fmt(row["closed_leg_count"]),
                op=_fmt(row["open_leg_count"]),
                w=_pct(row["win_rate"]),
                n=_fmt(row["costed_net_pnl_total"]),
            )
        )
    for row in groups["unavailable_group_rows"]:
        status_zh = "缺期（UNAVAILABLE）" if row["status"] == "UNAVAILABLE" else "失敗（FAILED）"
        lines.append(
            "| {lbl} | {key} | 無 | 無 | 無 | 無 | 2022 H2 全期{st}，"
            "本次沒有任何選股候選；不以零交易呈現 |".format(
                lbl=row["group_label_zh"],
                key=row["group_key"],
                st=status_zh,
            )
        )
    lines.append("")
    lines.append(
        "同一檔股票在 D 日可能同時被多個組別選中，會分別列在每個組別下一次；"
        "這些欄位是多標籤揭露，不可相加當成單一投組整體，也不可當成八組完整績效。"
        "chip_4（TDCC 週資料）在 2022 H2 全期缺期、curated 依賴失敗，"
        "因此本輪是每日 6/8 的部分探索樣本，缺的兩組不以零交易呈現。"
    )
    lines.append("")
    lines.append("## 五、十大賺錢與十大虧損股票（0 bps，已平倉）")
    lines.append("")
    lines.append("### 十大賺錢")
    lines.append("")
    lines.append(
        "| 代號 | 證券 | 策略 | D 選股 | T 進場 | 出場 | 原因 | 股數 | 成本後損益(元) | 報酬 |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for row in top_movers["winners"]:
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
                ret=_pct(row["net_return_pct"], already_percent=True),
            )
        )
    winners = top_movers["winners"]
    if winners:
        top = winners[0]
        lines.append("")
        strategy_total = primary.get(top["strategy"], {})
        total_text = strategy_total.get("costed_net_pnl_total")
        share_text = ""
        if total_text not in (None, "") and Decimal(str(total_text)) != 0:
            share = (
                Decimal(str(top["costed_net_pnl"]))
                / abs(Decimal(str(total_text)))
                * Decimal(100)
            )
            share_text = "，約為該策略成本後淨損益絕對值的 {s}%".format(
                s=_dstr(share, 2)
            )
        lines.append(
            "- 極端贏家揭露：{code} {symbol}（{strategy}）單筆成本後損益 {pnl} 元、"
            "報酬 {ret}{share}，是本次最大單筆獲利。價格為未還原的供應商收盤代理，"
            "本輪未處理除權息、減資或其他股本事件；若個股在區間內發生這類事件，"
            "該筆報酬與費用可能被高估或低估，屬已知限制。".format(
                code=top["code"],
                symbol=top["symbol"],
                strategy=top["strategy"],
                pnl=top["costed_net_pnl"],
                ret=_pct(top["net_return_pct"], already_percent=True),
                share=share_text,
            )
        )
        lines.append("")
    lines.append("### 十大虧損")
    lines.append("")
    lines.append(
        "| 代號 | 證券 | 策略 | D 選股 | T 進場 | 出場 | 原因 | 股數 | 成本後損益(元) | 報酬 |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for row in top_movers["losers"]:
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
                ret=_pct(row["net_return_pct"], already_percent=True),
            )
        )
    lines.append("")
    lines.append("## 六、典型個股：D → E → V → T 與成本")
    lines.append("")
    for check in spot_checks["checks"]:
        if check["kind"] != "D_TO_E_TO_V_TO_T_CLOSED_LEG":
            continue
        lines.append(
            "- {code} {symbol}（{strategy}）：D={d} → E={e} → V 型確認={v} → "
            "T 進場={t}（收盤 {close} > 快線 {fast}）→ 出場 {x}（{reason}）。"
            "樞紐低點={pivot}，突破價={brk}，股數 {shares}，"
            "買進總成本 {buy} 元，賣出淨額 {sell} 元，成本後損益 {pnl} 元"
            "（手算相符={ok}）。".format(
                code=check["code"],
                symbol=check["symbol"],
                strategy=check["strategy"],
                d=check["selection_date_D"],
                e=check["monitor_effective_date_E"],
                v=check["pivot_confirmed_date_V"],
                t=check["entry_signal_date_T"],
                close=check.get("entry_reference_close", ""),
                fast=check.get("entry_fast_sma", ""),
                x=check["exit_date"],
                reason=check["exit_reason"],
                pivot=check.get("pivot_low", ""),
                brk=check.get("breakout_level", ""),
                shares=check["shares"],
                buy=check["buy_total"],
                sell=check["sell_net"],
                pnl=check["costed_net_pnl"],
                ok=check["costed_net_pnl_matches_hand_calc"],
            )
        )
    lines.append("")
    lines.append("## 七、2022/12/30 未平倉獨立估值")
    lines.append("")
    lines.append(
        "截至截點仍持有的部位不當作已平倉，也不以 2023 之後的價格補平倉；"
        "下表以 2022-12-30 收盤推估的未實現損益單獨列出。"
    )
    lines.append("")
    lines.append(
        "- 未平倉筆數：{n}（中期 {m}、長期 {l}）".format(
            n=open_positions["open_position_count"],
            m=open_positions["medium_open_count"],
            l=open_positions["long_open_count"],
        )
    )
    lines.append(
        "- 截至截點未實現損益：中期 {m} 元、長期 {l} 元".format(
            m=open_positions["medium_mark_to_market_net_pnl"],
            l=open_positions["long_mark_to_market_net_pnl"],
        )
    )
    lines.append("")
    if open_positions["positions"]:
        lines.append(
            "| 代號 | 證券 | 策略 | D 選股 | T 進場 | 股數 | 買進成本(元) | "
            "截點收盤 | 未實現損益(元) |"
        )
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for item in open_positions["positions"]:
            lines.append(
                "| {c} | {s} | {st} | {d} | {t} | {sh} | {buy} | {close} | {pnl} |".format(
                    c=item.get("code"),
                    s=item.get("symbol"),
                    st=item.get("strategy"),
                    d=item.get("source_selection_date_D"),
                    t=item.get("entry_signal_date_T"),
                    sh=item.get("shares"),
                    buy=item.get("buy_total"),
                    close=item.get("cutoff_close"),
                    pnl=item.get("mark_to_market_net_pnl"),
                )
            )
    lines.append("")
    lines.append("## 八、檔案與可重跑")
    lines.append("")
    lines.append(
        "- trades.csv：每一腿一列，含 D（選股日）、E（監控生效日）、V（樞紐確認日）、"
        "T（進場訊號日）、進出價、股數、初始停損、樞紐低點、突破價、出場原因與成本後損益；"
        "0/10/20 bps 都在同一張表。"
    )
    lines.append(
        "- strategy_summary.csv / funnel.json / group_attribution.json：上表的機器可讀版本。"
    )
    lines.append("- year_end_open_positions.json：2022/12/30 未平倉與未實現估值。")
    lines.append(
        "- rejections.json、spot_checks.json、preflight.json、parent_runs.json。"
    )
    lines.append("- manifest.json、readback.json：來源 SHA、gates 與產物 SHA256。")
    lines.append("")
    lines.append("## 九、方法與邊界")
    lines.append("")
    lines.append(
        "- D→E 因果沿用 2022-h1-selection-episode-lifecycle-v1.md：D 收盤後才成立名單，"
        "E 為下一官方交易日，只有該策略在 E 嚴格 fast > slow 才啟動監控；E 不合格即到期，"
        "缺資料或暖機不足維持 HOLD，不順延。"
    )
    lines.append(
        "- 進場沿用 v0.5.0-preregistered 第 3 節的因果狀態機（PIVOT_V_CONFIRM_V1）："
        "拉回事件中第一次完整日 K 收盤跌破快線開始，底部取事件最低 Low，"
        "左臂 1–3 根、右臂 1–3 根、右側收盤嚴格突破 breakout level 才確認；"
        "更低點重置、確認後跌破 pivot low 失效。只有確認且當日收盤嚴格站上快線才成交。"
    )
    lines.append(
        "- 出場、成本、股數與風險上限沿用 v0.4.1-frozen：999 股 / 10,000 元名目、"
        "每腿初始 R 不超過 2,000 元、CATHAY_WEB_028_TW_V1 手續費與證交稅、"
        "EOD_CLOSE_PROXY_V1 當日收盤成交代理、結構停損與 EXIT_DOW_TRAIL、均線趨勢終止。"
    )
    lines.append(
        "- 本變體只改進場訊號：持有中不加碼；每個已啟動選股 episode 最多一張母單，"
        "出場後同 episode 不再進場。舊 episode 依 regime 失效結束後，新的 D/E/訊號可另起新 episode。"
    )
    lines.append(
        "- 價格為未還原的供應商收盤代理（UNADJUSTED_VENDOR_CLOSE_PROXY），非官方收盤；"
        "只讀 2022 及必要的 2021 暖機價，未讀 2023/2024 績效。2022-12-30 是本次績效截點。"
    )
    lines.append(
        "- 本輪不做 50 萬元組合淨值、資金佔用、最大回撤或參數最佳化；那需要另定資金衝突規則，"
        "本輪不虛構。"
    )
    lines.append("")
    lines.append("## 十、揭露與停止點")
    lines.append("")
    lines.append(
        "- 出場的 Dow 防線仍是固定的日線 n=2 單層工程基準，不是已驗證的大小級道氏防線；"
        "V 型樞紐只用在進場，不供 Dow 或出場消費。"
    )
    lines.append(
        "- 這是每日 6/8 的部分探索樣本：chip_4 缺期、curated 失敗，"
        "因此不是完整八組績效，也不是正式歷史可交易績效。"
    )
    holds = rejections.get("lifecycle_named_holds", {})
    reason_counts = holds.get("reason_counts", {})
    lines.append(
        "- 年底截斷：D 日為 2022-12-30 的選股事件，其下一個官方交易日已落在 2023，"
        "本輪以 HOLD_NO_NEXT_OFFICIAL_SESSION_AFTER_D 記為 {n} 筆並直接截斷，"
        "不啟動監控、不以 2023 價格交易；另有 "
        "HOLD_MISSING_E_BAR_OR_UNVERIFIED_NON_TRADING {e} 筆、"
        "HOLD_SMA_WARMUP_INSUFFICIENT {w} 筆。".format(
            n=reason_counts.get("HOLD_NO_NEXT_OFFICIAL_SESSION_AFTER_D", 0),
            e=reason_counts.get(
                "HOLD_MISSING_E_BAR_OR_UNVERIFIED_NON_TRADING", 0
            ),
            w=reason_counts.get("HOLD_SMA_WARMUP_INSUFFICIENT", 0),
        )
    )
    lines.append(
        "- Preflight：官方 H2 選股日 {sessions} 天、月報 {matched}、"
        "價格覆蓋缺項 {missing} 個。".format(
            sessions=preflight.get("official_h2_session_count"),
            matched=(
                "全部相符" if preflight.get("parents_all_matched") else "有落差"
            ),
            missing=len(preflight.get("codes_without_price_series") or ()),
        )
    )
    lines.append("- 完成本報告後停在 00 QA，不自行往 2023/24 或新變體。")
    lines.append("")
    lines.append(
        "run_id：{r}／module：{m}／run 狀態：{st}".format(
            r=manifest["run_id"], m=MODULE_VERSION, st=manifest["status"]
        )
    )
    lines.append("")
    return "\n".join(lines)


def load_h2_inputs(
    workdata_root: str | Path,
    *,
    progress: Callable[[str, int, int, Mapping[str, Any]], None] | None = None,
) -> dict[str, Any]:
    workdata_root = Path(workdata_root)
    pilot_root = workdata_root / pilot.PILOT_ROOT_RELATIVE
    parent = verify_h2_parent_runs(pilot_root, expectations=pilot.FROZEN_EXPECTATIONS)
    run_by_date = build_h2_run_by_date(pilot_root)
    selection_dates = tuple(sorted(run_by_date))
    calendar = pilot.load_official_calendar(
        workdata_root / pilot.CALENDAR_RELATIVE_PATH,
        expectations=pilot.FROZEN_EXPECTATIONS,
    )
    binding = pilot.load_price_basis_binding(
        workdata_root / pilot.C1_BINDING_RELATIVE_PATH,
        expectations=pilot.FROZEN_EXPECTATIONS,
    )
    reports = pilot.load_pilot_selection_dates(
        pilot_root, selection_dates=selection_dates, run_by_date=run_by_date
    )
    resolution = pilot.resolve_symbols(pilot_root, reports)
    snapshot = pilot.load_price_snapshot(
        workdata_root / pilot.PRICE_SNAPSHOT_RELATIVE_PATH,
        list(resolution.symbol_by_code.values()),
        expectations=pilot.FROZEN_EXPECTATIONS,
    )
    code_programs_by_date: dict[str, Mapping[str, Sequence[str]]] = {
        report.report_date: report.code_programs for report in reports
    }
    universe_sources_path = workdata_root / UNIVERSE_SOURCES_RELATIVE_PATH
    if not universe_sources_path.is_file():
        raise H2VPivotError(
            f"frozen universe sources are missing: {universe_sources_path}"
        )
    universe_sha = pilot.sha256_file(universe_sources_path)
    if universe_sha != UNIVERSE_SOURCES_RAW_SHA256:
        raise H2VPivotError(
            f"universe sources sha256 {universe_sha} != expected "
            f"{UNIVERSE_SOURCES_RAW_SHA256}"
        )
    if progress is not None:
        progress("inputs_loaded", 1, 1, {"reports": len(reports)})
    return {
        "workdata_root": workdata_root,
        "pilot_root": pilot_root,
        "parent": parent,
        "run_by_date": run_by_date,
        "selection_dates": selection_dates,
        "calendar": calendar,
        "binding": binding,
        "reports": reports,
        "resolution": resolution,
        "snapshot": snapshot,
        "code_programs_by_date": code_programs_by_date,
        "universe_sources_path": universe_sources_path,
        "universe_sha": universe_sha,
    }


def build_h2_preflight(inputs: Mapping[str, Any]) -> dict[str, Any]:
    """Light H2 calendar / report SHA / price-coverage preflight."""

    calendar = inputs["calendar"]
    snapshot = inputs["snapshot"]
    reports = inputs["reports"]
    resolution = inputs["resolution"]
    official_sessions = [
        day.isoformat() for day in calendar.sessions_between(H2_FIRST_DAY, H2_LAST_DAY)
    ]
    present = sorted(report.report_date for report in reports)
    status_counts: Counter[str] = Counter()
    every_day_expected_shape = True
    per_day: list[dict[str, Any]] = []
    for report in reports:
        day_counts = Counter(report.program_status.values())
        every_day_expected_shape = every_day_expected_shape and (
            dict(day_counts) == dict(EXPECTED_PROGRAM_STATUS_COUNTS)
        )
        for status, count in day_counts.items():
            status_counts[status] += count
        per_day.append(
            {
                "report_date": report.report_date,
                "pilot_run_dir": report.run_dir,
                "pilot_report_sha256": report.report_sha256,
                "available_groups": list(report.available_groups),
                "missing_groups": list(report.missing_groups),
                "deduplicated_candidate_count": report.candidate_count,
            }
        )
    selected_codes = sorted(
        {code for report in reports for code in report.code_programs}
    )
    unknown_codes = [
        code for code in selected_codes if code in set(resolution.unknown_codes)
    ]
    conflicting_codes = [
        code for code in selected_codes if code in set(resolution.conflicting_codes)
    ]
    symbols = {
        resolution.symbol_by_code[code]
        for code in selected_codes
        if code in resolution.symbol_by_code
    }
    codes_without_price_series = sorted(
        code
        for code in selected_codes
        if code in resolution.symbol_by_code
        and resolution.symbol_by_code[code] not in snapshot.bars_by_symbol
    )
    first_session_by_symbol = {
        symbol: bars[0].date.isoformat() if bars else None
        for symbol, bars in snapshot.bars_by_symbol.items()
    }
    return {
        "schema_version": PREFLIGHT_SCHEMA_VERSION,
        "run_id": RUN_ID_DEFAULT,
        "module_version": MODULE_VERSION,
        "h2_first_day": H2_FIRST_DAY.isoformat(),
        "h2_last_day": H2_LAST_DAY.isoformat(),
        "official_h2_session_count": len(official_sessions),
        "expected_h2_session_count": EXPECTED_H2_SESSION_COUNT,
        "reported_date_count": len(present),
        "dates_are_exactly_official_h2_sessions": present == official_sessions,
        "missing_official_sessions": [
            day for day in official_sessions if day not in set(present)
        ],
        "unexpected_dates": [
            day for day in present if day not in set(official_sessions)
        ],
        "program_status_counts": dict(sorted(status_counts.items())),
        "expected_program_status_per_day": dict(EXPECTED_PROGRAM_STATUS_COUNTS),
        "every_day_has_the_expected_program_status_shape": every_day_expected_shape,
        "parents_all_matched": inputs["parent"]["all_matched"],
        "parent_runs": inputs["parent"],
        "selected_code_count": len(selected_codes),
        "resolved_symbol_count": len(symbols),
        "codes_without_symbol_identity": sorted(unknown_codes + conflicting_codes),
        "codes_without_price_series": codes_without_price_series,
        "price_snapshot_content_sha256": snapshot.content_sha256,
        "price_snapshot_last_session": snapshot.last_session.isoformat(),
        "price_snapshot_never_reaches_2023": (
            snapshot.last_session.year <= LATEST_READABLE_YEAR
        ),
        "universe_sources": {
            "path": str(inputs["universe_sources_path"]),
            "raw_sha256": inputs["universe_sha"],
            "source_id": UNIVERSE_SOURCES_SOURCE_ID,
        },
        "dates": per_day,
    }


def run_h2_single_entry_v_pivot(
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
    inputs = load_h2_inputs(workdata_root, progress=progress)
    calendar = inputs["calendar"]
    snapshot = inputs["snapshot"]
    plan = pilot.build_lifecycle_plan(
        reports=inputs["reports"],
        resolution=inputs["resolution"],
        snapshot=snapshot,
        calendar=calendar,
    )
    preflight = build_h2_preflight(inputs)
    if run_dir.exists():
        raise H2VPivotError(
            f"refusing to overwrite an existing run directory: {run_dir}"
        )
    run_dir.mkdir(parents=True, exist_ok=True)
    commit = runner_commit or pilot.resolve_runner_commit(
        Path(__file__).resolve().parents[2]
    )["runner_commit"]

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
    summary = summarize_rows(rows)
    rejections = build_rejections(stats=stats, plan=plan)
    rejections["run_id"] = run_id
    groups = build_group_attribution(
        rows, code_programs_by_date=inputs["code_programs_by_date"]
    )
    top_movers = build_top_movers(rows)
    open_positions = build_year_end_open_positions(rows=rows, snapshot=snapshot)
    spot_checks = build_spot_checks(rows=rows, calendar=calendar, stats=stats)
    exit_reasons = build_exit_reason_breakdown(rows)

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
        bucket["activated_episodes"] = bucket.get("episodes", 0)
        bucket["filled_closed_legs"] = sum(
            1
            for row in rows
            if row["strategy"] == strategy.value
            and row["slippage_bps"] == PRIMARY_SLIPPAGE_BPS
            and not row["open_censored"]
        )
        bucket["filled_open_legs"] = sum(
            1
            for row in rows
            if row["strategy"] == strategy.value
            and row["slippage_bps"] == PRIMARY_SLIPPAGE_BPS
            and row["open_censored"]
        )
    funnel = {
        "schema_version": FUNNEL_SCHEMA_VERSION,
        "primary_slippage_bps": PRIMARY_SLIPPAGE_BPS,
        "note": (
            "counts are per activated selection episode at 0 bps; a stage is "
            "reached when the causal engine emitted the corresponding event "
            "inside that episode, never backfilled to an earlier date"
        ),
        "by_strategy": funnel_by_strategy,
    }

    h2_sessions = calendar.sessions_between(H2_FIRST_DAY, H2_LAST_DAY)
    primary_rows = [
        row for row in rows if row["slippage_bps"] == PRIMARY_SLIPPAGE_BPS
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
        or Decimal(str(row["buy_total"])) > Decimal("10000")
        or Decimal(str(row["initial_r"])) > Decimal("2000")
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
    multi_leg_rows = [row for row in rows if row["leg_number"] != 1]
    entry_without_confirm = [
        row
        for row in primary_rows
        if row["pivot_confirmed_date"] is None or row["pivot_low"] is None
    ]
    entry_below_fast = [
        row
        for row in primary_rows
        if row["entry_fast_sma"] is None
        or Decimal(str(row["entry_reference_close"]))
        <= Decimal(str(row["entry_fast_sma"]))
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
            if inputs["universe_sha"] == UNIVERSE_SOURCES_RAW_SHA256
            else "FAIL"
        ),
        "single_leg_per_symbol_strategy_episode_slippage": (
            "PASS" if stats["single_leg_per_episode"] else "FAIL"
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
        "sizing_within_frozen_leg_limits": "PASS" if not bad_sizing else "FAIL",
        "zero_bps_fill_equals_signal_day_close": (
            "PASS" if not fill_mismatch else "FAIL"
        ),
        "no_open_leg_carries_a_closed_leg_result": (
            "PASS" if not open_with_stats else "FAIL"
        ),
        "no_additions_were_filled": "PASS" if not multi_leg_rows else "FAIL",
        "spot_checks_hand_calculation_matches": (
            "PASS"
            if all(
                check.get("costed_net_pnl_matches_hand_calc", True)
                for check in spot_checks["checks"]
            )
            else "FAIL"
        ),
    }
    if episode_limit is not None:
        gates["full_129_day_sample"] = "FAIL"
    readback_status = (
        "PASS" if all(value == "PASS" for value in gates.values()) else "HOLD"
    )

    trade_rows = [[row.get(name) for name in TRADE_ROW_FIELDS] for row in rows]
    summary_rows = [[row.get(name) for name in SUMMARY_FIELDS] for row in summary]
    payloads: dict[str, bytes] = {
        "trades.csv": _csv_bytes(TRADE_ROW_FIELDS, trade_rows),
        "strategy_summary.csv": _csv_bytes(SUMMARY_FIELDS, summary_rows),
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
        "rejections.json": pilot._json_bytes(rejections),
        "spot_checks.json": pilot._json_bytes(spot_checks),
        "parent_runs.json": pilot._json_bytes(inputs["parent"]),
        "preflight.json": pilot._json_bytes(preflight),
    }
    artifact_hashes = {
        name: pilot.sha256_bytes(blob) for name, blob in payloads.items()
    }

    manifest = {
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
            "entry signal is ENTRY_RECLAIM_V_PIVOT_CONFIRMED: an entry requires "
            "a causally confirmed PIVOT_V_CONFIRM_V1 bottom and a strict close "
            "above the fast SMA on the same T session",
            "no additions: an entry signal raised while a leg of the activated "
            "selection episode is open is rejected",
            "at most one mother order per activated selection episode; after an "
            "exit inside the same episode no re-entry is allowed",
        ],
        "selection_program_modified": False,
        "sample_definition": (
            "the six accepted 01 selection runs for every official 2022 H2 "
            "session (2022-07-01 .. 2022-12-30); a 6/8 TDCC partial exploration "
            "sample; 2023 / 2024 data is never read and no formal performance "
            "label is produced"
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
        "semantic_labels": dict(SEMANTIC_LABELS),
        "runner_commit": commit,
        "inputs": {
            "official_calendar": calendar.as_binding(),
            "price_snapshot_content_sha256": snapshot.content_sha256,
            "price_snapshot_manifest_file_sha256": snapshot.manifest_file_sha256,
            "price_basis_binding": inputs["binding"].as_dict(),
            "universe_sources": {
                "path": str(inputs["universe_sources_path"]),
                "raw_sha256": inputs["universe_sha"],
                "source_id": UNIVERSE_SOURCES_SOURCE_ID,
            },
            "parent_runs": inputs["parent"],
        },
        "coverage": {
            "schema_version": PREFLIGHT_SCHEMA_VERSION,
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
            "single_leg_per_episode": stats["single_leg_per_episode"],
            "episodes_with_a_leg_by_strategy_and_slippage": stats[
                "episodes_with_a_leg_by_strategy_and_slippage"
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
        "year_end_open_positions": {
            "open_position_count": open_positions["open_position_count"],
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

    report = render_report_zh(
        manifest=manifest,
        summary=summary,
        funnel=funnel,
        groups=groups,
        top_movers=top_movers,
        exit_reasons=exit_reasons,
        open_positions=open_positions,
        rejections=rejections,
        spot_checks=spot_checks,
        preflight=preflight,
    )
    payloads["report_zh.md"] = report.encode("utf-8")
    for name, blob in payloads.items():
        (run_dir / name).write_bytes(blob)
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
        "group_attribution": groups,
        "exit_reason_breakdown": exit_reasons,
        "year_end_open_positions": manifest["year_end_open_positions"],
        "semantic_labels": dict(SEMANTIC_LABELS),
        "rejections": rejections,
        "spot_checks": spot_checks,
        "preflight": preflight,
        "source_transparency": {
            "price_basis": pilot.C1_DECLARED_PRICE_BASIS,
            "unadjusted_vendor_close_proxy": True,
            "engine_instrument_parameter": pilot.ENGINE_INSTRUMENT_PARAMETER.value,
            "instrument_is_a_current_registration_proxy": True,
            "dated_2022_instrument_identity_evidence": False,
            "tdcc_partial_exploration_sample": True,
            "programs_success_per_day": 6,
            "programs_unavailable_per_day": 1,
            "programs_failed_per_day": 1,
            "not_formal_historical_tradable_performance": True,
        },
        "artifact_sha256": declared,
        "artifact_sha256_recomputed": verified,
        "artifacts_verified": declared == verified,
        "source_sha256": {
            "h2_variant_contract": (
                "A68B1DC77D2C40156088365635F34A07280055C9E338F4F2E715AF8DB7BE8AA9"
            ),
            "frozen_spec": (
                "5A9E3A573178AA135FFBD960871D1ACEE20049F9ACF2DD7C117E1E2390B53DDA"
            ),
            "v050_preregistered_spec": (
                "AF6E2781EC81E8A46A42DC87E147F8CED02FC0FB2B91E35B25BA45CF49F85DAC"
            ),
            "pivot_pattern": (
                "docs/strategies/dual_ma/patterns/pivot-v-confirm-v1.md"
            ),
            "lifecycle_contract": (
                "361D5F35456FCA9B8BF00412CACBA4E2EF4173175ECDBB813D7600A977EB755E"
            ),
            "single_entry_no_add_contract": (
                "937D6585FD3727AC227542FEDCBB5266D2DBE98BE7CBBAB375469CFFA9BF4405"
            ),
            "price_snapshot_content": snapshot.content_sha256,
            "official_calendar_raw": calendar.raw_sha256,
        },
        "checkpoint": {
            "kind": "ATOMIC_H2_SINGLE_ENTRY_V_PIVOT",
            "state": "COMPLETE_AWAITING_00_QA",
            "resumable": False,
            "next_action": (
                "00 reviews the research_only H2 single-entry V-pivot variant; "
                "no 2023/2024 performance is read and nothing is deployed"
            ),
        },
    }
    (run_dir / "readback.json").write_bytes(pilot._json_bytes(readback))
    return manifest


def rerender_report_from_run(run_dir: str | Path) -> dict[str, Any]:
    """Rebuild ONLY the report text from a finished run's saved artifacts.

    A pure wording or table-layout change must never force a full H2 replay.
    This path reads the stored JSON artifacts and rewrites report_zh.md plus the
    report hash recorded in readback.json.  It never re-runs the engine, never
    rewrites trades.csv or any engine artifact, and never touches a frozen input.
    """

    run_dir = Path(run_dir)
    manifest = pilot._read_json(run_dir / "manifest.json")

    def _load(name: str) -> Any:
        return pilot._read_json(run_dir / name)

    report = render_report_zh(
        manifest=manifest,
        summary=manifest["stats"]["rows"],
        funnel=_load("funnel.json"),
        groups=_load("group_attribution.json"),
        top_movers=_load("top_movers.json"),
        exit_reasons=_load("exit_reason_breakdown.json"),
        open_positions=_load("year_end_open_positions.json"),
        rejections=_load("rejections.json"),
        spot_checks=_load("spot_checks.json"),
        preflight=_load("preflight.json"),
    )
    payload = report.encode("utf-8")
    (run_dir / "report_zh.md").write_bytes(payload)
    readback = _load("readback.json")
    declared = dict(readback["artifact_sha256"])
    declared["report_zh.md"] = pilot.sha256_bytes(payload)
    declared["manifest.json"] = pilot.sha256_file(run_dir / "manifest.json")
    verified = {name: pilot.sha256_file(run_dir / name) for name in declared}
    readback["artifact_sha256"] = declared
    readback["artifact_sha256_recomputed"] = verified
    readback["artifacts_verified"] = declared == verified
    history = list(readback.get("rerender_history") or ())
    history.append(
        {
            "kind": "REPORT_TEXT_RERENDER",
            "engine_rerun": False,
            "report_zh_sha256": declared["report_zh.md"],
        }
    )
    readback["rerender_history"] = history
    (run_dir / "readback.json").write_bytes(pilot._json_bytes(readback))
    return manifest


def run_h2_preflight_only(
    *,
    workdata_root: str | Path,
    output_root: str | Path | None = None,
    run_id: str = RUN_ID_DEFAULT,
    progress: Callable[[str, int, int, Mapping[str, Any]], None] | None = None,
) -> dict[str, Any]:
    workdata_root = Path(workdata_root)
    out_root = (
        Path(output_root)
        if output_root is not None
        else workdata_root / "02-causal-backtest"
    )
    preflight_dir = out_root / (run_id + "-preflight")
    if preflight_dir.exists():
        raise H2VPivotError(
            f"refusing to overwrite an existing preflight directory: {preflight_dir}"
        )
    inputs = load_h2_inputs(workdata_root, progress=progress)
    preflight = build_h2_preflight(inputs)
    preflight_dir.mkdir(parents=True, exist_ok=True)
    (preflight_dir / "preflight.json").write_bytes(pilot._json_bytes(preflight))
    (preflight_dir / "parent_runs.json").write_bytes(
        pilot._json_bytes(inputs["parent"])
    )
    return preflight


def _progress_printer(
    stage: str, done: int, total: int, extra: Mapping[str, Any]
) -> None:
    print(f"[{stage}] {done}/{total} {dict(extra)}", file=sys.stderr, flush=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdata-root", type=Path, default=None)
    parser.add_argument("--output-root", type=Path, default=None)
    parser.add_argument("--run-id", default=RUN_ID_DEFAULT)
    parser.add_argument("--episode-limit", type=int, default=None)
    parser.add_argument("--runner-commit", default=None)
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument(
        "--rerender-report",
        type=Path,
        default=None,
        help="rebuild report_zh.md from an existing run directory (no engine rerun)",
    )
    args = parser.parse_args(argv)
    if args.rerender_report is not None:
        manifest = rerender_report_from_run(args.rerender_report)
        print("rerendered report for {r}".format(r=manifest["run_id"]))
        return 0
    if args.preflight_only:
        if args.workdata_root is None:
            parser.error("--workdata-root is required for --preflight-only")
        result = run_h2_preflight_only(
            workdata_root=args.workdata_root,
            output_root=args.output_root,
            run_id=args.run_id,
            progress=_progress_printer,
        )
        print(
            "preflight: sessions={s} dates_match={m} parents_matched={p} "
            "codes_without_price={c}".format(
                s=result["official_h2_session_count"],
                m=result["dates_are_exactly_official_h2_sessions"],
                p=result["parents_all_matched"],
                c=len(result["codes_without_price_series"]),
            )
        )
        return 0
    if args.workdata_root is None:
        parser.error("--workdata-root is required for a full run")
    manifest = run_h2_single_entry_v_pivot(
        workdata_root=args.workdata_root,
        output_root=args.output_root,
        run_id=args.run_id,
        episode_limit=args.episode_limit,
        runner_commit=args.runner_commit,
        progress=_progress_printer,
    )
    print(
        "run {r} status={s} episodes={e} leg_rows={l}".format(
            r=manifest["run_id"],
            s=manifest["status"],
            e=manifest["episode_count_run"],
            l=manifest["engine"]["leg_rows"],
        )
    )
    return 0 if manifest["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
