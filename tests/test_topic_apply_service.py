"""Tests for topic_apply_service.py."""
import unittest
from unittest.mock import patch, MagicMock

from research_center.topic_models import (
    TopicChangeMode,
    TopicChangePack,
    TopicChangeStatus,
    TopicActionType,
    TopicEvidence,
    TopicSourceLevel,
)
from research_center import topic_apply_service as apply_service


class TestTopicApplyService(unittest.TestCase):
    def setUp(self):
        self._patchers = []

    def tearDown(self):
        for p in self._patchers:
            p.stop()

    def _mock_load_pack(self, pack):
        p = patch("research_center.topic_apply_service.load_change_pack", return_value=pack)
        self._patchers.append(p)
        return p.start()

    def _mock_backup(self, result):
        p = patch("research_center.topic_apply_service.backup_topic_files", return_value=result)
        self._patchers.append(p)
        return p.start()

    def _mock_save_profiles(self):
        p = patch("research_center.topic_apply_service.save_topic_profiles")
        self._patchers.append(p)
        return p.start()

    def _mock_update_status(self):
        p = patch("research_center.topic_apply_service.update_change_pack_status")
        self._patchers.append(p)
        return p.start()

    def _mock_write_audit(self):
        p = patch("research_center.topic_apply_service.write_topic_audit_log")
        self._patchers.append(p)
        return p.start()

    def _mock_load_profiles(self, profiles):
        p = patch("research_center.topic_apply_service.load_topic_profiles", return_value=profiles)
        self._patchers.append(p)
        return p.start()

    def _mock_load_company_map(self, data):
        p = patch("research_center.topic_apply_service.load_company_topic_map", return_value=data)
        self._patchers.append(p)
        return p.start()

    def _mock_load_supply_nodes(self, nodes):
        p = patch("research_center.topic_apply_service.load_supply_chain_nodes", return_value=nodes)
        self._patchers.append(p)
        return p.start()

    def _mock_save_company_map(self):
        p = patch("research_center.topic_apply_service.save_company_topic_map")
        self._patchers.append(p)
        return p.start()

    def _mock_save_supply_nodes(self):
        p = patch("research_center.topic_apply_service.save_supply_chain_nodes")
        self._patchers.append(p)
        return p.start()

    def test_confirm_nonexistent_returns_error(self):
        with patch("research_center.topic_apply_service.load_change_pack", return_value=None):
            result = apply_service.confirm_change_pack("nonexistent")
            self.assertFalse(result.success)

    def test_confirm_non_pending_returns_error(self):
        pack = TopicChangePack(
            change_id="change_test",
            parent_change_id=None,
            mode=TopicChangeMode.INITIAL,
            status=TopicChangeStatus.CONFIRMED,
            model="gemini",
            created_at="2026-01-01T10:00:00+0800",
            updated_at="2026-01-01T10:00:00+0800",
            summary="test",
            confidence="high",
            actions=[],
        )
        with patch("research_center.topic_apply_service.load_change_pack", return_value=pack):
            result = apply_service.confirm_change_pack("change_test")
            self.assertFalse(result.success)

    def test_confirm_create_theme_action(self):
        from research_center.topic_models import TopicChangeAction, TopicEvidence, TopicSourceLevel, TopicConfidence

        action = TopicChangeAction(
            action_type=TopicActionType.CREATE_THEME,
            theme_id="ai_server_new",
            theme_name="AI伺服器",
            keywords=["AI", "HBM"],
            industries=["半導體"],
            supply_chain_role="核心",
            confidence=TopicConfidence.HIGH,
            reason="需求爆發",
            evidence=[],
        )
        pack = TopicChangePack(
            change_id="change_test",
            parent_change_id=None,
            mode=TopicChangeMode.INITIAL,
            status=TopicChangeStatus.PENDING,
            model="gemini",
            created_at="2026-01-01T10:00:00+0800",
            updated_at="2026-01-01T10:00:00+0800",
            summary="Create AI server theme",
            confidence="high",
            actions=[action],
        )

        with patch("research_center.topic_apply_service.load_change_pack", return_value=pack):
            with patch("research_center.topic_apply_service.backup_topic_files", return_value={"backup_root": "/tmp/bak"}):
                with patch("research_center.topic_apply_service.load_topic_profiles", return_value=[]):
                    with patch("research_center.topic_apply_service.load_company_topic_map", return_value={}):
                        with patch("research_center.topic_apply_service.load_supply_chain_nodes", return_value=[]):
                            with patch("research_center.topic_apply_service.save_topic_profiles") as mock_save_profiles:
                                with patch("research_center.topic_apply_service.save_company_topic_map") as mock_save_map:
                                    with patch("research_center.topic_apply_service.save_supply_chain_nodes") as mock_save_nodes:
                                        with patch("research_center.topic_apply_service.update_change_pack_status"):
                                            with patch("research_center.topic_apply_service.write_topic_audit_log"):
                                                result = apply_service.confirm_change_pack("change_test")
                                                self.assertTrue(result.success)
                                                self.assertEqual(result.created, 1)
                                                mock_save_profiles.assert_called_once()
                                                mock_save_map.assert_called_once()
                                                mock_save_nodes.assert_called_once()

    def test_confirm_writes_risk_notes_missing_data(self):
        """risk_notes and missing_data should be written to TopicProfile."""
        from research_center.topic_models import TopicChangeAction, TopicEvidence, TopicSourceLevel, TopicConfidence

        action = TopicChangeAction(
            action_type=TopicActionType.CREATE_THEME,
            theme_id="ai_server_new",
            theme_name="AI伺服器",
            keywords=["AI", "HBM"],
            industries=["半導體"],
            supply_chain_role="核心",
            confidence=TopicConfidence.HIGH,
            reason="需求爆發",
            evidence=[],
            risk_notes=["供應鏈集中風險"],
            missing_data=["無營收占比資料"],
        )
        pack = TopicChangePack(
            change_id="change_test",
            parent_change_id=None,
            mode=TopicChangeMode.INITIAL,
            status=TopicChangeStatus.PENDING,
            model="gemini",
            created_at="2026-01-01T10:00:00+0800",
            updated_at="2026-01-01T10:00:00+0800",
            summary="Create AI server theme",
            confidence="high",
            actions=[action],
        )

        with patch("research_center.topic_apply_service.load_change_pack", return_value=pack):
            with patch("research_center.topic_apply_service.backup_topic_files", return_value={"backup_root": "/tmp/bak"}):
                with patch("research_center.topic_apply_service.load_topic_profiles", return_value=[]):
                    with patch("research_center.topic_apply_service.load_company_topic_map", return_value={}):
                        with patch("research_center.topic_apply_service.load_supply_chain_nodes", return_value=[]):
                            with patch("research_center.topic_apply_service.save_topic_profiles") as mock_save:
                                with patch("research_center.topic_apply_service.save_company_topic_map"):
                                    with patch("research_center.topic_apply_service.save_supply_chain_nodes"):
                                        with patch("research_center.topic_apply_service.update_change_pack_status"):
                                            with patch("research_center.topic_apply_service.write_topic_audit_log"):
                                                result = apply_service.confirm_change_pack("change_test")
                                                self.assertTrue(result.success)
                                                # Check that risk_notes and missing_data were saved
                                                saved_profiles = mock_save.call_args[0][0]
                                                self.assertEqual(len(saved_profiles), 1)
                                                self.assertEqual(saved_profiles[0].risk_notes, ["供應鏈集中風險"])
                                                self.assertEqual(saved_profiles[0].missing_data, ["無營收佔比資料"])

    def test_confirm_writes_affected_companies_and_supply_chain_nodes(self):
        """affected_companies and supply_chain_nodes should be written to formal files."""
        from research_center.topic_models import TopicChangeAction, TopicConfidence

        action = TopicChangeAction(
            action_type=TopicActionType.CREATE_THEME,
            theme_id="ai_server_new",
            theme_name="AI伺服器",
            keywords=["AI", "HBM"],
            industries=["半導體"],
            supply_chain_role="核心",
            confidence=TopicConfidence.HIGH,
            reason="需求爆發",
            evidence=[
                TopicEvidence(
                    source="TSMC",
                    source_level=TopicSourceLevel.L1_OFFICIAL,
                    content="AI demand evidence",
                    url="https://example.com",
                    publish_date="2026-01-01",
                )
            ],
            affected_companies=["2330 TSMC：advanced packaging", "3711 ASE：testing"],
            supply_chain_nodes=[
                {"node_id": "n1", "company_code": "2330", "company_name": "台積電", "role": "晶片製造", "upstream": [], "downstream": [], "product_keywords": []}
            ],
        )
        pack = TopicChangePack(
            change_id="change_test",
            parent_change_id=None,
            mode=TopicChangeMode.INITIAL,
            status=TopicChangeStatus.PENDING,
            model="gemini",
            created_at="2026-01-01T10:00:00+0800",
            updated_at="2026-01-01T10:00:00+0800",
            summary="Create AI server theme",
            confidence="high",
            actions=[action],
        )

        with patch("research_center.topic_apply_service.load_change_pack", return_value=pack):
            with patch("research_center.topic_apply_service.backup_topic_files", return_value={"backup_root": "/tmp/bak"}):
                with patch("research_center.topic_apply_service.load_topic_profiles", return_value=[]):
                    with patch("research_center.topic_apply_service.load_company_topic_map", return_value={}):
                        with patch("research_center.topic_apply_service.load_supply_chain_nodes", return_value=[]):
                            with patch("research_center.topic_apply_service.save_topic_profiles"):
                                with patch("research_center.topic_apply_service.save_company_topic_map") as mock_save_map:
                                    with patch("research_center.topic_apply_service.save_supply_chain_nodes") as mock_save_nodes:
                                        with patch("research_center.topic_apply_service.update_change_pack_status"):
                                            with patch("research_center.topic_apply_service.write_topic_audit_log"):
                                                result = apply_service.confirm_change_pack("change_test")
                                                self.assertTrue(result.success)
                                                # Verify company map was saved
                                                saved_map = mock_save_map.call_args[0][0]
                                                self.assertIn("2330", saved_map)
                                                self.assertIn("3711", saved_map)
                                                # Verify supply chain nodes were saved
                                                saved_nodes = mock_save_nodes.call_args[0][0]
                                                self.assertEqual(len(saved_nodes), 1)
                                                self.assertEqual(saved_nodes[0].company_code, "2330")

    def test_confirm_preserves_evidence_in_company_map_and_supply_chain_nodes(self):
        """Formal company map and supply-chain nodes should inherit action evidence."""
        from research_center.topic_models import TopicChangeAction, TopicConfidence

        action = TopicChangeAction(
            action_type=TopicActionType.CREATE_THEME,
            theme_id="advanced_packaging",
            theme_name="Advanced Packaging",
            keywords=["CoWoS"],
            industries=["Semiconductor"],
            supply_chain_role="Core beneficiary",
            confidence=TopicConfidence.HIGH,
            reason="AI demand",
            evidence=[
                TopicEvidence(
                    source="TSMC",
                    source_level=TopicSourceLevel.L1_OFFICIAL,
                    content="CoWoS demand",
                    url="https://example.com",
                    publish_date="2026-01-01",
                )
            ],
            affected_companies=[
                {"company_code": "2330", "company_name": "TSMC", "role": "advanced packaging"},
            ],
            supply_chain_nodes=[
                {
                    "company_code": "2330",
                    "company_name": "TSMC",
                    "role": "CoWoS",
                    "upstream": ["HBM"],
                    "downstream": ["AI GPU"],
                    "product_keywords": ["CoWoS"],
                }
            ],
        )
        pack = TopicChangePack(
            change_id="change_evidence_test",
            parent_change_id=None,
            mode=TopicChangeMode.INITIAL,
            status=TopicChangeStatus.PENDING,
            model="gemini",
            created_at="2026-01-01T10:00:00+0800",
            updated_at="2026-01-01T10:00:00+0800",
            summary="Create advanced packaging theme",
            confidence="high",
            actions=[action],
        )

        with patch("research_center.topic_apply_service.load_change_pack", return_value=pack):
            with patch("research_center.topic_apply_service.backup_topic_files", return_value={"backup_root": "/tmp/bak"}):
                with patch("research_center.topic_apply_service.load_topic_profiles", return_value=[]):
                    with patch("research_center.topic_apply_service.load_company_topic_map", return_value={}):
                        with patch("research_center.topic_apply_service.load_supply_chain_nodes", return_value=[]):
                            with patch("research_center.topic_apply_service.save_topic_profiles"):
                                with patch("research_center.topic_apply_service.save_company_topic_map") as mock_save_map:
                                    with patch("research_center.topic_apply_service.save_supply_chain_nodes") as mock_save_nodes:
                                        with patch("research_center.topic_apply_service.update_change_pack_status"):
                                            with patch("research_center.topic_apply_service.write_topic_audit_log"):
                                                result = apply_service.confirm_change_pack("change_evidence_test")

        self.assertTrue(result.success)
        saved_map = mock_save_map.call_args[0][0]
        self.assertEqual(saved_map["2330"].company_name, "TSMC")
        self.assertEqual(saved_map["2330"].evidence[0]["source"], "TSMC")
        saved_nodes = mock_save_nodes.call_args[0][0]
        self.assertEqual(saved_nodes[0].theme_id, "advanced_packaging")
        self.assertEqual(saved_nodes[0].evidence[0]["source"], "TSMC")
        self.assertEqual(saved_nodes[0].source_level, "L1_official")

    def test_confirm_writes_structured_company_relations_and_node_fields(self):
        from research_center.topic_models import TopicChangeAction, TopicConfidence

        action = TopicChangeAction(
            action_type=TopicActionType.CREATE_THEME,
            theme_id="ai_power",
            theme_name="AI Power",
            keywords=["BBU"],
            industries=["Power"],
            supply_chain_role="AI power supply chain",
            confidence=TopicConfidence.HIGH,
            reason="AI power demand",
            evidence=[
                TopicEvidence(
                    source="Company IR",
                    source_level=TopicSourceLevel.L1_OFFICIAL,
                    content="AI power evidence",
                    url="https://example.com",
                    publish_date="2026-01-01",
                )
            ],
            company_relations=[
                {
                    "company_code": "2308",
                    "company_name": "Delta",
                    "role": "power supply",
                    "relation_strength": "high",
                    "relation_type": "direct",
                    "products": ["power supply", "BBU"],
                    "customers": ["CSP"],
                    "revenue_exposure": {"level": "high", "description": "AI power exposure"},
                    "benefit_logic": "AI rack power demand",
                    "evidence": [{"source": "Company IR", "content": "power"}],
                    "counter_evidence": [{"source": "Inventory", "content": "cycle risk"}],
                    "missing_data": ["exact AI revenue share"],
                }
            ],
            supply_chain_nodes=[
                {
                    "node_id": "ai_power_2308_power",
                    "theme_id": "ai_power",
                    "company_code": "2308",
                    "company_name": "Delta",
                    "layer": 3,
                    "role": "power supply",
                    "upstream": ["components"],
                    "downstream": ["AI rack"],
                    "product_keywords": ["BBU"],
                    "customers": ["CSP"],
                    "revenue_exposure": {"level": "high", "description": "AI power exposure"},
                    "benefit_logic": "AI rack power demand",
                    "confidence": "high",
                    "source_level": "L1_official",
                    "evidence": [{"source": "Company IR", "content": "power"}],
                }
            ],
        )
        pack = TopicChangePack(
            change_id="change_structured_company",
            parent_change_id=None,
            mode=TopicChangeMode.INITIAL,
            status=TopicChangeStatus.PENDING,
            model="gemini",
            created_at="2026-01-01T10:00:00+0800",
            updated_at="2026-01-01T10:00:00+0800",
            summary="Create AI power theme",
            confidence="high",
            actions=[action],
        )

        with patch("research_center.topic_apply_service.load_change_pack", return_value=pack):
            with patch("research_center.topic_apply_service.backup_topic_files", return_value={"backup_root": "/tmp/bak"}):
                with patch("research_center.topic_apply_service.load_topic_profiles", return_value=[]):
                    with patch("research_center.topic_apply_service.load_company_topic_map", return_value={}):
                        with patch("research_center.topic_apply_service.load_supply_chain_nodes", return_value=[]):
                            with patch("research_center.topic_apply_service.save_topic_profiles"):
                                with patch("research_center.topic_apply_service.save_company_topic_map") as mock_save_map:
                                    with patch("research_center.topic_apply_service.save_supply_chain_nodes") as mock_save_nodes:
                                        with patch("research_center.topic_apply_service.update_change_pack_status"):
                                            with patch("research_center.topic_apply_service.write_topic_audit_log"):
                                                result = apply_service.confirm_change_pack("change_structured_company")

        self.assertTrue(result.success)
        saved_map = mock_save_map.call_args[0][0]
        rel = saved_map["2308"]
        self.assertEqual(rel.relation_strength, "high")
        self.assertEqual(rel.relation_type, "direct")
        self.assertEqual(rel.products, ["power supply", "BBU"])
        self.assertEqual(rel.revenue_exposure["level"], "high")
        self.assertEqual(rel.counter_evidence[0]["source"], "Inventory")
        saved_node = mock_save_nodes.call_args[0][0][0]
        self.assertEqual(saved_node.layer, 3)
        self.assertEqual(saved_node.customers, ["CSP"])
        self.assertEqual(saved_node.revenue_exposure["level"], "high")
        self.assertEqual(saved_node.benefit_logic, "AI rack power demand")

    def test_confirm_deduplicates_supply_chain_nodes(self):
        from research_center.topic_models import TopicChangeAction, TopicConfidence, TopicSupplyChainNode

        existing_node = TopicSupplyChainNode(
            node_id="ai_power_2308_power",
            theme_id="ai_power",
            company_code="2308",
            company_name="Delta",
            role="power supply",
            product_keywords=["power supply"],
        )
        action = TopicChangeAction(
            action_type=TopicActionType.UPDATE_THEME,
            theme_id="ai_power",
            theme_name="AI Power",
            confidence=TopicConfidence.HIGH,
            supply_chain_nodes=[
                {
                    "node_id": "ai_power_2308_power",
                    "theme_id": "ai_power",
                    "company_code": "2308",
                    "company_name": "Delta",
                    "role": "power supply",
                    "product_keywords": ["BBU"],
                }
            ],
        )
        pack = TopicChangePack(
            change_id="change_dedupe_node",
            parent_change_id=None,
            mode=TopicChangeMode.UPDATE,
            status=TopicChangeStatus.PENDING,
            model="gemini",
            created_at="2026-01-01T10:00:00+0800",
            updated_at="2026-01-01T10:00:00+0800",
            summary="Update AI power theme",
            confidence="high",
            actions=[action],
        )

        with patch("research_center.topic_apply_service.load_change_pack", return_value=pack):
            with patch("research_center.topic_apply_service.backup_topic_files", return_value={"backup_root": "/tmp/bak"}):
                with patch("research_center.topic_apply_service.load_topic_profiles", return_value=[]):
                    with patch("research_center.topic_apply_service.load_company_topic_map", return_value={}):
                        with patch("research_center.topic_apply_service.load_supply_chain_nodes", return_value=[existing_node]):
                            with patch("research_center.topic_apply_service.save_topic_profiles"):
                                with patch("research_center.topic_apply_service.save_company_topic_map"):
                                    with patch("research_center.topic_apply_service.save_supply_chain_nodes") as mock_save_nodes:
                                        with patch("research_center.topic_apply_service.update_change_pack_status"):
                                            with patch("research_center.topic_apply_service.write_topic_audit_log"):
                                                result = apply_service.confirm_change_pack("change_dedupe_node")

        self.assertTrue(result.success)
        saved_nodes = mock_save_nodes.call_args[0][0]
        self.assertEqual(len(saved_nodes), 1)
        self.assertEqual(saved_nodes[0].product_keywords, ["power supply", "BBU"])

    def test_confirm_retains_explicit_candidate_company_relation_as_hypothesis(self):
        from research_center.topic_models import TopicChangeAction, TopicConfidence

        action = TopicChangeAction(
            action_type=TopicActionType.CREATE_THEME,
            theme_id="candidate_theme",
            theme_name="Candidate Theme",
            confidence=TopicConfidence.MEDIUM,
            company_relations=[
                {
                    "company_code": "9999",
                    "company_name": "Candidate Co",
                    "role": "unverified role",
                    "verification_status": "candidate",
                    "products": ["unverified product"],
                }
            ],
        )
        pack = TopicChangePack(
            change_id="change_candidate_skip",
            parent_change_id=None,
            mode=TopicChangeMode.INITIAL,
            status=TopicChangeStatus.PENDING,
            model="gemini",
            created_at="2026-01-01T10:00:00+0800",
            updated_at="2026-01-01T10:00:00+0800",
            summary="candidate skip",
            confidence="medium",
            actions=[action],
        )

        with patch("research_center.topic_apply_service.load_change_pack", return_value=pack):
            with patch("research_center.topic_apply_service.backup_topic_files", return_value={"backup_root": "/tmp/bak"}):
                with patch("research_center.topic_apply_service.load_topic_profiles", return_value=[]):
                    with patch("research_center.topic_apply_service.load_company_topic_map", return_value={}):
                        with patch("research_center.topic_apply_service.load_supply_chain_nodes", return_value=[]):
                            with patch("research_center.topic_apply_service.save_topic_profiles"):
                                with patch("research_center.topic_apply_service.save_company_topic_map") as mock_save_map:
                                    with patch("research_center.topic_apply_service.save_supply_chain_nodes"):
                                        with patch("research_center.topic_apply_service.update_change_pack_status"):
                                            with patch("research_center.topic_apply_service.write_topic_audit_log"):
                                                result = apply_service.confirm_change_pack("change_candidate_skip")

        self.assertTrue(result.success)
        saved_map = mock_save_map.call_args[0][0]
        self.assertIn("9999", saved_map)
        relation = saved_map["9999"]
        self.assertNotIn("candidate_theme", relation.themes)
        self.assertEqual(relation.extra.get("candidate_usage_policy"), "hypothesis_only")
        self.assertEqual(relation.extra["candidate_themes"][0]["theme_id"], "candidate_theme")
        self.assertTrue(relation.extra["candidate_themes"][0]["not_representative"])

    def test_confirm_applies_inferred_relation_but_skips_candidate_field_value(self):
        from research_center.topic_models import TopicChangeAction, TopicConfidence

        action = TopicChangeAction(
            action_type=TopicActionType.CREATE_THEME,
            theme_id="ai_power_inferred",
            theme_name="AI Power Inferred",
            confidence=TopicConfidence.MEDIUM,
            company_relations=[
                {
                    "company_code": "2308",
                    "company_name": "Delta",
                    "role": "power",
                    "verification_status": "inferred",
                    "products": {"value": ["BBU"], "status": "verified", "evidence": [{"source": "IR", "content": "BBU"}]},
                    "benefit_logic": {"value": "unverified logic", "status": "candidate"},
                    "evidence": [{"source": "Media", "source_level": "L2_media", "content": "AI power demand"}],
                }
            ],
        )
        pack = TopicChangePack(
            change_id="change_inferred_relation",
            parent_change_id=None,
            mode=TopicChangeMode.INITIAL,
            status=TopicChangeStatus.PENDING,
            model="gemini",
            created_at="2026-01-01T10:00:00+0800",
            updated_at="2026-01-01T10:00:00+0800",
            summary="inferred relation",
            confidence="medium",
            actions=[action],
        )

        with patch("research_center.topic_apply_service.load_change_pack", return_value=pack):
            with patch("research_center.topic_apply_service.backup_topic_files", return_value={"backup_root": "/tmp/bak"}):
                with patch("research_center.topic_apply_service.load_topic_profiles", return_value=[]):
                    with patch("research_center.topic_apply_service.load_company_topic_map", return_value={}):
                        with patch("research_center.topic_apply_service.load_supply_chain_nodes", return_value=[]):
                            with patch("research_center.topic_apply_service.save_topic_profiles"):
                                with patch("research_center.topic_apply_service.save_company_topic_map") as mock_save_map:
                                    with patch("research_center.topic_apply_service.save_supply_chain_nodes"):
                                        with patch("research_center.topic_apply_service.update_change_pack_status"):
                                            with patch("research_center.topic_apply_service.write_topic_audit_log"):
                                                result = apply_service.confirm_change_pack("change_inferred_relation")

        self.assertTrue(result.success)
        rel = mock_save_map.call_args[0][0]["2308"]
        self.assertEqual(rel.products, ["BBU"])
        self.assertEqual(rel.benefit_logic, "")

    def test_reject_nonexistent_returns_error(self):
        with patch("research_center.topic_apply_service.load_change_pack", return_value=None):
            result = apply_service.reject_change_pack("nonexistent")
            self.assertFalse(result.get("success"))

    def test_confirm_writes_company_knowledge_updates(self):
        from research_center.topic_models import TopicChangeAction, TopicConfidence

        action = TopicChangeAction(
            action_type=TopicActionType.CREATE_THEME,
            theme_id="advanced_packaging",
            theme_name="先進封裝",
            confidence=TopicConfidence.HIGH,
            reason="test",
        )
        pack = TopicChangePack(
            change_id="change_company_knowledge",
            parent_change_id=None,
            mode=TopicChangeMode.INITIAL,
            status=TopicChangeStatus.PENDING,
            model="minimax",
            created_at="2026-01-01T10:00:00+0800",
            updated_at="2026-01-01T10:00:00+0800",
            summary="test",
            confidence="high",
            actions=[action],
            company_knowledge_updates={
                "companies": {
                    "2330": {
                        "company_name": "TSMC",
                        "product_lines": ["CoWoS"],
                        "customers": ["AI chip customers"],
                        "evidence_sources": [{"source": "IR", "content": "CoWoS"}],
                    }
                }
            },
        )
        with patch("research_center.topic_apply_service.load_change_pack", return_value=pack):
            with patch("research_center.topic_apply_service.backup_topic_files", return_value={"backup_root": "/tmp/bak"}):
                with patch("research_center.topic_apply_service.load_topic_profiles", return_value=[]):
                    with patch("research_center.topic_apply_service.load_company_topic_map", return_value={}):
                        with patch("research_center.topic_apply_service.load_supply_chain_nodes", return_value=[]):
                            with patch("research_center.topic_apply_service.load_company_knowledge_data", return_value={"metadata": {}, "companies": {}}):
                                with patch("research_center.topic_apply_service.save_topic_profiles"):
                                    with patch("research_center.topic_apply_service.save_company_topic_map"):
                                        with patch("research_center.topic_apply_service.save_supply_chain_nodes"):
                                            with patch("research_center.topic_apply_service.save_company_knowledge_data") as mock_save_knowledge:
                                                with patch("research_center.topic_apply_service.update_change_pack_status"):
                                                    with patch("research_center.topic_apply_service.write_topic_audit_log"):
                                                        result = apply_service.confirm_change_pack("change_company_knowledge")

        self.assertTrue(result.success)
        saved = mock_save_knowledge.call_args[0][0]
        self.assertEqual(saved["companies"]["2330"]["product_lines"], ["CoWoS"])
        self.assertEqual(saved["companies"]["2330"]["customers"], ["AI chip customers"])

    def test_confirm_normalizes_company_knowledge_list_wrappers_and_traditional_text(self):
        from research_center.topic_models import TopicChangeAction, TopicConfidence

        action = TopicChangeAction(
            action_type=TopicActionType.CREATE_THEME,
            theme_id="high_speed_networking_switch",
            theme_name="高速网络交换器",
            confidence=TopicConfidence.HIGH,
            reason="test",
        )
        pack = TopicChangePack(
            change_id="change_company_knowledge_normalize",
            parent_change_id=None,
            mode=TopicChangeMode.INITIAL,
            status=TopicChangeStatus.PENDING,
            model="minimax",
            created_at="2026-01-01T10:00:00+0800",
            updated_at="2026-01-01T10:00:00+0800",
            summary="test",
            confidence="high",
            actions=[action],
            company_knowledge_updates={
                "companies": {
                    "2345": {
                        "company_name": "智邦",
                        "product_lines": [{"value": ["网络交换器", "资料中心网通设备"], "status": "candidate"}],
                        "customers": [{"value": "云服务客户", "status": "candidate"}],
                        "risk_notes": ["客户验证不足"],
                    }
                }
            },
        )
        with patch("research_center.topic_apply_service.load_change_pack", return_value=pack):
            with patch("research_center.topic_apply_service.backup_topic_files", return_value={"backup_root": "/tmp/bak"}):
                with patch("research_center.topic_apply_service.load_topic_profiles", return_value=[]):
                    with patch("research_center.topic_apply_service.load_company_topic_map", return_value={}):
                        with patch("research_center.topic_apply_service.load_supply_chain_nodes", return_value=[]):
                            with patch("research_center.topic_apply_service.load_company_knowledge_data", return_value={"metadata": {}, "companies": {}}):
                                with patch("research_center.topic_apply_service.save_topic_profiles"):
                                    with patch("research_center.topic_apply_service.save_company_topic_map"):
                                        with patch("research_center.topic_apply_service.save_supply_chain_nodes"):
                                            with patch("research_center.topic_apply_service.save_company_knowledge_data") as mock_save_knowledge:
                                                with patch("research_center.topic_apply_service.update_change_pack_status"):
                                                    with patch("research_center.topic_apply_service.write_topic_audit_log"):
                                                        result = apply_service.confirm_change_pack("change_company_knowledge_normalize")

        self.assertTrue(result.success)
        saved = mock_save_knowledge.call_args[0][0]
        entry = saved["companies"]["2345"]
        self.assertEqual(entry["product_lines"], ["網路交換器", "資料中心網通設備"])
        self.assertEqual(entry["customers"], ["雲服務客戶"])
        self.assertEqual(entry["risk_notes"], ["客戶驗證不足"])

    def test_reject_non_pending_returns_error(self):
        pack = TopicChangePack(
            change_id="change_test",
            parent_change_id=None,
            mode=TopicChangeMode.INITIAL,
            status=TopicChangeStatus.CONFIRMED,
            model="gemini",
            created_at="2026-01-01T10:00:00+0800",
            updated_at="2026-01-01T10:00:00+0800",
            summary="test",
            confidence="high",
            actions=[],
        )
        with patch("research_center.topic_apply_service.load_change_pack", return_value=pack):
            result = apply_service.reject_change_pack("change_test")
            self.assertFalse(result.get("success"))

    def test_reject_pending_writes_audit(self):
        pack = TopicChangePack(
            change_id="change_reject_test",
            parent_change_id=None,
            mode=TopicChangeMode.UPDATE,
            status=TopicChangeStatus.PENDING,
            model="gemini",
            created_at="2026-01-01T10:00:00+0800",
            updated_at="2026-01-01T10:00:00+0800",
            summary="test",
            confidence="medium",
            actions=[],
        )
        with patch("research_center.topic_apply_service.load_change_pack", return_value=pack):
            with patch("research_center.topic_apply_service.update_change_pack_status") as mock_update:
                with patch("research_center.topic_apply_service.write_topic_audit_log") as mock_audit:
                    result = apply_service.reject_change_pack("change_reject_test", "user1", "not needed")
                    self.assertTrue(result.get("success"))
                    mock_update.assert_called_once()
                    mock_audit.assert_called_once()


if __name__ == "__main__":
    unittest.main()
