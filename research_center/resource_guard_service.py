from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, AsyncIterator

RESOURCE_GUARD_SCHEMA_VERSION = "resource_guard_v1"


@dataclass(frozen=True)
class ResourcePoolSnapshot:
    name: str
    limit: int
    active: int
    available: int
    schema_version: str = RESOURCE_GUARD_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ResourceGuardService:
    """Lightweight async resource limits for background work.

    This service only controls concurrency. It does not change command output,
    prompt content, data ordering, scoring, or report formatting.
    """

    def __init__(self, limits: dict[str, int] | None = None) -> None:
        self._limits = {name: max(1, int(limit)) for name, limit in (limits or {}).items()}
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._active: dict[str, int] = {}
        self._lock = asyncio.Lock()

    def configure(self, name: str, limit: int) -> None:
        if name in self._semaphores:
            raise RuntimeError(f"resource pool already initialized: {name}")
        self._limits[name] = max(1, int(limit))

    @asynccontextmanager
    async def acquire(self, name: str) -> AsyncIterator[None]:
        pool = str(name or "default")
        semaphore = self._semaphore(pool)
        await semaphore.acquire()
        async with self._lock:
            self._active[pool] = self._active.get(pool, 0) + 1
        try:
            yield
        finally:
            async with self._lock:
                self._active[pool] = max(0, self._active.get(pool, 0) - 1)
            semaphore.release()

    def snapshot(self) -> dict[str, Any]:
        pools = {}
        for name in sorted(set(self._limits) | set(self._semaphores) | set(self._active)):
            limit = self._limits.get(name, 1)
            active = self._active.get(name, 0)
            pools[name] = ResourcePoolSnapshot(
                name=name,
                limit=limit,
                active=active,
                available=max(0, limit - active),
            ).to_dict()
        return {
            "schema_version": RESOURCE_GUARD_SCHEMA_VERSION,
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "pool_count": len(pools),
            "pools": pools,
        }

    def _semaphore(self, name: str) -> asyncio.Semaphore:
        if name not in self._semaphores:
            self._semaphores[name] = asyncio.Semaphore(self._limits.get(name, 1))
        return self._semaphores[name]


DEFAULT_RESOURCE_GUARD = ResourceGuardService(
    {
        "background_backfill": 1,
        "background_ai_maintenance": 1,
        "background_news": 1,
    }
)
