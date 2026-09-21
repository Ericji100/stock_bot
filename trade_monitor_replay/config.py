from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / ".runtime" / "trade_monitor_replay" / "config.json"
DEFAULT_RULE_MANIFEST_PATH = (
    Path(__file__).resolve().parent
    / "rules"
    / "formal-v2.1.8-replay-adapter-v3"
    / "rule-manifest.json"
)


class ReplayConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ReplayConfig:
    instrument: str = "TMF"
    ai_provider: str = "minimax"
    ai_model: str = "MiniMax-M3"
    codex_reasoning_effort: str = "medium"
    codex_executable: str = "codex"
    codex_max_session_turns: int = 4
    ai_timeout_seconds: float = 300.0
    max_ai_attempts: int = 2
    max_output_tokens: int = 16384
    detail_bar_count: int = 180
    overview_minutes: int = 15
    analysis_every_bars: int = 2
    telegram_enabled: bool = False
    telegram_chat_id: str | None = None
    send_quiet_status: bool = True
    quiet_status_interval_minutes: int = 10
    message_profile: str = "course_event_cards"
    rule_manifest_path: Path = DEFAULT_RULE_MANIFEST_PATH


def load_replay_config(path: Path = DEFAULT_CONFIG_PATH) -> ReplayConfig:
    raw = _read_json(path)
    instrument = str(raw.get("instrument", "TMF")).strip().upper()
    if instrument != "TMF":
        raise ReplayConfigError("第一版歷史回放只允許明確指定 TMF。")
    ai_model = str(raw.get("ai_model", "MiniMax-M3")).strip()
    if not ai_model:
        raise ReplayConfigError("ai_model 不得空白。")
    ai_provider = str(raw.get("ai_provider", "minimax")).strip().lower()
    if ai_provider not in {"minimax", "codex"}:
        raise ReplayConfigError("ai_provider只允許minimax或codex。")
    codex_reasoning_effort = str(raw.get("codex_reasoning_effort", "medium")).strip().lower()
    if codex_reasoning_effort not in {"low", "medium", "high", "xhigh"}:
        raise ReplayConfigError("codex_reasoning_effort只允許low、medium、high或xhigh。")
    codex_executable = str(raw.get("codex_executable", "codex")).strip()
    if not codex_executable:
        raise ReplayConfigError("codex_executable不得空白。")
    codex_max_session_turns = _positive_int(
        raw.get("codex_max_session_turns", 4), "codex_max_session_turns"
    )
    if codex_max_session_turns > 16:
        raise ReplayConfigError("codex_max_session_turns最多16輪，避免長對話累積造成逾時。")
    ai_timeout_seconds = _positive_float(raw.get("ai_timeout_seconds", 300), "ai_timeout_seconds")
    max_ai_attempts = _positive_int(raw.get("max_ai_attempts", 2), "max_ai_attempts")
    if max_ai_attempts > 3:
        raise ReplayConfigError("max_ai_attempts 最多只能為 3，避免單根 K 無界重試。")
    max_output_tokens = _positive_int(raw.get("max_output_tokens", 16384), "max_output_tokens")
    detail_bar_count = _positive_int(raw.get("detail_bar_count", 180), "detail_bar_count")
    overview_minutes = _positive_int(raw.get("overview_minutes", 15), "overview_minutes")
    analysis_every_bars = _positive_int(raw.get("analysis_every_bars", 2), "analysis_every_bars")
    if analysis_every_bars > 15:
        raise ReplayConfigError("analysis_every_bars最多15根。")
    telegram_enabled = _boolean(raw.get("telegram_enabled", False), "telegram_enabled")
    send_quiet_status = _boolean(raw.get("send_quiet_status", True), "send_quiet_status")
    quiet_status_interval_minutes = _positive_int(
        raw.get("quiet_status_interval_minutes", 10),
        "quiet_status_interval_minutes",
    )
    if quiet_status_interval_minutes > 60:
        raise ReplayConfigError("quiet_status_interval_minutes最多60分鐘。")
    message_profile = str(raw.get("message_profile", "course_event_cards")).strip().lower()
    if message_profile not in {"course_event_cards", "formal_nine_sections"}:
        raise ReplayConfigError(
            "message_profile只允許course_event_cards或formal_nine_sections。"
        )
    chat_value = raw.get("telegram_chat_id")
    telegram_chat_id = None if chat_value is None else str(chat_value).strip() or None
    manifest_value = raw.get("rule_manifest_path")
    manifest_path = DEFAULT_RULE_MANIFEST_PATH
    if manifest_value:
        manifest_path = Path(str(manifest_value))
        if not manifest_path.is_absolute():
            manifest_path = PROJECT_ROOT / manifest_path
    return ReplayConfig(
        instrument=instrument,
        ai_provider=ai_provider,
        ai_model=ai_model,
        codex_reasoning_effort=codex_reasoning_effort,
        codex_executable=codex_executable,
        codex_max_session_turns=codex_max_session_turns,
        ai_timeout_seconds=ai_timeout_seconds,
        max_ai_attempts=max_ai_attempts,
        max_output_tokens=max_output_tokens,
        detail_bar_count=detail_bar_count,
        overview_minutes=overview_minutes,
        analysis_every_bars=analysis_every_bars,
        telegram_enabled=telegram_enabled,
        telegram_chat_id=telegram_chat_id,
        send_quiet_status=send_quiet_status,
        quiet_status_interval_minutes=quiet_status_interval_minutes,
        message_profile=message_profile,
        rule_manifest_path=manifest_path.resolve(),
    )


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReplayConfigError("歷史回放設定檔無法讀取。") from exc
    if not isinstance(payload, dict):
        raise ReplayConfigError("歷史回放設定檔必須是 JSON object。")
    return payload


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ReplayConfigError(f"{field} 必須是正整數。")
    return value


def _positive_float(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or float(value) <= 0:
        raise ReplayConfigError(f"{field} 必須是正數。")
    return float(value)


def _boolean(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ReplayConfigError(f"{field} 必須是 boolean。")
    return value
