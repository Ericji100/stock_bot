"""Backward-compatible entry point for :mod:`trade_monitor.bridge`."""

from trade_monitor.bridge import *  # noqa: F401,F403
from trade_monitor.bridge import main


if __name__ == "__main__":
    raise SystemExit(main())
