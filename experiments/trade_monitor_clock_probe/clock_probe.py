from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

try:
    from experiments.trade_monitor_clock_probe.message_renderer import (
        render_analysis_markdown,
        render_unavailable_markdown,
        validate_analysis_payload,
    )
except ModuleNotFoundError:
    from message_renderer import (
        render_analysis_markdown,
        render_unavailable_markdown,
        validate_analysis_payload,
    )


HERE = Path(__file__).resolve().parent
DEFAULT_RUNTIME_DIR = HERE / ".runtime"
DEFAULT_SCHEMA_FILE = HERE / "analysis_schema.json"
TAIPEI = ZoneInfo("Asia/Taipei")
DEFAULT_AUTOMATION_FILE = (
    Path.home() / ".codex" / "automations" / "1-k" / "automation.toml"
)
DEFAULT_PROTECTED_PATHS = (
    Path(r"D:\code\stock_ai_bot\main.py"),
    Path(r"D:\code\stock_ai_bot\monitor_service.py"),
    Path(r"D:\code\stock_ai_bot\config.json"),
    Path(r"D:\code\stock_ai_bot\trade_monitor_bridge.py"),
    Path(r"D:\code\stock_ai_bot\trade_monitor_resume_guard.py"),
    DEFAULT_AUTOMATION_FILE,
)


@dataclass(frozen=True)
class ProtectedFile:
    path: str
    sha256: str
    size: int


@dataclass(frozen=True)
class CaptureDecision:
    status: str
    reason: str
    context: dict[str, object]
    next_state: dict[str, object]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_fingerprint(path: Path) -> ProtectedFile:
    data = path.read_bytes()
    return ProtectedFile(path=str(path.resolve()), sha256=sha256_bytes(data), size=len(data))


def _ensure_experiment_output(path: Path) -> Path:
    resolved = path.resolve()
    root = HERE.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError(f"output must stay inside isolated experiment directory: {root}")
    return resolved


def _atomic_write_text(path: Path, text: str) -> None:
    path = _ensure_experiment_output(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temp_path.write_text(text, encoding="utf-8", newline="\n")
    temp_path.replace(path)


def _atomic_write_json(path: Path, payload: object) -> None:
    _atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def read_automation_prompt(automation_file: Path) -> str:
    with automation_file.open("rb") as handle:
        payload = tomllib.load(handle)
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt:
        raise ValueError("automation prompt is missing or empty")
    return prompt


def snapshot_prompt(
    automation_file: Path,
    runtime_dir: Path,
    protected_paths: Iterable[Path] = DEFAULT_PROTECTED_PATHS,
) -> dict[str, object]:
    runtime_dir = _ensure_experiment_output(runtime_dir)
    prompt = read_automation_prompt(automation_file)
    prompt_path = runtime_dir / "prompt_snapshot.txt"
    manifest_path = runtime_dir / "protected_manifest.json"
    fingerprints = [file_fingerprint(path) for path in protected_paths]
    manifest: dict[str, object] = {
        "created_at": datetime.now().astimezone().isoformat(),
        "mode": "read-only-source-snapshot",
        "automation_file": str(automation_file.resolve()),
        "prompt_snapshot": str(prompt_path.resolve()),
        "prompt_sha256": sha256_bytes(prompt.encode("utf-8")),
        "prompt_chars": len(prompt),
        "protected_files": [item.__dict__ for item in fingerprints],
    }
    _atomic_write_text(prompt_path, prompt)
    _atomic_write_json(manifest_path, manifest)
    return manifest


def verify_manifest(manifest_path: Path) -> tuple[bool, list[dict[str, str]]]:
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    changes: list[dict[str, str]] = []
    for expected in payload["protected_files"]:
        path = Path(expected["path"])
        if not path.exists():
            changes.append({"path": str(path), "status": "missing"})
            continue
        actual = file_fingerprint(path)
        if actual.sha256 != expected["sha256"]:
            changes.append(
                {
                    "path": str(path),
                    "status": "changed",
                    "expected_sha256": expected["sha256"],
                    "actual_sha256": actual.sha256,
                }
            )
    prompt_path = Path(payload["prompt_snapshot"])
    if not prompt_path.exists():
        changes.append({"path": str(prompt_path), "status": "snapshot_missing"})
    else:
        prompt_hash = sha256_bytes(prompt_path.read_bytes())
        if prompt_hash != payload["prompt_sha256"]:
            changes.append(
                {
                    "path": str(prompt_path),
                    "status": "snapshot_changed",
                    "expected_sha256": payload["prompt_sha256"],
                    "actual_sha256": prompt_hash,
                }
            )
    return not changes, changes


def next_minute_slot(now: datetime, align_second: int) -> datetime:
    if not 0 <= align_second <= 59:
        raise ValueError("align_second must be between 0 and 59")
    target = now.replace(second=align_second, microsecond=0)
    if target <= now:
        target += timedelta(minutes=1)
    return target


def newest_png(capture_dir: Path | None) -> Path | None:
    if capture_dir is None or not capture_dir.exists():
        return None
    candidates = [path for path in capture_dir.glob("*.png") if path.is_file()]
    return max(candidates, key=lambda path: path.stat().st_mtime_ns, default=None)


def parse_capture_time(value: str | None) -> datetime:
    if value is None:
        return datetime.now(TAIPEI)
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("capture time must include an explicit UTC offset")
    return parsed.astimezone(TAIPEI)


def expected_latest_closed_k(captured_at: datetime) -> datetime:
    if captured_at.tzinfo is None or captured_at.utcoffset() is None:
        raise ValueError("captured_at must be timezone-aware")
    taipei_time = captured_at.astimezone(TAIPEI)
    current_minute = taipei_time.replace(second=0, microsecond=0)
    return current_minute - timedelta(minutes=1)


def _load_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON object expected: {path}")
    return payload


def decide_capture(
    *,
    image_path: Path,
    captured_at: datetime,
    previous_state: Mapping[str, Any] | None,
    stale_after_seconds: float,
) -> CaptureDecision:
    if stale_after_seconds <= 0:
        raise ValueError("stale_after_seconds must be positive")
    if not image_path.is_file():
        raise FileNotFoundError(image_path)

    captured_at = captured_at.astimezone(TAIPEI)
    expected = expected_latest_closed_k(captured_at)
    current = expected + timedelta(minutes=1)
    image_hash = sha256_bytes(image_path.read_bytes())
    state = dict(previous_state or {})
    previous_hash = state.get("last_image_sha256")
    previous_completed_raw = state.get("last_completed_closed_k")
    previous_completed = (
        datetime.fromisoformat(previous_completed_raw).astimezone(TAIPEI)
        if isinstance(previous_completed_raw, str)
        else None
    )

    if previous_hash == image_hash:
        same_since_raw = state.get("same_image_since") or state.get("last_capture_at")
        same_since = (
            datetime.fromisoformat(same_since_raw).astimezone(TAIPEI)
            if isinstance(same_since_raw, str)
            else captured_at
        )
    else:
        same_since = captured_at
    unchanged_seconds = max(0.0, (captured_at - same_since).total_seconds())

    status = "FRESH"
    reason = "時間單調遞增且圖表未達停滯門檻。"
    if previous_completed is not None and expected < previous_completed:
        status = "REGRESSION"
        reason = "排程推導的最新已收盤 K 早於上一個已完成事件。"
    elif previous_completed is not None and expected == previous_completed:
        status = "SAME_BAR"
        reason = "相同的已收盤 K 已完成分析，本輪不重複處理。"
    elif previous_hash == image_hash and unchanged_seconds >= stale_after_seconds:
        status = "STALE"
        reason = f"純圖表連續 {unchanged_seconds:.0f} 秒完全相同，無法確認圖表仍在更新。"

    event_id = expected.strftime("%Y%m%d-%H%M")
    context: dict[str, object] = {
        "captured_at_taipei": captured_at.isoformat(),
        "expected_latest_closed_k": expected.isoformat(),
        "expected_latest_closed_k_hhmm": expected.strftime("%H:%M"),
        "current_unclosed_k": current.isoformat(),
        "current_unclosed_k_hhmm": current.strftime("%H:%M"),
        "time_source": "local_scheduler",
        "capture_status": status,
        "capture_reason": reason,
        "event_id": event_id,
        "image_path": str(image_path.resolve()),
        "image_sha256": image_hash,
        "image_changed": previous_hash != image_hash,
        "image_unchanged_seconds": round(unchanged_seconds, 3),
        "stale_after_seconds": stale_after_seconds,
    }
    next_state = {
        **state,
        "last_capture_at": captured_at.isoformat(),
        "last_image_sha256": image_hash,
        "same_image_since": same_since.isoformat(),
        "last_capture_status": status,
        "last_event_id_seen": event_id,
    }
    return CaptureDecision(status=status, reason=reason, context=context, next_state=next_state)


def commit_completed_capture(state: Mapping[str, Any], context: Mapping[str, Any]) -> dict[str, Any]:
    completed = dict(state)
    completed["last_completed_closed_k"] = context["expected_latest_closed_k"]
    completed["last_completed_event_id"] = context["event_id"]
    completed["last_completed_at"] = datetime.now(TAIPEI).isoformat()
    return completed


def _append_jsonl(path: Path, payload: object) -> None:
    path = _ensure_experiment_output(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def observe_clock(
    *,
    cycles: int,
    interval_seconds: float,
    align_second: int | None,
    runtime_dir: Path,
    capture_dir: Path | None,
) -> dict[str, object]:
    if cycles <= 0:
        raise ValueError("cycles must be positive")
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")
    runtime_dir = _ensure_experiment_output(runtime_dir)
    log_path = runtime_dir / "clock_events.jsonl"
    events: list[dict[str, object]] = []
    next_monotonic = time.monotonic()

    for index in range(cycles):
        if align_second is not None:
            target = next_minute_slot(datetime.now().astimezone(), align_second)
            delay = max(0.0, (target - datetime.now().astimezone()).total_seconds())
            time.sleep(delay)
            actual = datetime.now().astimezone()
            drift_ms = (actual - target).total_seconds() * 1000.0
            scheduled_at = target.isoformat()
        else:
            if index:
                next_monotonic += interval_seconds
                time.sleep(max(0.0, next_monotonic - time.monotonic()))
            actual = datetime.now().astimezone()
            drift_ms = max(0.0, (time.monotonic() - next_monotonic) * 1000.0)
            scheduled_at = actual.isoformat()

        image = newest_png(capture_dir)
        event: dict[str, object] = {
            "cycle": index + 1,
            "scheduled_at": scheduled_at,
            "triggered_at": actual.isoformat(),
            "drift_ms": round(drift_ms, 3),
            "image_path": str(image.resolve()) if image else None,
            "image_mtime": (
                datetime.fromtimestamp(image.stat().st_mtime).astimezone().isoformat()
                if image
                else None
            ),
        }
        events.append(event)
        _append_jsonl(log_path, event)
        print(json.dumps(event, ensure_ascii=False), flush=True)

    drifts = [float(event["drift_ms"]) for event in events]
    summary: dict[str, object] = {
        "cycles": cycles,
        "interval_seconds": interval_seconds,
        "align_second": align_second,
        "average_drift_ms": round(sum(drifts) / len(drifts), 3),
        "maximum_drift_ms": round(max(drifts), 3),
        "log_path": str(log_path.resolve()),
    }
    _atomic_write_json(runtime_dir / "clock_summary.json", summary)
    return summary


def build_isolated_analysis_prompt(original_prompt: str, prompt_sha256: str) -> str:
    return f"""你正在執行台指期監控的隔離一致性測試。

【本輪安全限制，優先於下方原始提示詞中的執行與傳輸指令】
- 只分析已附加的單張圖表圖片。
- 不得呼叫任何工具、命令、瀏覽器、網路、Telegram、bridge 或 resume guard。
- 不得讀取、建立、修改或刪除任何檔案，也不得傳送任何訊息。
- 不得啟動或變更任何 automation。
- 下方原始提示詞保持逐字不變，只使用其中的盤勢分析、風險管理與輸出格式規則。
- 將結果視為 dry-run，不得建立真實或模擬成交狀態；若圖片不足，直接明寫資料不足。

ORIGINAL_PROMPT_SHA256={prompt_sha256}
<ORIGINAL_PROMPT>
{original_prompt}
</ORIGINAL_PROMPT>
"""


def build_structured_analysis_prompt(
    original_prompt: str,
    prompt_sha256: str,
    context: Mapping[str, Any],
) -> str:
    context_json = json.dumps(context, ensure_ascii=False, indent=2)
    return f"""你正在執行台指期監控的隔離結構化一致性測試。

【本輪安全與資料限制，優先於下方原始提示詞】
- 只分析已附加的單張純圖表圖片。
- 不得呼叫任何工具、命令、瀏覽器、網路、Telegram、bridge 或 resume guard。
- 不得讀取、建立、修改或刪除任何檔案，也不得傳送任何訊息。
- 不得啟動或變更任何 automation。
- 下方原始提示詞保持逐字不變，只採用其中的盤勢分析與風險管理規則。
- CAPTURE_CONTEXT 的時間由本機排程計算，是本輪唯一權威時間來源。不得從圖面猜測或改寫最新已收盤 K 時間。
- 不得在文字欄位內重複或聲稱另一個最新已收盤 K 時間；只分析該時間對應的圖面價格與盤勢。
- latest_closed_k_price_estimate 只能寫價格／價格區間與「圖面估計」，不得包含日期、時間或時區。
- 免責聲明由固定 renderer 統一加入，不得放入任何 JSON 欄位。
- 圖面無法精確確認的價格必須寫「圖面估計」並使用區間。
- 本輪為 dry-run，不得建立真實或模擬成交狀態。
- 最終只回傳符合 output schema 的 JSON；不得輸出 Markdown、XML、前言、結語或程式碼圍欄。

ORIGINAL_PROMPT_SHA256={prompt_sha256}
<CAPTURE_CONTEXT>
{context_json}
</CAPTURE_CONTEXT>
<ORIGINAL_PROMPT>
{original_prompt}
</ORIGINAL_PROMPT>
"""


def _save_capture_decision(
    *,
    runtime_dir: Path,
    state_file: Path,
    decision: CaptureDecision,
) -> None:
    _atomic_write_json(runtime_dir / "capture_context.json", decision.context)
    _atomic_write_json(state_file, decision.next_state)


def run_codex_structured_dry_run(
    *,
    prompt_snapshot: Path,
    image_path: Path,
    runtime_dir: Path,
    state_file: Path,
    schema_path: Path,
    captured_at: datetime,
    stale_after_seconds: float,
    timeout_seconds: float,
    execute: bool,
) -> dict[str, object]:
    runtime_dir = _ensure_experiment_output(runtime_dir)
    state_file = _ensure_experiment_output(state_file)
    schema_path = schema_path.resolve()
    if HERE.resolve() not in schema_path.parents:
        raise ValueError("schema must stay inside isolated experiment directory")
    if not schema_path.is_file():
        raise FileNotFoundError(schema_path)

    previous_state = _load_json_object(state_file)
    decision = decide_capture(
        image_path=image_path,
        captured_at=captured_at,
        previous_state=previous_state,
        stale_after_seconds=stale_after_seconds,
    )
    _save_capture_decision(
        runtime_dir=runtime_dir,
        state_file=state_file,
        decision=decision,
    )

    markdown_path = runtime_dir / "analysis_message.md"
    output_path = runtime_dir / "analysis.json"
    if decision.status != "FRESH":
        message = render_unavailable_markdown(decision.context, decision.reason)
        _atomic_write_text(markdown_path, message)
        result = {
            "execute": execute,
            "skipped": True,
            "skip_status": decision.status,
            "skip_reason": decision.reason,
            "capture_context": decision.context,
            "output_path": str(output_path.resolve()),
            "markdown_path": str(markdown_path.resolve()),
            "state_file": str(state_file.resolve()),
            "telegram_enabled": False,
            "returncode": 0,
        }
        _atomic_write_json(runtime_dir / "structured_dry_run_result.json", result)
        return result

    original_prompt = prompt_snapshot.read_text(encoding="utf-8")
    prompt_hash = sha256_bytes(original_prompt.encode("utf-8"))
    effective_prompt = build_structured_analysis_prompt(
        original_prompt,
        prompt_hash,
        decision.context,
    )
    isolated_workspace = runtime_dir / "isolated_workspace"
    isolated_workspace.mkdir(parents=True, exist_ok=True)
    command: Sequence[str] = (
        "codex",
        "exec",
        "--ephemeral",
        "--sandbox",
        "read-only",
        "--ignore-user-config",
        "--ignore-rules",
        "-C",
        str(isolated_workspace),
        "-i",
        str(image_path.resolve()),
        "--output-schema",
        str(schema_path),
        "--output-last-message",
        str(output_path.resolve()),
        "-",
    )
    preview: dict[str, object] = {
        "execute": execute,
        "skipped": False,
        "command": list(command),
        "prompt_sha256": prompt_hash,
        "prompt_snapshot": str(prompt_snapshot.resolve()),
        "schema_path": str(schema_path),
        "capture_context": decision.context,
        "output_path": str(output_path.resolve()),
        "markdown_path": str(markdown_path.resolve()),
        "state_file": str(state_file.resolve()),
        "telegram_enabled": False,
    }
    if not execute:
        _atomic_write_json(runtime_dir / "structured_dry_run_preview.json", preview)
        return preview

    started = time.perf_counter()
    completed = subprocess.run(
        command,
        input=effective_prompt,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=timeout_seconds,
        check=False,
    )
    duration = time.perf_counter() - started
    result: dict[str, object] = {
        **preview,
        "returncode": completed.returncode,
        "duration_seconds": round(duration, 3),
        "stdout_chars": len(completed.stdout),
        "stderr_chars": len(completed.stderr),
        "completed_at": datetime.now(TAIPEI).isoformat(),
    }
    _atomic_write_text(runtime_dir / "codex_stderr.txt", completed.stderr)

    if completed.returncode == 0:
        raw_payload = json.loads(output_path.read_text(encoding="utf-8"))
        payload = validate_analysis_payload(raw_payload)
        markdown = render_analysis_markdown(payload, decision.context)
        _atomic_write_text(markdown_path, markdown)
        completed_state = commit_completed_capture(decision.next_state, decision.context)
        _atomic_write_json(state_file, completed_state)
        result["schema_valid"] = True
        result["message_sha256"] = sha256_bytes(markdown.encode("utf-8"))
        result["completed_event_id"] = decision.context["event_id"]
    else:
        result["schema_valid"] = False

    _atomic_write_json(runtime_dir / "structured_dry_run_result.json", result)
    return result


def run_codex_dry_run(
    *,
    prompt_snapshot: Path,
    image_path: Path,
    runtime_dir: Path,
    timeout_seconds: float,
    execute: bool,
) -> dict[str, object]:
    if not image_path.is_file():
        raise FileNotFoundError(image_path)
    runtime_dir = _ensure_experiment_output(runtime_dir)
    original_prompt = prompt_snapshot.read_text(encoding="utf-8")
    prompt_hash = sha256_bytes(original_prompt.encode("utf-8"))
    effective_prompt = build_isolated_analysis_prompt(original_prompt, prompt_hash)
    isolated_workspace = runtime_dir / "isolated_workspace"
    isolated_workspace.mkdir(parents=True, exist_ok=True)
    output_path = runtime_dir / "codex_last_message.md"
    command: Sequence[str] = (
        "codex",
        "exec",
        "--ephemeral",
        "--sandbox",
        "read-only",
        "--ignore-user-config",
        "--ignore-rules",
        "-C",
        str(isolated_workspace),
        "-i",
        str(image_path.resolve()),
        "--output-last-message",
        str(output_path.resolve()),
        "-",
    )
    preview = {
        "execute": execute,
        "command": list(command),
        "prompt_sha256": prompt_hash,
        "prompt_snapshot": str(prompt_snapshot.resolve()),
        "image_path": str(image_path.resolve()),
        "output_path": str(output_path.resolve()),
        "telegram_enabled": False,
    }
    if not execute:
        _atomic_write_json(runtime_dir / "codex_dry_run_preview.json", preview)
        return preview

    started = time.perf_counter()
    completed = subprocess.run(
        command,
        input=effective_prompt,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=timeout_seconds,
        check=False,
    )
    duration = time.perf_counter() - started
    result: dict[str, object] = {
        **preview,
        "returncode": completed.returncode,
        "duration_seconds": round(duration, 3),
        "stdout_chars": len(completed.stdout),
        "stderr_chars": len(completed.stderr),
        "completed_at": datetime.now().astimezone().isoformat(),
    }
    _atomic_write_json(runtime_dir / "codex_dry_run_result.json", result)
    _atomic_write_text(runtime_dir / "codex_stderr.txt", completed.stderr)
    return result


def _path(value: str) -> Path:
    return Path(value).expanduser()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Isolated clock and Codex dry-run probe. Never sends Telegram."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    snapshot_parser = subparsers.add_parser("snapshot")
    snapshot_parser.add_argument("--automation-file", type=_path, default=DEFAULT_AUTOMATION_FILE)
    snapshot_parser.add_argument("--runtime-dir", type=_path, default=DEFAULT_RUNTIME_DIR)

    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument(
        "--manifest",
        type=_path,
        default=DEFAULT_RUNTIME_DIR / "protected_manifest.json",
    )

    observe_parser = subparsers.add_parser("observe")
    observe_parser.add_argument("--cycles", type=int, default=10)
    observe_parser.add_argument("--interval-seconds", type=float, default=60.0)
    observe_parser.add_argument("--align-second", type=int)
    observe_parser.add_argument("--runtime-dir", type=_path, default=DEFAULT_RUNTIME_DIR)
    observe_parser.add_argument("--capture-dir", type=_path)

    codex_parser = subparsers.add_parser("codex-dry-run")
    codex_parser.add_argument(
        "--prompt-snapshot",
        type=_path,
        default=DEFAULT_RUNTIME_DIR / "prompt_snapshot.txt",
    )
    codex_parser.add_argument("--image", type=_path, required=True)
    codex_parser.add_argument("--runtime-dir", type=_path, default=DEFAULT_RUNTIME_DIR)
    codex_parser.add_argument("--timeout-seconds", type=float, default=180.0)
    codex_parser.add_argument("--execute", action="store_true")

    structured_parser = subparsers.add_parser("structured-dry-run")
    structured_parser.add_argument(
        "--prompt-snapshot",
        type=_path,
        default=DEFAULT_RUNTIME_DIR / "prompt_snapshot.txt",
    )
    structured_parser.add_argument("--image", type=_path, required=True)
    structured_parser.add_argument("--runtime-dir", type=_path, default=DEFAULT_RUNTIME_DIR)
    structured_parser.add_argument("--state-file", type=_path)
    structured_parser.add_argument("--schema", type=_path, default=DEFAULT_SCHEMA_FILE)
    structured_parser.add_argument("--captured-at")
    structured_parser.add_argument("--stale-after-seconds", type=float, default=90.0)
    structured_parser.add_argument("--timeout-seconds", type=float, default=180.0)
    structured_parser.add_argument("--execute", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "snapshot":
            result = snapshot_prompt(args.automation_file, args.runtime_dir)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "verify":
            ok, changes = verify_manifest(args.manifest)
            print(json.dumps({"ok": ok, "changes": changes}, ensure_ascii=False, indent=2))
            return 0 if ok else 2
        if args.command == "observe":
            result = observe_clock(
                cycles=args.cycles,
                interval_seconds=args.interval_seconds,
                align_second=args.align_second,
                runtime_dir=args.runtime_dir,
                capture_dir=args.capture_dir,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "codex-dry-run":
            result = run_codex_dry_run(
                prompt_snapshot=args.prompt_snapshot,
                image_path=args.image,
                runtime_dir=args.runtime_dir,
                timeout_seconds=args.timeout_seconds,
                execute=args.execute,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return int(result.get("returncode", 0))
        if args.command == "structured-dry-run":
            runtime_dir = _ensure_experiment_output(args.runtime_dir)
            state_file = args.state_file or (runtime_dir / "monitor_state.json")
            result = run_codex_structured_dry_run(
                prompt_snapshot=args.prompt_snapshot,
                image_path=args.image,
                runtime_dir=runtime_dir,
                state_file=state_file,
                schema_path=args.schema,
                captured_at=parse_capture_time(args.captured_at),
                stale_after_seconds=args.stale_after_seconds,
                timeout_seconds=args.timeout_seconds,
                execute=args.execute,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return int(result.get("returncode", 0))
    except subprocess.TimeoutExpired as exc:
        print(
            json.dumps(
                {"ok": False, "error": "codex_timeout", "timeout_seconds": exc.timeout},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 3
    except Exception as exc:
        print(
            json.dumps(
                {"ok": False, "error": type(exc).__name__, "message": str(exc)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
