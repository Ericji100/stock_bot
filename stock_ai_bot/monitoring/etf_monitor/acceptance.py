"""Read-only evidence for a complete production shadow-observation day."""
from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from stock_ai_bot.monitoring.etf_monitor.config import load_etf_monitor_config
from stock_ai_bot.workdata import get_workdata_path


CHECK_LABELS = {
    "sqlite_integrity": "資料庫完整性",
    "all_three_jobs_succeeded": "三個時段執行結果",
    "three_jobs_on_time": "三個時段準時完成",
    "all_three_notifications_delivered": "三個時段通知回執",
    "prior_reconciliations_complete": "歷史晚間資料核對",
    "no_delivery_review": "通知送達確認",
    "nov9_gap_verified": "11/9 漏核補齊",
    "full_daily_data_confirmed": "完整官方日資料",
    "formal_decisions_consistent_or_corrected": "正式判讀與晚間核對一致性",
    "official_open_day": "官方交易日確認",
    "shadow_configuration": "影子監控設定",
}


def inspect_day(day: date, *, database: Path | None = None, now: datetime | None = None):
    from .models import canonical_json
    from .service import _material_state

    cfg = load_etf_monitor_config()
    tz = ZoneInfo(cfg.timezone)
    current = now or datetime.now(tz)
    path = database or get_workdata_path(cfg.database_relative_path)
    with sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        jobs = [dict(row) for row in connection.execute(
            "SELECT phase,status,error,started_at,finished_at FROM job_runs WHERE trading_date=? ORDER BY phase",
            (day.isoformat(),),
        )]
        deliveries = [dict(row) for row in connection.execute(
            "SELECT e.phase,e.event_type,o.status,o.attempts,o.telegram_message_id,o.delivered_at "
            "FROM events e JOIN outbox o USING(event_id) WHERE e.trading_date=? AND e.is_test=0",
            (day.isoformat(),),
        )]
        pending = [dict(row) for row in connection.execute(
            "SELECT trading_date,status,error FROM job_runs WHERE phase='reconciliation_2200' "
            "AND trading_date<=? AND status NOT IN ('SUCCEEDED')", (day.isoformat(),),
        )]
        evidence = [dict(row) for row in connection.execute(
            "SELECT phase,event_type,payload_json FROM events WHERE trading_date=? "
            "AND phase IN ('decision_1355','reconciliation_2200') AND event_type IN "
            "('HOLD','BUY_NEXT_OPEN','SELL_NEXT_OPEN','RECONCILIATION_CONFIRMED','CORRECTION')", (day.isoformat(),),
        )]
        decisions = [canonical_json(_material_state(json.loads(row["payload_json"]))) for row in evidence]
        missing = connection.execute(
            "SELECT COUNT(*) FROM job_runs WHERE trading_date='2026-11-09' "
            "AND phase='reconciliation_2200' AND status='SUCCEEDED'"
        ).fetchone()[0]
        uncertain = connection.execute(
            "SELECT COUNT(*) FROM outbox WHERE status IN ('PENDING','RETRY','SENDING','DELIVERY_UNCERTAIN')"
        ).fetchone()[0]
        integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
    expected = {
        "preview_1245": (cfg.schedule.preview, "preview"),
        "decision_1355": (cfg.schedule.decision, "decision"),
        "reconciliation_2200": (cfg.schedule.reconciliation, "reconciliation"),
    }
    checks = {"sqlite_integrity": integrity == "ok", "all_three_jobs_succeeded": True,
              "three_jobs_on_time": True, "all_three_notifications_delivered": True,
              "prior_reconciliations_complete": not pending, "no_delivery_review": uncertain == 0,
              "nov9_gap_verified": missing == 1}
    for phase, (scheduled, timeout_key) in expected.items():
        job = next((row for row in jobs if row["phase"] == phase), None)
        due = datetime.combine(day, scheduled, tz)
        succeeded = job is not None and job["status"] == "SUCCEEDED"
        checks["all_three_jobs_succeeded"] &= succeeded
        on_time = (succeeded and datetime.fromisoformat(job["started_at"]) <= due + timedelta(minutes=5)
                   and datetime.fromisoformat(job["finished_at"]) <= due + timedelta(seconds=cfg.job_timeouts_seconds[timeout_key]))
        checks["three_jobs_on_time"] &= bool(on_time)
        checks["all_three_notifications_delivered"] &= any(
            row["phase"] == phase and row["event_type"] != "DATA_UNAVAILABLE"
            and row["status"] == "DELIVERED" and row["telegram_message_id"] for row in deliveries)
    checks["full_daily_data_confirmed"] = any(
        row["phase"] == "reconciliation_2200"
        and json.loads(row["payload_json"]).get("data", {}).get("is_complete") is True for row in evidence)
    checks["formal_decisions_consistent_or_corrected"] = (
        len(decisions) >= 2 and (len(set(decisions)) == 1
                                or any(row["event_type"] == "CORRECTION" for row in evidence)))
    ended = current >= datetime.combine(day, cfg.schedule.reconciliation, tz)
    status = "PASS" if ended and all(checks.values()) else "REVIEW_REQUIRED" if ended else "WAITING_SCHEDULE"
    return {"status": status, "checked_at": current.isoformat(), "target_day": day.isoformat(),
            "config_sha256": hashlib.sha256(cfg.source_path.read_bytes()).hexdigest(),
            "checks": checks, "jobs": jobs, "deliveries": deliveries,
            "pending_reconciliations": pending, "telegram_send_attempts": 0}


async def closeout_sweep(runtime, *, request_path: Path | None = None, now: datetime | None = None):
    """Finish an explicitly requested finite observation using the existing loop."""
    from .models import MonitorPhase, stable_event_id
    from .service import _trading_day_result

    path = request_path or get_workdata_path("runtime/etf_monitor/shadow-closeout.json")
    if not path.is_file():
        return
    request = json.loads(path.read_text(encoding="utf-8"))
    if request["status"] in {"COMPLETED", "REVIEW_REQUIRED"}:
        return
    day = date.fromisoformat(request["target_day"])
    current = now or datetime.now(ZoneInfo(runtime.config.timezone))
    # Allow the existing bounded 22:00 job and Telegram acknowledgement to finish.
    due = datetime.combine(day, runtime.config.schedule.reconciliation, current.tzinfo)
    due += timedelta(seconds=runtime.config.job_timeouts_seconds["reconciliation"] + 60)
    if current < due or runtime.startup_status != "READY":
        return
    with runtime.store.transaction() as connection:
        existing = connection.execute(
            "SELECT event_id FROM events WHERE trading_date=? AND event_type IN "
            "('SHADOW_OBSERVATION_ACCEPTED','SHADOW_OBSERVATION_REVIEW_REQUIRED') LIMIT 1",
            (day.isoformat(),)).fetchone()
    if existing is not None:
        event_id = existing[0]
    else:
        is_open, _ = _trading_day_result(await asyncio.to_thread(runtime.service.trading_day_predicate, day))
        result = inspect_day(day, database=runtime.store.path, now=current)
        result["checks"]["official_open_day"] = is_open
        result["checks"]["shadow_configuration"] = (
            runtime.config.enabled and runtime.config.mode == "shadow"
            and result["config_sha256"] == request["config_sha256"])
        passed = all(result["checks"].values())
        result["status"] = "PASS" if passed else "REVIEW_REQUIRED"
        kind = "SHADOW_OBSERVATION_ACCEPTED" if passed else "SHADOW_OBSERVATION_REVIEW_REQUIRED"
        event_id = stable_event_id(symbol="0050", strategy=runtime.config.strategy,
            phase=MonitorPhase.RECONCILIATION, signal_date=day, decision=kind)
        missing = "、".join(CHECK_LABELS.get(key, "驗收資料") for key, value in result["checks"].items() if not value)
        body = (f"0050 趨勢策略｜影子監控驗收{'完成' if passed else '待覆核'}\n"
                f"驗收日期：{day}\n"
                + ("三個時段、官方資料核對與通知回執已通過，歷史漏核已補齊。\n"
                   if passed else f"尚未通過：{missing}\n")
                + ("監控繼續以影子模式執行；正式交易建議模式仍需另行決定。" if passed
                   else "請先檢查未通過項目；本次驗收尚未結案。"))
        runtime.store.append_event_with_outbox(event_id=event_id, phase=MonitorPhase.RECONCILIATION,
            trading_date=day, event_type=kind, payload=result,
            target_chat_id=runtime.config.chat_id, body=body)
    event = runtime.store.event(event_id)
    evidence = json.loads(event["payload_json"])
    evidence_path = path.with_name("shadow-closeout-acceptance.json")
    evidence_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    # Delivery uncertainty stays in the original outbox and is never auto-resolved.
    await runtime.service._deliver(event_id)
    with runtime.store.transaction() as connection:
        receipt = connection.execute("SELECT status,telegram_message_id FROM outbox WHERE event_id=?", (event_id,)).fetchone()
    if receipt is not None and receipt[0] == "DELIVERED":
        request.update(status="COMPLETED" if evidence["status"] == "PASS" else "REVIEW_REQUIRED", event_id=event_id,
                       telegram_message_id=receipt[1], completed_at=current.isoformat())
        path.write_text(json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8")
