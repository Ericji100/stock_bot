from __future__ import annotations

import io
import json
from pathlib import Path

from trade_monitor.local_outbox import acknowledge_event, main, peek_event, publish_event


def _event(event_id: str) -> dict[str, str]:
    return {
        "event_id": event_id,
        "automation_id": "1-k",
        "decision": "NOTIFY",
        "message": "canonical message",
        "latest_closed_bar_time": "2026-09-02T10:00:00+08:00",
        "published_at": "2026-09-02T10:01:05+08:00",
    }


def test_publish_peek_and_acknowledge(tmp_path: Path) -> None:
    outbox = tmp_path / "outbox"
    publish_event(outbox, _event("event-1"))

    available = peek_event(outbox)
    assert available["status"] == "available"
    assert available["event"]["message"] == "canonical message"

    acknowledged = acknowledge_event(outbox, "event-1")
    assert acknowledged == {"ok": True, "status": "acknowledged", "event_id": "event-1"}
    assert peek_event(outbox)["status"] == "already_acknowledged"


def test_new_event_becomes_available_after_previous_ack(tmp_path: Path) -> None:
    outbox = tmp_path / "outbox"
    publish_event(outbox, _event("event-1"))
    acknowledge_event(outbox, "event-1")

    publish_event(outbox, _event("event-2"))

    available = peek_event(outbox)
    assert available["status"] == "available"
    assert available["event"]["event_id"] == "event-2"


def test_ack_rejects_superseded_event(tmp_path: Path) -> None:
    outbox = tmp_path / "outbox"
    publish_event(outbox, _event("event-2"))

    result = acknowledge_event(outbox, "event-1")

    assert result["status"] == "superseded"
    assert result["latest_event_id"] == "event-2"
    assert peek_event(outbox)["status"] == "available"


def test_cli_stdout_uses_ascii_safe_json_without_changing_message(tmp_path: Path) -> None:
    outbox = tmp_path / "outbox"
    event = _event("event-zh")
    event["message"] = "監控已恢復；樞紐未定"
    publish_event(outbox, event)
    output = io.StringIO()

    exit_code = main(["--outbox-dir", str(outbox), "peek"], stdout=output)

    raw = output.getvalue()
    assert exit_code == 0
    assert "監控" not in raw
    assert "\\u76e3\\u63a7" in raw
    assert json.loads(raw)["event"]["message"] == event["message"]
