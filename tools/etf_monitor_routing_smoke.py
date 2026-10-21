from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from telegram import Bot

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from stock_ai_bot.monitoring.etf_monitor.config import load_etf_monitor_config
from stock_ai_bot.monitoring.etf_monitor.store import EtfMonitorStore
from stock_ai_bot.workdata import get_workdata_path


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="One-shot real Telegram routing smoke for the 0050 shadow monitor."
    )
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--confirm-chat-id", required=True)
    return parser.parse_args()


async def _run() -> int:
    args = _arguments()
    monitor_config = load_etf_monitor_config()
    if not args.send or args.confirm_chat_id != monitor_config.chat_id:
        raise SystemExit(
            "Refusing to send: require --send and exact --confirm-chat-id from etf_monitor.json"
        )
    store = EtfMonitorStore(get_workdata_path(monitor_config.database_relative_path))
    if store.routing_smoke_sent():
        raise SystemExit("Refusing to send: this work-data profile already recorded a routing smoke")
    main_config = json.loads((PROJECT_ROOT / "config.json").read_text(encoding="utf-8-sig"))
    token = str(main_config.get("api_token") or "").strip()
    if not token:
        raise SystemExit("Existing config.json has no api_token")

    bot = Bot(token=token)
    try:
        chat = await bot.get_chat(chat_id=monitor_config.chat_id)
    except Exception:
        raise SystemExit("getChat verification failed; no message was sent") from None
    if str(chat.id) != monitor_config.chat_id or str(chat.type) not in {"group", "supergroup"}:
        raise SystemExit("getChat did not verify the dedicated ETF group")
    if not store.reserve_routing_smoke(monitor_config.chat_id):
        raise SystemExit("Refusing to send: routing smoke was already reserved")
    try:
        message = await bot.send_message(
            chat_id=monitor_config.chat_id,
            text=(
                "測試訊息｜非即時交易建議\n"
                "0050 final-v1.7 ETF 影子監控路由驗證。\n"
                "此訊息不代表 BUY、SELL 或 HOLD 市場判斷。"
            ),
            disable_web_page_preview=True,
        )
    except Exception:
        raise SystemExit(
            "sendMessage failed; the one-shot reservation remains to prevent a duplicate"
        ) from None
    store.mark_routing_smoke_sent(monitor_config.chat_id, str(message.message_id))
    print("Routing smoke delivered and permanently recorded for this work-data profile.")
    return 0


def main() -> int:
    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
