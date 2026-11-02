import json
import unittest

from research_center.topic_input_package import (
    append_topic_shared_records, intern_topic_payloads, merge_topic_sources, topic_stage_variables,
)


def expand_records(value, records):
    if isinstance(value, dict):
        if set(value) == {'$topic_ref'}:
            return expand_records(records[value['$topic_ref']], records)
        return {key: expand_records(item, records) for key, item in value.items()}
    if isinstance(value, list):
        return [expand_records(item, records) for item in value]
    return value


class TopicInputPackageTests(unittest.TestCase):
    def data(self):
        return {'discovery_sources':[{'source_id':'S030','url':'https://example.test/a','title':'供電公司','snippet':'完整證據'*8000},
                                     {'source_id':'S031','url':'https://example.test/b','title':'其他產業','snippet':'無關本批但候選階段仍應保留'}],
                'base_sources':[{'source_id':'S001','url':'https://example.test/a','title':'供電公司'}],
                'existing_topic_profiles':[{'theme_id':'power','theme_name':'電源','keywords':['供電'],'risk_notes':['完整既有風險']}],
                'webfetch_evidence':{'items':[{'url':'https://example.test/a','claim':'反證也不可裁掉'}]},
                'company_knowledge':{'companies':{'9999':{'company_name':'供電公司','risk_notes':['完整公司底稿']}}}}

    def test_candidate_receives_all_source_text_without_string_truncation(self):
        data=self.data()
        variables=topic_stage_variables(data,{})
        sources=json.loads(variables['discovery_sources_json'])
        self.assertEqual(len(sources),2)
        self.assertEqual(sources[0]['snippet'],data['discovery_sources'][0]['snippet'])
        self.assertNotIn('[truncated',variables['discovery_sources_json'])

    def test_detail_receives_full_related_sources_profile_and_company_knowledge(self):
        batch=[{'theme_id':'power','theme_name':'電源','candidate_companies':[{'company_code':'9999','company_name':'供電公司'}],
                'source_refs':[{'url':'https://example.test/a'}]}]
        variables=topic_stage_variables(self.data(),{},batch)
        sources=json.loads(variables['web_fetched_sources_json'])
        self.assertEqual(len(sources),1)
        self.assertEqual(len(sources[0]['snippet']),32000)
        self.assertEqual(json.loads(variables['existing_topic_profiles_json'])[0]['risk_notes'],['完整既有風險'])
        self.assertEqual(json.loads(variables['company_knowledge_json'])['companies']['9999']['risk_notes'],['完整公司底稿'])

    def test_canonical_discovery_source_id_not_overwritten_by_old_base_id(self):
        sources=merge_topic_sources(self.data())
        self.assertEqual(sources[0]['source_id'],'S030')
        self.assertEqual(self.data()['base_sources'][0]['source_id'],'S001')

    def test_body_kept_once_when_snippet_is_already_inside_full_body(self):
        data={'discovery_sources':[{'source_id':'S001','url':'https://example.test','snippet':'營收資料'}],
              'web_fetched_sources':[{'url':'https://example.test','content':'完整營收資料與風險'}]}
        result=merge_topic_sources(data)
        self.assertEqual(result[0]['content'],'完整營收資料與風險')
        self.assertIsNone(result[0]['snippet'])
        self.assertEqual(data['discovery_sources'][0]['snippet'],'營收資料')

    def test_repeated_full_records_roundtrip_without_losing_order_or_counter_evidence(self):
        evidence={'url':'https://example.test/claim','content':'原文完整主張與反證'*100,
                  'publish_date':'2026-11-02','risk_notes':['需求可能放緩']}
        payload={'sources':[evidence], 'company_map':{'9999':{'evidence':[evidence,evidence]}},
                 'nodes':[{'evidence':[evidence], 'missing_data':['未揭露營收占比']}]}
        before=json.dumps(payload,ensure_ascii=False)
        encoded,records=intern_topic_payloads(payload)
        self.assertEqual(expand_records(encoded,records),payload)
        self.assertLess(len(json.dumps([encoded,records],ensure_ascii=False)),len(before))
        self.assertEqual(json.dumps(payload,ensure_ascii=False),before)
        prompt=append_topic_shared_records('原始提示',{'topic_shared_records_json':json.dumps(records,ensure_ascii=False)})
        self.assertIn('完整共用資料',prompt)
        self.assertIn(evidence['content'],prompt)

    def test_same_url_different_claims_dates_and_statuses_are_not_coalesced(self):
        evidence={'url':'https://example.test/a','content':'有完整證據'*100,'publish_date':'2026-05-01','status':'candidate'}
        different={**evidence,'publish_date':'2026-11-02','status':'verified','content':'不同反證'*100}
        payload={'evidence':[evidence,different,evidence]}
        encoded,records=intern_topic_payloads(payload)
        self.assertEqual(expand_records(encoded,records),payload)

    def test_existing_reference_shaped_source_is_not_reinterpreted(self):
        payload={'original':[{'$topic_ref':'R0001'},'原文'*500,'原文'*500]}
        encoded,records=intern_topic_payloads(payload)
        self.assertEqual(encoded,payload)
        self.assertEqual(records,{})

    def test_stage_registry_keeps_shared_evidence_in_the_actual_prompt(self):
        data=self.data()
        data['supply_chain_nodes']=[{'company_code':'9999','evidence':data['discovery_sources']}]
        batch=[{'theme_name':'電源','candidate_companies':[{'company_code':'9999','company_name':'供電公司'}]}]
        variables=topic_stage_variables(data,{},batch)
        records=json.loads(variables['topic_shared_records_json'])
        prompt=append_topic_shared_records(variables['web_fetched_sources_json'],variables)
        self.assertTrue(data['discovery_sources'][0]['snippet'] in prompt)
        restored=expand_records(json.loads(variables['supply_chain_nodes_json']),records)
        self.assertEqual(restored,data['supply_chain_nodes'])
