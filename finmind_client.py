"""FinMind API client with health/cooldown management and quota tracking.

API Key resolution order:
  1. Passed as argument
  2. config/secrets.json → finmind_api_key
  3. Environment variable FINMIND_API_KEY

Behavior:
  - Before every request: check SourceHealthManager.is_available("finmind")
    and FinMindQuotaManager.can_use()
  - On success: record_use() + record_success()
  - On failure: record_failure() and re-raise
  - No key available → returns empty dict without raising
  - Quota exceeded → returns empty dict without raising
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx

ROOT_DIR = Path(__file__).resolve().parent


def _load_api_key() -> str | None:
    # 1. Environment variable
    env_key = os.getenv("FINMIND_API_KEY")
    if env_key:
        return env_key.strip()

    # 2. config/secrets.json
    secrets_path = ROOT_DIR / "config" / "secrets.json"
    if secrets_path.exists():
        try:
            import json
            data = json.loads(secrets_path.read_text(encoding="utf-8"))
            key = data.get("finmind_api_key")
            if key:
                return str(key).strip()
        except Exception:
            pass
    return None


class FinMindClient:
    def __init__(
        self,
        api_key: str | None = None,
        health_manager=None,
        quota_manager=None,
        timeout: float = 10.0,
    ) -> None:
        self._api_key = api_key or _load_api_key()
        self._health = health_manager
        self._quota = quota_manager
        self._timeout = timeout

    def request_dataset(
        self,
        dataset: str,
        params: dict,
        scope: str = "default",
    ) -> dict:
        """Call FinMind API for the given dataset and params.

        Returns {} if no key, quota exceeded, or source in cooldown.
        Raises on HTTP error (caller should catch and record_failure).
        """
        if not self._api_key:
            return {}

        # Check quota
        if self._quota and not self._quota.can_use(cost=1, scope=scope):
            return {}

        # Check health cooldown
        if self._health and not self._health.is_available("finmind"):
            return {}

        try:
            with httpx.Client(timeout=self._timeout, follow_redirects=True) as client:
                response = client.get(
                    "https://api.finmindtrade.com/api/v4/data",
                    params={"dataset": dataset, "data_id": params.get("stock_id", ""), **params},
                    headers={"Authorization": f"Bearer {self._api_key}"},
                )
                response.raise_for_status()
                result = response.json()
        except Exception as exc:
            if self._health:
                self._health.record_failure("finmind", str(exc))
            raise

        # Record successful use
        if self._quota:
            self._quota.record_use(cost=1, scope=scope)
        if self._health:
            self._health.record_success("finmind")

        return result