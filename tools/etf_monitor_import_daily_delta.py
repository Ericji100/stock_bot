"""Validate and immutably install one official 0050 calendar-day delta."""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path
from typing import Sequence

from stock_ai_bot.data_sources.etf_snapshot_registry import resolve_snapshot, validate_snapshot
from stock_ai_bot.monitoring.etf_monitor.config import load_etf_monitor_config
from stock_ai_bot.monitoring.etf_monitor.data import (
    RegistrySnapshotProvider,
    install_daily_delta_file,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--config", type=Path)
    args = parser.parse_args(argv)

    config = load_etf_monitor_config(args.config)
    provider = RegistrySnapshotProvider(config)
    snapshot = resolve_snapshot(
        config.symbol,
        config.snapshot_id,
        market_data_root=provider.market_data_root,
    )
    validate_snapshot(snapshot, verify_artifacts=True)
    raw = json.loads(args.input.read_text(encoding="utf-8-sig"))
    trade_date = str(raw.get("trade_date") or "")
    parsed_date = date.fromisoformat(trade_date)
    payload = install_daily_delta_file(
        args.input,
        destination_root=provider.delta_root,
        expected_date=parsed_date,
        snapshot_id=snapshot.snapshot_id,
        manifest_sha256=snapshot.package_manifest_sha256,
    )
    destination = provider.delta_root / f"{trade_date}.json"
    print(json.dumps({
        "status": "INSTALLED_OR_ALREADY_IDENTICAL",
        "trade_date": trade_date,
        "destination": str(destination),
        "content_sha256": payload["content_sha256"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
