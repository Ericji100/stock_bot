"""Tests for topic_repository.py."""
import json
import unittest
import uuid
from pathlib import Path

from research_center.topic_models import (
    TopicChangeMode,
    TopicChangePack,
    TopicChangeStatus,
    TopicProfile,
)
from research_center import topic_repository as repo
from tests.test_cache_utils import ensure_test_cache_dir, safe_remove_test_cache


class TestTopicRepositoryTempDir(unittest.TestCase):
    """Run with workspace .cache/test_tmp/ to avoid Windows PermissionError."""

    def setUp(self):
        self._orig_root = repo.ROOT
        self._orig_pack_dir = repo._CHANGE_PACK_DIR
        self._orig_audit_dir = repo._AUDIT_DIR
        self._orig_prompt_dir = repo._PROMPT_LOG_DIR
        self._orig_raw_dir = repo._RAW_RESP_DIR
        self._orig_backup_dir = repo._BACKUP_DIR
        self._orig_topic_profiles = repo._TOPIC_PROFILES_PATH
        self._orig_company_map = repo._COMPANY_TOPIC_MAP_PATH
        self._orig_supply_chain = repo._SUPPLY_CHAIN_PATH
        self._orig_company_knowledge = repo._COMPANY_KNOWLEDGE_PATH

        # Use unique key per-test workspace cache dir
        self._cache_key = f"topic_repo_{self._testMethodName}_{uuid.uuid4().hex}"
        self.cache_dir = ensure_test_cache_dir(self._cache_key)
        repo.ROOT = self.cache_dir
        repo._CHANGE_PACK_DIR = repo.ROOT / "data" / "topic" / "change_packs"
        repo._AUDIT_DIR = repo.ROOT / "data" / "topic" / "audit_logs"
        repo._PROMPT_LOG_DIR = repo.ROOT / "logs" / "ai_prompts"
        repo._RAW_RESP_DIR = repo.ROOT / "logs" / "topic_ai_raw"
        repo._BACKUP_DIR = repo.ROOT / "data" / "topic" / "backup"
        repo._TOPIC_PROFILES_PATH = repo.ROOT / "config" / "theme_profiles.json"
        repo._COMPANY_TOPIC_MAP_PATH = repo.ROOT / "config" / "company_theme_map.json"
        repo._SUPPLY_CHAIN_PATH = repo.ROOT / "config" / "supply_chain_nodes.json"
        repo._COMPANY_KNOWLEDGE_PATH = repo.ROOT / "config" / "company_knowledge.json"
        repo._ensure_dirs()

        # Explicitly verify the formal library files do not exist.
        # This guards against stale data that survived a previous cleanup attempt.
        for formal_path in [
            repo._TOPIC_PROFILES_PATH,
            repo._COMPANY_TOPIC_MAP_PATH,
            repo._SUPPLY_CHAIN_PATH,
            repo._COMPANY_KNOWLEDGE_PATH,
        ]:
            if formal_path.exists():
                # Force-remove and verify gone
                try:
                    formal_path.unlink()
                except Exception:
                    pass
                if formal_path.exists():
                    raise AssertionError(
                        f"Formal library file still exists after deletion: {formal_path}. "
                        "Possible Windows lock or permission issue."
                    )
        # Final verification
        self.assertEqual(repo.load_topic_profiles(), [])

    def tearDown(self):
        safe_remove_test_cache(self._cache_key)
        repo.ROOT = self._orig_root
        repo._CHANGE_PACK_DIR = self._orig_pack_dir
        repo._AUDIT_DIR = self._orig_audit_dir
        repo._PROMPT_LOG_DIR = self._orig_prompt_dir
        repo._RAW_RESP_DIR = self._orig_raw_dir
        repo._BACKUP_DIR = self._orig_backup_dir
        repo._TOPIC_PROFILES_PATH = self._orig_topic_profiles
        repo._COMPANY_TOPIC_MAP_PATH = self._orig_company_map
        repo._SUPPLY_CHAIN_PATH = self._orig_supply_chain
        repo._COMPANY_KNOWLEDGE_PATH = self._orig_company_knowledge

    def test_save_and_load_change_pack(self):
        pack = TopicChangePack(
            change_id="change_test_001",
            parent_change_id=None,
            mode=TopicChangeMode.INITIAL,
            status=TopicChangeStatus.PENDING,
            model="gemini",
            created_at="2026-01-01T10:00:00+0800",
            updated_at="2026-01-01T10:00:00+0800",
            summary="Test pack",
            confidence="high",
            actions=[],
        )
        repo.save_change_pack(pack)
        loaded = repo.load_change_pack("change_test_001")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.change_id, "change_test_001")
        self.assertEqual(loaded.status, TopicChangeStatus.PENDING)

    def test_load_nonexistent_returns_none(self):
        result = repo.load_change_pack("nonexistent")
        self.assertIsNone(result)

    def test_list_change_packs(self):
        for i in range(3):
            pack = TopicChangePack(
                change_id=f"change_list_{i}",
                parent_change_id=None,
                mode=TopicChangeMode.UPDATE,
                status=TopicChangeStatus.PENDING,
                model="gemini",
                created_at="2026-01-01T10:00:00+0800",
                updated_at="2026-01-01T10:00:00+0800",
                summary=f"Pack {i}",
                confidence="medium",
                actions=[],
            )
            repo.save_change_pack(pack)
        packs = repo.list_change_packs()
        self.assertEqual(len(packs), 3)
        ids = [p.change_id for p in packs]
        self.assertIn("change_list_0", ids)

    def test_list_change_packs_filtered_by_status(self):
        pack1 = TopicChangePack(
            change_id="change_pending_1",
            parent_change_id=None,
            mode=TopicChangeMode.UPDATE,
            status=TopicChangeStatus.PENDING,
            model="gemini",
            created_at="2026-01-01T10:00:00+0800",
            updated_at="2026-01-01T10:00:00+0800",
            summary="Pending",
            confidence="medium",
            actions=[],
        )
        pack2 = TopicChangePack(
            change_id="change_confirmed_1",
            parent_change_id=None,
            mode=TopicChangeMode.UPDATE,
            status=TopicChangeStatus.CONFIRMED,
            model="gemini",
            created_at="2026-01-01T11:00:00+0800",
            updated_at="2026-01-01T11:00:00+0800",
            summary="Confirmed",
            confidence="medium",
            actions=[],
        )
        repo.save_change_pack(pack1)
        repo.save_change_pack(pack2)

        pending = repo.list_change_packs(TopicChangeStatus.PENDING)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].change_id, "change_pending_1")

        confirmed = repo.list_change_packs(TopicChangeStatus.CONFIRMED)
        self.assertEqual(len(confirmed), 1)
        self.assertEqual(confirmed[0].change_id, "change_confirmed_1")

    def test_update_change_pack_status(self):
        pack = TopicChangePack(
            change_id="change_status_test",
            parent_change_id=None,
            mode=TopicChangeMode.UPDATE,
            status=TopicChangeStatus.PENDING,
            model="gemini",
            created_at="2026-01-01T10:00:00+0800",
            updated_at="2026-01-01T10:00:00+0800",
            summary="Status test",
            confidence="medium",
            actions=[],
        )
        repo.save_change_pack(pack)
        result = repo.update_change_pack_status("change_status_test", TopicChangeStatus.CONFIRMED)
        self.assertTrue(result)
        loaded = repo.load_change_pack("change_status_test")
        self.assertEqual(loaded.status, TopicChangeStatus.CONFIRMED)

    def test_save_and_load_topic_profiles(self):
        profiles = [
            TopicProfile(theme_id="ai_server", theme_name="AI伺服器", keywords=["AI"]),
            TopicProfile(theme_id="hbm_memory", theme_name="HBM記憶體", keywords=["HBM"]),
        ]
        repo.save_topic_profiles(profiles)
        loaded = repo.load_topic_profiles()
        self.assertEqual(len(loaded), 2)
        ids = [p.theme_id for p in loaded]
        self.assertIn("ai_server", ids)
        self.assertIn("hbm_memory", ids)

    def test_load_topic_profiles_nonexistent(self):
        profiles = repo.load_topic_profiles()
        self.assertEqual(profiles, [])

    def test_is_formal_library_empty(self):
        # Guard: initial state must be clean
        self.assertFalse(repo._TOPIC_PROFILES_PATH.exists(), "theme_profiles.json should not exist before test writes")
        self.assertEqual(repo.load_topic_profiles(), [], "load_topic_profiles should return empty list when no file")
        self.assertTrue(repo.is_formal_library_empty(), "is_formal_library_empty should be True on clean setUp")

        # Write one profile and verify state change
        profiles = [TopicProfile(theme_id="ai_server", theme_name="AI伺服器")]
        repo.save_topic_profiles(profiles)
        self.assertTrue(repo._TOPIC_PROFILES_PATH.exists(), "theme_profiles.json should exist after save")
        self.assertFalse(repo.is_formal_library_empty(), "is_formal_library_empty should be False after save")

    def test_backup_topic_files(self):
        profiles = [TopicProfile(theme_id="test", theme_name="Test")]
        repo.save_topic_profiles(profiles)
        repo.save_company_knowledge_data({"metadata": {"source": "test"}, "companies": {"2330": {"company_name": "TSMC"}}})
        result = repo.backup_topic_files("unit_test")
        self.assertIn("backup_root", result)
        self.assertTrue(Path(result["backup_root"]).exists())
        self.assertIn("company_knowledge.json", result["backed"])

    def test_save_and_load_company_knowledge_data(self):
        data = {
            "metadata": {"source": "unit_test"},
            "companies": {"2330": {"company_name": "TSMC", "product_lines": ["CoWoS"]}},
        }
        repo.save_company_knowledge_data(data)
        loaded = repo.load_company_knowledge_data()
        self.assertEqual(loaded["companies"]["2330"]["product_lines"], ["CoWoS"])


if __name__ == "__main__":
    unittest.main()
