from __future__ import annotations

import json
import copy
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .minimax_analyzer import MiniMaxReplayResult, normalize_tool_arguments, parse_json_object


SYSTEM_PROMPT = """你是台指期歷史逐K回放的主要判讀模型。你只能根據輸入中已揭露的歷史K棒與前次已驗證狀態判斷，不得使用未來資料、外部資料或隱藏知識補價。ai_hybrid_state_continuity_lock.required=true時，必須逐字承接其中指定的大錨與控制級數。不得呼叫任何工具、指令、網路或檔案；最終只能輸出符合指定JSON schema的單一JSON object，不得輸出Markdown、解說或思考過程。所有分析內容使用繁體中文，JSON鍵名與固定enum依schema原樣輸出。"""


class CodexReplayError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class CodexReplayAnalyzer:
    """Run one isolated, structured Codex analysis without loading the project agent rules."""

    def __init__(
        self,
        *,
        model: str,
        reasoning_effort: str,
        timeout_seconds: float,
        output_schema: Mapping[str, Any],
        executable: str = "codex",
        run_command: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        persistent_session: bool = False,
        max_session_turns: int = 32,
        system_prompt: str = SYSTEM_PROMPT,
        continuation_prompt_builder: Callable[[str], str] | None = None,
    ) -> None:
        if not str(model or "").strip():
            raise CodexReplayError("codex_model_missing", "Codex model尚未設定。")
        effort = str(reasoning_effort or "").strip().lower()
        if effort not in {"low", "medium", "high", "xhigh"}:
            raise CodexReplayError("codex_reasoning_invalid", "Codex reasoning effort設定無效。")
        if not output_schema:
            raise CodexReplayError("codex_schema_missing", "Codex output schema尚未設定。")
        self.model = str(model).strip()
        self.reasoning_effort = effort
        self.timeout_seconds = float(timeout_seconds)
        self.output_schema = dict(output_schema)
        self.executable = str(executable or "codex").strip()
        self._run_command = run_command or subprocess.run
        self.persistent_session = bool(persistent_session)
        self.max_session_turns = max(1, int(max_session_turns))
        self.system_prompt = str(system_prompt).strip()
        self.continuation_prompt_builder = continuation_prompt_builder or _continuation_prompt
        if not self.system_prompt:
            raise CodexReplayError("codex_system_prompt_missing", "Codex system prompt尚未設定。")
        self.session_id: str | None = None
        self.session_turn_count = 0
        self._session_workspace: Path | None = None

    def bind_run(
        self,
        run_dir: Path,
        *,
        session_id: str | None = None,
        session_turn_count: int = 0,
    ) -> None:
        """Bind one causal replay run to one resumable Codex conversation."""

        if not self.persistent_session:
            return
        workspace = Path(run_dir) / "codex-session-workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        self._session_workspace = workspace
        self.session_id = str(session_id).strip() if session_id else None
        self.session_turn_count = max(0, int(session_turn_count or 0))

    def reset_session(self) -> None:
        self.session_id = None
        self.session_turn_count = 0

    def analyze(self, prompt: str) -> MiniMaxReplayResult:
        started = time.perf_counter()
        if self.persistent_session and self._session_workspace is None:
            raise CodexReplayError("codex_session_unbound", "Codex延續工作階段尚未綁定回放run。")
        rotated_from_session_id: str | None = None
        if self.persistent_session and self.session_turn_count >= self.max_session_turns:
            rotated_from_session_id = self.session_id
            self.reset_session()
        resume_session = bool(self.persistent_session and self.session_id)
        actual_prompt = self.continuation_prompt_builder(prompt) if resume_session else prompt
        temporary_parent = str(self._session_workspace) if self._session_workspace is not None else None
        with tempfile.TemporaryDirectory(prefix="tmf-codex-replay-", dir=temporary_parent) as temporary:
            temporary_path = Path(temporary)
            schema_path = temporary_path / "output-schema.json"
            output_path = temporary_path / "last-message.json"
            schema_path.write_text(
                json.dumps(self.output_schema, ensure_ascii=False),
                encoding="utf-8",
            )
            if resume_session:
                command: Sequence[str] = (
                    self.executable,
                    "exec",
                    "resume",
                    "--all",
                    str(self.session_id),
                    "-",
                    "--model",
                    self.model,
                    "-c",
                    f'model_reasoning_effort="{self.reasoning_effort}"',
                    "--ignore-user-config",
                    "--ignore-rules",
                    "--skip-git-repo-check",
                    "--output-schema",
                    str(schema_path),
                    "--output-last-message",
                    str(output_path),
                    "--json",
                )
            else:
                base_command = [
                    self.executable,
                    "exec",
                    "-",
                    "--model",
                    self.model,
                    "-c",
                    f'model_reasoning_effort="{self.reasoning_effort}"',
                    "--sandbox",
                    "read-only",
                ]
                if not self.persistent_session:
                    base_command.append("--ephemeral")
                base_command.extend(
                    [
                        "--ignore-user-config",
                        "--ignore-rules",
                        "--skip-git-repo-check",
                        "--output-schema",
                        str(schema_path),
                        "--output-last-message",
                        str(output_path),
                        "--json",
                        "--color",
                        "never",
                        "--cd",
                        str(self._session_workspace or temporary_path),
                    ]
                )
                command = tuple(base_command)
            environment = os.environ.copy()
            environment["PYTHONUTF8"] = "1"
            environment["NO_COLOR"] = "1"
            try:
                completed = self._run_command(
                    list(command),
                    input=f"{self.system_prompt}\n\n{actual_prompt}",
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    capture_output=True,
                    timeout=self.timeout_seconds,
                    check=False,
                    env=environment,
                )
            except subprocess.TimeoutExpired as exc:
                raise CodexReplayError("codex_timeout", "Codex分析逾時，已停止本分析點。") from exc
            except OSError as exc:
                raise CodexReplayError("codex_launch_failed", _safe_error(exc)) from exc
            if completed.returncode != 0:
                detail = _safe_error(_command_failure_text(completed.stderr, completed.stdout))
                raise CodexReplayError("codex_request_failed", detail)
            if not output_path.exists():
                raise CodexReplayError("codex_output_missing", "Codex未產生結構化輸出。")
            raw_text = output_path.read_text(encoding="utf-8").strip()
            if not raw_text:
                raise CodexReplayError("codex_output_empty", "Codex結構化輸出為空。")
            payload, normalization_stats = normalize_tool_arguments(parse_json_object(raw_text))
            events = _jsonl_events(completed.stdout)
            usage = _last_usage(events)
            thread_id = next(
                (
                    str(event.get("thread_id"))
                    for event in events
                    if event.get("type") == "thread.started" and event.get("thread_id")
                ),
                None,
            )
            if self.persistent_session:
                resolved_thread_id = thread_id or self.session_id
                if not resolved_thread_id:
                    raise CodexReplayError(
                        "codex_session_id_missing",
                        "Codex未回傳可延續的工作階段ID。",
                    )
                self.session_id = str(resolved_thread_id)
                self.session_turn_count += 1
            diagnostics = {
                "provider": "codex_cli",
                "model": self.model,
                "reasoning_effort": self.reasoning_effort,
                "usage": usage,
                "elapsed_seconds": round(time.perf_counter() - started, 3),
                "prompt_chars": len(self.system_prompt) + len(actual_prompt),
                "full_prompt_chars": len(self.system_prompt) + len(prompt),
                "output_chars": len(raw_text),
                "thread_id": thread_id or self.session_id,
                "session_mode": "resumed" if resume_session else "new",
                "session_turn_count": self.session_turn_count,
                "rotated_from_session_id": rotated_from_session_id,
                "normalized_array_wrappers": normalization_stats["array_wrappers"],
                "normalized_null_fields": normalization_stats["null_fields"],
                "normalized_trailing_serialization_suffixes": normalization_stats[
                    "trailing_serialization_suffixes"
                ],
            }
            return MiniMaxReplayResult(payload=payload, raw_text=raw_text, diagnostics=diagnostics)


def _continuation_prompt(full_prompt: str) -> str:
    """Send only the new causal state after the locked rules were loaded once."""

    runtime = _tag_contents(full_prompt, "RUNTIME_CONTEXT")
    compact_runtime, transport = _compact_continuation_runtime(runtime)
    correction = _tag_contents(full_prompt, "VALIDATION_CORRECTION")
    correction_block = (
        f"<VALIDATION_CORRECTION>\n{correction}\n</VALIDATION_CORRECTION>\n"
        if correction is not None
        else ""
    )
    return f"""<REPLAY_CONTINUATION>
延續同一交易日、同一固定規則與同一schema。只分析本輪RUNTIME_CONTEXT截至expected_latest_closed_k_iso的資料；不得使用未揭露K棒或外部資料。前一輪輸出只有在previous_semantic_memory中出現者才視為已驗證狀態。
本輪使用{transport}傳輸：current snapshot與program audit取代舊值；detail_bars、overview_bars及spot bars只帶最新因果尾端，較早資料已在同一對話或current ledger中。不得因縮短傳輸誤稱資料不足。
本版仍為AI_HYBRID：AI依課程決定定錨控制意義、大小結構、道氏、Q1～Q4、太極／一之／左右／X、情境及觀察／準備／進出場；時間、價格、可引用ref、候選setup、下一根成交、硬停損與交易憲法受本輪程式證據約束。不得捏造ref或改價。
ENTRY_ELIGIBLE時必須ENTER或使用schema允許的固定否決原因；持倉須依事前應有行為與等待窗管理。ai_hybrid_state_continuity_lock.required=true時必須逐字承接其指定的大錨與控制級數。最終只輸出符合既定schema的單一JSON object。
每輪都分析但不等於每輪通知：空手且相較previous_semantic_memory沒有錨、防線、級數、象限、太極品質、主控戰法、setup、情境接管或交易事件的實質改變時，必須UNCHANGED＋DONT_NOTIFY。
</REPLAY_CONTINUATION>
{correction_block}<RUNTIME_CONTEXT>
{compact_runtime}
</RUNTIME_CONTEXT>
"""


def _compact_continuation_runtime(runtime_text: str | None) -> tuple[str, str]:
    """Compact one resumed turn without changing the authoritative run state.

    The first turn of every Codex session still receives the complete prompt.
    A resumed conversation already contains the prior causal history, so
    resending rolling 90-bar tables and the immutable pre-open reference tree
    only inflates context.  Keep new bars plus current state/audits.  If the
    payload cannot be decoded, preserve it exactly instead of risking data
    loss.
    """

    if not runtime_text:
        return "{}", "PERSISTENT_CAUSAL_DELTA"
    try:
        parsed = json.loads(runtime_text)
    except (TypeError, json.JSONDecodeError):
        return runtime_text, "FULL_RUNTIME_FALLBACK"
    if not isinstance(parsed, dict):
        return runtime_text, "FULL_RUNTIME_FALLBACK"

    result = copy.deepcopy(parsed)
    replay = result.get("replay")
    newly_revealed = 1
    if isinstance(replay, dict):
        raw_count = replay.get("newly_revealed_bar_count")
        if isinstance(raw_count, int) and not isinstance(raw_count, bool) and raw_count > 0:
            newly_revealed = raw_count
        replay["history_transport"] = "PERSISTENT_CAUSAL_DELTA"

    detail_limit = max(6, newly_revealed)
    result["detail_bars"] = _tail_table(result.get("detail_bars"), detail_limit)
    result["overview_bars"] = _tail_table(result.get("overview_bars"), 2)

    spot = result.get("spot_market_context")
    if isinstance(spot, dict) and isinstance(spot.get("bars"), list):
        spot["bars"] = spot["bars"][-6:]

    ledger = result.get("deterministic_evidence_ledger")
    if isinstance(ledger, dict):
        current_keys = (
            "version",
            "as_of",
            "session_key",
            "latest_closed_k",
            "indicators",
            "opening_ranges",
            "pivots",
            "working_pivots",
            "legs",
            "defenses",
            "structure_events",
            "quadrant_evidence",
            "trade_levels",
            "monitoring_session",
            "control_candidates",
            "anchor_lifecycle",
            "program_trade_policy",
            "program_constitution",
            "program_constitution_requalification_audit",
            "position_behavior_audit",
            "protective_stop_audit",
            "program_behavior_exit_audit",
            "program_reentry_expectation",
            "ai_input_view",
        )
        result["deterministic_evidence_ledger"] = {
            key: ledger[key] for key in current_keys if key in ledger
        }

    result["incremental_transport"] = {
        "mode": "PERSISTENT_CAUSAL_DELTA",
        "new_detail_rows": len((result.get("detail_bars") or {}).get("rows") or [])
        if isinstance(result.get("detail_bars"), dict)
        else 0,
        "rule": "current snapshots supersede prior turns; omitted history remains unchanged",
    }
    return json.dumps(result, ensure_ascii=False, separators=(",", ":")), "PERSISTENT_CAUSAL_DELTA"


def _tail_table(value: Any, limit: int) -> Any:
    if not isinstance(value, dict) or not isinstance(value.get("rows"), list):
        return value
    result = copy.deepcopy(value)
    result["rows"] = result["rows"][-max(1, int(limit)):]
    return result


def _tag_contents(text: str, tag: str) -> str | None:
    match = re.search(rf"(?s)<{re.escape(tag)}>(.*?)</{re.escape(tag)}>", str(text or ""))
    return match.group(1).strip() if match else None


def _jsonl_events(text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in str(text or "").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            events.append(item)
    return events


def _last_usage(events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    for event in reversed(events):
        usage = event.get("usage")
        if isinstance(usage, Mapping):
            return dict(usage)
        item = event.get("item")
        if isinstance(item, Mapping) and isinstance(item.get("usage"), Mapping):
            return dict(item["usage"])
    return {}


def _safe_error(error: object) -> str:
    text = re.sub(r"(?:bot)?\d{5,}:[A-Za-z0-9_-]{20,}", "[REDACTED_TOKEN]", str(error or "request failed"))
    text = re.sub(r"(?<!\d)-\d{8,}", "[REDACTED_CHAT_ID]", text)
    return re.sub(r"\s+", " ", text).strip()[:800]


def _command_failure_text(stderr: str | None, stdout: str | None) -> str:
    """Prefer the actual JSONL failure over the harmless PowerShell snapshot warning."""

    error_lines = [
        line
        for line in str(stderr or "").splitlines()
        if "shell_snapshot" not in line and "Shell snapshot not supported yet for PowerShell" not in line
    ]
    output_lines = [line for line in str(stdout or "").splitlines() if line.strip()]
    material = [*error_lines, *output_lines]
    return "\n".join(material) if material else (stderr or stdout or "Codex執行失敗。")
