from copy import deepcopy
from unittest.mock import patch

from research_center.topic_models import TopicChangeAction, TopicChangePack
from research_center.topic_citation_audit import restore_topic_source_quotes, audit_topic_citations
from research_center.topic_apply_service import coalesce_topic_updates, resolve_action_targets, validate_action_targets, confirm_change_pack


def pack(actions):
    return TopicChangePack.from_dict({'change_id':'offline-closeout','mode':'update','status':'pending',
        'model':'offline','created_at':'2026-11-10T00:00:00+0800','updated_at':'2026-11-10T00:00:00+0800',
        'summary':'八份分析','confidence':'medium','actions':actions})


def action(code, **kwargs):
    return {'action_type':'update_theme','theme_id':code,'theme_name':code,
            'target_theme_id':'existing',**kwargs}


BODY = '公司推出系統整合晶片（TSMC-SoIC ®）3D 晶片堆疊技術，A14 對 A14 的 SoIC 預計於 2029 年生產，其密度仍須驗證。'
QUOTE = '公司推出系統整合晶片（TSMC-SoIC）3D 晶片堆疊技術，A14 對 A14 的 SoIC 預計於 2029 年生產。'


def data(body=BODY):
    return {'report_date':'2026-11-10','discovery_sources':[{'source_id':'S1','url':'https://example.test/official',
        'published_date':'2026-04-23','content':body,'source_level':'L1_official','document_kind':'document_body'}]}


def quote_pack(quote=QUOTE):
    return pack([action('existing',evidence=[{'source_id':'S1','url':'https://example.test/official',
        'source':'公司','source_level':'L1_official','supporting_quote':quote}])])


def test_quote_restores_unique_source_typography_not_validator_rules():
    p=quote_pack()
    before=deepcopy(p.to_dict())
    assert not audit_topic_citations(p,data())['passed']
    repairs=restore_topic_source_quotes(p,data())
    assert len(repairs)==1
    assert repairs[0]['original_quote']==QUOTE
    assert p.actions[0].evidence[0].supporting_quote in BODY
    assert '®' in p.actions[0].evidence[0].supporting_quote
    assert audit_topic_citations(p,data())['passed']
    assert before['actions'][0]['evidence'][0]['supporting_quote']==QUOTE
    assert restore_topic_source_quotes(p,data())==[]


def test_quote_never_repairs_numbers_words_urls_or_ambiguous_body():
    for quote in (QUOTE.replace('2029','2028'),QUOTE.replace('生產','量產')):
        p=quote_pack(quote)
        assert restore_topic_source_quotes(p,data())==[]
        assert not audit_topic_citations(p,data())['passed']
    p=quote_pack()
    ambiguous=data()
    ambiguous['discovery_sources'].append({**ambiguous['discovery_sources'][0],'content':BODY.replace(' ®',' ™')})
    assert restore_topic_source_quotes(p,ambiguous)==[]
    p=quote_pack()
    missing=data();missing['discovery_sources'][0]['url']='https://different.test/official'
    assert restore_topic_source_quotes(p,missing)==[]


def test_quote_id_only_requires_unique_source_url_and_repairs_nested_relation():
    p=quote_pack()
    p.actions[0].company_relations=[{'source_id':'S1','supporting_quote':QUOTE,'status':'candidate'}]
    assert len(restore_topic_source_quotes(p,data()))==2
    assert p.actions[0].company_relations[0]['supporting_quote'] in BODY
    ambiguous=data()
    ambiguous['discovery_sources'].append({**ambiguous['discovery_sources'][0],'url':'https://different.test/body'})
    p=quote_pack();p.actions[0].company_relations=[{'source_id':'S1','supporting_quote':QUOTE}]
    restore_topic_source_quotes(p,ambiguous)
    assert p.actions[0].company_relations[0]['supporting_quote']==QUOTE


def test_coalescing_preserves_existing_context_all_analyses_and_candidate_status():
    rows=[action('a',keywords=['CoPoS'],risk_notes=['驗證失敗'],reason='第一份分析',
                 supply_chain_role='CoPoS設備',affected_companies=[{'company_code':'1234','verification_status':'candidate'}]),
          action('b',keywords=['SoIC'],risk_notes=['時程延後'],reason='第二份分析',supply_chain_role='SoIC技術',
                 counter_evidence=['未量產'],missing_data=['缺訂單'],extra={'unique_raw':'仍保留'})]
    p=pack(rows)
    profiles=[{'theme_id':'existing','theme_name':'完整先進封裝','keywords':['CoWoS'],
               'supply_chain_role':'既有脈絡','risk_notes':['既有風險']}]
    resolve_action_targets(p,profiles)
    originals=deepcopy([a.to_dict() for a in p.actions])
    assert not validate_action_targets(p,profiles)['passed']
    assert coalesce_topic_updates(p,profiles)==1
    assert len(p.actions)==1 and validate_action_targets(p,profiles)['passed']
    a=p.actions[0]
    assert a.theme_name=='完整先進封裝' and a.keywords==['CoWoS','CoPoS','SoIC']
    assert a.risk_notes==['既有風險','驗證失敗','時程延後']
    assert a.counter_evidence==['未量產'] and a.missing_data==['缺訂單']
    assert '第一份分析' in a.reason and '第二份分析' in a.reason
    assert all(s in a.supply_chain_role for s in ('既有脈絡','CoPoS設備','SoIC技術'))
    assert a.affected_companies[0]['verification_status']=='candidate'
    assert p.extra['coalesced_update_analyses'][0]['original_actions']==originals
    assert coalesce_topic_updates(p,profiles)==0


def test_conflicting_writers_are_rejected_before_any_backup_or_write():
    p=pack([action('existing'),action('existing',action_type='rename_theme')])
    profiles=[{'theme_id':'existing','theme_name':'既有題材'}]
    assert coalesce_topic_updates(p,profiles)==0
    assert not validate_action_targets(p,profiles)['passed']
    with patch('research_center.topic_apply_service.load_change_pack',return_value=p), \
         patch('research_center.topic_apply_service.load_topic_profiles',return_value=profiles), \
         patch('research_center.topic_apply_service.backup_topic_files') as backup:
        assert not confirm_change_pack('offline-closeout').success
        backup.assert_not_called()


def test_absent_target_duplicate_create_and_unrelated_updates_are_not_merged():
    for rows in ([action('missing'),action('missing')],
                 [action('existing',action_type='create_theme'),action('existing',action_type='create_theme')]):
        p=pack(rows)
        assert coalesce_topic_updates(p,[])==0
        assert not validate_action_targets(p,[])['passed']
    p=pack([action('first',target_theme_id='first'),action('second',target_theme_id='second')])
    profiles=[{'theme_id':n,'theme_name':n} for n in ('first','second')]
    before=deepcopy(p.to_dict())
    assert coalesce_topic_updates(p,profiles)==0 and p.to_dict()==before


def test_same_pack_new_theme_can_receive_one_update_but_not_repeated_updates():
    p=pack([action('new',action_type='create_theme',target_theme_id=None),action('new',target_theme_id='new')])
    assert validate_action_targets(p,[])['passed']
    p.actions.append(deepcopy(p.actions[-1]))
    assert not validate_action_targets(p,[])['passed']
    p=pack([action('existing',action_type='create_theme'),action('existing')])
    assert not validate_action_targets(p,[{'theme_id':'existing'}])['passed']
