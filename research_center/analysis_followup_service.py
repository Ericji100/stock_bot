"""Durable deferred evidence and explicit predictions; no model calls or scheduler."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Any
from zoneinfo import ZoneInfo

from .config import ROOT_DIR


def _now() -> datetime:
    return datetime.now(ZoneInfo('Asia/Taipei'))


class AnalysisFollowupStore:
    def __init__(self, path: Path | None = None):
        test_root = os.environ.get('CODEX_TASK_TEST_ROOT')
        self.path = Path(path) if path else (Path(test_root) if test_root else ROOT_DIR / '.cache') / 'analysis_followup.sqlite3'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn, conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS backlog (
                    scope TEXT, identity TEXT, payload TEXT, priority INTEGER,
                    created_at TEXT, updated_at TEXT, next_attempt TEXT, attempts INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'pending', reason TEXT, PRIMARY KEY(scope,identity));
                CREATE TABLE IF NOT EXISTS predictions (
                    id TEXT PRIMARY KEY, report_id TEXT, report_date TEXT, payload TEXT, created_at TEXT);
                CREATE TABLE IF NOT EXISTS observations (
                    id INTEGER PRIMARY KEY, prediction_id TEXT, observed_at TEXT, payload TEXT);
            ''')

    def _connect(self):
        return sqlite3.connect(self.path, timeout=15)

    @staticmethod
    def identity(row: dict[str, Any]) -> str:
        key = row.get('theme_id') or row.get('id') or row.get('url')
        return str(key) if key else hashlib.sha256(json.dumps(row, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()

    def defer(self, scope: str, rows: list[dict[str, Any]], *, reason: str = '本輪容量不足') -> None:
        now = _now().isoformat()
        with closing(self._connect()) as conn, conn:
            for row in rows:
                identity = self.identity(row)
                priority = 30 * bool(row.get('counter_evidence')) + 20 * bool(row.get('missing_data')) + 10 * bool(row.get('official_evidence'))
                payload = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
                conn.execute('''INSERT INTO backlog(scope,identity,payload,priority,created_at,updated_at,next_attempt,reason)
                    VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(scope,identity) DO UPDATE SET
                    updated_at=excluded.updated_at, reason=excluded.reason,
                    priority=MAX(backlog.priority,excluded.priority),
                    status=CASE WHEN backlog.payload!=excluded.payload THEN 'pending' ELSE backlog.status END,
                    next_attempt=CASE WHEN backlog.payload!=excluded.payload THEN excluded.next_attempt ELSE backlog.next_attempt END,
                    payload=excluded.payload''', (scope, identity, payload, priority, now, now, now, reason))

    def pending(self, scope: str, *, limit: int = 8) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT payload FROM backlog WHERE scope=? AND status='pending' AND next_attempt<=? ORDER BY attempts,priority DESC,created_at,identity LIMIT ?", (scope, _now().isoformat(), limit)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def resolve(self, scope: str, rows: list[dict[str, Any]], *, success: bool) -> None:
        with closing(self._connect()) as conn, conn:
            for row in rows:
                conn.execute('''UPDATE backlog SET status=?, attempts=attempts+1, next_attempt=?, reason=?
                    WHERE scope=? AND identity=?''', ('completed' if success else 'pending',
                    (_now() + timedelta(hours=6)).isoformat(), '已完成證據檢核' if success else '未通過檢核，等待補證或下一輪', scope, self.identity(row)))

    def record_predictions(self, report_id: str, report_date: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
        saved, rejected = [], []
        with closing(self._connect()) as conn, conn:
            for row in rows:
                required = ('target', 'hypothesis', 'horizon_end', 'trigger', 'invalidation', 'source_ids')
                try:
                    horizon = datetime.fromisoformat(str(row.get('horizon_end'))[:10]).date()
                    valid = all(row.get(key) for key in required) and horizon.isoformat() > report_date[:10]
                except (TypeError, ValueError):
                    valid = False
                if not valid:
                    rejected.append({'record': row, 'reason': '缺少明確期間、催化、失效條件或來源'})
                    continue
                payload = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
                identity = hashlib.sha256((report_id + payload).encode()).hexdigest()[:24]
                conn.execute('INSERT OR IGNORE INTO predictions VALUES(?,?,?,?,?)', (identity, report_id, report_date, payload, _now().isoformat()))
                saved.append(identity)
        return {'recorded_ids': saved, 'rejected': rejected, 'evaluation': '尚未到期或未有核對觀測；不宣稱預測準度'}

    def observe(self, prediction_id: str, observation: dict[str, Any]) -> None:
        if observation.get('status') not in {'supported', 'contradicted', 'not_assessable'}:
            raise ValueError('觀測須明確標示支持、不支持或無法評估')
        if observation['status'] != 'not_assessable' and not observation.get('source_ids'):
            raise ValueError('缺少觀測來源，不能判定預測成敗')
        if not observation.get('data_date'):
            raise ValueError('觀測須保留實際數據日期')
        try:
            data_date = datetime.fromisoformat(str(observation['data_date'])[:10]).date()
        except ValueError as exc:
            raise ValueError('觀測日期格式無效') from exc
        if observation['status'] != 'not_assessable' and data_date > _now().date():
            raise ValueError('未來的觀測不能判定預測成敗')
        with closing(self._connect()) as conn, conn:
            if not conn.execute('SELECT 1 FROM predictions WHERE id=?', (prediction_id,)).fetchone():
                raise ValueError('預測不存在')
            conn.execute('INSERT INTO observations(prediction_id,observed_at,payload) VALUES(?,?,?)',
                         (prediction_id, _now().isoformat(), json.dumps(observation, ensure_ascii=False, default=str)))


def explicit_prediction_records(markdown: str) -> list[dict[str, Any]]:
    """Accept only an explicitly named JSON section, never infer forecasts from prose."""
    import re
    records = []
    for block in re.findall(r'```json\s*([\s\S]*?)```', markdown):
        try:
            value = json.loads(block)
        except ValueError:
            continue
        if isinstance(value, dict) and isinstance(value.get('prediction_records'), list):
            records.extend(row for row in value['prediction_records'] if isinstance(row, dict))
    match = re.search(r'^#{1,6}\s*預測追蹤[^\n]*\n([\s\S]*?)(?=^#{1,6}\s|\Z)', markdown, re.M)
    if match:
        headers = []
        required = ('標的', '假設', '觀察截止日', '驗證條件', '失效條件', '來源')
        for line in match.group(1).splitlines():
            if not line.lstrip().startswith('|'):
                continue
            cells = [cell.strip().strip('*') for cell in line.strip().strip('|').split('|')]
            aliases = {'標的或主題': '標的', '假設或預測': '假設', '預測假設': '假設',
                       '截止日': '觀察截止日', '觀察期限': '觀察截止日', '來源編號': '來源',
                       '來源／證據': '來源', '成立條件': '驗證條件'}
            cells = [aliases.get(cell, cell) for cell in cells]
            if all(key in cells for key in required):
                headers = cells
            elif headers and len(cells) == len(headers) and not all(re.fullmatch(r'[-: ]+', cell) for cell in cells):
                row = dict(zip(headers, cells))
                records.append(dict(zip(('target', 'hypothesis', 'horizon_end', 'trigger', 'invalidation', 'source_ids'),
                    (row.get('標的'), row.get('假設'), row.get('觀察截止日'), row.get('驗證條件'), row.get('失效條件'), re.findall(r'S\d+', row.get('來源', ''))))))
    return records
