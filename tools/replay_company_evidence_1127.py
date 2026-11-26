"""Replay the retained fifteen-stock round without network or production writes."""
from pathlib import Path
import argparse
import hashlib
import json
import os
import sys
from dataclasses import fields
from datetime import date

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--task-root', type=Path, required=True)
    parser.add_argument('--result-root', type=Path, required=True)
    args = parser.parse_args()
    owned = args.task_root.resolve()
    marker = json.loads((owned/'.codex-task.json').read_text(encoding='utf-8-sig'))
    assert Path(marker['project']).resolve() == ROOT and owned == ROOT/'.codex-tmp'/marker['session']/marker['task']
    out = args.result_root.resolve()
    assert out.is_relative_to(ROOT/'verification')
    out.mkdir(parents=True, exist_ok=True)
    os.chdir(owned)
    os.environ.update(STOCK_AI_BOT_WORKDATA_ROOT=str(owned/'replay-workdata'),
                      DEEPSEEK_BUDGET_TEST_ROOT=str(owned/'replay-budget'))
    sys.path.insert(0, str(ROOT))
    import httpx
    import requests
    import urllib.request
    def deny(*a, **k):
        raise AssertionError('Offline evidence replay forbids all HTTP.')
    httpx.Client.send = httpx.AsyncClient.send = deny
    requests.sessions.Session.request = deny
    urllib.request.urlopen = deny
    from research_center.evidence_governance import company_identity_context, company_source_admission, company_safe_prompt_data
    from stock_ai_bot.monitoring import radar_service as radar
    from research_center.models import CommandRequest, SourceItem
    from research_center.ai_workflow_service import build_high_model_input_package
    source = ROOT/'verification/schedule-closure-20261126/live/resume-five/full-result.json'
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    raw = json.loads(source.read_text(encoding='utf-8'))
    names = {f.name for f in fields(radar.RadarCandidate)}
    day = date(2026, 11, 26)
    rows, wrong_news = [], None
    for original in raw['candidates']:
        if original['code'] not in raw['ai_enriched_codes']:
            continue
        item = radar.RadarCandidate(**{**{k:v for k,v in original.items() if k in names},
                                       'strategy_codes':set(original.get('strategy_codes', []))})
        snapshot = radar._json_safe(__import__('dataclasses').asdict(item))
        payload = radar._build_ai_comment_payload(item, day)
        assert radar._json_safe(__import__('dataclasses').asdict(item)) == snapshot
        official = payload['官方證據狀態']
        records = radar._candidate_official_records(item, day)
        assert official['同公司且日期合格的官方正文已取得'] is False
        assert all(not row['official_document_body_retrieved'] and not row['published_date']
                   for row in records if row['official_evidence_status']=='reference_only')
        structured = (item.evidence_pack or {}).get('research_structured_data') or {}
        from research_center.mops_sources import financial_prompt_context
        assert payload['ai_compact_pack']['payload']['research_summary']['financial_data'] == financial_prompt_context(structured.get('financial_data') or [], day)['rows']
        assert payload['財報實際供應商'] == sorted({str(r['Financial_Data_Source']) for r in structured.get('financial_data') or [] if r.get('Financial_Data_Source')})
        gates = [company_source_admission(n, company_identity_context({'stock': {'code':item.code,'name':item.name}})) for n in item.news_items]
        if item.code == '3703':
            wrong_news = next(n for n in item.news_items if '合勤控（3703）' in str(n.get('summary')))
            assert any(g['status']=='quarantined' for g in gates)
            assert '新增無人機' not in json.dumps(payload, ensure_ascii=False)
            assert '無人機通訊模組' not in json.dumps(payload, ensure_ascii=False)
            assert wrong_news['summary'] not in json.dumps(payload, ensure_ascii=False)
            assert wrong_news['title'] not in payload['local_news']
        rows.append({'code':item.code,'name':item.name,'raw_candidate_unchanged':True,
            'financial_rows_retained':len(structured.get('financial_data') or []),
            'actual_financial_providers':payload['財報實際供應商'],
            'news_admissions':[g['status'] for g in gates], 'official_body_not_fabricated':True,
            'official_source_records':records})
    assert len(rows)==15 and wrong_news
    shared = {}
    for command in ['research','value_scan','macro','theme','theme_flow','theme_radar','sector_strength','radar','news','topic_maintain']:
        data={'stock':{'code':'3703','name':'欣陸'}, 'news_context':{'local_news':[wrong_news]}}
        sources=[SourceItem('Sbad',wrong_news['title'],wrong_news['url'],'Level 2',snippet=wrong_news['summary'])]
        result=build_high_model_input_package(CommandRequest(command,'/'+command,report_date=day),data,sources,prompt_chars_estimate=1000)
        assert wrong_news['summary'] not in json.dumps(result,ensure_ascii=False)
        assert data['news_context']['local_news'][0] == wrong_news
        shared[command]='actual_retained_wrong_source_isolated_original_unchanged'
    assert before==hashlib.sha256(source.read_bytes()).hexdigest()
    result={'status':'passed','mode':'saved_real_data_offline_not_new_search_or_ai',
        'source':str(source),'source_sha256':before,'raw_source_unchanged':True,
        'candidates':rows,'shared_ten_command_package_checks':shared,
        'paid_requests':0,'telegram_messages':0,'production_writes':0,
        'limitation':'No new official body was obtained; missing evidence remains missing.'}
    (out/'replay.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'status':result['status'],'candidates':len(rows),'commands':len(shared),'paid_requests':0},ensure_ascii=False))


if __name__ == '__main__':
    main()
