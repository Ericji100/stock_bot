"""Shared, date-checked cache for public all-market daily JSON reports."""
from contextlib import closing
from datetime import date
import hashlib
import json
from pathlib import Path
import sqlite3
from threading import RLock
import time

_LOCK=RLock()


class OfficialDailyCache:
    def __init__(self,root: Path):
        self.root=Path(root)

    def get(self,url,params,target_date,loader):
        from research_center.data_source_gateway import keyed_request_lock
        key=hashlib.sha256(json.dumps([url,params,target_date.isoformat()],sort_keys=True).encode()).hexdigest()
        day=target_date.strftime('%Y%m%d')
        with keyed_request_lock(f'official:{self.root.resolve()}:{key}'):
            self.root.mkdir(parents=True,exist_ok=True)
            with closing(sqlite3.connect(self.root/'daily.sqlite3',timeout=15)) as conn,conn:
                conn.execute('CREATE TABLE IF NOT EXISTS responses (key TEXT PRIMARY KEY, expires REAL, payload TEXT)')
                row=conn.execute('SELECT payload FROM responses WHERE key=? AND expires>?',(key,time.time())).fetchone()
                if row:
                    payload=json.loads(row[0])
                    if self.valid(payload,day):
                        return payload
            # SQLite connections and the global DB lock must not span network I/O.
            payload=loader()
            if self.valid(payload,day):
                with _LOCK, closing(sqlite3.connect(self.root/'daily.sqlite3',timeout=15)) as conn,conn:
                    ttl=300 if target_date==date.today() else 21600
                    conn.execute('INSERT OR REPLACE INTO responses VALUES (?,?,?)',(key,time.time()+ttl,json.dumps(payload,ensure_ascii=False)))
            return payload

    @staticmethod
    def valid(payload,day):
        if not isinstance(payload,dict) or payload.get('stat')!='OK' or str(payload.get('date'))!=day:
            return False
        return bool(payload.get('data') or any(t.get('data') for t in payload.get('tables',[]) if isinstance(t,dict)))
