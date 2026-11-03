import unittest

from research_center.financial_unit_validation import validate_cited_monetary_units
from research_center.models import SourceItem


class FinancialUnitValidationTests(unittest.TestCase):
    def test_explicit_score_scale_does_not_count_deductions_as_positive_points(self):
        from research_center.report_numeric_validation import validate_score_formulas, validate_valuation_repair
        text = '評分公式：總分＝傳導（滿分30）＋證據（滿分25）＋量化（滿分20）＋時程（滿分15）－反證（滿分10）。滿分100。'
        self.assertFalse(validate_score_formulas(text)['passed'])
        self.assertTrue(validate_score_formulas(text.replace('滿分100。', '滿分90。'))['passed'])
        self.assertFalse(validate_valuation_repair(text, '分數供參考')['passed'])
        self.assertTrue(validate_score_formulas('評分公式：總分＝甲（滿分60）+乙（滿分40）-風險（滿分10）。滿分100。')['passed'])

    def setUp(self):
        self.source = SourceItem('S001', '財報', 'https://example.com/earnings', 'Level 1',
            snippet='Revenue was NT$1,134.10 billion and net income NT$572.48 billion. Capital US$31,284.30 million.')

    def test_billion_to_yi_requires_factor_ten(self):
        result = validate_cited_monetary_units('營收 **1,134.10億元**、純益572.48億元 [S001]。', [self.source])
        self.assertFalse(result['passed'])
        self.assertEqual(len(result['issues']), 2)
        self.assertEqual(result['issues'][0]['source_value_in_yi'], '11341.00')

    def test_correct_conversion_and_rounding_pass(self):
        for text in ('營收11,341億元、純益5,724.8億元 [S001]', '營收1.1341兆元 [S001]',
                     '資本預算312.843億美元 [S001]'):
            with self.subTest(text=text):
                self.assertTrue(validate_cited_monetary_units(text, [self.source])['passed'])

    def test_no_inferred_error_for_uncited_unmatched_or_other_currency(self):
        for text in ('營收1,134.10億元', '假設營收2,000億元 [S001]', '營收1,134.10億美元 [S001]'):
            with self.subTest(text=text):
                self.assertTrue(validate_cited_monetary_units(text, [self.source])['passed'])

    def test_matching_amount_in_another_citation_prevents_false_positive(self):
        other = SourceItem('S002', '資料', 'https://example.com/2', 'Level 1', snippet='營收1,134.10億元')
        self.assertTrue(validate_cited_monetary_units('營收1,134.10億元 [S001][S002]', [self.source, other])['passed'])

    def test_repeated_error_is_not_duplicated_and_stock_counts_are_not_money(self):
        result = validate_cited_monetary_units('營收1,134.10億元 [S001]\n\n營收1,134.10億元 [S001]', [self.source])
        self.assertEqual(len(result['issues']), 1)
        self.assertEqual(validate_cited_monetary_units('股數1,134.10億股 [S001]', [self.source])['checked_amounts'], 0)

    def test_report_review_and_repair_share_checks_without_allowing_deleted_facts(self):
        from research_center.report_quality_service import review_reader_content
        from research_center.report_numeric_validation import validate_valuation_repair
        original = '## 財務\n營收1,134.10億元、純益572.48億元 [S001]'
        self.assertFalse(review_reader_content(original, [self.source])['mechanical_checks_passed'])
        repaired = '## 財務\n營收11,341億元、純益5,724.8億元 [S001]'
        self.assertTrue(validate_valuation_repair(original, repaired, [self.source])['passed'])
        self.assertFalse(validate_valuation_repair(original, '## 財務\n資料待查 [S001]', [self.source])['passed'])

    def test_fallback_does_not_claim_search_failed_when_analysis_failed(self):
        from research_center.report_builder import fallback_markdown
        from research_center.models import CommandRequest
        text = fallback_markdown(CommandRequest('research', '/research 2330', target='2330'), {},
                                 [self.source], '最終整合失敗')
        self.assertIn('AI 正式分析未完成', text)
        self.assertNotIn('公開網路搜尋失敗', text)
