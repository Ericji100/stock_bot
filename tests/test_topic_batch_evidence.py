"""No model calls: strict local reference contracts, bounded repair and stop rules."""
from copy import deepcopy
import json

import pytest

from research_center.topic_batch_evidence import check_batch_evidence
from research_center.topic_input_package import topic_stage_variables, expand_topic_stage_variables
from research_center.topic_quote_binding import bind_topic_quotes
from research_center.topic_citation_audit import audit_topic_citations
from research_center.topic_models import TopicChangePack
from research_center.deepseek_service import DeepSeekUnavailable
from research_center.ai_execution_control import execution_scope, check_work, WorkStopped


def fixture():
    body='台積電表示正在評估新增產能，客戶訂單及營收貢獻仍未確定，不能當作已量產或已實現獲利。'
    data={'report_date':'2026-11-19','discovery_sources':[{'source_id':'S001',
        'url':'https://example.test/official','published_date':'2026-11-16',
        'content':body,'fetch_status':'success','fetch_provider':'requests_bs4'}]}
    batch=[{'theme_id':'power','theme_name':'電力','source_refs':[{'source_id':'S001'}]}]
    variables=topic_stage_variables(data,{},batch)
    span=json.loads(variables['topic_quote_catalog_json'])[0]
    payload={'actions':[{'theme_id':'power','theme_name':'電力','reason':'需求傳導是假設，仍須驗證。',
        'risk_notes':['客戶可能延後下單。'],'counter_evidence':['尚無訂單或營收資料。'],
        'missing_data':['追蹤月營收與法說會。'],
        'evidence':[{'quote_choice':span['quote_choice'],'claim':'產能評估仍有不確定性。'}]}]}
    return data,batch,variables,span,payload


def run(payload,data,batch,variables,callback):
    return check_batch_evidence(payload,data=data,batch=batch,variables=variables,
        stage='detail_expand_1',call_ai_json=callback,emit=lambda m:None)


def test_choice_resolves_to_full_immutable_body_without_model_transcription():
    data,batch,variables,span,payload=fixture()
    before=deepcopy(payload)
    def no_retry(*a):
        raise AssertionError('Valid reference needs no model repair.')
    bound,audit=run(payload,data,batch,variables,no_retry)
    row=bound['actions'][0]['evidence'][0]
    assert audit['passed'] and audit['retry_count']==0
    assert row['quote_id']==span['quote_id'] and row['supporting_quote']==data['discovery_sources'][0]['content']
    assert row['url']==span['url'] and row['publish_date']=='2026-11-16'
    assert payload==before
    decoded=expand_topic_stage_variables(variables)
    assert decoded['web_fetched_sources_json'][0]['content']==data['discovery_sources'][0]['content']


def test_choices_are_stable_but_cannot_cross_batch_or_body_version():
    data,batch,variables,span,payload=fixture()
    assert topic_stage_variables(data,{},batch)['topic_quote_catalog_json']==variables['topic_quote_catalog_json']
    for other in ([{**batch[0],'theme_id':'other'}],batch):
        altered=deepcopy(data)
        if other==batch:
            altered['discovery_sources'][0]['content']+='不同版本。'
        v=topic_stage_variables(altered,{},other)
        _,audit=run(payload,altered,other,v,lambda *a:{'repairs':[]})
        assert not audit['passed']


def test_one_reference_only_retry_preserves_reason_risks_counterevidence_and_other_rows():
    data,batch,variables,span,payload=fixture()
    row=payload['actions'][0]['evidence'][0]
    row.pop('quote_choice');row.update(quote_id=span['quote_id'][:-1],source_id='S001',
        url=span['url'],publish_date=span['publish_date'],supporting_quote='模型改寫的引句。')
    before=deepcopy(payload)
    calls=[]
    def repair(prompt,stage):
        calls.append((prompt,stage))
        requests=json.loads(prompt.split('\n',1)[1])
        assert len(requests)==1
        assert all(s['source_id']=='S001' for s in requests[0]['allowed_choices'].values())
        return {'repairs':[{'path':'/actions/0/evidence/0','quote_choice':span['quote_choice']}]}
    bound,audit=run(payload,data,batch,variables,repair)
    assert audit['passed'] and audit['retry_count']==1 and len(calls)==1
    assert calls[0][1]=='detail_expand_1_evidence_retry'
    for key in ('reason','risk_notes','counter_evidence','missing_data'):
        assert bound['actions'][0][key]==before['actions'][0][key]
    assert payload==before and audit['initial_issues']


@pytest.mark.parametrize('corruption',['date','invalid_date','url','source_url','choice_text_conflict'])
def test_conflicting_metadata_or_quote_does_not_silently_pass(corruption):
    data,batch,variables,span,payload=fixture()
    row=payload['actions'][0]['evidence'][0]
    if corruption=='date':row['publish_date']='2026-11-17'
    elif corruption=='invalid_date':row['publish_date']='not-a-date'
    elif corruption=='url':row['url']='https://example.test/another'
    elif corruption=='source_url':row['source_url']='https://example.test/another'
    else:row['supporting_quote']='杜撰，不能偽裝成原文。'
    _,audit=run(payload,data,batch,variables,lambda *a:{'repairs':[]})
    assert not audit['passed']


@pytest.mark.parametrize('answer',[
    {'repairs':[]}, {'repairs':[{'path':'/actions/1','quote_choice':'invented'}]},
    {'repairs':[],'actions':[]}, {'repairs':[{'path':'/actions/0/evidence/0','quote_choice':'invented','claim':'改主張'}]},
])
def test_repair_cannot_invent_choices_edit_other_rows_or_rewrite_analysis(answer):
    data,batch,variables,span,payload=fixture()
    payload['actions'][0]['evidence'][0]['quote_choice']='invented'
    calls=[]
    bound,audit=run(payload,data,batch,variables,lambda *a:(calls.append(a) or answer))
    assert len(calls)==1 and not audit['passed'] and bound==payload


@pytest.mark.parametrize('reason',['transport_unknown','openai_round_budget_exceeded','quota_exceeded','work_deadline'])
def test_provider_stop_retains_payload_and_never_retries_again(reason):
    data,batch,variables,span,payload=fixture()
    payload['actions'][0]['evidence'][0]['quote_choice']='invented'
    calls=[]
    def stop(*a):
        calls.append(a)
        raise DeepSeekUnavailable('stop',reason)
    with execution_scope(300):
        bound,audit=run(payload,data,batch,variables,stop)
        assert audit['stop_reason']==reason and bound==payload and len(calls)==1
        with pytest.raises(WorkStopped):check_work()


def test_insufficient_remaining_time_does_not_dispatch_repair():
    data,batch,variables,span,payload=fixture()
    payload['actions'][0]['evidence'][0]['quote_choice']='invented'
    def no_call(*a):raise AssertionError('Must not dispatch.')
    with execution_scope(119):
        bound,audit=run(payload,data,batch,variables,no_call)
    assert not audit['passed'] and audit['retry_count']==0 and bound==payload


def test_unresolved_batch_errors_survive_final_binding_and_still_block_pack():
    data,batch,variables,span,payload=fixture()
    pack=TopicChangePack.from_dict({'change_id':'pending','actions':[],
        'extra':{'batch_evidence_audit':[{'passed':False,'issues':[{'path':'/actions/0/evidence/0','reason':'quote_choice_invalid'}]}]}})
    bind_topic_quotes(pack,data)
    assert not audit_topic_citations(pack,data)['passed']


def test_native_repair_client_uses_existing_model_but_smaller_output_and_wait():
    from types import SimpleNamespace
    from research_center.topic_maintain_service import _topic_stage_client
    calls=[]
    class Client:
        timeout_seconds=600
        max_output_tokens=32768
        def with_timeout(self,value):
            calls.append(('timeout',value));return self
        def with_output_limit(self,value):
            calls.append(('output',value));return self
    client=Client()
    center=SimpleNamespace(deepseek=client,deepseek_organizer=client)
    assert _topic_stage_client(center,'detail_expand_1_evidence_retry',12000) is client
    assert calls==[('timeout',120),('output',8192)]


def test_pipeline_latches_unknown_repair_and_keeps_received_actions(monkeypatch):
    from research_center import topic_pipeline_service as pipeline
    from research_center.topic_models import TopicChangeMode
    data,batch,variables,span,payload=fixture()
    batch.append({**batch[0],'theme_id':'other','theme_name':'另一題'})
    payload['actions'][0]['evidence'][0]={'quote_id':span['quote_id'][:-1],
        'source_id':'S001','url':span['url'],'publish_date':span['publish_date'],
        'supporting_quote':'錯誤引句','claim':'保留原始主張'}
    calls=[]
    def model(prompt,stage):
        calls.append(stage)
        if stage=='candidate_extract':return {'candidates':deepcopy(batch)}
        if stage=='detail_expand_1':return deepcopy(payload)
        if stage.endswith('_evidence_retry'):raise DeepSeekUnavailable('費用未知','transport_unknown')
        raise AssertionError('Must not dispatch later model requests.')
    monkeypatch.setattr(pipeline,'_full_evidence_detail_batches',lambda candidates,*a,**kw:[[c] for c in candidates])
    pack,logs=pipeline.run_topic_pipeline(mode=TopicChangeMode.UPDATE,ai_model='offline',change_id='unknown-test',
        iso_ts='2026-11-19',structured_data=data,prompt_variables={},load_prompt=lambda name:'{topic_candidates_json}',
        render_prompt=lambda template,vars:template,call_ai_json=model,require_full_ai=True,
        prompt_capacity=lambda *a,**kw:{'fits':True})
    assert calls==['candidate_extract','detail_expand_1','detail_expand_1_evidence_retry']
    assert {a.theme_id for a in pack.actions}=={'power','other'}
    assert pack.actions[0].reason==payload['actions'][0]['reason']
    assert not pack.extra['ai_review_complete']
    assert any(row.get('stop_reason')=='transport_unknown' for row in pack.extra['batch_evidence_audit'])
    assert not audit_topic_citations(pack,data)['passed']


@pytest.mark.parametrize('bad_value',[['bad'],{'id':'bad'},42])
def test_malformed_reference_id_cannot_crash_the_validation_boundary(bad_value):
    data,batch,variables,span,payload=fixture()
    payload['actions'][0]['evidence'][0]={'quote_id':bad_value,'claim':'不可憑空確認'}
    _,audit=run(payload,data,batch,variables,lambda *a:{'repairs':[]})
    assert not audit['passed']


@pytest.mark.parametrize('bad_value',['',0,None,[],{}])
def test_empty_or_malformed_choice_is_not_silently_discarded(bad_value):
    data,batch,variables,span,payload=fixture()
    payload['actions'][0]['evidence'][0]['quote_choice']=bad_value
    _,audit=run(payload,data,batch,variables,lambda *a:{'repairs':[]})
    assert not audit['passed'] and any(i['reason']=='quote_choice_invalid' for i in audit['initial_issues'])


def test_candidate_company_evidence_uses_same_choice_binding():
    data,batch,variables,span,payload=fixture()
    variables=topic_stage_variables(data,{})
    span=json.loads(variables['topic_quote_catalog_json'])[0]
    payload={'candidates':batch,'company_knowledge_updates':{'companies':{'2330':{
        'evidence':[{'quote_choice':span['quote_choice'],'claim':'仍須驗證訂單'}]}}}}
    bound,audit=run(payload,data,[],variables,lambda *a:pytest.fail('No retry needed.'))
    assert audit['passed']
    row=bound['company_knowledge_updates']['companies']['2330']['evidence'][0]
    assert row['quote_id']==span['quote_id'] and row['supporting_quote']==data['discovery_sources'][0]['content']
