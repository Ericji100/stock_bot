"""Data source health management, quota tracking, and rate limiting.

Provides:
- SourceHealthManager: per-source cooldown after consecutive failures
- FinMindQuotaManager: 500/hour safe limit (official 600/hour)
- FugleRateLimiter: 60/min for historical/intraday, 1 connection / 5 subscriptions for WebSocket

State files (all under .cache/):
  source_health.json   - SourceHealthManager
  finmind_quota.json   - FinMindQuotaManager
  fugle_quota.json     - FugleRateLimiter
"""

from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from threading import Lock

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_CACHE_DIR = Path(".cache")
_CACHE_DIR.mkdir(exist_ok=True)

_LOCK = Lock()


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_json(path: Path, data: dict) -> None:
    with _LOCK:
        with path.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# SourceHealthManager
# ---------------------------------------------------------------------------

_SOURCE_HEALTH_PATH = _CACHE_DIR / "source_health.json"

# Failure tiers → cooldown seconds
_COOLDOWN_BY_FAILURE_COUNT = {
    1: 0,      # 1st failure: no cooldown, just record
    2: 300,    # 2nd: 5 minutes
    3: 600,    # 3rd: 10 minutes
}

_MAX_COOLDOWN_SECONDS = 30 * 60  # 4th+ failure → 30 minutes


class SourceHealthManager:
    """Tracks per-source availability with escalating cooldown on consecutive failures.

    Sources: yahoo, fugle, finmind, twse_t86, tpex_institutional,
             twse_mi_qfiis, official
    """

    def __init__(self) -> None:
        self._data = _load_json(_SOURCE_HEALTH_PATH)

    def _persist(self) -> None:
        _save_json(_SOURCE_HEALTH_PATH, self._data)

    def _now(self) -> datetime:
        return datetime.now()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_available(self, source: str) -> bool:
        """Return True if source is currently usable (not in cooldown)."""
        self._clean_expired(source)
        source_data = self._data.get(source, {})
        cooldown_until = source_data.get("cooldown_until")
        if cooldown_until:
            try:
                if self._now() < datetime.fromisoformat(cooldown_until):
                    return False
            except Exception:
                pass
        return True

    def record_success(self, source: str) -> None:
        """Clear failure count and cooldown after a successful request."""
        if source not in self._data:
            self._data[source] = {}
        self._data[source]["failure_count"] = 0
        self._data[source]["cooldown_until"] = None
        self._persist()

    def record_failure(self, source: str, error: str = "") -> None:
        """Increment failure count; apply escalating cooldown starting at 2 failures."""
        if source not in self._data:
            self._data[source] = {"failure_count": 0}
        fc = self._data[source].get("failure_count", 0) + 1
        self._data[source]["failure_count"] = fc
        self._data[source]["last_error"] = str(error)[:200]

        cooldown_seconds = _COOLDOWN_BY_FAILURE_COUNT.get(
            fc,
            _MAX_COOLDOWN_SECONDS,
        )
        if cooldown_seconds > 0:
            cooldown_until = self._now() + timedelta(seconds=cooldown_seconds)
            self._data[source]["cooldown_until"] = cooldown_until.isoformat()
        self._persist()

    def get_status(self, source: str) -> dict:
        """Return dict with failure_count, cooldown_until, last_error."""
        self._clean_expired(source)
        base = self._data.get(source, {})
        return {
            "failure_count": base.get("failure_count", 0),
            "cooldown_until": base.get("cooldown_until"),
            "last_error": base.get("last_error", ""),
        }

    def get_cooling_sources(self) -> list[str]:
        """Return list of sources currently in cooldown."""
        cooling = []
        for source in list(self._data.keys()):
            self._clean_expired(source)
            if not self.is_available(source):
                cooling.append(source)
        return cooling

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _clean_expired(self, source: str) -> None:
        source_data = self._data.get(source, {})
        cooldown_until = source_data.get("cooldown_until")
        if not cooldown_until:
            return
        try:
            if self._now() >= datetime.fromisoformat(cooldown_until):
                # Cooldown expired → clear
                self._data[source]["failure_count"] = 0
                self._data[source]["cooldown_until"] = None
                self._persist()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# FinMindQuotaManager
# ---------------------------------------------------------------------------

_FINMIND_QUOTA_PATH = _CACHE_DIR / "finmind_quota.json"
_FINMIND_SAFE_LIMIT = 500          # safe limit (official 600/hour)
_FINMIND_SCOPE_LIMITS = {
    "backfill": 300,   # per backfill run
    "scan": 80,        # per scan
    "research": 20,    # per research call
    "default": _FINMIND_SAFE_LIMIT,
}


class FinMindQuotaManager:
    """Tracks FinMind API usage with hourly reset and per-scope safe limits.

    Official limit: 600/hour.  Program safe limit: 500/hour.
    """

    def __init__(self) -> None:
        self._data = _load_json(_FINMIND_QUOTA_PATH)
        self._clean_expired_hour()

    def _persist(self) -> None:
        _save_json(_FINMIND_QUOTA_PATH, self._data)

    def _now(self) -> datetime:
        return datetime.now()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def can_use(self, cost: int = 1, scope: str = "default") -> bool:
        """Return True if we have remaining quota for the given cost and scope."""
        self._clean_expired_hour()
        key = f"scope_{scope}"
        used = self._data.get(key, 0)
        limit = _FINMIND_SCOPE_LIMITS.get(scope, _FINMIND_SAFE_LIMIT)
        # Also enforce global hourly limit across all scopes
        hourly_used = self._data.get("hourly_total", 0)
        if hourly_used + cost > _FINMIND_SAFE_LIMIT:
            return False
        return used + cost <= limit

    def record_use(self, cost: int = 1, scope: str = "default") -> None:
        """Record consumption and persist."""
        self._clean_expired_hour()
        key = f"scope_{scope}"
        self._data[key] = self._data.get(key, 0) + cost
        self._data["total"] = self._data.get("total", 0) + cost
        self._data["hourly_total"] = self._data.get("hourly_total", 0) + cost
        self._persist()

    def remaining_safe_quota(self, scope: str = "default") -> int:
        """Return remaining safe quota for the given scope."""
        self._clean_expired_hour()
        key = f"scope_{scope}"
        used = self._data.get(key, 0)
        limit = _FINMIND_SCOPE_LIMITS.get(scope, _FINMIND_SAFE_LIMIT)
        return max(0, limit - used)

    def hourly_remaining(self) -> int:
        """Return remaining out of the 500/hour safe limit."""
        self._clean_expired_hour()
        used = self._data.get("hourly_total", 0)
        return max(0, _FINMIND_SAFE_LIMIT - used)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _clean_expired_hour(self) -> None:
        """Reset hourly counters if the current hour has changed."""
        current_hour = self._now().strftime("%Y-%m-%dT%H")
        last_hour = self._data.get("_hour_key")
        if last_hour != current_hour:
            self._data["hourly_total"] = 0
            self._data["_hour_key"] = current_hour
            # Also reset all scope counters so they don't accumulate forever
            for k in list(self._data.keys()):
                if k.startswith("scope_"):
                    self._data[k] = 0
            self._persist()


# ---------------------------------------------------------------------------
# FugleRateLimiter
# ---------------------------------------------------------------------------

_FUGLE_QUOTA_PATH = _CACHE_DIR / "fugle_quota.json"

# Limits per endpoint type
_ENDPOINT_LIMITS = {
    "historical": 60,          # 60/min
    "intraday": 60,            # 60/min
    "websocket_connection": 1,
    "websocket_subscription": 5,
}

# Scope keys in JSON
_SCOPE_KEYS = {
    "historical": "minute_historical",
    "intraday": "minute_intraday",
    "websocket_connection": "websocket_connection",
    "websocket_subscription": "websocket_subscription",
}


class FugleRateLimiter:
    """Tracks Fugle API usage with per-minute reset for historical/intraday,
    and hard limits for websocket connection/subscriptions."""

    def __init__(self) -> None:
        self._data = _load_json(_FUGLE_QUOTA_PATH)
        self._clean_expired_minute()

    def _persist(self) -> None:
        _save_json(_FUGLE_QUOTA_PATH, self._data)

    def _now(self) -> datetime:
        return datetime.now()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def can_use(self, endpoint_type: str = "historical", cost: int = 1) -> bool:
        """Return True if we have remaining quota for the endpoint type."""
        self._clean_expired_minute()
        limit = _ENDPOINT_LIMITS.get(endpoint_type, 1)
        key = _SCOPE_KEYS.get(endpoint_type, endpoint_type)
        used = self._data.get(key, 0)
        return used + cost <= limit

    def record_use(self, endpoint_type: str = "historical", cost: int = 1) -> None:
        """Record consumption and persist."""
        self._clean_expired_minute()
        key = _SCOPE_KEYS.get(endpoint_type, endpoint_type)
        self._data[key] = self._data.get(key, 0) + cost
        self._persist()

    def remaining_quota(self, endpoint_type: str = "historical") -> int:
        """Return remaining quota for the endpoint type."""
        self._clean_expired_minute()
        limit = _ENDPOINT_LIMITS.get(endpoint_type, 1)
        key = _SCOPE_KEYS.get(endpoint_type, endpoint_type)
        used = self._data.get(key, 0)
        return max(0, limit - used)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _clean_expired_minute(self) -> None:
        """Reset per-minute counters if the current minute has changed."""
        current_minute = self._now().strftime("%Y-%m-%dT%H:%M")
        last_minute = self._data.get("_minute_key")
        if last_minute != current_minute:
            self._data["minute_historical"] = 0
            self._data["minute_intraday"] = 0
            self._data["_minute_key"] = current_minute
            self._persist()