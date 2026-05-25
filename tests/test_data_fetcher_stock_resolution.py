from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from data_fetcher import StockDataFetcher


class TestStockDataFetcherLocalResolution(unittest.TestCase):
    def setUp(self):
        from tests.test_cache_utils import ensure_test_cache_dir

        self.cache_dir = ensure_test_cache_dir("data_fetcher/local_resolution")
        self.stock_list = self.cache_dir / "stock_list.json"
        payload = {
            "stocks": [
                {"code": "1785", "symbol": "1785.TWO", "market": "TPEX", "name": "光洋科"},
                {"code": "2330", "symbol": "2330.TW", "market": "TWSE", "name": "台積電"},
            ]
        }
        self.stock_list.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def tearDown(self):
        from tests.test_cache_utils import safe_remove_test_cache

        safe_remove_test_cache("data_fetcher/local_resolution")

    def test_resolve_stock_code_uses_local_stock_list_without_network(self):
        with patch("data_fetcher.STOCK_LIST_PATH", self.stock_list), patch.object(StockDataFetcher, "_get_json") as mock_get_json:
            fetcher = StockDataFetcher()

            meta = fetcher.resolve_stock("1785")

            self.assertEqual(meta.code, "1785")
            self.assertEqual(meta.symbol, "1785.TWO")
            self.assertEqual(meta.market, "TPEX")
            self.assertEqual(meta.name, "光洋科")
            mock_get_json.assert_not_called()

    def test_resolve_stock_name_uses_local_stock_list_without_network(self):
        with patch("data_fetcher.STOCK_LIST_PATH", self.stock_list), patch.object(StockDataFetcher, "_get_json") as mock_get_json:
            fetcher = StockDataFetcher()

            meta = fetcher.resolve_stock("光洋科")

            self.assertEqual(meta.code, "1785")
            self.assertEqual(meta.symbol, "1785.TWO")
            self.assertEqual(meta.market, "TPEX")
            self.assertEqual(meta.name, "光洋科")
            mock_get_json.assert_not_called()


if __name__ == "__main__":
    unittest.main()
