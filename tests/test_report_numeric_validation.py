import unittest
from research_center.report_numeric_validation import validate_valuation_tables


class NumericValidationTests(unittest.TestCase):
    def test_full_weighted_equation_is_not_last_term_equal_total(self):
        from research_center.report_numeric_validation import validate_explicit_arithmetic
        weighted='88.39×0.6 + 100×0.25 + 82×0.1 + 43×0.05 = 53.034 + 25 + 8.2 + 2.15 = **88.384**'
        checked=validate_explicit_arithmetic(weighted)
        self.assertTrue(checked['passed'])
        self.assertEqual(checked['checked_formulas'],2)
        self.assertFalse(validate_explicit_arithmetic(weighted.replace('88.384','98.384'))['passed'])
        self.assertFalse(validate_explicit_arithmetic('35×0.10＝9.75')['passed'])
        self.assertTrue(validate_explicit_arithmetic('2295÷2220－1＝+3.378%')['passed'])
        self.assertFalse(validate_explicit_arithmetic('2295÷2220－1＝+30.378%')['passed'])

    def table(self, price):
        return '| 情境 | EPS 假設 | 本益比假設 | 合理股價 |\n|---|---|---|---|\n| 保守 | 2026E 100 元 | 18–20 倍 | '+price+' |'

    def test_inconsistent_price_range_fails(self):
        result=validate_valuation_tables(self.table('1,850–2,050 元'))
        self.assertFalse(result['passed'])
        self.assertEqual(result['checked_rows'],1)
        self.assertIn('1800–2000',result['errors'][0])

    def test_consistent_price_range_passes(self):
        self.assertTrue(validate_valuation_tables(self.table('1,800–2,000 元'))['passed'])

    def test_missing_data_does_not_invent_price(self):
        result=validate_valuation_tables(self.table('資料不足'))
        self.assertEqual(result['checked_rows'],0)

    def test_eps_and_pe_ranges_use_both_endpoints(self):
        text=self.table('3,360–3,900 元').replace('2026E 100','2027E 140–150').replace('18–20','24–26')
        self.assertTrue(validate_valuation_tables(text)['passed'])

    def test_unrelated_tables_not_treated_as_valuation(self):
        self.assertEqual(validate_valuation_tables('| 指數 | 價格 |\n|---|---|\n| 台股 | 30000 |')['checked_rows'],0)

    def test_separate_pe_and_price_bounds_are_checked(self):
        text='| 情境 | EPS | 本益比下限 | 本益比上限 | 估值下限 | 估值上限 |\n|---|---|---|---|---|---|\n| 中性 | 116 | 18 | 22 | 2088 | 2552 |'
        self.assertEqual(validate_valuation_tables(text)['checked_rows'],1)
        self.assertTrue(validate_valuation_tables(text)['passed'])
        self.assertFalse(validate_valuation_tables(text.replace('2552','2700'))['passed'])

    def test_two_year_eps_and_separate_target(self):
        text='| 情境 | 2026E EPS | 2027E EPS | 本益比 | 合理股價／目標價 |\n|---|---|---|---|---|\n| 保守 | 108 | 125 | 18–20 | 1944–2500；目標價 2250 |'
        self.assertEqual(validate_valuation_tables(text)['checked_rows'],1)
        self.assertTrue(validate_valuation_tables(text)['passed'])

    def test_numeric_repair_cannot_pass_by_deleting_table(self):
        from research_center.report_numeric_validation import validate_valuation_repair
        self.assertFalse(validate_valuation_repair(self.table('1850–2050'),'資料不足')['passed'])

    def test_numeric_repair_preserves_original_sections(self):
        from research_center.report_numeric_validation import validate_valuation_repair
        original='## 風險\n保留反證\n'+self.table('1850–2050')
        self.assertFalse(validate_valuation_repair(original,self.table('1800–2000'))['passed'])
        self.assertTrue(validate_valuation_repair(original,'## 風險\n保留反證\n'+self.table('1800–2000'))['passed'])


class ForbiddenClaimTests(unittest.TestCase):
    def test_margin_is_not_a_money_amount(self):
        from research_center.report_numeric_validation import validate_report_numbers
        self.assertFalse(validate_report_numbers('毛利率 88,829 千元。')['passed'])
        self.assertTrue(validate_report_numbers('營業毛利 88,829 千元，毛利率 68.66%。')['passed'])

    def test_signed_capex_must_not_create_false_financial_difference(self):
        from research_center.report_numeric_validation import validate_local_financial_claims
        data={'financial_data':[{'Quarter':'2026Q1','Operating_Cash_Flow':-201533000,
                'Capital_Expenditure':-7187000,'Free_Cash_Flow':-208720000}]}
        wrong='具名原始欄位 2026Q1 營業現金流 -201,533 千元、資本支出 -7,187 千元、自由現金流 -208,720 千元；OCF－Capex 與原始值仍差 14,374 千元。'
        checked=validate_local_financial_claims(wrong,data)
        self.assertEqual(checked['checked_claims'],3)
        self.assertFalse(checked['passed'])
        self.assertTrue(validate_local_financial_claims(wrong.split('；')[0],data)['passed'])
        correction=wrong.rstrip('。')+'係原稿重複相減負數所致，經核對後不存在該差額。應付帳款不是營業現金流，不能把 248,206 千元當成現金流。'
        self.assertTrue(validate_local_financial_claims(correction,data)['passed'])
        self.assertFalse(validate_local_financial_claims(wrong+'另一資料不存在該差額。',data)['passed'])

    def test_explicit_denial_is_not_a_guarantee(self):
        from research_center.report_validator import _forbidden_claims
        self.assertEqual(_forbidden_claims('題材強不等於「短線必漲」。不保證獲利。'),[])

    def test_affirmative_claim_after_denial_still_fails(self):
        from research_center.report_validator import _forbidden_claims
        self.assertEqual(_forbidden_claims('不保證獲利。但是這支股票必漲。'),['必漲'])

    def test_cannot_interpret_as_rising_is_a_denial_not_a_guarantee(self):
        from research_center.report_validator import _forbidden_claims
        self.assertEqual(_forbidden_claims('不能直接解讀為短線必漲。'),[])
        self.assertEqual(_forbidden_claims('不能解讀為短線必漲，但我認為股票必漲。'),['必漲'])
