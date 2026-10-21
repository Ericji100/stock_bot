"""Manually resolve a quarantined Telegram delivery after operator verification."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from stock_ai_bot.monitoring.etf_monitor.store import EtfMonitorStore


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--event-id", required=True)
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument("--mark-delivered", action="store_true")
    choice.add_argument("--confirm-not-delivered", action="store_true")
    parser.add_argument("--telegram-message-id")
    args = parser.parse_args(argv)
    store = EtfMonitorStore(args.database)
    store.resolve_uncertain_delivery(
        args.event_id,
        delivered=args.mark_delivered,
        telegram_message_id=args.telegram_message_id,
    )
    print(
        "DELIVERED_MANUAL"
        if args.mark_delivered
        else "RETRY_ALLOWED_AFTER_MANUAL_CONFIRMATION"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
