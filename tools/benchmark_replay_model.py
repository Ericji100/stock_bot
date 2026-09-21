from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from trade_monitor_replay.codex_analyzer import (
    CodexReplayAnalyzer,
    CodexReplayError,
    _tag_contents,
)
from trade_monitor_replay.rules import load_rule_package
from trade_monitor_replay.semantic_contract import (
    SemanticReplayError,
    validate_semantic_envelope,
)


DEFAULT_MANIFEST = (
    ROOT
    / "trade_monitor_replay"
    / "rules"
    / "course-state-v2.1.8-replay-adapter-v33-long-only"
    / "rule-manifest.json"
)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _summary(payload: Mapping[str, Any]) -> dict[str, Any]:
    analysis = _mapping(payload.get("analysis"))
    reading = _mapping(analysis.get("course_reading"))
    action = _mapping(analysis.get("action"))
    return {
        "message_type": analysis.get("message_type"),
        "message_direction": analysis.get("message_direction"),
        "large_trend": _mapping(analysis.get("large_trend")).get("classification"),
        "current_trend": _mapping(analysis.get("current_trend")).get("classification"),
        "controlling_grade": reading.get("controlling_grade"),
        "background_quadrant": reading.get("background_quadrant"),
        "working_quadrant": reading.get("working_quadrant"),
        "main_strategy": reading.get("main_strategy"),
        "setup_stage": reading.get("setup_stage"),
        "position_action": action.get("position_action"),
        "entry_rejection_reason": action.get("entry_rejection_reason"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="以同一個已保存的因果prompt單次比較Codex回放模型。"
    )
    parser.add_argument("--prompt", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--effort", choices=("low", "medium", "high", "xhigh"), required=True)
    parser.add_argument("--rule-manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--timeout", type=float, default=300.0)
    args = parser.parse_args()

    prompt = args.prompt.resolve().read_text(encoding="utf-8")
    runtime_text = _tag_contents(prompt, "RUNTIME_CONTEXT")
    if runtime_text is None:
        raise SystemExit("prompt缺少RUNTIME_CONTEXT。")
    runtime = json.loads(runtime_text)
    if not isinstance(runtime, dict):
        raise SystemExit("RUNTIME_CONTEXT不是JSON object。")
    rules = load_rule_package(args.rule_manifest.resolve())
    analyzer = CodexReplayAnalyzer(
        model=args.model,
        reasoning_effort=args.effort,
        timeout_seconds=args.timeout,
        output_schema=rules.schema,
        persistent_session=False,
    )

    result: dict[str, Any] = {
        "ok": False,
        "model": args.model,
        "reasoning_effort": args.effort,
        "prompt": args.prompt.name,
        "semantic_valid": False,
    }
    try:
        generated = analyzer.analyze(prompt)
        result["generation"] = generated.diagnostics
        result["raw_summary"] = _summary(generated.payload)
        try:
            validated = validate_semantic_envelope(
                generated.payload,
                ledger=_mapping(runtime.get("deterministic_evidence_ledger")),
                expected_as_of=str(runtime.get("expected_latest_closed_k_iso")),
                expected_session_key=str(runtime.get("expected_session_key")),
                preopen=bool(_mapping(runtime.get("replay")).get("preopen_snapshot")),
                position=_mapping(runtime.get("simulated_position_state")),
                evidence_events=list(runtime.get("deterministic_evidence_events") or []),
                previous_memory=_mapping(runtime.get("previous_semantic_memory")),
                entry_gate=_mapping(runtime.get("entry_eligibility")),
                ai_generated=True,
            )
        except SemanticReplayError as exc:
            result["validation_error"] = str(exc)
        else:
            result["semantic_valid"] = True
            result["validated_summary"] = _summary(validated)
        result["ok"] = True
    except (CodexReplayError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        result["error"] = str(exc)

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
