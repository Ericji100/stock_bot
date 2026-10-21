from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from stock_ai_bot.workdata import get_workdata_path

from .config import EtfMonitorConfig, load_etf_monitor_config
from .models import MonitorPhase


RUNTIME_KEY = "etf_monitor_final_v1_7_runtime"
JOB_NAMES = {
    MonitorPhase.PREVIEW: "etf-0050-final-v1.7-preview-1245",
    MonitorPhase.DECISION: "etf-0050-final-v1.7-decision-1355",
    MonitorPhase.RECONCILIATION: "etf-0050-final-v1.7-reconciliation-2200",
}
RETRY_DELAYS_SECONDS = {
    MonitorPhase.PREVIEW: (30, 90),
    MonitorPhase.DECISION: (30, 90, 180),
    MonitorPhase.RECONCILIATION: (60, 180, 300),
}


@dataclass
class EtfMonitorRuntime:
    config: EtfMonitorConfig
    store: Any
    router: Any
    service: Any
    locks: dict[MonitorPhase, asyncio.Lock]

    async def dispatch(self, phase: MonitorPhase) -> None:
        lock = self.locks[phase]
        if lock.locked():
            return
        timeout = self.config.job_timeouts_seconds[
            {
                MonitorPhase.PREVIEW: "preview",
                MonitorPhase.DECISION: "decision",
                MonitorPhase.RECONCILIATION: "reconciliation",
            }[phase]
        ]
        async with lock:
            delays = RETRY_DELAYS_SECONDS[phase]
            for attempt in range(len(delays) + 1):
                try:
                    outcome = await asyncio.wait_for(
                        self.service.run(phase), timeout=timeout
                    )
                except Exception as exc:
                    print(
                        f"[ETF 0050 final-v1.7] {phase.value} failed: "
                        f"{type(exc).__name__}",
                        flush=True,
                    )
                    return
                if outcome.status != "DATA_UNAVAILABLE" or attempt == len(delays):
                    return
                await asyncio.sleep(delays[attempt])


async def _scheduled_job(context: Any) -> None:
    runtime = context.application.bot_data.get(RUNTIME_KEY)
    phase = MonitorPhase(str(context.job.data["phase"]))
    if not isinstance(runtime, EtfMonitorRuntime):
        return
    context.application.create_task(
        runtime.dispatch(phase),
        name=JOB_NAMES[phase],
    )


def register_etf_monitor(application: Any, *, config_path: Any = None) -> EtfMonitorRuntime | None:
    """Attach final-v1.7 jobs to the existing Application and JobQueue.

    Disabled configuration is a strict no-op: no database, network request,
    handler, bot, polling process, or schedule is created.
    """

    config = load_etf_monitor_config(config_path)
    if not config.enabled:
        return None
    if application.job_queue is None:
        raise RuntimeError("ETF monitor requires the existing Telegram JobQueue")

    from .data import (
        MonitorMarketDataProvider,
        OfficialDailyDeltaUpdater,
        OfficialTwseQuoteProvider,
        OfficialTwseTradingCalendar,
        RegistrySnapshotProvider,
    )
    from .routing import FailClosedTelegramRouter
    from .service import EtfMonitorService
    from .store import EtfMonitorStore

    store = EtfMonitorStore(get_workdata_path(config.database_relative_path))
    registry = RegistrySnapshotProvider(config)
    updater = OfficialDailyDeltaUpdater(registry)
    calendar = OfficialTwseTradingCalendar(
        receipt_root=registry.delta_root / "calendar-gate-receipts"
    )
    data_provider = MonitorMarketDataProvider(
        registry,
        OfficialTwseQuoteProvider(),
        delta_updater=updater,
    )
    router = FailClosedTelegramRouter(config=config, store=store, bot=application.bot)
    service = EtfMonitorService(
        config=config,
        store=store,
        data_provider=data_provider,
        router=router,
        trading_day_predicate=calendar.verify,
    )
    runtime = EtfMonitorRuntime(
        config=config,
        store=store,
        router=router,
        service=service,
        locks={phase: asyncio.Lock() for phase in JOB_NAMES},
    )
    application.bot_data[RUNTIME_KEY] = runtime
    timezone = ZoneInfo(config.timezone)
    for phase, scheduled_time in (
        (MonitorPhase.PREVIEW, config.schedule.preview),
        (MonitorPhase.DECISION, config.schedule.decision),
        (MonitorPhase.RECONCILIATION, config.schedule.reconciliation),
    ):
        application.job_queue.run_daily(
            _scheduled_job,
            time=scheduled_time.replace(tzinfo=timezone),
            data={"phase": phase.value},
            name=JOB_NAMES[phase],
            job_kwargs={"misfire_grace_time": 30 * 60, "max_instances": 1},
        )
    return runtime


async def initialize_etf_monitor(application: Any) -> None:
    runtime = application.bot_data.get(RUNTIME_KEY)
    if not isinstance(runtime, EtfMonitorRuntime):
        return
    runtime.store.recover_after_restart()
    updater = getattr(
        getattr(runtime.service, "data_provider", None),
        "delta_updater",
        None,
    )
    if updater is not None:
        yesterday = (
            datetime.now(tz=ZoneInfo(runtime.config.timezone)).date()
            - timedelta(days=1)
        )
        try:
            await asyncio.to_thread(updater.ensure_through, yesterday)
        except Exception as exc:
            print(
                "[ETF 0050 final-v1.7] startup official delta update failed: "
                f"{type(exc).__name__}",
                flush=True,
            )
    try:
        await runtime.router.verify_route()
        await runtime.router.drain(limit=20)
    except Exception as exc:
        print(
            f"[ETF 0050 final-v1.7] startup route check failed: {type(exc).__name__}",
            flush=True,
        )
