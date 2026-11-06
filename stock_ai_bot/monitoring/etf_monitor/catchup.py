"""Same-day restart catch-up without inventing historical intraday prices."""
from __future__ import annotations

import asyncio
from datetime import datetime, time
from zoneinfo import ZoneInfo

from .models import MonitorPhase, stable_event_id
from .recovery import RETRYABLE_JOB_STATUSES, decision_recovery_open
from .service import _trading_day_result


async def catch_up_today(runtime, *, now: datetime | None = None) -> None:
    current = (now or datetime.now(ZoneInfo(runtime.config.timezone))).astimezone(ZoneInfo(runtime.config.timezone))
    day = current.date()
    is_open, _ = _trading_day_result(await asyncio.to_thread(runtime.service.trading_day_predicate, day))
    if not is_open:
        return
    for phase, scheduled in (
        (MonitorPhase.PREVIEW, runtime.config.schedule.preview),
        (MonitorPhase.DECISION, runtime.config.schedule.decision),
        (MonitorPhase.RECONCILIATION, runtime.config.schedule.reconciliation),
    ):
        if runtime.shutting_down or runtime.startup_status != "READY":
            return
        if current.time().replace(tzinfo=None) < scheduled:
            continue
        status = runtime.store.job_status(phase, day)
        if status in {"RUNNING", "SUCCEEDED"}:
            continue
        if phase is MonitorPhase.DECISION and not decision_recovery_open(runtime.config, current):
            continue
        if (phase in {MonitorPhase.DECISION, MonitorPhase.RECONCILIATION}
            and status in RETRYABLE_JOB_STATUSES
            and getattr(runtime, "reconciliation_recovery_task", None) is not None):
            # The durable recovery loop owns failed work.
            continue
        if phase is MonitorPhase.PREVIEW and current.time().replace(tzinfo=None) >= time(13, 30):
            event_id = stable_event_id(symbol="0050", strategy=runtime.config.strategy,
                phase=MonitorPhase.HEALTH, signal_date=day, decision="MISSED_PREVIEW")
            if runtime.store.event(event_id) is None:
                runtime.store.append_event_with_outbox(
                    event_id=event_id, phase=MonitorPhase.HEALTH, trading_date=day,
                    event_type="MISSED_PREVIEW", payload={"original_phase": phase.value, "historical_quote_available": False},
                    target_chat_id=runtime.config.chat_id,
                    body=(f"0050 趨勢策略｜盤中預覽缺漏補充\n日期：{day}\n"
                          f"補發時間：{current:%H:%M}\n原定 12:45 盤中預覽因監控未就緒而漏發。\n"
                          "當時行情未保存，無法還原該時點預覽；本訊息不構成交易訊號。\n"
                          "今日正式判讀另以可驗證的官方行情補發。"),
                )
            await runtime.router.deliver(event_id)
            continue
        note = (f"補發通知｜原定 {day} {scheduled:%H:%M}\n"
                f"恢復後於 {current:%Y-%m-%d %H:%M} 判讀；使用恢復時取得的官方資料，請以文內價格時間為準。")
        await runtime.dispatch(phase, delivery_note=note)
