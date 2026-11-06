"""Verified, expiring grants with atomic 70/20/10 reservations.

No billing API balance is invented here. A grant is a conservative, manually
reconciled allowance, not proof that other applications have stopped spending.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import json
import sqlite3
from uuid import uuid4

from .deepseek_service import DeepSeekUnavailable, TASK, RUN_ID

ACCOUNT_PRIORITY = ('secondary', 'primary')
_NOTICE_COMMANDS = {'research': '個股投研', 'value_scan': '價值重估', 'macro': '總體分析',
                    'theme': '題材分析', 'theme_flow': '題材資金流', 'theme_radar': '題材雷達',
                    'sector_strength': '族群強弱', 'radar': '選股雷達', 'news': '新聞整理',
                    'topic_maintain': '題材庫維護'}
_NOTICE_STAGES = {'web_search': '搜尋階段', 'model': 'AI分析／整理階段', 'preflight': '額度預檢階段'}


def amount(value):
    result = Decimal(str(value))
    if not result.is_finite() or result < 0:
        raise ValueError('額度必須是非負有限數值。')
    return result


def instant(value):
    result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('贈額時間必須包含時區。')
    return result.astimezone(timezone.utc)


class OpenAIUnavailable(DeepSeekUnavailable):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.diagnostics['provider'] = 'openai'


class OpenAICreditsUnavailable(OpenAIUnavailable):
    """Only this definitive preflight/quota refusal permits provider fallback."""


class OpenAICreditLedger:
    def __init__(self, root, grants=(), *, job_limit=5, now=None, development=False, artifact_root=None,
                 round_id='', round_limit=None, case_id=''):
        self.root = Path(root)
        self.artifact_root = Path(artifact_root or root)
        self.purpose = 'development' if development else 'production'
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.job_limit = amount(job_limit)
        self.round_id = str(round_id)
        self.case_id = str(case_id)
        self.round_limit = None if round_limit is None else amount(round_limit)
        if bool(self.round_id) != (self.round_limit is not None) or (self.round_id and
                (not development or not self.round_limit)):
            raise ValueError('本輪護欄必須指定開發用途、固定編號及正數上限。')
        if self.case_id and not self.round_id:
            raise ValueError('案例護欄必須屬於已登記的付費測試輪次。')
        self.grants = []
        identities = set()
        organizations = set()
        for raw in grants:
            row = {k: raw[k] for k in ('grant_id', 'account_id', 'organization_id', 'starts_at', 'expires_at', 'verified_at', 'evidence')}
            row['allowance_usd'] = str(amount(raw['allowance_usd']))
            if not all(isinstance(row[k], str) and row[k].strip() for k in row):
                raise ValueError('贈額需有帳戶、組織、期間、核對時間與證據。')
            start, end, verified = map(instant, (row['starts_at'], row['expires_at'], row['verified_at']))
            if not start <= verified < end:
                raise ValueError('贈額核對時間必須位於有效期間。')
            if row['grant_id'] in identities:
                raise ValueError('贈額編號不得重複。')
            identities.add(row['grant_id'])
            # Two API keys for one organization must not double its balance.
            org_period = (row['organization_id'], row['starts_at'], row['expires_at'])
            if org_period in organizations:
                raise ValueError('同組織同期間不得重複開帳。')
            organizations.add(org_period)
            self.grants.append(row)
        for i, a in enumerate(self.grants):
            for b in self.grants[i + 1:]:
                if a['organization_id'] == b['organization_id'] and max(instant(a['starts_at']), instant(b['starts_at'])) < min(instant(a['expires_at']), instant(b['expires_at'])):
                    raise ValueError('同組織贈額期間重疊，請合併核對後開帳。')

    @contextmanager
    def db(self):
        self.root.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.root / 'openai-credits.sqlite3', timeout=20)
        try:
            conn.executescript('''
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS grants (id TEXT PRIMARY KEY, record TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, grant_id TEXT NOT NULL,
                    background INTEGER NOT NULL, command TEXT, run_id TEXT, operation TEXT,
                    status TEXT NOT NULL, estimate TEXT NOT NULL, actual TEXT, detail TEXT,
                    purpose TEXT NOT NULL DEFAULT 'production');
                CREATE TABLE IF NOT EXISTS account_blocks (account_id TEXT PRIMARY KEY, grant_id TEXT, reason TEXT);
                CREATE TABLE IF NOT EXISTS route_events (id TEXT PRIMARY KEY, run_id TEXT, command TEXT,
                    background INTEGER, provider TEXT, reason TEXT, detail TEXT);
                CREATE TABLE IF NOT EXISTS notices (id TEXT PRIMARY KEY, background INTEGER, run_id TEXT,
                    reason TEXT, message TEXT, state TEXT);
                CREATE TABLE IF NOT EXISTS notice_attribution (
                    notice_id TEXT PRIMARY KEY REFERENCES notices(id),
                    purpose TEXT NOT NULL CHECK(purpose IN ('production','development','unclassified')),
                    command TEXT NOT NULL, operation TEXT NOT NULL, created_at TEXT,
                    attributed_at TEXT NOT NULL, evidence TEXT NOT NULL, delivery_result_at TEXT);
                CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, expires REAL, value TEXT);
                CREATE TABLE IF NOT EXISTS live_rounds (id TEXT PRIMARY KEY, limit_usd TEXT NOT NULL);
            ''')
            purpose_known = 'purpose' in {row[1] for row in conn.execute('PRAGMA table_info(requests)')}
            if not purpose_known:
                conn.execute("ALTER TABLE requests ADD COLUMN purpose TEXT NOT NULL DEFAULT 'production'")
            if 'round_id' not in {row[1] for row in conn.execute('PRAGMA table_info(requests)')}:
                conn.execute("ALTER TABLE requests ADD COLUMN round_id TEXT NOT NULL DEFAULT ''")
            if 'case_id' not in {row[1] for row in conn.execute('PRAGMA table_info(requests)')}:
                conn.execute("ALTER TABLE requests ADD COLUMN case_id TEXT NOT NULL DEFAULT ''")
            self._attribute_legacy_notices(conn, purpose_known=purpose_known)
            # Serialize schema upgrades across processes before callers start their transaction.
            conn.commit()
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _notice_time(self):
        return self.now().astimezone(timezone.utc).isoformat(timespec='seconds')

    def _attribute_legacy_notices(self, conn, *, purpose_known=True):
        # Keep the original six-column notices and every financial table intact.
        missing = conn.execute('''SELECT n.id,n.run_id FROM notices n
            LEFT JOIN notice_attribution a ON a.notice_id=n.id WHERE a.notice_id IS NULL''').fetchall()
        for notice_id, run_id in missing:
            rows = conn.execute('SELECT id,purpose,command FROM requests WHERE run_id=?', (run_id,)).fetchall() if run_id else []
            purposes = {p for _, p, _ in rows}
            purpose = next(iter(purposes)) if purpose_known and len(purposes) == 1 and purposes <= {'production', 'development'} else 'unclassified'
            commands = {command for _, _, command in rows if command}
            evidence = {'source': 'legacy_run_requests', 'run_id': run_id,
                        'request_ids': [identifier for identifier, _, _ in rows],
                        'purpose_values': sorted(str(value) for value in purposes),
                        'request_purpose_preexisted': purpose_known,
                        'classification': purpose, 'original_time_unknown': True}
            conn.execute('''INSERT INTO notice_attribution
                (notice_id,purpose,command,operation,created_at,attributed_at,evidence)
                VALUES (?,?,?,?,?,?,?)''', (notice_id, purpose, next(iter(commands)) if len(commands) == 1 else '',
                    '', None, self._notice_time(), json.dumps(evidence, ensure_ascii=False)))

    def _queue_notice(self, conn, reason, message, *, operation):
        command, background, _ = TASK.get()
        run_id, now = RUN_ID.get(), self._notice_time()
        query = '''SELECT 1 FROM notices n JOIN notice_attribution a ON a.notice_id=n.id
            WHERE a.purpose=? AND a.command=? AND n.background=? AND n.reason=? AND n.run_id=?'''
        params = [self.purpose, command, int(background), reason, run_id]
        if not run_id:
            # Unscoped callers have no job identity; throttle rather than suppress forever.
            query += ' AND a.created_at>=?'
            params.append((instant(now) - timedelta(minutes=5)).isoformat(timespec='seconds'))
        if conn.execute(query, params).fetchone():
            return
        label = ('排程' if background else '手動') + _NOTICE_COMMANDS.get(command, 'AI工作')
        stage = _NOTICE_STAGES.get(operation, '執行階段未記錄')
        local_time = instant(now).astimezone(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')
        text = f'【{label}｜{stage}】\n通知時間：{local_time}（台灣時間）\n{message}'
        identifier = uuid4().hex
        conn.execute('INSERT INTO notices VALUES (?,?,?,?,?,?)',
                     (identifier, int(background), run_id, reason, text, 'pending'))
        conn.execute('''INSERT INTO notice_attribution
            (notice_id,purpose,command,operation,created_at,attributed_at,evidence) VALUES (?,?,?,?,?,?,?)''',
            (identifier, self.purpose, command, operation, now, now,
             json.dumps({'source': 'runtime_explicit', 'purpose': self.purpose,
                         'run_id': run_id}, ensure_ascii=False)))

    def _sync(self, conn):
        for row in self.grants:
            encoded = json.dumps(row, sort_keys=True, ensure_ascii=False)
            existing = conn.execute('SELECT record FROM grants WHERE id=?', (row['grant_id'],)).fetchone()
            if existing and existing[0] != encoded:
                raise ValueError('已開帳贈額不得改寫或歸零；請保留歷史並建立下一個已核對週期。')
            conn.execute('INSERT OR IGNORE INTO grants VALUES (?,?)', (row['grant_id'], encoded))

    def choose(self, account_ids, estimate=Decimal(0), *, reserve=False, operation='model',
               acknowledged_unknown_ids=()):
        estimate = amount(estimate)
        acknowledged = frozenset(acknowledged_unknown_ids)
        if acknowledged and (self.purpose != 'development' or not self.round_id):
            raise ValueError('未知費用預留續跑僅供明確授權的固定開發輪次；正式流程不得略過。')
        command, background, _ = TASK.get()
        with self.db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            self._sync(conn)
            if self.round_id:
                previous = conn.execute('SELECT limit_usd FROM live_rounds WHERE id=?', (self.round_id,)).fetchone()
                if previous and amount(previous[0]) != self.round_limit:
                    raise ValueError('本輪既有授權上限不得改寫或重新起算。')
                conn.execute('INSERT OR IGNORE INTO live_rounds VALUES (?,?)', (self.round_id, str(self.round_limit)))
                round_rows = conn.execute('SELECT id,status,estimate,actual FROM requests WHERE round_id=?',
                                          (self.round_id,)).fetchall()
                eligible = {rid for rid, status, _, actual in round_rows if status == 'unknown' and actual is None}
                if not acknowledged <= eligible:
                    raise ValueError('授權預留必須對應同輪已停止且實付未知的原始請求。')
                if any(s in ('pending', 'unknown') and rid not in acknowledged for rid, s, _, _ in round_rows):
                    raise OpenAIUnavailable('本輪有未結算或費用未知請求；停止所有後續付費案例。', 'transport_unknown')
                spent = sum((amount(a if a is not None else e) for _, _, e, a in round_rows), Decimal(0))
                if spent + estimate > self.round_limit:
                    raise OpenAIUnavailable('本輪付費測試已達授權總上限，未送出新請求。', 'openai_round_budget_exceeded')
            if RUN_ID.get() and conn.execute("SELECT 1 FROM requests WHERE run_id=? AND status IN ('pending','unknown')", (RUN_ID.get(),)).fetchone():
                raise OpenAIUnavailable('本工作有進行中或費用未知的 OpenAI 請求，停止重送並保留成果。', 'transport_unknown')
            if not account_ids:
                raise OpenAICreditsUnavailable('尚未設定 OpenAI 金鑰；改檢查既有 DeepSeek 備援。', 'openai_not_configured')
            if not self.grants:
                raise OpenAICreditsUnavailable('尚未登記已核對的 OpenAI 贈額與期間，改檢查備援；未宣稱官方餘額用完。', 'openai_grant_unverified')
            rows = conn.execute('SELECT grant_id,background,run_id,estimate,actual,purpose FROM requests').fetchall()
            job = sum((Decimal(a if a is not None else e) for _, _, r, e, a, _ in rows if r and r == RUN_ID.get()), Decimal(0))
            if self.case_id:
                case_rows = conn.execute('SELECT estimate,actual FROM requests WHERE round_id=? AND case_id=?',
                                         (self.round_id, self.case_id)).fetchall()
                job = sum((amount(a if a is not None else e) for e, a in case_rows), Decimal(0))
            if self.job_limit and job + estimate > self.job_limit:
                raise OpenAICreditsUnavailable('OpenAI 本次工作已用及預留費用將超過單次費用護欄，改由備援依原限額檢查。', 'openai_job_budget_exceeded')
            priority = {account: index for index, account in enumerate(ACCOUNT_PRIORITY)}
            for grant in sorted(self.grants, key=lambda g: (priority.get(g['account_id'], len(priority)),
                                                          instant(g['expires_at']), g['grant_id'])):
                if grant['account_id'] not in account_ids or not instant(grant['verified_at']) <= self.now() < instant(grant['expires_at']):
                    continue
                if conn.execute('SELECT 1 FROM account_blocks WHERE account_id=? AND grant_id=?', (grant['account_id'], grant['grant_id'])).fetchone():
                    continue
                costs = [(bool(b), Decimal(a if a is not None else e), purpose) for g, b, _, e, a, purpose in rows if g == grant['grant_id']]
                used = sum((cost for b, cost, purpose in costs if b == bool(background) and purpose == 'production'), Decimal(0))
                all_used = sum((cost for _, cost, _ in costs), Decimal(0))
                allowance = Decimal(grant['allowance_usd'])
                limit = allowance * (Decimal('.7') if background else Decimal('.2'))
                if (self.purpose == 'production' and (used + estimate > limit or (estimate == 0 and used >= limit))) or all_used + estimate > allowance * Decimal('.9'):
                    continue
                if self.purpose == 'production' and background:
                    manual_used = sum((cost for b, cost, purpose in costs
                                       if not b and purpose == 'production'), Decimal(0))
                    manual_remaining = max(Decimal(0), allowance * Decimal('.2') - manual_used)
                    background_ceiling = allowance * Decimal('.9') - manual_remaining
                    if all_used + estimate > background_ceiling or (estimate == 0 and all_used >= background_ceiling):
                        continue
                request_id = uuid4().hex if reserve else None
                if reserve:
                    conn.execute('INSERT INTO requests (id,grant_id,background,command,run_id,operation,status,estimate,actual,detail,purpose,round_id,case_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        (request_id, grant['grant_id'], int(background), command, RUN_ID.get(), operation, 'pending', str(estimate), None, '{}', self.purpose, self.round_id, self.case_id))
                return grant, request_id
        raise OpenAICreditsUnavailable('OpenAI 已核對贈額／對應分額不足或已到期；不購買點數，改檢查 DeepSeek 備援。', 'openai_credits_unavailable')

    def finish(self, request_id, status, actual, detail):
        with self.db() as conn:
            conn.execute('UPDATE requests SET status=?,actual=?,detail=? WHERE id=?',
                (status, None if actual is None else str(amount(actual)), json.dumps(detail, ensure_ascii=False), request_id))

    def block(self, grant, reason):
        with self.db() as conn:
            conn.execute('INSERT OR REPLACE INTO account_blocks VALUES (?,?,?)', (grant['account_id'], grant['grant_id'], reason))

    def route(self, provider, reason='', detail=None):
        command, background, _ = TASK.get()
        with self.db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('INSERT INTO route_events VALUES (?,?,?,?,?,?,?)', (uuid4().hex, RUN_ID.get(), command, int(background), provider, reason, json.dumps(detail or {}, ensure_ascii=False)))
            if provider == 'deepseek':
                reason_text = {'openai_not_configured': '尚未設定 OpenAI 金鑰',
                               'openai_grant_unverified': '尚未登記已核對的 OpenAI 贈額',
                               'openai_job_budget_exceeded': 'OpenAI 已達單次費用護欄'}.get(reason, 'OpenAI 贈額／分額目前不可用')
                self._queue_notice(conn, reason,
                    reason_text + '，後續改由 DeepSeek 依原預算檢查；不是自動加值，也不保證整項工作完成。',
                    operation='preflight')

    def claim_notice(self):
        if self.purpose != 'production':
            return None
        with self.db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('''SELECT n.id,n.message,a.created_at,a.command
                FROM notices n JOIN notice_attribution a ON a.notice_id=n.id
                WHERE n.background=1 AND n.state='pending' AND a.purpose='production'
                ORDER BY n.rowid LIMIT 1''').fetchone()
            if row:
                conn.execute("UPDATE notices SET state='sending' WHERE id=?", (row[0],))
                message = row[1]
                if row[2] is None:
                    label = _NOTICE_COMMANDS.get(row[3], 'AI工作')
                    message = f'【歷史正式通知｜{label}】\n原始時間未記錄；非本次工作的即時狀態。\n{message}'
                return row[0], message
            return None

    def warn(self, reason, message, *, operation=''):
        with self.db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            text = 'OpenAI 工作暫停：' + message + ' 已保留資料；僅針對本次工作，其他排程仍於執行時重新檢查。'
            if reason == 'transport_unknown':
                text += ' 此通知不代表額度用完；費用未知仍保留預留，不跨帳戶或模型重送。'
            self._queue_notice(conn, reason, text, operation=operation)

    def notice_result(self, notice_id, success):
        if self.purpose != 'production':
            return False
        with self.db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            changed = conn.execute('''UPDATE notices SET state=? WHERE id=? AND state='sending'
                AND id IN (SELECT notice_id FROM notice_attribution WHERE purpose='production')''',
                ('sent' if success else 'delivery_unknown', notice_id)).rowcount
            if changed:
                conn.execute('UPDATE notice_attribution SET delivery_result_at=? WHERE notice_id=?',
                             (self._notice_time(), notice_id))
            return bool(changed)

    def summary(self):
        with self.db() as conn:
            self._sync(conn)
            rows = conn.execute('SELECT grant_id,background,estimate,actual,status,purpose FROM requests').fetchall()
        return {'provider': 'openai', 'balance_is_local_allowance': True,
                'account_priority': list(ACCOUNT_PRIORITY), 'grants': [
            {**g, 'background_limit': str(Decimal(g['allowance_usd']) * Decimal('.7')),
             'manual_limit': str(Decimal(g['allowance_usd']) * Decimal('.2')),
             'reserve': str(Decimal(g['allowance_usd']) * Decimal('.1')),
             'used_or_reserved': str(sum((Decimal(a if a is not None else e) for gid, _, e, a, _, _ in rows if gid == g['grant_id']), Decimal(0))),
             'production_used': str(sum((Decimal(a if a is not None else e) for gid, _, e, a, _, p in rows if gid == g['grant_id'] and p == 'production'), Decimal(0))),
             'development_used': str(sum((Decimal(a if a is not None else e) for gid, _, e, a, _, p in rows if gid == g['grant_id'] and p == 'development'), Decimal(0))),
             'unknown_or_pending': sum(s in ('pending', 'unknown') for gid, _, _, _, s, _ in rows if gid == g['grant_id'])}
            for g in self.grants]}
