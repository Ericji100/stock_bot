from __future__ import annotations

import json
import unittest
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
    _filter_taiwan_finance_news,
    _is_taiwan_finance_news,
    _normalize_news_title,
    _call_news_classifier,
    build_news_discovery_queries,
    run_news_7d,
    run_news_latest,
    save_user_submitted_news_url,
)
from research_center.news_categories import normalize_news_category, news_category_label, ordered_news_category_keys
from research_center.news_formatters import format_news_detail, format_news_digest
from research_center.web_fetch_service import WebFetchResult
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

    def test_classification_defaults_are_conservative(self):
        with patch.dict("os.environ", {}, clear=False):
            self.assertEqual(_classify_limit(), 50)
            self.assertEqual(_classify_batch_size(), 5)
            self.assertEqual(_classify_timeout_seconds(), 90.0)
            self.assertEqual(_classify_text_limit(), 800)

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

    def test_call_news_classifier_restores_temporary_timeout(self):
        gemini = self.FakeGemini()
        center = SimpleNamespace(gemini=gemini)
        _call_news_classifier(center, "gemini", "{}", 45.0)
        self.assertEqual(gemini.timeout_during_call, 45.0)
        self.assertEqual(gemini.timeout_seconds, 999)


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
