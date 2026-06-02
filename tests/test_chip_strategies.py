import unittest
from datetime import date
from unittest.mock import MagicMock, patch
import pandas as pd


class DummyContext:
    def __init__(self, daily_df, candidates_df):
        self.daily_data = daily_df
        self.candidates = candidates_df


class TestDailyChipFrameNormalization(unittest.TestCase):
    def test_duplicate_numeric_columns_do_not_crash(self):
        import chip_strategies

        frame = pd.DataFrame(
            [
                [
                    date(2026, 5, 29),
                    "2330",
                    "TWSE",
                    10,
                    99,
                    2,
                    88,
                    3.5,
                    77,
                    "cache",
                ]
            ],
            columns=[
                "date",
                "code",
                "market",
                "foreign_net_lots",
                "foreign_net_lots",
                "trust_net_lots",
                "trust_net_lots",
                "foreign_ratio_pct",
                "foreign_ratio_pct",
                "source",
            ],
        )

        result = chip_strategies._normalize_daily_chip_frame(frame)

        self.assertEqual(len(result), 1)
        self.assertEqual(result.iloc[0]["code"], "2330")
        self.assertEqual(result.iloc[0]["foreign_net_lots"], 10)
        self.assertEqual(result.iloc[0]["trust_net_lots"], 2)
        self.assertEqual(result.iloc[0]["foreign_ratio_pct"], 3.5)

    def test_duplicate_date_column_does_not_crash(self):
        import chip_strategies

        frame = pd.DataFrame(
            [[date(2026, 5, 29), date(2026, 5, 28), "2330", "TWSE", 10, 2, 3.5, "cache"]],
            columns=[
                "date",
                "date",
                "code",
                "market",
                "foreign_net_lots",
                "trust_net_lots",
                "foreign_ratio_pct",
                "source",
            ],
        )

        result = chip_strategies._normalize_daily_chip_frame(frame)

        self.assertEqual(len(result), 1)
        self.assertEqual(result.iloc[0]["date"], date(2026, 5, 29))


class TestChipCoverageComputation(unittest.TestCase):
    def test_coverage_ok(self):
        # 57 unique dates, 100 candidate codes, 86 codes present -> ok
        from datetime import timedelta
        from datetime import timedelta
        base = date(2026, 4, 1)
        codes = [str(1000 + i) for i in range(86)]
        dates = [base + timedelta(days=i % 57) for i in range(86)]
        df = pd.DataFrame({"date": dates, "code": codes})
        candidates_df = pd.DataFrame({"code": [str(1000 + i) for i in range(100)], "market": ["TWSE"] * 100})
        ctx = DummyContext(df, candidates_df)

        unique_dates = pd.to_datetime(ctx.daily_data["date"]).dt.date.unique()
        chip_days = len(unique_dates)
        codes_in_daily = set(ctx.daily_data["code"].astype(str).unique())
        coverage_pct = len(codes_in_daily) / max(1, len(ctx.candidates))

        self.assertEqual(chip_days, 57)
        self.assertAlmostEqual(coverage_pct, 86 / 100)

    def test_coverage_not_ok(self):
        # 20 unique dates, poor code coverage -> not ok
        dates = [date(2026, 5, 1 + (i % 20)) for i in range(20)]
        codes = [str(2000 + (i % 10)) for i in range(20)]
        df = pd.DataFrame({"date": dates, "code": codes})
        candidates_df = pd.DataFrame({"code": [str(2000 + i) for i in range(100)], "market": ["TWSE"] * 100})
        ctx = DummyContext(df, candidates_df)

        unique_dates = pd.to_datetime(ctx.daily_data["date"]).dt.date.unique()
        chip_days = len(unique_dates)
        codes_in_daily = set(ctx.daily_data["code"].astype(str).unique())
        coverage_pct = len(codes_in_daily) / max(1, len(ctx.candidates))

        self.assertEqual(chip_days, 20)
        self.assertLess(coverage_pct, 0.2)


class TestFinMindScopePropagation(unittest.TestCase):
    """Verify FinMind scope parameter propagates from callers through chip_strategies."""

    @patch("httpx.Client")
    def test_finmind_payload_receives_scope(self, mock_client_cls):
        """_finmind_payload must use scope parameter passed from caller, not hardcode default."""
        import chip_strategies
        import importlib
        importlib.reload(chip_strategies)

        # Mock quota so can_use returns True for any scope
        mock_quota = MagicMock()
        mock_quota.can_use.return_value = True
        mock_quota.record_use = MagicMock()
        chip_strategies._FINMIND_QUOTA = mock_quota

        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.json.return_value = {"status": 200, "data": []}
        mock_response.raise_for_status = MagicMock()
        mock_client.get.return_value = mock_response
        mock_client.__enter__.return_value = mock_client
        mock_client_cls.return_value = mock_client

        # Call with non-default scope
        result = chip_strategies._finmind_payload(
            mock_client,
            {"dataset": "TaiwanStockInstitutionalInvestorsBuySell", "data_id": "2330"},
            scope="scan",
        )

        # Verify can_use was called with scope="scan"
        mock_quota.can_use.assert_called_with(cost=1, scope="scan")
        mock_quota.record_use.assert_called_with(cost=1, scope="scan")

    @patch("httpx.Client")
    def test_fetch_finmind_net_buy_for_stock_propagates_scope(self, mock_client_cls):
        """_fetch_finmind_net_buy_for_stock passes scope to _finmind_payload."""
        import chip_strategies
        import importlib
        importlib.reload(chip_strategies)

        mock_quota = MagicMock()
        mock_quota.can_use.return_value = True
        mock_quota.record_use = MagicMock()
        chip_strategies._FINMIND_QUOTA = mock_quota

        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "status": 200,
            "data": [{"date": "2026-05-15", "name": "Foreign_Investor", "buy": 1000, "sell": 500}],
        }
        mock_response.raise_for_status = MagicMock()
        mock_client.get.return_value = mock_response
        mock_client.__enter__.return_value = mock_client
        mock_client_cls.return_value = mock_client

        from datetime import date
        result = chip_strategies._fetch_finmind_net_buy_for_stock(
            mock_client, date(2026, 5, 15), "2330", "TWSE", scope="backfill"
        )

        mock_quota.can_use.assert_called_with(cost=1, scope="backfill")
        mock_quota.record_use.assert_called_with(cost=1, scope="backfill")


class TestExtraCandidatesForBackfill(unittest.TestCase):
    def test_build_market_context_merges_extra_candidates(self):
        import chip_strategies
        import importlib
        importlib.reload(chip_strategies)

        base = pd.DataFrame(
            [
                {
                    "code": "2330",
                    "symbol": "2330.TW",
                    "market": "TWSE",
                    "name": "台積電",
                    "industry": "半導體",
                    "price": 100.0,
                    "avg_volume_20d": 1000.0,
                    "monthly_revenue": 1_000_000.0,
                    "issued_shares": 1_000_000.0,
                }
            ]
        )
        base.attrs["total_symbols"] = 2
        base.attrs["scan_settings"] = {"target_trading_days": 60}

        with patch("chip_strategies._build_hard_filter_candidates", return_value=base), \
             patch("chip_strategies._fetch_recent_daily_chip_data", return_value=(pd.DataFrame(), None)), \
             patch("chip_strategies._build_weekly_distribution", return_value=pd.DataFrame()):
            context = chip_strategies.build_market_context(
                include_daily_data=True,
                extra_candidates=[{"code": "5425", "symbol": "5425.TWO", "market": "TPEX", "name": "台半"}],
            )

        self.assertEqual(set(context.candidates["code"].astype(str)), {"2330", "5425"})


if __name__ == "__main__":
    unittest.main()
