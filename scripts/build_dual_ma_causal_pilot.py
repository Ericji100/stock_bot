"""Build the immutable 01-to-02 dual-MA causal replay pilot."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from stock_ai_bot.selection import dual_ma_causal_pilot_service as service


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selection-root", type=Path, default=service.DEFAULT_SELECTION_ROOT)
    parser.add_argument("--backfill-root", type=Path, default=service.DEFAULT_BACKFILL_ROOT)
    parser.add_argument("--output-root", type=Path, default=service.DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--through", type=_parse_date)
    parser.add_argument("--core-count", type=int, default=service.CORE_EVENT_COUNT)
    parser.add_argument("--max-count", type=int, default=service.MAX_EVENT_COUNT)
    args = parser.parse_args()

    payload, path = service.build_and_freeze_pilot(
        selection_root=args.selection_root,
        backfill_root=args.backfill_root,
        output_root=args.output_root,
        through=args.through,
        core_count=args.core_count,
        max_count=args.max_count,
    )
    print(
        json.dumps(
            {
                "pilot_id": payload["pilot_id"],
                "content_sha256": payload["content_sha256"],
                "event_count": len(payload["events"]),
                "ready_count": sum(row["can_handoff_to_02"] for row in payload["events"]),
                "manifest_path": str(path.resolve()),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

