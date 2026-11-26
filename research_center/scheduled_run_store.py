"""Durable scheduled-work checkpoints, separate from money and delivery success."""
from contextlib import contextmanager, closing
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from functools import wraps
import asyncio
import json
import os
from pathlib import Path
import sqlite3
from uuid import uuid4


CURRENT_RUN = ContextVar('scheduled_checkpoint_run', default=None)
TAIPEI = timezone(timedelta(hours=8))


def process_creation(pid):
    """Empty means absent, None means unverifiable; never reclaim an unknown owner."""
    if os.name != 'nt':
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return ''
        except OSError:
            return None
        return None
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return '' if ctypes.get_last_error() == 87 else None
    try:
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
            return None
        return str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
    finally:
        kernel.CloseHandle(handle)


def owner_alive(pid, creation):
    actual = process_creation(pid)
    if actual == '':
        return False
    if actual is None or not creation:
        return None
    return actual == creation


class ScheduledRunStore:
    def __init__(self, root):
        self.path = Path(root) / 'runs.sqlite3'

    @contextmanager
    def db(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=10)) as conn, conn:
            conn.row_factory = sqlite3.Row
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY, command TEXT, day TEXT, owner_pid INTEGER,
                    owner_creation TEXT, stage TEXT, status TEXT, updated_at TEXT,
                    detail TEXT, recovery_notice TEXT DEFAULT 'none');
                CREATE TABLE IF NOT EXISTS checkpoints (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT,
                    stage TEXT, saved_at TEXT, payload TEXT);
            ''')
            yield conn

    def begin(self, run_id, command, day, *, owner_pid=None, owner_creation=None):
        pid = os.getpid() if owner_pid is None else owner_pid
        creation = process_creation(pid) if owner_creation is None else owner_creation
        with self.db() as conn:
            conn.execute('INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?)',
                         (run_id, command, day, pid, creation, 'started', 'running',
                          self.stamp(), '{}', 'none'))

    @staticmethod
    def stamp():
        return datetime.now(TAIPEI).isoformat(timespec='seconds')

    def checkpoint(self, run_id, stage, payload):
        encoded = json.dumps(payload, ensure_ascii=False)
        with self.db() as conn:
            row = conn.execute('SELECT status FROM runs WHERE run_id=?', (run_id,)).fetchone()
            if row is None or row['status'] != 'running':
                raise RuntimeError('工作已結束或歸屬不明，不覆寫既有檢查點。')
            conn.execute('INSERT INTO checkpoints(run_id,stage,saved_at,payload) VALUES (?,?,?,?)',
                         (run_id, stage, self.stamp(), encoded))
            conn.execute('UPDATE runs SET stage=?,updated_at=? WHERE run_id=?',
                         (stage, self.stamp(), run_id))

    def finish(self, run_id, status, detail):
        with self.db() as conn:
            conn.execute("UPDATE runs SET status=?,detail=?,updated_at=? WHERE run_id=? AND status='running'",
                         (status, json.dumps(detail, ensure_ascii=False), self.stamp(), run_id))

    def get(self, run_id):
        with self.db() as conn:
            row = conn.execute('SELECT * FROM runs WHERE run_id=?', (run_id,)).fetchone()
            return dict(row) if row else None

    def recover(self, *, is_owner_alive=owner_alive):
        with self.db() as conn:
            rows = [dict(r) for r in conn.execute("SELECT * FROM runs WHERE status='running'")]
        for row in rows:
            if is_owner_alive(row['owner_pid'], row['owner_creation']) is False:
                self.finish(row['run_id'], 'interrupted', {
                    'reason': '原執行程序已退出；保留來源與短評，不自動重送付費請求。',
                    'last_stage': row['stage'], 'previous_detail': json.loads(row['detail']),
                    'cost_reservations_unchanged': True})
        with self.db() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM runs WHERE status='interrupted' AND recovery_notice='none'")]

    async def notify_recovered(self, send):
        for row in self.recover():
            with self.db() as conn:
                claimed = conn.execute("UPDATE runs SET recovery_notice='sending' WHERE run_id=? AND recovery_notice='none'",
                                       (row['run_id'],)).rowcount
            if not claimed:
                continue
            try:
                await send(f"Radar 排程中斷提醒｜{row['day']}\n"
                           f"最後階段：{stage_label(row['stage'])}\n"
                           '來源與已完成短評已保留，未確認費用仍保留預留；不是額度耗盡通知。\n'
                           '不自動重送分析或補發推薦；其他排程仍於執行時重新檢查。')
            except Exception:
                state = 'unknown'
            else:
                state = 'sent'
            with self.db() as conn:
                conn.execute('UPDATE runs SET recovery_notice=? WHERE run_id=?', (state, row['run_id']))


def stage_label(stage):
    return {'started': '準備資料', 'local_data': '本地資料已保存',
            'sources': '外部來源已保存', 'ai_batch': '短評批次已保存',
            'report_ready': '報告已產出', 'delivery_started': '通知發送中',
            'delivery_sent': '通知發送已成功'}.get(stage, '資料處理途中')


def scheduled_store():
    from stock_ai_bot.workdata import get_workdata_path
    return ScheduledRunStore(get_workdata_path('runtime', 'scheduled_runs'))


def checkpoint(stage, payload):
    if current := CURRENT_RUN.get():
        store, run_id = current
        try:
            store.checkpoint(run_id, stage, payload)
        except Exception as exc:
            from .ai_execution_control import CONTROL, WorkStopped
            if control := CONTROL.get():
                control.stop()
            raise WorkStopped('schedule_checkpoint_failed') from exc


def tracked_scheduled_run(command):
    def decorate(function):
        @wraps(function)
        async def run(*args, **kwargs):
            from .deepseek_service import RUN_ID
            run_id = RUN_ID.get() or uuid4().hex
            day = kwargs.get('report_date') or datetime.now(TAIPEI).date()
            store = scheduled_store()
            store.begin(run_id, command, day.isoformat())
            token = CURRENT_RUN.set((store, run_id))
            try:
                result = await function(*args, **kwargs)
            except asyncio.CancelledError:
                store.finish(run_id, 'cancelled', {'reason': '已停止並保存檢查點，未結算費用不變。'})
                raise
            except Exception as exc:
                store.finish(run_id, 'failed', {'error_type': type(exc).__name__, 'reason': str(exc)})
                raise
            else:
                store.finish(run_id, result.get('status', 'completed') if isinstance(result, dict) else 'completed',
                             result if isinstance(result, dict) else {})
                return result
            finally:
                CURRENT_RUN.reset(token)
        return run
    return decorate
