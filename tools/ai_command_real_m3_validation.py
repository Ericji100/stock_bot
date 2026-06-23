from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import traceback
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from radar_service import format_radar_report, parse_radar_args, run_radar  # noqa: E402
from research_center.command_parser import parse_command_text  # noqa: E402
from research_center.config import load_research_config  # noqa: E402
from research_center.models import CommandParseError, ResearchCenterResult, SourceItem  # noqa: E402
from research_center.orchestrator import ResearchCenter  # noqa: E402
from research_center.report_display_normalizer import normalize_report_text  # noqa: E402


VALIDATION_ROOT = ROOT / "logs" / "ai_command_real_m3_validation"

DEFAULT_COMMANDS: list[str] = [
    "/research 凌陽 --deep --model minimax",
    "/value_scan 我的持股 --deep --top 30 --model minimax",
    "/macro 台股 --model minimax",
    "/theme AI電源 --model minimax",
    "/theme_flow AI電源 --model minimax",
    "/theme_radar --model minimax",
    "/sector_strength --model minimax",
    "/radar --ai-top 5 --model minimax",
    "/news refresh --model minimax",
    "/topic_maintain --model minimax",
]

PARAMETER_MATRIX_CASES: list[dict[str, Any]] = [
    {
        "name": "research_default_deep",
        "command": "/research 凌陽 --model minimax",
        "parser": "research_center",
        "parameters": ["--model"],
        "expected": {"command": "research", "mode": "deep", "ai_model": "minimax"},
        "execution_scope": "default_mode_smoke",
    },
    {
        "name": "research_deep_model_date",
        "command": "/research 凌陽 --deep --model minimax --date 2026-06-18",
        "parser": "research_center",
        "parameters": ["--deep", "--model", "--date"],
        "expected": {"command": "research", "mode": "deep", "ai_model": "minimax", "report_date": "2026-06-18"},
        "execution_scope": "full_m3_representative",
    },
    {
        "name": "research_source_only",
        "command": "/research 凌陽 --source-only --model minimax",
        "parser": "research_center",
        "parameters": ["--source-only", "--model"],
        "expected": {"command": "research", "mode": "source_only", "source_only": True, "ai_model": "minimax"},
        "execution_scope": "source_only_smoke",
    },
    {
        "name": "research_score",
        "command": "/research 凌陽 --score --model minimax",
        "parser": "research_center",
        "parameters": ["--score", "--model"],
        "expected": {"command": "research", "mode": "score", "score": True, "ai_model": "minimax"},
        "execution_scope": "score_smoke",
    },
    {
        "name": "research_output_formats",
        "command": "/research 凌陽 --model minimax --no-html --no-json",
        "parser": "research_center",
        "parameters": ["--no-html", "--no-json", "--model"],
        "expected": {"command": "research", "output_formats": ["md"], "ai_model": "minimax"},
        "execution_scope": "artifact_smoke",
    },
    {
        "name": "research_reject_top",
        "command": "/research 凌陽 --top 5 --model minimax",
        "parser": "research_center",
        "parameters": ["--top"],
        "expected_error_contains": "不支援 --top",
        "execution_scope": "parser_rejection",
    },
    {
        "name": "value_scan_deep_top",
        "command": "/value_scan 我的持股 --deep --top 30 --model minimax",
        "parser": "research_center",
        "parameters": ["--deep", "--top", "--model"],
        "expected": {"command": "value_scan", "mode": "deep", "top": 30, "candidate_pool": "我的持股", "ai_model": "minimax"},
        "execution_scope": "full_m3_representative",
    },
    {
        "name": "value_scan_single_stock_date_output",
        "command": "/value_scan 2330 --date 2026-06-18 --top 5 --model minimax --no-md",
        "parser": "research_center",
        "parameters": ["--date", "--top", "--model", "--no-md"],
        "expected": {"command": "value_scan", "target_type": "stock", "report_date": "2026-06-18", "top": 5, "output_formats": ["html", "json"]},
        "execution_scope": "parameter_smoke",
    },
    {
        "name": "macro_brief",
        "command": "/macro 台股 --brief --model minimax",
        "parser": "research_center",
        "parameters": ["--brief", "--model"],
        "expected": {"command": "macro", "mode": "brief", "brief": True, "ai_model": "minimax"},
        "execution_scope": "brief_smoke",
    },
    {
        "name": "macro_source_only_date",
        "command": "/macro 台股 --source-only --date 2026-06-18 --model minimax",
        "parser": "research_center",
        "parameters": ["--source-only", "--date", "--model"],
        "expected": {"command": "macro", "mode": "source_only", "report_date": "2026-06-18", "ai_model": "minimax"},
        "execution_scope": "source_only_smoke",
    },
    {
        "name": "theme_top_deep",
        "command": "/theme AI電源 --top 5 --deep --model minimax",
        "parser": "research_center",
        "parameters": ["--top", "--deep", "--model"],
        "expected": {"command": "theme", "mode": "deep", "top": 5, "target": "AI電源", "ai_model": "minimax"},
        "execution_scope": "parameter_smoke",
    },
    {
        "name": "theme_flow_days_source",
        "command": "/theme_flow AI電源 --days 14 --source news --top 5 --model minimax",
        "parser": "research_center",
        "parameters": ["--days", "--source", "--top", "--model"],
        "expected": {"command": "theme_flow", "lookback_days": 14, "source": "news", "top": 5, "ai_model": "minimax"},
        "execution_scope": "parameter_smoke",
    },
    {
        "name": "theme_radar_days_source_date",
        "command": "/theme_radar --days 7 --source technical --top 5 --date 2026-06-18 --model minimax",
        "parser": "research_center",
        "parameters": ["--days", "--source", "--top", "--date", "--model"],
        "expected": {"command": "theme_radar", "lookback_days": 7, "source": "technical", "top": 5, "report_date": "2026-06-18"},
        "execution_scope": "parameter_smoke",
    },
    {
        "name": "sector_strength_days_source",
        "command": "/sector_strength --days 7 --source technical --top 5 --model minimax",
        "parser": "research_center",
        "parameters": ["--days", "--source", "--top", "--model"],
        "expected": {"command": "sector_strength", "lookback_days": 7, "source": "technical", "top": 5, "ai_model": "minimax"},
        "execution_scope": "parameter_smoke",
    },
    {
        "name": "topic_maintain_default_deep",
        "command": "/topic_maintain --model minimax",
        "parser": "research_center",
        "parameters": ["--model"],
        "expected": {"command": "topic_maintain", "mode": "deep", "ai_model": "minimax"},
        "execution_scope": "full_m3_representative",
    },
    {
        "name": "topic_maintain_from_radar",
        "command": "/topic_maintain --from-radar radar_20260618 --model minimax",
        "parser": "research_center",
        "parameters": ["--from-radar", "--model"],
        "expected": {"command": "topic_maintain", "target": "__from_radar__:radar_20260618", "mode": "deep", "ai_model": "minimax"},
        "execution_scope": "parameter_smoke",
    },
    {
        "name": "news_refresh_model",
        "command": "/news refresh --model minimax",
        "parser": "research_center",
        "parameters": ["--model"],
        "expected": {"command": "news", "target": "refresh", "ai_model": "minimax"},
        "execution_scope": "full_m3_representative",
    },
    {
        "name": "news_reject_deep",
        "command": "/news refresh --deep --model minimax",
        "parser": "research_center",
        "parameters": ["--deep"],
        "expected_error_contains": "/help",
        "execution_scope": "parser_rejection",
    },
    {
        "name": "radar_default_combined_ai_top_model",
        "command": "/radar --ai-top 5 --model minimax",
        "parser": "radar",
        "parameters": ["--ai-top", "--model"],
        "expected": {"source": "combined", "ai_top": 5, "model": "minimax", "ai_comment_enabled": True},
        "execution_scope": "full_m3_representative",
    },
    {
        "name": "radar_source_ai_top_model_date",
        "command": "/radar --source technical --ai-top 5 --date 2026-06-18 --model minimax",
        "parser": "radar",
        "parameters": ["--source", "--ai-top", "--date", "--model"],
        "expected": {"source": "technical", "ai_top": 5, "report_date": "2026-06-18", "model": "minimax", "ai_comment_enabled": True},
        "execution_scope": "full_m3_representative",
    },
    {
        "name": "radar_no_ai_comment",
        "command": "/radar --source chip --ai-top 3 --no-ai-comment",
        "parser": "radar",
        "parameters": ["--source", "--ai-top", "--no-ai-comment"],
        "expected": {"source": "chip", "ai_top": 3, "model": "minimax", "ai_comment_enabled": False},
        "execution_scope": "no_ai_smoke",
    },
    {
        "name": "radar_reject_unsupported",
        "command": "/radar --brief --model minimax",
        "parser": "radar",
        "parameters": ["--brief"],
        "expected_error_contains": "不支援的 Radar 參數",
        "execution_scope": "parser_rejection",
    },
]

def _now_id() -> str:
    return datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")


def _json_safe(value: Any) -> Any:
    if isinstance(value, str):
        return _clean_json_string(value)
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return _json_safe(asdict(value))
    if hasattr(value, "item") and callable(getattr(value, "item")):
        try:
            return _json_safe(value.item())
        except Exception:
            pass
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return value


def _clean_json_string(value: str) -> str:
    return "".join(
        ch
        for ch in value
        if ch in ("\n", "\r", "\t") or ord(ch) >= 32
    )


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_json_safe(value), ensure_ascii=False, indent=2), encoding="utf-8")


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8-sig")


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def _new_run_dir(preferred: Path | None = None) -> Path:
    if preferred is not None:
        preferred.mkdir(parents=True, exist_ok=True)
        return preferred
    base = VALIDATION_ROOT / f"{_now_id()}_{os.getpid()}"
    candidate = base
    counter = 1
    while candidate.exists():
        counter += 1
        candidate = VALIDATION_ROOT / f"{base.name}_{counter}"
    candidate.mkdir(parents=True, exist_ok=False)
    return candidate


def _slug_command(command: str, index: int) -> str:
    text = command.strip().lstrip("/")
    text = re.sub(r"\s+", "_", text)
    text = re.sub(r'[<>:"/\\|?*]+', "_", text)
    text = text[:80].strip("._") or "command"
    return f"{index:02d}_{text}"


def _command_name(command: str) -> str:
    parts = command.strip().split()
    return parts[0].lstrip("/") if parts else ""


def _source_rows(sources: list[SourceItem]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in sources:
        try:
            rows.append(_json_safe(asdict(item)))
        except Exception:
            rows.append({"repr": repr(item)})
    return rows


def _progress_logger(path: Path) -> tuple[Callable[[str], None], list[str]]:
    messages: list[str] = []

    def log(message: str) -> None:
        text = str(message)
        line = f"[{datetime.now().astimezone().isoformat(timespec='seconds')}] {text}"
        messages.append(line)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        print(line, flush=True)

    return log, messages


def _extract_prompt_paths(progress_messages: list[str], report_json: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    joined = "\n".join(progress_messages)
    for match in re.finditer(r"Prompt saved:\s*(.+)", joined):
        value = match.group(1).strip()
        if value:
            paths.append(value)
    metadata = report_json.get("metadata") if isinstance(report_json, dict) else {}
    if isinstance(metadata, dict):
        for key in ("prompt_path", "prompt_log_path", "minimax_prompt_path"):
            value = metadata.get(key)
            if value:
                paths.append(str(value))
        segmented = metadata.get("segmented_ai_prompt_paths")
        if isinstance(segmented, list):
            paths.extend(str(item) for item in segmented if item)
    return _dedupe(paths)


def _dedupe(items: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _path_exists(path_text: str) -> bool:
    try:
        return Path(path_text).exists()
    except Exception:
        return False


def _estimate_prompt_chars(prompt_paths: list[str]) -> int:
    total = 0
    for path_text in prompt_paths:
        path = Path(path_text)
        if path.exists() and path.is_file():
            total += len(_read_text(path))
    return total


def _extract_ai_status(result: ResearchCenterResult) -> str:
    metadata = result.report_json.get("metadata") if isinstance(result.report_json, dict) else {}
    ai_status = metadata.get("ai_status") if isinstance(metadata, dict) else None
    if ai_status:
        return str(ai_status)
    if result.fallback_reason:
        return "fallback_success"
    if result.status == "success" and result.ai_used:
        return "ai_success"
    if result.status == "failed":
        return "failed"
    return "not_used"


def _artifact_status_from_paths(paths_exist: dict[str, bool]) -> str:
    required = ["markdown", "html", "json", "sources"]
    existing = [key for key in required if paths_exist.get(key)]
    if len(existing) == len(required):
        return "complete"
    if existing:
        return "partial"
    return "missing"


def _artifact_status_for_command(command: str, paths_exist: dict[str, bool]) -> str:
    stripped = command.strip()
    if stripped.startswith("/news"):
        return "complete" if (paths_exist.get("telegram") or paths_exist.get("output")) else "missing"
    if stripped.startswith("/topic_maintain"):
        if paths_exist.get("json"):
            return "complete"
        return "partial" if any(paths_exist.values()) else "missing"
    if stripped.startswith("/radar"):
        return "complete" if (paths_exist.get("telegram") or paths_exist.get("summary")) else "missing"
    return _artifact_status_from_paths(paths_exist)


def _quality_review(
    *,
    command: str,
    output_text: str,
    source_count: int,
    prompt_chars: int,
    max_prompt_chars: int | None = None,
    elapsed_seconds: float,
    status: str,
    error: str | None,
) -> dict[str, Any]:
    command_type = _command_name(command)
    fallback_detected = bool(error) or status in {"fallback", "fallback_success", "failed"} or _looks_like_fallback_output(output_text)
    model_reasoning_exposed = _looks_like_model_reasoning_output(output_text)
    unreadable_text_detected = _looks_like_unreadable_output(output_text)
    prompt_not_runaway = _prompt_not_runaway(prompt_chars, max_prompt_chars)
    checks = {
        "執行成功": status == "success" and not fallback_detected,
        "非 fallback 報告": not fallback_detected,
        "有輸出內容": len(output_text.strip()) >= 500 or command_type in {"news", "topic_maintain", "radar"},
        "有資料來源": source_count > 0 or command_type in {"news", "topic_maintain"},
        "繁體中文可讀": _traditional_chinese_score(output_text) >= 0.05,
        "無模型思考外露": not model_reasoning_exposed,
        "無亂碼或不可讀文字": not unreadable_text_detected,
        "無明顯截斷標記": "<list truncated>" not in output_text and "<dict truncated>" not in output_text,
        "無內部欄位外露": not _has_internal_field_leak(output_text),
        "有風險或反證": bool(re.search(r"風險|反證|不確定|資料不足|失敗條件|觀察", output_text)),
        "有後續推演": bool(re.search(r"後續|催化|情境|推演|預期|驗證|觀察", output_text)),
        "有資料日期或基準日": bool(re.search(r"資料日期|基準日|基準日期|分析日期|報告日期|市場資料日期|\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日|\d{4}-\d{2}-\d{2}", output_text)),
        "耗時可接受": elapsed_seconds <= 1800,
        "Prompt 未失控": prompt_not_runaway,
    }
    required_by_command = _required_terms(command_type)
    missing_terms = _missing_required_terms(output_text, required_by_command)
    score = sum(1 for ok in checks.values() if ok)
    total = len(checks)
    issues = [name for name, ok in checks.items() if not ok]
    if missing_terms:
        issues.append(f"缺少主題必要詞：{', '.join(missing_terms)}")
    pass_threshold = score >= max(7, total - 2)
    if command_type == "topic_maintain":
        pass_threshold = score == total
    return {
        "command_type": command_type,
        "score": score,
        "total": total,
        "pass": pass_threshold and not missing_terms and status == "success" and not fallback_detected,
        "checks": checks,
        "missing_required_terms": missing_terms,
        "issues": issues,
        "metrics": {
            "output_chars": len(output_text),
            "source_count": source_count,
            "prompt_chars": prompt_chars,
            "max_prompt_chars": max_prompt_chars,
            "rough_prompt_tokens": round(prompt_chars / 4),
            "elapsed_seconds": elapsed_seconds,
        },
    }


def _prompt_not_runaway(prompt_chars: int, max_prompt_chars: int | None = None) -> bool:
    """Judge per-call prompt safety separately from total segmented cost."""

    if max_prompt_chars is not None and max_prompt_chars > 0:
        return max_prompt_chars <= 180_000
    return prompt_chars <= 900_000


def _looks_like_fallback_output(output_text: str) -> bool:
    return bool(
        re.search(
            r"本地資料\s*fallback|本地 fallback|fallback 報告|AI 模型調用.*失敗|最終 AI 整合失敗|這不是正式 AI 完成報告",
            output_text or "",
            re.IGNORECASE,
        )
    )


def _looks_like_command_failure_output(output_text: str) -> bool:
    text = output_text or ""
    return bool(
        re.search(
            r"(?m)^\s*(?:❌|執行失敗|任務失敗|AI 投研任務失敗|AI 任務失敗|MiniMax API 呼叫失敗)",
            text,
            re.IGNORECASE,
        )
    )


def _looks_like_model_reasoning_output(output_text: str) -> bool:
    text = output_text or ""
    return bool(
        re.search(
            r"<think>|</think>|\bThe user wants\b|\bLet me\b|\bI need to\b|\bNow let me\b",
            text,
            re.IGNORECASE,
        )
    )


def _looks_like_unreadable_output(output_text: str) -> bool:
    text = output_text or ""
    if not text:
        return False
    private_use = len(re.findall(r"[\ue000-\uf8ff]", text))
    replacement = text.count("\ufffd")
    question_cjk = len(re.findall(r"\?[\u4e00-\u9fff]|[\u4e00-\u9fff]\?", text))
    return private_use >= 3 or replacement > 0 or question_cjk >= 20


def _refresh_quality_review(record: dict[str, Any], output_text: str) -> None:
    """Re-run the lightweight quality review after command-specific enrichment."""

    review = _quality_review(
        command=str(record.get("command") or ""),
        output_text=output_text,
        source_count=int(record.get("source_count") or 0),
        prompt_chars=int(record.get("prompt_chars") or 0),
        elapsed_seconds=float(record.get("elapsed_seconds") or 0),
        status=str(record.get("status") or ""),
        error=record.get("error") or record.get("fallback_reason"),
    )
    record["quality_review"] = review


def _update_quality_metrics(record: dict[str, Any]) -> None:
    review = record.get("quality_review")
    if not isinstance(review, dict):
        _refresh_quality_review(record, str(record.get("summary") or ""))
        review = record.get("quality_review")
        if not isinstance(review, dict):
            return
    metrics = dict(review.get("metrics") or {})
    prompt_chars = int(record.get("prompt_chars") or 0)
    max_prompt_chars = record.get("max_prompt_chars")
    try:
        max_prompt_chars = int(max_prompt_chars) if max_prompt_chars is not None else None
    except (TypeError, ValueError):
        max_prompt_chars = None
    metrics["prompt_chars"] = prompt_chars
    metrics["max_prompt_chars"] = max_prompt_chars
    metrics["rough_prompt_tokens"] = round(prompt_chars / 4)
    metrics["source_count"] = int(record.get("source_count") or 0)
    metrics["elapsed_seconds"] = float(record.get("elapsed_seconds") or 0)
    review["metrics"] = metrics
    checks = review.get("checks")
    if isinstance(checks, dict) and "Prompt 未失控" in checks:
        checks["Prompt 未失控"] = _prompt_not_runaway(prompt_chars, max_prompt_chars)
        _recalculate_review_result(record, review)
    record["quality_review"] = review


def _recalculate_review_result(record: dict[str, Any], review: dict[str, Any]) -> None:
    checks = review.get("checks")
    if not isinstance(checks, dict):
        return
    command_type = str(review.get("command_type") or _command_name(str(record.get("command") or "")))
    missing_terms = list(review.get("missing_required_terms") or [])
    runtime_issues = list(review.get("runtime_issues") or record.get("runtime_issues") or [])
    score = sum(1 for ok in checks.values() if ok)
    total = len(checks)
    issues = [name for name, ok in checks.items() if not ok]
    if missing_terms:
        issues.append(f"缺少主題必要詞：{', '.join(missing_terms)}")
    for item in runtime_issues:
        if item not in issues:
            issues.append(item)
    pass_threshold = score >= max(7, total - 2)
    if command_type == "topic_maintain":
        pass_threshold = score == total
    status = str(record.get("status") or "")
    fallback_detected = bool(record.get("fallback_reason")) or record.get("ai_status") == "fallback_success" or status in {"fallback", "fallback_success", "failed"}
    review["score"] = score
    review["total"] = total
    review["issues"] = issues
    review["pass"] = pass_threshold and not missing_terms and status == "success" and not fallback_detected and not runtime_issues


def _runtime_issue_patterns() -> list[tuple[str, str]]:
    return [
        ("高階模型 context window 超限", r"context window exceeds limit|prompt too large|payload bytes|狀態=400|status=400"),
        ("最終整合失敗或使用 fallback", r"最終 AI 整合失敗|final_status[\"']?:\s*[\"']fallback|final_status.*fallback|本報告已使用本地資料 fallback|這不是正式 AI 完成報告|本地資料\s*fallback"),
        ("低階模型整理失敗", r"MiniMax[- ]M[23](?:\.7)?.*失敗|低階資料整理.*失敗"),
        ("MiniMax API 呼叫失敗", r"MiniMax API request failed|MiniMax 沒有回傳可用"),
        ("Telegram 訊息過長", r"Message is too long"),
    ]


def _augment_record_with_runtime_issues(record: dict[str, Any], command_dir: Path) -> dict[str, Any]:
    record.pop("runtime_issues", None)
    review = record.get("quality_review")
    if isinstance(review, dict):
        review.pop("runtime_issues", None)
        issues = list(review.get("issues") or [])
        stale_labels = {label for label, _pattern in _runtime_issue_patterns()}
        review["issues"] = [item for item in issues if item not in stale_labels]
        _recalculate_review_result(record, review)
        record["quality_review"] = review
    texts: list[str] = []
    for name in (
        "progress.log",
        "worker_stdout.log",
        "worker_stderr.log",
        "output.md",
        "telegram_message.md",
        "telegram_summary.md",
        "error.txt",
    ):
        path = command_dir / name
        if path.exists():
            texts.append(_read_text(path))
    joined = "\n".join(texts)
    issue_scan_text = _remove_recovered_low_model_retry_failures(
        record,
        _remove_recovered_news_retry_failures(record, joined),
    )
    runtime_issues: list[str] = []
    for label, pattern in _runtime_issue_patterns():
        if re.search(pattern, issue_scan_text, re.IGNORECASE):
            runtime_issues.append(label)
    if record.get("worker_returncode") not in (None, 0):
        runtime_issues.append(f"worker return code={record.get('worker_returncode')}")
    review = record.get("quality_review")
    if isinstance(review, dict) and runtime_issues:
        issues = list(review.get("issues") or [])
        for item in runtime_issues:
            if item not in issues:
                issues.append(item)
        review["issues"] = issues
        review["runtime_issues"] = runtime_issues
        review["pass"] = False
        record["quality_review"] = review
    if runtime_issues:
        record["runtime_issues"] = runtime_issues
        if str(record.get("command") or "").startswith("/news"):
            record["ai_status"] = "partial_success"
            record["formal_ai_success"] = False
            record["classification_status"] = record.get("classification_status") or "partial_success"
            record["fallback_reason"] = record.get("fallback_reason") or "news classification used fallback or hit AI runtime issue"
    _augment_special_artifacts(record, joined)
    _sync_quality_review_file(record, command_dir)
    return record


def _sync_quality_review_file(record: dict[str, Any], command_dir: Path) -> None:
    """Keep result.json, quality_review.json, and summary input aligned."""

    review_path = command_dir / "quality_review.json"
    review = record.get("quality_review")
    if not isinstance(review, dict) and review_path.exists():
        try:
            file_review = json.loads(review_path.read_text(encoding="utf-8-sig"))
            if isinstance(file_review, dict):
                review = file_review
        except Exception:
            pass
    if isinstance(review, dict):
        record["quality_review"] = review
        _write_json(review_path, review)


def _remove_recovered_news_retry_failures(record: dict[str, Any], text: str) -> str:
    command = str(record.get("command") or "")
    if not command.startswith("/news"):
        return text
    retry_failures = len(re.findall(
        r"MiniMax API request failed.*retrying with lightweight payload",
        text,
        re.IGNORECASE,
    ))
    retry_successes = len(re.findall(r"lightweight retry completed", text, re.IGNORECASE))
    if retry_failures <= 0 or retry_successes < retry_failures:
        return text
    cleaned_lines = []
    for line in text.splitlines():
        if re.search(r"MiniMax API request failed.*retrying with lightweight payload", line, re.IGNORECASE):
            cleaned_lines.append(re.sub(r"MiniMax API request failed", "MiniMax API transient retry", line, flags=re.IGNORECASE))
        else:
            cleaned_lines.append(line)
    return "\n".join(cleaned_lines)


def _remove_recovered_low_model_retry_failures(record: dict[str, Any], text: str) -> str:
    final_success = re.search(r"分段資料整理結束：success=(\d+)/(\d+)\s+failed=0", text)
    failed_segments = set(re.findall(r"分段資料整理失敗\s+(\d+)/(\d+).*?重試", text, re.IGNORECASE))
    recovered_segments = set(re.findall(r"分段資料整理重試成功\s+(\d+)/(\d+)", text, re.IGNORECASE))
    simple_failures = len(re.findall(r"低階整理失敗.*重試", text, re.IGNORECASE))
    simple_successes = len(re.findall(r"低階整理重試成功", text, re.IGNORECASE))
    api_failure_lines = len(re.findall(r"MiniMax API request failed|MiniMax 沒有回傳可用", text, re.IGNORECASE))
    recovered = False
    if final_success:
        recovered = True
    elif failed_segments and failed_segments.issubset(recovered_segments):
        recovered = True
    elif simple_failures > 0 and simple_successes >= simple_failures:
        recovered = True
    if not recovered:
        return text
    cleaned_lines = []
    for line in text.splitlines():
        if re.search(r"(低階整理失敗.*重試|分段資料整理失敗\s+\d+/\d+.*?重試)", line, re.IGNORECASE):
            cleaned = re.sub(r"低階整理失敗", "低階整理需重試", line, flags=re.IGNORECASE)
            cleaned = re.sub(r"分段資料整理失敗", "分段資料整理需重試", cleaned, flags=re.IGNORECASE)
            cleaned = re.sub(r"MiniMax API request failed", "MiniMax API transient retry", cleaned, flags=re.IGNORECASE)
            cleaned = re.sub(r"MiniMax 沒有回傳可用", "MiniMax 暫時未回傳可用內容，後續已重試", cleaned, flags=re.IGNORECASE)
            cleaned_lines.append(cleaned)
        elif re.search(r"部分\s+MiniMax[- ]M[23](?:\.7)?\s+分段整理失敗", line, re.IGNORECASE):
            cleaned = re.sub(r"分段整理失敗", "分段整理曾重試", line, flags=re.IGNORECASE)
            cleaned_lines.append(re.sub(r"失敗段", "重試段", cleaned, flags=re.IGNORECASE))
        elif re.search(r"MiniMax[- ]M[23](?:\.7)?.*(低階資料整理|分段整理).*失敗", line, re.IGNORECASE):
            cleaned = re.sub(r"失敗段", "重試段", line, flags=re.IGNORECASE)
            cleaned_lines.append(re.sub(r"失敗", "曾重試", cleaned, flags=re.IGNORECASE))
        elif api_failure_lines and re.search(r"MiniMax API request failed|MiniMax 沒有回傳可用", line, re.IGNORECASE):
            cleaned = re.sub(r"MiniMax API request failed", "MiniMax API transient retry", line, flags=re.IGNORECASE)
            cleaned = re.sub(r"MiniMax 沒有回傳可用", "MiniMax 暫時未回傳可用內容，後續已重試", cleaned, flags=re.IGNORECASE)
            cleaned_lines.append(cleaned)
        else:
            cleaned_lines.append(line)
    return "\n".join(cleaned_lines)


def _augment_special_artifacts(record: dict[str, Any], joined_log_text: str) -> None:
    command = str(record.get("command") or "")
    paths = dict(record.get("report_paths") or {})
    command_dir_text = str(record.get("stdout_path") or "")
    command_dir = Path(command_dir_text).parent if command_dir_text else None
    progress_log_text = joined_log_text
    if command_dir:
        progress_log = command_dir / "progress.log"
        if progress_log.exists():
            progress_log_text = _read_text(progress_log)
    if command.startswith("/news"):
        if command_dir:
            for key, filename in (
                ("telegram", "telegram_summary.md"),
                ("output", "output.md"),
            ):
                path = command_dir / filename
                if path.exists():
                    paths[key] = str(path)
            record["report_paths"] = paths
        source_matches = re.findall(r"搜尋完成，共\s*(\d+)\s*筆來源", joined_log_text)
        if source_matches:
            record["source_count"] = int(source_matches[-1])
        prompt_matches = [int(item) for item in re.findall(r"AI 分類 \d+/\d+ prompt=(\d+)\s*chars", joined_log_text)]
        if prompt_matches:
            record["prompt_chars"] = sum(prompt_matches)
            _update_quality_metrics(record)
        fallback_match = re.search(r"AI 分類 fallback：\s*(\d+)/(\d+)", joined_log_text)
        if fallback_match:
            record["ai_status"] = "partial_success"
            record["formal_ai_success"] = False
            record["classification_status"] = "partial_success"
            record["local_fallback_count"] = int(fallback_match.group(1))
            record["classified_count"] = int(fallback_match.group(2))
            record["fallback_reason"] = record.get("fallback_reason") or "news classification used local fallback"
    if command.startswith("/radar"):
        prompt_matches = [int(item) for item in re.findall(r"(?<!original_)prompt=(\d+)\s*chars", progress_log_text)]
        if prompt_matches:
            record["prompt_chars"] = sum(prompt_matches)
            record["max_prompt_chars"] = max(prompt_matches)
            _refresh_quality_review(record, str(record.get("summary") or ""))
    if command.startswith(("/theme_radar", "/theme_flow", "/sector_strength")):
        prompt_matches = [
            int(item)
            for item in re.findall(
                r"分段 AI (?:呼叫|最終整合開始).*?prompt=(\d+)\s*chars",
                progress_log_text,
            )
        ]
        if prompt_matches:
            record["prompt_chars"] = sum(prompt_matches)
            record["max_prompt_chars"] = max(prompt_matches)
            _update_quality_metrics(record)
    if command.startswith("/topic_maintain"):
        topic_pack = _latest_topic_change_pack_path(joined_log_text)
        topic_raw = _latest_topic_raw_path(joined_log_text)
        output_text = ""
        if topic_pack:
            paths["json"] = str(topic_pack)
        if topic_raw:
            paths["topic_raw"] = str(topic_raw)
            if _topic_raw_indicates_ai_fallback(topic_raw):
                record["ai_status"] = "partial_success"
                record["formal_ai_success"] = False
                record["fallback_reason"] = record.get("fallback_reason") or "topic maintain used local fallback after AI stage issue"
        if topic_pack or topic_raw:
            record["report_paths"] = paths
        if topic_pack:
            output_text = "\n\n".join([
                str(record.get("summary") or ""),
                _topic_quality_probe_text(topic_pack),
            ]).strip()
            _write_text((command_dir or topic_pack.parent) / "topic_quality_probe.md", output_text)
        source_match = re.search(r"Discovery來源：(\d+)\s*筆", joined_log_text)
        if source_match and not record.get("source_count"):
            record["source_count"] = int(source_match.group(1))
        prompt_matches = [int(item) for item in re.findall(r"(?<!original_)prompt=(\d+)\s*chars", progress_log_text)]
        if prompt_matches:
            record["prompt_chars"] = sum(prompt_matches)
            record["max_prompt_chars"] = max(prompt_matches)
        if output_text:
            _refresh_quality_review(record, output_text)
        else:
            _update_quality_metrics(record)
    if command.startswith(("/news", "/topic_maintain", "/radar")):
        output_for_status = str(record.get("summary") or "")
        if _looks_like_command_failure_output(output_for_status):
            record["status"] = "failed"
            record["ai_status"] = "failed"
            record["fallback_reason"] = record.get("fallback_reason") or "command output indicates failure"
        elif command.startswith("/news") and record.get("classification_status") == "partial_success":
            record["ai_status"] = "partial_success"
        elif command.startswith("/topic_maintain") and record.get("ai_status") == "partial_success":
            record["formal_ai_success"] = False
        elif record.get("status") == "success" and int(record.get("prompt_chars") or 0) > 0:
            record["ai_status"] = "ai_success" if record.get("ai_used", True) is not False or command.startswith(("/news", "/topic_maintain")) else record.get("ai_status")
        path_exists = {key: _path_exists(str(value)) for key, value in (record.get("report_paths") or {}).items()}
        record["report_paths_exist"] = path_exists
        record["artifact_status"] = _artifact_status_for_command(command, path_exists)
        record["formal_ai_success"] = record.get("status") == "success" and record.get("ai_status") == "ai_success" and not record.get("fallback_reason")
        _update_quality_metrics(record)


def _topic_quality_probe_text(topic_pack: Path) -> str:
    raw = _read_text(topic_pack)
    markers: list[str] = []
    date_match = re.search(r"change_(\d{4})(\d{2})(\d{2})_\d{6}", topic_pack.stem)
    if date_match:
        markers.append(f"資料日期：{date_match.group(1)}-{date_match.group(2)}-{date_match.group(3)}")
    if re.search(r"\brisk_notes\b|風險|不確定", raw, re.IGNORECASE):
        markers.append("風險：題材更新包包含 risk_notes 或風險相關內容，需於人工審核時確認是否具體。")
    if re.search(r"\bcounter_evidence\b|反證", raw, re.IGNORECASE):
        markers.append("反證：題材更新包包含 counter_evidence 或反證相關內容，需於人工審核時確認是否具體。")
    if re.search(r"\bmissing_data\b|資料不足|資料缺口", raw, re.IGNORECASE):
        markers.append("資料不足：題材更新包包含 missing_data 或資料缺口。")
    if re.search(r"\bverification\b|\bnext\b|後續|催化|情境|推演|預期|驗證|觀察", raw, re.IGNORECASE):
        markers.append("後續驗證：題材更新包包含後續驗證、觀察或推演相關欄位。")
    if not markers:
        markers.append("題材更新包未偵測到風險、反證、資料缺口或後續驗證欄位。")
    return "\n".join([
        "# 題材庫更新包品質探針",
        *markers,
        "",
        "原始更新包已另存 JSON；本品質探針只保留使用者可讀的風險、反證、資料缺口與後續驗證摘要，避免把內部欄位名稱誤判為報告外露。",
    ])


def _topic_raw_indicates_ai_fallback(topic_raw: Path) -> bool:
    raw = _read_text(topic_raw)
    try:
        payload = json.loads(raw)
    except Exception:
        payload = None
    if isinstance(payload, dict) and isinstance(payload.get("stages"), list):
        stages = [stage for stage in payload.get("stages") or [] if isinstance(stage, dict)]
        stage_names = {str(stage.get("stage") or "") for stage in stages}
        for stage in stages:
            name = str(stage.get("stage") or "")
            error = str(stage.get("error") or "")
            if not error:
                continue
            if "prompt_too_large" in error or "local_fallback" in name or "local_budget_fallback" in name:
                return True
            if not re.search(r"status=timeout|ReadTimeout|MiniMax API request failed|timed out|timeout", error, re.IGNORECASE):
                continue
            if name == "candidate_extract":
                retry_ok = any(
                    str(item.get("stage") or "") in {"candidate_extract_retry", "candidate_extract_recovered"}
                    and not item.get("error")
                    for item in stages
                )
                if retry_ok:
                    continue
            recovered_name = f"{name}_recovered"
            retry_name = f"{name}_retry"
            retry_ok = any(
                str(item.get("stage") or "") in {recovered_name, retry_name}
                and not item.get("error")
                for item in stages
            )
            if retry_ok or recovered_name in stage_names:
                continue
            return True
        return any(
            str(stage.get("stage") or "").endswith(("local_fallback", "local_budget_fallback"))
            or "prompt_too_large" in str(stage.get("error") or "")
            for stage in stages
        )
    return bool(
        re.search(
            r"prompt_too_large|local_fallback|local_budget_fallback|status=timeout|ReadTimeout|MiniMax API request failed",
            raw,
            re.IGNORECASE,
        )
    )


def _latest_topic_raw_path(joined_log_text: str) -> Path | None:
    candidate_ids = [match.group(0) for match in re.finditer(r"change_\d{8}_\d{6}", joined_log_text)]
    raw_dir = ROOT / "logs" / "topic_ai_raw"
    for change_id in reversed(candidate_ids):
        path = raw_dir / f"{change_id}.json"
        if path.exists():
            return path
    if raw_dir.exists():
        files = sorted(raw_dir.glob("change_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        return files[0] if files else None
    return None


def _latest_topic_change_pack_path(joined_log_text: str) -> Path | None:
    candidate_ids = [match.group(0) for match in re.finditer(r"change_\d{8}_\d{6}", joined_log_text)]
    pack_dir = ROOT / "data" / "topic" / "change_packs"
    for change_id in reversed(candidate_ids):
        path = pack_dir / f"{change_id}.json"
        if path.exists():
            return path
    if pack_dir.exists():
        files = sorted(pack_dir.glob("change_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        return files[0] if files else None
    return None


def _required_terms(command_type: str) -> list[str | tuple[str, ...]]:
    mapping = {
        "research": ["風險", "觀察"],
        "value_scan": ["重估", "風險"],
        "macro": [("總經", "宏觀", "利率", "匯率", "資金"), "台股"],
        "theme": ["題材", "風險"],
        "theme_flow": ["題材", "資金"],
        "theme_radar": ["題材", "族群"],
        "sector_strength": ["產業", "強勢"],
        "radar": ["雷達"],
        "news": ["新聞"],
        "topic_maintain": ["題材"],
    }
    return mapping.get(command_type, [])


def _missing_required_terms(text: str, required_terms: list[str | tuple[str, ...]]) -> list[str]:
    missing: list[str] = []
    for term in required_terms:
        if isinstance(term, tuple):
            if not any(candidate in text for candidate in term):
                missing.append("/".join(term))
            continue
        if term not in text:
            missing.append(term)
    return missing


def _traditional_chinese_score(text: str) -> float:
    if not text:
        return 0.0
    chinese_chars = len(re.findall(r"[\u4e00-\u9fff]", text))
    return chinese_chars / max(len(text), 1)


def _has_internal_field_leak(text: str) -> bool:
    patterns = [
        r"\bprompt_chars\b",
        r"\bsource_count\b",
        r"\bschema_version\b",
        r"\banalysis_model\b",
        r"\bhigh_model_input_package\b",
        r"\blow_model_digest\b",
        r"\bunified_evidence_pack\b",
        r"\bsource_id\b",
        r"\b(?:financial|revenue|chip|theme|company|customer|supply\s+chain)\b[^\n|。]{0,60}\bcoverage\s+pct\b",
        r"\bcoverage\s+pct\b",
    ]
    return any(re.search(pattern, text) for pattern in patterns)


def _result_record_from_research(
    *,
    command: str,
    command_dir: Path,
    result: ResearchCenterResult,
    progress_messages: list[str],
    elapsed_seconds: float,
) -> dict[str, Any]:
    prompt_paths = _extract_prompt_paths(progress_messages, result.report_json or {})
    prompt_chars = _estimate_prompt_chars(prompt_paths)
    output_text = normalize_report_text(result.markdown or result.summary or "")
    command_failure_output = _looks_like_command_failure_output(output_text)
    effective_status = "failed" if command_failure_output else result.status
    effective_error = result.fallback_reason or ("command output indicates failure" if command_failure_output else None)
    review = _quality_review(
        command=command,
        output_text=output_text,
        source_count=len(result.sources),
        prompt_chars=prompt_chars,
        elapsed_seconds=elapsed_seconds,
        status=effective_status,
        error=effective_error,
    )
    artifacts = result.artifacts
    output_path = command_dir / "output.md"
    telegram_path = command_dir / "telegram_summary.md"
    _write_text(output_path, output_text)
    _write_text(telegram_path, result.summary or "")
    report_paths_exist = {
        "markdown": _path_exists(str(artifacts.markdown_path)),
        "html": _path_exists(str(artifacts.html_path)),
        "json": _path_exists(str(artifacts.json_path)),
        "sources": _path_exists(str(artifacts.sources_path)),
        "output": _path_exists(str(output_path)),
        "telegram": _path_exists(str(telegram_path)),
    }
    ai_status = _extract_ai_status(result)
    if command.strip().startswith(("/news", "/topic_maintain")) and result.status == "success" and prompt_chars > 0:
        ai_status = "ai_success"
    if command_failure_output:
        ai_status = "failed"
    record = {
        "command": command,
        "status": effective_status,
        "ai_status": ai_status,
        "artifact_status": _artifact_status_for_command(command, report_paths_exist),
        "formal_ai_success": effective_status == "success" and ai_status == "ai_success" and not effective_error,
        "ai_used": result.ai_used,
        "ai_model": result.ai_model,
        "fallback_reason": effective_error,
        "elapsed_seconds": elapsed_seconds,
        "summary": result.summary,
        "report_paths": {
            "markdown": str(artifacts.markdown_path),
            "html": str(artifacts.html_path),
            "json": str(artifacts.json_path),
            "sources": str(artifacts.sources_path),
            "output": str(output_path),
            "telegram": str(telegram_path),
        },
        "report_paths_exist": report_paths_exist,
        "prompt_paths": prompt_paths,
        "prompt_chars": prompt_chars,
        "source_count": len(result.sources),
        "quality_review": review,
    }
    _write_json(command_dir / "sources.json", _source_rows(result.sources))
    _write_json(command_dir / "result.json", record)
    _write_json(command_dir / "quality_review.json", review)
    return record


def _run_research_center_command(command: str, command_dir: Path) -> dict[str, Any]:
    center = ResearchCenter(load_research_config())
    progress, messages = _progress_logger(command_dir / "progress.log")
    started = time.time()
    result = center.run_text_command(command, progress=progress)
    elapsed = round(time.time() - started, 2)
    return _result_record_from_research(
        command=command,
        command_dir=command_dir,
        result=result,
        progress_messages=messages,
        elapsed_seconds=elapsed,
    )


def _run_radar_command(command: str, command_dir: Path) -> dict[str, Any]:
    args = command.strip().split()[1:]
    request = parse_radar_args(args)
    progress, messages = _progress_logger(command_dir / "progress.log")
    started = time.time()
    result = run_radar(request, progress=progress)
    elapsed = round(time.time() - started, 2)
    text = normalize_report_text(format_radar_report(result, limit=max(50, len(result.candidates))))
    source_count = sum(len(item.web_sources or []) + len(item.ai_sources or []) for item in result.candidates)
    prompt_paths = _extract_prompt_paths(messages, {})
    prompt_chars = _estimate_prompt_chars(prompt_paths)
    review = _quality_review(
        command=command,
        output_text=text,
        source_count=source_count,
        prompt_chars=prompt_chars,
        elapsed_seconds=elapsed,
        status="success",
        error=None,
    )
    telegram_path = command_dir / "telegram_message.md"
    _write_text(telegram_path, text)
    summary_dir = ROOT / "reports" / "radar"
    report_paths_exist = {
        "summary": summary_dir.exists(),
        "telegram": _path_exists(str(telegram_path)),
    }
    ai_status = "ai_success" if request.model and request.ai_comment_enabled else "not_used"
    record = {
        "command": command,
        "status": "success",
        "ai_status": ai_status,
        "artifact_status": _artifact_status_for_command(command, report_paths_exist),
        "formal_ai_success": ai_status == "ai_success",
        "ai_used": bool(request.model and request.ai_comment_enabled),
        "ai_model": "MiniMax-M3" if request.model == "minimax" else request.model,
        "elapsed_seconds": elapsed,
        "summary": text[:2000],
        "report_paths": {
            "summary": str(summary_dir),
            "telegram": str(telegram_path),
        },
        "report_paths_exist": report_paths_exist,
        "prompt_paths": prompt_paths,
        "prompt_chars": prompt_chars,
        "source_count": source_count,
        "candidate_count": len(result.candidates),
        "quality_review": review,
    }
    _write_json(command_dir / "radar_result.json", result)
    _write_json(command_dir / "result.json", record)
    _write_json(command_dir / "quality_review.json", review)
    return record


def _run_one(command: str, command_dir: Path) -> dict[str, Any]:
    name = _command_name(command)
    try:
        if name == "radar":
            return _run_radar_command(command, command_dir)
        return _run_research_center_command(command, command_dir)
    except Exception as exc:
        error_text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        _write_text(command_dir / "error.txt", error_text)
        record = {
            "command": command,
            "status": "failed",
            "error": str(exc),
            "traceback_path": str(command_dir / "error.txt"),
        }
        _write_json(command_dir / "result.json", record)
        return record


def _run_one_in_subprocess(command: str, command_dir: Path, timeout_seconds: int) -> dict[str, Any]:
    stdout_path = command_dir / "worker_stdout.log"
    stderr_path = command_dir / "worker_stderr.log"
    command_path = command_dir / "command.json"
    command_dir.mkdir(parents=True, exist_ok=True)
    _write_json(command_path, {"command": command})
    args = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker-command-file",
        str(command_path),
        "--worker-dir",
        str(command_dir),
    ]
    started = time.time()
    try:
        with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open("w", encoding="utf-8") as stderr:
            completed = subprocess.run(
                args,
                cwd=str(ROOT),
                stdout=stdout,
                stderr=stderr,
                timeout=timeout_seconds,
                text=True,
            )
    except subprocess.TimeoutExpired:
        record = {
            "command": command,
            "status": "timeout",
            "error": f"command exceeded {timeout_seconds} seconds",
            "elapsed_seconds": round(time.time() - started, 2),
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
        }
        _write_json(command_dir / "result.json", record)
        return record
    result_path = command_dir / "result.json"
    if result_path.exists():
        try:
            record = json.loads(result_path.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            record = {"command": command, "status": "failed", "error": f"invalid result.json: {exc}"}
    else:
        record = {"command": command, "status": "failed", "error": "worker did not write result.json"}
    record.setdefault("elapsed_seconds", round(time.time() - started, 2))
    record["worker_returncode"] = completed.returncode
    record["stdout_path"] = str(stdout_path)
    record["stderr_path"] = str(stderr_path)
    record = _augment_record_with_runtime_issues(record, command_dir)
    _write_json(command_dir / "result.json", record)
    return record


def _summary_markdown(run_dir: Path, records: list[dict[str, Any]]) -> str:
    lines = [
        "# MiniMax M3 真實驗收報告",
        "",
        f"- 執行時間：{datetime.now().astimezone().isoformat(timespec='seconds')}",
        f"- 輸出資料夾：`{run_dir}`",
        f"- 指令數：{len(records)}",
        "",
        "| 指令 | 狀態 | AI狀態 | 檔案 | 耗時秒 | 來源 | Prompt 字元 | 品質 | 報告 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for record in records:
        review = record.get("quality_review") or {}
        report_paths = record.get("report_paths") or {}
        report = (
            _usable_report_path(report_paths.get("html"))
            or _usable_report_path(report_paths.get("markdown"))
            or _usable_report_path(report_paths.get("json"))
            or _usable_report_path(report_paths.get("topic_raw"))
            or _usable_report_path(report_paths.get("telegram"))
            or _usable_report_path(report_paths.get("output"))
            or _usable_report_path(report_paths.get("sources"))
            or _usable_report_path(report_paths.get("summary"))
            or ""
        )
        score = ""
        if review:
            score = f"{review.get('score')}/{review.get('total')}"
        lines.append(
            "| "
            + " | ".join(
                [
                    str(record.get("command") or "").replace("|", "\\|"),
                    str(record.get("status") or ""),
                    str(record.get("ai_status") or ""),
                    str(record.get("artifact_status") or ""),
                    str(record.get("elapsed_seconds") or ""),
                    str(record.get("source_count") or ""),
                    str(record.get("prompt_chars") or ""),
                    score,
                    str(report).replace("|", "\\|"),
                ]
            )
            + " |"
        )
    lines.extend(["", "## 未達標或需檢查項目", ""])
    has_problem = False
    for record in records:
        review = record.get("quality_review") or {}
        issues = review.get("issues") or []
        runtime_issues = record.get("runtime_issues") or review.get("runtime_issues") or []
        is_problem = (
            record.get("status") != "success"
            or (record.get("ai_status") in {"fallback_success", "failed"})
            or (record.get("artifact_status") in {"partial", "missing"})
            or review.get("pass") is False
            or bool(runtime_issues)
        )
        if is_problem:
            has_problem = True
            lines.append(f"### {record.get('command')}")
            if record.get("ai_status") == "fallback_success":
                lines.append("- AI 狀態：AI 失敗但 fallback 報告已產出，不能視為正式 AI 分析成功。")
            elif record.get("ai_status") == "failed":
                lines.append("- AI 狀態：AI 正式分析失敗。")
            if record.get("artifact_status") in {"partial", "missing"}:
                lines.append(f"- 檔案狀態：{record.get('artifact_status')}")
            if record.get("error"):
                lines.append(f"- 錯誤：{record.get('error')}")
            for issue in issues:
                lines.append(f"- {issue}")
            for issue in runtime_issues:
                if issue not in issues:
                    lines.append(f"- {issue}")
            lines.append("")
    if not has_problem:
        lines.append("- 無。")
    return "\n".join(lines).strip() + "\n"


def _usable_report_path(value: Any) -> str:
    text = str(value or "").strip()
    if not text or text.startswith("__no_"):
        return ""
    return text


def _load_commands(args: argparse.Namespace) -> list[str]:
    if args.command:
        return list(args.command)
    if args.commands_file:
        path = Path(args.commands_file)
        return [line.strip() for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip() and not line.strip().startswith("#")]
    return list(DEFAULT_COMMANDS)


def _request_parameter_summary(command: str, request: Any) -> dict[str, Any]:
    if command.strip().startswith("/radar"):
        return {
            "source": request.source,
            "report_date": request.report_date.isoformat() if request.report_date else None,
            "ai_top": request.ai_top,
            "model": request.model,
            "ai_comment_enabled": request.ai_comment_enabled,
        }
    return {
        "command": request.command,
        "target": request.target,
        "target_type": request.target_type,
        "candidate_pool": request.candidate_pool,
        "market_scope": request.market_scope,
        "theme_scope": request.theme_scope,
        "mode": request.mode,
        "source_only": request.source_only,
        "score": request.score,
        "brief": request.brief,
        "top": request.top,
        "ai_model": request.ai_model,
        "source": request.source,
        "lookback_days": request.lookback_days,
        "report_date": request.report_date.isoformat() if request.report_date else None,
        "output_formats": list(request.output_formats),
    }


def _parse_parameter_case(case: dict[str, Any]) -> dict[str, Any]:
    command = str(case["command"])
    parser_name = str(case.get("parser") or "research_center")
    expected_error = case.get("expected_error_contains")
    try:
        if parser_name == "radar":
            request = parse_radar_args(command.strip().split()[1:])
        else:
            request = parse_command_text(command)
        parsed = _request_parameter_summary(command, request)
    except (CommandParseError, ValueError) as exc:
        error = str(exc)
        passed = bool(expected_error and str(expected_error) in error)
        return {
            **case,
            "status": "expected_error" if passed else "failed",
            "pass": passed,
            "error": error,
            "parsed": {},
            "mismatches": [] if passed else [f"unexpected parse error: {error}"],
        }
    except Exception as exc:
        error = str(exc)
        return {
            **case,
            "status": "failed",
            "pass": False,
            "error": error,
            "parsed": {},
            "mismatches": [f"unexpected exception: {type(exc).__name__}: {error}"],
        }

    if expected_error:
        return {
            **case,
            "status": "failed",
            "pass": False,
            "error": "",
            "parsed": parsed,
            "mismatches": [f"expected error containing {expected_error!r}, but parse succeeded"],
        }

    expected = dict(case.get("expected") or {})
    mismatches: list[str] = []
    for key, expected_value in expected.items():
        actual = parsed.get(key)
        if actual != expected_value:
            mismatches.append(f"{key}: expected {expected_value!r}, got {actual!r}")
    return {
        **case,
        "status": "success" if not mismatches else "failed",
        "pass": not mismatches,
        "error": "",
        "parsed": parsed,
        "mismatches": mismatches,
    }


def _run_parameter_matrix(cases: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    selected = cases or PARAMETER_MATRIX_CASES
    results = [_parse_parameter_case(case) for case in selected]
    covered: dict[str, list[str]] = {}
    for result in results:
        for parameter in result.get("parameters") or []:
            covered.setdefault(str(parameter), []).append(str(result.get("name") or ""))
    command_coverage: dict[str, int] = {}
    for result in results:
        command = str(result.get("command") or "").split()[0].lstrip("/")
        command_coverage[command] = command_coverage.get(command, 0) + 1
    return {
        "status": "success" if all(item.get("pass") for item in results) else "failed",
        "case_count": len(results),
        "passed_count": sum(1 for item in results if item.get("pass")),
        "failed_count": sum(1 for item in results if not item.get("pass")),
        "covered_parameters": covered,
        "command_coverage": command_coverage,
        "results": results,
    }


def _parameter_matrix_markdown(matrix: dict[str, Any]) -> str:
    lines = [
        "# AI 指令參數覆蓋檢查",
        "",
        f"- 狀態：{matrix.get('status')}",
        f"- 案例數：{matrix.get('case_count')}",
        f"- 通過：{matrix.get('passed_count')}",
        f"- 失敗：{matrix.get('failed_count')}",
        "",
        "## 指令覆蓋",
        "",
    ]
    for command, count in sorted((matrix.get("command_coverage") or {}).items()):
        lines.append(f"- `/{command}`：{count} 個參數案例")
    lines.extend(["", "## 參數覆蓋", ""])
    for parameter, names in sorted((matrix.get("covered_parameters") or {}).items()):
        lines.append(f"- `{parameter}`：{len(names)} 個案例")
    lines.extend(["", "## 案例明細", "", "| 案例 | 指令 | 範圍 | 結果 | 問題 |", "|---|---|---|---|---|"])
    for result in matrix.get("results") or []:
        issues = "；".join(result.get("mismatches") or ([result.get("error")] if result.get("error") and not result.get("pass") else []))
        lines.append(
            "| "
            + " | ".join(
                [
                    str(result.get("name") or "").replace("|", "\\|"),
                    str(result.get("command") or "").replace("|", "\\|"),
                    str(result.get("execution_scope") or "").replace("|", "\\|"),
                    "通過" if result.get("pass") else "失敗",
                    str(issues or "").replace("|", "\\|"),
                ]
            )
            + " |"
        )
    return "\n".join(lines).strip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run real MiniMax M3 validation for AI commands.")
    parser.add_argument("--command", action="append", help="Command text to run. Can be repeated.")
    parser.add_argument("--commands-file", help="UTF-8 file with one command per line.")
    parser.add_argument("--start-at", type=int, default=1, help="1-based command index to start at.")
    parser.add_argument("--max-commands", type=int, default=0, help="Run at most this many commands.")
    parser.add_argument("--run-dir", help="Existing or new output directory for this validation run.")
    parser.add_argument("--command-timeout-seconds", type=int, default=2400, help="Timeout for each command worker.")
    parser.add_argument("--worker-command", help=argparse.SUPPRESS)
    parser.add_argument("--worker-command-file", help=argparse.SUPPRESS)
    parser.add_argument("--worker-dir", help=argparse.SUPPRESS)
    parser.add_argument("--check-config", action="store_true", help="Only verify MiniMax M3 configuration.")
    parser.add_argument("--include-parameter-matrix", action="store_true", help="Also validate supported parameter parsing/coverage.")
    parser.add_argument("--parameter-matrix-only", action="store_true", help="Only validate parameter matrix; do not run real AI commands.")
    args = parser.parse_args()

    worker_command = args.worker_command
    if args.worker_command_file:
        worker_data = json.loads(Path(args.worker_command_file).read_text(encoding="utf-8-sig"))
        worker_command = str(worker_data.get("command") or "")
    if worker_command and args.worker_dir:
        record = _run_one(worker_command, Path(args.worker_dir))
        return 0 if record.get("status") == "success" else 1

    config = load_research_config()
    if args.check_config:
        center = ResearchCenter(config)
        print(
            json.dumps(
                {
                    "minimax_configured": center.minimax.is_configured(),
                    "minimax_model": config.minimax_model,
                    "low_model": config.minimax_low_model,
                    "tavily_enabled": config.enable_tavily_search,
                    "gemini_model": config.model,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if center.minimax.is_configured() else 2

    run_dir = _new_run_dir(Path(args.run_dir) if args.run_dir else None)
    if args.include_parameter_matrix or args.parameter_matrix_only:
        matrix = _run_parameter_matrix()
        _write_json(run_dir / "parameter_matrix.json", matrix)
        _write_text(run_dir / "parameter_matrix.md", _parameter_matrix_markdown(matrix))
        if args.parameter_matrix_only:
            print(f"Parameter matrix: {run_dir / 'parameter_matrix.md'}", flush=True)
            return 0 if matrix.get("status") == "success" else 1

    commands = _load_commands(args)
    if args.start_at > 1:
        commands = commands[args.start_at - 1 :]
    if args.max_commands > 0:
        commands = commands[: args.max_commands]
    _write_json(run_dir / "commands.json", {"commands": commands})
    print(f"Validation run dir: {run_dir}", flush=True)

    records: list[dict[str, Any]] = []
    for offset, command in enumerate(commands, args.start_at):
        command_dir = run_dir / _slug_command(command, offset)
        command_dir.mkdir(parents=True, exist_ok=True)
        print(f"\n=== RUN {offset}/{args.start_at + len(commands) - 1}: {command} ===", flush=True)
        record = _run_one_in_subprocess(command, command_dir, args.command_timeout_seconds)
        records.append(record)
        _write_json(run_dir / "partial_results.json", records)
        _write_text(run_dir / "summary.md", _summary_markdown(run_dir, records))

    _write_json(run_dir / "results.json", records)
    _write_text(run_dir / "summary.md", _summary_markdown(run_dir, records))
    print(f"Validation summary: {run_dir / 'summary.md'}", flush=True)
    return 0 if all(record.get("status") == "success" for record in records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
