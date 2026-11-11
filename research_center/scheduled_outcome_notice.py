"""Persist schedule outcomes and one-attempt, restart-safe abnormal summaries."""
from datetime import date, datetime, timedelta
from pathlib import Path
from contextlib import contextmanager
import json
import sqlite3


class ScheduledOutcomeNotices:
    def __init__(self, root: Path):
        self.path = Path(root) / 'outcomes.sqlite3'

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
        ''')
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    async def observe(self, command, run_id, day, detail, send, *, provider_notice_sent=False):
        """Normal news is silent; recurring backlog or a real failure is disclosed."""
        date.fromisoformat(day)
        with self._db() as conn:
            conn.execute('INSERT OR IGNORE INTO outcomes VALUES (?,?,?,?,?)',
                (command, day, run_id, datetime.now().astimezone().isoformat(), json.dumps(detail, ensure_ascii=False)))
            previous_day = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
            previous = conn.execute('SELECT detail FROM outcomes WHERE command=? AND day=? ORDER BY recorded_at DESC LIMIT 1',
                                    (command, previous_day)).fetchone()
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


def format_outcome_notice(command, day, detail):
    if command == 'topic_maintain':
        selected, completed = detail.get('selected'), detail.get('completed')
        counts = (f'本次選定 {selected} 題，AI細節完成 {completed} 題，待補 {max(0, selected-completed)} 題。'
                  if isinstance(selected, int) and isinstance(completed, int) else 'AI細節完成數尚未確認，請查看保存的更新包。')
        return (f'題材庫維護未完整完成｜{day}\n{counts}\n'
                f'原因：{detail.get("reason") or "審核未通過"}\n'
                f'已保存草稿：{detail.get("artifact") or "請查執行紀錄"}\n'
                '本次未套用正式題材庫，不可將草稿當作已確認更新；成功資料保留供後續續處理。')
    counts = (f'本次AI判讀 {detail["classified"]} 則，近期待分類 {detail["pending"]} 則。'
              if 'classified' in detail and 'pending' in detail else '本次完成數尚待確認；待分類數以保存的資料庫為準。')
    return (f'新聞背景整理狀態提醒｜{day}\n{counts}\n'
            f'原因：{detail.get("reason") or "待辦連續累積"}\n'
            '原始來源與已完成結果已保存；未分類、日期不明與歷史資料不視為已完成判讀。')
