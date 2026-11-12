"""Cooperative, cross-thread deadlines; cancellation never erases incurred costs."""
from contextlib import contextmanager
from contextvars import ContextVar
import threading
import time

from .deepseek_service import DeepSeekUnavailable

CONTROL = ContextVar('ai_execution_control', default=None)
SOURCE_CHECKPOINT = ContextVar('ai_source_checkpoint', default=None)


class WorkStopped(DeepSeekUnavailable):
    def __init__(self, reason='work_cancelled'):
        super().__init__('本次工作已停止；保留已取得資料與費用紀錄，不再新增付費請求。', reason)


class ExecutionControl:
    def __init__(self, seconds=None, *, parent=None, reason='work_deadline', clock=None):
        self.parent = parent
        self.clock = clock or time.monotonic
        self.deadline = None if seconds is None else self.clock() + max(0, seconds)
        self.event = threading.Event()
        self.reason = reason

    def stop(self):
        self.event.set()

    def check(self):
        if self.parent:
            self.parent.check()
        if self.event.is_set() or self.deadline is not None and self.clock() >= self.deadline:
            raise WorkStopped(self.reason)

    def remaining(self):
        self.check()
        own = float('inf') if self.deadline is None else max(0, self.deadline - self.clock())
        return min(own, self.parent.remaining() if self.parent else float('inf'))


@contextmanager
def execution_scope(seconds=None, *, reason='work_deadline'):
    control = ExecutionControl(seconds, parent=CONTROL.get(), reason=reason)
    token = CONTROL.set(control)
    try:
        yield control
    finally:
        CONTROL.reset(token)


def check_work():
    if control := CONTROL.get():
        control.check()


def bounded_timeout(seconds):
    check_work()
    control = CONTROL.get()
    return min(float(seconds), control.remaining()) if control else float(seconds)


def require_work_time(seconds, *, reason):
    """Refuse optional paid work before reserving when its phase is nearly over."""
    check_work()
    if (control := CONTROL.get()) and control.remaining() < seconds:
        raise WorkStopped(reason)


def is_graceful_search_stop(reason):
    return reason in {'search_phase_deadline', 'insufficient_followup_time', 'insufficient_search_time'}


def require_search_dispatch_time(timeout_seconds):
    """Leave scheduled news time to finish a search and checkpoint before classifying."""
    from .deepseek_service import TASK
    check_work()
    if TASK.get()[0] != 'news' or not TASK.get()[1]:
        return
    required = min(90., float(timeout_seconds)) + 5.
    if (control := CONTROL.get()) and (remaining := control.remaining()) < required:
        error = WorkStopped('insufficient_search_time')
        error.diagnostics.update(remaining_seconds=remaining, required_seconds=required, http_sent=False)
        raise error


@contextmanager
def source_checkpoint(callback):
    token = SOURCE_CHECKPOINT.set(callback)
    try:
        yield
    finally:
        SOURCE_CHECKPOINT.reset(token)


def checkpoint_sources(sources):
    if callback := SOURCE_CHECKPOINT.get():
        try:
            callback(sources)
        except Exception as exc:
            if control := CONTROL.get():
                control.stop()
            raise WorkStopped('source_checkpoint_failed') from exc
