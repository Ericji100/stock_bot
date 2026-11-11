"""Resume failed reconciliation and same-day decision jobs in the existing Bot."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .models import MonitorPhase


RECOVERY_INTERVAL_SECONDS = 300
RETRYABLE_JOB_STATUSES = {"FAILED", "INTERRUPTED", "RETRY_PENDING"}


def decision_recovery_open(config, now: datetime) -> bool:
    decision_at = datetime.combine(now.date(), config.schedule.decision, now.tzinfo)
    reconciliation_at = datetime.combine(now.date(), config.schedule.reconciliation, now.tzinfo)
    return decision_at <= now and (
        now + timedelta(seconds=config.job_timeouts_seconds["decision"]) < reconciliation_at
    )


async def decision_recovery_sweep(runtime) -> None:
    now = datetime.now(ZoneInfo(runtime.config.timezone))
    if runtime.shutting_down or runtime.startup_status != "READY" or not decision_recovery_open(runtime.config, now):
        return
    day = now.date()
    phase = MonitorPhase.DECISION
    if runtime.store.job_status(phase, day) not in RETRYABLE_JOB_STATUSES:
        return
    if runtime.store.job_status(MonitorPhase.RECONCILIATION, day) is not None:
        return
    note = (f"行情恢復後補發｜原定 {day} {runtime.config.schedule.decision:%H:%M}\n"
            f"補核時間：{now:%Y-%m-%d %H:%M}\n"
            "本次使用通過收盤驗證的當日官方即時行情。")
    await runtime.dispatch(phase, trading_date=day, delivery_note=note, single_attempt=True)


async def reconciliation_recovery_sweep(runtime) -> None:
    now = datetime.now(ZoneInfo(runtime.config.timezone))
    from .service import _trading_day_result

    through = now.date()
    if now.time().replace(tzinfo=None) < runtime.config.schedule.reconciliation:
        through -= timedelta(days=1)
    discover = getattr(runtime.store, "missing_reconciliation_dates", None)
    if callable(discover) and hasattr(runtime.service, "trading_day_predicate"):
        candidates = await asyncio.to_thread(discover, through)
        for day in candidates:
            if runtime.shutting_down:
                return
            is_open, _ = _trading_day_result(await asyncio.to_thread(runtime.service.trading_day_predicate, day))
            if is_open:
                runtime.store.queue_reconciliation(day)
    dates = await asyncio.to_thread(runtime.store.reconciliation_retry_dates)
    runtime.reconciliation_recovery = {
        "status": "CHECKING", "pending_dates": [str(day) for day in dates],
        "checked_at": now.isoformat(), "next_check_at": None,
    }
    for day in dates:
        if runtime.shutting_down or runtime.startup_status != "READY":
            return
        if day > now.date():
            continue
        if day == now.date() and now.time().replace(tzinfo=None) < runtime.config.schedule.reconciliation:
            continue
        note = (f"資料恢復後補發｜原定 {day} {runtime.config.schedule.reconciliation:%H:%M}\n"
                f"補核時間：{datetime.now(ZoneInfo(runtime.config.timezone)):%Y-%m-%d %H:%M}\n"
                "本次使用原檢查日期的完整官方資料重新驗證。")
        await runtime.dispatch(MonitorPhase.RECONCILIATION, trading_date=day,
                               delivery_note=note, single_attempt=True)
    pending = await asyncio.to_thread(runtime.store.reconciliation_retry_dates)
    runtime.reconciliation_recovery.update(
        status="RETRY_WAIT" if pending else "IDLE",
        pending_dates=[str(day) for day in pending],
        next_check_at=(datetime.now(ZoneInfo(runtime.config.timezone)) +
                       timedelta(seconds=RECOVERY_INTERVAL_SECONDS)).isoformat(),
    )


async def reconciliation_recovery_loop(application, runtime) -> None:
    from .integration import RUNTIME_KEY, _report_runtime_status

    while not runtime.shutting_down and application.bot_data.get(RUNTIME_KEY) is runtime:
        try:
            if runtime.startup_status == "READY":
                await reconciliation_recovery_sweep(runtime)
                await decision_recovery_sweep(runtime)
                from .execution import refresh_execution_status

                if hasattr(runtime.store, "transaction"):
                    await asyncio.to_thread(refresh_execution_status, runtime.store)
                    from .acceptance import closeout_sweep

                    await closeout_sweep(runtime)
        except Exception as exc:
            # A broken endpoint or even a temporary SQLite error must not kill
            # the only recovery loop. Cancellation deliberately propagates.
            await runtime.report_failure("reconciliation_recovery", exc)
            runtime.reconciliation_recovery = {
                "status": "ERROR_RETRY_WAIT",
                "next_check_at": (datetime.now(ZoneInfo(runtime.config.timezone)) +
                                   timedelta(seconds=RECOVERY_INTERVAL_SECONDS)).isoformat(),
            }
        _report_runtime_status(application, runtime)
        await asyncio.sleep(RECOVERY_INTERVAL_SECONDS)
