import unittest

from research_center.models import CommandRequest
from research_center.prompt_registry import build_grounding_discovery_prompts
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

    def test_value_scan_deep_limits_search_queries_but_keeps_evidence_tasks(self):
        request = CommandRequest(
            command="value_scan",
            raw_text="/value_scan 精選選股 --deep --top 30",
            candidate_pool="精選選股",
            mode="deep",
        )
        candidates = [{"code": f"23{i:02d}", "name": f"測試{i}"} for i in range(30)]
        tasks = build_search_discovery_tasks(request, {"ai_candidates": candidates})
        queries = flatten_task_queries(tasks)

        self.assertEqual(len(tasks), 5)
        self.assertLessEqual(len(queries), 45)
        self.assertTrue(all(task.get("query_policy", {}).get("strategy") == "focus_batches_plus_pool_context" for task in tasks))
        self.assertTrue(any("2300" in query and "2304" in query for query in queries))
        self.assertTrue(any("2305" in query and "2309" in query for query in queries))
        self.assertFalse(any("2315" in query for query in queries))
        self.assertTrue(any("候選股集合" in query for query in queries))
        joined = "\n".join(queries)
        for term in ["MOPS", "月營收", "供應鏈", "價值重估", "TDCC", "庫存"]:
            self.assertIn(term, joined)

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

    def test_macro_includes_global_macro_keywords(self):
        request = CommandRequest(command="macro", raw_text="/macro", market_scope="台股", mode="deep")
        queries = flatten_task_queries(build_search_discovery_tasks(request, {}))
        joined = " ".join(queries)

        self.assertIn("Fed", joined)
        self.assertIn("台指期", joined)
        self.assertIn("VIX", joined)
        self.assertIn("原油", joined)
        self.assertIn("風險", joined)
        self.assertIn("SOX", joined)
        self.assertGreaterEqual(len(queries), 15)

    def test_news_returns_discovery_queries(self):
        request = CommandRequest(command="news", raw_text="/news")
        queries = flatten_task_queries(build_search_discovery_tasks(request, {}))

        self.assertTrue(queries)
        self.assertTrue(any("台股" in query or "Taiwan" in query for query in queries))

    def test_news_grounding_queries_are_budgeted_after_date_expansion(self):
        request = CommandRequest(command="news", raw_text="/news refresh", target="台股財經新聞")
        tasks = build_grounding_discovery_prompts(request, {}, [])

        self.assertGreaterEqual(len(tasks), 10)
        self.assertTrue(all(len(task.get("queries") or []) <= 6 for task in tasks))
        self.assertTrue(all((task.get("query_budget") or {}).get("max_queries_per_task") == 6 for task in tasks))
        self.assertTrue(any((task.get("query_budget") or {}).get("original_query_count", 0) > 6 for task in tasks))
        joined_queries = " ".join(query for task in tasks for query in (task.get("queries") or []))
        for keyword in ("MOPS", "TDCC", "negative news"):
            self.assertIn(keyword, joined_queries)
        self.assertTrue(any("台指期" in " ".join(task.get("queries") or []) for task in tasks))

    def test_topic_maintain_search_plan_is_budgeted(self):
        request = CommandRequest(command="topic_maintain", raw_text="/topic_maintain --model minimax")
        plan = [{"query": f"topic query {index}", "bucket": f"bucket_{index}"} for index in range(40)]
        tasks = build_search_discovery_tasks(request, {"candidate_discovery_plan": {"search_query_plan": plan}})
        queries = flatten_task_queries(tasks)

        self.assertEqual(len(tasks), 8)
        self.assertLessEqual(len(queries), 16)
        self.assertTrue(all((task.get("query_policy") or {}).get("strategy") == "topic_maintain_representative_budget" for task in tasks))
        self.assertTrue(any("topic query 0" in query for query in queries))
        self.assertFalse(any("topic query 8" in query for query in queries))

    def test_flatten_task_queries_deduplicates(self):
        tasks = [{"queries": [{"title": "a", "items": ["x", "x"]}, "y"]}]

        self.assertEqual(flatten_task_queries(tasks), ["x", "y"])


if __name__ == "__main__":
    unittest.main()
