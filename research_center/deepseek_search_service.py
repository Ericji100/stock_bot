"""Shared official DeepSeek Search/Fetch adapters with date-aware cache keys."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from decimal import Decimal
from typing import Any

from .deepseek_service import DeepSeekService, DeepSeekUnavailable, TASK
from .models import SourceItem
from .source_rank import make_source_items
from .source_text_cleaner import clean_source_title


@dataclass(frozen=True)
class DiscoveryResult:
    sources: list[SourceItem]
    diagnostics: dict[str, Any]


def _safe_result_title(row: dict, url: str = "") -> dict:
    title = str(row.get("title") or "")
    normalized = clean_source_title(title, row.get("url") or url)
    if normalized != title:
        # The untouched provider response stays in the cache for provenance.
        return {**row, "title": normalized, "title_normalization": "code_isolated"}
    return row


class DeepSeekSearchService:
    provider = "deepseek_search"

    def __init__(self, client: DeepSeekService):
        self.client = client
        self.max_queries_per_task = 0

    def is_configured(self):
        return self.client.is_configured()

    def search(self, query: str, *, report_date: str = "") -> dict:
        if not query.strip() or len(query) > 128:
            raise ValueError("搜尋查詢必須介於 1 至 128 字，請拆成具體問題。")
        body = {"query": query, "count": 10, "need_content": True}
        key = "search:" + hashlib.sha256(json.dumps([body, report_date], sort_keys=True).encode()).hexdigest()
        with self.client.account.singleflight(key):
            data = self.client.account.cached(key)
            cached = data is not None
            if data is None:
                data = self.client._post("v1/tools/search", body, Decimal("0.00075"))
                if data.get("results"):
                    self.client.account.cache(key, data, 900)
        return {**data, "results": [_safe_result_title(row) for row in data.get("results") or []], "cache_hit": cached}

    def fetch(self, url: str) -> dict:
        key = "fetch:" + hashlib.sha256(url.encode()).hexdigest()
        with self.client.account.singleflight(key):
            data = self.client.account.cached(key)
            if data is not None:
                return {**_safe_result_title(data, url), "cache_hit": True}
            data = self.client._post("v1/tools/fetch", {"url": url}, Decimal("0.00075"))
            if data.get("content"):
                self.client.account.cache(key, data, 3600)
            return {**_safe_result_title(data, url), "cache_hit": False}

    def discover(self, request, tasks, progress=None) -> DiscoveryResult:
        date = request.report_date.isoformat() if request.report_date else TASK.get()[2]
        raw_sources = []
        seen_queries = set()
        runs = []
        for task in tasks:
            queries = []
            for group in task.get("queries") or []:
                queries.extend(group.get("items", []) if isinstance(group, dict) else [str(group)])
            run = {"label": task.get("label"), "query_count": 0, "cache_hit_count": 0, "error_count": 0, "title_code_isolated_count": 0}
            for query in queries:
                if query in seen_queries:
                    continue
                if self.max_queries_per_task and run["query_count"] >= self.max_queries_per_task:
                    break
                seen_queries.add(query)
                if progress:
                    progress(f"DeepSeek 搜尋：{task.get('label', '')}｜查詢 {run['query_count'] + 1}")
                try:
                    result = self.search(query, report_date=date)
                    run["query_count"] += 1
                    run["cache_hit_count"] += bool(result.get("cache_hit"))
                    for row in result.get("results") or []:
                        isolated = row.get("title_normalization") == "code_isolated"
                        run["title_code_isolated_count"] += int(isolated)
                        raw_sources.append({"title": row.get("title"), "url": row.get("url"),
                            "snippet": row.get("content") or row.get("summary") or row.get("snippet"),
                            "published_date": row.get("date"), "provider": self.provider,
                            "found_by": [query, *(["source_title:code_isolated"] if isolated else [])],
                            "provider_detail": task.get("label", "") + ("；標題程式碼已隔離，原始回應保留" if isolated else "")})
                except DeepSeekUnavailable:
                    raise
                except Exception:
                    run["error_count"] += 1
            runs.append(run)
        return DiscoveryResult(make_source_items(raw_sources), {"enabled": True, "provider": self.provider,
            "query_count": sum(r["query_count"] for r in runs), "source_count": len(raw_sources), "runs": runs})
