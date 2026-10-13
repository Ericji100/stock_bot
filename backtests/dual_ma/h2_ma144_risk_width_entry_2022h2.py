"""2022 H2 MA144-upward + confirmed-pivot risk-width entry gate (02 role).

Authority (read-only, 00-frozen):

* docs/strategies/dual_ma/2022-h2-ma144-risk-width-entry-exploration-v1.md
  raw SHA-256 ED806601D540BF7EDD33BFF8E60796A0B7198691B0823AD3FAC6511D2F066818
  (3676 bytes)  -- this round one added rule
* docs/strategies/dual_ma/2022-h2-ma144-upward-entry-exploration-v1.md
  raw SHA-256 FE603E1E03F7BE44A04202A6EE903EF285C1421D46447226FD94997FB79D65B6
  (3875 bytes)  -- the direct baseline (MA144 upward gate)
* docs/strategies/dual_ma/2022-h2-flat-reentry-v-pivot-exploration-v1.md
  raw SHA-256 6376BDC1F45120A00AFA4468FCC9218E139BB58AADFF68ED22C268FE14469D87
  (4046 bytes)  -- the original flat re-entry baseline

Question: on top of the frozen flat-re-entry state machine AND the frozen MA144
upward entry gate, what changes when every complete buy signal at T additionally
requires

    risk_width = (T close entry-proxy price - confirmed V pivot low) / T close
    risk_width >= 0.07

for the first entry and for every same-episode re-entry.  The MA144 gate is
decided first; risk width only decides a signal the MA144 gate already passed.
Either gate only blocks the current buy; it never changes D, E, the V pivot, the
episode lifecycle or an exit, and a rejected T can never be bought later on a
backfilled basis.  A missing price or an unconfirmed pivot records
HOLD/ENTRY_GATE_UNDETERMINED and buys nothing.

This is a research_only / point_in_time_verified=false exploration.  No 2023 or
2024 performance is read, no parameter is tuned after seeing the result and
nothing is deployed.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import h1_real_pilot_2022_0419_0428 as pilot
from . import h2_flat_reentry_2022h2 as flat
from . import h2_flat_reentry_attribution_2022h2 as attribution
from . import h2_ma144_upward_entry_2022h2 as ma144
from . import h2_single_entry_v_pivot_2022h2 as h2
from .contracts import (
    COST_MODEL,
    EXECUTION_MODEL,
    SPEC_SHA256,
    STRATEGY_VERSION,
    RunManifest,
)
from .models import ExitReason, StrategyKind
from .single_entry_no_add import ENGINE_MATRIX_VEHICLE, STATUS_FILLED


MODULE_VERSION = "dual-ma-h2-ma144-risk-width-entry-2022h2-v1"
RUN_ID_DEFAULT = "h2-ma144-risk-width-entry-2022h2-v1"
MANIFEST_SCHEMA_VERSION = "dual-ma-h2-ma144-risk-width-manifest-v1"
READBACK_SCHEMA_VERSION = "dual-ma-h2-ma144-risk-width-readback-v1"
RISK_WIDTH_ANALYSIS_SCHEMA_VERSION = "dual-ma-h2-ma144-risk-width-analysis-v1"

VARIANT_ID = (
    "FLAT_REENTRY_AFTER_EXIT_WITHIN_EPISODE+MA144_UPWARD_ENTRY_GATE"
    "+RISK_WIDTH_GE_7PCT_ENTRY_GATE"
)
ENTRY_VARIANT = ma144.ENTRY_VARIANT

# The one added rule (on top of the frozen MA144 upward gate).
RISK_WIDTH_RULE = "RISK_WIDTH_GE_THRESHOLD_VS_CONFIRMED_V_PIVOT_LOW"
RISK_WIDTH_RULE_LABEL = "confirmed-pivot risk-width gate"
RISK_WIDTH_THRESHOLD = Decimal("0.07")

STATE_PASS = ma144.STATE_PASS
STATE_NOT_UP = ma144.STATE_NOT_UP
STATE_UNDETERMINED_MA144 = ma144.STATE_UNDETERMINED

STATE_RW_PASS = "PASS"
STATE_RW_BELOW = "BELOW"
STATE_RW_UNDETERMINED = "UNDETERMINED"
STATE_RW_NOT_EVALUATED = "NOT_EVALUATED"

STATUS_ENTRY_GATE_MA144_NOT_UP = ma144.STATUS_ENTRY_GATE_MA144_NOT_UP
STATUS_ENTRY_GATE_UNDETERMINED = ma144.STATUS_ENTRY_GATE_UNDETERMINED
STATUS_ENTRY_GATE_RISK_WIDTH_BELOW = "ENTRY_GATE_RISK_WIDTH_BELOW"

DECIDING_MA144 = "MA144_UPWARD"
DECIDING_RISK_WIDTH = "RISK_WIDTH"

REASON_ENTRY_PRICE_NOT_POSITIVE = "ENTRY_REFERENCE_PRICE_NOT_POSITIVE"
REASON_PIVOT_LOW_MISSING = "CONFIRMED_PIVOT_LOW_MISSING"
REASON_PIVOT_CONFIRMED_AFTER_T = "CONFIRMED_PIVOT_DATE_AFTER_T"
REASON_RISK_WIDTH_EVIDENCE_MISSING = "RISK_WIDTH_EVIDENCE_MISSING"

SPEC_SHA256_RISK_WIDTH = (
    "ED806601D540BF7EDD33BFF8E60796A0B7198691B0823AD3FAC6511D2F066818"
)
SPEC_BYTE_SIZE_RISK_WIDTH = 3676
SPEC_RELATIVE_PATH_RISK_WIDTH = (
    "docs/strategies/dual_ma/2022-h2-ma144-risk-width-entry-exploration-v1.md"
)

BASELINE_MA144_RUN_ID = ma144.RUN_ID_DEFAULT
BASELINE_MA144_RUN_RELATIVE = "02-causal-backtest/" + BASELINE_MA144_RUN_ID
BASELINE_MA144_TRADES_SHA256 = (
    "9CB88F71C7B8EDC82A0019605883F6C31007E8F642351CC841874196686E6A00"
)
BASELINE_MA144_OPEN_SHA256 = (
    "BADA07935240C4369EC810238D49E1DA0A8EEA448508539AC25CA7D5AD109B88"
)

BASELINE_FLAT_RUN_ID = flat.RUN_ID_DEFAULT
BASELINE_FLAT_RUN_RELATIVE = "02-causal-backtest/" + BASELINE_FLAT_RUN_ID
BASELINE_FLAT_TRADES_SHA256 = (
    "ED67AF2B7A9BC8150A7EEFC47FE985B9C09A2420C45C420058757F7860159610"
)
BASELINE_FLAT_OPEN_SHA256 = (
    "B44086562EA1C363E41000B480CD73C2B25724C36E4FFF587BE2AD0D2A919339"
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
ATTR_STRATEGIES = attribution.STRATEGIES


class H2RiskWidthError(RuntimeError):
    """A structural problem with the risk-width run or one of its inputs."""


# ---------------------------------------------------------------------------
# The one added rule: a confirmed-pivot risk-width floor at every signal T
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RiskWidthEvaluation:
    """Outcome of the risk-width test for one signal date."""

    state: str
    risk_width: Decimal | None
    entry_reference_close: Decimal | None
    pivot_low: Decimal | None
    pivot_confirmed_date: date | None
    reason: str | None = None

    @property
    def passed(self) -> bool:
        return self.state == STATE_RW_PASS

    @property
    def decision_status(self) -> str:
        if self.state == STATE_RW_BELOW:
            return STATUS_ENTRY_GATE_RISK_WIDTH_BELOW
        if self.state == STATE_RW_UNDETERMINED:
            return STATUS_ENTRY_GATE_UNDETERMINED
        return STATUS_FILLED


def evaluate_risk_width(
    *,
    signal_date: date,
    entry_reference_close: Any,
    pivot_low: Any,
    pivot_confirmed_date: Any,
    threshold: Decimal = RISK_WIDTH_THRESHOLD,
) -> RiskWidthEvaluation:
    """Causal (T close - confirmed V pivot low) / T close >= threshold.

    Every input is read only from the signal T bar and the already confirmed
    pivot, both of which exist at T.  A non-positive or missing T close, a
    missing pivot low, a missing confirmation date or a confirmation dated after
    T all record UNDETERMINED (HOLD) rather than backfilling a value.
    """

    close = None if entry_reference_close is None else Decimal(entry_reference_close)
    low = None if pivot_low is None else Decimal(pivot_low)
    confirmed = pivot_confirmed_date
    if confirmed is not None and not isinstance(confirmed, date):
        try:
            confirmed = date.fromisoformat(str(confirmed))
        except ValueError:
            confirmed = None
    if close is None or close <= 0:
        return RiskWidthEvaluation(
            STATE_RW_UNDETERMINED, None, close, low, confirmed,
            REASON_ENTRY_PRICE_NOT_POSITIVE,
        )
    if low is None:
        return RiskWidthEvaluation(
            STATE_RW_UNDETERMINED, None, close, low, confirmed,
            REASON_PIVOT_LOW_MISSING,
        )
    if confirmed is None:
        return RiskWidthEvaluation(
            STATE_RW_UNDETERMINED, None, close, low, confirmed,
            REASON_RISK_WIDTH_EVIDENCE_MISSING,
        )
    if confirmed > signal_date:
        return RiskWidthEvaluation(
            STATE_RW_UNDETERMINED, None, close, low, confirmed,
            REASON_PIVOT_CONFIRMED_AFTER_T,
        )
    width = (close - low) / close
    state = STATE_RW_PASS if width >= Decimal(threshold) else STATE_RW_BELOW
    return RiskWidthEvaluation(state, width, close, low, confirmed, None)


def _risk_width_fields(
    evaluation: RiskWidthEvaluation,
    *,
    row_state: str,
    deciding_gate: str | None,
    entry_gate_status: str,
) -> dict[str, Any]:
    return {
        "risk_width_rule": RISK_WIDTH_RULE,
        "risk_width_threshold": h2._dstr(RISK_WIDTH_THRESHOLD),
        "risk_width_state": row_state,
        "risk_width_reason": evaluation.reason,
        "risk_width_value": (
            None
            if evaluation.risk_width is None
            else h2._dstr(evaluation.risk_width, 6)
        ),
        "risk_width_entry_reference_close": h2._dstr(
            evaluation.entry_reference_close
        ),
        "risk_width_pivot_low": h2._dstr(evaluation.pivot_low),
        "risk_width_pivot_confirmed_date": (
            None
            if evaluation.pivot_confirmed_date is None
            else evaluation.pivot_confirmed_date.isoformat()
        ),
        "deciding_gate": deciding_gate,
        "entry_gate_status": entry_gate_status,
    }


def _ma144_gate_fields(gate: ma144.GateEvaluation) -> dict[str, Any]:
    return {
        "gate_rule": ma144.GATE_RULE,
        "gate_state": gate.state,
        "gate_reason": gate.reason,
        "gate_reference_date": (
            None if gate.reference_date is None else gate.reference_date.isoformat()
        ),
        "gate_sma144_at_signal": (
            None if gate.sma_at_signal is None else h2._dstr(gate.sma_at_signal)
        ),
        "gate_sma144_at_reference": (
            None
            if gate.sma_at_reference is None
            else h2._dstr(gate.sma_at_reference)
        ),
    }


class RiskWidthEntryEngine(ma144.Ma144UpwardEntryEngine):
    """MA144 upward gate plus, in sequence, a confirmed-pivot risk-width floor.

    Only the entry handler is extended.  The frozen rejection paths keep their
    own status: a signal while a leg is open stays an addition rejection, a
    same-day exit-and-rebuy stays refused and a regime-invalidated episode still
    needs a fresh D -> E -> V -> T.  The MA144 verdict is read first; the
    risk-width floor is only consulted for a signal the MA144 gate passed.  A
    blocked signal opens no legs, and an old T is never reused on a later
    session.
    """

    def __init__(
        self,
        *,
        slippage_bps: int = 0,
        entry_variant: Any = ENTRY_VARIANT,
        gate_map: Mapping[date, ma144.GateEvaluation] | None = None,
        official_sessions: Sequence[date] = (),
        back: int = ma144.GATE_BACK_SESSIONS,
        window: int = ma144.GATE_SMA_WINDOW,
        risk_width_threshold: Decimal = RISK_WIDTH_THRESHOLD,
        kd_2r_death_cross_exit: bool = False,
        kd_values_by_date: Mapping[
            date, tuple[Decimal | None, Decimal | None]
        ] | None = None,
    ) -> None:
        super().__init__(
            slippage_bps=slippage_bps,
            entry_variant=entry_variant,
            gate_map=gate_map,
            official_sessions=official_sessions,
            back=back,
            window=window,
            kd_2r_death_cross_exit=kd_2r_death_cross_exit,
            kd_values_by_date=kd_values_by_date,
        )
        self._risk_width_threshold = Decimal(risk_width_threshold)

    def _risk_width_for(
        self, evidence: Mapping[str, Any], signal_date: date
    ) -> RiskWidthEvaluation:
        return evaluate_risk_width(
            signal_date=signal_date,
            entry_reference_close=evidence.get("entry_reference_close"),
            pivot_low=evidence.get("pivot_low"),
            pivot_confirmed_date=evidence.get("confirmed_date"),
            threshold=self._risk_width_threshold,
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
        if self._would_fill(state, bar):
            previous = self._last_closed.get(state.kind)
            gate = self._gate_for(bar.date)
            risk_width = self._risk_width_for(evidence, bar.date)
            if not gate.passed:
                fields = _risk_width_fields(
                    risk_width,
                    row_state=STATE_RW_NOT_EVALUATED,
                    deciding_gate=DECIDING_MA144,
                    entry_gate_status=gate.decision_status,
                )
                evidence = {**evidence, **fields}
                super()._handle_entry_signal(
                    result, state, bar, signal_stop, pullback_event_id, events,
                    instrument_type, entry_evidence=evidence,
                )
                return
            fields = _risk_width_fields(
                risk_width,
                row_state=risk_width.state,
                deciding_gate=(None if risk_width.passed else DECIDING_RISK_WIDTH),
                entry_gate_status=risk_width.decision_status,
            )
            evidence = {**evidence, **fields}
            if not risk_width.passed:
                events.append(
                    {
                        "type": "ENTRY_DECISION",
                        "role": "MOTHER",
                        "status": risk_width.decision_status,
                        "variant_id": VARIANT_ID,
                        "hold": risk_width.state == STATE_RW_UNDETERMINED,
                        "pullback_event_id": pullback_event_id,
                        "initial_stop": signal_stop,
                        "prior_leg_id": None if previous is None else previous[2],
                        "prior_exit_date": (
                            None if previous is None else previous[0].isoformat()
                        ),
                        "prior_exit_reason": (
                            None if previous is None else previous[1].value
                        ),
                        **_ma144_gate_fields(gate),
                        **evidence,
                    }
                )
                return
            super()._handle_entry_signal(
                result, state, bar, signal_stop, pullback_event_id, events,
                instrument_type, entry_evidence=evidence,
            )
            return
        super()._handle_entry_signal(
            result, state, bar, signal_stop, pullback_event_id, events,
            instrument_type, entry_evidence=evidence,
        )



# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------


RISK_WIDTH_ROW_FIELDS: tuple[str, ...] = (
    "risk_width_rule",
    "risk_width_threshold",
    "risk_width_state",
    "risk_width_reason",
    "risk_width_value",
    "risk_width_entry_reference_close",
    "risk_width_pivot_low",
    "risk_width_pivot_confirmed_date",
    "deciding_gate",
    "entry_gate_status",
)

TRADE_ROW_FIELDS: tuple[str, ...] = ma144.TRADE_ROW_FIELDS + RISK_WIDTH_ROW_FIELDS
GATE_DECISION_FIELDS: tuple[str, ...] = (
    ma144.GATE_DECISION_FIELDS + RISK_WIDTH_ROW_FIELDS
)

GATE_BLOCK_STATUSES: tuple[str, ...] = (
    STATUS_ENTRY_GATE_MA144_NOT_UP,
    STATUS_ENTRY_GATE_RISK_WIDTH_BELOW,
    STATUS_ENTRY_GATE_UNDETERMINED,
)


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
            "variant_contract": SPEC_RELATIVE_PATH_RISK_WIDTH,
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
    """Replay one activated episode with the MA144 + risk-width engine."""

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
    result = RiskWidthEntryEngine(
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
    row = ma144._leg_row(
        leg=leg,
        episode=episode,
        slippage_bps=slippage_bps,
        run_id=run_id,
        pullback_event_id=pullback_event_id,
        entry_evidence=entry_evidence,
        groups=groups,
        reentry_ordinal=reentry_ordinal,
        episode_window_leg_count=episode_window_leg_count,
        prior_leg=prior_leg,
    )
    for name in RISK_WIDTH_ROW_FIELDS:
        row[name] = entry_evidence.get(name)
    return row


def _gate_decision_row(
    *,
    event: Mapping[str, Any],
    episode: Any,
    run_id: str,
    slippage_bps: int,
    prior_leg: Any | None,
) -> dict[str, Any]:
    row = ma144._gate_decision_row(
        event=event,
        episode=episode,
        run_id=run_id,
        slippage_bps=slippage_bps,
        prior_leg=prior_leg,
    )
    for name in RISK_WIDTH_ROW_FIELDS:
        row[name] = event.get(name)
    return row


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
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Replay every activated episode at 0/10/20 bps behind both gates."""

    episodes = list(plan.episodes)
    truncated = False
    if episode_limit is not None and episode_limit < len(episodes):
        episodes = episodes[: int(episode_limit)]
        truncated = True

    rows: list[dict[str, Any]] = []
    gate_rows: list[dict[str, Any]] = []
    decision_totals: Counter[str] = Counter()
    decision_first_example: dict[str, dict[str, Any]] = {}
    episodes_with_leg: Counter[str] = Counter()
    reentry_episodes: Counter[str] = Counter()
    gate_totals: Counter[str] = Counter()
    max_open_legs = 0
    filled_add_events = 0
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
                    if status in GATE_BLOCK_STATUSES:
                        gate_totals[f"{episode.strategy}|{slippage_bps}|{status}"] += 1
                        if slippage_bps == PRIMARY_SLIPPAGE_BPS:
                            prior_id = event.get("prior_leg_id")
                            prior_leg = leg_by_id.get(str(prior_id)) if prior_id else None
                            gate_rows.append(
                                _gate_decision_row(
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
                    _leg_row(
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
    }
    return rows, gate_rows, stats



# ---------------------------------------------------------------------------
# Analysis helpers
# ---------------------------------------------------------------------------


def _metrics_table_lines(
    title: str, new: Mapping[str, Any], base_row: Mapping[str, Any]
) -> list[str]:
    def pct(value: Any) -> str:
        return "" if value is None else f"{Decimal(value) * 100:.2f}%"

    def num(value: Any, places: str = "0.01") -> str:
        return "" if value is None else str(Decimal(value).quantize(Decimal(places)))

    lines = [f"### {title}", ""]
    lines.append("| 指標 | 新版本（MA144＋7% 風險寬度） | MA144 基準 | 差異 |")
    lines.append("| --- | --- | --- | --- |")
    rows = [
        ("已平倉筆數", new["closed_count"], base_row["closed_count"], "int"),
        ("未平倉筆數", new["open_count"], base_row["open_count"], "int"),
        ("總筆數", new["leg_count"], base_row["leg_count"], "int"),
        ("勝筆", new["win_count"], base_row["win_count"], "int"),
        ("敗筆", new["loss_count"], base_row["loss_count"], "int"),
        ("勝率（費後）", new["win_rate"], base_row["win_rate"], "pct"),
        ("費前損益合計", new["gross_pnl_total"], base_row["gross_pnl_total"], "money"),
        ("費稅合計", new["fees_and_tax_total"], base_row["fees_and_tax_total"], "money"),
        ("費後淨損益合計", new["costed_net_pnl_total"], base_row["costed_net_pnl_total"], "money"),
        ("平均每筆費後損益", new["mean_costed_net_pnl"], base_row["mean_costed_net_pnl"], "money"),
        ("中位數每筆費後損益", new["median_costed_net_pnl"], base_row["median_costed_net_pnl"], "money"),
        ("成本後獲利因子", new["profit_factor"], base_row["profit_factor"], "ratio"),
        ("平均持有官方交易日", new["mean_hold_sessions_closed"], base_row["mean_hold_sessions_closed"], "ratio"),
        ("中位持有官方交易日", new["median_hold_sessions_closed"], base_row["median_hold_sessions_closed"], "ratio"),
        ("平均持有日曆天", new["mean_hold_calendar_days_closed"], base_row["mean_hold_calendar_days_closed"], "ratio"),
        ("早期結構停損筆數", new["early_stop_count"], base_row["early_stop_count"], "int"),
        ("結構停損筆數", new["structural_stop_count"], base_row["structural_stop_count"], "int"),
        ("制度出場筆數", new["regime_exit_count"], base_row["regime_exit_count"], "int"),
        ("再進場筆數", new["reentry_leg_count"], base_row["reentry_leg_count"], "int"),
        ("再進場未平倉", new["reentry_open_count"], base_row["reentry_open_count"], "int"),
        ("再進場費後淨損益", new["reentry_net_total"], base_row["reentry_net_total"], "money"),
        ("累計買進成本（資金占用代理）", new["buy_total_sum"], base_row["buy_total_sum"], "money"),
        ("最大單筆買進成本", new["max_buy_total"], base_row["max_buy_total"], "money"),
    ]
    for label, left, right, kind in rows:
        if kind == "pct":
            left_txt, right_txt = pct(left), pct(right)
            delta = (
                ""
                if left is None or right is None
                else f"{Decimal(left) * 100 - Decimal(right) * 100:.2f}pp"
            )
        elif kind == "int":
            left_txt, right_txt = str(left), str(right)
            delta = f"{int(left) - int(right):+d}"
        else:
            left_txt = num(left)
            right_txt = num(right)
            delta = (
                ""
                if left is None or right is None
                else f"{Decimal(left) - Decimal(right):.4f}"
            )
        lines.append(f"| {label} | {left_txt} | {right_txt} | {delta} |")
    lines.append("")
    return lines


def build_risk_width_analysis(
    *,
    gate_rows: Sequence[Mapping[str, Any]],
    rows: Sequence[Mapping[str, Any]],
    baseline_diag: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Summarise accept / MA144 reject / risk-width reject / HOLD."""

    ma144_state_counts = Counter(str(row["gate_state"]) for row in gate_rows)
    rw_state_counts = Counter(str(row["risk_width_state"]) for row in gate_rows)
    deciding_counts = Counter(
        str(row["deciding_gate"]) for row in gate_rows if row["deciding_gate"]
    )
    status_counts = Counter(str(row["status"]) for row in gate_rows)
    ma144_reason_counts = Counter(
        str(row["gate_reason"]) for row in gate_rows if row["gate_reason"]
    )
    rw_reason_counts = Counter(
        str(row["risk_width_reason"])
        for row in gate_rows
        if row["risk_width_reason"]
    )
    per_strategy: dict[str, dict[str, int]] = {}
    for strategy in ATTR_STRATEGIES:
        subset = [row for row in gate_rows if row["strategy"] == strategy]
        accepted = [
            row
            for row in rows
            if int(row["slippage_bps"]) == PRIMARY_SLIPPAGE_BPS
            and row["strategy"] == strategy
        ]
        rw_blocked = [
            row
            for row in subset
            if row["deciding_gate"] == DECIDING_RISK_WIDTH
        ]
        per_strategy[strategy] = {
            "accepted_legs": len(accepted),
            "ma144_not_up": sum(
                1 for row in subset if row["gate_state"] == STATE_NOT_UP
            ),
            "ma144_hold": sum(
                1 for row in subset
                if row["gate_state"] == STATE_UNDETERMINED_MA144
            ),
            "risk_width_below": sum(
                1 for row in rw_blocked if row["risk_width_state"] == STATE_RW_BELOW
            ),
            "risk_width_hold": sum(
                1
                for row in rw_blocked
                if row["risk_width_state"] == STATE_RW_UNDETERMINED
            ),
            "blocked_would_be_reentry": sum(
                1
                for row in rw_blocked
                if str(row["would_be_reentry"]) in ("True", "true")
            ),
            "blocked_first_entry": sum(
                1
                for row in rw_blocked
                if str(row["would_be_reentry"]) not in ("True", "true")
            ),
        }
    baseline_by_key = {
        (str(row["code"]), str(row["strategy"]), str(row["T"])): row
        for row in baseline_diag
    }
    blocked: list[dict[str, Any]] = []
    blocked_without_baseline = 0
    for row in gate_rows:
        if row["deciding_gate"] != DECIDING_RISK_WIDTH:
            continue
        key = (
            str(row["code"]),
            str(row["strategy"]),
            str(row["entry_signal_date_T"]),
        )
        source = baseline_by_key.get(key)
        record = {
            **{name: row.get(name) for name in GATE_DECISION_FIELDS},
            "baseline_leg_found": source is not None,
            "baseline_costed_net_pnl": (
                None if source is None else source["costed_net_pnl"]
            ),
            "baseline_gross_pnl": None if source is None else source["gross_pnl"],
            "baseline_hold_sessions_transacted": (
                None if source is None else source["hold_sessions_transacted"]
            ),
            "baseline_exit_reason": None if source is None else source["exit_reason"],
            "baseline_is_reentry": None if source is None else source["is_reentry"],
        }
        if source is None:
            blocked_without_baseline += 1
        blocked.append(record)
    matched = [row for row in blocked if row["baseline_leg_found"]]
    blocked_winners = sorted(
        [
            row
            for row in matched
            if row["baseline_costed_net_pnl"] is not None
            and Decimal(row["baseline_costed_net_pnl"]) > 0
        ],
        key=lambda row: Decimal(row["baseline_costed_net_pnl"]),
        reverse=True,
    )
    blocked_losers = sorted(
        [
            row
            for row in matched
            if row["baseline_costed_net_pnl"] is not None
            and Decimal(row["baseline_costed_net_pnl"]) < 0
        ],
        key=lambda row: Decimal(row["baseline_costed_net_pnl"]),
    )
    blocked_open = [row for row in matched if row["baseline_costed_net_pnl"] is None]
    blocked_closed = [
        row for row in matched if row["baseline_costed_net_pnl"] is not None
    ]
    blocked_net_total = sum(
        (Decimal(row["baseline_costed_net_pnl"] or 0) for row in blocked_closed),
        Decimal(0),
    )
    blocked_gross_total = sum(
        (Decimal(row["baseline_gross_pnl"] or 0) for row in blocked_closed),
        Decimal(0),
    )
    ma144_blocked_total = sum(
        1 for row in gate_rows if row["deciding_gate"] == DECIDING_MA144
    )
    return {
        "schema_version": RISK_WIDTH_ANALYSIS_SCHEMA_VERSION,
        "risk_width_rule": RISK_WIDTH_RULE,
        "risk_width_threshold": str(RISK_WIDTH_THRESHOLD),
        "comparison": "GREATER_THAN_OR_EQUAL_TO_THRESHOLD",
        "ma144_gate_rule": ma144.GATE_RULE,
        "ma144_state_counts": dict(sorted(ma144_state_counts.items())),
        "risk_width_state_counts": dict(sorted(rw_state_counts.items())),
        "deciding_gate_counts": dict(sorted(deciding_counts.items())),
        "status_counts": dict(sorted(status_counts.items())),
        "ma144_reason_counts": dict(sorted(ma144_reason_counts.items())),
        "risk_width_reason_counts": dict(sorted(rw_reason_counts.items())),
        "per_strategy": per_strategy,
        "ma144_blocked_signal_total": ma144_blocked_total,
        "risk_width_blocked_signal_total": len(blocked),
        "blocked_without_baseline_leg": blocked_without_baseline,
        "blocked_matched_leg_count": len(matched),
        "blocked_winner_count": len(blocked_winners),
        "blocked_loser_count": len(blocked_losers),
        "blocked_open_count": len(blocked_open),
        "blocked_costed_net_pnl_total": blocked_net_total.quantize(
            Decimal("0.0001")
        ),
        "blocked_gross_pnl_total": blocked_gross_total.quantize(Decimal("0.0001")),
        "blocked_winners": blocked_winners[:10],
        "blocked_losers": blocked_losers[:10],
        "blocked_open": blocked_open[:20],
        "blocked_closed_count": len(blocked_closed),
        "blocked_all": blocked,
        "note": (
            "a blocked winner or loser is a leg of the frozen MA144 baseline run "
            "whose own T the added risk-width floor refused; it is evidence about "
            "what the floor removed, never a trade of this run"
        ),
    }


def build_spot_checks(
    *,
    rows: Sequence[Mapping[str, Any]],
    analysis: Mapping[str, Any],
    gate_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Real accepted and real rejected D -> E -> V -> T chains plus hand cost."""

    primary = [
        row
        for row in rows
        if int(row["slippage_bps"]) == PRIMARY_SLIPPAGE_BPS
        and not row["open_censored"]
        and row["pivot_confirmed_date"] is not None
        and row["risk_width_state"] == STATE_RW_PASS
        and row["gate_state"] == STATE_PASS
    ]
    primary.sort(key=lambda row: (row["code"], row["entry_signal_date_T"]))
    accepted = primary[:2]
    rejected_rows = [
        row
        for row in analysis.get("blocked_all", ())
        if row["baseline_leg_found"]
        and row["risk_width_state"] == STATE_RW_BELOW
    ]
    rejected_rows.sort(key=lambda row: (row["code"], row["entry_signal_date_T"]))
    risk_width_blocked = rejected_rows[:2]
    ma144_blocked = [
        row
        for row in gate_rows
        if row["deciding_gate"] == DECIDING_MA144
        and row["gate_state"] == STATE_NOT_UP
    ]
    ma144_blocked.sort(key=lambda row: (row["code"], row["entry_signal_date_T"]))
    ma144_blocked = ma144_blocked[:1]
    hand_calc: dict[str, Any] | None = None
    if accepted:
        row = accepted[0]
        shares = int(row["shares"])
        entry_price = Decimal(str(row["entry_fill_price"]))
        exit_price = Decimal(str(row["exit_fill_price"]))
        buy_order = flat.CATHAY_WEB_028_TW_V1.buy(entry_price, shares)
        sell_order = flat.CATHAY_WEB_028_TW_V1.sell(
            exit_price, shares, h2.InstrumentType.STOCK
        )
        fees = buy_order.commission + sell_order.commission + sell_order.tax
        net = sell_order.cash_amount - buy_order.cash_amount
        hand_calc = {
            "leg_key": row["leg_key"],
            "shares": shares,
            "entry_fill_price": h2._dstr(entry_price),
            "exit_fill_price": h2._dstr(exit_price),
            "commission_rate": str(flat.CATHAY_WEB_028_TW_V1.commission_rate),
            "stock_sell_tax_rate": str(flat.CATHAY_WEB_028_TW_V1.stock_sell_tax_rate),
            "hand_buy_gross": h2._dstr(buy_order.gross),
            "hand_buy_commission": h2._dstr(buy_order.commission),
            "hand_buy_total": h2._dstr(buy_order.cash_amount),
            "row_buy_gross": row["buy_gross"],
            "row_buy_commission": row["buy_commission"],
            "row_buy_total": row["buy_total"],
            "hand_sell_gross": h2._dstr(sell_order.gross),
            "hand_sell_commission": h2._dstr(sell_order.commission),
            "hand_sell_tax": h2._dstr(sell_order.tax),
            "hand_sell_net": h2._dstr(sell_order.cash_amount),
            "row_sell_gross": row["sell_gross"],
            "row_sell_commission": row["sell_commission"],
            "row_sell_tax": row["sell_tax"],
            "row_sell_net": row["sell_net"],
            "hand_total_fees_and_tax": h2._dstr(fees),
            "row_total_fees_and_tax": row["total_fees_and_tax"],
            "hand_costed_net_pnl": h2._dstr(net),
            "row_costed_net_pnl": row["costed_net_pnl"],
            "buy_matches": (
                buy_order.cash_amount == Decimal(str(row["buy_total"]))
                and buy_order.commission == Decimal(str(row["buy_commission"]))
            ),
            "sell_matches": (
                sell_order.cash_amount == Decimal(str(row["sell_net"]))
                and sell_order.tax == Decimal(str(row["sell_tax"]))
            ),
            "net_matches": net == Decimal(str(row["costed_net_pnl"])),
        }
    return {
        "accepted_d_e_v_t": accepted,
        "risk_width_blocked_d_e_v_t": risk_width_blocked,
        "ma144_blocked_d_e_v_t": ma144_blocked,
        "accepted_chain_found": len(accepted) >= 1,
        "risk_width_blocked_chain_found": len(risk_width_blocked) >= 1,
        "hand_cost_recheck": hand_calc,
    }



# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def _detail_table_lines(detail_rows: Sequence[Mapping[str, Any]]) -> list[str]:
    headers = (
        "唯一鍵", "策略", "代號", "證券", "組別", "D", "E", "V", "T", "進場價",
        "股數", "買進成本", "費稅", "出場日", "出場價", "出場原因",
        "MA144(T)", "MA144(T-20)", "風險寬度", "門檻", "賣出淨額", "費後損益",
        "重進", "前次出場", "公司行動",
    )
    rows: list[Sequence[Any]] = []
    for row in detail_rows:
        rows.append(
            (
                row["leg_key"],
                row["strategy"],
                row["code"],
                row["symbol"],
                row["selection_groups"],
                row["source_selection_date_D"],
                row["monitor_effective_date_E"],
                row["pivot_confirmed_date"],
                row["entry_signal_date_T"],
                row["entry_fill_price"],
                row["shares"],
                row["buy_total"],
                row["total_fees_and_tax"],
                row["exit_date"],
                row["exit_fill_price"],
                row["exit_reason"],
                row["gate_sma144_at_signal"],
                row["gate_sma144_at_reference"],
                row["risk_width_value"],
                row["risk_width_threshold"],
                row["sell_net"],
                row["costed_net_pnl"],
                "是" if row["is_reentry"] else "否",
                ma144._prior_reference(row),
                row["corporate_action_status"],
            )
        )
    return ma144._md_table(headers, rows)


def _gate_table_lines(gate_rows: Sequence[Mapping[str, Any]]) -> list[str]:
    headers = (
        "策略", "代號", "證券", "D", "E", "T", "T 收盤", "V 樞紐低點",
        "風險寬度", "門檻", "MA144(T)", "MA144(T-20)", "MA144 狀態",
        "風險寬度狀態", "決定門檻", "最終狀態", "原因", "可能為再進場",
    )
    rows: list[Sequence[Any]] = []
    for row in gate_rows:
        rw_state = row["risk_width_state"]
        rw_label = (
            "HOLD/ENTRY_GATE_UNDETERMINED"
            if rw_state == STATE_RW_UNDETERMINED
            else rw_state
        )
        rows.append(
            (
                row["strategy"],
                row["code"],
                row["symbol"],
                row["source_selection_date_D"],
                row["monitor_effective_date_E"],
                row["entry_signal_date_T"],
                row["entry_reference_close"],
                row["risk_width_pivot_low"],
                row["risk_width_value"],
                row["risk_width_threshold"],
                row["gate_sma144_at_signal"],
                row["gate_sma144_at_reference"],
                row["gate_state"],
                rw_label,
                row["deciding_gate"],
                row["status"],
                row["gate_reason"] or row["risk_width_reason"],
                "是" if str(row["would_be_reentry"]) in ("True", "true") else "否",
            )
        )
    return ma144._md_table(headers, rows)


def _blocked_table_lines(blocked: Sequence[Mapping[str, Any]]) -> list[str]:
    headers = (
        "代號", "證券", "策略", "T", "T 收盤", "V 樞紐低點", "風險寬度",
        "門檻", "門檻狀態", "MA144 基準出場原因", "MA144 基準費後損益",
        "基準持有官方交易日", "基準重進",
    )
    rows: list[Sequence[Any]] = []
    for row in blocked:
        rows.append(
            (
                row["code"],
                row["symbol"],
                row["strategy"],
                row["entry_signal_date_T"],
                row["entry_reference_close"],
                row["risk_width_pivot_low"],
                row["risk_width_value"],
                row["risk_width_threshold"],
                row["risk_width_state"],
                row["baseline_exit_reason"],
                row["baseline_costed_net_pnl"],
                row["baseline_hold_sessions_transacted"],
                "是" if row["baseline_is_reentry"] else "否",
            )
        )
    return ma144._md_table(headers, rows)


def _blocked_open_table_lines(blocked: Sequence[Mapping[str, Any]]) -> list[str]:
    headers = (
        "代號", "證券", "策略", "T", "風險寬度", "門檻", "門檻狀態",
        "基準狀態", "基準是否可能為再進場",
    )
    rows: list[Sequence[Any]] = []
    for row in blocked:
        rows.append(
            (
                row["code"],
                row["symbol"],
                row["strategy"],
                row["entry_signal_date_T"],
                row["risk_width_value"],
                row["risk_width_threshold"],
                row["risk_width_state"],
                "未平倉（基準期末仍持有，不計入已平倉損益）",
                "是" if row["baseline_is_reentry"] else "否",
            )
        )
    return ma144._md_table(headers, rows)


def _spot_check_lines(spot_checks: Mapping[str, Any]) -> list[str]:
    lines: list[str] = []
    accepted = spot_checks.get("accepted_d_e_v_t") or []
    rw_blocked = spot_checks.get("risk_width_blocked_d_e_v_t") or []
    ma144_blocked = spot_checks.get("ma144_blocked_d_e_v_t") or []
    hand = spot_checks.get("hand_cost_recheck")
    lines.append("### 通過雙門檻的真實 D -> E -> V -> T 抽樣（最多 2 筆）")
    lines.append("")
    if not accepted:
        lines.append("本次樣本找不到符合條件的已通過鏈，如實說明。")
    else:
        lines.append(
            "| D | E | V | T | 進場價 | 股數 | V 樞紐低點 | 風險寬度 | "
            "MA144(T) | MA144(T-20) |"
        )
        lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        for row in accepted:
            lines.append(
                "| {0} | {1} | {2} | {3} | {4} | {5} | {6} | {7} | {8} | {9} |".format(
                    row["source_selection_date_D"], row["monitor_effective_date_E"],
                    row["pivot_confirmed_date"], row["entry_signal_date_T"],
                    row["entry_fill_price"], row["shares"], row["risk_width_pivot_low"],
                    row["risk_width_value"], row["gate_sma144_at_signal"],
                    row["gate_sma144_at_reference"],
                )
            )
    lines.append("")
    lines.append("### 被 7% 風險寬度拒絕的真實 D -> E -> V -> T 抽樣（該筆在新版本不存在）")
    lines.append("")
    if not rw_blocked:
        lines.append("本次樣本找不到可對應 MA144 基準 leg 的風險寬度拒絕鏈，如實說明。")
    else:
        lines.append(
            "| D | E | V | T | T 收盤 | V 樞紐低點 | 風險寬度 | 門檻 | "
            "MA144 基準費後損益 |"
        )
        lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        for row in rw_blocked:
            lines.append(
                "| {0} | {1} | {2} | {3} | {4} | {5} | {6} | {7} | {8} |".format(
                    row["source_selection_date_D"], row["monitor_effective_date_E"],
                    row["risk_width_pivot_confirmed_date"], row["entry_signal_date_T"],
                    row["entry_reference_close"], row["risk_width_pivot_low"],
                    row["risk_width_value"], row["risk_width_threshold"],
                    row["baseline_costed_net_pnl"],
                )
            )
    lines.append("")
    lines.append("### MA144 先行：被 MA144 拒絕的真實 D -> E -> T 抽樣（風險寬度不決定）")
    lines.append("")
    if not ma144_blocked:
        lines.append("本次樣本找不到 MA144 拒絕鏈，如實說明。")
    else:
        lines.append("| D | E | T | T 收盤 | MA144(T) | MA144(T-20) | 參考日 | 決定門檻 |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
        for row in ma144_blocked:
            lines.append(
                "| {0} | {1} | {2} | {3} | {4} | {5} | {6} | {7} |".format(
                    row["source_selection_date_D"], row["monitor_effective_date_E"],
                    row["entry_signal_date_T"], row["entry_reference_close"],
                    row["gate_sma144_at_signal"], row["gate_sma144_at_reference"],
                    row["gate_reference_date"], row["deciding_gate"],
                )
            )
    lines.append("")
    lines.append("### 成本手算")
    lines.append("")
    if hand is None:
        lines.append("無可手算的成交 leg。")
    else:
        lines.append("以凍結成本模型對 {0} 重算：".format(hand["leg_key"]))
        lines.append("")
        lines.append("| 項目 | 手算 | 交易列 |")
        lines.append("| --- | --- | --- |")
        lines.append("| 買進價 | {0} | {1} |".format(hand["entry_fill_price"], hand["entry_fill_price"]))
        lines.append("| 買進毛額 | {0} | {1} |".format(hand["hand_buy_gross"], hand["row_buy_gross"]))
        lines.append("| 買進手續費 | {0} | {1} |".format(hand["hand_buy_commission"], hand["row_buy_commission"]))
        lines.append("| 買進總成本 | {0} | {1} |".format(hand["hand_buy_total"], hand["row_buy_total"]))
        lines.append("| 賣出毛額 | {0} | {1} |".format(hand["hand_sell_gross"], hand["row_sell_gross"]))
        lines.append("| 賣出手續費 | {0} | {1} |".format(hand["hand_sell_commission"], hand["row_sell_commission"]))
        lines.append("| 證交稅 | {0} | {1} |".format(hand["hand_sell_tax"], hand["row_sell_tax"]))
        lines.append("| 賣出淨額 | {0} | {1} |".format(hand["hand_sell_net"], hand["row_sell_net"]))
        lines.append("| 費後損益 | {0} | {1} |".format(hand["hand_costed_net_pnl"], hand["row_costed_net_pnl"]))
        lines.append("")
        lines.append("買進相符 {0}；賣出相符 {1}；淨損益相符 {2}。".format(
            hand["buy_matches"], hand["sell_matches"], hand["net_matches"],
        ))
    lines.append("")
    lines.append(
        "最小測試（tests/backtests/dual_ma/test_h2_ma144_risk_width_entry_2022h2.py）涵蓋："
        "7% 等號通過與低於拒絕、因果樞紐（確認日不得晚於 T）、MA144 先行、"
        "缺值 HOLD、首次進場與再進場同受條件、成本與未平倉分帳。"
    )
    lines.append("")
    return lines


def render_report_zh(
    *,
    manifest: Mapping[str, Any],
    summary: Sequence[Mapping[str, Any]],
    comparison_ma144: Mapping[str, Any],
    comparison_flat: Mapping[str, Any],
    analysis: Mapping[str, Any],
    groups: Mapping[str, Any],
    open_positions: Mapping[str, Any],
    corp_summary: Mapping[str, Any],
    corp_open: Mapping[str, Any],
    metrics_tables: Sequence[str],
    detail_rows: Sequence[Mapping[str, Any]],
    gate_rows: Sequence[Mapping[str, Any]],
    spot_checks: Mapping[str, Any],
    git_state: Mapping[str, Any],
    preflight: Mapping[str, Any],
) -> str:
    lines: list[str] = []
    lines.append("# 2022 H2 雙均線：MA144 上行＋樞紐風險寬度 7% 進場門檻探索回測 v1（02 角色）")
    lines.append("")
    lines.append("- run_id：`{0}»".format(manifest["run_id"]))
    lines.append("- variant_id：`{0}»".format(manifest["variant_id"]))
    lines.append("- 狀態：`{0}»（readback {1}）".format(manifest["status"], manifest["readback_status"]))
    lines.append("- research_only：true；point_in_time_verified：false；不得當正式可交易歷史報酬")
    lines.append("- 分支 {0}、HEAD {1}".format(
        git_state.get("branch"), git_state.get("head_commit")))
    lines.append("")
    lines.append("## 一、本輪唯一變因")
    lines.append("")
    lines.append(
        "在已凍結的「出場後可再進場」flat re-entry 規則與已驗收的 MA144 上行進場門檻之上，"
        "只新增一個進場門檻：每次原狀態機在 T 發出完整可買訊號時（含出場後再進場），還須"
    )
    lines.append(
        "`risk_width = (T 收盤進場代理價 - 已因果確認的 V 樞紐低點) / T 收盤進場代理價 >= 0.07»。"
        "門檻為事前固定的整數 7%，通過為大於或等於，等號視為通過、低於視為拒絕。"
    )
    lines.append(
        "判定順序固定：先判 MA144 上行門檻（`SMA144(T) > SMA144(T - 20 個官方交易日)»），"
        "只有 MA144 已通過者才進入風險寬度判定；因此被 MA144 擋下的 T，決定門檻一律記為 "
        "MA144_UPWARD，風險寬度記為 NOT_EVALUATED，僅保留其可得的數值供歸因。"
    )
    lines.append(
        "兩個門檻都只擋當次買入，不改 D 選股、E 啟動、V 樞紐、出場或 episode 生命週期；"
        "T 不合格時不得隔日補買，也不得使用 T 之後的資料。"
    )
    lines.append(
        "缺 T 進場價、非正進場價或無已確認樞紐低點時記為 "
        "`HOLD/ENTRY_GATE_UNDETERMINED»，不執行該筆買入。"
    )
    lines.append("")
    lines.append("## 二、固定輸入與基準核對")
    lines.append("")
    inputs_block = manifest["inputs"]
    coverage = manifest["coverage"]
    source = manifest["source_sha256"]
    lines.append("| 項目 | 值 |")
    lines.append("| --- | --- |")
    lines.append("| 2022 H2 官方選股日 | {0} 日（{1} 至 {2}） |".format(
        coverage["reported_date_count"], coverage["first_date"], coverage["last_date"]))
    lines.append("| 01 月報 SHA 全數符合 | {0} |".format(
        inputs_block["parent_runs"]["all_matched"]))
    lines.append("| 價格快照 content SHA-256 | `{0}» |".format(
        inputs_block["price_snapshot_content_sha256"]))
    lines.append("| 官方日曆 raw SHA-256 | `{0}» |".format(
        inputs_block["official_calendar"]["raw_sha256"]))
    lines.append("| MA144 基準 run | `{0}»（trades.csv SHA-256 未變：{1}） |".format(
        inputs_block["baseline_ma144_run"]["run_id"],
        inputs_block["baseline_ma144_run"]["trades_sha256_unchanged"]))
    lines.append("| MA144 基準 trades.csv SHA-256 | `{0}» |".format(
        inputs_block["baseline_ma144_run"]["trades_sha256"]))
    lines.append("| 原 flat re-entry 基準 run | `{0}»（trades.csv SHA-256 未變：{1}） |".format(
        inputs_block["baseline_flat_run"]["run_id"],
        inputs_block["baseline_flat_run"]["trades_sha256_unchanged"]))
    lines.append("| 本輪規格 SHA-256 | `{0}»（{1} bytes） |".format(
        source["risk_width_contract_sha256"], source["risk_width_contract_bytes"]))
    lines.append("| MA144 規格 SHA-256 | `{0}» |".format(
        source["ma144_gate_contract_sha256"]))
    lines.append("| flat re-entry 規格 SHA-256 | `{0}» |".format(
        source["flat_reentry_contract_sha256"]))
    lines.append("")
    lines.append("## 三、規則與績效盲化聲明")
    lines.append("")
    lines.append(
        "本輪只重播 2022-07-01 至 2022-12-30；未讀取 2023/2024 績效，未依結果調整門檻或挑選日期，"
        "未改動 00/01 規格、既有 run 或正式規則，也未部署或觸及 main / live / Telegram。"
    )
    lines.append(
        "7% 為事前固定，非事後挑選最佳績效；研究者已看過同一 H2 樣本的事後敏感度，"
        "故本輪即使由負轉正仍屬樣本內探索，不得宣稱獨立驗證或可交易正期望值。負值照實呈現。"
    )
    lines.append(
        "中期 5/21 與長期 21/144 為兩個獨立策略；任何把兩者或六個選股組相加的數字都只是描述性彙總，"
        "不是單一資金帳戶績效，也不臆造總帳戶報酬、尖峰資金或最大回撤。"
    )
    lines.append("")
    lines.append("## 四、門檻結果總覽（主口徑 0 bps）")
    lines.append("")
    lines.append("MA144 上行門檻狀態：")
    lines.append("")
    lines.append("| MA144 狀態 | 計數 |")
    lines.append("| --- | --- |")
    for state in (STATE_PASS, STATE_NOT_UP, STATE_UNDETERMINED_MA144):
        lines.append("| {0} | {1} |".format(
            state, analysis["ma144_state_counts"].get(state, 0)))
    lines.append("")
    lines.append("風險寬度門檻狀態（含未被評估者）：")
    lines.append("")
    lines.append("| 風險寬度狀態 | 計數 |")
    lines.append("| --- | --- |")
    for state in (STATE_RW_PASS, STATE_RW_BELOW, STATE_RW_UNDETERMINED, STATE_RW_NOT_EVALUATED):
        lines.append("| {0} | {1} |".format(
            state, analysis["risk_width_state_counts"].get(state, 0)))
    lines.append("")
    lines.append("決定門檻計數：{0}".format(analysis["deciding_gate_counts"]))
    lines.append("")
    lines.append("最終狀態計數：")
    lines.append("")
    lines.append("| 最終狀態 | 計數 |")
    lines.append("| --- | --- |")
    for status, count in sorted(analysis["status_counts"].items()):
        lines.append("| {0} | {1} |".format(status, count))
    lines.append("")
    lines.append("| 策略 | 通過筆數 | MA144 未上行 | MA144 HOLD | 風險寬度拒絕 | 風險寬度 HOLD | 被擋中再進場 | 被擋中初進場 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for strategy in ATTR_STRATEGIES:
        row = analysis["per_strategy"][strategy]
        lines.append("| {0} | {1} | {2} | {3} | {4} | {5} | {6} | {7} |".format(
            attribution.STRATEGY_LABEL_ZH.get(strategy, strategy),
            row["accepted_legs"], row["ma144_not_up"], row["ma144_hold"],
            row["risk_width_below"], row["risk_width_hold"],
            row["blocked_would_be_reentry"], row["blocked_first_entry"]))
    lines.append("")
    lines.append(
        "本次新增風險寬度門檻共擋下 {0} 個原 MA144 基準可買的訊號；其中 {1} 在 MA144 基準有對應已成交 leg，"
        "{2} 無對應已成交 leg（多為基準以再進場拒絕、本輪因前置被擋而訊號角色改變者）。".format(
            analysis["risk_width_blocked_signal_total"],
            analysis["blocked_matched_leg_count"],
            analysis["blocked_without_baseline_leg"]))
    lines.append(
        "另有 {0} 個訊號由 MA144 門檻先行擋下（與 MA144 基準一致，非本輪新增）。".format(
            analysis["ma144_blocked_signal_total"]))
    lines.append("")
    lines.append("拒絕原因碼（MA144）：{0}".format(analysis["ma144_reason_counts"]))
    lines.append("")
    lines.append("拒絕原因碼（風險寬度）：{0}".format(analysis["risk_width_reason_counts"]))
    lines.append("")
    lines.append("## 五、同基準比較（0 bps）")
    lines.append("")
    if manifest.get("episode_limit") is not None:
        lines.append(
            "注意：此 run 使用 episode_limit={0} 的截斷樣本，基準欄仍為完整 run，兩欄不可直接相比。".format(
                manifest["episode_limit"]))
        lines.append("")
    lines.extend(metrics_tables)
    lines.append("### 10 / 20 bps 費後淨損益（對比 MA144 基準）")
    lines.append("")
    lines.append("| 策略 | bps | 新版本淨損益 | MA144 基準淨損益 | 差異 |")
    lines.append("| --- | --- | --- | --- | --- |")
    for pair in comparison_ma144["pairs"]:
        if pair["strategy"] == DESCRIPTIVE_BOTH:
            continue
        lines.append("| {0} | {1} | {2} | {3} | {4} |".format(
            attribution.STRATEGY_LABEL_ZH.get(pair["strategy"], pair["strategy"]),
            pair["slippage_bps"],
            ma144._num(pair["reentry"]["costed_net_pnl_total"]),
            ma144._num(pair["baseline"]["costed_net_pnl_total"]),
            ma144._num(pair["delta"]["costed_net_pnl_total"])))
    lines.append("")
    lines.append("兩策略相加（描述性、不可當單一帳戶）：")
    lines.append("")
    lines.append("| bps | 新版本淨損益 | MA144 基準淨損益 | 差異 |")
    lines.append("| --- | --- | --- | --- |")
    for pair in comparison_ma144["pairs"]:
        if pair["strategy"] != DESCRIPTIVE_BOTH:
            continue
        lines.append("| {0} | {1} | {2} | {3} |".format(
            pair["slippage_bps"],
            ma144._num(pair["reentry"]["costed_net_pnl_total"]),
            ma144._num(pair["baseline"]["costed_net_pnl_total"]),
            ma144._num(pair["delta"]["costed_net_pnl_total"])))
    lines.append("")
    lines.append("### 對照原 flat re-entry 基準（0 / 10 / 20 bps，費後）")
    lines.append("")
    lines.append("| 策略 | bps | 新版本淨損益 | flat re-entry 基準淨損益 | 差異 |")
    lines.append("| --- | --- | --- | --- | --- |")
    for pair in comparison_flat["pairs"]:
        if pair["strategy"] == DESCRIPTIVE_BOTH:
            continue
        lines.append("| {0} | {1} | {2} | {3} | {4} |".format(
            attribution.STRATEGY_LABEL_ZH.get(pair["strategy"], pair["strategy"]),
            pair["slippage_bps"],
            ma144._num(pair["reentry"]["costed_net_pnl_total"]),
            ma144._num(pair["baseline"]["costed_net_pnl_total"]),
            ma144._num(pair["delta"]["costed_net_pnl_total"])))
    lines.append("")
    lines.append("| 策略 | bps | 新版本平均每筆 | flat 基準平均每筆 | 新版本筆數 | flat 基準筆數 |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for pair in comparison_flat["pairs"]:
        if pair["strategy"] == DESCRIPTIVE_BOTH:
            continue
        new_mean = pair["reentry"].get("mean_costed_net_pnl")
        old_mean = pair["baseline"].get("mean_costed_net_pnl")
        lines.append("| {0} | {1} | {2} | {3} | {4} | {5} |".format(
            attribution.STRATEGY_LABEL_ZH.get(pair["strategy"], pair["strategy"]),
            pair["slippage_bps"],
            ma144._num(new_mean), ma144._num(old_mean),
            pair["reentry_fill_count"], pair["baseline_fill_count"]))
    lines.append("")
    lines.append("## 六、六個選股組（多標籤，不可相加成帳戶）")
    lines.append("")
    lines.append("| 組別 | 筆數(0bps) | 已平倉 | 未平倉 | 勝筆 | 勝率 | 費後損益 | 平均每筆 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for row in groups["available_group_rows"]:
        closed_n = int(row["closed_leg_count"])
        total = row["costed_net_pnl_total"]
        mean = (
            None
            if not closed_n or total is None
            else (Decimal(str(total)) / Decimal(closed_n)).quantize(Decimal("0.0001"))
        )
        lines.append("| {0} | {1} | {2} | {3} | {4} | {5} | {6} | {7} |".format(
            row["group_label_zh"], row["leg_count_primary_bps"], row["closed_leg_count"],
            row["open_leg_count"], row["win_count"], ma144._pct(row["win_rate"]),
            ma144._num(row["costed_net_pnl_total"]), ma144._num(mean)))
    for row in groups["unavailable_group_rows"]:
        lines.append("| {0} | 無候選 | 無候選 | 無候選 | - | - | - | - |".format(
            row["group_label_zh"]))
    lines.append("")
    lines.append("多標籤 leg 數：{0}；{1}".format(
        groups["multi_label_leg_count"], groups["multi_label_note"]))
    lines.append("")
    lines.append("## 七、贏家／輸家與被風險寬度擋下的贏家／輸家")
    lines.append("")
    lines.append("### 新版本前十贏家（0 bps，費後）")
    lines.append("")
    lines.append("| 代號 | 策略 | T | 出場日 | 出場原因 | 費後損益 |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for row in manifest["top_movers"]["winners"]:
        lines.append("| {0} | {1} | {2} | {3} | {4} | {5} |".format(
            row["code"], row["strategy"], row["entry_signal_date_T"],
            row["exit_date"], row["exit_reason"], row["costed_net_pnl"]))
    lines.append("")
    lines.append("### 新版本前十輸家（0 bps，費後）")
    lines.append("")
    lines.append("| 代號 | 策略 | T | 出場日 | 出場原因 | 費後損益 |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for row in manifest["top_movers"]["losers"]:
        lines.append("| {0} | {1} | {2} | {3} | {4} | {5} |".format(
            row["code"], row["strategy"], row["entry_signal_date_T"],
            row["exit_date"], row["exit_reason"], row["costed_net_pnl"]))
    lines.append("")
    lines.append("### 被 7% 風險寬度擋下、且在 MA144 基準有對應成交 leg 的贏家")
    lines.append("")
    lines.extend(_blocked_table_lines(analysis["blocked_winners"]))
    lines.append("")
    lines.append("### 被 7% 風險寬度擋下的輸家")
    lines.append("")
    lines.extend(_blocked_table_lines(analysis["blocked_losers"]))
    lines.append("")
    lines.append("### 被 7% 風險寬度擋下、MA144 基準仍為未平倉者")
    lines.append("")
    lines.extend(_blocked_open_table_lines(analysis.get("blocked_open", ())))
    lines.append("")
    lines.append(
        "被擋且有對應 MA144 基準 leg 的已平倉合計 {0} 筆（贏家 {1}、輸家 {2}）、"
        "MA144 基準仍為未平倉 {3} 筆。".format(
            analysis["blocked_closed_count"], analysis["blocked_winner_count"],
            analysis["blocked_loser_count"], analysis["blocked_open_count"]))
    lines.append(
        "已平倉被擋 leg 的 MA144 基準費後損益合計 {0}（費前 {1}）。此數字只描述 MA144 基準中被本門檻移除的交易，"
        "不屬於新版本績效；比較時必須區分「總虧損因交易變少而縮小」與「平均每筆期望值改善」。".format(
            ma144._num(analysis["blocked_costed_net_pnl_total"]),
            ma144._num(analysis["blocked_gross_pnl_total"])))
    lines.append("")
    lines.append("## 八、未平倉與未解權息分列")
    lines.append("")
    lines.append("| 項目 | 中期 5/21 | 長期 21/144 |")
    lines.append("| --- | --- | --- |")
    lines.append("| 期末未平倉筆數 | {0} | {1} |".format(
        open_positions["medium_open_count"], open_positions["long_open_count"]))
    lines.append("| 未平倉標記市值淨損益（僅估算） | {0} | {1} |".format(
        ma144._num(open_positions["medium_mark_to_market_net_pnl"]),
        ma144._num(open_positions["long_mark_to_market_net_pnl"])))
    lines.append("| 未平倉中屬再進場 | {0} |".format(
        corp_open["open_reentry_position_count"]))
    lines.append("")
    lines.append("公司行動（0 bps leg，保守分類；應收股利／配股與賣出現金分列）：")
    lines.append("")
    lines.append("| 分類 | 筆數 |")
    lines.append("| --- | --- |")
    for status, count in sorted(corp_summary["status_counts"].items()):
        lines.append("| {0} | {1} |".format(status, count))
    lines.append("")
    lines.append("可解讀修正：{0} 筆；未解（未還原，留在原始基準）：{1} 筆；無法重現：{2} 筆。".format(
        corp_summary["resolved_leg_count"], corp_summary["unresolved_leg_count"],
        corp_summary["not_reproduced_leg_count"]))
    lines.append("未解 legs 的原始費後損益合計（不得混入已平倉淨損益）：{0}".format(
        corp_summary["raw_baseline_of_unresolved_legs"]))
    flows = corp_summary["corrected_flows"]
    lines.append(
        "已解 legs：賣出現金淨額 {0}、應收現金股利 {1}、應收配股 {2} 股（期末價值 {3}）、實現淨損益 {4}。".format(
            flows["sale_cash_net_total"], flows["cash_dividend_receivable_total"],
            flows["stock_receivable_shares_total"], flows["stock_receivable_value_total"],
            flows["realized_net_pnl_total"]))
    lines.append("")
    lines.append("## 九、抽查與最小測試")
    lines.append("")
    lines.extend(_spot_check_lines(spot_checks))
    lines.append("")
    lines.append("## 十、0 bps 逐筆交易明細（全部）")
    lines.append("")
    lines.extend(_detail_table_lines(detail_rows))
    lines.append("")
    lines.append("## 十一、門檻拒絕／HOLD 明細（0 bps，全部）")
    lines.append("")
    lines.extend(_gate_table_lines(gate_rows))
    lines.append("")
    lines.append("## 十二、Manifest、SHA 與 readback")
    lines.append("")
    lines.append("| artifact | SHA-256 |")
    lines.append("| --- | --- |")
    for name, digest in sorted(manifest["artifact_sha256"].items()):
        lines.append("| {0} | `{1}» |".format(name, digest))
    lines.append("")
    lines.append("| gate | 狀態 |")
    lines.append("| --- | --- |")
    for name, value in sorted(manifest["gates"].items()):
        lines.append("| {0} | {1} |".format(name, value))
    lines.append("")
    lines.append("完整機讀資料另見同目錄 manifest.json 與 readback.json。")
    lines.append("")
    return "\n".join(lines)



# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def run_h2_risk_width(
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
        raise H2RiskWidthError(
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
    integration_root = repo_root / ".worktrees/dual-ma-integration-v0.4.1"
    risk_spec_path = integration_root / SPEC_RELATIVE_PATH_RISK_WIDTH
    if not risk_spec_path.is_file():
        raise H2RiskWidthError(f"frozen risk-width spec is missing: {risk_spec_path}")
    risk_spec_bytes = risk_spec_path.read_bytes()
    risk_spec_sha = pilot.sha256_bytes(risk_spec_bytes).upper()
    if risk_spec_sha != SPEC_SHA256_RISK_WIDTH:
        raise H2RiskWidthError(
            f"risk-width spec sha256 {risk_spec_sha} != {SPEC_SHA256_RISK_WIDTH}"
        )
    if len(risk_spec_bytes) != SPEC_BYTE_SIZE_RISK_WIDTH:
        raise H2RiskWidthError(
            "risk-width spec byte size "
            f"{len(risk_spec_bytes)} != {SPEC_BYTE_SIZE_RISK_WIDTH}"
        )
    ma144_spec_path = integration_root / ma144.SPEC_RELATIVE_PATH
    if not ma144_spec_path.is_file():
        raise H2RiskWidthError(f"frozen MA144 spec is missing: {ma144_spec_path}")
    ma144_spec_bytes = ma144_spec_path.read_bytes()
    ma144_spec_sha = pilot.sha256_bytes(ma144_spec_bytes).upper()
    if ma144_spec_sha != ma144.SPEC_SHA256_MA144:
        raise H2RiskWidthError(
            f"MA144 spec sha256 {ma144_spec_sha} != {ma144.SPEC_SHA256_MA144}"
        )
    if len(ma144_spec_bytes) != ma144.SPEC_BYTE_SIZE_MA144:
        raise H2RiskWidthError(
            f"MA144 spec byte size {len(ma144_spec_bytes)} != {ma144.SPEC_BYTE_SIZE_MA144}"
        )
    flat_spec_path = integration_root / (
        "docs/strategies/dual_ma/2022-h2-flat-reentry-v-pivot-exploration-v1.md"
    )
    if not flat_spec_path.is_file():
        raise H2RiskWidthError(f"frozen flat re-entry spec is missing: {flat_spec_path}")
    flat_spec_bytes = flat_spec_path.read_bytes()
    flat_spec_sha = pilot.sha256_bytes(flat_spec_bytes).upper()
    if flat_spec_sha != ma144.SPEC_SHA256_BASE_REENTRY:
        raise H2RiskWidthError(
            f"flat re-entry spec sha256 {flat_spec_sha} != {ma144.SPEC_SHA256_BASE_REENTRY}"
        )
    if len(flat_spec_bytes) != ma144.SPEC_BYTE_SIZE_BASE_REENTRY:
        raise H2RiskWidthError(
            "flat re-entry spec byte size "
            f"{len(flat_spec_bytes)} != {ma144.SPEC_BYTE_SIZE_BASE_REENTRY}"
        )

    baseline_ma144_dir = workdata_root / BASELINE_MA144_RUN_RELATIVE
    baseline_ma144_trades = baseline_ma144_dir / "trades.csv"
    baseline_ma144_open = baseline_ma144_dir / "year_end_open_positions.json"
    baseline_ma144_sha = pilot.sha256_file(baseline_ma144_trades).upper()
    if baseline_ma144_sha != BASELINE_MA144_TRADES_SHA256:
        raise H2RiskWidthError(
            f"MA144 baseline trades.csv sha256 {baseline_ma144_sha} "
            f"!= {BASELINE_MA144_TRADES_SHA256}"
        )
    if pilot.sha256_file(baseline_ma144_open).upper() != BASELINE_MA144_OPEN_SHA256:
        raise H2RiskWidthError("MA144 baseline open-positions sha256 mismatch")
    ma144_csv_rows = flat._read_csv_rows(baseline_ma144_trades)

    baseline_flat_dir = workdata_root / BASELINE_FLAT_RUN_RELATIVE
    baseline_flat_trades = baseline_flat_dir / "trades.csv"
    baseline_flat_open = baseline_flat_dir / "year_end_open_positions.json"
    baseline_flat_sha = pilot.sha256_file(baseline_flat_trades).upper()
    if baseline_flat_sha != BASELINE_FLAT_TRADES_SHA256:
        raise H2RiskWidthError(
            f"flat baseline trades.csv sha256 {baseline_flat_sha} "
            f"!= {BASELINE_FLAT_TRADES_SHA256}"
        )
    if pilot.sha256_file(baseline_flat_open).upper() != BASELINE_FLAT_OPEN_SHA256:
        raise H2RiskWidthError("flat baseline open-positions sha256 mismatch")
    flat_csv_rows = flat._read_csv_rows(baseline_flat_trades)

    worktree_root = Path(__file__).resolve().parents[2]
    commit_state = pilot.resolve_runner_commit(worktree_root)
    commit = runner_commit or commit_state["runner_commit"]
    state = flat.git_state(worktree_root)

    official_sessions = calendar.sessions
    run_dir.mkdir(parents=True, exist_ok=True)
    rows, gate_rows, stats = collect_trade_rows(
        plan=plan,
        snapshot=snapshot,
        code_programs_by_date=inputs["code_programs_by_date"],
        official_sessions=official_sessions,
        run_id=run_id,
        runner_commit=commit,
        progress=progress,
        episode_limit=episode_limit,
    )
    if progress is not None:
        progress("engine_done", stats["episode_count_run"], stats["episode_count_run"], stats)

    summary = h2.summarize_rows(rows)
    comparison_ma144 = flat.build_comparison(
        new_rows=rows, baseline_rows=ma144_csv_rows, baseline_sha256=baseline_ma144_sha
    )
    comparison_flat = flat.build_comparison(
        new_rows=rows, baseline_rows=flat_csv_rows, baseline_sha256=baseline_flat_sha
    )
    groups = h2.build_group_attribution(
        rows, code_programs_by_date=inputs["code_programs_by_date"]
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
    new_legs = ma144.zero_bps_legs_from_rows(rows, run_dir=run_dir)
    new_diag = attribution.build_leg_diagnostics(new_legs, sessions=sessions, prices=prices)
    ma144_zero_rows = tuple(
        row for row in ma144_csv_rows if row.get("slippage_bps") == "0"
    )
    if not ma144_zero_rows:
        raise H2RiskWidthError("MA144 baseline has no 0 bps legs to compare")
    baseline_legs = attribution.FrozenLegs(
        run_dir=baseline_ma144_dir,
        trades_sha256=baseline_ma144_sha,
        rows=ma144_zero_rows,
    )
    baseline_diag = attribution.build_leg_diagnostics(
        baseline_legs, sessions=sessions, prices=prices
    )
    metrics_tables: list[str] = []
    for strategy in ATTR_STRATEGIES:
        metrics_tables.extend(
            _metrics_table_lines(
                attribution.STRATEGY_LABEL_ZH.get(strategy, strategy),
                ma144.strategy_metrics(new_diag, strategy=strategy, sessions=sessions),
                ma144.strategy_metrics(baseline_diag, strategy=strategy, sessions=sessions),
            )
        )
    analysis = build_risk_width_analysis(
        gate_rows=gate_rows, rows=rows, baseline_diag=baseline_diag
    )
    spot_checks = build_spot_checks(
        rows=rows, analysis=analysis, gate_rows=gate_rows
    )

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

    threshold = RISK_WIDTH_THRESHOLD
    early_entries = [
        row for row in primary_rows
        if date.fromisoformat(row["entry_signal_date_T"])
        < date.fromisoformat(row["monitor_effective_date_E"])
    ]
    late_entries = [
        row for row in primary_rows
        if date.fromisoformat(row["entry_signal_date_T"])
        > date.fromisoformat(row["regime_window_end"])
    ]
    year_end_leaks = [
        row for row in primary_rows
        if date.fromisoformat(row["entry_signal_date_T"]) > AS_OF_CUTOFF
        or (
            row["exit_date"] is not None
            and date.fromisoformat(row["exit_date"]) > AS_OF_CUTOFF
        )
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
    entry_without_confirm = [
        row for row in primary_rows
        if row["pivot_confirmed_date"] is None or row["pivot_low"] is None
    ]
    entry_below_fast = [
        row for row in primary_rows
        if row["entry_fast_sma"] is None
        or Decimal(str(row["entry_reference_close"]))
        <= Decimal(str(row["entry_fast_sma"]))
    ]
    stop_not_pivot = [
        row for row in primary_rows
        if row["pivot_low"] is None or row["initial_stop"] != row["pivot_low"]
    ]
    backfilled_confirmation = [
        row for row in primary_rows
        if date.fromisoformat(row["pivot_confirmed_date"])
        > date.fromisoformat(row["entry_signal_date_T"])
    ]
    bad_reentry_order = [
        row for row in primary_rows
        if row["is_reentry"]
        and (
            row["prior_exit_date"] is None
            or date.fromisoformat(row["entry_signal_date_T"])
            <= date.fromisoformat(row["prior_exit_date"])
        )
    ]
    reentry_after_regime = [
        row for row in primary_rows
        if row["is_reentry"] and row["prior_exit_reason"] == ExitReason.REGIME_EXIT.value
    ]
    ma144_gate_missing_on_accepted = [
        row for row in primary_rows if row["gate_state"] != STATE_PASS
    ]
    ma144_not_strictly_up = [
        row for row in primary_rows
        if row["gate_sma144_at_signal"] is None
        or row["gate_sma144_at_reference"] is None
        or Decimal(str(row["gate_sma144_at_signal"]))
        <= Decimal(str(row["gate_sma144_at_reference"]))
    ]
    risk_width_not_met = [
        row for row in primary_rows
        if row["risk_width_value"] is None
        or Decimal(str(row["risk_width_value"])) < threshold
    ]
    risk_width_verdict_missing = [
        row for row in primary_rows
        if row["risk_width_state"] != STATE_RW_PASS or row["deciding_gate"] is not None
    ]
    reentry_risk_width_not_met = [
        row for row in primary_rows
        if row["is_reentry"]
        and (
            row["risk_width_value"] is None
            or Decimal(str(row["risk_width_value"])) < threshold
        )
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
    bar_dates_by_symbol = {
        symbol: {bar.date for bar in bars}
        for symbol, bars in snapshot.bars_by_symbol.items()
    }
    backfilled_reference_rows = [
        row for row in gate_rows
        if row["gate_state"] in (STATE_PASS, STATE_NOT_UP)
        and row["gate_reference_date"]
        and date.fromisoformat(row["gate_reference_date"])
        not in bar_dates_by_symbol.get(row["symbol"], set())
    ]

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
            "PASS" if inputs["universe_sha"] == h2.UNIVERSE_SOURCES_RAW_SHA256 else "FAIL"
        ),
        "risk_width_spec_matches_declared_sha256": (
            "PASS" if risk_spec_sha == SPEC_SHA256_RISK_WIDTH else "FAIL"
        ),
        "ma144_gate_spec_matches_declared_sha256": (
            "PASS" if ma144_spec_sha == ma144.SPEC_SHA256_MA144 else "FAIL"
        ),
        "flat_reentry_spec_matches_declared_sha256": (
            "PASS" if flat_spec_sha == ma144.SPEC_SHA256_BASE_REENTRY else "FAIL"
        ),
        "ma144_baseline_trades_byte_unchanged": (
            "PASS"
            if pilot.sha256_file(baseline_ma144_trades).upper()
            == BASELINE_MA144_TRADES_SHA256
            else "FAIL"
        ),
        "flat_baseline_trades_byte_unchanged": (
            "PASS"
            if pilot.sha256_file(baseline_flat_trades).upper()
            == BASELINE_FLAT_TRADES_SHA256
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
        "no_entry_after_its_episode_window_end": "PASS" if not late_entries else "FAIL",
        "no_trade_or_exit_after_2022_12_30": "PASS" if not year_end_leaks else "FAIL",
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
        "sizing_within_frozen_leg_limits": "PASS" if not bad_sizing else "FAIL",
        "zero_bps_fill_equals_signal_day_close": "PASS" if not fill_mismatch else "FAIL",
        "no_open_leg_carries_a_closed_leg_result": (
            "PASS" if not open_with_stats else "FAIL"
        ),
        "every_accepted_entry_carries_a_pass_ma144_gate_verdict": (
            "PASS" if not ma144_gate_missing_on_accepted else "FAIL"
        ),
        "every_accepted_entry_ma144_is_strictly_up": (
            "PASS" if not ma144_not_strictly_up else "FAIL"
        ),
        "every_accepted_entry_risk_width_at_least_threshold": (
            "PASS" if not risk_width_not_met else "FAIL"
        ),
        "every_reentry_leg_passed_the_risk_width_floor": (
            "PASS" if not reentry_risk_width_not_met else "FAIL"
        ),
        "every_accepted_entry_carries_a_pass_risk_width_verdict": (
            "PASS" if not risk_width_verdict_missing else "FAIL"
        ),
        "no_blocked_signal_became_a_filled_leg": (
            "PASS" if not blocked_but_filled else "FAIL"
        ),
        "no_gate_decision_uses_a_backfilled_reference_bar": (
            "PASS" if not backfilled_reference_rows else "FAIL"
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
    }
    if episode_limit is not None:
        gates["full_129_day_sample"] = "FAIL"
    readback_status = "PASS" if all(value == "PASS" for value in gates.values()) else "HOLD"

    trade_rows = [[row.get(name) for name in TRADE_ROW_FIELDS] for row in rows]
    gate_row_values = [
        [row.get(name) for name in GATE_DECISION_FIELDS] for row in gate_rows_sorted
    ]
    summary_rows = [[row.get(name) for name in h2.SUMMARY_FIELDS] for row in summary]
    payloads: dict[str, bytes] = {
        "trades.csv": h2._csv_bytes(TRADE_ROW_FIELDS, trade_rows),
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
        "gate_decisions.csv": h2._csv_bytes(GATE_DECISION_FIELDS, gate_row_values),
        "risk_width_analysis.json": pilot._json_bytes(analysis),
        "comparison_ma144.json": pilot._json_bytes(comparison_ma144),
        "comparison_flat_reentry.json": pilot._json_bytes(comparison_flat),
        "group_attribution.json": pilot._json_bytes(groups),
        "top_movers.json": pilot._json_bytes(top_movers),
        "exit_reason_breakdown.json": pilot._json_bytes(exit_reasons),
        "year_end_open_positions.json": pilot._json_bytes(open_positions),
        "corpaction_summary.json": pilot._json_bytes(corp_summary),
        "corpaction_open_positions.json": pilot._json_bytes(corp_open),
        "spot_checks.json": pilot._json_bytes(spot_checks),
        "parent_runs.json": pilot._json_bytes(inputs["parent"]),
        "preflight.json": pilot._json_bytes(preflight),
    }
    artifact_hashes = {name: pilot.sha256_bytes(blob) for name, blob in payloads.items()}
    source_sha256 = {
        "risk_width_contract_path": SPEC_RELATIVE_PATH_RISK_WIDTH,
        "risk_width_contract_sha256": risk_spec_sha,
        "risk_width_contract_bytes": len(risk_spec_bytes),
        "ma144_gate_contract_sha256": ma144_spec_sha,
        "ma144_gate_contract_bytes": len(ma144_spec_bytes),
        "flat_reentry_contract_sha256": flat_spec_sha,
        "flat_reentry_contract_bytes": len(flat_spec_bytes),
        "frozen_spec": "5A9E3A573178AA135FFBD960871D1ACEE20049F9ACF2DD7C117E1E2390B53DDA",
        "lifecycle_contract": (
            "361D5F35456FCA9B8BF00412CACBA4E2EF4173175ECDBB813D7600A977EB755E"
        ),
        "corporate_action_correction_contract": flat.corp.SPEC_SHA256,
        "price_snapshot_content": snapshot.content_sha256,
        "official_calendar_raw": calendar.raw_sha256,
        "baseline_ma144_trades_sha256": baseline_ma144_sha,
        "baseline_flat_trades_sha256": baseline_flat_sha,
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
        "variant_rule_delta": [
            "baseline: the frozen flat re-entry state machine, sizing, cost, ",
            "structural / DOW trail / regime exits and the 2022-12-30 cut-off, ",
            "plus the accepted MA144 upward entry gate",
            "ONE change: a complete buy signal at T additionally needs ",
            "risk_width = (T close - confirmed V pivot low) / T close >= 0.07; ",
            "the MA144 gate is decided first, a missing price or pivot records ",
            "HOLD/ENTRY_GATE_UNDETERMINED, and a rejected T is never bought later",
        ],
        "selection_program_modified": False,
        "sample_definition": (
            "the six accepted 01 selection runs for every official 2022 H2 "
            "session (2022-07-01 .. 2022-12-30); 2023 / 2024 data is never read"
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
            "baseline_ma144_run": {
                "run_id": BASELINE_MA144_RUN_ID,
                "trades_path": str(baseline_ma144_trades),
                "trades_sha256": baseline_ma144_sha,
                "trades_sha256_expected": BASELINE_MA144_TRADES_SHA256,
                "trades_sha256_unchanged": None,
            },
            "baseline_flat_run": {
                "run_id": BASELINE_FLAT_RUN_ID,
                "trades_path": str(baseline_flat_trades),
                "trades_sha256": baseline_flat_sha,
                "trades_sha256_expected": BASELINE_FLAT_TRADES_SHA256,
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
            "gate_decision_totals": stats["gate_decision_totals"],
            "gate_decision_primary_rows": stats["gate_decision_primary_rows"],
        },
        "stats": {
            "primary_slippage_bps": PRIMARY_SLIPPAGE_BPS,
            "slippage_scenarios": list(SLIPPAGE_SCENARIOS),
            "rows": summary,
        },
        "group_attribution": groups,
        "top_movers": top_movers,
        "exit_reason_breakdown": exit_reasons,
        "comparison_ma144": comparison_ma144,
        "comparison_flat_reentry": comparison_flat,
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

    baseline_ma144_unchanged = (
        pilot.sha256_file(baseline_ma144_trades).upper() == BASELINE_MA144_TRADES_SHA256
    )
    baseline_flat_unchanged = (
        pilot.sha256_file(baseline_flat_trades).upper() == BASELINE_FLAT_TRADES_SHA256
    )
    manifest["inputs"]["baseline_ma144_run"]["trades_sha256_unchanged"] = (
        baseline_ma144_unchanged
    )
    manifest["inputs"]["baseline_flat_run"]["trades_sha256_unchanged"] = (
        baseline_flat_unchanged
    )
    report = render_report_zh(
        manifest=manifest,
        summary=summary,
        comparison_ma144=comparison_ma144,
        comparison_flat=comparison_flat,
        analysis=analysis,
        groups=groups,
        open_positions=open_positions,
        corp_summary=corp_summary,
        corp_open=corp_open,
        metrics_tables=metrics_tables,
        detail_rows=detail_rows,
        gate_rows=gate_rows_sorted,
        spot_checks=spot_checks,
        git_state=state,
        preflight=preflight,
    )
    payloads["report_zh.md"] = report.encode("utf-8")
    for name, blob in payloads.items():
        (run_dir / name).write_bytes(blob)

    if pilot.sha256_file(baseline_ma144_trades).upper() != BASELINE_MA144_TRADES_SHA256:
        raise H2RiskWidthError("the MA144 baseline run changed during this run")
    if pilot.sha256_file(baseline_flat_trades).upper() != BASELINE_FLAT_TRADES_SHA256:
        raise H2RiskWidthError("the flat baseline run changed during this run")
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
        "gates": gates,
        "coverage": manifest["coverage"],
        "lifecycle": manifest["lifecycle"],
        "engine": manifest["engine"],
        "stats": manifest["stats"],
        "group_attribution": groups,
        "comparison_ma144": comparison_ma144,
        "comparison_flat_reentry": comparison_flat,
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
            "kind": "ATOMIC_H2_MA144_RISK_WIDTH",
            "state": "COMPLETE_AWAITING_00_QA",
            "resumable": False,
            "next_action": (
                "00 reviews the research_only MA144+7% risk-width variant and its "
                "same-basis comparison; no 2023/2024 performance is read and "
                "nothing is deployed"
            ),
        },
    }
    (run_dir / "readback.json").write_bytes(pilot._json_bytes(readback))
    return manifest


def _progress_printer(
    stage: str, done: int, total: int, extra: Mapping[str, Any]
) -> None:
    print(f"[{stage}] {done}/{total} {dict(extra)}", flush=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdata-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, default=None)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--episode-limit", type=int, default=None)
    parser.add_argument("--runner-commit", default=None)
    args = parser.parse_args(argv)
    manifest = run_h2_risk_width(
        workdata_root=args.workdata_root,
        output_root=args.output_root,
        run_id=args.run_id or RUN_ID_DEFAULT,
        episode_limit=args.episode_limit,
        runner_commit=args.runner_commit,
        progress=_progress_printer,
    )
    print(
        "run {r} status={s} episodes={e} leg_rows={l} gate_rejections={g}".format(
            r=manifest["run_id"],
            s=manifest["status"],
            e=manifest["episode_count_run"],
            l=manifest["engine"]["leg_rows"],
            g=manifest["engine"]["gate_decision_primary_rows"],
        )
    )
    return 0 if manifest["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
