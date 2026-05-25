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


def _run_async_safely(coro, future_timeout: float = 120) -> Any:
    """Run coroutine, handling running event loop (ThreadPoolExecutor fallback).

    Args:
        coro: coroutine to run
        future_timeout: max seconds to wait for thread future (default 120).
            Should be >= timeout_seconds + 10 to allow graceful timeout.
    """
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
            return future.result(timeout=future_timeout)


def _build_mcp_startup_params(api_key: str) -> tuple[str, list[str], dict]:
    """Build MCP server startup command/args/env. Returns (command, args, env).

    Startup order:
    1. MINIMAX_MCP_COMMAND env override (user pre-installed tool)
    2. .venv/Scripts/uvx.exe (preferred)
    3. .venv/Scripts/uv.exe tool run --from
    4. python -m uv tool run --from (system python fallback)

    UV_CACHE_DIR and UV_TOOL_DIR are set to TEMP-based dirs to avoid WinError 5
    on the default LocalAppData/Roaming dirs.
    """
    # Resolve UV cache/tool dirs - prefer TEMP-based writable dirs
    temp_dir = os.environ.get("TEMP", ".")
    default_uv_cache = str(Path(temp_dir) / "uv_cache_stock_ai_bot")
    default_uv_tool = str(Path(temp_dir) / "uv_tools_stock_ai_bot")
    uv_cache_dir = os.environ.get("UV_CACHE_DIR") or default_uv_cache
    uv_tool_dir = os.environ.get("UV_TOOL_DIR") or default_uv_tool

    # Ensure writable directories exist
    Path(uv_cache_dir).mkdir(parents=True, exist_ok=True)
    Path(uv_tool_dir).mkdir(parents=True, exist_ok=True)

    base_env = dict(os.environ)
    base_env["MINIMAX_API_KEY"] = api_key
    base_env["MINIMAX_API_HOST"] = "https://api.minimax.io"
    base_env["UV_CACHE_DIR"] = uv_cache_dir
    base_env["UV_TOOL_DIR"] = uv_tool_dir

    # Check for user-provided full command first
    env_cmd = os.environ.get("MINIMAX_MCP_COMMAND")
    if env_cmd:
        env_args_str = os.environ.get("MINIMAX_MCP_ARGS", "")
        args_list = env_args_str.split() if env_args_str else []
        return env_cmd, args_list, base_env

    # Prefer .venv uvx/uv over system python
    venv_uvx = str(Path(__file__).parent.parent / ".venv" / "Scripts" / "uvx.exe")
    venv_uv = str(Path(__file__).parent.parent / ".venv" / "Scripts" / "uv.exe")

    python_exe = sys.executable

    if Path(venv_uvx).exists():
        cmd = venv_uvx
        args_list = [MCP_PACKAGE]
    elif Path(venv_uv).exists():
        cmd = venv_uv
        args_list = ["tool", "run", "--from", MCP_PACKAGE, MCP_PACKAGE]
    else:
        cmd = python_exe
        args_list = ["-m", "uv", "tool", "run", "--from", MCP_PACKAGE, MCP_PACKAGE]

    return cmd, args_list, base_env


def _flatten_task_queries(queries: list) -> list[str]:
    """Flatten task queries into a flat list of query strings.

    Handles the discovery task format where each query item can be:
    - a dict with "items": ["query1", "query2", ...]
    - a plain string query
    """
    flat: list[str] = []
    for item in queries or []:
        if isinstance(item, dict):
            flat.extend(str(q) for q in item.get("items", []) if str(q).strip())
        elif str(item).strip():
            flat.append(str(item))
    return flat


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
        max_queries_per_task: int = 0,
    ):
        self.serper_api_key = serper_api_key
        self.jina_api_key = jina_api_key
        self.minimax = minimax
        self.timeout_seconds = timeout_seconds
        self.max_results_per_query = max_results_per_query
        self.max_browse_urls_per_task = max_browse_urls_per_task
        self.mcp_startup_timeout = mcp_startup_timeout
        self.max_queries_per_task = max_queries_per_task  # 0 = no limit
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
        raw_response_samples: list[dict[str, Any]] = []
        max_samples = 3

        # Build once to include command info in diagnostics
        cmd, args_list, base_env = _build_mcp_startup_params(api_key)
        uv_cache_dir = base_env.get("UV_CACHE_DIR", "unknown")
        uv_tool_dir = base_env.get("UV_TOOL_DIR", "unknown")
        mcp_command = cmd
        mcp_args = args_list

        for task_index, task in enumerate(discovery_tasks, 1):
            label = str(task.get("label") or f"task_{task_index}")
            raw_queries = task.get("queries") or []
            queries = _flatten_task_queries(raw_queries)
            if not queries:
                runs.append({"label": label, "status": "skipped", "reason": "no queries"})
                continue

            # Apply per-task query limit for smoke tests / resource control
            if self.max_queries_per_task > 0:
                queries = queries[:self.max_queries_per_task]

            _emit(progress, f"MiniMax MCP Search {task_index}/{len(discovery_tasks)} [{label}] start: {len(queries)} queries")
            try:
                search_results, errors = self._search_many(queries, api_key, raw_response_samples, max_samples)
                raw_sources = self._sources_from_search_results(search_results, label)
                task_sources = make_source_items(raw_sources)
                before = len(all_sources)
                all_sources = _merge_minimax_sources(all_sources, task_sources)
                added = len(all_sources) - before

                # Classify any errors
                error_reasons = _classify_errors(errors)
                error_count = len(errors)
                error_samples = [
                    {"error": e[:200] if len(e) > 200 else e}
                    for e in errors[:3]
                ]
                # Detect "success but no results" case (not an error, but no data)
                empty_results = len(search_results) == 0 and error_count == 0
                run_entry: dict[str, Any] = {
                    "label": label,
                    "status": "ok" if not error_reasons else "partial",
                    "query_count": len(queries),
                    "search_result_count": len(search_results),
                    "source_count": len(task_sources),
                    "added_source_count": added,
                    "mcp_command": mcp_command,
                    "mcp_args": mcp_args,
                    "uv_cache_dir": uv_cache_dir,
                    "uv_tool_dir": uv_tool_dir,
                    "error_count": error_count,
                }
                if error_reasons:
                    run_entry["error_reasons"] = error_reasons
                if error_samples:
                    run_entry["error_samples"] = error_samples
                if empty_results:
                    run_entry["mcp_empty_results"] = True
                runs.append(run_entry)
                _emit(progress, f"MiniMax MCP Search {task_index}/{len(discovery_tasks)} [{label}] completed: results={len(search_results)}, added={added}")
            except Exception as exc:
                error_reasons = _classify_errors([str(exc)])
                runs.append({
                    "label": label,
                    "status": "failed",
                    "error": str(exc),
                    "error_reasons": error_reasons,
                    "mcp_command": mcp_command,
                    "mcp_args": mcp_args,
                    "uv_cache_dir": uv_cache_dir,
                    "uv_tool_dir": uv_tool_dir,
                })
                _emit(progress, f"MiniMax MCP Search {task_index}/{len(discovery_tasks)} [{label}] failed: {exc}")

        # Collect all unique error reasons and samples for the outer diagnostics
        all_reasons = set()
        all_samples = []
        for run in runs:
            if "error_reasons" in run:
                all_reasons.update(run["error_reasons"])
            if "error_samples" in run:
                all_samples.extend(run["error_samples"])
            if run.get("mcp_empty_results"):
                all_reasons.add("mcp_empty_results")

        return MiniMaxSearchResult(
            sources=all_sources,
            diagnostics={
                "enabled": True,
                "provider": "minimax_mcp_search",
                "task_count": len(discovery_tasks),
                "source_count": len(all_sources),
                "runs": runs,
                "raw_response_samples": raw_response_samples[:max_samples],
                "mcp_command": mcp_command,
                "mcp_args": mcp_args,
                "uv_cache_dir": uv_cache_dir,
                "uv_tool_dir": uv_tool_dir,
                "policy": "MiniMax Token Plan MCP web_search. Falls back to Tavily on failure.",
                "error_reasons": sorted(list(all_reasons)),
                "error_samples": all_samples[:3],
            },
        )

    def health_check(self, run_smoke: bool = False) -> dict:
        """Run diagnostic health check on MiniMax MCP service. Does not raise."""
        api_key = _get_api_key()
        api_key_present = bool(api_key)
        
        # Get config values
        enabled = self._config.enable_minimax_search
        configured = self.is_configured() and api_key_present
        
        # Build startup params to find commands and dirs
        try:
            cmd, args_list, env = _build_mcp_startup_params(api_key)
            mcp_command = cmd
            uv_cache_dir = env.get("UV_CACHE_DIR", "")
            uv_tool_dir = env.get("UV_TOOL_DIR", "")
        except Exception:
            mcp_command = ""
            uv_cache_dir = ""
            uv_tool_dir = ""
            
        mcp_command_exists = False
        if mcp_command:
            # Check if it exists as absolute path or via PATH search
            if Path(mcp_command).exists():
                mcp_command_exists = True
            elif shutil.which(mcp_command):
                mcp_command_exists = True
                
        status = "ok"
        source_count = 0
        error_reasons = []
        error_samples = []
        
        if not enabled:
            status = "failed"
            error_reasons.append("disabled_by_config")
        elif not configured:
            status = "failed"
            if not api_key_present:
                error_reasons.append("minimax_api_key_missing")
            else:
                error_reasons.append("not_configured")
        elif not mcp_command_exists:
            status = "failed"
            error_reasons.append("mcp_package_not_installed")
            
        if run_smoke and status == "ok":
            try:
                # Run search using smoke query
                results, errors = self._search_many(["台積電 法說會"], api_key, [])
                source_count = len(results)
                if errors:
                    error_reasons.extend(_classify_errors(errors))
                    error_samples.extend({"error": e} for e in errors[:3])
                    status = "failed"
                elif source_count == 0:
                    status = "failed"
                    error_reasons.append("mcp_empty_response")
            except Exception as e:
                status = "failed"
                err_str = str(e)
                error_reasons.extend(_classify_errors([err_str]))
                error_samples.append({"error": err_str})
                
        return {
            "enabled": enabled,
            "configured": configured,
            "api_key_present": api_key_present,
            "mcp_command": mcp_command,
            "mcp_command_exists": mcp_command_exists,
            "uv_cache_dir": uv_cache_dir,
            "uv_tool_dir": uv_tool_dir,
            "smoke_test_enabled": run_smoke,
            "source_count": source_count,
            "status": status,
            "error_reasons": sorted(list(set(error_reasons))),
            "error_samples": error_samples,
        }

    def _search_many(
        self,
        queries: list[str],
        api_key: str,
        raw_response_samples: list[dict[str, Any]],
        max_samples: int = 3,
    ) -> tuple[list[dict[str, str]], list[str]]:
        """Run web_search for each query via MCP session. Returns (results, errors)."""
        results: list[dict[str, str]] = []
        seen: set[str] = set()
        errors: list[str] = []
        cmd, args_list, base_env = _build_mcp_startup_params(api_key)
        # future_timeout must be >= asyncio timeout + buffer so wait_for can fire
        future_timeout = max(self.timeout_seconds + 10, 30)
        for query in queries:
            try:
                resp = _run_async_safely(_call_web_search_async(cmd, args_list, base_env, query, self.timeout_seconds), future_timeout)
                if not resp.get("ok"):
                    err = resp.get("error", "unknown")
                    errors.append(err)
                    if len(raw_response_samples) < max_samples:
                        raw_response_samples.append({
                            "query": query[:120],
                            "status": "error",
                            "error": err[:200],
                            "raw_type": type(resp.get("raw")).__name__ if resp.get("raw") is not None else "None",
                            "raw_keys": list(resp.get("raw", {}).keys()) if isinstance(resp.get("raw"), dict) else [],
                            "item_count": 0,
                            "preview": "",
                        })
                    _emit(None, f"MiniMax MCP query failed '{query}': {err}")
                    continue
                raw_result = resp.get("raw")
                # Collect raw response sample for diagnostics (first few queries only)
                if len(raw_response_samples) < max_samples and raw_result is not None:
                    raw_type = type(raw_result).__name__
                    raw_str = str(raw_result)
                    raw_keys = list(raw_result.keys()) if isinstance(raw_result, dict) else []
                    raw_preview = raw_str[:500] if len(raw_str) > 500 else raw_str
                    raw_response_samples.append({
                        "query": query[:120],
                        "status": "success",
                        "raw_type": raw_type,
                        "raw_keys": raw_keys,
                        "item_count": 0,
                        "preview": raw_preview,
                    })
                items = _extract_search_items(raw_result)
                # Update sample item count
                if raw_response_samples and raw_response_samples[-1]["query"] == query[:120]:
                    raw_response_samples[-1]["item_count"] = len(items)
                for item in items:
                    url = item.get("url") or ""
                    if not url or url in seen:
                        continue
                    seen.add(url)
                    item["query"] = query
                    results.append(item)
            except _McpParseError as exc:
                # Parse error - response received but couldn't extract items
                errors.append(str(exc))
                if len(raw_response_samples) < max_samples:
                    raw_response_samples.append({
                        "query": query[:120],
                        "status": "parse_error",
                        "error": str(exc)[:200],
                        "raw_type": type(raw_result).__name__ if raw_result is not None else "None",
                        "raw_keys": list(raw_result.keys()) if isinstance(raw_result, dict) else [],
                        "item_count": 0,
                        "preview": str(raw_result)[:500] if raw_result else "",
                    })
                _emit(None, f"MiniMax MCP parse error '{query}': {exc}")
                continue
            except Exception as exc:
                errors.append(str(exc))
                _emit(None, f"MiniMax MCP query failed '{query}': {exc}")
                continue
        return results, errors

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


def _classify_errors(errors: list[str]) -> list[str]:
    """Classify error strings into human-readable reason codes."""
    if not errors:
        return []
    reasons: set[str] = set()
    for err in errors:
        err_lower = err.lower()
        if "mcp_parseerror" in err_lower or "failed to extract search items" in err_lower or "no recognized item keys" in err_lower or "unexpected content list" in err_lower:
            reasons.add("mcp_parse_error")
        elif any(k in err_lower for k in ("MINIMAX_API_KEY", "environment variable is required", "api key not found")):
            reasons.add("minimax_api_key_missing")
        elif any(k in err_lower for k in ("401", "unauthorized", "authentication failed", "auth failed")):
            reasons.add("minimax_api_auth_failed")
        elif "mcp_error" in err_lower or "mcp_error:" in err_lower or (err.startswith("error response") and len(err) < 100):
            reasons.add("mcp_error_response")
        elif any(k in err_lower for k in ("quota", "credit", "rate limit", "exceeded", "insufficient")):
            reasons.add("minimax_quota_or_credit_failed")
        elif any(k in err_lower for k in ("empty response", "no content", "response is empty", "null response", "empty response data")):
            reasons.add("mcp_empty_response")
        elif any(k in err_lower for k in ("timed out", "timeout")):
            reasons.add("mcp_timeout")
        elif any(k in err_lower for k in ("存取被拒", "access denied", "permission denied", "winerror 5", "eperm")):
            reasons.add("uv_permission_denied")
        elif any(k in err_lower for k in ("failed to fetch", "pypi.org", "connection refused", "httperror", "sslerror", "proxy")):
            reasons.add("pypi_connection_failed")
        elif any(k in err_lower for k in ("not enough values to unpack", "typeerror", "attributeerror", "jsondecodeerror", "unexpected token", "expecting value")):
            reasons.add("mcp_protocol_error")
        elif (
            "ENOENT" in err
            or "not found" in err_lower
            or "not installed" in err_lower
            or "no module named" in err_lower
        ):
            reasons.add("mcp_package_not_installed")
        # Only add unknown if nothing matched
        if not reasons:
            reasons.add("mcp_unknown_error")
    return sorted(reasons)


class _McpParseError(Exception):
    """Raised when MCP response cannot be parsed into search items."""
    pass


def _extract_search_items(result: Any) -> list[dict[str, str]]:
    """Extract search result items from MCP call_tool result. Raises _McpParseError on failure."""
    try:
        content = result.content if hasattr(result, "content") else result
        if isinstance(content, list) and content:
            first = content[0]
            if hasattr(first, "text"):
                data = json.loads(first.text)
            elif hasattr(first, "data"):
                data = json.loads(first.data)
            else:
                raise _McpParseError(f"Unexpected content list item type: {type(first).__name__}")
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
        if not data:
            raise _McpParseError("Empty response data")
        # Response has content but no recognized search item keys
        raise _McpParseError(f"No recognized item keys in response: {list(data.keys())}")
    except _McpParseError:
        raise
    except Exception as exc:
        raise _McpParseError(f"Failed to extract search items: {exc}") from exc


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
