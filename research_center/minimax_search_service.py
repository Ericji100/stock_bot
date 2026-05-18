"""
MiniMax Token Plan MCP web_search service.
Replaces the old Serper/Jina search with MiniMax MCP subprocess.

Search order: MiniMax MCP web_search -> Tavily Search -> Gemini Search fallback
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from .config import load_research_config
from .models import CommandRequest, SourceItem
from .source_rank import make_source_items

ProgressCallback = Callable[[str], None] | None

DEFAULT_TIMEOUT = 60.0
MCP_STARTUP_TIMEOUT = 15.0
MCP_TOOL_NAME = "web_search"
MCP_PACKAGE = "minimax-coding-plan-mcp"


@dataclass(frozen=True)
class MiniMaxSearchResult:
    sources: list[SourceItem]
    diagnostics: dict[str, Any]


def _emit(progress: ProgressCallback, message: str) -> None:
    if progress is not None:
        progress(message)


def _find_command(name: str) -> str | None:
    """Find executable path using env override or shutil.which."""
    override = os.environ.get(f"{name.upper().replace('-', '_')}_EXE_PATH")
    if override and Path(override).exists():
        return override
    return shutil.which(name)


def _get_api_key() -> str:
    """Get MiniMax API key from environment or config/secrets.json."""
    key = os.environ.get("MINIMAX_API_KEY")
    if key:
        return key
    secrets_path = Path(__file__).parent.parent / "config" / "secrets.json"
    if secrets_path.exists():
        try:
            data = json.loads(secrets_path.read_text(encoding="utf-8-sig"))
            return data.get("minimax_api_key", "")
        except Exception:
            pass
    return ""


async def _call_web_search_async(
    command: str,
    args: list[str],
    env: dict,
    query: str,
    timeout_seconds: float,
) -> dict:
    """Start MCP server and call web_search, returning raw result or error dict."""
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    server_params = StdioServerParameters(
        command=command,
        args=args,
        env=env,
    )
    try:
        async with stdio_client(server_params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()

                tools_result = await session.list_tools()
                tool_names = [t.name for t in tools_result.tools]
                if MCP_TOOL_NAME not in tool_names:
                    return {"ok": False, "error": f"{MCP_TOOL_NAME} not found in tools: {tool_names}"}

                result = await asyncio.wait_for(
                    session.call_tool(MCP_TOOL_NAME, arguments={"query": query}),
                    timeout=timeout_seconds,
                )

                # Parse CallToolResult content
                raw_content: object = None
                if hasattr(result, "content") and result.content:
                    first = result.content[0]
                    if hasattr(first, "text"):
                        try:
                            raw_content = json.loads(first.text)
                        except Exception:
                            raw_content = first.text
                elif hasattr(result, "content") and isinstance(result.content, str):
                    raw_content = result.content

                return {"ok": True, "raw": raw_content}
    except asyncio.TimeoutError:
        return {"ok": False, "error": f"web_search timed out after {timeout_seconds}s"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _run_async_safely(coro) -> Any:
    """Run coroutine, handling running event loop (ThreadPoolExecutor fallback)."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop, safe to use asyncio.run
        return asyncio.run(coro)
    else:
        # Running loop exists, dispatch to thread pool
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(asyncio.run, coro)
            return future.result(timeout=120)


def _build_mcp_startup_params(api_key: str) -> tuple[str, list[str], dict]:
    """Build MCP server startup command/args/env. Returns (command, args, env)."""
    base_env = dict(os.environ)
    base_env["MINIMAX_API_KEY"] = api_key
    base_env["MINIMAX_API_HOST"] = "https://api.minimax.io"

    python_exe = sys.executable
    uvx_path = _find_command("uvx")
    uv_path = _find_command("uv")

    if uvx_path:
        cmd = uvx_path
        args_list = [MCP_PACKAGE]
    elif uv_path:
        cmd = uv_path
        args_list = ["tool", "run", "--from", MCP_PACKAGE, MCP_PACKAGE]
    else:
        cmd = python_exe
        args_list = ["-m", "uv", "tool", "run", "--from", MCP_PACKAGE, MCP_PACKAGE]

    return cmd, args_list, base_env


class MiniMaxSearchService:
    """
    MiniMax Token Plan MCP web_search service.

    Uses MiniMax MCP subprocess to perform web searches.
    Falls back gracefully on any MCP failure.
    """

    def __init__(
        self,
        serper_api_key: str | None = None,
        jina_api_key: str | None = None,
        minimax: Any = None,
        timeout_seconds: float = DEFAULT_TIMEOUT,
        max_results_per_query: int = 10,
        max_browse_urls_per_task: int = 5,
        mcp_startup_timeout: float = MCP_STARTUP_TIMEOUT,
    ):
        self.serper_api_key = serper_api_key
        self.jina_api_key = jina_api_key
        self.minimax = minimax
        self.timeout_seconds = timeout_seconds
        self.max_results_per_query = max_results_per_query
        self.max_browse_urls_per_task = max_browse_urls_per_task
        self.mcp_startup_timeout = mcp_startup_timeout
        self._config = load_research_config()

    def is_configured(self) -> bool:
        return bool(self._config.minimax_api_key) and self._config.enable_minimax_search

    def discover(
        self,
        request: CommandRequest,
        discovery_tasks: list[dict[str, Any]],
        progress: ProgressCallback = None,
    ) -> MiniMaxSearchResult:
        if not self.is_configured():
            return MiniMaxSearchResult([], {"enabled": False, "reason": "not_configured", "runs": []})

        api_key = _get_api_key()
        if not api_key:
            return MiniMaxSearchResult([], {"enabled": True, "reason": "no_api_key", "runs": []})

        all_sources: list[SourceItem] = []
        runs: list[dict[str, Any]] = []

        for task_index, task in enumerate(discovery_tasks, 1):
            label = str(task.get("label") or f"task_{task_index}")
            queries = [str(q) for q in (task.get("queries") or []) if str(q).strip()]
            if not queries:
                runs.append({"label": label, "status": "skipped", "reason": "no queries"})
                continue

            _emit(progress, f"MiniMax MCP Search {task_index}/{len(discovery_tasks)} [{label}] start: {len(queries)} queries")
            try:
                search_results = self._search_many(queries, api_key)
                raw_sources = self._sources_from_search_results(search_results, label)
                task_sources = make_source_items(raw_sources)
                before = len(all_sources)
                all_sources = _merge_minimax_sources(all_sources, task_sources)
                added = len(all_sources) - before
                runs.append({
                    "label": label,
                    "status": "ok",
                    "query_count": len(queries),
                    "search_result_count": len(search_results),
                    "source_count": len(task_sources),
                    "added_source_count": added,
                })
                _emit(progress, f"MiniMax MCP Search {task_index}/{len(discovery_tasks)} [{label}] completed: results={len(search_results)}, added={added}")
            except Exception as exc:
                runs.append({"label": label, "status": "failed", "error": str(exc)})
                _emit(progress, f"MiniMax MCP Search {task_index}/{len(discovery_tasks)} [{label}] failed: {exc}")

        return MiniMaxSearchResult(
            sources=all_sources,
            diagnostics={
                "enabled": True,
                "provider": "minimax_mcp_search",
                "task_count": len(discovery_tasks),
                "source_count": len(all_sources),
                "runs": runs,
                "policy": "MiniMax Token Plan MCP web_search. Falls back to Tavily on failure.",
            },
        )

    def _search_many(self, queries: list[str], api_key: str) -> list[dict[str, str]]:
        """Run web_search for each query via MCP session."""
        results: list[dict[str, str]] = []
        seen: set[str] = set()
        cmd, args_list, base_env = _build_mcp_startup_params(api_key)
        for query in queries:
            try:
                resp = _run_async_safely(_call_web_search_async(cmd, args_list, base_env, query, self.timeout_seconds))
                if not resp.get("ok"):
                    _emit(None, f"MiniMax MCP query failed '{query}': {resp.get('error', 'unknown')}")
                    continue
                raw_result = resp.get("raw")
                items = _extract_search_items(raw_result)
                for item in items:
                    url = item.get("url") or ""
                    if not url or url in seen:
                        continue
                    seen.add(url)
                    item["query"] = query
                    results.append(item)
            except Exception as exc:
                _emit(None, f"MiniMax MCP query failed '{query}': {exc}")
                continue
        return results

    def _sources_from_search_results(self, search_results: list[dict[str, str]], task_label: str) -> list[dict[str, str]]:
        """Convert raw search results to raw source dicts."""
        raw_sources: list[dict[str, str]] = []
        for item in search_results:
            url = item.get("url") or ""
            if not url:
                continue
            raw_sources.append({
                "title": item.get("title") or _domain_title(url),
                "url": url,
                "published_date": item.get("published_date"),
                "snippet": item.get("snippet") or f"MiniMax MCP Search: {item.get('query', '')}",
                "provider": "minimax_mcp_search",
                "provider_detail": f"query={item.get('query', '')[:60]}; task={task_label}",
            })
        return raw_sources


def _extract_search_items(result: Any) -> list[dict[str, str]]:
    """Extract search result items from MCP call_tool result."""
    try:
        content = result.content if hasattr(result, "content") else result
        if isinstance(content, list) and content:
            first = content[0]
            if hasattr(first, "text"):
                data = json.loads(first.text)
            elif hasattr(first, "data"):
                data = json.loads(first.data)
            else:
                return []
        elif isinstance(content, str):
            data = json.loads(content)
        else:
            data = content if isinstance(content, dict) else {}

        for key in ("results", "items", "organic", "sources", "web_results", "search_results"):
            items = data.get(key)
            if isinstance(items, list):
                return [_normalize_search_item(i) for i in items]
            if "url" in data:
                return [_normalize_search_item(data)]
        if isinstance(data, list):
            return [_normalize_search_item(i) for i in data if isinstance(i, dict)]
        return []
    except Exception:
        return []


def _normalize_search_item(item: dict[str, Any]) -> dict[str, str]:
    """Normalize a search item from various formats."""
    return {
        "title": str(item.get("title") or item.get("name") or ""),
        "url": str(item.get("url") or item.get("link") or ""),
        "snippet": str(item.get("snippet") or item.get("description") or item.get("summary") or ""),
        "published_date": str(item.get("published_date") or item.get("date") or ""),
    }


def _domain_title(url: str) -> str:
    host = urlparse(url).netloc
    return host or url or "MiniMax Search source"


def _merge_minimax_sources(base: list[SourceItem], extra: list[SourceItem]) -> list[SourceItem]:
    """Merge MiniMax sources, tracking found_by."""
    merged_dict: dict[str, SourceItem] = {}
    for item in base:
        merged_dict[item.url] = item
    for item in extra:
        existing = merged_dict.get(item.url)
        if existing is None:
            merged_dict[item.url] = item
        else:
            existing_found_by = list(existing.found_by) if existing.found_by else []
            item_found_by = list(item.found_by) if item.found_by else []
            combined_found_by = list(set(existing_found_by + item_found_by))
            merged_dict[item.url] = SourceItem(
                source_id=existing.source_id,
                title=existing.title,
                url=existing.url,
                source_level=existing.source_level,
                published_date=existing.published_date or item.published_date,
                snippet=existing.snippet or item.snippet,
                used_in_section=list(set(existing.used_in_section + item.used_in_section)),
                provider=existing.provider,
                provider_detail=existing.provider_detail,
                fetch_provider=item.fetch_provider or existing.fetch_provider,
                fetch_status=item.fetch_status or existing.fetch_status,
                failure_reason=item.failure_reason or existing.failure_reason,
                found_by=combined_found_by,
            )
    return list(merged_dict.values())
