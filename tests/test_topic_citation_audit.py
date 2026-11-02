import unittest
from unittest.mock import patch

from research_center.topic_citation_audit import audit_topic_citations, quarantine_topic_source_leads
from research_center.topic_models import TopicChangePack, TopicEvidence, TopicSourceLevel


class TopicCitationAuditTests(unittest.TestCase):
    def test_search_page_is_preserved_as_lead_not_used_as_formal_support(self):
        pack=self.pack()
        lead=TopicEvidence(source='搜尋線索',source_level=TopicSourceLevel.L3_COMMUNITY,
                           content='尚待原文查證的完整線索',url='https://example.test/search/power')
        pack.actions[0].evidence.append(lead)
        data={'discovery_sources':[{'url':'https://example.test/article'}]}
        quarantine_topic_source_leads(pack,data)
        self.assertEqual(len(pack.actions[0].evidence),1)
        saved=pack.actions[0].extra['unverified_source_leads'][0]
        self.assertEqual(saved['content'],lead.content)
        self.assertEqual(saved['url'],lead.url)
        audit=audit_topic_citations(pack,data)
        self.assertTrue(audit['passed'])
        self.assertEqual(audit['counts']['unverified_lead'],1)
        self.assertEqual(audit['counts']['traceable_url'],1)

    def test_lead_cannot_be_used_to_support_a_company_relation_or_replace_all_evidence(self):
        for only_lead in [False,True]:
            with self.subTest(only_lead=only_lead):
                pack=self.pack('https://example.test/search/power' if only_lead else 'https://example.test/article')
                if not only_lead:
                    pack.actions[0].company_relations=[{'company_code':'9999','verification_status':'verified',
                                                       'evidence':[{'url':'https://example.test/search/power'}]}]
                data={'discovery_sources':[{'url':'https://example.test/article'}]}
                quarantine_topic_source_leads(pack,data)
                self.assertFalse(audit_topic_citations(pack,data)['passed'])

    def test_source_level_aliases_do_not_promote_weak_or_unknown_sources_to_media(self):
        from research_center.topic_schema_normalizer import _normalize_evidence
        for label,expected in [('Level 1',TopicSourceLevel.L1_OFFICIAL),('L2_media',TopicSourceLevel.L2_MEDIA),
                               ('Level 3',TopicSourceLevel.L3_COMMUNITY),('Level 4',TopicSourceLevel.L3_COMMUNITY),
                               ('unknown',TopicSourceLevel.L3_COMMUNITY)]:
            with self.subTest(label=label):
                raw={'source':'來源','source_level':label,'content':'完整線索'}
                self.assertEqual(TopicEvidence.from_dict(raw).source_level,expected)
                self.assertEqual(_normalize_evidence([raw])[0].source_level,expected)

    def pack(self, url='https://example.test/article', **evidence):
        return TopicChangePack.from_dict({
            'change_id':'isolated', 'mode':'update', 'status':'pending',
            'created_at':'2026-11-02T10:00:00+0800', 'actions':[{
                'theme_id':'power','theme_name':'電源','evidence':[{
                    'source':'媒體', 'content':'主張仍須查證', 'url':url, **evidence}]}]})

    def test_body_link_is_not_promoted_to_fetched_evidence(self):
        url='https://example.test/linked-article'
        data={'discovery_sources':[{'url':'https://example.test/main','content':f'報導連結 {url}'}]}
        audit=audit_topic_citations(self.pack(url),data)
        self.assertFalse(audit['passed'])
        self.assertEqual(audit['counts']['body_link_not_fetched'],1)

    def test_search_page_fails_even_when_it_is_in_the_source_catalogue(self):
        url='https://example.test/search/Rubin?page=74'
        audit=audit_topic_citations(self.pack(url),{'discovery_sources':[{'url':url,'snippet':'搜尋結果'}]})
        self.assertFalse(audit['passed'])
        self.assertEqual(audit['counts']['non_document_url'],1)

    def test_pdf_and_old_dated_evidence_remain_traceable_as_history(self):
        url='https://example.test/company/annual.pdf'
        audit=audit_topic_citations(self.pack(url,publish_date='2024-05-01'),{
            'discovery_sources':[{'url':url,'published_date':'2024-05-01','content':'完整歷史證據'}]})
        self.assertTrue(audit['passed'])
        self.assertEqual(audit['counts']['historical_context'],1)
        self.assertIn('不表示已獨立證實',audit['scope'])

    def test_future_date_and_changed_publication_date_block(self):
        url='https://example.test/article'
        audit=audit_topic_citations(self.pack(url,publish_date='2026-11-03'),{
            'discovery_sources':[{'url':url,'published_date':'2026-11-01'}]})
        self.assertFalse(audit['passed'])
        self.assertEqual(audit['counts']['future_evidence'],1)
        self.assertEqual(audit['counts']['citation_date_mismatch'],1)

    def test_existing_company_evidence_is_traceable_but_not_marked_as_new_search(self):
        url='https://example.test/old'
        audit=audit_topic_citations(self.pack(url),{'company_knowledge':{'companies':{
            '9999':{'evidence':[{'url':url,'publish_date':'2026-05-01'}]}}}})
        self.assertTrue(audit['passed'])

    def test_unknown_and_invalid_urls_are_blocked_and_empty_sources_fail(self):
        for url in ['https://other.test/invented','https://[bad','javascript:alert(1)']:
            with self.subTest(url=url):
                self.assertFalse(audit_topic_citations(self.pack(url),{})['passed'])
        pack=self.pack()
        pack.actions[0].evidence=[]
        self.assertEqual(audit_topic_citations(pack,{})['counts']['action_without_citation'],1)

    def test_apply_stops_before_backup_and_writes_when_citation_audit_fails(self):
        from research_center import topic_apply_service as service
        from research_center.topic_formatters import format_change_pack_created_summary
        pack=self.pack()
        pack.extra.update(ai_review_complete=True,evidence_review_complete=False,
                          citation_audit={'passed':False,'blocking_count':2})
        with patch.object(service,'load_change_pack',return_value=pack), patch.object(service,'backup_topic_files') as backup:
            self.assertFalse(service.confirm_change_pack('isolated').success)
        backup.assert_not_called()
        text=format_change_pack_created_summary(pack)
        self.assertIn('不可套用',text)
        self.assertIn('未通過檢查',text)
