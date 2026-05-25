"""MiniMax Token Plan MCP web_search verification script.

Usage (tools-only, no search):
    python tools/minimax_mcp_verify.py

Usage (real web_search call):
    python tools/minimax_mcp_verify.py --search
    python tools/minimax_mcp_verify.py --search --query "台積電 2026 法說會"

Usage (offline normalization check):
    python tools/minimax_mcp_verify.py --normalize-file logs/minimax_mcp_verify/latest_result.json

This script:
1. Reads MINIMAX_API_KEY from env or config/secrets.json
2. Finds uvx/uv executable path
3. Attempts to start MCP server via stdio
4. Calls initialize() + list_tools() to verify web_search exists
5. Optionally calls web_search with the given query
6. Outputs result to CMD and logs/minimax_mcp_verify/latest_result.json

NOTE: Without --search, this script only verifies the MCP server can start.
With --search, it calls web_search once and consumes MiniMax credits.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import mcp
    from mcp.client.stdio import stdio_client, StdioServerParameters
    from mcp import ClientSession
except ImportError as exc:
    print(f"ERROR: mcp package not installed. Run: pip install mcp. Details: {exc}")
    sys.exit(1)

DEFAULT_QUERY = "台積電 2026 法說會"
SEARCH_TIMEOUT_SEC = 120
RAW_PREVIEW_MAX_CHARS = 2000
NORMALIZED_SOURCES_MAX = 10


def get_minimax_api_key() -> str | None:
    """Get MiniMax API key from environment or config/secrets.json."""
    key = os.environ.get("MINIMAX_API_KEY")
    if key:
        return key
    secrets_path = Path(__file__).parent.parent / "config" / "secrets.json"
    if secrets_path.exists():
        try:
            data = json.loads(secrets_path.read_text(encoding="utf-8-sig"))
            return data.get("minimax_api_key")
        except Exception:
            pass
    return None


def find_command(name: str) -> str | None:
    """Find executable path using env override or shutil.which."""
    override = os.environ.get(f"{name.upper().replace('-', '_')}_EXE_PATH")
    if override and Path(override).exists():
        return override
    found = shutil.which(name)
    return found


def build_timestamped_result_path(output_dir: Path, mode: str) -> Path | None:
    """Build timestamped result path for web_search mode.

    Returns None for non-web_search modes.
    Filename format: search_YYYYMMDD_HHMMSS.json (ASCII only, no query).
    """
    if mode != "web_search":
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return output_dir / f"search_{stamp}.json"


def write_result(result: dict, archived_path: Path | None = None) -> dict:
    """Write result JSON to logs/minimax_mcp_verify/latest_result.json.

    Also write archived copy if archived_path is provided (web_search mode).
    Adds latest_result_path and archived_result_path to result before writing.
    """
    output_dir = Path(__file__).parent.parent / "logs" / "minimax_mcp_verify"
    output_dir.mkdir(parents=True, exist_ok=True)

    latest_path = output_dir / "latest_result.json"

    # Add path fields
    result["latest_result_path"] = str(latest_path)
    result["archived_result_path"] = str(archived_path) if archived_path else None

    # Write latest
    latest_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # Write archive copy if applicable
    if archived_path:
        archived_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    return result


def build_attempt(command: str, args: list[str], env: dict, api_key: str) -> dict:
    return {
        "command": command,
        "args": args,
        "env_MINIMAX_API_KEY": f"{api_key[:8]}...{api_key[-4:]}" if len(api_key) > 12 else "***",
        "env_MINIMAX_API_HOST": env.get("MINIMAX_API_HOST", "https://api.minimax.io"),
    }


def normalize_web_search_result(result: object) -> dict:
    """Normalize MCP web_search result into standard fields.

    Checks has_snippets from:
    - Top-level "snippets" or "summaries" keys
    - Per-source "snippet" or "summary" fields
    """
    raw = result if result is not None else {}
    normalized: dict = {
        "source_count": 0,
        "url_count": 0,
        "has_snippets": False,
        "has_related_queries": False,
        "normalized_sources": [],
        "related_queries": [],
        "raw_text_preview": "",
        "raw_result": raw,
    }

    # The actual MiniMax response has an "organic" list of results
    sources: list = []
    top_level_snippets = False
    top_level_summaries = False
    if isinstance(raw, dict):
        # Try common keys: organic, results, sources, items
        sources = raw.get("organic") or raw.get("results") or raw.get("sources") or raw.get("items") or []
        top_level_snippets = bool(raw.get("snippets"))
        top_level_summaries = bool(raw.get("summaries"))
        normalized["has_related_queries"] = bool(raw.get("related_queries") or raw.get("suggestions"))
        if raw.get("related_queries"):
            normalized["related_queries"] = raw["related_queries"]
        elif raw.get("suggestions"):
            normalized["related_queries"] = raw["suggestions"]
    elif isinstance(raw, list):
        sources = raw

    normalized["source_count"] = len(sources)
    normalized["url_count"] = sum(
        1 for s in sources
        if isinstance(s, dict) and bool(s.get("url") or s.get("link"))
    )

    # Normalize each source to {title, url, snippet, published_date}
    has_source_snippets = False
    for s in sources[:NORMALIZED_SOURCES_MAX]:
        if isinstance(s, dict):
            snippet_or_summary = bool(s.get("snippet") or s.get("summary"))
            if snippet_or_summary:
                has_source_snippets = True
            normalized["normalized_sources"].append({
                "title": s.get("title") or s.get("name", ""),
                "url": s.get("url") or s.get("link", ""),
                "snippet": s.get("snippet") or s.get("summary", ""),
                "published_date": s.get("date") or s.get("published_date", ""),
            })

    # has_snippets: top-level OR per-source
    normalized["has_snippets"] = top_level_snippets or top_level_summaries or has_source_snippets

    # Build raw_text_preview
    try:
        raw_text = str(raw)
        normalized["raw_text_preview"] = raw_text[:RAW_PREVIEW_MAX_CHARS]
    except Exception:
        normalized["raw_text_preview"] = str(raw)[:RAW_PREVIEW_MAX_CHARS]

    return normalized


def do_normalize_file(input_path: Path) -> dict | None:
    """Read a result JSON and re-normalize from its raw_result."""
    try:
        data = json.loads(input_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"ERROR: Could not read {input_path}: {e}")
        return None

    raw = data.get("raw_result")
    if raw is None:
        print(f"ERROR: No raw_result found in {input_path}")
        return None

    norm = normalize_web_search_result(raw)
    print(f"\n=== Offline Normalization Check ===")
    print(f"source_count: {norm['source_count']}")
    print(f"url_count: {norm['url_count']}")
    print(f"has_snippets: {norm['has_snippets']}")
    print(f"has_related_queries: {norm['has_related_queries']}")
    print(f"related_queries: {norm['related_queries']}")
    print(f"\nFirst 3 normalized_sources:")
    for src in norm["normalized_sources"][:3]:
        print(f"  title: {src['title'][:50]}")
        print(f"  url: {src['url'][:80]}")
        print(f"  snippet: {src['snippet'][:80] if src['snippet'] else '(none)'}")
        print()

    output_dir = input_path.parent
    output_path = output_dir / "normalized_check.json"
    output_path.write_text(
        json.dumps({
            "source_count": norm["source_count"],
            "url_count": norm["url_count"],
            "has_snippets": norm["has_snippets"],
            "has_related_queries": norm["has_related_queries"],
            "normalized_sources": norm["normalized_sources"],
            "related_queries": norm["related_queries"],
            "source_file": str(input_path.name),
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Normalized check saved to: {output_path}")
    return norm


async def try_start_mcp(
    command: str,
    args: list[str],
    api_key: str,
    env: dict,
) -> tuple[bool, dict]:
    """Attempt to start MCP server and return (success, result_dict)."""
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
                web_search_found = "web_search" in tool_names

                return True, {
                    "tool_found": web_search_found,
                    "tools": tool_names,
                }
    except Exception as exc:
        return False, {"error": str(exc)}


async def call_web_search(
    command: str,
    args: list[str],
    api_key: str,
    env: dict,
    query: str,
) -> dict:
    """Start MCP server and call web_search, returning normalized result."""
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
                if "web_search" not in tool_names:
                    return {
                        "ok": False,
                        "error": "web_search not found in tool list",
                        "raw": None,
                    }

                result = await asyncio.wait_for(
                    session.call_tool("web_search", arguments={"query": query}),
                    timeout=SEARCH_TIMEOUT_SEC,
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

                return {
                    "ok": True,
                    "raw": raw_content,
                    "error": None,
                }
    except asyncio.TimeoutError:
        return {
            "ok": False,
            "error": f"web_search timed out after {SEARCH_TIMEOUT_SEC}s",
            "raw": None,
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
            "raw": None,
        }


async def main() -> dict:
    parser = argparse.ArgumentParser(description="MiniMax Token Plan MCP verification")
    parser.add_argument("--search", action="store_true", help="Call web_search (consumes credits)")
    parser.add_argument("--query", default=DEFAULT_QUERY, help=f"Search query (default: {DEFAULT_QUERY})")
    parser.add_argument("--normalize-file", type=Path, default=None,
                        help="Re-normalize raw_result from a prior result JSON (offline, no MCP)")
    args = parser.parse_args()

    # Offline normalization mode
    if args.normalize_file:
        norm = do_normalize_file(args.normalize_file)
        if norm is None:
            sys.exit(1)
        sys.exit(0)

    mode = "web_search" if args.search else "tools_only"

    api_key = get_minimax_api_key()
    if not api_key:
        result = {
            "ok": False,
            "mode": mode,
            "query": args.query if args.search else None,
            "tool_found": False,
            "tools": None,
            "selected_command": None,
            "selected_args": None,
            "source_count": 0,
            "url_count": 0,
            "has_snippets": False,
            "has_related_queries": False,
            "normalized_sources": [],
            "related_queries": [],
            "raw_text_preview": "",
            "raw_result": None,
            "attempts": [],
            "error": "MINIMAX_API_KEY not found in environment or config/secrets.json",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        print(f"ERROR: {result['error']}")
        write_result(result)
        return result

    masked = f"{api_key[:8]}...{api_key[-4:]}" if len(api_key) > 12 else "***"
    print(f"Using API key: {masked}")
    if args.search:
        print(f"MODE: web_search (will consume credits)")
        print(f"QUERY: {args.query}")
    else:
        print(f"MODE: tools_only (no search)")

    # Base environment - must include current PATH so subprocess can find uv/uvx
    base_env = dict(os.environ)

    # Discover uvx and uv paths
    uvx_path = find_command("uvx")
    uv_path = find_command("uv")

    print(f"uvx found: {uvx_path}")
    print(f"uv found: {uv_path}")

    # Build list of candidate startup attempts
    candidates: list[tuple[str, list[str]]] = []
    if uvx_path:
        candidates.append((uvx_path, ["minimax-coding-plan-mcp"]))
    if uv_path:
        candidates.append((uv_path, ["tool", "run", "--from", "minimax-coding-plan-mcp", "minimax-coding-plan-mcp"]))
    # Also try via python -m uv (uv installed but not in PATH as standalone)
    if not candidates:
        python_exe = sys.executable
        print(f"uvx/uv not in PATH, trying fallback: {python_exe} -m uv")
        candidates.append((python_exe, ["-m", "uv", "tool", "run", "--from", "minimax-coding-plan-mcp", "minimax-coding-plan-mcp"]))

    if args.search:
        # For search mode, we use call_web_search which handles its own session lifecycle
        output_dir = Path(__file__).parent.parent / "logs" / "minimax_mcp_verify"
        first_candidate = True
        for command, args_list in candidates:
            if first_candidate:
                first_candidate = False
                print(f"\nTrying: {command} {' '.join(args_list)}")
            env = dict(base_env)
            env["MINIMAX_API_KEY"] = api_key
            env["MINIMAX_API_HOST"] = "https://api.minimax.io"

            attempt = build_attempt(command, args_list, env, api_key)

            print(f"\nCalling web_search...")
            search_resp = await call_web_search(command, args_list, api_key, env, args.query)

            ts_path = build_timestamped_result_path(output_dir, mode)

            if search_resp["ok"]:
                norm = normalize_web_search_result(search_resp["raw"])
                result = {
                    "ok": True,
                    "mode": mode,
                    "query": args.query,
                    "tool_found": True,
                    "tools": ["web_search", "understand_image"],
                    "selected_command": command,
                    "selected_args": args_list,
                    "source_count": norm["source_count"],
                    "url_count": norm["url_count"],
                    "has_snippets": norm["has_snippets"],
                    "has_related_queries": norm["has_related_queries"],
                    "normalized_sources": norm["normalized_sources"],
                    "related_queries": norm["related_queries"],
                    "raw_text_preview": norm["raw_text_preview"],
                    "raw_result": norm["raw_result"],
                    "attempts": [],
                    "error": None,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
                print(f"web_search ok")
                print(f"source_count: {norm['source_count']}")
                print(f"url_count: {norm['url_count']}")
                print(f"has_snippets: {norm['has_snippets']}")
                print(f"has_related_queries: {norm['has_related_queries']}")
                print(f"\nResult: ok=True, tool_found=True")
                result = write_result(result, ts_path)
                print(f"Result saved to: {result['latest_result_path']}")
                if result.get("archived_result_path"):
                    print(f"Archived to: {result['archived_result_path']}")
                return result
            else:
                attempt["error"] = search_resp.get("error", "search failed")
                result = {
                    "ok": False,
                    "mode": mode,
                    "query": args.query,
                    "tool_found": False,
                    "tools": None,
                    "selected_command": command,
                    "selected_args": args_list,
                    "source_count": 0,
                    "url_count": 0,
                    "has_snippets": False,
                    "has_related_queries": False,
                    "normalized_sources": [],
                    "related_queries": [],
                    "raw_text_preview": "",
                    "raw_result": None,
                    "attempts": [attempt],
                    "error": search_resp.get("error", "search failed"),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
                print(f"web_search failed")
                print(f"error: {search_resp.get('error')}")
                print(f"\nResult: ok=False")
                result = write_result(result, ts_path)
                print(f"Result saved to: {result['latest_result_path']}")
                if result.get("archived_result_path"):
                    print(f"Archived to: {result['archived_result_path']}")
                return result

        # Exhausted all candidates
        result = {
            "ok": False,
            "mode": mode,
            "query": args.query,
            "tool_found": False,
            "tools": None,
            "selected_command": None,
            "selected_args": None,
            "source_count": 0,
            "url_count": 0,
            "has_snippets": False,
            "has_related_queries": False,
            "normalized_sources": [],
            "related_queries": [],
            "raw_text_preview": "",
            "raw_result": None,
            "attempts": [],
            "error": "All MCP server candidates failed",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        print(f"\nResult: ok=False, all candidates failed")
        output_dir = Path(__file__).parent.parent / "logs" / "minimax_mcp_verify"
        ts_path = build_timestamped_result_path(output_dir, mode)
        result = write_result(result, ts_path)
        print(f"Result saved to: {result['latest_result_path']}")
        if result.get("archived_result_path"):
            print(f"Archived to: {result['archived_result_path']}")
        return result
        return result

    # tools_only mode
    attempts: list[dict] = []
    tool_found = False
    tools: list[str] = []
    selected_command: str | None = None
    selected_args: list[str] | None = None
    final_error: str | None = None

    for command, args_list in candidates:
        env = dict(base_env)
        env["MINIMAX_API_KEY"] = api_key
        env["MINIMAX_API_HOST"] = "https://api.minimax.io"

        attempt = build_attempt(command, args_list, env, api_key)
        print(f"\nTrying: {command} {' '.join(args_list)}")

        success, response = await try_start_mcp(command, args_list, api_key, env)

        if success:
            tool_found = response["tool_found"]
            tools = response["tools"]
            selected_command = command
            selected_args = args_list
            final_error = None if tool_found else "web_search tool not found in server tool list"
            print(f"Success! web_search found: {tool_found}")
            print(f"Tools: {tools}")
            break
        else:
            attempt["error"] = response["error"]
            attempts.append(attempt)
            print(f"Failed: {response['error']}")
            final_error = response["error"]

    result = {
        "ok": tool_found,
        "mode": mode,
        "query": None,
        "tool_found": tool_found,
        "tools": tools if tool_found else None,
        "selected_command": selected_command,
        "selected_args": selected_args,
        "source_count": 0,
        "url_count": 0,
        "has_snippets": False,
        "has_related_queries": False,
        "normalized_sources": [],
        "related_queries": [],
        "raw_text_preview": "",
        "raw_result": None,
        "attempts": attempts,
        "error": final_error,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    print(f"\nResult: ok={result['ok']}, tool_found={result['tool_found']}")
    if result["error"]:
        print(f"Error: {result['error']}")

    result = write_result(result)
    print(f"Result saved to: {result['latest_result_path']}")
    return result


if __name__ == "__main__":
    try:
        result = asyncio.run(main())
        sys.exit(0 if result["ok"] else 1)
    except Exception as e:
        print(f"FATAL: {e}")
        sys.exit(1)