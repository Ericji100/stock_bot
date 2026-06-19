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


class TestTpexOpenApiSources(unittest.TestCase):
    def test_tpex_openapi_date_parses_compact_roc_date(self):
        import chip_strategies

        self.assertEqual(
            chip_strategies._parse_tpex_openapi_date("1150605"),
            date(2026, 6, 5),
        )

    def test_twse_holiday_schedule_parses_compact_roc_date(self):
        import chip_strategies

        response = MagicMock()
        response.json.return_value = [
            {"Name": "勞動節", "Date": "1150501"},
            {"Name": "國曆新年開始交易日", "Date": "1150102"},
        ]
        chip_strategies._HOLIDAY_DATES_CACHE.pop(2026, None)
        with patch("chip_strategies.httpx.get", return_value=response):
            holidays = chip_strategies._load_holiday_dates(2026)

        self.assertIn(date(2026, 5, 1), holidays)
        self.assertNotIn(date(2026, 1, 2), holidays)
        self.assertFalse(chip_strategies.is_possible_trading_day(date(2026, 5, 1)))
        chip_strategies._HOLIDAY_DATES_CACHE.pop(2026, None)

    def test_to_float_parses_percent_string(self):
        import chip_strategies

        self.assertEqual(chip_strategies._to_float("87.81%"), 87.81)

    def test_tpex_openapi_net_buy_parses_target_date_rows(self):
        import chip_strategies

        sample = [
            {
                "Date": "1150605",
                "SecuritiesCompanyCode": "5425",
                "ForeignInvestorsInclude MainlandAreaInvestors-Difference": "12,000",
                "SecuritiesInvestmentTrustCompanies-Difference": "-3,000",
            }
        ]
        with patch("chip_strategies._fetch_source_json", return_value=sample):
            frame = chip_strategies._fetch_tpex_openapi_net_buy_for_date(
                MagicMock(),
                date(2026, 6, 5),
                {"5425"},
            )

        self.assertEqual(len(frame), 1)
        self.assertEqual(frame.iloc[0]["code"], "5425")
        self.assertEqual(frame.iloc[0]["market"], "TPEX")
        self.assertEqual(frame.iloc[0]["foreign_net_lots"], 12.0)
        self.assertEqual(frame.iloc[0]["trust_net_lots"], -3.0)
        self.assertEqual(frame.iloc[0]["source"], "TPEX_OpenAPI")

    def test_tpex_openapi_net_buy_rejects_non_target_date_rows(self):
        import chip_strategies

        chip_strategies.TPEX_OPENAPI_AVAILABLE_DATES.pop("tpex_openapi_daily_trading", None)
        sample = [
            {
                "Date": "1150604",
                "SecuritiesCompanyCode": "5425",
                "ForeignInvestorsInclude MainlandAreaInvestors-Difference": "12,000",
                "SecuritiesInvestmentTrustCompanies-Difference": "-3,000",
            }
        ]
        with patch("chip_strategies._fetch_source_json", return_value=sample):
            frame = chip_strategies._fetch_tpex_openapi_net_buy_for_date(
                MagicMock(),
                date(2026, 6, 5),
                {"5425"},
            )

        self.assertTrue(frame.empty)
        self.assertEqual(
            chip_strategies.TPEX_OPENAPI_AVAILABLE_DATES.get("tpex_openapi_daily_trading"),
            {date(2026, 6, 4)},
        )
        self.assertFalse(chip_strategies._should_try_tpex_openapi("tpex_openapi_daily_trading", date(2026, 6, 5)))

    def test_tpex_openapi_foreign_ratio_parses_target_date_rows(self):
        import chip_strategies

        sample = [
            {
                "Date": "1150605",
                "SecuritiesCompanyCode": "5425",
                "PercentageOfSharesOC/FMIHeld": "18.25%",
            }
        ]
        with patch("chip_strategies._fetch_source_json", return_value=sample):
            frame = chip_strategies._fetch_tpex_openapi_foreign_ratio_for_date(
                MagicMock(),
                date(2026, 6, 5),
                {"5425"},
            )

        self.assertEqual(len(frame), 1)
        self.assertEqual(frame.iloc[0]["code"], "5425")
        self.assertEqual(frame.iloc[0]["foreign_ratio_pct"], 18.25)
        self.assertEqual(frame.iloc[0]["source"], "TPEX_OpenAPI")

    def test_recent_chip_data_uses_legacy_tpex_when_openapi_empty(self):
        import chip_strategies

        candidates = pd.DataFrame(
            [
                {
                    "code": "5425",
                    "market": "TPEX",
                    "issued_shares": 100_000_000.0,
                }
            ]
        )
        legacy_frame = pd.DataFrame(
            [
                {
                    "date": date(2026, 5, 29),
                    "code": "5425",
                    "market": "TPEX",
                    "foreign_net_lots": 2.0,
                    "trust_net_lots": 1.0,
                    "source": "TPEX",
                }
            ]
        )
        with patch("chip_strategies._load_daily_chip_cache", return_value=pd.DataFrame()), \
             patch("chip_strategies._fetch_tpex_openapi_net_buy_for_date", return_value=pd.DataFrame()) as openapi, \
             patch("chip_strategies._fetch_tpex_net_buy_for_date", return_value=legacy_frame) as legacy, \
             patch("chip_strategies._save_daily_chip_cache"):
            daily, latest = chip_strategies._fetch_recent_daily_chip_data(
                date(2026, 5, 29),
                candidates,
                target_trading_days=1,
                include_foreign_ratio=False,
            )

        openapi.assert_called_once()
        legacy.assert_called_once()
        self.assertEqual(latest, date(2026, 5, 29))
        self.assertEqual(len(daily), 1)
        self.assertEqual(daily.iloc[0]["code"], "5425")

    def test_tpex_legacy_payload_reads_buy_and_sell_tables(self):
        import chip_strategies

        def fake_fetch(_client, _source_key, _url, params=None):
            if params and params.get("searchType") == "buy":
                return {
                    "tables": [
                        {
                            "data": [
                                ["1", "5425", "台半", "1,000", "200", "800"],
                            ]
                        }
                    ]
                }
            return {
                "tables": [
                    {
                        "data": [
                            ["1", "5347", "世界", "100", "500", "-400"],
                        ]
                    }
                ]
            }

        with patch("chip_strategies._fetch_source_json", side_effect=fake_fetch):
            result = chip_strategies._fetch_tpex_net_payload(
                MagicMock(),
                date(2026, 5, 29),
                chip_strategies.TPEX_QFII_URL,
                "searchType",
                "tpex_qfii",
            )

        self.assertEqual(result["5425"], 800.0)
        self.assertEqual(result["5347"], -400.0)

    def test_recent_chip_data_skips_openapi_when_known_date_mismatch(self):
        import chip_strategies

        candidates = pd.DataFrame(
            [
                {
                    "code": "5425",
                    "market": "TPEX",
                    "issued_shares": 100_000_000.0,
                }
            ]
        )
        chip_strategies.TPEX_OPENAPI_AVAILABLE_DATES["tpex_openapi_daily_trading"] = {date(2026, 6, 5)}
        legacy_frame = pd.DataFrame(
            [
                {
                    "date": date(2026, 5, 29),
                    "code": "5425",
                    "market": "TPEX",
                    "foreign_net_lots": 2.0,
                    "trust_net_lots": 1.0,
                    "source": "TPEX",
                }
            ]
        )
        with patch("chip_strategies._load_daily_chip_cache", return_value=pd.DataFrame()), \
             patch("chip_strategies._fetch_tpex_openapi_net_buy_for_date") as openapi, \
             patch("chip_strategies._fetch_tpex_net_buy_for_date", return_value=legacy_frame) as legacy, \
             patch("chip_strategies._save_daily_chip_cache"):
            daily, latest = chip_strategies._fetch_recent_daily_chip_data(
                date(2026, 5, 29),
                candidates,
                target_trading_days=1,
                include_foreign_ratio=False,
            )

        openapi.assert_not_called()
        legacy.assert_called_once()
        self.assertEqual(latest, date(2026, 5, 29))
        self.assertEqual(len(daily), 1)
        chip_strategies.TPEX_OPENAPI_AVAILABLE_DATES.pop("tpex_openapi_daily_trading", None)

    def test_recent_chip_data_small_twse_ratio_gap_skips_mi_qfiis(self):
        import chip_strategies

        candidates = pd.DataFrame(
            [
                {
                    "code": "2330",
                    "market": "TWSE",
                    "issued_shares": 100_000_000.0,
                }
            ]
        )
        cached_frame = pd.DataFrame(
            [
                {
                    "date": date(2026, 5, 29),
                    "code": "2330",
                    "market": "TWSE",
                    "foreign_net_lots": 2.0,
                    "trust_net_lots": 1.0,
                    "foreign_ratio_pct": None,
                    "source": "cache",
                }
            ]
        )
        finmind_ratio = pd.DataFrame(
            [
                {
                    "date": date(2026, 5, 29),
                    "code": "2330",
                    "foreign_ratio_pct": 42.5,
                    "source": "FinMind",
                }
            ]
        )
        with patch("chip_strategies._load_daily_chip_cache", return_value=cached_frame), \
             patch("chip_strategies._fetch_twse_foreign_ratio_for_date") as mi_qfiis, \
             patch("chip_strategies._fetch_finmind_foreign_ratio_for_codes", return_value=finmind_ratio) as finmind, \
             patch("chip_strategies._save_daily_chip_cache"):
            daily, latest = chip_strategies._fetch_recent_daily_chip_data(
                date(2026, 5, 29),
                candidates,
                target_trading_days=1,
                include_foreign_ratio=True,
            )

        mi_qfiis.assert_not_called()
        finmind.assert_called_once()
        self.assertEqual(latest, date(2026, 5, 29))
        self.assertEqual(len(daily), 1)
        self.assertEqual(daily.iloc[0]["foreign_ratio_pct"], 42.5)

    def test_recent_chip_data_small_twse_net_gap_skips_t86(self):
        import chip_strategies

        candidates = pd.DataFrame(
            [
                {
                    "code": "2330",
                    "market": "TWSE",
                    "issued_shares": 100_000_000.0,
                }
            ]
        )
        finmind_net = pd.DataFrame(
            [
                {
                    "date": date(2026, 5, 29),
                    "code": "2330",
                    "market": "TWSE",
                    "foreign_net_lots": 2.0,
                    "trust_net_lots": 1.0,
                    "source": "FinMind",
                }
            ]
        )
        with patch("chip_strategies._load_daily_chip_cache", return_value=pd.DataFrame()), \
             patch("chip_strategies._fetch_twse_net_buy_for_date") as twse_t86, \
             patch("chip_strategies._fetch_finmind_net_buy_for_codes", return_value=finmind_net) as finmind, \
             patch("chip_strategies._save_daily_chip_cache"):
            daily, latest = chip_strategies._fetch_recent_daily_chip_data(
                date(2026, 5, 29),
                candidates,
                target_trading_days=1,
                include_foreign_ratio=False,
            )

        twse_t86.assert_not_called()
        finmind.assert_called_once()
        self.assertEqual(latest, date(2026, 5, 29))
        self.assertEqual(len(daily), 1)
        self.assertEqual(daily.iloc[0]["foreign_net_lots"], 2.0)

    def test_recent_chip_data_skips_taiwan_market_holiday(self):
        import chip_strategies

        candidates = pd.DataFrame(
            [
                {
                    "code": "2330",
                    "market": "TWSE",
                    "issued_shares": 100_000_000.0,
                }
            ]
        )
        with patch("chip_strategies.is_possible_trading_day", return_value=False), \
             patch("chip_strategies._load_daily_chip_cache") as load_cache, \
             patch("chip_strategies._fetch_twse_net_buy_for_date") as twse, \
             patch("chip_strategies._fetch_tpex_net_buy_for_date") as tpex:
            daily, latest = chip_strategies._fetch_recent_daily_chip_data(
                date(2026, 5, 1),
                candidates,
                target_trading_days=1,
                include_foreign_ratio=True,
            )

        load_cache.assert_not_called()
        twse.assert_not_called()
        tpex.assert_not_called()
        self.assertTrue(daily.empty)
        self.assertIsNone(latest)


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
