"""Validate and immutably install one official 0050 calendar-day delta."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from stock_ai_bot.data_sources.etf_snapshot_registry import resolve_snapshot, validate_snapshot
from stock_ai_bot.monitoring.etf_monitor.config import load_etf_monitor_config
from stock_ai_bot.monitoring.etf_monitor.data import (
    RegistrySnapshotProvider,
    load_daily_delta_file,
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
    from datetime import date

    parsed_date = date.fromisoformat(trade_date)
    payload = load_daily_delta_file(
        args.input,
        expected_date=parsed_date,
        snapshot_id=snapshot.snapshot_id,
        manifest_sha256=snapshot.package_manifest_sha256,
    )
    destination = provider.delta_root / f"{trade_date}.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    for evidence in (
        payload["calendar_evidence"],
        payload["bar_evidence"],
        *payload["corporate_action_evidence"],
    ):
        relative = Path(str(evidence["receipt_relative_path"]))
        source_receipt = args.input.parent / relative
        destination_receipt = provider.delta_root / relative
        raw_receipt = source_receipt.read_bytes()
        destination_receipt.parent.mkdir(parents=True, exist_ok=True)
        try:
            with destination_receipt.open("xb") as handle:
                handle.write(raw_receipt)
        except FileExistsError:
            if destination_receipt.read_bytes() != raw_receipt:
                raise SystemExit(
                    f"refusing to replace content-addressed receipt: {destination_receipt}"
                )
    canonical = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    try:
        with destination.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(canonical)
    except FileExistsError:
        existing = json.loads(destination.read_text(encoding="utf-8-sig"))
        if existing != payload:
            raise SystemExit(f"refusing to replace immutable delta: {destination}")
    print(json.dumps({
        "status": "INSTALLED_OR_ALREADY_IDENTICAL",
        "trade_date": trade_date,
        "destination": str(destination),
        "content_sha256": payload["content_sha256"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
