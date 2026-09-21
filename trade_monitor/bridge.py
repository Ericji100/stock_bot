from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import time
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TextIO

from telegram import Bot
from telegram.request import HTTPXRequest

from stock_ai_bot.telegram.telegram_push_service import (
    TELEGRAM_SAFE_CHUNK_LIMIT,
    TelegramPushResult,
    prepare_telegram_push_chunks,
    sanitize_telegram_error,
    send_telegram_message,
)


DEFAULT_CONFIG_PATH = Path("config.json")
DEFAULT_STATE_PATH = Path(".runtime/trade_monitor/delivery_state.json")
STATE_VERSION = 1
MAX_STATE_EVENTS = 1000
TRADE_MONITOR_TELEGRAM_PREFIX = "🔥 "


class BridgeError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class DeliveryFileLock(AbstractContextManager["DeliveryFileLock"]):
    def __init__(self, path: Path, timeout_seconds: float = 10.0):
        self.path = path
        self.timeout_seconds = timeout_seconds
        self._handle: Any = None

    def __enter__(self) -> "DeliveryFileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a+b")
        self._handle.seek(0, os.SEEK_END)
        if self._handle.tell() == 0:
            self._handle.write(b"0")
            self._handle.flush()

        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                self._lock_nonblocking()
                return self
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    self._handle.close()
                    self._handle = None
                    raise BridgeError("delivery_lock_timeout", "Delivery state is busy")
                time.sleep(0.05)

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self._handle is None:
            return
        try:
            self._unlock()
        finally:
            self._handle.close()
            self._handle = None

    def _lock_nonblocking(self) -> None:
        assert self._handle is not None
        self._handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(self) -> None:
        assert self._handle is not None
        self._handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)


def generate_event_id(
    automation_id: str,
    latest_closed_bar_time: str,
    decision: str,
    canonical_message: str,
) -> str:
    payload = "\n".join((automation_id, latest_closed_bar_time, decision, canonical_message))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Push a canonical trade-monitor message to Telegram")
    parser.add_argument("--event-id", required=True)
    parser.add_argument("--decision", required=True, choices=("NOTIFY", "DONT_NOTIFY"))
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE_PATH)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def load_bridge_config(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BridgeError("config_missing", "Telegram bridge configuration is missing") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise BridgeError("config_invalid", "Telegram bridge configuration is invalid") from exc
    if not isinstance(payload, dict):
        raise BridgeError("config_invalid", "Telegram bridge configuration must be an object")
    return payload


async def execute_bridge(
    *,
    event_id: str,
    decision: str,
    message: str,
    config_path: Path,
    state_path: Path,
    dry_run: bool = False,
    bot_factory: Callable[[str], Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    event_id = str(event_id or "").strip()
    if not event_id:
        return _error_payload("event_id_missing", "event_id is required", event_id)

    config = load_bridge_config(config_path)
    quiet_status = config.get("trade_monitor_send_quiet_status", False) is True
    if decision == "DONT_NOTIFY" and not quiet_status:
        return 0, _result_payload(True, "skipped", event_id, reason="decision_dont_notify")
    if not str(message or "").strip():
        return 0, _result_payload(True, "skipped", event_id, reason="empty_message")

    chunks = prepare_telegram_push_chunks(
        str(message),
        limit=TELEGRAM_SAFE_CHUNK_LIMIT,
        chunk_prefix=TRADE_MONITOR_TELEGRAM_PREFIX,
    )
    if any(len(chunk.text) > TELEGRAM_SAFE_CHUNK_LIMIT for chunk in chunks):
        return _error_payload("chunk_too_long", "Formatted Telegram chunk exceeds limit", event_id)
    if dry_run:
        return 0, _result_payload(True, "dry_run", event_id, chunk_count=len(chunks))

    if config.get("trade_monitor_telegram_enabled", False) is not True:
        return 0, _result_payload(True, "skipped", event_id, reason="telegram_disabled")

    token = str(config.get("api_token") or "").strip()
    chat_id = config.get("trade_monitor_chat_id")
    if chat_id is None or not str(chat_id).strip():
        chat_id = config.get("chat_id")
    if not token:
        return _error_payload("api_token_missing", "Telegram api_token is missing", event_id)
    if chat_id is None or not str(chat_id).strip():
        return _error_payload("chat_id_missing", "Telegram trade monitor chat_id is missing", event_id)
    if token in message or str(chat_id) in message:
        return _error_payload("sensitive_content", "Message contains configured Telegram credentials", event_id)

    lock_path = state_path.with_suffix(state_path.suffix + ".lock")
    with DeliveryFileLock(lock_path):
        state = _read_state(state_path)
        previous = state["events"].get(event_id)
        if isinstance(previous, dict) and previous.get("status") == "sent":
            return 0, _result_payload(
                True,
                "duplicate",
                event_id,
                chunk_count=int(previous.get("chunk_count") or 0),
                reason="already_sent",
            )

        result = await _send_with_bot(
            token=token,
            chat_id=chat_id,
            message=message,
            bot_factory=bot_factory,
        )
        _record_delivery(state, event_id, result)
        _write_state_atomic(state_path, state)

    payload = _result_payload(
        result.ok,
        result.status,
        event_id,
        chunk_count=result.chunk_count,
        sent_chunk_count=result.sent_chunk_count,
        message_ids=result.message_ids,
        error_code=result.error_code,
        error=result.error,
    )
    return (0 if result.ok else 1), payload


async def _send_with_bot(
    *,
    token: str,
    chat_id: int | str,
    message: str,
    bot_factory: Callable[[str], Any] | None,
) -> TelegramPushResult:
    factory = bot_factory or _build_telegram_bot
    bot = factory(token)
    request = getattr(bot, "request", None)
    request_initialized = False
    try:
        if request is not None and hasattr(request, "initialize"):
            await request.initialize()
            request_initialized = True
        return await send_telegram_message(
            bot,
            chat_id,
            message,
            chunk_prefix=TRADE_MONITOR_TELEGRAM_PREFIX,
        )
    except Exception as exc:
        return TelegramPushResult(
            ok=False,
            status="failed",
            chunk_count=0,
            error_code="telegram_client_failed",
            error=sanitize_telegram_error(exc, token, chat_id),
        )
    finally:
        if request_initialized and hasattr(request, "shutdown"):
            try:
                await request.shutdown()
            except Exception:
                pass


def _build_telegram_bot(token: str) -> Bot:
    request = HTTPXRequest(connect_timeout=5, read_timeout=10, write_timeout=10, pool_timeout=5)
    return Bot(token=token, request=request, get_updates_request=request)


def _read_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": STATE_VERSION, "events": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BridgeError("delivery_state_invalid", "Delivery state is invalid") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("events"), dict):
        raise BridgeError("delivery_state_invalid", "Delivery state is invalid")
    return payload


def _record_delivery(state: dict[str, Any], event_id: str, result: TelegramPushResult) -> None:
    state["version"] = STATE_VERSION
    events = state.setdefault("events", {})
    events[event_id] = {
        "event_id": event_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": result.status,
        "chunk_count": result.chunk_count,
        "error_code": result.error_code,
    }
    if len(events) > MAX_STATE_EVENTS:
        ordered = sorted(events.items(), key=lambda item: str(item[1].get("timestamp") or ""), reverse=True)
        state["events"] = dict(ordered[:MAX_STATE_EVENTS])


def _write_state_atomic(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    except OSError as exc:
        raise BridgeError("delivery_state_write_failed", "Delivery state could not be saved") from exc
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)


def _result_payload(
    ok: bool,
    status: str,
    event_id: str,
    *,
    chunk_count: int = 0,
    sent_chunk_count: int = 0,
    message_ids: list[int] | None = None,
    reason: str | None = None,
    error_code: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": ok,
        "status": status,
        "event_id": event_id,
        "chunk_count": chunk_count,
        "sent_chunk_count": sent_chunk_count,
        "message_ids": list(message_ids or []),
    }
    if reason:
        payload["reason"] = reason
    if error_code:
        payload["error_code"] = error_code
    if error:
        payload["error"] = error
    return payload


def _error_payload(code: str, message: str, event_id: str) -> tuple[int, dict[str, Any]]:
    return 2, _result_payload(False, "failed", event_id, error_code=code, error=message)


def _read_utf8_stdin(stream: TextIO | None = None) -> str:
    if stream is not None:
        return stream.read()
    return sys.stdin.buffer.read().decode("utf-8")


def main(
    argv: list[str] | None = None,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    bot_factory: Callable[[str], Any] | None = None,
) -> int:
    output = stdout or sys.stdout
    parser = build_argument_parser()
    try:
        args = parser.parse_args(argv)
        message = _read_utf8_stdin(stdin)
        exit_code, payload = asyncio.run(
            execute_bridge(
                event_id=args.event_id,
                decision=args.decision,
                message=message,
                config_path=args.config,
                state_path=args.state_file,
                dry_run=args.dry_run,
                bot_factory=bot_factory,
            )
        )
    except UnicodeDecodeError:
        exit_code, payload = _error_payload("stdin_not_utf8", "stdin must be valid UTF-8", "")
    except BridgeError as exc:
        exit_code, payload = _error_payload(exc.code, str(exc), getattr(locals().get("args"), "event_id", ""))
    except SystemExit:
        raise
    except Exception:
        exit_code, payload = _error_payload("bridge_internal_error", "Telegram bridge failed", "")

    output.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    output.flush()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
