from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace

import pytest

from tools.terra_focused_validation import CASES, ROUND, check_prior_rows, eligible_accounts, validate_authorization


def authorization():
    return {'round_id':ROUND,'approved':True,'round_limit_usd':'6','case_limit_usd':'3',
        'cases':list(CASES),'user_confirmation':'Explicit approval',
        'model':'gpt-6.1-terra','reasoning_effort':'xhigh',
        'deployment':False,'telegram_send':False,'new_paid_deepseek_fallback':False}


def test_fixed_two_case_authorization():
    validate_authorization(authorization())


@pytest.mark.parametrize('change',[{}, {'round_limit_usd':'7'}, {'case_limit_usd':'4'},
    {'case_models':{'value_scan':'gpt-6-luna','sector_strength':'gpt-6-luna'}},
    {'case_background':{'value_scan':True,'sector_strength':True}},
    {'prior_stop_sha256':'other'}, {'deployment':True}, {'new_paid_deepseek_fallback':True}])
def test_resume_keeps_original_caps_stop_evidence_and_role_models(change):
    from tools.terra_focused_validation import validate_resume_authorization
    record={**authorization(),'prior_stop_sha256':'known-stop',
        'case_models':{'value_scan':'gpt-6.1-terra','sector_strength':'gpt-6-luna'},
        'case_background':{'value_scan':False,'sector_strength':True}}
    if change:
        with pytest.raises(ValueError):validate_resume_authorization({**record,**change},'known-stop')
    else: validate_resume_authorization(record,'known-stop')


def test_saved_value_packet_preserves_core_and_already_retrieved_official_records():
    from tools.terra_focused_validation import prepare_value_data
    data,sources,proof=prepare_value_data()
    assert proof['candidate_core_unchanged'] is True
    assert proof['official_records_previously_retrieved']==2
    assert len(data['ai_candidate_evidence_pack'])==2
    supplied=data['high_model_input_package']['supplemental_official_sources']
    assert {row['source_id']for row in supplied}<={row.source_id for row in sources}
    assert all(row['published_date'] is None and row['data_date'] is None for row in supplied)


@pytest.mark.parametrize('key,value', [('round_limit_usd','7'),('case_limit_usd','5'),
    ('cases',['research']),('model','other'),('reasoning_effort','high'),
    ('deployment',True),('telegram_send',True),('new_paid_deepseek_fallback',True),('approved',False)])
def test_expanded_authorization_rejected(key,value):
    record=authorization(); record[key]=value
    with pytest.raises(ValueError): validate_authorization(record)


def test_original_unknown_is_preserved_but_new_unknown_stops():
    originals=[{'id':'prior','status':'unknown','actual':None,'estimate':'2.79884'}]
    current=deepcopy(originals)+[{'id':'new','status':'success','actual':'.12','estimate':'1'}]
    check_prior_rows(current,originals)
    current[-1].update(status='unknown',actual=None)
    with pytest.raises(ValueError,match='費用未知'): check_prior_rows(current,originals)
    current=deepcopy(originals);current[0]['actual']='0'
    with pytest.raises(ValueError,match='帳務紀錄被改動'): check_prior_rows(current,originals)


def test_development_budget_leaves_full_manual_and_buffer():
    ledger=SimpleNamespace(purpose='development',grants=[{'account_id':'secondary','grant_id':'s','allowance_usd':'22'},
                                                     {'account_id':'primary','grant_id':'p','allowance_usd':'58.79'}])
    rows=[{'grant_id':'s','estimate':'10','actual':'8.40790860'}]
    assert eligible_accounts(ledger,rows,{'primary','secondary'},Decimal('6'),False)=={'primary','secondary'}
    assert eligible_accounts(ledger,rows,{'primary','secondary'},Decimal('7'),False)=={'primary'}
    ledger.purpose='production'
    with pytest.raises(ValueError): eligible_accounts(ledger,rows,{'secondary'},0,False)


@pytest.mark.parametrize('gap_fill',[False,True])
def test_report_discovery_stop_retains_partial_sources_without_retry(gap_fill):
    from datetime import date
    from unittest.mock import Mock
    from research_center.deepseek_service import DeepSeekUnavailable
    from research_center.models import CommandRequest,SourceItem
    from research_center.orchestrator import _GeminiDiscoveryRunner
    source=SourceItem('S009','公司公告','https://pr.tsmc.com/announcement','Level 1',
        published_date='2026-11-04',date_status='explicit',snippet='公司公告正文資料。',document_kind='document_body')
    error=DeepSeekUnavailable('費用護欄停止','job_budget_exceeded')
    error.partial_discovery_sources=[source]
    error.partial_discovery_diagnostics={'provider':'openai_router_search','query_count':1,'source_count':1}
    service=SimpleNamespace(discover=Mock(side_effect=error),provider='openai_router_search')
    runner=_GeminiDiscoveryRunner(SimpleNamespace(deepseek_search=service))
    sources=[];data={}
    with pytest.raises(DeepSeekUnavailable) as caught:
        runner._run_deepseek(CommandRequest('value_scan','/value_scan',report_date=date(2026,11,4)),
            [],sources,data,None,gap_fill=gap_fill)
    assert caught.value is error and len(sources)==1 and sources[0].url==source.url
    key='required_gap_deepseek_discovery' if gap_fill else 'deepseek_search_discovery'
    assert data[key]['complete'] is False and data[key]['status']=='partial_stopped'
    assert service.discover.call_count==1


def test_custom_pool_date_queries_keep_company_identity_not_pool_label():
    from datetime import date
    from research_center.models import CommandRequest
    from research_center.date_aware_context import augment_discovery_tasks_with_date_context,extract_target_terms
    request=CommandRequest('value_scan','/value_scan 自訂:2330,2241',target='自訂:2330,2241',
        candidate_pool='自訂股票清單',report_date=date(2026,11,4))
    data={'ai_candidates':[{'code':'2330','name':'台積電'},{'code':'2241','name':'艾姆勒'}]}
    tasks=augment_discovery_tasks_with_date_context(request,data,[{'label':'官方公告','queries':[]}])
    assert len(tasks[0]['queries'])==4
    assert all('2330 台積電 2241 艾姆勒' in query and '自訂' not in query for query in tasks[0]['queries'])
    assert '自訂' not in extract_target_terms(request,data)
    assert '2330' in extract_target_terms(request,data) and '2241' in extract_target_terms(request,data)
    fallback=augment_discovery_tasks_with_date_context(request,{},[{'label':'官方公告','queries':[]}])
    assert all('2330 2241' in query and '自訂' not in query for query in fallback[0]['queries'])


@pytest.mark.parametrize('publication',[None,'2026-11-05'])
def test_stopped_sources_do_not_bypass_historical_date_guard(publication):
    from datetime import date
    from unittest.mock import Mock
    from research_center.deepseek_service import DeepSeekUnavailable
    from research_center.models import CommandRequest,SourceItem
    from research_center.orchestrator import _GeminiDiscoveryRunner
    source=SourceItem('S001','公司公告','https://pr.tsmc.com/announcement','Level 1',
        published_date=publication,date_status='explicit' if publication else 'unknown',snippet='公司公告正文資料。')
    error=DeepSeekUnavailable('停止','job_budget_exceeded')
    error.partial_discovery_sources=[source]
    runner=_GeminiDiscoveryRunner(SimpleNamespace(deepseek_search=SimpleNamespace(discover=Mock(side_effect=error))))
    sources=[];data={}
    with pytest.raises(DeepSeekUnavailable):
        runner._run_deepseek(CommandRequest('value_scan','/value_scan',report_date=date(2026,11,4)),[],sources,data,None)
    assert sources==[]
    audit=data['deepseek_search_discovery']
    assert audit['qualified_source_count']==0 and audit['quarantined_source_ids']==['S001']
    assert audit['raw_source_records'][0]['url']==source.url


@pytest.mark.parametrize('manual_already_used',['0','10'])
def test_background_preserves_physical_manual_share_after_development(tmp_path,manual_already_used):
    from datetime import datetime,timezone
    from research_center.deepseek_service import ai_task
    from research_center.openai_credit_service import OpenAICreditLedger,OpenAICreditsUnavailable
    from tests.test_openai_routing import grant
    now=lambda:datetime(2026,11,5,tzinfo=timezone.utc)
    development=OpenAICreditLedger(tmp_path,[grant('secondary')],development=True,job_limit=0,now=now)
    with ai_task('test-cost'):
        _,ident=development.choose({'secondary'},Decimal('20'),reserve=True)
        development.finish(ident,'success',Decimal('20'),{})
    production=OpenAICreditLedger(tmp_path,[grant('secondary')],job_limit=0,now=now)
    if Decimal(manual_already_used):
        with ai_task('manual'):
            _,ident=production.choose({'secondary'},Decimal(manual_already_used),reserve=True)
            production.finish(ident,'success',Decimal(manual_already_used),{})
    with ai_task('background',background=True):
        _,ident=production.choose({'secondary'},Decimal('50'),reserve=True)
        production.finish(ident,'success',Decimal('50'),{})
        with pytest.raises(OpenAICreditsUnavailable): production.choose({'secondary'},Decimal('.01'))
        with pytest.raises(OpenAICreditsUnavailable): production.choose({'secondary'})
    with ai_task('manual'):
        remaining=Decimal('20')-Decimal(manual_already_used)
        _,ident=production.choose({'secondary'},remaining,reserve=True)
        production.finish(ident,'success',remaining,{})
    assert production.summary()['grants'][0]['used_or_reserved']=='90'
