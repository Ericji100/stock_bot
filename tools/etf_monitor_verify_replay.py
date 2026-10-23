"""Run the formal full-history LIVE/REPLAY parity gate without writing research roots."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from stock_ai_bot.monitoring.etf_monitor.config import load_etf_monitor_config
from stock_ai_bot.monitoring.etf_monitor.data import resolve_frozen_market_data_root
from stock_ai_bot.monitoring.etf_monitor.replay import run_full_live_replay_parity


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path)
    parser.add_argument("--market-data-root", type=Path)
    parser.add_argument("--artifact-root", type=Path)
    args = parser.parse_args(argv)
    config = load_etf_monitor_config(args.config)
    result = run_full_live_replay_parity(
        market_data_root=args.market_data_root or resolve_frozen_market_data_root(config),
        artifact_root=args.artifact_root,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
