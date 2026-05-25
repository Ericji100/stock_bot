#!/usr/bin/env python
"""
Manual smoke test for /news refresh full flow.

Run manually to verify news search → WebFetch → AI classify → save works end-to-end.
This script is NOT auto-discovered by unittest (it has no unittest.TestCase).

Usage:
    python scripts/smoke_news_refresh.py --model deepseek --skip-minimax-search
    python scripts/smoke_news_refresh.py --model gemini --max-minimax-queries-per-task 1 --minimax-timeout 5

Environment variables:
    NEWS_SMOKE_TEST=1          Limit MiniMax to 2 queries per task
    NEWS_SKIP_MINIMAX_SEARCH=1  Skip MiniMax entirely (Tavily only)
    NEWS_SMOKE_TASK_LIMIT=2     Limit discovery tasks in smoke mode
    NEWS_SMOKE_MAX_SOURCES=5    Limit sources sent to WebFetch/classification
    NEWS_SMOKE_CLASSIFY_LIMIT=5 Limit items sent to AI classification
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, date
from pathlib import Path
from typing import Any

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from research_center.orchestrator import ResearchCenter
from research_center.news_service import run_news_refresh
from research_center.news_models import NewsItem
from research_center.news_repository import NewsRepository


# ── Config ────────────────────────────────────────────────────────────────────

DEFAULT_MODEL = "deepseek"
MAX_TOTAL_SECONDS = 300  # 5 minutes overall cap


def load_secrets() -> dict:
    secrets_path = Path(__file__).parent.parent / "config" / "secrets.json"
    if not secrets_path.exists():
        raise FileNotFoundError(f"secrets.json not found at {secrets_path}")
    return json.loads(secrets_path.read_text(encoding="utf-8-sig"))


def create_temp_db() -> Path:
    """Create a temporary SQLite DB for smoke test (in system temp dir).

    Returns the db_path. NewsRepository will manage its own connection.
    """
    tmp_dir = Path(tempfile.gettempdir()) / f"stock_ai_bot_news_smoke_{os.getpid()}"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    db_path = tmp_dir / "news_smoke.db"
    # Create minimal schema directly via NewsRepository._init_schema()
    # which opens its own connection
    return db_path


def stage_progress(stage: str, msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] [{stage}] {msg}"
    print(line, flush=True)


def run_smoke(args: argparse.Namespace, max_total_seconds: int) -> dict[str, Any]:
    secrets = load_secrets()
    db_path = create_temp_db()
    stage_progress("INIT", f"DB={db_path}, model={args.model}, timeout={max_total_seconds}s")

    try:
        # Build ResearchCenter with config (reads from config.json + secrets.json)
        from research_center.config import load_research_config
        config = load_research_config()
        center = ResearchCenter(config=config)

        # Configure MiniMax search limits from CLI args
        if hasattr(center, "minimax_search") and center.minimax_search is not None:
            if args.max_minimax_queries_per_task is not None:
                center.minimax_search.max_queries_per_task = args.max_minimax_queries_per_task
                stage_progress("MINIMAX", f"max_queries_per_task set to {args.max_minimax_queries_per_task}")
            if args.minimax_timeout is not None:
                center.minimax_search.timeout_seconds = args.minimax_timeout
                stage_progress("MINIMAX", f"timeout_seconds set to {args.minimax_timeout}")
        if args.model == "minimax" and hasattr(center, "minimax") and center.minimax is not None:
            if args.minimax_ai_timeout is not None and hasattr(center.minimax, "timeout_seconds"):
                center.minimax.timeout_seconds = args.minimax_ai_timeout
                stage_progress("MINIMAX", f"ai_timeout_seconds set to {args.minimax_ai_timeout}")

        repository = NewsRepository(db_path=db_path)

        # Progress callback for stage tracking
        def progress(msg: str) -> None:
            stage_progress("PROGRESS", msg)

        # Set smoke test env vars before calling run_news_refresh
        os.environ["NEWS_SMOKE_TEST"] = "1"
        os.environ.setdefault("NEWS_SMOKE_TASK_LIMIT", "2")
        os.environ.setdefault("NEWS_SMOKE_MAX_SOURCES", "5")
        os.environ.setdefault("NEWS_SMOKE_CLASSIFY_LIMIT", "5")
        if args.skip_minimax_search:
            os.environ["NEWS_SKIP_MINIMAX_SEARCH"] = "1"
            stage_progress("MINIMAX", "NEWS_SKIP_MINIMAX_SEARCH=1, MiniMax will be skipped")
        else:
            os.environ.pop("NEWS_SKIP_MINIMAX_SEARCH", None)

        # Wrap run_news_refresh with overall timeout
        start_time = time.time()

        def run_with_timeout():
            return run_news_refresh(
                center=center,
                repository=repository,
                progress=progress,
                ai_model=args.model,
            )

        # Run in a thread so we can timeout the main thread
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(run_with_timeout)
            try:
                result_items, stats = future.result(timeout=max_total_seconds)
            except concurrent.futures.TimeoutError:
                elapsed = time.time() - start_time
                stage_progress("TIMEOUT", f" exceeded {max_total_seconds}s. Last stage unknown.")
                return {
                    "success": False,
                    "error": f"timeout_after_{max_total_seconds}s",
                    "elapsed": elapsed,
                }

        elapsed = time.time() - start_time
        stage_progress("DONE", f"elapsed={elapsed:.1f}s")

        # Compute stats
        search_sources = stats.get("search_sources", stats.get("total", 0))
        saved = stats.get("saved", 0)
        skipped = stats.get("skipped", 0)

        # Count WebFetch success from enriched items
        webfetch_success = stats.get("webfetch_success")
        if webfetch_success is None:
            webfetch_success = sum(1 for it in result_items if it.summary and len(it.summary) > 10)

        # Filtered count (before classification)
        filtered_count = stats.get("filtered_count", len(result_items))

        # Latest digest count (open our own connection to check)
        try:
            with sqlite3.connect(db_path) as conn:
                cur = conn.execute(
                    "SELECT COUNT(*) FROM news_digests WHERE date = ?",
                    (date.today().isoformat(),)
                )
                digest_count = cur.fetchone()[0]
        except Exception:
            digest_count = 0

        # Top 10 items
        top10 = [
            {"title": it.title, "url": it.url, "category": it.category}
            for it in result_items[:10]
        ]

        result = {
            "success": True,
            "elapsed": elapsed,
            "search_sources": search_sources,
            "webfetch_success": webfetch_success,
            "filtered_count": filtered_count,
            "saved": saved,
            "skipped": skipped,
            "latest_digest_count": digest_count,
            "top10": top10,
            "model": args.model,
            "minimax_diagnostics": stats.get("minimax_diagnostics", {}),
            "web_fetch_diagnostics": stats.get("web_fetch_diagnostics", {}),
            "smoke_sources_used": stats.get("smoke_sources_used", len(result_items)),
        }

        stage_progress("RESULT", (
            f"sources={search_sources}, webfetch_ok={webfetch_success}, "
            f"filtered={filtered_count}, saved={saved}, skipped={skipped}"
        ))

        return result

    finally:
        # Clean up temp DB
        try:
            db_path.unlink(missing_ok=True)
            db_path.parent.rmdir(missing_ok=True)
        except Exception:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Smoke test for /news refresh")
    parser.add_argument(
        "--model",
        choices=["deepseek", "gemini", "minimax"],
        default=DEFAULT_MODEL,
        help="AI model for classification",
    )
    parser.add_argument(
        "--skip-minimax-search",
        action="store_true",
        help="Skip MiniMax MCP Search entirely (use Tavily only)",
    )
    parser.add_argument(
        "--max-minimax-queries-per-task",
        type=int, default=None,
        help="Max queries per MiniMax task (default: 2 in smoke mode, unlimited otherwise)",
    )
    parser.add_argument(
        "--minimax-timeout",
        type=int, default=None,
        help="MiniMax query timeout in seconds",
    )
    parser.add_argument(
        "--minimax-ai-timeout",
        type=int, default=45,
        help="MiniMax classification timeout in seconds when --model minimax",
    )
    parser.add_argument(
        "--max-total-seconds",
        type=int, default=MAX_TOTAL_SECONDS,
        help=f"Overall smoke test timeout (default: {MAX_TOTAL_SECONDS}s)",
    )
    args = parser.parse_args()

    print("=" * 60, flush=True)
    print(f"NEWS REFRESH SMOKE TEST  model={args.model}", flush=True)
    print(f"  skip_minimax_search={args.skip_minimax_search}", flush=True)
    print(f"  max_minimax_queries_per_task={args.max_minimax_queries_per_task}", flush=True)
    print(f"  minimax_timeout={args.minimax_timeout}", flush=True)
    print(f"  minimax_ai_timeout={args.minimax_ai_timeout}", flush=True)
    print(f"  max_total_seconds={args.max_total_seconds}", flush=True)
    print("=" * 60, flush=True)

    try:
        result = run_smoke(args, max_total_seconds=args.max_total_seconds)
    except Exception as exc:
        print(f"\n[ERROR] Smoke test failed: {exc}", flush=True)
        import traceback
        traceback.print_exc()
        sys.exit(1)

    print("\n" + "=" * 60, flush=True)
    print("SMOKE TEST RESULT", flush=True)
    print("=" * 60, flush=True)
    print(f"success           : {result.get('success')}", flush=True)
    print(f"elapsed           : {result.get('elapsed', 0):.1f}s", flush=True)
    print(f"search_sources    : {result.get('search_sources', 0)}", flush=True)
    print(f"webfetch_success  : {result.get('webfetch_success', 0)}", flush=True)
    print(f"filtered_count    : {result.get('filtered_count', 0)}", flush=True)
    print(f"saved             : {result.get('saved', 0)}", flush=True)
    print(f"skipped           : {result.get('skipped', 0)}", flush=True)
    print(f"latest_digest_cnt : {result.get('latest_digest_count', 0)}", flush=True)
    print(f"model             : {result.get('model')}", flush=True)
    print(f"smoke_sources_used: {result.get('smoke_sources_used', 0)}", flush=True)

    if result.get("web_fetch_diagnostics"):
        diag = result["web_fetch_diagnostics"]
        print(f"webfetch_selected : {diag.get('selected_url_count', diag.get('total_urls', 0))}", flush=True)
        print(f"webfetch_status   : {diag.get('status', '')}", flush=True)

    if result.get("top10"):
        print("\ntop 10 news items:", flush=True)
        for i, item in enumerate(result["top10"], 1):
            print(f"  {i}. [{item.get('category','')}] {item['title'][:50]}", flush=True)
            print(f"     {item['url'][:70]}", flush=True)

    # Print MiniMax diagnostics if available
    if "minimax_diagnostics" in result:
        diag = result["minimax_diagnostics"]
        print("\n" + "-" * 60, flush=True)
        print("MINIMAX DIAGNOSTICS", flush=True)
        print("-" * 60, flush=True)
        print(f"  enabled       : {diag.get('enabled', False)}", flush=True)
        print(f"  source_count  : {diag.get('source_count', 0)}", flush=True)
        print(f"  task_count    : {diag.get('task_count', 0)}", flush=True)
        print(f"  error_reasons : {diag.get('error_reasons', [])}", flush=True)
        runs = diag.get("runs", [])
        print(f"  runs          : {len(runs)} task(s)", flush=True)
        for r in runs:
            empty_flag = " [EMPTY]" if r.get("mcp_empty_results") else ""
            print(f"    - {r.get('label')}: status={r.get('status')}, "
                  f"sources={r.get('source_count', 0)}, "
                  f"added={r.get('added_source_count', 0)}, "
                  f"errors={r.get('error_count', 0)}{empty_flag}", flush=True)
            if r.get("error_reasons"):
                print(f"      error_reasons: {r['error_reasons']}", flush=True)
        raw_samples = diag.get("raw_response_samples", [])
        if raw_samples:
            print(f"  raw_samples   : {len(raw_samples)} (max 3)", flush=True)
            for s in raw_samples[:3]:
                print(f"    [{s.get('status')}] query={s.get('query', '')[:50]}", flush=True)
                print(f"      raw_type={s.get('raw_type')}, keys={s.get('raw_keys', [])}, items={s.get('item_count')}", flush=True)
                if s.get("error"):
                    print(f"      error: {s['error'][:80]}", flush=True)
                if s.get("preview"):
                    preview = s["preview"].replace("\n", " ")[:100]
                    print(f"      preview: {preview}", flush=True)

    if not result.get("success"):
        print(f"\nERROR: {result.get('error')}", flush=True)
        sys.exit(1)

    # Verify acceptance criteria
    print("\n" + "=" * 60, flush=True)
    print("ACCEPTANCE CRITERIA", flush=True)
    print("=" * 60, flush=True)
    checks = [
        ("search_sources > 0", result.get("search_sources", 0) > 0),
        ("webfetch_success > 0", result.get("webfetch_success", 0) > 0),
        ("filtered_count > 0", result.get("filtered_count", 0) > 0),
        ("saved > 0", result.get("saved", 0) > 0),
    ]
    all_pass = True
    for name, passed in checks:
        status = "PASS" if passed else "FAIL"
        if not passed:
            all_pass = False
        print(f"  {status}: {name}", flush=True)

    if all_pass:
        print("\nAll acceptance criteria PASSED.", flush=True)
    else:
        print("\nSome acceptance criteria FAILED.", flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
