"""Persist schedule outcomes and one-attempt, restart-safe abnormal summaries."""
from datetime import date, datetime, timedelta
from pathlib import Path
from contextlib import contextmanager
import json
import re
import sqlite3

from .news_categories import NEWS_CATEGORY_LABELS, news_category_label


class ScheduledOutcomeNotices:
    def __init__(self, root: Path):
        self.path = Path(root) / 'outcomes.sqlite3'

    def recorded_run_ids(self, command):
        with self._db() as conn:
            return {row[0] for row in conn.execute('SELECT run_id FROM outcomes WHERE command=?', (command,))}

    @contextmanager
    def _db(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=10)
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS outcomes (
                command TEXT, day TEXT, run_id TEXT, recorded_at TEXT, detail TEXT,
                PRIMARY KEY(command,day,run_id));
            CREATE TABLE IF NOT EXISTS deliveries (
                command TEXT, day TEXT, state TEXT, detail TEXT,
                PRIMARY KEY(command,day));
            CREATE TABLE IF NOT EXISTS news_notice_deliveries (
                command TEXT, day TEXT, kind TEXT, state TEXT, detail TEXT,
                PRIMARY KEY(command,day,kind));
        ''')
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    async def observe(self, command, run_id, day, detail, send, *, provider_notice_sent=False):
        """Record locally before claiming a one-attempt abnormal/recovery notice."""
        date.fromisoformat(day)
        with self._db() as conn:
            conn.execute('INSERT OR IGNORE INTO outcomes VALUES (?,?,?,?,?)',
                (command, day, run_id, datetime.now().astimezone().isoformat(), json.dumps(detail, ensure_ascii=False)))
            previous_day = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
            previous = conn.execute('SELECT detail FROM outcomes WHERE command=? AND day=? ORDER BY recorded_at DESC, rowid DESC LIMIT 1',
                                    (command, previous_day)).fetchone()
        if command == 'news':
            return await self._observe_news(command, day, detail, previous, send, provider_notice_sent)
        if command == 'topic_maintain':
            alert = detail.get('status') != 'success'
        else:
            pending = int(detail.get('pending', 0) or 0)
            previous_pending = int(json.loads(previous[0]).get('pending', 0) or 0) if previous else 0
            alert = bool(detail.get('failed') or (pending > 0 and previous_pending > 0 and pending >= previous_pending))
        if not alert:
            return 'silent'
        with self._db() as conn:
            claimed = conn.execute('INSERT OR IGNORE INTO deliveries VALUES (?,?,?,?)',
                (command, day, 'provider_notice' if provider_notice_sent else 'sending',
                 json.dumps(detail, ensure_ascii=False))).rowcount
        if not claimed:
            return 'duplicate'
        if provider_notice_sent:
            return 'provider_notice'
        # Persist before sending. Interruption or an ambiguous send is never retried.
        try:
            await send(format_outcome_notice(command, day, detail))
        except Exception:
            state = 'unknown'
        else:
            state = 'sent'
        with self._db() as conn:
            conn.execute('UPDATE deliveries SET state=? WHERE command=? AND day=?', (state, command, day))
        return state

    async def _observe_news(self, command, day, detail, previous, send, provider_notice_sent):
        issues = _news_issues(detail, json.loads(previous[0]) if previous else {})
        content_gap = bool(issues.keys() - {'failure', 'backlog'})
        owns_legacy = False
        with self._db() as conn:
            # Serialize claims across processes; never hold the lock over send().
            conn.execute('BEGIN IMMEDIATE')
            if issues:
                kind = 'alert'
                existing = conn.execute(
                    'SELECT detail FROM news_notice_deliveries WHERE command=? AND day=? AND kind=?',
                    (command, day, kind)).fetchone()
                if existing:
                    merged = json.loads(existing[0])
                    merged['issues'].update(issues)
                    merged['latest_detail'] = detail
                    conn.execute('UPDATE news_notice_deliveries SET detail=? WHERE command=? AND day=? AND kind=?',
                                 (json.dumps(merged, ensure_ascii=False), command, day, kind))
                    return 'duplicate'
                legacy = conn.execute('SELECT state FROM deliveries WHERE command=? AND day=?',
                                      (command, day)).fetchone()
                if legacy and (legacy[0] != 'provider_notice' or not content_gap):
                    return 'duplicate'
                if provider_notice_sent and not content_gap:
                    conn.execute('INSERT OR IGNORE INTO deliveries VALUES (?,?,?,?)',
                                 (command, day, 'provider_notice', json.dumps(detail, ensure_ascii=False)))
                    conn.execute('INSERT OR IGNORE INTO news_notice_deliveries VALUES (?,?,?,?,?)',
                                 (command, day, 'provider', 'provider_notice', json.dumps(
                                     {'issues': issues, 'notified_issues': issues, 'latest_detail': detail},
                                     ensure_ascii=False)))
                    return 'provider_notice'
                payload = {'issues': issues, 'notified_issues': issues, 'latest_detail': detail}
                owns_legacy = bool(conn.execute('INSERT OR IGNORE INTO deliveries VALUES (?,?,?,?)',
                    (command, day, 'sending', json.dumps(detail, ensure_ascii=False))).rowcount)
                text = format_outcome_notice(command, day, detail, issues=issues)
            else:
                kind = 'recovery'
                notified = self._notified_news_issues(conn, command, day)
                if not notified or not _news_restored(notified, detail):
                    return 'silent'
                payload = {'issues': notified, 'latest_detail': detail}
                text = (f'新聞背景整理恢復摘要｜{day}\n'
                        '先前通知的問題已恢復：' + '；'.join(
                            _NEWS_RECOVERY_LABELS.get(issue, '先前處理問題已恢復') for issue in notified) + '\n'
                        + _news_counts(detail) + '\n僅回報處理狀態，不推送一般新聞。')
            claimed = conn.execute('INSERT OR IGNORE INTO news_notice_deliveries VALUES (?,?,?,?,?)',
                (command, day, kind, 'sending', json.dumps(payload, ensure_ascii=False))).rowcount
            if not claimed:
                return 'duplicate'
        # A sending/unknown claim is final for this day and kind, including cancellation.
        try:
            await send(text)
        except Exception:
            state = 'unknown'
        else:
            state = 'sent'
        with self._db() as conn:
            conn.execute('UPDATE news_notice_deliveries SET state=? WHERE command=? AND day=? AND kind=?',
                         (state, command, day, kind))
            if owns_legacy:
                conn.execute('UPDATE deliveries SET state=? WHERE command=? AND day=?', (state, command, day))
        return state

    @staticmethod
    def _notified_news_issues(conn, command, day):
        recovery = conn.execute("SELECT rowid, day FROM news_notice_deliveries "
                                "WHERE command=? AND day<=? AND kind='recovery' ORDER BY rowid DESC LIMIT 1",
                                (command, day)).fetchone()
        after_row, after_day = recovery if recovery else (0, '')
        issues = {}
        # Keep old deliveries immutable and use their confirmed history as well.
        for raw, in conn.execute("SELECT d.detail FROM deliveries d WHERE d.command=? AND d.day>? AND d.day<=? "
                                 "AND d.state IN ('sent','provider_notice') AND NOT EXISTS "
                                 "(SELECT 1 FROM news_notice_deliveries n WHERE n.command=d.command "
                                 "AND n.day=d.day AND (n.kind='provider' OR "
                                 "(n.kind='alert' AND d.state!='provider_notice'))) ORDER BY d.day", (command, after_day, day)):
            old = json.loads(raw)
            old_issues = _news_issues(old)
            if not old_issues and int(old.get('pending', 0) or 0) > 0:
                old_issues.setdefault('backlog', '近期待辦連續兩天未減少')
            issues.update(old_issues)
        for raw, in conn.execute("SELECT detail FROM news_notice_deliveries WHERE command=? AND day<=? "
                                 "AND rowid>? AND kind IN ('alert','provider') "
                                 "AND state IN ('sent','provider_notice') ORDER BY rowid", (command, day, after_row)):
            issues.update(json.loads(raw)['notified_issues'])
        return issues


_NEWS_COMPLETE = {'success', 'ai_success', 'complete', 'completed', 'complete_with_deferred'}
_NEWS_STATUS_LABELS = {
    **dict.fromkeys(_NEWS_COMPLETE, '本次選定新聞已完成判讀'),
    'partial_success': '部分新聞尚未完成判讀',
    'partial': '部分新聞尚未完成判讀',
    'incomplete': '本次判讀尚未完成',
    'pending_ai': '新聞尚待判讀',
    'pending': '新聞尚待判讀',
    'failed': '判讀作業失敗',
    'error': '判讀作業發生錯誤',
    'interrupted': '判讀作業中斷',
    'skipped': '本次尚未執行判讀',
}
_NEWS_SEARCH_REASONS = {
    'search_phase_deadline': '搜尋階段時間已到',
    'insufficient_followup_time': '後續搜尋時間不足',
    'insufficient_search_time': '剩餘搜尋時間不足',
    'transport_unknown': '請求結果尚未確認',
    'work_deadline': '本次處理時間已到',
    'work_cancelled': '本次處理已停止',
    'classification_timeout': '分類等待時間已到',
    'source_checkpoint_failed': '來源保存未完成',
    'authentication_failed': '服務驗證失敗',
    'rate_limited': '服務暫時限制請求',
    'provider_disabled': '服務目前停用',
    'not_configured': '服務尚未設定',
    'balance_unknown': '服務可用額度尚未確認',
    'insufficient_balance': '服務可用額度不足',
    'low_balance': '服務可用額度不足',
    'daily_budget_exceeded': '已達本日費用保護上限',
    'TimeoutError': '搜尋等待逾時',
    'ConnectionError': '搜尋連線中斷',
}
_NEWS_GRACEFUL_SEARCH_STOPS = {'search_phase_deadline', 'insufficient_followup_time', 'insufficient_search_time'}
_NEWS_SEARCH_GAPS_COMPLETE = {'coverage_checked_not_fact_verified', 'complete', 'clear', 'none', 'no_gaps', 'resolved'}
_NEWS_SEARCH_GAPS_LABELS = {
    **dict.fromkeys(_NEWS_SEARCH_GAPS_COMPLETE, '搜尋覆蓋檢查已完成'),
    'declared_incomplete': '仍有搜尋覆蓋缺口待補',
    'partial': '搜尋覆蓋尚未完整',
    'incomplete': '搜尋覆蓋尚未完整',
    'pending': '搜尋覆蓋尚待補齊',
    'unresolved': '搜尋覆蓋缺口尚未清除',
}
_NEWS_RECOVERY_LABELS = {
    'failure': '搜尋或分類作業已恢復',
    'classification': '本次選定新聞已完成判讀',
    'processing': '新聞整理作業已恢復',
    'categories': '必要新聞類別覆蓋已補齊',
    'search': '搜尋覆蓋缺口已清除',
    'dates': '來源日期缺口已清除',
    'backlog': '近期待分類已清除',
}


def _news_reason(reason):
    """Free-text legacy reasons must not reintroduce raw diagnostic codes."""
    def label(match):
        code = match.group()
        if code == 'AI':
            return 'AI'
        if code in NEWS_CATEGORY_LABELS:
            return news_category_label(code)
        return (_NEWS_STATUS_LABELS.get(code) or _NEWS_SEARCH_REASONS.get(code)
                or _NEWS_SEARCH_GAPS_LABELS.get(code) or '其他處理原因（詳見本地紀錄）')
    return re.sub(r'[A-Za-z][A-Za-z0-9_]*', label, str(reason or ''))


def _news_search_gap(detail):
    stop = detail.get('search_stop_reason')
    remaining = detail.get('remaining_search_gaps_status')
    return (detail.get('search_complete') is False
            or bool(remaining and remaining not in _NEWS_SEARCH_GAPS_COMPLETE)
            or bool(stop and (stop not in _NEWS_GRACEFUL_SEARCH_STOPS or detail.get('search_complete') is not True)))


def _has_gaps(value):
    if isinstance(value, dict):
        return any(_has_gaps(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return bool(value)
    if isinstance(value, (int, float)):
        return value > 0
    return bool(value) and str(value).strip() not in {'', '0'}


def _news_issues(detail, previous=None):
    issues = {}
    reason = str(detail.get('reason') or '')
    if detail.get('failed') or detail.get('status') in {'failed', 'error', 'interrupted'}:
        issues['failure'] = '搜尋或分類失敗：' + (_news_reason(reason) or '處理中斷，成果已保存')
    classification = detail.get('classification_status')
    selected_complete = detail.get('selected_classification_complete')
    if selected_complete is False:
        issues['classification'] = '分類未完整：本次選定新聞尚未全部完成判讀'
    elif selected_complete is not True and classification and classification not in _NEWS_COMPLETE:
        issues['classification'] = '分類未完整：' + _NEWS_STATUS_LABELS.get(classification, '分類完成狀態尚待確認，詳見本地紀錄')
    if _has_gaps(detail.get('missing_categories')):
        categories = detail['missing_categories']
        if isinstance(categories, str):
            labels = [news_category_label(categories)]
        elif isinstance(categories, (list, tuple, dict)):
            labels = [news_category_label(str(category)) for category in categories]
        else:
            labels = ['尚有新聞類別待補']
        issues['categories'] = '類別覆蓋不足：' + '、'.join(dict.fromkeys(labels))
    elif 'missing_categories' not in detail and any(word in reason for word in ('類別覆蓋不足', '類別不足', '類別缺口')):
        issues['categories'] = _news_reason(reason)
    if _news_search_gap(detail):
        stop = detail.get('search_stop_reason')
        remaining = detail.get('remaining_search_gaps_status')
        if stop:
            explanation = _NEWS_SEARCH_REASONS.get(stop, '搜尋停止原因尚待確認，詳見本地紀錄')
        elif remaining and remaining not in _NEWS_SEARCH_GAPS_COMPLETE:
            explanation = _NEWS_SEARCH_GAPS_LABELS.get(remaining, '搜尋覆蓋狀態尚待確認，詳見本地紀錄')
        else:
            explanation = '搜尋覆蓋尚未完整'
        issues['search'] = '搜尋未完整：' + explanation
    elif 'search_stop_reason' not in detail and 'search_complete' not in detail and any(
            word in reason for word in ('搜尋未完整', '搜尋未完成', '搜尋中止', '搜尋中斷', '搜尋異常', '搜尋或分類中止')):
        issues['search'] = _news_reason(reason)
    if _has_gaps(detail.get('publication_date_gaps')):
        issues['dates'] = '來源日期缺口：尚有來源日期不明或異常，詳見本地紀錄'
    elif 'publication_date_gaps' not in detail and any(word in reason for word in ('日期不明', '日期異常', '日期缺口')):
        issues['dates'] = _news_reason(reason)
    if selected_complete is not True and 'classification_status' not in detail and detail.get('status') == 'partial_success':
        # An aggregate legacy partial status does not prove selected classification failed.
        classification_reason = re.sub(r'搜尋(?:或|／|/)分類(?:中止|中斷)', '', reason)
        if any(word in classification_reason for word in (
                '分類未完整', '分類未完成', '分類不完整', '分類尚未完整', '分類尚未完成',
                '分類中止', '分類中斷', '分類失敗', '判讀未完整', '判讀未完成', '判讀中止')):
            issues['classification'] = _news_reason(reason)
        elif not issues and reason and not any(word in reason for word in ('待辦', '待分類', '其餘新聞', '保留待補')):
            issues['processing'] = _news_reason(reason)
    pending = int(detail.get('pending', 0) or 0)
    previous_pending = int((previous or {}).get('pending', 0) or 0)
    if pending > 0 and previous_pending > 0 and pending >= previous_pending:
        issues['backlog'] = '近期待辦連續兩天未減少'
    return issues


def _news_restored(issues, detail):
    complete = detail.get('status') in _NEWS_COMPLETE
    checks = {
        'failure': complete or (detail.get('failed') is False
                    and detail.get('selected_classification_complete') is True
                    and detail.get('search_complete') is True),
        'processing': complete,
        'classification': detail.get('selected_classification_complete') is True or
                          (detail.get('selected_classification_complete') is not False and
                           (detail.get('classification_status') in _NEWS_COMPLETE or
                            ('classification_status' not in detail and complete))),
        'categories': 'missing_categories' in detail and detail['missing_categories'] is not None
                      and not _has_gaps(detail['missing_categories']),
        'search': not _news_search_gap(detail) and (detail.get('search_complete') is True or
                  ('search_complete' not in detail and 'search_stop_reason' in detail and not detail['search_stop_reason'])),
        'dates': 'publication_date_gaps' in detail and detail['publication_date_gaps'] is not None
                 and not _has_gaps(detail['publication_date_gaps']),
        'backlog': 'pending' in detail and detail['pending'] is not None and int(detail['pending'] or 0) == 0,
    }
    return all(checks.get(issue, False) for issue in issues)


def _news_counts(detail):
    counts = (f'本次AI判讀 {detail["classified"]} 則，近期待分類 {detail["pending"]} 則。'
              if isinstance(detail.get('classified'), int) and isinstance(detail.get('pending'), int)
              else '本次完成數尚待確認；待分類數以保存的資料庫為準。')
    if isinstance(detail.get('historical_pending'), int):
        counts += f'\n歷史待分類 {detail["historical_pending"]} 則（含日期不明，另列，不併入近期待辦）。'
    return counts


def format_outcome_notice(command, day, detail, *, issues=None):
    if command == 'topic_maintain':
        selected, completed = detail.get('selected'), detail.get('completed')
        counts = (f'本次選定 {selected} 題，AI細節完成 {completed} 題，待補 {max(0, selected-completed)} 題。'
                  if isinstance(selected, int) and isinstance(completed, int) else 'AI細節完成數尚未確認，請查看保存的更新包。')
        return (f'題材庫維護未完整完成｜{day}\n{counts}\n'
                f'原因：{detail.get("reason") or "審核未通過"}\n'
                f'已保存草稿：{detail.get("artifact") or "請查執行紀錄"}\n'
                '本次未套用正式題材庫，不可將草稿當作已確認更新；成功資料保留供後續續處理。')
    reasons = {key: _news_reason(value) for key, value in
               (issues if issues is not None else _news_issues(detail)).items()}
    reason = _news_reason(detail.get('reason'))
    if reason and reason not in reasons.values():
        reasons['detail'] = reason
    return (f'新聞背景整理狀態提醒｜{day}\n{_news_counts(detail)}\n'
            '原因：' + '；'.join(dict.fromkeys(reasons.values()) or ['待辦連續累積']) + '\n'
            '原始來源與已完成結果已保存；未分類、日期不明與歷史資料不視為已完成判讀。')
