"""Isolated pure-AI causal TMF replay; never imports Telegram or live monitoring."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping
from uuid import uuid4

import pandas as pd
from jsonschema import Draft202012Validator

from .codex_analyzer import CodexReplayAnalyzer, CodexReplayError
from .pure_ai_source import (
    DAY,
    NIGHT_US,
    PureAISessionData,
    compact_bars,
    completed_overview,
    load_session,
    session_facts,
)
from .state import append_jsonl, read_json, write_json_atomic


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = PROJECT_ROOT / ".runtime" / "trade_monitor_replay" / "pure-ai-v1-config.json"
DEFAULT_RUNTIME = PROJECT_ROOT / ".runtime" / "trade_monitor_replay" / "pure-ai-runs"
SYSTEM_PROMPT = """你是TMF啟蒙課程歷史因果回放的唯一課程判讀模型。程式沒有提供定錨、道氏、象限、太極、戰法或進出場答案；你必須依固定課程契約與本輪截至as_of的資料自行判斷。禁止使用未揭露K棒、外部資料、工具、網路、檔案或事後結果。前次輸出只是前一輪已驗證狀態，可由新證據依課程更新但不可無事件改寫。完整判讀多空市場，只執行LONG_ONLY。不得使用固定點數、固定分鐘或固定K數代替課程結構。文字證據以足以稽核的一句話為限；未成立戰法只需簡述最關鍵缺件，不重複長篇解釋。最終只能輸出符合外部JSON schema的單一JSON object，不得輸出Markdown、解說或思考過程。"""

PURE_AI_CONTINUATION_INSTRUCTION = """<PURE_AI_CAUSAL_CONTINUATION>
延續同一交易日時段、同一課程契約、同一schema與LONG_ONLY規則。上一個assistant JSON是前次已驗證狀態；本輪只可依下方截至as_of的新因果資料更新。不得使用未揭露K、外部資料或事後結果，不得憑固定點數、分鐘或K數裁決結構。每輪仍須在內部完整掃描全部課程方法，但文字證據保持簡潔；沒有實質變化可承接原狀。程式未提供策略候選或gate，定錨、道氏、象限、太極、主控戰法與交易決定仍完全由你判讀。輸出完整符合既定schema的單一JSON。
</PURE_AI_CAUSAL_CONTINUATION>"""


class PureAIRunError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PureAIConfig:
    model: str
    reasoning_effort: str
    timeout_seconds: float
    analysis_every_bars: int
    current_bar_tail: int
    background_bar_tail: int
    contract_path: Path
    schema_path: Path
    rule_manifest_path: Path
    cohort_plan_path: Path
    source_paths: dict[str, Path]
    point_value_ntd: float
    round_trip_cost_ntd: float
    slippage_scenarios_points_per_side: tuple[float, ...]
    persistent_session: bool = True
    max_session_turns: int = 8


def load_config(path: Path = DEFAULT_CONFIG) -> PureAIConfig:
    config_path = Path(path).resolve()
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PureAIRunError("config_invalid", "純AI回放設定檔無法讀取。") from exc
    if not isinstance(raw, dict):
        raise PureAIRunError("config_invalid", "純AI回放設定必須是JSON object。")

    def project_path(value: Any, label: str) -> Path:
        candidate = Path(str(value or ""))
        if not candidate.is_absolute():
            candidate = PROJECT_ROOT / candidate
        candidate = candidate.resolve()
        if not candidate.exists():
            raise PureAIRunError("config_path_missing", f"{label}不存在。")
        return candidate

    sources_raw = raw.get("source_paths")
    if not isinstance(sources_raw, dict):
        raise PureAIRunError("source_paths_missing", "source_paths尚未設定。")
    sources = {str(key): project_path(value, f"source_paths.{key}") for key, value in sources_raw.items()}
    cadence = int(raw.get("analysis_every_bars", 2))
    if cadence < 1 or cadence > 15:
        raise PureAIRunError("cadence_invalid", "analysis_every_bars只允許1至15。")
    scenarios = tuple(float(item) for item in raw.get("slippage_scenarios_points_per_side", [0, 1, 2]))
    if not scenarios or any(value < 0 for value in scenarios):
        raise PureAIRunError("slippage_invalid", "滑價情境必須是非負數。")
    return PureAIConfig(
        model=str(raw.get("model") or "gpt-5.6-sol"),
        reasoning_effort=str(raw.get("reasoning_effort") or "medium").lower(),
        timeout_seconds=float(raw.get("timeout_seconds", 300)),
        analysis_every_bars=cadence,
        current_bar_tail=int(raw.get("current_bar_tail", 180)),
        background_bar_tail=int(raw.get("background_bar_tail", 180)),
        contract_path=project_path(raw.get("contract_path"), "contract_path"),
        schema_path=project_path(raw.get("schema_path"), "schema_path"),
        rule_manifest_path=project_path(raw.get("rule_manifest_path"), "rule_manifest_path"),
        cohort_plan_path=project_path(raw.get("cohort_plan_path"), "cohort_plan_path"),
        source_paths=sources,
        point_value_ntd=float(raw.get("point_value_ntd", 10)),
        round_trip_cost_ntd=float(raw.get("round_trip_cost_ntd", 50)),
        slippage_scenarios_points_per_side=scenarios,
        persistent_session=bool(raw.get("persistent_session", True)),
        max_session_turns=max(1, int(raw.get("max_session_turns", 8))),
    )


def load_cohort_plan(config: PureAIConfig) -> dict[str, Any]:
    try:
        plan = json.loads(config.cohort_plan_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PureAIRunError("cohort_plan_invalid", "純AI cohort plan無法讀取。") from exc
    if not isinstance(plan, dict) or not isinstance(plan.get("sessions"), list):
        raise PureAIRunError("cohort_plan_invalid", "純AI cohort plan格式錯誤。")
    keys = [str(item.get("session_key") or "") for item in plan["sessions"] if isinstance(item, dict)]
    if not keys or len(keys) != len(set(keys)):
        raise PureAIRunError("cohort_plan_invalid", "session_key缺失或重複。")
    return plan


def validate_plan(config: PureAIConfig) -> dict[str, Any]:
    plan = load_cohort_plan(config)
    contract = config.contract_path.read_text(encoding="utf-8")
    schema_text = config.schema_path.read_text(encoding="utf-8")
    schema = json.loads(schema_text)
    Draft202012Validator.check_schema(schema)
    try:
        rule_manifest = json.loads(config.rule_manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PureAIRunError("rule_manifest_invalid", "純AI規則manifest無法讀取。") from exc
    if rule_manifest.get("contract_sha256") != _sha256_text(contract):
        raise PureAIRunError("rule_manifest_contract_mismatch", "規則manifest的課程契約雜湊不一致。")
    if rule_manifest.get("schema_sha256") != _sha256_text(schema_text):
        raise PureAIRunError("rule_manifest_schema_mismatch", "規則manifest的schema雜湊不一致。")
    for source in rule_manifest.get("course_sources") or []:
        source_path = (PROJECT_ROOT / str(source.get("path") or "")).resolve()
        if not source_path.exists() or _sha256_file(source_path) != source.get("sha256"):
            raise PureAIRunError("course_source_hash_mismatch", f"課程來源已改變：{source.get('path')}")
    formal = rule_manifest.get("formal_reference") or {}
    formal_path = (PROJECT_ROOT / str(formal.get("path") or "")).resolve()
    if not formal_path.exists() or _sha256_file(formal_path) != formal.get("sha256"):
        raise PureAIRunError("formal_reference_hash_mismatch", "正式v2.1.8參考已改變。")
    if plan.get("contract_sha256") != _sha256_text(contract):
        raise PureAIRunError("contract_hash_mismatch", "課程契約雜湊與封存plan不一致。")
    if plan.get("schema_sha256") != _sha256_text(schema_text):
        raise PureAIRunError("schema_hash_mismatch", "輸出schema雜湊與封存plan不一致。")
    if plan.get("model") != config.model or plan.get("reasoning_effort") != config.reasoning_effort:
        raise PureAIRunError("model_plan_mismatch", "模型或推理強度與封存plan不一致。")
    if int(plan.get("analysis_every_bars") or 0) != config.analysis_every_bars:
        raise PureAIRunError("cadence_plan_mismatch", "分析頻率與封存plan不一致。")
    if bool(plan.get("persistent_session")) != config.persistent_session:
        raise PureAIRunError("transport_plan_mismatch", "持續對話傳輸設定與封存plan不一致。")
    if int(plan.get("max_session_turns") or 0) != config.max_session_turns:
        raise PureAIRunError("transport_plan_mismatch", "持續對話輪替設定與封存plan不一致。")
    if plan.get("system_prompt_sha256") != _sha256_text(SYSTEM_PROMPT):
        raise PureAIRunError("system_prompt_hash_mismatch", "system prompt與封存plan不一致。")
    if plan.get("continuation_prompt_sha256") != _sha256_text(PURE_AI_CONTINUATION_INSTRUCTION):
        raise PureAIRunError("continuation_prompt_hash_mismatch", "延續提示詞與封存plan不一致。")
    if plan.get("prompt_template_sha256") != _sha256_text(_build_prompt(contract, {})):
        raise PureAIRunError("prompt_template_hash_mismatch", "完整提示詞模板與封存plan不一致。")
    counts: dict[str, int] = {DAY: 0, NIGHT_US: 0}
    classifications: dict[str, int] = {}
    months: dict[str, set[str]] = {}
    validated: list[dict[str, Any]] = []
    for item in plan["sessions"]:
        if not isinstance(item, dict):
            raise PureAIRunError("cohort_plan_invalid", "session項目格式錯誤。")
        month = str(item["source_month"])
        if month not in config.source_paths:
            raise PureAIRunError("cohort_source_missing", f"找不到{month}資料來源。")
        source = config.source_paths[month]
        if _sha256_file(source) != item.get("source_sha256"):
            raise PureAIRunError("source_hash_mismatch", f"{month}圖表來源雜湊不一致。")
        session_date = date.fromisoformat(str(item["date"]))
        kind = str(item["session_kind"])
        data = load_session(source, session_date=session_date, session_kind=kind)
        if data.session_key != item["session_key"]:
            raise PureAIRunError("session_key_mismatch", f"{item['session_key']}與資料不一致。")
        counts[kind] = counts.get(kind, 0) + 1
        classification = str(item["classification"])
        classifications[classification] = classifications.get(classification, 0) + 1
        months.setdefault(month, set()).add(item["date"])
        validated.append(
            {
                "session_key": data.session_key,
                "classification": classification,
                "session_bars": len(data.session_bars),
                "background_bars": len(data.background_bars),
            }
        )
    if counts != {DAY: 8, NIGHT_US: 8}:
        raise PureAIRunError("cohort_count_invalid", f"時段數不符：{counts}")
    if any(len(months.get(month, set())) != 5 for month in ("2026-07", "2026-08")):
        raise PureAIRunError("cohort_month_days_invalid", "7月與8月都必須恰好5個日期。")
    return {
        "ok": True,
        "status": "plan_valid",
        "contract_sha256": _sha256_text(contract),
        "schema_sha256": _sha256_text(schema_text),
        "counts": counts,
        "classifications": classifications,
        "distinct_dates": {month: sorted(days) for month, days in months.items()},
        "sessions": validated,
        "estimated_ai_calls": sum(
            1 + math.ceil(item["session_bars"] / config.analysis_every_bars)
            for item in validated
        ),
    }


class PureAIReplayRunner:
    def __init__(self, config: PureAIConfig, *, analyzer: Any | None = None, runtime_root: Path = DEFAULT_RUNTIME):
        self.config = config
        self.plan = load_cohort_plan(config)
        self.contract = config.contract_path.read_text(encoding="utf-8")
        self.schema_text = config.schema_path.read_text(encoding="utf-8")
        self.schema = json.loads(self.schema_text)
        Draft202012Validator.check_schema(self.schema)
        self.validator = Draft202012Validator(self.schema)
        self.runtime_root = Path(runtime_root).resolve()
        self.analyzer = analyzer
        validate_plan(config)

    def run_session(
        self,
        session_key: str,
        *,
        resume_run_id: str | None = None,
        max_ai_calls: int | None = None,
    ) -> dict[str, Any]:
        item = self._session_item(session_key)
        data = load_session(
            self.config.source_paths[item["source_month"]],
            session_date=date.fromisoformat(item["date"]),
            session_kind=item["session_kind"],
        )
        if resume_run_id:
            run_dir = self._resolve_run(resume_run_id)
            manifest = read_json(run_dir / "manifest.json")
            if manifest.get("session_key") != session_key:
                raise PureAIRunError("resume_session_mismatch", "run_id與session_key不一致。")
            self._assert_run_identity(manifest, data)
            state = read_json(run_dir / "state.json")
        else:
            run_dir = self._create_run(data)
            manifest = self._new_manifest(data, item)
            state = self._new_state(data)
            write_json_atomic(run_dir / "manifest.json", manifest)
            write_json_atomic(run_dir / "state.json", state)
            self._write_bars(run_dir, data)
        analyzer = self._get_analyzer()
        if hasattr(analyzer, "bind_run"):
            analyzer.bind_run(
                run_dir,
                session_id=state.get("codex_session_id"),
                session_turn_count=int(state.get("codex_session_turn_count") or 0),
            )
        calls_started = 0
        manifest["status"] = "running"
        self._save(run_dir, manifest, state)
        try:
            if state.get("preopen_analysis") is None:
                if _limit_reached(calls_started, max_ai_calls):
                    return self._pause(run_dir, manifest, state, "max_ai_calls")
                output, diagnostics = self._analyze_preopen(analyzer, data, run_dir)
                calls_started += 1
                state["preopen_analysis"] = output
                state["last_analysis"] = output
                state["ai_call_count"] += 1
                self._capture_analyzer_session(state, analyzer, diagnostics)
                append_jsonl(run_dir / "ai-usage.jsonl", {"phase": "PREOPEN", **diagnostics})
                self._save(run_dir, manifest, state)

            analysis_positions = _analysis_positions(len(data.session_bars), self.config.analysis_every_bars)
            while state["next_bar_index"] < len(data.session_bars):
                index = int(state["next_bar_index"])
                if index in analysis_positions and _limit_reached(calls_started, max_ai_calls):
                    return self._pause(run_dir, manifest, state, "max_ai_calls")
                row = data.session_bars.iloc[index]
                execution_events = self._process_bar_open_and_stop(state, row)
                for event in execution_events:
                    append_jsonl(run_dir / "executions.jsonl", event)
                state["events_since_last_analysis"].extend(execution_events)
                state["next_bar_index"] = index + 1
                state["last_processed_bar_time"] = row["bar_time"].isoformat()
                if index in analysis_positions:
                    output, diagnostics = self._analyze_replay(
                        analyzer,
                        data,
                        visible=data.session_bars.iloc[: index + 1],
                        state=state,
                        recent_execution_events=list(state["events_since_last_analysis"]),
                        run_dir=run_dir,
                    )
                    calls_started += 1
                    self._apply_decision(state, output, row)
                    state["last_analysis"] = output
                    state["events_since_last_analysis"] = []
                    state["analysis_count"] += 1
                    state["ai_call_count"] += 1
                    self._capture_analyzer_session(state, analyzer, diagnostics)
                    append_jsonl(
                        run_dir / "decisions.jsonl",
                        {
                            "as_of": row["bar_time"].isoformat(),
                            "decision": output["decision"],
                            "primary_setup": output["primary_setup"],
                        },
                    )
                    append_jsonl(
                        run_dir / "ai-usage.jsonl",
                        {"phase": "REPLAY", "as_of": row["bar_time"].isoformat(), **diagnostics},
                    )
                self._save(run_dir, manifest, state)

            close_event = self._close_at_session_end(state, data.session_bars.iloc[-1])
            if close_event is not None:
                append_jsonl(run_dir / "executions.jsonl", close_event)
            manifest["status"] = "completed"
            manifest["completed_at"] = datetime.now().astimezone().isoformat()
            manifest["ai_call_count"] = state["ai_call_count"]
            manifest["analysis_count"] = state["analysis_count"]
            report = build_session_performance(run_dir, self.config)
            write_json_atomic(run_dir / "performance.json", report)
            self._save(run_dir, manifest, state)
            return self._result(run_dir, manifest, report)
        except (PureAIRunError, CodexReplayError):
            manifest["status"] = "failed"
            self._save(run_dir, manifest, state)
            raise

    def _analyze_preopen(self, analyzer: Any, data: PureAISessionData, run_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
        as_of = data.background_bars.iloc[-1]["bar_time"].isoformat()
        runtime = {
            "phase": "PREOPEN",
            "as_of": as_of,
            "session_key": data.session_key,
            "session_kind": data.session_kind,
            "causal_rule": "Only background bars are complete. The target session has not opened.",
            "background": {
                "facts": session_facts(data.background_bars),
                "completed_15m": completed_overview(data.background_bars),
                "recent_1m": compact_bars(data.background_bars, tail=self.config.background_bar_tail),
            },
            "current_session": {"facts": {}, "recent_1m": {"columns": [], "rows": []}},
            "previous_validated_analysis": None,
            "position": _public_position({"position": None}),
            "execution_events_since_last_analysis": [],
            "analysis_every_bars": self.config.analysis_every_bars,
        }
        return self._invoke(analyzer, runtime, run_dir, artifact_key="preopen")

    def _analyze_replay(
        self,
        analyzer: Any,
        data: PureAISessionData,
        *,
        visible: pd.DataFrame,
        state: dict[str, Any],
        recent_execution_events: list[dict[str, Any]],
        run_dir: Path,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        as_of = visible.iloc[-1]["bar_time"].isoformat()
        runtime = {
            "phase": "REPLAY",
            "as_of": as_of,
            "session_key": data.session_key,
            "session_kind": data.session_kind,
            "causal_rule": "No current-session bar after as_of is included.",
            "background": {
                "preopen_ai_snapshot": state["preopen_analysis"],
                "facts": session_facts(data.background_bars),
            },
            "current_session": {
                "facts": session_facts(visible),
                "completed_15m": completed_overview(visible),
                "recent_1m": compact_bars(visible, tail=self.config.current_bar_tail),
            },
            "previous_validated_analysis": state["last_analysis"],
            "position": _public_position(state),
            "pending_execution": state.get("pending_execution"),
            "prepared_setup": state.get("prepared_setup"),
            "execution_events_since_last_analysis": recent_execution_events,
            "analysis_every_bars": self.config.analysis_every_bars,
        }
        key = "replay-" + visible.iloc[-1]["bar_time"].strftime("%Y%m%d-%H%M")
        return self._invoke(analyzer, runtime, run_dir, artifact_key=key)

    def _invoke(self, analyzer: Any, runtime: dict[str, Any], run_dir: Path, *, artifact_key: str) -> tuple[dict[str, Any], dict[str, Any]]:
        prompt = _build_prompt(self.contract, runtime)
        prompt_path = run_dir / "prompts" / f"{artifact_key}.txt"
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text(prompt, encoding="utf-8")
        generated = analyzer.analyze(prompt)
        raw_path = run_dir / "raw" / f"{artifact_key}.json"
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_text(generated.raw_text + "\n", encoding="utf-8")
        payload = dict(generated.payload)
        self._validate_output(payload, runtime)
        validated_path = run_dir / "validated" / f"{artifact_key}.json"
        write_json_atomic(validated_path, payload)
        return payload, dict(generated.diagnostics)

    def _validate_output(self, payload: dict[str, Any], runtime: dict[str, Any]) -> None:
        errors = sorted(self.validator.iter_errors(payload), key=lambda item: list(item.path))
        if errors:
            detail = errors[0]
            raise PureAIRunError("schema_validation_failed", f"{list(detail.path)}: {detail.message}")
        if payload["as_of"] != runtime["as_of"] or payload["session_key"] != runtime["session_key"]:
            raise PureAIRunError("causal_identity_mismatch", "AI改寫as_of或session_key。")
        if payload["phase"] != runtime["phase"]:
            raise PureAIRunError("phase_mismatch", "AI輸出phase不一致。")
        probabilities = [payload["scenarios"][key]["probability"] for key in ("bull", "range", "bear")]
        if sum(probabilities) != 100:
            raise PureAIRunError("scenario_probability_invalid", "多方、盤整、空方權重合計必須為100。")
        current_rows = runtime["current_session"]["recent_1m"].get("rows") or []
        background_rows = (runtime.get("background") or {}).get("recent_1m", {}).get("rows") or []
        allowed_bars = _allowed_bar_lookup(current_rows + background_rows)
        _add_fact_points(allowed_bars, (runtime.get("current_session") or {}).get("facts") or {})
        _add_fact_points(allowed_bars, (runtime.get("background") or {}).get("facts") or {})
        _add_overview_points(
            allowed_bars,
            (runtime.get("current_session") or {}).get("completed_15m") or {},
        )
        _add_overview_points(
            allowed_bars,
            (runtime.get("background") or {}).get("completed_15m") or {},
        )
        if runtime["phase"] == "REPLAY":
            preopen = (runtime["background"].get("preopen_ai_snapshot") or {})
            for point in _walk_points(preopen):
                allowed_bars.setdefault(point["time"], {})[point["role"]] = float(point["price"])
            previous = runtime.get("previous_validated_analysis") or {}
            for point in _walk_points(previous):
                allowed_bars.setdefault(point["time"], {})[point["role"]] = float(point["price"])
        for point in _walk_points(payload):
            _validate_point(point, allowed_bars, runtime["as_of"])
        action = payload["decision"]["action"]
        position = runtime.get("position") or {}
        is_long = position.get("status") == "LONG"
        if runtime["phase"] == "PREOPEN":
            if action not in {"OBSERVE", "NO_LONG"}:
                raise PureAIRunError("preopen_trade_forbidden", "盤前快照不得建立交易決定。")
            return
        if is_long and action in {"PREPARE_LONG", "ENTER_LONG", "OBSERVE", "NO_LONG", "CANCEL_LONG"}:
            raise PureAIRunError("position_action_invalid", "持有多單時決定與持倉不一致。")
        if not is_long and action in {"HOLD_LONG", "MANAGE_LONG", "EXIT_LONG"}:
            raise PureAIRunError("position_action_invalid", "空手時不得輸出持倉決定。")
        setup = payload.get("primary_setup")
        if action in {"PREPARE_LONG", "ENTER_LONG", "HOLD_LONG", "MANAGE_LONG", "EXIT_LONG"}:
            if not isinstance(setup, dict) or setup.get("direction") != "BULL" or setup.get("strategy") == "NONE":
                raise PureAIRunError("bull_setup_required", "多方交易決定必須有唯一多方主控戰法。")
            if payload["decision"]["setup_id"] != setup["setup_id"] or payload["decision"]["strategy"] != setup["strategy"]:
                raise PureAIRunError("setup_identity_mismatch", "decision與primary_setup不一致。")
        if action == "ENTER_LONG":
            plan = setup.get("plan") if isinstance(setup, dict) else None
            if not isinstance(plan, dict) or setup.get("status") != "TRIGGERED":
                raise PureAIRunError("entry_plan_missing", "ENTER_LONG必須有已觸發且完整的事前計畫。")
            stop = float(plan["structural_stop"]["price"])
            if payload["decision"]["new_stop_price"] != stop:
                raise PureAIRunError("entry_stop_mismatch", "ENTER_LONG的停損與事前計畫不一致。")
            latest_close = float(runtime["current_session"]["facts"]["latest_close"])
            if stop >= latest_close:
                raise PureAIRunError("entry_stop_invalid", "多方初始停損必須低於訊號收盤。")
        new_stop = payload["decision"]["new_stop_price"]
        if action == "MANAGE_LONG" and new_stop is not None:
            old_stop = float(position["stop_price"])
            latest_close = float(runtime["current_session"]["facts"]["latest_close"])
            if float(new_stop) < old_stop or float(new_stop) >= latest_close:
                raise PureAIRunError("managed_stop_invalid", "多方移動停損不得放寬或高於目前收盤。")

    def _process_bar_open_and_stop(self, state: dict[str, Any], row: pd.Series) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        pending = state.get("pending_execution")
        if isinstance(pending, dict):
            if pending["action"] == "ENTER_LONG" and state.get("position") is None:
                stop = float(pending["stop_price"])
                open_price = float(row["open"])
                if open_price <= stop:
                    events.append(
                        _event("ENTRY_REJECTED_GAP", row, pending, fill_price=None, reason="next_open_at_or_below_stop")
                    )
                else:
                    position = {
                        "position_id": "long-" + uuid4().hex[:12],
                        "setup_id": pending["setup_id"],
                        "strategy": pending["strategy"],
                        "entry_role": pending["entry_role"],
                        "entry_signal_time": pending["signal_time"],
                        "entry_time": row["bar_time"].isoformat(),
                        "entry_price": open_price,
                        "initial_stop": stop,
                        "stop_price": stop,
                        "initial_risk_points": open_price - stop,
                        "plan": pending["plan"],
                        "reentry_count": int(pending.get("reentry_count", 0)),
                    }
                    state["position"] = position
                    entry_event = _event(
                        "ENTRY_FILLED", row, pending, fill_price=open_price, reason="next_bar_open"
                    )
                    entry_event["position_id"] = position["position_id"]
                    events.append(entry_event)
            elif pending["action"] == "EXIT_LONG" and state.get("position") is not None:
                events.append(self._exit_position(state, row, float(row["open"]), "EXIT_FILLED", pending["reason"]))
            state["pending_execution"] = None
        position = state.get("position")
        if isinstance(position, dict) and float(row["low"]) <= float(position["stop_price"]):
            fill = min(float(row["open"]), float(position["stop_price"]))
            events.append(self._exit_position(state, row, fill, "STOP_FILLED", "protective_stop_touched"))
        return events

    def _apply_decision(self, state: dict[str, Any], output: dict[str, Any], row: pd.Series) -> None:
        decision = output["decision"]
        action = decision["action"]
        setup = output.get("primary_setup")
        at = row["bar_time"].isoformat()
        if action == "PREPARE_LONG":
            state["prepared_setup"] = setup
        elif action == "ENTER_LONG":
            entry_role = "REENTRY" if state.get("last_stopped_setup_id") == setup["setup_id"] else "INITIAL"
            if entry_role == "REENTRY" and int(state["reentry_count_by_setup"].get(setup["setup_id"], 0)) >= 1:
                raise PureAIRunError("reentry_limit_exceeded", "同一setup最多只允許再進場一次。")
            if entry_role == "REENTRY":
                state["reentry_count_by_setup"][setup["setup_id"]] = 1
            state["pending_execution"] = {
                "action": action,
                "setup_id": setup["setup_id"],
                "strategy": setup["strategy"],
                "stop_price": float(decision["new_stop_price"]),
                "signal_time": at,
                "entry_role": entry_role,
                "reentry_count": 1 if entry_role == "REENTRY" else 0,
                "plan": setup["plan"],
            }
            state["prepared_setup"] = setup
        elif action == "CANCEL_LONG":
            state["prepared_setup"] = None
        elif action == "MANAGE_LONG" and decision["new_stop_price"] is not None:
            state["position"]["stop_price"] = float(decision["new_stop_price"])
        elif action == "EXIT_LONG":
            state["pending_execution"] = {
                "action": action,
                "setup_id": state["position"]["setup_id"],
                "strategy": state["position"]["strategy"],
                "signal_time": at,
                "reason": decision["reason"],
            }

    def _exit_position(
        self,
        state: dict[str, Any],
        row: pd.Series,
        fill_price: float,
        event_type: str,
        reason: str,
    ) -> dict[str, Any]:
        position = dict(state["position"])
        event = {
            "event_type": event_type,
            "event_id": hashlib.sha256(
                f"{event_type}\n{position['position_id']}\n{row['bar_time'].isoformat()}".encode("utf-8")
            ).hexdigest(),
            "position_id": position["position_id"],
            "setup_id": position["setup_id"],
            "strategy": position["strategy"],
            "entry_role": position["entry_role"],
            "entry_time": position["entry_time"],
            "entry_price": position["entry_price"],
            "initial_stop": position["initial_stop"],
            "initial_risk_points": position["initial_risk_points"],
            "fill_time": row["bar_time"].isoformat(),
            "fill_price": fill_price,
            "reason": reason,
        }
        if event_type == "STOP_FILLED":
            state["last_stopped_setup_id"] = position["setup_id"]
        state["position"] = None
        state["prepared_setup"] = None
        return event

    def _close_at_session_end(self, state: dict[str, Any], row: pd.Series) -> dict[str, Any] | None:
        state["pending_execution"] = None
        if state.get("position") is None:
            return None
        return self._exit_position(state, row, float(row["close"]), "SESSION_CLOSE_FILLED", "fixed_session_boundary")

    def _session_item(self, session_key: str) -> dict[str, Any]:
        for item in self.plan["sessions"]:
            if item.get("session_key") == session_key:
                return dict(item)
        raise PureAIRunError("session_not_in_plan", "session_key不在封存cohort plan。")

    def _get_analyzer(self) -> Any:
        if self.analyzer is None:
            self.analyzer = CodexReplayAnalyzer(
                model=self.config.model,
                reasoning_effort=self.config.reasoning_effort,
                timeout_seconds=self.config.timeout_seconds,
                output_schema=self.schema,
                persistent_session=self.config.persistent_session,
                max_session_turns=self.config.max_session_turns,
                system_prompt=SYSTEM_PROMPT,
                continuation_prompt_builder=_build_pure_ai_continuation_prompt,
            )
        return self.analyzer

    @staticmethod
    def _capture_analyzer_session(
        state: dict[str, Any], analyzer: Any, diagnostics: Mapping[str, Any]
    ) -> None:
        session_id = getattr(analyzer, "session_id", None) or diagnostics.get("thread_id")
        if session_id:
            state["codex_session_id"] = str(session_id)
        turn_count = getattr(analyzer, "session_turn_count", None)
        if turn_count is not None:
            state["codex_session_turn_count"] = int(turn_count)

    def _create_run(self, data: PureAISessionData) -> Path:
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        tag = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = self.runtime_root / f"pure-ai-{data.session_key.replace(':', '-')}-{tag}-{uuid4().hex[:6]}"
        path.mkdir(parents=False, exist_ok=False)
        return path

    def _resolve_run(self, run_id: str) -> Path:
        if Path(run_id).name != run_id:
            raise PureAIRunError("run_id_invalid", "run_id格式錯誤。")
        path = (self.runtime_root / run_id).resolve()
        if self.runtime_root not in path.parents or not path.is_dir():
            raise PureAIRunError("run_not_found", "找不到純AI回放run。")
        return path

    def _new_manifest(self, data: PureAISessionData, item: dict[str, Any]) -> dict[str, Any]:
        static_prompt = _build_prompt(self.contract, {})
        return {
            "version": "tmf-enlightenment-pure-ai-run-v1",
            "status": "initialized",
            "session_key": data.session_key,
            "date": data.session_date.isoformat(),
            "session_kind": data.session_kind,
            "classification": item["classification"],
            "source_path": str(data.source_path),
            "source_sha256": data.source_sha256,
            "source_meta": data.source_meta,
            "contract_path": str(self.config.contract_path),
            "contract_sha256": _sha256_text(self.contract),
            "schema_path": str(self.config.schema_path),
            "schema_sha256": _sha256_text(self.schema_text),
            "prompt_template_sha256": _sha256_text(static_prompt),
            "system_prompt_sha256": _sha256_text(SYSTEM_PROMPT),
            "continuation_prompt_sha256": _sha256_text(PURE_AI_CONTINUATION_INSTRUCTION),
            "model": self.config.model,
            "reasoning_effort": self.config.reasoning_effort,
            "analysis_every_bars": self.config.analysis_every_bars,
            "current_bar_tail": self.config.current_bar_tail,
            "background_bar_tail": self.config.background_bar_tail,
            "persistent_session": self.config.persistent_session,
            "max_session_turns": self.config.max_session_turns,
            "point_value_ntd": self.config.point_value_ntd,
            "round_trip_cost_ntd": self.config.round_trip_cost_ntd,
            "slippage_scenarios_points_per_side": list(self.config.slippage_scenarios_points_per_side),
            "future_data_policy": "AI input contains no current-session bar after as_of",
            "telegram_enabled": False,
            "created_at": datetime.now().astimezone().isoformat(),
        }

    @staticmethod
    def _new_state(data: PureAISessionData) -> dict[str, Any]:
        return {
            "version": 1,
            "session_key": data.session_key,
            "next_bar_index": 0,
            "last_processed_bar_time": None,
            "preopen_analysis": None,
            "last_analysis": None,
            "prepared_setup": None,
            "pending_execution": None,
            "position": None,
            "last_stopped_setup_id": None,
            "reentry_count_by_setup": {},
            "events_since_last_analysis": [],
            "analysis_count": 0,
            "ai_call_count": 0,
            "codex_session_id": None,
            "codex_session_turn_count": 0,
        }

    def _assert_run_identity(self, manifest: Mapping[str, Any], data: PureAISessionData) -> None:
        expected = {
            "source_sha256": data.source_sha256,
            "contract_sha256": _sha256_text(self.contract),
            "schema_sha256": _sha256_text(self.schema_text),
            "model": self.config.model,
            "reasoning_effort": self.config.reasoning_effort,
            "analysis_every_bars": self.config.analysis_every_bars,
            "system_prompt_sha256": _sha256_text(SYSTEM_PROMPT),
            "continuation_prompt_sha256": _sha256_text(PURE_AI_CONTINUATION_INSTRUCTION),
            "persistent_session": self.config.persistent_session,
            "max_session_turns": self.config.max_session_turns,
        }
        for key, value in expected.items():
            if manifest.get(key) != value:
                raise PureAIRunError("resume_identity_mismatch", f"resume的{key}已改變。")

    @staticmethod
    def _write_bars(run_dir: Path, data: PureAISessionData) -> None:
        data.background_bars.to_csv(run_dir / "background-bars.csv", index=False, encoding="utf-8-sig")
        data.session_bars.to_csv(run_dir / "session-bars.csv", index=False, encoding="utf-8-sig")

    @staticmethod
    def _save(run_dir: Path, manifest: dict[str, Any], state: dict[str, Any]) -> None:
        manifest["updated_at"] = datetime.now().astimezone().isoformat()
        manifest["ai_call_count"] = state["ai_call_count"]
        manifest["analysis_count"] = state["analysis_count"]
        write_json_atomic(run_dir / "state.json", state)
        write_json_atomic(run_dir / "manifest.json", manifest)

    def _pause(self, run_dir: Path, manifest: dict[str, Any], state: dict[str, Any], reason: str) -> dict[str, Any]:
        manifest["status"] = "paused"
        manifest["pause_reason"] = reason
        self._save(run_dir, manifest, state)
        return self._result(run_dir, manifest, None)

    @staticmethod
    def _result(run_dir: Path, manifest: Mapping[str, Any], report: Mapping[str, Any] | None) -> dict[str, Any]:
        return {
            "ok": manifest.get("status") in {"paused", "completed"},
            "status": manifest.get("status"),
            "run_id": run_dir.name,
            "session_key": manifest.get("session_key"),
            "classification": manifest.get("classification"),
            "ai_call_count": manifest.get("ai_call_count"),
            "analysis_count": manifest.get("analysis_count"),
            "performance": dict(report) if isinstance(report, Mapping) else None,
            "run_directory": str(run_dir),
        }


def build_session_performance(run_dir: Path, config: PureAIConfig) -> dict[str, Any]:
    events = list(_read_jsonl(run_dir / "executions.jsonl"))
    entries = [event for event in events if event.get("event_type") == "ENTRY_FILLED"]
    exits = [
        event
        for event in events
        if event.get("event_type") in {"STOP_FILLED", "EXIT_FILLED", "SESSION_CLOSE_FILLED"}
    ]
    bars = pd.read_csv(run_dir / "session-bars.csv", encoding="utf-8-sig")
    bars["bar_time"] = pd.to_datetime(bars["bar_time"])
    trades: list[dict[str, Any]] = []
    for entry in entries:
        exit_event = next(
            (
                event
                for event in exits
                if event.get("position_id") == entry.get("position_id")
                and event.get("fill_time") >= entry.get("fill_time")
            ),
            None,
        )
        if exit_event is None:
            continue
        entry_price = float(entry["fill_price"])
        exit_price = float(exit_event["fill_price"])
        entry_time = pd.Timestamp(entry["fill_time"])
        exit_time = pd.Timestamp(exit_event["fill_time"])
        held = bars[(bars["bar_time"] >= entry_time) & (bars["bar_time"] <= exit_time)]
        gross_points = exit_price - entry_price
        risk = float(entry["stop_price"])
        initial_risk = entry_price - risk
        mfe = max([0.0, gross_points, *list(held["high"].astype(float) - entry_price)])
        mae = max([0.0, -gross_points, *list(entry_price - held["low"].astype(float))])
        trade = {
            "position_id": entry["position_id"],
            "setup_id": entry["setup_id"],
            "strategy": entry["strategy"],
            "entry_role": entry["entry_role"],
            "signal_time": entry["signal_time"],
            "entry_time": entry["fill_time"],
            "entry_price": entry_price,
            "initial_stop": risk,
            "initial_risk_points": initial_risk,
            "exit_time": exit_event["fill_time"],
            "exit_price": exit_price,
            "exit_reason": exit_event["reason"],
            "gross_points": gross_points,
            "gross_r": gross_points / initial_risk if initial_risk > 0 else None,
            "mfe_points": mfe,
            "mae_points": mae,
            "gross_ntd": gross_points * config.point_value_ntd,
            "base_net_ntd": gross_points * config.point_value_ntd - config.round_trip_cost_ntd,
            "slippage_sensitivity": {
                str(value): gross_points * config.point_value_ntd
                - config.round_trip_cost_ntd
                - 2 * value * config.point_value_ntd
                for value in config.slippage_scenarios_points_per_side
            },
        }
        trades.append(trade)
    return {
        "sample_type": "single_session_pure_ai_causal_replay",
        "trade_count": len(trades),
        "trades": trades,
        "gross_points": sum(item["gross_points"] for item in trades),
        "gross_ntd": sum(item["gross_ntd"] for item in trades),
        "base_net_ntd": sum(item["base_net_ntd"] for item in trades),
        "win_rate_after_base_cost": (
            sum(item["base_net_ntd"] > 0 for item in trades) / len(trades) if trades else None
        ),
        "limitations": [
            "One-minute OHLC cannot reveal intrabar high/low order.",
            "Market impact, queue priority and broker rounding are not modeled.",
            "A single session cannot establish expectancy.",
        ],
    }


def aggregate_performance(run_dirs: Iterable[Path], config: PureAIConfig) -> dict[str, Any]:
    manifests: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    identities: set[tuple[Any, ...]] = set()
    sessions: set[str] = set()
    for path in run_dirs:
        run_dir = Path(path).resolve()
        manifest = read_json(run_dir / "manifest.json")
        if manifest.get("status") != "completed":
            raise PureAIRunError("aggregate_incomplete", f"{run_dir.name}尚未完成。")
        identity = tuple(
            manifest.get(key)
            for key in (
                "contract_sha256",
                "schema_sha256",
                "prompt_template_sha256",
                "system_prompt_sha256",
                "continuation_prompt_sha256",
                "model",
                "reasoning_effort",
                "analysis_every_bars",
                "persistent_session",
                "max_session_turns",
                "point_value_ntd",
                "round_trip_cost_ntd",
            )
        )
        identities.add(identity)
        session_key = str(manifest["session_key"])
        if session_key in sessions:
            raise PureAIRunError("aggregate_duplicate_session", "同一session不可重複計入績效。")
        sessions.add(session_key)
        report = read_json(run_dir / "performance.json")
        for trade in report.get("trades", []):
            trades.append({**trade, "session_key": session_key, "classification": manifest["classification"]})
        manifests.append(manifest)
    if len(identities) != 1:
        raise PureAIRunError("aggregate_identity_mismatch", "不同契約、模型或成本版本不得混合統計。")
    nets = [float(item["base_net_ntd"]) for item in trades]
    winners = [value for value in nets if value > 0]
    losers = [-value for value in nets if value < 0]
    equity = peak = max_drawdown = 0.0
    losing_streak = max_losing_streak = 0
    for net in nets:
        equity += net
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
        losing_streak = losing_streak + 1 if net < 0 else 0
        max_losing_streak = max(max_losing_streak, losing_streak)
    strategies: dict[str, list[dict[str, Any]]] = {}
    for trade in trades:
        strategies.setdefault(str(trade["strategy"]), []).append(trade)
    return {
        "sample_type": "multi_session_pure_ai_causal_replay",
        "session_count": len(sessions),
        "sessions": sorted(sessions),
        "trade_count": len(trades),
        "trades": trades,
        "wins_after_base_cost": len(winners),
        "losses_after_base_cost": len(losers),
        "win_rate_after_base_cost": len(winners) / len(trades) if trades else None,
        "gross_points": sum(float(item["gross_points"]) for item in trades),
        "gross_ntd": sum(float(item["gross_ntd"]) for item in trades),
        "net_ntd": sum(nets),
        "average_win_net_ntd": sum(winners) / len(winners) if winners else None,
        "average_loss_net_ntd": sum(losers) / len(losers) if losers else None,
        "payoff_ratio": (
            (sum(winners) / len(winners)) / (sum(losers) / len(losers))
            if winners and losers
            else None
        ),
        "expectancy_net_ntd_per_trade": sum(nets) / len(trades) if trades else None,
        "profit_factor": sum(winners) / sum(losers) if losers else None,
        "max_drawdown_ntd": max_drawdown,
        "max_consecutive_losses": max_losing_streak,
        "average_mae_points": sum(float(item["mae_points"]) for item in trades) / len(trades) if trades else None,
        "average_mfe_points": sum(float(item["mfe_points"]) for item in trades) / len(trades) if trades else None,
        "largest_trade_share_of_positive_net": (
            max(winners) / sum(winners) if winners else None
        ),
        "strategy_breakdown": {
            strategy: _small_metrics(items) for strategy, items in sorted(strategies.items())
        },
        "evidence_status": "INSUFFICIENT" if len(trades) < 20 else "INITIAL_ONLY",
    }


def _small_metrics(trades: list[dict[str, Any]]) -> dict[str, Any]:
    nets = [float(item["base_net_ntd"]) for item in trades]
    profit = sum(value for value in nets if value > 0)
    loss = -sum(value for value in nets if value < 0)
    return {
        "trade_count": len(trades),
        "net_ntd": sum(nets),
        "win_rate": sum(value > 0 for value in nets) / len(nets) if nets else None,
        "expectancy_ntd": sum(nets) / len(nets) if nets else None,
        "profit_factor": profit / loss if loss else None,
    }


def _build_prompt(contract: str, runtime: Mapping[str, Any]) -> str:
    return f"""<PURE_AI_COURSE_CONTRACT>
{contract}
</PURE_AI_COURSE_CONTRACT>

<EXECUTION_OVERRIDE>
這是隔離歷史回放，不得呼叫工具或發送Telegram。程式沒有提供策略候選、樞紐、錨、防線、象限或進出答案。你必須完整掃描schema固定的全部多方戰法欄位；不適用可簡短填NOT_APPLICABLE，但不得漏欄。市場分析保持多空中立，交易只允許LONG_ONLY。PREOPEN不得交易。REPLAY的ENTER_LONG／EXIT_LONG都是本根收盤決定、下一根開盤才由程式執行；不要猜下一根價格。點位若對應K棒，time、role、price必須逐字符合輸入。輸出前自查錨與道氏防線：若文字聲稱某方向錨起點已造成同級新高／新低或同級結構破壞，對應防線不得無說明地留空；若不具因果資格，須明說缺少哪個同級條件。情境權重合計100。只輸出schema要求的JSON。
</EXECUTION_OVERRIDE>

<RUNTIME_CONTEXT>
{json.dumps(dict(runtime), ensure_ascii=False, separators=(",", ":"))}
</RUNTIME_CONTEXT>
"""


def _build_pure_ai_continuation_prompt(full_prompt: str) -> str:
    """Send only new causal state after the immutable contract was loaded once.

    This changes transport volume, not decision authority: Codex still performs
    every course judgement and returns the complete schema on every turn.
    """

    opening = "<RUNTIME_CONTEXT>"
    closing = "</RUNTIME_CONTEXT>"
    start = full_prompt.find(opening)
    end = full_prompt.rfind(closing)
    if start < 0 or end <= start:
        return full_prompt
    try:
        runtime = json.loads(full_prompt[start + len(opening) : end].strip())
    except (TypeError, json.JSONDecodeError):
        return full_prompt
    if not isinstance(runtime, dict):
        return full_prompt

    cadence = max(1, int(runtime.get("analysis_every_bars") or 1))
    current = runtime.get("current_session") or {}
    recent = current.get("recent_1m") or {}
    recent_rows = list(recent.get("rows") or [])[-max(6, cadence) :]
    previous = runtime.get("previous_validated_analysis") or {}
    compact = {
        "phase": runtime.get("phase"),
        "as_of": runtime.get("as_of"),
        "session_key": runtime.get("session_key"),
        "session_kind": runtime.get("session_kind"),
        "causal_rule": runtime.get("causal_rule"),
        "background_reference": {
            "rule": "盤前完整背景與盤前AI快照已在本對話首輪；仍只作背景。",
            "facts": (runtime.get("background") or {}).get("facts") or {},
        },
        "current_session": {
            "facts": current.get("facts") or {},
            "completed_15m": current.get("completed_15m") or {},
            "new_and_overlap_1m": {
                "columns": recent.get("columns") or [],
                "rows": recent_rows,
            },
        },
        "previous_validated_identity": {
            "as_of": previous.get("as_of"),
            "decision": previous.get("decision"),
            "rule": "完整前次狀態就是緊接本輪之前的assistant JSON，不得無事件改寫。",
        },
        "position": runtime.get("position") or {"status": "FLAT"},
        "pending_execution": runtime.get("pending_execution"),
        "prepared_setup": runtime.get("prepared_setup"),
        "execution_events_since_last_analysis": runtime.get("execution_events_since_last_analysis") or [],
        "analysis_every_bars": cadence,
    }
    return (
        PURE_AI_CONTINUATION_INSTRUCTION
        + "\n<RUNTIME_CONTEXT>\n"
        + json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
        + "\n</RUNTIME_CONTEXT>\n"
    )


def _analysis_positions(length: int, cadence: int) -> set[int]:
    positions = set(range(cadence - 1, length, cadence))
    if length and length - 1 not in positions:
        positions.add(length - 1)
    return positions


def _public_position(state: Mapping[str, Any]) -> dict[str, Any]:
    position = state.get("position")
    if not isinstance(position, Mapping):
        return {"status": "FLAT"}
    return {"status": "LONG", **dict(position)}


def _event(
    event_type: str,
    row: pd.Series,
    pending: Mapping[str, Any],
    *,
    fill_price: float | None,
    reason: str,
) -> dict[str, Any]:
    material = f"{event_type}\n{pending.get('setup_id')}\n{row['bar_time'].isoformat()}"
    return {
        "event_type": event_type,
        "event_id": hashlib.sha256(material.encode("utf-8")).hexdigest(),
        "position_id": None,
        "setup_id": pending.get("setup_id"),
        "strategy": pending.get("strategy"),
        "entry_role": pending.get("entry_role"),
        "signal_time": pending.get("signal_time"),
        "fill_time": row["bar_time"].isoformat(),
        "fill_price": fill_price,
        "stop_price": pending.get("stop_price"),
        "reason": reason,
    }


def _allowed_bar_lookup(rows: list[list[Any]]) -> dict[str, dict[str, float]]:
    result: dict[str, dict[str, float]] = {}
    for row in rows:
        if len(row) < 5:
            continue
        result[str(row[0])] = {
            "OPEN": float(row[1]),
            "HIGH": float(row[2]),
            "LOW": float(row[3]),
            "CLOSE": float(row[4]),
        }
    return result


def _add_fact_points(result: dict[str, dict[str, float]], facts: Mapping[str, Any]) -> None:
    first_time = facts.get("first_bar_time")
    if first_time is not None and facts.get("session_open") is not None:
        result.setdefault(str(first_time), {})["OPEN"] = float(facts["session_open"])
    latest_time = facts.get("latest_bar_time")
    if latest_time is not None and facts.get("latest_close") is not None:
        result.setdefault(str(latest_time), {})["CLOSE"] = float(facts["latest_close"])
    for key, role in (("high_so_far", "HIGH"), ("low_so_far", "LOW")):
        item = facts.get(key)
        if isinstance(item, Mapping) and item.get("time") is not None and item.get("price") is not None:
            result.setdefault(str(item["time"]), {})[role] = float(item["price"])


def _add_overview_points(result: dict[str, dict[str, float]], overview: Mapping[str, Any]) -> None:
    columns = overview.get("columns") or []
    if not columns:
        return
    indexes = {str(name): index for index, name in enumerate(columns)}
    required = {"bucket_start", "open", "high", "high_time", "low", "low_time", "close", "close_time"}
    if not required.issubset(indexes):
        return
    for row in overview.get("rows") or []:
        result.setdefault(str(row[indexes["bucket_start"]]), {})["OPEN"] = float(row[indexes["open"]])
        result.setdefault(str(row[indexes["high_time"]]), {})["HIGH"] = float(row[indexes["high"]])
        result.setdefault(str(row[indexes["low_time"]]), {})["LOW"] = float(row[indexes["low"]])
        result.setdefault(str(row[indexes["close_time"]]), {})["CLOSE"] = float(row[indexes["close"]])


def _walk_points(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        if set(("time", "role", "price")).issubset(value) and value.get("role") in {"OPEN", "HIGH", "LOW", "CLOSE"}:
            yield value
        for child in value.values():
            yield from _walk_points(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_points(child)


def _validate_point(point: Mapping[str, Any], allowed: Mapping[str, Mapping[str, float]], as_of: str) -> None:
    at = str(point["time"])
    if at > as_of:
        raise PureAIRunError("future_point", f"AI引用未來點位{at}。")
    roles = allowed.get(at)
    if roles is None:
        raise PureAIRunError("unknown_point_time", f"AI引用輸入不存在的K棒{at}。")
    expected = roles.get(str(point["role"]))
    if expected is None or abs(expected - float(point["price"])) > 1e-6:
        raise PureAIRunError("point_price_mismatch", f"AI引用的{at} {point['role']}價格不符。")


def _read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _limit_reached(started: int, limit: int | None) -> bool:
    return limit is not None and started >= limit


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("plan")
    run = sub.add_parser("run")
    run.add_argument("--session-key", required=True)
    run.add_argument("--resume-run-id")
    run.add_argument("--max-ai-calls", type=int)
    aggregate = sub.add_parser("aggregate")
    aggregate.add_argument("run_dirs", type=Path, nargs="+")
    return parser


def main() -> int:
    if hasattr(os.sys.stdout, "reconfigure"):
        os.sys.stdout.reconfigure(encoding="utf-8")
    args = _build_parser().parse_args()
    try:
        config = load_config(args.config)
        if args.command == "plan":
            result = validate_plan(config)
        elif args.command == "aggregate":
            result = aggregate_performance(args.run_dirs, config)
        else:
            runner = PureAIReplayRunner(config)
            result = runner.run_session(
                args.session_key,
                resume_run_id=args.resume_run_id,
                max_ai_calls=args.max_ai_calls,
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "status": "failed",
                    "error_code": getattr(exc, "code", "pure_ai_replay_failed"),
                    "error": " ".join(str(exc).split())[:1000],
                },
                ensure_ascii=False,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
