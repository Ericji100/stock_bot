"""Verify a missed historical EOD while preserving the newer live projection."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime

from .models import Decision, MonitorPhase, accepted_core_payload, canonical_json, stable_event_id
from .service import TAIPEI, _material_state, _trading_day_result
from .errors import EtfMonitorDataError


async def reconcile_historical(service, day) -> None:
    store = service.store
    phase = MonitorPhase.RECONCILIATION
    if store.job_status(phase, day) == "REVIEW_REQUIRED":
        return
    if not store.begin_job(phase, day):
        return
    try:
        is_open, calendar = _trading_day_result(await asyncio.to_thread(service.trading_day_predicate, day))
        if not is_open:
            await service._closed_day(phase, day, calendar)
            return
        snapshot = await service.data_provider.load(phase, day)
        if not snapshot.is_complete or snapshot.as_of != day:
            raise EtfMonitorDataError("historical reconciliation requires complete matching-date data")
        core = await asyncio.to_thread(service.core.evaluate, snapshot)
        accounting = await asyncio.to_thread(service.accounting.validate, snapshot)
        prior = store.latest_formal_phase_event(phase=MonitorPhase.DECISION, trading_date=day)
        prior_payload = None if prior is None else json.loads(prior["payload_json"])
        matches = prior_payload is not None and canonical_json(_material_state(prior_payload)) == canonical_json(
            _material_state(accepted_core_payload(core)))
        # A verified, unchanged HOLD with no associated pending instruction is
        # safe to audit retrospectively. Economic transitions need review.
        with store.transaction() as connection:
            pending = connection.execute(
                "SELECT COUNT(*) FROM pending_actions WHERE signal_date=?", (day.isoformat(),)
            ).fetchone()[0]
        verified = matches and core.decision is Decision.HOLD and pending == 0
        kind = "HISTORICAL_RECONCILIATION_VERIFIED" if verified else "HISTORICAL_RECONCILIATION_REVIEW"
        event_id = stable_event_id(symbol="0050", strategy=service.config.strategy, phase=phase,
                                   signal_date=day, decision=kind)
        body = (f"0050 趨勢策略｜歷史晚間核對{'補充' if verified else '待覆核'}\n"
                f"檢查日期：{day}\n補核時間：{datetime.now(TAIPEI):%Y-%m-%d %H:%M}\n"
                f"官方收盤價：{core.raw_close:.2f} 元\n"
                + ("完整官方資料與當日收盤判讀一致，歷史核對已補齊。" if verified else
                   "完整官方資料涉及歷史交易安排或判讀差異，請先覆核原通知與實際操作。")
                + "\n此為歷史補核紀錄，當前策略狀態保留較新日期的判讀。")
        if store.event(event_id) is None:
            store.append_event_with_outbox(
                event_id=event_id, phase=phase, trading_date=day, event_type=kind,
                payload={"core": accepted_core_payload(core), "data": {
                    "as_of": snapshot.as_of, "snapshot_sha256": snapshot.snapshot_sha256,
                    "is_complete": snapshot.is_complete}, "accounting": accounting,
                    "same_day_decision_event_id": None if prior is None else prior["event_id"],
                    "matches_same_day_decision": matches, "live_state_rewritten": False},
                target_chat_id=service.config.chat_id, body=body,
            )
        await service._deliver(event_id)
        store.finish_job(phase, day, succeeded=verified,
                         error=None if verified else "HISTORICAL_REVIEW_REQUIRED")
        if not verified:
            with store.transaction() as connection:
                connection.execute("UPDATE job_runs SET status='REVIEW_REQUIRED' WHERE run_key=?",
                                   (f"{phase.value}:{day}",))
    except asyncio.CancelledError:
        store.finish_job(phase, day, succeeded=False, error="CANCELLED_RECOVERY_PENDING")
        raise
    except Exception as exc:
        store.finish_job(phase, day, succeeded=False, error=type(exc).__name__)
        raise
