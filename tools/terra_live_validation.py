"""Explicit Terra-only paid verification. Never starts the Bot or sends Telegram."""
from __future__ import annotations

import argparse
import ast
from collections import Counter
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
from unittest.mock import patch
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
EVIDENCE = ROOT / 'verification/terra-xhigh/live-20261104'
ROUND = 'terra-xhigh-20261104-user25'
CASES = {
    'research': '/research 2330 --deep --date 2026-11-04',
    'value_scan': '/value_scan 自訂:2330,2241 --deep --date 2026-11-04',
    'macro': '/macro 台股 --date 2026-11-04',
    'theme': '/theme AI電源 --date 2026-11-04',
    'theme_flow': '/theme_flow AI電源 --date 2026-11-04',
    'theme_radar': '/theme_radar --date 2026-11-04',
    'sector_strength': '/sector_strength --date 2026-11-04',
    'radar': '/radar --source combined --ai-top 15 --date 2026-11-04',
    'news': '/news refresh',
    'topic_maintain': '/topic_maintain --deep',
}


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding='utf-8')


def live_grants(evidence=EVIDENCE):
    grants = [json.loads((evidence/'grant-verification.json').read_text(encoding='utf-8'))]
    secondary = evidence/'secondary-grant-verification.json'
    if secondary.exists():
        row = json.loads(secondary.read_text(encoding='utf-8'))
        if (row.get('account_id') != 'secondary' or not row.get('key_project')
                or row.get('verification_basis') != 'user_supplied_billing_screenshot_and_ids'):
            raise ValueError('第二帳戶必須有可追查的贈額、組織及專案核對收據。')
        grants.append(row)
    return tuple(grants)


def assert_no_operator_pause(ledger):
    with ledger.db() as conn:
        blocks = conn.execute('SELECT reason FROM account_blocks').fetchall()
    if any(str(reason).startswith(('operator_', 'user_pause_')) for (reason,) in blocks):
        from research_center.openai_credit_service import OpenAIUnavailable
        raise OpenAIUnavailable('本輪人工暫停仍有效，不以新增帳戶繞過。', 'authorized_live_operator_pause')


def authorized_unknown_reservations(ledger, evidence=EVIDENCE):
    path = evidence/'crash-reserved-unknown-approval.json'
    if not path.exists():
        return ()
    approval = json.loads(path.read_text(encoding='utf-8'))
    if (ledger.purpose != 'development' or ledger.round_id != ROUND
            or approval.get('round_id') != ROUND or approval.get('round_limit_usd') != '25'
            or approval.get('approved') is not True or not approval.get('user_confirmation')
            or approval.get('question_id') != 'call_f00523a211eb41f68c5a8f2cfd2119f3'):
        raise ValueError('未知費用續跑必須保留本輪使用者明確授權，不擴大至正式帳本。')
    request_id = approval['request_id']
    with ledger.db() as conn:
        row = conn.execute('SELECT status,estimate,actual,case_id FROM requests WHERE id=? AND round_id=?',
                           (request_id, ROUND)).fetchone()
    if row != ('unknown', approval['reserved_unknown_usd'], None, 'topic_maintain'):
        raise ValueError('原未知請求或完整預留已改變，停止續跑並重新核對。')
    return (request_id,)


def authorized_case_limit(case, evidence=EVIDENCE):
    exceptions = {'radar': '7', 'news': '6', 'topic_maintain': '7'}
    if case not in exceptions:
        return 5
    expected = exceptions[case]
    path = evidence/(case+'-limit-approval.json')
    if not path.exists():
        return 5
    approval = json.loads(path.read_text(encoding='utf-8'))
    if (approval.get('round_id') != ROUND or approval.get('case') != case
            or approval.get('case_limit_usd') != expected or approval.get('round_limit_usd') != '25'
            or approval.get('approved') is not True or not approval.get('user_confirmation')):
        raise ValueError('案例例外須有本輪明確授權收據，不提高其他案例或整輪上限。')
    return int(expected)


def latch_live_case_cap(ledger, grant, case, error, evidence=EVIDENCE):
    if ledger.round_id != ROUND or ledger.purpose != 'development' or error.status != 'openai_job_budget_exceeded':
        return False
    for registered in ledger.grants:
        ledger.block(registered, 'operator_stop_authorized_live_guard')
    receipt = {'case': case, 'round_id': ROUND, 'reason': error.status,
               'case_limit_usd': str(ledger.job_limit), 'round_limit_usd': str(ledger.round_limit),
               'scope': 'Only this isolated ledger; no production change or fee reset.'}
    write_json(evidence/('paid-guard-stop-'+case+'-'+datetime.now().strftime('%Y%m%d_%H%M%S_%f')+'.json'), receipt)
    return True


def restore_radar_candidates(rows):
    from stock_ai_bot.monitoring import radar_service as radar
    result = []
    for row in rows:
        codes = row.get('strategy_codes', [])
        if isinstance(codes, str):
            codes = ast.literal_eval(codes)
        if not isinstance(codes, (list, tuple, set)) or any(not isinstance(code, str) for code in codes):
            raise ValueError('Radar 策略集合快照型別無效，不猜測或丟棄核心欄位。')
        result.append(radar.RadarCandidate(**{**row, 'strategy_codes': set(codes)}))
    return result


def resume_radar_checkpoint(path, progress, evidence=EVIDENCE):
    from datetime import date
    from stock_ai_bot.monitoring import radar_service as radar
    path = path.resolve()
    if not path.is_relative_to((evidence/'radar').resolve()) or path.name != 'result.json':
        raise ValueError('只能續跑本輪 Radar 真實產物，不讀其他專案或日期。')
    raw = path.read_bytes()
    data = json.loads(raw)
    if data.get('report_date') != '2026-11-04':
        raise ValueError('Radar 快照日期不符。')
    request = radar.RadarRequest(**{**data['request'], 'report_date': date(2026, 11, 4)})
    candidates = restore_radar_candidates(data['candidates'])
    codes = data['ai_enriched_codes']
    if (request.source != 'combined' or request.ai_top != 15 or len(codes) != 15
            or len(set(codes)) != 15 or not set(codes) <= {item.code for item in candidates}):
        raise ValueError('Radar 快照的候選與十五檔分析名單不符。')
    proof = {'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest(),
             'candidate_count': len(candidates), 'selected_codes': codes,
             'mode': 'resume_real_collection_checkpoint', 'new_full_market_collection': False,
             'scoring_rules_changed': False}
    for candidate in candidates:
        if candidate.code in codes:
            candidate.ai_comment = {}
    progress(f'Radar：從本輪真實快照續跑 {len(candidates)} 檔候選及 Top15；不是重新全市場抓取。')
    radar._attach_research_center_sources(candidates, codes, request.report_date, progress)
    radar._ensure_radar_source_sufficiency(candidates, codes, request.report_date, progress)
    radar._attach_research_evidence_packs(candidates, codes, request.report_date, progress)
    analysis = radar._attach_ai_comments(candidates, codes, request.model, request.report_date, progress)
    radar._score_candidates(candidates, request.report_date, scoring_version=request.scoring_version)
    radar._attach_base_evidence_packs(candidates, request.report_date)
    candidates.sort(key=lambda item: (item.total_score, len(item.strategy_codes), item.code), reverse=True)
    result = radar.RadarResult(request, request.report_date, candidates, codes,
        {**data['diagnostics'], 'ai_analysis': analysis, 'checkpoint_resume': proof,
         'evidence_pack_status': radar._radar_evidence_pack_status(candidates, codes)})
    radar.save_radar_result(result)
    if hashlib.sha256(path.read_bytes()).hexdigest() != proof['sha256']:
        raise ValueError('原始 Radar 快照在續跑期間改變，不能宣稱穩定輸入。')
    return result, proof


def validate_production_health(restored, heartbeat, *, now, process_alive):
    updated = datetime.fromisoformat(heartbeat['updated_at'])
    age = (now - updated).total_seconds()
    if heartbeat.get('pid') != restored.get('bot_pid') or not 0 <= age <= 180:
        raise ValueError('正式 Bot 身分或心跳時效改變，先停止隔離付費測試。')
    if not heartbeat.get('job_queue_available') or (heartbeat.get('schedule_health') or {}).get('schedule_unhealthy_reason'):
        raise ValueError('正式 Bot 佇列狀態異常，先停止隔離付費測試。')
    if not all(process_alive(int(restored[key])) for key in ('bot_pid', 'watchdog_pid')):
        raise ValueError('正式 Bot 或監護程序已停止，先停止隔離付費測試。')


def windows_process_alive(pid):
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetExitCodeProcess.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x1000, False, pid)  # Query only; never send a signal.
    if not handle:
        return False
    try:
        status = wintypes.DWORD()
        return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(status))) and status.value == 259
    finally:
        kernel.CloseHandle(handle)


def report_quality_blockers(metadata, sources):
    blockers = []
    if not sources:
        blockers.append('正式模型沒有收到任何可追溯外部來源；格式通過不代表內容達標。')
    package = metadata.get('high_model_input_package') or {}
    audit = (package.get('command_specific_data') or {}).get('core_input_audit') or {}
    for row in audit.get('sections') or []:
        if row.get('status') in {'appendix_only', 'compression_error'} and row.get('raw_count', 0):
            blockers.append('已有核心資料未完整入模：'+str(row.get('section')))
    return blockers


def audit(ledger, case):
    with ledger.db() as conn:
        rows = conn.execute('SELECT id,status,estimate,actual,detail,operation FROM requests WHERE round_id=? AND case_id=?',
                            (ROUND, case)).fetchall()
        all_rows = conn.execute('SELECT status,estimate,actual FROM requests WHERE round_id=?', (ROUND,)).fetchall()
    actual = sum((Decimal(a) for _, _, _, a, _, _ in rows if a is not None), Decimal(0))
    details = [json.loads(row[4]) for row in rows]
    sent_inputs = []
    for row, detail in zip(rows, details):
        if row[1] not in ('success', 'incomplete'):
            continue
        path = ledger.artifact_root/'requests'/(row[0]+'-request.json')
        if not path.exists():
            continue
        raw = path.read_bytes()
        body = json.loads(raw)['body']
        sent_inputs.append({'request_id': row[0], 'run_id': detail.get('run_id'), 'operation': row[5],
            'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest(),
            'input_chars': len(body.get('input') or ''), 'model': body.get('model'),
            'reasoning_effort': (body.get('reasoning') or {}).get('effort'),
            'truncation': body.get('truncation'), 'store': body.get('store')})
    return {'case_actual_usd': str(actual),
        'round_actual_usd': str(sum((Decimal(a) for _, _, a in all_rows if a is not None), Decimal(0))),
        'round_used_or_reserved_usd': str(sum((Decimal(a if a is not None else e) for _, e, a in all_rows), Decimal(0))),
        'unknown_or_pending': sum(s in ('unknown', 'pending') for s, _, _ in all_rows),
        'requests': [{'id': r[0], 'status': r[1], 'reserve_usd': r[2], 'actual_usd': r[3],
                      'operation': r[5], 'detail': d} for r, d in zip(rows, details)],
        'statuses': dict(Counter(r[1] for r in rows)),
        'actual_models': sorted({d['actual_model'] for d in details if d.get('actual_model')}),
        'input_tokens': sum(int((d.get('usage') or {}).get('input_tokens', 0)) for d in details),
        'output_tokens_including_reasoning': sum(int((d.get('usage') or {}).get('output_tokens', 0)) for d in details),
        'search_calls': sum(int(d.get('search_calls', 0)) for d in details),
        'successful_request_inputs': sent_inputs,
        'price_basis': 'Official response usage, standard Terra 30m cache writes and long-context rates; not a final invoice.'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--case', choices=['smoke', *CASES], required=True)
    parser.add_argument('--task-root', type=Path, required=True)
    parser.add_argument('--radar-checkpoint', type=Path)
    parser.add_argument('--account-only', choices=['secondary'])
    parser.add_argument('--smoke-search', action='store_true')
    args = parser.parse_args()
    if args.radar_checkpoint and args.case != 'radar':
        parser.error('--radar-checkpoint 僅限本輪 Radar 斷點續跑。')
    if (args.account_only or args.smoke_search) and args.case != 'smoke':
        parser.error('指定帳戶與搜尋連線驗證僅限 smoke 案例，不改正式十入口的路由。')
    sys.stdout.reconfigure(encoding='utf-8', errors='backslashreplace')
    sys.stderr.reconfigure(encoding='utf-8', errors='backslashreplace')
    work = args.task_root.resolve()
    owner = json.loads((work/'.codex-task.json').read_text(encoding='utf-8-sig'))
    if Path(owner['project']).resolve() != ROOT or work != ROOT/'.codex-tmp'/owner['session']/owner['task']:
        raise ValueError('Live 暫存必須為本專案新建 owned 目錄。')
    if not (ROOT/'.git').is_file():
        raise ValueError('只允許隔離 worktree 執行，不可在正式 Bot 專案執行。')
    os.environ.update(DEEPSEEK_BUDGET_TEST_ROOT=str(EVIDENCE/'accounting'),
        OPENAI_LIVE_ROUND_ID=ROUND, OPENAI_LIVE_ROUND_LIMIT_USD='25', OPENAI_LIVE_CASE_ID=args.case,
        OPENAI_LIVE_RESUME_SNAPSHOT='1',
        TEMP=str(work), TMP=str(work), STOCK_AI_BOT_WORKDATA_ROOT=str(work/'workdata'),
        STOCK_AI_BOT_WORKDATA_PROFILE='terra-live', OPENBLAS_NUM_THREADS='1', OMP_NUM_THREADS='1')
    for name in ['DEEPSEEK_API_KEY', 'GEMINI_API_KEY', 'GOOGLE_API_KEY', 'MINIMAX_API_KEY', 'OPENAI_API_KEY', 'OPENAI_SECONDARY_API_KEY']:
        os.environ.pop(name, None)
    import httpx
    import requests
    original_send = httpx.Client.send
    original_request = requests.sessions.Session.request
    scopes = {}
    response_scopes = []
    def guard(url):
        parsed = urlsplit(str(url))
        host = parsed.hostname or ''
        if host.endswith(('telegram.org', 'deepseek.com', 'minimax.io', 'minimaxi.com', 'tavily.com', 'generativelanguage.googleapis.com', 'opencode.ai')):
            raise RuntimeError('Live 測試禁止 Telegram、其他模型或付費搜尋備援。')
        if host == 'api.openai.com' and (parsed.scheme != 'https' or parsed.path not in ('/v1/responses', '/v1/responses/input_tokens')):
            raise RuntimeError('本輪僅授權 OpenAI Responses 分析與搜尋。')
    def guarded_send(self, request, *a, **kw):
        guard(request.url)
        if request.url.host == 'api.openai.com':
            scope = scopes.get(request.headers.get('Authorization'))
            if scope:
                request.headers['OpenAI-Organization'] = scope['organization_id']
                request.headers['OpenAI-Project'] = scope['key_project']
        response = original_send(self, request, *a, **kw)
        if request.url.host == 'api.openai.com':
            response_scopes.append({'http_status': response.status_code,
                'organization': response.headers.get('openai-organization'),
                'project': response.headers.get('openai-project'),
                'request_id': response.headers.get('x-request-id')})
        return response
    def guarded_request(self, method, url, *a, **kw):
        guard(url)
        return original_request(self, method, url, *a, **kw)
    httpx.Client.send = guarded_send
    requests.sessions.Session.request = guarded_request
    from research_center import config as config_module, orchestrator, prompt_logging, ai_workflow_service
    from research_center.deepseek_service import development_accounting, ai_task, RUN_ID
    from research_center.ai_provider_service import create_ai_client
    from research_center.openai_credit_service import OpenAICreditLedger, OpenAIUnavailable
    from stock_ai_bot.monitoring import radar_service
    config = config_module.load_research_config()
    grants = live_grants()
    grant = grants[0]
    if not config.openai_api_keys.get('primary') or not config.openai_api_keys['primary'].endswith('LA4A'):
        raise ValueError('設定金鑰與已核對的 Stock Bot 金鑰不符；未送出請求。')
    case_limit = authorized_case_limit(args.case)
    keys = dict(config.openai_api_keys)
    for row in grants:
        key = keys.get(row['account_id'])
        if not key:
            raise ValueError('已登記的驗證帳戶缺少金鑰；未送出請求。')
        scopes['Bearer ' + key] = row
    if args.account_only:
        if not any(row['account_id'] == args.account_only for row in grants):
            raise ValueError('指定的第二帳戶尚未核對贈額。')
        keys = {args.account_only: keys[args.account_only]}
    config = replace(config, openai_primary_enabled=True, deepseek_only=True, deepseek_api_key=None,
        openai_credit_grants=grants, openai_api_keys=keys, openai_job_limit_usd=case_limit,
        report_root=EVIDENCE/'reports', database_path=EVIDENCE/'database/research.db')
    prompt_logging.PROMPT_LOG_DIR = EVIDENCE/'prompts'
    ai_workflow_service.LOW_MODEL_ARTIFACT_DIR = EVIDENCE/'digests'
    case_dir = EVIDENCE/args.case
    attempt = case_dir/datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    attempt.mkdir(parents=True)
    messages = []
    def progress(message):
        line = f'[{datetime.now().isoformat(timespec="seconds")}] {message}'
        messages.append(line)
        with (attempt/'progress.log').open('a', encoding='utf-8') as handle:
            handle.write(line+'\n')
        print(line, flush=True)
    record = {'case': args.case, 'requested_model': 'gpt-6.1-terra', 'reasoning_effort': 'xhigh',
              'authorized_case_limit_usd': str(case_limit), 'authorized_round_limit_usd': '25',
              'telegram_sent': False, 'topic_applied': False, 'manual_quality_review': 'pending',
              'account_only': args.account_only, 'response_scopes': response_scopes,
              'date_policy': '2026-11-04 analysis cutoff; pre-close price data must retain its actual date.'}
    started = time.monotonic()
    center = None
    original_choose = OpenAICreditLedger.choose
    def guarded_choose(ledger, *a, **kw):
        if ledger.round_id == ROUND:
            assert_no_operator_pause(ledger)
            kw['acknowledged_unknown_ids'] = authorized_unknown_reservations(ledger)
            try:
                health_receipt = EVIDENCE/'production-current.json'
                if not health_receipt.exists():
                    health_receipt = EVIDENCE/'production-restore.json'
                restored = json.loads(health_receipt.read_text(encoding='utf-8'))
                heartbeat = json.loads((ROOT.parent.parent/'.workdata/prod/.runtime/bot_heartbeat.json').read_text(encoding='utf-8'))
                validate_production_health(restored, heartbeat, now=datetime.now(timezone.utc),
                                           process_alive=windows_process_alive)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise OpenAIUnavailable(str(exc), 'production_health_unavailable') from None
        try:
            return original_choose(ledger, *a, **kw)
        except OpenAIUnavailable as exc:
            if kw.get('reserve') and latch_live_case_cap(ledger, grant, args.case, exc):
                raise OpenAIUnavailable('本輪已停在單案費用預留護欄，未送出新請求；不是官方餘額用完。',
                                        'authorized_live_cap_stop') from exc
            raise
    stack = (attempt/'stacks.log').open('w', encoding='utf-8')
    faulthandler.dump_traceback_later(180, repeat=True, file=stack)
    with development_accounting(), patch.object(config_module, 'load_research_config', return_value=config), \
            patch.object(orchestrator, 'load_research_config', return_value=config), \
            patch.object(radar_service, 'load_research_config', return_value=config), \
            patch.object(OpenAICreditLedger, 'choose', guarded_choose):
        client = create_ai_client(config, ROOT)
        ledger = client.primary.ledger
        try:
            with ai_task(args.case, report_date='2026-11-04'):
                record['run_id'] = RUN_ID.get()
                client.ensure_ready()
                if args.case == 'smoke':
                    progress('Terra xhigh JSON 相容性實測開始（不是正式報告）。')
                    result = client.with_output_limit(2048).generate_json(
                        f'本次連線驗證批號：{attempt.name}。只輸出 JSON：status="ok"，message="繁體中文測試通過"；不要提供投資結論。')
                    write_json(attempt/'result.json', result.raw)
                    if args.smoke_search:
                        progress('第二帳戶真實搜尋相容性驗證；不是正式報告。')
                        searched = client.primary.with_output_limit(4096).search(
                            'site:pr.tsmc.com 台積電 2026 第三季 財務 業績 10月15日', report_date='2026-11-04')
                        write_json(attempt/'search-result.json', searched)
                        if not searched.get('results') or (searched.get('diagnostics') or {}).get('cache_hit'):
                            raise ValueError('真實搜尋未取得引用，不視為搜尋相容性通過。')
                        record['smoke_search_sources'] = len(searched['results'])
                    record.update(status='success', formal_ai_success=False, smoke_success=True,
                                  model=result.diagnostics.get('actual_model'))
                elif args.case == 'radar':
                    if args.radar_checkpoint:
                        result, proof = resume_radar_checkpoint(args.radar_checkpoint, progress)
                        record['checkpoint_resume'] = proof
                    else:
                        result = radar_service.run_radar(CASES[args.case].split()[1:], progress=progress)
                    text = radar_service.format_radar_report(result, limit=50)
                    selected = [c for c in result.candidates if c.code in result.ai_enriched_codes]
                    formal = len(selected) == 15 and all(c.ai_comment.get('status') == 'ok' for c in selected)
                    write_json(attempt/'result.json', radar_service._json_safe(asdict(result)))
                    (attempt/'output.md').write_text(text, encoding='utf-8')
                    record.update(status='success' if formal else 'partial_success', formal_ai_success=formal,
                                  selected_count=len(selected), command=CASES[args.case])
                else:
                    center = orchestrator.ResearchCenter(config)
                    result = center.run_text_command(CASES[args.case], progress=progress)
                    write_json(attempt/'result.json', result.report_json)
                    (attempt/'output.md').write_text(result.markdown or result.summary, encoding='utf-8')
                    (attempt/'telegram-preview.md').write_text(result.summary or '', encoding='utf-8')
                    metadata = result.report_json.get('metadata') or {}
                    formal = result.status == 'success' and result.ai_used and not result.fallback_reason
                    if args.case not in ('news', 'topic_maintain'):
                        formal = formal and metadata.get('ai_status', 'ai_success') == 'ai_success' and metadata.get('qa_validation', {}).get('passed') is True
                    record.update(status=result.status, formal_ai_success=formal, fallback_reason=result.fallback_reason,
                        model=result.ai_model, artifacts=asdict(result.artifacts), source_count=len(result.sources),
                        command=CASES[args.case])
                    if args.case not in ('news', 'topic_maintain'):
                        record['quality_blockers'] = report_quality_blockers(metadata, result.sources)
                preview = attempt/'telegram-preview.md'
                if args.case == 'radar':
                    preview.write_text(text, encoding='utf-8')
                if preview.exists():
                    from stock_ai_bot.telegram.telegram_stock_formatting import prepare_telegram_chunks
                    chunks = prepare_telegram_chunks(preview.read_text(encoding='utf-8'))
                    record['telegram_chunk_lengths'] = [len(chunk.text.encode('utf-16-le'))//2 for chunk in chunks]
                    record['telegram_lengths_safe'] = all(n <= 4096 for n in record['telegram_chunk_lengths'])
        except Exception as exc:
            record.update(status='blocked' if getattr(exc, 'status', None) else 'failed', formal_ai_success=False,
                error_type=type(exc).__name__, error_status=getattr(exc, 'status', ''),
                error_message=re.sub(r'sk-[\w-]+', '[REDACTED]', str(exc)))
            progress(f'案例已停止：{record["error_type"]} {record["error_status"]}；不切付費備援。')
        finally:
            if center is not None:
                center.database.close()
            faulthandler.cancel_dump_traceback_later()
            stack.close()
            record['elapsed_seconds'] = round(time.monotonic()-started, 3)
            record['cost_and_input_audit'] = audit(ledger, args.case)
            write_json(attempt/'verification.json', record)
            write_json(case_dir/'latest.json', {'attempt': str(attempt), **record})
            progress('已保存產出與費用核對：'+json.dumps({k: record['cost_and_input_audit'][k]
                for k in ['case_actual_usd', 'round_actual_usd', 'unknown_or_pending']}, ensure_ascii=False))
    return 0 if (record.get('formal_ai_success') and not record.get('quality_blockers')) or record.get('smoke_success') else 1


if __name__ == '__main__':
    raise SystemExit(main())
