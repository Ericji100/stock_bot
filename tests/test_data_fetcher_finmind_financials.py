from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from data_fetcher import StockDataFetcher, StockMeta


def _result(rows):
    return {"status": 200, "data": rows}


class TestFinMindQuarterlyFinancials(unittest.TestCase):
    @patch("data_fetcher.FinMindQuotaManager")
    @patch("data_fetcher.SourceHealthManager")
    @patch("data_fetcher.FinMindClient")
    def test_maps_three_statements_to_unified_schema(self, client_cls, _health_cls, _quota_cls):
        client = MagicMock()
        client_cls.return_value = client
        client.request_dataset.side_effect = [
            _result(
                [
                    {"date": "2025-03-31", "type": "Revenue", "value": 100.0},
                    {"date": "2025-03-31", "type": "GrossProfit", "value": 40.0},
                    {"date": "2025-03-31", "type": "OperatingExpenses", "value": 20.0},
                    {"date": "2025-03-31", "type": "OperatingIncome", "value": 20.0},
                    {"date": "2025-03-31", "type": "IncomeAfterTaxes", "value": 10.0},
                    {"date": "2025-03-31", "type": "EPS", "value": 1.2},
                ]
            ),
            _result(
                [
                    {"date": "2025-03-31", "type": "TotalAssets", "value": 500.0},
                    {"date": "2025-03-31", "type": "Liabilities", "value": 200.0},
                    {"date": "2025-03-31", "type": "CurrentAssets", "value": 250.0},
                    {"date": "2025-03-31", "type": "CurrentLiabilities", "value": 100.0},
                    {"date": "2025-03-31", "type": "Equity", "value": 300.0},
                    {"date": "2025-03-31", "type": "Inventories", "value": 50.0},
                    {"date": "2025-03-31", "type": "CashAndCashEquivalents", "value": 60.0},
                    {"date": "2025-03-31", "type": "AccountsReceivableNet", "value": 40.0},
                    {"date": "2025-03-31", "type": "AccountsPayable", "value": 30.0},
                    {"date": "2025-03-31", "type": "ShorttermBorrowings", "value": 20.0},
                    {"date": "2025-03-31", "type": "CurrentContractLiabilities", "value": 15.0},
                    {"date": "2025-03-31", "type": "CapitalStock", "value": 100.0},
                    {"date": "2025-03-31", "type": "CapitalStock_per", "value": 5.0},
                ]
            ),
            _result(
                [
                    {"date": "2025-03-31", "type": "CashFlowsFromOperatingActivities", "value": 25.0},
                    {"date": "2025-03-31", "type": "PropertyAndPlantAndEquipment", "value": -5.0},
                ]
            ),
        ]

        with StockDataFetcher(twse_delay_seconds=0) as fetcher:
            frame = fetcher.fetch_quarterly_financials(StockMeta("2330", "2330.TW", "TWSE", "台積電"))

        self.assertEqual(len(frame), 1)
        row = frame.iloc[0]
        self.assertEqual(row["Quarter"], "2025Q1")
        self.assertEqual(row["Quick_Assets"], 100.0)
        self.assertEqual(row["Interest_Bearing_Debt"], 20.0)
        self.assertEqual(row["Contract_Liabilities"], 15.0)
        self.assertEqual(row["Contract_Liabilities_Status"], "reported")
        self.assertEqual(row["Free_Cash_Flow"], 20.0)
        self.assertEqual(row["Financial_Data_Source"], "FinMind")
        self.assertEqual(str(row["Published_At"]), "2025-05-15")
        self.assertAlmostEqual(row["Gross_Margin"], 40.0)
        self.assertEqual(client.request_dataset.call_count, 3)

    @patch("data_fetcher.FinMindQuotaManager")
    @patch("data_fetcher.SourceHealthManager")
    @patch("data_fetcher.FinMindClient")
    def test_empty_finmind_preserves_existing_mops_fallback(self, client_cls, _health_cls, _quota_cls):
        client_cls.return_value.request_dataset.return_value = {}
        with StockDataFetcher(twse_delay_seconds=0) as fetcher:
            with patch.object(fetcher, "_fetch_mops_quarter_income_statement", return_value=None) as income:
                with patch.object(fetcher, "_fetch_mops_quarter_balance_sheet", return_value=None):
                    with patch.object(fetcher, "_fetch_mops_quarter_cash_flow", return_value=None):
                        frame = fetcher.fetch_quarterly_financials(
                            StockMeta("2330", "2330.TW", "TWSE", "台積電")
                        )

        self.assertTrue(frame.empty)
        self.assertTrue(income.called)


if __name__ == "__main__":
    unittest.main()
