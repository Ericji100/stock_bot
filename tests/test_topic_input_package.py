import json
import unittest

from research_center.topic_input_package import merge_topic_sources,topic_stage_variables


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
