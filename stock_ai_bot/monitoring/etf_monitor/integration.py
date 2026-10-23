from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
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
SHUTDOWN_TIMEOUT_SECONDS = 5


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
    background_tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    shutting_down: bool = False

    async def dispatch(self, phase: MonitorPhase) -> None:
        if self.shutting_down or self.startup_status != "READY":
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
        or runtime.shutting_down
        or runtime.startup_status != "READY"
    ):
        return
    _create_runtime_task(
        context.application,
        runtime,
        runtime.dispatch(phase),
        name=JOB_NAMES[phase],
    )


def _create_runtime_task(
    application: Any,
    runtime: EtfMonitorRuntime,
    coroutine: Any,
    *,
    name: str,
) -> asyncio.Task[Any]:
    """Own ETF tasks across post_init/start/stop, without PTB pre-start tasks.

    PTB only joins Application.create_task tasks created while running. ETF
    tasks have an explicit, bounded shutdown hook instead, including startup
    tasks created in post_init before Application.start.
    """
    try:
        task = asyncio.create_task(coroutine, name=name)
    except BaseException:
        coroutine.close()
        raise
    runtime.background_tasks.add(task)

    def completed(finished: asyncio.Task[Any]) -> None:
        runtime.background_tasks.discard(finished)
        if finished.cancelled():
            return
        error = finished.exception()
        if error is not None:
            _disable_runtime(application, runtime, stage="background task", exc=error)

    task.add_done_callback(completed)
    return task


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
    current = asyncio.current_task()
    remaining = {
        task for task in runtime.background_tasks
        if not task.done() and task is not current
    }
    for task in remaining:
        task.cancel()
    # Keep ownership reachable by the shutdown hook until siblings are joined.
    if not remaining:
        _remove_runtime_safely(application, runtime)
    _remove_jobs_safely(application, runtime.jobs)
    _report_runtime_status(application, runtime)
    print(
        f"[ETF 0050 final-v1.7] {stage} disabled ETF runtime: "
        f"{type(exc).__name__}",
        flush=True,
    )


def _report_runtime_status(application: Any, runtime: EtfMonitorRuntime) -> None:
    """Expose ETF readiness separately from the original Bot's liveness."""
    try:
        from stock_ai_bot.monitoring.bot_runtime_health import update_schedule_health

        update_schedule_health(
            job_queue_available=bool(application.job_queue),
            etf_monitor={
                "status": runtime.startup_status,
                "updated_at": datetime.now(ZoneInfo(runtime.config.timezone)).isoformat(),
                "mode": runtime.config.mode,
                "schedule": {
                    "preview": runtime.config.schedule.preview.isoformat(),
                    "decision": runtime.config.schedule.decision.isoformat(),
                    "reconciliation": runtime.config.schedule.reconciliation.isoformat(),
                },
            },
        )
    except Exception as exc:
        print(f"[ETF 0050] health status write failed: {type(exc).__name__}", flush=True)


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
    if not runtime.shutting_down and application.bot_data.get(RUNTIME_KEY) is runtime:
        runtime.startup_status = "READY"
        _report_runtime_status(application, runtime)
        print("[0050 趨勢策略] 背景準備完成，監控排程已就緒", flush=True)
    else:
        runtime.startup_status = "DISABLED"


async def initialize_etf_monitor(application: Any) -> None:
    runtime = application.bot_data.get(RUNTIME_KEY)
    if not isinstance(runtime, EtfMonitorRuntime):
        return
    if (
        runtime.shutting_down
        or runtime.startup_status != "PENDING"
        or runtime.startup_task is not None
    ):
        return
    coroutine = _initialize_runtime(application, runtime)
    _report_runtime_status(application, runtime)
    try:
        runtime.startup_task = _create_runtime_task(
            application,
            runtime,
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


async def shutdown_etf_monitor(application: Any) -> None:
    """Stop only ETF work; safe from both post_stop and post_shutdown.

    SQLite transactions/immutable imports remain durable. A synchronous network
    call already running in a daemon thread cannot be killed by cancellation;
    the cancelled coroutine cannot advance to routing or READY afterwards.
    Abrupt process termination still relies on the existing restart recovery.
    """
    runtime = application.bot_data.get(RUNTIME_KEY)
    if not isinstance(runtime, EtfMonitorRuntime):
        return
    runtime.shutting_down = True
    runtime.startup_status = "STOPPING"
    _remove_jobs_safely(application, runtime.jobs)
    current = asyncio.current_task()
    tasks = {task for task in runtime.background_tasks if task is not current and not task.done()}
    if (
        runtime.startup_task is not None
        and runtime.startup_task is not current
        and not runtime.startup_task.done()
    ):
        tasks.add(runtime.startup_task)
    for task in tasks:
        task.cancel()
    pending: set[asyncio.Task[Any]] = set()
    if tasks:
        _, pending = await asyncio.wait(tasks, timeout=SHUTDOWN_TIMEOUT_SECONDS)
    runtime.startup_status = "STOPPED_WITH_PENDING_TASKS" if pending else "STOPPED"
    _report_runtime_status(application, runtime)
    if pending:
        # Do not hide incomplete shutdown or wait indefinitely on bad dependencies.
        print("[ETF 0050] shutdown timed out; ETF remains disabled", flush=True)
    else:
        _remove_runtime_safely(application, runtime)
        print("[0050 趨勢策略] 背景工作已停止", flush=True)
