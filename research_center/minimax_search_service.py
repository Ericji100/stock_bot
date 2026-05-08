from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlparse

import httpx

from .minimax_service import MiniMaxService
from .models import CommandRequest, SourceItem
from .source_rank import make_source_items

ProgressCallback = Callable[[str], None]

SERPER_SEARCH_URL = "https://google.serper.dev/search"
JINA_READER_PREFIX = "https://r.jina.ai/"


@dataclass(frozen=True)
class MiniMaxSearchResult:
    sources: list[SourceItem]
    diagnostics: dict[str, Any]


class MiniMaxSearchService:
    def __init__(
        self,
        serper_api_key: str | None,
        jina_api_key: str | None,
        minimax: MiniMaxService | None = None,
        timeout_seconds: float = 25.0,
        max_results_per_query: int = 5,
        max_browse_urls_per_task: int = 5,
    ):
        self.serper_api_key = serper_api_key
        self.jina_api_key = jina_api_key
        self.minimax = minimax
        self.timeout_seconds = timeout_seconds
        self.max_results_per_query = max_results_per_query
        self.max_browse_urls_per_task = max_browse_urls_per_task

    def is_configured(self) -> bool:
        return bool(self.serper_api_key)

    def discover(
        self,
        request: CommandRequest,
        discovery_tasks: list[dict[str, Any]],
        progress: ProgressCallback | None = None,
    ) -> MiniMaxSearchResult:
        if not self.serper_api_key:
            return MiniMaxSearchResult([], {"enabled": False, "reason": "SERPER_API_KEY not configured", "runs": []})

        all_sources: list[SourceItem] = []
        runs: list[dict[str, Any]] = []
        for task_index, task in enumerate(discovery_tasks, 1):
            label = str(task.get("label") or f"task_{task_index}")
            queries = [str(query) for query in (task.get("queries") or []) if str(query).strip()]
            if not queries:
                runs.append({"label": label, "status": "skipped", "reason": "no queries"})
                continue
            _emit(progress, f"MiniMax Search {task_index}/{len(discovery_tasks)} [{label}] start: {len(queries)} Google queries")
            try:
                search_results = self._search_many(queries)
                browse_blocks = self._browse_top_results(search_results, progress=progress)
                summary = self._summarize(label, task, browse_blocks)
                raw_sources = self._sources_from_results(search_results, browse_blocks, summary)
                task_sources = make_source_items(raw_sources)
                before = len(all_sources)
                all_sources = _merge_sources(all_sources, task_sources)
                added = len(all_sources) - before
                runs.append(
                    {
                        "label": label,
                        "status": "ok",
                        "query_count": len(queries),
                        "search_result_count": len(search_results),
                        "browsed_url_count": len(browse_blocks),
                        "source_count": len(task_sources),
                        "added_source_count": added,
                        "summary": summary,
                    }
                )
                _emit(progress, f"MiniMax Search {task_index}/{len(discovery_tasks)} [{label}] completed: results={len(search_results)}, browsed={len(browse_blocks)}, added={added}")
            except Exception as exc:
                runs.append({"label": label, "status": "failed", "error": str(exc)})
                _emit(progress, f"MiniMax Search {task_index}/{len(discovery_tasks)} [{label}] failed: {exc}")

        return MiniMaxSearchResult(
            sources=all_sources,
            diagnostics={
                "enabled": True,
                "task_count": len(discovery_tasks),
                "source_count": len(all_sources),
                "runs": runs,
                "policy": "Serper Google Search + Jina Reader + optional MiniMax-M2.7 summarization. If Jina fails, Serper snippets are retained as lower-depth evidence.",
            },
        )

    def _search_many(self, queries: list[str]) -> list[dict[str, str]]:
        results: list[dict[str, str]] = []
        seen: set[str] = set()
        for query in queries:
            for item in self._serper_search(query):
                url = item.get("url") or ""
                if not url or url in seen:
                    continue
                seen.add(url)
                item["query"] = query
                results.append(item)
        return results

    def _serper_search(self, query: str) -> list[dict[str, str]]:
        headers = {"X-API-KEY": str(self.serper_api_key), "Content-Type": "application/json"}
        payload = {"q": query, "num": self.max_results_per_query, "gl": "tw", "hl": "zh-tw"}
        with httpx.Client(timeout=self.timeout_seconds, follow_redirects=True) as client:
            response = client.post(SERPER_SEARCH_URL, headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
        raw_items = data.get("organic") or []
        output: list[dict[str, str]] = []
        for raw in raw_items[: self.max_results_per_query]:
            url = str(raw.get("link") or "").strip()
            if not url:
                continue
            output.append(
                {
                    "title": str(raw.get("title") or url),
                    "url": url,
                    "snippet": str(raw.get("snippet") or "Serper Google Search result"),
                    "published_date": str(raw.get("date") or "") or None,
                }
            )
        return output

    def _browse_top_results(self, search_results: list[dict[str, str]], progress: ProgressCallback | None = None) -> list[dict[str, str]]:
        blocks: list[dict[str, str]] = []
        for item in search_results[: self.max_browse_urls_per_task]:
            url = item.get("url") or ""
            if not url:
                continue
            try:
                content = self._read_with_jina(url)
                blocks.append({**item, "content": content[:6000], "content_status": "jina_ok"})
            except Exception as exc:
                _emit(progress, f"MiniMax Search Jina browse failed, keeping Serper snippet: {url} ({exc})")
                blocks.append({**item, "content": item.get("snippet") or "", "content_status": f"jina_failed: {exc}"})
        return blocks

    def _read_with_jina(self, url: str) -> str:
        headers = {"Accept": "text/plain"}
        if self.jina_api_key:
            headers["Authorization"] = f"Bearer {self.jina_api_key}"
        reader_url = JINA_READER_PREFIX + url
        with httpx.Client(timeout=self.timeout_seconds, follow_redirects=True) as client:
            response = client.get(reader_url, headers=headers)
            response.raise_for_status()
            text = response.text.strip()
        if not text:
            raise RuntimeError("empty Jina response")
        return text

    def _summarize(self, label: str, task: dict[str, Any], blocks: list[dict[str, str]]) -> str:
        if not blocks:
            return ""
        if self.minimax and self.minimax.is_configured():
            objective = str(task.get("objective") or label)
            return self.minimax.summarize_search_content(objective, blocks).markdown
        return "\n".join(f"- {block.get('title')}: {block.get('snippet')}" for block in blocks[:5])

    def _sources_from_results(self, search_results: list[dict[str, str]], browse_blocks: list[dict[str, str]], summary: str) -> list[dict[str, str]]:
        content_by_url = {block.get("url"): block for block in browse_blocks}
        raw_sources: list[dict[str, str]] = []
        for item in search_results:
            url = item.get("url") or ""
            block = content_by_url.get(url) or {}
            status = block.get("content_status") or "serper_only"
            snippet = item.get("snippet") or ""
            if status == "jina_ok":
                snippet = f"MiniMax Search / Serper + Jina: {snippet}"
            else:
                snippet = f"MiniMax Search / Serper snippet only ({status}): {snippet}"
            raw_sources.append(
                {
                    "title": item.get("title") or _domain_title(url),
                    "url": url,
                    "published_date": item.get("published_date"),
                    "snippet": snippet[:500],
                }
            )
        if summary:
            raw_sources.append(
                {
                    "title": "MiniMax-M2.7 Search Browse Summary",
                    "url": f"minimax-search://summary/{abs(hash(summary))}",
                    "snippet": summary[:500],
                }
            )
        return raw_sources


def _merge_sources(base: list[SourceItem], extra: list[SourceItem]) -> list[SourceItem]:
    merged: list[SourceItem] = []
    seen: set[str] = set()
    for item in [*base, *extra]:
        if item.url in seen:
            continue
        seen.add(item.url)
        merged.append(SourceItem(f"S{len(merged) + 1:03d}", item.title, item.url, item.source_level, item.published_date, item.snippet, item.used_in_section))
    return merged


def _domain_title(url: str) -> str:
    host = urlparse(url).netloc
    return host or url or "MiniMax Search source"


def _emit(progress: ProgressCallback | None, message: str) -> None:
    if progress is not None:
        progress(message)
