import unittest

from research_center.models import CommandRequest
from research_center.search_query_service import build_search_discovery_tasks, flatten_task_queries


class SearchQueryServiceTests(unittest.TestCase):
    def test_research_deep_includes_official_and_rerating_tasks(self):
        request = CommandRequest(command="research", raw_text="/research 2330 --deep", target="2330", mode="deep")
        tasks = build_search_discovery_tasks(request, {"stock": {"name": "台積電"}})
        labels = [task["label"] for task in tasks]

        self.assertIn("官方公告與財報", labels)
        self.assertIn("評分與價值重估證據", labels)
        self.assertTrue(any("2330 台積電" in query for query in flatten_task_queries(tasks)))

    def test_value_scan_uses_ai_candidates(self):
        request = CommandRequest(command="value_scan", raw_text="/value_scan 精選選股 --deep", candidate_pool="精選選股", mode="deep")
        tasks = build_search_discovery_tasks(request, {"ai_candidates": [{"code": "6217", "name": "中探針"}], "candidates": [{"code": "2330", "name": "台積電"}]})
        queries = flatten_task_queries(tasks)

        self.assertTrue(any("6217 中探針" in query for query in queries))
        self.assertFalse(any("2330 台積電" in query for query in queries))

    def test_theme_includes_theme_and_matched_companies(self):
        request = CommandRequest(command="theme", raw_text="/theme AI伺服器", theme_scope="AI伺服器")
        tasks = build_search_discovery_tasks(request, {"matched_companies": [{"code": "6669", "name": "緯穎"}]})
        queries = flatten_task_queries(tasks)

        self.assertTrue(any("AI伺服器" in query for query in queries))
        self.assertTrue(any("緯穎" in query for query in queries))

    def test_macro_includes_global_macro_keywords(self):
        request = CommandRequest(command="macro", raw_text="/macro", market_scope="台股", mode="deep")
        queries = flatten_task_queries(build_search_discovery_tasks(request, {}))
        joined = " ".join(queries)

        self.assertIn("Fed", joined)
        self.assertIn("油價", joined)
        self.assertIn("原物料", joined)

    def test_news_returns_discovery_queries(self):
        request = CommandRequest(command="news", raw_text="/news")
        queries = flatten_task_queries(build_search_discovery_tasks(request, {}))

        self.assertTrue(queries)
        self.assertTrue(any("台股" in query or "Taiwan" in query for query in queries))

    def test_flatten_task_queries_deduplicates(self):
        tasks = [{"queries": [{"title": "a", "items": ["x", "x"]}, "y"]}]

        self.assertEqual(flatten_task_queries(tasks), ["x", "y"])


if __name__ == "__main__":
    unittest.main()
