from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from stock_ai_bot.workdata import get_workdata_path

from .config import EtfMonitorConfig, load_etf_monitor_config
from .errors import EtfMonitorConfigError
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
STARTUP_RECOVERY_TIMEOUT_SECONDS = 30
STARTUP_BACKFILL_TIMEOUT_SECONDS = 20 * 60
STARTUP_ROUTE_TIMEOUT_SECONDS = 30


@dataclass
class EtfMonitorRuntime:
    config: EtfMonitorConfig
    store: Any
    router: Any
    service: Any
    locks: dict[MonitorPhase, asyncio.Lock]
    jobs: tuple[Any, ...] = ()
    startup_status: str = "PENDING"
    startup_task: Any | None = None

    async def dispatch(self, phase: MonitorPhase) -> None:
        if self.startup_status != "READY":
            return
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
    if (
        not isinstance(runtime, EtfMonitorRuntime)
        or runtime.startup_status != "READY"
    ):
        return
    context.application.create_task(
        runtime.dispatch(phase),
        name=JOB_NAMES[phase],
    )


def _remove_jobs_safely(application: Any, jobs: tuple[Any, ...] | list[Any]) -> None:
    candidates = [job for job in jobs if job is not None]
    job_queue = getattr(application, "job_queue", None)
    finder = getattr(job_queue, "get_jobs_by_name", None)
    if callable(finder):
        for name in JOB_NAMES.values():
            try:
                candidates.extend(finder(name) or ())
            except Exception:
                pass
    seen: set[int] = set()
    for job in candidates:
        identity = id(job)
        if identity in seen:
            continue
        seen.add(identity)
        try:
            job.schedule_removal()
        except Exception:
            # Missing/failed cleanup is still safe because callbacks require
            # the exact READY runtime in bot_data before they do any work.
            pass


def _remove_runtime_safely(application: Any, runtime: EtfMonitorRuntime) -> None:
    try:
        if application.bot_data.get(RUNTIME_KEY) is runtime:
            application.bot_data.pop(RUNTIME_KEY, None)
    except Exception:
        pass


def _disable_runtime(
    application: Any,
    runtime: EtfMonitorRuntime,
    *,
    stage: str,
    exc: BaseException,
) -> None:
    runtime.startup_status = "DISABLED"
    _remove_runtime_safely(application, runtime)
    _remove_jobs_safely(application, runtime.jobs)
    print(
        f"[ETF 0050 final-v1.7] {stage} disabled ETF runtime: "
        f"{type(exc).__name__}",
        flush=True,
    )


def register_etf_monitor(application: Any, *, config_path: Any = None) -> EtfMonitorRuntime | None:
    """Attach final-v1.7 jobs to the existing Application and JobQueue.

    Disabled configuration is a strict no-op: no database, network request,
    handler, bot, polling process, or schedule is created.
    """

    try:
        config = load_etf_monitor_config(config_path)
    except EtfMonitorConfigError as exc:
        print(
            "[ETF 0050 final-v1.7] disabled by invalid configuration: "
            f"{type(exc).__name__}",
            flush=True,
        )
        return None
    if not config.enabled:
        return None
    registered_jobs: list[Any] = []
    runtime: EtfMonitorRuntime | None = None
    try:
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
        timezone = ZoneInfo(config.timezone)
        for phase, scheduled_time in (
            (MonitorPhase.PREVIEW, config.schedule.preview),
            (MonitorPhase.DECISION, config.schedule.decision),
            (MonitorPhase.RECONCILIATION, config.schedule.reconciliation),
        ):
            job = application.job_queue.run_daily(
                _scheduled_job,
                time=scheduled_time.replace(tzinfo=timezone),
                data={"phase": phase.value},
                name=JOB_NAMES[phase],
                job_kwargs={"misfire_grace_time": 30 * 60, "max_instances": 1},
            )
            if job is not None:
                registered_jobs.append(job)
        runtime.jobs = tuple(registered_jobs)
        application.bot_data[RUNTIME_KEY] = runtime
        return runtime
    except Exception as exc:
        if runtime is not None:
            runtime.startup_status = "DISABLED"
            _remove_runtime_safely(application, runtime)
        _remove_jobs_safely(application, registered_jobs)
        print(
            "[ETF 0050 final-v1.7] registration disabled ETF runtime: "
            f"{type(exc).__name__}",
            flush=True,
        )
        return None


async def _run_sync_bounded(call: Any, *args: Any, timeout: float) -> Any:
    """Run blocking startup work in a daemon thread with a bounded await."""

    loop = asyncio.get_running_loop()
    future = loop.create_future()

    def deliver(result: Any, error: BaseException | None) -> None:
        if future.done():
            return
        if error is not None:
            future.set_exception(error)
        else:
            future.set_result(result)

    def worker() -> None:
        try:
            result = call(*args)
        except BaseException as exc:
            try:
                loop.call_soon_threadsafe(deliver, None, exc)
            except RuntimeError:
                pass
        else:
            try:
                loop.call_soon_threadsafe(deliver, result, None)
            except RuntimeError:
                pass

    threading.Thread(
        target=worker,
        name="etf-monitor-startup-worker",
        daemon=True,
    ).start()
    return await asyncio.wait_for(future, timeout=timeout)


async def _initialize_runtime(application: Any, runtime: EtfMonitorRuntime) -> None:
    try:
        await _run_sync_bounded(
            runtime.store.recover_after_restart,
            timeout=STARTUP_RECOVERY_TIMEOUT_SECONDS,
        )
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
            await _run_sync_bounded(
                updater.ensure_through,
                yesterday,
                timeout=STARTUP_BACKFILL_TIMEOUT_SECONDS,
            )
        await asyncio.wait_for(
            runtime.router.verify_route(),
            timeout=STARTUP_ROUTE_TIMEOUT_SECONDS,
        )
        await asyncio.wait_for(
            runtime.router.drain(limit=20),
            timeout=STARTUP_ROUTE_TIMEOUT_SECONDS,
        )
    except asyncio.CancelledError:
        runtime.startup_status = "DISABLED"
        _remove_runtime_safely(application, runtime)
        _remove_jobs_safely(application, runtime.jobs)
        raise
    except Exception as exc:
        _disable_runtime(
            application,
            runtime,
            stage="startup",
            exc=exc,
        )
        return
    if application.bot_data.get(RUNTIME_KEY) is runtime:
        runtime.startup_status = "READY"
    else:
        runtime.startup_status = "DISABLED"


async def initialize_etf_monitor(application: Any) -> None:
    runtime = application.bot_data.get(RUNTIME_KEY)
    if not isinstance(runtime, EtfMonitorRuntime):
        return
    if runtime.startup_status != "PENDING" or runtime.startup_task is not None:
        return
    coroutine = _initialize_runtime(application, runtime)
    try:
        runtime.startup_task = application.create_task(
            coroutine,
            name="etf-0050-final-v1.7-startup",
        )
    except Exception as exc:
        coroutine.close()
        _disable_runtime(
            application,
            runtime,
            stage="startup scheduling",
            exc=exc,
        )
