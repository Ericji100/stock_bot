from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research_center.orchestrator import ResearchCenter
from research_center.topic_repository import list_change_packs


def _stamp() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _pack_ids() -> set[str]:
    return {pack.change_id for pack in list_change_packs()}


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the real /topic_maintain flow once.")
    parser.add_argument("--command", default="/topic_maintain --model minimax")
    parser.add_argument("--user-id", default="codex_live_audit")
    parser.add_argument("--result-json", default="")
    args, extra = parser.parse_known_args()
    if extra:
        args.command = " ".join([args.command, *extra])

    result_path = Path(args.result_json) if args.result_json else None
    before_ids = _pack_ids()
    progress_events: list[dict[str, Any]] = []

    def progress(message: str) -> None:
        event = {"ts": _stamp(), "message": message}
        progress_events.append(event)
        print(f"[{event['ts']}] {message}", flush=True)

    center = ResearchCenter()
    request = center.parse(args.command, user_id=args.user_id)
    progress(f"parsed command={request.command} ai_model={request.ai_model}")
    result = center.run(request, progress=progress)

    after_packs = list_change_packs()
    new_packs = [pack for pack in after_packs if pack.change_id not in before_ids]
    payload: dict[str, Any] = {
        "command": args.command,
        "status": result.status,
        "summary": result.summary,
        "new_change_ids": [pack.change_id for pack in new_packs],
        "new_change_packs": [pack.to_dict() for pack in new_packs],
        "progress_events": progress_events,
    }
    if result_path:
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        progress(f"result_json={result_path}")
    print(json.dumps(payload, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
