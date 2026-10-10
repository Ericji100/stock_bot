"""2022 H1 入選樣本：MA144 上行 + 樞紐風險寬度 7% 跨期間檢查 (02 role).

Authority (read-only):

* docs/strategies/dual_ma/2022-h1-ma144-risk-width-validation-v1.md
  raw SHA-256 D40A48911C26856ECFD855775E8B22656C81DB25AF677A4AE839E4F70806DD1D
  (3444 bytes) -- this round's validation spec
* docs/strategies/dual_ma/2022-h2-ma144-risk-width-entry-exploration-v1.md
  raw SHA-256 ED806601D540BF7EDD33BFF8E60796A0B7198691B0823AD3FAC6511D2F066818
  (3676 bytes) -- the frozen rule source, reused byte-for-byte

The rule is copied verbatim from the accepted H2 risk-width run: the frozen
flat-re-entry state machine, the MA144 upward entry gate and, at every complete
buy signal T that already passed MA144, the confirmed-pivot risk-width floor

    risk_width = (T close entry-proxy price - confirmed V pivot low) / T close
    risk_width >= 0.07

Nothing is re-tuned: the 7% threshold, the MA144 20-session back reference, the
entry/exit variants, the CATHAY_WEB_028_TW_V1 cost model and the 0/10/20 bps
slippage ladder are unchanged.  The only difference from the H2 run is the
sample: the nine accepted 2022 H1 selection reports (2022-01-03 .. 2022-06-30,
117 official sessions).  No 2022 H2 selection event is added, and the replay
stops at the frozen 2022-12-30 price snapshot so an H1 episode or leg may run
into H2 causally; anything still open on 2022-12-30 is listed separately and is
never mixed into the realised net P&L.

research_only=true, point_in_time_verified=false.  The researcher has already
seen other H1 rule results, so this is not an independent blind test, and no
2023 / 2024 performance is read.
"""

from __future__ import annotations

import argparse
import statistics
import sys
from collections import Counter
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import h1_full_exploration_2022h1 as h1x
from . import h1_real_pilot_2022_0419_0428 as pilot
from . import h2_flat_reentry_2022h2 as flat
from . import h2_flat_reentry_attribution_2022h2 as attribution
from . import h2_ma144_risk_width_entry_2022h2 as rw
from . import h2_ma144_upward_entry_2022h2 as ma144
from . import h2_single_entry_v_pivot_2022h2 as h2
from .contracts import (
    COST_MODEL,
    EXECUTION_MODEL,
    SPEC_SHA256,
    STRATEGY_VERSION,
    RunManifest,
)
from .models import StrategyKind
from .single_entry_no_add import ENGINE_MATRIX_VEHICLE, STATUS_FILLED


MODULE_VERSION = "dual-ma-h1-ma144-risk-width-cross-period-2022h1-v1"
RUN_ID_DEFAULT = "h1-ma144-risk-width-cross-period-2022h1-v1"
MANIFEST_SCHEMA_VERSION = "dual-ma-h1-ma144-risk-width-cross-period-manifest-v1"
READBACK_SCHEMA_VERSION = "dual-ma-h1-ma144-risk-width-cross-period-readback-v1"
PREFLIGHT_SCHEMA_VERSION = "dual-ma-h1-ma144-risk-width-cross-period-preflight-v1"

VARIANT_ID = (
    "H1_SAMPLE_FLAT_REENTRY_AFTER_EXIT_WITHIN_EPISODE"
    "+MA144_UPWARD_ENTRY_GATE+RISK_WIDTH_GE_7PCT_ENTRY_GATE"
)

# The one frozen rule source, reused byte-for-byte from the accepted H2 run.
SPEC_RELATIVE_PATH_H2_RULE = (
    "docs/strategies/dual_ma/2022-h2-ma144-risk-width-entry-exploration-v1.md"
)
SPEC_SHA256_H2_RULE = "ED806601D540BF7EDD33BFF8E60796A0B7198691B0823AD3FAC6511D2F066818"
SPEC_BYTE_SIZE_H2_RULE = 3676

SPEC_RELATIVE_PATH_H1_VALIDATION = (
    "docs/strategies/dual_ma/2022-h1-ma144-risk-width-validation-v1.md"
)
SPEC_SHA256_H1_VALIDATION = (
    "D40A48911C26856ECFD855775E8B22656C81DB25AF677A4AE839E4F70806DD1D"
)
SPEC_BYTE_SIZE_H1_VALIDATION = 3444

RISK_WIDTH_THRESHOLD = rw.RISK_WIDTH_THRESHOLD
RISK_WIDTH_RULE = rw.RISK_WIDTH_RULE
ENTRY_VARIANT = rw.ENTRY_VARIANT

H1_FIRST_DAY = date(2022, 1, 1)
H1_LAST_DAY = date(2022, 6, 30)
AS_OF_CUTOFF = date(2022, 12, 30)
EXPECTED_H1_SESSION_COUNT = 117
LATEST_READABLE_YEAR = 2022

SLIPPAGE_SCENARIOS = rw.SLIPPAGE_SCENARIOS
PRIMARY_SLIPPAGE_BPS = rw.PRIMARY_SLIPPAGE_BPS
STRATEGY_ORDER = rw.STRATEGY_ORDER
DESCRIPTIVE_BOTH = rw.DESCRIPTIVE_BOTH

# Read-only byte-unchanged checks for the accepted runs this check depends on.
BASELINE_H1_RUN_ID = h1x.RUN_ID_DEFAULT
BASELINE_H1_MANIFEST_SHA256 = (
    "8864338BD19AF3C1D0D83948BA4B46FC32A5F2CF6F48C33E9BA7EB8B671A6115"
)
BASELINE_H1_ENGINE_EVENTS_SHA256 = (
    "0ADA6C2825DB879C077F6D2572270F124E5C3B148148AC9CCE57B572877114F2"
)
H1_SINGLE_ENTRY_RUN_ID = "h1-single-entry-no-add-2022h1-v1"
H1_SINGLE_ENTRY_MANIFEST_SHA256 = (
    "F6F047491B2CF9C0B24B99086A472F5D1CD1FCF54392F60CAA79527B63B62EDB"
)
H2_RULE_RUN_ID = "h2-ma144-risk-width-entry-2022h2-v2-report-errata"
H2_RULE_RUN_MANIFEST_SHA256 = (
    "0B247782A52F193AB71EF52686641D041D8B372E52A62852007771881DD563B8"
)
H2_RULE_RUN_TRADES_SHA256 = (
    "9C17D704ED7FBBDF5BBAE98BD8A59CF2B5DF55A411A1EFCDAB05C992E99EDC64"
)

MAX_REPORT_GATE_ROWS = 60
MAX_REPORT_BLOCKED_ROWS = 20


class H1RiskWidthError(RuntimeError):
    """A structural problem with the H1 cross-period check or one of its inputs."""


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


def load_h1_risk_width_inputs(
    workdata_root: str | Path,
    *,
    progress: Callable[[str, int, int, Mapping[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Load the nine accepted H1 selection runs plus the frozen shared sources."""

    workdata_root = Path(workdata_root)
    pilot_root = workdata_root / pilot.PILOT_ROOT_RELATIVE
    parent = h1x.verify_parent_runs(pilot_root)
    if not parent["all_matched"]:
        raise H1RiskWidthError("one or more accepted H1 01 reports do not match")
    run_by_date = h1x.build_run_by_date(pilot_root)
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
    code_programs_by_date = {
        report.report_date: report.code_programs for report in reports
    }
    universe_sources_path = workdata_root / h1x.UNIVERSE_SOURCES_RELATIVE_PATH
    if not universe_sources_path.is_file():
        raise H1RiskWidthError(
            f"frozen universe sources are missing: {universe_sources_path}"
        )
    universe_sha = pilot.sha256_file(universe_sources_path)
    if universe_sha != h1x.UNIVERSE_SOURCES_RAW_SHA256:
        raise H1RiskWidthError(
            f"universe sources sha256 {universe_sha} != expected "
            f"{h1x.UNIVERSE_SOURCES_RAW_SHA256}"
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


def build_h1_preflight(inputs: Mapping[str, Any]) -> dict[str, Any]:
    """Light H1 calendar / report SHA / price-coverage preflight."""

    calendar = inputs["calendar"]
    snapshot = inputs["snapshot"]
    reports = inputs["reports"]
    resolution = inputs["resolution"]
    official_sessions = [
        day.isoformat() for day in calendar.sessions_between(H1_FIRST_DAY, H1_LAST_DAY)
    ]
    present = sorted(report.report_date for report in reports)
    status_counts: Counter[str] = Counter()
    per_day: list[dict[str, Any]] = []
    for report in reports:
        day_counts = Counter(report.program_status.values())
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
    return {
        "schema_version": PREFLIGHT_SCHEMA_VERSION,
        "run_id": RUN_ID_DEFAULT,
        "module_version": MODULE_VERSION,
        "h1_first_day": H1_FIRST_DAY.isoformat(),
        "h1_last_day": H1_LAST_DAY.isoformat(),
        "official_h1_session_count": len(official_sessions),
        "expected_h1_session_count": EXPECTED_H1_SESSION_COUNT,
        "reported_date_count": len(present),
        "dates_are_exactly_official_h1_sessions": present == official_sessions,
        "missing_official_sessions": [
            day for day in official_sessions if day not in set(present)
        ],
        "unexpected_dates": [
            day for day in present if day not in set(official_sessions)
        ],
        "program_status_counts": dict(sorted(status_counts.items())),
        "parents_all_matched": inputs["parent"]["all_matched"],
        "parent_runs": inputs["parent"],
        "selected_code_count": len(selected_codes),
        "resolved_symbol_count": len(symbols),
        "codes_without_symbol_identity": sorted(
            set(resolution.unknown_codes) | set(resolution.conflicting_codes)
        ),
        "codes_without_price_series": codes_without_price_series,
        "price_snapshot_content_sha256": snapshot.content_sha256,
        "price_snapshot_last_session": snapshot.last_session.isoformat(),
        "price_snapshot_never_reaches_2023": (
            snapshot.last_session.year <= LATEST_READABLE_YEAR
        ),
        "universe_sources": {
            "path": str(inputs["universe_sources_path"]),
            "raw_sha256": inputs["universe_sha"],
            "source_id": h1x.UNIVERSE_SOURCES_SOURCE_ID,
        },
        "dates": per_day,
    }


# ---------------------------------------------------------------------------
# Engine wiring (identical rule, new sample)
# ---------------------------------------------------------------------------


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
            "entry_gate": ma144.GATE_RULE,
            "entry_gate_back_official_sessions": ma144.GATE_BACK_SESSIONS,
            "entry_gate_sma_window": ma144.GATE_SMA_WINDOW,
            "risk_width_gate": RISK_WIDTH_RULE,
            "risk_width_threshold": str(RISK_WIDTH_THRESHOLD),
        },
        metadata={
            "source_code": code,
            "variant_id": VARIANT_ID,
            "engine_matrix_vehicle": ENGINE_MATRIX_VEHICLE,
            "lifecycle_contract": (
                "docs/strategies/dual_ma/2022-h1-selection-episode-lifecycle-v1.md"
            ),
            "variant_contract": SPEC_RELATIVE_PATH_H1_VALIDATION,
            "rule_source_contract": SPEC_RELATIVE_PATH_H2_RULE,
        },
    )


def run_variant_episode(
    *,
    episode: Any,
    bars: Sequence[Any],
    gate_map: Mapping[date, ma144.GateEvaluation],
    official_sessions: Sequence[date],
    slippage_bps: int,
    run_id: str,
    runner_commit: str,
    snapshot_id: str,
    data_hash: str,
    instrument_parameter: Any = pilot.ENGINE_INSTRUMENT_PARAMETER,
) -> tuple[list[Any], dict[str, int], dict[str, str], Any]:
    """Replay one activated H1 episode with the unchanged H2 risk-width engine."""

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
    result = rw.RiskWidthEntryEngine(
        slippage_bps=slippage_bps,
        entry_variant=ENTRY_VARIANT,
        gate_map=gate_map,
        official_sessions=official_sessions,
    ).run(
        episode.symbol,
        run_bars,
        run_manifest=manifest,
        instrument_type=instrument_parameter,
        backtest_start=window.regime_window_start,
        backtest_end=window.regime_window_end,
    )
    legs = [leg for leg in result.legs if leg.strategy.value == episode.strategy]
    tally = flat.tally_entry_decisions(result.decisions, strategy=episode.strategy)
    pullbacks = flat.pullback_event_by_leg(result.transactions, strategy=episode.strategy)
    return legs, tally, pullbacks, result


def run_ma144_baseline_rows(
    *,
    episode: Any,
    bars: Sequence[Any],
    gate_map: Mapping[date, ma144.GateEvaluation],
    official_sessions: Sequence[date],
    code_programs_by_date: Mapping[str, Mapping[str, Sequence[str]]],
    run_id: str,
    runner_commit: str,
    snapshot: Any,
    instrument_parameter: Any = pilot.ENGINE_INSTRUMENT_PARAMETER,
) -> list[dict[str, Any]]:
    """0 bps MA144-only leg rows for one episode (gate-blocked evidence only)."""

    window = episode.window
    run_bars = [bar for bar in bars if bar.date <= window.regime_window_end]
    manifest = _run_manifest(
        code=episode.code,
        bars=run_bars,
        slippage_bps=PRIMARY_SLIPPAGE_BPS,
        run_id=run_id,
        runner_commit=runner_commit,
        snapshot_id=snapshot.snapshot_id,
        data_hash=snapshot.content_sha256,
    )
    result = ma144.Ma144UpwardEntryEngine(
        slippage_bps=PRIMARY_SLIPPAGE_BPS,
        entry_variant=ENTRY_VARIANT,
        gate_map=gate_map,
        official_sessions=official_sessions,
    ).run(
        episode.symbol,
        run_bars,
        run_manifest=manifest,
        instrument_type=instrument_parameter,
        backtest_start=window.regime_window_start,
        backtest_end=window.regime_window_end,
    )
    legs = [leg for leg in result.legs if leg.strategy.value == episode.strategy]
    ordered = sorted(legs, key=lambda item: (item.entry_date, item.leg_id))
    pullbacks = flat.pullback_event_by_leg(result.transactions, strategy=episode.strategy)
    groups_today = tuple(
        code_programs_by_date.get(window.source_selection_date.isoformat(), {}).get(
            episode.code, ()
        )
    )
    out: list[dict[str, Any]] = []
    for ordinal, leg in enumerate(ordered, start=1):
        prior_leg = ordered[ordinal - 2] if ordinal > 1 else None
        out.append(
            ma144._leg_row(
                leg=leg,
                episode=episode,
                slippage_bps=PRIMARY_SLIPPAGE_BPS,
                run_id=run_id,
                pullback_event_id=pullbacks.get(leg.leg_id),
                entry_evidence=h2._leg_entry_evidence(result, leg),
                groups=groups_today,
                reentry_ordinal=ordinal,
                episode_window_leg_count=len(ordered),
                prior_leg=prior_leg,
            )
        )
    return out


def collect_trade_rows(
    *,
    plan: Any,
    snapshot: Any,
    code_programs_by_date: Mapping[str, Mapping[str, Sequence[str]]],
    official_sessions: Sequence[date],
    run_id: str,
    runner_commit: str,
    progress: Callable[[str, int, int, Mapping[str, Any]], None] | None = None,
    episode_limit: int | None = None,
    ma144_baseline: bool = True,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Replay every activated H1 episode at 0/10/20 bps behind both entry gates."""

    episodes = list(plan.episodes)
    truncated = False
    if episode_limit is not None and episode_limit < len(episodes):
        episodes = episodes[: int(episode_limit)]
        truncated = True

    rows: list[dict[str, Any]] = []
    gate_rows: list[dict[str, Any]] = []
    baseline_rows: list[dict[str, Any]] = []
    decision_totals: Counter[str] = Counter()
    decision_first_example: dict[str, dict[str, Any]] = {}
    episodes_with_leg: Counter[str] = Counter()
    reentry_episodes: Counter[str] = Counter()
    gate_totals: Counter[str] = Counter()
    max_open_legs = 0
    filled_add_events = 0
    baseline_episode_count = 0
    primary_block_episode_count = 0
    gate_cache: dict[str, dict[date, ma144.GateEvaluation]] = {}

    for index, episode in enumerate(episodes, start=1):
        bars = snapshot.bars_by_symbol[episode.symbol]
        gate_map = gate_cache.get(episode.symbol)
        if gate_map is None:
            gate_map = ma144.build_ma144_gate_map(
                dates=[bar.date for bar in bars],
                closes=[bar.close for bar in bars],
                official_sessions=official_sessions,
            )
            gate_cache[episode.symbol] = gate_map
        groups_today = tuple(
            code_programs_by_date.get(
                episode.window.source_selection_date.isoformat(), {}
            ).get(episode.code, ())
        )
        episode_had_block = False
        for slippage_bps in SLIPPAGE_SCENARIOS:
            legs, tally, pullbacks, result = run_variant_episode(
                episode=episode,
                bars=bars,
                gate_map=gate_map,
                official_sessions=official_sessions,
                slippage_bps=slippage_bps,
                run_id=run_id,
                runner_commit=runner_commit,
                snapshot_id=snapshot.snapshot_id,
                data_hash=snapshot.content_sha256,
            )
            ordered_legs = sorted(legs, key=lambda item: (item.entry_date, item.leg_id))
            leg_by_id = {leg.leg_id: leg for leg in ordered_legs}
            for decision in result.decisions:
                # An episode is (code, strategy); the engine returns the same
                # symbol's other strategy too.  Diagnostics must stay on the
                # episode's own strategy, exactly like the leg filter above.
                if str(decision.get("strategy")) != episode.strategy:
                    continue
                for key in ("before_actions", "after_actions"):
                    snap = decision.get(key) or {}
                    max_open_legs = max(
                        max_open_legs, int(snap.get("open_leg_count") or 0)
                    )
                for event in decision.get("events") or ():
                    if not isinstance(event, Mapping):
                        continue
                    if event.get("type") != "ENTRY_DECISION":
                        continue
                    status = event.get("status")
                    if status == STATUS_FILLED and event.get("role") == "ADD":
                        filled_add_events += 1
                    if status in rw.GATE_BLOCK_STATUSES:
                        gate_totals[f"{episode.strategy}|{slippage_bps}|{status}"] += 1
                        if slippage_bps == PRIMARY_SLIPPAGE_BPS:
                            episode_had_block = True
                            prior_id = event.get("prior_leg_id")
                            prior_leg = leg_by_id.get(str(prior_id)) if prior_id else None
                            gate_rows.append(
                                rw._gate_decision_row(
                                    event=event,
                                    episode=episode,
                                    run_id=run_id,
                                    slippage_bps=slippage_bps,
                                    prior_leg=prior_leg,
                                )
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
            if legs:
                episodes_with_leg[f"{episode.strategy}|{slippage_bps}"] += 1
            if len(legs) > 1:
                reentry_episodes[f"{episode.strategy}|{slippage_bps}"] += 1
            for ordinal, leg in enumerate(ordered_legs, start=1):
                prior_leg = ordered_legs[ordinal - 2] if ordinal > 1 else None
                rows.append(
                    rw._leg_row(
                        leg=leg,
                        episode=episode,
                        slippage_bps=slippage_bps,
                        run_id=run_id,
                        pullback_event_id=pullbacks.get(leg.leg_id),
                        entry_evidence=h2._leg_entry_evidence(result, leg),
                        groups=groups_today,
                        reentry_ordinal=ordinal,
                        episode_window_leg_count=len(legs),
                        prior_leg=prior_leg,
                    )
                )
        if ma144_baseline:
            baseline_rows.extend(
                run_ma144_baseline_rows(
                    episode=episode,
                    bars=bars,
                    gate_map=gate_map,
                    official_sessions=official_sessions,
                    code_programs_by_date=code_programs_by_date,
                    run_id=run_id,
                    runner_commit=runner_commit,
                    snapshot=snapshot,
                )
            )
            baseline_episode_count += 1
        if episode_had_block:
            primary_block_episode_count += 1
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
        "gate_decision_totals": dict(sorted(gate_totals.items())),
        "gate_decision_primary_rows": sum(
            1 for row in gate_rows if int(row["slippage_bps"]) == PRIMARY_SLIPPAGE_BPS
        ),
        "ma144_baseline_episode_count": baseline_episode_count,
        "ma144_baseline_leg_row_count": len(baseline_rows),
        "episodes_with_a_primary_gate_block": primary_block_episode_count,
    }
    return rows, gate_rows, baseline_rows, stats


# ---------------------------------------------------------------------------
# Small report helpers
# ---------------------------------------------------------------------------


def _money(value: Any) -> str:
    if value is None or value == "":
        return ""
    return f"{Decimal(str(value)):.4f}"


def _int_or_blank(value: Any) -> str:
    return "" if value is None else str(int(value))


def _pct_or_blank(value: Any) -> str:
    if value is None or value == "":
        return ""
    return f"{Decimal(str(value)) * 100:.2f}%"


def _summary_table_lines(summary: Sequence[Mapping[str, Any]]) -> list[str]:
    lines = [
        "| 記帳單位 | 滑價(bps) | 已平倉筆數 | 未平倉筆數 | 勝 | 敗 | 平 | 勝率 | 平均每筆費後損益 | 中位數 | 費後損益總額 | 毛獲利 | 毛虧損 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in summary:
        lines.append(
            "| {0} | {1} | {2} | {3} | {4} | {5} | {6} | {7} | {8} | {9} | {10} | {11} | {12} |".format(
                row["strategy"],
                row["slippage_bps"],
                row["closed_leg_count"],
                row["open_leg_count"],
                row["win_count"],
                row["loss_count"],
                row["flat_count"],
                _pct_or_blank(row["win_rate"]),
                _money(row["mean_costed_net_pnl"]),
                _money(row["median_costed_net_pnl"]),
                _money(row["costed_net_pnl_total"]),
                _money(row["gross_profit"]),
                _money(row["gross_loss"]),
            )
        )
    lines.append("")
    return lines


def _reentry_section_lines(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    lines: list[str] = []
    primary = [
        row for row in rows if int(row["slippage_bps"]) == PRIMARY_SLIPPAGE_BPS
    ]
    for strategy in STRATEGY_ORDER:
        subset = [row for row in primary if row["strategy"] == strategy]
        reentry = [
            row for row in subset if str(row["is_reentry"]) in ("True", "true")
        ]
        closed = [row for row in reentry if not row["open_censored"]]
        net = sum(
            (Decimal(str(row["costed_net_pnl"])) for row in closed), Decimal(0)
        )
        lines.append(
            "- {0}：0 bps 再進場 leg {1} 筆（已平倉 {2}、未平倉 {3}），再進場費後損益合計 {4}。".format(
                attribution.STRATEGY_LABEL_ZH.get(strategy, strategy),
                len(reentry),
                len(closed),
                len(reentry) - len(closed),
                f"{net:.4f}",
            )
        )
    episodes = Counter(
        (row["strategy"], row["episode_key"]) for row in primary
    )
    multi = {key: count for key, count in episodes.items() if count > 1}
    lines.append("")
    lines.append(
        "0 bps 出現再進場的 episode 數：{0}（其中中期 {1}、長期 {2}）。".format(
            len(multi),
            sum(1 for (strategy, _), _count in multi.items() if strategy == StrategyKind.MEDIUM.value),
            sum(1 for (strategy, _), _count in multi.items() if strategy == StrategyKind.LONG.value),
        )
    )
    lines.append("")
    return lines


def _exit_reason_lines(exit_reasons: Mapping[str, Any]) -> list[str]:
    lines = [
        "| 策略 | 出場原因 | 筆數 | 費後損益合計 |",
        "| --- | --- | --- | --- |",
    ]
    by_strategy = (
        exit_reasons.get("by_strategy", {}) if isinstance(exit_reasons, Mapping) else {}
    )
    for strategy, items in sorted(by_strategy.items()):
        for payload in items:
            if not isinstance(payload, Mapping):
                continue
            lines.append(
                "| {0} | {1} | {2} | {3} |".format(
                    strategy,
                    payload.get("exit_reason"),
                    _int_or_blank(payload.get("count")),
                    _money(payload.get("costed_net_pnl_total")),
                )
            )
    lines.append("")
    return lines


def _group_table_lines(groups: Mapping[str, Any]) -> list[str]:
    lines = [
        "| 選股組 | 0 bps leg 數 | 已平倉 | 未平倉 | 勝 | 勝率 | 費後損益總額 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in groups.get("available_group_rows", ()):
        lines.append(
            "| {0} | {1} | {2} | {3} | {4} | {5} | {6} |".format(
                row.get("group_label_zh"),
                _int_or_blank(row.get("leg_count_primary_bps")),
                _int_or_blank(row.get("closed_leg_count")),
                _int_or_blank(row.get("open_leg_count")),
                _int_or_blank(row.get("win_count")),
                _pct_or_blank(row.get("win_rate")),
                _money(row.get("costed_net_pnl_total")),
            )
        )
    for row in groups.get("unavailable_group_rows", ()):
        lines.append(
            f"| {row.get('group_label_zh')} | 不適用（{row.get('status')}） |  |  |  |  |  |"
        )
    lines.append("")
    lines.append(str(groups.get("multi_label_note") or ""))
    lines.append("")
    return lines


def _movers_lines(top_movers: Mapping[str, Any]) -> list[str]:
    lines: list[str] = []
    for title, key in (("大贏家（0 bps，前 10）", "winners"), ("大輸家（0 bps，前 10）", "losers")):
        lines.append(f"### {title}")
        lines.append("")
        lines.append(
            "| 代號 | 策略 | D | T | 出場日 | 出場原因 | 股數 | 費後損益 | 報酬率 |"
        )
        lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        for row in top_movers.get(key, ()):
            net_return = row.get("net_return_pct")
            lines.append(
                "| {0} | {1} | {2} | {3} | {4} | {5} | {6} | {7} | {8} |".format(
                    row.get("code"),
                    row.get("strategy"),
                    row.get("source_selection_date_D"),
                    row.get("entry_signal_date_T"),
                    row.get("exit_date"),
                    row.get("exit_reason"),
                    row.get("shares"),
                    _money(row.get("costed_net_pnl")),
                    "" if net_return is None else net_return,
                )
            )
        lines.append("")
    return lines


def _open_position_lines(open_positions: Mapping[str, Any]) -> list[str]:
    lines = [
        "- 未平倉筆數：{0}（中期 {1}、長期 {2}）".format(
            open_positions.get("open_position_count"),
            open_positions.get("medium_open_count"),
            open_positions.get("long_open_count"),
        ),
        "- 未實現評價（以 2022-12-30 收盤、凍結費稅試算，僅供研究）：中期 {0}、長期 {1}".format(
            _money(open_positions.get("medium_mark_to_market_net_pnl")),
            _money(open_positions.get("long_mark_to_market_net_pnl")),
        ),
        "",
        "| 代號 | 策略 | D | E | T | 股數 | 進場價/停損 | 期末收盤 | 買進成本 | 期末評價損益 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in open_positions.get("positions", ()):
        lines.append(
            "| {0} | {1} | {2} | {3} | {4} | {5} | {6} / {7} | {8} | {9} | {10} |".format(
                row.get("code"),
                row.get("strategy"),
                row.get("source_selection_date_D"),
                row.get("monitor_effective_date_E"),
                row.get("entry_signal_date_T"),
                row.get("shares"),
                row.get("initial_stop"),
                "",
                row.get("cutoff_close"),
                row.get("buy_total"),
                _money(row.get("mark_to_market_net_pnl")),
            )
        )
    lines.append("")
    return lines


def _corpaction_lines(corp_summary: Mapping[str, Any]) -> list[str]:
    lines = [
        "- 0 bps leg 數：{0}".format(corp_summary.get("zero_bps_leg_count")),
        "- 受公司行動影響：{0}；已解：{1}；未解：{2}；無法重製：{3}".format(
            corp_summary.get("affected_leg_count"),
            corp_summary.get("resolved_leg_count"),
            corp_summary.get("unresolved_leg_count"),
            corp_summary.get("not_reproduced_leg_count"),
        ),
        "- 狀態分佈：{0}".format(corp_summary.get("status_counts")),
        "- 未解原因分佈：{0}".format(corp_summary.get("unresolved_reason_counts")),
        "- 未解 leg 的原始（未修正）基準彙總：{0}".format(
            corp_summary.get("raw_baseline_of_unresolved_legs")
        ),
        "",
    ]
    for title, key in (
        ("已解公司行動的修正後現金流", "corrected_flows"),
        ("未解 leg 清單", "unresolved_legs"),
    ):
        payload = corp_summary.get(key)
        if not payload:
            continue
        lines.append(f"#### {title}")
        lines.append("")
        lines.append("```json")
        lines.append(pilot._json_bytes(payload).decode("utf-8"))
        lines.append("```")
        lines.append("")
    return lines


def render_report_zh(
    *,
    manifest: Mapping[str, Any],
    summary: Sequence[Mapping[str, Any]],
    metrics_tables: Sequence[str],
    analysis: Mapping[str, Any],
    groups: Mapping[str, Any],
    open_positions: Mapping[str, Any],
    corp_summary: Mapping[str, Any],
    detail_rows: Sequence[Mapping[str, Any]],
    gate_rows: Sequence[Mapping[str, Any]],
    spot_checks: Mapping[str, Any],
    preflight: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    exit_reasons: Mapping[str, Any],
    top_movers: Mapping[str, Any],
    git_state: Mapping[str, Any],
) -> str:
    lines: list[str] = []
    add = lines.append
    add("# 2022 H1 入選樣本：MA144 上行＋樞紐風險寬度 7% 跨期間檢查 v1")
    add("")
    add(
        "狀態：`research_only=true`、`point_in_time_verified=false`。本輪只把 2022 H2 已跑的"
        "候選規則原封不動套到 2022 H1 已驗收選股日，未調參、未另加條件、未重算 H2。"
        "研究者先前已看過 H1 其他規則結果，因此**不是獨立盲測**，也不是正式可交易績效。"
    )
    add("")
    add(
        "- run id：`{0}`".format(manifest.get("run_id"))
    )
    add("- module：`{0}`".format(manifest.get("module_version")))
    add("- runner commit：`{0}`".format(manifest.get("runner_commit")))
    add("- variant：`{0}`".format(manifest.get("variant_id")))
    add("- 進場變體：`{0}`；MA144 門檻：`{1}`；風險寬度門檻：`{2}`".format(
        manifest.get("entry_variant"), manifest.get("entry_gate"), manifest.get("risk_width_threshold")
    ))
    add("- git 狀態：branch=`{0}`、HEAD=`{1}`；工作區未提交變更：`{2}`".format(
        git_state.get("branch"),
        git_state.get("head_commit"),
        (git_state.get("status_porcelain") or "（無）").replace("\n", "；"),
    ))
    add("- readback：`{0}`（{1}）".format(
        manifest.get("readback_status"), manifest.get("status")
    ))
    add("")

    add("## 一、固定輸入與規格綁定")
    add("")
    inputs = manifest.get("inputs", {})
    sources = manifest.get("source_sha256", {})
    add("- 官方日曆 content SHA-256：`{0}`".format(
        inputs.get("official_calendar", {}).get("content_sha256")
    ))
    add("- 價格快照 content SHA-256：`{0}`（最後 session {1}，標籤 `{2}`）".format(
        inputs.get("price_snapshot_content_sha256"),
        preflight.get("price_snapshot_last_session"),
        manifest.get("data_labels", {}).get("price_basis"),
    ))
    add("- H1 驗收規格 `{0}`：SHA-256 `{1}`（{2} bytes）".format(
        SPEC_RELATIVE_PATH_H1_VALIDATION,
        sources.get("h1_validation_contract_sha256"),
        sources.get("h1_validation_contract_bytes"),
    ))
    add("- H2 規則來源 `{0}`：SHA-256 `{1}`（{2} bytes，沿用未改）".format(
        SPEC_RELATIVE_PATH_H2_RULE,
        sources.get("h2_rule_contract_sha256"),
        sources.get("h2_rule_contract_bytes"),
    ))
    add("- universe sources：`{0}`".format(
        inputs.get("universe_sources", {}).get("raw_sha256")
    ))
    add("")
    add("### 選股來源（9 份已驗收 01 reports）")
    add("")
    add("| run dir | 日期數 | report SHA-256 |")
    add("| --- | --- | --- |")
    for row in inputs.get("parent_runs", {}).get("runs", ()):
        add("| {0} | {1} | `{2}` |".format(
            row.get("run_dir"), row.get("date_block_count"), row.get("report_sha256")
        ))
    add("")

    add("## 二、樣本覆蓋與因果邊界")
    add("")
    coverage = manifest.get("coverage", {})
    add("- H1 官方交易日：{0}（預期 {1}），已報告日：{2}".format(
        coverage.get("official_h1_session_count"),
        EXPECTED_H1_SESSION_COUNT,
        coverage.get("reported_date_count"),
    ))
    add("- 首日 {0}、末日 {1}；價格快照最後 session {2}，逐日重播截止 2022-12-30。".format(
        coverage.get("first_date"), coverage.get("last_date"),
        preflight.get("price_snapshot_last_session"),
    ))
    add("- H2 新選入事件：0（只用 2022-01-03..2022-06-30 的 117 個 D 日）。")
    add("- H1 episode/持倉可因果延續至 2022-12-30，12/30 尚未平倉者另列，不混入已平倉損益。")
    add("- 未讀取本專案 2023/24 績效；權息還原一律使用既有凍結來源，未新增還原價假設。")
    add("")

    add("## 三、兩策略分帳績效（0 / 10 / 20 bps）")
    add("")
    add("中期 5/21 與長期 21/144 各自獨立，不相加成同一帳戶；下列 BOTH 列僅為描述性合計。")
    add("")
    lines.extend(_summary_table_lines(summary))

    add("## 四、與 MA144 基準對照（0 bps，僅供門檻診斷）")
    add("")
    lines.extend(metrics_tables)

    add("## 五、進場門檻判定")
    add("")
    _ma144_states = analysis.get("ma144_state_counts", {})
    add(
        "- MA144 未上行：{0} 次；MA144 無法判定（HOLD）：{1} 次；"
        "風險寬度低於門檻：{2} 次；風險寬度無法判定（HOLD）：{3} 次".format(
            _ma144_states.get(rw.STATE_NOT_UP, 0),
            _ma144_states.get(rw.STATE_UNDETERMINED_MA144, 0),
            analysis.get("risk_width_state_counts", {}).get(rw.STATE_RW_BELOW, 0),
            analysis.get("risk_width_state_counts", {}).get(rw.STATE_RW_UNDETERMINED, 0),
        )
    )
    add("- 決定門檻分佈：{0}".format(analysis.get("deciding_gate_counts")))
    add("- 最終狀態分佈：{0}".format(analysis.get("status_counts")))
    add("- MA144 原因分佈：{0}".format(analysis.get("ma144_reason_counts")))
    add("- 風險寬度原因分佈：{0}".format(analysis.get("risk_width_reason_counts")))
    add("")
    add("### 各策略門檻判定計數")
    add("")
    add("| 策略 | 通過 leg | MA144 未上行 | MA144 HOLD | 風險寬度低於 | 風險寬度 HOLD | 被擋且可能是再進場 | 被擋首進 |")
    add("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for strategy in STRATEGY_ORDER:
        item = analysis.get("per_strategy", {}).get(strategy, {})
        add("| {0} | {1} | {2} | {3} | {4} | {5} | {6} | {7} |".format(
            getattr(strategy, "value", strategy),
            item.get("accepted_legs"), item.get("ma144_not_up"), item.get("ma144_hold"),
            item.get("risk_width_below"), item.get("risk_width_hold"),
            item.get("blocked_would_be_reentry"), item.get("blocked_first_entry"),
        ))
    add("")
    add("### 門檻判定明細（最多前 {0} 筆，完整清單見 gate_decisions.csv）".format(MAX_REPORT_GATE_ROWS))
    add("")
    lines.extend(rw._gate_table_lines(gate_rows[:MAX_REPORT_GATE_ROWS]))
    if len(gate_rows) > MAX_REPORT_GATE_ROWS:
        add("（其餘 {0} 筆見 `gate_decisions.csv`。）".format(len(gate_rows) - MAX_REPORT_GATE_ROWS))
        add("")
    add("### 被 7% 門檻擋掉、且在同規則 MA144 基準中存在對應 leg 的個案")
    add("")
    blocked = analysis.get("blocked_all", ())
    matched = [row for row in blocked if row.get("baseline_leg_found")]
    add("- 有對應基準 leg：{0}；無對應基準 leg：{1}".format(
        len(matched), analysis.get("blocked_without_baseline_leg")
    ))
    if matched:
        add("")
        lines.extend(rw._blocked_table_lines(matched[:MAX_REPORT_BLOCKED_ROWS]))
    else:
        add("")
        add("本次樣本找不到可對應的 MA144 基準 leg，如實說明。")
        add("")
    open_blocked = [row for row in matched if row.get("baseline_costed_net_pnl") is None]
    if open_blocked:
        add("其中基準期末仍持有者：")
        add("")
        lines.extend(rw._blocked_open_table_lines(open_blocked[:MAX_REPORT_BLOCKED_ROWS]))

    add("## 六、未平倉部位（2022-12-30 另列）")
    add("")
    lines.extend(_open_position_lines(open_positions))

    add("## 七、再進場")
    add("")
    lines.extend(_reentry_section_lines(rows))

    add("## 八、出場理由")
    add("")
    lines.extend(_exit_reason_lines(exit_reasons))

    add("## 九、選股組多標籤績效（多標籤不可相加）")
    add("")
    lines.extend(_group_table_lines(groups))

    add("## 十、大贏家與大輸家")
    add("")
    lines.extend(_movers_lines(top_movers))

    add("## 十一、公司行動未解口徑")
    add("")
    lines.extend(_corpaction_lines(corp_summary))

    add("## 十二、真實 D → E → V → T 抽查與成本手算")
    add("")
    lines.extend(rw._spot_check_lines(spot_checks))

    add("## 十三、完整逐筆交易（0 bps）")
    add("")
    add("以下為本研究 run 的 0 bps 每一筆成交，欄位含 D/E/V/T、進場價、費稅、出場與門檻數值。")
    add("")
    lines.extend(rw._detail_table_lines(detail_rows))

    add("## 十四、最小測試與 gates")
    add("")
    add("| gate | 結果 |")
    add("| --- | --- |")
    for name, value in manifest.get("gates", {}).items():
        add(f"| {name} | {value} |")
    add("")
    add(
        "最小測試位於 `tests/backtests/dual_ma/test_h1_ma144_risk_width_cross_period_2022h1.py`，"
        "涵蓋 7% 邊界（等於通過、低於拒絕）、缺值 HOLD、樞紐確認時間、MA144 先行、"
        "首次進場與再進場、成本手算與未平倉分帳。"
    )
    add("")

    add("## 十五、SHA-256 與 readback")
    add("")
    add("### 產出 artifact SHA-256")
    add("")
    add("| 檔案 | SHA-256 |")
    add("| --- | --- |")
    for name, digest in sorted(manifest.get("artifact_sha256", {}).items()):
        add(f"| {name} | `{digest}` |")
    add("")
    add("### 來源 SHA-256")
    add("")
    add("| 項目 | SHA-256 |")
    add("| --- | --- |")
    for name, value in sorted(sources.items()):
        add(f"| {name} | `{value}` |")
    add("")
    add("readback.json 內另有逐檔重算 SHA-256 與 gates，manifest.json 為本次 run 的權威索引。")
    add("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def _resolve_spec_bytes(
    worktree_root: Path, integration_root: Path, relative: str
) -> tuple[bytes, str]:
    local = worktree_root / relative
    remote = integration_root / relative
    path = local if local.is_file() else remote
    if not path.is_file():
        raise H1RiskWidthError(f"frozen contract is missing: {relative}")
    return path.read_bytes(), str(path)


def _find_integration_root(worktree_root: Path, workdata_root: Path) -> Path:
    """Locate the read-only 00 integration worktree that carries the H2 spec."""

    candidates: list[Path] = []
    try:
        repo_root = Path(workdata_root).parents[2]
        candidates.append(
            repo_root / ".worktrees" / "dual-ma-integration-v0.4.1"
        )
    except IndexError:
        pass
    candidates.append(
        worktree_root / ".worktrees" / "dual-ma-integration-v0.4.1"
    )
    candidates.append(Path("D:/code/stock_ai_bot/.worktrees/dual-ma-integration-v0.4.1"))
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return candidates[-1]


def build_h1_run_by_date(pilot_root: str | Path) -> dict[str, str]:
    return dict(h1x.build_run_by_date(pilot_root))


def run_h1_risk_width(
    *,
    workdata_root: str | Path,
    output_root: str | Path | None = None,
    run_id: str = RUN_ID_DEFAULT,
    episode_limit: int | None = None,
    runner_commit: str | None = None,
    ma144_baseline: bool = True,
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
        raise H1RiskWidthError(
            f"refusing to overwrite an existing run directory: {run_dir}"
        )

    inputs = load_h1_risk_width_inputs(workdata_root, progress=progress)
    calendar = inputs["calendar"]
    snapshot = inputs["snapshot"]
    plan = pilot.build_lifecycle_plan(
        reports=inputs["reports"],
        resolution=inputs["resolution"],
        snapshot=snapshot,
        calendar=calendar,
    )
    preflight = build_h1_preflight(inputs)
    preflight["run_id"] = run_id
    preflight["module_version"] = MODULE_VERSION

    worktree_root = Path(__file__).resolve().parents[2]
    integration_root = _find_integration_root(worktree_root, workdata_root)

    h1_spec_bytes, h1_spec_path = _resolve_spec_bytes(
        worktree_root, integration_root, SPEC_RELATIVE_PATH_H1_VALIDATION
    )
    h1_spec_sha = pilot.sha256_bytes(h1_spec_bytes).upper()
    if h1_spec_sha != SPEC_SHA256_H1_VALIDATION:
        raise H1RiskWidthError(
            f"H1 validation spec sha256 {h1_spec_sha} != {SPEC_SHA256_H1_VALIDATION}"
        )
    if len(h1_spec_bytes) != SPEC_BYTE_SIZE_H1_VALIDATION:
        raise H1RiskWidthError(
            "H1 validation spec byte size "
            f"{len(h1_spec_bytes)} != {SPEC_BYTE_SIZE_H1_VALIDATION}"
        )
    rule_spec_bytes, rule_spec_path = _resolve_spec_bytes(
        worktree_root, integration_root, SPEC_RELATIVE_PATH_H2_RULE
    )
    rule_spec_sha = pilot.sha256_bytes(rule_spec_bytes).upper()
    if rule_spec_sha != SPEC_SHA256_H2_RULE:
        raise H1RiskWidthError(
            f"H2 rule spec sha256 {rule_spec_sha} != {SPEC_SHA256_H2_RULE}"
        )
    if len(rule_spec_bytes) != SPEC_BYTE_SIZE_H2_RULE:
        raise H1RiskWidthError("H2 rule spec byte size mismatch")

    baseline_h1_dir = workdata_root / "02-causal-backtest" / BASELINE_H1_RUN_ID
    baseline_h1_manifest = baseline_h1_dir / "manifest.json"
    baseline_h1_events = baseline_h1_dir / "engine_events.jsonl"
    single_entry_dir = workdata_root / "02-causal-backtest" / H1_SINGLE_ENTRY_RUN_ID
    single_entry_manifest = single_entry_dir / "manifest.json"
    h2_rule_dir = workdata_root / "02-causal-backtest" / H2_RULE_RUN_ID
    h2_rule_manifest = h2_rule_dir / "manifest.json"
    h2_rule_trades = h2_rule_dir / "trades.csv"
    for path, expected, label in (
        (baseline_h1_manifest, BASELINE_H1_MANIFEST_SHA256, "H1 accepted manifest"),
        (baseline_h1_events, BASELINE_H1_ENGINE_EVENTS_SHA256, "H1 accepted engine events"),
        (single_entry_manifest, H1_SINGLE_ENTRY_MANIFEST_SHA256, "H1 single-entry manifest"),
        (h2_rule_manifest, H2_RULE_RUN_MANIFEST_SHA256, "H2 rule run manifest"),
        (h2_rule_trades, H2_RULE_RUN_TRADES_SHA256, "H2 rule run trades"),
    ):
        if not path.is_file():
            raise H1RiskWidthError(f"{label} is missing: {path}")
        actual = pilot.sha256_file(path).upper()
        if actual != expected:
            raise H1RiskWidthError(f"{label} sha256 {actual} != {expected}")

    commit_state = pilot.resolve_runner_commit(worktree_root)
    commit = runner_commit or commit_state["runner_commit"]
    state = flat.git_state(worktree_root)

    official_sessions = calendar.sessions
    run_dir.mkdir(parents=True, exist_ok=True)
    rows, gate_rows, baseline_rows, stats = collect_trade_rows(
        plan=plan,
        snapshot=snapshot,
        code_programs_by_date=inputs["code_programs_by_date"],
        official_sessions=official_sessions,
        run_id=run_id,
        runner_commit=commit,
        progress=progress,
        episode_limit=episode_limit,
        ma144_baseline=ma144_baseline,
    )
    if progress is not None:
        progress("engine_done", stats["episode_count_run"], stats["episode_count_run"], stats)

    summary = h2.summarize_rows(rows)
    groups = h2.build_group_attribution(
        rows, code_programs_by_date=inputs["code_programs_by_date"]
    )
    groups["schema_version"] = "dual-ma-h1-ma144-risk-width-cross-period-groups-v1"
    groups["multi_label_note"] = (
        "同一 D 被多組選中的個股在每個組別各計一次，不可把組別欄位相加成單一投組；"
        "本輪樣本為 2022 H1 的六個可用組別。"
    )
    for item in groups.get("unavailable_group_rows", ()):
        item["note"] = (
            "此組別在 2022 H1 選股日中沒有產生候選股，不得記為 0 筆交易。"
        )
    top_movers = h2.build_top_movers(rows)
    exit_reasons = h2.build_exit_reason_breakdown(rows)
    open_positions = h2.build_year_end_open_positions(rows=rows, snapshot=snapshot)
    corp_summary = flat.enrich_corporate_actions(
        rows=rows,
        snapshot=snapshot,
        workdata_root=workdata_root,
        run_id=run_id,
        runner_commit=commit,
        progress=progress,
    )
    corp_open = flat.build_corpaction_open_positions(rows=rows, snapshot=snapshot)

    prices, sessions = ma144.load_price_and_sessions(workdata_root)
    new_diag = attribution.build_leg_diagnostics(
        ma144.zero_bps_legs_from_rows(rows, run_dir=run_dir),
        sessions=sessions,
        prices=prices,
    )
    baseline_diag = (
        attribution.build_leg_diagnostics(
            ma144.zero_bps_legs_from_rows(baseline_rows, run_dir=run_dir),
            sessions=sessions,
            prices=prices,
        )
        if baseline_rows
        else []
    )
    metrics_tables: list[str] = []
    for strategy in STRATEGY_ORDER:
        metrics_tables.extend(
            rw._metrics_table_lines(
                attribution.STRATEGY_LABEL_ZH.get(strategy, strategy),
                ma144.strategy_metrics(new_diag, strategy=strategy, sessions=sessions),
                ma144.strategy_metrics(baseline_diag, strategy=strategy, sessions=sessions),
            )
        )
    analysis = rw.build_risk_width_analysis(
        gate_rows=gate_rows, rows=rows, baseline_diag=baseline_diag
    )
    analysis["schema_version"] = (
        "dual-ma-h1-ma144-risk-width-cross-period-analysis-v1"
    )
    spot_checks = rw.build_spot_checks(rows=rows, analysis=analysis, gate_rows=gate_rows)

    primary_rows = [
        row for row in rows if int(row["slippage_bps"]) == PRIMARY_SLIPPAGE_BPS
    ]
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
    gate_rows_sorted = sorted(
        gate_rows, key=lambda row: (row["strategy"], row["code"], row["entry_signal_date_T"])
    )

    # ---- structural gates -------------------------------------------------
    session_set = set(official_sessions)
    h1_sessions = list(calendar.sessions_between(H1_FIRST_DAY, H1_LAST_DAY))
    selection_dates = [date.fromisoformat(day) for day in inputs["selection_dates"]]
    h2_selection_leaks = [day for day in selection_dates if day > H1_LAST_DAY]
    late_entries = [
        row for row in primary_rows
        if date.fromisoformat(row["entry_signal_date_T"]) > AS_OF_CUTOFF
    ]
    year_end_leaks = [
        row for row in primary_rows
        if (row["exit_date"] is not None and date.fromisoformat(row["exit_date"]) > AS_OF_CUTOFF)
    ]
    early_entries = [
        row for row in primary_rows
        if date.fromisoformat(row["entry_signal_date_T"])
        < date.fromisoformat(row["monitor_effective_date_E"])
    ]
    bad_E = []
    for row in primary_rows:
        D = date.fromisoformat(row["source_selection_date_D"])
        E = date.fromisoformat(row["monitor_effective_date_E"])
        if calendar.next_session(D) != E:
            bad_E.append(row)
    bad_T = [
        row for row in primary_rows
        if date.fromisoformat(row["entry_signal_date_T"]) not in session_set
    ]
    bad_V = [
        row for row in primary_rows
        if row["pivot_confirmed_date"] is None
        or date.fromisoformat(row["pivot_confirmed_date"])
        > date.fromisoformat(row["entry_signal_date_T"])
    ]
    bad_sizing = [
        row for row in primary_rows
        if int(row["shares"]) > 999
        or Decimal(str(row["buy_total"])) > Decimal("10000")
        or Decimal(str(row["initial_r"])) > Decimal("2000")
    ]
    fill_mismatch = [
        row for row in primary_rows
        if row["entry_fill_price"] != row["entry_reference_close"]
    ]
    ma144_gate_missing = [row for row in primary_rows if row["gate_state"] != rw.STATE_PASS]
    risk_width_not_met = [
        row for row in primary_rows
        if row["risk_width_value"] is None
        or Decimal(str(row["risk_width_value"])) < RISK_WIDTH_THRESHOLD
    ]
    risk_width_verdict_missing = [
        row for row in primary_rows
        if row["risk_width_state"] != rw.STATE_RW_PASS or row["deciding_gate"] is not None
    ]
    blocked_keys = {
        (str(row["code"]), str(row["strategy"]), str(row["entry_signal_date_T"]))
        for row in gate_rows
    }
    blocked_but_filled = [
        row for row in primary_rows
        if (str(row["code"]), str(row["strategy"]), str(row["entry_signal_date_T"]))
        in blocked_keys
    ]
    open_with_stats = [
        row for row in rows
        if row["open_censored"]
        and (row["sell_net"] is not None or row["costed_net_pnl"] is not None)
    ]
    reentry_bad_order = [
        row for row in primary_rows
        if row["is_reentry"]
        and (
            row["prior_exit_date"] is None
            or date.fromisoformat(row["entry_signal_date_T"])
            <= date.fromisoformat(row["prior_exit_date"])
        )
    ]

    gates = {
        "parent_reports_match_declared_sha256": (
            "PASS" if inputs["parent"]["all_matched"] else "FAIL"
        ),
        "coverage_is_exactly_117_official_h1_sessions": (
            "PASS"
            if len(inputs["reports"]) == EXPECTED_H1_SESSION_COUNT
            and len(h1_sessions) == EXPECTED_H1_SESSION_COUNT
            and preflight["dates_are_exactly_official_h1_sessions"]
            else "FAIL"
        ),
        "zero_2022_h2_selection_dates_added": (
            "PASS" if not h2_selection_leaks else "FAIL"
        ),
        "price_snapshot_never_reaches_2023": (
            "PASS" if snapshot.last_session <= AS_OF_CUTOFF else "FAIL"
        ),
        "universe_sources_match_declared_sha256": (
            "PASS" if inputs["universe_sha"] == h1x.UNIVERSE_SOURCES_RAW_SHA256 else "FAIL"
        ),
        "h1_validation_spec_matches_declared_sha256": (
            "PASS" if h1_spec_sha == SPEC_SHA256_H1_VALIDATION else "FAIL"
        ),
        "h2_rule_spec_matches_declared_sha256": (
            "PASS" if rule_spec_sha == SPEC_SHA256_H2_RULE else "FAIL"
        ),
        "h1_accepted_run_bytes_unchanged": (
            "PASS"
            if pilot.sha256_file(baseline_h1_manifest).upper() == BASELINE_H1_MANIFEST_SHA256
            and pilot.sha256_file(baseline_h1_events).upper()
            == BASELINE_H1_ENGINE_EVENTS_SHA256
            else "FAIL"
        ),
        "h2_rule_run_bytes_unchanged": (
            "PASS"
            if pilot.sha256_file(h2_rule_manifest).upper() == H2_RULE_RUN_MANIFEST_SHA256
            and pilot.sha256_file(h2_rule_trades).upper() == H2_RULE_RUN_TRADES_SHA256
            else "FAIL"
        ),
        "every_episode_E_equals_next_official_session_after_D": (
            "PASS" if not bad_E else "FAIL"
        ),
        "every_entry_T_is_official_session": "PASS" if not bad_T else "FAIL",
        "every_entry_has_confirmed_pivot_on_or_before_T": (
            "PASS" if not bad_V else "FAIL"
        ),
        "every_entry_is_at_or_after_monitor_effective_E": (
            "PASS" if not early_entries else "FAIL"
        ),
        "no_trade_or_exit_after_2022_12_30": (
            "PASS" if not year_end_leaks and not late_entries else "FAIL"
        ),
        "at_most_one_open_leg_per_strategy_at_any_time": (
            "PASS" if int(stats["max_open_legs_observed"]) <= 1 else "FAIL"
        ),
        "no_additions_were_filled": (
            "PASS" if int(stats["filled_add_events"]) == 0 else "FAIL"
        ),
        "every_reentry_is_strictly_after_its_prior_exit": (
            "PASS" if not reentry_bad_order else "FAIL"
        ),
        "sizing_within_frozen_leg_limits": "PASS" if not bad_sizing else "FAIL",
        "zero_bps_fill_equals_signal_day_close": (
            "PASS" if not fill_mismatch else "FAIL"
        ),
        "no_open_leg_carries_a_closed_leg_result": (
            "PASS" if not open_with_stats else "FAIL"
        ),
        "every_accepted_entry_carries_a_pass_ma144_gate_verdict": (
            "PASS" if not ma144_gate_missing else "FAIL"
        ),
        "every_accepted_entry_risk_width_at_least_threshold": (
            "PASS" if not risk_width_not_met else "FAIL"
        ),
        "every_accepted_entry_carries_a_pass_risk_width_verdict": (
            "PASS" if not risk_width_verdict_missing else "FAIL"
        ),
        "no_blocked_signal_became_a_filled_leg": (
            "PASS" if not blocked_but_filled else "FAIL"
        ),
        "spot_checks_present": (
            "PASS"
            if spot_checks["accepted_chain_found"]
            and spot_checks["risk_width_blocked_chain_found"]
            else "FAIL"
        ),
        "spot_check_hand_cost_matches": (
            "PASS"
            if (
                spot_checks["hand_cost_recheck"] is not None
                and spot_checks["hand_cost_recheck"]["buy_matches"]
                and spot_checks["hand_cost_recheck"]["sell_matches"]
                and spot_checks["hand_cost_recheck"]["net_matches"]
            )
            else "FAIL"
        ),
        "ma144_baseline_collected": (
            "PASS" if (not ma144_baseline or baseline_rows) else "FAIL"
        ),
    }
    if episode_limit is not None:
        gates["full_117_day_sample"] = "FAIL"
    readback_status = "PASS" if all(value == "PASS" for value in gates.values()) else "HOLD"

    trade_rows = [[row.get(name) for name in rw.TRADE_ROW_FIELDS] for row in rows]
    gate_row_values = [
        [row.get(name) for name in rw.GATE_DECISION_FIELDS] for row in gate_rows_sorted
    ]
    summary_rows = [[row.get(name) for name in h2.SUMMARY_FIELDS] for row in summary]
    payloads: dict[str, bytes] = {
        "trades.csv": h2._csv_bytes(rw.TRADE_ROW_FIELDS, trade_rows),
        "strategy_summary.csv": h2._csv_bytes(h2.SUMMARY_FIELDS, summary_rows),
        "strategy_summary.json": pilot._json_bytes(
            {
                "schema_version": h2.STATS_SCHEMA_VERSION,
                "run_id": run_id,
                "variant_id": VARIANT_ID,
                "entry_variant": ENTRY_VARIANT.value,
                "entry_gate": ma144.GATE_RULE,
                "risk_width_gate": RISK_WIDTH_RULE,
                "risk_width_threshold": str(RISK_WIDTH_THRESHOLD),
                "primary_slippage_bps": PRIMARY_SLIPPAGE_BPS,
                "slippage_scenarios": list(SLIPPAGE_SCENARIOS),
                "research_only": True,
                "point_in_time_verified": False,
                "rows": summary,
            }
        ),
        "gate_decisions.csv": h2._csv_bytes(rw.GATE_DECISION_FIELDS, gate_row_values),
        "risk_width_analysis.json": pilot._json_bytes(analysis),
        "ma144_baseline_legs.csv": h2._csv_bytes(
            ma144.TRADE_ROW_FIELDS,
            [[row.get(name) for name in ma144.TRADE_ROW_FIELDS] for row in baseline_rows],
        ),
        "group_attribution.json": pilot._json_bytes(groups),
        "top_movers.json": pilot._json_bytes(top_movers),
        "exit_reason_breakdown.json": pilot._json_bytes(exit_reasons),
        "year_end_open_positions.json": pilot._json_bytes(open_positions),
        "corpaction_summary.json": pilot._json_bytes(corp_summary),
        "corpaction_open_positions.json": pilot._json_bytes(corp_open),
        "spot_checks.json": pilot._json_bytes(spot_checks),
        "parent_runs.json": pilot._json_bytes(inputs["parent"]),
        "preflight.json": pilot._json_bytes(preflight),
        "entry_decision_totals.json": pilot._json_bytes(
            {
                "totals": stats["entry_decision_totals"],
                "first_example": stats["entry_decision_first_example"],
            }
        ),
    }
    artifact_hashes = {name: pilot.sha256_bytes(blob) for name, blob in payloads.items()}
    source_sha256 = {
        "h1_validation_contract_path": SPEC_RELATIVE_PATH_H1_VALIDATION,
        "h1_validation_contract_path_used": h1_spec_path,
        "h1_validation_contract_sha256": h1_spec_sha,
        "h1_validation_contract_bytes": len(h1_spec_bytes),
        "h2_rule_contract_path": SPEC_RELATIVE_PATH_H2_RULE,
        "h2_rule_contract_path_used": rule_spec_path,
        "h2_rule_contract_sha256": rule_spec_sha,
        "h2_rule_contract_bytes": len(rule_spec_bytes),
        "frozen_spec": "5A9E3A573178AA135FFBD960871D1ACEE20049F9ACF2DD7C117E1E2390B53DDA",
        "lifecycle_contract": (
            "361D5F35456FCA9B8BF00412CACBA4E2EF4173175ECDBB813D7600A977EB755E"
        ),
        "corporate_action_correction_contract": flat.corp.SPEC_SHA256,
        "price_snapshot_content": snapshot.content_sha256,
        "official_calendar_raw": calendar.raw_sha256,
        "official_calendar_content": calendar.content_sha256,
        "baseline_h1_manifest_sha256": BASELINE_H1_MANIFEST_SHA256,
        "baseline_h1_engine_events_sha256": BASELINE_H1_ENGINE_EVENTS_SHA256,
        "h1_single_entry_manifest_sha256": H1_SINGLE_ENTRY_MANIFEST_SHA256,
        "h2_rule_run_manifest_sha256": H2_RULE_RUN_MANIFEST_SHA256,
        "h2_rule_run_trades_sha256": H2_RULE_RUN_TRADES_SHA256,
        "runner_module_sha256": pilot.sha256_file(Path(__file__)).upper(),
    }
    manifest: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "module_version": MODULE_VERSION,
        "run_id": run_id,
        "variant_id": VARIANT_ID,
        "entry_variant": ENTRY_VARIANT.value,
        "entry_gate": ma144.GATE_RULE,
        "risk_width_gate": RISK_WIDTH_RULE,
        "risk_width_threshold": str(RISK_WIDTH_THRESHOLD),
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
        "not_independent_blind_test": True,
        "h2_run_recomputed": False,
        "variant_rule_delta": [
            "sample: the nine accepted 2022 H1 01 selection reports (117 official ",
            "sessions, 2022-01-03 .. 2022-06-30), no 2022 H2 selection event added",
            "rule: copied verbatim from the accepted H2 risk-width run -- frozen ",
            "flat re-entry + MA144 upward gate + risk_width >= 0.07 at every signal T",
            "monitoring: an H1 episode or leg may run causally to the 2022-12-30 ",
            "snapshot cutoff; open legs stay separate from realised net P&L",
        ],
        "selection_program_modified": False,
        "sample_definition": (
            "the nine accepted 01 selection runs for every official 2022 H1 "
            "session (2022-01-03 .. 2022-06-30); 2023 / 2024 data is never read"
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
                "source_id": h1x.UNIVERSE_SOURCES_SOURCE_ID,
            },
            "parent_runs": inputs["parent"],
            "reference_runs": {
                "h1_accepted_run": {
                    "run_id": BASELINE_H1_RUN_ID,
                    "manifest_sha256": BASELINE_H1_MANIFEST_SHA256,
                    "engine_events_sha256": BASELINE_H1_ENGINE_EVENTS_SHA256,
                },
                "h1_single_entry_run": {
                    "run_id": H1_SINGLE_ENTRY_RUN_ID,
                    "manifest_sha256": H1_SINGLE_ENTRY_MANIFEST_SHA256,
                },
                "h2_rule_run": {
                    "run_id": H2_RULE_RUN_ID,
                    "manifest_sha256": H2_RULE_RUN_MANIFEST_SHA256,
                    "trades_sha256": H2_RULE_RUN_TRADES_SHA256,
                },
            },
        },
        "source_sha256": source_sha256,
        "coverage": {
            "schema_version": preflight["schema_version"],
            "reported_date_count": len(inputs["reports"]),
            "official_h1_session_count": len(h1_sessions),
            "first_date": min(report.report_date for report in inputs["reports"]),
            "last_date": max(report.report_date for report in inputs["reports"]),
            "replay_cutoff": AS_OF_CUTOFF.isoformat(),
            "h2_selection_dates_added": len(h2_selection_leaks),
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
            "gate_decision_totals": stats["gate_decision_totals"],
            "gate_decision_primary_rows": stats["gate_decision_primary_rows"],
            "ma144_baseline_episode_count": stats["ma144_baseline_episode_count"],
            "ma144_baseline_leg_row_count": stats["ma144_baseline_leg_row_count"],
        },
        "stats": {
            "primary_slippage_bps": PRIMARY_SLIPPAGE_BPS,
            "slippage_scenarios": list(SLIPPAGE_SCENARIOS),
            "rows": summary,
        },
        "group_attribution": groups,
        "top_movers": top_movers,
        "exit_reason_breakdown": exit_reasons,
        "risk_width_analysis": analysis,
        "corporate_action": {
            "zero_bps_leg_count": corp_summary["zero_bps_leg_count"],
            "affected_leg_count": corp_summary["affected_leg_count"],
            "resolved_leg_count": corp_summary["resolved_leg_count"],
            "unresolved_leg_count": corp_summary["unresolved_leg_count"],
            "not_reproduced_leg_count": corp_summary["not_reproduced_leg_count"],
            "status_counts": corp_summary["status_counts"],
            "resolved_class_counts": corp_summary["resolved_class_counts"],
            "unresolved_reason_counts": corp_summary["unresolved_reason_counts"],
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

    report = render_report_zh(
        manifest=manifest,
        summary=summary,
        metrics_tables=metrics_tables,
        analysis=analysis,
        groups=groups,
        open_positions=open_positions,
        corp_summary=corp_summary,
        detail_rows=detail_rows,
        gate_rows=gate_rows_sorted,
        spot_checks=spot_checks,
        preflight=preflight,
        rows=rows,
        exit_reasons=exit_reasons,
        top_movers=top_movers,
        git_state=state,
    )
    payloads["report_zh.md"] = report.encode("utf-8")
    for name, blob in payloads.items():
        (run_dir / name).write_bytes(blob)

    for path, expected, label in (
        (baseline_h1_manifest, BASELINE_H1_MANIFEST_SHA256, "H1 accepted manifest"),
        (h2_rule_trades, H2_RULE_RUN_TRADES_SHA256, "H2 rule run trades"),
    ):
        if pilot.sha256_file(path).upper() != expected:
            raise H1RiskWidthError(f"{label} changed during this run")
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
        "entry_gate": ma144.GATE_RULE,
        "risk_width_gate": RISK_WIDTH_RULE,
        "risk_width_threshold": str(RISK_WIDTH_THRESHOLD),
        "status": readback_status,
        "research_only": True,
        "point_in_time_verified": False,
        "formal_historical_performance": False,
        "not_independent_blind_test": True,
        "gates": gates,
        "coverage": manifest["coverage"],
        "lifecycle": manifest["lifecycle"],
        "engine": manifest["engine"],
        "stats": manifest["stats"],
        "group_attribution": groups,
        "risk_width_analysis": {
            key: value for key, value in analysis.items() if key != "blocked_all"
        },
        "corporate_action": manifest["corporate_action"],
        "year_end_open_positions": manifest["year_end_open_positions"],
        "spot_checks": spot_checks,
        "preflight": preflight,
        "git_state": state,
        "runner_commit_state": commit_state,
        "source_sha256": source_sha256,
        "artifact_sha256": declared,
        "artifact_sha256_recomputed": verified,
        "artifacts_verified": declared == verified,
        "checkpoint": {
            "kind": "ATOMIC_H1_MA144_RISK_WIDTH_CROSS_PERIOD",
            "state": "COMPLETE_AWAITING_00_QA",
            "resumable": False,
            "next_action": (
                "00 reviews the H1 cross-period check of the unchanged MA144+7% "
                "rule; no 2023/2024 performance is read and nothing is deployed"
            ),
        },
    }
    (run_dir / "readback.json").write_bytes(pilot._json_bytes(readback))
    return manifest


def _progress_printer(stage: str, done: int, total: int, extra: Mapping[str, Any]) -> None:
    print(f"[{stage}] {done}/{total} {dict(extra)}", flush=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="2022 H1 cross-period check of the frozen MA144+7% risk-width rule"
    )
    parser.add_argument("--workdata-root", required=True)
    parser.add_argument("--output-root", default=None)
    parser.add_argument("--run-id", default=RUN_ID_DEFAULT)
    parser.add_argument("--episode-limit", type=int, default=None)
    parser.add_argument("--no-ma144-baseline", action="store_true")
    args = parser.parse_args(argv)
    manifest = run_h1_risk_width(
        workdata_root=args.workdata_root,
        output_root=args.output_root,
        run_id=args.run_id,
        episode_limit=args.episode_limit,
        ma144_baseline=not args.no_ma144_baseline,
        progress=_progress_printer,
    )
    print(
        f"run_id={manifest['run_id']} status={manifest['status']} "
        f"readback={manifest['readback_status']}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
