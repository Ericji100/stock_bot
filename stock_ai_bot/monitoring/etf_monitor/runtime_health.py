"""Durable runtime diagnostics and deduplicated dedicated-route alerts."""
from __future__ import annotations

import json
import logging
import traceback
from datetime import datetime
from logging.handlers import RotatingFileHandler
from zoneinfo import ZoneInfo

from stock_ai_bot.workdata import get_workdata_path

from .models import MonitorPhase, stable_event_id
from .routing import _TOKENISH, _safe_error


def record_runtime_error(stage: str, exc: BaseException, attempt: int) -> str:
    """Log every attempt, including the cause chain, without Telegram tokens."""
    path = get_workdata_path("logs/etf_monitor/runtime-errors.jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "observed_at": datetime.now(ZoneInfo("Asia/Taipei")).isoformat(),
        "stage": stage, "attempt": attempt, "error": _safe_error(exc),
        "traceback": _TOKENISH.sub("[REDACTED]", "".join(traceback.format_exception(exc))),
    }
    handler = RotatingFileHandler(path, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
    try:
        handler.emit(logging.makeLogRecord({"msg": json.dumps(row, ensure_ascii=False)}))
    finally:
        handler.close()
    return row["error"]


async def runtime_alert(runtime, *, stage: str, recovered: bool = False) -> None:
    """One failure and one recovery per day/stage; ambiguous sends stay quarantined."""
    day = datetime.now(ZoneInfo(runtime.config.timezone)).date()
    kind = "RECOVERED" if recovered else "FAILURE"
    event_id = stable_event_id(
        symbol="0050", strategy=runtime.config.strategy, phase=MonitorPhase.HEALTH,
        signal_date=day, decision=kind, suffix=stage,
    )
    if runtime.store.event(event_id) is None:
        body = (
            f"0050 趨勢策略｜{'監控已恢復' if recovered else '監控異常'}\n"
            f"時間：{datetime.now(ZoneInfo(runtime.config.timezone)):%Y-%m-%d %H:%M}\n"
            + ("資料與群組檢查已通過，監控排程已恢復。" if recovered else
               "監控暫時無法完成準備或執行，系統會自動重試。\n"
               "故障期間可能缺少通知；詳細錯誤已保存在本機。")
        )
        runtime.store.append_event_with_outbox(
            event_id=event_id, phase=MonitorPhase.HEALTH, trading_date=day,
            event_type=f"RUNTIME_{kind}", payload={"stage": stage, "kind": kind},
            target_chat_id=runtime.config.chat_id, body=body,
        )
    await runtime.router.deliver(event_id)
