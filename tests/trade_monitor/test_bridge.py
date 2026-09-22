from __future__ import annotations

import asyncio
import io
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import trade_monitor.bridge as bridge
from stock_ai_bot.telegram.telegram_push_service import TelegramPushResult


TEST_TOKEN = "123456:abcdefghijklmnopqrstuvwxyzABCDE"
TEST_CHAT_ID = "-1001234567890"


class FakeBot:
    def __init__(self):
        self.calls: list[dict[str, object]] = []

    async def send_message(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(message_id=700 + len(self.calls))


def write_config(path: Path, **overrides: object) -> Path:
    payload = {
        "api_token": TEST_TOKEN,
        "chat_id": "legacy-chat",
        "trade_monitor_telegram_enabled": True,
        "trade_monitor_chat_id": TEST_CHAT_ID,
        "trade_monitor_send_quiet_status": False,
    }
    payload.update(overrides)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def run_bridge(tmp_path: Path, *, event_id: str = "event-1", decision: str = "NOTIFY", message: str = "訊息", **config):
    config_path = write_config(tmp_path / "config.json", **config)
    state_path = tmp_path / "delivery.json"
    bot = FakeBot()
    code, payload = asyncio.run(
        bridge.execute_bridge(
            event_id=event_id,
            decision=decision,
            message=message,
            config_path=config_path,
            state_path=state_path,
            bot_factory=lambda _token: bot,
        )
    )
    return code, payload, bot, state_path, config_path


def test_disabled_by_default_does_not_create_bot(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"api_token": TEST_TOKEN, "chat_id": TEST_CHAT_ID}), encoding="utf-8")
    called = False

    def bot_factory(_token):
        nonlocal called
        called = True
        return FakeBot()

    code, payload = asyncio.run(
        bridge.execute_bridge(
            event_id="disabled",
            decision="NOTIFY",
            message="訊息",
            config_path=config_path,
            state_path=tmp_path / "state.json",
            bot_factory=bot_factory,
        )
    )

    assert code == 0
    assert payload["status"] == "skipped"
    assert payload["reason"] == "telegram_disabled"
    assert called is False


def test_notify_sends_to_explicit_trade_monitor_chat(tmp_path):
    code, payload, bot, state_path, _ = run_bridge(tmp_path)

    assert code == 0
    assert payload["status"] == "sent"
    assert payload["message_ids"] == [701]
    assert bot.calls[0]["chat_id"] == TEST_CHAT_ID
    assert state_path.exists()


def test_chat_id_falls_back_to_existing_chat_id(tmp_path):
    code, payload, bot, _, _ = run_bridge(tmp_path, trade_monitor_chat_id=None)

    assert code == 0
    assert payload["status"] == "sent"
    assert bot.calls[0]["chat_id"] == "legacy-chat"


def test_dont_notify_skips_without_creating_delivery_state(tmp_path):
    code, payload, bot, state_path, _ = run_bridge(tmp_path, decision="DONT_NOTIFY")

    assert code == 0
    assert payload["status"] == "skipped"
    assert payload["reason"] == "decision_dont_notify"
    assert bot.calls == []
    assert state_path.exists() is False


def test_quiet_status_can_be_enabled_explicitly(tmp_path):
    code, payload, bot, _, _ = run_bridge(
        tmp_path,
        decision="DONT_NOTIFY",
        trade_monitor_send_quiet_status=True,
    )

    assert code == 0
    assert payload["status"] == "sent"
    assert len(bot.calls) == 1


def test_blank_message_is_skipped(tmp_path):
    code, payload, bot, state_path, _ = run_bridge(tmp_path, message=" \r\n\t")

    assert code == 0
    assert payload["reason"] == "empty_message"
    assert bot.calls == []
    assert state_path.exists() is False


def test_same_event_id_is_sent_only_once(tmp_path):
    config_path = write_config(tmp_path / "config.json")
    state_path = tmp_path / "state.json"
    bot = FakeBot()

    async def execute():
        return await bridge.execute_bridge(
            event_id="same-event",
            decision="NOTIFY",
            message="同一內容",
            config_path=config_path,
            state_path=state_path,
            bot_factory=lambda _token: bot,
        )

    first_code, first = asyncio.run(execute())
    second_code, second = asyncio.run(execute())

    assert first_code == second_code == 0
    assert first["status"] == "sent"
    assert second["status"] == "duplicate"
    assert len(bot.calls) == 1


def test_different_event_ids_are_sent_independently(tmp_path):
    config_path = write_config(tmp_path / "config.json")
    state_path = tmp_path / "state.json"
    bot = FakeBot()

    for event_id in ("event-a", "event-b"):
        code, payload = asyncio.run(
            bridge.execute_bridge(
                event_id=event_id,
                decision="NOTIFY",
                message="內容",
                config_path=config_path,
                state_path=state_path,
                bot_factory=lambda _token: bot,
            )
        )
        assert code == 0
        assert payload["status"] == "sent"

    assert len(bot.calls) == 2


def test_dry_run_formats_and_splits_without_bot_or_state(tmp_path):
    config_path = write_config(tmp_path / "config.json", trade_monitor_telegram_enabled=False)
    state_path = tmp_path / "state.json"

    code, payload = asyncio.run(
        bridge.execute_bridge(
            event_id="dry",
            decision="NOTIFY",
            message="**標題**\n" + ("長訊息🙂" * 1200),
            config_path=config_path,
            state_path=state_path,
            dry_run=True,
            bot_factory=lambda _token: (_ for _ in ()).throw(AssertionError("bot must not be created")),
        )
    )

    assert code == 0
    assert payload["status"] == "dry_run"
    assert payload["chunk_count"] > 1
    assert state_path.exists() is False


def test_failure_returns_nonzero_and_stdout_fields_are_sanitized(tmp_path):
    config_path = write_config(tmp_path / "config.json")
    failed = TelegramPushResult(
        ok=False,
        status="failed",
        chunk_count=1,
        error_code="send_chunk_1_failed",
        error="safe failure",
    )

    with patch.object(bridge, "send_telegram_message", new=AsyncMock(return_value=failed)):
        code, payload = asyncio.run(
            bridge.execute_bridge(
                event_id="failed",
                decision="NOTIFY",
                message="訊息",
                config_path=config_path,
                state_path=tmp_path / "state.json",
                bot_factory=lambda _token: FakeBot(),
            )
        )

    serialized = json.dumps(payload, ensure_ascii=False)
    assert code != 0
    assert payload["status"] == "failed"
    assert TEST_TOKEN not in serialized
    assert TEST_CHAT_ID not in serialized


def test_state_stores_no_message_token_chat_id_or_message_id(tmp_path):
    _, _, _, state_path, _ = run_bridge(tmp_path, message="完整而不應落盤的交易分析")
    state_text = state_path.read_text(encoding="utf-8")

    assert "完整而不應落盤的交易分析" not in state_text
    assert TEST_TOKEN not in state_text
    assert TEST_CHAT_ID not in state_text
    assert "701" not in state_text


def test_cli_reads_complete_multiline_utf8_stdin_and_emits_json(tmp_path):
    config_path = write_config(tmp_path / "config.json")
    state_path = tmp_path / "state.json"
    bot = FakeBot()
    stdout = io.StringIO()
    message = "**時間／最新已收盤 K**\n- 17:30🙂\n\n一般技術分析。"

    code = bridge.main(
        [
            "--event-id",
            "stdin-event",
            "--decision",
            "NOTIFY",
            "--config",
            str(config_path),
            "--state-file",
            str(state_path),
        ],
        stdin=io.StringIO(message),
        stdout=stdout,
        bot_factory=lambda _token: bot,
    )

    payload = json.loads(stdout.getvalue())
    assert code == 0
    assert payload["status"] == "sent"
    assert bot.calls[0]["text"] == "🔥 時間／最新已收盤 K\n- 17:30🙂\n\n一般技術分析。"
    assert TEST_TOKEN not in stdout.getvalue()
    assert TEST_CHAT_ID not in stdout.getvalue()


def test_event_id_is_stable_and_changes_with_bar_or_message():
    first = bridge.generate_event_id("1-k", "17:29", "NOTIFY", "訊息 A")

    assert first == bridge.generate_event_id("1-k", "17:29", "NOTIFY", "訊息 A")
    assert first != bridge.generate_event_id("1-k", "17:30", "NOTIFY", "訊息 A")
    assert first != bridge.generate_event_id("1-k", "17:29", "NOTIFY", "訊息 B")
    assert len(first) == 64


def test_bridge_import_does_not_import_or_start_main_module():
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import trade_monitor.bridge; assert 'main' not in sys.modules; print('ok')",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "ok"
