"""Tests for topic_models.py."""
import unittest
from research_center.topic_models import (
    TopicActionType,
    TopicApplyResult,
    TopicAuditEntry,
    TopicChangeAction,
    TopicChangeMode,
    TopicChangePack,
    TopicChangeStatus,
    TopicCompanyRelation,
    TopicConfidence,
    TopicEvidence,
    TopicProfile,
    TopicSourceLevel,
    TopicSupplyChainNode,
)


class TestTopicEvidence(unittest.TestCase):
    def test_to_dict(self):
        ev = TopicEvidence(
            source="DIGITIMES",
            source_level=TopicSourceLevel.L2_MEDIA,
            content="台積電 AI 晶片供應",
            url="https://example.com",
            publish_date="2026-01-15",
            score_contribution=10.0,
        )
        d = ev.to_dict()
        self.assertEqual(d["source"], "DIGITIMES")
        self.assertEqual(d["source_level"], "L2_media")
        self.assertEqual(d["score_contribution"], 10.0)

    def test_from_dict(self):
        d = {
            "source": "MOPS",
            "source_level": "L1_official",
            "content": "營收公告",
            "publish_date": "2026-01-31",
            "score_contribution": 15.0,
        }
        ev = TopicEvidence.from_dict(d)
        self.assertEqual(ev.source, "MOPS")
        self.assertEqual(ev.source_level, TopicSourceLevel.L1_OFFICIAL)
        self.assertEqual(ev.score_contribution, 15.0)


class TestTopicChangeAction(unittest.TestCase):
    def test_to_dict(self):
        action = TopicChangeAction(
            action_type=TopicActionType.CREATE_THEME,
            theme_id="ai_server",
            theme_name="AI伺服器",
            keywords=["AI", "HBM"],
            industries=["半導體"],
            supply_chain_role="核心受惠",
            confidence=TopicConfidence.HIGH,
            reason="全球 AI 需求爆發",
            evidence=[],
        )
        d = action.to_dict()
        self.assertEqual(d["action_type"], "create_theme")
        self.assertEqual(d["theme_id"], "ai_server")
        self.assertEqual(d["confidence"], "high")

    def test_from_dict(self):
        d = {
            "action_type": "update_theme",
            "theme_id": "ai_server",
            "theme_name": "AI伺服器更新",
            "keywords": ["AI"],
            "industries": ["半導體"],
            "supply_chain_role": "受惠",
            "confidence": "medium",
            "reason": "需求成長",
            "evidence": [],
            "company_code": "2330",
            "company_name": "台積電",
        }
        action = TopicChangeAction.from_dict(d)
        self.assertEqual(action.action_type, TopicActionType.UPDATE_THEME)
        self.assertEqual(action.company_code, "2330")

    def test_roundtrip_full_fields(self):
        """All new action fields must survive to_dict -> from_dict roundtrip."""
        action = TopicChangeAction(
            action_type=TopicActionType.CREATE_THEME,
            theme_id="ai_server",
            theme_name="AI伺服器",
            keywords=["AI伺服器", "GB200"],
            industries=["半導體", "伺服器"],
            supply_chain_role="核心受惠",
            confidence=TopicConfidence.HIGH,
            reason="全球AI需求爆發",
            evidence=[],
            affected_companies=["2330", "3711"],
            risk_notes=["供應鏈集中風險"],
            missing_data=["無營收占比資料"],
            supply_chain_nodes=[
                {"node_id": "n1", "company_code": "2330", "company_name": "台積電", "role": "晶片製造", "upstream": [], "downstream": [], "product_keywords": []}
            ],
            counter_evidence=[{"source": "論壇", "source_level": "L3_community", "content": "證據", "url": "https://example.com", "published_date": "2026-01-01", "score_contribution": 3.0}],
        )
        d = action.to_dict()
        restored = TopicChangeAction.from_dict(d)
        self.assertEqual(restored.affected_companies, ["2330", "3711"])
        self.assertEqual(restored.risk_notes, ["供應鏈集中風險"])
        self.assertEqual(restored.missing_data, ["無營收占比資料"])
        self.assertEqual(len(restored.supply_chain_nodes), 1)
        self.assertEqual(restored.supply_chain_nodes[0]["company_code"], "2330")
        self.assertEqual(len(restored.counter_evidence), 1)
        self.assertEqual(restored.counter_evidence[0]["source"], "論壇")

    def test_from_dict_missing_fields_default_to_empty(self):
        """Old change packs without new fields should still deserialize."""
        d = {
            "action_type": "create_theme",
            "theme_id": "old_theme",
            "theme_name": "舊題材",
            "keywords": [],
            "industries": [],
            "supply_chain_role": "",
            "confidence": "medium",
            "reason": "",
            "evidence": [],
        }
        action = TopicChangeAction.from_dict(d)
        self.assertEqual(action.affected_companies, [])
        self.assertEqual(action.risk_notes, [])
        self.assertEqual(action.missing_data, [])
        self.assertEqual(action.supply_chain_nodes, [])
        self.assertEqual(action.counter_evidence, [])

    def test_supply_chain_node_roundtrip_rich_fields(self):
        node = TopicSupplyChainNode(
            node_id="advanced_packaging_2330",
            company_code="2330",
            company_name="TSMC",
            role="CoWoS",
            upstream=["HBM"],
            downstream=["AI GPU"],
            product_keywords=["CoWoS"],
            theme_id="advanced_packaging",
            confidence="high",
            source_level="L1_official",
            evidence=[{"source": "TSMC", "content": "CoWoS demand"}],
            risk_notes=["capacity risk"],
            missing_data=["revenue mix"],
            extra={"custom_field": "kept"},
        )
        restored = TopicSupplyChainNode.from_dict(node.to_dict())
        self.assertEqual(restored.theme_id, "advanced_packaging")
        self.assertEqual(restored.evidence[0]["source"], "TSMC")
        self.assertEqual(restored.risk_notes, ["capacity risk"])
        self.assertEqual(restored.missing_data, ["revenue mix"])
        self.assertEqual(restored.extra["custom_field"], "kept")


class TestTopicChangePack(unittest.TestCase):
    def test_to_dict_roundtrip(self):
        pack = TopicChangePack(
            change_id="change_20260101_001",
            parent_change_id=None,
            mode=TopicChangeMode.INITIAL,
            status=TopicChangeStatus.PENDING,
            model="gemini",
            created_at="2026-01-01T10:00:00+0800",
            updated_at="2026-01-01T10:00:00+0800",
            summary="Initial AI server theme",
            confidence="high",
            actions=[
                TopicChangeAction(
                    action_type=TopicActionType.CREATE_THEME,
                    theme_id="ai_server",
                    theme_name="AI伺服器",
                    keywords=["AI", "HBM"],
                    industries=["半導體"],
                    supply_chain_role="核心受惠",
                    confidence=TopicConfidence.HIGH,
                    reason="需求爆發",
                    evidence=[],
                )
            ],
            warnings=["資料覆蓋率不足"],
            sources=[],
            company_knowledge_updates={
                "companies": {
                    "2330": {
                        "company_name": "TSMC",
                        "product_lines": ["CoWoS"],
                    }
                }
            },
            adjustment_notes="",
            raw_response_path="/logs/topic_ai_raw/change_20260101_001.json",
            prompt_log_path="/logs/ai_prompts/change_20260101_001.json",
        )
        d = pack.to_dict()
        restored = TopicChangePack.from_dict(d)
        self.assertEqual(restored.change_id, "change_20260101_001")
        self.assertEqual(restored.mode, TopicChangeMode.INITIAL)
        self.assertEqual(restored.status, TopicChangeStatus.PENDING)
        self.assertEqual(len(restored.actions), 1)
        self.assertEqual(restored.actions[0].theme_id, "ai_server")
        self.assertEqual(restored.company_knowledge_updates["companies"]["2330"]["product_lines"], ["CoWoS"])

    def test_from_dict_missing_fields(self):
        d = {
            "change_id": "change_minimal",
            "actions": [],
        }
        pack = TopicChangePack.from_dict(d)
        self.assertEqual(pack.change_id, "change_minimal")
        self.assertEqual(pack.mode, TopicChangeMode.UPDATE)
        self.assertEqual(pack.status, TopicChangeStatus.PENDING)
        self.assertEqual(pack.company_knowledge_updates, {})

    def test_adjustment_check_roundtrip(self):
        pack = TopicChangePack(
            change_id="change_20260101_001",
            parent_change_id=None,
            mode=TopicChangeMode.INITIAL,
            status=TopicChangeStatus.PENDING,
            model="gemini",
            created_at="2026-01-01T10:00:00+0800",
            updated_at="2026-01-01T10:00:00+0800",
            summary="Test",
            confidence="medium",
            actions=[],
            adjustment_check={
                "user_request_summary": "請增加更多題材",
                "changes_made": ["新增了5個題材"],
                "not_fully_satisfied": ["資料不足"],
                "satisfaction": "partial",
            },
        )
        d = pack.to_dict()
        self.assertEqual(d["adjustment_check"]["satisfaction"], "partial")
        restored = TopicChangePack.from_dict(d)
        self.assertEqual(restored.adjustment_check["satisfaction"], "partial")
        self.assertEqual(restored.adjustment_check["user_request_summary"], "請增加更多題材")

    def test_from_dict_without_adjustment_check(self):
        """Old packs without adjustment_check should still deserialize cleanly."""
        d = {
            "change_id": "change_old",
            "actions": [],
            "mode": "adjust",
        }
        pack = TopicChangePack.from_dict(d)
        self.assertEqual(pack.change_id, "change_old")
        self.assertEqual(pack.adjustment_check, {})
        self.assertEqual(pack.mode, TopicChangeMode.ADJUST)


class TestTopicApplyResult(unittest.TestCase):
    def test_to_dict(self):
        result = TopicApplyResult(
            change_id="change_001",
            success=True,
            created=3,
            updated=1,
            merged=0,
            skipped=2,
            failed=0,
            errors=[],
            backup_path="/data/topic/backup/pre_apply_001",
        )
        d = result.to_dict()
        self.assertTrue(d["success"])
        self.assertEqual(d["created"], 3)


class TestTopicAuditEntry(unittest.TestCase):
    def test_create(self):
        entry = TopicAuditEntry.create(
            "confirmed", "change_001", "user123", {"created": 3}
        )
        self.assertEqual(entry.change_id, "change_001")
        self.assertEqual(entry.action, "confirmed")
        self.assertTrue(entry.timestamp)


class TestTopicProfile(unittest.TestCase):
    def test_roundtrip(self):
        p = TopicProfile(
            theme_id="ai_server",
            theme_name="AI伺服器",
            keywords=["AI", "HBM"],
            industries=["半導體"],
            confidence="high",
        )
        d = p.to_dict()
        restored = TopicProfile.from_dict(d)
        self.assertEqual(restored.theme_id, "ai_server")
        self.assertEqual(restored.keywords, ["AI", "HBM"])


if __name__ == "__main__":
    unittest.main()
