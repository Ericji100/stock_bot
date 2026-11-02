from datetime import date
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace
from uuid import uuid4

from stock_ai_bot.data_sources.official_market_cache import OfficialDailyCache
from stock_ai_bot.data_sources.data_fetcher import StockDataFetcher, StockMeta


class OfficialMarketCacheTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(os.environ.get('CODEX_TASK_TEST_ROOT','.cache/test_tmp/official_market'))/uuid4().hex
        self.day=date(2026,11,2)
        self.cache=OfficialDailyCache(self.root)

    def test_all_market_reply_shared_between_instances_without_dropping_rows(self):
        payload={'stat':'OK','date':'20261102','fields':['代號','數值'],'data':[['2330','1'],['2317','2']]}
        load=Mock(return_value=payload)
        self.assertEqual(self.cache.get('https://www.twse.com.tw/report',{},self.day,load),payload)
        another=OfficialDailyCache(self.root)
        self.assertEqual(another.get('https://www.twse.com.tw/report',{},self.day,load),payload)
        self.assertEqual(load.call_count,1)

    def test_stale_or_empty_response_not_cached_as_current(self):
        load=Mock(side_effect=[{'stat':'OK','date':'20261030','data':[['2330']]},
                               {'stat':'OK','date':'20261102','data':[]},
                               {'stat':'OK','date':'20261102','data':[['2330']]}])
        for _ in range(3): self.cache.get('https://www.twse.com.tw/report',{},self.day,load)
        self.assertEqual(load.call_count,3)

    def test_margin_current_api_preserves_lots_and_net_changes(self):
        payload={'stat':'OK','date':'20261102','tables':[{'fields':['代號','名稱','買進','賣出','現金償還','前日餘額','今日餘額','次一營業日限額','買進','賣出','現券償還','前日餘額','今日餘額','次一營業日限額','資券互抵','註記'],
            'data':[['2330','公司','884','608','18','34,961','35,219','100000','4','8','0','9','13','100000','2','']]}]}
        fetcher=StockDataFetcher.__new__(StockDataFetcher)
        fetcher._official_daily_cache=self.cache
        fetcher._get_json=Mock(return_value=payload)
        fetcher.notes=[]
        result=fetcher._fetch_twse_margin_daily(StockMeta('2330','2330.TW','TWSE','公司'),[self.day])
        row=result.iloc[0]
        self.assertEqual(row['Financing_Balance_Lots'],35219)
        self.assertEqual(row['Financing_Net_Change_Lots'],258)
        self.assertEqual(row['Short_Balance_Lots'],13)
        self.assertEqual(row['Short_Net_Change_Lots'],4)
        self.assertIn('MI_MARGN',fetcher._get_json.call_args.args[0])

    def test_unknown_margin_schema_is_missing_not_guessed(self):
        fetcher=StockDataFetcher.__new__(StockDataFetcher)
        fetcher._official_daily_cache=self.cache
        fetcher._get_json=Mock(return_value={'stat':'OK','date':'20261102','data':[['2330','unknown']]})
        fetcher.notes=[]
        result=fetcher._fetch_twse_margin_daily(StockMeta('2330','2330.TW','TWSE','公司'),[self.day])
        self.assertTrue(result.empty)
        self.assertIn('保留資料缺口',fetcher.notes[0])

    def test_tpex_company_issue_shares_avoids_per_stock_yahoo_fallback(self):
        from stock_ai_bot.strategies import chip_strategies as chips
        def payload(client, url, params=None):
            if url == chips.TPEX_NAME_API_URL:
                return [{'SecuritiesCompanyCode':'1240', 'IssueShares':'46443992',
                         'Paidin.Capital.NTDollars':'464439920'}]
            return []
        with patch.object(chips, '_fetch_json', side_effect=payload), patch.object(
            chips, '_fallback_shares_outstanding'
        ) as fallback:
            shares=chips._load_issued_shares_map([SimpleNamespace(code='1240',symbol='1240.TWO',market='TPEX')])
        self.assertEqual(shares['1240'],46443992)
        fallback.assert_not_called()
