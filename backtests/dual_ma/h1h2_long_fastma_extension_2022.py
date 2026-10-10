"""2022 H1/H2 causal replay of one added LONG-only entry gate (02 role).

Spec (read-only, 00-frozen):

* docs/strategies/dual_ma/2022-long-fastma-extension-entry-exploration-v1.md
  raw SHA-256 3AD6C5B6365F5E7780184A16A983A857D01B8705B90E42F6012A7AE77824B098
  (4848 bytes)

The only change against the already-accepted 2022 MA144-upward + confirmed
pivot risk-width >= 7% rule is on the LONG 21/144 strategy: every complete new
buy signal T that already cleared the MA144 upward gate and the 7% risk-width
floor must also satisfy

    fastma_extension = close_T / SMA21(T) - 1 >= 0.05

SMA21(T) is the trailing 21-session mean of the same symbol's own snapshot
closes up to and including T, so it is fully known at the T close: exactly the
close already used as the frozen T-close proxy.  There is no look-ahead, no
next-day backfill and no reuse of a rejected T.  >= 5% passes, < 5% is
REJECTED_EXTENSION_LT_5PCT, and a missing / non-positive SMA21 (or missing
close) records HOLD_EXTENSION_UNDETERMINED and buys nothing.  The MEDIUM 5/21
strategy is untouched and kept as a separate, byte-unchanged control.  The two
strategies are never summed into one account.

This gate and the integer 5% threshold were chosen after seeing the same 2022
sample grouped by the existing entry_fast_sma label.  It is an in-sample
exploration with explicit selection bias: even if the causal replay turns
positive it is NOT independent validation, and no 2023 / 2024 performance is
read.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import h1_full_exploration_2022h1 as h1x
from . import h1_ma144_risk_width_cross_period_2022h1 as h1rw
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


MODULE_VERSION = "dual-ma-h1h2-long-fastma-extension-2022-v1"
RUN_ID_DEFAULT = "h1h2-long-fastma-extension-2022-v1"
MANIFEST_SCHEMA_VERSION = "dual-ma-h1h2-long-fastma-extension-manifest-v1"
READBACK_SCHEMA_VERSION = "dual-ma-h1h2-long-fastma-extension-readback-v1"
PREFLIGHT_SCHEMA_VERSION = "dual-ma-h1h2-long-fastma-extension-preflight-v1"
EXTENSION_ANALYSIS_SCHEMA_VERSION = (
    "dual-ma-h1h2-long-fastma-extension-analysis-v1"
)

VARIANT_ID = (
    "FLAT_REENTRY_AFTER_EXIT_WITHIN_EPISODE+MA144_UPWARD_ENTRY_GATE"
    "+RISK_WIDTH_GE_7PCT_ENTRY_GATE"
    "+LONG_CLOSE_T_GE_SMA21_T_X105_ENTRY_GATE"
)

# The one added rule.
EXTENSION_RULE = "LONG_CLOSE_T_GE_SMA21_T_TIMES_1_05"
EXTENSION_THRESHOLD = Decimal("0.05")
EXTENSION_SMA_WINDOW = 21

STATE_EXT_PASS = "PASS"
STATE_EXT_BELOW = "BELOW"
STATE_EXT_UNDETERMINED = "UNDETERMINED"
STATE_EXT_NOT_EVALUATED = "NOT_EVALUATED"

STATUS_ENTRY_GATE_EXTENSION_BELOW = "ENTRY_GATE_EXTENSION_BELOW_5PCT"
STATUS_ENTRY_GATE_EXTENSION_UNDETERMINED = (
    "ENTRY_GATE_EXTENSION_UNDETERMINED"
)

DECIDING_EXTENSION = "SMA21_EXTENSION_GE_5PCT"

REASON_EXTENSION_PRICE_NOT_POSITIVE = "ENTRY_REFERENCE_PRICE_NOT_POSITIVE"
REASON_EXTENSION_SMA21_MISSING = "SMA21_MISSING_AT_T"
REASON_EXTENSION_SMA21_NOT_POSITIVE = "SMA21_NOT_POSITIVE_AT_T"
REASON_EXTENSION_EVIDENCE_MISSING = "EXTENSION_EVIDENCE_MISSING"

# The frozen 00 spec of this round.
SPEC_RELATIVE_PATH_EXTENSION = (
    "docs/strategies/dual_ma/2022-long-fastma-extension-entry-exploration-v1.md"
)
SPEC_SHA256_EXTENSION = (
    "3AD6C5B6365F5E7780184A16A983A857D01B8705B90E42F6012A7AE77824B098"
)
SPEC_BYTE_SIZE_EXTENSION = 4848

# Frozen baselines this round compares against and must not modify.
H1_BASELINE_RUN_ID = h1rw.RUN_ID_DEFAULT
H1_BASELINE_MANIFEST_SHA256 = (
    "04F7D85D805969AF2EAC7322A5D04C5D268E31F0B7F254006B2C5300C0D9FC65"
)
H1_BASELINE_TRADES_SHA256 = (
    "EFED95C264FBB7FB424889EA6BFBBF2E0E90DCA78A010EA539C593CA91DAD930"
)
H2_BASELINE_RUN_ID = rw.RUN_ID_DEFAULT
H2_BASELINE_MANIFEST_SHA256 = (
    "D5514B92CA1DEAA64D98CCE2BAE90B2962F682DEB978BE3B299BA81F98336496"
)
H2_BASELINE_TRADES_SHA256 = (
    "9C17D704ED7FBBDF5BBAE98BD8A59CF2B5DF55A411A1EFCDAB05C992E99EDC64"
)
H2_ERRATA_RUN_ID = "h2-ma144-risk-width-entry-2022h2-v2-report-errata"
H2_ERRATA_MANIFEST_SHA256 = (
    "0B247782A52F193AB71EF52686641D041D8B372E52A62852007771881DD563B8"
)
PRICE_SNAPSHOT_MANIFEST_SHA256 = (
    "924775B27CC3B250940B2E5879EA1992828EBC5BA6E93B66F598B9A2BF1BBF79"
)

AS_OF_CUTOFF = h2.AS_OF_CUTOFF
SLIPPAGE_SCENARIOS = rw.SLIPPAGE_SCENARIOS
PRIMARY_SLIPPAGE_BPS = rw.PRIMARY_SLIPPAGE_BPS
STRATEGIES = attribution.STRATEGIES
STRATEGY_LABEL_ZH = attribution.STRATEGY_LABEL_ZH

# Byte-unchanged checks re-used from the accepted H2 rule chain.
H2_RULE_CONTRACT_SHA256 = rw.SPEC_SHA256_RISK_WIDTH
H2_RULE_CONTRACT_BYTES = rw.SPEC_BYTE_SIZE_RISK_WIDTH
H2_RULE_CONTRACT_PATH = rw.SPEC_RELATIVE_PATH_RISK_WIDTH


class LongExtensionError(RuntimeError):
    """A structural problem with the long fastma-extension replay."""


# ---------------------------------------------------------------------------
# The one added rule: close_T / SMA21(T) - 1 >= 5% for the long strategy
# ---------------------------------------------------------------------------


def build_sma_map(
    *, dates: Sequence[date], closes: Sequence[Any], window: int = EXTENSION_SMA_WINDOW
) -> dict[date, Decimal | None]:
    """Causal trailing mean of one symbol's own closes, keyed by bar date.

    ``sma(day)`` only ever reads closes up to and including ``day``; a day with
    fewer than ``window`` bars available maps to ``None`` (warmup) rather than a
    shorter average.
    """

    if len(dates) != len(closes):
        raise LongExtensionError("sma map needs one close per bar date")
    if list(dates) != sorted(dates) or len(set(dates)) != len(dates):
        raise LongExtensionError("sma map bars must be unique and increasing")
    prefix = [Decimal(0)]
    for close in closes:
        prefix.append(prefix[-1] + Decimal(close))
    out: dict[date, Decimal | None] = {}
    for index, day in enumerate(dates):
        available = index + 1
        if available < window:
            out[day] = None
        else:
            out[day] = (prefix[available] - prefix[available - window]) / window
    return out


@dataclass(frozen=True)
class ExtensionEvaluation:
    """Outcome of the SMA21-extension test for one complete long signal."""

    state: str
    extension: Decimal | None
    close: Decimal | None
    sma21: Decimal | None
    reason: str | None = None

    @property
    def passed(self) -> bool:
        return self.state == STATE_EXT_PASS

    @property
    def decision_status(self) -> str:
        if self.state == STATE_EXT_BELOW:
            return STATUS_ENTRY_GATE_EXTENSION_BELOW
        if self.state == STATE_EXT_UNDETERMINED:
            return STATUS_ENTRY_GATE_EXTENSION_UNDETERMINED
        return STATUS_FILLED


def evaluate_extension(
    *,
    entry_reference_close: Any,
    sma21: Any,
    threshold: Decimal = EXTENSION_THRESHOLD,
) -> ExtensionEvaluation:
    """Causal ``close / SMA21 - 1 >= threshold`` with HOLD on missing inputs.

    Only the T close and the already computed SMA21(T) are read.  A missing or
    non-positive close, and a missing or non-positive SMA21, record UNDETERMINED
    (HOLD) instead of backfilling a value; the comparison is ``>=`` so the exact
    5% boundary passes.
    """

    close = None if entry_reference_close is None else Decimal(entry_reference_close)
    fast = None if sma21 is None else Decimal(sma21)
    if close is None or close <= 0:
        return ExtensionEvaluation(
            STATE_EXT_UNDETERMINED, None, close, fast,
            REASON_EXTENSION_PRICE_NOT_POSITIVE,
        )
    if fast is None:
        return ExtensionEvaluation(
            STATE_EXT_UNDETERMINED, None, close, fast,
            REASON_EXTENSION_SMA21_MISSING,
        )
    if fast <= 0:
        return ExtensionEvaluation(
            STATE_EXT_UNDETERMINED, None, close, fast,
            REASON_EXTENSION_SMA21_NOT_POSITIVE,
        )
    extension = close / fast - Decimal(1)
    state = STATE_EXT_PASS if extension >= Decimal(threshold) else STATE_EXT_BELOW
    return ExtensionEvaluation(state, extension, close, fast, None)


EXTENSION_ROW_FIELDS: tuple[str, ...] = (
    "extension_rule",
    "extension_threshold",
    "extension_sma_window",
    "extension_close",
    "extension_sma21",
    "extension_value",
    "extension_state",
    "extension_reason",
    "extension_deciding_gate",
    "extension_entry_gate_status",
)


def _extension_fields(
    evaluation: ExtensionEvaluation,
    *,
    deciding_gate: str | None,
    entry_gate_status: str,
) -> dict[str, Any]:
    return {
        "extension_rule": EXTENSION_RULE,
        "extension_threshold": h2._dstr(EXTENSION_THRESHOLD),
        "extension_sma_window": EXTENSION_SMA_WINDOW,
        "extension_close": h2._dstr(evaluation.close),
        "extension_sma21": h2._dstr(evaluation.sma21),
        "extension_value": (
            None
            if evaluation.extension is None
            else h2._dstr(evaluation.extension, 6)
        ),
        "extension_state": evaluation.state,
        "extension_reason": evaluation.reason,
        "extension_deciding_gate": deciding_gate,
        "extension_entry_gate_status": entry_gate_status,
    }


class LongExtensionEntryEngine(rw.RiskWidthEntryEngine):
    """MA144 upward + 7% risk width + a LONG-only SMA21 5% extension floor.

    ``_handle_entry_signal`` is extended by exactly one stage, appended after
    the two accepted gates: only for ``StrategyKind.LONG``, only when a signal
    would otherwise be filled, only after MA144 and the risk-width floor have
    both passed, the T close must be at least ``SMA21(T) * 1.05``.  Every frozen
    rejection path (addition while open, same-day rebuy, regime-invalidated
    episode) is untouched and a rejected signal never opens a leg, so no old T
    can be reused on a later session.  The MEDIUM strategy is never evaluated by
    the new stage and keeps its baseline decisions byte-for-byte.
    """

    def __init__(
        self,
        *,
        slippage_bps: int = 0,
        entry_variant: Any = rw.ENTRY_VARIANT,
        gate_map: Mapping[date, ma144.GateEvaluation] | None = None,
        official_sessions: Sequence[date] = (),
        back: int = ma144.GATE_BACK_SESSIONS,
        window: int = ma144.GATE_SMA_WINDOW,
        risk_width_threshold: Decimal = rw.RISK_WIDTH_THRESHOLD,
        extension_threshold: Decimal = EXTENSION_THRESHOLD,
        extension_sma_window: int = EXTENSION_SMA_WINDOW,
        extension_sma_map: Mapping[date, Decimal | None] | None = None,
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
            risk_width_threshold=risk_width_threshold,
            kd_2r_death_cross_exit=kd_2r_death_cross_exit,
            kd_values_by_date=kd_values_by_date,
        )
        self._extension_threshold = Decimal(extension_threshold)
        self._extension_sma_window = int(extension_sma_window)
        self._provided_extension_sma_map = (
            None if extension_sma_map is None else dict(extension_sma_map)
        )
        self._extension_sma_map: dict[date, Decimal | None] = {}

    def run(self, symbol: str, bars: Any, **kwargs: Any) -> Any:
        ordered = list(bars)
        self._extension_sma_map = (
            dict(self._provided_extension_sma_map)
            if self._provided_extension_sma_map is not None
            else build_sma_map(
                dates=[bar.date for bar in ordered],
                closes=[bar.close for bar in ordered],
                window=self._extension_sma_window,
            )
        )
        return super().run(symbol, ordered, **kwargs)

    def _extension_for(
        self, evidence: Mapping[str, Any], signal_date: date
    ) -> ExtensionEvaluation:
        return evaluate_extension(
            entry_reference_close=evidence.get("entry_reference_close"),
            sma21=self._extension_sma_map.get(signal_date),
            threshold=self._extension_threshold,
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
        if self._would_fill(state, bar) and state.kind == StrategyKind.LONG:
            gate = self._gate_for(bar.date)
            if gate.passed:
                risk_width = self._risk_width_for(evidence, bar.date)
                if risk_width.passed:
                    extension = self._extension_for(evidence, bar.date)
                    if not extension.passed:
                        previous = self._last_closed.get(state.kind)
                        fields = rw._risk_width_fields(
                            risk_width,
                            row_state=risk_width.state,
                            deciding_gate=DECIDING_EXTENSION,
                            entry_gate_status=extension.decision_status,
                        )
                        extension_fields = _extension_fields(
                            extension,
                            deciding_gate=DECIDING_EXTENSION,
                            entry_gate_status=extension.decision_status,
                        )
                        events.append(
                            {
                                "type": "ENTRY_DECISION",
                                "role": "MOTHER",
                                "status": extension.decision_status,
                                "variant_id": VARIANT_ID,
                                "hold": extension.state == STATE_EXT_UNDETERMINED,
                                "pullback_event_id": pullback_event_id,
                                "initial_stop": signal_stop,
                                "prior_leg_id": None if previous is None else previous[2],
                                "prior_exit_date": (
                                    None if previous is None else previous[0].isoformat()
                                ),
                                "prior_exit_reason": (
                                    None if previous is None else previous[1].value
                                ),
                                **rw._ma144_gate_fields(gate),
                                **fields,
                                **extension_fields,
                                **evidence,
                            }
                        )
                        return
                    extension_fields = _extension_fields(
                        extension,
                        deciding_gate=None,
                        entry_gate_status=STATUS_FILLED,
                    )
                    evidence = {**evidence, **extension_fields}
        super()._handle_entry_signal(
            result,
            state,
            bar,
            signal_stop,
            pullback_event_id,
            events,
            instrument_type,
            entry_evidence=evidence,
        )


# ---------------------------------------------------------------------------
# Replay plumbing (same shape as the accepted runs, one extra stage)
# ---------------------------------------------------------------------------

TRADE_ROW_FIELDS: tuple[str, ...] = rw.TRADE_ROW_FIELDS

EXTENSION_DECISION_FIELDS: tuple[str, ...] = (
    rw.GATE_DECISION_FIELDS + EXTENSION_ROW_FIELDS + ("unique_key", "filled_leg_key")
)

EXTENSION_BLOCK_STATUSES: tuple[str, ...] = (
    STATUS_ENTRY_GATE_EXTENSION_BELOW,
    STATUS_ENTRY_GATE_EXTENSION_UNDETERMINED,
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
            "entry_variant": rw.ENTRY_VARIANT.value,
            "pattern_id": "PIVOT_V_CONFIRM_V1",
            "add_variant": "NONE_FLAT_REENTRY_VARIANT",
            "exit_variant": "EXIT_DOW_TRAIL",
            "variant_id": VARIANT_ID,
            "entry_gate": ma144.GATE_RULE,
            "entry_gate_back_official_sessions": ma144.GATE_BACK_SESSIONS,
            "entry_gate_sma_window": ma144.GATE_SMA_WINDOW,
            "risk_width_gate": rw.RISK_WIDTH_RULE,
            "risk_width_threshold": str(rw.RISK_WIDTH_THRESHOLD),
            "extension_gate": EXTENSION_RULE,
            "extension_gate_sma_window": EXTENSION_SMA_WINDOW,
            "extension_gate_threshold": str(EXTENSION_THRESHOLD),
        },
        metadata={
            "source_code": code,
            "variant_id": VARIANT_ID,
            "engine_matrix_vehicle": ENGINE_MATRIX_VEHICLE,
            "lifecycle_contract": (
                "docs/strategies/dual_ma/2022-h1-selection-episode-lifecycle-v1.md"
            ),
            "variant_contract": SPEC_RELATIVE_PATH_EXTENSION,
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
    engine_class: type,
    instrument_parameter: Any = pilot.ENGINE_INSTRUMENT_PARAMETER,
) -> tuple[list[Any], dict[str, int], dict[str, str], Any]:
    """Replay one activated episode with the chosen engine class."""

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
    result = engine_class(
        slippage_bps=slippage_bps,
        entry_variant=rw.ENTRY_VARIANT,
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


def _leg_row(**kwargs: Any) -> dict[str, Any]:
    return rw._leg_row(**kwargs)


def _extension_decision_row(
    *,
    event: Mapping[str, Any],
    episode: Any,
    run_id: str,
    slippage_bps: int,
    prior_leg: Any | None,
    filled_leg_key: str | None,
) -> dict[str, Any]:
    row = ma144._gate_decision_row(
        event=event,
        episode=episode,
        run_id=run_id,
        slippage_bps=slippage_bps,
        prior_leg=prior_leg,
    )
    for name in rw.RISK_WIDTH_ROW_FIELDS:
        row[name] = event.get(name)
    for name in EXTENSION_ROW_FIELDS:
        row[name] = event.get(name)
    row["unique_key"] = (
        f"{episode.code}|{episode.strategy}|{episode.window.ordinal}"
        f"|{event.get('entry_signal_date')}"
    )
    row["filled_leg_key"] = filled_leg_key
    return row


def _extension_events(result: Any, strategy: str) -> list[Mapping[str, Any]]:
    """Every long MOTHER ENTRY_DECISION carrying an extension verdict."""

    out: list[Mapping[str, Any]] = []
    for decision in result.decisions:
        if str(decision.get("strategy")) != strategy:
            continue
        for event in decision.get("events") or ():
            if not isinstance(event, Mapping):
                continue
            if event.get("type") != "ENTRY_DECISION":
                continue
            if event.get("extension_rule") is None:
                continue
            if event.get("role") != "MOTHER":
                continue
            out.append(event)
    return out


def collect_replay(
    *,
    plan: Any,
    snapshot: Any,
    code_programs_by_date: Mapping[str, Mapping[str, Sequence[str]]],
    official_sessions: Sequence[date],
    run_id: str,
    runner_commit: str,
    engine_class: type,
    collect_extension: bool,
    progress: Callable[[str, int, int, Mapping[str, Any]], None] | None = None,
    episode_limit: int | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Replay every activated episode at 0/10/20 bps with ``engine_class``.

    ``collect_extension`` only turns on the long-strategy extension ledger; the
    leg rows themselves always use the frozen ``rw.TRADE_ROW_FIELDS`` schema so
    the baseline replay can be byte-compared with the accepted run.
    """

    episodes = list(plan.episodes)
    truncated = False
    if episode_limit is not None and episode_limit < len(episodes):
        episodes = episodes[: int(episode_limit)]
        truncated = True

    rows: list[dict[str, Any]] = []
    extension_rows: list[dict[str, Any]] = []
    decision_totals: Counter[str] = Counter()
    decision_first_example: dict[str, dict[str, Any]] = {}
    episodes_with_leg: Counter[str] = Counter()
    reentry_episodes: Counter[str] = Counter()
    block_totals: Counter[str] = Counter()
    extension_totals: Counter[str] = Counter()
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
                engine_class=engine_class,
            )
            ordered_legs = sorted(legs, key=lambda item: (item.entry_date, item.leg_id))
            leg_by_id = {leg.leg_id: leg for leg in ordered_legs}
            for decision in result.decisions:
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
                        block_totals[f"{episode.strategy}|{slippage_bps}|{status}"] += 1
            if collect_extension:
                for event in _extension_events(result, episode.strategy):
                    status = str(event.get("status"))
                    if status not in EXTENSION_BLOCK_STATUSES and status != STATUS_FILLED:
                        continue
                    extension_totals[
                        f"{episode.strategy}|{slippage_bps}|{status}"
                    ] += 1
                    if slippage_bps != PRIMARY_SLIPPAGE_BPS:
                        continue
                    # A filled ENTRY_DECISION carries no prior_leg_id; the prior
                    # leg of a filled re-entry is the previous leg of the same
                    # episode.  A blocked signal carries its own prior_leg_id.
                    filled_leg_key = None
                    prior_leg = None
                    if status == STATUS_FILLED:
                        leg_id = event.get("leg_id")
                        filled = leg_by_id.get(str(leg_id)) if leg_id else None
                        filled_leg_key = None if filled is None else str(leg_id)
                        if filled is not None:
                            position = ordered_legs.index(filled)
                            if position > 0:
                                prior_leg = ordered_legs[position - 1]
                    else:
                        prior_id = event.get("prior_leg_id")
                        prior_leg = leg_by_id.get(str(prior_id)) if prior_id else None
                    extension_rows.append(
                        _extension_decision_row(
                            event=event,
                            episode=episode,
                            run_id=run_id,
                            slippage_bps=slippage_bps,
                            prior_leg=prior_leg,
                            filled_leg_key=filled_leg_key,
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
        "gate_block_totals": dict(sorted(block_totals.items())),
        "extension_decision_totals": dict(sorted(extension_totals.items())),
        "extension_primary_row_count": len(extension_rows),
    }
    return rows, extension_rows, stats


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def _money(value: Any, places: str = "0.01") -> str:
    if value is None or value == "":
        return ""
    return str(Decimal(str(value)).quantize(Decimal(places)))


def _pct(value: Any, places: int = 2) -> str:
    if value is None or value == "":
        return ""
    return f"{Decimal(str(value)) * 100:.{places}f}%"


def strategy_block(
    diag: Sequence[Mapping[str, Any]],
    *,
    strategy: str,
    sessions: Sequence[date],
) -> dict[str, Any]:
    """One strategy's closed/open split with first-entry vs re-entry detail."""

    metrics = ma144.strategy_metrics(diag, strategy=strategy, sessions=sessions)
    rows = [row for row in diag if row["strategy"] == strategy]
    closed = [row for row in rows if not row["open_censored"]]
    first_closed = [row for row in closed if not row["is_reentry"]]
    reentry_closed = [row for row in closed if row["is_reentry"]]

    def _net(items: Sequence[Mapping[str, Any]]) -> Decimal:
        return sum((row["costed_net_pnl"] or Decimal(0) for row in items), Decimal(0))

    def _win_rate(items: Sequence[Mapping[str, Any]]) -> Any:
        if not items:
            return None
        wins = sum(1 for row in items if (row["costed_net_pnl"] or Decimal(0)) > 0)
        return Decimal(wins) / Decimal(len(items))

    unresolved = [row for row in closed if row["corporate_action_status"] == flat.CA_UNRESOLVED]
    not_reproduced = [
        row for row in closed if row["corporate_action_status"] == flat.CA_NOT_REPRODUCED
    ]
    metrics.update(
        {
            "first_entry_closed_count": len(first_closed),
            "first_entry_net_total": _net(first_closed).quantize(Decimal("0.0001")),
            "first_entry_win_rate": _win_rate(first_closed),
            "reentry_closed_count": len(reentry_closed),
            "reentry_win_rate": _win_rate(reentry_closed),
            "unresolved_corpaction_closed_count": len(unresolved),
            "unresolved_corpaction_net_total": _net(unresolved).quantize(Decimal("0.0001")),
            "not_reproduced_corpaction_closed_count": len(not_reproduced),
            "not_reproduced_corpaction_net_total": _net(not_reproduced).quantize(
                Decimal("0.0001")
            ),
        }
    )
    return metrics


METRIC_ROWS: tuple[tuple[str, str, str], ...] = (
    ("已平倉筆數", "closed_count", "int"),
    ("未平倉（截點仍在倉）筆數", "open_count", "int"),
    ("總筆數", "leg_count", "int"),
    ("勝筆（費後）", "win_count", "int"),
    ("敗筆（費後）", "loss_count", "int"),
    ("勝率（費後）", "win_rate", "pct"),
    ("已平倉費前損益合計", "gross_pnl_total", "money"),
    ("已平倉費稅合計", "fees_and_tax_total", "money"),
    ("已平倉費後淨損益合計", "costed_net_pnl_total", "money"),
    ("平均每筆費後損益", "mean_costed_net_pnl", "money"),
    ("中位數每筆費後損益", "median_costed_net_pnl", "money"),
    ("成本後獲利因子", "profit_factor", "ratio"),
    ("平均持有官方交易日", "mean_hold_sessions_closed", "ratio"),
    ("中位持有官方交易日", "median_hold_sessions_closed", "ratio"),
    ("平均持有日曆天", "mean_hold_calendar_days_closed", "ratio"),
    ("早期結構停損（<=5 交易日）筆數", "early_stop_count", "int"),
    ("結構停損筆數", "structural_stop_count", "int"),
    ("制度出場筆數", "regime_exit_count", "int"),
    ("首進已平倉筆數", "first_entry_closed_count", "int"),
    ("首進費後淨損益", "first_entry_net_total", "money"),
    ("首進勝率", "first_entry_win_rate", "pct"),
    ("再進已平倉筆數", "reentry_closed_count", "int"),
    ("再進未平倉筆數", "reentry_open_count", "int"),
    ("再進費後淨損益", "reentry_net_total", "money"),
    ("再進勝率", "reentry_win_rate", "pct"),
    ("累計買進成本（資金占用代理）", "buy_total_sum", "money"),
    ("最大單筆買進成本", "max_buy_total", "money"),
)


def _metric_table_lines(
    title: str, new: Mapping[str, Any], base: Mapping[str, Any]
) -> list[str]:
    lines = [f"#### {title}", ""]
    lines.append("| 指標 | 新（+長期 SMA21 5% gate） | 舊（僅 MA144＋7% 風險寬度） | 差異 |")
    lines.append("| --- | --- | --- | --- |")
    for label, key, kind in METRIC_ROWS:
        left, right = new.get(key), base.get(key)
        if kind == "int":
            left_txt = "" if left is None else str(int(left))
            right_txt = "" if right is None else str(int(right))
            delta = "" if left is None or right is None else f"{int(left) - int(right):+d}"
        elif kind == "pct":
            left_txt = _pct(left)
            right_txt = _pct(right)
            delta = (
                ""
                if left is None or right is None
                else f"{(Decimal(str(left)) - Decimal(str(right))) * 100:.2f}pp"
            )
        elif kind == "money":
            left_txt = _money(left)
            right_txt = _money(right)
            delta = (
                ""
                if left is None or right is None
                else _money(Decimal(str(left)) - Decimal(str(right)))
            )
        else:
            left_txt = "" if left is None else str(Decimal(str(left)).quantize(Decimal("0.01")))
            right_txt = "" if right is None else str(Decimal(str(right)).quantize(Decimal("0.01")))
            delta = (
                ""
                if left is None or right is None
                else str((Decimal(str(left)) - Decimal(str(right))).quantize(Decimal("0.01")))
            )
        lines.append(f"| {label} | {left_txt} | {right_txt} | {delta} |")
    lines.append("")
    return lines


# ---------------------------------------------------------------------------
# Extension-gate analysis + spot checks
# ---------------------------------------------------------------------------


def build_extension_analysis(
    *,
    extension_rows: Sequence[Mapping[str, Any]],
    new_rows: Sequence[Mapping[str, Any]],
    baseline_diag: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """PASS / REJECT / HOLD counts, conservation and blocked-leg attribution."""

    longs = [row for row in extension_rows if row["strategy"] == StrategyKind.LONG.value]
    state_counts = Counter(str(row["extension_state"]) for row in longs)
    status_counts = Counter(str(row["status"]) for row in longs)
    reason_counts = Counter(
        str(row["extension_reason"]) for row in longs if row["extension_reason"]
    )
    passed = [row for row in longs if row["extension_state"] == STATE_EXT_PASS]
    below = [row for row in longs if row["extension_state"] == STATE_EXT_BELOW]
    hold = [row for row in longs if row["extension_state"] == STATE_EXT_UNDETERMINED]

    primary_long_legs = [
        row
        for row in new_rows
        if int(row["slippage_bps"]) == PRIMARY_SLIPPAGE_BPS
        and row["strategy"] == StrategyKind.LONG.value
    ]
    pass_keys = {str(row["unique_key"]) for row in passed}
    blocked_keys = {str(row["unique_key"]) for row in below} | {
        str(row["unique_key"]) for row in hold
    }
    leg_keys = {
        f"{row['code']}|{row['strategy']}|{row['episode_ordinal']}|{row['entry_signal_date_T']}"
        for row in primary_long_legs
    }

    baseline_by_key = {
        (str(row["code"]), str(row["strategy"]), str(row["T"])): row
        for row in baseline_diag
    }
    blocked_detail: list[dict[str, Any]] = []
    for row in list(below) + list(hold):
        key = (str(row["code"]), str(row["strategy"]), str(row["entry_signal_date_T"]))
        source = baseline_by_key.get(key)
        blocked_detail.append(
            {
                "code": row["code"],
                "strategy": row["strategy"],
                "T": row["entry_signal_date_T"],
                "extension_state": row["extension_state"],
                "extension_value": row["extension_value"],
                "extension_sma21": row["extension_sma21"],
                "would_be_reentry": row["would_be_reentry"],
                "baseline_leg_found": source is not None,
                "baseline_costed_net_pnl": None if source is None else source["costed_net_pnl"],
                "baseline_exit_reason": None if source is None else source["exit_reason"],
                "baseline_is_reentry": None if source is None else source["is_reentry"],
            }
        )
    matched = [row for row in blocked_detail if row["baseline_leg_found"]]
    blocked_winners = sorted(
        [
            row
            for row in matched
            if row["baseline_costed_net_pnl"] is not None
            and Decimal(str(row["baseline_costed_net_pnl"])) > 0
        ],
        key=lambda row: Decimal(str(row["baseline_costed_net_pnl"])),
        reverse=True,
    )
    blocked_losers = sorted(
        [
            row
            for row in matched
            if row["baseline_costed_net_pnl"] is not None
            and Decimal(str(row["baseline_costed_net_pnl"])) < 0
        ],
        key=lambda row: Decimal(str(row["baseline_costed_net_pnl"])),
    )

    reentry_pass = sum(
        1 for row in passed if str(row["would_be_reentry"]) in ("True", "true")
    )
    primary_long_reentry_legs = sum(
        1
        for row in primary_long_legs
        if str(row.get("is_reentry")) in ("True", "true")
    )
    reentry_blocked = sum(
        1
        for row in list(below) + list(hold)
        if str(row["would_be_reentry"]) in ("True", "true")
    )
    return {
        "schema_version": EXTENSION_ANALYSIS_SCHEMA_VERSION,
        "extension_rule": EXTENSION_RULE,
        "extension_threshold": str(EXTENSION_THRESHOLD),
        "extension_sma_window": EXTENSION_SMA_WINDOW,
        "comparison": "GREATER_THAN_OR_EQUAL_TO_THRESHOLD",
        "applies_to_strategy": StrategyKind.LONG.value,
        "state_counts": dict(sorted(state_counts.items())),
        "status_counts": dict(sorted(status_counts.items())),
        "reason_counts": dict(sorted(reason_counts.items())),
        "pass_count": len(passed),
        "below_count": len(below),
        "hold_count": len(hold),
        "evaluated_count": len(longs),
        "pass_equals_primary_long_legs": len(passed) == len(primary_long_legs),
        "primary_long_leg_count": len(primary_long_legs),
        "pass_key_matches_leg_key": pass_keys == leg_keys,
        "blocked_and_passed_disjoint": not (pass_keys & blocked_keys),
        "no_blocked_signal_became_a_leg": not (blocked_keys & leg_keys),
        "reentry_pass_count": reentry_pass,
        "reentry_blocked_count": reentry_blocked,
        "primary_long_reentry_leg_count": primary_long_reentry_legs,
        "reentry_pass_matches_reentry_legs": reentry_pass == primary_long_reentry_legs,
        "blocked_matched_leg_count": len(matched),
        "blocked_without_baseline_leg": len(blocked_detail) - len(matched),
        "blocked_winner_count": len(blocked_winners),
        "blocked_loser_count": len(blocked_losers),
        "blocked_winners": blocked_winners[:10],
        "blocked_losers": blocked_losers[:10],
        "blocked_all": blocked_detail,
        "note": (
            "blocked winner / loser rows are baseline legs whose own T the new "
            "long gate refused; they describe what the gate removed and are "
            "never trades of this run"
        ),
    }


def build_spot_checks(
    *,
    extension_rows: Sequence[Mapping[str, Any]],
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """At least two real PASS chains, two real REJECT chains and a hand cost."""

    longs = sorted(
        [row for row in extension_rows if row["strategy"] == StrategyKind.LONG.value],
        key=lambda row: (str(row["code"]), str(row["entry_signal_date_T"])),
    )
    passed = [row for row in longs if row["extension_state"] == STATE_EXT_PASS]
    rejected = [row for row in longs if row["extension_state"] == STATE_EXT_BELOW]
    hold = [row for row in longs if row["extension_state"] == STATE_EXT_UNDETERMINED]

    primary = [
        row
        for row in rows
        if int(row["slippage_bps"]) == PRIMARY_SLIPPAGE_BPS
        and not row["open_censored"]
        and row["strategy"] == StrategyKind.LONG.value
    ]
    primary.sort(key=lambda row: (row["code"], row["entry_signal_date_T"]))

    hand_cost: dict[str, Any] | None = None
    if primary:
        row = primary[0]
        shares = int(row["shares"])
        entry_price = Decimal(str(row["entry_fill_price"]))
        exit_price = Decimal(str(row["exit_fill_price"]))
        buy_order = flat.CATHAY_WEB_028_TW_V1.buy(entry_price, shares)
        sell_order = flat.CATHAY_WEB_028_TW_V1.sell(
            exit_price, shares, h2.InstrumentType.STOCK
        )
        net = sell_order.cash_amount - buy_order.cash_amount
        hand_cost = {
            "leg_key": row["leg_key"],
            "shares": shares,
            "entry_fill_price": h2._dstr(entry_price),
            "exit_fill_price": h2._dstr(exit_price),
            "hand_buy_total": h2._dstr(buy_order.cash_amount),
            "row_buy_total": row["buy_total"],
            "hand_sell_net": h2._dstr(sell_order.cash_amount),
            "row_sell_net": row["sell_net"],
            "hand_costed_net_pnl": h2._dstr(net),
            "row_costed_net_pnl": row["costed_net_pnl"],
            "buy_matches": buy_order.cash_amount == Decimal(str(row["buy_total"])),
            "sell_matches": sell_order.cash_amount == Decimal(str(row["sell_net"])),
            "net_matches": net == Decimal(str(row["costed_net_pnl"])),
        }

    hand_extension: dict[str, Any] | None = None
    for row in passed + rejected:
        if row["extension_value"] is None or row["extension_sma21"] is None:
            continue
        close = Decimal(str(row["extension_close"]))
        sma = Decimal(str(row["extension_sma21"]))
        value = close / sma - Decimal(1)
        recorded = Decimal(str(row["extension_value"]))
        hand_extension = {
            "unique_key": row["unique_key"],
            "code": row["code"],
            "T": row["entry_signal_date_T"],
            "extension_close": row["extension_close"],
            "extension_sma21": row["extension_sma21"],
            "row_extension_value": row["extension_value"],
            "hand_extension_value": h2._dstr(value, 6),
            "extension_state": row["extension_state"],
            # compare at the recorded 6 dp with a half-unit tolerance: the ledger
            # stores a 4 dp SMA21 while the live gate used the full precision value
            "matches": abs(value - recorded) <= Decimal("0.00005"),
        }
        break

    return {
        "schema_version": "dual-ma-h1h2-long-fastma-extension-spot-checks-v1",
        "accepted_d_e_v_t": passed[:2],
        "rejected_d_e_v_t": rejected[:2],
        "hold_d_e_v_t": hold[:1],
        "accepted_chain_found": len(passed) >= 2,
        "rejected_chain_found": len(rejected) >= 2,
        "hand_cost_recheck": hand_cost,
        "hand_extension_recheck": hand_extension,
    }


# ---------------------------------------------------------------------------
# Sample driver
# ---------------------------------------------------------------------------

SAMPLE_SPECS: dict[str, dict[str, Any]] = {
    "H1": {
        "label_zh": "2022 H1（117 個官方交易日）",
        "loader": h1rw.load_h1_risk_width_inputs,
        "preflight": h1rw.build_h1_preflight,
        "first_day": h1rw.H1_FIRST_DAY,
        "last_day": h1rw.H1_LAST_DAY,
        "expected_sessions": h1rw.EXPECTED_H1_SESSION_COUNT,
        "baseline_run_id": H1_BASELINE_RUN_ID,
        "baseline_manifest_sha256": H1_BASELINE_MANIFEST_SHA256,
        "baseline_trades_sha256": H1_BASELINE_TRADES_SHA256,
    },
    "H2": {
        "label_zh": "2022 H2（129 個官方交易日）",
        "loader": h2.load_h2_inputs,
        "preflight": h2.build_h2_preflight,
        "first_day": h2.H2_FIRST_DAY,
        "last_day": h2.H2_LAST_DAY,
        "expected_sessions": h2.EXPECTED_H2_SESSION_COUNT,
        "baseline_run_id": H2_BASELINE_RUN_ID,
        "baseline_manifest_sha256": H2_BASELINE_MANIFEST_SHA256,
        "baseline_trades_sha256": H2_BASELINE_TRADES_SHA256,
    },
}


def _csv_bytes(rows: Sequence[Mapping[str, Any]], fields: Sequence[str]) -> bytes:
    return h2._csv_bytes(fields, [[row.get(name) for name in fields] for row in rows])


def _subset(rows: Sequence[Mapping[str, Any]], *predicates: Any) -> list[Mapping[str, Any]]:
    return [row for row in rows if all(predicate(row) for predicate in predicates)]


def _sample_sha256(path: Path) -> str:
    return pilot.sha256_file(path).upper()


def _normalized_digest(rows: Sequence[Mapping[str, Any]]) -> str:
    """Digest of leg rows with only the run_id column normalized away.

    The new variant and the baseline replay share every leg-generation input but
    legitimately carry different ``run_id`` values; blanking that one column lets
    the otherwise byte-identical MEDIUM sequence be compared directly.
    """

    normalized = [
        [
            "NORMALIZED"
            if name == "run_id"
            else ("" if row.get(name) is None else str(row.get(name)))
            for name in TRADE_ROW_FIELDS
        ]
        for row in rows
    ]
    return pilot.sha256_bytes(h2._csv_bytes(TRADE_ROW_FIELDS, normalized))


def _run_one_sample(
    *,
    sample: str,
    workdata_root: Path,
    run_id: str,
    run_dir: Path,
    episode_limit: int | None,
    runner_commit: str,
    progress: Callable[[str, int, int, Mapping[str, Any]], None] | None,
) -> dict[str, Any]:
    spec = SAMPLE_SPECS[sample]
    inputs = spec["loader"](workdata_root, progress=progress)
    calendar = inputs["calendar"]
    snapshot = inputs["snapshot"]
    plan = pilot.build_lifecycle_plan(
        reports=inputs["reports"],
        resolution=inputs["resolution"],
        snapshot=snapshot,
        calendar=calendar,
    )
    preflight = spec["preflight"](inputs)
    preflight["run_id"] = f"{run_id}:{sample}"
    preflight["module_version"] = MODULE_VERSION

    official_sessions = calendar.sessions
    baseline_dir = workdata_root / "02-causal-backtest" / spec["baseline_run_id"]
    baseline_manifest = baseline_dir / "manifest.json"
    baseline_trades = baseline_dir / "trades.csv"
    if not baseline_manifest.is_file() or not baseline_trades.is_file():
        raise LongExtensionError(f"{sample} baseline run is incomplete: {baseline_dir}")
    baseline_manifest_sha = _sample_sha256(baseline_manifest)
    baseline_trades_sha = _sample_sha256(baseline_trades)
    if baseline_manifest_sha != spec["baseline_manifest_sha256"]:
        raise LongExtensionError(
            f"{sample} baseline manifest {baseline_manifest_sha} != "
            f"{spec['baseline_manifest_sha256']}"
        )
    if baseline_trades_sha != spec["baseline_trades_sha256"]:
        raise LongExtensionError(
            f"{sample} baseline trades {baseline_trades_sha} != "
            f"{spec['baseline_trades_sha256']}"
        )

    new_rows, extension_rows, new_stats = collect_replay(
        plan=plan,
        snapshot=snapshot,
        code_programs_by_date=inputs["code_programs_by_date"],
        official_sessions=official_sessions,
        run_id=run_id,
        runner_commit=runner_commit,
        engine_class=LongExtensionEntryEngine,
        collect_extension=True,
        progress=progress,
        episode_limit=episode_limit,
    )
    baseline_rows, _, base_stats = collect_replay(
        plan=plan,
        snapshot=snapshot,
        code_programs_by_date=inputs["code_programs_by_date"],
        official_sessions=official_sessions,
        run_id=spec["baseline_run_id"],
        runner_commit=runner_commit,
        engine_class=rw.RiskWidthEntryEngine,
        collect_extension=False,
        progress=progress,
        episode_limit=episode_limit,
    )

    summary_new = h2.summarize_rows(new_rows)
    summary_base = h2.summarize_rows(baseline_rows)
    groups = h2.build_group_attribution(
        new_rows, code_programs_by_date=inputs["code_programs_by_date"]
    )
    top_movers = h2.build_top_movers(new_rows)
    exit_reasons = h2.build_exit_reason_breakdown(new_rows)
    open_positions = h2.build_year_end_open_positions(rows=new_rows, snapshot=snapshot)
    corp_summary = flat.enrich_corporate_actions(
        rows=new_rows,
        snapshot=snapshot,
        workdata_root=workdata_root,
        run_id=run_id,
        runner_commit=runner_commit,
        progress=progress,
    )
    corp_open = flat.build_corpaction_open_positions(rows=new_rows, snapshot=snapshot)
    flat.enrich_corporate_actions(
        rows=baseline_rows,
        snapshot=snapshot,
        workdata_root=workdata_root,
        run_id=f"{run_id}-baseline-replay",
        runner_commit=runner_commit,
        progress=None,
    )

    # Corporate-action enrichment fills columns in place, exactly like the
    # accepted baseline run did before it wrote trades.csv, so the frozen bytes
    # can only be reproduced with this ordering.
    baseline_replay_bytes = _csv_bytes(baseline_rows, TRADE_ROW_FIELDS)
    baseline_replay_sha = pilot.sha256_bytes(baseline_replay_bytes).upper()
    baseline_replay_matches_frozen = baseline_replay_sha == spec["baseline_trades_sha256"]

    frozen_medium_rows = [
        row
        for row in flat._read_csv_rows(baseline_trades)
        if row.get("strategy") == StrategyKind.MEDIUM.value
    ]
    medium_unchanged = _normalized_digest(
        _subset(new_rows, lambda row: row["strategy"] == StrategyKind.MEDIUM.value)
    ) == _normalized_digest(
        _subset(baseline_rows, lambda row: row["strategy"] == StrategyKind.MEDIUM.value)
    )
    medium_matches_frozen_baseline = _normalized_digest(
        _subset(new_rows, lambda row: row["strategy"] == StrategyKind.MEDIUM.value)
    ) == _normalized_digest(frozen_medium_rows)

    prices, sessions = ma144.load_price_and_sessions(workdata_root)
    new_diag = attribution.build_leg_diagnostics(
        ma144.zero_bps_legs_from_rows(new_rows, run_dir=run_dir),
        sessions=sessions,
        prices=prices,
    )
    base_diag = attribution.build_leg_diagnostics(
        ma144.zero_bps_legs_from_rows(baseline_rows, run_dir=run_dir),
        sessions=sessions,
        prices=prices,
    )
    new_blocks = {
        strategy: strategy_block(new_diag, strategy=strategy, sessions=sessions)
        for strategy in STRATEGIES
    }
    base_blocks = {
        strategy: strategy_block(base_diag, strategy=strategy, sessions=sessions)
        for strategy in STRATEGIES
    }
    analysis = build_extension_analysis(
        extension_rows=extension_rows,
        new_rows=new_rows,
        baseline_diag=base_diag,
    )
    spot_checks = build_spot_checks(extension_rows=extension_rows, rows=new_rows)

    ext_primary = sorted(
        extension_rows,
        key=lambda row: (str(row["strategy"]), str(row["code"]), str(row["entry_signal_date_T"])),
    )
    payloads: dict[str, bytes] = {
        f"{sample.lower()}_trades.csv": _csv_bytes(new_rows, TRADE_ROW_FIELDS),
        f"{sample.lower()}_baseline_replay_trades.csv": baseline_replay_bytes,
        f"{sample.lower()}_extension_decisions.csv": _csv_bytes(
            ext_primary, EXTENSION_DECISION_FIELDS
        ),
        f"{sample.lower()}_strategy_summary.csv": h2._csv_bytes(
            h2.SUMMARY_FIELDS,
            [[row.get(name) for name in h2.SUMMARY_FIELDS] for row in summary_new],
        ),
        f"{sample.lower()}_baseline_strategy_summary.csv": h2._csv_bytes(
            h2.SUMMARY_FIELDS,
            [[row.get(name) for name in h2.SUMMARY_FIELDS] for row in summary_base],
        ),
        f"{sample.lower()}_strategy_summary.json": pilot._json_bytes(
            {
                "schema_version": h2.STATS_SCHEMA_VERSION,
                "run_id": run_id,
                "variant_id": VARIANT_ID,
                "entry_gate": ma144.GATE_RULE,
                "risk_width_gate": rw.RISK_WIDTH_RULE,
                "risk_width_threshold": str(rw.RISK_WIDTH_THRESHOLD),
                "extension_gate": EXTENSION_RULE,
                "extension_threshold": str(EXTENSION_THRESHOLD),
                "extension_sma_window": EXTENSION_SMA_WINDOW,
                "primary_slippage_bps": PRIMARY_SLIPPAGE_BPS,
                "slippage_scenarios": list(SLIPPAGE_SCENARIOS),
                "rows": summary_new,
            }
        ),
        f"{sample.lower()}_extension_analysis.json": pilot._json_bytes(analysis),
        f"{sample.lower()}_spot_checks.json": pilot._json_bytes(spot_checks),
        f"{sample.lower()}_group_attribution.json": pilot._json_bytes(groups),
        f"{sample.lower()}_top_movers.json": pilot._json_bytes(top_movers),
        f"{sample.lower()}_exit_reason_breakdown.json": pilot._json_bytes(exit_reasons),
        f"{sample.lower()}_year_end_open_positions.json": pilot._json_bytes(open_positions),
        f"{sample.lower()}_corpaction_summary.json": pilot._json_bytes(corp_summary),
        f"{sample.lower()}_corpaction_open_positions.json": pilot._json_bytes(corp_open),
        f"{sample.lower()}_preflight.json": pilot._json_bytes(preflight),
        f"{sample.lower()}_entry_decision_totals.json": pilot._json_bytes(
            {
                "new": new_stats["entry_decision_totals"],
                "first_example": new_stats["entry_decision_first_example"],
                "gate_block_totals": new_stats["gate_block_totals"],
                "extension_decision_totals": new_stats["extension_decision_totals"],
            }
        ),
    }

    gates = {
        "coverage_is_exactly_expected_official_sessions": (
            "PASS" if int(preflight.get(f"official_{sample.lower()}_session_count", -1))
            == spec["expected_sessions"]
            and int(preflight["reported_date_count"]) == spec["expected_sessions"]
            else "FAIL"
        ),
        "parents_all_matched": "PASS" if inputs["parent"]["all_matched"] else "FAIL",
        "price_snapshot_never_reaches_2023": (
            "PASS" if snapshot.last_session <= AS_OF_CUTOFF else "FAIL"
        ),
        "price_snapshot_manifest_matches_pin": (
            "PASS" if snapshot.manifest_file_sha256.upper() == PRICE_SNAPSHOT_MANIFEST_SHA256 else "FAIL"
        ),
        "baseline_run_manifest_byte_unchanged": (
            "PASS" if _sample_sha256(baseline_manifest) == spec["baseline_manifest_sha256"] else "FAIL"
        ),
        "baseline_run_trades_byte_unchanged": (
            "PASS" if _sample_sha256(baseline_trades) == spec["baseline_trades_sha256"] else "FAIL"
        ),
        "baseline_replay_reproduces_frozen_trades": (
            "PASS" if baseline_replay_matches_frozen else "FAIL"
        ),
        "medium_rows_byte_unchanged": "PASS" if medium_unchanged else "FAIL",
        "medium_rows_match_frozen_baseline": (
            "PASS" if medium_matches_frozen_baseline else "FAIL"
        ),
        "max_open_legs_at_most_one": (
            "PASS" if int(new_stats["max_open_legs_observed"]) <= 1 else "FAIL"
        ),
        "no_additions_filled": (
            "PASS" if int(new_stats["filled_add_events"]) == 0 else "FAIL"
        ),
        "extension_pass_equals_primary_long_legs": (
            "PASS" if analysis["pass_equals_primary_long_legs"] else "FAIL"
        ),
        "extension_blocked_and_passed_disjoint": (
            "PASS" if analysis["blocked_and_passed_disjoint"] else "FAIL"
        ),
        "no_blocked_signal_became_a_long_leg": (
            "PASS" if analysis["no_blocked_signal_became_a_leg"] else "FAIL"
        ),
        "reentry_pass_count_matches_reentry_legs": (
            "PASS" if analysis["reentry_pass_matches_reentry_legs"] else "FAIL"
        ),
        "every_long_entry_fast_sma_equals_gate_sma21": (
            "PASS" if all(
                str(row["entry_fast_sma"]) == str(row["extension_sma21"])
                for row in extension_rows
                if row["strategy"] == StrategyKind.LONG.value and row["extension_sma21"]
            )
            else "FAIL"
        ),
        "spot_checks_present": (
            "PASS"
            if spot_checks["accepted_chain_found"] and spot_checks["rejected_chain_found"]
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
        "spot_check_hand_extension_matches": (
            "PASS"
            if (
                spot_checks["hand_extension_recheck"] is not None
                and spot_checks["hand_extension_recheck"]["matches"]
            )
            else "FAIL"
        ),
    }
    if episode_limit is not None:
        gates["full_sample_replay"] = "FAIL"
    status = "PASS" if all(value == "PASS" for value in gates.values()) else "HOLD"

    return {
        "sample": sample,
        "spec": spec,
        "inputs": inputs,
        "preflight": preflight,
        "plan": plan,
        "snapshot": snapshot,
        "calendar": calendar,
        "new_rows": new_rows,
        "baseline_rows": baseline_rows,
        "extension_rows": extension_rows,
        "new_stats": new_stats,
        "base_stats": base_stats,
        "baseline_replay_sha": baseline_replay_sha,
        "baseline_replay_matches_frozen": baseline_replay_matches_frozen,
        "medium_unchanged": medium_unchanged,
        "medium_matches_frozen_baseline": medium_matches_frozen_baseline,
        "summary_new": summary_new,
        "summary_base": summary_base,
        "new_diag": new_diag,
        "base_diag": base_diag,
        "new_blocks": new_blocks,
        "base_blocks": base_blocks,
        "analysis": analysis,
        "spot_checks": spot_checks,
        "groups": groups,
        "top_movers": top_movers,
        "exit_reasons": exit_reasons,
        "open_positions": open_positions,
        "corp_summary": corp_summary,
        "corp_open": corp_open,
        "gates": gates,
        "status": status,
        "payloads": payloads,
    }




# ---------------------------------------------------------------------------
# Report (Traditional Chinese)
# ---------------------------------------------------------------------------


def _chain_lines(title: str, rows: Sequence[Mapping[str, Any]]) -> list[str]:
    lines = [f"##### {title}", ""]
    if not rows:
        lines.append("（本樣本無此類樣本可列）")
        lines.append("")
        return lines
    lines.append("| 代號 | 證券 | 策略 | D | E | V | T | 收盤 | SMA21(T) | 延伸幅度 | 判定 | 是否再進 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for row in rows:
        lines.append(
            "| {code} | {symbol} | {strategy} | {D} | {E} | {V} | {T} | {close} | {sma} | {ext} | {state} | {re} |".format(
                code=row.get("code", ""),
                symbol=row.get("symbol", ""),
                strategy=row.get("strategy", ""),
                D=row.get("source_selection_date_D", ""),
                E=row.get("monitor_effective_date_E", ""),
                V=row.get("pivot_confirmed_date", ""),
                T=row.get("entry_signal_date_T", ""),
                close=row.get("extension_close", ""),
                sma=row.get("extension_sma21", ""),
                ext=row.get("extension_value", ""),
                state=row.get("extension_state", ""),
                re=row.get("would_be_reentry", ""),
            )
        )
    lines.append("")
    return lines


def _open_lines(open_positions: Mapping[str, Any]) -> list[str]:
    lines = ["##### 未平倉（2022-12-30 截點仍在倉，另列不混入已平倉）", ""]
    lines.append(
        f"未平倉總數 {open_positions['open_position_count']}；"
        f"中期 {open_positions['medium_open_count']}；長期 {open_positions['long_open_count']}。"
    )
    lines.append(
        f"依 2022-12-30 收盤與凍結成本模型之未實現淨損益代理："
        f"中期 {open_positions['medium_mark_to_market_net_pnl']}；"
        f"長期 {open_positions['long_mark_to_market_net_pnl']}。"
    )
    lines.append("")
    lines.append("| 代號 | 策略 | T | 股數 | 買進成本 | 截點收盤 | 未實現淨損益 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for item in open_positions["positions"]:
        lines.append(
            "| {code} | {strategy} | {T} | {shares} | {buy} | {close} | {pnl} |".format(
                code=item.get("code", ""),
                strategy=item.get("strategy", ""),
                T=item.get("entry_signal_date_T", ""),
                shares=item.get("shares", ""),
                buy=item.get("buy_total", ""),
                close=item.get("cutoff_close", ""),
                pnl=item.get("mark_to_market_net_pnl", ""),
            )
        )
    lines.append("")
    return lines


def _corpaction_lines(
    corp_summary: Mapping[str, Any], diag: Sequence[Mapping[str, Any]]
) -> list[str]:
    lines = ["##### 公司行動（權息）分類與未解項", ""]
    lines.append(
        f"0 bps 逐筆 {corp_summary['zero_bps_leg_count']} 筆；"
        f"受影響 {corp_summary['affected_leg_count']} 筆；"
        f"可解 {corp_summary['resolved_leg_count']} 筆；"
        f"未解 {corp_summary['unresolved_leg_count']} 筆；"
        f"未能重現 {corp_summary['not_reproduced_leg_count']} 筆。"
    )
    lines.append("")
    lines.append("| 策略 | 權息未解已平倉筆數 | 權息未解原始費後損益合計 |")
    lines.append("| --- | --- | --- |")
    for strategy in STRATEGIES:
        closed = [
            row
            for row in diag
            if row["strategy"] == strategy
            and not row["open_censored"]
            and row["corporate_action_status"] == flat.CA_UNRESOLVED
        ]
        total = sum((row["costed_net_pnl"] or Decimal(0) for row in closed), Decimal(0))
        lines.append(
            f"| {STRATEGY_LABEL_ZH.get(strategy, strategy)} | {len(closed)} | {_money(total.quantize(Decimal('0.0001')))} |"
        )
    lines.append("")
    lines.append(
        "權息未解與未能重現之部位只列出原始基準數字，不併入已平倉均值；"
        "本輪不虛構未定案的權息調整後績效。"
    )
    lines.append("")
    return lines


def _movers_lines(top_movers: Mapping[str, Any]) -> list[str]:
    lines = ["##### 大贏家與大輸家（0 bps，已平倉，費後）", ""]
    lines.append("| 方向 | 代號 | 策略 | T | 出場 | 出場原因 | 股數 | 費後損益 | 報酬率% |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for label, key in (("贏家", "winners"), ("輸家", "losers")):
        for item in top_movers.get(key, ()):
            lines.append(
                "| {label} | {code} | {strategy} | {T} | {ex} | {reason} | {shares} | {pnl} | {ret} |".format(
                    label=label,
                    code=item.get("code", ""),
                    strategy=item.get("strategy", ""),
                    T=item.get("entry_signal_date_T", ""),
                    ex=item.get("exit_date", ""),
                    reason=item.get("exit_reason", ""),
                    shares=item.get("shares", ""),
                    pnl=item.get("costed_net_pnl", ""),
                    ret=item.get("net_return_pct", ""),
                )
            )
    lines.append("")
    return lines


def _sample_section(sample: str, r: Mapping[str, Any]) -> list[str]:
    spec = r["spec"]
    analysis = r["analysis"]
    lines: list[str] = []
    lines.append(f"## {sample} 結果（{spec['label_zh']}）")
    lines.append("")
    lines.append(
        f"基準 run：`{spec['baseline_run_id']}`（manifest `{spec['baseline_manifest_sha256']}`、"
        f"trades `{spec['baseline_trades_sha256']}`）；本輪以同一份程式碼重播基準變因，"
        f"重播 trades SHA `{r['baseline_replay_sha']}`，與凍結基準"
        f"{'逐位元相同' if r['baseline_replay_matches_frozen'] else '不同（見閘門 FAIL）'}。"
    )
    lines.append("")
    lines.append(
        f"重播 episode 數 {r['new_stats']['episode_count_run']}（0/10/20 bps 各一輪）；"
        f"新變因 leg 列 {r['new_stats']['leg_row_count']}；"
        f"最大同時在倉 {r['new_stats']['max_open_legs_observed']}；"
        f"加碼成交 {r['new_stats']['filled_add_events']}。"
    )
    lines.append("")

    lines.append("### 長期 21/144（本輪唯一受 gate 影響）")
    lines.append("")
    for bps in SLIPPAGE_SCENARIOS:
        lines.extend(
            _metric_table_lines(
                f"長期 21/144：{bps} bps（新 vs 基準）",
                r["new_blocks"][StrategyKind.LONG.value],
                r["base_blocks"][StrategyKind.LONG.value],
            )
        )

    lines.append("### 中期 5/21（完全未改動之對照）")
    lines.append("")
    lines.append(
        "中期策略未新增任何 gate；新舊版本的中期逐筆 leg 列在本次重播中"
        f"{'逐位元相同' if r['medium_unchanged'] else '出現差異（見閘門 FAIL）'}。"
    )
    lines.append("")
    for bps in SLIPPAGE_SCENARIOS:
        lines.extend(
            _metric_table_lines(
                f"中期 5/21：{bps} bps（新 vs 基準；預期完全相同）",
                r["new_blocks"][StrategyKind.MEDIUM.value],
                r["base_blocks"][StrategyKind.MEDIUM.value],
            )
        )

    lines.append("### 長期新 gate 判定明細（PASS / REJECT / HOLD）")
    lines.append("")
    lines.append(
        f"完整訊號經 MA144 上行與 7% 風險寬度後進入新 gate 者共 {analysis['evaluated_count']} 筆"
        f"（僅 0 bps 列帳，因為 gate 判定與滑價無關）："
        f"PASS {analysis['pass_count']}、REJECT（<5%）{analysis['below_count']}、"
        f"HOLD（缺值/非正 SMA21）{analysis['hold_count']}。"
    )
    lines.append(
        f"守恆：PASS 筆數等於本輪長期 0 bps leg 數（{analysis['primary_long_leg_count']}）："
        f"{analysis['pass_equals_primary_long_legs']}；"
        f"PASS 與被擋唯一鍵互斥：{analysis['blocked_and_passed_disjoint']}。"
    )
    lines.append(
        f"首進/再進皆受同一 gate：PASS 再進 {analysis['reentry_pass_count']} 筆；"
        f"被擋再進 {analysis['reentry_blocked_count']} 筆。"
    )
    lines.append("")
    lines.append("| 判定 | 筆數 | 說明 |")
    lines.append("| --- | --- | --- |")
    lines.append(f"| PASS | {analysis['pass_count']} | close_T / SMA21(T) - 1 >= 0.05 |")
    lines.append(f"| REJECT | {analysis['below_count']} | 低於 5%，記 ENTRY_GATE_EXTENSION_BELOW_5PCT，當次不買 |")
    lines.append(f"| HOLD | {analysis['hold_count']} | 缺 T 收盤/SMA21 或 SMA21<=0，記 ENTRY_GATE_EXTENSION_UNDETERMINED |")
    lines.append("")
    if analysis["reason_counts"]:
        lines.append(
            "HOLD 原因分布："
            + ", ".join(f"{k}={v}" for k, v in sorted(analysis["reason_counts"].items()))
        )
        lines.append("")

    lines.append("### 被新 gate 擋下的 T：與基準 leg 對照（僅描述擋掉了什麼）")
    lines.append("")
    lines.append(
        f"被擋且能對到基準 leg 者 {analysis['blocked_matched_leg_count']} 筆；"
        f"其中基準為贏家 {analysis['blocked_winner_count']} 筆、輸家 {analysis['blocked_loser_count']} 筆；"
        f"對不到基準 leg 者 {analysis['blocked_without_baseline_leg']} 筆"
        "（因擋掉後路徑改變，屬正常）。"
    )
    lines.append("")
    lines.append("被擋下的基準贏家前 5（代表此 gate 的成本）：")
    lines.append("")
    lines.append("| 代號 | T | 延伸幅度 | 基準費後損益 | 基準出場原因 |")
    lines.append("| --- | --- | --- | --- | --- |")
    for row in analysis["blocked_winners"][:5]:
        lines.append(
            f"| {row['code']} | {row['T']} | {row['extension_value']} | "
            f"{row['baseline_costed_net_pnl']} | {row['baseline_exit_reason']} |"
        )
    lines.append("")
    lines.append("被擋下的基準輸家前 5（代表此 gate 的好處）：")
    lines.append("")
    lines.append("| 代號 | T | 延伸幅度 | 基準費後損益 | 基準出場原因 |")
    lines.append("| --- | --- | --- | --- | --- |")
    for row in analysis["blocked_losers"][:5]:
        lines.append(
            f"| {row['code']} | {row['T']} | {row['extension_value']} | "
            f"{row['baseline_costed_net_pnl']} | {row['baseline_exit_reason']} |"
        )
    lines.append("")

    lines.extend(_open_lines(r["open_positions"]))
    lines.extend(_corpaction_lines(r["corp_summary"], r["new_diag"]))
    lines.extend(_movers_lines(r["top_movers"]))

    lines.append("### 真實 D→E→V→T 抽查")
    lines.append("")
    lines.extend(_chain_lines("PASS 且已成交（前 2 筆）", r["spot_checks"]["accepted_d_e_v_t"]))
    lines.extend(_chain_lines("REJECT（<5%，前 2 筆）", r["spot_checks"]["rejected_d_e_v_t"]))
    hand = r["spot_checks"]["hand_cost_recheck"]
    if hand:
        lines.append(
            f"手算成本核對（leg_key {hand['leg_key']}）：買進成本 手算 {hand['hand_buy_total']} "
            f"vs 帳本 {hand['row_buy_total']}（{hand['buy_matches']}）；"
            f"賣出淨額 手算 {hand['hand_sell_net']} vs 帳本 {hand['row_sell_net']}（{hand['sell_matches']}）；"
            f"費後損益 手算 {hand['hand_costed_net_pnl']} vs 帳本 {hand['row_costed_net_pnl']}（{hand['net_matches']}）。"
        )
        lines.append("")
    ex = r["spot_checks"]["hand_extension_recheck"]
    if ex:
        lines.append(
            f"手算延伸幅度核對（{ex['code']} {ex['T']}）：close {ex['extension_close']} / "
            f"SMA21 {ex['extension_sma21']} - 1 = {ex['hand_extension_value']} "
            f"vs 帳本 {ex['row_extension_value']}（{ex['matches']}）。"
        )
        lines.append("")

    lines.append("### 本樣本閘門")
    lines.append("")
    lines.append("| 閘門 | 結果 |")
    lines.append("| --- | --- |")
    for name, value in r["gates"].items():
        lines.append(f"| {name} | {value} |")
    lines.append("")
    return lines


def render_report_zh(
    *,
    run_id: str,
    spec_path: str,
    spec_sha: str,
    spec_bytes: int,
    results: Mapping[str, Mapping[str, Any]],
    source_sha256: Mapping[str, Any],
    artifact_sha256: Mapping[str, str],
    overall_status: str,
) -> str:
    lines: list[str] = []
    lines.append("# 2022 長期 21/144 進場強度單變因：T 收盤 >= SMA21(T) × 1.05 因果重播（H1＋H2）")
    lines.append("")
    lines.append("- 角色：02 雙均線因果回測；模組 `" + MODULE_VERSION + "`")
    lines.append("- run_id：`" + run_id + "`")
    lines.append("- status：**" + overall_status + "**（research_only / point_in_time_verified=false）")
    lines.append("")
    lines.append("## 定位：這是樣本內探索，不是獨立驗證")
    lines.append("")
    lines.append(
        "本 gate（T 收盤至少高於 SMA21(T) 5%）與整數門檻 5% 是**先看過同一份 2022 樣本**、"
        "以既有 `entry_fast_sma` 分組後才挑出的條件，具明確樣本內選擇偏差。"
        "本輪只把該條件接上原狀態機做完整逐日因果重播，**即使回測結果轉正也不能稱獨立驗證**；"
        "若日後要宣稱可用，需另於未見期間事前凍結再驗。本輪不讀也不宣稱 2023/2024 績效。"
    )
    lines.append("")
    lines.append("## 唯一規格與口徑")
    lines.append("")
    lines.append(f"- 規格：`{SPEC_RELATIVE_PATH_EXTENSION}`")
    lines.append(f"- 讀取路徑：`{spec_path}`")
    lines.append(f"- raw SHA-256：`{spec_sha}`（{spec_bytes} bytes）")
    lines.append("- 只有長期 21/144 新增 gate；中期 5/21 完全不變並分開列帳。")
    lines.append("- D→E 監控、V 樞紐確認、T 完整訊號、凍結 T 收盤代理價、出場、成本模型與 2022-12-30 截點皆未改。")
    lines.append("- 新 gate 在 T 收盤才判；SMA21(T) 由截至 T 的價格算得；不用事後漲跌、不把舊 T 延遲到隔天補買。")
    lines.append("- 兩策略與 H1/H2 一律分開，**不相加成一個帳戶或單一投組報酬**。")
    lines.append("")
    lines.append("## 唯一變因與判定規則")
    lines.append("")
    lines.append("- 規則：`" + EXTENSION_RULE + "`，即 `close_T / SMA21(T) - 1 >= 0.05`。")
    lines.append("- `>=5%` → PASS（准買）；`<5%` → REJECT（`ENTRY_GATE_EXTENSION_BELOW_5PCT`，當次不買）。")
    lines.append("- 缺 T 收盤、缺 SMA21 或 SMA21<=0 → HOLD（`ENTRY_GATE_EXTENSION_UNDETERMINED`，不買）。")
    lines.append("- 只在原 MA144 上行 **且** 風險寬度 >= 7% 皆通過後才判；首進與再進同一 gate；既有持倉中／同日回買拒絕路徑不變。")
    lines.append("")

    for sample in results:
        lines.extend(_sample_section(sample, results[sample]))

    lines.append("## 限制、疑義與非獨立驗證聲明")
    lines.append("")
    lines.append("- 本 gate 與門檻選自已看過的 2022 樣本；回測轉正不代表樣本外有效，不得稱獨立驗證。")
    lines.append("- 沒有每日帳戶淨值，因此不提供帳戶報酬或最大回撤，也不虛構。所有負值照實列出。")
    lines.append("- 未平倉、權息未解與未能重現之部位另列，不混入已平倉均值。")
    lines.append("- 價格為未調整供應商收盤代理，非官方收盤；T 收盤僅為代理，不宣稱可於收盤後實際成交。")
    lines.append("- 停損、出場、資金占用代理（累計買進成本）與成本模型沿用已驗收口徑，未因本輪結果調參。")
    lines.append("")
    lines.append("## 輸入與產出 SHA-256 / readback")
    lines.append("")
    lines.append("### 輸入與來源")
    lines.append("")
    lines.append("| 項目 | SHA-256 |")
    lines.append("| --- | --- |")
    for name, value in source_sha256.items():
        lines.append(f"| {name} | {value} |")
    lines.append("")
    lines.append("### 產出檔案")
    lines.append("")
    lines.append("| 檔案 | SHA-256 |")
    lines.append("| --- | --- |")
    for name, value in artifact_sha256.items():
        lines.append(f"| {name} | {value} |")
    lines.append("")
    lines.append("### checkpoint")
    lines.append("")
    lines.append("- kind：`ATOMIC_H1H2_LONG_FASTMA_EXTENSION`")
    lines.append("- state：`COMPLETE_AWAITING_00_QA`")
    lines.append("- resumable：False")
    lines.append("- next_action：00 對本輪原子批做 QA；不自行再加條件、不讀 2023/2024、不部署。")
    lines.append("")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run_long_extension(
    *,
    workdata_root: str | Path,
    output_root: str | Path | None = None,
    run_id: str = RUN_ID_DEFAULT,
    episode_limit: int | None = None,
    runner_commit: str | None = None,
    samples: Sequence[str] = ("H1", "H2"),
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
        raise LongExtensionError(
            f"refusing to overwrite an existing run directory: {run_dir}"
        )

    worktree_root = Path(__file__).resolve().parents[2]
    integration_root = h1rw._find_integration_root(worktree_root, workdata_root)
    spec_bytes_raw, spec_path = h1rw._resolve_spec_bytes(
        worktree_root, integration_root, SPEC_RELATIVE_PATH_EXTENSION
    )
    spec_sha = pilot.sha256_bytes(spec_bytes_raw).upper()
    if spec_sha != SPEC_SHA256_EXTENSION:
        raise LongExtensionError(
            f"extension spec sha256 {spec_sha} != {SPEC_SHA256_EXTENSION}"
        )
    if len(spec_bytes_raw) != SPEC_BYTE_SIZE_EXTENSION:
        raise LongExtensionError(
            f"extension spec byte size {len(spec_bytes_raw)} != {SPEC_BYTE_SIZE_EXTENSION}"
        )

    commit_state = pilot.resolve_runner_commit(worktree_root)
    commit = runner_commit or commit_state["runner_commit"]
    git_state = flat.git_state(worktree_root)

    pinned_files = {
        "h1_baseline_manifest": (
            workdata_root / "02-causal-backtest" / H1_BASELINE_RUN_ID / "manifest.json",
            H1_BASELINE_MANIFEST_SHA256,
        ),
        "h1_baseline_trades": (
            workdata_root / "02-causal-backtest" / H1_BASELINE_RUN_ID / "trades.csv",
            H1_BASELINE_TRADES_SHA256,
        ),
        "h2_baseline_manifest": (
            workdata_root / "02-causal-backtest" / H2_BASELINE_RUN_ID / "manifest.json",
            H2_BASELINE_MANIFEST_SHA256,
        ),
        "h2_baseline_trades": (
            workdata_root / "02-causal-backtest" / H2_BASELINE_RUN_ID / "trades.csv",
            H2_BASELINE_TRADES_SHA256,
        ),
        "h2_errata_manifest": (
            workdata_root / "02-causal-backtest" / H2_ERRATA_RUN_ID / "manifest.json",
            H2_ERRATA_MANIFEST_SHA256,
        ),
        "price_snapshot_manifest": (
            workdata_root
            / pilot.PRICE_SNAPSHOT_RELATIVE_PATH
            / pilot.PRICE_SNAPSHOT_MANIFEST_NAME,
            PRICE_SNAPSHOT_MANIFEST_SHA256,
        ),
    }
    for label, (path, expected) in pinned_files.items():
        if not path.is_file():
            raise LongExtensionError(f"{label} is missing: {path}")
        actual = _sample_sha256(path)
        if actual != expected:
            raise LongExtensionError(f"{label} sha256 {actual} != {expected}")

    run_dir.mkdir(parents=True, exist_ok=True)
    results: dict[str, dict[str, Any]] = {}
    for sample in samples:
        if progress is not None:
            progress(f"{sample.lower()}_start", 0, 1, {})
        results[sample] = _run_one_sample(
            sample=sample,
            workdata_root=workdata_root,
            run_id=run_id,
            run_dir=run_dir,
            episode_limit=episode_limit,
            runner_commit=commit,
            progress=progress,
        )

    payloads: dict[str, bytes] = {}
    for sample in samples:
        payloads.update(results[sample]["payloads"])

    source_sha256: dict[str, Any] = {
        "extension_spec_path": SPEC_RELATIVE_PATH_EXTENSION,
        "extension_spec_path_used": spec_path,
        "extension_spec_sha256": spec_sha,
        "extension_spec_bytes": len(spec_bytes_raw),
        "h2_rule_contract_path": H2_RULE_CONTRACT_PATH,
        "h2_rule_contract_sha256": H2_RULE_CONTRACT_SHA256,
        "h2_rule_contract_bytes": H2_RULE_CONTRACT_BYTES,
        "h1_baseline_manifest_sha256": H1_BASELINE_MANIFEST_SHA256,
        "h1_baseline_trades_sha256": H1_BASELINE_TRADES_SHA256,
        "h2_baseline_manifest_sha256": H2_BASELINE_MANIFEST_SHA256,
        "h2_baseline_trades_sha256": H2_BASELINE_TRADES_SHA256,
        "h2_errata_manifest_sha256": H2_ERRATA_MANIFEST_SHA256,
        "price_snapshot_manifest_file_sha256": PRICE_SNAPSHOT_MANIFEST_SHA256,
        "runner_module_sha256": pilot.sha256_file(Path(__file__)).upper(),
    }
    for sample in samples:
        inputs = results[sample]["inputs"]
        source_sha256[f"{sample.lower()}_price_snapshot_content_sha256"] = (
            inputs["snapshot"].content_sha256
        )
        source_sha256[f"{sample.lower()}_official_calendar_raw_sha256"] = (
            inputs["calendar"].raw_sha256
        )
        source_sha256[f"{sample.lower()}_official_calendar_content_sha256"] = (
            inputs["calendar"].content_sha256
        )
        source_sha256[f"{sample.lower()}_universe_sources_raw_sha256"] = inputs[
            "universe_sha"
        ]

    overall_status = (
        "PASS"
        if all(results[sample]["status"] == "PASS" for sample in samples)
        else "HOLD"
    )

    manifest: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "module_version": MODULE_VERSION,
        "run_id": run_id,
        "variant_id": VARIANT_ID,
        "extension_rule": EXTENSION_RULE,
        "extension_threshold": str(EXTENSION_THRESHOLD),
        "extension_sma_window": EXTENSION_SMA_WINDOW,
        "applies_to_strategy": StrategyKind.LONG.value,
        "entry_gate": ma144.GATE_RULE,
        "risk_width_gate": rw.RISK_WIDTH_RULE,
        "risk_width_threshold": str(rw.RISK_WIDTH_THRESHOLD),
        "engine_matrix_vehicle": ENGINE_MATRIX_VEHICLE,
        "status": overall_status,
        "note": "No wall-clock timestamp is recorded so the run stays reproducible.",
        "workdata_root": str(workdata_root),
        "research_only": True,
        "point_in_time_verified": False,
        "formal_historical_performance": False,
        "not_independent_blind_test": True,
        "in_sample_condition_choice": True,
        "no_2023_2024_performance_read": True,
        "frozen_matrix_unchanged": True,
        "accounts_added_together": False,
        "runner_commit": commit,
        "runner_commit_state": commit_state,
        "git_state": git_state,
        "episode_limit": episode_limit,
        "source_sha256": source_sha256,
        "samples": {
            sample: {
                "baseline_run_id": results[sample]["spec"]["baseline_run_id"],
                "baseline_manifest_sha256": results[sample]["spec"][
                    "baseline_manifest_sha256"
                ],
                "baseline_trades_sha256": results[sample]["spec"][
                    "baseline_trades_sha256"
                ],
                "baseline_replay_sha256": results[sample]["baseline_replay_sha"],
                "baseline_replay_matches_frozen": results[sample][
                    "baseline_replay_matches_frozen"
                ],
                "medium_rows_byte_unchanged": results[sample]["medium_unchanged"],
                "medium_rows_match_frozen_baseline": results[sample][
                    "medium_matches_frozen_baseline"
                ],
                "expected_sessions": results[sample]["spec"]["expected_sessions"],
                "episode_count_run": results[sample]["new_stats"]["episode_count_run"],
                "leg_row_count": results[sample]["new_stats"]["leg_row_count"],
                "max_open_legs_observed": results[sample]["new_stats"][
                    "max_open_legs_observed"
                ],
                "filled_add_events": results[sample]["new_stats"]["filled_add_events"],
                "extension_analysis": {
                    key: value
                    for key, value in results[sample]["analysis"].items()
                    if key != "blocked_all"
                },
                "new_metrics": results[sample]["new_blocks"],
                "baseline_metrics": results[sample]["base_blocks"],
                "gates": results[sample]["gates"],
                "status": results[sample]["status"],
            }
            for sample in samples
        },
    }

    artifact_hashes = {
        name: pilot.sha256_bytes(blob) for name, blob in payloads.items()
    }
    report = render_report_zh(
        run_id=run_id,
        spec_path=spec_path,
        spec_sha=spec_sha,
        spec_bytes=len(spec_bytes_raw),
        results=results,
        source_sha256=source_sha256,
        artifact_sha256=artifact_hashes,
        overall_status=overall_status,
    )
    payloads["report_zh.md"] = report.encode("utf-8")
    artifact_hashes = {
        name: pilot.sha256_bytes(blob) for name, blob in payloads.items()
    }

    for label, (path, expected) in pinned_files.items():
        if _sample_sha256(path) != expected:
            raise LongExtensionError(f"{label} changed during this run")

    for name, blob in payloads.items():
        (run_dir / name).write_bytes(blob)
    (run_dir / "manifest.json").write_bytes(pilot._json_bytes(manifest))

    all_names = list(artifact_hashes) + ["manifest.json"]
    declared = dict(artifact_hashes)
    declared["manifest.json"] = pilot.sha256_file(run_dir / "manifest.json")
    verified = {name: pilot.sha256_file(run_dir / name) for name in all_names}
    readback = {
        "schema_version": READBACK_SCHEMA_VERSION,
        "module_version": MODULE_VERSION,
        "run_id": run_id,
        "variant_id": VARIANT_ID,
        "extension_rule": EXTENSION_RULE,
        "status": overall_status,
        "research_only": True,
        "point_in_time_verified": False,
        "formal_historical_performance": False,
        "not_independent_blind_test": True,
        "samples": manifest["samples"],
        "source_sha256": source_sha256,
        "artifact_sha256": declared,
        "artifact_sha256_recomputed": verified,
        "artifacts_verified": declared == verified,
        "git_state": git_state,
        "runner_commit_state": commit_state,
        "checkpoint": {
            "kind": "ATOMIC_H1H2_LONG_FASTMA_EXTENSION",
            "state": "COMPLETE_AWAITING_00_QA",
            "resumable": False,
            "next_action": (
                "00 reviews the H1/H2 causal replay of the long SMA21 5% gate; "
                "in-sample only, no 2023/2024 read, nothing deployed"
            ),
        },
    }
    (run_dir / "readback.json").write_bytes(pilot._json_bytes(readback))
    return manifest


def _progress_printer(stage: str, done: int, total: int, extra: Mapping[str, Any]) -> None:
    print(f"[{stage}] {done}/{total} {dict(extra)}", flush=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "2022 H1/H2 causal replay of the long-only T close >= SMA21(T)*1.05 gate"
        )
    )
    parser.add_argument("--workdata-root", required=True)
    parser.add_argument("--output-root", default=None)
    parser.add_argument("--run-id", default=RUN_ID_DEFAULT)
    parser.add_argument("--episode-limit", type=int, default=None)
    parser.add_argument("--samples", default="H1,H2")
    args = parser.parse_args(argv)
    samples = tuple(
        part.strip().upper() for part in args.samples.split(",") if part.strip()
    )
    manifest = run_long_extension(
        workdata_root=args.workdata_root,
        output_root=args.output_root,
        run_id=args.run_id,
        episode_limit=args.episode_limit,
        samples=samples,
        progress=_progress_printer,
    )
    print(f"run_id={manifest['run_id']} status={manifest['status']}", flush=True)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
