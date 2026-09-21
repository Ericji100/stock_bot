"""Backward-compatible entry point for :mod:`trade_monitor.analysis_adapter`."""

from trade_monitor.analysis_adapter import *  # noqa: F401,F403
from trade_monitor.analysis_adapter import main


if __name__ == "__main__":
    raise SystemExit(main())
