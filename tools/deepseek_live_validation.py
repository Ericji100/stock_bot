"""Explicit, paid official-API smoke tests. Captures messages without sending Telegram."""
from __future__ import annotations

import argparse
import faulthandler
from dataclasses import asdict
from datetime import datetime
import json
import re
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research_center.config import load_research_config
from research_center.orchestrator import ResearchCenter
from stock_ai_bot.monitoring.radar_service import run_radar, format_radar_report

COMMANDS = {
    "research": "/research 2330 --deep",
    "value_scan": "/value_scan 2330 --deep",
    "macro": "/macro 台股",
    "theme": "/theme AI電源",
    "theme_flow": "/theme_flow AI電源",
    "theme_radar": "/theme_radar",
    "sector_strength": "/sector_strength",
    "radar": "/radar --ai-top 3",
    "news": "/news refresh",
    "topic_maintain": "/topic_maintain --deep",
}


def input_and_usage_audit(account, before_usage, before_activity, result_json):
    with account._db() as conn:
        rows=conn.execute("SELECT id,operation,status,actual_usd,detail FROM usage").fetchall()
        activities=conn.execute("SELECT id,operation,cache_hit,detail FROM activity").fetchall()
    rows=[row for row in rows if row[0] not in before_usage]
    activities=[row for row in activities if row[0] not in before_activity]
    prompt_paths=[json.loads(d).get("prompt_path") for _,op,_,d in activities if op=='model']
    texts=[]
    for path in prompt_paths:
        body=json.loads(Path(path).read_text(encoding='utf-8'))['body']
        texts.extend(str(m.get('content') or '') for m in body['messages'])
    combined='\n'.join(texts)
    sources=result_json.get('sources') or []
    if not sources and result_json.get('change_pack'):
        def evidence_urls(value):
            if isinstance(value,dict):
                for key,item in value.items():
                    if key in {'url','source_url'} and isinstance(item,str) and item.startswith(('https://','http://')):
                        yield item
                    else:
                        yield from evidence_urls(item)
            elif isinstance(value,list):
                for item in value:
                    yield from evidence_urls(item)
        sources=[{'url':url} for url in dict.fromkeys(evidence_urls(result_json['change_pack']))]
    audit=(result_json.get('metadata') or {}).get('ai_input_audit') or {}
    available=(audit.get('structured_coverage') or {}).get('available_sections') or []
    missing_urls=[s.get('url') for s in sources if s.get('url') and s['url'] not in combined]
    input_tokens=output_tokens=hit_tokens=0
    http_attempts=retry_attempts=0
    statuses={}
    operations={}
    for _,op,status,_,detail in rows:
        statuses[status]=statuses.get(status,0)+1
        operations[op]=operations.get(op,0)+1
        detail=json.loads(detail)
        http_attempts+=int(detail.get('attempts') or 1)
        retry_attempts+=int(detail.get('retry_count') or 0)
        usage=detail.get('usage') or {}
        input_tokens+=int(usage.get('prompt_tokens') or 0)
        output_tokens+=int(usage.get('completion_tokens') or 0)
        hit_tokens+=int(usage.get('prompt_cache_hit_tokens') or 0)
    return {'paid_attempts':len(rows),'operations':operations,'statuses':statuses,
        'http_attempts':http_attempts,'retry_attempts':retry_attempts,
        'source_inventory_available':bool(sources),
        'estimated_usd':round(sum(float(r[3] or 0) for r in rows),8),
        'unknown_fee_count':sum(r[3] is None and r[2] not in ('failed','rejected') for r in rows),
        'input_tokens':input_tokens,'output_tokens_including_reasoning':output_tokens,'provider_cache_hit_tokens':hit_tokens,
        'cache_hit_operations':sum(bool(r[2]) for r in activities),'actual_model_prompt_paths':prompt_paths,
        'actual_model_prompt_chars':sum(len(t) for t in texts),'source_urls_not_found_in_sent_text':missing_urls,
        'core_field_names_not_found_in_sent_text':[key for key in available if key not in combined],
        'explicit_data_truncation_markers':re.findall(r'\.\.\. \[truncated(?: for detail stage)?, total \d+ chars\]',combined),
        'declared_source_data_gaps':(audit.get('structured_coverage') or {}).get('missing_sections') or [],
        'coverage_caveat':'網址或欄位名稱出現只證明索引或字串存在，不代表所有正文與數值已被完整分析；需檢閱實際 prompt 與報告。'}


def main(argv=None):
    parser = argparse.ArgumentParser(description="官方 DeepSeek 真實測試；會消耗搜尋與模型額度，不發 Telegram、不套用題材變更。")
    parser.add_argument("--case", choices=list(COMMANDS), action="append")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--radar-source", choices=['combined','technical','curated','chip'], default='technical')
    parser.add_argument("--allow-market-scan", action="store_true", help="明確允許雷達缺少候選快取時重掃全市場。")
    parser.add_argument("--check-config", action="store_true", help="只檢查設定，不連線、不付費。")
    args = parser.parse_args(argv)
    if (args.all or 'radar' in (args.case or [])) and not args.allow_market_scan:
        from stock_ai_bot.monitoring import radar_service
        target_date, _ = radar_service.resolve_radar_report_date()
        technical = radar_service._find_technical_scan_cache(target_date)
        curated = radar_service.curated_scan_service.find_cached_curated_scan(target_date)
        required = {'technical': bool(technical), 'curated': bool(curated),
                    'combined': bool(technical and curated), 'chip': False}
        if not required[args.radar_source]:
            print("雷達缺少所選來源的同日有效快取；未啟動付費測試。需先完成選股，或明確指定 --allow-market-scan。")
            return 2
    config = load_research_config()
    if not config.deepseek_only or not config.deepseek_api_key:
        print("尚未設定官方 DeepSeek 金鑰或未啟用遷移模式；未開始任何付費請求。")
        return 2
    if args.check_config:
        print(json.dumps({"provider": "deepseek", "model": config.deepseek_model, "key_configured": True}, ensure_ascii=False))
        return 0
    # No Bot/Telegram client is created. Ordinary command paths perform real collection and analysis.
    center = ResearchCenter(config)
    records = []
    run_dir = ROOT / "logs" / "deepseek_live_validation" / (datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
    run_dir.mkdir(parents=True, exist_ok=False)
    stack_log = (run_dir / "stalled-stacks.log").open("w", encoding="utf-8")
    faulthandler.dump_traceback_later(180, repeat=True, file=stack_log)
    try:
        center.deepseek.ensure_ready()
        for name in COMMANDS if args.all else (args.case or ['research']):
            command = COMMANDS[name]
            if name == 'radar':
                command = f"/radar --source {args.radar_source} --ai-top 3"
            case_dir = run_dir / name
            case_dir.mkdir()
            messages = []
            def progress(message):
                messages.append(str(message))
                print(message, flush=True)
            started = time.monotonic()
            with center.deepseek.account._db() as conn:
                before_usage={r[0] for r in conn.execute('SELECT id FROM usage')}
                before_activity={r[0] for r in conn.execute('SELECT id FROM activity')}
            record = {"command": command, "manual_quality_review": "pending", "telegram_sent": False}
            result_json={}
            try:
                if name == "radar":
                    result = run_radar(command.split()[1:], progress=progress)
                    text = format_radar_report(result, limit=50)
                    selected = [c for c in result.candidates if c.code in result.ai_enriched_codes]
                    result_json={'sources':[source for c in selected for source in [*c.ai_sources,*c.web_sources]],'metadata':{}}
                    formal = bool(selected) and all(c.ai_comment.get("status") == "ok" for c in selected)
                    record.update({"formal_ai_success": formal, "status": "success" if formal else "partial_success"})
                    (case_dir / "result.json").write_text(json.dumps(asdict(result), ensure_ascii=False, indent=2, default=str), encoding="utf-8")
                else:
                    result = center.run_text_command(command, progress=progress)
                    result_json=result.report_json
                    text = result.markdown or result.summary
                    meta = result.report_json.get("metadata") or {}
                    formal = result.status == "success" and result.ai_used and not result.fallback_reason and meta.get("ai_status", "ai_success") == "ai_success"
                    if name not in {'news','topic_maintain'}:
                        formal = formal and meta.get('qa_validation',{}).get('passed') is True
                    record.update({"status": result.status, "formal_ai_success": formal,
                        "fallback_reason": result.fallback_reason, "model": result.ai_model,
                        "artifact_paths": asdict(result.artifacts), "source_count": len(result.sources)})
                    (case_dir / "result.json").write_text(json.dumps(result.report_json, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
                (case_dir / "output.md").write_text(text, encoding="utf-8")
            except Exception as exc:
                record.update({"status": "failed", "formal_ai_success": False, "error_type": type(exc).__name__})
            finally:
                record["elapsed_seconds"] = round(time.monotonic() - started, 2)
                record['input_and_usage_audit']=input_and_usage_audit(center.deepseek.account,before_usage,before_activity,result_json)
                if record['input_and_usage_audit']['explicit_data_truncation_markers']:
                    record['formal_ai_success']=False
                    record['quality_blocker']='實際送出的資料含截斷標記，需核對缺失內容，不視為完整通過'
                record['monthly_budget']=center.deepseek.account.budget.summary()
                (case_dir / "progress.log").write_text("\n".join(messages), encoding="utf-8")
                records.append(record)
                (run_dir / "manifest.json").write_text(json.dumps(records, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            if not record.get("formal_ai_success"):
                break
    finally:
        center.database.close()
        faulthandler.cancel_dump_traceback_later()
        stack_log.close()
    print(f"驗證資料：{run_dir}；正式報告品質仍需人工檢閱，不以產檔成功代替品質驗收。")
    expected = 10 if args.all else len(args.case or ['research'])
    return 0 if len(records) == expected and all(r.get("formal_ai_success") for r in records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
