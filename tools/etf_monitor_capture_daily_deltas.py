"""Capture exact TWSE receipts and derive a review-only 0050 delta bundle."""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path
from typing import Sequence

from stock_ai_bot.data_sources.etf_snapshot_registry import resolve_snapshot, validate_snapshot
from stock_ai_bot.monitoring.etf_monitor.config import load_etf_monitor_config
from stock_ai_bot.monitoring.etf_monitor.data import resolve_frozen_market_data_root
from stock_ai_bot.monitoring.etf_monitor.delta_pipeline import build_daily_delta_bundle


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--from", dest="start", required=True, type=date.fromisoformat)
    parser.add_argument("--through", required=True, type=date.fromisoformat)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--market-data-root", type=Path)
    args = parser.parse_args(argv)
    config = load_etf_monitor_config(args.config)
    market_root = args.market_data_root or resolve_frozen_market_data_root(config)
    snapshot = resolve_snapshot(
        config.symbol, config.snapshot_id, market_data_root=market_root
    )
    validate_snapshot(snapshot, verify_artifacts=True)
    result = build_daily_delta_bundle(
        start=args.start,
        through=args.through,
        output=args.output,
        frozen_snapshot_id=snapshot.snapshot_id,
        frozen_manifest_sha256=snapshot.package_manifest_sha256,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("REVIEW_REQUIRED: bundle is not installed; run the importer per day only after review.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
