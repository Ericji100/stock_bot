from __future__ import annotations

import asyncio
import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from stock_ai_bot.strategies.chip_strategies import is_possible_trading_day

from .accounting import CorporateActionAccounting
from .config import EtfMonitorConfig
from .core import AcceptedFinalV17Core
from .data import MonitorMarketDataProvider
from .errors import EtfMonitorRoutingError
from .messages import render_core_message, render_data_unavailable, render_offline_test
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
        trading_day_predicate: Callable[[date], bool] = is_possible_trading_day,
    ):
        self.config = config
        self.store = store
        self.data_provider = data_provider
        self.router = router
        self.core = core or AcceptedFinalV17Core()
        self.accounting = accounting or CorporateActionAccounting()
        self.trading_day_predicate = trading_day_predicate

    async def _deliver(self, event_id: str, *, has_outbox: bool = True) -> None:
        if has_outbox and self.router is not None:
            await self.router.deliver(event_id)

    async def _data_unavailable(
        self,
        *,
        phase: MonitorPhase,
        trading_date: date,
        exc: BaseException,
    ) -> PhaseOutcome:
        reason_code = type(exc).__name__.upper()
        event_id = stable_event_id(
            symbol="0050",
            strategy="final-v1.7",
            phase=phase,
            signal_date=trading_date,
            decision=Decision.DATA_UNAVAILABLE,
            suffix=reason_code,
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
            payload={"reason_code": reason_code, "fail_closed": True},
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
        if not self.trading_day_predicate(trading_date):
            return PhaseOutcome(phase, trading_date, "SKIPPED_NON_TRADING_DAY")
        if not self.store.begin_job(phase, trading_date):
            return PhaseOutcome(phase, trading_date, "ALREADY_COMPLETED")
        try:
            try:
                snapshot = await self.data_provider.load(phase, trading_date)
                core = await asyncio.to_thread(self.core.evaluate, snapshot)
                accounting_validation: Mapping[str, Any] = {}
                if phase is not MonitorPhase.PREVIEW:
                    accounting_validation = await asyncio.to_thread(
                        self.accounting.validate, snapshot
                    )
                shadow_fills: tuple[Mapping[str, Any], ...] = ()
                if phase in {MonitorPhase.PREVIEW, MonitorPhase.DECISION}:
                    current_row = snapshot.raw_row_for(trading_date)
                    if current_row is None:
                        raise ValueError("current trading-day raw open is unavailable")
                    shadow_fills = await asyncio.to_thread(
                        self.store.settle_pending_actions_at_open,
                        trading_date=trading_date,
                        raw_open=Decimal(str(current_row["open"])),
                        snapshot_id=snapshot.snapshot_id,
                    )
            except Exception as exc:
                outcome = await self._data_unavailable(
                    phase=phase,
                    trading_date=trading_date,
                    exc=exc,
                )
                self.store.finish_job(phase, trading_date, succeeded=True)
                return outcome

            previous = self.store.latest_state()
            correction_of: str | None = None
            correction_reason: str | None = None
            prior_same_day_event_id: str | None = None
            if (
                phase is MonitorPhase.RECONCILIATION
                and previous is not None
                and str(previous["as_of"]) == trading_date.isoformat()
            ):
                prior_same_day_event_id = str(previous["event_id"])
                prior_payload = json.loads(str(previous["core_state_json"]))
                current_payload = {
                    "core": json.loads(canonical_json(accepted_core_payload(core)))
                }
                if stable_sha256(_material_state(prior_payload)) != stable_sha256(
                    _material_state(current_payload)
                ):
                    correction_of = str(previous["event_id"])
                    correction_reason = "FULL_DAY_ACCEPTED_DATA_CHANGED_MATERIAL_STATE"

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
            should_notify = not (
                phase is MonitorPhase.DECISION
                and core.decision is Decision.HOLD
                and not self.config.daily_shadow_status
            )
            pending_transition = None
            if prior_same_day_event_id is not None:
                pending_transition = self.store.describe_pending_transition(
                    original_event_id=prior_same_day_event_id,
                    core=core,
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
