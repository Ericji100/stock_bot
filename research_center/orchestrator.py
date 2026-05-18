from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from data_fetcher import StockExportError

from .command_parser import parse_command_text
from .config import ROOT_DIR, ResearchCenterConfig, load_research_config
from .data_services import collect_structured_data
from .database import ResearchDatabase
from .date_guard import filter_sources_for_report_date
from .event_store import build_source_events, extract_structured_events, historical_policy
from .gemini_service import GeminiService, build_prompt
from .minimax_service import MiniMaxService
from .minimax_search_service import MiniMaxSearchService
from .opencode_service import OpenCodeService
from .tavily_search_service import TavilySearchService, TavilyQuotaError
from .quota_guard import SearchProviderQuotaGuard
from .knowledge_drafts import write_knowledge_draft
from .models import CommandRequest, ReportArtifacts, ResearchCenterResult, SourceItem
from .prompt_registry import build_grounding_discovery_prompts, prompt_metadata
from .prompt_logging import write_prompt_log
from .source_snapshots import build_source_snapshots, snapshots_to_structured_context, target_for_snapshots
from .report_builder import fallback_markdown, summarize_for_telegram, write_report_artifacts
from .research_logger import log_error, log_task
from .scoring_engine import build_buy_rating, build_local_scores
from .web_fetch_service import WebFetchService

ProgressCallback = Callable[[str], None]


class ResearchCenter:
    def __init__(self, config: ResearchCenterConfig | None = None):
        self.config = config or load_research_config()
        self.database = ResearchDatabase(self.config.database_path)
        self.gemini = GeminiService(
            api_key=self.config.api_key,
            model=self.config.model,
            enable_grounding=self.config.enable_grounding,
            fallback_models=self.config.fallback_models,
        )
        self.minimax = MiniMaxService(
            api_key=self.config.minimax_api_key,
            model=self.config.minimax_model,
            base_url=self.config.minimax_base_url,
        )
        self.minimax_search = MiniMaxSearchService(
            serper_api_key=self.config.serper_api_key,
            jina_api_key=self.config.jina_api_key,
            minimax=self.minimax,
        )
        self.opencode = OpenCodeService(
            api_key=self.config.opencode_api_key,
            model=self.config.opencode_model,
            base_url=self.config.opencode_base_url,
            reasoning_effort=self.config.opencode_reasoning_effort,
        )
        self.tavily_search = TavilySearchService(
            api_key=self.config.tavily_api_key,
            enable_search=self.config.enable_tavily_search,
            enable_extract=self.config.enable_tavily_extract,
            search_depth=self.config.tavily_search_depth,
            extract_depth=self.config.tavily_extract_depth,
            max_results_per_query=self.config.tavily_max_results_per_query,
            max_extract_urls_per_task=self.config.tavily_max_extract_urls_per_task,
        )
        self.quota_guard = SearchProviderQuotaGuard(ROOT_DIR / ".cache" / "search_provider_quota.json")
        self._gemini_discovery_runner = _GeminiDiscoveryRunner(self)

    def parse(self, raw_text: str, user_id: str | None = None) -> CommandRequest:
        request = parse_command_text(raw_text, user_id=user_id)
        return _with_output_formats(request, self.config.output_formats)

    def run_text_command(self, raw_text: str, user_id: str | None = None, progress: ProgressCallback | None = None) -> ResearchCenterResult:
        _emit_progress(progress, "解析 AI 投研指令")
        request = self.parse(raw_text, user_id=user_id)
        _emit_progress(progress, f"指令解析完成：/{request.command} mode={request.mode}")
        return self.run(request, progress=progress)

    def should_use_parallel_model_reports(self, raw_text: str, user_id: str | None = None) -> bool:
        try:
            request = self.parse(raw_text, user_id=user_id)
        except Exception:
            return False
        return bool(
            request.command != "report"
            and not request.source_only
            and self.config.enable_minimax_comparison
            and self.minimax.is_configured()
            and self.gemini.is_configured()
        )

    def prepare_parallel_model_run(self, raw_text: str, user_id: str | None = None, progress: ProgressCallback | None = None) -> ResearchCenterResult:
        _emit_progress(progress, "解析 AI 投研指令")
        request = self.parse(raw_text, user_id=user_id)
        _emit_progress(progress, f"指令解析完成：/{request.command} mode={request.mode}")
        if request.command == "report" or request.source_only:
            return self.run(request, progress=progress)

        _emit_progress(progress, "開始收集結構化資料與外部來源")
        structured_data, base_sources = collect_structured_data(request, progress=progress)
        _emit_progress(progress, f"資料收集完成：來源 {len(base_sources)} 筆")

        sources: list[SourceItem] = list(base_sources)
        dropped_sources: list[str] = []
        if request.report_date is not None:
            _emit_progress(progress, "套用 --date 日期治理")
            sources, dropped_sources = filter_sources_for_report_date(sources, request.report_date)
        if request.report_date is not None:
            snapshot_target = target_for_snapshots(request, structured_data)
            snapshots = self.database.query_snapshots_before(snapshot_target, request.report_date.isoformat(), command=request.command)
            structured_data["historical_snapshots"] = snapshots_to_structured_context(snapshots)
            _emit_progress(progress, f"歷史快照載入：{len(snapshots)} 筆；Gemini Search 將停用，只整理快照與本地資料")
        structured_data["historical_data_policy"] = historical_policy(request, dropped_sources)
        structured_data["prompt_policy"] = prompt_metadata(request)
        scores = build_local_scores(request, structured_data)
        mechanical_buy_rating = build_buy_rating(scores) if request.command == "research" and request.mode in {"score", "deep"} else None
        structured_data["local_scoring"] = {
            "name": "本地量化底稿",
            "role": "機械式資料檢查，不是最終投研評分。",
            "policy": "本地量化底稿依可驗證結構化資料保守計算；CAGR、護城河、轉型效益與題材熱度若缺來源不得高分。AI 最終投研評分必須根據全部資料、搜尋來源與反證重新評估。",
            "scores": scores,
            "buy_rating": mechanical_buy_rating,
            "mechanical_buy_rating": mechanical_buy_rating,
        }
        if scores:
            _emit_progress(progress, f"本地量化底稿完成：{len(scores)} 項")
        else:
            _emit_progress(progress, "本地量化底稿略過：本模式不需要機械式資料檢查，AI 將整理與分析已收集資料")

        _emit_progress(progress, "Build shared model prompt for parallel AI reports")
        use_grounding = self.config.enable_grounding and request.report_date is None
        sources, gemini_search_used = self._gemini_discovery_runner.run_discovery_flow(request, sources, structured_data, use_grounding, progress)
        _enrich_sources_with_web_fetch(request, sources, structured_data, progress)

        prompt = build_prompt(request, structured_data=structured_data, source_list=sources)
        prompt_log_path = write_prompt_log(request, prompt, self.config.model, use_grounding, sources, structured_data.get("prompt_policy"))
        _emit_progress(progress, f"Shared prompt saved: {prompt_log_path}")
        _emit_progress(progress, f"Prompt template={structured_data.get('prompt_policy', {}).get('template')}, length={len(prompt)} chars, grounding={use_grounding}, sources={len(sources)}")

        model_jobs = [
            {"model_key": "gemini", "model": self.config.model, "status": "pending", "prompt_path": str(prompt_log_path)},
            {"model_key": "minimax", "model": self.config.minimax_model, "status": "pending"},
        ]
        runtime_context = {
            "parallel_model_jobs": {
                "prompt": prompt,
                "prompt_path": str(prompt_log_path),
                "sources": list(sources),
                "structured_data": dict(structured_data),
                "use_grounding": bool(gemini_search_used),
                "model_jobs": model_jobs,
            }
        }
        summary = "多模型 AI 分析已開始，Gemini 與 MiniMax-M2.7 會並行產生報告；哪個模型先完成就會先傳送。"
        artifacts = ReportArtifacts("parallel_model_pending", request.command, Path("__no_markdown_file__"), Path("__no_html_file__"), Path("__no_json_file__"), Path("__no_sources_file__"))
        return ResearchCenterResult(
            status="pending_models",
            request=request,
            summary=summary,
            markdown=summary,
            report_json={"report_type": request.command, "summary": summary, "metadata": {"model_runs": model_jobs}},
            sources=sources,
            artifacts=artifacts,
            ai_used=False,
            ai_model=None,
            fallback_reason=None,
            runtime_context=runtime_context,
        )

    def run_parallel_model_job(self, result: ResearchCenterResult, model_key: str, progress: ProgressCallback | None = None) -> dict[str, Any]:
        context = ((result.runtime_context or {}).get("parallel_model_jobs") or {})
        prompt = context.get("prompt")
        if not prompt:
            return {"model_key": model_key, "model": model_key, "status": "failed", "error": "parallel prompt not available"}
        sources = list(context.get("sources") or result.sources)
        base_data = dict(context.get("structured_data") or {})
        use_grounding = bool(context.get("use_grounding"))
        if model_key == "gemini":
            return self._run_gemini_model_job(result.request, prompt, sources, base_data, use_grounding, progress)
        if model_key == "minimax":
            return self._run_minimax_model_job(result.request, prompt, sources, base_data, str(context.get("prompt_path") or ""), progress)
        return {"model_key": model_key, "model": model_key, "status": "failed", "error": "unknown model job"}

    def _run_gemini_model_job(self, request: CommandRequest, prompt: str, sources: list[SourceItem], structured_data: dict[str, Any], use_grounding: bool, progress: ProgressCallback | None) -> dict[str, Any]:
        prompt_log_path = ""
        try:
            _emit_progress(progress, f"Calling parallel AI model: {self.config.model}")
            prompt_log_path = str(write_prompt_log(request, prompt, self.config.model, use_grounding, sources, {**(structured_data.get("prompt_policy") or {}), "purpose": "parallel_model_report", "model_key": "gemini"}))
            _emit_progress(progress, f"Gemini model prompt saved: {prompt_log_path}")
            gemini_result = self.gemini.generate_report(prompt, enable_grounding=use_grounding)
            job_sources = _merge_sources(sources, gemini_result.sources)
            actual_model = gemini_result.diagnostics.get("actual_model") or self.config.model
            model_data = {**structured_data, "analysis_model": actual_model, "gemini_search_diagnostics": gemini_result.diagnostics}
            if gemini_result.diagnostics.get("fallback_used"):
                _emit_progress(progress, f"Gemini fallback used: {self.config.model} -> {actual_model}")
            if gemini_result.sources:
                _emit_progress(progress, f"Gemini grounding citations: {len(gemini_result.sources)} sources will be written")
            summary = summarize_for_telegram(gemini_result.markdown)
            draft_path = write_knowledge_draft(request, gemini_result.markdown, job_sources, model_data)
            if draft_path:
                model_data["knowledge_draft_path"] = str(draft_path)
                _emit_progress(progress, f"知識庫草稿已保存：{draft_path}")
            artifacts, report_json = write_report_artifacts(self.config.report_root, request, gemini_result.markdown, summary, job_sources, True, None, model_data)
            self.database.save_report(request, artifacts, summary, job_sources, True, None)
            self.database.save_events([*build_source_events(request, job_sources, model_data), *extract_structured_events(model_data)])
            self.database.save_snapshots(build_source_snapshots(request, job_sources, model_data, gemini_result.raw))
            _emit_progress(progress, f"Parallel AI model report completed: {artifacts.report_id}")
            return _model_job_entry("gemini", str(actual_model), "success", artifacts, prompt_log_path, summary, report_json, diagnostics=gemini_result.diagnostics)
        except Exception as exc:
            _emit_progress(progress, f"Parallel AI model report failed: {self.config.model}: {exc}")
            return {"model_key": "gemini", "model": self.config.model, "status": "failed", "error": str(exc), "prompt_path": prompt_log_path}

    def _run_minimax_model_job(self, request: CommandRequest, prompt: str, sources: list[SourceItem], structured_data: dict[str, Any], shared_prompt_path: str, progress: ProgressCallback | None) -> dict[str, Any]:
        minimax_prompt_log_path = ""
        try:
            _emit_progress(progress, f"Calling parallel AI model: {self.config.minimax_model}")
            minimax_prompt_log_path = str(write_prompt_log(
                request,
                prompt,
                self.config.minimax_model,
                False,
                sources,
                {**(structured_data.get("prompt_policy") or {}), "purpose": "parallel_model_report", "primary_model": self.config.model, "shared_prompt_path": shared_prompt_path, "model_key": "minimax"}))
            _emit_progress(progress, f"MiniMax model prompt saved: {minimax_prompt_log_path}")
            minimax_result = self.minimax.generate_report(prompt)
            summary = summarize_for_telegram(minimax_result.markdown)
            model_data = {**structured_data, "analysis_model": self.config.minimax_model, "minimax_diagnostics": minimax_result.diagnostics}
            model_data.pop("comparison_reports", None)
            artifacts, report_json = write_report_artifacts(self.config.report_root, request, minimax_result.markdown, summary, sources, True, None, model_data, report_variant="minimax")
            self.database.save_report(request, artifacts, summary, sources, True, None)
            self.database.save_events([*build_source_events(request, sources, model_data), *extract_structured_events(model_data)])
            self.database.save_snapshots(build_source_snapshots(request, sources, model_data, minimax_result.raw))
            _emit_progress(progress, f"Parallel AI model report completed: {artifacts.report_id}")
            return _model_job_entry("minimax", self.config.minimax_model, "success", artifacts, minimax_prompt_log_path, summary, report_json, diagnostics=minimax_result.diagnostics)
        except Exception as exc:
            _emit_progress(progress, f"Parallel AI model report failed: {self.config.minimax_model}: {exc}")
            return {"model_key": "minimax", "model": self.config.minimax_model, "status": "failed", "error": str(exc), "prompt_path": minimax_prompt_log_path}
    def run(self, request: CommandRequest, progress: ProgressCallback | None = None) -> ResearchCenterResult:
        if request.command == "report":
            _emit_progress(progress, "查詢歷史報告")
            return self._run_report_lookup(request)

        _emit_progress(progress, "開始收集結構化資料與外部來源")
        structured_data, base_sources = collect_structured_data(request, progress=progress)
        _emit_progress(progress, f"資料收集完成：來源 {len(base_sources)} 筆")

        sources: list[SourceItem] = list(base_sources)
        dropped_sources: list[str] = []
        if request.report_date is not None:
            _emit_progress(progress, "套用 --date 日期治理")
            sources, dropped_sources = filter_sources_for_report_date(sources, request.report_date)
        if request.report_date is not None:
            snapshot_target = target_for_snapshots(request, structured_data)
            snapshots = self.database.query_snapshots_before(snapshot_target, request.report_date.isoformat(), command=request.command)
            structured_data["historical_snapshots"] = snapshots_to_structured_context(snapshots)
            _emit_progress(progress, f"歷史快照載入：{len(snapshots)} 筆；Gemini Search 將停用，只整理快照與本地資料")
        structured_data["historical_data_policy"] = historical_policy(request, dropped_sources)
        structured_data["prompt_policy"] = prompt_metadata(request)
        selected_ai_model = request.ai_model or "gemini"
        structured_data["analysis_model_choice"] = selected_ai_model
        structured_data["analysis_model"] = self.config.model if selected_ai_model == "gemini" else self.config.opencode_model
        scores = build_local_scores(request, structured_data)
        mechanical_buy_rating = build_buy_rating(scores) if request.command == "research" and request.mode in {"score", "deep"} else None
        structured_data["local_scoring"] = {
            "name": "本地量化底稿",
            "role": "機械式資料檢查，不是最終投研評分。",
            "policy": "本地量化底稿依可驗證結構化資料保守計算；CAGR、護城河、轉型效益與題材熱度若缺來源不得高分。AI 最終投研評分必須根據全部資料、搜尋來源與反證重新評估。",
            "scores": scores,
            "buy_rating": mechanical_buy_rating,
            "mechanical_buy_rating": mechanical_buy_rating,
        }
        if scores:
            _emit_progress(progress, f"本地量化底稿完成：{len(scores)} 項")
        else:
            _emit_progress(progress, "本地量化底稿略過：本模式不需要機械式資料檢查，AI 將整理與分析已收集資料")

        ai_used = False
        fallback_reason: str | None = None
        gemini_raw: dict[str, Any] = {}
        actual_gemini_model: str | None = None
        runtime_context: dict[str, Any] = {}

        if request.source_only:
            _emit_progress(progress, "source-only 模式：略過 AI 模型，建立本地報告")
            markdown = fallback_markdown(request, structured_data, sources)
        else:
            try:
                _emit_progress(progress, f"Build AI prompt, selected_model={selected_ai_model}")
                use_grounding = self.config.enable_grounding and request.report_date is None
                sources, gemini_search_used = self._gemini_discovery_runner.run_discovery_flow(request, sources, structured_data, use_grounding, progress)
                _enrich_sources_with_web_fetch(request, sources, structured_data, progress)
                prompt = build_prompt(request, structured_data=structured_data, source_list=sources)
                final_model_name = self.config.model if selected_ai_model == "gemini" else self.config.opencode_model
                final_grounding = bool(gemini_search_used) and selected_ai_model == "gemini"
                prompt_log_path = write_prompt_log(request, prompt, final_model_name, final_grounding, sources, structured_data.get("prompt_policy"))
                _emit_progress(progress, f"Prompt saved: {prompt_log_path}")
                _emit_progress(progress, f"Prompt template={structured_data.get('prompt_policy', {}).get('template')}, length={len(prompt)} chars, grounding={final_grounding}, sources={len(sources)}")
                _emit_progress(progress, f"Calling AI model: {final_model_name}")
                if selected_ai_model == "deepseek":
                    if not self.config.enable_opencode_analysis or not self.opencode.is_configured():
                        raise RuntimeError("OpenCode Go / DeepSeek model is not enabled or API key is missing.")
                    opencode_result = self.opencode.generate_report(prompt)
                    markdown = opencode_result.markdown
                    gemini_raw = opencode_result.raw
                    actual_gemini_model = str(opencode_result.diagnostics.get("actual_model") or self.config.opencode_model)
                    structured_data["analysis_model"] = actual_gemini_model
                    structured_data["analysis_provider"] = "opencode_go"
                    structured_data["opencode_diagnostics"] = opencode_result.diagnostics
                    gemini_discovery_count = _gemini_discovery_source_count(structured_data)
                    if gemini_discovery_count:
                        _emit_progress(progress, f"DeepSeek analysis will use {gemini_discovery_count} Gemini Search fallback sources")
                else:
                    gemini_result = self.gemini.generate_report(prompt, enable_grounding=final_grounding)
                    markdown = gemini_result.markdown
                    gemini_raw = gemini_result.raw
                    structured_data["gemini_search_diagnostics"] = gemini_result.diagnostics
                    actual_gemini_model = str(gemini_result.diagnostics.get("actual_model") or self.config.model)
                    structured_data["analysis_model"] = actual_gemini_model
                    structured_data["analysis_provider"] = "gemini"
                    gemini_discovery_count = _gemini_discovery_source_count(structured_data)
                    if gemini_result.diagnostics.get("fallback_used"):
                        _emit_progress(progress, f"Gemini fallback used: {self.config.model} -> {actual_gemini_model}")
                    _emit_progress(progress, f"Gemini Search diagnostics: metadata={gemini_result.diagnostics.get('grounding_metadata_present')}, queries={gemini_result.diagnostics.get('web_search_query_count')}, chunks={gemini_result.diagnostics.get('grounding_chunk_count')}, sources={len(gemini_result.sources)}")
                    if gemini_result.sources:
                        _emit_progress(progress, f"Gemini grounding citations: {len(gemini_result.sources)} sources will be written")
                    elif gemini_discovery_count:
                        _emit_progress(progress, f"Final report returned no citations; keeping {gemini_discovery_count} Gemini Search fallback sources")
                    elif use_grounding:
                        _emit_progress(progress, "Gemini Search returned no parseable citations; report will keep diagnostics and local/existing sources")
                    sources = _merge_sources(sources, gemini_result.sources)
                ai_used = True
                _emit_progress(progress, f"AI model completed: {actual_gemini_model or self.config.model}")
                if selected_ai_model == "gemini" and self.config.enable_minimax_comparison and self.minimax.is_configured():
                    structured_data["comparison_reports"] = [{"model": self.config.minimax_model, "status": "pending"}]
                    runtime_context["minimax_comparison"] = {
                        "prompt": prompt,
                        "sources": list(sources),
                        "structured_data": dict(structured_data),
                    }
                    _emit_progress(progress, f"MiniMax comparison queued in background: {self.config.minimax_model}")
            except Exception as exc:
                fallback_reason = str(exc)
                _emit_progress(progress, f"AI 模型失敗，改用本地 fallback：{fallback_reason}")
                markdown = fallback_markdown(request, structured_data, sources, fallback_reason)

        _emit_progress(progress, "整理 Telegram 摘要與報告檔案")
        summary = summarize_for_telegram(markdown)
        draft_path = write_knowledge_draft(request, markdown, sources, structured_data)
        if draft_path:
            _emit_progress(progress, f"知識庫草稿已保存：{draft_path}")
            structured_data["knowledge_draft_path"] = str(draft_path)
        artifacts, report_json = write_report_artifacts(self.config.report_root, request, markdown, summary, sources, ai_used, fallback_reason, structured_data)

        _emit_progress(progress, "寫入報告 metadata 與事件資料庫")
        self.database.save_report(request, artifacts, summary, sources, ai_used, fallback_reason)
        self.database.save_events([*build_source_events(request, sources, structured_data), *extract_structured_events(structured_data)])
        self.database.save_snapshots(build_source_snapshots(request, sources, structured_data, gemini_raw))
        _emit_progress(progress, f"AI 投研任務完成：{artifacts.report_id}")

        return ResearchCenterResult(
            status="success",
            request=request,
            summary=summary,
            markdown=markdown,
            report_json=report_json,
            sources=sources,
            artifacts=artifacts,
            ai_used=ai_used,
            ai_model=(actual_gemini_model or self.config.model) if ai_used else None,
            fallback_reason=fallback_reason,
            runtime_context=runtime_context,
        )

    def run_minimax_comparison_for_result(self, result: ResearchCenterResult, progress: ProgressCallback | None = None) -> dict[str, Any]:
        context = (result.runtime_context or {}).get("minimax_comparison") or {}
        prompt = context.get("prompt")
        if not prompt:
            entry = {"model": self.config.minimax_model, "status": "skipped", "reason": "comparison prompt not available"}
            self._update_comparison_metadata(result, entry)
            return entry
        sources = list(context.get("sources") or result.sources)
        structured_data = dict(context.get("structured_data") or {})
        minimax_prompt_log_path = ""
        try:
            _emit_progress(progress, f"Calling background comparison AI model: {self.config.minimax_model}")
            minimax_prompt_log_path = str(write_prompt_log(
                result.request,
                prompt,
                self.config.minimax_model,
                False,
                sources,
                {**(structured_data.get("prompt_policy") or {}), "purpose": "comparison_report", "primary_model": self.config.model, "background": True},
            ))
            _emit_progress(progress, f"MiniMax comparison prompt saved: {minimax_prompt_log_path}")
            minimax_result = self.minimax.generate_report(prompt)
            comparison_summary = summarize_for_telegram(minimax_result.markdown)
            comparison_data = {**structured_data, "analysis_model": self.config.minimax_model, "minimax_diagnostics": minimax_result.diagnostics}
            comparison_data.pop("comparison_reports", None)
            comparison_artifacts, _comparison_json = write_report_artifacts(
                self.config.report_root,
                result.request,
                minimax_result.markdown,
                comparison_summary,
                sources,
                True,
                None,
                comparison_data,
                report_variant="minimax",
            )
            entry = {
                "model": self.config.minimax_model,
                "status": "success",
                "markdown_path": str(comparison_artifacts.markdown_path),
                "html_path": str(comparison_artifacts.html_path),
                "json_path": str(comparison_artifacts.json_path),
                "sources_path": str(comparison_artifacts.sources_path),
                "prompt_path": minimax_prompt_log_path,
                "diagnostics": minimax_result.diagnostics,
            }
            self._update_comparison_metadata(result, entry)
            _emit_progress(progress, f"MiniMax comparison report completed: {comparison_artifacts.report_id}")
            return entry
        except Exception as minimax_exc:
            entry = {"model": self.config.minimax_model, "status": "failed", "error": str(minimax_exc), "prompt_path": minimax_prompt_log_path}
            self._update_comparison_metadata(result, entry)
            _emit_progress(progress, f"MiniMax comparison report failed: {minimax_exc}")
            return entry

    def _update_comparison_metadata(self, result: ResearchCenterResult, entry: dict[str, Any]) -> None:
        result.report_json.setdefault("metadata", {})["comparison_reports"] = [entry]
        json_path = result.artifacts.json_path
        if json_path.exists() and json_path.is_file():
            data = json.loads(json_path.read_text(encoding="utf-8"))
            data.setdefault("metadata", {})["comparison_reports"] = [entry]
            json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _run_report_lookup(self, request: CommandRequest) -> ResearchCenterResult:
        query = request.target or "__recent__"
        if query == "__recent__":
            return self._run_recent_report_list(request)

        report_type, target, report_date = _parse_report_query(query)
        row = self.database.latest_report(target=target, report_type=report_type, report_date=report_date)
        if row is None:
            raise StockExportError("查無指定報告，可使用 /research、/macro、/theme 或 /value_scan 重新產生。")

        markdown_path = Path(row["markdown_path"])
        markdown = markdown_path.read_text(encoding="utf-8") if markdown_path.exists() else row["summary"]
        json_path = Path(row["json_path"])
        report_json: dict[str, Any] = json.loads(json_path.read_text(encoding="utf-8")) if json_path.exists() else dict(row)

        sources_path = Path(row["sources_path"])
        sources: list[SourceItem] = []
        if sources_path.exists():
            for raw in json.loads(sources_path.read_text(encoding="utf-8")):
                sources.append(SourceItem(**raw))

        artifacts = _artifacts_from_row(row)
        ai_used = bool(row.get("ai_used"))
        metadata = (report_json or {}).get("metadata") or {}
        ai_model = (
            metadata.get("analysis_model")
            or metadata.get("analysis_model_choice")
            or row.get("model")
            or (self.config.model if ai_used else None)
        )
        return ResearchCenterResult(
            status="success",
            request=request,
            summary=row["summary"],
            markdown=markdown,
            report_json=report_json,
            sources=sources,
            artifacts=artifacts,
            ai_used=ai_used,
            ai_model=ai_model,
            fallback_reason=row.get("fallback_reason"),
        )

    def _run_recent_report_list(self, request: CommandRequest) -> ResearchCenterResult:
        labels = {
            "research": "個股研究",
            "macro": "宏觀市場",
            "theme": "題材研究",
            "value_scan": "價值重估掃描",
        }
        lines = ["# 最近產生的 AI 投研報告", ""]
        for report_type, label in labels.items():
            rows = self.database.recent_reports(report_type=report_type, limit=5)
            if not rows:
                continue
            lines.append(f"## {label}")
            for index, row in enumerate(rows, 1):
                target = row.get("target") or "latest"
                lines.append(f"{index}. {target}｜{row.get('report_date')}｜{row.get('mode')}｜{row.get('created_at')}")
            lines.append("")
        if len(lines) <= 2:
            lines.append("目前沒有任何 AI 投研歷史報告。")
        markdown = "\n".join(lines).strip()
        artifacts = ReportArtifacts("recent_reports", "report", Path("__no_markdown_file__"), Path("__no_html_file__"), Path("__no_json_file__"), Path("__no_sources_file__"))
        return ResearchCenterResult(
            status="success",
            request=request,
            summary=markdown,
            markdown=markdown,
            report_json={"report_type": "report", "summary": markdown},
            sources=[],
            artifacts=artifacts,
            ai_used=False,
            ai_model=None,
            fallback_reason=None,
        )


def _model_job_entry(
    model_key: str,
    model: str,
    status: str,
    artifacts: ReportArtifacts,
    prompt_path: str,
    summary: str,
    report_json: dict[str, Any],
    diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "model_key": model_key,
        "model": model,
        "status": status,
        "markdown_path": str(artifacts.markdown_path),
        "html_path": str(artifacts.html_path),
        "json_path": str(artifacts.json_path),
        "sources_path": str(artifacts.sources_path),
        "prompt_path": prompt_path,
        "summary": summary,
        "report_id": artifacts.report_id,
        "diagnostics": diagnostics or {},
        "report_json": report_json,
    }

def _with_output_formats(request: CommandRequest, formats: tuple[str, ...]) -> CommandRequest:
    output_formats = request.output_formats if request.output_formats != ("md", "html", "json") else formats
    return CommandRequest(
        command=request.command,
        raw_text=request.raw_text,
        target=request.target,
        target_type=request.target_type,
        market_scope=request.market_scope,
        theme_scope=request.theme_scope,
        region_scope=request.region_scope,
        candidate_pool=request.candidate_pool,
        mode=request.mode,
        source_only=request.source_only,
        score=request.score,
        brief=request.brief,
        top=request.top,
        report_date=request.report_date,
        output_formats=output_formats,
        user_id=request.user_id,
        created_at=request.created_at,
        ai_model=request.ai_model,
    )


def _merge_sources(base: list[SourceItem], extra: list[SourceItem]) -> list[SourceItem]:
    merged_dict: dict[str, SourceItem] = {}
    for item in base:
        merged_dict[item.url] = item
    for item in extra:
        existing = merged_dict.get(item.url)
        if existing is None:
            merged_dict[item.url] = item
        else:
            existing_priority = PROVIDER_PRIORITY.get(existing.provider, 0)
            new_priority = PROVIDER_PRIORITY.get(item.provider, 0)
            if new_priority > existing_priority:
                merged_dict[item.url] = item
            else:
                # Merge fields when same priority or new priority lower
                existing_found_by = list(existing.found_by) if existing.found_by else []
                item_found_by = list(item.found_by) if item.found_by else []
                combined_found_by = list(set(existing_found_by + item_found_by))
                merged_dict[item.url] = SourceItem(
                    source_id=existing.source_id,
                    title=existing.title,
                    url=existing.url,
                    source_level=existing.source_level,
                    published_date=existing.published_date or item.published_date,
                    snippet=existing.snippet or item.snippet,
                    used_in_section=list(set(existing.used_in_section + item.used_in_section)),
                    provider=existing.provider,
                    provider_detail=existing.provider_detail,
                    fetch_provider=item.fetch_provider or existing.fetch_provider,
                    fetch_status=item.fetch_status or existing.fetch_status,
                    failure_reason=item.failure_reason or existing.failure_reason,
                    found_by=combined_found_by,
                )
    merged: list[SourceItem] = []
    for url, item in merged_dict.items():
        merged.append(SourceItem(
            f"S{len(merged) + 1:03d}", item.title, item.url, item.source_level,
            item.published_date, item.snippet, item.used_in_section,
            item.provider, item.provider_detail,
            item.fetch_provider, item.fetch_status, item.failure_reason, item.found_by,
        ))
    return merged


def _artifacts_from_row(row: dict[str, Any]) -> ReportArtifacts:
    return ReportArtifacts(row["report_id"], row["report_type"], Path(row["markdown_path"]), Path(row["html_path"]), Path(row["json_path"]), Path(row["sources_path"]))


def _parse_report_query(query: str) -> tuple[str | None, str | None, str | None]:
    parts = query.split()
    report_date = None
    if parts and _looks_like_date(parts[-1]):
        report_date = parts.pop()
    if not parts:
        return None, None, report_date
    if len(parts) == 1 and parts[0] == "latest":
        return None, None, report_date
    if parts[0] == "macro":
        return "macro", None, report_date
    if parts[0] == "value_scan":
        return "value_scan", None, report_date
    if parts[0] == "theme":
        target = " ".join(parts[1:]).strip() or None
        return "theme", target, report_date
    return "research", " ".join(parts), report_date


def _looks_like_date(value: str) -> bool:
    import re

    return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", value))


def _emit_progress(progress: ProgressCallback | None, message: str) -> None:
    if progress is not None:
        progress(message)


PROVIDER_PRIORITY = {
    "official_connector": 100,
    "tavily_extract": 90,
    "requests_bs4": 75,
    "gemini_grounding": 80,
    "html_fetch": 70,
    "minimax_mcp_search": 65,
    "tavily_search": 60,
    "forum_direct": 50,
    "forum_search": 40,
    None: 0,
}


class _GeminiDiscoveryRunner:
    def __init__(self, center: ResearchCenter):
        self._center = center

    def run_discovery_flow(self, request: CommandRequest, sources: list[SourceItem], structured_data: dict[str, Any], use_grounding: bool, progress: ProgressCallback | None) -> tuple[list[SourceItem], bool]:
        discovery_tasks = build_grounding_discovery_prompts(request, structured_data=structured_data, source_list=sources) if request.report_date is None else []
        discovery_sources: list[SourceItem] = []
        discovery_runs: list[dict[str, Any]] = []
        gemini_search_used = False

        if request.report_date is None and discovery_tasks:
            # Step 1: MiniMax MCP Search (highest priority)
            self._run_minimax_mcp(request, discovery_tasks, sources, structured_data, progress)
            # Step 2: Tavily Search (second priority)
            self._run_tavily(request, discovery_tasks, sources, structured_data, progress)
            # Step 3: Gemini fallback if needed
            should_run = self._should_run_gemini(request, sources)
            if should_run and use_grounding:
                self._run_gemini(request, discovery_tasks, sources, structured_data, discovery_sources, discovery_runs, progress)
                gemini_search_used = True
            else:
                structured_data["gemini_search_discovery"] = {
                    "enabled": False,
                    "reason": "skipped_enough_non_gemini_sources" if not should_run else "gemini_search_mode_off",
                    "source_quality": _source_quality_summary(sources, request),
                }
                if progress:
                    _emit_progress(progress, "Gemini Search skipped: sources sufficient or mode not enabled")

        return sources, gemini_search_used

    def _run_minimax_mcp(self, request, discovery_tasks, sources, structured_data, progress):
        if not self._center.config.enable_minimax_search:
            structured_data["minimax_search_discovery"] = {"enabled": False, "reason": "disabled_by_config"}
            _emit_progress(progress, "MiniMax MCP Search skipped: disabled by config")
            return
        if not self._center.minimax_search.is_configured():
            structured_data["minimax_search_discovery"] = {"enabled": False, "reason": "not_configured"}
            _emit_progress(progress, "MiniMax MCP Search skipped: not configured")
            return
        try:
            _emit_progress(progress, f"MiniMax MCP Search: {len(discovery_tasks)} tasks")
            minimax_result = self._center.minimax_search.discover(request, discovery_tasks, progress=progress)
            before = len(sources)
            merged = _merge_sources(sources, minimax_result.sources)
            sources.clear()
            sources.extend(merged)
            added = len(sources) - before
            structured_data["minimax_search_discovery"] = minimax_result.diagnostics
            _emit_progress(progress, f"MiniMax MCP Search completed: {len(minimax_result.sources)} sources, added={added}")
        except Exception as exc:
            structured_data["minimax_search_discovery"] = {"enabled": False, "reason": "failed", "error": str(exc)}
            _emit_progress(progress, f"MiniMax MCP Search failed: {exc}")

    def _run_tavily(self, request, discovery_tasks, sources, structured_data, progress):
        if not self._center.config.enable_tavily_search:
            structured_data["tavily_search_discovery"] = {"enabled": False, "reason": "disabled_by_config"}
            _emit_progress(progress, "Tavily Search skipped: disabled by config")
            return
        if not self._center.quota_guard.is_available("tavily"):
            structured_data["tavily_search_discovery"] = {"enabled": False, "reason": "quota_exhausted_this_month"}
            _emit_progress(progress, "Tavily Search skipped: quota exhausted this month")
            return
        if not self._center.quota_guard.is_under_monthly_limit("tavily", self._center.config.tavily_monthly_credit_limit, self._center.config.tavily_credit_reserve):
            structured_data["tavily_search_discovery"] = {"enabled": False, "reason": "monthly_credit_reserve_reached"}
            _emit_progress(progress, "Tavily Search skipped: monthly credit reserve reached")
            return
        try:
            _emit_progress(progress, f"Run Tavily Search discovery: {len(discovery_tasks)} tasks")
            tavily_result = self._center.tavily_search.discover(request, discovery_tasks, progress=progress)
            before = len(sources)
            merged = _merge_sources(sources, tavily_result.sources)
            sources.clear()
            sources.extend(merged)
            added = len(sources) - before
            structured_data["tavily_search_discovery"] = tavily_result.diagnostics
            estimated_units = int((tavily_result.diagnostics or {}).get("estimated_credits") or 1)
            self._center.quota_guard.record_usage("tavily", estimated_units)
            _emit_progress(progress, f"Tavily Search completed: {len(tavily_result.sources)} sources, added={added}")
        except TavilyQuotaError as exc:
            self._center.quota_guard.mark_exhausted("tavily", str(exc))
            structured_data["tavily_search_discovery"] = {"enabled": False, "reason": "quota_exhausted", "error": str(exc)}
            _emit_progress(progress, f"Tavily quota exhausted; disabled until next month: {exc}")
        except Exception as exc:
            structured_data["tavily_search_discovery"] = {"enabled": False, "reason": "failed", "error": str(exc)}
            _emit_progress(progress, f"Tavily Search failed: {exc}")

    def _should_run_gemini(self, request, sources):
        mode = self._center.config.gemini_search_mode
        if mode == "always":
            return True
        if mode == "off":
            return False
        return _should_run_gemini_search_fallback(request, sources, self._center.config)

    def _run_gemini(self, request, discovery_tasks, sources, structured_data, discovery_sources, discovery_runs, progress):
        _emit_progress(progress, f"Run Gemini Search discovery (fallback): {len(discovery_tasks)} compact prompts")
        for task_index, task in enumerate(discovery_tasks, 1):
            label = task.get("label") or f"task_{task_index}"
            discovery_prompt = task.get("prompt") or ""
            try:
                _emit_progress(progress, f"Gemini Search discovery {task_index}/{len(discovery_tasks)} [{label}] start")
                discovery_log_path = write_prompt_log(
                    request, discovery_prompt, self._center.config.model, True, sources,
                    {**(structured_data.get("prompt_policy") or {}), "purpose": "grounding_discovery", "discovery_label": label, "discovery_index": task_index},
                )
                _emit_progress(progress, f"Search discovery prompt saved: {discovery_log_path}")
                discovery_result = self._center.gemini.generate_report(discovery_prompt, enable_grounding=True)
                task_sources = discovery_result.sources
                before = len(sources)
                merged = _merge_sources(sources, task_sources)
                sources.clear()
                sources.extend(merged)
                added = len(sources) - before
                merged_discovery = _merge_sources(discovery_sources, task_sources)
                discovery_sources.clear()
                discovery_sources.extend(merged_discovery)
                discovery_runs.append({
                    "label": label, "prompt_path": str(discovery_log_path), "diagnostics": discovery_result.diagnostics,
                    "source_count": len(task_sources), "added_source_count": added, "markdown": discovery_result.markdown,
                })
                _emit_progress(progress, f"Gemini Search discovery {task_index}/{len(discovery_tasks)} [{label}] diagnostics: metadata={discovery_result.diagnostics.get('grounding_metadata_present')}, queries={discovery_result.diagnostics.get('web_search_query_count')}, chunks={discovery_result.diagnostics.get('grounding_chunk_count')}, sources={len(task_sources)}, added={added}")
            except Exception as exc:
                discovery_runs.append({"label": label, "status": "failed", "error": str(exc), "source_count": 0, "added_source_count": 0})
                _emit_progress(progress, f"Gemini Search discovery {task_index}/{len(discovery_tasks)} [{label}] failed: {exc}")
        structured_data["gemini_search_discovery"] = {
            "mode": "multi_stage", "task_count": len(discovery_tasks),
            "source_count": len(discovery_sources), "runs": discovery_runs,
        }
        if discovery_sources:
            _emit_progress(progress, f"Gemini Search discovery completed: {len(discovery_sources)} unique Google sources merged")
        else:
            _emit_progress(progress, "Gemini Search discovery returned no parseable citations")


def _fallback_threshold_key(request: CommandRequest) -> str:
    if request.command == "research":
        return f"research_{request.mode}"
    if request.command == "macro":
        return f"macro_{request.mode if request.mode == 'deep' else 'normal'}"
    if request.command == "theme":
        return f"theme_{request.mode if request.mode == 'deep' else 'normal'}"
    if request.command == "value_scan":
        return f"value_scan_{request.mode if request.mode == 'deep' else 'normal'}"
    return "default"


def _source_quality_summary(sources: list[SourceItem], request: CommandRequest) -> dict[str, Any]:
    total = len(sources)
    level1 = sum(1 for s in sources if s.source_level == "Level 1")
    level23 = sum(1 for s in sources if s.source_level in {"Level 2", "Level 3"})
    risk_terms = ("風險", "反證", "衰退", "下滑", "庫存", "毛利", "虧損", "制裁", "關稅", "戰爭", "risk", "decline", "inventory")
    risk = sum(1 for s in sources if any(term.lower() in ((s.title or "") + " " + (s.snippet or "")).lower() for term in risk_terms))
    by_provider: dict[str, int] = {}
    for s in sources:
        provider = s.provider or "unknown"
        by_provider[provider] = by_provider.get(provider, 0) + 1
    return {"total": total, "level1": level1, "level2_or_3": level23, "risk_or_contradiction": risk, "by_provider": by_provider}


def _gemini_discovery_source_count(structured_data: dict[str, Any]) -> int:
    discovery = structured_data.get("gemini_search_discovery") or {}
    if not discovery.get("enabled", True) and discovery.get("reason"):
        return 0
    return int(discovery.get("source_count") or 0)


def _should_run_gemini_search_fallback(request: CommandRequest, sources: list[SourceItem], config) -> bool:
    thresholds = config.gemini_fallback_thresholds.get(_fallback_threshold_key(request), {})
    summary = _source_quality_summary(sources, request)
    if summary["total"] < thresholds.get("min_total_sources", 10):
        return True
    if summary["level1"] < thresholds.get("min_level1_sources", 0):
        return True
    if summary["level2_or_3"] < thresholds.get("min_level2_or_3_sources", 0):
        return True
    if summary["risk_or_contradiction"] < thresholds.get("min_risk_or_contradiction_sources", 0):
        return True
    return False


def _enrich_sources_with_web_fetch(
    request: CommandRequest,
    sources: list[SourceItem],
    structured_data: dict[str, Any],
    progress: ProgressCallback | None = None,
) -> None:
    """Best-effort page-content enrichment; failures must never block AI analysis."""
    if request.report_date is not None or not sources:
        return

    max_urls = 8 if request.mode == "deep" else 4
    selected: list[SourceItem] = []
    seen: set[str] = set()
    for source in sources:
        url = (source.url or "").strip()
        if not url or url in seen:
            continue
        lower = url.lower()
        if not lower.startswith(("http://", "https://")):
            continue
        if any(lower.endswith(ext) for ext in (".pdf", ".xls", ".xlsx", ".csv", ".zip")):
            continue
        selected.append(source)
        seen.add(url)
        if len(selected) >= max_urls:
            break

    if not selected:
        structured_data["web_fetch_diagnostics"] = {
            "enabled": True,
            "status": "skipped",
            "reason": "no_fetchable_urls",
            "total_urls": 0,
        }
        return

    try:
        if progress:
            progress(f"WebFetch：開始讀取來源正文 {len(selected)} 筆")
        service = WebFetchService(timeout=12.0, max_workers=3)
        result = service.fetch_many([item.url for item in selected], progress=progress)
        by_url = {item.url: item for item in result.results}
        enriched_sources: list[SourceItem] = []
        enriched_count = 0
        for source in sources:
            fetched = by_url.get(source.url)
            if not fetched:
                enriched_sources.append(source)
                continue
            if fetched.content:
                enriched_count += 1
                snippet = fetched.content[:2000]
            else:
                snippet = source.snippet
            enriched_sources.append(
                replace(
                    source,
                    title=fetched.title or source.title,
                    snippet=snippet,
                    fetch_provider=fetched.fetch_provider,
                    fetch_status=fetched.content_status,
                    failure_reason=fetched.failure_reason,
                )
            )
        sources[:] = enriched_sources
        structured_data["web_fetch_diagnostics"] = {
            **result.diagnostics,
            "enabled": True,
            "status": "completed",
            "selected_url_count": len(selected),
            "enriched_source_count": enriched_count,
        }
        structured_data["web_fetched_sources"] = [
            {
                "url": item.url,
                "title": item.title,
                "content_status": item.content_status,
                "fetch_provider": item.fetch_provider,
                "failure_reason": item.failure_reason,
                "content_preview": item.content[:1200],
            }
            for item in result.results
        ]
        if progress:
            progress(f"WebFetch：完成，成功補正文 {enriched_count}/{len(selected)} 筆")
    except Exception as exc:
        structured_data["web_fetch_diagnostics"] = {
            "enabled": True,
            "status": "failed",
            "error": str(exc),
            "selected_url_count": len(selected),
        }
        if progress:
            progress(f"WebFetch：失敗但不中斷 AI 分析：{exc}")















