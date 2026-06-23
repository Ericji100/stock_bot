import json
import unittest

from research_center.topic_models import TopicChangeMode, TopicChangeStatus
from research_center.topic_pipeline_service import run_topic_pipeline


class TopicPipelineServiceTests(unittest.TestCase):
    def _load_prompt(self, name):
        if name == "topic_candidate_extract":
            return "{mode} {webfetch_evidence_json}"
        if name == "topic_detail_expand":
            return "{topic_candidates_json}"
        return ""

    def _render_prompt(self, template, variables):
        result = template
        for key, value in variables.items():
            result = result.replace("{" + key + "}", value)
        return result

    def _variables(self):
        return {
            "mode": "initial",
            "report_date": "2026-05-31",
            "model": "minimax",
            "webfetch_evidence_json": "{}",
            "discovery_sources_json": "[]",
            "web_fetched_sources_json": "[]",
            "recent_scan_candidates_json": "[]",
            "market_signals_json": "{}",
            "external_topic_source_caches_json": "{}",
            "existing_topic_profiles_json": "[]",
            "company_topic_map_json": "{}",
            "supply_chain_nodes_json": "[]",
            "company_knowledge_json": "{}",
        }

    def test_pipeline_builds_pack_from_staged_ai_outputs(self):
        calls = []

        def call_ai_json(prompt, stage):
            calls.append(stage)
            if stage == "candidate_extract":
                return {"candidates": [
                    {"theme_id": "ai_server_cooling", "theme_name": "AI伺服器散熱", "keywords": ["散熱"]},
                    {"theme_id": "high_speed_networking", "theme_name": "高速網通", "keywords": ["交換器"]},
                ]}
            return {"actions": [
                {"theme_id": "ai_server_cooling", "theme_name": "AI伺服器散熱"},
                {"theme_id": "high_speed_networking", "theme_name": "高速網通"},
            ]}

        pack, logs = run_topic_pipeline(
            mode=TopicChangeMode.INITIAL,
            ai_model="minimax",
            change_id="change_test",
            iso_ts="2026-05-31T10:00:00+0800",
            structured_data={"existing_topic_profiles": []},
            prompt_variables=self._variables(),
            load_prompt=self._load_prompt,
            render_prompt=self._render_prompt,
            call_ai_json=call_ai_json,
        )
        self.assertEqual(pack.status, TopicChangeStatus.PENDING)
        self.assertEqual(len(pack.actions), 2)
        self.assertIn("candidate_extract", calls)
        self.assertIn("detail_expand_1", calls)

    def test_pipeline_appends_low_model_digest_to_stage_prompts(self):
        prompts = []
        variables = self._variables()
        variables["low_model_digest_json"] = json.dumps(
            {"status": "success", "facts": [{"fact": "候選題材證據"}]},
            ensure_ascii=False,
        )

        def call_ai_json(prompt, stage):
            prompts.append(prompt)
            if stage == "candidate_extract":
                return {"candidates": [{"theme_id": "ai_power", "theme_name": "AI電源"}]}
            return {"actions": [{"theme_id": "ai_power", "theme_name": "AI電源"}]}

        pack, _logs = run_topic_pipeline(
            mode=TopicChangeMode.UPDATE,
            ai_model="gemini",
            change_id="change_test",
            iso_ts="2026-05-31T10:00:00+0800",
            structured_data={"existing_topic_profiles": []},
            prompt_variables=variables,
            load_prompt=self._load_prompt,
            render_prompt=self._render_prompt,
            call_ai_json=call_ai_json,
        )

        self.assertEqual(pack.status, TopicChangeStatus.PENDING)
        self.assertTrue(any("MiniMax M3 資料整理底稿" in prompt for prompt in prompts))
        self.assertTrue(any("候選題材證據" in prompt for prompt in prompts))

    def test_pipeline_keeps_pack_when_detail_batch_fails(self):
        def call_ai_json(prompt, stage):
            if stage == "candidate_extract":
                return {"candidates": [
                    {
                        "theme_id": "ai_server_cooling",
                        "theme_name": "AI伺服器散熱",
                        "source_refs": [{"source": "測試來源", "content": "AI散熱需求增加"}],
                    },
                    {
                        "theme_id": "high_speed_networking",
                        "theme_name": "高速網通",
                        "source_refs": [{"source": "測試來源", "content": "高速傳輸需求增加"}],
                    },
                    {"theme_id": "memory_recovery", "theme_name": "記憶體復甦"},
                    {"theme_id": "robotics", "theme_name": "機器人"},
                    {"theme_id": "power_grid", "theme_name": "電力基建"},
                ]}
            if stage == "detail_expand_1":
                raise ValueError("bad json")
            return {"actions": [{"theme_id": "power_grid", "theme_name": "電力基建"}]}

        pack, logs = run_topic_pipeline(
            mode=TopicChangeMode.UPDATE,
            ai_model="minimax",
            change_id="change_test",
            iso_ts="2026-05-31T10:00:00+0800",
            structured_data={"existing_topic_profiles": []},
            prompt_variables=self._variables(),
            load_prompt=self._load_prompt,
            render_prompt=self._render_prompt,
            call_ai_json=call_ai_json,
        )
        self.assertEqual(pack.status, TopicChangeStatus.PENDING)
        self.assertEqual(len(pack.actions), 5)
        self.assertTrue(pack.actions[0].evidence)
        self.assertTrue(pack.actions[1].evidence)
        self.assertTrue(any("bad json" in warning for warning in pack.warnings))
        self.assertTrue(any(log.get("stage") == "detail_expand_1_local_fallback" for log in logs))

    def test_pipeline_retries_malformed_detail_json_once(self):
        stages = []

        def call_ai_json(_prompt, stage):
            stages.append(stage)
            if stage == "candidate_extract":
                return {"candidates": [{"theme_id": "ai_power", "theme_name": "AI電源"}]}
            if stage == "detail_expand_1":
                raise ValueError("Expecting property name enclosed in double quotes: line 1 column 2")
            if stage == "detail_expand_1_retry":
                return {"actions": [{"theme_id": "ai_power", "theme_name": "AI電源"}]}
            return {"actions": []}

        pack, logs = run_topic_pipeline(
            mode=TopicChangeMode.UPDATE,
            ai_model="minimax",
            change_id="change_test",
            iso_ts="2026-05-31T10:00:00+0800",
            structured_data={"existing_topic_profiles": []},
            prompt_variables=self._variables(),
            load_prompt=self._load_prompt,
            render_prompt=self._render_prompt,
            call_ai_json=call_ai_json,
        )

        self.assertEqual(pack.status, TopicChangeStatus.PENDING)
        self.assertIn("detail_expand_1_retry", stages)
        self.assertTrue(any(log.get("stage") == "detail_expand_1_recovered" for log in logs))
        self.assertFalse(any(log.get("stage") == "detail_expand_1_local_fallback" for log in logs))

    def test_pipeline_retries_detail_timeout_once(self):
        stages = []

        def call_ai_json(_prompt, stage):
            stages.append(stage)
            if stage == "candidate_extract":
                return {"candidates": [{"theme_id": "ai_power", "theme_name": "AI電源"}]}
            if stage == "detail_expand_1":
                raise TimeoutError("MiniMax API request failed; status=timeout; reason=ReadTimeout")
            if stage == "detail_expand_1_retry":
                return {"actions": [{"theme_id": "ai_power", "theme_name": "AI電源"}]}
            return {"actions": []}

        pack, logs = run_topic_pipeline(
            mode=TopicChangeMode.UPDATE,
            ai_model="minimax",
            change_id="change_test",
            iso_ts="2026-05-31T10:00:00+0800",
            structured_data={"existing_topic_profiles": []},
            prompt_variables=self._variables(),
            load_prompt=self._load_prompt,
            render_prompt=self._render_prompt,
            call_ai_json=call_ai_json,
        )

        self.assertEqual(pack.status, TopicChangeStatus.PENDING)
        self.assertIn("detail_expand_1_retry", stages)
        self.assertTrue(any(log.get("stage") == "detail_expand_1_recovered" for log in logs))
        self.assertFalse(any(log.get("stage") == "detail_expand_1_local_fallback" for log in logs))

    def test_pipeline_compacts_large_detail_stage_inputs(self):
        prompts = {}
        variables = self._variables()
        variables["webfetch_evidence_json"] = "E" * 30000
        variables["web_fetched_sources_json"] = "W" * 30000
        variables["existing_topic_profiles_json"] = "P" * 12000
        variables["company_topic_map_json"] = "M" * 12000
        variables["supply_chain_nodes_json"] = "S" * 12000
        variables["company_knowledge_json"] = "K" * 12000
        variables["low_model_digest_json"] = json.dumps(
            {"status": "success", "facts": [{"fact": "D" * 12000}]},
            ensure_ascii=False,
        )

        def load_prompt(name):
            if name == "topic_candidate_extract":
                return "{webfetch_evidence_json} {web_fetched_sources_json} {low_model_digest_json}"
            if name == "topic_detail_expand":
                return (
                    "{topic_candidates_json} {webfetch_evidence_json} "
                    "{web_fetched_sources_json} {existing_topic_profiles_json} "
                    "{company_topic_map_json} {supply_chain_nodes_json} "
                    "{company_knowledge_json} {low_model_digest_json}"
                )
            return ""

        def call_ai_json(prompt, stage):
            prompts[stage] = prompt
            if stage == "candidate_extract":
                return {"candidates": [{"theme_id": "ai_power", "theme_name": "AI電源"}]}
            return {"actions": [{"theme_id": "ai_power", "theme_name": "AI電源"}]}

        pack, _logs = run_topic_pipeline(
            mode=TopicChangeMode.UPDATE,
            ai_model="minimax",
            change_id="change_test",
            iso_ts="2026-05-31T10:00:00+0800",
            structured_data={"existing_topic_profiles": []},
            prompt_variables=variables,
            load_prompt=load_prompt,
            render_prompt=self._render_prompt,
            call_ai_json=call_ai_json,
        )

        self.assertEqual(pack.status, TopicChangeStatus.PENDING)
        self.assertLess(len(prompts["detail_expand_1"]), 24000)
        self.assertIn("truncated for detail stage", prompts["detail_expand_1"])

    def test_pipeline_uses_local_fallback_when_candidate_stage_too_large(self):
        prompts = {}
        variables = self._variables()
        variables["webfetch_evidence_json"] = "E" * 50000
        variables["web_fetched_sources_json"] = "W" * 50000
        variables["discovery_sources_json"] = "D" * 50000
        variables["recent_scan_candidates_json"] = "R" * 50000
        variables["market_signals_json"] = "V" * 50000
        variables["external_topic_source_caches_json"] = "X" * 50000
        variables["existing_topic_profiles_json"] = "P" * 50000
        variables["company_topic_map_json"] = "M" * 50000
        variables["supply_chain_nodes_json"] = "S" * 50000
        variables["company_knowledge_json"] = "K" * 50000
        variables["low_model_digest_json"] = json.dumps(
            {"status": "success", "facts": [{"fact": "L" * 50000}]},
            ensure_ascii=False,
        )

        def load_prompt(name):
            if name == "topic_candidate_extract":
                block = (
                    "{webfetch_evidence_json} {web_fetched_sources_json} "
                    "{discovery_sources_json} {external_topic_source_caches_json} "
                    "{recent_scan_candidates_json} {market_signals_json} "
                    "{existing_topic_profiles_json} {company_topic_map_json} "
                    "{supply_chain_nodes_json} {company_knowledge_json} "
                    "{low_model_digest_json}"
                )
                return "\n".join([block, block, block])
            if name == "topic_detail_expand":
                return "{topic_candidates_json}"
            return ""

        def call_ai_json(prompt, stage):
            prompts[stage] = prompt
            if stage == "candidate_extract":
                raise AssertionError("candidate_extract should be skipped when prompt exceeds hard limit")
            return {"actions": [{"theme_id": "ai_power", "theme_name": "AI電源"}]}

        pack, logs = run_topic_pipeline(
            mode=TopicChangeMode.UPDATE,
            ai_model="minimax",
            change_id="change_test",
            iso_ts="2026-05-31T10:00:00+0800",
            structured_data={
                "existing_topic_profiles": [],
                "webfetch_evidence": {
                    "items": [
                        {
                            "title": "AI 電源供應題材升溫",
                            "claim": "AI資料中心推升電源供應與BBU需求。",
                            "topic_hints": ["AI伺服器電源與BBU"],
                            "companies": ["2308"],
                        }
                    ]
                },
            },
            prompt_variables=variables,
            load_prompt=load_prompt,
            render_prompt=self._render_prompt,
            call_ai_json=call_ai_json,
        )

        self.assertEqual(pack.status, TopicChangeStatus.PENDING)
        self.assertNotIn("candidate_extract", prompts)
        self.assertIn("detail_expand_1", prompts)
        self.assertTrue(any(str(log.get("error", "")).startswith("prompt_too_large") for log in logs))

    def test_pipeline_falls_back_to_local_candidates_when_candidate_extract_fails(self):
        def call_ai_json(prompt, stage):
            if stage.startswith("candidate_extract"):
                raise TimeoutError("candidate timeout")
            return {
                "actions": [
                    {"theme_id": "mlcc_passive_components", "theme_name": "MLCC與被動元件"},
                    {"theme_id": "heavy_electrical_power_grid", "theme_name": "重電與強韌電網"},
                ]
            }

        structured_data = {
            "existing_topic_profiles": [],
            "webfetch_evidence": {
                "items": [
                    {
                        "title": "MLCC 缺貨帶動被動元件漲價",
                        "claim": "國巨與華新科受惠 MLCC 缺貨與報價上漲。",
                        "companies": ["2327", "2492"],
                    },
                    {
                        "title": "AI資料中心推升重電與變壓器需求",
                        "snippet": "華城、士電受惠強韌電網與變壓器訂單。",
                        "companies": ["1519", "1503"],
                    },
                ]
            },
        }

        pack, logs = run_topic_pipeline(
            mode=TopicChangeMode.UPDATE,
            ai_model="minimax",
            change_id="change_test",
            iso_ts="2026-05-31T10:00:00+0800",
            structured_data=structured_data,
            prompt_variables=self._variables(),
            load_prompt=self._load_prompt,
            render_prompt=self._render_prompt,
            call_ai_json=call_ai_json,
        )

        self.assertEqual(pack.status, TopicChangeStatus.PENDING)
        self.assertGreaterEqual(pack.extra["candidate_count"], 2)
        self.assertGreaterEqual(len(pack.actions), 2)
        self.assertTrue(any(log.get("stage") == "candidate_fallback" for log in logs))
        self.assertTrue(any("candidate timeout" in warning for warning in pack.warnings))

    def test_pipeline_retries_candidate_timeout_once(self):
        stages = []

        def call_ai_json(prompt, stage):
            stages.append(stage)
            if stage == "candidate_extract":
                raise TimeoutError("ReadTimeout: candidate timeout")
            if stage == "candidate_extract_retry":
                return {"candidates": [{"theme_id": "ai_power", "theme_name": "AI電源"}]}
            return {"actions": [{"theme_id": "ai_power", "theme_name": "AI電源"}]}

        pack, logs = run_topic_pipeline(
            mode=TopicChangeMode.UPDATE,
            ai_model="minimax",
            change_id="change_test",
            iso_ts="2026-05-31T10:00:00+0800",
            structured_data={"existing_topic_profiles": []},
            prompt_variables=self._variables(),
            load_prompt=self._load_prompt,
            render_prompt=self._render_prompt,
            call_ai_json=call_ai_json,
        )

        self.assertEqual(pack.status, TopicChangeStatus.PENDING)
        self.assertIn("candidate_extract_retry", stages)
        self.assertFalse(any(log.get("stage") == "candidate_fallback" for log in logs))
        self.assertTrue(any(log.get("stage") == "candidate_extract_recovered" for log in logs))

    def test_pipeline_fails_when_ai_exposes_model_reasoning(self):
        def call_ai_json(prompt, stage):
            if stage == "candidate_extract":
                return {
                    "candidates": [
                        {"theme_id": "ai_power", "theme_name": "AI ??", "keywords": ["AI ??"]},
                    ],
                    "raw": "<think>The user wants internal reasoning.</think>",
                }
            return {"actions": [{"theme_id": "ai_power", "theme_name": "AI ??"}]}

        pack, _logs = run_topic_pipeline(
            mode=TopicChangeMode.UPDATE,
            ai_model="minimax",
            change_id="change_test",
            iso_ts="2026-05-31T10:00:00+0800",
            structured_data={"existing_topic_profiles": []},
            prompt_variables=self._variables(),
            load_prompt=self._load_prompt,
            render_prompt=self._render_prompt,
            call_ai_json=call_ai_json,
        )

        self.assertEqual(pack.status, TopicChangeStatus.FAILED)
        self.assertTrue(any("model_reasoning_exposed" in warning or "<think>" in warning for warning in pack.warnings))


if __name__ == "__main__":
    unittest.main()
