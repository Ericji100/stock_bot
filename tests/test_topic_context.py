"""Tests for research_center/topic_context.py — topic library context injection."""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from research_center.topic_context import (
    build_candidates_topic_context,
    build_stock_topic_context,
    build_theme_topic_context,
)


class TestTopicContext(unittest.TestCase):
    _ROOT = Path(__file__).resolve().parents[1]

    def setUp(self):
        self._mock_profiles = [
            {
                "theme_id": "ai_server",
                "theme_name": "AI伺服器",
                "keywords": ["AI", "伺服器", "散熱"],
                "affected_companies": ["2330", "2382"],
                "risk_notes": ["客戶集中度高"],
                "missing_data": ["出貨量未公開"],
            },
            {
                "theme_id": "semiconductor",
                "theme_name": "半導體",
                "keywords": ["半導體", "晶圓", "封裝"],
                "affected_companies": ["2330", "2303"],
                "risk_notes": ["景氣週期"],
                "missing_data": ["庫存週轉天數"],
            },
            {
                "theme_id": "robot",
                "theme_name": "機器人",
                "keywords": ["機器人", "自動化", "伺服馬達"],
                "affected_companies": ["4545"],
                "risk_notes": ["訂單能見度低"],
                "missing_data": ["營收占比不明"],
            },
        ]
        self._mock_company_map = {
            "2330": ["ai_server", "semiconductor"],
            "2382": [{"theme_id": "ai_server", "theme_name": "AI伺服器"}],
            "4545": ["robot"],
            "9999": [],
        }
        self._mock_supply_chain = [
            {"company_code": "2330", "theme_id": "ai_server", "role": "核心晶片供應商"},
            {"company_code": "2382", "theme_id": "ai_server", "role": "PCB供應商"},
        ]

    def _patch_topic_loaders(self):
        """Patch internal loaders to return mock data."""
        return patch.multiple(
            "research_center.topic_context",
            _theme_profiles=lambda: self._mock_profiles,
            _company_theme_map=lambda: self._mock_company_map,
            _supply_chain_nodes=lambda: self._mock_supply_chain,
        )

    # ── build_stock_topic_context ──────────────────────────────

    def test_stock_with_direct_matches(self):
        """Stock with direct company_theme_map entries should return matched topics."""
        with self._patch_topic_loaders():
            ctx = build_stock_topic_context("2330", "台積電")

        matched = ctx["matched_topics"]
        self.assertEqual(len(matched), 2)
        self.assertEqual(matched[0]["theme_id"], "ai_server")
        self.assertEqual(matched[0]["confidence"], "high")
        self.assertEqual(matched[1]["theme_id"], "semiconductor")
        self.assertEqual(matched[1]["confidence"], "high")

        # Supply chain role lookup
        self.assertEqual(matched[0]["supply_chain_role"], "核心晶片供應商")

        # Usage policy must contain reference rules
        policy = ctx["usage_policy"]
        self.assertEqual(policy["role"], "背景參考與候選假設")
        rules = " ".join(policy["rules"])
        self.assertIn("僅供背景參考", rules)
        self.assertIn("不得僅因題材庫標記", rules)
        self.assertIn("重新驗證", rules)
        self.assertIn("以最新證據為準", rules)

    def test_stock_object_map_keeps_candidate_as_hypothesis_only(self):
        company_map = {
            "7777": {
                "company_name": "Candidate Co",
                "themes": ["ai_server"],
                "candidate_themes": [
                    {
                        "theme_id": "robot",
                        "verification_status": "candidate",
                        "usage_policy": "hypothesis_only",
                        "not_representative": True,
                    }
                ],
            }
        }
        with patch.multiple(
            "research_center.topic_context",
            _theme_profiles=lambda: self._mock_profiles,
            _company_theme_map=lambda: company_map,
            _supply_chain_nodes=lambda: [],
        ):
            ctx = build_stock_topic_context("7777", "Candidate Co")

        matched = ctx["matched_topics"]
        formal = next(m for m in matched if m["theme_id"] == "ai_server")
        candidate = next(m for m in matched if m["theme_id"] == "robot")
        self.assertEqual(formal["confidence"], "high")
        self.assertEqual(candidate["confidence"], "candidate")
        self.assertEqual(candidate["usage_policy"], "hypothesis_only")
        self.assertTrue(candidate["not_representative"])

    def test_stock_with_weak_name_match(self):
        """Stock with no direct map but keyword-in-name weak match should return low confidence."""
        with self._patch_topic_loaders():
            ctx = build_stock_topic_context("9999", "自動化設備公司")

        matched = ctx["matched_topics"]
        # Weak match by keyword "自動化" in robot theme
        self.assertTrue(
            any(m["theme_id"] == "robot" and m["confidence"] == "low" for m in matched)
        )

    def test_stock_with_no_matches(self):
        """Stock with no match should return empty topics but still have usage_policy."""
        with self._patch_topic_loaders():
            ctx = build_stock_topic_context("8888", "完全不搭公司")

        self.assertEqual(ctx["matched_topics"], [])
        self.assertEqual(ctx["company_topic_relations"]["direct_matches"], 0)
        self.assertEqual(ctx["company_topic_relations"]["weak_matches"], 0)
        self.assertIn("usage_policy", ctx)

    def test_missing_files_no_crash(self):
        """Missing config files should not raise — graceful fallback."""
        with patch.multiple(
            "research_center.topic_context",
            _theme_profiles=lambda: [],
            _company_theme_map=lambda: {},
            _supply_chain_nodes=lambda: [],
        ):
            ctx = build_stock_topic_context("2330", "台積電")
        self.assertEqual(ctx["matched_topics"], [])
        self.assertIn("usage_policy", ctx)

    def test_max_8_themes(self):
        """Should cap at 8 themes total."""
        many_profiles = [
            {
                "theme_id": f"theme_{i:02d}",
                "theme_name": f"題材{i}",
                "keywords": [f"kw{i}"],
                "affected_companies": ["2330"],
                "risk_notes": [],
                "missing_data": [],
            }
            for i in range(20)
        ]
        with patch.multiple(
            "research_center.topic_context",
            _theme_profiles=lambda: many_profiles,
            _company_theme_map=lambda: {"2330": [f"theme_{i:02d}" for i in range(20)]},
            _supply_chain_nodes=lambda: [],
        ):
            ctx = build_stock_topic_context("2330", "台積電")
        self.assertLessEqual(len(ctx["matched_topics"]), 8)

    # ── build_candidates_topic_context ─────────────────────────

    def test_candidates_topic_context(self):
        """Multi-stock candidate pool should return topic map."""
        candidates = [
            {"code": "2330", "name": "台積電"},
            {"code": "2382", "name": "廣達"},
            {"code": "4545", "name": "某機器人股"},
            {"code": "9999", "name": "無題材股"},
        ]
        with self._patch_topic_loaders():
            ctx = build_candidates_topic_context(candidates, limit=30)

        topic_map = ctx["candidate_topic_map"]
        self.assertEqual(len(topic_map), 4)

        # 2330 should have 2 themes
        t2330 = next((t for t in topic_map if t["code"] == "2330"), None)
        self.assertIsNotNone(t2330)
        self.assertEqual(len(t2330["themes"]), 2)

        # 9999 should have empty themes
        t9999 = next((t for t in topic_map if t["code"] == "9999"), None)
        self.assertIsNotNone(t9999)
        self.assertEqual(t9999["themes"], [])

        # topic_summary should aggregate matched themes
        summary = ctx["topic_summary"]
        summary_ids = {s["theme_id"] for s in summary}
        self.assertIn("ai_server", summary_ids)
        self.assertIn("semiconductor", summary_ids)

        # usage policy
        self.assertIn("候選股題材背景", ctx["usage_policy"]["role"])
        rules = " ".join(ctx["usage_policy"]["rules"])
        self.assertIn("不得只因某股票命中熱門題材就給高分", rules)
        self.assertIn("重估分數仍需依財報", rules)

    def test_candidates_max_3_themes_per_stock(self):
        """Each candidate should have at most 3 themes."""
        many_profiles = [
            {
                "theme_id": f"theme_{i:02d}",
                "theme_name": f"題材{i}",
                "keywords": [f"kw{i}"],
                "affected_companies": ["2330"],
                "risk_notes": [],
                "missing_data": [],
            }
            for i in range(10)
        ]
        candidates = [{"code": "2330", "name": "台積電"}]
        with patch.multiple(
            "research_center.topic_context",
            _theme_profiles=lambda: many_profiles,
            _company_theme_map=lambda: {"2330": [f"theme_{i:02d}" for i in range(10)]},
            _supply_chain_nodes=lambda: [],
        ):
            ctx = build_candidates_topic_context(candidates, limit=30)

        t2330 = next((t for t in ctx["candidate_topic_map"] if t["code"] == "2330"), None)
        self.assertIsNotNone(t2330)
        self.assertLessEqual(len(t2330["themes"]), 3)

    def test_candidates_limit_respected(self):
        """Only first 'limit' candidates should be processed."""
        candidates = [{"code": str(i), "name": f"公司{i}"} for i in range(100)]
        with patch.multiple(
            "research_center.topic_context",
            _theme_profiles=lambda: [],
            _company_theme_map=lambda: {},
            _supply_chain_nodes=lambda: [],
        ):
            ctx = build_candidates_topic_context(candidates, limit=30)
        self.assertEqual(len(ctx["candidate_topic_map"]), 30)

    # ── build_theme_topic_context ─────────────────────────────

    def test_theme_topic_context_exact_name_match(self):
        """Theme query should match existing formal topic profiles."""
        with self._patch_topic_loaders():
            ctx = build_theme_topic_context("AI伺服器")

        matched = ctx["matched_topics"]
        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0]["theme_id"], "ai_server")
        self.assertEqual(matched[0]["confidence"], "high")
        self.assertIn("客戶集中度高", ctx["risk_notes"])
        self.assertEqual(len(ctx["related_supply_chain_nodes"]), 2)

    def test_theme_topic_context_keyword_match(self):
        """Theme query can match by keyword and should be reference-only."""
        with self._patch_topic_loaders():
            ctx = build_theme_topic_context("散熱")

        matched = ctx["matched_topics"]
        self.assertTrue(any(m["theme_id"] == "ai_server" for m in matched))
        rules = " ".join(ctx["usage_policy"]["rules"])
        self.assertIn("不得直接當成最終結論", rules)
        self.assertIn("重新驗證", rules)

    def test_theme_topic_context_no_match(self):
        """Unknown theme should still return usage_policy without crashing."""
        with self._patch_topic_loaders():
            ctx = build_theme_topic_context("完全不存在的題材")

        self.assertEqual(ctx["matched_topics"], [])
        self.assertEqual(ctx["related_supply_chain_nodes"], [])
        self.assertIn("usage_policy", ctx)


if __name__ == "__main__":
    unittest.main()
