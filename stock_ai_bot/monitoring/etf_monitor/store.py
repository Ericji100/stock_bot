from __future__ import annotations

import json
import sqlite3
from contextlib import closing, contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
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


SCHEMA_VERSION = 6


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
                    fill_date TEXT,
                    fill_snapshot_id TEXT,
                    expected_fill_date TEXT,
                    expected_fill_calendar_json TEXT,
                    status TEXT NOT NULL,
                    source_event_id TEXT NOT NULL,
                    superseded_by_event_id TEXT,
                    superseded_at TEXT,
                    supersede_reason TEXT,
                    quarantined_by_event_id TEXT,
                    quarantined_at TEXT,
                    quarantine_reason TEXT,
                    reconciled_by_event_id TEXT,
                    reconciled_at TEXT,
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
                CREATE TABLE IF NOT EXISTS data_failures (
                    failure_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    trading_date TEXT NOT NULL,
                    exception_type TEXT NOT NULL,
                    reason_code TEXT NOT NULL,
                    message TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
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
                ("fill_date", "TEXT"),
                ("fill_snapshot_id", "TEXT"),
                ("expected_fill_date", "TEXT"),
                ("expected_fill_calendar_json", "TEXT"),
                ("quarantined_by_event_id", "TEXT"),
                ("quarantined_at", "TEXT"),
                ("quarantine_reason", "TEXT"),
                ("reconciled_by_event_id", "TEXT"),
                ("reconciled_at", "TEXT"),
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

    def record_data_failure(
        self, *, event_id: str, phase: MonitorPhase, trading_date: date,
        exception_type: str, reason_code: str, message: str,
        details: Mapping[str, Any],
    ) -> int:
        # One immutable record per failed attempt, separate from the idempotent
        # alert: changing clocks/errors must not collide or resend Telegram.
        with self.transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO data_failures(event_id, phase, trading_date, exception_type, "
                "reason_code, message, details_json, created_at) VALUES(?,?,?,?,?,?,?,?)",
                (event_id, phase.value, trading_date.isoformat(), exception_type,
                 reason_code, message, canonical_json(details), _utc_now()),
            )
            return int(cursor.lastrowid)

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
        action_id = f"pending:{event_id}"
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
        original_event_id: str | None,
        core: AcceptedCoreState,
    ) -> Mapping[str, Any]:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM pending_actions WHERE source_event_id=? "
                "AND status IN ('PENDING_NEXT_SESSION_OPEN',"
                "'QUARANTINED_RECONCILIATION_REQUIRED') "
                "ORDER BY created_at DESC LIMIT 1",
                (original_event_id,),
            ).fetchone()
        old_action = "HOLD" if row is None else str(row["action"])
        new_decision = core.decision
        new_action = new_decision.value
        old_leg_id = None if row is None else row["leg_id"]
        old_reference = None if row is None else row["signal_reference_price"]
        new_leg_id = (
            None
            if core.signal_transaction is None
            else str(core.signal_transaction.get("leg_id") or "")
        )
        new_reference = (
            None
            if core.signal_reference_price is None
            else str(core.signal_reference_price)
        )
        details_changed = row is not None and (
            old_leg_id != new_leg_id
            or (
                old_reference is None
                or new_reference is None
                or Decimal(str(old_reference)) != Decimal(str(new_reference))
            )
        )
        if row is None:
            outcome = (
                "UNCHANGED"
                if new_decision is Decision.HOLD
                else "CREATED_BY_RECONCILIATION"
            )
        elif new_action == old_action:
            if details_changed:
                outcome = "SUPERSEDED_BY_RECONCILIATION"
            elif row["status"] == "QUARANTINED_RECONCILIATION_REQUIRED":
                outcome = "RESTORED_AFTER_RECONCILIATION"
            else:
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
            "old_status": None if row is None else str(row["status"]),
            "old_leg_id": old_leg_id,
            "new_leg_id": new_leg_id,
            "old_signal_reference_price": old_reference,
            "new_signal_reference_price": new_reference,
        }

    def reconcile_pending_action(
        self,
        *,
        core: AcceptedCoreState,
        event_id: str,
        original_event_id: str | None,
        expected_fill_date: date | None = None,
        expected_fill_calendar_evidence: Mapping[str, Any] | None = None,
        planned_transition: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        if (
            original_event_id is None
            and expected_fill_date is None
            and planned_transition is None
        ):
            self.save_pending_action(core, event_id=event_id)
            return {
                "old_action": None,
                "new_action": core.decision.value,
                "outcome": "CREATED" if core.decision is not Decision.HOLD else "UNCHANGED",
            }
        transition = dict(
            planned_transition
            or self.describe_pending_transition(
                original_event_id=original_event_id,
                core=core,
            )
        )
        allowed_outcomes = {
            "UNCHANGED",
            "CREATED_BY_RECONCILIATION",
            "RESTORED_AFTER_RECONCILIATION",
            "CANCELLED_BY_RECONCILIATION",
            "SUPERSEDED_BY_RECONCILIATION",
        }
        if (
            transition.get("outcome") not in allowed_outcomes
            or transition.get("new_action") != core.decision.value
        ):
            raise EtfMonitorStateError("invalid durable pending-action transition plan")
        if (
            core.decision in {Decision.BUY_NEXT_OPEN, Decision.SELL_NEXT_OPEN}
            and expected_fill_date is None
        ):
            raise EtfMonitorStateError(
                "reconciled next-open action requires a fixed official next session"
            )
        if expected_fill_date is not None and expected_fill_calendar_evidence is None:
            raise EtfMonitorStateError(
                "reconciled next-open action requires official calendar evidence"
            )
        expected_calendar_json = (
            None
            if expected_fill_calendar_evidence is None
            else canonical_json(expected_fill_calendar_evidence)
        )
        now = _utc_now()
        with self.transaction() as connection:
            old_action_id = transition.get("old_action_id")
            old = (
                None
                if old_action_id is None
                else connection.execute(
                    "SELECT * FROM pending_actions WHERE action_id=?",
                    (str(old_action_id),),
                ).fetchone()
            )
            if old_action_id is not None and old is None:
                raise EtfMonitorStateError(
                    "durable pending-action transition references a missing action"
                )
            if old is not None and (
                original_event_id is None
                or old["source_event_id"] != original_event_id
                or old["action"] != transition.get("old_action")
            ):
                raise EtfMonitorStateError(
                    "durable pending-action transition does not match its source action"
                )
            outcome = str(transition["outcome"])
            if old is None:
                expected_outcome = (
                    "UNCHANGED"
                    if core.decision is Decision.HOLD
                    else "CREATED_BY_RECONCILIATION"
                )
                if outcome != expected_outcome:
                    raise EtfMonitorStateError(
                        "durable pending-action transition has no matching source action"
                    )
            elif outcome == "CANCELLED_BY_RECONCILIATION":
                if core.decision is not Decision.HOLD:
                    raise EtfMonitorStateError("only HOLD can cancel a pending action")
            elif outcome == "SUPERSEDED_BY_RECONCILIATION":
                if core.decision is Decision.HOLD:
                    raise EtfMonitorStateError("HOLD cannot create a replacement action")
            elif outcome in {"UNCHANGED", "RESTORED_AFTER_RECONCILIATION"}:
                if old["action"] != core.decision.value:
                    raise EtfMonitorStateError(
                        "durable unchanged transition does not match the current decision"
                    )
            if old is not None and transition["outcome"] == "RESTORED_AFTER_RECONCILIATION":
                connection.execute(
                    "UPDATE pending_actions SET status='PENDING_NEXT_SESSION_OPEN', "
                    "reconciled_by_event_id=?, reconciled_at=?, expected_fill_date=?, "
                    "expected_fill_calendar_json=?, updated_at=? "
                    "WHERE action_id=? AND status='QUARANTINED_RECONCILIATION_REQUIRED'",
                    (
                        event_id,
                        now,
                        expected_fill_date.isoformat(),
                        expected_calendar_json,
                        now,
                        old["action_id"],
                    ),
                )
            if old is not None and transition["outcome"] == "UNCHANGED":
                fixed_date = expected_fill_date.isoformat()
                if not (
                    old["status"] == "PENDING_NEXT_SESSION_OPEN"
                    and old["reconciled_by_event_id"] == event_id
                    and old["expected_fill_date"] == fixed_date
                    and old["expected_fill_calendar_json"] == expected_calendar_json
                ):
                    if old["status"] != "PENDING_NEXT_SESSION_OPEN":
                        raise EtfMonitorStateError(
                            "unchanged pending action is no longer fill-eligible"
                        )
                    connection.execute(
                        "UPDATE pending_actions SET reconciled_by_event_id=?, reconciled_at=?, "
                        "expected_fill_date=?, expected_fill_calendar_json=?, updated_at=? "
                        "WHERE action_id=? AND status='PENDING_NEXT_SESSION_OPEN'",
                        (
                            event_id,
                            now,
                            fixed_date,
                            expected_calendar_json,
                            now,
                            old["action_id"],
                        ),
                    )
            if old is not None and transition["outcome"] in {
                "CANCELLED_BY_RECONCILIATION",
                "SUPERSEDED_BY_RECONCILIATION",
            }:
                if not (
                    old["status"] == transition["outcome"]
                    and old["superseded_by_event_id"] == event_id
                ):
                    if old["status"] not in {
                        "PENDING_NEXT_SESSION_OPEN",
                        "QUARANTINED_RECONCILIATION_REQUIRED",
                    }:
                        raise EtfMonitorStateError(
                            "pending action was already changed by another event"
                        )
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
                old is None
                or transition["outcome"] == "SUPERSEDED_BY_RECONCILIATION"
            ):
                action_id = f"pending:{event_id}"
                connection.execute(
                    "INSERT INTO pending_actions(action_id, signal_date, action, leg_id, "
                    "signal_reference_price, fill_price, expected_fill_date, "
                    "expected_fill_calendar_json, status, source_event_id, "
                    "reconciled_by_event_id, reconciled_at, created_at, updated_at) "
                    "VALUES(?,?,?,?,?,NULL,?,?,'PENDING_NEXT_SESSION_OPEN',?,?,?,?,?) "
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
                        expected_fill_date.isoformat(),
                        expected_calendar_json,
                        event_id,
                        event_id,
                        now,
                        now,
                        now,
                    ),
                )
        return transition

    def shadow_fills_for_snapshot(
        self, *, snapshot_id: str
    ) -> tuple[Mapping[str, Any], ...]:
        """Return a stable projection of fills already written for one snapshot."""

        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT action_id, signal_date, action, leg_id, signal_reference_price, "
                "status, source_event_id, reconciled_by_event_id, expected_fill_date, "
                "fill_price, fill_date, fill_snapshot_id FROM pending_actions "
                "WHERE status='FILLED_SHADOW_NEXT_OPEN' AND fill_snapshot_id=? "
                "ORDER BY signal_date, action_id",
                (snapshot_id,),
            ).fetchall()
        return tuple(dict(row) for row in rows)

    def _has_successful_matching_reconciliation(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> bool:
        evidence_event_id = row["reconciled_by_event_id"]
        if evidence_event_id is None:
            return False
        evidence = connection.execute(
            "SELECT e.phase, e.trading_date, e.event_type, e.payload_json, "
            "j.status AS job_status FROM events e "
            "LEFT JOIN job_runs j ON j.run_key='reconciliation_2200:' || e.trading_date "
            "WHERE e.event_id=?",
            (evidence_event_id,),
        ).fetchone()
        if evidence is None:
            raise EtfMonitorStateError("pending action reconciliation event is missing")
        if evidence["job_status"] != "SUCCEEDED":
            return False
        if (
            evidence["phase"] != MonitorPhase.RECONCILIATION.value
            or evidence["trading_date"] != row["signal_date"]
            or evidence["event_type"] not in {"RECONCILIATION_CONFIRMED", "CORRECTION"}
        ):
            raise EtfMonitorStateError("pending action reconciliation evidence is inconsistent")
        try:
            payload = json.loads(str(evidence["payload_json"]))
        except (TypeError, ValueError) as exc:
            raise EtfMonitorStateError(
                "pending action reconciliation payload is invalid"
            ) from exc
        core = payload.get("core")
        data = payload.get("data")
        gate = payload.get("next_session_open_gate")
        transition = payload.get("pending_transition")
        if not all(isinstance(value, Mapping) for value in (core, data, gate, transition)):
            raise EtfMonitorStateError(
                "pending action lacks matching confirmation or correction evidence"
            )
        if core.get("decision") != row["action"] or data.get("is_complete") is not True:
            raise EtfMonitorStateError("pending action was not confirmed by complete data")
        if (
            gate.get("expected_fill_date") != row["expected_fill_date"]
            or row["expected_fill_calendar_json"] is None
            or canonical_json(gate.get("calendar_evidence"))
            != row["expected_fill_calendar_json"]
        ):
            raise EtfMonitorStateError("pending action next-session calendar pin is inconsistent")
        if transition.get("new_action") != row["action"]:
            raise EtfMonitorStateError("pending action does not match reconciliation decision")
        outcome = transition.get("outcome")
        if outcome in {"UNCHANGED", "RESTORED_AFTER_RECONCILIATION"}:
            if transition.get("old_action_id") != row["action_id"]:
                raise EtfMonitorStateError("confirmation does not identify the pending action")
        elif outcome in {
            "SUPERSEDED_BY_RECONCILIATION",
            "CREATED_BY_RECONCILIATION",
        }:
            if row["source_event_id"] != evidence_event_id:
                raise EtfMonitorStateError(
                    "reconciliation does not identify the created replacement action"
                )
            if (
                outcome == "CREATED_BY_RECONCILIATION"
                and transition.get("old_action_id") is not None
            ):
                raise EtfMonitorStateError(
                    "new EOD instruction unexpectedly references an old pending action"
                )
        else:
            raise EtfMonitorStateError("pending action evidence is not fill-eligible")
        return True

    def confirmed_pending_actions_due_by(
        self, *, trading_date: date
    ) -> tuple[Mapping[str, Any], ...]:
        """Return successfully confirmed actions whose fixed next session has arrived."""

        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM pending_actions WHERE status='PENDING_NEXT_SESSION_OPEN' "
                "AND expected_fill_date IS NOT NULL AND expected_fill_date<=? "
                "ORDER BY expected_fill_date, signal_date, created_at, action_id",
                (trading_date.isoformat(),),
            ).fetchall()
            ready = [
                dict(row)
                for row in rows
                if self._has_successful_matching_reconciliation(connection, row)
            ]
        if len(ready) > 1:
            raise EtfMonitorStateError(
                "multiple confirmed pending actions require manual reconciliation"
            )
        return tuple(ready)

    def quarantine_pending_for_reconciliation(
        self,
        *,
        original_event_id: str,
        failure_event_id: str,
    ) -> Mapping[str, Any] | None:
        """Fail closed when full-day confirmation is unavailable.

        A quarantined instruction is deliberately excluded from next-open
        settlement until a later successful reconciliation restores or
        supersedes it.
        """

        now = _utc_now()
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM pending_actions WHERE source_event_id=? "
                "AND status IN ('PENDING_NEXT_SESSION_OPEN',"
                "'QUARANTINED_RECONCILIATION_REQUIRED') "
                "ORDER BY created_at DESC LIMIT 1",
                (original_event_id,),
            ).fetchone()
            if row is None:
                return None
            if row["status"] == "PENDING_NEXT_SESSION_OPEN":
                connection.execute(
                    "UPDATE pending_actions SET "
                    "status='QUARANTINED_RECONCILIATION_REQUIRED', "
                    "quarantined_by_event_id=?, quarantined_at=?, quarantine_reason=?, "
                    "updated_at=? WHERE action_id=? AND status='PENDING_NEXT_SESSION_OPEN'",
                    (
                        failure_event_id,
                        now,
                        "FULL_DAY_OFFICIAL_DATA_UNAVAILABLE",
                        now,
                        row["action_id"],
                    ),
                )
            return {
                **dict(row),
                "status": "QUARANTINED_RECONCILIATION_REQUIRED",
                "quarantined_by_event_id": (
                    row["quarantined_by_event_id"] or failure_event_id
                ),
                "quarantined_at": row["quarantined_at"] or now,
                "quarantine_reason": "FULL_DAY_OFFICIAL_DATA_UNAVAILABLE",
            }

    def settle_pending_actions_at_open(
        self,
        *,
        trading_date: date,
        raw_open: Decimal,
        snapshot_id: str,
    ) -> tuple[Mapping[str, Any], ...]:
        """Atomically fill only at the fixed official next-session raw open."""

        if raw_open <= 0:
            raise EtfMonitorStateError("shadow next-open fill requires a positive raw open")
        now = _utc_now()
        with self.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM pending_actions WHERE status='PENDING_NEXT_SESSION_OPEN' "
                "AND expected_fill_date=? ORDER BY signal_date, created_at, action_id",
                (trading_date.isoformat(),),
            ).fetchall()
            eligible = [
                row
                for row in rows
                if self._has_successful_matching_reconciliation(connection, row)
            ]
            if len(eligible) > 1:
                raise EtfMonitorStateError(
                    "multiple prior-session pending actions require manual reconciliation"
                )
            for row in eligible:
                connection.execute(
                    "UPDATE pending_actions SET status='FILLED_SHADOW_NEXT_OPEN', "
                    "fill_price=?, fill_date=?, fill_snapshot_id=?, updated_at=? "
                    "WHERE action_id=? AND status='PENDING_NEXT_SESSION_OPEN'",
                    (
                        str(raw_open),
                        trading_date.isoformat(),
                        snapshot_id,
                        now,
                        row["action_id"],
                    ),
                )
            return tuple(
                {
                    **dict(row),
                    "status": "FILLED_SHADOW_NEXT_OPEN",
                    "fill_price": str(raw_open),
                    "fill_date": trading_date.isoformat(),
                    "fill_snapshot_id": snapshot_id,
                }
                for row in eligible
            )

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
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM monitor_state WHERE singleton=1").fetchone()
            return None if row is None else dict(row)

    def event(self, event_id: str) -> Mapping[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
            return None if row is None else dict(row)

    def latest_formal_phase_event(
        self,
        *,
        phase: MonitorPhase,
        trading_date: date,
    ) -> Mapping[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM events WHERE phase=? AND trading_date=? "
                "AND event_type!='DATA_UNAVAILABLE' "
                "ORDER BY created_at DESC, event_id DESC LIMIT 1",
                (phase.value, trading_date.isoformat()),
            ).fetchone()
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
        with closing(self._connect()) as connection:
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
