"""CMD progress logging utilities with unified timestamp format.

Format: [YYYY-MM-DD HH:MM:SS] [category] task | message
"""
from __future__ import annotations

import re
import threading
import time
from datetime import datetime
from typing import Callable, Literal

_TIMESTAMP_RE = re.compile(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]")


def now_timestamp() -> str:
    """Return current timestamp in YYYY-MM-DD HH:MM:SS format."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def has_leading_timestamp(message: str) -> bool:
    """Check if a message string starts with a [YYYY-MM-DD HH:MM:SS] timestamp.

    Args:
        message: The message string to check.

    Returns:
        True if message starts with [YYYY-MM-DD HH:MM:SS], False otherwise.
    """
    return _TIMESTAMP_RE.match(message) is not None


def format_progress_message(
    category: str,
    message: str,
    task: str | None = None,
    percent: float | None = None,
) -> str:
    """Format a progress message with timestamp.

    Args:
        category: Progress category (e.g., "選股進度", "AI投研")
        message: Progress message
        task: Optional task name
        percent: Optional progress percentage (0-100)

    Returns:
        Formatted message: [YYYY-MM-DD HH:MM:SS] [category] task | message
        If message already starts with a timestamp, returns message unchanged
        to avoid double timestamp.
    """
    if has_leading_timestamp(message):
        return message
    ts = now_timestamp()
    if task:
        task_part = f" {task}"
    else:
        task_part = ""
    if percent is not None:
        pct_part = f" {percent:.0f}%"
    else:
        pct_part = ""
    return f"[{ts}] [{category}]{task_part}{pct_part} | {message}"


def format_cmd_message(message: str, category: str | None = None) -> str:
    """Format a CMD message with optional timestamp and category.

    Args:
        message: The message string to format
        category: Optional category to prepend (e.g., "選股進度", "監控策略")

    Returns:
        If message already has a timestamp AND category is provided:
            [original_timestamp] [category] original_message_content
        If message already has a timestamp AND category is None:
            Returns unchanged
        If category is provided: [YYYY-MM-DD HH:MM:SS] [category] message
        If category is None: [YYYY-MM-DD HH:MM:SS] message
    """
    if has_leading_timestamp(message):
        if category:
            # Extract timestamp and rest of message, then prepend category
            ts_match = _TIMESTAMP_RE.match(message)
            if ts_match:
                ts = ts_match.group(0)  # e.g., "[2026-05-21 10:00:00]"
                rest = message[ts_match.end():].strip()  # message content after timestamp
                return f"{ts} [{category}] {rest}"
        return message
    ts = now_timestamp()
    if category:
        return f"[{ts}] [{category}] {message}"
    return f"[{ts}] {message}"


def print_cmd(message: str, category: str | None = None) -> None:
    """Print a CMD message with optional timestamp and category."""
    print(format_cmd_message(message, category), flush=True)


def print_progress(
    category: str,
    message: str,
    task: str | None = None,
    percent: float | None = None,
) -> None:
    """Print a progress message with timestamp to stdout."""
    msg = format_progress_message(category, message, task, percent)
    print(msg, flush=True)


def format_duration(seconds: float) -> str:
    """Format a duration in seconds to a human-readable string.

    Args:
        seconds: Duration in seconds

    Returns:
        Human-readable duration string (e.g., "2m 30s", "1h 15m")
    """
    if seconds < 60:
        return f"{seconds:.1f}s"
    elif seconds < 3600:
        minutes = int(seconds // 60)
        secs = int(seconds % 60)
        return f"{minutes}m {secs}s"
    else:
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        return f"{hours}h {minutes}m"


# Convenience functions for common categories
def print_scan_progress(message: str, task: str | None = None, percent: float | None = None) -> None:
    """Print progress for stock scanner."""
    print_progress("選股進度", message, task, percent)


def print_research_progress(message: str, task: str | None = None) -> None:
    """Print progress for AI research center."""
    print_progress("AI投研", message, task)


def print_backfill_progress(message: str, task: str | None = None) -> None:
    """Print progress for backfill service."""
    print_progress("回填進度", message, task)


def print_chip_progress(label: str, progress: float, message: str) -> None:
    """Print progress for chip strategies with percentage."""
    print_progress(label, message, percent=progress)


class ProgressHeartbeat:
    """Emit periodic progress heartbeats for long-running tasks."""

    def __init__(
        self,
        label: str,
        *,
        sink: Callable[[str], None] | None = None,
        interval_seconds: float = 30.0,
    ) -> None:
        self.label = label
        self.sink = sink or (lambda message: print_cmd(message, label))
        self.interval_seconds = max(1.0, float(interval_seconds))
        self._started_at = time.monotonic()
        self._last_stage = "準備中"
        self._last_detail = "尚未收到進度"
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def update(self, message: str, *, stage: str | None = None) -> None:
        clean = str(message or "").strip()
        if not clean:
            return
        with self._lock:
            self._last_detail = clean
            self._last_stage = stage or _infer_progress_stage(clean)

    def start(self) -> "ProgressHeartbeat":
        if self._thread and self._thread.is_alive():
            return self
        self._thread = threading.Thread(target=self._run, name=f"{self.label} heartbeat", daemon=True)
        self._thread.start()
        return self

    def stop(self, final_message: str | None = None) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=1.0)
        if final_message:
            self.sink(final_message)

    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self._started_at

    def _run(self) -> None:
        while not self._stop_event.wait(self.interval_seconds):
            with self._lock:
                stage = self._last_stage
                detail = self._last_detail
            self.sink(
                f"{self.label} 仍在執行，已耗時 {format_duration(self.elapsed_seconds)}，"
                f"目前階段：{stage}，最近進度：{detail}"
            )


def _infer_progress_stage(message: str) -> str:
    text = str(message or "")
    for marker in ("：", ":", "|"):
        if marker in text:
            tail = text.rsplit(marker, 1)[-1].strip()
            if tail:
                return tail[:60]
    return text[:60] or "執行中"
