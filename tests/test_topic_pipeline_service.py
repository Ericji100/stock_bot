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
        self.assertTrue(any("MiniMax M2.7 資料整理底稿" in prompt for prompt in prompts))
        self.assertTrue(any("候選題材證據" in prompt for prompt in prompts))

    def test_pipeline_keeps_pack_when_detail_batch_fails(self):
        def call_ai_json(prompt, stage):
            if stage == "candidate_extract":
                return {"candidates": [
                    {"theme_id": "ai_server_cooling", "theme_name": "AI伺服器散熱"},
                    {"theme_id": "high_speed_networking", "theme_name": "高速網通"},
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
        self.assertEqual(len(pack.actions), 1)
        self.assertTrue(any("bad json" in warning for warning in pack.warnings))


if __name__ == "__main__":
    unittest.main()
