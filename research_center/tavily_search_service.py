from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import httpx

from .models import CommandRequest, SourceItem
from .source_rank import make_source_items

ProgressCallback = Callable[[str], None] | None

TAVILY_SEARCH_URL = "https://api.tavily.com/search"
TAVILY_EXTRACT_URL = "https://api.tavily.com/extract"


class TavilyQuotaError(RuntimeError):
    pass


@dataclass(frozen=True)
class TavilySearchResult:
    sources: list[SourceItem]
    diagnostics: dict[str, Any]


class TavilySearchService:
    def __init__(
        self,
        api_key: str | None,
        enable_search: bool = True,
        enable_extract: bool = True,
        search_depth: str = "basic",
        extract_depth: str = "basic",
        timeout_seconds: float = 30.0,
        max_results_per_query: int = 5,
        max_extract_urls_per_task: int = 5,
    ):
        self.api_key = api_key
        self.enable_search = enable_search
        self.enable_extract = enable_extract
        self.search_depth = search_depth
        self.extract_depth = extract_depth
        self.timeout_seconds = timeout_seconds
        self.max_results_per_query = max_results_per_query
        self.max_extract_urls_per_task = max_extract_urls_per_task

    def is_configured(self) -> bool:
        return bool(self.api_key) and self.enable_search

    def discover(
        self,
        request: CommandRequest,
        discovery_tasks: list[dict[str, Any]],
        progress: ProgressCallback = None,
    ) -> TavilySearchResult:
        if not self.is_configured():
            return TavilySearchResult([], {"enabled": False, "reason": "not_configured", "runs": []})

        all_sources: list[SourceItem] = []
        runs: list[dict[str, Any]] = []
        for task_index, task in enumerate(discovery_tasks, 1):
            label = str(task.get("label") or f"task_{task_index}")
            queries = [str(q).strip() for q in (task.get("queries") or []) if str(q).strip()]
            if not queries:
                runs.append({"label": label, "status": "skipped", "reason": "no_queries"})
                continue

            if progress:
                progress(f"Tavily Search {task_index}/{len(discovery_tasks)} [{label}] start: {len(queries)} queries")

            try:
                search_results = self._search_many(queries, label)
                raw_sources = self._sources_from_results(search_results, [], label)
                task_sources = make_source_items(raw_sources)
                before = len(all_sources)
                all_sources = _merge_tavily_sources(all_sources, task_sources)
                added = len(all_sources) - before
                runs.append({
                    "label": label,
                    "status": "ok",
                    "query_count": len(queries),
                    "search_result_count": len(search_results),
                    "extracted_url_count": 0,
                    "source_count": len(task_sources),
                    "added_source_count": added,
                })
                if progress:
                    progress(f"Tavily Search {task_index}/{len(discovery_tasks)} [{label}] completed: results={len(search_results)}, extracted=0, added={added}")
            except TavilyQuotaError as exc:
                runs.append({"label": label, "status": "quota_exhausted", "error": str(exc)})
                if progress:
                    progress(f"Tavily Search {task_index}/{len(discovery_tasks)} [{label}] quota exhausted: {exc}")
                raise
            except Exception as exc:
                runs.append({"label": label, "status": "failed", "error": str(exc)})
                if progress:
                    progress(f"Tavily Search {task_index}/{len(discovery_tasks)} [{label}] failed: {exc}")

        estimated_credits = sum(run.get("query_count", 0) for run in runs if run.get("status") == "ok")
        return TavilySearchResult(
            sources=all_sources,
            diagnostics={
                "enabled": True,
                "provider": "tavily",
                "task_count": len(discovery_tasks),
                "source_count": len(all_sources),
                "runs": runs,
                "search_depth": self.search_depth,
                "extract_depth": self.extract_depth,
                "estimated_credits": estimated_credits,
            },
        )

    def _search_many(self, queries: list[str], task_label: str) -> list[dict[str, str]]:
        results: list[dict[str, str]] = []
        seen: set[str] = set()
        for query in queries:
            for item in self._search(query):
                url = item.get("url") or ""
                if not url or url in seen:
                    continue
                seen.add(url)
                item["query"] = query
                item["task_label"] = task_label
                results.append(item)
        return results

    def _search(self, query: str) -> list[dict[str, str]]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload: dict[str, Any] = {
            "query": query,
            "search_depth": self.search_depth,
            "max_results": self.max_results_per_query,
            "include_answer": False,
            "include_raw_content": False,
            "topic": "general",
        }
        with httpx.Client(timeout=self.timeout_seconds, follow_redirects=True) as client:
            response = client.post(TAVILY_SEARCH_URL, headers=headers, json=payload)
            if response.status_code in {402, 429, 432, 433}:
                raise TavilyQuotaError(f"Tavily quota exceeded (HTTP {response.status_code}): {response.text[:200]}")
            response.raise_for_status()
            data = response.json()
            if isinstance(data, dict) and _looks_like_quota_error(str(data.get("error") or data.get("message") or "")):
                raise TavilyQuotaError(f"Tavily quota exceeded: {data.get('error') or data.get('message') or 'unknown'}")
        raw_items = data.get("results") or []
        output: list[dict[str, str]] = []
        for raw in raw_items[: self.max_results_per_query]:
            url = str(raw.get("url") or "").strip()
            if not url:
                continue
            output.append({
                "title": str(raw.get("title") or url),
                "url": url,
                "snippet": str(raw.get("content") or raw.get("snippet") or "Tavily search result"),
                "published_date": str(raw.get("published_date") or "") or None,
                "provider": "tavily_search",
                "provider_detail": f"query={query[:60]}; search_depth={self.search_depth}",
            })
        return output

    def _extract_top_results(self, search_results: list[dict[str, str]], task_label: str, progress: ProgressCallback = None) -> list[dict[str, str]]:
        urls = [item.get("url") or "" for item in search_results[: self.max_extract_urls_per_task]]
        urls = [u for u in urls if u]
        if not urls:
            return []
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload: dict[str, Any] = {
            "urls": urls,
            "extract_depth": self.extract_depth,
            "format": "markdown",
        }
        try:
            with httpx.Client(timeout=self.timeout_seconds * 2, follow_redirects=True) as client:
                response = client.post(TAVILY_EXTRACT_URL, headers=headers, json=payload)
                if response.status_code in {402, 429, 432, 433}:
                    raise TavilyQuotaError(f"Tavily quota exceeded on extract (HTTP {response.status_code}): {response.text[:200]}")
                response.raise_for_status()
                data = response.json()
                if isinstance(data, dict) and _looks_like_quota_error(str(data.get("error") or data.get("message") or "")):
                    raise TavilyQuotaError(f"Tavily quota exceeded on extract: {data.get('error') or data.get('message') or 'unknown'}")
        except TavilyQuotaError:
            raise
        except Exception as exc:
            if progress:
                progress(f"Tavily Extract failed, keeping search snippets: {exc}")
            return []

        results = data.get("results") or []
        blocks: list[dict[str, str]] = []
        for item in results:
            raw_url = str(item.get("url") or "")
            raw_content = str(item.get("raw_content") or item.get("content") or item.get("markdown") or "")
            if raw_url and raw_content:
                blocks.append({
                    "url": raw_url,
                    "title": str(item.get("title") or raw_url),
                    "content": raw_content[:6000],
                    "content_status": "tavily_extract_ok",
                    "provider": "tavily_extract",
                    "provider_detail": f"extract_depth={self.extract_depth}; task={task_label}",
                })
        return blocks

    def _sources_from_results(self, search_results: list[dict[str, str]], extract_blocks: list[dict[str, str]], task_label: str) -> list[dict[str, str]]:
        raw_sources: list[dict[str, str]] = []
        extract_urls = {b.get("url") or "" for b in extract_blocks}

        for item in search_results:
            url = item.get("url") or ""
            if url in extract_urls:
                matching = [b for b in extract_blocks if b.get("url") == url]
                if matching:
                    b = matching[0]
                    raw_sources.append({
                        "title": str(b.get("title") or item.get("title") or url),
                        "url": url,
                        "snippet": str(b.get("content") or item.get("snippet") or "")[:2000],
                        "published_date": item.get("published_date"),
                        "provider": "tavily_extract",
                        "provider_detail": b.get("provider_detail", f"extract_depth={self.extract_depth}; task={task_label}"),
                    })
                    continue
            raw_sources.append({
                "title": str(item.get("title") or url),
                "url": url,
                "snippet": str(item.get("snippet") or "Tavily search result")[:2000],
                "published_date": item.get("published_date"),
                "provider": item.get("provider", "tavily_search"),
                "provider_detail": item.get("provider_detail", f"query={item.get('query', '')[:60]}; search_depth={self.search_depth}"),
            })
        return raw_sources


def _merge_tavily_sources(base: list[SourceItem], extra: list[SourceItem]) -> list[SourceItem]:
    merged: list[SourceItem] = list(base)
    seen: set[str] = {item.url for item in base}
    for item in extra:
        if item.url in seen:
            continue
        seen.add(item.url)
        merged.append(item)
    return merged


def _looks_like_quota_error(text: str) -> bool:
    lower = text.lower()
    indicators = ["quota", "credit", "limit", "insufficient", "exceeded", "upgrade", "payment"]
    return any(indicator in lower for indicator in indicators)
