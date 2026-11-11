"""Explicit, short-lived simulation sessions, isolated from the strategy store."""
from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from .config import ETF_MONITOR_CHAT_ID
from .position_sizing import render_estimate, validate_prices


SIMULATION_HEADER = "0050 趨勢策略｜模擬進場試算（非真實交易訊號）"
RESULT_HEADER = "0050 股數試算｜模擬測試（非真實交易訊號）"


class SimulationRejected(ValueError):
    pass


class SizingSimulations:
    def __init__(self, path: Path):
        self.path = path

    def reserve(self, request_id: str, bot_id: int, now: datetime) -> str:
        if not request_id or len(request_id) > 100 or now.tzinfo is None:
            raise ValueError("invalid simulation request")
        expires = now + timedelta(hours=2)
        body = (f"{SIMULATION_HEADER}\n"
                f"測試時間：{now:%Y-%m-%d %H:%M}\n"
                f"測試有效至：{expires:%Y-%m-%d %H:%M}\n\n"
                "情境：假設正式進場條件與晚間核對均已通過。\n"
                "模擬進場參考價：120.00 元\n模擬初始防守價：108.00 元\n"
                "以上均為虛構範例，不是今日行情，沒有真實交易安排。\n\n"
                "請回覆這則訊息輸入可用現金，例如 100000；\n"
                "或輸入「現金 模擬委託價」，例如 100000 123.4。\n"
                "Bot 會回覆明標模擬的股數、分筆費用、餘款及 20% 估計風險。\n"
                "僅原授權帳號可測試，金額在群組可見。測試不記入正式訊號或持倉，不會下單。")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=5)) as connection, connection:
            connection.execute("CREATE TABLE IF NOT EXISTS simulations (request_id TEXT PRIMARY KEY, "
                "chat_id TEXT NOT NULL,bot_id INTEGER NOT NULL,message_id INTEGER,created_at TEXT NOT NULL,"
                "expires_at TEXT NOT NULL,status TEXT NOT NULL,price TEXT NOT NULL,stop TEXT NOT NULL,"
                "body_sha256 TEXT NOT NULL,error_type TEXT,UNIQUE(chat_id,message_id))")
            # A request ID is a one-shot reservation, including uncertain outcomes.
            connection.execute("INSERT INTO simulations VALUES(?,?,?,NULL,?,?,'RESERVED','120','108',?,NULL)",
                (request_id, ETF_MONITOR_CHAT_ID, bot_id, now.isoformat(), expires.isoformat(),
                 hashlib.sha256(body.encode()).hexdigest()))
        return body

    def sending(self, request_id):
        with closing(sqlite3.connect(self.path, timeout=5)) as connection, connection:
            changed = connection.execute("UPDATE simulations SET status='SENDING' WHERE request_id=? AND status='RESERVED'", (request_id,)).rowcount
            if changed != 1:
                raise ValueError("simulation is not sendable")

    def delivered(self, request_id, message_id):
        if type(message_id) is not int or message_id <= 0:
            raise ValueError("simulation acknowledgement missing")
        with closing(sqlite3.connect(self.path, timeout=5)) as connection, connection:
            changed = connection.execute("UPDATE simulations SET status='DELIVERED',message_id=? "
                "WHERE request_id=? AND status='SENDING'", (message_id, request_id)).rowcount
            if changed != 1:
                raise ValueError("simulation delivery reservation mismatch")

    def uncertain(self, request_id, error_type):
        with closing(sqlite3.connect(self.path, timeout=5)) as connection, connection:
            connection.execute("UPDATE simulations SET status='UNCERTAIN',error_type=? "
                               "WHERE request_id=? AND status='SENDING'", (error_type, request_id))

    def resolve(self, *, chat_id, bot_id, message_id, text, now):
        if not self.path.is_file():
            raise SimulationRejected("這則模擬通知尚未登記，請使用最新的可回覆測試通知。")
        with closing(sqlite3.connect(f'{self.path.resolve().as_uri()}?mode=ro', uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute("SELECT * FROM simulations WHERE chat_id=? AND bot_id=? AND message_id=?",
                                     (str(chat_id), bot_id, message_id)).fetchone()
        if row is None or row['status'] != 'DELIVERED' or str(chat_id) != ETF_MONITOR_CHAT_ID:
            raise SimulationRejected("這則模擬通知沒有已確認的測試送達紀錄，暫不試算。")
        if hashlib.sha256(text.encode()).hexdigest() != row['body_sha256']:
            raise SimulationRejected("模擬通知內容已改變，請使用新發送的測試通知。")
        start, end = datetime.fromisoformat(row['created_at']), datetime.fromisoformat(row['expires_at'])
        if not start <= now < end:
            raise SimulationRejected("這則模擬試算已過期，請勿當成正式進場通知使用。")
        price, stop = Decimal(row['price']), Decimal(row['stop'])
        validate_prices(price, stop)
        return {'price': price, 'stop': stop, 'trading_date': start.date().isoformat(),
                'regime': 'SMA21_GE_SMA144', 'confirmed': True, 'simulation': True,
                'expires_at': end.isoformat()}


def render_simulation(value, *, quote, cash, override):
    rendered = render_estimate(value, quote=quote, cash=cash, override=override)
    _, _, detail = rendered.partition('\n')
    detail = detail.replace('訊號參考價：', '模擬參考價：').replace('自填委託價：', '自填模擬委託價：')
    detail = detail.replace('使用訊號價時僅供前一晚參考；價格改變請回覆原進場通知，輸入「現金 委託價」重算。',
                            '使用虛構範例價；改價請回覆原模擬通知輸入「現金 模擬委託價」重算。')
    detail = detail.replace('依自填價格重算，未取得即時報價或驗證當日漲跌幅。',
                            '依自填模擬價格重算，不使用真實行情。')
    return f'{RESULT_HEADER}\n{detail}\n\n以上僅為試算演練，沒有正式 BUY、真實成交或持倉變更，請勿據此下單。'
