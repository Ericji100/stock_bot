"""Read-only smoke test for the exact data roots used by the ETF monitor."""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path
from typing import Sequence

from stock_ai_bot.monitoring.etf_monitor.config import load_etf_monitor_config
from stock_ai_bot.monitoring.etf_monitor.data import RegistrySnapshotProvider
from stock_ai_bot.monitoring.etf_monitor.errors import EtfMonitorDataError


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--as-of", required=True, type=date.fromisoformat)
    parser.add_argument("--config", type=Path)
    args = parser.parse_args(argv)
    config = load_etf_monitor_config(args.config)
    provider = RegistrySnapshotProvider(config)
    try:
        snapshot = provider.load_complete(args.as_of)
    except EtfMonitorDataError as exc:
        print(json.dumps({
            "status": "DATA_UNAVAILABLE",
            "as_of": args.as_of.isoformat(),
            "reason": str(exc),
            "frozen_market_data_root": str(provider.market_data_root),
            "delta_root": str(provider.delta_root),
        }, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps({
        "status": "PASS",
        "as_of": snapshot.as_of.isoformat(),
        "latest_date": snapshot.latest_date.isoformat(),
        "rows": len(snapshot.raw_rows),
        "corporate_actions": len(snapshot.corporate_actions),
        "snapshot_id": snapshot.snapshot_id,
        "snapshot_sha256": snapshot.snapshot_sha256,
        "frozen_market_data_root": str(provider.market_data_root),
        "delta_root": str(provider.delta_root),
        "delta_count": snapshot.metadata.get("delta_count"),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
