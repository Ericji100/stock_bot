"""News knowledge base repository — SQLite-backed news storage."""
from __future__ import annotations

import json
import hashlib
import sqlite3
from dataclasses import asdict
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .config import ROOT_DIR
from .news_models import NewsItem, NewsPreference, apply_news_signal_tags
from .source_date_normalizer import normalize_published_date

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
    counter_evidence TEXT,
    missing_data TEXT,
    news_origin TEXT DEFAULT 'refresh',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_news_url ON news_articles(url);
CREATE INDEX IF NOT EXISTS idx_news_published ON news_articles(published_at);
CREATE INDEX IF NOT EXISTS idx_news_category ON news_articles(category);
CREATE INDEX IF NOT EXISTS idx_news_created ON news_articles(created_at);

CREATE TABLE IF NOT EXISTS news_discovery_sources (
    run_id TEXT NOT NULL,
    record_sha256 TEXT NOT NULL,
    url TEXT NOT NULL,
    record TEXT NOT NULL,
    saved_at TEXT NOT NULL,
    PRIMARY KEY (run_id, record_sha256)
);

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

CREATE TABLE IF NOT EXISTS news_classification_inputs (
    url TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    record TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    saved_at TEXT NOT NULL,
    classified_at TEXT,
    PRIMARY KEY (url, input_sha256)
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
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.executescript(SCHEMA)
            _ensure_news_signal_columns(conn)
            if 'classified_at' not in {row[1] for row in conn.execute('PRAGMA table_info(news_classification_inputs)')}:
                conn.execute('ALTER TABLE news_classification_inputs ADD COLUMN classified_at TEXT')

    def save_discovery_checkpoint(self, sources) -> None:
        """Retain every distinct source version, even before date/classification checks."""
        from dataclasses import asdict
        import hashlib
        from .deepseek_service import RUN_ID
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            for source in sources:
                row = asdict(source) if not isinstance(source, dict) else source
                record = json.dumps(row, ensure_ascii=False, sort_keys=True)
                conn.execute('INSERT OR IGNORE INTO news_discovery_sources VALUES (?,?,?,?,?)',
                    (RUN_ID.get(), hashlib.sha256(record.encode()).hexdigest(), row.get('url') or '',
                     record, datetime.now().astimezone().isoformat()))

    def article_count(self) -> int:
        with closing(sqlite3.connect(self.db_path)) as conn:
            return conn.execute('SELECT COUNT(*) FROM news_articles').fetchone()[0]

    def recent_discovery_sources(self, as_of, hours: int = 48):
        """Merge all recent versions for reuse; untouched snapshots remain in SQLite."""
        from datetime import time
        from dataclasses import replace
        from zoneinfo import ZoneInfo
        from .models import SourceItem
        from .orchestrator import _merge_sources
        end = datetime.combine(as_of, time.max, ZoneInfo('Asia/Taipei'))
        start = end - timedelta(hours=hours)
        with closing(sqlite3.connect(self.db_path)) as conn:
            rows = conn.execute('''SELECT record FROM news_discovery_sources
                WHERE julianday(saved_at) BETWEEN julianday(?) AND julianday(?)
                ORDER BY julianday(saved_at) DESC, rowid DESC''', (start.isoformat(), end.isoformat())).fetchall()
        sources, seen = [], set()
        for (record,) in rows:
            row = json.loads(record)
            if record in seen:
                continue
            seen.add(record)
            sources.append(SourceItem(**{key: value for key, value in row.items()
                                         if key in SourceItem.__dataclass_fields__}))
        merged = _merge_sources([], sources)
        result = []
        for item in merged:
            versions = [source for source in sources if source.url == item.url]
            body = max((source.content or '' for source in versions), key=len)
            provenance = [f"saved_publication:{source.published_date or 'unknown'}:{source.date_status or 'unknown'}"
                          for source in versions]
            result.append(replace(item, content=body or item.content,
                                  found_by=list(dict.fromkeys([*item.found_by, *provenance]))))
        return result

    def prepare_classification_inputs(self, items, *, classifier_context='', source_events=None):
        """Retain originals before AI mutation and skip only completed identical inputs."""
        needed, fingerprints = [], {}
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            for item in items:
                digest = _classification_input_hash(item, classifier_context, (source_events or {}).get(item.url))
                fingerprints[item.url] = digest
                previous = conn.execute('SELECT status FROM news_classification_inputs WHERE url=? AND input_sha256=?',
                                        (item.url, digest)).fetchone()
                status = previous[0] if previous else 'pending'
                if previous is None:
                    conn.execute('INSERT INTO news_classification_inputs (url,input_sha256,record,status,saved_at) VALUES (?,?,?,?,?)',
                        (item.url, digest, json.dumps(asdict(item), ensure_ascii=False), status,
                         datetime.now().astimezone().isoformat()))
                if status == 'pending':
                    needed.append(item)
        return needed, fingerprints

    def classification_reuse_summary(self, fingerprints):
        reused = []
        with closing(sqlite3.connect(self.db_path)) as conn:
            for url, digest in fingerprints.items():
                row = conn.execute('SELECT status,saved_at,classified_at FROM news_classification_inputs WHERE url=? AND input_sha256=?',
                                   (url, digest)).fetchone()
                if row and row[0] != 'pending':
                    reused.append({'url': url, 'original_input_saved_at': row[1], 'classified_at': row[2], 'status': row[0]})
        return {'items': reused, 'note': 'Unchanged input reused from its original classification; not reanalysed for the current day.'}

    def save_classification_checkpoint(self, items, fingerprints):
        self.save_many(items)
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            for item in items:
                if 'ai_classified' in item.tags and 'local_rule_fallback' not in item.tags:
                    conn.execute('UPDATE news_classification_inputs SET status=?,classified_at=COALESCE(classified_at,?) WHERE url=? AND input_sha256=?',
                        ('ai_excluded' if 'ai_excluded' in item.tags else 'ai_classified',
                         datetime.now().astimezone().isoformat(), item.url, fingerprints.get(item.url, '')))

    def save(self, item: NewsItem) -> bool:
        """Save a news item; return False if URL already exists (dedup)."""
        try:
            with closing(sqlite3.connect(self.db_path)) as conn, conn:
                conn.execute(
                    """
                    INSERT INTO news_articles
                    (url, title, source, published_at, category, related_symbols,
                     related_topics, summary, full_text, importance_score, impact_direction,
                     tags, news_signal_score, news_heat_risk_score, news_signal_reason, news_heat_risk_reason,
                     news_origin, created_at, counter_evidence, missing_data)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        item.news_origin or "refresh",
                        item.created_at or datetime.now().isoformat(),
                        json.dumps(item.counter_evidence, ensure_ascii=False),
                        json.dumps(item.missing_data, ensure_ascii=False),
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
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            for item in items:
                try:
                    conn.execute(
                        """
                        INSERT INTO news_articles
                        (url, title, source, published_at, category, related_symbols,
                         related_topics, summary, full_text, importance_score, impact_direction,
                         tags, news_signal_score, news_heat_risk_score, news_signal_reason, news_heat_risk_reason,
                         news_origin, created_at, counter_evidence, missing_data)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                            item.news_origin or "refresh",
                            item.created_at or datetime.now().isoformat(),
                            json.dumps(item.counter_evidence, ensure_ascii=False),
                            json.dumps(item.missing_data, ensure_ascii=False),
                        ),
                    )
                    saved += 1
                except sqlite3.IntegrityError:
                    # Source date corrections must survive deduplication, even when
                    # the article is waiting for AI classification. Keep precise time.
                    previous = conn.execute('SELECT published_at,tags,full_text FROM news_articles WHERE url=?', (item.url,)).fetchone()
                    old_date = str(previous[0] or '') if previous else ''
                    old_tags = json.loads(previous[1] or '[]') if previous else []
                    old_body = str(previous[2] or '') if previous else ''
                    incoming = str(item.published_at or '')
                    loses_precision = len(incoming) == 10 and len(old_date) > 10 and normalize_published_date(old_date) == incoming
                    explicit = 'publication_explicit' in item.tags and bool(normalize_published_date(incoming))
                    weakens_date = 'publication_explicit' in old_tags and not explicit
                    if incoming and normalize_published_date(incoming) and incoming != old_date and not loses_precision and not weakens_date:
                        conn.execute('UPDATE news_articles SET published_at=? WHERE url=?', (incoming, item.url))
                    if ('local_rule_fallback' in item.tags and 'local_rule_fallback' in old_tags
                            and 'ai_classified' not in old_tags):
                        richer = len(item.full_text or '') > len(old_body)
                        date_upgrade = explicit and 'publication_explicit' not in old_tags
                        if richer or date_upgrade:
                            date_tags = {'publication_explicit', 'publication_inferred', 'publication_unknown'}
                            incoming_dates = [tag for tag in item.tags if tag in date_tags]
                            dates = (['publication_explicit'] if explicit or 'publication_explicit' in old_tags
                                     else incoming_dates or [tag for tag in old_tags if tag in date_tags])
                            tags = list(dict.fromkeys([tag for tag in [*old_tags, *item.tags] if tag not in date_tags] + dates))
                            conn.execute('''UPDATE news_articles SET tags=?,full_text=?,
                                title=CASE WHEN ? THEN ? ELSE title END,
                                summary=CASE WHEN ? THEN ? ELSE summary END
                                WHERE url=?''', (json.dumps(tags, ensure_ascii=False),
                                    item.full_text if richer else old_body,
                                    int(richer and bool(item.title)), item.title,
                                    int(richer and bool(item.summary)), item.summary, item.url))
                    if "local_rule_fallback" not in item.tags:
                        conn.execute(
                            """UPDATE news_articles SET category=?, related_symbols=?, related_topics=?,
                               summary=?, importance_score=?, impact_direction=?, tags=?, news_signal_score=?,
                               news_heat_risk_score=?, news_signal_reason=?, news_heat_risk_reason=?
                               , title=?, full_text=CASE WHEN ?<>'' THEN ? ELSE full_text END,
                               counter_evidence=?, missing_data=?
                               WHERE url=? AND (tags LIKE '%local_rule_fallback%' OR (?<>'' AND full_text<>?)
                                   OR (counter_evidence IS NULL AND missing_data IS NULL) OR ?=1)""",
                            (item.category, json.dumps(item.related_symbols, ensure_ascii=False),
                             json.dumps(item.related_topics, ensure_ascii=False), item.summary,
                             item.importance_score, item.impact_direction, json.dumps(item.tags, ensure_ascii=False),
                             item.news_signal_score, item.news_heat_risk_score, item.news_signal_reason,
                             item.news_heat_risk_reason, item.title, item.full_text or '', item.full_text or '',
                             json.dumps(item.counter_evidence, ensure_ascii=False),
                             json.dumps(item.missing_data, ensure_ascii=False),
                             item.url, item.full_text or '', item.full_text or '', int('ai_classified' in item.tags)),
                        )
                    skipped += 1
            conn.commit()
        return saved, skipped

    def _pending_classification_items(self):
        items = self._query("""SELECT * FROM news_articles WHERE tags LIKE '%local_rule_fallback%'
            AND tags NOT LIKE '%ai_classified%' ORDER BY created_at, id""", ())
        by_url = {item.url: item for item in items}
        with closing(sqlite3.connect(self.db_path)) as conn:
            rows = conn.execute('''SELECT url,record,status FROM news_classification_inputs
                WHERE rowid IN (SELECT MAX(rowid) FROM news_classification_inputs GROUP BY url)
                ORDER BY saved_at, rowid''').fetchall()
        for url, record, status in rows:
            if status == 'pending':
                by_url[url] = NewsItem(**json.loads(record))
            else:
                by_url.pop(url, None)
        return list(by_url.values())

    def query_pending_classification(self, limit: int = 6, *, as_of=None) -> list[NewsItem]:
        day = as_of or datetime.now().date()
        cutoff = (day - timedelta(days=7)).isoformat()
        # Selection uses publication evidence, never rediscovery/created_at freshness.
        items = [item for item in self._pending_classification_items()
                 if cutoff <= (normalize_published_date(item.published_at) or '') <= day.isoformat()]
        return items[:limit] if limit > 0 else items

    def pending_classification_summary(self, *, as_of=None) -> dict[str, int]:
        total = len(self._pending_classification_items())
        eligible = len(self.query_pending_classification(limit=0, as_of=as_of))
        return {'pending_total': total, 'eligible_recent': eligible, 'historical_or_date_unknown': total - eligible}

    def delete_by_urls(self, urls: list[str]) -> int:
        """Delete stored rows by URL; return deleted count."""
        clean_urls = [url for url in urls if url]
        if not clean_urls:
            return 0
        placeholders = ",".join("?" for _ in clean_urls)
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            cur = conn.execute(
                f"DELETE FROM news_articles WHERE url IN ({placeholders})",
                tuple(clean_urls),
            )
            conn.commit()
            return int(cur.rowcount or 0)

    def query_recent(self, hours: int = 24, news_origin: str | None = None) -> list[NewsItem]:
        """Query news from the last N hours."""
        cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
        if news_origin:
            return self._query(
                "SELECT * FROM news_articles WHERE created_at >= ? AND news_origin = ? ORDER BY created_at DESC",
                (cutoff, news_origin),
            )
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

    def count_recent(self, hours: int = 24, news_origin: str | None = None) -> int:
        """Count news items from last N hours."""
        cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            if news_origin:
                row = conn.execute(
                    "SELECT COUNT(*) FROM news_articles WHERE created_at >= ? AND news_origin = ?",
                    (cutoff, news_origin),
                ).fetchone()
                return row[0] if row else 0
            row = conn.execute(
                "SELECT COUNT(*) FROM news_articles WHERE created_at >= ?", (cutoff,)
            ).fetchone()
            return row[0] if row else 0

    def save_preference(self, preference: NewsPreference) -> bool:
        """Append a lightweight user news preference signal."""
        if not preference.url:
            return False
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
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
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM news_preferences ORDER BY created_at DESC, id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [_row_to_preference(dict(r)) for r in rows]

    def count_preferences(self) -> int:
        """Count stored user news preference signals."""
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
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
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_item(dict(r)) for r in rows]


def _classification_input_hash(item: NewsItem, classifier_context='', source_events=None) -> str:
    # Ignore AI summaries when an original body exists, and all observation times.
    record = [classifier_context, source_events, item.url, item.title, item.source, item.published_at, item.full_text or item.summary,
              sorted(tag for tag in item.tags if tag.startswith('publication_') or tag == 'official_body_retrieved')]
    return hashlib.sha256(json.dumps(record, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


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
        news_origin=str(row.get("news_origin", "unknown") or "unknown"),
        created_at=str(row.get("created_at", "")),
        counter_evidence=_json_list(row.get("counter_evidence")),
        missing_data=_json_list(row.get("missing_data")),
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
        "news_origin": "TEXT DEFAULT 'unknown'",
        "counter_evidence": "TEXT",
        "missing_data": "TEXT",
    }
    for name, ddl in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE news_articles ADD COLUMN {name} {ddl}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_news_origin ON news_articles(news_origin)")


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
