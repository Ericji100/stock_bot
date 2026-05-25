#!/usr/bin/env python
"""
Manual smoke test for MiniMax topic maintain flow.

Run manually to verify MiniMax JSON-only topic maintenance works end-to-end.
This script is NOT auto-discovered by unittest (it has no unittest.TestCase).

Usage:
    python scripts/smoke_topic_maintain_minimax.py

This will consume MiniMax API credits. Run only when necessary.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, date
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from research_center.orchestrator import ResearchCenter
from research_center.command_parser import CommandRequest
from research_center.topic_repository import (
    load_change_pack,
    list_change_packs,
    raw_response_path,
)
from research_center.topic_models import TopicChangeStatus

# ── Config ────────────────────────────────────────────────────────────────────

TOPIC_MODEL = "minimax"  # only minimax is tested here
MODE = "deep"            # deep mode for thorough test


def load_secrets() -> dict:
    secrets_path = Path(__file__).parent.parent / "config" / "secrets.json"
    if not secrets_path.exists():
        raise FileNotFoundError(f"secrets.json not found at {secrets_path}")
    return json.loads(secrets_path.read_text(encoding="utf-8-sig"))


def build_request() -> CommandRequest:
    """Build a CommandRequest matching /topic_maintain --deep --model minimax."""
    return CommandRequest(
        command="topic_maintain",
        raw_text="/topic_maintain --deep --model minimax",
        target="",
        theme_scope="",
        target_type="topic_maintain",
        mode=MODE,
        source_only=False,
        score=False,
        brief=False,
        top=None,
        ai_model=TOPIC_MODEL,
        report_date=None,
        output_formats=("json",),
        user_id="smoke_test_user",
        created_at=None,
    )


def find_latest_change_pack():
    """Find the most recently created change pack."""
    packs = list_change_packs(status=None)  # all statuses
    if not packs:
        return None
    return max(packs, key=lambda p: p.created_at)


def print_result(pack, raw_p, prompt_log_p, elapsed):
    print("\n" + "=" * 60)
    print("SMOKE TEST RESULT")
    print("=" * 60)
    print(f"change_id              : {pack.change_id}")
    print(f"status                 : {pack.status.value}")
    print(f"model                  : {pack.model}")
    print(f"mode                   : {pack.mode.value}")
    print(f"actions_count          : {len(pack.actions)}")
    print(f"warnings               : {pack.warnings}")
    print(f"raw_response_path      : {raw_p}")
    print(f"prompt_log_path        : {prompt_log_p}")
    print(f"elapsed                : {elapsed:.1f}s")
    print(f"skip_webfetch_evidence : True")
    print("=" * 60)

    # Check conditions
    issues = []
    has_429 = False
    has_json_decode_error = False

    if pack.status == TopicChangeStatus.FAILED and not pack.warnings:
        issues.append("WARN: status=failed but no warnings set")

    if not pack.actions and pack.status != TopicChangeStatus.FAILED:
        issues.append("WARN: no actions but status is not failed")

    # Check raw response for 429 or JSON decode errors
    if raw_p.exists():
        raw_content = raw_p.read_text(encoding="utf-8")
        if "429" in raw_content:
            has_429 = True
            issues.append("ERROR: Raw response contains 429 (rate limit)")
        try:
            json.loads(raw_content)
        except json.JSONDecodeError:
            # Check if it's a wrapped {"raw": "..."} response
            try:
                wrapped = json.loads(raw_content)
                if "raw" in wrapped:
                    inner = wrapped["raw"]
                    if "429" in inner:
                        has_429 = True
                        issues.append("ERROR: Wrapped raw response contains 429")
                    try:
                        json.loads(inner)
                    except json.JSONDecodeError:
                        has_json_decode_error = True
                        issues.append("ERROR: Wrapped inner content is not valid JSON")
            except json.JSONDecodeError:
                has_json_decode_error = True
                issues.append("ERROR: Raw response is not valid JSON")

    if issues:
        print("\nISSUES FOUND:")
        for issue in issues:
            print(f"  - {issue}")
    else:
        print("\nAll checks passed.")

    # Show raw response preview if failed or no actions
    if pack.status == TopicChangeStatus.FAILED or not pack.actions:
        if raw_p.exists():
            raw_content = raw_p.read_text(encoding="utf-8")
            print(f"\nRaw response preview (first 500 chars):\n{raw_content[:500]}")

    return len(issues) == 0, has_429, has_json_decode_error


def run():
    print(f"[{datetime.now().isoformat()}] Building ResearchCenter...")
    center = ResearchCenter()

    # Verify MiniMax is configured
    if not center.minimax or not center.minimax.is_configured():
        print("ERROR: MiniMax is not configured. Check config/secrets.json")
        return False

    # Build request
    request = build_request()
    print(f"[{datetime.now().isoformat()}] Executing /topic_maintain --{MODE} --model {TOPIC_MODEL}...")

    start = datetime.now()
    try:
        from research_center.topic_maintain_service import run_topic_maintain
        pack = run_topic_maintain(
            request,
            center=center,
            progress=lambda m: print(f"  {m}"),
            skip_webfetch_evidence=True,
        )
    except Exception as exc:
        elapsed = (datetime.now() - start).total_seconds()
        print(f"\nEXCEPTION during run_topic_maintain: {exc}")
        print(f"Elapsed: {elapsed:.1f}s")

        # Try to find the most recent change pack as it may have been saved before the error
        latest = find_latest_change_pack()
        if latest:
            raw_p = Path(latest.raw_response_path) if latest.raw_response_path else None
            prompt_p = Path(latest.prompt_log_path) if latest.prompt_log_path else None
            print(f"\nLatest change pack found: {latest.change_id}")
            print(f"  status: {latest.status.value}")
            print(f"  raw_response_path: {raw_p}")
            print(f"  prompt_log_path: {prompt_p}")
            if raw_p and raw_p.exists():
                print(f"\nRaw response preview:\n{raw_p.read_text(encoding='utf-8')[:500]}")
        return False

    elapsed = (datetime.now() - start).total_seconds()

    # Find paths
    raw_p = Path(pack.raw_response_path) if pack.raw_response_path else None
    prompt_p = Path(pack.prompt_log_path) if pack.prompt_log_path else None

    # Also try to get paths from the change pack dir
    if not raw_p or not raw_p.exists():
        # Find the raw response file that was just created
        topic_ai_raw_dir = Path("logs/topic_ai_raw")
        if topic_ai_raw_dir.exists():
            files = sorted(topic_ai_raw_dir.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
            if files:
                raw_p = files[0]
    if not prompt_p or not prompt_p.exists():
        topic_prompts_dir = Path("logs/ai_prompts")
        if topic_prompts_dir.exists():
            files = sorted(topic_prompts_dir.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
            if files:
                prompt_p = files[0]

    ok, has_429, has_json_decode_error = print_result(pack, raw_p or Path("?"), prompt_p or Path("?"), elapsed)
    return ok


if __name__ == "__main__":
    print("=" * 60)
    print("MiniMax Topic Maintain Smoke Test")
    print(f"Started: {datetime.now().isoformat()}")
    print("=" * 60)

    success = run()

    print(f"\nFinished: {datetime.now().isoformat()}")
    sys.exit(0 if success else 1)