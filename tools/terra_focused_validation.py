"""Authorized two-case verification; never deploys, sends TG, or erases old charges."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timezone
from decimal import Decimal
import faulthandler
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlsplit
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
OLD = ROOT/'verification/terra-xhigh/live-20261104'
EVIDENCE = ROOT/'verification/terra-xhigh/focused-live-20261105'
ROUND = 'terra-focused-20261105-user6'
CASES = {'value_scan': '/value_scan 自訂:2330,2241 --deep --date 2026-11-04',
         'sector_strength': '/sector_strength --date 2026-11-04'}
sys.path.insert(0, str(ROOT))


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding='utf-8')


def validate_authorization(record):
    if (record.get('round_id') != ROUND or record.get('approved') is not True
            or record.get('round_limit_usd') != '6' or record.get('case_limit_usd') != '3'
            or record.get('cases') != list(CASES) or not record.get('user_confirmation')
            or record.get('model') != 'gpt-6.1-terra' or record.get('reasoning_effort') != 'xhigh'
            or any(record.get(key) is not False for key in ('deployment','telegram_send','new_paid_deepseek_fallback'))):
        raise ValueError('本輪僅授權兩案、總額6美元、單案3美元，不部署或切付費備援。')


def validate_resume_authorization(record, prior_stop_sha256):
    if (record.get('approved') is not True or record.get('round_id') != ROUND
            or record.get('round_limit_usd') != '6' or record.get('case_limit_usd') != '3'
            or record.get('cases') != list(CASES) or not record.get('user_confirmation')
            or record.get('case_models') != {'value_scan':'gpt-6.1-terra','sector_strength':'gpt-6-luna'}
            or record.get('case_background') != {'value_scan':False,'sector_strength':True}
            or record.get('reasoning_effort') != 'xhigh'
            or record.get('prior_stop_sha256') != prior_stop_sha256
            or any(record.get(key) is not False for key in ('deployment','telegram_send','new_paid_deepseek_fallback'))):
        raise ValueError('續驗必須對應原停止證據、原兩案上限與本次分流授權；不得歸零重開。')


def ledger_snapshot(ledger):
    with ledger.db() as conn:
        conn.row_factory = __import__('sqlite3').Row
        return [dict(row) for row in conn.execute('SELECT * FROM requests ORDER BY rowid')]


def check_prior_rows(rows, originals):
    current = {row['id']: row for row in rows}
    if any(current.get(row['id']) != row for row in originals):
        raise ValueError('本輪開始前的帳務紀錄被改動，先停止核對。')
    known = {row['id'] for row in originals}
    if any(row['status'] in ('pending','unknown') for row in rows if row['id'] not in known):
        raise ValueError('出現新增未結算或費用未知請求；停止，不自動重送。')


def eligible_accounts(ledger, rows, account_ids, estimate, background):
    allowed = set(account_ids)
    if ledger.purpose != 'development':
        raise ValueError('複驗只可使用開發記帳。')
    for grant in ledger.grants:
        used = sum((Decimal(row['actual'] if row['actual'] is not None else row['estimate'])
                    for row in rows if row['grant_id'] == grant['grant_id']), Decimal(0))
        # Testing must leave the full manual 20% and buffer 10% physically available.
        if used + Decimal(str(estimate)) > Decimal(grant['allowance_usd'])*Decimal('.7'):
            allowed.discard(grant['account_id'])
    return allowed


def prepare_sector_data():
    from research_center.evidence_governance import build_evidence_governance
    from research_center.market_movers_service import _annotate_cached_population
    from research_center.models import SourceItem
    latest = json.loads((OLD/'sector_strength/latest.json').read_text(encoding='utf-8'))
    original = json.loads(Path(latest['artifacts']['json_path']).read_text(encoding='utf-8'))
    package = deepcopy(original['metadata']['high_model_input_package'])
    data = deepcopy(package['command_specific_data']['payload'])
    before = deepcopy(data['market_movers'])
    data['market_movers'] = _annotate_cached_population(before)
    for key in before.keys() - {'data_quality'}:
        assert before[key] == data['market_movers'][key]
    package['command_specific_data']['payload'] = deepcopy(data)
    sources = [SourceItem(**row) for row in original['sources']]
    from datetime import date
    governance = build_evidence_governance('sector_strength', date(2026,11,4), sources, data)
    package['evidence_governance'] = governance
    for key in ('local_scoring','ai_workflow_policy','high_model_input_mode','low_model_validation'):
        data[key] = deepcopy(original['metadata'].get(key))
    data.update(high_model_input_package=package, evidence_governance=governance,
                analysis_model='gpt-6.1-terra', analysis_model_choice='deepseek', analysis_provider='openai')
    return data, sources, {'mode': 'saved_complete_packet_new_paid_analysis',
        'original_report_id': latest['artifacts']['report_id'],
        'original_report_sha256': hashlib.sha256(Path(latest['artifacts']['json_path']).read_bytes()).hexdigest(),
        'rankings_unchanged': True, 'ranking_population': data['market_movers']['data_quality']['ranking_population']}


def prepare_value_data():
    from datetime import date
    from research_center.evidence_governance import build_evidence_governance
    from research_center.models import SourceItem
    replay=json.loads((ROOT/'verification/terra-xhigh/offline-20261105/offline-replay.json').read_text(encoding='utf-8'))
    path=Path(replay['value_scan']['artifacts']['json_path'])
    if not path.resolve().is_relative_to(ROOT/'verification'):
        raise ValueError('既有資料包不在隔離驗證目錄。')
    original=json.loads(path.read_text(encoding='utf-8'))
    metadata=original['metadata']; package=deepcopy(metadata['high_model_input_package'])
    data=deepcopy(package['command_specific_data']['payload'])
    sources=[SourceItem(**row)for row in original['sources']]
    governance=build_evidence_governance('value_scan',date(2026,11,4),sources,data)
    package['evidence_governance']=governance
    # Preserve the complete candidate packet and its original notes, while
    # making the already acquired parsed official records directly readable.
    package['supplemental_official_sources']=[asdict(row)for row in sources if row.provider=='official_parsed']
    for key in ('local_scoring','ai_workflow_policy','high_model_input_mode','low_model_validation','low_model_digest'):
        data[key]=deepcopy(metadata.get(key))
    data.update(high_model_input_package=package,evidence_governance=governance,
                analysis_model='gpt-6.1-terra',analysis_model_choice='deepseek',analysis_provider='openai')
    return data,sources,{'mode':'saved_complete_packet_new_paid_analysis','original_report_id':path.stem,
        'original_report_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
        'candidate_core_unchanged':data['ai_candidate_evidence_pack']==package['command_specific_data']['payload']['ai_candidate_evidence_pack'],
        'official_records_previously_retrieved':sum(row.provider=='official_parsed'for row in sources)}


def main():
    global EVIDENCE
    parser = argparse.ArgumentParser()
    parser.add_argument('--case', choices=CASES, required=True)
    parser.add_argument('--task-root', type=Path, required=True)
    parser.add_argument('--resume-approval',type=Path)
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding='utf-8', errors='backslashreplace')
    sys.stderr.reconfigure(encoding='utf-8', errors='backslashreplace')
    work = args.task_root.resolve()
    owner = json.loads((work/'.codex-task.json').read_text(encoding='utf-8-sig'))
    if (not (ROOT/'.git').is_file() or Path(owner['project']).resolve() != ROOT
            or work != ROOT/'.codex-tmp'/owner['session']/owner['task']):
        raise ValueError('只能使用隔離分支新建的owned暫存。')
    authorization = json.loads((EVIDENCE/'authorization.json').read_text(encoding='utf-8'))
    validate_authorization(authorization)
    resume=None
    if args.resume_approval:
        approval_path=args.resume_approval.resolve()
        expected=ROOT/'verification/terra-xhigh/luna-routing-20261105/resume-approval.json'
        if approval_path!=expected or not (EVIDENCE/'stop.json').exists():
            raise ValueError('只能使用本次明確授權的續驗收據。')
        resume=json.loads(approval_path.read_text(encoding='utf-8'))
        prior_stop=EVIDENCE/'stop.json'
        validate_resume_authorization(resume,hashlib.sha256(prior_stop.read_bytes()).hexdigest())
        if Path(resume['prior_stop_path']).resolve()!=prior_stop.resolve():
            raise ValueError('停止證據路徑不符。')
        EVIDENCE=expected.parent
    if (EVIDENCE/'stop.json').exists():
        raise ValueError('本輪先前已停止；不得自動解除或換程序續付費。')
    original_rows = json.loads((EVIDENCE/'ledger-before-rows.json').read_text(encoding='utf-8'))
    os.environ.update(DEEPSEEK_BUDGET_TEST_ROOT=str(OLD/'accounting'),
        OPENAI_LIVE_ROUND_ID=ROUND, OPENAI_LIVE_ROUND_LIMIT_USD='6', OPENAI_LIVE_CASE_ID=args.case,
        OPENAI_LIVE_RESUME_SNAPSHOT='0', CODEX_TASK_TEST_ROOT=str(work/'local-store'),
        TEMP=str(work), TMP=str(work), STOCK_AI_BOT_WORKDATA_ROOT=str(work/'workdata'),
        STOCK_AI_BOT_WORKDATA_PROFILE='terra-focused', OPENBLAS_NUM_THREADS='1', OMP_NUM_THREADS='1')
    for name in ('DEEPSEEK_API_KEY','GEMINI_API_KEY','GOOGLE_API_KEY','MINIMAX_API_KEY','OPENAI_API_KEY','OPENAI_SECONDARY_API_KEY'):
        os.environ.pop(name, None)
    import httpx
    import requests
    from research_center import config as config_module, orchestrator, prompt_logging, ai_workflow_service
    from research_center.deepseek_service import development_accounting, ai_task, RUN_ID
    from research_center.ai_provider_service import create_ai_client
    from research_center.openai_credit_service import OpenAICreditLedger, OpenAIUnavailable
    from tools import terra_live_validation as previous
    config = config_module.load_research_config()
    grants = previous.live_grants()
    scopes = {'Bearer '+value: next(row for row in grants if row['account_id']==key)
              for key, value in config.openai_api_keys.items() if value and any(g['account_id']==key for g in grants)}
    if not any(row['account_id']=='secondary' for row in scopes.values()):
        raise ValueError('第二帳戶金鑰或贈額未就緒，未付費。')
    config = replace(config, openai_primary_enabled=True, deepseek_only=True, deepseek_api_key=None,
        openai_credit_grants=grants, openai_job_limit_usd=3, report_root=EVIDENCE/'reports',
        database_path=EVIDENCE/'database/research.db')
    prompt_logging.PROMPT_LOG_DIR = EVIDENCE/'prompts'
    ai_workflow_service.LOW_MODEL_ARTIFACT_DIR = EVIDENCE/'digests'
    attempt = EVIDENCE/args.case/datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    attempt.mkdir(parents=True)
    record = {'case': args.case, 'command': CASES[args.case], 'round_id': ROUND,
        'authorized_round_limit_usd':'6','authorized_case_limit_usd':'3',
        'telegram_sent':False, 'deployed':False,'manual_quality_review':'pending', 'response_scopes':[],
        'background':bool(resume and resume['case_background'][args.case])}
    def progress(message):
        line = f'[{datetime.now().isoformat(timespec="seconds")}] {message}'
        with (attempt/'progress.log').open('a', encoding='utf-8') as handle:
            handle.write(line+'\n')
        print(line, flush=True)
    def stop(reason):
        write_json(EVIDENCE/'stop.json', {'case':args.case,'reason':str(reason),
            'at':datetime.now(timezone.utc).isoformat(),'requires_review_before_new_paid_requests':True})
    original_choose = OpenAICreditLedger.choose
    def guarded_choose(ledger, account_ids, estimate=Decimal(0), **kw):
        if ledger.round_id != ROUND:
            raise ValueError('複驗不得寫入其他輪次。')
        if (EVIDENCE/'stop.json').exists():
            raise OpenAIUnavailable('本輪已停止。','focused_live_stopped')
        try:
            rows = ledger_snapshot(ledger)
            check_prior_rows(rows, original_rows)
            heartbeat = json.loads((ROOT.parents[1]/'.workdata/prod/.runtime/bot_heartbeat.json').read_text(encoding='utf-8'))
            previous.validate_production_health({'bot_pid':17944,'watchdog_pid':18044},heartbeat,
                now=datetime.now(timezone.utc),process_alive=previous.windows_process_alive)
            allowed = eligible_accounts(ledger, rows, account_ids, estimate, record['background'])
            return original_choose(ledger, allowed, estimate, **kw)
        except Exception as exc:
            # Non-reserving job preflight may use the existing exact-count path.
            if getattr(exc,'status',None)=='openai_job_budget_exceeded' and not kw.get('reserve'):
                raise
            stop(f'{type(exc).__name__}: {getattr(exc,"status","")} {exc}')
            raise OpenAIUnavailable(str(exc),'focused_live_stopped') from None
    original_send, original_request = httpx.Client.send, requests.sessions.Session.request
    def guard_url(url):
        parsed = urlsplit(str(url)); host = parsed.hostname or ''
        if host.endswith(('telegram.org','deepseek.com','minimax.io','minimaxi.com','tavily.com','generativelanguage.googleapis.com','opencode.ai')):
            raise RuntimeError('禁止正式Telegram或其他付費模型／搜尋。')
        if host=='api.openai.com':
            if parsed.scheme!='https' or parsed.path not in ('/v1/responses','/v1/responses/input_tokens'):
                raise RuntimeError('未授權的OpenAI端點。')
        elif args.case=='sector_strength':
            raise RuntimeError('類股案例沿用完整既有來源，不新增網路蒐證。')
    def send(client, request, *a, **kw):
        guard_url(request.url)
        if request.url.host=='api.openai.com':
            row = scopes.get(request.headers.get('Authorization'))
            if row is None:
                raise ValueError('無法核對請求帳戶。')
            request.headers['OpenAI-Organization']=row['organization_id']
            request.headers['OpenAI-Project']=row['key_project']
        response = original_send(client,request,*a,**kw)
        if request.url.host=='api.openai.com':
            record['response_scopes'].append({'account_id':row['account_id'],'http_status':response.status_code,
                'project':response.headers.get('openai-project'),'organization':response.headers.get('openai-organization'),
                'request_id':response.headers.get('x-request-id')})
            actual_project = response.headers.get('openai-project')
            if actual_project and actual_project!=row['key_project']:
                stop('回應Project與核對帳戶不一致；保留原回應，後續停止。')
        return response
    def request(client, method, url, *a, **kw):
        guard_url(url)
        return original_request(client,method,url,*a,**kw)
    started=time.monotonic(); center=None
    stack=(attempt/'stacks.log').open('w',encoding='utf-8')
    faulthandler.dump_traceback_later(180,repeat=True,file=stack)
    with ExitStack() as context:
        context.enter_context(development_accounting())
        context.enter_context(patch.object(config_module,'load_research_config',return_value=config))
        context.enter_context(patch.object(orchestrator,'load_research_config',return_value=config))
        context.enter_context(patch.object(OpenAICreditLedger,'choose',guarded_choose))
        context.enter_context(patch.object(httpx.Client,'send',send))
        context.enter_context(patch.object(requests.sessions.Session,'request',request))
        client=create_ai_client(config,ROOT); ledger=client.primary.ledger
        try:
            with ai_task(args.case,background=record['background'],report_date='2026-11-04'):
                record['run_id']=RUN_ID.get(); client.ensure_ready()
                if args.case=='value_scan' and not resume:
                    record['execution_mode']='native_collection_search_analysis'
                    center=orchestrator.ResearchCenter(config)
                    result=center.run_text_command(CASES[args.case],progress=progress)
                    report=result.report_json; artifacts=result.artifacts; sources=result.sources
                    markdown=result.markdown; summary=result.summary
                    record.update(status=result.status,model=result.ai_model,fallback_reason=result.fallback_reason,
                                  ai_used=result.ai_used)
                else:
                    data,sources,proof=prepare_value_data() if args.case=='value_scan' else prepare_sector_data()
                    model=client.model
                    data['analysis_model']=model
                    record.update(execution_mode=proof['mode'],replay_proof=proof)
                    from datetime import date
                    from research_center.models import CommandRequest
                    from research_center.gemini_service import build_prompt
                    from research_center.segmented_analysis_service import should_use_segmented_analysis, run_segmented_theme_analysis
                    from research_center.report_builder import write_report_artifacts
                    from research_center.prompt_logging import write_prompt_log
                    req=CommandRequest(args.case,CASES[args.case],market_scope='market' if args.case=='sector_strength' else None,
                        target='自訂:2330,2241' if args.case=='value_scan' else None,
                        candidate_pool='自訂:2330,2241' if args.case=='value_scan' else None,
                        mode='deep' if args.case=='value_scan' else 'normal',ai_model='deepseek',report_date=date(2026,11,4))
                    prompt=build_prompt(req,structured_data=data,source_list=sources)
                    write_json(attempt/'input-packet.json',data)
                    write_prompt_log(req,prompt,model,False,sources,{'purpose':'focused_saved_data_new_analysis'})
                    progress(f'完整既有{args.case}封包重新分析：model={model} prompt={len(prompt)} chars，sources={len(sources)}；無新搜尋，不冒稱重新蒐證。')
                    if should_use_segmented_analysis(req,'deepseek',prompt_chars=len(prompt)):
                        generated=run_segmented_theme_analysis(request=req,structured_data=data,sources=sources,
                            ai_client=client,model_name=model,original_prompt_chars=len(prompt),progress=progress)
                        markdown=generated.markdown
                        data.update(segmented_ai_analysis=generated.diagnostics,segmented_ai_prompt_paths=generated.prompt_paths)
                        data['ai_status']='ai_success' if all(row.status=='success' for row in generated.segment_runs) else 'partial_success'
                    else:
                        generated=client.with_timeout(900).generate_report(prompt); markdown=generated.markdown
                    summary=orchestrator.summarize_for_telegram(markdown)
                    artifacts,report=write_report_artifacts(config.report_root,req,markdown,summary,sources,True,None,data)
                    record.update(status='success',model=model,fallback_reason=None,ai_used=True)
                metadata=report.get('metadata') or {}
                record.update(artifacts=asdict(artifacts),source_count=len(sources),
                    formal_ai_success=record['ai_used'] and not record['fallback_reason']
                        and metadata.get('ai_status')=='ai_success' and metadata.get('qa_validation',{}).get('passed') is True,
                    quality_blockers=previous.report_quality_blockers(metadata,sources))
                write_json(attempt/'result.json',report)
                (attempt/'output.md').write_text(markdown,encoding='utf-8')
                from stock_ai_bot.telegram.telegram_stock_formatting import prepare_telegram_chunks
                chunks=prepare_telegram_chunks(summary)
                record['telegram_utf16_lengths']=[len(chunk.text.encode('utf-16-le'))//2 for chunk in chunks]
                (attempt/'telegram-preview.md').write_text(summary,encoding='utf-8')
        except Exception as exc:
            record.update(status='blocked' if getattr(exc,'status',None) else 'failed',formal_ai_success=False,
                error_type=type(exc).__name__,error_status=getattr(exc,'status',''),
                error_message=re.sub(r'sk-[\w-]+','[REDACTED]',str(exc)))
            progress(f'案例停止：{record["error_type"]} {record["error_status"]}，不加額或切付費備援。')
        finally:
            if center is not None: center.database.close()
            faulthandler.cancel_dump_traceback_later(); stack.close()
            record['elapsed_seconds']=round(time.monotonic()-started,3)
            with patch.object(previous,'ROUND',ROUND):
                record['cost_and_input_audit']=previous.audit(ledger,args.case)
            record['cost_and_input_audit']['price_basis']='Official response usage; each requested Terra/Luna model uses its own cache, long-context and output rates; web-search tool fees separate. Not a final invoice.'
            if record['cost_and_input_audit']['unknown_or_pending']:
                stop('本輪有新增未結算或費用未知請求。')
            write_json(attempt/'verification.json',record)
            write_json(EVIDENCE/args.case/'latest.json',{'attempt':str(attempt),**record})
            progress('費用：'+json.dumps({k:record['cost_and_input_audit'][k] for k in
                ('case_actual_usd','round_actual_usd','round_used_or_reserved_usd','unknown_or_pending')},ensure_ascii=False))
    return 0 if record.get('formal_ai_success') and not record.get('quality_blockers') else 1


if __name__=='__main__':
    raise SystemExit(main())
