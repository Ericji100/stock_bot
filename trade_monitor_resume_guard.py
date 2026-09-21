"""Backward-compatible entry point for :mod:`trade_monitor.resume_guard`."""

from trade_monitor.resume_guard import *  # noqa: F401,F403
from trade_monitor.resume_guard import main


if __name__ == "__main__":
    raise SystemExit(main())
