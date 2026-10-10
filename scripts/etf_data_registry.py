from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from stock_ai_bot.data_sources.etf_snapshot_registry import (
    default_market_data_root,
    resolve_snapshot,
    validate_snapshot,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Resolve and validate a shared ETF data snapshot")
    parser.add_argument("symbol")
    parser.add_argument("--snapshot-id")
    parser.add_argument("--market-data-root", type=Path, default=default_market_data_root())
    parser.add_argument("--manifest-only", action="store_true")
    args = parser.parse_args(argv)
    snapshot = resolve_snapshot(
        args.symbol,
        args.snapshot_id,
        market_data_root=args.market_data_root,
    )
    result = validate_snapshot(snapshot, verify_artifacts=not args.manifest_only)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
