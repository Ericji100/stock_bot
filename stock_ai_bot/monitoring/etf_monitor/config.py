from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from typing import Any, Mapping

from .errors import EtfMonitorConfigError


ETF_MONITOR_CONFIG_SCHEMA = "stock-ai-bot-etf-monitor-config-v1"
ETF_MONITOR_CHAT_ID = "-5527055845"
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "etf_monitor.json"
_CHAT_ID_RE = re.compile(r"^-[1-9][0-9]{5,}$")
_TIME_RE = re.compile(r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")


@dataclass(frozen=True)
class EtfSchedule:
    preview: time
    decision: time
    reconciliation: time


@dataclass(frozen=True)
class EtfMonitorConfig:
    schema_version: str
    enabled: bool
    mode: str
    chat_id: str
    symbol: str
    strategy: str
    timezone: str
    schedule: EtfSchedule
    daily_shadow_status: bool
    quiet_status_in_formal_mode: bool
    require_get_chat: bool
    database_relative_path: str
    snapshot_id: str | None
    frozen_market_data_relative_path: str
    delta_relative_path: str
    raw_daily_relative_path: str
    corporate_actions_relative_path: str
    dividends_relative_path: str
    job_timeouts_seconds: Mapping[str, int]
    source_path: Path


def _parse_time(value: Any, field: str) -> time:
    text = str(value or "")
    if not _TIME_RE.fullmatch(text):
        raise EtfMonitorConfigError(f"{field} must be HH:MM")
    hour, minute = (int(part) for part in text.split(":"))
    return time(hour=hour, minute=minute)


def _safe_relative_path(value: Any, field: str) -> str:
    text = str(value or "").strip().replace("\\", "/")
    path = Path(text)
    if not text or path.is_absolute() or ".." in path.parts:
        raise EtfMonitorConfigError(f"{field} must be a contained relative path")
    return text


def validate_etf_monitor_config(payload: Mapping[str, Any], *, source_path: Path) -> EtfMonitorConfig:
    allowed = {
        "schema_version",
        "enabled",
        "mode",
        "chat_id",
        "symbol",
        "strategy",
        "timezone",
        "schedule",
        "daily_shadow_status",
        "quiet_status_in_formal_mode",
        "require_get_chat",
        "database_relative_path",
        "data",
        "job_timeouts_seconds",
    }
    unknown = set(payload) - allowed
    if unknown:
        raise EtfMonitorConfigError(f"unknown ETF monitor config fields: {sorted(unknown)}")
    if payload.get("schema_version") != ETF_MONITOR_CONFIG_SCHEMA:
        raise EtfMonitorConfigError("unsupported ETF monitor config schema")
    if not isinstance(payload.get("enabled"), bool):
        raise EtfMonitorConfigError("enabled must be boolean")
    if payload.get("mode") != "shadow":
        raise EtfMonitorConfigError("first release is shadow-only")
    chat_id = str(payload.get("chat_id") or "").strip()
    if not _CHAT_ID_RE.fullmatch(chat_id):
        raise EtfMonitorConfigError("ETF chat_id must be a negative Telegram group id string")
    if chat_id != ETF_MONITOR_CHAT_ID:
        raise EtfMonitorConfigError("ETF chat_id must remain the dedicated 0050 group")
    if str(payload.get("symbol") or "").strip().upper() != "0050":
        raise EtfMonitorConfigError("first release only supports symbol 0050")
    if payload.get("strategy") != "final-v1.7":
        raise EtfMonitorConfigError("first release only supports final-v1.7")
    if payload.get("timezone") != "Asia/Taipei":
        raise EtfMonitorConfigError("ETF monitor timezone must be Asia/Taipei")
    if payload.get("require_get_chat") is not True:
        raise EtfMonitorConfigError("ETF route verification with getChat cannot be disabled")
    for field in ("daily_shadow_status", "quiet_status_in_formal_mode"):
        if not isinstance(payload.get(field), bool):
            raise EtfMonitorConfigError(f"{field} must be boolean")
    schedule = payload.get("schedule")
    if not isinstance(schedule, Mapping):
        raise EtfMonitorConfigError("schedule must be an object")
    if set(schedule) != {"preview", "decision", "reconciliation"}:
        raise EtfMonitorConfigError("schedule contains missing or unknown fields")
    parsed_schedule = EtfSchedule(
        preview=_parse_time(schedule.get("preview"), "schedule.preview"),
        decision=_parse_time(schedule.get("decision"), "schedule.decision"),
        reconciliation=_parse_time(schedule.get("reconciliation"), "schedule.reconciliation"),
    )
    actual_times = (
        parsed_schedule.preview.strftime("%H:%M"),
        parsed_schedule.decision.strftime("%H:%M"),
        parsed_schedule.reconciliation.strftime("%H:%M"),
    )
    if actual_times != ("12:45", "13:55", "22:00"):
        raise EtfMonitorConfigError("final-v1.7 schedule must remain 12:45/13:55/22:00")
    data = payload.get("data") or {}
    if not isinstance(data, Mapping):
        raise EtfMonitorConfigError("data must be an object")
    if set(data) != {
        "snapshot_id",
        "frozen_market_data_relative_path",
        "delta_relative_path",
        "raw_daily_relative_path",
        "corporate_actions_relative_path",
        "dividends_relative_path",
    }:
        raise EtfMonitorConfigError("data contains missing or unknown fields")
    if data.get("snapshot_id") is not None and not isinstance(data.get("snapshot_id"), str):
        raise EtfMonitorConfigError("data.snapshot_id must be a string or null")
    timeouts = payload.get("job_timeouts_seconds") or {}
    if not isinstance(timeouts, Mapping):
        raise EtfMonitorConfigError("job_timeouts_seconds must be an object")
    if set(timeouts) != {"preview", "decision", "reconciliation"}:
        raise EtfMonitorConfigError("job_timeouts_seconds contains missing or unknown fields")
    timeout_values: dict[str, int] = {}
    for key in ("preview", "decision", "reconciliation"):
        try:
            value = int(timeouts.get(key))
        except (TypeError, ValueError) as exc:
            raise EtfMonitorConfigError(f"job_timeouts_seconds.{key} must be an integer") from exc
        if value < 30 or value > 3600:
            raise EtfMonitorConfigError(f"job_timeouts_seconds.{key} is outside 30..3600")
        timeout_values[key] = value
    database_relative_path = _safe_relative_path(
        payload.get("database_relative_path"), "database_relative_path"
    )
    if not database_relative_path.endswith((".sqlite", ".sqlite3", ".db")):
        raise EtfMonitorConfigError("database_relative_path must name a SQLite file")
    return EtfMonitorConfig(
        schema_version=ETF_MONITOR_CONFIG_SCHEMA,
        enabled=bool(payload["enabled"]),
        mode="shadow",
        chat_id=chat_id,
        symbol="0050",
        strategy="final-v1.7",
        timezone="Asia/Taipei",
        schedule=parsed_schedule,
        daily_shadow_status=bool(payload.get("daily_shadow_status", True)),
        quiet_status_in_formal_mode=bool(payload.get("quiet_status_in_formal_mode", True)),
        require_get_chat=True,
        database_relative_path=database_relative_path,
        snapshot_id=(str(data.get("snapshot_id")).strip() if data.get("snapshot_id") else None),
        frozen_market_data_relative_path=_safe_relative_path(
            data.get("frozen_market_data_relative_path"),
            "data.frozen_market_data_relative_path",
        ),
        delta_relative_path=_safe_relative_path(
            data.get("delta_relative_path"), "data.delta_relative_path"
        ),
        raw_daily_relative_path=_safe_relative_path(
            data.get("raw_daily_relative_path", "data/0050/daily_raw.json"),
            "data.raw_daily_relative_path",
        ),
        corporate_actions_relative_path=_safe_relative_path(
            data.get(
                "corporate_actions_relative_path",
                "events/0050_corporate_action_events.json",
            ),
            "data.corporate_actions_relative_path",
        ),
        dividends_relative_path=_safe_relative_path(
            data.get("dividends_relative_path", "events/0050_dividends.json"),
            "data.dividends_relative_path",
        ),
        job_timeouts_seconds=timeout_values,
        source_path=source_path.resolve(),
    )


def load_etf_monitor_config(path: Path | str | None = None) -> EtfMonitorConfig:
    source = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    try:
        payload = json.loads(source.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EtfMonitorConfigError(f"cannot load ETF monitor config: {source}") from exc
    if not isinstance(payload, Mapping):
        raise EtfMonitorConfigError("ETF monitor config root must be an object")
    forbidden = {"api_token", "bot_token", "token", "trade_monitor_chat_id"} & set(payload)
    if forbidden:
        raise EtfMonitorConfigError(
            "ETF monitor config must not contain Telegram secrets or legacy routes"
        )
    return validate_etf_monitor_config(payload, source_path=source)
