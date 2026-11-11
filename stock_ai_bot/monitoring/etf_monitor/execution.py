"""Record notification timing separately from the strategy's shadow fills."""
from __future__ import annotations

import json
from datetime import datetime, time
from zoneinfo import ZoneInfo

from .models import MonitorPhase, stable_sha256


TAIPEI = ZoneInfo("Asia/Taipei")
TRADE_DECISIONS = {"BUY_NEXT_OPEN", "SELL_NEXT_OPEN"}


def _timestamp(value):
    if not value:
        return None
    try:
        result = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return result.astimezone(TAIPEI) if result.tzinfo is not None else None


def _assess(connection, event_id, now):
    row = connection.execute(
        "SELECT e.*,o.status AS delivery_status,o.delivered_at,o.body FROM events e "
        "JOIN outbox o USING(event_id) WHERE e.event_id=?", (event_id,),
    ).fetchone()
    if row is None or row["phase"] not in {MonitorPhase.DECISION.value, MonitorPhase.RECONCILIATION.value}:
        return None
    payload = json.loads(row["payload_json"])
    if payload.get("core", {}).get("decision") not in TRADE_DECISIONS:
        return None
    window = payload.get("execution_window") or {}
    expected = _timestamp(window.get("expected_open_at")) if isinstance(window, dict) else None
    if expected is None:
        pinned = payload.get("next_session_open_gate") or {}
        day = pinned.get("expected_fill_date")
        if day is None:
            pending = connection.execute(
                "SELECT expected_fill_date FROM pending_actions WHERE source_event_id=? "
                "OR reconciled_by_event_id=? ORDER BY created_at DESC LIMIT 1", (event_id, event_id),
            ).fetchone()
            day = None if pending is None else pending[0]
        if day:
            expected = _timestamp(f"{day}T09:00:00+08:00")
    if expected is not None and (expected.date().isoformat() <= row["trading_date"] or expected.time() != time(9)):
        expected = None
    delivered = _timestamp(row["delivered_at"])
    superseded = connection.execute(
        "SELECT 1 FROM events WHERE supersedes_event_id=? LIMIT 1", (event_id,),
    ).fetchone() is not None
    job = connection.execute("SELECT status FROM job_runs WHERE run_key=?",
                             (f"reconciliation_2200:{row['trading_date']}",)).fetchone()
    confirmed = (row["phase"] == MonitorPhase.RECONCILIATION.value
                 and payload.get("data", {}).get("is_complete") is True
                 and job is not None and job[0] == "SUCCEEDED")
    status = "PENDING_DELIVERY"
    if superseded:
        status = "SUPERSEDED"
    elif expected is None:
        status = "LEGACY_WINDOW_UNKNOWN"
    elif row["delivery_status"] == "DELIVERY_UNCERTAIN":
        status = "DELIVERY_UNCERTAIN"
    elif delivered is not None and delivered >= expected:
        status = "LATE_DELIVERY"
    elif now >= expected:
        status = "WINDOW_CLOSED"
    elif row["delivery_status"] == "DELIVERED_MANUAL":
        # Manual resolution time is not proof of when the user could first act.
        status = "MANUAL_TIMING_REVIEW"
    elif delivered is not None and row["delivery_status"] == "DELIVERED":
        status = "CONFIRMED_BEFORE_OPEN" if confirmed else "PROVISIONAL_BEFORE_OPEN"
    prior = connection.execute("SELECT original_body FROM execution_status WHERE event_id=?", (event_id,)).fetchone()
    original = row["body"] if prior is None else prior[0]
    connection.execute(
        "INSERT INTO execution_status(event_id,original_body,expected_open_at,delivery_status,"
        "executable_status,delivered_at,checked_at) VALUES(?,?,?,?,?,?,?) "
        "ON CONFLICT(event_id) DO UPDATE SET expected_open_at=excluded.expected_open_at,"
        "delivery_status=excluded.delivery_status,executable_status=excluded.executable_status,"
        "delivered_at=excluded.delivered_at,checked_at=excluded.checked_at",
        (event_id, original, None if expected is None else expected.isoformat(), row["delivery_status"],
         status, row["delivered_at"], now.isoformat()),
    )
    return original, expected, status, payload.get("data", {}).get("is_complete") is True


def prepare_execution_message(store, event_id: str, *, now: datetime | None = None) -> str | None:
    current = (now or datetime.now(TAIPEI)).astimezone(TAIPEI)
    with store.transaction() as connection:
        result = _assess(connection, event_id, current)
        if result is None:
            return None
        body, expected, status, complete = result
        if status in {"SUPERSEDED", "LEGACY_WINDOW_UNKNOWN"}:
            note = "原交易安排已更新或原定執行時間缺少證據，請先人工覆核。"
        elif expected is not None and current >= expected:
            note = (f"原定開盤 {expected:%Y-%m-%d %H:%M} 已過。\n"
                    "原定開盤價已無法據以執行，請先人工覆核目前部位與行情。\n"
                    "影子模型補帳僅供歷史參考，實際成交須另行記錄。")
        else:
            note = (f"原定執行時間 {expected:%Y-%m-%d %H:%M}。\n"
                    + ("本次採完整日資料，請依實際收到時間與部位覆核安排。"
                       if complete else "收盤判讀尚待晚間完整資料確認，請留意後續更正。"))
        if expected is None or current >= expected or status == "SUPERSEDED":
            body = body.replace("策略安排：下一交易日開盤進場", "原策略安排：歷史開盤進場參考")
            body = body.replace("策略安排：下一交易日開盤出場", "原策略安排：歷史開盤出場參考")
        text = f"0050 趨勢策略｜交易安排時間檢查\n{note}\n\n{body}"
        if expected is not None and current < expected and status != "SUPERSEDED":
            from .sizing_config import load_reply_config

            try:
                sizing_enabled = load_reply_config().enabled
            except (ValueError, OSError, TypeError):
                sizing_enabled = False
            if sizing_enabled:
                payload = json.loads(connection.execute(
                    "SELECT payload_json FROM events WHERE event_id=?", (event_id,)).fetchone()[0])
                if payload.get("core", {}).get("decision") == "BUY_NEXT_OPEN":
                    text += ("\n\n股數試算：回覆這則進場通知，輸入「可用現金」或「可用現金 委託價」"
                             "（例：100000 123.4）。僅授權帳號可用；金額會在群組可見。")
        connection.execute(
            "UPDATE outbox SET body=?,body_sha256=? WHERE event_id=? AND status='SENDING'",
            (text, stable_sha256(text), event_id),
        )
        return text


def refresh_execution_status(store, *, now: datetime | None = None, event_id: str | None = None) -> None:
    current = (now or datetime.now(TAIPEI)).astimezone(TAIPEI)
    with store.transaction() as connection:
        event_ids = [row[0] for row in connection.execute(
            "SELECT e.event_id FROM events e JOIN outbox o USING(event_id) "
            "WHERE e.phase IN ('decision_1355','reconciliation_2200') "
            "AND json_extract(e.payload_json,'$.core.decision') IN ('BUY_NEXT_OPEN','SELL_NEXT_OPEN')"
            + (" AND e.event_id=?" if event_id is not None else ""),
            () if event_id is None else (event_id,),
        )]
        for event_id in event_ids:
            _assess(connection, event_id, current)
