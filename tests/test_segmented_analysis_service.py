from __future__ import annotations

from dataclasses import dataclass

from research_center.command_parser import parse_command_text
from research_center.config import ResearchCenterConfig
from research_center.orchestrator import ResearchCenter
from research_center.segmented_analysis_service import (
    SEGMENTED_ANALYSIS_PROMPT_THRESHOLD,
    run_segmented_theme_analysis,
    should_use_segmented_analysis,
)
from tests.test_cache_utils import ensure_test_cache_dir, safe_remove_test_cache


@dataclass
class _FakeResult:
    markdown: str
    raw: dict
    diagnostics: dict


class _FakeMiniMax:
    def __init__(self, fail_on: int | None = None):
        self.prompts: list[str] = []
        self.fail_on = fail_on

    def is_configured(self):
        return True

    def generate_report(self, prompt: str):
        self.prompts.append(prompt)
        if self.fail_on is not None and len(self.prompts) == self.fail_on:
            raise RuntimeError("context window exceeds limit")
        return _FakeResult(
            markdown=f"## segment {len(self.prompts)}\n分析完成",
            raw={"call": len(self.prompts)},
            diagnostics={"model": "MiniMax-M3", "call": len(self.prompts)},
        )


class _FakeGemini:
    def __init__(self):
        self.prompts: list[str] = []
        self.enable_grounding_values: list[bool] = []

    def generate_report(self, prompt: str, enable_grounding: bool = False):
        self.prompts.append(prompt)
        self.enable_grounding_values.append(enable_grounding)
        return _FakeResult(
            markdown=f"## gemini segment {len(self.prompts)}",
            raw={"call": len(self.prompts)},
            diagnostics={"actual_model": "gemini-test", "call": len(self.prompts)},
        )


def _theme_radar_data() -> dict:
    market_rows = [
        {"code": f"{2300 + idx}", "name": f"Stock{idx}", "industry": "電子零組件業", "change_pct": 9 - idx * 0.1, "volume_ratio": 2.0}
        for idx in range(60)
    ]
    return {
        "command_role": "market_theme_radar",
        "report_date": "2026-05-29",
        "market_data_date": "2026-05-29",
        "market_movers": {
            "market_data_date": "2026-05-29",
            "top_gainers": market_rows,
            "top_volume_surge": market_rows,
            "top_turnover": market_rows,
            "new_highs": market_rows,
            "sector_mover_rankings": [{"sector": "電子零組件業", "sector_score": 95, "top_gainers": market_rows[:5]}],
        },
        "sector_strength": {
            "sector_rankings": [{"sector": "電子零組件業", "sector_score": 95, "sector_strong_samples": market_rows[:10]}],
            "subsector_rankings": [{"sector": "電子零組件業", "subsector": "被動元件", "subsector_score": 92, "strong_samples": market_rows[:8]}],
        },
        "subsector_rankings": [{"sector": "電子零組件業", "subsector": "被動元件", "subsector_score": 92, "strong_samples": market_rows[:8]}],
        "theme_rankings": [{"theme_id": "passive_component", "theme_name": "被動元件", "theme_strength_score": 90}],
        "theme_flow_summaries": [{"theme_query": "被動元件", "related_stock_count": 8, "layers": []}],
        "strong_stocks": market_rows,
        "data_quality": {"theme_mapped_stock_rows": 30},
    }


def _sector_strength_data() -> dict:
    data = _theme_radar_data()
    sector = data["sector_strength"]
    return {
        **data,
        "command_role": "sector_strength",
        "sector_rankings": sector["sector_rankings"] * 8,
        "subsector_rankings": sector["subsector_rankings"] * 12,
    }


def _theme_flow_data() -> dict:
    related = [
        {
            "code": f"{3000 + idx}",
            "name": f"FlowStock{idx}",
            "industry": "電子零組件業",
            "sector": "電子零組件",
            "primary_subsector": "電源",
            "change_pct": 5.0 + idx * 0.01,
            "volume_ratio": 1.5,
            "turnover": 100000 + idx,
            "latest_monthly_revenue": 1000000 + idx,
            "revenue_yoy": 20 + idx * 0.1,
            "theme_matches": [{"theme_id": "ai_power", "theme_name": "AI電源", "status": "verified"}],
        }
        for idx in range(85)
    ]
    layers = [
        {
            "layer": idx + 1,
            "name": f"Layer{idx + 1}",
            "nodes": [f"node-{idx}-{node}" for node in range(20)],
            "current_strength": "strong",
            "stage": "擴散",
            "representative_stocks": related[idx * 5: idx * 5 + 8],
            "candidate_stocks": related[idx * 5 + 8: idx * 5 + 16],
            "inference": "AI電源需求延伸",
            "verification_needed": "營收與訂單驗證",
        }
        for idx in range(5)
    ]
    return {
        "command_role": "theme_flow",
        "report_date": "2026-05-29",
        "market_data_date": "2026-05-29",
        "theme_query": "AI電源",
        "theme": {"theme_id": "ai_power", "theme_name": "AI電源", "keywords": ["AI電源", "電源供應器"]},
        "related_stock_count": len(related),
        "related_stocks": related,
        "layers": layers,
        "layer_market_validation": [{"layer": layer["layer"], "status": "盤面已驗證", "market_validated": True, "strong_samples": related[:5]} for layer in layers],
        "next_layer_candidates": [{"layer": layer["layer"], "candidate": layer["name"], "reason": "供應鏈擴散"} for layer in layers],
        "news_stats": [{"keyword": "AI電源", "news_count_7d": 12, "trend": "up"}],
        "market_movers": {"market_data_date": "2026-05-29", "top_gainers": related[:40], "top_volume_surge": related[:40]},
        "sector_rankings": [{"sector": "電子零組件", "sector_score": 90, "sector_strong_samples": related[:8]}],
        "subsector_rankings": [{"sector": "電子零組件", "subsector": "電源", "subsector_score": 88, "strong_samples": related[:8]}],
        "data_quality": {"related_stock_count": len(related)},
    }


def test_should_use_segmented_analysis_uses_prompt_size_for_all_models():
    large_prompt = SEGMENTED_ANALYSIS_PROMPT_THRESHOLD
    assert should_use_segmented_analysis(parse_command_text("/theme_radar --model minimax"), "minimax", prompt_chars=large_prompt) is True
    assert should_use_segmented_analysis(parse_command_text("/sector_strength --model gemini"), "gemini", prompt_chars=large_prompt) is True
    assert should_use_segmented_analysis(parse_command_text("/theme_flow AI伺服器 --model deepseek"), "deepseek", prompt_chars=large_prompt) is True
    assert should_use_segmented_analysis(parse_command_text("/theme_radar --model deepseek"), "deepseek", prompt_chars=large_prompt - 1) is False
    assert should_use_segmented_analysis(parse_command_text("/research 2330 --model minimax"), "minimax", prompt_chars=large_prompt) is False
    assert should_use_segmented_analysis(parse_command_text("/theme_radar --model minimax"), "minimax") is False


def test_segmented_theme_analysis_calls_multiple_small_prompts():
    request = parse_command_text("/theme_radar --model minimax")
    client = _FakeMiniMax()

    result = run_segmented_theme_analysis(
        request=request,
        structured_data=_theme_radar_data(),
        sources=[],
        ai_client=client,
        model_name="MiniMax-M3",
    )

    assert len(client.prompts) == 9
    assert result.diagnostics["mode"] == "segmented_theme_analysis"
    assert result.diagnostics["success_count"] == 8
    assert result.diagnostics["final_status"] == "success"
    assert all(len(prompt) < 50000 for prompt in client.prompts)
    assert "segment 9" in result.markdown


def test_segmented_theme_analysis_keeps_report_when_one_segment_fails():
    request = parse_command_text("/theme_radar --model minimax")
    client = _FakeMiniMax(fail_on=2)

    result = run_segmented_theme_analysis(
        request=request,
        structured_data=_theme_radar_data(),
        sources=[],
        ai_client=client,
        model_name="MiniMax-M3",
    )

    assert result.diagnostics["fallback_count"] == 1
    assert result.segment_runs[1].status == "fallback"
    assert "segment 9" in result.markdown


def test_sector_strength_segmented_analysis_uses_market_and_sector_segments():
    request = parse_command_text("/sector_strength --model minimax")
    client = _FakeMiniMax()

    result = run_segmented_theme_analysis(
        request=request,
        structured_data=_sector_strength_data(),
        sources=[],
        ai_client=client,
        model_name="MiniMax-M3",
    )

    assert result.diagnostics["segment_count"] == 5
    assert len(client.prompts) == 6
    assert [run.label for run in result.segment_runs] == [
        "market_price_rankings",
        "market_sector_movers",
        "sector_strength",
        "subsector_strength",
        "sector_subsector",
    ]


def test_theme_flow_segmented_analysis_batches_related_stocks_and_layers():
    request = parse_command_text("/theme_flow AI電源 --model minimax")
    client = _FakeMiniMax()

    result = run_segmented_theme_analysis(
        request=request,
        structured_data=_theme_flow_data(),
        sources=[],
        ai_client=client,
        model_name="MiniMax-M3",
    )

    assert result.diagnostics["segment_count"] == 10
    assert len(client.prompts) == 11
    labels = [run.label for run in result.segment_runs]
    assert labels[:4] == [
        "theme_flow_profile",
        "theme_flow_related_stocks_1",
        "theme_flow_related_stocks_2",
        "theme_flow_related_stocks_3",
    ]
    assert "theme_flow_layers_3" in labels
    assert labels[-3:] == [
        "theme_flow_market_validation",
        "theme_flow_next_candidates",
        "theme_flow_news_stats",
    ]
    assert all(len(prompt) < 50000 for prompt in client.prompts)


def test_theme_radar_minimax_orchestrator_uses_segmented_flow(monkeypatch):
    tmp_path = ensure_test_cache_dir("segmented_analysis/orchestrator")
    request = parse_command_text("/theme_radar --model minimax")
    try:
        monkeypatch.setattr("research_center.orchestrator.collect_structured_data", lambda req, progress=None: (_theme_radar_data(), []))
        monkeypatch.setattr("research_center.orchestrator.filter_and_sort_sources_for_analysis_date", lambda sources, req: (sources, []))
        monkeypatch.setattr("research_center.orchestrator._select_sources_for_prompt", lambda req, sources, data, progress=None: sources)
        monkeypatch.setattr("research_center.orchestrator._enrich_sources_with_web_fetch", lambda *args, **kwargs: None)
        monkeypatch.setattr("research_center.orchestrator.persist_search_sources_to_news", lambda *args, **kwargs: None)
        monkeypatch.setattr("research_center.orchestrator.attach_news_events", lambda *args, **kwargs: None)
        monkeypatch.setattr("research_center.orchestrator.attach_data_gap_summary", lambda *args, **kwargs: None)
        monkeypatch.setattr("research_center.orchestrator.attach_unified_evidence_pack", lambda *args, **kwargs: None)
        monkeypatch.setattr("research_center.orchestrator.build_prompt", lambda *args, **kwargs: "x" * SEGMENTED_ANALYSIS_PROMPT_THRESHOLD)

        config = ResearchCenterConfig(
            api_key=None,
            minimax_api_key="test-key",
            enable_grounding=False,
            report_root=tmp_path / "reports",
            database_path=tmp_path / "research.db",
        )
        center = ResearchCenter(config)
        center.minimax = _FakeMiniMax()
        monkeypatch.setattr(center._gemini_discovery_runner, "run_discovery_flow", lambda req, sources, data, use_grounding, progress=None: (sources, False))

        result = center.run(request)

        assert result.ai_used is True
        assert result.ai_model == "MiniMax-M3"
        assert result.report_json["metadata"]["analysis_provider"] == "minimax_segmented"
        segmented = result.report_json["metadata"]["segmented_ai_analysis"]
        assert segmented["mode"] == "segmented_theme_analysis"
        assert segmented["segment_count"] == 8
        assert len(center.minimax.prompts) == 9
    finally:
        safe_remove_test_cache("segmented_analysis/orchestrator")


def test_theme_radar_deepseek_orchestrator_uses_segmented_flow(monkeypatch):
    tmp_path = ensure_test_cache_dir("segmented_analysis/orchestrator_deepseek")
    request = parse_command_text("/theme_radar --model deepseek")
    try:
        monkeypatch.setattr("research_center.orchestrator.collect_structured_data", lambda req, progress=None: (_theme_radar_data(), []))
        monkeypatch.setattr("research_center.orchestrator.filter_and_sort_sources_for_analysis_date", lambda sources, req: (sources, []))
        monkeypatch.setattr("research_center.orchestrator._select_sources_for_prompt", lambda req, sources, data, progress=None: sources)
        monkeypatch.setattr("research_center.orchestrator._enrich_sources_with_web_fetch", lambda *args, **kwargs: None)
        monkeypatch.setattr("research_center.orchestrator.persist_search_sources_to_news", lambda *args, **kwargs: None)
        monkeypatch.setattr("research_center.orchestrator.attach_news_events", lambda *args, **kwargs: None)
        monkeypatch.setattr("research_center.orchestrator.attach_data_gap_summary", lambda *args, **kwargs: None)
        monkeypatch.setattr("research_center.orchestrator.attach_unified_evidence_pack", lambda *args, **kwargs: None)
        monkeypatch.setattr("research_center.orchestrator.build_prompt", lambda *args, **kwargs: "x" * SEGMENTED_ANALYSIS_PROMPT_THRESHOLD)

        config = ResearchCenterConfig(
            api_key=None,
            opencode_api_key="test-key",
            enable_opencode_analysis=True,
            enable_grounding=False,
            report_root=tmp_path / "reports",
            database_path=tmp_path / "research.db",
        )
        center = ResearchCenter(config)
        center.opencode = _FakeMiniMax()
        monkeypatch.setattr(center._gemini_discovery_runner, "run_discovery_flow", lambda req, sources, data, use_grounding, progress=None: (sources, False))

        result = center.run(request)

        assert result.ai_used is True
        assert result.report_json["metadata"]["analysis_provider"] == "opencode_go_segmented"
        segmented = result.report_json["metadata"]["segmented_ai_analysis"]
        assert segmented["original_prompt_chars"] == SEGMENTED_ANALYSIS_PROMPT_THRESHOLD
        assert len(center.opencode.prompts) == 9
    finally:
        safe_remove_test_cache("segmented_analysis/orchestrator_deepseek")


def test_theme_radar_gemini_orchestrator_uses_segmented_flow(monkeypatch):
    tmp_path = ensure_test_cache_dir("segmented_analysis/orchestrator_gemini")
    request = parse_command_text("/theme_radar --model gemini")
    try:
        monkeypatch.setattr("research_center.orchestrator.collect_structured_data", lambda req, progress=None: (_theme_radar_data(), []))
        monkeypatch.setattr("research_center.orchestrator.filter_and_sort_sources_for_analysis_date", lambda sources, req: (sources, []))
        monkeypatch.setattr("research_center.orchestrator._select_sources_for_prompt", lambda req, sources, data, progress=None: sources)
        monkeypatch.setattr("research_center.orchestrator._enrich_sources_with_web_fetch", lambda *args, **kwargs: None)
        monkeypatch.setattr("research_center.orchestrator.persist_search_sources_to_news", lambda *args, **kwargs: None)
        monkeypatch.setattr("research_center.orchestrator.attach_news_events", lambda *args, **kwargs: None)
        monkeypatch.setattr("research_center.orchestrator.attach_data_gap_summary", lambda *args, **kwargs: None)
        monkeypatch.setattr("research_center.orchestrator.attach_unified_evidence_pack", lambda *args, **kwargs: None)
        monkeypatch.setattr("research_center.orchestrator.build_prompt", lambda *args, **kwargs: "x" * SEGMENTED_ANALYSIS_PROMPT_THRESHOLD)

        config = ResearchCenterConfig(
            api_key="test-key",
            enable_grounding=True,
            report_root=tmp_path / "reports",
            database_path=tmp_path / "research.db",
        )
        center = ResearchCenter(config)
        center.gemini = _FakeGemini()
        monkeypatch.setattr(center._gemini_discovery_runner, "run_discovery_flow", lambda req, sources, data, use_grounding, progress=None: (sources, True))

        result = center.run(request)

        assert result.ai_used is True
        assert result.report_json["metadata"]["analysis_provider"] == "gemini_segmented"
        segmented = result.report_json["metadata"]["segmented_ai_analysis"]
        assert segmented["original_prompt_chars"] == SEGMENTED_ANALYSIS_PROMPT_THRESHOLD
        assert len(center.gemini.prompts) == 9
        assert center.gemini.enable_grounding_values == [False, False, False, False, False, False, False, False, False]
    finally:
        safe_remove_test_cache("segmented_analysis/orchestrator_gemini")
