from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, TextIO

from .bridge import DeliveryFileLock


DEFAULT_OUTBOX_DIR = Path(".runtime/trade_monitor/outbox")
LATEST_FILE_NAME = "latest.json"
RELAY_STATE_FILE_NAME = "relay_state.json"
HISTORY_FILE_NAME = "history.jsonl"


class LocalOutboxError(RuntimeError):
    pass


def publish_event(outbox_dir: Path, event: Mapping[str, Any]) -> dict[str, Any]:
    payload = _validate_event(event)
    outbox_dir.mkdir(parents=True, exist_ok=True)
    lock_path = outbox_dir / ".outbox.lock"
    with DeliveryFileLock(lock_path):
        latest_path = outbox_dir / LATEST_FILE_NAME
        _write_json_atomic(latest_path, payload)
        with (outbox_dir / HISTORY_FILE_NAME).open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    return payload


def peek_event(outbox_dir: Path) -> dict[str, Any]:
    lock_path = outbox_dir / ".outbox.lock"
    with DeliveryFileLock(lock_path):
        latest = _read_json_object(outbox_dir / LATEST_FILE_NAME, missing_ok=True)
        relay_state = _read_json_object(outbox_dir / RELAY_STATE_FILE_NAME, missing_ok=True)
        if not latest:
            return {"ok": True, "status": "empty"}
        event_id = str(latest.get("event_id") or "")
        if relay_state.get("acknowledged_event_id") == event_id:
            return {"ok": True, "status": "already_acknowledged", "event_id": event_id}
        return {"ok": True, "status": "available", "event": latest}


def acknowledge_event(outbox_dir: Path, event_id: str) -> dict[str, Any]:
    event_id = str(event_id or "").strip()
    if not event_id:
        raise LocalOutboxError("event_id is required")
    lock_path = outbox_dir / ".outbox.lock"
    with DeliveryFileLock(lock_path):
        latest = _read_json_object(outbox_dir / LATEST_FILE_NAME, missing_ok=True)
        if not latest:
            return {"ok": True, "status": "empty"}
        latest_event_id = str(latest.get("event_id") or "")
        if latest_event_id != event_id:
            return {
                "ok": True,
                "status": "superseded",
                "event_id": event_id,
                "latest_event_id": latest_event_id,
            }
        state = {
            "version": 1,
            "acknowledged_event_id": event_id,
            "acknowledged_at": datetime.now(timezone.utc).isoformat(),
        }
        _write_json_atomic(outbox_dir / RELAY_STATE_FILE_NAME, state)
    return {"ok": True, "status": "acknowledged", "event_id": event_id}


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Relay local trade-monitor messages to Codex heartbeat")
    parser.add_argument("--outbox-dir", type=Path, default=DEFAULT_OUTBOX_DIR)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("peek")
    acknowledge = subparsers.add_parser("ack")
    acknowledge.add_argument("--event-id", required=True)
    return parser


def main(argv: list[str] | None = None, *, stdout: TextIO | None = None) -> int:
    output = stdout or sys.stdout
    parser = build_argument_parser()
    try:
        args = parser.parse_args(argv)
        if args.command == "peek":
            result = peek_event(args.outbox_dir)
        else:
            result = acknowledge_event(args.outbox_dir, args.event_id)
        exit_code = 0
    except LocalOutboxError as exc:
        result = {"ok": False, "status": "failed", "error": str(exc)}
        exit_code = 2
    except SystemExit:
        raise
    except Exception:
        result = {"ok": False, "status": "failed", "error": "Local outbox operation failed"}
        exit_code = 2
    # CLI stdout crosses Windows console/PTY boundaries before the heartbeat
    # parses it. ASCII-escaped JSON preserves the exact Unicode message after
    # json.loads without depending on the host code page.
    output.write(json.dumps(result, ensure_ascii=True, separators=(",", ":")) + "\n")
    output.flush()
    return exit_code


def _validate_event(event: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(event)
    required = (
        "event_id",
        "automation_id",
        "decision",
        "message",
        "latest_closed_bar_time",
        "published_at",
    )
    for key in required:
        if not str(payload.get(key) or "").strip():
            raise LocalOutboxError(f"{key} is required")
    if payload["decision"] not in {"NOTIFY", "DONT_NOTIFY"}:
        raise LocalOutboxError("decision is invalid")
    return payload


def _read_json_object(path: Path, *, missing_ok: bool) -> dict[str, Any]:
    if not path.exists():
        if missing_ok:
            return {}
        raise LocalOutboxError(f"Missing JSON file: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LocalOutboxError(f"Invalid JSON file: {path}") from exc
    if not isinstance(payload, dict):
        raise LocalOutboxError(f"JSON object expected: {path}")
    return payload


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(dict(payload), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
