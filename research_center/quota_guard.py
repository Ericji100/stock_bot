from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any


class SearchProviderQuotaGuard:
    def __init__(self, state_path: Path):
        self.state_path = state_path

    def is_available(self, provider: str, today: date | None = None) -> bool:
        today = today or date.today()
        state = self._read()
        entry = state.get(provider) or {}
        disabled_until_raw = entry.get("disabled_until")
        if not disabled_until_raw:
            return True
        try:
            disabled_until = date.fromisoformat(str(disabled_until_raw))
        except ValueError:
            return True
        if today >= disabled_until:
            self._clear(provider)
            return True
        return False

    def mark_exhausted(self, provider: str, reason: str, today: date | None = None) -> None:
        today = today or date.today()
        if today.month == 12:
            next_month = date(today.year + 1, 1, 1)
        else:
            next_month = date(today.year, today.month + 1, 1)
        state = self._read()
        state[provider] = {
            "disabled_until": next_month.isoformat(),
            "reason": reason,
            "last_error": reason[:500],
            "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        self._write(state)

    def record_usage(self, provider: str, units: int = 1, today: date | None = None) -> None:
        today = today or date.today()
        state = self._read()
        entry = state.get(provider) or {}
        month_key = today.strftime("%Y-%m")
        usage = entry.get("usage") or {}
        usage[month_key] = usage.get(month_key, 0) + units
        entry["usage"] = usage
        state[provider] = entry
        self._write(state)

    def is_under_monthly_limit(
        self,
        provider: str,
        monthly_limit: int,
        reserve: int = 0,
        today: date | None = None,
    ) -> bool:
        today = today or date.today()
        state = self._read()
        entry = state.get(provider) or {}
        usage = entry.get("usage") or {}
        used = int(usage.get(today.strftime("%Y-%m"), 0) or 0)
        return used < max(0, monthly_limit - reserve)

    def _read(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {}
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _write(self, state: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    def _clear(self, provider: str) -> None:
        state = self._read()
        state.pop(provider, None)
        self._write(state)
