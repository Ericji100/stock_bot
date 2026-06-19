from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

ERROR_CLASSIFICATION_SCHEMA_VERSION = "error_classification_v1"

NETWORK_ERROR = "network_error"
QUOTA_EXHAUSTED = "quota_exhausted"
SOURCE_BLOCKED = "source_blocked"
PARSE_FAILED = "parse_failed"
CACHE_INVALID = "cache_invalid"
AI_TIMEOUT = "ai_timeout"
REPORT_GENERATION_FAILED = "report_generation_failed"
UNKNOWN_ERROR = "unknown_error"


@dataclass(frozen=True)
class ClassifiedError:
    error_type: str
    message: str
    source: str | None = None
    operation: str | None = None
    retryable: bool = False
    created_at: str = field(default_factory=lambda: datetime.now().astimezone().isoformat(timespec="seconds"))
    schema_version: str = ERROR_CLASSIFICATION_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def classify_error(exc: BaseException | str, *, source: str | None = None, operation: str | None = None) -> ClassifiedError:
    message = str(exc)
    lowered = message.lower()
    error_type = UNKNOWN_ERROR
    retryable = False
    if isinstance(exc, TimeoutError):
        error_type = AI_TIMEOUT
        retryable = True
    elif any(token in lowered for token in ("timeout", "timed out", "read operation timed out")):
        error_type = AI_TIMEOUT if source in {"gemini", "minimax", "opencode", "ai"} or "model" in lowered else NETWORK_ERROR
        retryable = True
    elif any(token in lowered for token in ("429", "quota", "rate limit", "exceeded", "too many requests")):
        error_type = QUOTA_EXHAUSTED
        retryable = True
    elif any(token in lowered for token in ("403", "forbidden", "blocked", "captcha", "10054")):
        error_type = SOURCE_BLOCKED
        retryable = False
    elif any(token in lowered for token in ("connection", "dns", "ssl", "certificate", "network", "remote host")):
        error_type = NETWORK_ERROR
        retryable = True
    elif any(token in lowered for token in ("json", "parse", "decode", "schema", "csv", "html")):
        error_type = PARSE_FAILED
        retryable = False
    elif any(token in lowered for token in ("cache", "marker", "expired", "invalid")):
        error_type = CACHE_INVALID
        retryable = False
    elif any(token in lowered for token in ("report", "markdown", "html", "artifact")):
        error_type = REPORT_GENERATION_FAILED
        retryable = False
    return ClassifiedError(error_type=error_type, message=message, source=source, operation=operation, retryable=retryable)


def build_health_event(exc: BaseException | str, *, source: str | None = None, operation: str | None = None, context: dict[str, Any] | None = None) -> dict[str, Any]:
    classified = classify_error(exc, source=source, operation=operation).to_dict()
    classified["context"] = context or {}
    return classified


def summarize_health_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    retryable = 0
    for event in events:
        key = str(event.get("error_type") or UNKNOWN_ERROR)
        counts[key] = counts.get(key, 0) + 1
        if event.get("retryable"):
            retryable += 1
    return {
        "schema_version": ERROR_CLASSIFICATION_SCHEMA_VERSION,
        "total": len(events),
        "counts": counts,
        "retryable_count": retryable,
        "latest": events[-1] if events else None,
    }
