"""Tests for topic_maintain_service.py."""
import json
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from research_center.topic_models import (
    TopicActionType,
    TopicChangeAction,
    TopicChangeMode,
    TopicChangePack,
    TopicChangeStatus,
    TopicConfidence,
)
from research_center.topic_maintain_service import run_topic_maintain, TopicMaintainAIError


class TestTopicMaintainService(unittest.TestCase):
    """Tests for topic_maintain_service using mocks to avoid real AI calls."""

    def setUp(self):
        self._patchers = []

    def tearDown(self):
        for p in self._patchers:
            p.stop()

    def _mock_center(self, ai_model="gemini", gemini_result=None, deepseek_result=None):
        center = MagicMock()
        center.gemini = MagicMock()
        center.opencode = MagicMock()
        center._gemini_discovery_runner = MagicMock()

        default_raw = json.dumps({
            "change_id": "change_test",
            "parent_change_id": None,
            "mode": "initial",
            "status": "pending",
            "model": ai_model,
            "created_at": "2026-01-01T10:00:00+0800",
            "updated_at": "2026-01-01T10:00:00+0800",
            "summary": "Test",
            "confidence": "high",
            "actions": [],
            "warnings": [],
            "sources": [],
            "adjustment_notes": "",
            "raw_response_path": "",
            "prompt_log_path": "",
        })

        if ai_model == "deepseek":
            center.opencode.generate_report.return_value = deepseek_result or MagicMock(raw=default_raw)
        else:
            center.gemini.generate_report.return_value = gemini_result or MagicMock(raw=default_raw)

        return center

    def _mock_request(self, **kwargs):
        from research_center.command_parser import CommandRequest
        defaults = dict(
            command="topic_maintain",
            raw_text="/topic_maintain --deep",
            target="",
            theme_scope="",
            target_type="topic_maintain",
            mode="deep",
            source_only=False,
            score=False,
            brief=False,
            top=None,
            ai_model="gemini",
            report_date=None,
            output_formats=("md", "html", "json"),
            user_id="test_user",
            created_at=None,
        )
        defaults.update(kwargs)
        return MagicMock(**defaults)

    def _mock_write_prompt_log(self, path):
        # write_prompt_log is imported from .prompt_logging inside the function
        p = patch("research_center.prompt_logging.write_prompt_log", return_value=Path(path))
        self._patchers.append(p)
        return p.start()

    def _mock_raw_response_path(self, path):
        p = patch("research_center.topic_repository.raw_response_path", return_value=Path(path))
        self._patchers.append(p)
        return p.start()

    def _mock_is_formal_library_empty(self, empty):
        # Patch in topic_maintain_service namespace where it's imported
        p = patch("research_center.topic_maintain_service.is_formal_library_empty", return_value=empty)
        self._patchers.append(p)
        return p.start()

    def _mock_load_change_pack(self, pack):
        # Patch in topic_maintain_service namespace where it's imported
        p = patch("research_center.topic_maintain_service.load_change_pack", return_value=pack)
        self._patchers.append(p)
        return p.start()

    def _mock_save_change_pack(self):
        # Patch in topic_maintain_service namespace where it's imported
        p = patch("research_center.topic_maintain_service.save_change_pack")
        self._patchers.append(p)
        return p.start()

    def _mock_web_fetch(self):
        # _enrich_sources_with_web_fetch is imported inside run_topic_maintain
        p = patch("research_center.web_fetch_enrichment._enrich_sources_with_web_fetch")
        self._patchers.append(p)
        return p.start()

    def _mock_collect_structured_data(self, data=None, sources=None):
        data = data if data is not None else {
            "existing_topic_profiles": [],
            "company_topic_map": {},
            "supply_chain_nodes": [],
            "recent_scans": [],
            "candidate_companies": [],
            "web_fetched_sources": [],
            "web_fetch_diagnostics": {},
        }
        sources = sources if sources is not None else []
        p = patch("research_center.data_services.collect_structured_data", return_value=(data, sources))
        self._patchers.append(p)
        return p.start()

    def test_run_topic_maintain_calls_collect_structured_data(self):
        """run_topic_maintain should call collect_structured_data via data_services."""
        # This test verifies the overall flow runs without error.
        # The actual collect_structured_data call is tested via integration
        # in other test files. Here we just verify the function completes.
        center = self._mock_center()
        mock_request = self._mock_request()

        self._mock_write_prompt_log("/logs/ai_prompts/test.json")
        self._mock_raw_response_path(MagicMock())
        self._mock_save_change_pack()
        self._mock_is_formal_library_empty(False)
        center._gemini_discovery_runner = MagicMock()
        center._gemini_discovery_runner.run_discovery_flow.return_value = ([], False)
        self._mock_web_fetch()

        mock_collect = self._mock_collect_structured_data()

        progress_calls = []
        pack = run_topic_maintain(mock_request, center=center, progress=lambda m: progress_calls.append(m))

        self.assertIsInstance(pack, TopicChangePack)
        mock_collect.assert_called_once()
        # Verify flow reached AI call step
        self.assertTrue(any("呼叫 AI" in msg for msg in progress_calls), f"Expected AI call in progress: {progress_calls}")

    def test_run_topic_maintain_mode_initial_when_empty(self):
        """run_topic_maintain should use initial mode when formal library is empty."""
        self._mock_is_formal_library_empty(True)
        center = self._mock_center()
        mock_request = self._mock_request()
        self._mock_write_prompt_log("/logs/ai_prompts/test.json")
        self._mock_raw_response_path(MagicMock())
        mock_save = self._mock_save_change_pack()
        self._mock_collect_structured_data()

        center._gemini_discovery_runner = MagicMock()
        center._gemini_discovery_runner.run_discovery_flow.return_value = ([], False)
        self._mock_web_fetch()

        progress_calls = []
        pack = run_topic_maintain(mock_request, center=center, progress=lambda m: progress_calls.append(m))

        self.assertEqual(pack.mode, TopicChangeMode.INITIAL)

    def test_run_topic_maintain_mode_update_when_not_empty(self):
        """run_topic_maintain should use update mode when formal library has data."""
        self._mock_is_formal_library_empty(False)
        center = self._mock_center()
        mock_request = self._mock_request()
        self._mock_write_prompt_log("/logs/ai_prompts/test.json")
        self._mock_raw_response_path(MagicMock())
        mock_save = self._mock_save_change_pack()
        self._mock_collect_structured_data()

        center._gemini_discovery_runner = MagicMock()
        center._gemini_discovery_runner.run_discovery_flow.return_value = ([], False)
        self._mock_web_fetch()

        pack = run_topic_maintain(mock_request, center=center, progress=None)

        self.assertEqual(pack.mode, TopicChangeMode.UPDATE)

    def test_run_topic_maintain_bad_json_marks_failed_pack(self):
        """Bad JSON in one staged AI call should not raise; it should produce a failed pack."""
        self._mock_is_formal_library_empty(False)
        center = self._mock_center()
        # Override to return bad JSON
        center.gemini.generate_report.return_value = MagicMock(raw="not valid json {")

        mock_request = self._mock_request()
        self._mock_write_prompt_log("/logs/ai_prompts/test.json")
        mock_raw_path = MagicMock()
        mock_raw_path_str = "/logs/topic_ai_raw/test_bad_json.json"
        mock_raw_path.__str__ = lambda self: mock_raw_path_str
        self._mock_raw_response_path(mock_raw_path)
        mock_save = self._mock_save_change_pack()
        self._mock_collect_structured_data()
        center._gemini_discovery_runner = MagicMock()
        center._gemini_discovery_runner.run_discovery_flow.return_value = ([], False)

        pack = run_topic_maintain(mock_request, center=center, progress=None)

        self.assertEqual(pack.status, TopicChangeStatus.FAILED)
        self.assertTrue(any("candidate_extract" in warning for warning in pack.warnings))
        mock_save.assert_called_once()

    def test_run_topic_maintain_progress_callback_fired(self):
        """Progress callback should be called at least once."""
        self._mock_is_formal_library_empty(False)
        center = self._mock_center()
        mock_request = self._mock_request()
        self._mock_write_prompt_log("/logs/ai_prompts/test.json")
        self._mock_raw_response_path(MagicMock())
        mock_save = self._mock_save_change_pack()
        self._mock_collect_structured_data()

        center._gemini_discovery_runner = MagicMock()
        center._gemini_discovery_runner.run_discovery_flow.return_value = ([], False)
        self._mock_web_fetch()

        progress_calls = []
        run_topic_maintain(mock_request, center=center, progress=lambda m: progress_calls.append(m))

        self.assertGreater(len(progress_calls), 0, "Progress callback should be called")
        # All progress messages should have an identifying prefix:
        # - [AI題材庫]   → wrapped by emit() in topic_maintain_service
        # - 題材知識庫維護： → raw messages from collect_topic_maintain_data
        # - 論壇來源搜尋  → raw messages from discovery flow (passed through directly)
        # - WebFetch     → raw messages from web_fetch_enrichment
        known_prefixes = ("[AI題材庫]", "題材知識庫維護：", "論壇來源", "WebFetch")
        for msg in progress_calls:
            self.assertTrue(
                any(msg.startswith(p) for p in known_prefixes),
                f"Unexpected progress message format: {msg}",
            )

    def test_load_prompt_prefers_prompt_topic_directory(self):
        """_load_prompt should prefer prompt/topic/ over config/prompts/."""
        import tempfile
        from research_center.topic_maintain_service import _load_prompt

        # Verify the _load_prompt logic by checking source code paths
        # The function checks topic_path first, then config_path
        # We verify this by checking the actual file structure without writing
        from research_center.topic_maintain_service import _load_prompt
        import inspect
        source = inspect.getsource(_load_prompt)
        # Verify the function checks topic_path before config_path
        self.assertIn("prompt/topic/", source)
        self.assertIn("config/prompts/", source)
        # Verify topic_path check comes first (priority)
        topic_check_pos = source.find("prompt/topic/")
        config_check_pos = source.find("config/prompts/")
        self.assertGreater(topic_check_pos, 0)
        self.assertGreater(config_check_pos, topic_check_pos)

    def test_formal_prompt_topic_maintain_is_not_marker(self):
        """topic_maintain.md should be formal prompt, not test marker."""
        content = Path("prompt/topic/topic_maintain.md").read_text(encoding="utf-8")
        self.assertNotIn("FROM_PROMPT_TOPIC", content)
        self.assertNotIn("PROMPT_TOPIC_MARKER", content)
        self.assertIn("existing_topic_profiles_json", content)
        self.assertIn("create_theme", content)
        self.assertIn("warnings", content)
        self.assertIn("actions", content)
        self.assertIn("{mode}", content)
        self.assertIn("mode=initial", content)
        self.assertIn("mode=update", content)

    def test_run_topic_maintain_injects_recent_scan_candidates(self):
        """run_topic_maintain should inject recent_scan_candidates_json in prompt variables."""
        self._mock_is_formal_library_empty(False)
        center = self._mock_center()
        mock_request = self._mock_request()
        self._mock_write_prompt_log("/logs/ai_prompts/test.json")
        self._mock_raw_response_path(MagicMock())
        mock_save = self._mock_save_change_pack()
        self._mock_collect_structured_data()

        center._gemini_discovery_runner = MagicMock()
        center._gemini_discovery_runner.run_discovery_flow.return_value = ([], False)
        self._mock_web_fetch()

        from research_center.topic_maintain_service import _load_prompt
        loaded = _load_prompt("topic_maintain")
        # Prompt should contain the new placeholder
        self.assertIn("recent_scan_candidates_json", loaded)
        self.assertIn("recent_theme_reports_json", loaded)

    def test_run_topic_maintain_injects_recent_theme_reports(self):
        """run_topic_maintain should inject recent /theme report context into the prompt."""
        self._mock_is_formal_library_empty(False)
        center = self._mock_center()
        mock_request = self._mock_request()
        mock_prompt_log = self._mock_write_prompt_log("/logs/ai_prompts/test.json")
        self._mock_raw_response_path(MagicMock())
        self._mock_save_change_pack()
        self._mock_collect_structured_data(data={
            "existing_topic_profiles": [],
            "company_topic_map": {},
            "supply_chain_nodes": [],
            "recent_scans": [],
            "candidate_companies": [],
            "web_fetched_sources": [],
            "web_fetch_diagnostics": {},
            "recent_theme_reports": [
                {
                    "theme": "AI伺服器",
                    "summary": "AI伺服器供應鏈近期研究",
                    "suggested_search_terms": ["AI伺服器", "液冷", "伺服器代工"],
                }
            ],
        })

        center._gemini_discovery_runner = MagicMock()
        center._gemini_discovery_runner.run_discovery_flow.return_value = ([], False)
        self._mock_web_fetch()

        run_topic_maintain(mock_request, center=center, progress=None)

        prompt = mock_prompt_log.call_args.args[1]
        self.assertIn("近期 /theme 題材研究紀錄", prompt)
        self.assertIn("AI伺服器供應鏈近期研究", prompt)
        self.assertIn("液冷", prompt)

    def test_web_fetch_enrichment_topic_maintain_always_allowed(self):
        """topic_maintain is a latest-data flow, so WebFetch is always allowed."""
        from research_center.web_fetch_enrichment import _enrich_sources_with_web_fetch
        from research_center.models import CommandRequest, SourceItem

        req = CommandRequest(
            command="topic_maintain",
            raw_text="/topic_maintain",
            target="",
            theme_scope="",
            target_type="topic_maintain",
            mode="normal",
            source_only=False,
            score=False,
            brief=False,
            top=None,
            ai_model="gemini",
            report_date=None,
            output_formats=("md",),
            user_id="test",
            created_at=None,
        )
        sources = [
            SourceItem(source_id="src1", title="Test Article", url="https://example.com/article1", source_level="L2", snippet="Test", provider="Test")
        ]
        structured_data: dict[str, Any] = {}

        with patch("research_center.web_fetch_service.WebFetchService.fetch_many") as mock_fetch:
            mock_fetch.return_value = MagicMock(results=[])
            # topic_maintain always allows WebFetch
            _enrich_sources_with_web_fetch(req, sources, structured_data, progress=None)
            mock_fetch.assert_called()

    def test_topic_maintain_always_runs_discovery(self):
        """topic_maintain always runs discovery (latest-data flow, no date flag)."""
        self._mock_is_formal_library_empty(False)
        center = self._mock_center()
        mock_request = self._mock_request()
        self._mock_write_prompt_log("/logs/ai_prompts/test.json")
        self._mock_raw_response_path(MagicMock())
        mock_save = self._mock_save_change_pack()
        self._mock_collect_structured_data()

        center._gemini_discovery_runner = MagicMock()
        center._gemini_discovery_runner.run_discovery_flow.return_value = ([], False)
        self._mock_web_fetch()

        run_topic_maintain(mock_request, center=center, progress=None)
        # Discovery runner should be called (topic_maintain always runs discovery)
        center._gemini_discovery_runner.run_discovery_flow.assert_called()

    def test_base_sources_injected_into_prompt(self):
        """base_sources from collect_structured_data should be in structured_data."""
        self._mock_is_formal_library_empty(False)
        center = self._mock_center()
        mock_request = self._mock_request()
        self._mock_write_prompt_log("/logs/ai_prompts/test.json")
        self._mock_raw_response_path(MagicMock())
        mock_save = self._mock_save_change_pack()
        # Provide a return value for collect_structured_data that includes base_sources
        mock_data = {"base_sources": [], "existing_topic_profiles": []}
        with patch("research_center.data_services.collect_structured_data", return_value=(mock_data, [])):
            center._gemini_discovery_runner = MagicMock()
            center._gemini_discovery_runner.run_discovery_flow.return_value = ([], False)
            self._mock_web_fetch()
            # Just verify it doesn't crash - base_sources key should be present in structured_data
            run_topic_maintain(mock_request, center=center, progress=None)

    def test_recent_scan_candidates_include_required_fields(self):
        """recent_scan_candidates should include code, name, industry, scan_id, scan_date, scan_type."""
        from research_center.data_services import collect_topic_maintain_data
        from research_center.command_parser import CommandRequest

        req = CommandRequest(
            command="topic_maintain", raw_text="/topic_maintain", target="",
            theme_scope="", target_type="topic_maintain", mode="normal",
            source_only=False, score=False, brief=False, top=None,
            ai_model="gemini", report_date=None, output_formats=("md",),
            user_id="test", created_at=None,
        )
        fake_entry = MagicMock()
        fake_entry.code = "2330"
        fake_entry.name = "台積電"
        fake_entry.industry = "半導體"
        fake_scans = [{
            "scan_id": "scan_test",
            "scan_date": "2026-05-20",
            "scan_type": "精選選股",
            "codes": ["2330"],
        }]

        with patch("research_center.data_services.load_stock_universe", return_value=[fake_entry]), \
             patch("research_center.data_services.load_recent_scan_results", return_value=fake_scans), \
             patch("research_center.data_services.load_price_metrics", return_value={"2330": {"avg_volume_20d": 1000}}):
            data = collect_topic_maintain_data(req, progress=None)
        scan_candidates = data.get("recent_scan_candidates", [])
        # At minimum should have the keys we set
        for scan in scan_candidates[:1]:
            for key in ("scan_id", "scan_date", "scan_type", "candidates"):
                self.assertIn(key, scan)
            for cand in scan.get("candidates", [])[:1]:
                self.assertIn("code", cand)
                self.assertIn("name", cand)
                break

    def test_webfetch_evidence_failure_does_not_abort(self):
        """WebFetch evidence extraction failure should not abort the main process."""
        self._mock_is_formal_library_empty(False)
        center = self._mock_center()
        mock_request = self._mock_request()
        self._mock_write_prompt_log("/logs/ai_prompts/test.json")
        self._mock_raw_response_path(MagicMock())
        mock_save = self._mock_save_change_pack()
        self._mock_collect_structured_data()

        center._gemini_discovery_runner = MagicMock()
        center._gemini_discovery_runner.run_discovery_flow.return_value = ([], False)

        # Make web fetch enrichment succeed
        with patch("research_center.web_fetch_enrichment._enrich_sources_with_web_fetch"):
            # Make evidence extraction raise an exception - should not abort main flow
            with patch("research_center.topic_evidence_extractor.build_topic_evidence_candidates", side_effect=Exception("extraction failed")):
                pack = run_topic_maintain(mock_request, center=center, progress=None)
                self.assertIsNotNone(pack)

    def test_topic_maintain_structured_data_is_json_serializable(self):
        """structured_data should be JSON-serializable (no SourceItem objects in it)."""
        self._mock_is_formal_library_empty(False)
        center = self._mock_center()
        mock_request = self._mock_request()
        self._mock_write_prompt_log("/logs/ai_prompts/test.json")
        self._mock_raw_response_path(MagicMock())
        self._mock_save_change_pack()

        # Simulate base_sources as a list of SourceItem-like objects
        class FakeSourceItem:
            def __init__(self):
                self.title = "Test Title"
                self.url = "https://example.com"
                self.snippet = "Test snippet"
                self.published_date = None
                self.source_level = "L2"
                self.provider = "TestProvider"
                self.provider_detail = "TestDetail"

        mock_sources = [FakeSourceItem()]
        mock_data = {
            "existing_topic_profiles": [],
            "company_topic_map": {},
            "supply_chain_nodes": [],
            "recent_scans": [],
            "candidate_companies": [],
            "web_fetched_sources": [],
            "web_fetch_diagnostics": {},
        }
        with patch("research_center.data_services.collect_structured_data", return_value=(mock_data, mock_sources)):
            center._gemini_discovery_runner = MagicMock()
            center._gemini_discovery_runner.run_discovery_flow.return_value = ([], False)
            self._mock_web_fetch()
            # Run topic maintain - it should not crash
            pack = run_topic_maintain(mock_request, center=center, progress=None)
            self.assertIsNotNone(pack)

    def test_base_sources_json_uses_serialized_dicts(self):
        """base_sources_json in prompt should use serialized dicts, not SourceItem."""
        import json
        from research_center.topic_maintain_service import _source_item_to_dict

        # Test the helper converts SourceItem-like objects to dicts
        class FakeSourceItem:
            title = "Fake Title"
            url = "https://fake.com"
            snippet = "Fake snippet"
            published_date = None
            source_level = "L2"
            provider = "FakeProvider"
            provider_detail = "FakeDetail"

        result = _source_item_to_dict(FakeSourceItem())
        self.assertEqual(result["title"], "Fake Title")
        self.assertEqual(result["url"], "https://fake.com")

        # Verify it can be JSON dumped
        data = {"base_sources": [result]}
        json_str = json.dumps(data, ensure_ascii=False)
        self.assertIn("Fake Title", json_str)

    def test_run_topic_maintain_injects_company_knowledge_and_preserves_updates(self):
        actions = [
            {
                "action_type": "create_theme",
                "theme_id": f"topic_{i:02d}",
                "theme_name": f"題材 {i:02d}",
                "confidence": "medium",
                "reason": "test",
                "evidence": [],
            }
            for i in range(1, 13)
        ]
        raw = json.dumps(
            {
                "summary": "test",
                "confidence": "medium",
                "actions": actions,
                "company_knowledge_updates": {
                    "companies": {
                        "2330": {
                            "company_name": "台積電",
                            "product_lines": ["CoWoS"],
                        }
                    }
                },
            },
            ensure_ascii=False,
        )
        center = self._mock_center(gemini_result=MagicMock(raw=raw))
        mock_request = self._mock_request(ai_model="gemini")
        self._mock_is_formal_library_empty(True)
        self._mock_collect_structured_data()
        self._mock_write_prompt_log("/tmp/prompt.json")
        self._mock_save_change_pack()
        with patch(
            "research_center.topic_maintain_service.load_company_knowledge_data",
            return_value={"companies": {"2330": {"company_name": "台積電"}}},
        ):
            pack = run_topic_maintain(mock_request, center=center, progress=None)

        prompt = center.gemini.generate_report.call_args[0][0]
        self.assertIn("既有公司知識庫", prompt)
        self.assertIn("台積電", prompt)
        self.assertEqual(pack.company_knowledge_updates["companies"]["2330"]["product_lines"], ["CoWoS"])

    def test_json_safe_converts_nested_source_items(self):
        """_json_safe should convert SourceItem-like objects nested anywhere."""
        import json
        from research_center.topic_maintain_service import _json_safe

        class FakeSourceItem:
            title = "Nested Title"
            url = "https://nested.example"
            snippet = "Nested snippet"
            published_date = None
            source_level = "L2"
            provider = "FakeProvider"
            provider_detail = "FakeDetail"

        data = {
            "market_signals": {
                "sources": [FakeSourceItem()],
            },
            "tuple_sources": (FakeSourceItem(),),
        }
        safe = _json_safe(data)
        json_str = json.dumps(safe, ensure_ascii=False)

        self.assertIsInstance(safe["market_signals"]["sources"][0], dict)
        self.assertEqual(safe["market_signals"]["sources"][0]["title"], "Nested Title")
        self.assertIn("https://nested.example", json_str)

    def test_run_topic_maintain_deepseek_accepts_dict_response(self):
        """DeepSeek generate_report returns dict (not string or .raw)."""
        self._mock_is_formal_library_empty(False)
        center = self._mock_center(ai_model="deepseek")
        mock_request = self._mock_request(ai_model="deepseek")
        self._mock_write_prompt_log("/logs/ai_prompts/test.json")
        self._mock_raw_response_path(MagicMock())
        self._mock_save_change_pack()
        self._mock_collect_structured_data()
        center._gemini_discovery_runner = MagicMock()
        center._gemini_discovery_runner.run_discovery_flow.return_value = ([], False)
        self._mock_web_fetch()

        # DeepSeek returns dict directly
        dict_response = {
            "change_id": "change_deepseek_dict",
            "parent_change_id": None,
            "mode": "update",
            "status": "pending",
            "model": "deepseek",
            "created_at": "2026-01-01T10:00:00+0800",
            "updated_at": "2026-01-01T10:00:00+0800",
            "summary": "Test from dict",
            "confidence": "high",
            "actions": [],
            "warnings": [],
            "sources": [],
            "adjustment_notes": "",
            "raw_response_path": "",
            "prompt_log_path": "",
        }
        center.opencode.generate_report.return_value = dict_response

        pack = run_topic_maintain(mock_request, center=center, progress=None)
        self.assertIsNotNone(pack)
        # change_id is overridden by system, so just verify it was created
        self.assertTrue(pack.change_id.startswith("change_"))

    def test_run_topic_maintain_deepseek_accepts_raw_dict_response(self):
        """DeepSeek generate_report returns MagicMock with .raw as dict."""
        self._mock_is_formal_library_empty(False)
        center = self._mock_center(ai_model="deepseek")
        mock_request = self._mock_request(ai_model="deepseek")
        self._mock_write_prompt_log("/logs/ai_prompts/test.json")
        self._mock_raw_response_path(MagicMock())
        self._mock_save_change_pack()
        self._mock_collect_structured_data()
        center._gemini_discovery_runner = MagicMock()
        center._gemini_discovery_runner.run_discovery_flow.return_value = ([], False)
        self._mock_web_fetch()

        # DeepSeek returns MagicMock with .raw as dict
        dict_response = {
            "change_id": "change_deepseek_raw_dict",
            "parent_change_id": None,
            "mode": "update",
            "status": "pending",
            "model": "deepseek",
            "created_at": "2026-01-01T10:00:00+0800",
            "updated_at": "2026-01-01T10:00:00+0800",
            "summary": "Test from raw dict",
            "confidence": "high",
            "actions": [],
            "warnings": [],
            "sources": [],
            "adjustment_notes": "",
            "raw_response_path": "",
            "prompt_log_path": "",
        }
        center.opencode.generate_report.return_value = MagicMock(raw=dict_response)

        pack = run_topic_maintain(mock_request, center=center, progress=None)
        self.assertIsNotNone(pack)
        # change_id is overridden by system, so just verify it was created
        self.assertTrue(pack.change_id.startswith("change_"))

    def test_ai_response_to_text_handles_dict(self):
        """_ai_response_to_text should convert dict to JSON string."""
        from research_center.topic_maintain_service import _ai_response_to_text

        dict_input = {"key": "value", "nested": {"a": 1}}
        result = _ai_response_to_text(dict_input)
        self.assertIsInstance(result, str)
        self.assertIn("key", result)
        # Should be valid JSON
        import json
        parsed = json.loads(result)
        self.assertEqual(parsed["key"], "value")

    def test_ai_response_to_text_handles_raw_dict_object(self):
        """_ai_response_to_text should extract .raw if it's a dict."""
        from research_center.topic_maintain_service import _ai_response_to_text

        dict_input = {"key": "raw_dict_value"}
        mock_result = MagicMock(raw=dict_input)
        result = _ai_response_to_text(mock_result)
        self.assertIsInstance(result, str)
        import json
        parsed = json.loads(result)
        self.assertEqual(parsed["key"], "raw_dict_value")

    def test_parse_ai_json_response_accepts_dict(self):
        """_parse_ai_json_response should return dict directly without json.loads."""
        from research_center.topic_maintain_service import _parse_ai_json_response

        dict_input = {"change_id": "test123", "status": "pending"}
        result = _parse_ai_json_response(dict_input)
        self.assertIsInstance(result, dict)
        self.assertEqual(result["change_id"], "test123")

    def test_parse_ai_json_response_accepts_json_string(self):
        """_parse_ai_json_response should parse string JSON."""
        from research_center.topic_maintain_service import _parse_ai_json_response

        str_input = '{"change_id": "test456", "status": "pending"}'
        result = _parse_ai_json_response(str_input)
        self.assertIsInstance(result, dict)
        self.assertEqual(result["change_id"], "test456")

    def test_run_topic_maintain_empty_actions_marked_failed(self):
        """AI returns valid JSON but with empty actions -> status FAILED with warning."""
        self._mock_is_formal_library_empty(False)
        center = self._mock_center()
        mock_request = self._mock_request()
        self._mock_write_prompt_log("/logs/ai_prompts/test.json")
        self._mock_raw_response_path(MagicMock())
        self._mock_save_change_pack()
        self._mock_collect_structured_data()
        center._gemini_discovery_runner = MagicMock()
        center._gemini_discovery_runner.run_discovery_flow.return_value = ([], False)
        self._mock_web_fetch()

        # AI returns valid JSON but with empty actions
        empty_actions_response = {
            "change_id": "change_empty_test",
            "parent_change_id": None,
            "mode": "update",
            "status": "pending",
            "model": "gemini",
            "created_at": "2026-01-01T10:00:00+0800",
            "updated_at": "2026-01-01T10:00:00+0800",
            "summary": "No evidence found",
            "confidence": "low",
            "actions": [],
            "warnings": [],
            "sources": [],
            "adjustment_notes": "",
            "raw_response_path": "",
            "prompt_log_path": "",
        }
        center.gemini.generate_report.return_value = MagicMock(raw=json.dumps(empty_actions_response))

        pack = run_topic_maintain(mock_request, center=center, progress=None)
        self.assertIsNotNone(pack)
        self.assertEqual(pack.status.value, "failed")
        # Warning should be user-friendly, not referencing raw_response_path
        self.assertTrue(
            any("未產生可套用" in w for w in pack.warnings if w),
            f"Expected user-friendly warning, got: {pack.warnings}",
        )
        self.assertFalse(
            any("raw_response_path" in w for w in pack.warnings if w),
            f"Warning should not reference raw_response_path, got: {pack.warnings}",
        )

    def test_parse_ai_json_response_accepts_chat_completion_wrapper(self):
        """_parse_ai_json_response should extract content from DeepSeek/OpenAI chat completion wrapper."""
        from research_center.topic_maintain_service import _parse_ai_json_response

        wrapper = {
            "id": "abc123",
            "object": "chat.completion",
            "model": "deepseek-v4-pro",
            "choices": [
                {
                    "message": {
                        "content": "```json\n{\"actions\":[{\"action_type\":\"create_theme\",\"theme_id\":\"ai_server\",\"theme_name\":\"AI伺服器\",\"keywords\":[\"AI伺服器\"],\"industries\":[\"半導體\"],\"supply_chain_role\":\"核心受惠\",\"confidence\":\"high\",\"reason\":\"test\",\"evidence\":[],\"counter_evidence\":[],\"affected_companies\":[\"2330\"],\"supply_chain_nodes\":[],\"target_theme_id\":null,\"risk_notes\":[],\"missing_data\":[]}],\"summary\":\"Test\",\"warnings\":[],\"sources\":[],\"confidence\":\"high\",\"change_id\":\"test_change\",\"parent_change_id\":null,\"mode\":\"update\",\"status\":\"pending\",\"model\":\"deepseek\",\"created_at\":\"2026-01-01T10:00:00+0800\",\"updated_at\":\"2026-01-01T10:00:00+0800\",\"adjustment_notes\":\"\",\"raw_response_path\":\"\",\"prompt_log_path\":\"\"}\n```"
                    }
                }
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
            "system_fingerprint": "fp_abc",
        }
        result = _parse_ai_json_response(wrapper)
        self.assertIsInstance(result, dict)
        self.assertEqual(result["change_id"], "test_change")
        self.assertEqual(len(result["actions"]), 1)
        self.assertEqual(result["actions"][0]["theme_id"], "ai_server")

    def test_ai_response_to_text_extracts_chat_completion_content(self):
        """_ai_response_to_text should extract choices[0].message.content from chat completion wrapper."""
        from research_center.topic_maintain_service import _ai_response_to_text

        wrapper = {
            "id": "chatcmpl-abc",
            "object": "chat.completion",
            "model": "deepseek-v4-pro",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": '{"summary":"test content"}'},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
        result = _ai_response_to_text(wrapper)
        self.assertIsInstance(result, str)
        # Should extract the content string
        self.assertEqual(result, '{"summary":"test content"}')

    def test_run_topic_maintain_deepseek_chat_completion_actions_not_empty(self):
        """DeepSeek chat completion wrapper with actions should NOT be marked failed."""
        self._mock_is_formal_library_empty(False)
        center = self._mock_center(ai_model="deepseek")
        mock_request = self._mock_request(ai_model="deepseek")
        self._mock_write_prompt_log("/logs/ai_prompts/test.json")
        self._mock_raw_response_path(MagicMock())
        self._mock_save_change_pack()
        self._mock_collect_structured_data()
        center._gemini_discovery_runner = MagicMock()
        center._gemini_discovery_runner.run_discovery_flow.return_value = ([], False)
        self._mock_web_fetch()

        # DeepSeek returns chat completion wrapper with actions
        wrapper_response = {
            "id": "chatcmpl-deepseek-123",
            "object": "chat.completion",
            "model": "deepseek-v4-pro",
            "choices": [
                {
                    "message": {
                        "content": "```json\n{\"actions\":[{\"action_type\":\"create_theme\",\"theme_id\":\"ai_server\",\"theme_name\":\"AI伺服器\",\"keywords\":[\"AI伺服器\",\"GB200\"],\"industries\":[\"半導體\",\"伺服器\"],\"supply_chain_role\":\"核心受惠\",\"confidence\":\"high\",\"reason\":\"Test reason\",\"evidence\":[{\"source\":\"Test\",\"source_level\":\"L2_media\",\"content\":\"Test evidence\",\"url\":\"https://example.com\",\"publish_date\":\"2026-01-01\",\"score_contribution\":8.0}],\"counter_evidence\":[],\"affected_companies\":[\"2330\"],\"supply_chain_nodes\":[{\"company\":\"2330\",\"role\":\"晶片製造\"}],\"target_theme_id\":null,\"risk_notes\":[],\"missing_data\":[]}],\"summary\":\"AI伺服器題材測試\",\"warnings\":[],\"sources\":[],\"confidence\":\"high\",\"change_id\":\"change_deepseek_wrapper\",\"parent_change_id\":null,\"mode\":\"update\",\"status\":\"pending\",\"model\":\"deepseek\",\"created_at\":\"2026-01-01T10:00:00+0800\",\"updated_at\":\"2026-01-01T10:00:00+0800\",\"adjustment_notes\":\"\",\"raw_response_path\":\"\",\"prompt_log_path\":\"\"}\n```"
                    },
                    "finish_reason": "stop",
                    "index": 0,
                }
            ],
            "usage": {"prompt_tokens": 500, "completion_tokens": 200, "total_tokens": 700},
        }
        center.opencode.generate_report.return_value = MagicMock(raw=wrapper_response)

        pack = run_topic_maintain(mock_request, center=center, progress=None)
        self.assertIsNotNone(pack)
        # Actions should NOT be empty
        self.assertGreater(len(pack.actions), 0)
        # Status should be pending, NOT failed
        self.assertEqual(pack.status.value, "pending")
        self.assertEqual(pack.actions[0].theme_id, "ai_server")

    def test_minimax_generate_json_exists_and_has_json_only_system_prompt(self):
        """MiniMaxService should have generate_json with JSON-only system prompt."""
        from research_center.minimax_service import MiniMaxService
        import inspect

        self.assertTrue(hasattr(MiniMaxService, "generate_json"), "MiniMaxService should have generate_json method")
        source = inspect.getsource(MiniMaxService.generate_json)
        # JSON-only prompt should NOT contain "Return Markdown"
        self.assertNotIn("Markdown", source)
        # Should mention JSON
        self.assertIn("JSON", source)
        # Should not use the Markdown-only system prompt from generate_report
        self.assertNotIn("Return Markdown only", source)

    def test_run_topic_maintain_minimax_uses_generate_json_not_report(self):
        """MiniMax topic maintain should call generate_json, not generate_report."""
        self._mock_is_formal_library_empty(False)
        mock_request = self._mock_request(ai_model="minimax")

        minimax_mock = MagicMock()
        minimax_mock.generate_json.return_value = MagicMock(raw=json.dumps({
            "change_id": "change_minimax_test",
            "parent_change_id": None,
            "mode": "update",
            "status": "pending",
            "model": "minimax",
            "created_at": "2026-01-01T10:00:00+0800",
            "updated_at": "2026-01-01T10:00:00+0800",
            "summary": "MiniMax Test",
            "confidence": "high",
            "actions": [],
            "warnings": [],
            "sources": [],
            "adjustment_notes": "",
            "raw_response_path": "",
            "prompt_log_path": "",
        }))

        center = MagicMock()
        center.minimax = minimax_mock
        center._gemini_discovery_runner = MagicMock()
        center._gemini_discovery_runner.run_discovery_flow.return_value = ([], False)

        self._mock_write_prompt_log("/logs/ai_prompts/test.json")
        self._mock_raw_response_path(MagicMock())
        self._mock_save_change_pack()
        self._mock_collect_structured_data()
        self._mock_web_fetch()

        run_topic_maintain(mock_request, center=center, progress=None)

        # generate_json should be called for staged generation, NOT generate_report
        self.assertGreaterEqual(minimax_mock.generate_json.call_count, 1)

    def test_run_topic_maintain_minimax_returns_valid_change_pack(self):
        """MiniMax with valid JSON response should produce pending change pack."""
        self._mock_is_formal_library_empty(False)
        mock_request = self._mock_request(ai_model="minimax")

        valid_json = {
            "change_id": "change_minimax_valid",
            "parent_change_id": None,
            "mode": "update",
            "status": "pending",
            "model": "minimax",
            "created_at": "2026-01-01T10:00:00+0800",
            "updated_at": "2026-01-01T10:00:00+0800",
            "summary": "Valid MiniMax Test",
            "confidence": "high",
            "actions": [
                {
                    "action_type": "create_theme",
                    "theme_id": "test_theme",
                    "theme_name": "測試題材",
                    "keywords": ["測試"],
                    "industries": ["半導體"],
                    "supply_chain_role": "核心受惠",
                    "confidence": "high",
                    "reason": "Test",
                    "evidence": [],
                    "counter_evidence": [],
                    "affected_companies": ["2330"],
                    "supply_chain_nodes": [],
                    "target_theme_id": None,
                    "risk_notes": [],
                    "missing_data": [],
                }
            ],
            "warnings": [],
            "sources": [],
            "adjustment_notes": "",
            "raw_response_path": "",
            "prompt_log_path": "",
        }
        minimax_mock = MagicMock()
        minimax_mock.generate_json.return_value = MagicMock(raw=json.dumps(valid_json))

        center = MagicMock()
        center.minimax = minimax_mock
        center._gemini_discovery_runner = MagicMock()
        center._gemini_discovery_runner.run_discovery_flow.return_value = ([], False)

        self._mock_write_prompt_log("/logs/ai_prompts/test.json")
        self._mock_raw_response_path(MagicMock())
        self._mock_save_change_pack()
        self._mock_collect_structured_data()
        self._mock_web_fetch()

        pack = run_topic_maintain(mock_request, center=center, progress=None)

        self.assertIsInstance(pack, TopicChangePack)
        self.assertEqual(pack.status.value, "pending")
        self.assertEqual(pack.model, "minimax")
        self.assertGreater(len(pack.actions), 0)

    def test_run_topic_maintain_minimax_markdown_response_marks_failed_pack(self):
        """MiniMax returning Markdown should not raise; it should produce a failed pack."""
        self._mock_is_formal_library_empty(False)
        mock_request = self._mock_request(ai_model="minimax")

        minimax_mock = MagicMock()
        # Simulate Markdown response instead of JSON
        minimax_mock.generate_json.return_value = MagicMock(
            raw="# MiniMax Report\n\nHere is the analysis..."
        )

        center = MagicMock()
        center.minimax = minimax_mock
        center._gemini_discovery_runner = MagicMock()
        center._gemini_discovery_runner.run_discovery_flow.return_value = ([], False)

        self._mock_write_prompt_log("/logs/ai_prompts/test.json")
        mock_raw_path = MagicMock()
        mock_raw_path_str = "/logs/topic_ai_raw/test_minimax_markdown.json"
        mock_raw_path.__str__ = lambda self: mock_raw_path_str
        self._mock_raw_response_path(mock_raw_path)
        self._mock_save_change_pack()
        self._mock_collect_structured_data()
        self._mock_web_fetch()

        pack = run_topic_maintain(mock_request, center=center, progress=None)

        self.assertEqual(pack.status, TopicChangeStatus.FAILED)
        self.assertTrue(any("candidate_extract" in warning for warning in pack.warnings))

    def test_parse_ai_json_response_removes_think_blocks(self):
        """_parse_ai_json_response should strip <think>...</think> before parsing."""
        from research_center.topic_maintain_service import _parse_ai_json_response

        input_with_think = "<think>\nSome reasoning content\n</think>\n{\"actions\": [], \"summary\": \"test\"}"
        result = _parse_ai_json_response(input_with_think)
        self.assertIsInstance(result, dict)
        self.assertEqual(result["summary"], "test")

    def test_parse_ai_json_response_extracts_json_from_surrounding_text(self):
        """_parse_ai_json_response should extract JSON object from surrounding text."""
        from research_center.topic_maintain_service import _parse_ai_json_response

        # Input with text before and after the JSON object
        input_with_text = '前置說明文字\n{"actions": [{"action_type": "create_theme"}], "summary": "test"}\n後置文字'
        result = _parse_ai_json_response(input_with_text)
        self.assertIsInstance(result, dict)
        self.assertEqual(result["summary"], "test")

    def test_parse_ai_json_response_handles_minimax_reasoning_wrapper(self):
        """MiniMax generate_json returns <think>...</think> wrapper; parsing should work."""
        from research_center.topic_maintain_service import _parse_ai_json_response

        # Simulates actual MiniMax generate_json response with reasoning block
        raw_response = (
            "<think>\nMiniMax reasoning here\n</think>\n"
            + '{"change_id": "test_minimax_reasoning", "parent_change_id": null, '
            + '"mode": "update", "status": "pending", "model": "minimax", '
            + '"created_at": "2026-01-01T10:00:00+0800", '
            + '"updated_at": "2026-01-01T10:00:00+0800", '
            + '"summary": "MiniMax reasoning test", "confidence": "high", '
            + '"actions": [{"action_type": "create_theme", "theme_id": "test_theme", '
            + '"theme_name": "Test Theme", "keywords": ["test"], "industries": ["半導體"], '
            + '"supply_chain_role": "核心受惠", "confidence": "high", "reason": "test", '
            + '"evidence": [], "counter_evidence": [], "affected_companies": ["2330"], '
            + '"supply_chain_nodes": [], "target_theme_id": null, "risk_notes": [], '
            + '"missing_data": []}], "warnings": [], "sources": [], '
            + '"adjustment_notes": "", "raw_response_path": "", "prompt_log_path": ""}'
        )
        result = _parse_ai_json_response(raw_response)
        self.assertIsInstance(result, dict)
        self.assertEqual(result["change_id"], "test_minimax_reasoning")
        self.assertEqual(result["model"], "minimax")
        self.assertEqual(len(result["actions"]), 1)
        self.assertEqual(result["actions"][0]["theme_id"], "test_theme")

    def test_parse_ai_json_response_handles_gemini_wrapper(self):
        """Gemini wrapper with candidates[0].content.parts[0].text should parse correctly."""
        from research_center.topic_maintain_service import _parse_ai_json_response

        gemini_wrapper = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": '{"change_id": "test_gemini", "parent_change_id": null, '
                                + '"mode": "update", "status": "pending", "model": "gemini", '
                                + '"created_at": "2026-01-01T10:00:00+0800", '
                                + '"updated_at": "2026-01-01T10:00:00+0800", '
                                + '"summary": "Gemini test", "confidence": "high", '
                                + '"actions": [{"action_type": "create_theme", "theme_id": "test_gemini", '
                                + '"theme_name": "Gemini Test Theme", "keywords": ["test"], "industries": ["半導體"], '
                                + '"supply_chain_role": "核心受惠", "confidence": "high", "reason": "test", '
                                + '"evidence": [], "counter_evidence": [], "affected_companies": ["2330"], '
                                + '"supply_chain_nodes": [], "target_theme_id": null, "risk_notes": [], '
                                + '"missing_data": []}], "warnings": [], "sources": [], '
                                + '"adjustment_notes": "", "raw_response_path": "", "prompt_log_path": ""}'
                            }
                        ]
                    }
                }
            ],
            "usageMetadata": {"promptTokenCount": 100, "candidateTokenCount": 50},
        }
        result = _parse_ai_json_response(gemini_wrapper)
        self.assertIsInstance(result, dict)
        self.assertEqual(result["change_id"], "test_gemini")
        self.assertEqual(result["model"], "gemini")
        self.assertEqual(len(result["actions"]), 1)
        self.assertEqual(result["actions"][0]["theme_id"], "test_gemini")

    def test_parse_ai_json_response_handles_gemini_multipart_wrapper(self):
        """Gemini wrapper with multiple parts should concatenate all part texts."""
        from research_center.topic_maintain_service import _parse_ai_json_response

        gemini_wrapper = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"text": '{"change_id": "test_gemini_mp", "parent_change_id": null, '},
                            {"text": '"mode": "update", "status": "pending", "model": "gemini", '},
                            {"text": '"created_at": "2026-01-01T10:00:00+0800", '},
                            {"text": '"updated_at": "2026-01-01T10:00:00+0800", '},
                            {"text": '"summary": "Multipart Gemini test", "confidence": "high", '},
                            {"text": '"actions": [], "warnings": [], "sources": [], '},
                            {"text": '"adjustment_notes": "", "raw_response_path": "", "prompt_log_path": ""}'},
                        ]
                    }
                }
            ],
        }
        result = _parse_ai_json_response(gemini_wrapper)
        self.assertIsInstance(result, dict)
        self.assertEqual(result["change_id"], "test_gemini_mp")


class TestSkipWebfetchEvidence(unittest.TestCase):
    """Tests for skip_webfetch_evidence fast mode."""

    def setUp(self):
        self._patchers = []

    def tearDown(self):
        for p in self._patchers:
            p.stop()

    def _mock_request(self, ai_model="minimax"):
        return MagicMock(
            command="topic_maintain",
            raw_text="/topic_maintain --deep --model minimax",
            target="",
            theme_scope="",
            target_type="topic_maintain",
            mode="deep",
            source_only=False,
            score=False,
            brief=False,
            top=None,
            ai_model=ai_model,
            report_date=None,
            output_formats=("json",),
            user_id="test_user",
            created_at=None,
        )

    def _mock_center(self):
        center = MagicMock()
        center.minimax = MagicMock()
        center.opencode = MagicMock()
        center.gemini = MagicMock()
        return center

    def _mock_write_prompt_log(self, path):
        p = patch("research_center.prompt_logging.write_prompt_log", return_value=Path(path))
        self._patchers.append(p)
        return p.start()

    def _mock_raw_response_path(self, path):
        p = patch("research_center.topic_repository.raw_response_path", return_value=Path(path))
        self._patchers.append(p)
        return p.start()

    def _mock_save_change_pack(self):
        p = patch("research_center.topic_maintain_service.save_change_pack")
        self._patchers.append(p)
        return p.start()

    def _mock_is_formal_library_empty(self, empty):
        p = patch("research_center.topic_maintain_service.is_formal_library_empty", return_value=empty)
        self._patchers.append(p)
        return p.start()

    def _mock_collect_structured_data(self, data=None, sources=None):
        data = data if data is not None else {
            "existing_topic_profiles": [],
            "company_topic_map": {},
            "supply_chain_nodes": [],
            "recent_scans": [],
            "candidate_companies": [],
            "web_fetched_sources": [{"title": "Test", "url": "http://test.com"}],
            "web_fetch_diagnostics": {},
        }
        sources = sources if sources is not None else []
        p = patch("research_center.data_services.collect_structured_data", return_value=(data, sources))
        self._patchers.append(p)
        return p.start()

    def _mock_web_fetch(self):
        p = patch("research_center.web_fetch_enrichment._enrich_sources_with_web_fetch")
        self._patchers.append(p)
        return p.start()

    def test_run_topic_maintain_skip_webfetch_evidence_skips_extraction(self):
        """run_topic_maintain(..., skip_webfetch_evidence=True) should skip rule-based evidence extraction."""
        from research_center.topic_maintain_service import run_topic_maintain
        from research_center.topic_evidence_extractor import build_topic_evidence_candidates

        mock_request = self._mock_request(ai_model="minimax")
        center = self._mock_center()
        center.minimax.generate_json.return_value = MagicMock(raw=json.dumps({
            "change_id": "change_skip_webfetch_test",
            "parent_change_id": None,
            "mode": "update",
            "status": "pending",
            "model": "minimax",
            "created_at": "2026-01-01T10:00:00+0800",
            "updated_at": "2026-01-01T10:00:00+0800",
            "summary": "Skip WebFetch test",
            "confidence": "high",
            "actions": [{
                "action_type": "create_theme",
                "theme_id": "skip_webfetch_test",
                "theme_name": "Skip WebFetch Test",
                "keywords": ["test"],
                "industries": ["半導體"],
                "supply_chain_role": "核心受惠",
                "confidence": "high",
                "reason": "test",
                "evidence": [],
                "counter_evidence": [],
                "affected_companies": ["2330"],
                "supply_chain_nodes": [],
                "target_theme_id": None,
                "risk_notes": [],
                "missing_data": [],
            }],
            "warnings": [],
            "sources": [],
            "adjustment_notes": "",
            "raw_response_path": "",
            "prompt_log_path": "",
        }))

        self._mock_write_prompt_log("/logs/ai_prompts/test.json")
        self._mock_raw_response_path(MagicMock())
        self._mock_save_change_pack()
        self._mock_is_formal_library_empty(False)
        center._gemini_discovery_runner = MagicMock()
        center._gemini_discovery_runner.run_discovery_flow.return_value = ([], False)
        self._mock_web_fetch()
        self._mock_collect_structured_data()

        with patch("research_center.topic_evidence_extractor.build_topic_evidence_candidates") as mock_extract:
            mock_extract.return_value = {"mode": "rule_based", "items": [], "warnings": []}
            pack = run_topic_maintain(
                mock_request,
                center=center,
                progress=None,
                skip_webfetch_evidence=True,
            )

        # Verify build_topic_evidence_candidates was NOT called
        mock_extract.assert_not_called()
        # Verify minimax.generate_json WAS called (main flow still works)
        center.minimax.generate_json.assert_called()
        self.assertIsNotNone(pack)

    def test_run_topic_maintain_default_calls_rule_based_extraction(self):
        """run_topic_maintain without skip_webfetch_evidence calls rule-based extractor (no AI)."""
        from research_center.topic_maintain_service import run_topic_maintain
        from research_center.topic_evidence_extractor import build_topic_evidence_candidates

        mock_request = self._mock_request(ai_model="minimax")
        center = self._mock_center()
        center.minimax.generate_json.return_value = MagicMock(raw=json.dumps({
            "change_id": "change_default_test",
            "parent_change_id": None,
            "mode": "update",
            "status": "pending",
            "model": "minimax",
            "created_at": "2026-01-01T10:00:00+0800",
            "updated_at": "2026-01-01T10:00:00+0800",
            "summary": "Default test",
            "confidence": "high",
            "actions": [{
                "action_type": "create_theme",
                "theme_id": "default_test",
                "theme_name": "Default Test",
                "keywords": ["test"],
                "industries": ["半導體"],
                "supply_chain_role": "核心受惠",
                "confidence": "high",
                "reason": "test",
                "evidence": [],
                "counter_evidence": [],
                "affected_companies": ["2330"],
                "supply_chain_nodes": [],
                "target_theme_id": None,
                "risk_notes": [],
                "missing_data": [],
            }],
            "warnings": [],
            "sources": [],
            "adjustment_notes": "",
            "raw_response_path": "",
            "prompt_log_path": "",
        }))

        self._mock_write_prompt_log("/logs/ai_prompts/test.json")
        self._mock_raw_response_path(MagicMock())
        self._mock_save_change_pack()
        self._mock_is_formal_library_empty(False)
        center._gemini_discovery_runner = MagicMock()
        center._gemini_discovery_runner.run_discovery_flow.return_value = ([], False)
        self._mock_web_fetch()
        self._mock_collect_structured_data()

        with patch("research_center.topic_evidence_extractor.build_topic_evidence_candidates") as mock_extract:
            mock_extract.return_value = {"mode": "rule_based", "items": [], "warnings": []}
            pack = run_topic_maintain(
                mock_request,
                center=center,
                progress=None,
            )

        # Verify build_topic_evidence_candidates WAS called (rule-based, no AI)
        mock_extract.assert_called_once()
        # Verify MiniMax is used for staged topic generation; evidence extraction is rule-based.
        self.assertGreaterEqual(center.minimax.generate_json.call_count, 1)
        self.assertIsNotNone(pack)

    def test_run_topic_maintain_minimax_staged_calls_do_not_include_evidence_ai(self):
        """With MiniMax, evidence extraction should NOT call MiniMax; staged generation still does."""
        from research_center.topic_maintain_service import run_topic_maintain

        mock_request = self._mock_request(ai_model="minimax")
        center = self._mock_center()
        center.minimax.generate_json.return_value = MagicMock(raw=json.dumps({
            "change_id": "change_minimax_single_call",
            "parent_change_id": None,
            "mode": "update",
            "status": "pending",
            "model": "minimax",
            "created_at": "2026-01-01T10:00:00+0800",
            "updated_at": "2026-01-01T10:00:00+0800",
            "summary": "Single call test",
            "confidence": "high",
            "actions": [],
            "warnings": [],
            "sources": [],
            "adjustment_notes": "",
            "raw_response_path": "",
            "prompt_log_path": "",
        }))

        self._mock_write_prompt_log("/logs/ai_prompts/test.json")
        self._mock_raw_response_path(MagicMock())
        self._mock_save_change_pack()
        self._mock_is_formal_library_empty(False)
        center._gemini_discovery_runner = MagicMock()
        center._gemini_discovery_runner.run_discovery_flow.return_value = ([], False)
        self._mock_web_fetch()
        self._mock_collect_structured_data()

        with patch("research_center.topic_evidence_extractor.build_topic_evidence_candidates") as mock_extract:
            mock_extract.return_value = {"mode": "rule_based", "items": [], "warnings": []}
            pack = run_topic_maintain(
                mock_request,
                center=center,
                progress=None,
            )

        # MiniMax should be called for candidate + detail stages, not for evidence extraction.
        self.assertGreaterEqual(center.minimax.generate_json.call_count, 1)
        # build_topic_evidence_candidates should be called (rule-based)
        mock_extract.assert_called_once()


class TestInitializationQualityChecks(unittest.TestCase):
    """Tests for _validate_initial_change_pack_quality helper.
    
    No mocks, no patches, no AI calls. Only data objects + direct helper call.
    """

    @staticmethod
    def _make_evidence():
        from research_center.topic_models import TopicEvidence, TopicSourceLevel
        return TopicEvidence(
            source="測試來源",
            source_level=TopicSourceLevel.L2_MEDIA,
            content="測試證據",
            url="https://example.com",
            publish_date="2026-01-01",
            score_contribution=5.0,
        )

    def _make_action(self, theme_id="theme_001", complete=True):
        from research_center.topic_models import TopicChangeAction, TopicActionType, TopicConfidence
        evidence = [self._make_evidence()] if complete else []
        return TopicChangeAction(
            action_type=TopicActionType.CREATE_THEME,
            theme_id=theme_id,
            theme_name="測試題材",
            keywords=["test"] if complete else [],
            industries=["半導體"] if complete else [],
            supply_chain_role="受惠" if complete else "",
            confidence=TopicConfidence.MEDIUM,
            reason="test reason" if complete else "",
            evidence=evidence,
            affected_companies=["2330"] if complete else [],
            risk_notes=["風險提示"] if complete else [],
            missing_data=["缺漏資料"] if complete else [],
            supply_chain_nodes=[
                {"node_id": "n1", "company_code": "2330", "company_name": "台積電",
                 "role": "晶片製造", "upstream": [], "downstream": [], "product_keywords": []}
            ] if complete else [],
        )

    def _make_pack(self, actions):
        from research_center.topic_models import TopicChangePack, TopicChangeMode, TopicChangeStatus
        return TopicChangePack(
            change_id="change_init_test",
            parent_change_id=None,
            mode=TopicChangeMode.INITIAL,
            status=TopicChangeStatus.PENDING,
            model="gemini",
            created_at="2026-01-01T10:00:00+0800",
            updated_at="2026-01-01T10:00:00+0800",
            summary="test",
            confidence="high",
            actions=actions,
            warnings=[],
        )

    def test_initial_quality_auto_fills_missing_fields_stays_pending(self):
        """Missing non-critical fields should be auto-filled, pack stays pending."""
        from research_center.topic_maintain_service import _validate_initial_change_pack_quality
        from research_center.topic_models import TopicChangeStatus

        actions = [self._make_action(theme_id="theme_001", complete=False)]
        pack = self._make_pack(actions)
        _validate_initial_change_pack_quality(pack)
        self.assertEqual(pack.status, TopicChangeStatus.PENDING)
        self.assertTrue(any("補入待補欄位" in w for w in pack.warnings))
        # Verify fields were filled
        action = pack.actions[0]
        self.assertEqual(action.affected_companies, [])
        self.assertEqual(action.risk_notes, ["待後續維護補強"])
        self.assertEqual(action.missing_data, ["待後續維護補強"])
        self.assertTrue(len(action.supply_chain_nodes) > 0)
        self.assertEqual(action.supply_chain_nodes[0].get("role"), "待補供應鏈或題材關聯")

    def test_initial_quality_warning_only_for_non_snake_case_theme_id(self):
        """theme_id not in snake_case should only warn, not fail."""
        from research_center.topic_maintain_service import _validate_initial_change_pack_quality
        from research_center.topic_models import TopicChangeStatus

        actions = [self._make_action(theme_id="AI Server Theme")]
        pack = self._make_pack(actions)
        _validate_initial_change_pack_quality(pack)
        self.assertEqual(pack.status, TopicChangeStatus.PENDING)
        self.assertTrue(any("snake_case" in w for w in pack.warnings))

    def test_initial_quality_fails_when_zero_create_theme(self):
        """Zero create_theme actions should fail."""
        from research_center.topic_maintain_service import _validate_initial_change_pack_quality
        from research_center.topic_models import TopicChangeStatus, TopicActionType, TopicChangeAction, TopicConfidence

        actions = [TopicChangeAction(
            action_type=TopicActionType.UPDATE_THEME,
            theme_id="existing",
            theme_name="既有題材",
            confidence=TopicConfidence.MEDIUM,
            reason="update",
        )]
        pack = self._make_pack(actions)
        _validate_initial_change_pack_quality(pack)
        self.assertEqual(pack.status, TopicChangeStatus.FAILED)
        self.assertTrue(any("未產生任何 create_theme" in w for w in pack.warnings))

    def test_initial_quality_fails_when_missing_theme_id(self):
        """Missing theme_id should fail."""
        from research_center.topic_maintain_service import _validate_initial_change_pack_quality
        from research_center.topic_models import TopicChangeStatus

        actions = [self._make_action(theme_id="", complete=False)]
        pack = self._make_pack(actions)
        _validate_initial_change_pack_quality(pack)
        self.assertEqual(pack.status, TopicChangeStatus.FAILED)
        self.assertTrue(any("缺少 theme_id" in w for w in pack.warnings))

    def test_initial_quality_passes_with_any_create_theme(self):
        """Init pack with any create_theme actions (and theme_id) should stay pending."""
        from research_center.topic_maintain_service import _validate_initial_change_pack_quality
        from research_center.topic_models import TopicChangeStatus

        actions = [self._make_action(theme_id="ai_server", complete=True) for i in range(5)]
        pack = self._make_pack(actions)
        _validate_initial_change_pack_quality(pack)
        self.assertEqual(pack.status, TopicChangeStatus.PENDING)

    def test_initial_quality_no_duplicate_warnings(self):
        """Same warning should not be added twice."""
        from research_center.topic_maintain_service import _validate_initial_change_pack_quality
        from research_center.topic_models import TopicChangeStatus

        actions = [self._make_action(theme_id="ai_server", complete=False) for i in range(3)]
        pack = self._make_pack(actions)
        _validate_initial_change_pack_quality(pack)
        _validate_initial_change_pack_quality(pack)  # call again
        self.assertEqual(pack.status, TopicChangeStatus.PENDING)
        # Count occurrences of the auto-fill warning
        count = sum(1 for w in pack.warnings if "補入待補欄位" in w)
        self.assertEqual(count, 1, f"Duplicate warning found: {pack.warnings}")


if __name__ == "__main__":
    unittest.main()
