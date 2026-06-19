from __future__ import annotations

import json
import os
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from research_center.news_models import NewsItem
from research_center.news_repository import NewsRepository
from research_center.news_service import (
    _batch_classify_news,
    _classification_payload,
    _classify_batch_size,
    _classify_limit,
    _classify_text_limit,
    _classify_timeout_seconds,
    _news_high_tier_classify_limit,
    _select_high_tier_news_items,
    _filter_taiwan_finance_news,
    _backfill_refresh_item_dates,
    _is_non_article_page,
    _is_taiwan_finance_news,
    _normalize_news_title,
    _call_news_classifier,
    _apply_display_source_penalties,
    build_scheduled_news_diagnostics,
    build_news_discovery_queries,
    run_news_7d,
    run_news_latest,
    run_news_scheduled_latest,
    run_scheduled_news_lightweight_refresh,
    scheduled_news_lightweight_refresh_categories,
    save_user_submitted_news_url,
)
from research_center.news_categories import normalize_news_category, news_category_label, ordered_news_category_keys
from research_center.news_formatters import format_news_detail, format_news_digest, format_news_refresh_result
from research_center.web_fetch_service import WebFetchResult, fetch_web_content
from tests.test_cache_utils import ensure_test_cache_dir, safe_remove_test_cache


class NewsRepositoryTests(unittest.TestCase):
    def tearDown(self):
        safe_remove_test_cache("news_repository")

    def test_accepts_string_db_path(self):
        tmp = ensure_test_cache_dir("news_repository")
        db_path = tmp / "news.db"
        with patch.object(NewsRepository, "_init_schema", return_value=None):
            repo = NewsRepository(str(db_path))
        self.assertEqual(repo.db_path, db_path)

    def test_get_by_id_and_get_by_url(self):
        item = NewsItem(
            id="123",
            title="Taiwan semiconductor AI stocks rise",
            url="https://www.cna.com.tw/news/afe/202605210001.aspx",
            source="CNA",
            published_at="2026-05-21T09:00:00",
            summary="Taiwan stock market semiconductor AI news.",
            created_at="2026-05-21T09:00:00",
        )
        with patch.object(NewsRepository, "_init_schema", return_value=None):
            repo = NewsRepository("dummy.db")
        with patch.object(repo, "_query", return_value=[item]) as query:
            self.assertEqual(repo.get_by_url(item.url).title, item.title)
            self.assertEqual(repo.get_by_id("N123").url, item.url)
        self.assertEqual(query.call_count, 2)

    def test_save_and_query_recent_by_origin(self):
        with patch.object(NewsRepository, "_init_schema", return_value=None):
            repo = NewsRepository("dummy.db")
        with patch.object(repo, "_query", return_value=[]) as query:
            repo.query_recent(hours=24, news_origin="refresh")
        sql, params = query.call_args.args
        self.assertIn("news_origin = ?", sql)
        self.assertEqual(params[1], "refresh")

    def test_news_signal_tags_split_signal_and_heat(self):
        from research_center.news_models import apply_news_signal_tags

        item = NewsItem(
            title="AI 伺服器供應鏈打入新客戶，股價漲停爆量",
            summary="公司出貨量產題材升溫，但市場追高。",
            importance_score=90,
        )
        tagged = apply_news_signal_tags(item)

        self.assertIn("topic_clue", tagged.tags)
        self.assertIn("catalyst", tagged.tags)
        self.assertIn("heat_risk", tagged.tags)
        self.assertGreater(tagged.news_signal_score, 0)
        self.assertGreater(tagged.news_heat_risk_score, 0)


class NewsQueriesTests(unittest.TestCase):
    def test_news_queries_are_taiwan_finance_only(self):
        tasks = build_news_discovery_queries("latest")
        flat: list[str] = []
        for task in tasks:
            for query in task.get("queries", []):
                if isinstance(query, dict):
                    flat.extend(str(item) for item in query.get("items", []))
                else:
                    flat.append(str(query))
        self.assertGreater(len(flat), 0)
        self.assertTrue(any("Taiwan" in query or "台股" in query for query in flat))
        self.assertFalse(any(query.strip().lower() in {"latest news", "breaking news"} for query in flat))
        self.assertFalse(any("??" in query for query in flat))

    def test_news_queries_cover_display_categories(self):
        tasks = build_news_discovery_queries("latest")
        labels = {str(task.get("label", "")) for task in tasks}
        self.assertIn("台股與大盤", labels)
        self.assertIn("題材與族群輪動", labels)
        self.assertIn("AI / 半導體", labels)
        self.assertIn("供應鏈與產業", labels)
        self.assertIn("台指期與盤前風險", labels)
        self.assertIn("個股利多利空", labels)

    def test_news_queries_cover_taiex_futures_risk_events(self):
        tasks = build_news_discovery_queries("latest")
        flat: list[str] = []
        for task in tasks:
            for query in task.get("queries", []):
                if isinstance(query, dict):
                    flat.extend(str(item) for item in query.get("items", []))
                else:
                    flat.append(str(query))
        joined = "\n".join(flat)
        self.assertIn("台指期", joined)
        self.assertIn("夜盤", joined)
        self.assertIn("跌停", joined)
        self.assertIn("盤前風險", joined)

    def test_news_queries_can_be_filtered_for_lightweight_categories(self):
        with patch.dict(os.environ, {"NEWS_REFRESH_TASK_CATEGORIES": "sector_rotation,macro_policy"}):
            tasks = build_news_discovery_queries("latest")
        labels = [task.get("label") for task in tasks]
        self.assertIn("題材與族群輪動", labels)
        self.assertIn("政策 / 匯率 / 總經", labels)
        self.assertNotIn("台股與大盤", labels)
        self.assertNotIn("AI / 半導體", labels)

    def test_news_queries_do_not_use_generic_english_news_queries(self):
        tasks = build_news_discovery_queries("latest")
        flat: list[str] = []
        for task in tasks:
            for query in task.get("queries", []):
                if isinstance(query, dict):
                    flat.extend(str(item) for item in query.get("items", []))
                else:
                    flat.append(str(query))
        joined = "\n".join(flat).lower()
        self.assertNotIn("latest news", joined)
        self.assertNotIn("breaking news", joined)
        self.assertNotIn("world news", joined)


class NonArticlePageFilterTests(unittest.TestCase):
    def _item(self, url: str, title: str = "Taiwan stock market news") -> NewsItem:
        return NewsItem(
            id="x",
            title=title,
            url=url,
            source="test",
            published_at="1 hours ago",
            summary="Taiwan stock market and industry news.",
        )

    def test_rejects_stock_quote_and_detail_pages(self):
        urls = [
            "https://tw.stock.yahoo.com/quote/2419.TW/news",
            "https://tw.stock.yahoo.com/quote/1785.TWO",
            "https://www.msn.com/zh-tw/money/markets?id=anb7lh&tab=TopGainers",
            "https://hk.finance.yahoo.com/quote/1785.TWO/news",
            "https://tw.stock.yahoo.com/rank/change-up",
            "https://tw.stock.yahoo.com/tw-market",
            "https://histock.tw/stock/2353",
            "https://goodinfo.tw/tw/StockDetail.asp?STOCK_ID=2419",
            "https://www.nstock.tw/2419",
            "https://www.nstock.tw/stock_info?stock_id=5425&status=4",
            "https://finance.biggo.com.tw/quote/2353.TW/news",
            "https://www.cnyes.com/twstock/idx_cashflow.aspx?code=0000O",
            "https://www.cnyes.com/twstock/1785",
            "https://www.cnyes.com/twstock/1785/financials/income",
            "https://goodinfo.tw/tw/StockBzPerformance.asp?STOCK_ID=1785&RPT_CAT=M%5FYEAR",
            "https://statementdog.com/analysis/1785/e-report",
            "https://treelazy.com/stock/1785",
            "https://www.wantgoo.com/stock/calendar/shareholders-meeting-souvenirs/1785/detail",
            "https://www.fugle.tw/ai/1785/EPS",
            "https://pchome.megatime.com.tw/m/stockinfo/sid1785_4_5.html",
            "https://ww2.money-link.com.tw/TWStock/StockNews.aspx?SymId=6182",
            "https://www.ctee.com.tw/market-stock/1785",
        ]
        for url in urls:
            with self.subTest(url=url):
                self.assertTrue(_is_non_article_page(self._item(url)))

    def test_rejects_news_list_and_video_pages(self):
        cases = [
            ("https://www.moneydj.com/KMDJ/News/NewsRealList.aspx?a=MB010000", "即時新聞 - MoneyDJ理財網"),
            ("https://www.ctee.com.tw/stock/matchplay", "台股逐洞賽 - 證券 - 工商時報"),
            ("https://tw.stock.yahoo.com/rank/change-up", "台股漲幅排行 - Yahoo股市"),
            ("https://tw.stock.yahoo.com/tw-market", "台股盤勢 - Yahoo股市"),
            ("https://www.cnyes.com/twstock/idx_cashflow.aspx?code=0000O", "台股台灣店頭市場指數資金流向 - 鉅亨網新聞"),
            ("https://www.sinotrade.com.tw/richclub/Daily_livestream/video/foo", "營收亮眼法人買"),
            ("https://www.youtube.com/watch?v=abc", "台股影音新聞"),
        ]
        for url, title in cases:
            with self.subTest(url=url):
                self.assertTrue(_is_non_article_page(self._item(url, title)))

    def test_rejects_reference_rank_forum_and_api_pages(self):
        cases = [
            ("https://statementdog.com/tags/575", "特用化學品概念股有哪些股票 - 財報狗"),
            ("https://www.my-finance.com.tw/tw/News_detail/2285/foo", "台股概念股有哪些 - MY-Learning 理財通"),
            ("https://www.wantgoo.com/stock/dividend-yield?market=ETF", "台股現金殖利率排行 - 玩股網"),
            ("https://rate.bot.com.tw/xrt?Lang=en-US", "Foreign Exchange Rate, Bank of Taiwan - 匯率"),
            ("https://www.yuanta.com.tw/eYuanta/securities/News/GetAPIList?Type=stockhotnews", "熱門新聞 - 元大證券"),
            ("https://tw.stock.yahoo.com/institutional-trading", "法人進出 - Yahoo股市"),
            ("https://www.cmoney.tw/forum/stock/00878", "國泰永續高股息(00878)走勢與討論- 股市爆料同學會"),
            ("https://tw.stock.yahoo.com/s/otc.php", "上櫃指數即時走勢 - Yahoo股市"),
            ("https://tw.stock.yahoo.com/class-quote?sectorId=22&exchange=TAI", "上市金融業分類行情 - Yahoo股市"),
            ("https://www.ptt.cc/bbs/Stock/M.1780701233.A.7E3.html", "[新聞] 快新聞／星期一慘了！台指期夜盤崩跌3006 - 看板Stock"),
            ("https://vocus.cc/salon/sscc", "全球資產佈局筆記：從台股走向美股 - 方格子"),
        ]
        for url, title in cases:
            with self.subTest(url=url):
                self.assertTrue(_is_non_article_page(self._item(url, title)))

    def test_rejects_financial_media_root_pages(self):
        cases = [
            "https://money.udn.com/",
            "https://news.cnyes.com/",
            "https://www.moneydj.com/",
        ]
        for url in cases:
            with self.subTest(url=url):
                self.assertTrue(_is_non_article_page(self._item(url, "台灣財經新聞首頁")))

    def test_rejects_grounding_redirect_with_domain_only_title(self):
        item = self._item(
            "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQHoWoatzhLb",
            "taiwanlife.com",
        )
        self.assertTrue(_is_non_article_page(item))

    def test_rejects_grounding_redirect_with_home_title(self):
        item = self._item(
            "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQEAfJNpFXAELH",
            "Home - Taiwan Stock Exchange Corporation",
        )
        self.assertTrue(_is_non_article_page(item))

    def test_keeps_real_financial_article_urls(self):
        urls = [
            "https://news.cnyes.com/news/id/6472559",
            "https://tw.stock.yahoo.com/news/taiwan-semiconductor-ai-server-news-114556922.html",
            "https://www.ctee.com.tw/news/20260531700000-430503",
        ]
        for url in urls:
            with self.subTest(url=url):
                self.assertFalse(_is_non_article_page(self._item(url)))


class TaiwanFinanceNewsFilterTests(unittest.TestCase):
    def test_valid_taiwan_finance_news_is_kept(self):
        item = NewsItem(
            id="tw1",
            title="Taiwan semiconductor AI stocks rise",
            url="https://www.cna.com.tw/news/afe/202605210001.aspx",
            source="CNA",
            published_at="1 hours ago",
            summary="Taiwan stock market AI semiconductor companies and electronics supply chain news.",
        )
        self.assertTrue(_is_taiwan_finance_news(item))

    def test_generic_or_dictionary_news_is_filtered(self):
        items = [
            NewsItem(id="bad1", title="LATEST Definition & Meaning", url="https://www.dictionary.com/browse/latest", source="Dictionary", published_at="1 hours ago", summary="dictionary"),
            NewsItem(id="bad2", title="CNN: Breaking News, Latest News and Videos", url="https://www.cnn.com/", source="CNN", published_at="1 hours ago", summary="World news"),
            NewsItem(id="good", title="Taiwan semiconductor AI stocks rise", url="https://www.cna.com.tw/news/afe/202605210001.aspx", source="CNA", published_at="1 hours ago", summary="Taiwan stock market AI semiconductor companies news."),
        ]
        filtered = _filter_taiwan_finance_news(items)
        self.assertEqual([item.id for item in filtered], ["good"])

    def test_generic_english_market_event_pages_are_filtered(self):
        items = [
            NewsItem(
                id="bad1",
                title="Randolph Street Market | 05/24/2026 - Choose Chicago",
                url="https://www.choosechicago.com/event/randolph-street-market/2026-05-24/",
                source="choosechicago.com",
                published_at="in 2 hours",
                summary="The market will feature musical bands, beer, wine and food trucks.",
            ),
            NewsItem(
                id="bad2",
                title="Gay Street Open-Air Market 2026 - Downtown West Chester PA",
                url="https://www.downtownwestchester.com/event/gay-street-open-air-market-2026/2026-05-24/",
                source="downtownwestchester.com",
                published_at="in 2 hours",
                summary="The open-air market returns with local events and restaurants.",
            ),
        ]
        self.assertEqual(_filter_taiwan_finance_news(items), [])

    def test_pure_crypto_market_news_without_taiwan_context_is_filtered(self):
        item = NewsItem(
            id="bad",
            title="Crypto market sees bullish trend with 307 tokens rising on May 24, 2026",
            url="https://aimsfx.com/2026/05/24/crypto-market-sees-bullish-trend-with-307-tokens-rising-on-may-24-2026/",
            source="aimsfx.com",
            published_at="1 hour ago",
            summary="Daily crypto market summary with top 10 gainers and USDT tokens.",
        )
        self.assertFalse(_is_taiwan_finance_news(item))

    def test_generic_english_ai_pages_without_taiwan_context_are_filtered(self):
        items = [
            NewsItem(
                id="bad1",
                title="Yesterday's Marketing Technology & AI News | May 23, 2026",
                url="https://agilebrandguide.com/yesterdays-marketing-technology-ai-news-may-23-2026/",
                source="agilebrandguide.com",
                published_at="1 day ago",
                summary="Marketing technology and AI news without Taiwan stock market relevance.",
            ),
            NewsItem(
                id="bad2",
                title="Senior Product Manager (Supply Chain Management) - Coupang",
                url="https://bebee.com/tw/jobs/senior-product-manager-supply-chain-management-coupang-taipei-taipei-city",
                source="bebee.com",
                published_at="1 day ago",
                summary="Job listing for supply chain management in Taipei.",
            ),
        ]
        self.assertEqual(_filter_taiwan_finance_news(items), [])

    def test_readmo_taiwan_investment_article_is_kept(self):
        item = NewsItem(
            id="good",
            title="Readmo.ai - 投資網誌",
            url="https://readmo.cmoney.tw/article/00f1c94a-221c-4932-9870-d0219debf299",
            source="readmo.cmoney.tw",
            published_at="1 day ago",
            summary="凱美AI供應鏈題材，伺服器、工控到高階電源。台股投資人關注法人買盤與月營收。",
        )
        self.assertTrue(_is_taiwan_finance_news(item))

class NewsTitleCleanupTests(unittest.TestCase):
    def test_normalize_generic_readmo_title_from_markdown_h1(self):
        text = (
            "![CMoney](/_nuxt/logo.svg)\n\n"
            "# 凱美AI供應鏈題材有多強？從伺服器、工控到高階電源看它站在哪個位置\n\n"
            "Answer / Powered by Readmo.ai"
        )
        self.assertEqual(
            _normalize_news_title("Readmo.ai - 投資網誌", text),
            "凱美AI供應鏈題材有多強？從伺服器、工控到高階電源看它站在哪個位置",
        )

    def test_normalize_keeps_specific_title(self):
        self.assertEqual(
            _normalize_news_title("台股AI供應鏈新聞", "# 其他標題"),
            "台股AI供應鏈新聞",
        )


class WebFetchDateExtractionTests(unittest.TestCase):
    def _fetch_fixture(self, url: str, html: str) -> WebFetchResult:
        class Response:
            headers = {"content-type": "text/html; charset=utf-8"}
            text = html

            def __init__(self, response_url: str):
                self.url = response_url

            def raise_for_status(self):
                return None

        with patch("research_center.web_fetch_service.requests.get", return_value=Response(url)):
            return fetch_web_content(url)

    def test_fetch_web_content_extracts_article_published_time_meta(self):
        class Response:
            headers = {"content-type": "text/html; charset=utf-8"}
            text = """
            <html><head>
              <title>台股 AI 供應鏈新聞</title>
              <meta property="article:published_time" content="2026-06-15T09:10:00+08:00">
            </head><body><article>
              <p>台股 AI 半導體 供應鏈 今日新聞。</p>
              <p>{}</p>
            </article></body></html>
            """.format("台股" * 400)

            def raise_for_status(self):
                return None

        with patch("research_center.web_fetch_service.requests.get", return_value=Response()):
            result = fetch_web_content("https://www.cna.com.tw/news/afe/202606150001.aspx")

        self.assertEqual(result.published_date, "2026-06-15")
        self.assertEqual(result.content_status, "success")

    def test_fetch_web_content_extracts_chinese_publish_time_text(self):
        class Response:
            headers = {"content-type": "text/html; charset=utf-8"}
            text = """
            <html><head><title>台股供應鏈新聞</title></head>
            <body><article>
              <p>發布時間：2026年6月15日 09:10</p>
              <p>{}</p>
            </article></body></html>
            """.format("台股半導體供應鏈" * 200)

            def raise_for_status(self):
                return None

        with patch("research_center.web_fetch_service.requests.get", return_value=Response()):
            result = fetch_web_content("https://news.cnyes.com/news/id/123")

        self.assertEqual(result.published_date, "2026-06-15")

    def test_fetch_web_content_extracts_site_specific_dates_for_taiwan_finance_sources(self):
        cases = [
            ("https://money.udn.com/money/story/5607/9536629", "發布時間 2026/06/15 09:10"),
            ("https://news.cnyes.com/news/id/6479266", "鉅亨網新聞中心 2026/06/15 10:20"),
            ("https://m.cnyes.com/news/id/6479266", "更新時間 2026/06/15 10:25"),
            ("https://tw.stock.yahoo.com/news/example-024541110.html", "發布時間 2026/06/15 11:30"),
            ("https://www.ctee.com.tw/news/20260615700001-430503", "刊登日期 2026/06/15 12:40"),
            ("https://technews.tw/2026/06/15/ai-stock/", "上架時間 2026/06/15 13:50"),
            ("https://www.moneydj.com/kmdj/news/newsviewer.aspx?a=abc", "MoneyDJ 2026/06/15 14:00"),
            ("https://www.moneyweekly.com.tw/ArticleData/Info/Article/230481", "日期 2026/06/15 15:10"),
        ]
        for url, date_text in cases:
            with self.subTest(url=url):
                html = f"""
                <html><head><title>Taiwan finance news</title></head>
                <body><article><p>{date_text}</p><p>{"Taiwan stock market " * 80}</p></article></body></html>
                """
                result = self._fetch_fixture(url, html)
                self.assertEqual(result.published_date, "2026-06-15")

    def test_fetch_web_content_extracts_site_specific_url_date_when_page_has_no_date(self):
        html = """
        <html><head><title>Taiwan finance news</title></head>
        <body><article><p>{}</p></article></body></html>
        """.format("Taiwan stock market " * 80)
        result = self._fetch_fixture("https://technews.tw/2026/06/15/ai-supply-chain/", html)
        self.assertEqual(result.published_date, "2026-06-15")

    def test_fetch_web_content_does_not_invent_date_for_site_without_date(self):
        html = """
        <html><head><title>Taiwan finance news</title></head>
        <body><article><p>{}</p></article></body></html>
        """.format("Taiwan stock market " * 80)
        result = self._fetch_fixture("https://money.udn.com/money/story/5607/no-date", html)
        self.assertIsNone(result.published_date)


class NewsQueryDisplayFilterTests(unittest.TestCase):
    class FakeRepository:
        def __init__(self, items: list[NewsItem], preferences=None):
            self.items = items
            self.deleted_urls: list[str] = []
            self.preferences = preferences or []

        def query_recent(self, hours=24):
            return list(self.items)

        def query_all_recent(self, hours=168):
            return list(self.items)

        def query_by_topic(self, topic, hours=168):
            return []

        def delete_by_urls(self, urls):
            self.deleted_urls.extend(urls)
            return len(urls)

        def list_preferences(self, limit=300):
            return list(self.preferences[:limit])

    def _valid_item(self):
        return NewsItem(
            id="tw1",
            title="Taiwan semiconductor AI stocks rise",
            url="https://www.cna.com.tw/news/afe/202605210001.aspx",
            source="CNA",
            published_at="1 hours ago",
            category="AI / 半導體",
            summary="Taiwan stock market AI semiconductor companies and electronics supply chain news.",
            importance_score=90,
        )

    def _invalid_items(self):
        return [
            NewsItem(id="bad1", title="LATEST Definition & Meaning - Dictionary.com", url="https://www.dictionary.com/browse/latest", source="Dictionary", published_at="1 hours ago", summary="The word latest means most recent."),
            NewsItem(id="bad2", title="CNN: Breaking News, Latest News and Videos", url="https://www.cnn.com/", source="CNN", published_at="1 hours ago", summary="Breaking news today for U.S. and world."),
            NewsItem(id="bad3", title="Yahoo stock news landing", url="https://tw.stock.yahoo.com/news", source="Yahoo", published_at="1 hours ago", summary="Yahoo stock news listing page."),
        ]

    def test_run_news_latest_filters_invalid_stored_news_without_deleting(self):
        repo = self.FakeRepository([self._valid_item(), *self._invalid_items()])
        digests = run_news_latest(repo)
        titles = [item.title for digest in digests for item in digest.items]
        self.assertEqual(titles, ["Taiwan semiconductor AI stocks rise"])
        self.assertEqual(repo.deleted_urls, [])

    def test_run_news_latest_hides_english_only_display_sources(self):
        english_item = NewsItem(
            id="en1",
            title="AI demand and chip investment lift Taiwan exports and business investment",
            url="https://www.digitimes.com/news/a20260529PD203/taiwan-outlook-demand-business-investment.html",
            source="DIGITIMES",
            published_at="1 hours ago",
            category="macro_policy",
            summary="Taiwan stock market and semiconductor industry news.",
            importance_score=95,
        )
        chinese_item = NewsItem(
            id="tw2",
            title="台股AI供應鏈資金輪動 法人看好半導體族群",
            url="https://www.cna.com.tw/news/afe/202605210002.aspx",
            source="CNA",
            published_at="1 hours ago",
            category="ai_semiconductor",
            summary="台股、AI、半導體、供應鏈與法人買盤新聞。",
            importance_score=90,
        )
        self.assertTrue(_is_taiwan_finance_news(english_item))
        repo = self.FakeRepository([english_item, chinese_item])

        titles = [item.title for digest in run_news_latest(repo) for item in digest.items]

        self.assertNotIn(english_item.title, titles)
        self.assertIn(chinese_item.title, titles)

    def test_run_news_latest_includes_empty_display_categories(self):
        repo = self.FakeRepository([self._valid_item()])
        digests = run_news_latest(repo)
        categories = [digest.category for digest in digests]

        for category in ordered_news_category_keys():
            self.assertIn(category, categories)
        self.assertEqual(categories[-1], "holdings")

    def test_run_news_7d_filters_invalid_stored_news(self):
        repo = self.FakeRepository([self._valid_item(), *self._invalid_items()])
        digests = run_news_7d(repo)
        urls = [item.url for digest in digests for item in digest.items]
        self.assertEqual(urls, ["https://www.cna.com.tw/news/afe/202605210001.aspx"])

    def test_run_news_latest_appends_holding_digest_section(self):
        repo = self.FakeRepository([self._valid_item(), *self._invalid_items()])
        digests = run_news_latest(repo, {"2330": "台積電"})
        self.assertGreaterEqual(len(digests), 1)
        self.assertEqual(digests[-1].items, [])

    def test_run_news_latest_does_not_use_large_related_symbols_as_holdings(self):
        item = NewsItem(
            id="many_symbols",
            title="Taiwan Biostar 2399 stock rallies on IPC transition story",
            url="https://www.cna.com.tw/news/afe/202605210031.aspx",
            source="CNA",
            published_at="1 hours ago",
            category="other",
            related_symbols=["1608", "2305", "2353", "2399", "2405", "6770", "8110"],
            related_topics=["最近掃描:精選選股_20260522_063652", "映泰", "力積電"],
            summary="Taiwan stock market company news about Biostar transformation.",
            importance_score=85,
        )
        repo = self.FakeRepository([item])
        digests = run_news_latest(repo, {"2353": "Acer"})

        holding_digest = digests[-1]
        self.assertEqual(holding_digest.category, "holdings")
        self.assertEqual(holding_digest.items, [])

    def test_run_news_latest_matches_holdings_from_article_text(self):
        item = NewsItem(
            id="acer",
            title="Taiwan Acer 2353 revenue beats estimates as foreign broker upgrades target price",
            url="https://www.cna.com.tw/news/afe/202605210032.aspx",
            source="CNA",
            published_at="1 hours ago",
            category="company_news",
            summary="Taiwan stock market company news about Acer revenue and target price upgrade.",
            importance_score=85,
        )
        repo = self.FakeRepository([item])
        digests = run_news_latest(repo, {"2353": "Acer"})

        holding_digest = digests[-1]
        self.assertEqual([news.id for news in holding_digest.items], ["acer"])

    def test_run_news_latest_moves_related_symbol_holdings_out_of_general_categories(self):
        item = NewsItem(
            id="acer_symbol",
            title="Taiwan PC sector rallies as notebook brands attract fund flow",
            url="https://www.cna.com.tw/news/afe/202605210033.aspx",
            source="CNA",
            published_at="1 hours ago",
            category="market_focus",
            related_symbols=["2353"],
            summary="Taiwan stock market sector rotation and PC supply chain news.",
            importance_score=85,
        )
        repo = self.FakeRepository([item])
        digests = run_news_latest(repo, {"2353": "Acer"})

        market_ids = [news.id for digest in digests[:-1] for news in digest.items]
        holding_ids = [news.id for news in digests[-1].items]
        self.assertIn("acer_symbol", market_ids)
        self.assertEqual(holding_ids, [])

    def test_run_news_latest_moves_company_related_symbol_holdings_out_of_general_categories(self):
        item = NewsItem(
            id="acer_symbol_company",
            title="Taiwan notebook brand revenue beats estimates",
            url="https://www.cna.com.tw/news/afe/202605210133.aspx",
            source="CNA",
            published_at="1 hours ago",
            category="company_news",
            related_symbols=["2353"],
            summary="Taiwan stock market company revenue and foreign broker target price news.",
            importance_score=85,
        )
        repo = self.FakeRepository([item])
        digests = run_news_latest(repo, {"2353": "Acer"})

        market_ids = [news.id for digest in digests[:-1] for news in digest.items]
        holding_ids = [news.id for news in digests[-1].items]
        self.assertNotIn("acer_symbol_company", market_ids)
        self.assertEqual(holding_ids, ["acer_symbol_company"])

    def test_run_news_latest_matches_holdings_from_related_topics(self):
        item = NewsItem(
            id="acer_topic",
            title="Taiwan PC supply chain company revenue beats estimates",
            url="https://www.cna.com.tw/news/afe/202605210034.aspx",
            source="CNA",
            published_at="1 hours ago",
            category="company_news",
            related_topics=["Acer"],
            summary="Taiwan stock market company revenue and target price news.",
            importance_score=85,
        )
        repo = self.FakeRepository([item])
        digests = run_news_latest(repo, {"2353": "Acer"})

        self.assertEqual([news.id for news in digests[-1].items], ["acer_topic"])

    def test_run_news_latest_prefers_saved_news_type_after_filtering(self):
        regular_supply = NewsItem(
            id="s0",
            title="Taiwan electronics industry company update",
            url="https://www.cna.com.tw/news/afe/202605210002.aspx",
            source="CNA",
            published_at="1 hours ago",
            category="supply_chain",
            summary="Taiwan stock market electronics company news.",
            importance_score=80,
        )
        supply_chain = NewsItem(
            id="s1",
            title="Taiwan MLCC supply chain shortage benefits power management stocks",
            url="https://www.cna.com.tw/news/afe/202605210003.aspx",
            source="CNA",
            published_at="1 hours ago",
            category="supply_chain",
            summary="Taiwan stock market supply chain shortage and MLCC price quote news.",
            importance_score=55,
        )
        preferences = [
            SimpleNamespace(news_type="supply_chain_benefit", normalized_category="supply_chain", source="CNA", weight=1)
            for _ in range(5)
        ]
        repo = self.FakeRepository([regular_supply, supply_chain], preferences=preferences)
        digests = run_news_latest(repo)
        supply_digest = [digest for digest in digests if digest.category == "supply_chain"][0]
        self.assertEqual(supply_digest.items[0].id, "s1")
        self.assertGreater(supply_digest.items[0].importance_score, 55)

    def test_preferences_do_not_bypass_taiwan_filter(self):
        invalid = NewsItem(
            id="bad1",
            title="CNN: Breaking News, Latest News and Videos",
            url="https://www.cnn.com/",
            source="CNN",
            published_at="1 hours ago",
            category="supply_chain",
            summary="Breaking news today for U.S. and world.",
            importance_score=1,
        )
        preferences = [
            SimpleNamespace(news_type="supply_chain_benefit", normalized_category="supply_chain", source="CNN", weight=10)
        ]
        repo = self.FakeRepository([invalid], preferences=preferences)
        digests = run_news_latest(repo)
        self.assertEqual([item for digest in digests for item in digest.items], [])

    def test_run_news_latest_replaces_generic_readmo_title_from_h1(self):
        item = NewsItem(
            id="944",
            title="Readmo.ai - 投資網誌",
            url="https://readmo.cmoney.tw/article/00f1c94a-221c-4932-9870-d0219debf299",
            source="readmo.cmoney.tw",
            published_at="1 hours ago",
            category="theme_radar",
            summary=(
                "![CMoney](/_nuxt/logo.svg)\n\n"
                "# 凱美AI供應鏈題材有多強？從伺服器、工控到高階電源看它站在哪個位置\n\n"
                "台股 AI 供應鏈 凱美 伺服器 高階電源"
            ),
            importance_score=80,
        )
        repo = self.FakeRepository([item])
        digests = run_news_latest(repo)
        titles = [news.title for digest in digests for news in digest.items]
        self.assertIn(
            "凱美AI供應鏈題材有多強？從伺服器、工控到高階電源看它站在哪個位置",
            titles,
        )
        self.assertNotIn("Readmo.ai - 投資網誌", titles)


    def test_run_news_latest_filters_share_google_redirect_urls(self):
        item = NewsItem(
            id="redir",
            title="Taiwan passive components stocks rally on AI server demand",
            url="https://share.google/RvskaB1TYEoy5RURa",
            source="tavily_extract",
            published_at="1 hours ago",
            category="supply_chain",
            summary="Taiwan stock market passive components and AI server supply chain news.",
            importance_score=95,
        )
        repo = self.FakeRepository([item, self._valid_item()])
        urls = [news.url for digest in run_news_latest(repo) for news in digest.items]
        self.assertNotIn("https://share.google/RvskaB1TYEoy5RURa", urls)
        self.assertIn("https://www.cna.com.tw/news/afe/202605210001.aspx", urls)

    def test_run_news_latest_filters_moneydj_internal_list_pages(self):
        item = NewsItem(
            id="dj",
            title="NVKMDJ新聞內文-{9AF75A9D-5C31-4C7F-9110-9CD3FD75C04E}",
            url="https://taishinlife.moneydj.com/ETFData/djhtm/ETKMDJNEWSContentRwd.djhtm?type=list&svc=NV&a=abc",
            source="taishinlife.moneydj.com",
            published_at="1 hours ago",
            category="macro_policy",
            summary="Taiwan stock market ETF finance news.",
            importance_score=90,
        )
        repo = self.FakeRepository([item, self._valid_item()])
        titles = [news.title for digest in run_news_latest(repo) for news in digest.items]
        self.assertNotIn(item.title, titles)
        self.assertEqual(titles, ["Taiwan semiconductor AI stocks rise"])

    def test_run_news_latest_filters_global_market_only_news(self):
        item = NewsItem(
            id="global",
            title="《美股》S&P 500 and Nasdaq rise as Wall Street awaits Fed decision",
            url="https://tw.stock.yahoo.com/news/us-stocks-sp500-nasdaq-fed-001122334.html",
            source="Yahoo股市",
            published_at="1 hours ago",
            category="macro_policy",
            summary="US stocks and global market news without Taiwan market impact.",
            importance_score=90,
        )
        repo = self.FakeRepository([item, self._valid_item()])
        titles = [news.title for digest in run_news_latest(repo) for news in digest.items]
        self.assertNotIn(item.title, titles)
        self.assertEqual(titles, ["Taiwan semiconductor AI stocks rise"])

    def test_run_news_latest_keeps_global_news_with_taiwan_market_link(self):
        item = NewsItem(
            id="global_tw",
            title="美股AI供應鏈走強帶動台積電ADR與台股半導體族群",
            url="https://tw.stock.yahoo.com/news/taiwan-stocks-ai-semiconductor-001122334.html",
            source="Yahoo股市",
            published_at="1 hours ago",
            category="macro_policy",
            summary="US stocks AI supply chain news with Taiwan stock market and TSMC impact.",
            importance_score=90,
        )
        repo = self.FakeRepository([item])
        titles = [news.title for digest in run_news_latest(repo) for news in digest.items]
        self.assertEqual(titles, [item.title])

    def test_run_news_latest_uses_created_at_when_published_at_is_blank(self):
        item = NewsItem(
            id="blank_published",
            title="Taiwan passive components stocks rally on AI server demand",
            url="https://www.cna.com.tw/news/afe/202605210071.aspx",
            source="CNA",
            published_at="",
            category="sector_rotation",
            summary="Taiwan stock market passive components and AI server supply chain news.",
            news_origin="refresh",
            created_at=datetime.now().isoformat(timespec="seconds"),
            importance_score=90,
        )
        repo = self.FakeRepository([item])
        titles = [news.title for digest in run_news_latest(repo) for news in digest.items]
        self.assertEqual(titles, [item.title])

    def test_run_news_scheduled_latest_uses_created_at_for_trusted_refresh_news(self):
        item = NewsItem(
            id="blank_published_scheduled",
            title="Taiwan passive components stocks rally on AI server demand",
            url="https://www.cna.com.tw/news/afe/202605210171.aspx",
            source="CNA",
            published_at="",
            category="sector_rotation",
            summary="Taiwan stock market passive components and AI server supply chain news.",
            news_origin="refresh",
            created_at=datetime.now().isoformat(timespec="seconds"),
            importance_score=90,
        )
        repo = self.FakeRepository([item])
        titles = [news.title for digest in run_news_scheduled_latest(repo) for news in digest.items]
        self.assertEqual(titles, [item.title])

    def test_run_news_scheduled_latest_rejects_untrusted_created_at_fallback(self):
        item = NewsItem(
            id="blank_untrusted_scheduled",
            title="Taiwan passive components stocks rally on AI server demand",
            url="https://example.com/random-blog",
            source="Unknown",
            published_at="",
            category="sector_rotation",
            summary="Taiwan stock market passive components and AI server supply chain news.",
            news_origin="refresh",
            created_at=datetime.now().isoformat(timespec="seconds"),
            importance_score=90,
        )
        repo = self.FakeRepository([item])
        titles = [news.title for digest in run_news_scheduled_latest(repo) for news in digest.items]
        self.assertEqual(titles, [])

    def test_scheduled_news_diagnostics_counts_missing_and_displayed_dates(self):
        trusted_blank = NewsItem(
            id="trusted_blank",
            title="Taiwan passive components stocks rally on AI server demand",
            url="https://www.cna.com.tw/news/afe/202605210172.aspx",
            source="CNA",
            published_at="",
            category="sector_rotation",
            summary="Taiwan stock market passive components and AI server supply chain news.",
            news_origin="refresh",
            created_at=datetime.now().isoformat(timespec="seconds"),
            importance_score=90,
        )
        explicit = NewsItem(
            id="explicit_date",
            title="Taiwan semiconductor AI stocks rise on new orders",
            url="https://www.cna.com.tw/news/afe/202605210173.aspx",
            source="CNA",
            published_at="1 hours ago",
            category="ai_semiconductor",
            summary="Taiwan stock market AI semiconductor companies news.",
            news_origin="refresh",
            importance_score=80,
        )
        repo = self.FakeRepository([trusted_blank, explicit])
        diagnostics = build_scheduled_news_diagnostics(repo)
        self.assertEqual(diagnostics["raw48"], 2)
        self.assertEqual(diagnostics["explicit_dates"], 1)
        self.assertEqual(diagnostics["missing_dates"], 1)
        self.assertEqual(diagnostics["display_total"], 2)

    def test_scheduled_lightweight_refresh_categories_when_primary_is_thin(self):
        diagnostics = {
            "primary24": 3,
            "display_categories": {
                "market_focus": 4,
                "ai_semiconductor": 2,
                "company_news": 1,
            },
        }
        categories = scheduled_news_lightweight_refresh_categories(diagnostics)
        self.assertEqual(categories[:3], ["sector_rotation", "supply_chain", "macro_policy"])

    def test_scheduled_lightweight_refresh_runs_once_with_limited_env(self):
        diagnostics = {
            "primary24": 3,
            "display_categories": {
                "market_focus": 4,
                "ai_semiconductor": 2,
                "company_news": 1,
            },
        }
        captured_env: dict[str, str | None] = {}

        def fake_refresh(center, repository, progress=None, ai_model="gemini"):
            for key in (
                "NEWS_REFRESH_TASK_CATEGORIES",
                "NEWS_REFRESH_MAX_SOURCES",
                "NEWS_REFRESH_WEBFETCH_MAX_URLS",
                "NEWS_REFRESH_CLASSIFY_LIMIT",
            ):
                captured_env[key] = os.environ.get(key)
            return [], {"saved": 1, "skipped": 2}

        with patch("research_center.news_service.run_news_refresh", side_effect=fake_refresh):
            _, meta = run_scheduled_news_lightweight_refresh(
                SimpleNamespace(),
                self.FakeRepository([]),
                progress=None,
                ai_model="minimax",
                diagnostics=diagnostics,
            )

        self.assertTrue(meta["ran"])
        self.assertEqual(captured_env["NEWS_REFRESH_MAX_SOURCES"], "24")
        self.assertEqual(captured_env["NEWS_REFRESH_WEBFETCH_MAX_URLS"], "6")
        self.assertEqual(captured_env["NEWS_REFRESH_CLASSIFY_LIMIT"], "8")
        self.assertIn("sector_rotation", captured_env["NEWS_REFRESH_TASK_CATEGORIES"] or "")
        self.assertIsNone(os.environ.get("NEWS_REFRESH_TASK_CATEGORIES"))

    def test_backfill_refresh_item_dates_uses_relative_time_from_summary(self):
        item = NewsItem(
            id="relative",
            title="Taiwan semiconductor AI stocks rise",
            url="https://www.cna.com.tw/news/afe/202605210181.aspx",
            source="CNA",
            published_at="",
            category="ai_semiconductor",
            summary="Taiwan stock market AI semiconductor news published 7 hours ago.",
            news_origin="refresh",
            importance_score=80,
        )
        _backfill_refresh_item_dates([item])
        self.assertRegex(item.published_at, r"^20\d{2}-\d{2}-\d{2}$")
        self.assertTrue(item.created_at)

    def test_backfill_refresh_item_dates_does_not_change_manual_news(self):
        item = NewsItem(
            id="manual",
            title="Taiwan semiconductor AI stocks rise",
            url="https://www.cna.com.tw/news/afe/202605210182.aspx",
            source="CNA",
            published_at="",
            category="ai_semiconductor",
            summary="published 7 hours ago",
            news_origin="manual",
        )
        _backfill_refresh_item_dates([item])
        self.assertEqual(item.published_at, "")

    def test_run_news_latest_ranks_explicit_date_above_blank_published_at(self):
        explicit = NewsItem(
            id="explicit",
            title="Taiwan semiconductor AI stocks rise on new orders",
            url="https://www.cna.com.tw/news/afe/202605210081.aspx",
            source="CNA",
            published_at="1 hours ago",
            category="ai_semiconductor",
            summary="Taiwan stock market AI semiconductor companies news.",
            news_origin="refresh",
            created_at=datetime.now().isoformat(timespec="seconds"),
            importance_score=80,
        )
        blank = NewsItem(
            id="blank",
            title="Taiwan passive components stocks rally on AI server demand",
            url="https://www.cna.com.tw/news/afe/202605210082.aspx",
            source="CNA",
            published_at="",
            category="ai_semiconductor",
            summary="Taiwan stock market AI semiconductor companies news.",
            news_origin="refresh",
            created_at=datetime.now().isoformat(timespec="seconds"),
            importance_score=200,
        )
        repo = self.FakeRepository([blank, explicit])
        ai_digest = [digest for digest in run_news_latest(repo) if digest.category == "ai_semiconductor"][0]
        self.assertEqual([news.id for news in ai_digest.items], ["explicit", "blank"])

    def test_run_news_latest_does_not_use_old_created_at_for_blank_published_at(self):
        item = NewsItem(
            id="old_blank_published",
            title="Taiwan passive components stocks rally on AI server demand",
            url="https://www.cna.com.tw/news/afe/202605210072.aspx",
            source="CNA",
            published_at="",
            category="sector_rotation",
            summary="Taiwan stock market passive components and AI server supply chain news.",
            created_at=(datetime.now() - timedelta(days=3)).isoformat(timespec="seconds"),
            importance_score=90,
        )
        repo = self.FakeRepository([item])
        titles = [news.title for digest in run_news_latest(repo) for news in digest.items]
        self.assertEqual(titles, [])

    def test_run_news_latest_uses_48_hour_fallback(self):
        item = NewsItem(
            id="fallback_48h",
            title="Taiwan passive components stocks rally on AI server demand",
            url="https://www.cna.com.tw/news/afe/202605210076.aspx",
            source="CNA",
            published_at="36 hours ago",
            category="sector_rotation",
            summary="Taiwan stock market passive components and AI server supply chain news.",
            news_origin="refresh",
            created_at=datetime.now().isoformat(timespec="seconds"),
            importance_score=90,
        )
        repo = self.FakeRepository([item])
        titles = [news.title for digest in run_news_latest(repo) for news in digest.items]
        self.assertEqual(titles, [item.title])

    def test_run_news_scheduled_latest_uses_48_hour_explicit_fallback(self):
        item = NewsItem(
            id="scheduled_fallback_48h",
            title="Taiwan passive components stocks rally on AI server demand",
            url="https://www.cna.com.tw/news/afe/202605210276.aspx",
            source="CNA",
            published_at="36 hours ago",
            category="sector_rotation",
            summary="Taiwan stock market passive components and AI server supply chain news.",
            news_origin="refresh",
            created_at=datetime.now().isoformat(timespec="seconds"),
            importance_score=90,
        )
        repo = self.FakeRepository([item])
        titles = [news.title for digest in run_news_scheduled_latest(repo) for news in digest.items]
        self.assertEqual(titles, [item.title])

    def test_display_source_penalty_demotes_cmoney_below_mainstream_news(self):
        cmoney = NewsItem(
            id="cmoney",
            title="AI 與新能源車帶動特殊銅材成長，能否從題材走向體質？",
            url="https://readmo.cmoney.tw/article/example",
            source="CMoney",
            published_at="1 hours ago",
            category="ai_semiconductor",
            summary="Taiwan stock market AI semiconductor supply chain news.",
            news_origin="refresh",
            importance_score=260,
        )
        mainstream = NewsItem(
            id="mainstream",
            title="台股台積電供應鏈受惠AI需求升溫",
            url="https://money.udn.com/money/story/5607/9999999",
            source="經濟日報",
            published_at="1 hours ago",
            category="ai_semiconductor",
            summary="台股 AI 半導體 供應鏈 新聞，Taiwan stock market AI semiconductor supply chain news.",
            news_origin="refresh",
            importance_score=120,
        )
        _apply_display_source_penalties([cmoney, mainstream])
        self.assertLess(cmoney.importance_score, mainstream.importance_score)

    def test_run_news_latest_prefers_24h_explicit_when_enough_items(self):
        recent_items = [
            NewsItem(
                id=f"recent_{idx}",
                title=f"Taiwan semiconductor AI stock news {idx}",
                url=f"https://www.cna.com.tw/news/afe/20260521{idx:04d}.aspx",
                source="CNA",
                published_at="1 hours ago",
                category="ai_semiconductor",
                summary="Taiwan stock market AI semiconductor companies news.",
                news_origin="refresh",
                importance_score=50,
            )
            for idx in range(20)
        ]
        older = NewsItem(
            id="fallback_high_score",
            title="Taiwan passive components stocks rally on AI server demand",
            url="https://www.cna.com.tw/news/afe/202605219999.aspx",
            source="CNA",
            published_at="36 hours ago",
            category="sector_rotation",
            summary="Taiwan stock market passive components and AI server supply chain news.",
            news_origin="refresh",
            importance_score=999,
        )
        repo = self.FakeRepository([older, *recent_items])
        ids = [news.id for digest in run_news_latest(repo) for news in digest.items]
        self.assertNotIn("fallback_high_score", ids)
        self.assertEqual(len(ids), 20)

    def test_run_news_latest_excludes_two_days_ago_from_latest(self):
        item = NewsItem(
            id="two_days",
            title="Taiwan passive components stocks rally on AI server demand",
            url="https://www.cna.com.tw/news/afe/202605210176.aspx",
            source="CNA",
            published_at="2 days ago",
            category="sector_rotation",
            summary="Taiwan stock market passive components and AI server supply chain news.",
            news_origin="refresh",
            created_at=datetime.now().isoformat(timespec="seconds"),
            importance_score=90,
        )
        repo = self.FakeRepository([item])
        titles = [news.title for digest in run_news_latest(repo) for news in digest.items]
        self.assertEqual(titles, [])

    def test_run_news_latest_excludes_blank_published_with_old_embedded_date(self):
        item = NewsItem(
            id="old_embedded_date",
            title="Taiwan passive components stocks rally on AI server demand",
            url="https://www.cna.com.tw/news/afe/202605210077.aspx",
            source="CNA",
            published_at="",
            category="sector_rotation",
            summary="MoneyDJ新聞 2026-04-28 11:03:42 發佈 Taiwan stock market passive components news.",
            full_text="MoneyDJ新聞 2026-04-28 11:03:42 發佈 Taiwan AI server supply chain news.",
            news_origin="refresh",
            created_at=datetime.now().isoformat(timespec="seconds"),
            importance_score=90,
        )
        repo = self.FakeRepository([item])
        titles = [news.title for digest in run_news_latest(repo) for news in digest.items]
        self.assertEqual(titles, [])

    def test_run_news_latest_excludes_blank_published_with_old_chinese_date(self):
        item = NewsItem(
            id="old_chinese_date",
            title="台股半導體供應鏈新聞",
            url="https://www.cna.com.tw/news/afe/202605210078.aspx",
            source="CNA",
            published_at="",
            category="ai_semiconductor",
            summary="本文發布於 2026年4月28日，說明 Taiwan stock market AI semiconductor companies news.",
            full_text="2026年4月28日 台股半導體與AI供應鏈新聞。",
            news_origin="refresh",
            created_at=datetime.now().isoformat(timespec="seconds"),
            importance_score=90,
        )
        repo = self.FakeRepository([item])
        titles = [news.title for digest in run_news_latest(repo) for news in digest.items]
        self.assertEqual(titles, [])

    def test_run_news_latest_excludes_manual_news_even_when_recent(self):
        item = NewsItem(
            id="manual",
            title="Taiwan passive components stocks rally on AI server demand",
            url="https://www.cna.com.tw/news/afe/202605210074.aspx",
            source="CNA",
            published_at="1 hours ago",
            category="sector_rotation",
            summary="Taiwan stock market passive components and AI server supply chain news.",
            news_origin="manual",
            created_at=datetime.now().isoformat(timespec="seconds"),
            importance_score=90,
        )
        repo = self.FakeRepository([item])
        titles = [news.title for digest in run_news_latest(repo) for news in digest.items]
        self.assertEqual(titles, [])

    def test_run_news_latest_excludes_research_news_with_blank_published_at(self):
        item = NewsItem(
            id="research",
            title="Taiwan passive components stocks rally on AI server demand",
            url="https://www.cna.com.tw/news/afe/202605210075.aspx",
            source="CNA",
            published_at="",
            category="sector_rotation",
            summary="Taiwan stock market passive components and AI server supply chain news.",
            news_origin="research",
            created_at=datetime.now().isoformat(timespec="seconds"),
            importance_score=90,
        )
        repo = self.FakeRepository([item])
        titles = [news.title for digest in run_news_latest(repo) for news in digest.items]
        self.assertEqual(titles, [])

    def test_run_news_latest_parses_english_month_dates_as_published_dates(self):
        item = NewsItem(
            id="english_month_old",
            title="Taiwan semiconductor AI stocks rise",
            url="https://www.cna.com.tw/news/afe/202605210073.aspx",
            source="CNA",
            published_at="Nov 19, 2025",
            category="ai_semiconductor",
            summary="Taiwan stock market AI semiconductor companies news.",
            created_at=datetime.now().isoformat(timespec="seconds"),
            importance_score=90,
        )
        repo = self.FakeRepository([item])
        titles = [news.title for digest in run_news_latest(repo) for news in digest.items]
        self.assertEqual(titles, [])

    def test_run_news_latest_dedupes_same_title_for_display(self):
        item1 = NewsItem(
            id="dup1",
            title="Taiwan AI semiconductor supply chain stocks rise",
            url="https://www.cna.com.tw/news/afe/202605210011.aspx",
            source="CNA",
            published_at="1 hours ago",
            category="ai_semiconductor",
            summary="Taiwan stock market AI semiconductor companies and electronics supply chain news.",
            importance_score=70,
        )
        item2 = NewsItem(
            id="dup2",
            title="Taiwan AI semiconductor supply chain stocks rise",
            url="https://tw.stock.yahoo.com/news/taiwan-ai-semiconductor-supply-chain-001122334.html",
            source="Yahoo股市",
            published_at="1 hours ago",
            category="ai_semiconductor",
            summary="Taiwan stock market AI semiconductor companies and electronics supply chain news.",
            importance_score=90,
        )
        repo = self.FakeRepository([item1, item2])
        shown = [news for digest in run_news_latest(repo) for news in digest.items]
        self.assertEqual(len(shown), 1)
        self.assertEqual(shown[0].id, "dup2")

    def test_run_news_latest_recategorizes_market_tape_from_macro_policy(self):
        item = NewsItem(
            id="market",
            title="台股早盤大漲逾千點 三大法人買超帶動指數創高",
            url="https://money.udn.com/money/story/5612/9523384",
            source="經濟日報",
            published_at="1 hours ago",
            category="macro_policy",
            summary="台股加權指數早盤大漲，外資與投信買超，成交量放大。",
            importance_score=90,
        )
        repo = self.FakeRepository([item])
        digests = run_news_latest(repo)
        digest = [d for d in digests if d.category == "market_focus"][0]
        self.assertEqual(digest.items[0].id, "market")

    def test_run_news_latest_keeps_market_focus_ahead_of_supply_terms(self):
        item = NewsItem(
            id="market_supply",
            title="台股再創高法人：短線過熱整理更有助後市",
            url="https://money.udn.com/money/story/5607/9536629",
            source="經濟日報",
            published_at="1 hours ago",
            category="supply_chain",
            summary="台股大盤再創高，法人認為短線過熱整理有助後市，AI與供應鏈仍是盤面焦點。",
            importance_score=90,
        )
        repo = self.FakeRepository([item])
        digests = run_news_latest(repo)
        digest = [d for d in digests if d.category == "market_focus"][0]
        self.assertEqual([news.id for news in digest.items], ["market_supply"])

    def test_run_news_latest_keeps_true_macro_policy_news(self):
        item = NewsItem(
            id="macro",
            title="央行關注通膨與新台幣匯率 利率走向下月說分明",
            url="https://money.udn.com/money/story/5613/9515718",
            source="經濟日報",
            published_at="1 hours ago",
            category="macro_policy",
            summary="央行說明通膨、利率與新台幣匯率政策，聚焦貨幣政策與物價展望。",
            importance_score=90,
        )
        repo = self.FakeRepository([item])
        digests = run_news_latest(repo)
        digest = [d for d in digests if d.category == "macro_policy"][0]
        self.assertEqual(digest.items[0].id, "macro")


    def test_run_news_latest_recategorizes_company_event_news(self):
        item = NewsItem(
            id="company_event",
            title="Taiwan Acer revenue beats estimates; foreign broker upgrades target price",
            url="https://www.cna.com.tw/news/afe/202605210041.aspx",
            source="CNA",
            published_at="1 hours ago",
            category="other",
            summary="Taiwan stock market company revenue, earnings and target price upgrade news.",
            importance_score=90,
        )
        repo = self.FakeRepository([item])
        digests = run_news_latest(repo)
        digest = [d for d in digests if d.category == "company_news"][0]
        self.assertEqual([news.id for news in digest.items], ["company_event"])

    def test_run_news_latest_recategorizes_sector_rotation_news(self):
        item = NewsItem(
            id="sector_event",
            title="Taiwan passive components concept stocks rally as multiple MLCC names rise",
            url="https://www.cna.com.tw/news/afe/202605210042.aspx",
            source="CNA",
            published_at="1 hours ago",
            category="market_focus",
            summary="Taiwan stock market sector rotation news: passive components, MLCC and power management stocks rise together.",
            importance_score=90,
        )
        repo = self.FakeRepository([item])
        digests = run_news_latest(repo)
        digest = [d for d in digests if d.category == "sector_rotation"][0]
        self.assertEqual([news.id for news in digest.items], ["sector_event"])

    def test_run_news_latest_recategorizes_supply_chain_news(self):
        item = NewsItem(
            id="supply",
            title="台股PCB與散熱供應鏈訂單升溫 伺服器電源族群受惠",
            url="https://money.udn.com/money/story/5612/9539001",
            source="經濟日報",
            published_at="1 hours ago",
            category="market_focus",
            summary="台股供應鏈新聞，PCB、散熱、電源與伺服器零組件出貨增加。",
            importance_score=90,
        )
        repo = self.FakeRepository([item])
        digests = run_news_latest(repo)
        digest = [d for d in digests if d.category == "supply_chain"][0]
        self.assertEqual([news.id for news in digest.items], ["supply"])

    def test_run_news_latest_recategorizes_sector_basket_headline(self):
        item = NewsItem(
            id="sector_basket",
            title="頻率元件廠股價5月大噴發 Q2業績走旺還有AI及光通訊續加持",
            url="https://news.cnyes.com/news/id/6477884",
            source="鉅亨網",
            published_at="11 hours ago",
            category="market_focus",
            summary="台股族群輪動，頻率元件與光通訊多檔受惠。",
            importance_score=90,
        )
        repo = self.FakeRepository([item])
        digest = [d for d in run_news_latest(repo) if d.category == "sector_rotation"][0]
        self.assertEqual([news.id for news in digest.items], ["sector_basket"])

    def test_run_news_latest_recategorizes_company_headline_even_with_ai_terms(self):
        item = NewsItem(
            id="company_ai",
            title="Taiwan AI server supplier target price raised on stronger earnings",
            url="https://www.cna.com.tw/news/afe/202605210099.aspx",
            source="CNA",
            published_at="6 hours ago",
            category="ai_semiconductor",
            summary="Taiwan stock market company earnings and broker target price upgrade news.",
            importance_score=90,
        )
        repo = self.FakeRepository([item])
        digest = [d for d in run_news_latest(repo) if d.category == "company_news"][0]
        self.assertEqual([news.id for news in digest.items], ["company_ai"])

    def test_run_news_latest_recategorizes_macro_policy_news(self):
        item = NewsItem(
            id="policy",
            title="央行說明新台幣匯率與利率政策 通膨仍是觀察重點",
            url="https://money.udn.com/money/story/5613/9539002",
            source="經濟日報",
            published_at="1 hours ago",
            category="other",
            summary="政策、匯率、利率、通膨與貨幣政策影響台灣財經市場。",
            importance_score=90,
        )
        repo = self.FakeRepository([item])
        digests = run_news_latest(repo)
        digest = [d for d in digests if d.category == "macro_policy"][0]
        self.assertEqual([news.id for news in digest.items], ["policy"])

    def test_run_news_latest_keeps_global_risk_headline_in_macro_policy(self):
        item = NewsItem(
            id="global_risk",
            title="美股財報季結束恐回調 分析師：注意力轉向Fed與中東",
            url="https://news.cnyes.com/news/id/6477833",
            source="鉅亨網",
            published_at="4 hours ago",
            category="company_news",
            summary="台股投資人關注美股財報、Fed與中東風險對市場影響。",
            importance_score=90,
        )
        repo = self.FakeRepository([item])
        digests = run_news_latest(repo)
        digest = [d for d in digests if d.category == "macro_policy"][0]
        self.assertEqual([news.id for news in digest.items], ["global_risk"])

    def test_run_news_latest_recategorizes_clean_market_terms(self):
        item = NewsItem(
            id="clean_market",
            title="6月1日五件財經大事搶先看 台股盤後法人買超",
            url="https://money.udn.com/money/story/5607/9539991",
            source="經濟日報",
            published_at="1 hours ago",
            category="company_news",
            summary="台股 大盤 三大法人 市場焦點。",
            importance_score=90,
        )
        repo = self.FakeRepository([item])
        digest = [d for d in run_news_latest(repo) if d.category == "market_focus"][0]
        self.assertEqual([news.id for news in digest.items], ["clean_market"])

    def test_run_news_latest_recategorizes_clean_supply_chain_terms(self):
        item = NewsItem(
            id="clean_supply",
            title="合約價快速上漲 TrendForce：首季 DRAM 產業營收季增",
            url="https://money.udn.com/money/story/5612/9539992",
            source="經濟日報",
            published_at="1 hours ago",
            category="company_news",
            summary="記憶體 DRAM 供應鏈 材料 價格。",
            importance_score=90,
        )
        repo = self.FakeRepository([item])
        digest = [d for d in run_news_latest(repo) if d.category == "supply_chain"][0]
        self.assertEqual([news.id for news in digest.items], ["clean_supply"])

    def test_run_news_latest_demotes_low_priority_cmoney_sources(self):
        cmoney_item = NewsItem(
            id="cmoney_low",
            title="台股資金結構健康 內外資合力攻擊",
            url="https://readmo.cmoney.tw/article/abc",
            source="readmo.cmoney.tw",
            published_at="1 hours ago",
            category="market_focus",
            summary="台股 大盤 法人 買超。",
            importance_score=180,
        )
        mainstream = NewsItem(
            id="mainstream",
            title="台股再創高 法人提醒短線過熱",
            url="https://money.udn.com/money/story/5607/9539993",
            source="經濟日報",
            published_at="1 hours ago",
            category="market_focus",
            summary="台股 大盤 法人 市場焦點。",
            importance_score=90,
        )
        repo = self.FakeRepository([cmoney_item, mainstream])
        digest = [d for d in run_news_latest(repo) if d.category == "market_focus"][0]
        self.assertEqual([news.id for news in digest.items[:2]], ["mainstream", "cmoney_low"])


class NewsFormatterTests(unittest.TestCase):
    def test_format_news_digest_is_title_first_without_summary(self):
        item = NewsItem(
            id="123",
            title="Taiwan semiconductor AI stocks rise",
            url="https://www.cna.com.tw/news/afe/202605210001.aspx",
            source="CNA",
            published_at="2026-05-21T09:00:00",
            category="AI / 半導體",
            summary="This summary should not appear in list output.",
            importance_score=90,
        )
        digest = format_news_digest([SimpleNamespace(category="AI / 半導體", items=[item])], period_label="今日")
        self.assertIn("/news_detail", digest)
        self.assertIn("Taiwan semiconductor AI stocks rise", digest)
        self.assertNotIn("This summary should not appear", digest)

    def test_format_news_digest_normalizes_internal_categories(self):
        item = NewsItem(
            id="123",
            title="Taiwan stock theme rotation",
            url="https://www.cna.com.tw/news/afe/202605210001.aspx",
            source="CNA",
            published_at="2026-05-21T09:00:00",
            category="sector_strength",
            summary="Taiwan stock market sector rotation news.",
            importance_score=90,
        )
        digest = format_news_digest([SimpleNamespace(category="sector_strength", items=[item])], period_label="今日")
        self.assertIn("題材與族群輪動", digest)
        self.assertNotIn("sector_strength", digest)

    def test_format_news_digest_shows_empty_category_placeholder(self):
        digest = format_news_digest([SimpleNamespace(category="sector_rotation", items=[])], period_label="今日")
        self.assertIn("本期暫無符合新聞", digest)

    def test_format_news_detail_shows_summary(self):
        item = NewsItem(
            id="123",
            title="Taiwan semiconductor AI stocks rise",
            url="https://www.cna.com.tw/news/afe/202605210001.aspx",
            source="CNA",
            published_at="2026-05-21T09:00:00",
            category="AI / 半導體",
            summary="This summary should appear in detail output.",
        )
        detail = format_news_detail(item)
        self.assertIn("N123", detail)
        self.assertIn("This summary should appear", detail)

    def test_format_news_refresh_result_includes_actionable_digest(self):
        item = NewsItem(
            id="123",
            title="台股AI供應鏈新聞",
            url="https://www.cna.com.tw/news/afe/202605210001.aspx",
            source="中央社",
            published_at="2026-05-21T09:00:00",
            category="ai_semiconductor",
            related_symbols=["2330", "2308"],
            related_topics=["AI伺服器", "電源"],
            summary="AI伺服器供應鏈受惠，但需觀察估值與訂單能見度。",
            importance_score=92,
            news_signal_score=80,
            news_heat_risk_score=55,
            news_signal_reason="具備題材催化",
            news_heat_risk_reason="短線熱度偏高",
        )
        text = format_news_refresh_result(
            1,
            2,
            1,
            [item],
            {
                "search_sources": 20,
                "filtered_count": 5,
                "total": 1,
                "webfetch_success": 0,
                "category_counts": {"ai_semiconductor": 1},
            },
        )

        self.assertIn("資料狀態", text)
        self.assertIn("分類分布", text)
        self.assertIn("重點新聞", text)
        self.assertIn("台股AI供應鏈新聞", text)
        self.assertIn("限制：", text)


class NewsAIClassificationBatchTests(unittest.TestCase):
    class FakeGemini:
        def __init__(self):
            self.timeout_seconds = 999
            self.calls: list[str] = []
            self.timeout_during_call: float | None = None

        def generate_report(self, prompt, enable_grounding=False):
            self.calls.append(prompt)
            self.timeout_during_call = self.timeout_seconds
            return SimpleNamespace(raw=json.dumps({
                "0": {"category": "AI / 半導體", "summary": "AI summary 0", "importance_score": 90},
                "1": {"category": "金融股", "summary": "AI summary 1", "importance_score": 80},
            }))

    class FakeMiniMaxLow:
        def __init__(self):
            self.timeout_seconds = 999
            self.calls: list[str] = []

        def is_configured(self):
            return True

        def generate_json(self, prompt):
            self.calls.append(prompt)
            content = json.dumps({
                "0": {"category": "AI / 半導體", "summary": "low summary 0", "importance_score": 55},
                "1": {"category": "金融股", "summary": "low summary 1", "importance_score": 45},
            })
            return SimpleNamespace(raw={"choices": [{"message": {"content": content}}]})

    class FailingOnceGemini(FakeGemini):
        def generate_report(self, prompt, enable_grounding=False):
            self.calls.append(prompt)
            if len(self.calls) == 1:
                raise TimeoutError("simulated timeout")
            return SimpleNamespace(raw=json.dumps({
                "0": {"category": "AI / 半導體", "summary": "second batch", "importance_score": 70},
            }))

    def _items(self, count: int) -> list[NewsItem]:
        return [
            NewsItem(
                id=f"n{i}",
                title=f"Taiwan semiconductor news {i}",
                url=f"https://www.cna.com.tw/news/afe/20260522{i:03d}.aspx",
                source="CNA",
                published_at="1 hours ago",
                summary="Taiwan stock market semiconductor AI news.",
            )
            for i in range(count)
        ]

    def test_batch_classify_splits_items_and_reports_progress(self):
        gemini = self.FakeGemini()
        center = SimpleNamespace(gemini=gemini)
        messages: list[str] = []
        with patch.dict("os.environ", {"NEWS_AI_CLASSIFY_BATCH_SIZE": "2", "NEWS_AI_CLASSIFY_TIMEOUT_SECONDS": "33"}):
            classified = _batch_classify_news(self._items(5), center, messages.append, ai_model="gemini")

        self.assertEqual(len(classified), 5)
        self.assertEqual(len(gemini.calls), 3)
        self.assertEqual(gemini.timeout_during_call, 33.0)
        self.assertTrue(any("AI" in msg and "1/3" in msg for msg in messages))
        self.assertTrue(any("AI" in msg and "3/3" in msg for msg in messages))
        self.assertTrue(any("prompt=" in msg and "est_tokens=" in msg for msg in messages))

    def test_classification_defaults_are_conservative(self):
        with patch.dict("os.environ", {}, clear=False):
            self.assertEqual(_classify_limit(), 18)
            self.assertEqual(_classify_batch_size(), 3)
            self.assertEqual(_classify_timeout_seconds(), 45.0)
            self.assertEqual(_classify_text_limit(), 500)
            self.assertEqual(_news_high_tier_classify_limit(), 12)

    def test_classification_payload_truncates_full_text(self):
        item = self._items(1)[0]
        item.full_text = "A" * 1200
        payload = _classification_payload([item], text_limit=80)
        self.assertEqual(len(payload), 1)
        self.assertLessEqual(len(payload[0]["text"]), 83)
        self.assertTrue(payload[0]["text"].endswith("..."))

    def test_failed_batch_falls_back_without_stopping_remaining_batches(self):
        gemini = self.FailingOnceGemini()
        center = SimpleNamespace(gemini=gemini)
        messages: list[str] = []
        with patch.dict("os.environ", {"NEWS_AI_CLASSIFY_BATCH_SIZE": "1"}):
            classified = _batch_classify_news(self._items(2), center, messages.append, ai_model="gemini")

        self.assertEqual(len(classified), 2)
        self.assertEqual(len(gemini.calls), 3)
        self.assertTrue(any("retrying with lightweight payload" in msg for msg in messages))
        self.assertTrue(any("lightweight retry completed" in msg for msg in messages))
        self.assertEqual(classified[1].summary, "second batch")

    def test_failed_batch_fallbacks_after_retry_failure(self):
        class AlwaysFailGemini(self.FakeGemini):
            def generate_report(self, prompt, enable_grounding=False):
                self.calls.append(prompt)
                raise TimeoutError("simulated timeout")

        gemini = AlwaysFailGemini()
        center = SimpleNamespace(gemini=gemini)
        messages: list[str] = []
        with patch.dict("os.environ", {"NEWS_AI_CLASSIFY_BATCH_SIZE": "1"}):
            classified = _batch_classify_news(self._items(1), center, messages.append, ai_model="gemini")

        self.assertEqual(len(classified), 1)
        self.assertEqual(len(gemini.calls), 2)
        self.assertTrue(any("fallback to local rules" in msg for msg in messages))

    def test_timeout_retry_failure_fallbacks_remaining_items_without_more_ai_calls(self):
        class AlwaysFailGemini(self.FakeGemini):
            def generate_report(self, prompt, enable_grounding=False):
                self.calls.append(prompt)
                raise TimeoutError("simulated timeout")

        gemini = AlwaysFailGemini()
        center = SimpleNamespace(gemini=gemini)
        messages: list[str] = []
        with patch.dict("os.environ", {"NEWS_AI_CLASSIFY_BATCH_SIZE": "1"}):
            classified = _batch_classify_news(self._items(3), center, messages.append, ai_model="gemini")

        self.assertEqual(len(classified), 3)
        self.assertEqual(len(gemini.calls), 2)
        self.assertTrue(any("fallback remaining 2 items" in msg for msg in messages))

    def test_call_news_classifier_restores_temporary_timeout(self):
        gemini = self.FakeGemini()
        center = SimpleNamespace(gemini=gemini)
        _call_news_classifier(center, "gemini", "{}", 45.0)
        self.assertEqual(gemini.timeout_during_call, 45.0)
        self.assertEqual(gemini.timeout_seconds, 999)

    def test_low_model_classifies_all_and_high_model_reviews_top_items(self):
        gemini = self.FakeGemini()
        low = self.FakeMiniMaxLow()
        center = SimpleNamespace(gemini=gemini, low_model_minimax=low)
        messages: list[str] = []
        items = self._items(3)
        items[0].source = "中央社"
        items[0].importance_score = 100
        with patch.dict("os.environ", {"NEWS_AI_CLASSIFY_BATCH_SIZE": "2", "NEWS_HIGH_TIER_CLASSIFY_LIMIT": "1"}):
            classified = _batch_classify_news(items, center, messages.append, ai_model="gemini")

        self.assertEqual(len(classified), 3)
        self.assertGreaterEqual(len(low.calls), 2)
        self.assertEqual(len(gemini.calls), 1)
        self.assertTrue(any("新聞分類分流" in msg for msg in messages))

    def test_select_high_tier_news_prefers_major_source_and_theme(self):
        items = self._items(3)
        items[0].source = "random"
        items[0].importance_score = 1
        items[1].source = "中央社"
        items[1].title = "台股半導體重大訊息"
        items[1].importance_score = 10
        items[2].source = "random"
        items[2].importance_score = 5

        selected = _select_high_tier_news_items(items, 1)

        self.assertEqual(selected[0].id, items[1].id)


class UserSubmittedNewsUrlTests(unittest.TestCase):
    class FakeRepository:
        def __init__(self):
            self.items: dict[str, NewsItem] = {}
            self.preferences = []

        def get_by_url(self, url):
            return self.items.get(url)

        def save(self, item):
            if item.url in self.items:
                return False
            saved = NewsItem.from_dict(item.to_dict())
            saved.id = "123"
            self.items[item.url] = saved
            return True

        def count_recent(self, hours=24):
            return len(self.items)

        def save_preference(self, preference):
            self.preferences.append(preference)
            return True

        def count_preferences(self):
            return len(self.preferences)

    class FakeGemini:
        timeout_seconds = 999

        def generate_report(self, prompt, enable_grounding=False):
            return SimpleNamespace(raw=json.dumps({
                "0": {
                    "category": "AI / 半導體",
                    "summary": "台股半導體新聞摘要",
                    "related_symbols": ["2330"],
                    "related_topics": ["AI"],
                    "importance_score": 88,
                }
            }, ensure_ascii=False))

    class FakeWebFetchService:
        def __init__(self, *args, **kwargs):
            pass

        def fetch(self, url, progress=None):
            return WebFetchResult(
                url=url,
                title="Taiwan semiconductor AI stocks rise",
                content="Taiwan stock market semiconductor AI news. 台股 半導體 AI 產業 新聞 台積電 供應鏈。",
                content_status="success",
                fetch_provider="fake",
            )

    def test_save_user_submitted_news_url_fetches_classifies_and_saves(self):
        repo = self.FakeRepository()
        center = SimpleNamespace(gemini=self.FakeGemini())
        with patch("research_center.news_service.WebFetchService", self.FakeWebFetchService):
            item, status = save_user_submitted_news_url(
                "https://www.cna.com.tw/news/afe/202605210001.aspx",
                center,
                repo,
            )
        self.assertEqual(status, "saved")
        self.assertIsNotNone(item)
        self.assertEqual(item.summary, "台股半導體新聞摘要")
        self.assertEqual(repo.count_recent(hours=24), 1)
        self.assertEqual(item.news_origin, "manual")
        self.assertEqual(repo.count_preferences(), 1)
        self.assertEqual(repo.preferences[0].normalized_category, "ai_semiconductor")
        self.assertIn(repo.preferences[0].news_type, {"supply_chain_benefit", "macro_market", "other"})

    def test_save_user_submitted_news_url_detects_duplicate(self):
        repo = self.FakeRepository()
        repo.save(NewsItem(
            title="Taiwan semiconductor AI stocks rise",
            url="https://www.cna.com.tw/news/afe/202605210001.aspx",
            source="CNA",
            summary="Taiwan stock market semiconductor AI news.",
            created_at="2026-05-21T09:00:00",
        ))
        center = SimpleNamespace(gemini=self.FakeGemini())
        item, status = save_user_submitted_news_url(
            "https://www.cna.com.tw/news/afe/202605210001.aspx",
            center,
            repo,
        )
        self.assertEqual(status, "duplicate")
        self.assertIsNotNone(item)
        self.assertEqual(repo.count_preferences(), 1)


class NewsCategoryTests(unittest.TestCase):
    def test_normalizes_known_internal_categories(self):
        self.assertEqual(normalize_news_category("sector_strength"), "sector_rotation")
        self.assertEqual(normalize_news_category("theme"), "sector_rotation")
        self.assertEqual(normalize_news_category("theme_radar"), "sector_rotation")
        self.assertEqual(news_category_label("theme_radar"), "題材與族群輪動")


if __name__ == "__main__":
    unittest.main()
