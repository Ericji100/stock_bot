from copy import deepcopy
import json
import unittest

from research_center.entity_resolver import exact_company_identity
from research_center.source_rank import make_source_items
from research_center.source_text_cleaner import source_body_text
from research_center.topic_citation_audit import _provided_body
from research_center.topic_input_package import expand_evidence_json, topic_stage_variables
from research_center.topic_maintain_service import _source_item_to_dict
from research_center.topic_pipeline_service import _detail_covers_batch
from research_center.topic_pipeline_service import run_topic_pipeline
from research_center.topic_models import TopicChangeMode, TopicChangeStatus
from research_center.topic_schema_normalizer import (
    normalize_topic_detail_actions, repair_topic_detail_structure, resolve_topic_company_identities,
)


class TopicBodyTransmissionTests(unittest.TestCase):
    def legacy(self):
        return {'source_id':'S001','url':'https://example.test/a','title':'來源',
                'snippet':'搜尋摘要（尚須原文核對）：\n尚未確認的摘要\n\n取得的本文：\n完整正文、公司說明與風險。',
                'document_kind':'document_body','fetch_status':'success','fetch_provider':'requests_bs4'}

    def test_actual_body_survives_shared_source_and_topic_serialization(self):
        raw={**self.legacy(),'content':'原始正文\n不得刪除的反證'*1000}
        source=make_source_items([raw])[0]
        saved=_source_item_to_dict(source)
        self.assertEqual(source.content,raw['content'])
        self.assertEqual(saved['content'],raw['content'])
        self.assertEqual(_provided_body(saved),raw['content'])
        self.assertEqual(saved['search_summary_events'],[])

    def test_legacy_envelope_decodes_only_fetched_body_and_preserves_original(self):
        raw=self.legacy(); before=deepcopy(raw)
        self.assertEqual(_provided_body(raw),'完整正文、公司說明與風險。')
        source=make_source_items([raw])[0]
        self.assertEqual(source.content,'完整正文、公司說明與風險。')
        self.assertEqual(raw,before)
        self.assertIn('尚未確認的摘要',source.snippet)

    def test_success_flags_long_summaries_failed_or_reference_pages_do_not_create_bodies(self):
        raw=self.legacy()
        for change in ({'document_kind':'reference_link'},{'fetch_status':'partial'},
                       {'fetch_provider':'openai_search'},{'snippet':'可能受惠'*2000},
                       {'snippet':raw['snippet']+'取得的本文：另一段'}):
            with self.subTest(change=change):
                self.assertEqual(source_body_text({**raw,**change}),'')

    def test_different_events_same_url_keep_all_body_text(self):
        first={**self.legacy(),'content':'第一個事件與其風險'*30}
        second={**self.legacy(),'content':'第二個事件與其反證'*30}
        source=make_source_items([first,second])[0]
        self.assertIn(first['content'],source.content)
        self.assertIn(second['content'],source.content)


class TopicDetailStructureTests(unittest.TestCase):
    def payload(self):
        return {'actions':[{'theme_id':'power','theme_name':'電源','evidence':[]},
            {'source_id':'S001','url':'https://example.test/a','supporting_quote':'完整證據與風險'*10,
             'supply_chain_nodes':[{'theme_id':'power','company_code':'9999'}],
             'risk_notes':['訂單可能延遲'],'counter_evidence':['需求下降']}]}

    def test_explicit_single_owner_repairs_structure_without_altering_raw_response(self):
        raw=self.payload(); before=deepcopy(raw)
        result,repairs=repair_topic_detail_structure(raw,[{'theme_id':'power'}])
        self.assertTrue(_detail_covers_batch(result,[{'theme_id':'power'}]))
        self.assertEqual(raw,before)
        self.assertEqual(repairs[0]['original_row'],before['actions'][1])
        action=result['actions'][0]
        self.assertEqual(action['risk_notes'],['訂單可能延遲'])
        self.assertEqual(action['evidence'][0]['supporting_quote'],raw['actions'][1]['supporting_quote'])
        self.assertEqual(action['supply_chain_nodes'],raw['actions'][1]['supply_chain_nodes'])

    def test_ambiguous_conflicting_or_unowned_objects_still_fail_coverage(self):
        for nodes in ([],[{'theme_id':'other'}],[{'theme_id':'power'},{'theme_id':'other'}],[{}]):
            with self.subTest(nodes=nodes):
                raw=self.payload();raw['actions'][1]['supply_chain_nodes']=nodes
                result,repairs=repair_topic_detail_structure(raw,[{'theme_id':'power'}])
                self.assertEqual(result,raw)
                self.assertFalse(repairs)
                self.assertFalse(_detail_covers_batch(result,[{'theme_id':'power'}]))
        raw=self.payload();raw['actions'][0]['risk_notes']=['原先不同風險']
        result,repairs=repair_topic_detail_structure(raw,[{'theme_id':'power'}])
        self.assertEqual(result,raw)
        self.assertFalse(repairs)

    def test_explicit_root_continuation_and_orphan_quote_are_reparented_together(self):
        raw=self.payload()
        continuation=raw['actions'][1]
        for key in ('supply_chain_nodes','risk_notes','counter_evidence'):
            raw[key]=continuation.pop(key)
        before=deepcopy(raw)
        result,repairs=repair_topic_detail_structure(raw,[{'theme_id':'power'}])
        self.assertTrue(_detail_covers_batch(result,[{'theme_id':'power'}]))
        self.assertEqual(result['actions'][0]['risk_notes'],before['risk_notes'])
        self.assertEqual(repairs[0]['original_row'],before['actions'][1])
        self.assertEqual(repairs[0]['original_root_fields']['supply_chain_nodes'],before['supply_chain_nodes'])
        self.assertEqual(raw,before)

    def test_unowned_root_fields_cannot_be_silently_ignored(self):
        payload={'actions':[{'theme_id':'power'}],'risk_notes':['未知歸屬']}
        result,repairs=repair_topic_detail_structure(payload,[{'theme_id':'power'}])
        self.assertFalse(_detail_covers_batch(result,[{'theme_id':'power'}]))
        self.assertFalse(repairs)

    def test_root_conflict_does_not_mutate_or_partly_repair_the_response(self):
        raw=self.payload()
        for key in ('supply_chain_nodes','risk_notes','counter_evidence'):
            raw[key]=raw['actions'][1].pop(key)
        raw['actions'][0]['risk_notes']=['不同風險']
        before=deepcopy(raw)
        result,repairs=repair_topic_detail_structure(raw,[{'theme_id':'power'}])
        self.assertEqual(result,before)
        self.assertEqual(raw,before)
        self.assertFalse(repairs)

    def test_native_pipeline_adopts_only_explicitly_repaired_complete_batch(self):
        raw=self.payload()
        for key in ('supply_chain_nodes','risk_notes','counter_evidence'):
            raw[key]=raw['actions'][1].pop(key)
        def call(prompt,stage):
            return {'candidates':[{'theme_id':'power','theme_name':'電源'}]} if stage=='candidate_extract' else deepcopy(raw)
        pack,logs=run_topic_pipeline(mode=TopicChangeMode.UPDATE,ai_model='offline',change_id='test',
            iso_ts='2026-11-04T10:00:00+0800',structured_data={},prompt_variables={},
            load_prompt=lambda name:'{topic_candidates_json}',render_prompt=lambda text,variables:text,
            call_ai_json=call,require_full_ai=True)
        self.assertTrue(pack.extra['ai_review_complete'])
        self.assertTrue(any(log.get('repairs') for log in logs))
        self.assertEqual(pack.actions[0].counter_evidence,['需求下降'])

    def test_missing_response_still_fails_native_pipeline_after_valid_batch(self):
        candidates=[{'theme_id':str(i),'theme_name':'題材'+str(i)} for i in range(3)]
        def call(prompt,stage):
            if stage=='candidate_extract':return {'candidates':candidates}
            if stage=='detail_expand_1':return {'actions':candidates[:2]}
            raise RuntimeError('No recorded response')
        pack,_=run_topic_pipeline(mode=TopicChangeMode.UPDATE,ai_model='offline',change_id='test',
            iso_ts='2026-11-04T10:00:00+0800',structured_data={},prompt_variables={},
            load_prompt=lambda name:'{topic_candidates_json}',render_prompt=lambda text,variables:text,
            call_ai_json=call,require_full_ai=True)
        self.assertFalse(pack.extra['ai_review_complete'])
        self.assertEqual(pack.status,TopicChangeStatus.FAILED)

    def test_missing_candidate_and_unresolved_refs_are_not_repaired_into_success(self):
        result,_=repair_topic_detail_structure(self.payload(),[{'theme_id':'power'},{'theme_id':'missing'}])
        self.assertFalse(_detail_covers_batch(result,[{'theme_id':'power'},{'theme_id':'missing'}]))
        raw=self.payload();raw['actions'][0]['evidence']=[{'$topic_ref':'R001'}]
        result,_=repair_topic_detail_structure(raw,[{'theme_id':'power'}])
        self.assertFalse(_detail_covers_batch(result,[{'theme_id':'power'}]))


class TopicCompanyIdentityTests(unittest.TestCase):
    def test_exact_identity_does_not_guess_group_names_or_ambiguous_aliases(self):
        names={'1111':'甲公司','2222':'甲公司','9999':'供電公司'}
        self.assertEqual(exact_company_identity('供電公司','',names)['company_code'],'9999')
        self.assertEqual(exact_company_identity('甲公司','',names)['status'],'ambiguous')
        self.assertEqual(exact_company_identity('供電集團','',names)['status'],'unresolved')
        self.assertEqual(exact_company_identity('甲公司','9999',names)['status'],'conflict')

    def test_profile_company_keywords_supply_complete_matching_background(self):
        data={'authoritative_company_names':{'9999':'供電公司'},
              'existing_topic_profiles':[{'theme_id':'power','theme_name':'電源','keywords':['供電公司']}],
              'company_knowledge':{'companies':{'9999':{'company_name':'供電公司','risk_notes':['完整風險'*100]}}},
              'company_topic_map':{'9999':{'themes':['power']}},
              'supply_chain_nodes':[{'theme_id':'power','company_code':'9999','company_name':'供電公司'}]}
        before=deepcopy(data)
        variables=topic_stage_variables(data,{},[{'theme_id':'power','theme_name':'電源'}])
        restored=expand_evidence_json({'values':{k:json.loads(variables[k]) for k in ('company_knowledge_json','company_topic_map_json','supply_chain_nodes_json')},
            'shared_records':json.loads(variables['topic_shared_records_json']),
            'field_names':json.loads(variables['topic_field_names_json'])})
        self.assertEqual(restored['company_knowledge_json']['companies'],data['company_knowledge']['companies'])
        self.assertEqual(restored['company_topic_map_json'],data['company_topic_map'])
        self.assertEqual(restored['supply_chain_nodes_json'],data['supply_chain_nodes'])
        self.assertEqual(data,before)

    def test_mapping_does_not_upgrade_candidate_or_invent_roles_and_evidence(self):
        actions=normalize_topic_detail_actions({'actions':[{'theme_id':'power','affected_companies':[
            {'company_name':'供電公司','role':'尚待核實','verification_status':'candidate'},
            {'company_name':'供電集團','verification_status':'candidate'}]}]})
        checks=resolve_topic_company_identities(actions,{'9999':'供電公司'})
        first,second=actions[0].affected_companies
        self.assertEqual(first['company_code'],'9999')
        self.assertEqual(first['verification_status'],'candidate')
        self.assertEqual(first['role'],'尚待核實')
        self.assertEqual(first['evidence'],[])
        self.assertEqual(second['company_code'],'')
        self.assertEqual(checks[-1]['status'],'unresolved')
