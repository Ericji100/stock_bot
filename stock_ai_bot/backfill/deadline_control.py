"""Backfill-only cooperation, without importing AI services during chip imports."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from inspect import signature
import time


_BACKFILL_CONTROL = ContextVar("backfill_control", default=None)


def current_backfill_control():
    return _BACKFILL_CONTROL.get()


@contextmanager
def backfill_scope(*, stop_event=None, seconds=None, reason="backfill_stopped"):
    from research_center.ai_execution_control import CONTROL, ExecutionControl

    control = ExecutionControl(seconds, parent=CONTROL.get(), reason=reason)
    if stop_event is not None:
        control.event = stop_event
    token = CONTROL.set(control)
    backfill_token = _BACKFILL_CONTROL.set(control)
    try:
        yield control
    finally:
        _BACKFILL_CONTROL.reset(backfill_token)
        CONTROL.reset(token)


def check_backfill():
    if control := _BACKFILL_CONTROL.get():
        control.check()


def backfill_operation(function):
    """Bind legacy stop_event arguments as well as an inherited deadline."""
    parameters = signature(function)

    @wraps(function)
    def run(*args, **kwargs):
        event = parameters.bind(*args, **kwargs).arguments.get("stop_event")
        with backfill_scope(stop_event=event):
            check_backfill()
            try:
                result = function(*args, **kwargs)
            except Exception:
                check_backfill()
                raise
            check_backfill()
            return result

    return run


def backfill_checkpoint(function):
    """Check a shared chip boundary only when called from backfill."""
    @wraps(function)
    def run(*args, **kwargs):
        check_backfill()
        result = function(*args, **kwargs)
        check_backfill()
        return result

    return run


def request_timeout(seconds):
    """Leave ordinary chip requests unchanged; bound backfill request phases."""
    if control := _BACKFILL_CONTROL.get():
        return min(float(seconds), control.remaining())
    return float(seconds)


def request_timeout_kwargs(seconds, *, client=None):
    if not _BACKFILL_CONTROL.get():
        return {}
    from httpx import Timeout

    remaining = request_timeout(seconds)
    configured = getattr(client, "timeout", None)
    if isinstance(configured, Timeout):
        return {"timeout": Timeout(**{
            phase: remaining if limit is None else min(limit, remaining)
            for phase, limit in configured.as_dict().items()
        })}
    return {"timeout": remaining}


def backfill_sleep(seconds):
    control = _BACKFILL_CONTROL.get()
    if control is None:
        time.sleep(seconds)
        return
    until = time.monotonic() + max(0, seconds)
    while True:
        remaining = control.remaining()
        delay = min(until - time.monotonic(), remaining, 0.05)
        if delay <= 0:
            check_backfill()
            return
        control.event.wait(delay)
