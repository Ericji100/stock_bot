from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

import pandas as pd

from research_center.config import load_research_config
from trade_monitor.analysis_contract import (
    DISCLAIMER,
    analysis_state_summary,
    render_analysis_markdown,
    render_quiet_status_markdown,
)
from trade_monitor.constitution_state import apply_constitution_event, load_constitution_snapshot
from trade_monitor.market_structure_state import empty_market_structure_state
from trade_monitor.structured_market_data import load_structured_market_snapshot

from .config import PROJECT_ROOT, ReplayConfig
from .codex_analyzer import CodexReplayAnalyzer
from .contract import empty_replay_memory, validate_replay_envelope
from .data_source import (
    ReplayDataset,
    compact_bar_table,
    compact_completed_overview,
    load_replay_dataset,
)
from .full_fidelity import validate_full_fidelity_payload
from .execution_gate import (
    advance_program_risk_controls,
    build_event_lifecycle,
    derive_analysis_exit_audit,
    derive_entry_eligibility,
    derive_program_behavior_exit_audit,
    derive_position_behavior_audit,
    derive_protective_stop_audit,
    expire_stale_armed_setups,
    fill_pending_entry,
    fill_pending_exit,
    invalidate_preentry_stop_touched_setup,
    mark_events_analyzed,
    schedule_pending_entry,
    schedule_pending_exit,
)
from .deterministic_state import (
    DETERMINISTIC_TIMELINE_ENGINE_SHA256,
    DETERMINISTIC_TIMELINE_ENGINE_VERSION,
    apply_profit_milestone_protection,
    build_deterministic_timeline,
    build_evidence_ledger,
    evidence_events,
)
from .minimax_analyzer import (
    AI_HYBRID_EXECUTION_VERSION,
    AI_HYBRID_LONG_ONLY_EXECUTION_VERSION,
    AI_HYBRID_LONG_ONLY_Q2_Q4_EXECUTION_VERSION,
    ANCHOR_LIFECYCLE_EXECUTION_VERSION,
    COURSE_CHAIN_EXECUTION_VERSION,
    DETERMINISTIC_EXECUTION_VERSION,
    ENTRY_GATE_EXECUTION_VERSION,
    FULL_FIDELITY_EXECUTION_VERSION,
    REPLAY_EXECUTION_VERSION,
    MiniMaxReplayAnalyzer,
    MiniMaxReplayError,
    MiniMaxReplayResult,
    build_replay_prompt,
    normalize_tool_arguments,
    parse_json_object,
)
from .notifier import ReplayNotifier, load_bot_token, replay_event_id
from .presentation import (
    render_programmatic_entry_fill,
    render_programmatic_exit_signal,
    render_programmatic_exit_fill,
    render_programmatic_stop_fill,
    render_replay_event_card,
    render_semantic_replay_event_card,
)
from .program_analyzer import build_program_semantic_envelope
from .program_constitution import (
    apply_program_execution_event,
    apply_program_requalification,
    requalification_audit,
    retire_setups_for_constitution_lock,
    suppress_candidate_for_constitution_lock,
)
from .rules import ReplayRulePackage, load_rule_package
from .semantic_disagreement import build_semantic_comparison
from .semantic_contract import (
    _is_countertrend_to_highest_active_anchor,
    empty_semantic_memory,
    initial_position_state,
    preview_position_transition,
    validate_semantic_envelope,
)
from .state import (
    DEFAULT_RUNTIME_ROOT,
    ReplayStateError,
    append_jsonl,
    create_run_directory,
    read_json,
    resolve_run_directory,
    write_json_atomic,
)
from .twse_spot_source import spot_context


TAIPEI = ZoneInfo("Asia/Taipei")
SIMULATION_DISCLAIMER = "一般技術分析，非個人化投資建議。\n**這是歷史資料模擬，不是即時交易訊號。**"
EVENT_DRIVEN_EXACT_TYPES = {
    "DEFENSE_BROKEN",
    "WORKING_ANCHOR_ESTABLISHED",
    "WORKING_ANCHOR_CHANGED",
    "GRADE_UPGRADE",
    "GRADE_DOWNGRADE",
    "STRUCTURE_INVALIDATED",
    "FALSE_BREAK_RECLAIM",
    "BACKGROUND_ANCHOR_ESTABLISHED",
    "BACKGROUND_ANCHOR_REPLACED",
    "BACKGROUND_DEFENSE_BROKEN",
    "BACKGROUND_DEFENSE_RECLAIMED",
    "REVERSE_CANDIDATE_ESTABLISHED",
    "CHILD_ANCHOR_ESTABLISHED",
    "PROGRAM_SETUP_ARMED",
    "PROGRAM_SETUP_CHANGED",
    "PROGRAM_DEFENSE_AVAILABLE",
    "PROGRAM_QUADRANT_CHANGED",
    "PROGRAM_TAIJI_CHANGED",
    "PROGRAM_METHOD_CHANGED",
}
EVENT_DRIVEN_STATE_TYPES = {
    "BACKGROUND_QUADRANT_BASELINE_CHANGED",
    "WORKING_QUADRANT_BASELINE_CHANGED",
}
EVENT_DRIVEN_STATE_MINUTES = 10


def _execution_implementation_sha256() -> str:
    """Content-address every local module that can change replay decisions.

    Human-readable execution versions explain intent, but they are easy to
    forget during development.  This digest makes v34 runs fail closed when
    state transitions, semantic guards, prompt assembly, fills, or position
    management code changes without a matching new run.
    """

    package_root = Path(__file__).parent
    project_root = package_root.parent
    paths = [
        package_root / name
        for name in (
            "anchor_lifecycle.py",
            "codex_analyzer.py",
            "config.py",
            "data_source.py",
            "deterministic_state.py",
            "execution_gate.py",
            "minimax_analyzer.py",
            "presentation.py",
            "program_analyzer.py",
            "program_constitution.py",
            "rules.py",
            "runner.py",
            "semantic_contract.py",
        )
    ] + [
        project_root / "trade_monitor" / "pivot_replay.py",
        project_root / "trade_monitor" / "structured_market_data.py",
    ]
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda item: item.as_posix()):
        digest.update(path.relative_to(project_root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


class ReplayRunError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _substantive_retry_decision(
    payload: Any,
    *,
    position: Mapping[str, Any] | None,
) -> str | None:
    """Extract the trading choice that a validation retry must not rewrite.

    In the schema, holding an existing position and waiting while flat both use
    ``position_action=NONE``.  Preserve their distinct meanings by joining the
    raw action to the position that existed before the model call.  A malformed
    response without a recognizable action does not create a lock.
    """

    analysis = payload.get("analysis") if isinstance(payload, Mapping) else None
    action = analysis.get("action") if isinstance(analysis, Mapping) else None
    raw = str(action.get("position_action") or "").upper() if isinstance(action, Mapping) else ""
    if raw == "NONE":
        status = str((position or {}).get("status") or "FLAT").upper()
        return "HOLD" if status in {"LONG", "SHORT"} else "WAIT"
    return raw if raw in {"ENTER", "EXIT", "STOP"} else None


def _assert_retry_decision_unchanged(
    locked: str | None,
    current: str | None,
) -> None:
    """Fail closed instead of accepting a stochastic trade-decision rewrite."""

    if locked is None:
        return
    if current is None:
        raise ReplayRunError(
            "retry_decision_missing",
            f"AI重試遺失已鎖定的{locked}交易決定；本輪停止，不以格式修正改變交易。",
        )
    if current != locked:
        raise ReplayRunError(
            "retry_decision_changed",
            f"AI重試把交易決定由{locked}改為{current}；本輪停止，不接受重試改寫交易。",
        )


def _register_retry_decision(
    locked: str | None,
    payload: Any,
    *,
    position: Mapping[str, Any] | None,
) -> tuple[str | None, str | None]:
    """Observe one raw attempt and return the persistent lock plus its decision."""

    current = _substantive_retry_decision(payload, position=position)
    if locked is None:
        return current, current
    _assert_retry_decision_unchanged(locked, current)
    return locked, current


def _retry_decision_instruction(locked: str | None) -> str:
    if locked is None:
        return ""
    position_action = "NONE" if locked in {"HOLD", "WAIT"} else locked
    return (
        f" 前次可辨識的實質交易決定已鎖定為{locked}"
        f"（action.position_action={position_action}）；只能修正無效欄位，不得改變該決定。"
    )


class ReplayRunner:
    def __init__(
        self,
        config: ReplayConfig,
        *,
        analyzer: MiniMaxReplayAnalyzer | Any | None = None,
        dataset_loader: Callable[..., ReplayDataset] = load_replay_dataset,
        runtime_root: Path = DEFAULT_RUNTIME_ROOT,
        step_callback: Callable[[dict[str, Any]], bool] | None = None,
    ) -> None:
        self.config = config
        self.rules = load_rule_package(config.rule_manifest_path)
        if (self.rules.contract_version, self.rules.memory_version) not in {(1, 1), (2, 2), (3, 3), (4, 3), (5, 3), (6, 3), (8, 6)}:
            raise ReplayRunError(
                "rule_contract_mismatch",
                "回放只允許舊精簡contract v1、程式證據contract v2至v6或正式contract v8/state v6。",
            )
        self.full_fidelity = self.rules.contract_version == 8
        self.deterministic_contract = self.rules.contract_version in {2, 3, 4, 5, 6}
        self.execution_gate_contract = self.rules.contract_version in {4, 5, 6}
        self.anchor_lifecycle_contract = self.rules.contract_version in {5, 6}
        self.course_chain_contract = self.rules.contract_version == 6
        self.ai_hybrid = self.rules.analysis_mode == "AI_HYBRID"
        self.long_only = self.rules.trade_direction_policy == "LONG_ONLY"
        if self.ai_hybrid and not self.course_chain_contract:
            raise ReplayRunError(
                "ai_hybrid_contract_required",
                "AI_HYBRID必須使用完整課程狀態contract v6。",
            )
        self.execution_version = (
            FULL_FIDELITY_EXECUTION_VERSION
            if self.full_fidelity
            else AI_HYBRID_LONG_ONLY_Q2_Q4_EXECUTION_VERSION
            if self.ai_hybrid and self.rules.trade_setup_policy == "LONG_Q2_Q4_ONLY"
            else AI_HYBRID_LONG_ONLY_EXECUTION_VERSION
            if self.ai_hybrid and self.long_only
            else AI_HYBRID_EXECUTION_VERSION
            if self.ai_hybrid
            else COURSE_CHAIN_EXECUTION_VERSION
            if self.course_chain_contract
            else ANCHOR_LIFECYCLE_EXECUTION_VERSION
            if self.anchor_lifecycle_contract
            else ENTRY_GATE_EXECUTION_VERSION
            if self.execution_gate_contract
            else DETERMINISTIC_EXECUTION_VERSION
            if self.deterministic_contract
            else REPLAY_EXECUTION_VERSION
        )
        static_execution_prompt = build_replay_prompt(
            rules_text=self.rules.model_rules,
            schema_text=self.rules.schema_text,
            runtime_context={},
            full_fidelity=self.full_fidelity,
            deterministic_contract=self.deterministic_contract,
            course_chain_contract=self.course_chain_contract,
            ai_hybrid=self.ai_hybrid,
            trade_direction_policy=self.rules.trade_direction_policy,
            trade_setup_policy=self.rules.trade_setup_policy,
        )
        self.execution_prompt_sha256 = hashlib.sha256(static_execution_prompt.encode("utf-8")).hexdigest()
        self.execution_implementation_sha256 = _execution_implementation_sha256()
        self._analyzer = analyzer
        self._dataset_loader = dataset_loader
        self._runtime_root = runtime_root
        self._step_callback = step_callback or _interactive_step
        self._revalidate_raw_once: Path | None = None
        self._reuse_saved_raw = False

    def _cached_course_timeline(
        self,
        dataset: ReplayDataset,
        *,
        target_date: date,
        decision_authority: str | None = None,
    ) -> list[dict[str, Any]]:
        """Reuse an immutable causal timeline only for identical inputs/code."""

        timeline_authority = decision_authority or (
            "AI_HYBRID" if self.ai_hybrid else "PROGRAM"
        )

        identity = {
            "version": 2,
            "deterministic_timeline_engine_version": DETERMINISTIC_TIMELINE_ENGINE_VERSION,
            "deterministic_timeline_engine_sha256": DETERMINISTIC_TIMELINE_ENGINE_SHA256,
            "target_date": target_date.isoformat(),
            "instrument": dataset.instrument,
            "source_sha256": dataset.source_sha256,
            "rule_sha256": self.rules.prompt_sha256,
            "model_rules_sha256": self.rules.model_rules_sha256,
            "schema_sha256": self.rules.schema_sha256,
            "execution_version": self.execution_version,
            "execution_prompt_sha256": self.execution_prompt_sha256,
            "execution_implementation_sha256": self.execution_implementation_sha256,
            "decision_authority": self.rules.decision_authority,
            "timeline_decision_authority": timeline_authority,
            "trade_direction_policy": self.rules.trade_direction_policy,
            "trade_setup_policy": self.rules.trade_setup_policy,
        }
        cache_key = hashlib.sha256(
            json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        cache_path = self._runtime_root / "timeline-cache" / f"{cache_key}.json"
        cached = read_json(cache_path)
        if (
            isinstance(cached, Mapping)
            and cached.get("identity") == identity
            and isinstance(cached.get("event_times"), list)
        ):
            return [dict(item) for item in cached["event_times"] if isinstance(item, Mapping)]
        timeline = build_deterministic_timeline(
            _visible_bar_records(dataset.day_bars),
            session_key=f"{target_date.isoformat()}:DAY",
            anchor_lifecycle_enabled=self.anchor_lifecycle_contract,
            course_chain_enabled=self.course_chain_contract,
            decision_authority=timeline_authority,
            trade_direction_policy=self.rules.trade_direction_policy,
            trade_setup_policy=self.rules.trade_setup_policy,
            initial_bars=_visible_bar_records(dataset.night_bars),
        )
        write_json_atomic(
            cache_path,
            {"version": 1, "identity": identity, "event_times": timeline},
        )
        return timeline

    def data_plan(
        self,
        *,
        target_date: date,
        event_driven: bool = False,
        start_time: time | None = None,
        end_time: time | None = None,
        analysis_times: list[time] | None = None,
        max_bars: int | None = None,
        analysis_every_bars: int | None = None,
        program_only: bool = False,
    ) -> dict[str, Any]:
        dataset = self._dataset_loader(target_date, instrument=self.config.instrument)
        if event_driven and not self.deterministic_contract:
            raise ReplayRunError("event_driven_contract_required", "事件驅動回放只支援程式證據contract。")
        if event_driven and (analysis_times or analysis_every_bars is not None):
            raise ReplayRunError("event_driven_conflict", "--event-driven不能與--at或--analysis-every並用。")
        timeline: list[dict[str, Any]] = []
        cadence = 1 if analysis_times or event_driven else analysis_every_bars or self.config.analysis_every_bars
        if event_driven:
            selected, timeline = _select_event_driven_bars(
                dataset.day_bars,
                session_key=f"{target_date.isoformat()}:DAY",
                start_time=start_time,
                end_time=end_time,
                max_bars=max_bars,
                anchor_lifecycle_enabled=self.anchor_lifecycle_contract,
                course_chain_enabled=self.course_chain_contract,
                decision_authority=(
                    "AI_HYBRID" if self.ai_hybrid and not program_only else "PROGRAM"
                ),
                trade_direction_policy=self.rules.trade_direction_policy,
                trade_setup_policy=self.rules.trade_setup_policy,
                history_frame=dataset.night_bars,
            )
            estimated_day_calls = len(selected)
        else:
            selected = (
                _select_day_bars_at_times(dataset.day_bars, analysis_times)
                if analysis_times
                else _select_day_bars(
                    dataset.day_bars,
                    start_time=start_time,
                    end_time=end_time,
                    max_bars=max_bars,
                    analysis_every_bars=cadence,
                )
            )
            estimated_day_calls = len(selected)
        return {
            "ok": True,
            "status": "data_ready",
            "target_date": target_date.isoformat(),
            "instrument": dataset.instrument,
            "expiry_month": dataset.expiry_month,
            "night_bar_count": len(dataset.night_bars),
            "day_bar_count": len(dataset.day_bars),
            "spot_status": "ready" if dataset.spot is not None else "unavailable",
            "spot_bar_count": len(dataset.spot.bars) if dataset.spot is not None else 0,
            "spot_previous_close": dataset.spot.previous_close if dataset.spot is not None else None,
            "analysis_every_bars": cadence,
            "detail_bar_count": self.config.detail_bar_count,
            "codex_max_session_turns": self.config.codex_max_session_turns,
            "event_driven": event_driven,
            "deterministic_event_time_count": len(timeline),
            "selected_event_time_count": estimated_day_calls if event_driven else 0,
            "analysis_mode": "program_only" if program_only else self.rules.analysis_mode,
            "decision_authority": "PROGRAM" if program_only else self.rules.decision_authority,
            "execution_profile": self.rules.execution_profile,
            "trade_direction_policy": self.rules.trade_direction_policy,
            "trade_setup_policy": self.rules.trade_setup_policy,
            "estimated_ai_calls": 0 if program_only else estimated_day_calls + 1,
            "night_first_bar": dataset.night_bars.iloc[0]["bar_time"].isoformat(),
            "night_last_bar": dataset.night_bars.iloc[-1]["bar_time"].isoformat(),
            "day_first_bar": dataset.day_bars.iloc[0]["bar_time"].isoformat(),
            "day_last_bar": dataset.day_bars.iloc[-1]["bar_time"].isoformat(),
            "source_sha256": dataset.source_sha256,
            "rule_version": self.rules.version,
            "rule_sha256": self.rules.prompt_sha256,
            "model_rules_sha256": self.rules.model_rules_sha256,
            "schema_sha256": self.rules.schema_sha256,
            "execution_version": self.execution_version,
            "execution_prompt_sha256": self.execution_prompt_sha256,
            "deterministic_timeline_engine_version": DETERMINISTIC_TIMELINE_ENGINE_VERSION,
            "deterministic_timeline_engine_sha256": DETERMINISTIC_TIMELINE_ENGINE_SHA256,
        }

    async def run(
        self,
        *,
        target_date: date,
        mode: str = "auto",
        start_time: time | None = None,
        end_time: time | None = None,
        analysis_times: list[time] | None = None,
        max_bars: int | None = None,
        analysis_every_bars: int | None = None,
        event_driven: bool = False,
        preopen_only: bool = False,
        telegram: bool = False,
        interval_seconds: float = 0,
        program_only: bool = False,
        review_mode: str = "unattended",
    ) -> dict[str, Any]:
        if mode not in {"auto", "step"}:
            raise ReplayRunError("mode_invalid", "mode只允許auto或step。")
        if review_mode not in {"supervised", "unattended"}:
            raise ReplayRunError("review_mode_invalid", "review_mode只允許supervised或unattended。")
        if review_mode == "supervised" and telegram:
            raise ReplayRunError(
                "supervised_direct_delivery_forbidden",
                "監督式回放不得直接批次傳送；請先審核已驗證訊息，再用--deliver-at逐則核准送出。",
            )
        if program_only and not self.deterministic_contract:
            raise ReplayRunError(
                "program_only_contract_required",
                "--program-only只支援確定性課程狀態contract。",
            )
        if event_driven and not self.deterministic_contract:
            raise ReplayRunError("event_driven_contract_required", "事件驅動回放只支援程式證據contract。")
        if event_driven and (analysis_times or analysis_every_bars is not None):
            raise ReplayRunError(
                "event_driven_conflict",
                "--event-driven不能與--at或--analysis-every並用。",
            )
        if analysis_times and any(
            value is not None for value in (start_time, end_time, max_bars, analysis_every_bars)
        ):
            raise ReplayRunError(
                "analysis_times_conflict",
                "指定分析時間不能和區間、根數或固定間隔混用。",
            )
        cadence = 1 if analysis_times or event_driven else analysis_every_bars or self.config.analysis_every_bars
        if cadence < 1 or cadence > 15:
            raise ReplayRunError("analysis_every_invalid", "analysis_every_bars只允許1至15。")
        dataset = self._dataset_loader(target_date, instrument=self.config.instrument)
        deterministic_timeline: list[dict[str, Any]] = []
        if event_driven:
            precomputed_timeline = (
                self._cached_course_timeline(
                    dataset,
                    target_date=target_date,
                    decision_authority=(
                        "AI_HYBRID" if self.ai_hybrid and not program_only else "PROGRAM"
                    ),
                )
                if self.course_chain_contract
                else None
            )
            selected, deterministic_timeline = _select_event_driven_bars(
                dataset.day_bars,
                session_key=f"{target_date.isoformat()}:DAY",
                start_time=start_time,
                end_time=end_time,
                max_bars=max_bars,
                anchor_lifecycle_enabled=self.anchor_lifecycle_contract,
                course_chain_enabled=self.course_chain_contract,
                decision_authority=(
                    "AI_HYBRID" if self.ai_hybrid and not program_only else "PROGRAM"
                ),
                trade_direction_policy=self.rules.trade_direction_policy,
                trade_setup_policy=self.rules.trade_setup_policy,
                history_frame=dataset.night_bars,
                precomputed_timeline=precomputed_timeline,
            )
        else:
            selected = (
                _select_day_bars_at_times(dataset.day_bars, analysis_times)
                if analysis_times
                else _select_day_bars(
                dataset.day_bars,
                start_time=start_time,
                end_time=end_time,
                max_bars=max_bars,
                    analysis_every_bars=cadence,
                )
            )
        presentation_selected = selected.copy()
        if self.course_chain_contract:
            selected = _program_tick_bars(
                dataset.day_bars,
                presentation_selected=presentation_selected,
                start_time=start_time,
            )
        run_dir = create_run_directory(
            target_date=target_date.isoformat(),
            instrument=dataset.instrument,
            rule_version=self.rules.version,
            runtime_root=self._runtime_root,
        )
        manifest = self._new_manifest(
            run_dir=run_dir,
            dataset=dataset,
            mode=mode,
            selected=selected,
            analysis_times=analysis_times,
            start_time=start_time,
            end_time=end_time,
            max_bars=max_bars,
            analysis_every_bars=cadence,
            event_driven=event_driven,
            deterministic_timeline=deterministic_timeline,
            preopen_only=preopen_only,
            telegram=telegram,
            interval_seconds=interval_seconds,
            program_only=program_only,
            review_mode=review_mode,
        )
        manifest["presentation_bar_times"] = [
            row.bar_time.isoformat()
            for row in presentation_selected.itertuples(index=False)
        ]
        manifest["program_tick_every_bars"] = 1 if self.course_chain_contract else cadence
        write_json_atomic(run_dir / "manifest.json", manifest)
        if event_driven or deterministic_timeline:
            write_json_atomic(
                run_dir / "deterministic-timeline.json",
                {
                    "version": 1,
                    "session_key": f"{target_date.isoformat()}:DAY",
                    "event_times": deterministic_timeline,
                    "selected_bar_times": manifest["selected_bar_times"],
                },
            )
        _write_bars(run_dir / "bars.csv", dataset)
        return await self._continue_run(run_dir=run_dir, dataset=dataset, manifest=manifest)

    async def resume(
        self,
        run_id: str,
        *,
        continue_day: bool = False,
        rerun_last: bool = False,
        rewind_to: time | None = None,
        max_bars: int | None = None,
        analysis_every_bars: int | None = None,
        end_time: time | None = None,
        batch_size: int | None = None,
        telegram: bool | None = None,
        revalidate_latest_raw: bool = False,
        reuse_saved_raw: bool = False,
    ) -> dict[str, Any]:
        self._reuse_saved_raw = False
        run_dir = resolve_run_directory(run_id, runtime_root=self._runtime_root)
        manifest = read_json(run_dir / "manifest.json")
        if not isinstance(manifest, dict):
            raise ReplayRunError("manifest_invalid", "回放manifest無效。")
        immutable_contract_matches = (
            manifest.get("rule_version") == self.rules.version
            and manifest.get("rule_sha256") == self.rules.prompt_sha256
            and manifest.get("schema_sha256") == self.rules.schema_sha256
            and manifest.get("execution_prompt_sha256") == self.execution_prompt_sha256
            and str(manifest.get("trade_direction_policy") or "BOTH")
            == self.rules.trade_direction_policy
            and str(manifest.get("trade_setup_policy") or "ALL")
            == self.rules.trade_setup_policy
            and int(manifest.get("detail_bar_count") or self.config.detail_bar_count)
            == self.config.detail_bar_count
            and (
                self.rules.trade_setup_policy != "LONG_Q2_Q4_ONLY"
                or manifest.get("execution_implementation_sha256")
                == self.execution_implementation_sha256
            )
        )
        if not immutable_contract_matches:
            raise ReplayRunError("resume_rule_mismatch", "規則、schema或回放執行提示版本不同，不能接續舊run。")
        execution_version_mismatch = manifest.get("execution_version") != self.execution_version
        if execution_version_mismatch and self.rules.trade_setup_policy == "LONG_Q2_Q4_ONLY":
            # Stage-one performance may never combine state or trades produced
            # by two execution implementations.  Even an explicit rewind can
            # leave earlier fills/checkpoints from the previous implementation
            # in the same run, so Q2/Q4 development always starts a new run.
            raise ReplayRunError(
                "resume_rule_mismatch",
                "Q2/Q4回放執行版本不同，必須建立新run；不得在同一績效run內遷移版本。",
            )
        # A stricter local validator can change the execution version without
        # changing the immutable prompt/schema hashes.  Permit that migration
        # only together with an explicit causal rewind; ordinary resume stays
        # fail-closed so state-affecting code changes cannot be mixed silently.
        if execution_version_mismatch and rewind_to is None:
            raise ReplayRunError("resume_rule_mismatch", "回放執行版本不同；必須明確--rewind-to後才能遷移守門器版本。")
        manifest_program_only = bool(manifest.get("program_only"))
        manifest_provider = None if manifest_program_only else str(manifest.get("ai_provider") or "minimax")
        if not manifest_program_only:
            if manifest_provider != self.config.ai_provider or manifest.get("ai_model") != self.config.ai_model:
                raise ReplayRunError("resume_provider_mismatch", "AI provider或model不同，不能接續舊run。")
            if manifest_provider == "codex" and manifest.get("ai_reasoning_effort") != self.config.codex_reasoning_effort:
                raise ReplayRunError("resume_provider_mismatch", "Codex reasoning effort不同，不能接續舊run。")
            saved_attempts = manifest.get("max_ai_attempts")
            if saved_attempts is not None and int(saved_attempts) != self.config.max_ai_attempts:
                raise ReplayRunError("resume_provider_mismatch", "AI單點重試上限不同，不能接續舊run。")
        if manifest.get("event_driven") and (
            analysis_every_bars is not None or end_time is not None or max_bars is not None
        ):
            raise ReplayRunError(
                "event_driven_plan_locked",
                "事件驅動run的因果事件時間已鎖定；續跑只能使用--continue-day或--batch-size。",
            )
        target_date = date.fromisoformat(str(manifest.get("target_date")))
        dataset = self._dataset_loader(target_date, instrument=self.config.instrument)
        if dataset.source_sha256 != manifest.get("source_sha256"):
            raise ReplayRunError("resume_data_mismatch", "歷史來源雜湊不同，不能接續舊run。")
        if revalidate_latest_raw:
            if (
                continue_day
                or rerun_last
                or rewind_to is not None
                or max_bars is not None
                or analysis_every_bars is not None
                or end_time is not None
                or batch_size is not None
                or telegram is not None
                or reuse_saved_raw
            ):
                raise ReplayRunError(
                    "revalidate_arguments_invalid",
                    "--revalidate-latest-raw必須單獨搭配--resume使用。",
                )
            if manifest.get("status") in {"paused", "completed"}:
                # A paused/completed run owns a current accepted cutoff. Old
                # raws from a previously rewound future branch may still be on
                # disk, but they must never outrank that accepted cutoff.
                self._revalidate_raw_once = _latest_completed_raw(run_dir, manifest)
                self._rewind_last_day_bar(run_dir, manifest)
            else:
                try:
                    self._revalidate_raw_once = _latest_pending_raw(run_dir, manifest, dataset)
                except ReplayRunError as exc:
                    if exc.code not in {"revalidate_pending_missing", "revalidate_raw_missing"}:
                        raise
                    # Presentation-only or local guard fixes often need to
                    # rebuild the latest already accepted point.
                    self._revalidate_raw_once = _latest_completed_raw(run_dir, manifest)
                    self._rewind_last_day_bar(run_dir, manifest)
        if reuse_saved_raw:
            if (
                continue_day
                or rerun_last
                or rewind_to is not None
                or max_bars is not None
                or analysis_every_bars is not None
                or end_time is not None
                or telegram is not None
            ):
                raise ReplayRunError(
                    "reuse_saved_raw_arguments_invalid",
                    "--reuse-saved-raw只可搭配--resume及可選的--batch-size使用。",
                )
            if manifest.get("status") not in {"paused", "failed"}:
                raise ReplayRunError(
                    "reuse_saved_raw_status_invalid",
                    "只有已暫停或失敗的因果回放可依序重驗保存的原始輸出。",
                )
            self._reuse_saved_raw = True
        if rerun_last:
            if continue_day or rewind_to is not None or max_bars is not None or analysis_every_bars is not None or end_time is not None or batch_size is not None:
                raise ReplayRunError(
                    "rerun_arguments_invalid",
                    "--rerun-last不能與--continue-day、--max-bars、--analysis-every、--to或--batch-size並用。",
                )
            self._rewind_last_day_bar(run_dir, manifest)
            _retire_codex_session(manifest, reason="rerun_last")
        elif rewind_to is not None:
            if continue_day or max_bars is not None or analysis_every_bars is not None or end_time is not None or batch_size is not None or telegram is not None:
                raise ReplayRunError(
                    "rewind_arguments_invalid",
                    "--rewind-to必須單獨搭配--resume使用；回退完成後再用--continue-day續跑。",
                )
            previous_execution_version = manifest.get("execution_version")
            self._rewind_to_day_bar(run_dir, manifest, rewind_to=rewind_to)
            _retire_codex_session(manifest, reason="causal_rewind")
            if execution_version_mismatch:
                history = list(manifest.get("execution_version_migrations") or [])
                history.append(
                    {
                        "migrated_at": datetime.now(TAIPEI).isoformat(),
                        "from": previous_execution_version,
                        "to": self.execution_version,
                        "rewind_to": rewind_to.isoformat(timespec="minutes"),
                        "reason": "immutable_prompt_schema_match_guard_only_migration",
                    }
                )
                manifest["execution_version_migrations"] = history
                manifest["execution_version"] = self.execution_version
            manifest["invocation_batch_size"] = 0
            manifest["status"] = "paused"
            manifest.pop("error_code", None)
            manifest.pop("error", None)
            self._save_manifest(run_dir, manifest)
            return self._result(run_dir, manifest)
        elif continue_day:
            completed = int(manifest.get("next_day_index") or 0)
            existing_tick_times = list(manifest.get("selected_bar_times") or [])
            completed_tick_times = existing_tick_times[:completed]
            last_completed_tick = (
                datetime.fromisoformat(str(completed_tick_times[-1]))
                if completed_tick_times
                else None
            )
            existing_presentation_times = list(
                manifest.get("presentation_bar_times")
                or manifest.get("selected_bar_times")
                or []
            )
            completed_presentation_times = [
                str(value)
                for value in existing_presentation_times
                if last_completed_tick is not None
                and datetime.fromisoformat(str(value)) <= last_completed_tick
            ]
            if end_time is not None:
                old_end = _optional_time(manifest.get("end_time"))
                if old_end is not None and end_time <= old_end:
                    raise ReplayRunError("resume_end_time_invalid", "新的--to必須晚於既有run終點。")
                # Rebuild the extended cadence from the immutable session
                # start.  Starting a fresh cadence after the last completed
                # tick changes an 08:46/08:48/... two-bar plan into an odd-
                # minute plan after a failure or pause.  Keep every existing
                # planned presentation point (including a partial tail added
                # by an earlier shorter --to) and union it with the rebuilt
                # aligned plan.
                extended_plan = _select_day_bars(
                    dataset.day_bars,
                    start_time=_optional_time(manifest.get("start_time")),
                    end_time=end_time,
                    max_bars=None,
                    analysis_every_bars=int(manifest.get("analysis_every_bars") or self.config.analysis_every_bars),
                )
                presentation_times = existing_presentation_times + [
                    row.bar_time.isoformat()
                    for row in extended_plan.itertuples(index=False)
                ]
                manifest["end_time"] = end_time.isoformat(timespec="minutes")
                manifest["max_bars"] = None
            elif analysis_every_bars is not None:
                if completed:
                    raise ReplayRunError("resume_cadence_locked", "已有日盤K完成後不能變更分析頻率。")
                if analysis_every_bars < 1 or analysis_every_bars > 15:
                    raise ReplayRunError("analysis_every_invalid", "analysis_every_bars只允許1至15。")
                selected = _select_day_bars(
                    dataset.day_bars,
                    start_time=_optional_time(manifest.get("start_time")),
                    end_time=_optional_time(manifest.get("end_time")),
                    max_bars=max_bars,
                    analysis_every_bars=analysis_every_bars,
                )
                presentation_times = [
                    row.bar_time.isoformat()
                    for row in selected.itertuples(index=False)
                ]
                manifest["analysis_every_bars"] = analysis_every_bars
                manifest["max_bars"] = max_bars
            else:
                presentation_times = existing_presentation_times
            if max_bars is not None and analysis_every_bars is None:
                if max_bars < len(completed_presentation_times):
                    raise ReplayRunError("resume_max_bars_invalid", "max_bars不能小於已完成的日盤K數。")
                # Rebuild the plan from the immutable source bars instead of
                # slicing the previously truncated list.  This lets an
                # operator validate one analysis point first, then safely
                # extend the same run to more points without changing cadence.
                selected = _select_day_bars(
                    dataset.day_bars,
                    start_time=_optional_time(manifest.get("start_time")),
                    end_time=_optional_time(manifest.get("end_time")),
                    max_bars=max_bars,
                    analysis_every_bars=int(manifest.get("analysis_every_bars") or self.config.analysis_every_bars),
                )
                presentation_times = [
                    row.bar_time.isoformat()
                    for row in selected.itertuples(index=False)
                ]
                manifest["max_bars"] = max_bars
            presentation_times = list(dict.fromkeys(str(value) for value in presentation_times))
            presentation_set = set(presentation_times)
            presentation_selected = dataset.day_bars[
                dataset.day_bars["bar_time"].map(
                    lambda value: value.isoformat() in presentation_set
                )
            ].copy()
            presentation_selected.sort_values("bar_time", inplace=True)
            presentation_selected.reset_index(drop=True, inplace=True)
            if self.course_chain_contract:
                selected = _program_tick_bars(
                    dataset.day_bars,
                    presentation_selected=presentation_selected,
                    start_time=_optional_time(manifest.get("start_time")),
                )
                selected_times = [
                    row.bar_time.isoformat()
                    for row in selected.itertuples(index=False)
                ]
            else:
                selected_times = presentation_times
            manifest["presentation_bar_times"] = presentation_times
            manifest["selected_bar_times"] = selected_times
            if last_completed_tick is None:
                manifest["next_day_index"] = 0
            else:
                manifest["next_day_index"] = sum(
                    datetime.fromisoformat(str(value)) <= last_completed_tick
                    for value in selected_times
                )
            manifest["preopen_only"] = False
        elif max_bars is not None or analysis_every_bars is not None or end_time is not None:
            raise ReplayRunError("resume_plan_requires_continue", "接續盤前run並調整日盤計畫時，必須同時使用--continue-day。")
        if batch_size is not None and batch_size <= 0:
            raise ReplayRunError("batch_size_invalid", "--batch-size必須是正整數。")
        # Repairing one saved raw result / rewinding one point must never
        # silently run the remaining day (or send unreviewed later results).
        manifest["invocation_batch_size"] = 1 if revalidate_latest_raw or rerun_last else batch_size
        if telegram is not None:
            manifest["telegram_requested"] = telegram
        manifest["status"] = "running"
        manifest.pop("error_code", None)
        manifest.pop("error", None)
        self._save_manifest(run_dir, manifest)
        return await self._continue_run(run_dir=run_dir, dataset=dataset, manifest=manifest)

    def _rewind_last_day_bar(self, run_dir: Path, manifest: dict[str, Any]) -> None:
        completed = int(manifest.get("next_day_index") or 0)
        if completed < 1:
            raise ReplayRunError("rerun_last_missing", "目前沒有已完成的日盤K可以重跑。")
        selected_times = list(manifest.get("selected_bar_times") or [])
        rewind_index = completed - 1
        if rewind_index >= len(selected_times):
            raise ReplayRunError("rerun_last_invalid", "manifest的日盤進度無效。")
        if rewind_index == 0:
            candidates = list((run_dir / "analysis").glob("preopen-*-validated.json"))
        else:
            previous = datetime.fromisoformat(str(selected_times[rewind_index - 1]))
            candidates = list(
                (run_dir / "analysis").glob(f"day-{previous.strftime('%Y%m%d-%H%M')}*-validated.json")
            )
        if not candidates:
            raise ReplayRunError("rerun_checkpoint_missing", "找不到前一個已驗證狀態，不能安全重跑。")
        checkpoint_path = max(candidates, key=lambda path: path.stat().st_mtime_ns)
        checkpoint = read_json(checkpoint_path)
        if self.full_fidelity:
            state_checkpoint_path = checkpoint_path.with_name(
                checkpoint_path.name.replace("-validated.json", "-state-checkpoint.json")
            )
            state_checkpoint = read_json(state_checkpoint_path)
            structure = (
                state_checkpoint.get("market_structure_state")
                if isinstance(state_checkpoint, Mapping)
                else None
            )
            summary = (
                state_checkpoint.get("last_analysis_summary")
                if isinstance(state_checkpoint, Mapping)
                else None
            )
            constitution = (
                state_checkpoint.get("constitution_state")
                if isinstance(state_checkpoint, Mapping)
                else None
            )
            if not all(isinstance(item, Mapping) for item in (structure, summary, constitution)):
                raise ReplayRunError(
                    "rerun_checkpoint_invalid",
                    "前一個正式v8回放狀態無效，不能安全重跑。",
                )
            write_json_atomic(run_dir / "market-structure-state.json", dict(structure))
            write_json_atomic(run_dir / "last-analysis-summary.json", dict(summary))
            write_json_atomic(run_dir / "constitution-state.json", dict(constitution))
        elif self.deterministic_contract:
            state_checkpoint_path = checkpoint_path.with_name(
                checkpoint_path.name.replace("-validated.json", "-state-checkpoint.json")
            )
            state_checkpoint = read_json(state_checkpoint_path)
            if not isinstance(state_checkpoint, Mapping):
                raise ReplayRunError("rerun_checkpoint_invalid", "前一個程式證據回放狀態無效。")
            required = {
                "deterministic_evidence_ledger": "deterministic-evidence-ledger.json",
                "semantic_memory": "replay-memory.json",
                "position_state": "replay-position-state.json",
                "constitution_state": "constitution-state.json",
            }
            if self.execution_gate_contract:
                required["deterministic_event_lifecycle"] = "deterministic-event-state.json"
            if self.anchor_lifecycle_contract:
                required["anchor_lifecycle_state"] = "anchor-lifecycle-state.json"
            for key, filename in required.items():
                value = state_checkpoint.get(key)
                if not isinstance(value, Mapping):
                    raise ReplayRunError("rerun_checkpoint_invalid", "前一個程式證據回放狀態不完整。")
                write_json_atomic(run_dir / filename, dict(value))
        else:
            memory = checkpoint.get("memory") if isinstance(checkpoint, Mapping) else None
            if not isinstance(memory, Mapping):
                raise ReplayRunError("rerun_checkpoint_invalid", "前一個已驗證狀態無效，不能安全重跑。")
            write_json_atomic(run_dir / "replay-memory.json", dict(memory))
        append_jsonl(
            run_dir / "rewinds.jsonl",
            {
                "rewound_at": datetime.now(TAIPEI).isoformat(),
                "bar_time": selected_times[rewind_index],
                "restored_checkpoint": checkpoint_path.name,
                "previous_completed_day_bars": completed,
            },
        )
        manifest["next_day_index"] = rewind_index
        manifest["completed_program_ticks"] = rewind_index
        presentation_times = set(
            str(value)
            for value in (
                manifest.get("presentation_bar_times")
                or manifest.get("selected_bar_times")
                or []
            )
        )
        completed_presentation_bars = sum(
            str(value) in presentation_times
            for value in selected_times[:rewind_index]
        )
        manifest["completed_presentation_bars"] = completed_presentation_bars
        manifest["completed_day_bars"] = completed_presentation_bars

    def _rewind_to_day_bar(self, run_dir: Path, manifest: dict[str, Any], *, rewind_to: time) -> None:
        selected_times = list(manifest.get("selected_bar_times") or [])
        matches = [
            index
            for index, value in enumerate(selected_times)
            if datetime.fromisoformat(str(value)).time().replace(second=0, microsecond=0)
            == rewind_to.replace(second=0, microsecond=0)
        ]
        if len(matches) != 1:
            raise ReplayRunError("rewind_time_missing", "指定時間不是此run唯一的日盤分析點。")
        target_index = matches[0]
        completed = int(manifest.get("next_day_index") or 0)
        if target_index >= completed:
            raise ReplayRunError("rewind_time_not_completed", "指定分析點尚未完成，無需回退。")
        while int(manifest.get("next_day_index") or 0) > target_index:
            self._rewind_last_day_bar(run_dir, manifest)

    async def deliver_latest(self, run_id: str) -> dict[str, Any]:
        """Deliver the latest already-validated canonical message without rerunning AI."""
        run_dir = resolve_run_directory(run_id, runtime_root=self._runtime_root)
        manifest = read_json(run_dir / "manifest.json")
        if not isinstance(manifest, dict):
            raise ReplayRunError("manifest_invalid", "回放manifest無效。")
        if (
            manifest.get("rule_version") != self.rules.version
            or manifest.get("rule_sha256") != self.rules.prompt_sha256
            or manifest.get("schema_sha256") != self.rules.schema_sha256
            or manifest.get("execution_version") != self.execution_version
            or manifest.get("execution_prompt_sha256") != self.execution_prompt_sha256
            or (
                self.rules.trade_setup_policy == "LONG_Q2_Q4_ONLY"
                and manifest.get("execution_implementation_sha256")
                != self.execution_implementation_sha256
            )
        ):
            raise ReplayRunError("delivery_rule_mismatch", "已保存訊息的規則、schema或執行提示版本不同，拒絕補送。")
        message_record = _latest_jsonl_record(run_dir / "messages.jsonl")
        if not message_record:
            raise ReplayRunError("delivery_message_missing", "這個run沒有已驗證的canonical message。")
        if not _message_is_current(manifest, message_record):
            raise ReplayRunError("delivery_message_superseded", "最新訊息已被回退取代，必須等該根K重新驗證成功後才能補送。")
        decision = str(message_record.get("decision") or "")
        if decision != "NOTIFY" and not self.config.send_quiet_status:
            return {
                "ok": True,
                "status": "skipped_quiet",
                "run_id": run_dir.name,
                "event_id": message_record.get("event_id"),
                "chunk_count": 0,
                "message_ids": [],
            }
        notifier = self._get_notifier(run_dir)
        _record_manual_delivery_approval(
            run_dir,
            manifest,
            message_record=message_record,
            approval_source="deliver_latest",
        )
        delivery = await notifier.send(
            event_id=str(message_record.get("event_id") or ""),
            message=str(message_record.get("message") or ""),
        )
        append_jsonl(
            run_dir / "deliveries.jsonl",
            {
                "stage": message_record.get("stage"),
                "bar_time": message_record.get("bar_time"),
                "event_id": message_record.get("event_id"),
                "ok": delivery.get("ok"),
                "status": delivery.get("status"),
                "chunk_count": delivery.get("chunk_count"),
                "message_ids": delivery.get("message_ids") or [],
                "error_code": delivery.get("error_code"),
                "delivery_source": "saved_canonical_message",
            },
        )
        manifest["notification_count"] = _count_notifications(run_dir)
        self._save_manifest(run_dir, manifest)
        _write_summary(run_dir, manifest)
        if not delivery.get("ok"):
            raise ReplayRunError(
                str(delivery.get("error_code") or "telegram_delivery_failed"),
                str(delivery.get("error") or "Telegram補送失敗。"),
            )
        return {
            "ok": True,
            "status": delivery.get("status"),
            "run_id": run_dir.name,
            "event_id": message_record.get("event_id"),
            "chunk_count": delivery.get("chunk_count"),
            "message_ids": delivery.get("message_ids") or [],
        }

    async def deliver_at(self, run_id: str, *, at_time: time) -> dict[str, Any]:
        """Deliver one already-validated selected cutoff without rerunning AI."""

        run_dir = resolve_run_directory(run_id, runtime_root=self._runtime_root)
        manifest = read_json(run_dir / "manifest.json")
        if not isinstance(manifest, dict):
            raise ReplayRunError("manifest_invalid", "回放manifest無效。")
        if (
            manifest.get("rule_version") != self.rules.version
            or manifest.get("rule_sha256") != self.rules.prompt_sha256
            or manifest.get("schema_sha256") != self.rules.schema_sha256
            or manifest.get("execution_version") != self.execution_version
            or manifest.get("execution_prompt_sha256") != self.execution_prompt_sha256
            or (
                self.rules.trade_setup_policy == "LONG_Q2_Q4_ONLY"
                and manifest.get("execution_implementation_sha256")
                != self.execution_implementation_sha256
            )
        ):
            raise ReplayRunError("delivery_rule_mismatch", "已保存訊息的規則、schema或執行提示版本不同，拒絕補送。")
        records = _jsonl_records(run_dir / "messages.jsonl")
        matches = [
            item
            for item in records
            if item.get("stage") == "day"
            and _optional_hhmm(item.get("bar_time")) == at_time
        ]
        if not matches:
            raise ReplayRunError("delivery_message_missing", "指定時間沒有已驗證的canonical message。")
        message_record = matches[-1]
        if not _message_is_current(manifest, message_record):
            raise ReplayRunError("delivery_message_superseded", "指定訊息已被回退取代，不能補送。")
        if message_record.get("decision") != "NOTIFY" and not self.config.send_quiet_status:
            return {"ok": True, "status": "skipped_quiet", "run_id": run_dir.name, "chunk_count": 0, "message_ids": []}
        notifier = self._get_notifier(run_dir)
        _record_manual_delivery_approval(
            run_dir,
            manifest,
            message_record=message_record,
            approval_source="deliver_at",
        )
        delivery = await notifier.send(
            event_id=str(message_record.get("event_id") or ""),
            message=str(message_record.get("message") or ""),
        )
        append_jsonl(
            run_dir / "deliveries.jsonl",
            {
                "stage": message_record.get("stage"),
                "bar_time": message_record.get("bar_time"),
                "event_id": message_record.get("event_id"),
                "ok": delivery.get("ok"),
                "status": delivery.get("status"),
                "chunk_count": delivery.get("chunk_count"),
                "message_ids": delivery.get("message_ids") or [],
                "error_code": delivery.get("error_code"),
                "delivery_source": "saved_selected_canonical_message",
            },
        )
        manifest["notification_count"] = _count_notifications(run_dir)
        self._save_manifest(run_dir, manifest)
        _write_summary(run_dir, manifest)
        if not delivery.get("ok"):
            raise ReplayRunError(
                str(delivery.get("error_code") or "telegram_delivery_failed"),
                str(delivery.get("error") or "Telegram補送失敗。"),
            )
        return {
            "ok": True,
            "status": delivery.get("status"),
            "run_id": run_dir.name,
            "event_id": message_record.get("event_id"),
            "bar_time": message_record.get("bar_time"),
            "chunk_count": delivery.get("chunk_count"),
            "message_ids": delivery.get("message_ids") or [],
        }

    async def test_telegram(self) -> dict[str, Any]:
        """Send one deduplicated connectivity message to the replay-only group."""
        state_path = self._runtime_root / "telegram-connectivity-state.json"
        notifier = self._notifier_for_state(state_path)
        tested_at = datetime.now(TAIPEI).replace(microsecond=0).isoformat()
        message = (
            "🧪【TMF歷史回放 Telegram 整合測試】\n\n"
            "- 期交所TMF與證交所現貨資料管線已連線。\n"
            "- 預設每兩根已收盤1分K交由分析模型判讀。\n"
            "- 這是系統連線測試，不是交易訊號。\n"
            f"- 測試時間：{tested_at}\n\n"
            "一般技術分析，非個人化投資建議。\n"
            "**這是歷史資料模擬，不是即時交易訊號。**\n"
        )
        event_id = hashlib.sha256(
            f"trade-monitor-replay-connectivity-v1\n{self.config.telegram_chat_id}".encode("utf-8")
        ).hexdigest()
        delivery = await notifier.send(event_id=event_id, message=message)
        if not delivery.get("ok"):
            raise ReplayRunError(
                str(delivery.get("error_code") or "telegram_test_failed"),
                str(delivery.get("error") or "模擬Telegram連線測試失敗。"),
            )
        return {
            "ok": True,
            "status": delivery.get("status"),
            "event_id": event_id,
            "chunk_count": delivery.get("chunk_count"),
            "message_ids": delivery.get("message_ids") or [],
            "tested_at": tested_at,
        }

    def _new_manifest(
        self,
        *,
        run_dir: Path,
        dataset: ReplayDataset,
        mode: str,
        selected: pd.DataFrame,
        analysis_times: list[time] | None,
        start_time: time | None,
        end_time: time | None,
        max_bars: int | None,
        analysis_every_bars: int,
        event_driven: bool,
        deterministic_timeline: list[dict[str, Any]],
        preopen_only: bool,
        telegram: bool,
        interval_seconds: float,
        program_only: bool,
        review_mode: str,
    ) -> dict[str, Any]:
        now = datetime.now(TAIPEI).isoformat()
        return {
            "version": 1,
            "run_id": run_dir.name,
            "status": "running",
            "created_at": now,
            "updated_at": now,
            "target_date": dataset.target_date.isoformat(),
            "instrument": dataset.instrument,
            "expiry_month": dataset.expiry_month,
            "source_sha256": dataset.source_sha256,
            "rule_version": self.rules.version,
            "rule_source_version": self.rules.source_version,
            "rule_sha256": self.rules.prompt_sha256,
            "model_rules_sha256": self.rules.model_rules_sha256,
            "schema_sha256": self.rules.schema_sha256,
            "execution_version": self.execution_version,
            "execution_prompt_sha256": self.execution_prompt_sha256,
            "execution_implementation_sha256": self.execution_implementation_sha256,
            "deterministic_timeline_engine_version": DETERMINISTIC_TIMELINE_ENGINE_VERSION,
            "deterministic_timeline_engine_sha256": DETERMINISTIC_TIMELINE_ENGINE_SHA256,
            "mode": mode,
            "start_time": start_time.isoformat(timespec="minutes") if start_time else None,
            "end_time": end_time.isoformat(timespec="minutes") if end_time else None,
            "analysis_times": [value.isoformat(timespec="minutes") for value in analysis_times or []],
            "max_bars": max_bars,
            "analysis_every_bars": analysis_every_bars,
            "detail_bar_count": self.config.detail_bar_count,
            "event_driven": event_driven,
            "deterministic_timeline_mode": (
                "precomputed_event_driven" if event_driven else "not_required_fixed_cadence"
            ),
            "deterministic_event_time_count": len(deterministic_timeline),
            "preopen_only": preopen_only,
            "telegram_requested": telegram,
            "interval_seconds": interval_seconds,
            "analysis_mode": "program_only" if program_only else self.rules.analysis_mode,
            "decision_authority": "PROGRAM" if program_only else self.rules.decision_authority,
            "execution_profile": self.rules.execution_profile,
            "trade_direction_policy": self.rules.trade_direction_policy,
            "trade_setup_policy": self.rules.trade_setup_policy,
            "ai_input_view_version": (
                AI_HYBRID_PROMPT_VIEW_VERSION
                if self.ai_hybrid and not program_only
                else None
            ),
            "ai_input_interpretive_answers_redacted": bool(
                self.ai_hybrid and not program_only
            ),
            "program_only": program_only,
            "review_mode": review_mode,
            "human_intervention": review_mode == "supervised",
            "approved_delivery_count": 0,
            "preopen_completed": False,
            "selected_bar_times": [row.bar_time.isoformat() for row in selected.itertuples(index=False)],
            "next_day_index": 0,
            "completed_day_bars": 0,
            "completed_program_ticks": 0,
            "completed_presentation_bars": 0,
            "failed_bars": 0,
            "ai_provider": None if program_only else self.config.ai_provider,
            "ai_model": None if program_only else self.config.ai_model,
            "ai_reasoning_effort": (
                None
                if program_only
                else self.config.codex_reasoning_effort
                if self.config.ai_provider == "codex"
                else None
            ),
            "max_ai_attempts": 0 if program_only else self.config.max_ai_attempts,
            "ai_call_count": 0,
            "minimax_call_count": 0,
            "notification_count": 0,
            "codex_persistent_session": bool(
                self.ai_hybrid and not program_only and self.config.ai_provider == "codex"
            ),
            "codex_max_session_turns": (
                self.config.codex_max_session_turns
                if self.ai_hybrid and not program_only and self.config.ai_provider == "codex"
                else None
            ),
            "codex_session_id": None,
            "codex_session_turn_count": 0,
            "retired_codex_sessions": [],
        }

    async def _continue_run(
        self,
        *,
        run_dir: Path,
        dataset: ReplayDataset,
        manifest: dict[str, Any],
    ) -> dict[str, Any]:
        program_only = bool(manifest.get("program_only"))
        analyzer = None if program_only else self._get_analyzer()
        if isinstance(analyzer, CodexReplayAnalyzer) and analyzer.persistent_session:
            analyzer.bind_run(
                run_dir,
                session_id=manifest.get("codex_session_id"),
                session_turn_count=int(manifest.get("codex_session_turn_count") or 0),
            )
        notifier = self._get_notifier(run_dir) if manifest.get("telegram_requested") else None
        selected_times = set(str(item) for item in manifest.get("selected_bar_times") or [])
        presentation_times = set(
            str(item)
            for item in (
                manifest.get("presentation_bar_times")
                or manifest.get("selected_bar_times")
                or []
            )
        )
        selected = dataset.day_bars[dataset.day_bars["bar_time"].map(lambda value: value.isoformat() in selected_times)].copy()
        selected.sort_values("bar_time", inplace=True)
        selected.reset_index(drop=True, inplace=True)
        try:
            if not manifest.get("preopen_completed"):
                await self._process_stage(
                    run_dir=run_dir,
                    dataset=dataset,
                    visible=dataset.night_bars,
                    bar_time=dataset.night_bars.iloc[-1]["bar_time"].to_pydatetime(),
                    stage="preopen",
                    new_closed_bar_count=1,
                    analyzer=analyzer,
                    notifier=notifier,
                    program_only=program_only,
                )
                manifest["preopen_completed"] = True
                manifest["ai_call_count"] = _count_usage_calls(run_dir)
                manifest["minimax_call_count"] = manifest["ai_call_count"] if self.config.ai_provider == "minimax" else 0
                manifest["notification_count"] = _count_notifications(run_dir)
                _capture_codex_session(manifest, analyzer)
                self._save_manifest(run_dir, manifest)
            if manifest.get("preopen_only"):
                manifest["status"] = "completed"
                self._save_manifest(run_dir, manifest)
                return self._result(run_dir, manifest)
            start_index = int(manifest.get("next_day_index") or 0)
            batch_size = manifest.get("invocation_batch_size")
            batch_limit = int(batch_size) if batch_size is not None else None
            processed_this_invocation = 0
            all_bars = dataset.all_bars.reset_index(drop=True)
            for index in range(start_index, len(selected)):
                row = selected.iloc[index]
                current_time = row["bar_time"]
                current_iso = current_time.isoformat()
                emit_output = current_iso in presentation_times
                visible = all_bars[all_bars["bar_time"] <= current_time].copy()
                if index == 0:
                    new_closed_bar_count = int((dataset.day_bars["bar_time"] <= current_time).sum())
                else:
                    previous_time = selected.iloc[index - 1]["bar_time"]
                    new_closed_bar_count = int(
                        ((dataset.day_bars["bar_time"] > previous_time) & (dataset.day_bars["bar_time"] <= current_time)).sum()
                    )
                stage_result = await self._process_stage(
                    run_dir=run_dir,
                    dataset=dataset,
                    visible=visible,
                    bar_time=current_time.to_pydatetime(),
                    stage="day",
                    new_closed_bar_count=new_closed_bar_count,
                    analyzer=analyzer,
                    notifier=notifier,
                    program_only=program_only or not emit_output,
                    emit_output=emit_output,
                )
                manifest["next_day_index"] = index + 1
                manifest["completed_program_ticks"] = index + 1
                completed_presentation_bars = sum(
                    str(value) in presentation_times
                    for value in manifest.get("selected_bar_times", [])[: index + 1]
                )
                manifest["completed_presentation_bars"] = completed_presentation_bars
                # Backward-compatible public progress means completed
                # analysis/presentation points, not hidden 1m program ticks.
                manifest["completed_day_bars"] = completed_presentation_bars
                manifest["ai_call_count"] = _count_usage_calls(run_dir)
                manifest["minimax_call_count"] = manifest["ai_call_count"] if self.config.ai_provider == "minimax" else 0
                manifest["notification_count"] = _count_notifications(run_dir)
                _capture_codex_session(manifest, analyzer)
                self._save_manifest(run_dir, manifest)
                if emit_output:
                    processed_this_invocation += 1
                if emit_output and manifest.get("mode") == "step" and not self._step_callback(stage_result):
                    manifest["status"] = "paused"
                    self._save_manifest(run_dir, manifest)
                    return self._result(run_dir, manifest)
                if (
                    emit_output
                    and batch_limit is not None
                    and processed_this_invocation >= batch_limit
                    and index + 1 < len(selected)
                ):
                    manifest["status"] = "paused"
                    self._save_manifest(run_dir, manifest)
                    return self._result(run_dir, manifest)
                delay = float(manifest.get("interval_seconds") or 0)
                if emit_output and delay > 0 and index + 1 < len(selected):
                    await asyncio.sleep(delay)
            manifest["status"] = "completed"
            self._save_manifest(run_dir, manifest)
            _write_summary(run_dir, manifest)
            return self._result(run_dir, manifest)
        except Exception as exc:
            _capture_codex_session(manifest, analyzer)
            manifest["status"] = "failed"
            manifest["failed_bars"] = int(manifest.get("failed_bars") or 0) + 1
            manifest["error_code"] = getattr(exc, "code", "replay_failed")
            manifest["error"] = _safe_error(exc)
            self._save_manifest(run_dir, manifest)
            _write_summary(run_dir, manifest)
            raise

    async def _process_stage(
        self,
        *,
        run_dir: Path,
        dataset: ReplayDataset,
        visible: pd.DataFrame,
        bar_time: datetime,
        stage: str,
        new_closed_bar_count: int,
        analyzer: Any | None,
        notifier: ReplayNotifier | None,
        program_only: bool = False,
        emit_output: bool = True,
    ) -> dict[str, Any]:
        available_at = bar_time + timedelta(minutes=1)
        context = _build_context(
            run_dir=run_dir,
            bar_time=bar_time,
            available_at=available_at,
            stage=stage,
            new_closed_bar_count=new_closed_bar_count,
        )
        structured = _structured_context(
            run_dir=run_dir,
            visible=visible,
            instrument=dataset.instrument,
            expected=bar_time,
        )
        memory_path = run_dir / "replay-memory.json"
        session_suffix = "NIGHT" if stage == "preopen" else "DAY"
        separator = "-" if self.full_fidelity else ":"
        expected_session_key = f"{dataset.target_date.isoformat()}{separator}{session_suffix}"
        previous_market_structure: Mapping[str, Any] | None = None
        previous_analysis_summary: Mapping[str, Any] | None = None
        constitution_snapshot: Mapping[str, Any] | None = None
        previous_memory: Mapping[str, Any] | None = None
        previous_ledger: Mapping[str, Any] | None = None
        position_snapshot: Mapping[str, Any] | None = None
        pending_execution_event: Mapping[str, Any] | None = None
        pending_exit_event: Mapping[str, Any] | None = None
        program_stop_event: Mapping[str, Any] | None = None
        protective_stop_audit: Mapping[str, Any] | None = None
        program_behavior_exit_audit: Mapping[str, Any] | None = None
        constitution_requalification_audit: Mapping[str, Any] | None = None
        if self.full_fidelity:
            saved_structure = read_json(run_dir / "market-structure-state.json")
            if isinstance(saved_structure, Mapping):
                previous_market_structure = saved_structure
            saved_summary = read_json(run_dir / "last-analysis-summary.json")
            if isinstance(saved_summary, Mapping):
                previous_analysis_summary = saved_summary
            constitution_snapshot = load_constitution_snapshot(
                run_dir / "constitution-state.json",
                at=bar_time,
            )
        else:
            saved_memory = read_json(memory_path)
            if isinstance(saved_memory, Mapping):
                previous_memory = saved_memory
            else:
                previous_memory = (
                    empty_semantic_memory(
                        as_of=bar_time.isoformat(),
                        session_key=expected_session_key,
                        version=self.rules.memory_version,
                        anchor_lifecycle=self.anchor_lifecycle_contract,
                    )
                    if self.deterministic_contract
                    else empty_replay_memory(as_of=bar_time.isoformat(), session_key=expected_session_key)
                )
            if self.deterministic_contract:
                constitution_snapshot = load_constitution_snapshot(
                    run_dir / "constitution-state.json",
                    at=bar_time,
                )
                saved_ledger = read_json(run_dir / "deterministic-evidence-ledger.json")
                if isinstance(saved_ledger, Mapping):
                    previous_ledger = saved_ledger
                saved_position = read_json(run_dir / "replay-position-state.json")
                position_snapshot = (
                    saved_position
                    if isinstance(saved_position, Mapping)
                    else initial_position_state(
                        as_of=bar_time.isoformat(),
                        version=2 if self.rules.memory_version >= 3 else 1,
                    )
                )
                if self.execution_gate_contract:
                    position_snapshot, pending_exit_event = fill_pending_exit(
                        position_snapshot,
                        _visible_bar_records(visible),
                        as_of=bar_time.isoformat(),
                    )
                    if pending_exit_event is not None:
                        write_json_atomic(run_dir / "replay-position-state.json", position_snapshot)
                        _append_execution_event_once(
                            run_dir / "execution-events.jsonl",
                            dict(pending_exit_event),
                        )
                        if pending_exit_event.get("event_type") == "EXIT_FILLED":
                            constitution_snapshot, constitution_lock_event = (
                                apply_program_execution_event(
                                    run_dir / "constitution-state.json",
                                    pending_exit_event,
                                )
                            )
                            if constitution_lock_event is not None:
                                _append_execution_event_once(
                                    run_dir / "execution-events.jsonl",
                                    constitution_lock_event,
                                )
                            await self._record_programmatic_exit_fill(
                                run_dir=run_dir,
                                dataset=dataset,
                                event=pending_exit_event,
                                notifier=notifier,
                            )
                    stop_scan_after = position_snapshot.get("as_of")
                    position_snapshot, pending_execution_event = fill_pending_entry(
                        position_snapshot,
                        _visible_bar_records(visible),
                        as_of=bar_time.isoformat(),
                    )
                    if (
                        isinstance(pending_execution_event, Mapping)
                        and pending_execution_event.get("event_type") == "ENTRY_FILLED"
                        and isinstance(pending_execution_event.get("fill_time"), str)
                    ):
                        fill_at = datetime.fromisoformat(str(pending_execution_event["fill_time"]))
                        stop_scan_after = (fill_at - timedelta(microseconds=1)).isoformat()
                    if pending_execution_event is not None:
                        write_json_atomic(run_dir / "replay-position-state.json", position_snapshot)
                        _append_execution_event_once(
                            run_dir / "execution-events.jsonl",
                            dict(pending_execution_event),
                        )
                        if pending_execution_event.get("event_type") == "ENTRY_FILLED":
                            constitution_snapshot, constitution_lock_event = (
                                apply_program_execution_event(
                                    run_dir / "constitution-state.json",
                                    pending_execution_event,
                                )
                            )
                            if constitution_lock_event is not None:
                                _append_execution_event_once(
                                    run_dir / "execution-events.jsonl",
                                    constitution_lock_event,
                                )
                            await self._record_programmatic_entry_fill(
                                run_dir=run_dir,
                                dataset=dataset,
                                event=pending_execution_event,
                                notifier=notifier,
                            )
                    visible_records = _visible_bar_records(visible)
                    program_behavior_exit_audit = derive_program_behavior_exit_audit(
                        position_snapshot,
                        visible_records,
                        as_of=bar_time.isoformat(),
                    )
                    risk_cutoff = bar_time.isoformat()
                    if (
                        isinstance(program_behavior_exit_audit, Mapping)
                        and program_behavior_exit_audit.get("status") in {"PENDING_FILL", "TRIGGERED"}
                        and isinstance(program_behavior_exit_audit.get("signal_time"), str)
                    ):
                        # A behavior exit is known only at that bar's close and
                        # fills at the next open. Check resting stops through
                        # the signal bar, but never scan intrabar ranges after
                        # the already committed exit fill.
                        risk_cutoff = str(program_behavior_exit_audit["signal_time"])
                    position_snapshot, risk_control_events, protective_stop_audit = advance_program_risk_controls(
                        position_snapshot,
                        visible_records,
                        as_of=risk_cutoff,
                        after=str(stop_scan_after) if stop_scan_after else None,
                        structural_protection_events=_program_protection_events_through(
                            run_dir,
                            through=risk_cutoff,
                        ),
                    )
                    if (
                        isinstance(protective_stop_audit, Mapping)
                        and protective_stop_audit.get("status") == "TRIGGERED"
                    ):
                        # A resting stop is an execution fact known before the
                        # AI turn. Apply it to the formal constitution now so a
                        # third consecutive stop cannot be presented as only a
                        # one-bar re-entry wait on this same cutoff.
                        (
                            constitution_snapshot,
                            program_stop_event,
                            constitution_lock_event,
                        ) = _apply_program_stop_before_ai(
                            run_dir=run_dir,
                            protective_stop_audit=protective_stop_audit,
                            recorded_at=bar_time.isoformat(),
                        )
                    if (
                        isinstance(protective_stop_audit, Mapping)
                        and protective_stop_audit.get("status") == "TRIGGERED"
                        and isinstance(program_behavior_exit_audit, Mapping)
                        and program_behavior_exit_audit.get("status") in {"PENDING_FILL", "TRIGGERED"}
                    ):
                        program_behavior_exit_audit = {
                            **dict(program_behavior_exit_audit),
                            "status": "SUPPRESSED_BY_STOP",
                            "required_position_action": None,
                        }
                    for risk_event in risk_control_events:
                        _append_execution_event_once(
                            run_dir / "execution-events.jsonl",
                            dict(risk_event),
                        )
                        if risk_event.get("event_type") == "STOP_FILLED":
                            constitution_snapshot, constitution_lock_event = (
                                apply_program_execution_event(
                                    run_dir / "constitution-state.json",
                                    risk_event,
                                )
                            )
                            if constitution_lock_event is not None:
                                _append_execution_event_once(
                                    run_dir / "execution-events.jsonl",
                                    constitution_lock_event,
                                )
                    previous_memory = _reconcile_program_reentry_memory(
                        previous_memory,
                        position=position_snapshot,
                        as_of=bar_time.isoformat(),
                    )
        detail_source = visible
        if stage == "day":
            # The preceding night is already represented by the read-only
            # reference snapshot and compact night summary. Feeding another
            # 100+ raw night bars into every day-session explanation both
            # wastes latency and encourages accidental cross-session anchors.
            detail_source = visible[
                visible["bar_time"].map(
                    lambda value: value.date() == dataset.target_date
                    and value.time() >= time(8, 45)
                )
            ]
        detail = detail_source.tail(self.config.detail_bar_count)
        prompt_context = dict(context)
        if stage == "preopen":
            prompt_context["current_unclosed_k_iso"] = None
            prompt_context["current_unclosed_k_hhmm"] = None
            prompt_context["session_status"] = "NIGHT_CLOSED_DAY_NOT_OPEN"
            prompt_context["next_session_open_iso"] = datetime.combine(
                dataset.target_date,
                time(8, 45),
                tzinfo=TAIPEI,
            ).isoformat()
        runtime_context: dict[str, Any] = {
            "replay": {
                "target_date": dataset.target_date.isoformat(),
                "instrument": dataset.instrument,
                "expiry_month": dataset.expiry_month,
                "preopen_snapshot": stage == "preopen",
                "revealed_bar_count": len(visible),
                "future_data_available": False,
                "newly_revealed_bar_count": new_closed_bar_count,
                "detail_bar_count": self.config.detail_bar_count,
            },
            "context": prompt_context,
            "expected_latest_closed_k_iso": context["expected_latest_closed_k_iso"],
            "expected_session_key": expected_session_key,
            "trade_direction_policy": self.rules.trade_direction_policy,
            "structured_market_data_context": _prompt_structured_market_view(
                structured, ai_hybrid=self.ai_hybrid
            ),
            "deterministic_constraints": _prompt_deterministic_constraints(
                structured, stage=stage, ai_hybrid=self.ai_hybrid
            ),
            "night_session_summary": _night_summary(dataset.night_bars),
            "spot_market_context": spot_context(dataset.spot, latest_futures_bar=bar_time),
            "detail_bars": compact_bar_table(detail),
            "overview_bars": compact_completed_overview(
                visible,
                minutes=self.config.overview_minutes,
                available_at=available_at,
            ),
        }
        deterministic_ledger: Mapping[str, Any] | None = None
        deterministic_events: list[dict[str, Any]] = []
        raw_deterministic_events: list[dict[str, Any]] = []
        event_lifecycle_state: Mapping[str, Any] | None = None
        entry_gate: Mapping[str, Any] | None = None
        preentry_invalidation_audit: Mapping[str, Any] | None = None
        retired_setup_keys: set[str] = set()
        if self.deterministic_contract:
            deterministic_ledger = build_evidence_ledger(
                structured,
                bars=_visible_bar_records(visible),
                expected_as_of=context["expected_latest_closed_k_iso"],
                session_key=expected_session_key,
                previous_ledger=previous_ledger,
                anchor_lifecycle_enabled=self.anchor_lifecycle_contract,
                course_chain_enabled=self.course_chain_contract,
                # ``program_only`` is also used for hidden one-minute ticks in
                # a hybrid run. Such a tick may suppress the scheduled AI
                # presentation, but it must not change who owns course
                # interpretation. This matters when a hidden tick becomes
                # ENTRY_ELIGIBLE and is promoted to an immediate AI decision.
                decision_authority=_stage_decision_authority(
                    ai_hybrid=self.ai_hybrid,
                    analyzer=analyzer,
                ),
                trade_direction_policy=self.rules.trade_direction_policy,
                trade_setup_policy=self.rules.trade_setup_policy,
            )
            retired_setup_keys = _retired_setup_keys(
                run_dir,
                previous_memory,
                active_setup_key=(
                    position_snapshot.get("active_setup_key")
                    if isinstance(position_snapshot, Mapping)
                    else None
                ),
            )
            deterministic_ledger = _freeze_continuation_candidate(
                deterministic_ledger,
                previous_ledger=previous_ledger,
                previous_memory=previous_memory,
                retired_setup_keys=retired_setup_keys,
            )
            deterministic_ledger = _suppress_retired_continuation_candidate(
                deterministic_ledger,
                retired_setup_keys=retired_setup_keys,
                as_of=context["expected_latest_closed_k_iso"],
            )
            deterministic_ledger = _suppress_countertrend_continuation_candidate(
                deterministic_ledger,
            )
            deterministic_ledger = _suppress_stale_unseen_continuation_candidate(
                deterministic_ledger,
                previous_memory=previous_memory,
                as_of=context["expected_latest_closed_k_iso"],
            )
            deterministic_ledger = _bind_continuation_candidate_to_available_reentry(
                deterministic_ledger,
                previous_memory=previous_memory,
                position=position_snapshot,
            )
            deterministic_ledger = _suppress_exhausted_reentry_candidate(
                deterministic_ledger,
                previous_memory=previous_memory,
                position=position_snapshot,
            )
            if isinstance(constitution_snapshot, Mapping):
                constitution_requalification_audit = requalification_audit(
                    constitution_snapshot,
                    deterministic_ledger,
                    as_of=context["expected_latest_closed_k_iso"],
                )
                if constitution_requalification_audit.get("eligible") is True:
                    constitution_snapshot, requalified_event = (
                        apply_program_requalification(
                            run_dir / "constitution-state.json",
                            constitution_requalification_audit,
                        )
                    )
                    _append_execution_event_once(
                        run_dir / "execution-events.jsonl",
                        requalified_event,
                    )
                    deterministic_ledger = dict(deterministic_ledger)
                    deterministic_ledger["program_constitution"] = dict(
                        constitution_snapshot
                    )
                    deterministic_ledger[
                        "program_constitution_requalification_audit"
                    ] = dict(constitution_requalification_audit)
                elif constitution_snapshot.get("trading_locked") is True:
                    previous_memory, constitution_retired = (
                        retire_setups_for_constitution_lock(
                            previous_memory,
                            as_of=context["expected_latest_closed_k_iso"],
                        )
                    )
                    retired_setup_keys.update(constitution_retired)
                    deterministic_ledger = suppress_candidate_for_constitution_lock(
                        deterministic_ledger,
                        constitution_snapshot,
                        constitution_requalification_audit,
                    )
                else:
                    deterministic_ledger = dict(deterministic_ledger)
                    deterministic_ledger["program_constitution"] = dict(
                        constitution_snapshot
                    )
                    deterministic_ledger[
                        "program_constitution_requalification_audit"
                    ] = dict(constitution_requalification_audit)
            deterministic_ledger = _filter_ai_hybrid_candidate_inventory(
                deterministic_ledger,
                retired_setup_keys=retired_setup_keys,
                previous_memory=previous_memory,
                as_of=context["expected_latest_closed_k_iso"],
            )
            if self.execution_gate_contract and isinstance(position_snapshot, Mapping):
                levels = deterministic_ledger.get("trade_levels")
                candidate = _ai_hybrid_preentry_invalidation_candidate(
                    deterministic_ledger,
                    previous_ledger=previous_ledger,
                    previous_memory=previous_memory,
                )
                previous_memory, preentry_invalidation_audit = (
                    invalidate_preentry_stop_touched_setup(
                        previous_memory,
                        _visible_bar_records(visible),
                        as_of=context["expected_latest_closed_k_iso"],
                        current_candidate=candidate,
                        position=position_snapshot,
                    )
                )
                if preentry_invalidation_audit.get("status") == "INVALIDATED_BEFORE_ENTRY":
                    deterministic_ledger = _apply_preentry_invalidation_to_ledger(
                        deterministic_ledger,
                        audit=preentry_invalidation_audit,
                    )
                    retired_setup_keys.add(str(preentry_invalidation_audit["setup_key"]))
                    _append_execution_event_once(
                        run_dir / "execution-events.jsonl",
                        {
                            "event_type": "PROGRAM_SETUP_INVALIDATED",
                            **dict(preentry_invalidation_audit),
                        },
                    )
                    deterministic_ledger = _filter_ai_hybrid_candidate_inventory(
                        deterministic_ledger,
                        retired_setup_keys=retired_setup_keys,
                        previous_memory=previous_memory,
                        as_of=context["expected_latest_closed_k_iso"],
                    )
            deterministic_ledger = _annotate_course_cclass_constraint(deterministic_ledger)
            if self.execution_gate_contract and isinstance(position_snapshot, Mapping):
                deterministic_ledger = dict(deterministic_ledger)
                trade_levels = dict(deterministic_ledger.get("trade_levels") or {})
                trade_levels["position_protection_candidates"] = apply_profit_milestone_protection(
                    trade_levels.get("position_protection_candidates"),
                    position=position_snapshot,
                    bars=_visible_bar_records(visible),
                    as_of=context["expected_latest_closed_k_iso"],
                )
                deterministic_ledger["trade_levels"] = trade_levels
                current_behavior_audit = derive_position_behavior_audit(
                    position_snapshot,
                    _visible_bar_records(visible),
                    as_of=context["expected_latest_closed_k_iso"],
                )
                if (
                    isinstance(program_behavior_exit_audit, Mapping)
                    and program_behavior_exit_audit.get("status") in {"PENDING_FILL", "TRIGGERED"}
                    and isinstance(program_behavior_exit_audit.get("behavior_audit"), Mapping)
                ):
                    current_behavior_audit = dict(program_behavior_exit_audit["behavior_audit"])
                deterministic_ledger["position_behavior_audit"] = current_behavior_audit
                deterministic_ledger["protective_stop_audit"] = protective_stop_audit or {
                    "version": 1,
                    "status": "NOT_APPLICABLE",
                    "as_of": context["expected_latest_closed_k_iso"],
                }
                deterministic_ledger["program_behavior_exit_audit"] = (
                    dict(program_behavior_exit_audit)
                    if isinstance(program_behavior_exit_audit, Mapping)
                    else {
                        "version": 1,
                        "status": "NOT_APPLICABLE",
                        "as_of": context["expected_latest_closed_k_iso"],
                    }
                )
                deterministic_ledger["program_reentry_expectation"] = (
                    _program_reentry_expectation(
                        position_snapshot,
                        protective_stop_audit=protective_stop_audit,
                        constitution_snapshot=constitution_snapshot,
                        as_of=context["expected_latest_closed_k_iso"],
                    )
                )
            if _monitoring_segment_changed(previous_ledger, deterministic_ledger):
                previous_memory = empty_semantic_memory(
                    as_of=bar_time.isoformat(),
                    session_key=expected_session_key,
                    version=self.rules.memory_version,
                    anchor_lifecycle=self.anchor_lifecycle_contract,
                )
            raw_deterministic_events = evidence_events(previous_ledger, deterministic_ledger)
            if (
                isinstance(preentry_invalidation_audit, Mapping)
                and preentry_invalidation_audit.get("status") == "INVALIDATED_BEFORE_ENTRY"
            ):
                raw_deterministic_events.append(
                    {
                        "event_type": "PROGRAM_SETUP_INVALIDATED",
                        "event_id": preentry_invalidation_audit.get("event_id"),
                        "event_time": preentry_invalidation_audit.get("invalidated_at"),
                        "recorded_at": context["expected_latest_closed_k_iso"],
                        "setup_key": preentry_invalidation_audit.get("setup_key"),
                        "direction": preentry_invalidation_audit.get("direction"),
                        "reason": preentry_invalidation_audit.get("reason"),
                    }
                )
            deterministic_events = raw_deterministic_events
            if self.execution_gate_contract:
                # Detect a trigger on every newly visible closed bar before
                # pruning stale setups.  With two-minute AI sampling, an armed
                # setup can trigger on its final valid one-minute bar and only
                # become observable at the following analysis cutoff.
                entry_gate = derive_entry_eligibility(
                    previous_memory,
                    _visible_bar_records(visible),
                    as_of=context["expected_latest_closed_k_iso"],
                    position=position_snapshot,
                    current_candidate=(
                        _ai_hybrid_execution_candidate(
                            deterministic_ledger,
                            previous_memory=previous_memory,
                        )
                    ),
                    blocked_setup_keys=_blocked_program_setup_keys(deterministic_ledger),
                )
                protected_setup_key = (
                    position_snapshot.get("active_setup_key")
                    if isinstance(position_snapshot, Mapping)
                    and position_snapshot.get("status") in {"LONG", "SHORT"}
                    else entry_gate.get("setup_key")
                    if isinstance(entry_gate, Mapping)
                    and entry_gate.get("status") == "ENTRY_ELIGIBLE"
                    else None
                )
                previous_memory = expire_stale_armed_setups(
                    previous_memory,
                    armed_at_by_key=_armed_setup_first_seen(run_dir, previous_memory),
                    as_of=context["expected_latest_closed_k_iso"],
                    protected_setup_key=protected_setup_key,
                    structural_setup_keys=_structural_ai_candidate_keys(
                        deterministic_ledger
                    ),
                )
                # Expiry is evaluated after the entry gate so a trigger on the
                # final valid minute can still survive sparse sampling.  When
                # no trigger survives, synchronize the deterministic candidate
                # with the newly terminal memory immediately; otherwise the
                # semantic contract is forced to arm a setup that the execution
                # layer has already retired.
                retired_setup_keys.update(_terminal_setup_keys(previous_memory))
                deterministic_ledger = _suppress_retired_continuation_candidate(
                    deterministic_ledger,
                    retired_setup_keys=retired_setup_keys,
                    as_of=context["expected_latest_closed_k_iso"],
                )
                deterministic_ledger = _filter_ai_hybrid_candidate_inventory(
                    deterministic_ledger,
                    retired_setup_keys=retired_setup_keys,
                    previous_memory=previous_memory,
                    as_of=context["expected_latest_closed_k_iso"],
                )
                saved_event_state = read_json(run_dir / "deterministic-event-state.json")
                event_lifecycle_state, deterministic_events = build_event_lifecycle(
                    saved_event_state if isinstance(saved_event_state, Mapping) else None,
                    raw_deterministic_events,
                    as_of=context["expected_latest_closed_k_iso"],
                )
            runtime_context.update(
                {
                    "deterministic_evidence_ledger": _prompt_ledger_view(
                        deterministic_ledger, ai_hybrid=self.ai_hybrid
                    ),
                    "deterministic_evidence_events": deterministic_events,
                    "previous_semantic_memory": previous_memory,
                    "ai_hybrid_state_continuity_lock": (
                        _ai_hybrid_state_continuity_lock(
                            previous_memory,
                            deterministic_events,
                            ledger=deterministic_ledger,
                        )
                        if self.ai_hybrid
                        else None
                    ),
                    "simulated_position_state": position_snapshot,
                    "deterministic_constraints": _prompt_deterministic_constraints(
                        structured,
                        stage=stage,
                        ledger=deterministic_ledger,
                        ai_hybrid=self.ai_hybrid,
                    ),
                }
            )
            if self.execution_gate_contract:
                runtime_context.update(
                    {
                    "deterministic_event_lifecycle": _prompt_event_lifecycle_view(
                        event_lifecycle_state
                    ),
                    "entry_eligibility": entry_gate,
                    "retired_setup_keys": sorted(retired_setup_keys),
                }
                )
            if (
                self.ai_hybrid
                and program_only
                and not emit_output
                and analyzer is not None
                and _requires_ai_hybrid_off_cadence_decision(entry_gate)
            ):
                # A causal entry trigger cannot be decided by the hidden
                # program-only minute.  Insert one AI turn immediately instead
                # of delaying or silently accepting/rejecting it at the next
                # two-bar presentation cutoff.
                program_only = False
        prior_contract_errors = _recent_validation_errors(
            run_dir / "validation-errors.jsonl",
            stage=stage,
            bar_time=bar_time,
        )
        if prior_contract_errors:
            runtime_context["prior_contract_validation_errors"] = prior_contract_errors
        if self.full_fidelity:
            runtime_context.update(
                {
                    "previous_analysis_summary": previous_analysis_summary,
                    "market_structure_state": previous_market_structure,
                    "constitution_state": constitution_snapshot,
                }
            )
        elif not self.deterministic_contract:
            runtime_context["previous_replay_memory"] = previous_memory
        prompt_dir = run_dir / "prompts"
        output_dir = run_dir / "analysis"
        prompt_dir.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)
        key = f"{stage}-{bar_time.strftime('%Y%m%d-%H%M')}"
        artifact_key = _next_artifact_key(prompt_dir, key)
        correction: str | None = None
        last_error: Exception | None = None
        retry_decision_lock: str | None = None
        attempt_decision: str | None = None
        bulk_saved_raws: list[Path] = []
        if _saved_raw_revalidation_enabled(
            reuse_saved_raw=self._reuse_saved_raw,
            program_only=program_only,
        ):
            candidates = list(output_dir.glob(f"{key}*-attempt-*-raw.txt"))
            if not candidates:
                raise ReplayRunError(
                    "reuse_saved_raw_missing",
                    f"{bar_time.strftime('%H:%M')}找不到先前保存的因果AI原始輸出。",
                )
            bulk_saved_raws = sorted(
                candidates,
                key=lambda path: path.stat().st_mtime_ns,
                reverse=True,
            )
        revalidation_mode = self._revalidate_raw_once is not None or bool(bulk_saved_raws)
        attempt_limit = (
            1
            if program_only or self._revalidate_raw_once is not None
            else len(bulk_saved_raws)
            if bulk_saved_raws
            else self.config.max_ai_attempts
        )
        for attempt in range(1, attempt_limit + 1):
            attempt_decision = None
            saved_raw_for_attempt = self._revalidate_raw_once or (
                bulk_saved_raws[attempt - 1] if bulk_saved_raws else None
            )
            analysis_source = (
                "saved_raw"
                if saved_raw_for_attempt is not None
                else "program"
                if program_only
                else f"{self.config.ai_provider}_live"
            )
            try:
                if saved_raw_for_attempt is not None:
                    saved_raw_path = saved_raw_for_attempt
                    if self._revalidate_raw_once is not None:
                        self._revalidate_raw_once = None
                        artifact_key = re.sub(r"-attempt-\d+-raw$", "", saved_raw_path.stem)
                    raw_text = saved_raw_path.read_text(encoding="utf-8")
                    parsed, normalization_stats = normalize_tool_arguments(parse_json_object(raw_text))
                    generated = MiniMaxReplayResult(
                        payload=parsed,
                        raw_text=raw_text,
                        diagnostics={
                            "model": self.config.ai_model,
                            "source": "saved_raw",
                            "normalized_array_wrappers": normalization_stats["array_wrappers"],
                            "normalized_null_fields": normalization_stats["null_fields"],
                            "normalized_trailing_serialization_suffixes": normalization_stats[
                                "trailing_serialization_suffixes"
                            ],
                        },
                    )
                elif program_only:
                    if not isinstance(deterministic_ledger, Mapping) or not isinstance(position_snapshot, Mapping):
                        raise ReplayRunError(
                            "program_state_missing",
                            "程式分析需要確定性證據與模擬持倉狀態。",
                        )
                    program_payload = build_program_semantic_envelope(
                        ledger=deterministic_ledger,
                        expected_as_of=context["expected_latest_closed_k_iso"],
                        expected_session_key=expected_session_key,
                        preopen=stage == "preopen",
                        position=position_snapshot,
                        previous_memory=previous_memory,
                        entry_gate=entry_gate,
                        evidence_events=deterministic_events,
                    )
                    raw_text = json.dumps(program_payload, ensure_ascii=False, indent=2)
                    generated = MiniMaxReplayResult(
                        payload=program_payload,
                        raw_text=raw_text,
                        diagnostics={
                            "provider": "program",
                            "model": "deterministic-course-engine",
                            "normalized_array_wrappers": 0,
                        },
                    )
                    (output_dir / f"{artifact_key}-attempt-{attempt}-raw.txt").write_text(
                        raw_text,
                        encoding="utf-8",
                    )
                else:
                    if analyzer is None:
                        raise ReplayRunError("analyzer_missing", "AI分析器尚未初始化。")
                    prompt = build_replay_prompt(
                        rules_text=self.rules.model_rules,
                        schema_text=self.rules.schema_text,
                        runtime_context=runtime_context,
                        correction=correction,
                        full_fidelity=self.full_fidelity,
                        deterministic_contract=self.deterministic_contract,
                        course_chain_contract=self.course_chain_contract,
                        ai_hybrid=self.ai_hybrid,
                        trade_direction_policy=self.rules.trade_direction_policy,
                        trade_setup_policy=self.rules.trade_setup_policy,
                    )
                    (prompt_dir / f"{artifact_key}-attempt-{attempt}.txt").write_text(prompt, encoding="utf-8")
                    generated = analyzer.analyze(prompt)
                    (output_dir / f"{artifact_key}-attempt-{attempt}-raw.txt").write_text(generated.raw_text, encoding="utf-8")
                    append_jsonl(
                        run_dir / "ai-usage.jsonl",
                        {
                            "stage": stage,
                            "bar_time": bar_time.isoformat(),
                            "attempt": attempt,
                            **generated.diagnostics,
                        },
                    )
                    retry_decision_lock, attempt_decision = _register_retry_decision(
                        retry_decision_lock,
                        generated.payload,
                        position=position_snapshot,
                    )
                if self.deterministic_contract and isinstance(deterministic_ledger, Mapping):
                    normalized_roles = _normalize_session_open_role_labels(
                        generated.payload,
                        ledger=deterministic_ledger,
                    )
                    if normalized_roles:
                        generated.diagnostics["normalized_session_open_roles"] = normalized_roles
                expected = context["expected_latest_closed_k_iso"]
                material_reasons: list[str] = []
                constitution_result: dict[str, Any] | None = None
                if self.full_fidelity:
                    payload, material_reasons = validate_full_fidelity_payload(
                        generated.payload,
                        previous_market_structure=previous_market_structure,
                        expected_as_of=expected,
                        expected_session_key=expected_session_key,
                        preopen=stage == "preopen",
                    )
                    envelope = payload
                elif self.deterministic_contract:
                    if deterministic_ledger is None or position_snapshot is None:
                        raise ReplayRunError("deterministic_state_missing", "程式證據或模擬持倉狀態遺失。")
                    envelope = validate_semantic_envelope(
                        generated.payload,
                        ledger=deterministic_ledger,
                        expected_as_of=expected,
                        expected_session_key=expected_session_key,
                        preopen=stage == "preopen",
                        position=position_snapshot,
                        evidence_events=deterministic_events,
                        previous_memory=previous_memory,
                        entry_gate=entry_gate,
                        ai_generated=analysis_source != "program",
                    )
                    _validate_retired_setup_keys(
                        envelope,
                        retired_setup_keys=retired_setup_keys,
                    )
                    payload = envelope["analysis"]
                else:
                    envelope = validate_replay_envelope(
                        generated.payload,
                        expected_as_of=expected,
                        expected_session_key=expected_session_key,
                    )
                    payload = envelope["analysis"]
                    _validate_replay_semantics(envelope, structured=structured, stage=stage)
                _validate_available_data_references(
                    {
                        key: value
                        for key, value in payload.items()
                        if key not in {"constitution_event", "market_structure_state"}
                    },
                    spot_status=str(runtime_context["spot_market_context"].get("status") or "UNAVAILABLE"),
                )
                if self.deterministic_contract:
                    _validate_latest_ohlc_claims(
                        payload,
                        latest=(deterministic_ledger or {}).get("latest_closed_k"),
                    )
                _validate_grounded_level_claims(
                    payload,
                    structured=structured,
                    night_summary=runtime_context["night_session_summary"],
                    ledger=deterministic_ledger,
                )
                if payload.get("message_type"):
                    _apply_required_profit_protection(
                        payload,
                        ledger=deterministic_ledger or {},
                        position=position_snapshot or {},
                    )
                    _validate_v3_obstacles(
                        payload,
                        ledger=deterministic_ledger or {},
                        night_summary=runtime_context["night_session_summary"],
                        position=position_snapshot,
                    )
                    _validate_v3_trade_levels(
                        payload,
                        ledger=deterministic_ledger or {},
                        position=position_snapshot,
                    )
                decision_promoted = _apply_replay_delivery_policy(payload, stage=stage)
                decision_suppressed_by_exit_fill = (
                    _suppress_redundant_analysis_after_exit_fill(
                        payload,
                        exit_fill_event=pending_exit_event,
                    )
                )
                decision = payload["original_decision"]
                force_event_output = _requires_forced_trade_event_output(
                    entry_gate=entry_gate,
                    protective_stop_audit=protective_stop_audit,
                    program_behavior_exit_audit=program_behavior_exit_audit,
                )
                semantic_memory_source = "validated_analysis"
                semantic_memory_to_save: Mapping[str, Any] | None = (
                    envelope.get("memory") if isinstance(envelope, Mapping) else None
                )
                if self.deterministic_contract:
                    semantic_memory_to_save, semantic_memory_source = (
                        _hybrid_hidden_tick_memory_for_persistence(
                            validated_memory=semantic_memory_to_save,
                            previous_ai_memory=previous_memory,
                            ai_hybrid=self.ai_hybrid,
                            program_only=program_only,
                            forced_trade_event=force_event_output,
                        )
                    )
                position_after: Mapping[str, Any] | None = None
                if self.deterministic_contract:
                    latest = structured.get("latest_closed_k")
                    if not isinstance(latest, Mapping):
                        raise ReplayRunError("latest_bar_missing", "最新已收盤K遺失。")
                    if (
                        self.execution_gate_contract
                        and isinstance(entry_gate, Mapping)
                        and entry_gate.get("status") == "ENTRY_ELIGIBLE"
                        and payload.get("action", {}).get("position_action") == "ENTER"
                    ):
                        position_after = schedule_pending_entry(
                            position_snapshot or {},
                            payload,
                            entry_gate,
                            as_of=expected,
                        )
                    elif (
                        self.execution_gate_contract
                        and isinstance(program_behavior_exit_audit, Mapping)
                        and program_behavior_exit_audit.get("status") == "PENDING_FILL"
                        and payload.get("action", {}).get("position_action") == "EXIT"
                    ):
                        position_after = schedule_pending_exit(
                            position_snapshot or {},
                            payload,
                            program_behavior_exit_audit,
                            as_of=expected,
                        )
                    elif (
                        self.execution_gate_contract
                        and self.long_only
                        and (position_snapshot or {}).get("status") in {"LONG", "SHORT"}
                        and payload.get("action", {}).get("position_action") == "EXIT"
                    ):
                        analysis_exit_audit = derive_analysis_exit_audit(
                            position_snapshot or {},
                            payload,
                            as_of=expected,
                        )
                        position_after = schedule_pending_exit(
                            position_snapshot or {},
                            payload,
                            analysis_exit_audit,
                            as_of=expected,
                        )
                    else:
                        position_after = preview_position_transition(
                            position_snapshot or {},
                            payload,
                            as_of=expected,
                            latest_close=float(latest["close"]),
                            preopen=stage == "preopen",
                            protective_stop_audit=protective_stop_audit,
                        )
                    if (
                        self.ai_hybrid
                        and program_only
                        and isinstance(protective_stop_audit, Mapping)
                        and protective_stop_audit.get("status") == "TRIGGERED"
                        and payload.get("action", {}).get("position_action") == "STOP"
                    ):
                        rendered = render_programmatic_stop_fill(protective_stop_audit)
                    elif (
                        self.ai_hybrid
                        and program_only
                        and isinstance(program_behavior_exit_audit, Mapping)
                        and program_behavior_exit_audit.get("status") == "PENDING_FILL"
                        and payload.get("action", {}).get("position_action") == "EXIT"
                    ):
                        rendered = render_programmatic_exit_signal(program_behavior_exit_audit)
                    else:
                        rendered = render_semantic_replay_event_card(
                            payload,
                            context,
                            stage=stage,
                            ledger=deterministic_ledger or {},
                            position_before=position_snapshot or {},
                            position_after=position_after,
                            previous_memory=previous_memory,
                        )
                    body = rendered.body
                    message_kind = rendered.kind
                elif self.config.message_profile == "course_event_cards":
                    rendered = render_replay_event_card(
                        payload,
                        context,
                        stage=stage,
                        constitution_snapshot=constitution_snapshot,
                    )
                    body = rendered.body
                    message_kind = rendered.kind
                else:
                    body = (
                        render_analysis_markdown(payload, context, resumed=False)
                        if decision == "NOTIFY"
                        else render_quiet_status_markdown(payload, context)
                    )
                    message_kind = "FORMAL_NINE_SECTIONS"
                message = _replay_message(
                    body=body,
                    dataset=dataset,
                    bar_time=bar_time,
                    stage=stage,
                )
                if self.full_fidelity:
                    constitution_result = apply_constitution_event(
                        run_dir / "constitution-state.json",
                        payload["constitution_event"],
                        expected_latest_closed_bar_time=expected,
                    )
                    summary = analysis_state_summary(payload)
                    write_json_atomic(
                        run_dir / "market-structure-state.json",
                        payload["market_structure_state"],
                    )
                    write_json_atomic(run_dir / "last-analysis-summary.json", summary)
                elif self.deterministic_contract:
                    summary = _semantic_analysis_summary(payload, deterministic_ledger or {})
                    write_json_atomic(run_dir / "deterministic-evidence-ledger.json", deterministic_ledger)
                    if self.anchor_lifecycle_contract:
                        anchor_state = deterministic_ledger.get("anchor_lifecycle")
                        if not isinstance(anchor_state, Mapping):
                            raise ReplayRunError("anchor_lifecycle_missing", "錨生命週期狀態遺失。")
                        write_json_atomic(run_dir / "anchor-lifecycle-state.json", anchor_state)
                    write_json_atomic(run_dir / "replay-position-state.json", position_after)
                    if (
                        isinstance(program_behavior_exit_audit, Mapping)
                        and program_behavior_exit_audit.get("status") == "TRIGGERED"
                        and payload.get("action", {}).get("position_action") == "EXIT"
                    ):
                        program_exit_event = {
                            "event_type": "EXIT_FILLED",
                            "event_id": program_behavior_exit_audit.get("event_id"),
                            "setup_key": program_behavior_exit_audit.get("setup_key"),
                            "direction": program_behavior_exit_audit.get("direction"),
                            "entry_time": program_behavior_exit_audit.get("entry_time"),
                            "entry_price": program_behavior_exit_audit.get("entry_price"),
                            "signal_time": program_behavior_exit_audit.get("signal_time"),
                            "fill_time": program_behavior_exit_audit.get("fill_time"),
                            "fill_price": program_behavior_exit_audit.get("fill_price"),
                            "reason_code": program_behavior_exit_audit.get("reason_code"),
                            "recorded_at": expected,
                        }
                        _append_execution_event_once(
                            run_dir / "execution-events.jsonl",
                            program_exit_event,
                        )
                        constitution_snapshot, constitution_lock_event = (
                            apply_program_execution_event(
                                run_dir / "constitution-state.json",
                                program_exit_event,
                            )
                        )
                        if constitution_lock_event is not None:
                            _append_execution_event_once(
                                run_dir / "execution-events.jsonl",
                                constitution_lock_event,
                            )
                    for evidence_event in raw_deterministic_events:
                        append_jsonl(run_dir / "deterministic-evidence-events.jsonl", evidence_event)
                    if self.execution_gate_contract and isinstance(event_lifecycle_state, Mapping):
                        # Hidden one-minute ticks must persist newly observed
                        # events without consuming them.  Otherwise the next
                        # scheduled AI turn rebuilds an empty ledger diff and
                        # never sees the intervening structure change.
                        if emit_output or force_event_output:
                            event_lifecycle_state = mark_events_analyzed(
                                event_lifecycle_state,
                                [
                                    str(item.get("event_id"))
                                    for item in deterministic_events
                                    if item.get("event_id")
                                ],
                                analyzed_at=expected,
                            )
                        write_json_atomic(
                            run_dir / "deterministic-event-state.json",
                            event_lifecycle_state,
                        )
                else:
                    summary = analysis_state_summary(payload)
                event_id = replay_event_id(
                    rule_version=self.rules.version,
                    instrument=dataset.instrument,
                    target_date=dataset.target_date.isoformat(),
                    bar_time=bar_time.isoformat(),
                    decision=decision,
                    message=message,
                )
                delivery: dict[str, Any] = {"ok": True, "status": "not_requested", "chunk_count": 0}
                quiet_delivery_due = decision == "NOTIFY" or _quiet_status_delivery_due(
                    run_dir / "deliveries.jsonl",
                    bar_time=bar_time,
                    interval_minutes=self.config.quiet_status_interval_minutes,
                )
                should_send = (
                    notifier is not None
                    and (emit_output or force_event_output)
                    and (
                        decision == "NOTIFY"
                        or (self.config.send_quiet_status and quiet_delivery_due)
                    )
                )
                if should_send:
                    delivery = await notifier.send(event_id=event_id, message=message)
                    append_jsonl(
                        run_dir / "deliveries.jsonl",
                        {
                            "stage": stage,
                            "bar_time": bar_time.isoformat(),
                            "event_id": event_id,
                            "ok": delivery.get("ok"),
                            "status": delivery.get("status"),
                            "chunk_count": delivery.get("chunk_count"),
                            "message_ids": delivery.get("message_ids") or [],
                            "error_code": delivery.get("error_code"),
                        },
                    )
                elif notifier is not None and decision != "NOTIFY" and self.config.send_quiet_status:
                    delivery = {
                        "ok": True,
                        "status": "quiet_throttled",
                        "chunk_count": 0,
                        "interval_minutes": self.config.quiet_status_interval_minutes,
                    }
                result = {
                    "stage": stage,
                    "bar_time": bar_time.isoformat(),
                    "decision": decision,
                    "event_id": event_id,
                    "message": message,
                    "message_kind": message_kind,
                    "delivery": delivery,
                    "analysis_summary": summary,
                    "attempts": 0 if analysis_source in {"saved_raw", "program"} else attempt,
                    "analysis_source": analysis_source,
                    "scheduled_output": emit_output,
                    "forced_trade_event_output": force_event_output,
                    "decision_promoted": decision_promoted,
                    "decision_suppressed_by_exit_fill": decision_suppressed_by_exit_fill,
                    "semantic_memory_source": semantic_memory_source,
                    "material_notification_reasons": material_reasons,
                    "constitution_status": (
                        None if constitution_result is None else constitution_result.get("status")
                    ),
                }
                append_jsonl(run_dir / "events.jsonl", {key: value for key, value in result.items() if key != "message"})
                # Internal one-minute program ticks are audit checkpoints, not
                # user-facing cards.  Saving them in messages.jsonl made
                # ``deliver_latest`` capable of selecting an unscheduled hidden
                # tick.  Only presentation points and forced trade events own a
                # deliverable canonical message.
                if emit_output or force_event_output:
                    append_jsonl(
                        run_dir / "messages.jsonl",
                        {
                            "stage": stage,
                            "bar_time": bar_time.isoformat(),
                            "event_id": event_id,
                            "decision": decision,
                            "message_kind": message_kind,
                            "message": message,
                            "scheduled_output": emit_output,
                            "forced_trade_event_output": force_event_output,
                        },
                    )
                validated_path = output_dir / f"{artifact_key}-validated.json"
                write_json_atomic(validated_path, envelope)
                if self.full_fidelity:
                    checkpoint = {
                        "market_structure_state": payload["market_structure_state"],
                        "last_analysis_summary": summary,
                        "constitution_state": read_json(run_dir / "constitution-state.json"),
                    }
                    write_json_atomic(
                        output_dir / f"{artifact_key}-state-checkpoint.json",
                        checkpoint,
                    )
                elif self.deterministic_contract:
                    write_json_atomic(memory_path, semantic_memory_to_save)
                    write_json_atomic(
                        output_dir / f"{artifact_key}-state-checkpoint.json",
                        {
                            "deterministic_evidence_ledger": deterministic_ledger,
                            "anchor_lifecycle_state": (
                                deterministic_ledger.get("anchor_lifecycle")
                                if self.anchor_lifecycle_contract
                                else None
                            ),
                            "deterministic_event_lifecycle": event_lifecycle_state,
                            "entry_eligibility": entry_gate,
                            "semantic_memory": semantic_memory_to_save,
                            "position_state": position_after,
                            "constitution_state": read_json(
                                run_dir / "constitution-state.json"
                            ),
                        },
                    )
                else:
                    write_json_atomic(memory_path, envelope["memory"])
                if self.ai_hybrid and analysis_source != "program":
                    raw_analysis = generated.payload.get("analysis")
                    comparison = build_semantic_comparison(
                        raw_analysis=(
                            raw_analysis if isinstance(raw_analysis, Mapping) else {}
                        ),
                        validated_analysis=payload,
                        ledger=deterministic_ledger or {},
                        evidence_events=deterministic_events,
                        entry_gate=entry_gate,
                        stage=stage,
                        bar_time=expected,
                        artifact_key=artifact_key,
                        analysis_source=analysis_source,
                    )
                    append_jsonl(
                        run_dir / "semantic-comparisons.jsonl",
                        comparison,
                    )
                if analysis_source == "saved_raw":
                    append_jsonl(
                        run_dir / "revalidations.jsonl",
                        {
                            "stage": stage,
                            "bar_time": bar_time.isoformat(),
                            "status": "accepted",
                            "source_file": saved_raw_path.name,
                            "validated_at": datetime.now(TAIPEI).isoformat(),
                        },
                    )
                return result
            except Exception as exc:
                last_error = exc
                correction = _safe_error(exc) + _retry_decision_instruction(
                    retry_decision_lock
                )
                append_jsonl(
                    run_dir / "validation-errors.jsonl",
                    {
                        "stage": stage,
                        "bar_time": bar_time.isoformat(),
                        "attempt": attempt,
                        "source": analysis_source,
                        "error_code": getattr(exc, "code", "validation_failed"),
                        "error": _safe_error(exc),
                        "retry_decision_lock": retry_decision_lock,
                        "attempt_decision": attempt_decision,
                    },
                )
        if revalidation_mode:
            raise ReplayRunError(
                getattr(last_error, "code", "saved_raw_validation_failed"),
                f"保存的AI原始輸出重新驗證未通過：{_safe_error(last_error)}",
            )
        if analysis_source == "program":
            raise ReplayRunError(
                getattr(last_error, "code", "program_validation_failed"),
                f"程式在{bar_time.strftime('%H:%M')}輸出未通過：{_safe_error(last_error)}",
            )
        raise ReplayRunError(
            getattr(last_error, "code", "ai_validation_failed"),
            f"AI在{bar_time.strftime('%H:%M')}輸出未通過：{_safe_error(last_error)}",
        )

    async def _record_programmatic_entry_fill(
        self,
        *,
        run_dir: Path,
        dataset: ReplayDataset,
        event: Mapping[str, Any],
        notifier: ReplayNotifier | None,
    ) -> dict[str, Any]:
        fill_time = datetime.fromisoformat(str(event.get("fill_time")))
        rendered = render_programmatic_entry_fill(event)
        message = _replay_message(
            body=rendered.body,
            dataset=dataset,
            bar_time=fill_time,
            stage="day",
        )
        event_id = replay_event_id(
            rule_version=self.rules.version,
            instrument=dataset.instrument,
            target_date=dataset.target_date.isoformat(),
            bar_time=fill_time.isoformat(),
            decision="NOTIFY",
            message=message,
        )
        delivery: dict[str, Any] = {"ok": True, "status": "not_requested", "chunk_count": 0}
        if notifier is not None:
            delivery = await notifier.send(event_id=event_id, message=message)
            append_jsonl(
                run_dir / "deliveries.jsonl",
                {
                    "stage": "day",
                    "bar_time": fill_time.isoformat(),
                    "event_id": event_id,
                    "ok": delivery.get("ok"),
                    "status": delivery.get("status"),
                    "chunk_count": delivery.get("chunk_count"),
                    "message_ids": delivery.get("message_ids") or [],
                    "error_code": delivery.get("error_code"),
                    "source": "programmatic_entry_fill",
                },
            )
        result = {
            "stage": "day",
            "bar_time": fill_time.isoformat(),
            "decision": "NOTIFY",
            "event_id": event_id,
            "message_kind": rendered.kind,
            "delivery": delivery,
            "analysis_source": "programmatic_entry_fill",
        }
        append_jsonl(run_dir / "events.jsonl", result)
        append_jsonl(
            run_dir / "messages.jsonl",
            {
                "stage": "day",
                "bar_time": fill_time.isoformat(),
                "event_id": event_id,
                "decision": "NOTIFY",
                "message_kind": rendered.kind,
                "message": message,
                "source": "programmatic_entry_fill",
            },
        )
        return result

    async def _record_programmatic_exit_fill(
        self,
        *,
        run_dir: Path,
        dataset: ReplayDataset,
        event: Mapping[str, Any],
        notifier: ReplayNotifier | None,
    ) -> dict[str, Any]:
        fill_time = datetime.fromisoformat(str(event.get("fill_time")))
        rendered = render_programmatic_exit_fill(event)
        message = _replay_message(
            body=rendered.body,
            dataset=dataset,
            bar_time=fill_time,
            stage="day",
        )
        event_id = replay_event_id(
            rule_version=self.rules.version,
            instrument=dataset.instrument,
            target_date=dataset.target_date.isoformat(),
            bar_time=fill_time.isoformat(),
            decision="NOTIFY",
            message=message,
        )
        delivery: dict[str, Any] = {"ok": True, "status": "not_requested", "chunk_count": 0}
        if notifier is not None:
            delivery = await notifier.send(event_id=event_id, message=message)
            append_jsonl(
                run_dir / "deliveries.jsonl",
                {
                    "stage": "day",
                    "bar_time": fill_time.isoformat(),
                    "event_id": event_id,
                    "ok": delivery.get("ok"),
                    "status": delivery.get("status"),
                    "chunk_count": delivery.get("chunk_count"),
                    "message_ids": delivery.get("message_ids") or [],
                    "error_code": delivery.get("error_code"),
                    "source": "programmatic_exit_fill",
                },
            )
        result = {
            "stage": "day",
            "bar_time": fill_time.isoformat(),
            "decision": "NOTIFY",
            "event_id": event_id,
            "message_kind": rendered.kind,
            "delivery": delivery,
            "analysis_source": "programmatic_exit_fill",
        }
        append_jsonl(run_dir / "events.jsonl", result)
        append_jsonl(
            run_dir / "messages.jsonl",
            {
                "stage": "day",
                "bar_time": fill_time.isoformat(),
                "event_id": event_id,
                "decision": "NOTIFY",
                "message_kind": rendered.kind,
                "message": message,
                "source": "programmatic_exit_fill",
            },
        )
        return result

    def _get_analyzer(self) -> Any:
        if self._analyzer is not None:
            return self._analyzer
        if self.config.ai_provider == "codex":
            self._analyzer = CodexReplayAnalyzer(
                model=self.config.ai_model,
                reasoning_effort=self.config.codex_reasoning_effort,
                timeout_seconds=self.config.ai_timeout_seconds,
                output_schema=self.rules.schema,
                executable=self.config.codex_executable,
                persistent_session=self.ai_hybrid,
                max_session_turns=self.config.codex_max_session_turns,
            )
            return self._analyzer
        research = load_research_config(PROJECT_ROOT)
        if not research.minimax_api_key:
            raise ReplayRunError("minimax_key_missing", "MiniMax API Key尚未設定。")
        self._analyzer = MiniMaxReplayAnalyzer(
            api_key=research.minimax_api_key,
            model=self.config.ai_model or research.minimax_model,
            base_url=research.minimax_base_url,
            timeout_seconds=self.config.ai_timeout_seconds,
            max_output_tokens=self.config.max_output_tokens,
            output_schema=self.rules.schema,
        )
        return self._analyzer

    def _get_notifier(self, run_dir: Path) -> ReplayNotifier:
        return self._notifier_for_state(run_dir / "delivery-state.json")

    def _notifier_for_state(self, state_path: Path) -> ReplayNotifier:
        if not self.config.telegram_enabled:
            raise ReplayRunError("replay_telegram_disabled", "模擬Telegram尚未啟用。")
        if not self.config.telegram_chat_id:
            raise ReplayRunError("replay_chat_missing", "模擬群組ID未設定；不得回退正式群組。")
        token = load_bot_token(PROJECT_ROOT / "config.json")
        return ReplayNotifier(
            bot_token=token,
            chat_id=self.config.telegram_chat_id,
            state_path=state_path,
        )

    def _save_manifest(self, run_dir: Path, manifest: dict[str, Any]) -> None:
        manifest["updated_at"] = datetime.now(TAIPEI).isoformat()
        write_json_atomic(run_dir / "manifest.json", manifest)

    @staticmethod
    def _result(run_dir: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "ok": manifest.get("status") in {"completed", "paused"},
            "status": manifest.get("status"),
            "run_id": run_dir.name,
            "target_date": manifest.get("target_date"),
            "instrument": manifest.get("instrument"),
            "preopen_completed": manifest.get("preopen_completed"),
            "completed_day_bars": manifest.get("completed_day_bars"),
            "completed_program_ticks": manifest.get(
                "completed_program_ticks",
                manifest.get("next_day_index"),
            ),
            "completed_presentation_bars": manifest.get(
                "completed_presentation_bars",
                manifest.get("completed_day_bars"),
            ),
            "selected_day_bars": len(manifest.get("selected_bar_times") or []),
            "presentation_day_bars": len(
                manifest.get("presentation_bar_times")
                or manifest.get("selected_bar_times")
                or []
            ),
            "ai_provider": manifest.get("ai_provider", "minimax"),
            "ai_model": manifest.get("ai_model"),
            "ai_call_count": manifest.get("ai_call_count", manifest.get("minimax_call_count")),
            "minimax_call_count": manifest.get("minimax_call_count"),
            "analysis_mode": manifest.get("analysis_mode", "ai_assisted"),
            "trade_direction_policy": manifest.get("trade_direction_policy", "BOTH"),
            "trade_setup_policy": manifest.get("trade_setup_policy", "ALL"),
            "notification_count": manifest.get("notification_count"),
            "run_directory": str(run_dir),
        }


def _build_context(
    *,
    run_dir: Path,
    bar_time: datetime,
    available_at: datetime,
    stage: str,
    new_closed_bar_count: int,
) -> dict[str, Any]:
    material = "\n".join((run_dir.name, stage, bar_time.isoformat()))
    context_id = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return {
        "context_id": context_id,
        "capture_status": "FRESH",
        "captured_at": available_at.isoformat(),
        "expected_latest_closed_k_iso": bar_time.isoformat(),
        "expected_latest_closed_k_hhmm": bar_time.strftime("%H:%M"),
        "current_unclosed_k_iso": available_at.isoformat(),
        "current_unclosed_k_hhmm": available_at.strftime("%H:%M"),
        "new_closed_bar_count": new_closed_bar_count,
        "image_sha256": hashlib.sha256(material.encode("utf-8")).hexdigest(),
        "image_path": "historical-structured-data-only",
        "unchanged_image_seconds": 0,
    }


def _structured_context(
    *,
    run_dir: Path,
    visible: pd.DataFrame,
    instrument: str,
    expected: datetime,
) -> dict[str, Any]:
    snapshot_path = run_dir / "structured-market-data.json"
    payload = {
        "version": 1,
        "source": "TAIFEX historical daily time-and-sales causal replay",
        "instrument": instrument,
        "timezone": "Asia/Taipei",
        "bars": [
            {
                "time": row.bar_time.isoformat(),
                "open": _number(row.open),
                "high": _number(row.high),
                "low": _number(row.low),
                "close": _number(row.close),
                "volume": _number(row.volume),
            }
            for row in visible.itertuples(index=False)
        ],
    }
    write_json_atomic(snapshot_path, payload)
    result = load_structured_market_snapshot(
        snapshot_path,
        expected_latest_closed_k_iso=expected.isoformat(),
        max_age_seconds=90,
    )
    if result.get("status") != "FRESH":
        raise ReplayRunError("structured_context_invalid", str(result.get("reason") or "結構化行情驗證失敗"))
    return result


def _monitoring_segment_changed(
    previous: Mapping[str, Any] | None,
    current: Mapping[str, Any] | None,
) -> bool:
    if not isinstance(previous, Mapping) or not isinstance(current, Mapping):
        return False
    old = previous.get("monitoring_session")
    new = current.get("monitoring_session")
    if not isinstance(old, Mapping) or not isinstance(new, Mapping):
        return False
    return str(old.get("start") or "") != str(new.get("start") or "")


def _visible_bar_records(visible: pd.DataFrame) -> list[dict[str, Any]]:
    return [
        {
            "time": row.bar_time.isoformat(),
            "open": _number(row.open),
            "high": _number(row.high),
            "low": _number(row.low),
            "close": _number(row.close),
            "volume": _number(row.volume),
        }
        for row in visible.itertuples(index=False)
    ]


def _semantic_analysis_summary(
    payload: Mapping[str, Any],
    ledger: Mapping[str, Any],
) -> dict[str, Any]:
    reading = payload.get("course_reading")
    reading = reading if isinstance(reading, Mapping) else {}
    scenario = payload.get("scenario")
    scenario = scenario if isinstance(scenario, Mapping) else {}
    action = payload.get("action")
    action = action if isinstance(action, Mapping) else {}
    return {
        "original_decision": payload.get("original_decision"),
        "message_type": payload.get("message_type"),
        "notification_reason": payload.get("notification_reason"),
        "large_trend": payload.get("large_trend"),
        "current_trend": payload.get("current_trend"),
        "dow": ledger.get("dow"),
        "quadrant": reading.get("working_quadrant", reading.get("quadrant")),
        "background_quadrant": reading.get("background_quadrant"),
        "controlling_grade": reading.get("controlling_grade"),
        "working_anchor_ref": reading.get("working_anchor_ref"),
        "structure_event_ref": reading.get("structure_event_ref"),
        "cclass_mode": reading.get("cclass_mode"),
        "taiji": reading.get("taiji"),
        "yizhi": reading.get("yizhi"),
        "x_stage": reading.get("x_stage"),
        "x_process": reading.get("x_process"),
        "main_strategy": reading.get("main_strategy"),
        "setup_stage": reading.get("setup_stage"),
        "scenario_weights": {
            "bull": scenario.get("bull_probability"),
            "range": scenario.get("range_probability"),
            "bear": scenario.get("bear_probability"),
        },
        "position_action": action.get("position_action"),
        "entry_rejection_reason": action.get("entry_rejection_reason"),
    }


def _replay_message(
    *,
    body: str,
    dataset: ReplayDataset,
    bar_time: datetime,
    stage: str,
) -> str:
    label = "盤前夜盤快照" if stage == "preopen" else f"模擬K {bar_time.strftime('%H:%M')}"
    header = f"🧪【歷史回放｜{dataset.instrument}｜交易日{dataset.target_date.isoformat()}｜{label}】"
    cleaned = body.replace(DISCLAIMER, SIMULATION_DISCLAIMER).strip()
    if stage == "preopen":
        next_time = (bar_time + timedelta(minutes=1)).strftime("%H:%M")
        cleaned = cleaned.replace(
            f"{next_time} 為未收盤即時 K，只供觀察。",
            "夜盤已收盤，日盤尚未開盤；以下為盤前完整背景快照。",
        )
    return f"{header}\n\n{cleaned}\n"


def _night_summary(frame: pd.DataFrame) -> dict[str, Any]:
    first = frame.iloc[0]
    last = frame.iloc[-1]
    high_row = frame.loc[frame["high"].idxmax()]
    low_row = frame.loc[frame["low"].idxmin()]
    return {
        "first_bar": first["bar_time"].isoformat(),
        "last_bar": last["bar_time"].isoformat(),
        "open": _number(first["open"]),
        "high": _number(frame["high"].max()),
        "high_time": high_row["bar_time"].isoformat(),
        "low": _number(frame["low"].min()),
        "low_time": low_row["bar_time"].isoformat(),
        "close": _number(last["close"]),
        "volume": _number(frame["volume"].sum()),
        "bar_count": len(frame),
    }


def _select_day_bars(
    frame: pd.DataFrame,
    *,
    start_time: time | None,
    end_time: time | None,
    max_bars: int | None,
    analysis_every_bars: int = 1,
) -> pd.DataFrame:
    session = frame.copy().sort_values("bar_time").reset_index(drop=True)
    selected = session
    if start_time is not None:
        selected = selected[selected["bar_time"].dt.time >= start_time]
    if end_time is not None:
        selected = selected[selected["bar_time"].dt.time <= end_time]
    if analysis_every_bars < 1:
        raise ReplayRunError("analysis_every_invalid", "analysis_every_bars不得小於1。")
    if analysis_every_bars > 1 and not selected.empty:
        # Cadence is anchored to the complete monitoring session, not to an
        # arbitrary replay window. Otherwise ``--from 10:04`` would shift a
        # production 08:46/08:48/... schedule to 10:05/10:07/... and could
        # change the signal/fill minute being tested.
        cadence_positions = set(
            range(analysis_every_bars - 1, len(session), analysis_every_bars)
        )
        selected_positions = [
            position
            for position, row in session.iterrows()
            if position in cadence_positions
            and (start_time is None or row["bar_time"].time() >= start_time)
            and (end_time is None or row["bar_time"].time() <= end_time)
        ]
        eligible_positions = [
            position
            for position, row in session.iterrows()
            if (start_time is None or row["bar_time"].time() >= start_time)
            and (end_time is None or row["bar_time"].time() <= end_time)
        ]
        last_eligible = eligible_positions[-1]
        if not selected_positions or selected_positions[-1] != last_eligible:
            selected_positions.append(last_eligible)
        selected = session.iloc[selected_positions]
    if max_bars is not None:
        if max_bars < 0:
            raise ReplayRunError("max_bars_invalid", "max_bars不得小於0。")
        selected = selected.head(max_bars)
    return selected.reset_index(drop=True)


def _program_tick_bars(
    frame: pd.DataFrame,
    *,
    presentation_selected: pd.DataFrame,
    start_time: time | None,
) -> pd.DataFrame:
    """Return every causal 1m tick through the last requested output point.

    ``analysis_every_bars`` controls AI/TG presentation cadence only.  The
    deterministic structure and execution state machine must still observe
    every closed bar so a signal at 09:01 can schedule the 09:02 open instead
    of being discovered after that fill was already impossible.
    """

    if presentation_selected.empty:
        return presentation_selected.copy().reset_index(drop=True)
    last_output = presentation_selected.iloc[-1]["bar_time"]
    selected = frame[frame["bar_time"] <= last_output].copy()
    if start_time is not None:
        selected = selected[selected["bar_time"].dt.time >= start_time]
    selected.sort_values("bar_time", inplace=True)
    return selected.reset_index(drop=True)


def _select_event_driven_bars(
    frame: pd.DataFrame,
    *,
    session_key: str,
    start_time: time | None,
    end_time: time | None,
    max_bars: int | None,
    anchor_lifecycle_enabled: bool = False,
    course_chain_enabled: bool = False,
    decision_authority: str = "PROGRAM",
    trade_direction_policy: str = "BOTH",
    trade_setup_policy: str = "ALL",
    history_frame: pd.DataFrame | None = None,
    precomputed_timeline: list[dict[str, Any]] | None = None,
) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    """Select causal program events without asking AI on every unchanged bar.

    Exact structure events retain their first observable bar.  Baseline-only
    quadrant changes are debounced in the deterministic scanner and then
    coalesced to at most one AI call per ten minutes.  The full scoped event
    timeline is returned for audit even when more than one event shares a bar.
    """

    timeline = (
        [dict(item) for item in precomputed_timeline]
        if precomputed_timeline is not None
        else build_deterministic_timeline(
            _visible_bar_records(frame),
            session_key=session_key,
            anchor_lifecycle_enabled=anchor_lifecycle_enabled,
            course_chain_enabled=course_chain_enabled,
            decision_authority=decision_authority,
            trade_direction_policy=trade_direction_policy,
            trade_setup_policy=trade_setup_policy,
            initial_bars=(
                _visible_bar_records(history_frame)
                if history_frame is not None and not history_frame.empty
                else None
            ),
        )
    )
    scoped: list[dict[str, Any]] = []
    for entry in timeline:
        at = datetime.fromisoformat(str(entry.get("bar_time")))
        if start_time is not None and at.time() < start_time:
            continue
        if end_time is not None and at.time() > end_time:
            continue
        scoped.append(entry)

    selected_times: list[str] = []
    last_state_only: datetime | None = None
    for entry in scoped:
        events = entry.get("events") if isinstance(entry.get("events"), list) else []
        types = {
            str(item.get("event_type"))
            for item in events
            if isinstance(item, Mapping)
        }
        at = datetime.fromisoformat(str(entry["bar_time"]))
        exact = bool(types & EVENT_DRIVEN_EXACT_TYPES)
        state_only = bool(types & EVENT_DRIVEN_STATE_TYPES)
        keep_state = state_only and (
            last_state_only is None
            or at - last_state_only >= timedelta(minutes=EVENT_DRIVEN_STATE_MINUTES)
        )
        if exact or keep_state:
            selected_times.append(at.isoformat())
            if keep_state:
                last_state_only = at

    if max_bars is not None:
        if max_bars < 0:
            raise ReplayRunError("max_bars_invalid", "max_bars不得小於0。")
        selected_times = selected_times[:max_bars]
    selected_set = set(selected_times)
    selected = frame[
        frame["bar_time"].map(lambda value: value.isoformat() in selected_set)
    ].copy()
    selected.sort_values("bar_time", inplace=True)
    return selected.reset_index(drop=True), scoped


def _select_day_bars_at_times(frame: pd.DataFrame, analysis_times: list[time]) -> pd.DataFrame:
    """Select exact historical cutoffs while preserving chronological replay state."""
    unique_times = sorted(set(analysis_times))
    if not unique_times:
        raise ReplayRunError("analysis_times_empty", "指定分析時間不得為空。")
    available = set(frame["bar_time"].dt.time)
    missing = [value.strftime("%H:%M") for value in unique_times if value not in available]
    if missing:
        raise ReplayRunError(
            "analysis_time_missing",
            "資料中找不到指定的已收盤K時間：" + "、".join(missing),
        )
    selected = frame[frame["bar_time"].dt.time.isin(unique_times)].copy()
    selected.sort_values("bar_time", inplace=True)
    return selected.reset_index(drop=True)


def _program_protection_events_through(
    run_dir: Path,
    *,
    through: str,
) -> list[dict[str, Any]]:
    """Read only causal defense events visible through the current cutoff."""

    payload = read_json(run_dir / "deterministic-timeline.json")
    if not isinstance(payload, Mapping):
        return []
    cutoff = datetime.fromisoformat(through)
    result: list[dict[str, Any]] = []
    for entry in payload.get("event_times", []):
        if not isinstance(entry, Mapping):
            continue
        bar_time = entry.get("bar_time")
        if not isinstance(bar_time, str) or datetime.fromisoformat(bar_time) > cutoff:
            continue
        for event in entry.get("events", []):
            if isinstance(event, Mapping) and event.get("event_type") == "PROGRAM_DEFENSE_AVAILABLE":
                result.append(dict(event))
    result.sort(key=lambda item: (str(item.get("event_time")), str(item.get("event_id"))))
    return result


AI_HYBRID_PROMPT_VIEW_VERSION = "ai-hybrid-evidence-only-v2-actionable-stage"


def _prompt_structured_market_view(
    structured: Mapping[str, Any], *, ai_hybrid: bool
) -> dict[str, Any]:
    result = copy.deepcopy(dict(structured))
    if not ai_hybrid:
        return result
    causal = result.get("causal_structure_n2")
    if isinstance(causal, dict):
        causal.pop("dow_small", None)
        causal.pop("dow_large", None)
        causal["role"] = "PIVOT_AND_PRICE_EVIDENCE_ONLY"
    return result


def _prompt_deterministic_constraints(
    structured: Mapping[str, Any],
    *,
    stage: str,
    ledger: Mapping[str, Any] | None = None,
    ai_hybrid: bool,
) -> dict[str, Any]:
    result = _deterministic_constraints(structured, stage=stage, ledger=ledger)
    if not ai_hybrid:
        return result
    for key in (
        "dow_small",
        "dow_large",
        "dow_status_source",
        "legacy_causal_dow_role",
        "allowed_large_trend",
        "allowed_large_memory_direction",
    ):
        result.pop(key, None)
    result["market_interpretation_authority"] = "AI_HYBRID"
    return result


def _prompt_ledger_view(
    ledger: Mapping[str, Any], *, ai_hybrid: bool = False
) -> dict[str, Any]:
    """Remove replay history that cannot affect the current AI explanation.

    The validator and execution engine retain the complete in-memory ledger.
    This is only the serialized model view; no program decision is derived
    from the compacted object.
    """

    result = copy.deepcopy(dict(ledger))
    recent_levels = ledger.get("recent_bar_levels")
    if isinstance(recent_levels, list):
        result["recent_bar_levels"] = [
            dict(item) if isinstance(item, Mapping) else item
            for item in recent_levels[-15:]
        ]
    reference = ledger.get("reference_anchor_lifecycle")
    if isinstance(reference, Mapping):
        compact_reference = {
            key: reference.get(key)
            for key in (
                "version",
                "as_of",
                "session_key",
                "background_anchor",
                "child_anchor",
                "working_leg",
                "reverse_candidate",
                "dow_context",
                "session_scope",
            )
        }
        quadrant = reference.get("quadrant_context")
        if isinstance(quadrant, Mapping):
            compact_reference["quadrant_context"] = {
                key: quadrant.get(key)
                for key in (
                    "authority",
                    "reference_anchor_id",
                    "reference_grade",
                    "anchor_direction",
                    "phase",
                    "retracement_ratio",
                    "working_direction",
                    "working_structure_direction",
                    "working_phase",
                    "rule",
                )
            }
        taiji = reference.get("taiji_context")
        if isinstance(taiji, Mapping):
            compact_reference["taiji_context"] = {
                key: taiji.get(key)
                for key in (
                    "dynasty_anchor_ref",
                    "parent_direction",
                    "parent_start_time",
                    "parent_start_price",
                    "parent_start_kind",
                    "parent_end_time",
                    "parent_end_price",
                    "parent_end_kind",
                    "current_leg_ref",
                    "current_relation",
                    "reverse_candidate_ref",
                    "assessment_authority",
                )
            }
        result["reference_anchor_lifecycle"] = compact_reference
    if ai_hybrid:
        _strip_program_interpretive_answers(result)
    return result


def _strip_program_interpretive_answers(result: dict[str, Any]) -> None:
    """Keep causal measurements/candidates while hiding program conclusions.

    The validator still receives the complete ledger.  This function changes
    only the model-facing copy so AI_HYBRID cannot merely repeat program-owned
    quadrant, Taiji, Dow, X-stage or scenario-weight answers.
    """

    result["ai_input_view"] = {
        "version": AI_HYBRID_PROMPT_VIEW_VERSION,
        "interpretive_answers_redacted": True,
        "note": (
            "錨、防線與setup為可核對候選；大小級控制、道氏、象限、太極、"
            "X流程、情境權重與交易取捨由AI依課程判斷。"
        ),
    }
    _normalize_ai_hybrid_candidate_stages(result)
    for key in (
        "course_method_state",
        "course_constraints",
        "legacy_dow_diagnostic",
        "dow_authority",
        "dow",
    ):
        result.pop(key, None)

    for lifecycle_key in ("anchor_lifecycle", "reference_anchor_lifecycle"):
        lifecycle = result.get(lifecycle_key)
        if not isinstance(lifecycle, dict):
            continue
        quadrant = lifecycle.get("quadrant_context")
        if isinstance(quadrant, dict):
            for key in (
                "authority",
                "background_primary",
                "background_confidence",
                "background_candidates",
                "background_trend_dynamics",
                "background_volatility_dynamics",
                "working_primary",
                "working_confidence",
                "working_candidates",
                "working_trend_dynamics",
                "working_volatility_dynamics",
                "background_reason_codes",
                "working_reason_codes",
            ):
                quadrant.pop(key, None)
            quadrant["role"] = "SAME_GRADE_MEASUREMENTS_NOT_FINAL_QUADRANT"
        taiji = lifecycle.get("taiji_context")
        if isinstance(taiji, dict):
            for key in (
                "program_state",
                "program_quality",
                "last_copy_status",
                "reason_codes",
                "engine_mode",
                "assessment_authority",
            ):
                taiji.pop(key, None)
            taiji["role"] = "PARENT_AND_LEG_EVIDENCE_NOT_FINAL_TAIJI_VERDICT"
        dow = lifecycle.get("dow_context")
        if isinstance(dow, dict):
            dow.pop("large_state", None)
            dow.pop("small_state", None)
            dow["role"] = "DIRECTIONAL_DEFENSE_CANDIDATES_NOT_FINAL_DOW_VERDICT"

    quadrant_evidence = result.get("quadrant_evidence")
    if isinstance(quadrant_evidence, dict):
        for window in quadrant_evidence.values():
            if not isinstance(window, dict):
                continue
            for key in tuple(window):
                if str(key).startswith("recommended_"):
                    window.pop(key, None)
        quadrant_evidence["role"] = "NUMERIC_WINDOWS_ONLY_NOT_GRADE_OR_QUADRANT"


def _normalize_ai_hybrid_candidate_stages(result: dict[str, Any]) -> None:
    """Expose one unambiguous executable setup set to the model.

    ``trade_levels`` retains several diagnostic setup candidates so the AI can
    interpret false breaks, Q4 pullbacks, and other course evidence.  Their
    internal generators may label a raw candidate ``ARMED`` before the course
    quality gate selects the single executable continuation plan.  Showing
    that raw stage to the AI conflicts with the semantic guard, which permits
    actionable stages only for ``continuation_arm_candidate``.  Normalize the
    model-facing deep copy only; the authoritative ledger remains unchanged.
    """

    levels = result.get("trade_levels")
    if not isinstance(levels, dict):
        return
    # Interpretive program audits are retained in the authoritative ledger for
    # later comparison, but are intentionally absent from the AI input.  The
    # model must judge quadrant/Taiji/C-class from causal measurements rather
    # than inherit the program's advisory conclusion.
    levels.pop("ai_candidate_interpretive_audits", None)
    continuation = levels.get("continuation_arm_candidate")
    legacy_keys = {
        str(continuation.get("setup_key"))
        for continuation in (continuation,)
        if isinstance(continuation, Mapping) and continuation.get("setup_key")
    }
    inventory = levels.get("ai_candidate_inventory")
    inventory_keys = {
        str(item.get("setup_key"))
        for item in inventory
        if isinstance(item, Mapping) and item.get("setup_key")
    } if isinstance(inventory, list) else set()
    declared_armable = {
        str(value)
        for value in levels.get("ai_armable_setup_keys", [])
        if isinstance(value, str) and value.strip()
    }
    # New AI_HYBRID ledgers expose all hard-valid Q2/Q4 options.  Old saved
    # runs and fixtures have no inventory, so retain the singular fallback.
    executable_keys = (
        inventory_keys & declared_armable
        if isinstance(inventory, list)
        else legacy_keys
    )
    if isinstance(inventory, list):
        # The complete candidate inventory supersedes the legacy one-candidate
        # course-gate diagnostics in AI input.  Keep those diagnostics only in
        # the authoritative ledger; otherwise names/reasons such as a program
        # quadrant or Taiji rejection would disclose the interpretation that
        # the hybrid model is expected to make independently.
        for key in (
            "course_entry_quality_audit",
            "course_filtered_candidate",
            "selected_entry_candidate_before_course_gate",
        ):
            levels.pop(key, None)
    actionable = {
        "ARMED",
        "ENTRY_ELIGIBLE",
        "AGGRESSIVE_CONFIRMED",
        "CONSERVATIVE_CONFIRMED",
    }

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            setup_key = str(value.get("setup_key") or "")
            if setup_key:
                value.pop("course_entry_quality", None)
                value.pop("course_entry_quality_authority", None)
            if (
                setup_key
                and setup_key not in executable_keys
                and value.get("stage") in actionable
            ):
                value["stage"] = "FORMING"
                value["execution_status"] = "OBSERVATION_ONLY"
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(levels)
    levels["ai_armable_setup_keys"] = sorted(executable_keys)
    levels["ai_executable_setup_keys"] = sorted(executable_keys)


def _prompt_event_lifecycle_view(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    events = value.get("events")
    event_items = events if isinstance(events, list) else []
    active = [
        {key: child for key, child in item.items() if key != "payload"}
        for item in event_items
        if isinstance(item, Mapping)
        and item.get("status") == "ACTIVE"
        and not item.get("analyzed")
    ]
    return {
        "version": value.get("version"),
        "as_of": value.get("as_of"),
        "events": active,
        "historical_event_count": len(event_items),
        "note": "Only active unanalyzed events are serialized; full lifecycle remains program-owned.",
    }


def _optional_time(value: Any) -> time | None:
    if value in {None, ""}:
        return None
    return time.fromisoformat(str(value))


def _write_bars(path: Path, dataset: ReplayDataset) -> None:
    frame = dataset.all_bars.copy()
    frame.insert(0, "session", ["night"] * len(dataset.night_bars) + ["day"] * len(dataset.day_bars))
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False, encoding="utf-8-sig")


def _write_summary(run_dir: Path, manifest: Mapping[str, Any]) -> None:
    lines = [
        "# 台指期歷史回放摘要",
        "",
        f"- Run：`{manifest.get('run_id')}`",
        f"- 狀態：`{manifest.get('status')}`",
        f"- 交易日：`{manifest.get('target_date')}`",
        f"- 商品：`{manifest.get('instrument')}`",
        f"- 規則：`{manifest.get('rule_version')}`",
        f"- 盤前快照：`{manifest.get('preopen_completed')}`",
        f"- 完成日盤K：`{manifest.get('completed_day_bars')}`",
        f"- AI provider：`{manifest.get('ai_provider', 'minimax')}`",
        f"- AI model：`{manifest.get('ai_model')}`",
        f"- AI呼叫：`{manifest.get('ai_call_count', manifest.get('minimax_call_count'))}`",
        f"- Telegram傳送紀錄：`{manifest.get('notification_count')}`",
    ]
    if manifest.get("error"):
        lines.extend([f"- 錯誤代碼：`{manifest.get('error_code')}`", f"- 錯誤：{manifest.get('error')}"])
    (run_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _count_usage_calls(run_dir: Path) -> int:
    path = run_dir / "ai-usage.jsonl"
    if not path.exists():
        path = run_dir / "minimax-usage.jsonl"
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())


def _count_notifications(run_dir: Path) -> int:
    path = run_dir / "deliveries.jsonl"
    if not path.exists():
        return 0
    count = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if payload.get("status") in {"sent", "duplicate"}:
            count += 1
    return count


def _record_manual_delivery_approval(
    run_dir: Path,
    manifest: dict[str, Any],
    *,
    message_record: Mapping[str, Any],
    approval_source: str,
) -> None:
    """Persist the explicit review action before sending a saved message."""

    append_jsonl(
        run_dir / "review-events.jsonl",
        {
            "event_type": "MESSAGE_APPROVED_FOR_DELIVERY",
            "review_mode": manifest.get("review_mode", "legacy_unspecified"),
            "approval_source": approval_source,
            "stage": message_record.get("stage"),
            "bar_time": message_record.get("bar_time"),
            "event_id": message_record.get("event_id"),
            "approved_at": datetime.now(TAIPEI).isoformat(),
        },
    )
    manifest["human_intervention"] = True
    manifest["approved_delivery_count"] = int(
        manifest.get("approved_delivery_count") or 0
    ) + 1


def _latest_pending_raw(
    run_dir: Path,
    manifest: Mapping[str, Any],
    dataset: ReplayDataset,
) -> Path:
    if not manifest.get("preopen_completed"):
        bar_time = dataset.night_bars.iloc[-1]["bar_time"].to_pydatetime()
        key = f"preopen-{bar_time.strftime('%Y%m%d-%H%M')}"
    else:
        selected = list(manifest.get("selected_bar_times") or [])
        index = int(manifest.get("next_day_index") or 0)
        if index >= len(selected):
            raise ReplayRunError("revalidate_pending_missing", "目前沒有待重新驗證的分析點。")
        bar_time = datetime.fromisoformat(str(selected[index]))
        key = f"day-{bar_time.strftime('%Y%m%d-%H%M')}"
    candidates = list((run_dir / "analysis").glob(f"{key}*-attempt-*-raw.txt"))
    if not candidates:
        raise ReplayRunError("revalidate_raw_missing", "找不到目前分析點已保存的AI原始輸出。")
    return max(candidates, key=lambda path: path.stat().st_mtime_ns)


def _latest_completed_raw(run_dir: Path, manifest: Mapping[str, Any]) -> Path:
    if not manifest.get("preopen_completed"):
        raise ReplayRunError("revalidate_completed_missing", "目前沒有已完成的分析點可重新驗證。")
    selected = list(manifest.get("selected_bar_times") or [])
    completed = int(manifest.get("next_day_index") or 0)
    if completed < 1 or completed > len(selected):
        raise ReplayRunError("revalidate_completed_missing", "目前沒有已完成的日盤分析點可重新驗證。")
    bar_time = datetime.fromisoformat(str(selected[completed - 1]))
    key = f"day-{bar_time.strftime('%Y%m%d-%H%M')}"
    candidates = list((run_dir / "analysis").glob(f"{key}*-attempt-*-raw.txt"))
    if not candidates:
        raise ReplayRunError("revalidate_raw_missing", "找不到最近已完成分析點的AI原始輸出。")
    return max(candidates, key=lambda path: path.stat().st_mtime_ns)


def _latest_jsonl_record(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    latest: dict[str, Any] | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            latest = payload
    return latest


def _jsonl_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            records.append(payload)
    return records


def _append_execution_event_once(path: Path, event: Mapping[str, Any]) -> bool:
    """Append one execution event idempotently across causal rewinds."""

    event_id = event.get("event_id")
    if event_id and any(item.get("event_id") == event_id for item in _jsonl_records(path)):
        return False
    append_jsonl(path, dict(event))
    return True


def _optional_hhmm(value: Any) -> time | None:
    try:
        return datetime.fromisoformat(str(value)).time().replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def _recent_validation_errors(
    path: Path,
    *,
    stage: str,
    bar_time: datetime,
    limit: int = 6,
) -> list[str]:
    """Return only prior contract feedback for this causal analysis point."""
    if not path.exists():
        return []
    expected_time = bar_time.isoformat()
    errors: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, Mapping):
            continue
        if payload.get("stage") != stage or payload.get("bar_time") != expected_time:
            continue
        error = str(payload.get("error") or "").strip()
        if error and error not in errors:
            errors.append(error)
    return errors[-limit:]


def _quiet_status_delivery_due(
    path: Path,
    *,
    bar_time: datetime,
    interval_minutes: int,
) -> bool:
    """Throttle only quiet cards; material NOTIFY events bypass this helper."""
    if not path.exists():
        return True
    latest_success_time: datetime | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, Mapping) or payload.get("status") not in {"sent", "duplicate"}:
            continue
        raw_time = payload.get("bar_time")
        if not raw_time:
            continue
        try:
            delivered_at = datetime.fromisoformat(str(raw_time).replace("Z", "+00:00"))
        except ValueError:
            continue
        if latest_success_time is None or delivered_at > latest_success_time:
            latest_success_time = delivered_at
    if latest_success_time is None:
        return True
    return bar_time - latest_success_time >= timedelta(minutes=interval_minutes)


def _next_artifact_key(prompt_dir: Path, key: str) -> str:
    if not (prompt_dir / f"{key}-attempt-1.txt").exists():
        return key
    cycle = 2
    while (prompt_dir / f"{key}-resume-{cycle}-attempt-1.txt").exists():
        cycle += 1
    return f"{key}-resume-{cycle}"


def _armed_setup_first_seen(
    run_dir: Path,
    previous_memory: Mapping[str, Any] | None,
) -> dict[str, str]:
    """Find the first timestamp in each current contiguous ARMED lifetime."""

    if not isinstance(previous_memory, Mapping):
        return {}
    previous_at = previous_memory.get("as_of")
    session_key = previous_memory.get("session_key")
    try:
        cutoff = datetime.fromisoformat(str(previous_at))
    except (TypeError, ValueError):
        return {}
    current_setups = previous_memory.get("active_setups")
    if not isinstance(current_setups, list):
        return {}
    active_stages = {"ARMED", "AGGRESSIVE_CONFIRMED", "CONSERVATIVE_CONFIRMED"}
    keys = {
        str(item.get("setup_key"))
        for item in current_setups
        if isinstance(item, Mapping)
        and item.get("setup_key")
        and item.get("stage") in active_stages
    }
    if not keys:
        return {}

    # A rerun can leave several immutable artifacts for one simulated minute.
    # Use the newest artifact for each memory timestamp, while never reading a
    # checkpoint later than the previous causal state.
    by_time: dict[datetime, tuple[int, Mapping[str, Any]]] = {}
    for path in (run_dir / "analysis").glob("*-validated.json"):
        payload = read_json(path)
        memory = payload.get("memory") if isinstance(payload, Mapping) else None
        if not isinstance(memory, Mapping) or memory.get("session_key") != session_key:
            continue
        try:
            at = datetime.fromisoformat(str(memory.get("as_of")))
        except (TypeError, ValueError):
            continue
        if at > cutoff:
            continue
        stamp = path.stat().st_mtime_ns
        if at not in by_time or stamp > by_time[at][0]:
            by_time[at] = (stamp, memory)

    records = [(at, value[1]) for at, value in sorted(by_time.items())]
    result: dict[str, str] = {}
    for setup_key in keys:
        first: datetime | None = None
        for at, memory in reversed(records):
            setups = memory.get("active_setups")
            # Keep the comprehension simple enough to make malformed historic
            # artifacts non-fatal.
            matching = next(
                (
                    item
                    for item in setups
                    if isinstance(item, Mapping) and item.get("setup_key") == setup_key
                ),
                None,
            ) if isinstance(setups, list) else None
            if not isinstance(matching, Mapping) or matching.get("stage") not in active_stages:
                if first is not None:
                    break
                continue
            first = at
        if first is not None:
            result[setup_key] = first.isoformat()
    return result


def _ai_hybrid_state_continuity_lock(
    previous_memory: Mapping[str, Any] | None,
    evidence_events: list[Mapping[str, Any]],
    *,
    ledger: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Expose an exact carry-forward contract for prior AI-accepted control.

    This does not choose a new anchor or grade.  It prevents a later model turn
    from silently deleting an interpretation it already accepted.  A current
    formal structure event reopens that decision and the semantic validator
    remains the final authority over whether the referenced event is legal.
    """

    previous_control = (
        previous_memory.get("structure_control")
        if isinstance(previous_memory, Mapping)
        and previous_memory.get("version") == 3
        else None
    )
    prior_ref = (
        previous_control.get("active_large_anchor_ref")
        if isinstance(previous_control, Mapping)
        else None
    )
    prior_grade = (
        previous_control.get("controlling_grade")
        if isinstance(previous_control, Mapping)
        else None
    )
    formal_types = {"GRADE_UPGRADE", "GRADE_DOWNGRADE", "STRUCTURE_INVALIDATED"}
    structure_events = (
        ledger.get("structure_events")
        if isinstance(ledger, Mapping)
        else None
    )
    structure_by_id = {
        str(item.get("id")): item
        for item in structure_events
        if isinstance(item, Mapping) and item.get("id")
    } if isinstance(structure_events, list) else {}
    permitted: list[dict[str, Any]] = []
    for item in evidence_events:
        if not isinstance(item, Mapping):
            continue
        event_id = item.get("event_id") or item.get("id")
        event_type = item.get("event_type")
        if event_type == "STRUCTURE_EVENT" and event_id is not None:
            resolved = structure_by_id.get(str(event_id))
            event_type = resolved.get("event_type") if isinstance(resolved, Mapping) else None
        if event_type not in formal_types:
            continue
        permitted.append(
            {
                "event_id": event_id,
                "event_type": event_type,
            }
        )
    required = bool(prior_ref and prior_grade == "LARGE" and not permitted)
    return {
        "version": 1,
        "authority": "PREVIOUS_AI_ACCEPTED_STATE_TRANSITION",
        "required": required,
        "reason": (
            "NO_EXPLICIT_CONTROL_CHANGE_EVENT"
            if required
            else "CONTROL_CHANGE_EVENT_AVAILABLE"
            if permitted
            else "NO_PRIOR_ACCEPTED_LARGE_CONTROL"
        ),
        "required_analysis_course_reading": (
            {
                "large_anchor_ref": prior_ref,
                "controlling_grade": "LARGE",
            }
            if required
            else None
        ),
        "required_memory_structure_control": (
            {
                "active_large_anchor_ref": prior_ref,
                "controlling_grade": "LARGE",
            }
            if required
            else None
        ),
        "permitted_change_events": permitted,
    }


def _retired_setup_keys(
    run_dir: Path,
    previous_memory: Mapping[str, Any] | None,
    *,
    active_setup_key: object = None,
) -> set[str]:
    """Return every setup key causally consumed in this replay session.

    Validated semantic memory is normally the source of truth.  Program-owned
    invalidation events are also durable evidence because an unseen candidate
    can have its frozen stop crossed before it ever enters AI memory.
    """

    if not isinstance(previous_memory, Mapping):
        return set()
    try:
        cutoff = datetime.fromisoformat(str(previous_memory.get("as_of")))
    except (TypeError, ValueError):
        return set()
    session_key = previous_memory.get("session_key")
    by_time: dict[datetime, tuple[int, Mapping[str, Any]]] = {}
    for path in (run_dir / "analysis").glob("*-validated.json"):
        payload = read_json(path)
        memory = payload.get("memory") if isinstance(payload, Mapping) else None
        if not isinstance(memory, Mapping) or memory.get("session_key") != session_key:
            continue
        try:
            at = datetime.fromisoformat(str(memory.get("as_of")))
        except (TypeError, ValueError):
            continue
        if at > cutoff:
            continue
        stamp = path.stat().st_mtime_ns
        if at not in by_time or stamp > by_time[at][0]:
            by_time[at] = (stamp, memory)

    retired: set[str] = set()
    execution_events_path = run_dir / "execution-events.jsonl"
    if execution_events_path.exists():
        for line in execution_events_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if (
                not isinstance(event, Mapping)
                or event.get("event_type") != "PROGRAM_SETUP_INVALIDATED"
                or not event.get("setup_key")
            ):
                continue
            event_at: datetime | None = None
            for field in ("invalidated_at", "event_time", "recorded_at", "evaluated_at"):
                try:
                    event_at = datetime.fromisoformat(str(event.get(field)))
                except (TypeError, ValueError):
                    continue
                break
            if event_at is not None and event_at <= cutoff:
                retired.add(str(event["setup_key"]))
    previously_actionable: set[str] = set()
    prior_actionable: set[str] = set()
    for _, memory in sorted(by_time.values(), key=lambda item: str(item[1].get("as_of"))):
        setups = memory.get("active_setups")
        current_actionable: set[str] = set()
        for setup in setups if isinstance(setups, list) else []:
            if not isinstance(setup, Mapping) or not setup.get("setup_key"):
                continue
            setup_key = str(setup["setup_key"])
            if setup.get("stage") in {
                "ARMED", "ENTRY_ELIGIBLE", "AGGRESSIVE_CONFIRMED", "CONSERVATIVE_CONFIRMED",
            }:
                previously_actionable.add(setup_key)
                current_actionable.add(setup_key)
            if setup.get("stage") in {"NO_CHASE", "INVALIDATED"}:
                retired.add(setup_key)
        # Once an actionable setup disappears or leaves an actionable stage,
        # its historical trigger is retired permanently.  This catches stale
        # pre-rewind artifacts that later try to reintroduce the same key.
        retired.update(prior_actionable - current_actionable)
        prior_actionable = current_actionable

    # An actionable setup that disappears from the accepted current memory was
    # consumed, expired, or superseded. Treat disappearance as retirement so a
    # deterministic candidate cannot resurrect the same historic trigger on a
    # later bar. The active position's owner is excluded below.
    current_setups = previous_memory.get("active_setups")
    current_keys = {
        str(item.get("setup_key"))
        for item in (current_setups if isinstance(current_setups, list) else [])
        if isinstance(item, Mapping) and item.get("setup_key")
    }
    retired.update(previously_actionable - current_keys)
    if active_setup_key:
        retired.discard(str(active_setup_key))
    return retired


def _terminal_setup_keys(memory: Mapping[str, Any] | None) -> set[str]:
    """Return setup keys already terminal in the current causal memory."""

    if not isinstance(memory, Mapping):
        return set()
    setups = memory.get("active_setups")
    return {
        str(item.get("setup_key"))
        for item in (setups if isinstance(setups, list) else [])
        if isinstance(item, Mapping)
        and item.get("setup_key")
        and item.get("stage") in {"NO_CHASE", "INVALIDATED"}
    }


def _suppress_retired_continuation_candidate(
    ledger: Mapping[str, Any],
    *,
    retired_setup_keys: set[str],
    as_of: str | None = None,
) -> dict[str, Any]:
    """Hide a consumed candidate and only promote a still-live queued plan.

    A newly derived plan can wait behind an already armed setup.  Waiting does
    not restart its original three-bar trigger window: promoting that plan
    after the window ended would turn a historical event into a new signal.
    ``as_of`` is optional only for small unit-test/diagnostic callers that do
    not have a replay clock.
    """

    current = dict(ledger)
    levels = current.get("trade_levels")
    if not isinstance(levels, Mapping):
        return current
    candidate = levels.get("continuation_arm_candidate")
    if not isinstance(candidate, Mapping) or str(candidate.get("setup_key")) not in retired_setup_keys:
        return current
    normalized_levels = dict(levels)
    queued = normalized_levels.get("next_continuation_candidate")
    promote_queued = (
        isinstance(queued, Mapping)
        and str(queued.get("setup_key") or "") not in retired_setup_keys
    )
    queued_expired = False
    if promote_queued and as_of is not None:
        try:
            queued_seen = datetime.fromisoformat(str(queued.get("first_seen_at")))
            current_at = datetime.fromisoformat(as_of)
            valid_bars = max(1, int(queued.get("valid_bars") or 3))
            queued_expired = current_at > queued_seen + timedelta(minutes=valid_bars)
        except (TypeError, ValueError):
            # Malformed timing is handled by the semantic validator.  Do not
            # invent an expiry here because that would make this helper a
            # second source of truth for invalid records.
            queued_expired = False
    promote_queued = promote_queued and not queued_expired
    normalized_levels["continuation_arm_candidate"] = dict(queued) if promote_queued else None
    normalized_levels.pop("next_continuation_candidate", None)
    normalized_levels["retired_continuation_setup_key"] = str(candidate.get("setup_key"))
    normalized_levels["retired_continuation_candidate"] = dict(candidate)
    if queued_expired:
        normalized_levels["stale_unseen_continuation_candidate"] = dict(queued)
        normalized_levels["stale_unseen_reason"] = (
            "ORIGINAL_TRIGGER_WINDOW_EXPIRED_WHILE_QUEUED"
        )
    current["trade_levels"] = normalized_levels
    return current


def _publish_frozen_plan_quality_audit(
    levels: Mapping[str, Any],
    *,
    frozen_candidate: Mapping[str, Any],
    prior_levels: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Separate the active frozen plan's qualification from a fresh rebuild.

    A plan that passed the course gate remains immutable for its declared
    trigger window.  The live rebuild may temporarily become observation-only
    while the correction develops; that is useful diagnostic evidence, but it
    must not overwrite the already accepted plan's qualification or make the
    audit claim that an observation-only setup entered a position.
    """

    normalized = dict(levels)
    rebuilt_audit = normalized.get("course_entry_quality_audit")
    if isinstance(rebuilt_audit, Mapping):
        normalized["rebuilt_course_entry_quality_audit"] = dict(rebuilt_audit)
    rebuilt_filtered = normalized.get("course_filtered_candidate")
    if isinstance(rebuilt_filtered, Mapping):
        normalized["rebuilt_course_filtered_candidate"] = dict(rebuilt_filtered)

    prior_audit = (
        prior_levels.get("course_entry_quality_audit")
        if isinstance(prior_levels, Mapping)
        else None
    )
    setup_key = str(frozen_candidate.get("setup_key") or "")
    if (
        isinstance(prior_audit, Mapping)
        and prior_audit.get("status") == "EXECUTABLE"
        and str(prior_audit.get("setup_key") or "") == setup_key
    ):
        active_audit = dict(prior_audit)
    else:
        active_audit = {
            "version": 1,
            "authority": "PROGRAM_COURSE_GATE_V1",
            "status": "EXECUTABLE",
            "setup_key": setup_key or None,
            "candidate_source": frozen_candidate.get("candidate_source"),
            "reason_codes": ["FROZEN_PROGRAM_PLAN_PREVIOUSLY_QUALIFIED"],
        }
    reasons = list(active_audit.get("reason_codes") or [])
    marker = "FROZEN_PROGRAM_PLAN_REMAINS_VALID"
    if marker not in reasons:
        reasons.append(marker)
    active_audit.update(
        {
            "status": "EXECUTABLE",
            "setup_key": setup_key or active_audit.get("setup_key"),
            "candidate_source": (
                frozen_candidate.get("candidate_source")
                or active_audit.get("candidate_source")
            ),
            "reason_codes": reasons,
            "frozen_plan": True,
        }
    )
    normalized["course_entry_quality_audit"] = active_audit
    normalized.pop("course_filtered_candidate", None)
    return normalized


def _freeze_continuation_candidate(
    ledger: Mapping[str, Any],
    *,
    previous_ledger: Mapping[str, Any] | None,
    previous_memory: Mapping[str, Any] | None = None,
    retired_setup_keys: set[str] | None = None,
) -> dict[str, Any]:
    """Freeze an ARMED continuation setup instead of chasing each new low/high.

    ``_continuation_arm_candidate`` is rebuilt from all bars at every cutoff.
    While its structural setup key stays the same, the forming extreme can
    advance and otherwise move the trigger away after the setup was already
    armed.  The first causal candidate is the trade plan; a different plan must
    receive a different setup key after the old one is consumed or retired.
    """

    current = dict(ledger)
    levels = current.get("trade_levels")
    if not isinstance(levels, Mapping):
        return current
    prior_levels = (
        previous_ledger.get("trade_levels")
        if isinstance(previous_ledger, Mapping)
        else None
    )
    candidate = levels.get("continuation_arm_candidate")
    prior = (
        prior_levels.get("continuation_arm_candidate")
        if isinstance(prior_levels, Mapping)
        else None
    )
    retired = retired_setup_keys or set()
    prior_key = str(prior.get("setup_key") or "") if isinstance(prior, Mapping) else ""
    active_prior_keys = {
        str(item.get("setup_key"))
        for item in (
            previous_memory.get("active_setups", [])
            if isinstance(previous_memory, Mapping)
            else []
        )
        if isinstance(item, Mapping)
        and item.get("stage") in {
            "ARMED", "ENTRY_ELIGIBLE", "AGGRESSIVE_CONFIRMED", "CONSERVATIVE_CONFIRMED",
        }
        and item.get("setup_key")
    }
    same_rebuilt_setup = bool(
        isinstance(candidate, Mapping)
        and candidate.get("setup_key")
        and candidate.get("setup_key") == prior.get("setup_key")
    ) if isinstance(prior, Mapping) else False
    if (
        isinstance(candidate, Mapping)
        and candidate.get("setup_key")
        and candidate.get("setup_key") != prior_key
    ):
        prior_direction = {"LONG": "BULL", "SHORT": "BEAR"}.get(
            str(prior.get("direction") or "")
        ) if isinstance(prior, Mapping) else None
        if (
            prior_direction is not None
            and _is_countertrend_to_highest_active_anchor(
                prior_direction,
                ledger=current,
            )
        ):
            # A frozen plan is immutable while its course controller remains
            # valid.  It is not immortal: an objective right-side takeover
            # invalidates the old direction and allows the new unique
            # program-owned candidate to become actionable immediately.
            normalized_levels = dict(levels)
            normalized_levels["invalidated_program_candidate"] = dict(prior)
            normalized_levels["invalidated_program_candidate_reason"] = (
                "PRIOR_DIRECTION_NO_LONGER_MATCHES_HIGHEST_ACTIVE_CONTROLLER"
            )
            current["trade_levels"] = normalized_levels
            return current
    if (
        not isinstance(prior, Mapping)
        or not prior_key
        or prior_key in retired
        or (not same_rebuilt_setup and prior_key not in active_prior_keys)
    ):
        return current
    normalized_levels = dict(levels)
    if not isinstance(candidate, Mapping) or not candidate.get("setup_key"):
        # Once exposed as ARMED, the program-owned plan remains immutable for
        # its short declared validity window even if the freshly rebuilt
        # market snapshot no longer emits the raw candidate.  Expiry,
        # countertrend suppression and structural retirement still run after
        # this freeze and are the only authorities allowed to consume it.
        normalized_levels["continuation_arm_candidate"] = dict(prior)
    elif candidate.get("setup_key") == prior.get("setup_key"):
        normalized_levels["continuation_arm_candidate"] = dict(prior)
    else:
        # Keep the already armed plan until its trigger/expiry is consumed.
        # Preserve the independently keyed newer pullback so it can be promoted
        # at the exact cutoff where the old plan becomes terminal.
        normalized_levels["continuation_arm_candidate"] = dict(prior)
        normalized_levels["next_continuation_candidate"] = dict(candidate)
    normalized_levels = _publish_frozen_plan_quality_audit(
        normalized_levels,
        frozen_candidate=prior,
        prior_levels=prior_levels,
    )
    current["trade_levels"] = normalized_levels
    return current


def _blocked_program_setup_keys(ledger: Mapping[str, Any] | None) -> set[str]:
    """Return old setup keys that objective course state has invalidated."""

    levels = ledger.get("trade_levels") if isinstance(ledger, Mapping) else None
    if not isinstance(levels, Mapping):
        return set()
    blocked: set[str] = set()
    for field in (
        "countertrend_continuation_observation",
        "invalidated_program_candidate",
        "preentry_invalidated_candidate",
        "exhausted_reentry_candidate",
    ):
        item = levels.get(field)
        if isinstance(item, Mapping) and item.get("setup_key"):
            blocked.add(str(item["setup_key"]))
    return blocked


def _filter_ai_hybrid_candidate_inventory(
    ledger: Mapping[str, Any],
    *,
    retired_setup_keys: set[str],
    previous_memory: Mapping[str, Any] | None,
    as_of: str,
) -> dict[str, Any]:
    """Apply execution-only invalidation to the v34 AI candidate inventory.

    Course interpretation never enters this filter.  It removes only setups
    that are retired, objectively invalidated, constitution-blocked, or whose
    original fixed validity window elapsed before the AI ever selected them.
    """

    result = dict(ledger)
    policy = result.get("program_trade_policy")
    levels = result.get("trade_levels")
    if not (
        isinstance(policy, Mapping)
        and policy.get("decision_authority") == "AI_HYBRID"
        and policy.get("trade_setup_policy") == "LONG_Q2_Q4_ONLY"
        and isinstance(levels, Mapping)
        and isinstance(levels.get("ai_candidate_inventory"), list)
    ):
        return result

    normalized = dict(levels)
    constitution_gate = normalized.get("constitution_gate")
    constitution_blocked = bool(
        isinstance(constitution_gate, Mapping)
        and constitution_gate.get("status") == "BLOCKED"
    )
    blocked = set(retired_setup_keys) | _blocked_program_setup_keys(result)
    prior_actionable = {
        str(item.get("setup_key"))
        for item in (
            previous_memory.get("active_setups", [])
            if isinstance(previous_memory, Mapping)
            else []
        )
        if isinstance(item, Mapping)
        and item.get("setup_key")
        and item.get("stage")
        in {"ARMED", "ENTRY_ELIGIBLE", "AGGRESSIVE_CONFIRMED", "CONSERVATIVE_CONFIRMED"}
    }
    try:
        current_at = datetime.fromisoformat(as_of)
    except ValueError:
        current_at = None

    kept: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    for raw in normalized.get("ai_candidate_inventory", []):
        if not isinstance(raw, Mapping):
            continue
        item = dict(raw)
        setup_key = str(item.get("setup_key") or "")
        reason: str | None = None
        if constitution_blocked:
            reason = "PROGRAM_CONSTITUTION_BLOCKED"
        elif not setup_key or setup_key in blocked:
            reason = "SETUP_RETIRED_OR_OBJECTIVELY_INVALIDATED"
        elif (
            setup_key not in prior_actionable
            and current_at is not None
            and not (
                isinstance(item.get("validity_policy"), Mapping)
                and item["validity_policy"].get("kind") == "STRUCTURAL"
            )
        ):
            try:
                first_seen = datetime.fromisoformat(str(item.get("first_seen_at")))
                valid_bars = max(1, int(item.get("valid_bars") or 3))
                if current_at > first_seen + timedelta(minutes=valid_bars):
                    reason = "ORIGINAL_VALIDITY_WINDOW_EXPIRED_BEFORE_AI_SELECTION"
            except (TypeError, ValueError):
                # The hard-fact inventory validator owns malformed candidate
                # fields.  Do not invent a second interpretation here.
                pass
        if reason is None:
            kept.append(item)
        else:
            removed.append({**item, "inventory_removal_reason": reason})

    normalized["ai_candidate_inventory"] = kept
    normalized["ai_armable_setup_keys"] = sorted(
        str(item["setup_key"]) for item in kept if item.get("setup_key")
    )
    if removed:
        normalized["ai_unavailable_candidate_audit"] = removed
    result["trade_levels"] = normalized
    return result


def _structural_ai_candidate_keys(ledger: Mapping[str, Any]) -> set[str]:
    levels = ledger.get("trade_levels")
    inventory = levels.get("ai_candidate_inventory") if isinstance(levels, Mapping) else None
    return {
        str(item.get("setup_key"))
        for item in (inventory if isinstance(inventory, list) else [])
        if isinstance(item, Mapping)
        and item.get("setup_key")
        and isinstance(item.get("validity_policy"), Mapping)
        and item["validity_policy"].get("kind") == "STRUCTURAL"
    }


def _ai_hybrid_execution_candidate(
    ledger: Mapping[str, Any],
    *,
    previous_memory: Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    """Resolve the one candidate selected by AI, with legacy compatibility.

    When several hard-valid candidates coexist, the program must not select
    one by its own quadrant/Taiji priority.  A prior AI-selected actionable
    setup wins.  With no prior choice, a sole candidate may use the existing
    same-cutoff gate; multiple candidates first require an AI selection.
    """

    levels = ledger.get("trade_levels")
    if not isinstance(levels, Mapping):
        return None
    inventory = levels.get("ai_candidate_inventory")
    if not isinstance(inventory, list):
        legacy = levels.get("continuation_arm_candidate")
        return legacy if isinstance(legacy, Mapping) else None
    by_key = {
        str(item.get("setup_key")): item
        for item in inventory
        if isinstance(item, Mapping) and item.get("setup_key")
    }
    selected_keys = {
        str(item.get("setup_key"))
        for item in (
            previous_memory.get("active_setups", [])
            if isinstance(previous_memory, Mapping)
            else []
        )
        if isinstance(item, Mapping)
        and item.get("setup_key")
        and item.get("stage")
        in {"ARMED", "ENTRY_ELIGIBLE", "AGGRESSIVE_CONFIRMED", "CONSERVATIVE_CONFIRMED"}
        and str(item.get("setup_key")) in by_key
    }
    if len(selected_keys) == 1:
        return by_key[next(iter(selected_keys))]
    if not selected_keys and len(by_key) == 1:
        return next(iter(by_key.values()))
    return None


def _ai_hybrid_preentry_invalidation_candidate(
    ledger: Mapping[str, Any],
    *,
    previous_ledger: Mapping[str, Any] | None,
    previous_memory: Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    """Keep a selected structural plan visible long enough to retire it.

    A structural candidate may disappear from the freshly derived inventory on
    the exact bar that trades through its frozen stop.  The stop audit must then
    inspect the prior, AI-selected candidate; otherwise the old ARMED memory can
    survive after its objective source has vanished.  Fixed-window candidates
    deliberately do not use this fallback because their normal expiry owns that
    lifecycle.
    """

    current = _ai_hybrid_execution_candidate(
        ledger,
        previous_memory=previous_memory,
    )
    if current is not None:
        return current
    if not isinstance(previous_ledger, Mapping):
        return None
    prior = _ai_hybrid_execution_candidate(
        previous_ledger,
        previous_memory=previous_memory,
    )
    if not isinstance(prior, Mapping):
        return None
    validity = prior.get("validity_policy")
    if not isinstance(validity, Mapping) or validity.get("kind") != "STRUCTURAL":
        return None
    return prior


def _apply_preentry_invalidation_to_ledger(
    ledger: Mapping[str, Any],
    *,
    audit: Mapping[str, Any],
) -> dict[str, Any]:
    """Remove a stop-touched flat setup from the executable ledger."""

    current = dict(ledger)
    levels = current.get("trade_levels")
    if not isinstance(levels, Mapping):
        current["preentry_invalidation_audit"] = dict(audit)
        return current
    normalized = dict(levels)
    candidate = normalized.get("continuation_arm_candidate")
    if (
        isinstance(candidate, Mapping)
        and str(candidate.get("setup_key") or "") == str(audit.get("setup_key") or "")
    ):
        normalized["preentry_invalidated_candidate"] = dict(candidate)
        normalized["preentry_invalidated_candidate_reason"] = str(
            audit.get("reason") or "STRUCTURAL_STOP_TOUCHED_BEFORE_ENTRY"
        )
        normalized["continuation_arm_candidate"] = None
    current["trade_levels"] = normalized
    current["preentry_invalidation_audit"] = dict(audit)
    return current


def _suppress_countertrend_continuation_candidate(
    ledger: Mapping[str, Any],
) -> dict[str, Any]:
    """Do not mechanically arm a continuation against the highest active anchor.

    The deterministic candidate describes a completed small-grade correction
    and trigger line.  Under the one-contract course policy it is still only a
    Type1/left-side observation when the higher controlling anchor points the
    other way and its defense remains active.
    """

    current = dict(ledger)
    levels = current.get("trade_levels")
    if not isinstance(levels, Mapping):
        return current
    candidate = levels.get("continuation_arm_candidate")
    if not isinstance(candidate, Mapping):
        return current
    course_direction = {"LONG": "BULL", "SHORT": "BEAR"}.get(
        str(candidate.get("direction"))
    )
    if course_direction is None or not _is_countertrend_to_highest_active_anchor(
        course_direction,
        ledger=current,
    ):
        return current
    normalized_levels = dict(levels)
    normalized_levels["continuation_arm_candidate"] = None
    normalized_levels["countertrend_continuation_observation"] = dict(candidate)
    normalized_levels["countertrend_continuation_reason"] = (
        "TYPE1_HIGHEST_ACTIVE_ANCHOR_NOT_INVALIDATED"
    )
    current["trade_levels"] = normalized_levels
    return current


def _suppress_stale_unseen_continuation_candidate(
    ledger: Mapping[str, Any],
    *,
    previous_memory: Mapping[str, Any] | None,
    as_of: str,
) -> dict[str, Any]:
    """Prevent an old historical candidate from becoming newly actionable.

    A candidate can be hidden for many bars by a higher-grade conflict or an
    open position and then reappear when that context changes.  Its original
    trigger window does not restart.  Only a setup already carried as an
    actionable item in the immediately preceding causal memory may continue
    through its normal expiry check.
    """

    current = dict(ledger)
    levels = current.get("trade_levels")
    candidate = levels.get("continuation_arm_candidate") if isinstance(levels, Mapping) else None
    if not isinstance(candidate, Mapping):
        return current
    setup_key = str(candidate.get("setup_key") or "")
    prior_setups = (
        previous_memory.get("active_setups", [])
        if isinstance(previous_memory, Mapping)
        else []
    )
    was_actionable = any(
        isinstance(item, Mapping)
        and str(item.get("setup_key") or "") == setup_key
        and item.get("stage") in {
            "ARMED", "ENTRY_ELIGIBLE", "AGGRESSIVE_CONFIRMED", "CONSERVATIVE_CONFIRMED",
        }
        for item in prior_setups
    )
    if was_actionable:
        return current
    try:
        first_seen = datetime.fromisoformat(str(candidate.get("first_seen_at")))
        current_at = datetime.fromisoformat(as_of)
        valid_bars = int(candidate.get("valid_bars") or 3)
    except (TypeError, ValueError):
        return current
    if current_at <= first_seen + timedelta(minutes=max(1, valid_bars)):
        return current
    normalized = dict(levels)
    normalized["continuation_arm_candidate"] = None
    normalized["stale_unseen_continuation_candidate"] = dict(candidate)
    normalized["stale_unseen_reason"] = "ORIGINAL_TRIGGER_WINDOW_EXPIRED_BEFORE_FIRST_ACTIONABLE_EXPOSURE"
    current["trade_levels"] = normalized
    return current


def _reconcile_program_reentry_memory(
    memory: Mapping[str, Any] | None,
    *,
    position: Mapping[str, Any],
    as_of: str,
) -> Mapping[str, Any] | None:
    """Repair stale AI labels from the authoritative execution position.

    Older accepted replay points could carry the prior setup's global
    ``USED`` label into a newly opened initial position.  Feeding that stale
    label back to the analyzer makes it repeat the error even though the
    program position correctly reset ``reentry_count``.  This reconciliation
    changes only duplicated re-entry bookkeeping; market structure, setups,
    triggers and trading decisions remain untouched.
    """

    if not isinstance(memory, Mapping) or memory.get("version") != 3:
        return memory
    status, last_stop_at, count = _current_program_reentry_state(position, as_of=as_of)
    current = dict(memory)
    current["reentry"] = {
        "status": status,
        "last_stop_at": last_stop_at,
        "count": count,
    }
    active_key = position.get("active_setup_key")
    setups: list[Any] = []
    for item in memory.get("active_setups", []):
        if not isinstance(item, Mapping):
            setups.append(item)
            continue
        normalized = dict(item)
        if active_key and item.get("setup_key") == active_key:
            normalized["reentry_status"] = status
        if (
            position.get("status") == "FLAT"
            and count >= 1
            and normalized.get("reentry_status") == "USED"
            and normalized.get("stage") not in {"NO_CHASE", "INVALIDATED"}
        ):
            normalized["stage"] = "NO_CHASE"
            normalized["trigger"] = (
                "本setup已使用允許的1次再進場；不得重設為INITIAL或再次觸發。"
            )
        setups.append(normalized)
    current["active_setups"] = setups
    return current


def _program_reentry_expectation(
    position: Mapping[str, Any],
    *,
    protective_stop_audit: Mapping[str, Any] | None,
    constitution_snapshot: Mapping[str, Any] | None = None,
    as_of: str,
) -> dict[str, Any]:
    """Expose the required post-action re-entry labels to the analyzer."""

    stop_audit = (
        protective_stop_audit
        if isinstance(protective_stop_audit, Mapping)
        else {}
    )
    if (
        position.get("status") in {"LONG", "SHORT"}
        and stop_audit.get("status") == "TRIGGERED"
        and isinstance(stop_audit.get("trigger_time"), str)
    ):
        last_stop_at = str(stop_audit["trigger_time"])
        count = int(position.get("reentry_count") or 0)
        status = (
            "NOT_APPLICABLE"
            if isinstance(constitution_snapshot, Mapping)
            and constitution_snapshot.get("trading_locked") is True
            else "USED"
            if count >= 1
            else _time_based_reentry_status(last_stop_at=last_stop_at, as_of=as_of)
        )
        return {
            "authority": "PROGRAM_OWNED",
            "required_position_action": "STOP",
            "setup_key": position.get("active_setup_key"),
            "status_after_action": status,
            "last_stop_at_after_action": last_stop_at,
            "count_after_action": count,
        }
    status, last_stop_at, count = _current_program_reentry_state(position, as_of=as_of)
    if (
        isinstance(constitution_snapshot, Mapping)
        and constitution_snapshot.get("trading_locked") is True
    ):
        status = "NOT_APPLICABLE"
    return {
        "authority": "PROGRAM_OWNED",
        "required_position_action": None,
        "setup_key": position.get("active_setup_key"),
        "status_after_action": status,
        "last_stop_at_after_action": last_stop_at,
        "count_after_action": count,
    }


def _current_program_reentry_state(
    position: Mapping[str, Any],
    *,
    as_of: str,
) -> tuple[str, str | None, int]:
    count = int(position.get("reentry_count") or 0)
    last_stop_at = position.get("last_stop_time")
    if count >= 1:
        return "USED", str(last_stop_at) if last_stop_at else None, count
    if (
        position.get("status") == "FLAT"
        and position.get("active_setup_key")
        and isinstance(last_stop_at, str)
    ):
        return (
            _time_based_reentry_status(last_stop_at=last_stop_at, as_of=as_of),
            last_stop_at,
            0,
        )
    return "NOT_APPLICABLE", None, 0


def _time_based_reentry_status(*, last_stop_at: str, as_of: str) -> str:
    stop_at = datetime.fromisoformat(last_stop_at)
    current = datetime.fromisoformat(as_of)
    return "AVAILABLE" if current - stop_at >= timedelta(minutes=1) else "WAIT_ONE_BAR"


def _bind_continuation_candidate_to_available_reentry(
    ledger: Mapping[str, Any],
    *,
    previous_memory: Mapping[str, Any] | None,
    position: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Keep a post-stop continuation under the originating setup identity.

    A stopped position intentionally retains ``active_setup_key`` for at most
    one course-authorized re-entry. When a fresh correction/continuation
    candidate appears after that stop, the generic structure engine would
    otherwise mint a second setup key for the same opportunity. Bind the
    candidate back only when re-entry is already available, direction agrees,
    no re-entry has been used, and the new stop source formed after the stop.
    """

    current = dict(ledger)
    if not isinstance(previous_memory, Mapping) or not isinstance(position, Mapping):
        return current
    active_key = position.get("active_setup_key")
    last_stop_at = position.get("last_stop_time")
    if (
        position.get("status") != "FLAT"
        or not active_key
        or not last_stop_at
        or position.get("reentry_count") != 0
    ):
        return current
    reentry = previous_memory.get("reentry")
    if (
        not isinstance(reentry, Mapping)
        or reentry.get("status") != "AVAILABLE"
        or reentry.get("count") != 0
        or reentry.get("last_stop_at") != last_stop_at
    ):
        return current
    setups = previous_memory.get("active_setups")
    setup_items = setups if isinstance(setups, list) else []
    prior_setup = next(
        (
            item
            for item in setup_items if isinstance(item, Mapping)
            if str(item.get("setup_key")) == str(active_key)
        ),
        None,
    )
    if (
        not isinstance(prior_setup, Mapping)
        or prior_setup.get("reentry_status") != "AVAILABLE"
        or prior_setup.get("stage") not in {
            "FORMING",
            "ARMED",
            "ENTRY_ELIGIBLE",
            "AGGRESSIVE_CONFIRMED",
            "CONSERVATIVE_CONFIRMED",
        }
    ):
        return current

    levels = current.get("trade_levels")
    candidate = (
        levels.get("continuation_arm_candidate")
        if isinstance(levels, Mapping)
        else None
    )
    if (
        not isinstance(candidate, Mapping)
        or candidate.get("candidate_source") not in {
            "ANCHOR_LEG_SEQUENCE",
            "CONFIRMED_PULLBACK_ENDPOINT_N2",
        }
        or candidate.get("direction") != prior_setup.get("direction")
    ):
        return current
    try:
        stop_at = datetime.fromisoformat(str(last_stop_at))
        stop_source_at = datetime.fromisoformat(str(candidate.get("stop_source_time")))
    except (TypeError, ValueError):
        return current
    if stop_source_at <= stop_at:
        return current

    rebound = dict(candidate)
    rebound["setup_key"] = str(active_key)
    rebound["setup_name"] = str(prior_setup.get("name") or candidate.get("setup_name"))
    rebound["entry_role"] = "REENTRY"
    rebound["reentry_binding"] = "ORIGINATING_STOPPED_SETUP"
    normalized_levels = dict(levels)
    normalized_levels["continuation_arm_candidate"] = rebound
    current["trade_levels"] = normalized_levels
    return current


def _suppress_exhausted_reentry_candidate(
    ledger: Mapping[str, Any],
    *,
    previous_memory: Mapping[str, Any] | None,
    position: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Retire the originating setup after its single re-entry is consumed.

    Frozen candidates intentionally survive transient market rebuilds. That
    persistence must end when execution says the one permitted re-entry has
    already been used; otherwise the same stable setup key can reappear as a
    fresh INITIAL trade.
    """

    current = dict(ledger)
    if not isinstance(position, Mapping) or int(position.get("reentry_count") or 0) < 1:
        return current
    levels = current.get("trade_levels")
    if not isinstance(levels, Mapping):
        return current
    candidate = levels.get("continuation_arm_candidate")
    if not isinstance(candidate, Mapping) or not candidate.get("setup_key"):
        return current
    exhausted_keys = {
        str(item.get("setup_key"))
        for item in (
            previous_memory.get("active_setups", [])
            if isinstance(previous_memory, Mapping)
            else []
        )
        if isinstance(item, Mapping)
        and item.get("reentry_status") == "USED"
        and item.get("setup_key")
    }
    setup_key = str(candidate.get("setup_key"))
    if candidate.get("entry_role") != "REENTRY" and setup_key not in exhausted_keys:
        return current
    normalized = dict(levels)
    normalized["continuation_arm_candidate"] = None
    normalized["exhausted_reentry_candidate"] = dict(candidate)
    normalized["exhausted_reentry_reason"] = "ONE_REENTRY_ALREADY_USED"
    current["trade_levels"] = normalized
    return current


def _annotate_course_cclass_constraint(ledger: Mapping[str, Any]) -> dict[str, Any]:
    """Expose a validator-owned C-class reset fact in runtime evidence.

    This does not choose a trading action. It makes an already enforced course
    invariant explicit to the analyzer: a degraded child anchor under opposite
    small-grade Dow control cannot still call a same-direction rebound an
    ordered copy before a replacement reverse anchor exists.
    """

    current = dict(ledger)
    lifecycle = current.get("anchor_lifecycle")
    if not isinstance(lifecycle, Mapping):
        return current
    child = lifecycle.get("child_anchor")
    dow = lifecycle.get("dow_context")
    reverse = lifecycle.get("reverse_candidate")
    opposite = {"BULL": "BEAR", "BEAR": "BULL"}
    requires_reset = (
        isinstance(child, Mapping)
        and child.get("status") in {"DEGRADED", "DEGRADED_RECLAIMED"}
        and child.get("direction") in opposite
        and isinstance(dow, Mapping)
        and dow.get("small_state") == opposite.get(str(child.get("direction")))
        and not isinstance(reverse, Mapping)
    )
    current["course_constraints"] = {
        "required_cclass_mode": "RESETTING" if requires_reset else None,
        "taiji_current_role": "REBUILDING_NOT_ORDERED_COPY" if requires_reset else None,
        "reason": (
            "CHILD_ANCHOR_DEGRADED_OPPOSITE_SMALL_DOW_NO_REVERSE_ANCHOR"
            if requires_reset
            else None
        ),
        "authority": "PROGRAM_DERIVED_FROM_ANCHOR_LIFECYCLE",
    }
    return current


def _validate_retired_setup_keys(
    envelope: Mapping[str, Any],
    *,
    retired_setup_keys: set[str],
) -> None:
    """Reject resurrection of a consumed setup even after it leaves memory."""

    if not retired_setup_keys:
        return
    analysis = envelope.get("analysis")
    action = analysis.get("action") if isinstance(analysis, Mapping) else None
    action_key = action.get("setup_key") if isinstance(action, Mapping) else None
    memory = envelope.get("memory")
    setups = memory.get("active_setups") if isinstance(memory, Mapping) else None
    terminal_keys = {
        str(setup.get("setup_key"))
        for setup in (setups if isinstance(setups, list) else [])
        if isinstance(setup, Mapping)
        and setup.get("setup_key")
        and setup.get("stage") in {"NO_CHASE", "INVALIDATED"}
    }
    terminal_notice = bool(
        isinstance(analysis, Mapping)
        and analysis.get("message_type") == "INVALIDATION"
        and isinstance(action, Mapping)
        and action.get("position_action") == "NONE"
        and (
            str(action_key or "") in terminal_keys
            or (
                isinstance(analysis.get("course_reading"), Mapping)
                and analysis["course_reading"].get("setup_stage") == "INVALIDATED"
            )
        )
    )
    if action_key and str(action_key) in retired_setup_keys and not terminal_notice:
        raise ReplayRunError(
            "retired_setup_reused",
            "已否決或逾期的setup_key不得重新使用；必須等待新的同級修正結構。",
        )
    for setup in setups if isinstance(setups, list) else []:
        if (
            isinstance(setup, Mapping)
            and str(setup.get("setup_key")) in retired_setup_keys
            and setup.get("stage") not in {"NO_CHASE", "INVALIDATED"}
        ):
            raise ReplayRunError(
                "retired_setup_reused",
                "已否決或逾期的setup_key不得重新啟用；新機會必須建立新setup_key。",
            )


def _validate_available_data_references(payload: Mapping[str, Any], *, spot_status: str) -> None:
    if spot_status in {"PREOPEN", "FRESH"}:
        return
    unavailable_markers = ("不可用", "不可得", "未提供", "無資料", "UNAVAILABLE", "無法取得", "無法驗證")
    for text in _string_values(payload):
        if "現貨" not in text:
            continue
        if any(marker in text for marker in unavailable_markers) and "等待" not in text and "需" not in text:
            continue
        raise ReplayRunError(
            "unsupported_spot_reference",
            "回放只有TMF資料；只能註明現貨不可得，不得引用、等待或以現貨作為觸發條件。",
        )


def _validate_latest_ohlc_claims(
    payload: Mapping[str, Any],
    *,
    latest: Any,
) -> None:
    """Field-check prose that explicitly claims the latest bar's OHLC."""

    if not isinstance(latest, Mapping):
        return
    markers = {"開": "open", "高": "high", "低": "low", "收": "close"}
    for value in _string_values(payload):
        if "本根" not in value:
            continue
        latest_clauses = re.finditer(r"本根[^。；\n]*", value)
        for clause_match in latest_clauses:
            prefix = value[: clause_match.start()]
            last_spot_marker = max(prefix.rfind("現貨"), prefix.rfind("加權指數"))
            last_futures_marker = max(prefix.rfind("期貨"), prefix.rfind("TMF"))
            if last_spot_marker >= 0 and last_spot_marker > last_futures_marker:
                # This validator owns the replay instrument's latest OHLC.
                # Spot/index claims are checked against their own structured
                # context and must not be compared with the TMF candle.
                continue
            clause = clause_match.group(0)
            explicit = re.search(
                r"本根(?:已收盤K|K棒|K)?(?:的)?(?P<marker>開|高|低|收)"
                r"(?:盤價|盤|價|點)?\s*(?P<number>[0-9][0-9,]*(?:\.[0-9]+)?)",
                clause,
            )
            if explicit is None:
                # Example:「本根收盤站穩44,875，09:19低點44,780後反彈」。
                # The later low is a cited historical pivot, not this bar's
                # low.  A comma alone must never turn it into an OHLC claim.
                continue
            claims = [explicit]
            if explicit.group("marker") == "開":
                # Compact OHLC prose conventionally starts with 本根開, then
                # lists 高／低／收 without repeating 本根 before every field.
                claims.extend(
                    re.finditer(
                        r"[、，,](?P<marker>高|低|收)"
                        r"(?:盤價|盤|價|點)?\s*(?P<number>[0-9][0-9,]*(?:\.[0-9]+)?)",
                        clause[explicit.end() :],
                    )
                )
            for match in claims:
                marker = match.group("marker")
                field = markers[marker]
                claimed = float(match.group("number").replace(",", ""))
                actual = latest.get(field)
                if (
                    isinstance(actual, bool)
                    or not isinstance(actual, (int, float))
                    or abs(claimed - float(actual)) > 1e-6
                ):
                    raise ReplayRunError(
                        "latest_ohlc_mismatch",
                        f"本根{marker}價必須逐字符合程式化latest_closed_k.{field}。",
                    )


def _deterministic_constraints(structured: Mapping[str, Any], *, stage: str,
                               ledger: Mapping[str, Any] | None = None) -> dict[str, Any]:
    causal = structured.get("causal_structure_n2")
    causal = causal if isinstance(causal, Mapping) else {}
    constraints: dict[str, Any] = {
        "dow_small": causal.get("dow_small") or "UNDEFINED",
        "dow_large": causal.get("dow_large") or "UNDEFINED",
    }
    course_authority = isinstance(ledger, Mapping) and ledger.get("dow_authority") == "COURSE_ANCHOR_LIFECYCLE"
    if course_authority:
        constraints.update(dow_small=ledger["dow"]["small"], dow_large=ledger["dow"]["large"],
            dow_status_source="COURSE_ANCHOR_LIFECYCLE",
            legacy_causal_dow_role="DIAGNOSTIC_ONLY_NOT_AN_ADDITIONAL_GATE")
    if stage == "preopen":
        constraints.update(
            {
                "session_status": "NIGHT_CLOSED_DAY_NOT_OPEN",
                "next_valid_futures_bar": "08:45",
                "position_status": "FLAT",
                "active_setup": None,
                "required_decision": "NOTIFY",
            }
        )
        if not course_authority and constraints["dow_large"] == "TRANSITION":
            constraints["allowed_large_trend"] = ["盤整"]
            constraints["allowed_large_memory_direction"] = ["RANGE", "UNDEFINED"]
    return constraints


def _validate_replay_semantics(
    envelope: Mapping[str, Any],
    *,
    structured: Mapping[str, Any],
    stage: str,
) -> None:
    analysis = envelope["analysis"]
    memory = envelope["memory"]
    causal = structured.get("causal_structure_n2")
    causal = causal if isinstance(causal, Mapping) else {}
    dow_small = causal.get("dow_small")
    expected_small = {
        # A confirmed direction is necessary for a matching visible directional
        # label, but the formal rules permit a conservative RANGE/TRANSITION
        # label when moving averages, 5/15-minute structure or session context
        # conflict.  Do not turn that one-way requirement into equivalence.
        "BULL": ("BULL", {"偏多", "盤整", "轉換中"}),
        "BEAR": ("BEAR", {"偏空", "盤整", "轉換中"}),
        "TRANSITION": (None, {"盤整", "轉換中"}),
    }.get(dow_small)
    if expected_small is not None:
        memory_direction, allowed_current = expected_small
        actual_memory_direction = memory["small_structure"]["direction"]
        if memory_direction is None:
            if actual_memory_direction not in {"RANGE", "UNDEFINED"}:
                raise ReplayRunError("small_structure_conflict", "小級memory方向與權威道氏狀態不一致。")
        elif actual_memory_direction != memory_direction:
            raise ReplayRunError("small_structure_conflict", "小級memory方向與權威道氏狀態不一致。")
        if analysis["current_trend"]["classification"] not in allowed_current:
            raise ReplayRunError("current_trend_conflict", "當前趨勢分類與權威小級道氏狀態不一致。")

    current_classification = analysis["current_trend"]["classification"]
    if current_classification == "偏多" and dow_small != "BULL":
        raise ReplayRunError("current_trend_conflict", "偏多當前趨勢缺少權威小級多頭道氏狀態。")
    if current_classification == "偏空" and dow_small != "BEAR":
        raise ReplayRunError("current_trend_conflict", "偏空當前趨勢缺少權威小級空頭道氏狀態。")

    flip_text = str(memory["thesis"]["flip"])
    upper_break = re.search(r"(?:突破|站上|收復).{0,18}(?:夜盤高|高點|箱頂|上緣)", flip_text)
    lower_break = re.search(r"(?:跌破|失守).{0,18}(?:夜盤低|低點|箱底|下緣)", flip_text)
    bearish_failure_markers = ("假突破", "無法守住", "跌回", "跌破", "失敗")
    bullish_failure_markers = ("假跌破", "收復", "站回", "無法延續", "失敗")
    if upper_break and "翻空" in flip_text and not any(marker in flip_text for marker in bearish_failure_markers):
        raise ReplayRunError("thesis_flip_conflict", "看法翻向條件把有效向上突破錯寫成翻空。")
    if lower_break and "翻多" in flip_text and not any(marker in flip_text for marker in bullish_failure_markers):
        raise ReplayRunError("thesis_flip_conflict", "看法翻向條件把有效向下跌破錯寫成翻多。")

    if stage != "preopen":
        return
    if memory["position"]["status"] != "FLAT" or memory["active_setup"] is not None:
        raise ReplayRunError("preopen_state_invalid", "盤前夜盤快照不得保留持倉或作用中setup。")
    if causal.get("dow_large") == "TRANSITION":
        if analysis["large_trend"]["classification"] != "盤整":
            raise ReplayRunError("preopen_large_trend_conflict", "盤前大級道氏為TRANSITION時，大趨勢必須維持盤整。")
        if memory["large_structure"]["direction"] not in {"RANGE", "UNDEFINED"}:
            raise ReplayRunError("preopen_large_structure_conflict", "盤前大級memory方向不得覆蓋TRANSITION狀態。")
    invalid_next_bar_markers = ("下一根1分K", "下一根 1 分 K", "下一根已收盤K", "下一根已收盤 K")
    for text in _string_values(envelope):
        invalid_0500 = (
            re.search(r"05:00.{0,16}(?:未收盤|下一根)", text) is not None
            or re.search(r"(?:未收盤|下一根).{0,16}05:00", text) is not None
        )
        if invalid_0500:
            raise ReplayRunError("preopen_session_time_invalid", "夜盤已收盤，不得把05:00描述為未收盤K。")
        if any(marker in text for marker in invalid_next_bar_markers) and "08:45" not in text:
            raise ReplayRunError("preopen_next_bar_invalid", "盤前下一個有效期貨K是日盤08:45，不得沿用夜盤下一根觸發。")


def _string_values(value: Any):
    if isinstance(value, Mapping):
        for item in value.values():
            yield from _string_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from _string_values(item)
    elif isinstance(value, str):
        yield value


def _requires_forced_trade_event_output(
    *,
    entry_gate: Mapping[str, Any] | None,
    protective_stop_audit: Mapping[str, Any] | None,
    program_behavior_exit_audit: Mapping[str, Any] | None,
) -> bool:
    """Expose execution events even when they occur between AI cadence bars."""

    return bool(
        (
            isinstance(entry_gate, Mapping)
            and entry_gate.get("status") == "ENTRY_ELIGIBLE"
        )
        or (
            isinstance(protective_stop_audit, Mapping)
            and protective_stop_audit.get("status") == "TRIGGERED"
        )
        or (
            isinstance(program_behavior_exit_audit, Mapping)
            and program_behavior_exit_audit.get("status")
            in {"PENDING_FILL", "TRIGGERED"}
        )
    )


def _program_stop_fill_event(
    protective_stop_audit: Mapping[str, Any],
    *,
    recorded_at: str,
) -> dict[str, Any]:
    """Build the canonical STOP_FILLED event before the AI presentation turn."""

    entry_price = protective_stop_audit.get("entry_price")
    fill_price = protective_stop_audit.get("fill_price")
    direction = protective_stop_audit.get("direction")
    realized_points = None
    if (
        isinstance(entry_price, (int, float))
        and not isinstance(entry_price, bool)
        and isinstance(fill_price, (int, float))
        and not isinstance(fill_price, bool)
    ):
        if direction == "LONG":
            realized_points = float(fill_price) - float(entry_price)
        elif direction == "SHORT":
            realized_points = float(entry_price) - float(fill_price)
    return {
        "event_type": "STOP_FILLED",
        "event_id": protective_stop_audit.get("event_id"),
        "setup_key": protective_stop_audit.get("setup_key"),
        "direction": protective_stop_audit.get("direction"),
        "entry_time": protective_stop_audit.get("entry_time"),
        "entry_price": protective_stop_audit.get("entry_price"),
        "stop_price": protective_stop_audit.get("stop_price"),
        "trigger_time": protective_stop_audit.get("trigger_time"),
        "fill_time": protective_stop_audit.get("fill_time"),
        "fill_price": protective_stop_audit.get("fill_price"),
        "gap_through_stop": protective_stop_audit.get("gap_through_stop"),
        "realized_points": realized_points,
        "exit_classification": (
            "PROTECTIVE_PROFIT_EXIT"
            if realized_points is not None and realized_points > 0
            else "LOSS_OR_BREAKEVEN_STOP"
        ),
        "recorded_at": recorded_at,
    }


def _apply_program_stop_before_ai(
    *,
    run_dir: Path,
    protective_stop_audit: Mapping[str, Any],
    recorded_at: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    """Persist a triggered stop and expose any constitution lock to this AI turn."""

    event = _program_stop_fill_event(
        protective_stop_audit,
        recorded_at=recorded_at,
    )
    _append_execution_event_once(run_dir / "execution-events.jsonl", event)
    snapshot, lock_event = apply_program_execution_event(
        run_dir / "constitution-state.json",
        event,
    )
    if lock_event is not None:
        _append_execution_event_once(
            run_dir / "execution-events.jsonl",
            lock_event,
        )
    return snapshot, event, lock_event


def _saved_raw_revalidation_enabled(*, reuse_saved_raw: bool, program_only: bool) -> bool:
    """Saved AI output never replaces a deterministic hidden one-minute tick."""

    return bool(reuse_saved_raw and not program_only)


def _suppress_redundant_analysis_after_exit_fill(
    payload: dict[str, Any],
    *,
    exit_fill_event: Mapping[str, Any] | None,
) -> bool:
    """Let the deterministic exit-fill card own a same-minute quiet update."""

    if (
        not isinstance(exit_fill_event, Mapping)
        or exit_fill_event.get("event_type") != "EXIT_FILLED"
        or payload.get("message_type") not in {"OBSERVATION", "UNCHANGED"}
        or payload.get("original_decision") != "NOTIFY"
    ):
        return False
    payload["original_decision"] = "DONT_NOTIFY"
    return True


def _message_is_current(manifest: Mapping[str, Any], message: Mapping[str, Any]) -> bool:
    if message.get("stage") != "day":
        return bool(manifest.get("preopen_completed"))
    selected = list(manifest.get("selected_bar_times") or [])
    try:
        index = selected.index(message.get("bar_time"))
    except ValueError:
        if message.get("source") not in {
            "programmatic_entry_fill",
            "programmatic_exit_fill",
        }:
            return False
        message_time = str(message.get("bar_time") or "")
        # A next-open fill can sit between two selected analysis cutoffs.  It
        # belongs to the first selected cutoff at or after the fill.  Only
        # consider it current after that cutoff has completed; a rewind before
        # the covering cutoff still makes the fill superseded.
        index = next(
            (position for position, selected_time in enumerate(selected) if str(selected_time) >= message_time),
            len(selected),
        )
    return index < int(manifest.get("next_day_index") or 0)


def _validate_grounded_level_claims(
    payload: Mapping[str, Any],
    *,
    structured: Mapping[str, Any],
    night_summary: Mapping[str, Any],
    ledger: Mapping[str, Any] | None = None,
) -> None:
    named_levels = {
        "高": float(night_summary["high"]),
        "低": float(night_summary["low"]),
        "收": float(night_summary["close"]),
    }
    if isinstance(night_summary.get("open"), (int, float)):
        named_levels["開"] = float(night_summary["open"])
    numeric_level = r"[\d,]{5,}(?:\.\d+)?"
    price_first = re.compile(rf"(?P<price>{numeric_level})\s*[（(]\s*夜盤(?P<label>開|高|低|收)")
    label_first = re.compile(
        r"夜盤(?P<label>開盤|開|高點|高|低點|低|收盤|收)"
        r"\s*(?:價(?:格)?\s*)?(?:為|是|=|：|:|約|在)?\s*"
        rf"(?P<price>{numeric_level})"
    )
    pivot_after_label = re.compile(
        r"(?P<time>[0-2]\d:[0-5]\d)(?::[0-5]\d\+08:00)?"
        r"[^0-9:。；\n]{0,12}?(?<!新)(?P<label>高點|低點|高|低)"
        rf"\s*(?:為|是|=|：|:|約)?\s*(?P<price>{numeric_level})"
    )
    pivot_before_label = re.compile(
        r"(?P<time>[0-2]\d:[0-5]\d)(?::[0-5]\d\+08:00)?"
        rf"[^0-9:。；\n]{{0,12}}?(?P<price>{numeric_level})"
        r"\s*(?P<label>高點|低點|高|低)"
    )
    bar_level_claim = re.compile(
        r"(?P<time>[0-2]\d:[0-5]\d)\s*"
        r"(?:該根|本根|的)?\s*"
        r"(?P<label>開盤價|開盤|收盤價|收盤|高點|低點|開|收|高|低)\s*"
        r"(?:為|是|=|：|:|約)?\s*"
        rf"(?P<price>{numeric_level})"
    )
    structure = structured.get("causal_structure_n2")
    pivot_prices: dict[tuple[str, str], set[float]] = {}
    if isinstance(structure, Mapping):
        for group in ("recent_confirmed_pivots", "recent_large_pivots"):
            for pivot in structure.get(group) or []:
                if not isinstance(pivot, Mapping):
                    continue
                try:
                    hhmm = datetime.fromisoformat(str(pivot["bar_time"])).strftime("%H:%M")
                    kind = "高" if pivot.get("kind") == "HIGH" else "低"
                    price = float(pivot["price"])
                except (KeyError, TypeError, ValueError):
                    continue
                pivot_prices.setdefault((hhmm, kind), set()).add(price)
    bar_levels: dict[str, Mapping[str, Any]] = {}
    if isinstance(ledger, Mapping):
        for bar in ledger.get("recent_bar_levels") or []:
            if not isinstance(bar, Mapping):
                continue
            try:
                hhmm = datetime.fromisoformat(str(bar["time"])).strftime("%H:%M")
            except (KeyError, TypeError, ValueError):
                continue
            bar_levels[hhmm] = bar
    bar_key_by_label = {
        "開盤價": "open",
        "開盤": "open",
        "開": "open",
        "高點": "high",
        "高": "high",
        "低點": "low",
        "低": "low",
        "收盤價": "close",
        "收盤": "close",
        "收": "close",
    }
    for text in _string_values(payload):
        for pattern in (price_first, label_first):
            for match in pattern.finditer(text):
                label = match.group("label")[0]
                actual = float(match.group("price").replace(",", ""))
                if label not in named_levels:
                    continue
                if abs(actual - named_levels[label]) > 0.01:
                    raise ReplayRunError(
                        "ungrounded_night_level",
                        f"標示為夜盤{label}的價位與官方資料不一致。",
                    )
        for pivot_pattern in (pivot_after_label, pivot_before_label):
            for match in pivot_pattern.finditer(text):
                label = match.group("label")[0]
                expected = pivot_prices.get((match.group("time"), label))
                actual = float(match.group("price").replace(",", ""))
                if expected and all(abs(actual - price) > 0.01 for price in expected):
                    raise ReplayRunError(
                        "ungrounded_pivot_level",
                        "引用的因果樞紐時間與價位不一致。",
                    )
        for match in bar_level_claim.finditer(text):
            claim_prefix = re.split(r"[。；;\n]", text[: match.start()])[-1]
            if re.search(r"現貨|加權(?:指數)?|TAIEX", claim_prefix, re.IGNORECASE):
                # Spot-market bars have their own official feed.  A timestamp
                # shared with the futures session must not be validated
                # against TMF OHLC values.
                continue
            bar = bar_levels.get(match.group("time"))
            if not isinstance(bar, Mapping):
                continue
            key = bar_key_by_label[match.group("label")]
            expected = bar.get(key)
            if (
                isinstance(expected, (int, float))
                and abs(float(match.group("price").replace(",", "")) - float(expected)) > 0.01
            ):
                raise ReplayRunError(
                    "ungrounded_bar_level",
                    (
                        f"{match.group('time')}標為{match.group('label')}"
                        f"{match.group('price')}，但該根{key}為{float(expected):g}；"
                        "K棒時間、開高低收角色與價位不一致。"
                    ),
                )


def _normalize_session_open_role_labels(
    payload: Any,
    *,
    ledger: Mapping[str, Any],
) -> int:
    """Correct only provably mislabelled session-open anchor origins.

    AI raw artifacts remain untouched.  The validated semantic payload may
    normalize a HIGH/LOW label to SESSION_OPEN when the deterministic anchor
    and the source candle prove that the quoted price is the open and is not
    the quoted high/low.
    """

    bars_by_time: dict[str, Mapping[str, Any]] = {}
    for bar in ledger.get("recent_bar_levels") or []:
        if not isinstance(bar, Mapping):
            continue
        try:
            hhmm = datetime.fromisoformat(str(bar["time"])).strftime("%H:%M")
        except (KeyError, TypeError, ValueError):
            continue
        bars_by_time[hhmm] = bar
    origins: list[tuple[str, float]] = []
    for anchor in ledger.get("anchor_records") or []:
        if not isinstance(anchor, Mapping) or anchor.get("anchor_origin_kind") != "SESSION_OPEN":
            continue
        try:
            origins.append(
                (
                    datetime.fromisoformat(str(anchor["origin_time"])).strftime("%H:%M"),
                    float(anchor["origin_price"]),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    if not origins:
        return 0
    numeric_level = r"[\d,]{5,}(?:\.\d+)?"
    count = 0

    def normalize(value: Any) -> Any:
        nonlocal count
        if isinstance(value, dict):
            for key, item in list(value.items()):
                value[key] = normalize(item)
            return value
        if isinstance(value, list):
            for index, item in enumerate(value):
                value[index] = normalize(item)
            return value
        if not isinstance(value, str):
            return value
        text = value
        for hhmm, origin_price in origins:
            bar = bars_by_time.get(hhmm)
            if not isinstance(bar, Mapping) or not _same_number(bar.get("open"), origin_price):
                continue
            pattern = re.compile(
                rf"(?P<time>{re.escape(hhmm)})(?P<link>\s*(?:的)?\s*)"
                rf"(?P<label>高點|低點|高|低)\s*(?P<price>{numeric_level})"
            )

            def replace(match: re.Match[str]) -> str:
                nonlocal count
                actual = float(match.group("price").replace(",", ""))
                label_key = "high" if match.group("label").startswith("高") else "low"
                if not _same_number(actual, origin_price) or _same_number(bar.get(label_key), actual):
                    return match.group(0)
                count += 1
                return (
                    f"{match.group('time')}{match.group('link')}開盤價"
                    f"{match.group('price')}"
                )

            text = pattern.sub(replace, text)
        return text

    normalize(payload)
    return count


def _same_number(left: Any, right: Any, *, tolerance: float = 0.01) -> bool:
    try:
        return abs(float(left) - float(right)) <= tolerance
    except (TypeError, ValueError):
        return False


def _validate_v3_obstacles(
    payload: Mapping[str, Any],
    *,
    ledger: Mapping[str, Any],
    night_summary: Mapping[str, Any],
    position: Mapping[str, Any] | None = None,
) -> None:
    """Reject numeric obstacle levels that were not visible at this cutoff."""

    known: set[float] = set()
    latest = ledger.get("latest_closed_k")
    for key in ("open", "high", "low", "close"):
        value = latest.get(key) if isinstance(latest, Mapping) else None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            known.add(float(value))
    # A provisional endpoint is already visible at this cutoff and may be the
    # most useful nearby checkpoint even though n=2 has not confirmed it yet.
    # Keep obstacle grounding aligned with setup-trigger grounding, which has
    # always accepted the same causal working-pivot collection.
    for collection in (
        "pivots",
        "working_pivots",
        "legs",
        "defenses",
        "structure_events",
        "anchor_records",
        "recent_bar_levels",
    ):
        for item in ledger.get(collection, []) if isinstance(ledger.get(collection), list) else []:
            if not isinstance(item, Mapping):
                continue
            for key in (
                "price", "start_price", "end_price", "parent_origin_price",
                "absorbed_defense_price", "replacement_defense_price", "reclaimed_boundary_price",
                "origin_price", "first_extreme_price", "latest_extreme_price", "current_extreme_price",
                "open", "high", "low", "close",
            ):
                value = item.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    known.add(float(value))
            nested_defense = item.get("defense")
            if isinstance(nested_defense, Mapping):
                value = nested_defense.get("price")
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    known.add(float(value))
            defense_candidate = item.get("defense_candidate")
            if isinstance(defense_candidate, Mapping):
                value = defense_candidate.get("price")
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    known.add(float(value))
    # The course-aware anchor lifecycle owns the authoritative large/small
    # Dow slots.  Those defenses are deliberately separate from the legacy
    # flat ``ledger.defenses`` collection, so include their structural price
    # fields explicitly when grounding an obstacle or trigger.
    anchor_lifecycle = ledger.get("anchor_lifecycle")
    if isinstance(anchor_lifecycle, Mapping):
        structural_price_keys = {
            "price",
            "origin_price",
            "first_extreme_price",
            "latest_extreme_price",
            "current_extreme_price",
            "start_price",
            "end_price",
        }
        stack: list[Mapping[str, Any]] = [anchor_lifecycle]
        while stack:
            current = stack.pop()
            for key, value in current.items():
                if isinstance(value, Mapping):
                    stack.append(value)
                elif isinstance(value, list):
                    stack.extend(item for item in value if isinstance(item, Mapping))
                elif (
                    key in structural_price_keys
                    and isinstance(value, (int, float))
                    and not isinstance(value, bool)
                ):
                    known.add(float(value))
    opening = ledger.get("opening_ranges")
    if isinstance(opening, Mapping):
        for value in opening.values():
            if isinstance(value, Mapping):
                for key in ("high", "low"):
                    item = value.get(key)
                    if isinstance(item, (int, float)) and not isinstance(item, bool):
                        known.add(float(item))
    indicators = ledger.get("indicators")
    if isinstance(indicators, Mapping):
        for value in indicators.values():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                known.add(float(value))
    trade_levels = ledger.get("trade_levels")
    if isinstance(trade_levels, Mapping):
        stack: list[Mapping[str, Any]] = [trade_levels]
        allowed_price_keys = {
            "stop_price", "source_price", "breach_extreme", "reclaimed_level", "trigger_level",
        }
        while stack:
            current = stack.pop()
            for key, value in current.items():
                if isinstance(value, Mapping):
                    stack.append(value)
                elif (
                    key in allowed_price_keys
                    and isinstance(value, (int, float))
                    and not isinstance(value, bool)
                ):
                    known.add(float(value))
    # Once a simulated position exists, the execution engine derives a finite
    # course-native behaviour plan from the locked entry and risk.  In
    # particular, Q1/Yizhi uses a minimum-progress checkpoint (for example
    # entry + 0.25R) to enforce the promised fast continuation.  That level is
    # causal and program-owned even though it is not necessarily an OHLC,
    # pivot, MA, or opening-range price.  Accept only the explicitly exposed
    # management fields; do not make arbitrary model projections valid.
    behavior_audit = ledger.get("position_behavior_audit")
    if isinstance(behavior_audit, Mapping):
        for key in (
            "entry_price",
            "stop_price",
            "trigger_level",
            "checkpoint_price",
            "hold_price",
        ):
            value = behavior_audit.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                known.add(float(value))
    if isinstance(position, Mapping):
        for key in ("entry_price", "stop_price"):
            value = position.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                known.add(float(value))
        behavior_plan = position.get("behavior_plan")
        if isinstance(behavior_plan, Mapping):
            trigger_level = behavior_plan.get("trigger_level")
            if isinstance(trigger_level, (int, float)) and not isinstance(trigger_level, bool):
                known.add(float(trigger_level))
            for obstacle in behavior_plan.get("obstacles") or []:
                value = obstacle.get("price") if isinstance(obstacle, Mapping) else None
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    known.add(float(value))
    for key in ("open", "high", "low", "close"):
        value = night_summary.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            known.add(float(value))
    action = payload.get("action")
    obstacles = action.get("obstacles") if isinstance(action, Mapping) else None
    reading = payload.get("course_reading")
    working_quadrant = reading.get("working_quadrant") if isinstance(reading, Mapping) else None
    message_direction = payload.get("message_direction")
    lifecycle_quadrants = (
        anchor_lifecycle.get("quadrant_context")
        if isinstance(anchor_lifecycle, Mapping)
        else None
    )
    working_direction = (
        lifecycle_quadrants.get("working_direction")
        if isinstance(lifecycle_quadrants, Mapping)
        else None
    )
    message_is_working_direction = (
        working_direction not in {"BULL", "BEAR"}
        or message_direction == working_direction
    )
    latest_close = latest.get("close") if isinstance(latest, Mapping) else None
    for obstacle in obstacles if isinstance(obstacles, list) else []:
        if not isinstance(obstacle, Mapping):
            continue
        value = obstacle.get("price")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        if not any(abs(float(value) - reference) <= 0.01 for reference in known):
            raise ReplayRunError(
                "ungrounded_obstacle_level",
                f"障礙價位{float(value):,.0f}必須來自本輪已揭露的K棒、樞紐、錨、防線、"
                "開盤區間、均線、夜盤結構或程式鎖定的持倉管理價位。",
            )
        continuation_side = (
            working_quadrant in {"Q1", "Q4"}
            and message_is_working_direction
            and isinstance(latest_close, (int, float))
            and (
                (message_direction == "BULL" and float(value) > float(latest_close))
                or (message_direction == "BEAR" and float(value) < float(latest_close))
            )
        )
        if continuation_side and obstacle.get("role") == "HARD_TARGET":
            raise ReplayRunError(
                "continuation_obstacle_misclassified",
                "Q1／Q4順勢方向的已知前高前低或區間邊界必須先列CHECKPOINT，不得直接作HARD_TARGET否決進場。",
            )


def _validate_v3_trade_levels(
    payload: Mapping[str, Any],
    *,
    ledger: Mapping[str, Any],
    position: Mapping[str, Any] | None = None,
) -> None:
    """Keep executable stop prices tied to deterministic structural evidence."""

    action = payload.get("action")
    if not isinstance(action, Mapping):
        return
    position = position if isinstance(position, Mapping) else {}
    if (
        action.get("position_action") == "NONE"
        and position.get("status") in {"LONG", "SHORT"}
        and isinstance(action.get("stop_price"), (int, float))
        and not isinstance(action.get("stop_price"), bool)
    ):
        trade_levels = ledger.get("trade_levels")
        candidates = (
            trade_levels.get("position_protection_candidates")
            if isinstance(trade_levels, Mapping)
            else None
        )
        candidate = (
            candidates.get(position.get("status"))
            if isinstance(candidates, Mapping)
            else None
        )
        old_stop = position.get("stop_price")
        if isinstance(old_stop, (int, float)) and not isinstance(old_stop, bool):
            allowed_stops = [float(old_stop)]
            candidate_stop = candidate.get("stop_price") if isinstance(candidate, Mapping) else None
            if (
                isinstance(candidate_stop, (int, float))
                and not isinstance(candidate_stop, bool)
                and candidate.get("protection_required") is True
            ):
                improves = (
                    float(candidate_stop) > float(old_stop)
                    if position.get("status") == "LONG"
                    else float(candidate_stop) < float(old_stop)
                )
                if improves:
                    allowed_stops.append(float(candidate_stop))
            if not any(abs(float(action["stop_price"]) - allowed) <= 0.05 for allowed in allowed_stops):
                raise ReplayRunError(
                    "management_stop_not_program_owned",
                    "持倉保護價只能維持舊值，或採用新完成防線在首次可知時鎖定的程式候選；"
                    "不得因後續ATR變動或主觀微調而漂移。",
                )
        return
    if action.get("position_action") != "ENTER":
        return
    direction = action.get("direction")
    if direction not in {"LONG", "SHORT"}:
        return
    stop_price = action.get("stop_price")
    if not isinstance(stop_price, (int, float)) or isinstance(stop_price, bool):
        return
    trade_levels = ledger.get("trade_levels")
    if not isinstance(trade_levels, Mapping):
        return
    reading = payload.get("course_reading")
    reading = reading if isinstance(reading, Mapping) else {}
    candidate: Any = None
    if action.get("entry_role") == "REENTRY":
        expected_direction = "BULL" if direction == "LONG" else "BEAR"
        continuation = trade_levels.get("continuation_arm_candidate")
        if (
            isinstance(continuation, Mapping)
            and continuation.get("direction") == direction
            and continuation.get("setup_key") == action.get("setup_key")
            and isinstance(continuation.get("stop_source_price"), (int, float))
            and not isinstance(continuation.get("stop_source_price"), bool)
        ):
            # A stopped strategy may re-arm from a later, independently
            # confirmed pullback.  The original setup key preserves the
            # one-reentry allowance, but the new causal structure owns the
            # new stop.  Do not validate that stop against an older sweep.
            candidate = {"source_price": continuation["stop_source_price"]}
        else:
            reentry = trade_levels.get("latest_false_break_reentry")
            if isinstance(reentry, Mapping) and reentry.get("direction") == expected_direction:
                candidate = reentry
    elif reading.get("working_quadrant") == "Q4":
        expected_direction = "BULL" if direction == "LONG" else "BEAR"
        continuation = trade_levels.get("continuation_arm_candidate")
        if (
            isinstance(continuation, Mapping)
            and continuation.get("direction") == direction
            and continuation.get("setup_key") == action.get("setup_key")
            and isinstance(continuation.get("stop_source_price"), (int, float))
            and not isinstance(continuation.get("stop_source_price"), bool)
        ):
            # A Q4 continuation is defined by its own complete correction.
            # A generic recent pivot can predate that correction and sit
            # farther outside it; validating against that unrelated pivot
            # falsely rejects the strategy-native stop.  Keep the setup key
            # and direction coupled so one setup can never borrow another
            # setup's shallower boundary.
            candidate = {"source_price": continuation["stop_source_price"]}
        else:
            candidate = trade_levels.get(
                "long_structural_stop" if direction == "LONG" else "short_structural_stop"
            )
    if not isinstance(candidate, Mapping):
        return
    boundary = candidate.get("source_price", candidate.get("breach_extreme"))
    if not isinstance(boundary, (int, float)):
        return
    # The 0.2 ATR buffer is an engineering example, not the course's only
    # lawful stop. Reject stops inside the observed correction/sweep, while
    # allowing a documented full same-grade correction further outside it.
    inside = float(stop_price) >= boundary if direction == "LONG" else float(stop_price) <= boundary
    if inside:
        raise ReplayRunError(
            "structural_stop_mismatch",
            "Q4或假突破再進場的stop_price必須在所引用修正／掃單極值外，不得塞進結構內。",
        )


def _apply_required_profit_protection(
    payload: dict[str, Any],
    *,
    ledger: Mapping[str, Any],
    position: Mapping[str, Any],
) -> None:
    """Apply the program-owned structural or +1.5R stop improvement."""

    side = position.get("status")
    if side not in {"LONG", "SHORT"}:
        return
    action = payload.get("action")
    if not isinstance(action, dict) or action.get("position_action") != "NONE":
        return
    trade_levels = ledger.get("trade_levels")
    candidates = (
        trade_levels.get("position_protection_candidates")
        if isinstance(trade_levels, Mapping)
        else None
    )
    candidate = candidates.get(side) if isinstance(candidates, Mapping) else None
    if not (
        isinstance(candidate, Mapping)
        and candidate.get("protection_required") is True
        and candidate.get("protection_reason")
        in {"NEW_FAVORABLE_DEFENSE", "PROFIT_MILESTONE_1_5R_NEAR_COST"}
    ):
        return
    stop = candidate.get("stop_price")
    if isinstance(stop, bool) or not isinstance(stop, (int, float)):
        return
    old_stop = position.get("stop_price")
    improves = (
        isinstance(old_stop, (int, float))
        and not isinstance(old_stop, bool)
        and (
            (side == "LONG" and float(stop) > float(old_stop))
            or (side == "SHORT" and float(stop) < float(old_stop))
        )
    )
    if not improves:
        return
    action["stop_price"] = float(stop)
    reason = candidate.get("protection_reason")
    if reason == "NEW_FAVORABLE_DEFENSE":
        source_time = str(candidate.get("source_time") or "新完成")
        action["structural_stop"] = (
            f"{source_time}有利方向道氏防線完成；程式將保護停損改善至{float(stop):,.0f}點。"
        )
    else:
        action["structural_stop"] = (
            f"已達+1.5R且尚無更有利的5分K保護位置；依正式規則將停損移至進場成本附近{float(stop):,.0f}點。"
        )
    action["management"] = (
        f"保護停損由{float(old_stop):,.0f}點改善至{float(stop):,.0f}點；後續只可依新的5分K有利結構繼續改善，不得放寬。"
    )
    if payload.get("message_type") == "UNCHANGED":
        payload["message_type"] = "MANAGEMENT"
        payload["original_decision"] = "NOTIFY"


def _apply_replay_delivery_policy(payload: dict[str, Any], *, stage: str) -> bool:
    """Apply replay-only delivery policy without changing trading analysis.

    A pre-open snapshot is a mandatory experiment baseline.  The model still
    produces every analytical field, but the deterministic runner owns whether
    that initial baseline must be rendered and delivered.
    """
    if stage != "preopen" or payload.get("original_decision") == "NOTIFY":
        return False
    payload["original_decision"] = "NOTIFY"
    prior_reason = str(payload.get("notification_reason") or "").strip()
    prefix = "歷史回放開始，固定輸出完整夜盤盤前快照。"
    payload["notification_reason"] = f"{prefix}{prior_reason}"[:500]
    return True


def _interactive_step(result: dict[str, Any]) -> bool:
    print(
        json.dumps(
            {
                "status": "step_completed",
                "bar_time": result.get("bar_time"),
                "decision": result.get("decision"),
                "telegram_status": result.get("delivery", {}).get("status"),
            },
            ensure_ascii=False,
        )
    )
    answer = input("按 Enter 繼續下一根K；輸入 q 暫停：").strip().lower()
    return answer != "q"


def _capture_codex_session(manifest: dict[str, Any], analyzer: Any | None) -> None:
    if not isinstance(analyzer, CodexReplayAnalyzer) or not analyzer.persistent_session:
        return
    manifest["codex_session_id"] = analyzer.session_id
    manifest["codex_session_turn_count"] = analyzer.session_turn_count


def _hybrid_hidden_tick_memory_for_persistence(
    *,
    validated_memory: Mapping[str, Any] | None,
    previous_ai_memory: Mapping[str, Any] | None,
    ai_hybrid: bool,
    program_only: bool,
    forced_trade_event: bool,
) -> tuple[dict[str, Any], str]:
    """Keep AI interpretation authoritative between scheduled AI turns.

    Every hidden one-minute tick updates deterministic structure and execution,
    but its program-generated semantic envelope is not an AI market judgment.
    Preserve the last validated AI memory, which already contains any setup
    expiry/invalidation applied before this call.  A program-owned hard stop or
    behaviour exit may synchronize only transaction lifecycle fields; it still
    cannot rewrite anchors, quadrants, Taiji, X, or scenarios.
    """

    validated = copy.deepcopy(dict(validated_memory or {}))
    if not (ai_hybrid and program_only) or not isinstance(previous_ai_memory, Mapping):
        return validated, "validated_analysis"
    preserved = copy.deepcopy(dict(previous_ai_memory))
    if forced_trade_event:
        for key in ("as_of", "active_setups", "reentry"):
            if key in validated:
                preserved[key] = copy.deepcopy(validated[key])
        return preserved, "prior_ai_plus_program_trade_lifecycle"
    return preserved, "prior_ai_with_program_lifecycle_updates"


def _requires_ai_hybrid_off_cadence_decision(entry_gate: Any) -> bool:
    return isinstance(entry_gate, Mapping) and entry_gate.get("status") == "ENTRY_ELIGIBLE"


def _stage_decision_authority(*, ai_hybrid: bool, analyzer: Any | None) -> str:
    """Resolve configured authority independently from presentation cadence.

    A real ``--program-only`` run has no analyzer. Hidden one-minute ticks in
    an AI-hybrid run still receive the configured analyzer even though they
    normally use a deterministic semantic envelope.
    """

    return "AI_HYBRID" if ai_hybrid and analyzer is not None else "PROGRAM"


def _retire_codex_session(manifest: dict[str, Any], *, reason: str) -> None:
    session_id = manifest.get("codex_session_id")
    if session_id:
        retired = list(manifest.get("retired_codex_sessions") or [])
        retired.append(
            {
                "session_id": str(session_id),
                "turn_count": int(manifest.get("codex_session_turn_count") or 0),
                "reason": reason,
                "retired_at": datetime.now(TAIPEI).isoformat(),
            }
        )
        manifest["retired_codex_sessions"] = retired
    manifest["codex_session_id"] = None
    manifest["codex_session_turn_count"] = 0


def _number(value: object) -> int | float:
    number = float(value)
    return int(number) if number.is_integer() else round(number, 4)


def _safe_error(error: object) -> str:
    text = str(error or "回放失敗")
    return " ".join(text.split())[:800]
