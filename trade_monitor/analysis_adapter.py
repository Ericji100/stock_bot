from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, TextIO
from zoneinfo import ZoneInfo

from .analysis_contract import (
    AnalysisValidationError,
    analysis_state_summary,
    render_analysis_markdown,
    render_quiet_status_markdown,
    render_quiet_unavailable_markdown,
    render_unavailable_markdown,
    validate_analysis_payload,
)
from .anchor_state import normalize_legacy_quadrant_axis_consistency
from .bridge import (
    BridgeError,
    DeliveryFileLock,
    execute_bridge,
    generate_event_id,
    load_bridge_config,
)
from .constitution_state import (
    DEFAULT_STATE_PATH as DEFAULT_CONSTITUTION_STATE_PATH,
    ConstitutionStateError,
    apply_constitution_event,
    load_constitution_snapshot,
)
from .market_structure_state import (
    MarketStructureStateError,
    market_structure_notification_reasons,
    upgrade_market_structure_state,
    validate_market_structure_state,
    validate_market_structure_transition,
)
from .resume_guard import complete_monitor_run


AUTOMATION_ID = "1-k"
TAIPEI = ZoneInfo("Asia/Taipei")
STATE_VERSION = 1
DEFAULT_STATE_PATH = Path(".runtime/trade_monitor/analysis_state.json")
DEFAULT_CONFIG_PATH = Path("config.json")
DEFAULT_DELIVERY_STATE_PATH = Path(".runtime/trade_monitor/delivery_state.json")
DEFAULT_RUNTIME_STATE_PATH = Path(".runtime/trade_monitor/runtime_state.json")
DEFAULT_STALE_AFTER_SECONDS = 90
MAX_PENDING_CONTEXTS = 20
BRIDGE_TIMEOUT_SECONDS = 45


class AnalysisAdapterError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def prepare_capture(
    image_path: Path,
    captured_at: datetime,
    *,
    state_path: Path = DEFAULT_STATE_PATH,
    constitution_state_path: Path = DEFAULT_CONSTITUTION_STATE_PATH,
    stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
    target_market_structure_version: int = 2,
) -> dict[str, Any]:
    if stale_after_seconds <= 0:
        raise AnalysisAdapterError("stale_threshold_invalid", "stale threshold must be positive")
    if captured_at.tzinfo is None:
        raise AnalysisAdapterError("captured_at_invalid", "captured_at must include a timezone")
    if target_market_structure_version not in {2, 3, 4, 5, 6}:
        raise AnalysisAdapterError(
            "market_structure_version_invalid",
            "target market structure state version must be 2, 3, 4, 5, or 6",
        )
    try:
        image_bytes = image_path.read_bytes()
    except FileNotFoundError as exc:
        raise AnalysisAdapterError("image_missing", "chart capture is missing") from exc
    except OSError as exc:
        raise AnalysisAdapterError("image_unreadable", "chart capture could not be read") from exc
    if not image_bytes:
        raise AnalysisAdapterError("image_empty", "chart capture is empty")

    captured_taipei = captured_at.astimezone(TAIPEI)
    current_unclosed = captured_taipei.replace(second=0, microsecond=0)
    expected_closed = current_unclosed - timedelta(minutes=1)
    image_sha256 = hashlib.sha256(image_bytes).hexdigest()
    lock_path = state_path.with_suffix(state_path.suffix + ".lock")

    with DeliveryFileLock(lock_path):
        state = _read_state(state_path)
        last_completed = _parse_state_time(state.get("last_completed_closed_k"))
        previous_hash = str(state.get("last_observed_image_sha256") or "")
        same_since = _parse_state_time(state.get("same_image_since"))

        if image_sha256 != previous_hash or same_since is None:
            same_since = captured_taipei
        unchanged_seconds = max(0.0, (captured_taipei - same_since).total_seconds())

        if last_completed is not None and expected_closed < last_completed:
            status = "REGRESSION"
        elif last_completed is not None and expected_closed == last_completed:
            status = "SAME_BAR"
        elif image_sha256 == previous_hash and unchanged_seconds >= stale_after_seconds:
            status = "STALE"
        else:
            status = "FRESH"

        if last_completed is None:
            new_closed_bar_count = 1
        else:
            delta_minutes = int((expected_closed - last_completed).total_seconds() // 60)
            new_closed_bar_count = max(1, delta_minutes)

        context_material = "\n".join(
            (captured_taipei.isoformat(), expected_closed.isoformat(), image_sha256)
        )
        context_id = hashlib.sha256(context_material.encode("utf-8")).hexdigest()
        context = {
            "context_id": context_id,
            "capture_status": status,
            "captured_at": captured_taipei.isoformat(),
            "expected_latest_closed_k_iso": expected_closed.isoformat(),
            "expected_latest_closed_k_hhmm": expected_closed.strftime("%H:%M"),
            "current_unclosed_k_iso": current_unclosed.isoformat(),
            "current_unclosed_k_hhmm": current_unclosed.strftime("%H:%M"),
            "new_closed_bar_count": new_closed_bar_count,
            "image_sha256": image_sha256,
            "image_path": str(image_path.resolve()),
            "unchanged_image_seconds": round(unchanged_seconds, 3),
        }

        pending = state.setdefault("pending", {})
        pending[context_id] = context
        _prune_pending(pending)
        state.update(
            {
                "version": STATE_VERSION,
                "last_observed_image_sha256": image_sha256,
                "same_image_since": same_since.isoformat(),
                "last_observed_at": captured_taipei.isoformat(),
            }
        )
        _write_state_atomic(state_path, state)
        previous_summary = state.get("last_analysis_summary")
        previous_market_structure = state.get("market_structure_state")
        if previous_market_structure is not None:
            if isinstance(previous_market_structure, Mapping):
                previous_market_structure = dict(previous_market_structure)
                anchor_context = previous_market_structure.get("anchor_context")
                if isinstance(anchor_context, Mapping):
                    previous_market_structure["anchor_context"] = (
                        normalize_legacy_quadrant_axis_consistency(anchor_context)
                    )
            previous_market_structure = validate_market_structure_state(previous_market_structure)
            previous_market_structure = upgrade_market_structure_state(
                previous_market_structure,
                target_version=target_market_structure_version,
            )

    constitution_snapshot = load_constitution_snapshot(constitution_state_path, at=expected_closed)
    return {
        "ok": True,
        "status": status,
        "context_id": context_id,
        "requires_analysis": status == "FRESH",
        "context": context,
        "previous_analysis_summary": previous_summary if isinstance(previous_summary, dict) else None,
        "market_structure_state": previous_market_structure,
        "constitution_state": constitution_snapshot,
    }


async def finalize_analysis(
    *,
    context_id: str,
    analysis_payload: Mapping[str, Any] | None,
    force_notify: bool,
    run_id: str | None,
    state_path: Path = DEFAULT_STATE_PATH,
    config_path: Path = DEFAULT_CONFIG_PATH,
    delivery_state_path: Path = DEFAULT_DELIVERY_STATE_PATH,
    runtime_state_path: Path = DEFAULT_RUNTIME_STATE_PATH,
    constitution_state_path: Path = DEFAULT_CONSTITUTION_STATE_PATH,
    dry_run: bool = False,
) -> dict[str, Any]:
    context_id = str(context_id or "").strip()
    if not context_id:
        raise AnalysisAdapterError("context_id_missing", "context_id is required")

    lock_path = state_path.with_suffix(state_path.suffix + ".lock")
    with DeliveryFileLock(lock_path):
        state = _read_state(state_path)
        context = state.get("pending", {}).get(context_id)
    if not isinstance(context, dict):
        raise AnalysisAdapterError("context_missing", "prepared capture context was not found")

    capture_status = str(context.get("capture_status") or "")
    validated: dict[str, Any] | None = None
    analysis_valid = False
    validation_error: str | None = None
    constitution_result: dict[str, Any] | None = None

    if capture_status == "FRESH":
        try:
            if analysis_payload is None:
                raise AnalysisValidationError("analysis payload is required for a fresh capture")
            validated = validate_analysis_payload(analysis_payload)
            market_structure = validated.get("market_structure_state")
            if market_structure is not None:
                with DeliveryFileLock(lock_path):
                    current_state = _read_state(state_path)
                    previous_market_structure = current_state.get("market_structure_state")
                normalized_structure = validate_market_structure_transition(
                    previous_market_structure if isinstance(previous_market_structure, Mapping) else None,
                    market_structure,
                    expected_as_of=str(context["expected_latest_closed_k_iso"]),
                )
                material_reasons = market_structure_notification_reasons(
                    previous_market_structure if isinstance(previous_market_structure, Mapping) else None,
                    normalized_structure,
                )
                validated["market_structure_state"] = normalized_structure
                if material_reasons:
                    validated["original_decision"] = "NOTIFY"
                    reason_text = "、".join(material_reasons)
                    if reason_text not in validated["notification_reason"]:
                        validated["notification_reason"] = f"{validated['notification_reason']}（系統強制通知：{reason_text}）"
            message = render_analysis_markdown(validated, context, resumed=force_notify)
            constitution_event = validated.get("constitution_event") or {
                "event_type": "NONE",
                "event_id": f"legacy-none:{context['expected_latest_closed_k_iso']}",
                "setup_id": None,
                "position_id": None,
                "direction": None,
                "entry_price_estimate": None,
                "stop_price_estimate": None,
                "risk_points": None,
                "latest_closed_bar_time": context["expected_latest_closed_k_iso"],
                "reason": "Legacy v1 payload without a persistent constitution event.",
            }
            constitution_result = apply_constitution_event(
                constitution_state_path,
                constitution_event,
                expected_latest_closed_bar_time=str(context["expected_latest_closed_k_iso"]),
            )
            original_decision = validated["original_decision"]
            analysis_valid = True
        except (AnalysisValidationError, ConstitutionStateError, MarketStructureStateError) as exc:
            if isinstance(exc, ConstitutionStateError):
                validation_error = "交易憲法狀態或事件未通過驗證；本輪不採用任何新進場或持倉異動。"
            else:
                validation_error = (
                    "圖表擷取正常，但內部結構化狀態未通過一致性驗證；"
                    "本輪不採用任何方向或價位。"
                )
            message = render_unavailable_markdown(context, validation_error, resumed=force_notify)
            original_decision = "NOTIFY"
    elif capture_status == "SAME_BAR":
        validation_error = "本輪仍是上一輪已處理的同一根已收盤 K，未重複產生交易判斷。"
        message = render_unavailable_markdown(context, validation_error, resumed=force_notify)
        original_decision = "DONT_NOTIFY"
    elif capture_status == "STALE":
        validation_error = "Chrome 圖表影像長時間未變，可能延遲或停止更新。"
        message = render_unavailable_markdown(context, validation_error, resumed=force_notify)
        original_decision = "NOTIFY"
    elif capture_status == "REGRESSION":
        validation_error = "排程推算的已收盤 K 早於先前完成時間，拒絕倒退判讀。"
        message = render_unavailable_markdown(context, validation_error, resumed=force_notify)
        original_decision = "NOTIFY"
    else:
        validation_error = "圖表擷取狀態無法辨識。"
        message = render_unavailable_markdown(context, validation_error, resumed=force_notify)
        original_decision = "NOTIFY"

    decision = "NOTIFY" if force_notify else original_decision
    if decision == "DONT_NOTIFY":
        if analysis_valid and validated is not None:
            message = render_quiet_status_markdown(validated, context)
        else:
            message = render_quiet_unavailable_markdown(
                context,
                validation_error or "本輪沒有需要主動通知的新狀態。",
            )
    latest_closed_bar_time = str(context["expected_latest_closed_k_iso"])
    event_id = generate_event_id(AUTOMATION_ID, latest_closed_bar_time, decision, message)

    telegram_result: dict[str, Any] = {
        "ok": True,
        "status": "not_called",
        "event_id": event_id,
        "reason": "decision_dont_notify",
    }
    telegram_exit_code = 0
    telegram_failure = False
    should_call_bridge = decision == "NOTIFY" or _quiet_status_enabled(config_path)
    if should_call_bridge:
        try:
            telegram_exit_code, telegram_result = await asyncio.wait_for(
                execute_bridge(
                    event_id=event_id,
                    decision=decision,
                    message=message,
                    config_path=config_path,
                    state_path=delivery_state_path,
                    dry_run=dry_run,
                ),
                timeout=BRIDGE_TIMEOUT_SECONDS,
            )
            telegram_failure = telegram_exit_code != 0 or telegram_result.get("ok") is not True
        except asyncio.TimeoutError:
            telegram_exit_code = 1
            telegram_failure = True
            telegram_result = {
                "ok": False,
                "status": "failed",
                "event_id": event_id,
                "error_code": "bridge_timeout",
                "error": "Telegram bridge timed out",
            }
        except Exception:
            telegram_exit_code = 1
            telegram_failure = True
            telegram_result = {
                "ok": False,
                "status": "failed",
                "event_id": event_id,
                "error_code": "bridge_internal_error",
                "error": "Telegram bridge failed",
            }

    resume_delivered = (
        force_notify
        and not dry_run
        and telegram_result.get("status") in {"sent", "duplicate"}
    )
    resume_result: dict[str, Any] | None = None
    if run_id:
        try:
            resume_result = complete_monitor_run(
                runtime_state_path,
                run_id=run_id,
                resume_delivered=resume_delivered,
            )
        except Exception:
            resume_result = {
                "ok": False,
                "status": "failed",
                "error_code": "resume_complete_failed",
                "error": "Trade monitor resume completion failed",
            }

    if analysis_valid and validated is not None:
        with DeliveryFileLock(lock_path):
            state = _read_state(state_path)
            state.update(
                {
                    "version": STATE_VERSION,
                    "last_completed_closed_k": latest_closed_bar_time,
                    "last_completed_event_id": event_id,
                    "last_completed_at": datetime.now(timezone.utc).isoformat(),
                    "last_analysis_summary": analysis_state_summary(validated),
                }
            )
            if validated.get("market_structure_state") is not None:
                state["market_structure_state"] = validated["market_structure_state"]
            state.setdefault("pending", {}).pop(context_id, None)
            _write_state_atomic(state_path, state)

    return {
        "ok": True,
        "decision": decision,
        "original_decision": original_decision,
        "latest_closed_bar_time": latest_closed_bar_time,
        "message": message,
        "capture_status": capture_status,
        "analysis_valid": analysis_valid,
        "validation_status": validation_error,
        "event_id": event_id,
        "telegram_exit_code": telegram_exit_code,
        "telegram_ok": telegram_result.get("ok") is True,
        "telegram_status": telegram_result.get("status"),
        "telegram_failure": telegram_failure,
        "resume_delivered": resume_delivered,
        "resume_status": None if resume_result is None else resume_result.get("status"),
        "resume_ok": None if resume_result is None else resume_result.get("ok") is True,
        "constitution_status": None if constitution_result is None else constitution_result.get("status"),
        "constitution_event_key": None if constitution_result is None else constitution_result.get("event_key"),
    }


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Anchor and render structured Codex trade-monitor analysis")
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE_PATH)
    parser.add_argument("--constitution-state-file", type=Path, default=DEFAULT_CONSTITUTION_STATE_PATH)
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("--image", type=Path, required=True)
    prepare_parser.add_argument("--captured-at", required=True)
    prepare_parser.add_argument("--stale-after-seconds", type=int, default=DEFAULT_STALE_AFTER_SECONDS)
    prepare_parser.add_argument("--market-structure-version", type=int, choices=(2, 3, 4, 5), default=2)

    finalize_parser = subparsers.add_parser("finalize")
    finalize_parser.add_argument("--context-id", required=True)
    finalize_parser.add_argument("--force-notify", choices=("true", "false"), required=True)
    finalize_parser.add_argument("--run-id")
    finalize_parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    finalize_parser.add_argument("--delivery-state-file", type=Path, default=DEFAULT_DELIVERY_STATE_PATH)
    finalize_parser.add_argument("--runtime-state-file", type=Path, default=DEFAULT_RUNTIME_STATE_PATH)
    finalize_parser.add_argument("--dry-run", action="store_true")
    return parser


def main(
    argv: list[str] | None = None,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> int:
    input_stream = stdin or sys.stdin
    output = stdout or sys.stdout
    parser = build_argument_parser()
    try:
        args = parser.parse_args(argv)
        if args.command == "prepare":
            captured_at = _parse_input_time(args.captured_at)
            payload = prepare_capture(
                args.image,
                captured_at,
                state_path=args.state_file,
                constitution_state_path=args.constitution_state_file,
                stale_after_seconds=args.stale_after_seconds,
                target_market_structure_version=args.market_structure_version,
            )
        else:
            raw = input_stream.read()
            analysis_payload = None
            if raw.strip():
                parsed = json.loads(raw)
                if not isinstance(parsed, dict):
                    raise AnalysisAdapterError("analysis_invalid", "analysis input must be a JSON object")
                analysis_payload = parsed
            payload = asyncio.run(
                finalize_analysis(
                    context_id=args.context_id,
                    analysis_payload=analysis_payload,
                    force_notify=args.force_notify == "true",
                    run_id=args.run_id,
                    state_path=args.state_file,
                    config_path=args.config,
                    delivery_state_path=args.delivery_state_file,
                    runtime_state_path=args.runtime_state_file,
                    constitution_state_path=args.constitution_state_file,
                    dry_run=args.dry_run,
                )
            )
        exit_code = 0
    except (AnalysisAdapterError, BridgeError, ConstitutionStateError, MarketStructureStateError) as exc:
        exit_code = 2
        payload = {
            "ok": False,
            "status": "failed",
            "error_code": getattr(exc, "code", "analysis_adapter_error"),
            "error": str(exc),
        }
    except json.JSONDecodeError:
        exit_code = 2
        payload = {
            "ok": False,
            "status": "failed",
            "error_code": "analysis_json_invalid",
            "error": "analysis input is not valid JSON",
        }
    except SystemExit:
        raise
    except Exception:
        exit_code = 2
        payload = {
            "ok": False,
            "status": "failed",
            "error_code": "analysis_adapter_internal_error",
            "error": "Trade monitor analysis adapter failed",
        }

    output.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    output.flush()
    return exit_code


def _quiet_status_enabled(config_path: Path) -> bool:
    try:
        return load_bridge_config(config_path).get("trade_monitor_send_quiet_status", False) is True
    except BridgeError:
        return False


def _read_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": STATE_VERSION, "pending": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AnalysisAdapterError("analysis_state_invalid", "analysis state is invalid") from exc
    if not isinstance(payload, dict):
        raise AnalysisAdapterError("analysis_state_invalid", "analysis state must be an object")
    pending = payload.get("pending")
    if pending is None:
        payload["pending"] = {}
    elif not isinstance(pending, dict):
        raise AnalysisAdapterError("analysis_state_invalid", "analysis pending state is invalid")
    return payload


def _write_state_atomic(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    except OSError as exc:
        raise AnalysisAdapterError("analysis_state_write_failed", "analysis state could not be saved") from exc
    finally:
        temporary.unlink(missing_ok=True)


def _parse_input_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise AnalysisAdapterError("captured_at_invalid", "captured_at is not a valid ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise AnalysisAdapterError("captured_at_invalid", "captured_at must include a timezone")
    return parsed


def _parse_state_time(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise AnalysisAdapterError("analysis_state_invalid", "analysis state timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise AnalysisAdapterError("analysis_state_invalid", "analysis state timestamp needs a timezone")
    return parsed.astimezone(TAIPEI)


def _prune_pending(pending: dict[str, Any]) -> None:
    if len(pending) <= MAX_PENDING_CONTEXTS:
        return
    ordered = sorted(
        pending.items(),
        key=lambda item: str(item[1].get("captured_at") if isinstance(item[1], dict) else ""),
        reverse=True,
    )
    pending.clear()
    pending.update(ordered[:MAX_PENDING_CONTEXTS])


if __name__ == "__main__":
    raise SystemExit(main())
