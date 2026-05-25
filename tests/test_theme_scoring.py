"""Tests for theme_scoring."""
from __future__ import annotations

import unittest

from research_center.theme_models import ThemeConfidence, ThemeEvidence, ThemeRelationType, ThemeSourceLevel
from research_center.theme_scoring import (
    confidence_label,
    freshness_score,
    theme_relation_score,
)


class TestThemeScoring(unittest.TestCase):

    def test_theme_relation_score_full(self):
        """Test scoring with all evidence types."""
        evidence = [
            {
                "source": "台積電法說會",
                "source_level": "L1_official",
                "content": "AI需求強勁",
                "score_contribution": 30.0,
            },
            {
                "source": "科技新報",
                "source_level": "L2_media",
                "content": "AI伺服器需求爆發",
                "score_contribution": 20.0,
            },
        ]
        result = theme_relation_score(
            evidence_list=evidence,
            supply_chain_hits=["台積電", "日月光"],
            financial_verifiable=True,
            financial_has_numbers=True,
            market_heat_score=8.0,
            counter_evidence_count=0,
        )
        self.assertEqual(result.official_score, 30.0)
        self.assertEqual(result.media_score, 20.0)
        self.assertEqual(result.supply_chain_score, 20.0)
        self.assertEqual(result.financial_score, 15.0)
        self.assertEqual(result.heat_score, 8.0)
        self.assertEqual(result.counter_penalty, 0.0)
        self.assertGreater(result.total, 70)

    def test_theme_relation_score_l3_no_high_confidence(self):
        """L3 community source cannot independently produce high confidence."""
        evidence = [
            {
                "source": "PTT 八卦板",
                "source_level": "L3_community",
                "content": "聽說 AI 要爆",
                "score_contribution": 10.0,
            }
        ]
        result = theme_relation_score(
            evidence_list=evidence,
            supply_chain_hits=[],
            financial_verifiable=False,
            market_heat_score=5.0,
        )
        # L3 only should not reach high confidence
        self.assertNotEqual(result.confidence, ThemeConfidence.HIGH)

    def test_theme_relation_score_counter_evidence(self):
        """Counter evidence should reduce score."""
        evidence = [
            {
                "source": "經濟日報",
                "source_level": "L2_media",
                "content": "AI需求放緩",
                "score_contribution": 15.0,
            }
        ]
        result = theme_relation_score(
            evidence_list=evidence,
            supply_chain_hits=["不明公司"],
            financial_verifiable=True,
            financial_has_numbers=False,
            market_heat_score=3.0,
            counter_evidence_count=2,
            counter_evidence_strength=0.5,
        )
        self.assertGreater(result.counter_penalty, 0)
        self.assertLess(result.total, 50)

    def test_confidence_from_score(self):
        self.assertEqual(ThemeConfidence.from_score(80), ThemeConfidence.HIGH)
        self.assertEqual(ThemeConfidence.from_score(75), ThemeConfidence.HIGH)
        self.assertEqual(ThemeConfidence.from_score(70), ThemeConfidence.MEDIUM)
        self.assertEqual(ThemeConfidence.from_score(60), ThemeConfidence.MEDIUM)
        self.assertEqual(ThemeConfidence.from_score(55), ThemeConfidence.WATCH_ONLY)
        self.assertEqual(ThemeConfidence.from_score(40), ThemeConfidence.WATCH_ONLY)
        self.assertEqual(ThemeConfidence.from_score(39), ThemeConfidence.REJECT)

    def test_relation_type_inference(self):
        # Direct product
        evidence = [{"source": "官方", "source_level": "L1_official", "content": "", "score_contribution": 30}]
        r = theme_relation_score(evidence, supply_chain_hits=["具體公司"], financial_verifiable=True, financial_has_numbers=True)
        self.assertEqual(r.relation_type, ThemeRelationType.DIRECT_PRODUCT)

        # Supply chain
        evidence = [{"source": "媒體", "source_level": "L2_media", "content": "", "score_contribution": 20}]
        r = theme_relation_score(evidence, supply_chain_hits=["具體公司"], financial_verifiable=False)
        self.assertEqual(r.relation_type, ThemeRelationType.SUPPLY_CHAIN)

        # Unclear
        evidence = [{"source": "社群", "source_level": "L3_community", "content": "", "score_contribution": 5}]
        r = theme_relation_score(evidence, supply_chain_hits=[], financial_verifiable=False)
        self.assertEqual(r.relation_type, ThemeRelationType.UNCLEAR)

    def test_freshness_score(self):
        from datetime import datetime, timedelta
        now = datetime.now().astimezone()
        recent = (now - timedelta(days=10)).isoformat()
        old = (now - timedelta(days=60)).isoformat()

        self.assertEqual(freshness_score(recent, days_threshold=30), 10.0)
        self.assertEqual(freshness_score(old, days_threshold=30), 0.0)
        self.assertEqual(freshness_score(None), 0.0)

    def test_confidence_label(self):
        self.assertEqual(confidence_label(ThemeConfidence.HIGH), "高信心")
        self.assertEqual(confidence_label(ThemeConfidence.MEDIUM), "中等信心")
        self.assertEqual(confidence_label(ThemeConfidence.WATCH_ONLY), "觀望")
        self.assertEqual(confidence_label(ThemeConfidence.REJECT), "否決")


if __name__ == "__main__":
    unittest.main()