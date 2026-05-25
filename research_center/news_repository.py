"""News knowledge base repository — SQLite-backed news storage."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .config import ROOT_DIR
from .news_models import NewsItem, NewsPreference, apply_news_signal_tags

NEWS_DB_PATH = ROOT_DIR / "database" / "stock_research.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS news_articles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    source TEXT NOT NULL,
    published_at TEXT,
    category TEXT,
    related_symbols TEXT,
    related_topics TEXT,
    summary TEXT,
    full_text TEXT,
    importance_score INTEGER DEFAULT 0,
    impact_direction TEXT,
    tags TEXT,
    news_signal_score INTEGER DEFAULT 0,
    news_heat_risk_score INTEGER DEFAULT 0,
    news_signal_reason TEXT,
    news_heat_risk_reason TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_news_url ON news_articles(url);
CREATE INDEX IF NOT EXISTS idx_news_published ON news_articles(published_at);
CREATE INDEX IF NOT EXISTS idx_news_category ON news_articles(category);
CREATE INDEX IF NOT EXISTS idx_news_created ON news_articles(created_at);

CREATE TABLE IF NOT EXISTS news_preferences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    category TEXT,
    normalized_category TEXT,
    source TEXT,
    news_type TEXT,
    weight INTEGER DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_news_preferences_url ON news_preferences(url);
CREATE INDEX IF NOT EXISTS idx_news_preferences_category ON news_preferences(normalized_category);
CREATE INDEX IF NOT EXISTS idx_news_preferences_type ON news_preferences(news_type);
"""


class NewsRepository:
    """SQLite-backed news repository with deduplication and time-window queries."""

    def __init__(self, db_path: Path | str | None = None):
        self.db_path = Path(db_path) if db_path is not None else NEWS_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _init_schema(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript(SCHEMA)
            _ensure_news_signal_columns(conn)

    def save(self, item: NewsItem) -> bool:
        """Save a news item; return False if URL already exists (dedup)."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO news_articles
                    (url, title, source, published_at, category, related_symbols,
                     related_topics, summary, full_text, importance_score, impact_direction,
                     tags, news_signal_score, news_heat_risk_score, news_signal_reason, news_heat_risk_reason,
                     created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        apply_news_signal_tags(item).url,
                        item.title,
                        item.source,
                        item.published_at,
                        item.category,
                        json.dumps(item.related_symbols, ensure_ascii=False),
                        json.dumps(item.related_topics, ensure_ascii=False),
                        item.summary,
                        item.full_text,
                        item.importance_score,
                        item.impact_direction,
                        json.dumps(item.tags, ensure_ascii=False),
                        item.news_signal_score,
                        item.news_heat_risk_score,
                        item.news_signal_reason,
                        item.news_heat_risk_reason,
                        item.created_at or datetime.now().isoformat(),
                    ),
                )
                conn.commit()
                return True
        except sqlite3.IntegrityError:
            return False

    def save_many(self, items: list[NewsItem]) -> tuple[int, int]:
        """Batch save; return (saved_count, skipped_count)."""
        saved = 0
        skipped = 0
        with sqlite3.connect(self.db_path) as conn:
            for item in items:
                try:
                    conn.execute(
                        """
                        INSERT INTO news_articles
                        (url, title, source, published_at, category, related_symbols,
                         related_topics, summary, full_text, importance_score, impact_direction,
                         tags, news_signal_score, news_heat_risk_score, news_signal_reason, news_heat_risk_reason,
                         created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            apply_news_signal_tags(item).url,
                            item.title,
                            item.source,
                            item.published_at,
                            item.category,
                            json.dumps(item.related_symbols, ensure_ascii=False),
                            json.dumps(item.related_topics, ensure_ascii=False),
                            item.summary,
                            item.full_text,
                            item.importance_score,
                            item.impact_direction,
                            json.dumps(item.tags, ensure_ascii=False),
                            item.news_signal_score,
                            item.news_heat_risk_score,
                            item.news_signal_reason,
                            item.news_heat_risk_reason,
                            item.created_at or datetime.now().isoformat(),
                        ),
                    )
                    saved += 1
                except sqlite3.IntegrityError:
                    skipped += 1
            conn.commit()
        return saved, skipped

    def delete_by_urls(self, urls: list[str]) -> int:
        """Delete stored rows by URL; return deleted count."""
        clean_urls = [url for url in urls if url]
        if not clean_urls:
            return 0
        placeholders = ",".join("?" for _ in clean_urls)
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                f"DELETE FROM news_articles WHERE url IN ({placeholders})",
                tuple(clean_urls),
            )
            conn.commit()
            return int(cur.rowcount or 0)

    def query_recent(self, hours: int = 24) -> list[NewsItem]:
        """Query news from the last N hours."""
        cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
        return self._query("SELECT * FROM news_articles WHERE created_at >= ? ORDER BY created_at DESC", (cutoff,))

    def query_by_symbol(self, symbol: str, hours: int = 168) -> list[NewsItem]:
        """Query news related to a stock symbol (last N hours, default 7 days)."""
        cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
        return self._query(
            "SELECT * FROM news_articles WHERE created_at >= ? AND related_symbols LIKE ? ORDER BY created_at DESC",
            (cutoff, f'%"{symbol}"%'),
        )

    def query_by_topic(self, topic: str, hours: int = 168) -> list[NewsItem]:
        """Query news related to a topic keyword (last N hours, default 7 days)."""
        cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
        return self._query(
            "SELECT * FROM news_articles WHERE created_at >= ? AND related_topics LIKE ? ORDER BY created_at DESC",
            (cutoff, f'%"{topic}"%'),
        )

    def query_by_category(self, category: str, hours: int = 168) -> list[NewsItem]:
        """Query news by category."""
        cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
        return self._query(
            "SELECT * FROM news_articles WHERE created_at >= ? AND category = ? ORDER BY created_at DESC",
            (cutoff, category),
        )

    def query_all_recent(self, hours: int = 168) -> list[NewsItem]:
        """Query all recent news."""
        cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
        return self._query("SELECT * FROM news_articles WHERE created_at >= ? ORDER BY created_at DESC", (cutoff,))

    def get_by_id(self, news_id: str) -> NewsItem | None:
        """Return a single news item by numeric id, #N{id}, or N{id}."""
        clean_id = str(news_id or "").strip().lstrip("#")
        if clean_id.upper().startswith("N"):
            clean_id = clean_id[1:]
        if not clean_id.isdigit():
            return None
        rows = self._query("SELECT * FROM news_articles WHERE id = ? LIMIT 1", (int(clean_id),))
        return rows[0] if rows else None

    def get_by_url(self, url: str) -> NewsItem | None:
        """Return a single news item by URL."""
        clean_url = str(url or "").strip()
        if not clean_url:
            return None
        rows = self._query("SELECT * FROM news_articles WHERE url = ? LIMIT 1", (clean_url,))
        return rows[0] if rows else None

    def count_recent(self, hours: int = 24) -> int:
        """Count news items from last N hours."""
        cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM news_articles WHERE created_at >= ?", (cutoff,)
            ).fetchone()
            return row[0] if row else 0

    def save_preference(self, preference: NewsPreference) -> bool:
        """Append a lightweight user news preference signal."""
        if not preference.url:
            return False
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO news_preferences
                (url, title, category, normalized_category, source, news_type, weight, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    preference.url,
                    preference.title,
                    preference.category,
                    preference.normalized_category,
                    preference.source,
                    preference.news_type,
                    int(preference.weight or 1),
                    preference.created_at or datetime.now().isoformat(),
                ),
            )
            conn.commit()
        return True

    def list_preferences(self, limit: int = 100) -> list[NewsPreference]:
        """Return most recent user news preference signals."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM news_preferences ORDER BY created_at DESC, id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [_row_to_preference(dict(r)) for r in rows]

    def count_preferences(self) -> int:
        """Count stored user news preference signals."""
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT COUNT(*) FROM news_preferences").fetchone()
            return row[0] if row else 0

    def get_preference_stats(self, limit: int = 300) -> dict[str, dict[str, int]]:
        """Return aggregate preference weights for news ranking."""
        prefs = self.list_preferences(limit=limit)
        stats: dict[str, dict[str, int]] = {
            "news_type": {},
            "category": {},
            "source": {},
        }
        for pref in prefs:
            weight = int(pref.weight or 1)
            if pref.news_type:
                stats["news_type"][pref.news_type] = stats["news_type"].get(pref.news_type, 0) + weight
            if pref.normalized_category:
                stats["category"][pref.normalized_category] = stats["category"].get(pref.normalized_category, 0) + weight
            if pref.source:
                key = pref.source.strip().lower()
                stats["source"][key] = stats["source"].get(key, 0) + weight
        return stats

    def _query(self, sql: str, params: tuple[Any, ...]) -> list[NewsItem]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_item(dict(r)) for r in rows]


def _row_to_item(row: dict[str, Any]) -> NewsItem:
    return apply_news_signal_tags(NewsItem(
        id=str(row.get("id", "")),
        title=str(row.get("title", "")),
        url=str(row.get("url", "")),
        source=str(row.get("source", "")),
        published_at=str(row.get("published_at", "")),
        category=str(row.get("category", "")),
        related_symbols=json.loads(row.get("related_symbols", "[]") or "[]"),
        related_topics=json.loads(row.get("related_topics", "[]") or "[]"),
        summary=str(row.get("summary", "")),
        full_text=str(row.get("full_text", "")),
        importance_score=int(row.get("importance_score", 0) or 0),
        impact_direction=str(row.get("impact_direction", "")),
        tags=_json_list(row.get("tags")),
        news_signal_score=int(row.get("news_signal_score", 0) or 0),
        news_heat_risk_score=int(row.get("news_heat_risk_score", 0) or 0),
        news_signal_reason=str(row.get("news_signal_reason", "")),
        news_heat_risk_reason=str(row.get("news_heat_risk_reason", "")),
        created_at=str(row.get("created_at", "")),
    ))


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value or "[]")
    except Exception:
        return []
    return parsed if isinstance(parsed, list) else []


def _ensure_news_signal_columns(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(news_articles)").fetchall()}
    columns = {
        "tags": "TEXT",
        "news_signal_score": "INTEGER DEFAULT 0",
        "news_heat_risk_score": "INTEGER DEFAULT 0",
        "news_signal_reason": "TEXT",
        "news_heat_risk_reason": "TEXT",
    }
    for name, ddl in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE news_articles ADD COLUMN {name} {ddl}")


def _row_to_preference(row: dict[str, Any]) -> NewsPreference:
    return NewsPreference(
        id=str(row.get("id", "")),
        url=str(row.get("url", "")),
        title=str(row.get("title", "")),
        category=str(row.get("category", "")),
        normalized_category=str(row.get("normalized_category", "")),
        source=str(row.get("source", "")),
        news_type=str(row.get("news_type", "")),
        weight=int(row.get("weight", 1) or 1),
        created_at=str(row.get("created_at", "")),
    )
