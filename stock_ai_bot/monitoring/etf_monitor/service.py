from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from .accounting import CorporateActionAccounting
from .config import EtfMonitorConfig
from .core import AcceptedFinalV17Core
from .data import MonitorMarketDataProvider, OfficialTwseTradingCalendar
from .errors import (
    EtfMonitorDataError,
    EtfMonitorNextSessionOpenUnavailable,
    EtfMonitorRoutingError,
    EtfMonitorStateError,
)
from .messages import render_core_message, render_data_unavailable, render_market_closed, render_offline_test
from .models import (
    Decision,
    MonitorPhase,
    PhaseOutcome,
    accepted_core_payload,
    canonical_json,
    stable_event_id,
    stable_sha256,
)
from .routing import FailClosedTelegramRouter
from .store import EtfMonitorStore


TAIPEI = ZoneInfo("Asia/Taipei")


def _material_state(core_payload: Mapping[str, Any]) -> Mapping[str, Any]:
    core = core_payload.get("core", core_payload)
    if not isinstance(core, Mapping):
        return {}
    return {
        key: core.get(key)
        for key in (
            "decision",
            "regime",
            "active_position",
            "active_leg_id",
            "fixed_initial_stop",
            "macd_armed",
            "exit_reason",
            "signal_reference_price",
            "signal_transaction",
        )
    }


def _trading_day_result(value: Any) -> tuple[bool, Mapping[str, Any]]:
    if isinstance(value, bool):
        return value, {"is_trading_day": value, "source": "INJECTED_TEST_GATE"}
    if isinstance(value, Mapping) and isinstance(value.get("is_trading_day"), bool):
        return bool(value["is_trading_day"]), dict(value)
    raise TypeError("ETF trading-day gate returned an invalid result")


def _durable_calendar_evidence(evidence: Mapping[str, Any]) -> Mapping[str, Any]:
    """Remove observation-time fields so restart retries keep a stable event payload."""

    return {
        key: value
        for key, value in evidence.items()
        if key not in {"fetched_at"}
    }


class EtfMonitorService:
    def __init__(
        self,
        *,
        config: EtfMonitorConfig,
        store: EtfMonitorStore,
        data_provider: MonitorMarketDataProvider,
        router: FailClosedTelegramRouter | None,
        core: AcceptedFinalV17Core | None = None,
        accounting: CorporateActionAccounting | None = None,
        trading_day_predicate: Callable[[date], Any] | None = None,
    ):
        self.config = config
        self.store = store
        self.data_provider = data_provider
        self.router = router
        self.core = core or AcceptedFinalV17Core()
        self.accounting = accounting or CorporateActionAccounting()
        self.trading_day_predicate = (
            trading_day_predicate or OfficialTwseTradingCalendar().verify
        )

    async def _deliver(self, event_id: str, *, has_outbox: bool = True) -> None:
        if has_outbox and self.router is not None:
            await self.router.deliver(event_id)

    async def _next_trading_session_after(
        self, signal_date: date
    ) -> tuple[date, Mapping[str, Any]]:
        """Pin the next official trading session instead of the next observed run."""

        for offset in range(1, 46):
            candidate = signal_date + timedelta(days=offset)
            gate_value = await asyncio.to_thread(self.trading_day_predicate, candidate)
            is_trading_day, evidence = _trading_day_result(gate_value)
            if is_trading_day:
                return candidate, _durable_calendar_evidence(evidence)
        raise EtfMonitorDataError(
            f"official next trading session not found within 45 days after {signal_date}"
        )

    async def _data_unavailable(
        self,
        *,
        phase: MonitorPhase,
        trading_date: date,
        exc: BaseException,
    ) -> PhaseOutcome:
        reason_code = type(exc).__name__.upper()
        decision_event = (
            self.store.latest_formal_phase_event(
                phase=MonitorPhase.DECISION,
                trading_date=trading_date,
            )
            if phase is MonitorPhase.RECONCILIATION
            else None
        )
        original_event_id = (
            None if decision_event is None else str(decision_event["event_id"])
        )
        event_id = stable_event_id(
            symbol="0050",
            strategy="final-v1.7",
            phase=phase,
            signal_date=trading_date,
            decision=Decision.DATA_UNAVAILABLE,
            suffix=f"{reason_code}:{original_event_id or 'NO_SAME_DAY_STATE'}",
        )
        quarantined = None
        if original_event_id is not None:
            quarantined = self.store.quarantine_pending_for_reconciliation(
                original_event_id=original_event_id,
                failure_event_id=event_id,
            )
        body = render_data_unavailable(
            phase=phase,
            trading_date=trading_date.isoformat(),
            reason_code=reason_code,
        )
        self.store.append_event_with_outbox(
            event_id=event_id,
            phase=phase,
            trading_date=trading_date,
            event_type="DATA_UNAVAILABLE",
            payload={
                "reason_code": reason_code,
                "fail_closed": True,
                "same_day_state_event_id": original_event_id,
                "pending_quarantined": quarantined is not None,
            },
            target_chat_id=self.config.chat_id,
            body=body,
        )
        await self._deliver(event_id)
        return PhaseOutcome(
            phase=phase,
            trading_date=trading_date,
            status="DATA_UNAVAILABLE",
            event_ids=(event_id,),
            decision=Decision.DATA_UNAVAILABLE,
            message=reason_code,
            metadata={"quarantined_pending_action": quarantined},
        )

    async def _closed_day(
        self, phase: MonitorPhase, trading_date: date, evidence: Mapping[str, Any]
    ) -> PhaseOutcome:
        ids: tuple[str, ...] = ()
        # A single scheduled daily status, not three notifications and not a
        # HOLD trading decision. Never read prices or mutate position state.
        if phase is MonitorPhase.RECONCILIATION and self.config.daily_shadow_status:
            event_id = stable_event_id(
                symbol="0050", strategy="final-v1.7", phase=phase,
                signal_date=trading_date, decision=Decision.HOLD, suffix="MARKET_CLOSED",
            )
            existing = self.store.event(event_id)
            if existing is not None:
                if (existing["event_type"] != "MARKET_CLOSED"
                    or existing["phase"] != phase.value
                    or existing["trading_date"] != trading_date.isoformat()):
                    raise EtfMonitorStateError("market-closed event identity collision")
            else:
                self.store.append_event_with_outbox(
                    event_id=event_id, phase=phase, trading_date=trading_date,
                    event_type="MARKET_CLOSED",
                    payload={"market_status": "CLOSED", "trading_day_gate": evidence},
                    target_chat_id=self.config.chat_id,
                    body=render_market_closed(trading_date=trading_date.isoformat()),
                )
            await self._deliver(event_id)
            ids = (event_id,)
        self.store.finish_job(phase, trading_date, succeeded=True)
        print(f"[0050 趨勢策略] {trading_date.isoformat()} 今日休市", flush=True)
        return PhaseOutcome(
            phase, trading_date, "SKIPPED_OFFICIAL_NON_TRADING_DAY",
            event_ids=ids, message="今日休市", metadata={"trading_day_gate": evidence},
        )

    async def run(self, phase: MonitorPhase, *, now: datetime | None = None) -> PhaseOutcome:
        if phase not in {
            MonitorPhase.PREVIEW,
            MonitorPhase.DECISION,
            MonitorPhase.RECONCILIATION,
        }:
            raise ValueError("unsupported scheduled ETF monitor phase")
        local_now = (now or datetime.now(tz=TAIPEI)).astimezone(TAIPEI)
        trading_date = local_now.date()
        if not self.store.begin_job(phase, trading_date):
            return PhaseOutcome(phase, trading_date, "ALREADY_COMPLETED")
        try:
            try:
                gate_value = await asyncio.to_thread(
                    self.trading_day_predicate, trading_date
                )
                is_trading_day, trading_day_evidence = _trading_day_result(gate_value)
                trading_day_evidence = _durable_calendar_evidence(
                    trading_day_evidence
                )
                if not is_trading_day:
                    return await self._closed_day(phase, trading_date, trading_day_evidence)
                snapshot = await self.data_provider.load(phase, trading_date)
                core = await asyncio.to_thread(self.core.evaluate, snapshot)
                accounting_validation: Mapping[str, Any] = {}
                if phase is not MonitorPhase.PREVIEW:
                    accounting_validation = await asyncio.to_thread(
                        self.accounting.validate, snapshot
                    )
                shadow_fills: tuple[Mapping[str, Any], ...] = ()
                if phase is MonitorPhase.DECISION:
                    due_actions = await asyncio.to_thread(
                        self.store.confirmed_pending_actions_due_by,
                        trading_date=trading_date,
                    )
                    for action in due_actions:
                        expected_date = date.fromisoformat(str(action["expected_fill_date"]))
                        expected_row = snapshot.raw_row_for(expected_date)
                        if expected_row is None:
                            raise EtfMonitorNextSessionOpenUnavailable(
                                "verified raw open is unavailable for fixed next session "
                                f"{expected_date}; action {action['action_id']} remains pending"
                            )
                        fills = await asyncio.to_thread(
                            self.store.settle_pending_actions_at_open,
                            trading_date=expected_date,
                            raw_open=Decimal(str(expected_row["open"])),
                            snapshot_id=snapshot.snapshot_id,
                        )
                        shadow_fills = (*shadow_fills, *fills)
                    shadow_fills = await asyncio.to_thread(
                        self.store.shadow_fills_for_snapshot,
                        snapshot_id=snapshot.snapshot_id,
                    )
            except EtfMonitorRoutingError:
                raise
            except Exception as exc:
                outcome = await self._data_unavailable(
                    phase=phase,
                    trading_date=trading_date,
                    exc=exc,
                )
                self.store.finish_job(
                    phase,
                    trading_date,
                    succeeded=False,
                    error=f"DATA_UNAVAILABLE:{outcome.message}",
                )
                return outcome

            correction_of: str | None = None
            correction_reason: str | None = None
            prior_same_day_event_id: str | None = None
            decision_event = (
                self.store.latest_formal_phase_event(
                    phase=MonitorPhase.DECISION,
                    trading_date=trading_date,
                )
                if phase is MonitorPhase.RECONCILIATION
                else None
            )
            if decision_event is not None:
                prior_same_day_event_id = str(decision_event["event_id"])
                prior_payload = json.loads(str(decision_event["payload_json"]))
                current_payload = {
                    "core": json.loads(canonical_json(accepted_core_payload(core)))
                }
                if stable_sha256(_material_state(prior_payload)) != stable_sha256(
                    _material_state(current_payload)
                ):
                    correction_of = prior_same_day_event_id
                    correction_reason = "FULL_DAY_ACCEPTED_DATA_CHANGED_MATERIAL_STATE"

            expected_fill_date: date | None = None
            expected_fill_calendar_evidence: Mapping[str, Any] | None = None
            if phase is MonitorPhase.RECONCILIATION and core.decision in {
                Decision.BUY_NEXT_OPEN,
                Decision.SELL_NEXT_OPEN,
            }:
                (
                    expected_fill_date,
                    expected_fill_calendar_evidence,
                ) = await self._next_trading_session_after(trading_date)

            suffix = None
            event_type = core.decision.value
            if phase is MonitorPhase.RECONCILIATION:
                event_type = "CORRECTION" if correction_of else "RECONCILIATION_CONFIRMED"
                suffix = correction_of or snapshot.snapshot_sha256[:16]
            event_id = stable_event_id(
                symbol="0050",
                strategy="final-v1.7",
                phase=phase,
                signal_date=trading_date,
                decision=core.decision,
                suffix=suffix,
            )
            existing_event = self.store.event(event_id)
            existing_payload: Mapping[str, Any] | None = None
            if existing_event is not None:
                if (
                    existing_event["phase"] != phase.value
                    or existing_event["trading_date"] != trading_date.isoformat()
                    or existing_event["event_type"] != event_type
                    or existing_event["supersedes_event_id"] != correction_of
                ):
                    raise EtfMonitorStateError(
                        "stable event id collided with different event metadata"
                    )
                decoded = json.loads(str(existing_event["payload_json"]))
                if not isinstance(decoded, Mapping):
                    raise EtfMonitorStateError("existing stable event payload is invalid")
                existing_payload = decoded
            should_notify = not (
                phase is MonitorPhase.DECISION
                and core.decision is Decision.HOLD
                and not self.config.daily_shadow_status
            )
            pending_transition = None
            if phase is MonitorPhase.RECONCILIATION:
                durable_transition = (
                    None
                    if existing_payload is None
                    else existing_payload.get("pending_transition")
                )
                if existing_payload is not None and not isinstance(
                    durable_transition, Mapping
                ):
                    raise EtfMonitorStateError(
                        "existing reconciliation event lacks a durable transition plan"
                    )
                pending_transition = (
                    durable_transition
                    if durable_transition is not None
                    else self.store.describe_pending_transition(
                        original_event_id=prior_same_day_event_id,
                        core=core,
                    )
                )
            body = render_core_message(
                phase=phase,
                core=core,
                snapshot=snapshot,
                correction_of=correction_of,
                pending_transition=pending_transition,
            )
            payload = {
                "core": accepted_core_payload(core),
                "data": {
                    "source": snapshot.source,
                    "price_basis": snapshot.price_basis,
                    "as_of": snapshot.as_of,
                    "snapshot_id": snapshot.snapshot_id,
                    "snapshot_version": snapshot.snapshot_version,
                    "snapshot_sha256": snapshot.snapshot_sha256,
                    "is_complete": snapshot.is_complete,
                    "is_provisional": snapshot.is_provisional,
                },
                "correction_of": correction_of,
                "correction_reason": correction_reason,
                "pending_transition": pending_transition,
                "shadow_next_open_fills": shadow_fills,
                "next_session_open_gate": (
                    None
                    if expected_fill_date is None
                    else {
                        "expected_fill_date": expected_fill_date,
                        "calendar_evidence": expected_fill_calendar_evidence,
                    }
                ),
                "trading_day_gate": trading_day_evidence,
            }
            self.store.append_event_with_outbox(
                event_id=event_id,
                phase=phase,
                trading_date=trading_date,
                event_type=event_type,
                payload=payload,
                target_chat_id=self.config.chat_id if should_notify else None,
                body=body if should_notify else None,
                supersedes_event_id=correction_of,
            )

            accounting_summary: Mapping[str, Any] = accounting_validation
            if phase is not MonitorPhase.PREVIEW:
                accounting_summary = await asyncio.to_thread(
                    self.accounting.reconcile, snapshot, self.store
                )
                self.store.save_core_state(core=core, snapshot=snapshot, event_id=event_id)
                pending_transition = self.store.reconcile_pending_action(
                    core=core,
                    event_id=event_id,
                    original_event_id=prior_same_day_event_id,
                    expected_fill_date=expected_fill_date,
                    expected_fill_calendar_evidence=expected_fill_calendar_evidence,
                    planned_transition=pending_transition,
                )
            if correction_of is not None:
                self.store.append_correction(
                    correction_id=f"correction:{event_id}",
                    original_event_id=correction_of,
                    corrected_event_id=event_id,
                    reason=str(correction_reason),
                    payload=payload,
                )
            await self._deliver(event_id, has_outbox=should_notify)
            self.store.finish_job(phase, trading_date, succeeded=True)
            return PhaseOutcome(
                phase=phase,
                trading_date=trading_date,
                status="CORRECTED" if correction_of else "COMPLETED",
                event_ids=(event_id,),
                decision=core.decision,
                metadata={
                    "accounting": accounting_summary,
                    "shadow_next_open_fills": shadow_fills,
                    "trading_day_gate": trading_day_evidence,
                },
            )
        except EtfMonitorRoutingError as exc:
            self.store.finish_job(
                phase,
                trading_date,
                succeeded=False,
                error=type(exc).__name__,
            )
            raise
        except Exception as exc:
            self.store.finish_job(
                phase,
                trading_date,
                succeeded=False,
                error=type(exc).__name__,
            )
            raise

    def create_offline_test_events(self, *, trading_date: date) -> tuple[str, ...]:
        ids = []
        cases = (
            (Decision.BUY_NEXT_OPEN, "BUY"),
            (Decision.SELL_NEXT_OPEN, "SELL"),
            (Decision.HOLD, "HOLD"),
            (Decision.HOLD, "CORRECTION"),
            (Decision.HOLD, "RESTART_RECOVERY"),
        )
        for decision, marker in cases:
            event_id = stable_event_id(
                symbol="0050",
                strategy="final-v1.7",
                phase=MonitorPhase.TEST,
                signal_date=trading_date,
                decision=decision,
                suffix=marker,
            )
            self.store.append_event_with_outbox(
                event_id=event_id,
                phase=MonitorPhase.TEST,
                trading_date=trading_date,
                event_type=f"OFFLINE_TEST_{marker}",
                payload={"test": True, "case": marker, "decision": decision},
                target_chat_id=None,
                body=None,
                is_test=True,
            )
            ids.append(event_id)
        return tuple(ids)
