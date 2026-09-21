"""Prepare Candidate3 research execution v2 after the first smoke failure.

V2 preserves the Candidate3 universe, V3 strategy, schema, reducer, model and
reasoning effort.  It changes only the transport prompt to state an existing
validator invariant explicitly: one evidence ref cannot support and
contradict the same answer.  Runtime attempt directories must be short on
Windows; that is an execution-path requirement, not a semantic change.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

try:
    from . import hybrid_v3_research_track_v1 as v1
    from .hybrid_v3_course_gold_v4 import (
        extract_selected_records,
        validate_candidate3_universe_manifest,
        validate_holdout_plan,
    )
    from .hybrid_v3_sharding_v2 import canonical_sha256, file_sha256
except ImportError:
    import hybrid_v3_research_track_v1 as v1
    from hybrid_v3_course_gold_v4 import (
        extract_selected_records,
        validate_candidate3_universe_manifest,
        validate_holdout_plan,
    )
    from hybrid_v3_sharding_v2 import canonical_sha256, file_sha256


TRACK_VERSION = "hybrid-v3-research-track-v2"
TRACK_STATUS = "FROZEN_OUTCOME_BLIND_RESEARCH_ONLY"
MODEL = v1.MODEL
REASONING_EFFORT = v1.REASONING_EFFORT
ROOT = Path(__file__).resolve().parents[1]
PROMPT_V2 = ROOT / "config/hybrid_semantic_prompt_v2.md"
PROMPT_V3 = ROOT / "config/hybrid_semantic_prompt_v3.md"
RUNNER = ROOT / "scripts/hybrid_v3_atomic_runner_v2.py"


def _read_json(path: Path) -> dict[str, Any]:
    return v1._read_json(path)


def _validate_prompt_delta() -> dict[str, Any]:
    old = PROMPT_V2.read_text(encoding="utf-8-sig")
    new = PROMPT_V3.read_text(encoding="utf-8-sig")
    old_body = old.replace("HYBRID_SEMANTIC_PROMPT_V2", "HYBRID_SEMANTIC_PROMPT_V3").replace(
        "hybrid-semantic-prompt-v2", "hybrid-semantic-prompt-v3"
    )
    clarification = (
        "同一個答案的 `supporting_evidence_refs` 與 `contradicting_evidence_refs` 必須完全互斥；"
        "同一個 ref 絕不可同時出現在兩邊。輸出前必須逐一檢查所有答案。"
        "這只是在明示既有 schema／validator 契約，不改變任何 PASS、FAIL、UNKNOWN 或交易規則。\n\n"
    )
    expected = old_body.replace(
        "### PASS\n",
        clarification + "### PASS\n",
        1,
    )
    if new != expected:
        raise ValueError("semantic prompt v3 contains changes beyond the frozen validator clarification")
    return {
        "old_prompt_sha256": file_sha256(PROMPT_V2),
        "new_prompt_sha256": file_sha256(PROMPT_V3),
        "only_change": "EXPLICIT_SUPPORTING_CONTRADICTING_REF_DISJOINTNESS",
        "strategy_or_gate_change": False,
    }


def build_execution_contract(
    *,
    candidate_manifest_path: Path,
    holdout_plan_path: Path,
    diff_audit_path: Path,
    superseded_execution_path: Path,
) -> dict[str, Any]:
    prompt_delta = _validate_prompt_delta()
    protocol = _read_json(v1.DEFAULT_PROTOCOL)
    if protocol.get("status") != "FINAL_RESEARCH_LOCKED":
        raise ValueError("research protocol is not locked")
    diff = _read_json(diff_audit_path)
    if diff.get("status") != "PASS" or diff.get("performance_or_future_fields_read") is not False:
        raise ValueError("outcome-blind Candidate3 differential audit did not pass")
    candidate = validate_candidate3_universe_manifest(_read_json(candidate_manifest_path))
    holdout = _read_json(holdout_plan_path)
    if holdout.get("status") != "LOCKED" or holdout.get("block_count") != 3:
        raise ValueError("Candidate3 holdout is not locked")
    if not superseded_execution_path.is_file():
        raise ValueError("superseded v1 execution freeze is missing")
    components = [
        v1._component("reviewer", v1.DEFAULT_REVIEWER),
        v1._component("runner", RUNNER),
        v1._component("policy", v1.DEFAULT_POLICY),
        v1._component("prompt", PROMPT_V3),
        v1._component("schema", v1.DEFAULT_SCHEMA),
        v1._component("protocol", v1.DEFAULT_PROTOCOL),
    ]
    return {
        "freeze_version": "hybrid-v3-research-execution-freeze-v2",
        "status": v1.EXECUTION_FREEZE_STATUS,
        "track_version": TRACK_VERSION,
        "track_status": TRACK_STATUS,
        "classification": "RESEARCH_BACKTEST_NOT_COURSE_VALIDATED",
        "supersedes_execution_freeze_sha256": file_sha256(superseded_execution_path),
        "supersession_reason": [
            "FIRST_SMOKE_OUTPUT_VIOLATED_EXISTING_EVIDENCE_REF_DISJOINT_VALIDATOR",
            "WINDOWS_RUNTIME_OUTPUT_PATH_MUST_BE_SHORTER_THAN_LEGACY_REPORT_PATH",
        ],
        "first_smoke_published_as_valid": False,
        "prompt_delta": prompt_delta,
        "runtime_output_path_requirement": "USE_SHORT_ABSOLUTE_PATH_TO_AVOID_WINDOWS_MAX_PATH",
        "course_fidelity_claim_allowed": False,
        "human_course_gold_status": "DEFERRED_BY_USER_FOR_INITIAL_PERFORMANCE_CHECK",
        "candidate3_review_point_manifest_sha256": file_sha256(candidate_manifest_path),
        "candidate3_review_points_artifact_sha256": candidate["artifact_sha256"],
        "candidate3_holdout_plan_sha256": file_sha256(holdout_plan_path),
        "candidate3_diff_audit_sha256": file_sha256(diff_audit_path),
        "outcome_blind": True,
        "identity_visible": False,
        "future_data_visible": False,
        "performance_visible": False,
        "old_ai_output_visible": False,
        "v3_strategy_rules_unchanged": True,
        "v2_components": components,
        "execution_contract": {"model": MODEL, "reasoning_effort": REASONING_EFFORT},
    }


def prepare(
    *,
    source_path: Path,
    source_manifest_path: Path,
    holdout_plan_path: Path,
    diff_audit_path: Path,
    superseded_execution_path: Path,
    output_dir: Path,
) -> dict[str, Any]:
    candidate_manifest = validate_candidate3_universe_manifest(_read_json(source_manifest_path))
    holdout = validate_holdout_plan(
        _read_json(holdout_plan_path),
        source_manifest=candidate_manifest,
        source_path=source_path,
        source_manifest_path=source_manifest_path,
    )
    selected_records = extract_selected_records(source_path, holdout)
    execution = build_execution_contract(
        candidate_manifest_path=source_manifest_path,
        holdout_plan_path=holdout_plan_path,
        diff_audit_path=diff_audit_path,
        superseded_execution_path=superseded_execution_path,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    execution_path = output_dir / "research_execution_freeze.json"
    v1._publish(execution_path, json.dumps(execution, ensure_ascii=False, indent=2).encode("utf-8") + b"\n")
    block_entries: list[dict[str, Any]] = []
    for block in [holdout["primary"], *holdout["reserve_blocks"]]:
        safe_name = str(block["block_id"]).lower()
        packet_path = output_dir / f"{safe_name}_packets.jsonl"
        manifest_path = output_dir / f"{safe_name}_packets.manifest.json"
        payload, manifest = v1._block_artifacts(block, selected_records)
        v1._publish(packet_path, payload)
        v1._publish(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8") + b"\n")
        block_entries.append(
            {
                "block_id": block["block_id"],
                "packet_path": str(packet_path.resolve()),
                "packet_sha256": file_sha256(packet_path),
                "manifest_path": str(manifest_path.resolve()),
                "manifest_sha256": file_sha256(manifest_path),
                "rows": manifest["rows"],
            }
        )
    core = {
        "track_version": TRACK_VERSION,
        "status": TRACK_STATUS,
        "classification": "RESEARCH_BACKTEST_NOT_COURSE_VALIDATED",
        "course_fidelity_claim_allowed": False,
        "outcome_blind": True,
        "model": MODEL,
        "reasoning_effort": REASONING_EFFORT,
        "execution_freeze_path": str(execution_path.resolve()),
        "execution_freeze_sha256": file_sha256(execution_path),
        "candidate3_source_manifest_sha256": file_sha256(source_manifest_path),
        "candidate3_holdout_plan_sha256": file_sha256(holdout_plan_path),
        "blocks": block_entries,
    }
    track = {**core, "track_manifest_sha256": canonical_sha256(core)}
    v1._publish(
        output_dir / "research_track_manifest.json",
        json.dumps(track, ensure_ascii=False, indent=2).encode("utf-8") + b"\n",
    )
    return track


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--holdout-plan", type=Path, required=True)
    parser.add_argument("--diff-audit", type=Path, required=True)
    parser.add_argument("--superseded-execution", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    result = prepare(
        source_path=args.source.resolve(),
        source_manifest_path=args.source_manifest.resolve(),
        holdout_plan_path=args.holdout_plan.resolve(),
        diff_audit_path=args.diff_audit.resolve(),
        superseded_execution_path=args.superseded_execution.resolve(),
        output_dir=args.output_dir.resolve(),
    )
    print(
        json.dumps(
            {
                "status": result["status"],
                "track_version": result["track_version"],
                "blocks": {block["block_id"]: block["rows"] for block in result["blocks"]},
                "execution_freeze_sha256": result["execution_freeze_sha256"],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )


if __name__ == "__main__":
    main()
