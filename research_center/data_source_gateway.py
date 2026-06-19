from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Callable

from .error_classification_service import build_health_event

DATA_SOURCE_GATEWAY_SCHEMA_VERSION = "data_source_gateway_v1"

DEFAULT_GATEWAY_SOURCE_NAMES = (
    "yahoo",
    "fugle",
    "finmind",
    "twse_t86",
    "tpex_institutional",
    "twse_mi_qfiis",
    "official",
    "tavily",
    "minimax_search",
    "gemini_search",
)


@dataclass(frozen=True)
class DataSourceAttempt:
    provider: str
    status: str
    elapsed_seconds: float
    error: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DataSourceResult:
    status: str
    provider: str | None
    data: Any
    attempts: list[DataSourceAttempt]
    schema_version: str = DATA_SOURCE_GATEWAY_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "provider": self.provider,
            "schema_version": self.schema_version,
            "attempts": [asdict(item) for item in self.attempts],
        }


def run_provider_chain(
    providers: list[tuple[str, Callable[[], Any]]],
    *,
    operation: str,
    accept: Callable[[Any], bool] | None = None,
) -> DataSourceResult:
    attempts: list[DataSourceAttempt] = []
    accept_fn = accept or _has_data
    for name, call in providers:
        started = datetime.now()
        try:
            data = call()
            elapsed = (datetime.now() - started).total_seconds()
            if accept_fn(data):
                attempts.append(DataSourceAttempt(provider=name, status="success", elapsed_seconds=elapsed))
                return DataSourceResult(status="success", provider=name, data=data, attempts=attempts)
            attempts.append(DataSourceAttempt(provider=name, status="empty", elapsed_seconds=elapsed))
        except Exception as exc:
            elapsed = (datetime.now() - started).total_seconds()
            attempts.append(
                DataSourceAttempt(
                    provider=name,
                    status="failed",
                    elapsed_seconds=elapsed,
                    error=build_health_event(exc, source=name, operation=operation),
                )
            )
    return DataSourceResult(status="failed", provider=None, data=None, attempts=attempts)


def gateway_health_summary(results: list[DataSourceResult]) -> dict[str, Any]:
    providers: dict[str, dict[str, int]] = {}
    for result in results:
        for attempt in result.attempts:
            row = providers.setdefault(attempt.provider, {"success": 0, "failed": 0, "empty": 0})
            row[attempt.status] = row.get(attempt.status, 0) + 1
    return {
        "schema_version": DATA_SOURCE_GATEWAY_SCHEMA_VERSION,
        "provider_count": len(providers),
        "providers": providers,
    }


def build_data_source_gateway_snapshot(
    *,
    source_names: tuple[str, ...] = DEFAULT_GATEWAY_SOURCE_NAMES,
    health_manager: Any | None = None,
    finmind_quota: Any | None = None,
    fugle_limiter: Any | None = None,
) -> dict[str, Any]:
    """Return a shared snapshot for source health, cooldowns, and quota state."""
    try:
        if health_manager is None or finmind_quota is None or fugle_limiter is None:
            from data_source_manager import FinMindQuotaManager, FugleRateLimiter, SourceHealthManager

            health_manager = health_manager or SourceHealthManager()
            finmind_quota = finmind_quota or FinMindQuotaManager()
            fugle_limiter = fugle_limiter or FugleRateLimiter()

        sources = {
            name: _source_status_snapshot(health_manager, name)
            for name in source_names
        }
        cooling_sources = _safe_list_call(health_manager, "get_cooling_sources")
        return {
            "schema_version": DATA_SOURCE_GATEWAY_SCHEMA_VERSION,
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "source_count": len(sources),
            "sources": sources,
            "cooling_sources": cooling_sources,
            "available_sources": [
                name
                for name, status in sources.items()
                if status.get("available") is True
            ],
            "quota": {
                "finmind_hourly_remaining": _safe_call(finmind_quota, "hourly_remaining"),
                "finmind_default_remaining": _safe_call(finmind_quota, "remaining_safe_quota", "default"),
                "finmind_backfill_remaining": _safe_call(finmind_quota, "remaining_safe_quota", "backfill"),
                "finmind_scan_remaining": _safe_call(finmind_quota, "remaining_safe_quota", "scan"),
                "finmind_research_remaining": _safe_call(finmind_quota, "remaining_safe_quota", "research"),
                "fugle_historical_remaining": _safe_call(fugle_limiter, "remaining_quota", "historical"),
                "fugle_intraday_remaining": _safe_call(fugle_limiter, "remaining_quota", "intraday"),
            },
        }
    except Exception as exc:
        return {
            "schema_version": DATA_SOURCE_GATEWAY_SCHEMA_VERSION,
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "source_count": 0,
            "sources": {},
            "cooling_sources": [],
            "available_sources": [],
            "quota": {},
            "error": build_health_event(exc, source="data_source_gateway", operation="build_snapshot"),
        }


def format_data_source_gateway_snapshot(snapshot: dict[str, Any]) -> str:
    quota = snapshot.get("quota") or {}
    return "\n".join(
        [
            "【資料來源閘道】",
            f"- 來源數：{snapshot.get('source_count', 0)}",
            f"- 可用來源：{_format_list(snapshot.get('available_sources') or [])}",
            f"- 冷卻來源：{_format_list(snapshot.get('cooling_sources') or [])}",
            f"- FinMind 每小時剩餘：{quota.get('finmind_hourly_remaining', 'unknown')}",
            f"- Fugle historical 剩餘：{quota.get('fugle_historical_remaining', 'unknown')}/min",
            f"- Fugle intraday 剩餘：{quota.get('fugle_intraday_remaining', 'unknown')}/min",
        ]
    )


def _has_data(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, (str, list, tuple, set, dict)):
        return bool(value)
    try:
        import pandas as pd

        if isinstance(value, pd.DataFrame):
            return not value.empty
    except Exception:
        pass
    return True


def _source_status_snapshot(health_manager: Any, source: str) -> dict[str, Any]:
    status = dict(_safe_call(health_manager, "get_status", source) or {})
    status["available"] = bool(_safe_call(health_manager, "is_available", source))
    status["source"] = source
    return status


def _safe_call(target: Any, method_name: str, *args: Any) -> Any:
    method = getattr(target, method_name, None)
    if method is None:
        return None
    try:
        return method(*args)
    except Exception:
        return None


def _safe_list_call(target: Any, method_name: str) -> list[str]:
    value = _safe_call(target, method_name)
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def _format_list(values: list[Any]) -> str:
    return "、".join(str(value) for value in values) if values else "無"
