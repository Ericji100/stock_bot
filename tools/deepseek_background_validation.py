"""Paid scheduler-core validation with real AI/search and a local-only Telegram sink."""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
from datetime import datetime
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0,str(ROOT))

import main as bot_main
from research_center.deepseek_service import configured_client
from research_center.orchestrator import ResearchCenter
from tools.deepseek_live_validation import input_and_usage_audit


class LocalTelegramSink:
    def __init__(self):
        self.messages=[]

    async def send_message(self,chat_id,text,**kwargs):
        self.messages.append({'kind':'message','text':text})

    async def send_document(self,chat_id,document,**kwargs):
        self.messages.append({'kind':'document','filename':str(getattr(document,'name','')),'caption':kwargs.get('caption','')})


def main(argv=None):
    parser=argparse.ArgumentParser(description='會消耗官方額度；不啟動 Bot、不連 Telegram、不套用題材更新。')
    parser.add_argument('--case',choices=['radar','news','topic_maintain'],required=True)
    args=parser.parse_args(argv)
    account=configured_client().account
    with account._db() as conn:
        before_usage={r[0] for r in conn.execute('SELECT id FROM usage')}
        before_activity={r[0] for r in conn.execute('SELECT id FROM activity')}
    sink=LocalTelegramSink()
    captured={}
    original=bot_main.run_radar
    original_news=bot_main.run_news_refresh
    original_run=ResearchCenter.run
    def radar(*a,**kw):
        result=original(*a,**kw)
        captured['radar']=asdict(result)
        return result
    def news(*a,**kw):
        items,meta=original_news(*a,**kw)
        captured['news']={'items':[item.to_dict() for item in items],'meta':meta}
        return items,meta
    def center_run(center,*a,**kw):
        result=original_run(center,*a,**kw)
        captured['command_result']=result.report_json
        return result
    functions={'radar':bot_main._scheduled_radar_push,'news':bot_main._scheduled_news_refresh,'topic_maintain':bot_main._scheduled_topic_maintain}
    started=time.monotonic()
    record={'case':args.case,'telegram_sent':False,'notification_transport':'local_capture_only','scheduled_time_triggered':False}
    try:
        with patch.object(bot_main,'load_config',return_value={'chat_id':'local-capture-only','scan_settings':{}}), \
             patch.object(bot_main,'run_radar',side_effect=radar), \
             patch.object(bot_main,'run_news_refresh',side_effect=news), \
             patch.object(ResearchCenter,'run',new=center_run):
            record['outcome']=asyncio.run(functions[args.case](SimpleNamespace(bot=sink,job=None))) or {'status':'completed'}
    except Exception as exc:
        record['outcome']={'status':'failed','error_type':type(exc).__name__}
    record['elapsed_seconds']=round(time.monotonic()-started,2)
    record['captured_messages']=sink.messages
    source_data={}
    if captured.get('radar'):
        radar_data=captured['radar']
        source_data={'sources':[source for c in radar_data['candidates'] if c['code'] in radar_data['ai_enriched_codes']
                                for source in [*c.get('ai_sources',[]),*c.get('web_sources',[])]]}
    elif captured.get('news'):
        source_data={'sources':[{'url':item['url']} for item in captured['news']['items']
                                if 'local_rule_fallback' not in item.get('tags',[])]}
    elif captured.get('command_result'):
        source_data=captured['command_result']
    record['usage']=input_and_usage_audit(account,before_usage,before_activity,source_data)
    record['budget']=account.budget.summary()
    record['quality_review']='pending; runtime completion alone is not report quality approval'
    directory=ROOT/'logs'/'deepseek_live_validation'/datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    directory.mkdir(parents=True)
    (directory/'scheduled.json').write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')
    if captured:
        (directory/'observed-results.json').write_text(json.dumps(captured,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    print(f'排程核心驗證資料：{directory}；未測試真實排程時鐘或 Telegram 連線。',flush=True)
    return 0 if record['outcome']['status']=='completed' else 1


if __name__=='__main__':
    raise SystemExit(main())
