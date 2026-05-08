from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import CommandRequest, ReportArtifacts, SourceItem

SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
    report_id TEXT PRIMARY KEY,
    report_type TEXT NOT NULL,
    target TEXT,
    mode TEXT NOT NULL,
    report_date TEXT NOT NULL,
    summary TEXT NOT NULL,
    markdown_path TEXT NOT NULL,
    html_path TEXT NOT NULL,
    json_path TEXT NOT NULL,
    sources_path TEXT NOT NULL,
    ai_used INTEGER NOT NULL,
    fallback_reason TEXT,
    request_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    source_level TEXT NOT NULL,
    published_date TEXT,
    snippet TEXT,
    FOREIGN KEY(report_id) REFERENCES reports(report_id)
);

CREATE TABLE IF NOT EXISTS events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    target TEXT NOT NULL,
    title TEXT NOT NULL,
    source_url TEXT,
    source_level TEXT,
    published_date TEXT,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_target_date ON events(target, published_date);
CREATE INDEX IF NOT EXISTS idx_events_type_date ON events(event_type, published_date);

CREATE TABLE IF NOT EXISTS source_snapshots (
    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    target TEXT NOT NULL,
    command TEXT NOT NULL,
    source_url TEXT NOT NULL,
    title TEXT NOT NULL,
    source_level TEXT,
    published_date TEXT,
    fetched_at TEXT NOT NULL,
    report_date TEXT,
    content_type TEXT NOT NULL,
    content_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_snapshots_target_date ON source_snapshots(target, published_date, fetched_at);
CREATE INDEX IF NOT EXISTS idx_snapshots_url ON source_snapshots(source_url);
"""


class ResearchDatabase:
    def __init__(self, path: Path):
        self.path = path
        self._memory_uri: str | None = None
        self._memory_anchor: sqlite3.Connection | None = None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.init_schema()
        except sqlite3.OperationalError as exc:
            if not _is_recoverable_sqlite_error(exc):
                raise
            self._recover_corrupt_database(exc)
            try:
                self.init_schema()
            except sqlite3.OperationalError as second_exc:
                if not _is_recoverable_sqlite_error(second_exc):
                    raise
                self._use_memory_fallback()
                self.init_schema()

    def _recover_corrupt_database(self, exc: sqlite3.OperationalError) -> None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = self.path.with_suffix(self.path.suffix + f".corrupt_{stamp}")
        journal = self.path.with_name(self.path.name + "-journal")
        try:
            if journal.exists():
                journal.replace(journal.with_suffix(journal.suffix + f".corrupt_{stamp}"))
            if self.path.exists():
                self.path.replace(backup)
        except OSError:
            # If Windows keeps the broken DB/journal locked, keep it untouched and
            # continue with a fresh sidecar DB so the bot can still start.
            self.path = self.path.with_name(f"{self.path.stem}.recovered_{stamp}{self.path.suffix}")

    def _use_memory_fallback(self) -> None:
        self._memory_uri = "file:stock_research_fallback?mode=memory&cache=shared"
        self._memory_anchor = sqlite3.connect(self._memory_uri, uri=True)

    def connect(self) -> sqlite3.Connection:
        if self._memory_uri:
            return sqlite3.connect(self._memory_uri, uri=True)
        return sqlite3.connect(self.path)

    def init_schema(self) -> None:
        with self.connect() as connection:
            connection.executescript(SCHEMA)

    def save_report(
        self,
        request: CommandRequest,
        artifacts: ReportArtifacts,
        summary: str,
        sources: list[SourceItem],
        ai_used: bool,
        fallback_reason: str | None,
    ) -> None:
        payload = _request_to_json(request)
        with self.connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO reports (
                    report_id, report_type, target, mode, report_date, summary,
                    markdown_path, html_path, json_path, sources_path, ai_used,
                    fallback_reason, request_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifacts.report_id,
                    artifacts.report_type,
                    request.target or request.market_scope or request.candidate_pool,
                    request.mode,
                    (request.report_date.isoformat() if request.report_date else datetime.now().date().isoformat()),
                    summary,
                    str(artifacts.markdown_path),
                    str(artifacts.html_path),
                    str(artifacts.json_path),
                    str(artifacts.sources_path),
                    1 if ai_used else 0,
                    fallback_reason,
                    json.dumps(payload, ensure_ascii=False, default=str),
                    datetime.now().astimezone().isoformat(timespec="seconds"),
                ),
            )
            connection.execute("DELETE FROM sources WHERE report_id = ?", (artifacts.report_id,))
            connection.executemany(
                """
                INSERT INTO sources (report_id, source_id, title, url, source_level, published_date, snippet)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        artifacts.report_id,
                        item.source_id,
                        item.title,
                        item.url,
                        item.source_level,
                        item.published_date,
                        item.snippet,
                    )
                    for item in sources
                ],
            )

    def latest_report(self, target: str | None = None, report_type: str | None = None, report_date: str | None = None) -> dict[str, Any] | None:
        clauses: list[str] = []
        params: list[str] = []
        if target and target != "latest":
            clauses.append("target LIKE ?")
            params.append(f"%{target}%")
        if report_type:
            clauses.append("report_type = ?")
            params.append(report_type)
        if report_date:
            clauses.append("report_date = ?")
            params.append(report_date)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"SELECT * FROM reports {where} ORDER BY created_at DESC LIMIT 1"
        with self.connect() as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(query, params).fetchone()
            return dict(row) if row else None


    def recent_reports(self, report_type: str | None = None, limit: int = 5) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if report_type:
            clauses.append("report_type = ?")
            params.append(report_type)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        query = f"SELECT * FROM reports {where} ORDER BY created_at DESC LIMIT ?"
        with self.connect() as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(query, params).fetchall()
            return [dict(row) for row in rows]

    def get_report(self, report_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute("SELECT * FROM reports WHERE report_id = ?", (report_id,)).fetchone()
            return dict(row) if row else None

    def save_events(self, events: list[dict[str, Any]]) -> None:
        if not events:
            return
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO events (
                    event_type, target, title, source_url, source_level,
                    published_date, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        str(event.get("event_type") or "source"),
                        str(event.get("target") or "unknown"),
                        str(event.get("title") or "untitled"),
                        event.get("source_url"),
                        event.get("source_level"),
                        event.get("published_date"),
                        json.dumps(event.get("payload") or {}, ensure_ascii=False, default=str),
                        now,
                    )
                    for event in events
                ],
            )

    def save_snapshots(self, snapshots: list[dict[str, Any]]) -> None:
        if not snapshots:
            return
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO source_snapshots (
                    target, command, source_url, title, source_level,
                    published_date, fetched_at, report_date, content_type, content_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        str(item.get("target") or "unknown"),
                        str(item.get("command") or "unknown"),
                        str(item.get("source_url") or item.get("url") or ""),
                        str(item.get("title") or "untitled"),
                        item.get("source_level"),
                        item.get("published_date"),
                        item.get("fetched_at") or now,
                        item.get("report_date"),
                        str(item.get("content_type") or "summary"),
                        json.dumps(item.get("content") or {}, ensure_ascii=False, default=str),
                    )
                    for item in snapshots
                    if item.get("source_url") or item.get("url")
                ],
            )

    def query_snapshots_before(self, target: str, report_date: str, command: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        clauses = ["target = ?", "(published_date IS NULL OR published_date <= ?)", "fetched_at <= ?"]
        params: list[Any] = [target, report_date, f"{report_date}T23:59:59"]
        if command:
            clauses.append("command = ?")
            params.append(command)
        params.append(limit)
        query = f"SELECT * FROM source_snapshots WHERE {' AND '.join(clauses)} ORDER BY COALESCE(published_date, fetched_at) DESC LIMIT ?"
        with self.connect() as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(query, params).fetchall()
            results = []
            for row in rows:
                item = dict(row)
                try:
                    item["content"] = json.loads(item.get("content_json") or "{}")
                except json.JSONDecodeError:
                    item["content"] = {}
                results.append(item)
            return results

    def query_events_before(self, target: str, report_date: str | None = None, event_type: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        clauses = ["target = ?"]
        params: list[Any] = [target]
        if report_date:
            clauses.append("(published_date IS NULL OR published_date <= ?)")
            params.append(report_date)
        if event_type:
            clauses.append("event_type = ?")
            params.append(event_type)
        params.append(limit)
        query = f"SELECT * FROM events WHERE {' AND '.join(clauses)} ORDER BY COALESCE(published_date, created_at) DESC LIMIT ?"
        with self.connect() as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(query, params).fetchall()
            return [dict(row) for row in rows]

def _is_recoverable_sqlite_error(exc: sqlite3.OperationalError) -> bool:
    text = str(exc).lower()
    return "disk i/o" in text or "unable to open database file" in text

def _request_to_json(request: CommandRequest) -> dict[str, Any]:
    payload = asdict(request)
    for key in ("report_date", "created_at"):
        value = payload.get(key)
        if value is not None:
            payload[key] = value.isoformat()
    return payload







