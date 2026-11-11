"""Send exactly one explicitly authorized, replyable simulation per request ID."""
import argparse
import asyncio
import json
from datetime import datetime
from pathlib import Path

from telegram import Bot
from stock_ai_bot.monitoring.etf_monitor.config import load_etf_monitor_config
from stock_ai_bot.monitoring.etf_monitor.execution import TAIPEI
from stock_ai_bot.monitoring.etf_monitor.sizing_config import load_reply_config
from stock_ai_bot.monitoring.etf_monitor.sizing_simulation import SizingSimulations
from stock_ai_bot.workdata import get_workdata_path


async def send(args):
    config = load_etf_monitor_config()
    if not args.send or args.confirm_chat_id != config.chat_id or not load_reply_config().enabled:
        raise SystemExit('Require explicit --send and the exact enabled 0050 chat ID')
    token = json.loads(Path('config.json').read_text(encoding='utf-8-sig'))['api_token']
    registry = SizingSimulations(get_workdata_path('runtime/etf_monitor/sizing-simulations.sqlite3'))
    async with Bot(token) as bot:
        chat = await bot.get_chat(config.chat_id)
        if str(chat.id) != config.chat_id or chat.type not in {'group', 'supergroup'}:
            raise SystemExit('Route identity mismatch; no send')
        body = registry.reserve(args.request_id, bot.id, datetime.now(TAIPEI))
        registry.sending(args.request_id)
        try:
            message = await bot.send_message(chat_id=config.chat_id, text=body,
                disable_web_page_preview=True, parse_mode=None)
            registry.delivered(args.request_id, message.message_id)
        except BaseException as error:
            registry.uncertain(args.request_id, type(error).__name__)
            raise SystemExit('Simulation delivery uncertain; reservation retained, no automatic resend') from None
        print(json.dumps({'status': 'DELIVERED_SIMULATION', 'message_id': message.message_id,
                          'request_id': args.request_id, 'send_attempts': 1}))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--send', action='store_true')
    parser.add_argument('--confirm-chat-id', required=True)
    parser.add_argument('--request-id', required=True)
    asyncio.run(send(parser.parse_args()))


if __name__ == '__main__':
    main()
