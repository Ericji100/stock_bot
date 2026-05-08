from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from data_fetcher import StockExportError

from .command_parser import parse_command_text
from .config import ResearchCenterConfig, load_research_config
from .data_services import collect_structured_data
from .database import ResearchDatabase
from .date_guard import filter_sources_for_report_date
from .event_store import build_source_events, extract_structured_events, historical_policy
from .gemini_service import GeminiService, build_prompt
from .minimax_service import MiniMaxService
from .minimax_search_service import MiniMaxSearchService
from .knowledge_drafts import write_knowledge_draft
from .models import CommandRequest, ReportArtifacts, ResearchCenterResult, SourceItem
from .prompt_registry import build_grounding_discovery_prompts, prompt_metadata
from .prompt_logging import write_prompt_log
from .source_snapshots import build_source_snapshots, snapshots_to_structured_context, target_for_snapshots
from .report_builder import fallback_markdown, summarize_for_telegram, write_report_artifacts
from .research_logger import log_error, log_task
from .scoring_engine import build_buy_rating, build_local_scores

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
        structured_data["local_scoring"] = {
            "policy": "本地評分依可驗證資料保守計算；CAGR、護城河、轉型效益與題材熱度若缺來源不得高分。",
            "scores": scores,
            "buy_rating": build_buy_rating(scores) if request.command == "research" and request.mode in {"score", "deep"} else None,
        }
        if scores:
            _emit_progress(progress, f"Local scoring completed: {len(scores)} items")
        else:
            _emit_progress(progress, "Local full scoring skipped for this mode; AI will organize and analyze the collected data")

        _emit_progress(progress, "Build shared model prompt for parallel AI reports")
        use_grounding = self.config.enable_grounding and request.report_date is None
        discovery_tasks = build_grounding_discovery_prompts(request, structured_data=structured_data, source_list=sources) if request.report_date is None else []
        discovery_sources: list[SourceItem] = []
        discovery_runs: list[dict[str, Any]] = []
        if use_grounding:
            _emit_progress(progress, f"Run multi-stage Gemini Search discovery: {len(discovery_tasks)} compact prompts")
            for task_index, task in enumerate(discovery_tasks, 1):
                label = task.get("label") or f"task_{task_index}"
                discovery_prompt = task.get("prompt") or ""
                try:
                    _emit_progress(progress, f"Gemini Search discovery {task_index}/{len(discovery_tasks)} [{label}] start")
                    discovery_log_path = write_prompt_log(
                        request,
                        discovery_prompt,
                        self.config.model,
                        True,
                        sources,
                        {**(structured_data.get("prompt_policy") or {}), "purpose": "grounding_discovery", "discovery_label": label, "discovery_index": task_index},
                    )
                    _emit_progress(progress, f"Search discovery prompt saved: {discovery_log_path}")
                    discovery_result = self.gemini.generate_report(discovery_prompt, enable_grounding=True)
                    task_sources = discovery_result.sources
                    before_count = len(sources)
                    sources = _merge_sources(sources, task_sources)
                    added_count = len(sources) - before_count
                    discovery_sources = _merge_sources(discovery_sources, task_sources)
                    discovery_runs.append({
                        "label": label,
                        "prompt_path": str(discovery_log_path),
                        "diagnostics": discovery_result.diagnostics,
                        "source_count": len(task_sources),
                        "added_source_count": added_count,
                        "markdown": discovery_result.markdown,
                    })
                    _emit_progress(progress, f"Gemini Search discovery {task_index}/{len(discovery_tasks)} [{label}] diagnostics: metadata={discovery_result.diagnostics.get('grounding_metadata_present')}, queries={discovery_result.diagnostics.get('web_search_query_count')}, chunks={discovery_result.diagnostics.get('grounding_chunk_count')}, sources={len(task_sources)}, added={added_count}")
                except Exception as discovery_exc:
                    discovery_runs.append({"label": label, "status": "failed", "error": str(discovery_exc), "source_count": 0, "added_source_count": 0})
                    _emit_progress(progress, f"Gemini Search discovery {task_index}/{len(discovery_tasks)} [{label}] failed: {discovery_exc}")
            structured_data["gemini_search_discovery"] = {
                "mode": "multi_stage",
                "task_count": len(discovery_tasks),
                "source_count": len(discovery_sources),
                "runs": discovery_runs,
            }
            if discovery_sources:
                _emit_progress(progress, f"Multi-stage Gemini Search discovery completed: {len(discovery_sources)} unique Google sources merged into final prompt")
            else:
                _emit_progress(progress, "Multi-stage Gemini Search discovery returned no parseable citations; model reports will still run")

        if self.config.enable_minimax_search and request.report_date is None and discovery_tasks:
            if self.minimax_search.is_configured():
                _emit_progress(progress, f"Run MiniMax Search discovery: {len(discovery_tasks)} compact Google search tasks")
                minimax_search_result = self.minimax_search.discover(request, discovery_tasks, progress=progress)
                before_count = len(sources)
                sources = _merge_sources(sources, minimax_search_result.sources)
                structured_data["minimax_search_discovery"] = minimax_search_result.diagnostics
                _emit_progress(progress, f"MiniMax Search completed: {len(minimax_search_result.sources)} sources, added={len(sources) - before_count}")
            else:
                structured_data["minimax_search_discovery"] = {"enabled": False, "reason": "SERPER_API_KEY not configured"}
                _emit_progress(progress, "MiniMax Search skipped: SERPER_API_KEY not configured")

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
                "use_grounding": use_grounding,
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
            minimax_prompt_log_path = str(write_prompt_log(request, prompt, self.config.minimax_model, False, sources, {**(structured_data.get("prompt_policy") or {}), "purpose": "parallel_model_report", "primary_model": self.config.model, "shared_prompt_path": shared_prompt_path, "model_key": "minimax"}))
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
        structured_data["analysis_model"] = self.config.model
        scores = build_local_scores(request, structured_data)
        structured_data["local_scoring"] = {
            "policy": "本地評分依可驗證資料保守計算；CAGR、護城河、轉型效益與題材熱度若缺來源不得高分。",
            "scores": scores,
            "buy_rating": build_buy_rating(scores) if request.command == "research" and request.mode in {"score", "deep"} else None,
        }
        if scores:
            _emit_progress(progress, f"Local scoring completed: {len(scores)} items")
        else:
            _emit_progress(progress, "Local full scoring skipped for this mode; AI will organize and analyze the collected data")

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
                _emit_progress(progress, f"Build Gemini prompt, model={self.config.model}")
                use_grounding = self.config.enable_grounding and request.report_date is None
                discovery_tasks = build_grounding_discovery_prompts(request, structured_data=structured_data, source_list=sources) if request.report_date is None else []
                discovery_sources: list[SourceItem] = []
                discovery_runs: list[dict[str, Any]] = []
                if use_grounding:
                    _emit_progress(progress, f"Run multi-stage Gemini Search discovery: {len(discovery_tasks)} compact prompts")
                    for task_index, task in enumerate(discovery_tasks, 1):
                        label = task.get("label") or f"task_{task_index}"
                        discovery_prompt = task.get("prompt") or ""
                        try:
                            _emit_progress(progress, f"Gemini Search discovery {task_index}/{len(discovery_tasks)} [{label}] start")
                            discovery_log_path = write_prompt_log(
                                request,
                                discovery_prompt,
                                self.config.model,
                                True,
                                sources,
                                {**(structured_data.get("prompt_policy") or {}), "purpose": "grounding_discovery", "discovery_label": label, "discovery_index": task_index},
                            )
                            _emit_progress(progress, f"Search discovery prompt saved: {discovery_log_path}")
                            discovery_result = self.gemini.generate_report(discovery_prompt, enable_grounding=True)
                            task_sources = discovery_result.sources
                            before_count = len(sources)
                            sources = _merge_sources(sources, task_sources)
                            added_count = len(sources) - before_count
                            discovery_sources = _merge_sources(discovery_sources, task_sources)
                            run_info = {
                                "label": label,
                                "prompt_path": str(discovery_log_path),
                                "diagnostics": discovery_result.diagnostics,
                                "source_count": len(task_sources),
                                "added_source_count": added_count,
                                "markdown": discovery_result.markdown,
                            }
                            discovery_runs.append(run_info)
                            _emit_progress(progress, f"Gemini Search discovery {task_index}/{len(discovery_tasks)} [{label}] diagnostics: metadata={discovery_result.diagnostics.get('grounding_metadata_present')}, queries={discovery_result.diagnostics.get('web_search_query_count')}, chunks={discovery_result.diagnostics.get('grounding_chunk_count')}, sources={len(task_sources)}, added={added_count}")
                        except Exception as discovery_exc:
                            discovery_runs.append({"label": label, "status": "failed", "error": str(discovery_exc), "source_count": 0, "added_source_count": 0})
                            _emit_progress(progress, f"Gemini Search discovery {task_index}/{len(discovery_tasks)} [{label}] failed: {discovery_exc}")
                    structured_data["gemini_search_discovery"] = {
                        "mode": "multi_stage",
                        "task_count": len(discovery_tasks),
                        "source_count": len(discovery_sources),
                        "runs": discovery_runs,
                    }
                    if discovery_sources:
                        _emit_progress(progress, f"Multi-stage Gemini Search discovery completed: {len(discovery_sources)} unique Google sources merged into final prompt")
                    else:
                        _emit_progress(progress, "Multi-stage Gemini Search discovery returned no parseable citations; final report grounding will still run")
                if self.config.enable_minimax_search and request.report_date is None and discovery_tasks:
                    if self.minimax_search.is_configured():
                        _emit_progress(progress, f"Run MiniMax Search discovery: {len(discovery_tasks)} compact Google search tasks")
                        minimax_search_result = self.minimax_search.discover(request, discovery_tasks, progress=progress)
                        before_count = len(sources)
                        sources = _merge_sources(sources, minimax_search_result.sources)
                        structured_data["minimax_search_discovery"] = minimax_search_result.diagnostics
                        _emit_progress(progress, f"MiniMax Search completed: {len(minimax_search_result.sources)} sources, added={len(sources) - before_count}")
                    else:
                        structured_data["minimax_search_discovery"] = {"enabled": False, "reason": "SERPER_API_KEY not configured"}
                        _emit_progress(progress, "MiniMax Search skipped: SERPER_API_KEY not configured")
                prompt = build_prompt(request, structured_data=structured_data, source_list=sources)
                prompt_log_path = write_prompt_log(request, prompt, self.config.model, use_grounding, sources, structured_data.get("prompt_policy"))
                _emit_progress(progress, f"Prompt saved: {prompt_log_path}")
                _emit_progress(progress, f"Prompt template={structured_data.get('prompt_policy', {}).get('template')}, length={len(prompt)} chars, grounding={use_grounding}, sources={len(sources)}")
                _emit_progress(progress, f"Calling AI model: {self.config.model}")
                gemini_result = self.gemini.generate_report(prompt, enable_grounding=use_grounding)
                markdown = gemini_result.markdown
                gemini_raw = gemini_result.raw
                structured_data["gemini_search_diagnostics"] = gemini_result.diagnostics
                actual_gemini_model = str(gemini_result.diagnostics.get("actual_model") or self.config.model)
                structured_data["analysis_model"] = actual_gemini_model
                if gemini_result.diagnostics.get("fallback_used"):
                    _emit_progress(progress, f"Gemini fallback used: {self.config.model} -> {actual_gemini_model}")
                _emit_progress(progress, f"Gemini Search diagnostics: metadata={gemini_result.diagnostics.get('grounding_metadata_present')}, queries={gemini_result.diagnostics.get('web_search_query_count')}, chunks={gemini_result.diagnostics.get('grounding_chunk_count')}, sources={len(gemini_result.sources)}")
                if gemini_result.sources:
                    _emit_progress(progress, f"Gemini grounding citations: {len(gemini_result.sources)} sources will be written")
                elif discovery_sources:
                    _emit_progress(progress, f"Final report returned no citations; keeping {len(discovery_sources)} Google sources from Search discovery")
                elif use_grounding:
                    _emit_progress(progress, "Gemini Search returned no parseable citations; report will keep diagnostics and local/existing sources")
                sources = _merge_sources(sources, gemini_result.sources)
                ai_used = True
                _emit_progress(progress, f"AI model completed: {actual_gemini_model or self.config.model}")
                if self.config.enable_minimax_comparison and self.minimax.is_configured():
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
        return ResearchCenterResult(
            status="success",
            request=request,
            summary=row["summary"],
            markdown=markdown,
            report_json=report_json,
            sources=sources,
            artifacts=artifacts,
            ai_used=ai_used,
            ai_model=self.config.model if ai_used else None,
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
    )


def _merge_sources(base: list[SourceItem], extra: list[SourceItem]) -> list[SourceItem]:
    merged: list[SourceItem] = []
    seen: set[str] = set()
    for item in [*base, *extra]:
        if item.url in seen:
            continue
        seen.add(item.url)
        merged.append(SourceItem(f"S{len(merged) + 1:03d}", item.title, item.url, item.source_level, item.published_date, item.snippet, item.used_in_section))
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
















