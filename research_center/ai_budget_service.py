"""Persistent, atomic monthly AI budgets and deduplicated incident notices."""
from __future__ import annotations

import calendar
from contextlib import contextmanager, closing
from datetime import datetime
from decimal import Decimal
from pathlib import Path
import sqlite3
import re
from uuid import uuid4
from zoneinfo import ZoneInfo


class BudgetBlocked(RuntimeError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


REASONS = {
    "monthly_budget_exceeded": "本月 AI 總預算已用完或不足以預留本次費用",
    "background_budget_exceeded": "本月排程預算已用完或不足以預留本次費用",
    "manual_budget_exceeded": "本月手動與測試預算不足",
    "schedule_pacing": "排程已達本月目前進度的費用保護額度，後續日期會重新檢查",
    "job_budget_exceeded": "本次工作預估費用超過單次保護值，請先確認費用再調整設定",
    "insufficient_balance": "DeepSeek 官方餘額不足，請儲值",
    "authentication_failed": "DeepSeek 金鑰驗證失敗，請檢查設定",
    "balance_unknown": "暫時無法查證 DeepSeek 餘額，未啟動新付費請求",
    "low_balance": "官方餘額不足以預留本次需求與背景保留額",
    "rate_limited": "DeepSeek 暫時限流，已進入冷卻",
    "not_configured": "尚未設定 DeepSeek 金鑰",
    "transport_unknown": "DeepSeek 連線逾時或中斷，該次費用未知，已保留成功成果",
    "service_unavailable": "DeepSeek 服務暫時不可用，已停止重複呼叫",
}


def shared_budget_root(root: Path) -> Path:
    """Use the Git common checkout so switching worktrees cannot reset spending."""
    root = Path(root).resolve()
    marker = root / ".git"
    if marker.is_file():
        text = marker.read_text(encoding="utf-8-sig").strip()
        if text.startswith("gitdir:"):
            gitdir = Path(text.split(":", 1)[1].strip())
            if not gitdir.is_absolute():
                gitdir = root / gitdir
            common_file = gitdir / "commondir"
            if common_file.is_file():
                root = (gitdir / common_file.read_text(encoding="utf-8").strip()).resolve().parent
    return root / ".workdata" / "ai_budget"


class MonthlyBudget:
    def __init__(self, root, *, total=10, background=7, manual=2, reserve=1, job_limit=.30, pacing=True, manual_allowances=None):
        self.root = Path(root)
        self.total, self.background, self.manual, self.reserve = map(lambda x: Decimal(str(x)), (total, background, manual, reserve))
        self.job_limit = Decimal(str(job_limit))
        self.pacing = pacing
        self.manual_allowances = {str(month): Decimal(str(amount)) for month, amount in (manual_allowances or {}).items()}
        if any(not re.fullmatch(r'\d{4}-(?:0[1-9]|1[0-2])', month) or not amount.is_finite() or amount < 0
               for month, amount in self.manual_allowances.items()):
            raise ValueError('單月追加額度須使用 YYYY-MM 與非負有限金額。')
        if min(self.total, self.background, self.manual, self.reserve, self.job_limit) < 0 or self.total <= 0:
            raise ValueError("AI 月度預算必須為正數，分額不可為負數。")
        if self.background + self.manual + self.reserve > self.total:
            raise ValueError("AI 排程、手動與緩衝分額合計不得超過月額。")

    @contextmanager
    def db(self):
        self.root.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.root / "budget.sqlite3", timeout=20)
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS spending (id TEXT PRIMARY KEY, day TEXT, background INTEGER, command TEXT, run_id TEXT, status TEXT, estimate TEXT, actual TEXT)")
            conn.execute("CREATE TABLE IF NOT EXISTS incidents (scope TEXT PRIMARY KEY, reason TEXT)")
            conn.execute("CREATE TABLE IF NOT EXISTS notices (id TEXT PRIMARY KEY, scope TEXT, kind TEXT, reason TEXT, message TEXT, state TEXT)")
            with conn:
                yield conn
        finally:
            conn.close()

    def import_legacy(self, path):
        path = Path(path)
        if not path.is_file():
            return
        with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as old:
            rows = old.execute("SELECT id,day,background,command,status,estimated_usd,actual_usd FROM usage").fetchall()
        with self.db() as conn:
            conn.executemany("INSERT OR IGNORE INTO spending VALUES (?,?,?,?,?,?,?,?)",
                [(i,d,b,c,"legacy:"+i,s,e,a) for i,d,b,c,s,e,a in rows])

    def _check(self, conn, estimate, background, run_id, now):
        month = now.strftime("%Y-%m")
        total_limit, manual_limit = self._limits(now)
        rows = conn.execute("SELECT background,run_id,status,estimate,actual FROM spending WHERE day LIKE ?", (month+"-%",)).fetchall()
        values = [(b,r,Decimal(a if a is not None else e)) for b,r,s,e,a in rows if s not in ("failed", "rejected")]
        total = sum((v for _,_,v in values), Decimal(0))
        used = sum((v for b,_,v in values if bool(b) == bool(background)), Decimal(0))
        def exceeds(value, limit):
            return value + estimate > limit or (estimate == 0 and value >= limit)
        if exceeds(total, total_limit - self.reserve):
            raise BudgetBlocked("monthly_budget_exceeded")
        if exceeds(used, self.background if background else manual_limit):
            raise BudgetBlocked("background_budget_exceeded" if background else "manual_budget_exceeded")
        if background and self.pacing:
            pace = min(self.background, self.background * now.day / calendar.monthrange(now.year, now.month)[1] + Decimal("0.50"))
            if exceeds(used, pace):
                raise BudgetBlocked("schedule_pacing")
        job = sum((v for _,r,v in values if r == run_id), Decimal(0)) if run_id else Decimal(0)
        if run_id and self.job_limit and job + estimate > self.job_limit:
            raise BudgetBlocked("job_budget_exceeded")

    def check(self, *, estimate=Decimal(0), background=False, run_id="", now=None):
        now = now or datetime.now(ZoneInfo("Asia/Taipei"))
        with self.db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._check(conn, estimate, background, run_id, now)

    def reserve_request(self, request_id, command, background, run_id, estimate, now=None):
        now = now or datetime.now(ZoneInfo("Asia/Taipei"))
        with self.db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._check(conn, estimate, background, run_id, now)
            conn.execute("INSERT INTO spending VALUES (?,?,?,?,?,?,?,?)", (request_id,now.date().isoformat(),int(background),command,run_id,"pending",str(estimate),None))

    def finish(self, request_id, status, actual):
        with self.db() as conn:
            conn.execute("UPDATE spending SET status=?,actual=? WHERE id=?", (status,str(actual) if actual is not None else None,request_id))

    def incident(self, reason, *, background, preflight=False):
        scope = "background" if background else "manual"
        with self.db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            previous = conn.execute("SELECT reason FROM incidents WHERE scope=?", (scope,)).fetchone()
            old = previous[0] if previous else ""
            if preflight and not reason and old in {'rate_limited','transport_unknown','service_unavailable'}:
                return
            reason = reason or ""
            if old == reason:
                return
            conn.execute("INSERT OR REPLACE INTO incidents VALUES (?,?)", (scope,reason))
            if reason:
                message = "AI 背景工作暫停：" + REASONS.get(reason, "AI 服務暫時不可用") + "。已完成資料已保留；其他非 AI 排程照常執行。"
                kind = "blocked"
            else:
                message = "AI 背景服務檢查已恢復，可繼續付費工作；先前待補工作不代表已自動完成。"
                kind = "recovered"
            conn.execute("INSERT INTO notices VALUES (?,?,?,?,?,?)", (uuid4().hex,scope,kind,reason,message,"pending"))

    def claim_notice(self):
        with self.db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT id,message FROM notices WHERE scope='background' AND state='pending' ORDER BY rowid LIMIT 1").fetchone()
            if row:
                conn.execute("UPDATE notices SET state='sending' WHERE id=?", (row[0],))
            return row

    def notice_result(self, notice_id, success):
        with self.db() as conn:
            conn.execute("UPDATE notices SET state=? WHERE id=?", ("sent" if success else "delivery_unknown",notice_id))

    def summary(self, now=None):
        now = now or datetime.now(ZoneInfo("Asia/Taipei"))
        total_limit, manual_limit = self._limits(now)
        with self.db() as conn:
            rows = conn.execute("SELECT background,status,estimate,actual FROM spending WHERE day LIKE ?", (now.strftime("%Y-%m")+"-%",)).fetchall()
        charged = [(bool(b),Decimal(a if a is not None else e)) for b,s,e,a in rows if s not in ("failed","rejected")]
        return {"month":now.strftime("%Y-%m"),"total_limit":str(total_limit),"reserve":str(self.reserve),
                "background_limit":str(self.background),"manual_limit":str(manual_limit),
                "used_or_reserved":str(sum((v for _,v in charged),Decimal(0))),
                "background_used":str(sum((v for b,v in charged if b),Decimal(0))),
                "manual_used":str(sum((v for b,v in charged if not b),Decimal(0))),
                "unknown_or_pending":sum(s in ("unknown","pending") for _,s,_,_ in rows)}

    def _limits(self, now):
        extra = self.manual_allowances.get(now.strftime('%Y-%m'), Decimal(0))
        return self.total + extra, self.manual + extra
