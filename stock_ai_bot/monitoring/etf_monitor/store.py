from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

from .errors import EtfMonitorStateError
from .models import (
    AcceptedCoreState,
    Decision,
    MarketDataSnapshot,
    MonitorPhase,
    OutboxItem,
    accepted_core_payload,
    canonical_json,
    stable_sha256,
)


SCHEMA_VERSION = 2


def _utc_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


class EtfMonitorStore:
    def __init__(self, path: Path | str):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _migrate(self) -> None:
        with self.transaction() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS monitor_state (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    symbol TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    as_of TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    position_status TEXT NOT NULL,
                    active_leg_id TEXT,
                    entry_signal_date TEXT,
                    entry_reference_price TEXT,
                    fixed_initial_stop TEXT,
                    macd_armed INTEGER NOT NULL,
                    data_source TEXT NOT NULL,
                    price_basis TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL,
                    snapshot_version TEXT NOT NULL,
                    snapshot_sha256 TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    core_state_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS pending_actions (
                    action_id TEXT PRIMARY KEY,
                    signal_date TEXT NOT NULL,
                    action TEXT NOT NULL,
                    leg_id TEXT,
                    signal_reference_price TEXT,
                    fill_price TEXT,
                    status TEXT NOT NULL,
                    source_event_id TEXT NOT NULL,
                    superseded_by_event_id TEXT,
                    superseded_at TEXT,
                    supersede_reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    phase TEXT NOT NULL,
                    trading_date TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    supersedes_event_id TEXT,
                    is_test INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS outbox (
                    event_id TEXT PRIMARY KEY REFERENCES events(event_id),
                    target_chat_id TEXT NOT NULL,
                    body TEXT NOT NULL,
                    body_sha256 TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    telegram_message_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    delivered_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_outbox_status ON outbox(status, created_at);
                CREATE TABLE IF NOT EXISTS corrections (
                    correction_id TEXT PRIMARY KEY,
                    original_event_id TEXT NOT NULL,
                    corrected_event_id TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS corporate_actions (
                    action_id TEXT PRIMARY KEY,
                    effective_date TEXT NOT NULL,
                    action_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    first_seen_snapshot_id TEXT NOT NULL,
                    applied_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS job_runs (
                    run_key TEXT PRIMARY KEY,
                    phase TEXT NOT NULL,
                    trading_date TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error TEXT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT
                );
                CREATE TABLE IF NOT EXISTS routing_smoke (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    target_chat_id TEXT NOT NULL,
                    telegram_message_id TEXT,
                    sent_at TEXT NOT NULL
                );
                """
            )
            row = connection.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is not None and int(row["value"]) > SCHEMA_VERSION:
                raise EtfMonitorStateError("ETF monitor database schema is newer than this code")
            columns = {
                item["name"]
                for item in connection.execute("PRAGMA table_info(pending_actions)").fetchall()
            }
            for name, declaration in (
                ("superseded_by_event_id", "TEXT"),
                ("superseded_at", "TEXT"),
                ("supersede_reason", "TEXT"),
            ):
                if name not in columns:
                    connection.execute(
                        f"ALTER TABLE pending_actions ADD COLUMN {name} {declaration}"
                    )
            connection.execute(
                "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )
    def recover_after_restart(self) -> None:
        now = _utc_now()
        with self.transaction() as connection:
            connection.execute(
                "UPDATE outbox SET status='DELIVERY_UNCERTAIN', "
                "last_error='SEND_RESULT_UNKNOWN_AFTER_RESTART_MANUAL_REVIEW_REQUIRED', "
                "updated_at=? WHERE status='SENDING'",
                (now,),
            )
            connection.execute(
                "UPDATE job_runs SET status='INTERRUPTED', error='RECOVERED_AFTER_RESTART', "
                "finished_at=? WHERE status='RUNNING'",
                (now,),
            )

    def begin_job(self, phase: MonitorPhase, trading_date: date) -> bool:
        run_key = f"{phase.value}:{trading_date.isoformat()}"
        now = _utc_now()
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT status FROM job_runs WHERE run_key=?", (run_key,)
            ).fetchone()
            if row is not None and row["status"] in {"RUNNING", "SUCCEEDED"}:
                return False
            connection.execute(
                "INSERT INTO job_runs(run_key, phase, trading_date, status, started_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(run_key) DO UPDATE SET status='RUNNING', "
                "error=NULL, started_at=excluded.started_at, finished_at=NULL",
                (run_key, phase.value, trading_date.isoformat(), "RUNNING", now),
            )
        return True

    def finish_job(
        self,
        phase: MonitorPhase,
        trading_date: date,
        *,
        succeeded: bool,
        error: str | None = None,
    ) -> None:
        run_key = f"{phase.value}:{trading_date.isoformat()}"
        with self.transaction() as connection:
            connection.execute(
                "UPDATE job_runs SET status=?, error=?, finished_at=? WHERE run_key=?",
                ("SUCCEEDED" if succeeded else "FAILED", error, _utc_now(), run_key),
            )

    def append_event_with_outbox(
        self,
        *,
        event_id: str,
        phase: MonitorPhase,
        trading_date: date,
        event_type: str,
        payload: Mapping[str, Any],
        target_chat_id: str | None,
        body: str | None,
        supersedes_event_id: str | None = None,
        is_test: bool = False,
    ) -> bool:
        now = _utc_now()
        body_sha = None if body is None else stable_sha256(body)
        payload_json = canonical_json(payload)
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT payload_json FROM events WHERE event_id=?", (event_id,)
            ).fetchone()
            if existing is not None:
                if existing["payload_json"] != payload_json:
                    raise EtfMonitorStateError("stable event id collided with different payload")
                return False
            connection.execute(
                "INSERT INTO events(event_id, phase, trading_date, event_type, payload_json, "
                "supersedes_event_id, is_test, created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    event_id,
                    phase.value,
                    trading_date.isoformat(),
                    event_type,
                    payload_json,
                    supersedes_event_id,
                    int(is_test),
                    now,
                ),
            )
            if body is not None:
                if target_chat_id is None:
                    raise EtfMonitorStateError("outbox body requires a target chat id")
                connection.execute(
                    "INSERT INTO outbox(event_id, target_chat_id, body, body_sha256, status, "
                    "created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
                    (event_id, target_chat_id, body, body_sha, "PENDING", now, now),
                )
        return True

    def save_core_state(
        self,
        *,
        core: AcceptedCoreState,
        snapshot: MarketDataSnapshot,
        event_id: str,
    ) -> None:
        state_payload = {
            "core": accepted_core_payload(core),
            "data": {
                "source": snapshot.source,
                "price_basis": snapshot.price_basis,
                "as_of": snapshot.as_of,
                "snapshot_id": snapshot.snapshot_id,
                "snapshot_version": snapshot.snapshot_version,
                "snapshot_sha256": snapshot.snapshot_sha256,
                "is_complete": snapshot.is_complete,
                "is_provisional": snapshot.is_provisional,
            },
        }
        entry = None
        for transaction in reversed(core.core_transactions):
            if transaction.get("role") == "MOTHER" and transaction.get("leg_id") == core.active_leg_id:
                entry = transaction
                break
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO monitor_state(
                    singleton, symbol, strategy, as_of, decision, position_status,
                    active_leg_id, entry_signal_date, entry_reference_price,
                    fixed_initial_stop, macd_armed, data_source, price_basis,
                    snapshot_id, snapshot_version, snapshot_sha256, event_id,
                    core_state_json, updated_at
                ) VALUES(1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(singleton) DO UPDATE SET
                    symbol=excluded.symbol, strategy=excluded.strategy, as_of=excluded.as_of,
                    decision=excluded.decision, position_status=excluded.position_status,
                    active_leg_id=excluded.active_leg_id,
                    entry_signal_date=excluded.entry_signal_date,
                    entry_reference_price=excluded.entry_reference_price,
                    fixed_initial_stop=excluded.fixed_initial_stop,
                    macd_armed=excluded.macd_armed, data_source=excluded.data_source,
                    price_basis=excluded.price_basis, snapshot_id=excluded.snapshot_id,
                    snapshot_version=excluded.snapshot_version,
                    snapshot_sha256=excluded.snapshot_sha256, event_id=excluded.event_id,
                    core_state_json=excluded.core_state_json, updated_at=excluded.updated_at
                """,
                (
                    core.symbol,
                    core.strategy,
                    core.as_of.isoformat(),
                    core.decision.value,
                    "LONG" if core.active_position else "FLAT",
                    core.active_leg_id,
                    None if entry is None else entry["date"].isoformat(),
                    None if entry is None else str(entry["reference_price"]),
                    None if core.fixed_initial_stop is None else str(core.fixed_initial_stop),
                    int(core.macd_armed),
                    snapshot.source,
                    snapshot.price_basis,
                    snapshot.snapshot_id,
                    snapshot.snapshot_version,
                    snapshot.snapshot_sha256,
                    event_id,
                    canonical_json(state_payload),
                    _utc_now(),
                ),
            )

    def save_pending_action(self, core: AcceptedCoreState, *, event_id: str) -> None:
        if core.decision not in {Decision.BUY_NEXT_OPEN, Decision.SELL_NEXT_OPEN}:
            return
        action_id = f"{core.strategy}:{core.as_of.isoformat()}:{core.decision.value}"
        now = _utc_now()
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO pending_actions(action_id, signal_date, action, leg_id, "
                "signal_reference_price, fill_price, status, source_event_id, created_at, updated_at) "
                "VALUES(?,?,?,?,?,NULL,'PENDING_NEXT_SESSION_OPEN',?,?,?) "
                "ON CONFLICT(action_id) DO NOTHING",
                (
                    action_id,
                    core.as_of.isoformat(),
                    core.decision.value,
                    (
                        None
                        if core.signal_transaction is None
                        else str(core.signal_transaction.get("leg_id") or "")
                    ),
                    None if core.signal_reference_price is None else str(core.signal_reference_price),
                    event_id,
                    now,
                    now,
                ),
            )

    def describe_pending_transition(
        self,
        *,
        original_event_id: str,
        new_decision: Decision,
    ) -> Mapping[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM pending_actions WHERE source_event_id=? "
                "AND status='PENDING_NEXT_SESSION_OPEN' ORDER BY created_at DESC LIMIT 1",
                (original_event_id,),
            ).fetchone()
        old_action = "HOLD" if row is None else str(row["action"])
        new_action = new_decision.value
        if row is None:
            outcome = (
                "UNCHANGED"
                if new_decision is Decision.HOLD
                else "CREATED_BY_RECONCILIATION"
            )
        elif new_action == old_action:
            outcome = "UNCHANGED"
        elif new_decision is Decision.HOLD:
            outcome = "CANCELLED_BY_RECONCILIATION"
        else:
            outcome = "SUPERSEDED_BY_RECONCILIATION"
        return {
            "old_action": old_action,
            "new_action": new_action,
            "outcome": outcome,
            "old_action_id": None if row is None else str(row["action_id"]),
        }

    def reconcile_pending_action(
        self,
        *,
        core: AcceptedCoreState,
        event_id: str,
        original_event_id: str | None,
    ) -> Mapping[str, Any]:
        if original_event_id is None:
            self.save_pending_action(core, event_id=event_id)
            return {
                "old_action": None,
                "new_action": core.decision.value,
                "outcome": "CREATED" if core.decision is not Decision.HOLD else "UNCHANGED",
            }
        transition = dict(self.describe_pending_transition(
            original_event_id=original_event_id,
            new_decision=core.decision,
        ))
        now = _utc_now()
        with self.transaction() as connection:
            old = connection.execute(
                "SELECT * FROM pending_actions WHERE source_event_id=? "
                "AND status='PENDING_NEXT_SESSION_OPEN' ORDER BY created_at DESC LIMIT 1",
                (original_event_id,),
            ).fetchone()
            if old is not None and transition["outcome"] in {
                "CANCELLED_BY_RECONCILIATION",
                "SUPERSEDED_BY_RECONCILIATION",
            }:
                connection.execute(
                    "UPDATE pending_actions SET status=?, superseded_by_event_id=?, "
                    "superseded_at=?, supersede_reason=?, updated_at=? WHERE action_id=?",
                    (
                        transition["outcome"],
                        event_id,
                        now,
                        "FULL_DAY_ACCEPTED_DATA_CHANGED_MATERIAL_STATE",
                        now,
                        old["action_id"],
                    ),
                )
            if core.decision in {Decision.BUY_NEXT_OPEN, Decision.SELL_NEXT_OPEN} and (
                old is None or str(old["action"]) != core.decision.value
            ):
                action_id = f"{core.strategy}:{core.as_of.isoformat()}:{core.decision.value}"
                connection.execute(
                    "INSERT INTO pending_actions(action_id, signal_date, action, leg_id, "
                    "signal_reference_price, fill_price, status, source_event_id, "
                    "created_at, updated_at) VALUES(?,?,?,?,?,NULL,'PENDING_NEXT_SESSION_OPEN',?,?,?) "
                    "ON CONFLICT(action_id) DO NOTHING",
                    (
                        action_id,
                        core.as_of.isoformat(),
                        core.decision.value,
                        None if core.signal_transaction is None else str(
                            core.signal_transaction.get("leg_id") or ""
                        ),
                        None if core.signal_reference_price is None else str(
                            core.signal_reference_price
                        ),
                        event_id,
                        now,
                        now,
                    ),
                )
        return transition

    def record_corporate_actions(self, snapshot: MarketDataSnapshot) -> int:
        count = 0
        with self.transaction() as connection:
            for event in snapshot.corporate_actions:
                cursor = connection.execute(
                    "INSERT INTO corporate_actions(action_id, effective_date, action_type, "
                    "payload_json, first_seen_snapshot_id, applied_at) VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(action_id) DO NOTHING",
                    (
                        event.action_id,
                        event.effective_date.isoformat(),
                        event.action_type.value,
                        canonical_json(event),
                        snapshot.snapshot_id,
                        _utc_now(),
                    ),
                )
                count += cursor.rowcount
        return count

    def latest_state(self) -> Mapping[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM monitor_state WHERE singleton=1").fetchone()
            return None if row is None else dict(row)

    def event(self, event_id: str) -> Mapping[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
            return None if row is None else dict(row)

    def claim_outbox(self, event_id: str | None = None) -> OutboxItem | None:
        with self.transaction() as connection:
            if event_id is None:
                row = connection.execute(
                    "SELECT * FROM outbox WHERE status IN ('PENDING','RETRY') "
                    "ORDER BY created_at LIMIT 1"
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM outbox WHERE event_id=? AND status IN ('PENDING','RETRY')",
                    (event_id,),
                ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE outbox SET status='SENDING', attempts=attempts+1, updated_at=? "
                "WHERE event_id=?",
                (_utc_now(), row["event_id"]),
            )
            return OutboxItem(
                event_id=row["event_id"],
                target_chat_id=row["target_chat_id"],
                body=row["body"],
                body_sha256=row["body_sha256"],
                attempts=int(row["attempts"]) + 1,
                status="SENDING",
            )

    def mark_outbox_delivered(self, event_id: str, telegram_message_id: str) -> None:
        now = _utc_now()
        with self.transaction() as connection:
            connection.execute(
                "UPDATE outbox SET status='DELIVERED', telegram_message_id=?, "
                "last_error=NULL, updated_at=?, delivered_at=? WHERE event_id=?",
                (telegram_message_id, now, now, event_id),
            )

    def mark_outbox_retry(self, event_id: str, error: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                "UPDATE outbox SET status='RETRY', last_error=?, updated_at=? WHERE event_id=?",
                (error[:500], _utc_now(), event_id),
            )

    def mark_outbox_uncertain(self, event_id: str, error: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                "UPDATE outbox SET status='DELIVERY_UNCERTAIN', last_error=?, updated_at=? "
                "WHERE event_id=?",
                (error[:500], _utc_now(), event_id),
            )

    def resolve_uncertain_delivery(
        self,
        event_id: str,
        *,
        delivered: bool,
        telegram_message_id: str | None = None,
    ) -> None:
        """Resolve an ambiguous send only after explicit operator verification."""

        with self.transaction() as connection:
            row = connection.execute(
                "SELECT status FROM outbox WHERE event_id=?", (event_id,)
            ).fetchone()
            if row is None or row["status"] != "DELIVERY_UNCERTAIN":
                raise EtfMonitorStateError("outbox event is not awaiting manual delivery review")
            now = _utc_now()
            if delivered:
                connection.execute(
                    "UPDATE outbox SET status='DELIVERED_MANUAL', telegram_message_id=?, "
                    "last_error=NULL, delivered_at=?, updated_at=? WHERE event_id=?",
                    (telegram_message_id, now, now, event_id),
                )
            else:
                connection.execute(
                    "UPDATE outbox SET status='RETRY', "
                    "last_error='MANUAL_REVIEW_CONFIRMED_NOT_DELIVERED', updated_at=? "
                    "WHERE event_id=?",
                    (now, event_id),
                )

    def append_correction(
        self,
        *,
        correction_id: str,
        original_event_id: str,
        corrected_event_id: str,
        reason: str,
        payload: Mapping[str, Any],
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO corrections(correction_id, original_event_id, corrected_event_id, "
                "reason, payload_json, created_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(correction_id) DO NOTHING",
                (
                    correction_id,
                    original_event_id,
                    corrected_event_id,
                    reason,
                    canonical_json(payload),
                    _utc_now(),
                ),
            )

    def routing_smoke_sent(self) -> bool:
        with self._connect() as connection:
            return connection.execute(
                "SELECT 1 FROM routing_smoke WHERE singleton=1"
            ).fetchone() is not None

    def reserve_routing_smoke(self, target_chat_id: str) -> bool:
        with self.transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO routing_smoke(singleton, target_chat_id, telegram_message_id, sent_at) "
                "VALUES(1,?,NULL,?) ON CONFLICT(singleton) DO NOTHING",
                (target_chat_id, _utc_now()),
            )
            return cursor.rowcount == 1

    def mark_routing_smoke_sent(self, target_chat_id: str, message_id: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                "UPDATE routing_smoke SET target_chat_id=?, telegram_message_id=?, sent_at=? "
                "WHERE singleton=1",
                (target_chat_id, message_id, _utc_now()),
            )
