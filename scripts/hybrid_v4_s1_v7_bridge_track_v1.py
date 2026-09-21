"""Build the frozen V4-S1 Scenario-1 bridge validation track.

The bridge sample answers a narrower question than the original V7 sample:
does the frozen V4-S1 decision process still recognize dates that the earlier,
outcome-blind formal V3 review classified as MATURE_TREND_PULLBACK/V2_CORE?

Earlier AI decisions are used only to stratify the sample.  They, real stock
identity, and all post-as-of performance remain outside every AI-visible packet.
The V4-S1 rules, atomic prompt/schema, reducer, model, and reasoning effort are
not changed.
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Iterable, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from . import hybrid_v4_s1_track_v1 as base
    from . import hybrid_v4_s1_v7_track_v1 as v7
    from .hybrid_v3_sharding_v2 import (
        _publish_immutable,
        canonical_json_bytes,
        canonical_sha256,
        file_sha256,
    )
except ImportError:  # pragma: no cover
    from scripts import hybrid_v4_s1_track_v1 as base
    from scripts import hybrid_v4_s1_v7_track_v1 as v7
    from scripts.hybrid_v3_sharding_v2 import (
        _publish_immutable,
        canonical_json_bytes,
        canonical_sha256,
        file_sha256,
    )


TRACK_VERSION = "hybrid-v4-s1-v7-bridge-research-track-v3"
FREEZE_VERSION = "hybrid-v4-s1-v7-bridge-execution-freeze-v3"
SELECTION_VERSION = "hybrid-v4-s1-v7-bridge-selection-v3"
PACKET_MANIFEST_VERSION = "hybrid-v4-s1-v7-bridge-packet-block-v3"
TRACK_STATUS = "FROZEN_OUTCOME_BLIND_V4_S1_V7_BRIDGE_V3_RESEARCH_ONLY"
STOP_REMAINING_PERCENT = 70

POSITIVE = "LEGACY_S1_V2_CORE_POSITIVE"
BOUNDARY = "LEGACY_S1_BOUNDARY_NO_TRADE"
NEGATIVE = "COMPETING_OR_INVALID_NO_TRADE"
FOCUS_ORDER = [POSITIVE, BOUNDARY, NEGATIVE]
QUOTAS = {POSITIVE: 18, BOUNDARY: 9, NEGATIVE: 9}
QUESTION_MINIMUM = {"global_question_ids": 1, "relation_candidates": 1, "stop_candidates": 1}

REPORT_BASE = (
    ROOT
    / "reports/course_backtest/2024-02-02"
    / "historical_scan_2023h2_formal_ai_v3_daily_scan"
)
V4_ROOT = REPORT_BASE / "hybrid_monitoring_v4_s1_mature_v1"
DEFAULT_OUTPUT = V4_ROOT / "v7_bridge_validation_v3"
FORMAL_ROOT = (
    ROOT
    / "reports/course_backtest/2024-02-02"
    / "historical_scan_2023h2_formal_ai_v1_v2_v3_full_codex"
)
REVIEW_ROOT = (
    ROOT
    / "reports/course_backtest/2024-02-02"
    / "historical_scan_2023h2_formal_ai_v3_full_review"
)
OLD_LEDGER = FORMAL_ROOT / "v3_ai_decisions.jsonl"
OLD_QUEUE = REVIEW_ROOT / "ai_review_queue.jsonl"
OLD_REVIEW_DIR = REVIEW_ROOT / "ai_reviews"
OLD_VALIDATION = FORMAL_ROOT / "v3_decision_validation.json"
OLD_PREPERFORMANCE_LOCK = FORMAL_ROOT / "preperformance_decision_lock.json"
IDENTITY_MAP = REPORT_BASE / "sealed_identity_map.json"

EXPECTED_HASHES = {
    "old_v3_decision_ledger": "f8c995e227c04c6a4fc60d4cc1b67d31ceb01e24d98e81816ca373febdd7a307",
    "old_v3_review_queue": "3d4ea27b9c3fd0d2ec39cb5058280888b942697a33f0cd31d9cb6ba36abdf3e9",
    "sealed_identity_map": "e2782dde8b0fedb4b437c4d8b56dd4d97115eb5a4b6c2104905cacd204941cef",
    "old_v3_decision_validation": "358e669abc589878e4d1ec1727dc97e090e3a5929e0b3cbb6d4d6e11ac0a9f78",
    "old_preperformance_decision_lock": "98afcecc04746c2243e9b74464052fa2de5bfc403c01d1d93b498fa087131080",
    "old_review_directory_manifest": "20b625c121de3551edf7e4c43d6d37949c098db9dc785202e8cb81972b94262d",
}

EVALUATOR = ROOT / "scripts/hybrid_v4_s1_v7_bridge_consistency_v1.py"
EXECUTION_ORCHESTRATOR = ROOT / "scripts/hybrid_v4_s1_v7_bridge_execution_orchestrator_v1.py"


class BridgeTrackError(ValueError):
    """The bridge sampling provenance or frozen sample is not exact."""


def _jsonl(path: Path) -> Iterable[dict[str, Any]]:
    try:
        with Path(path).open(encoding="utf-8-sig") as handle:
            for number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise BridgeTrackError(f"expected object at {path}:{number}")
                yield value
    except (OSError, json.JSONDecodeError) as exc:
        raise BridgeTrackError(f"cannot read JSONL: {path}") from exc


def review_directory_manifest_sha256(path: Path = OLD_REVIEW_DIR) -> str:
    rows = [
        f"{file_sha256(child)}  {child.name}"
        for child in sorted(Path(path).glob("*.json"), key=lambda item: item.name)
    ]
    return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()


def _validate_sampling_provenance() -> None:
    paths = {
        "old_v3_decision_ledger": OLD_LEDGER,
        "old_v3_review_queue": OLD_QUEUE,
        "sealed_identity_map": IDENTITY_MAP,
        "old_v3_decision_validation": OLD_VALIDATION,
        "old_preperformance_decision_lock": OLD_PREPERFORMANCE_LOCK,
    }
    for name, path in paths.items():
        if not path.is_file() or file_sha256(path) != EXPECTED_HASHES[name]:
            raise BridgeTrackError(f"bridge sampling provenance changed: {name}")
    if review_directory_manifest_sha256() != EXPECTED_HASHES["old_review_directory_manifest"]:
        raise BridgeTrackError("old V3 review directory changed")

    validation = json.loads(OLD_VALIDATION.read_text(encoding="utf-8-sig"))
    lock = json.loads(OLD_PREPERFORMANCE_LOCK.read_text(encoding="utf-8-sig"))
    if validation.get("decision_boundary") != (
        "AI authored route/NO_TRADE; code validated and replayed only"
    ):
        raise BridgeTrackError("old decision-boundary attestation changed")
    if (
        lock.get("status") != "LOCKED_BEFORE_PERFORMANCE_REPLAY"
        or lock.get("performance_visible_when_decisions_authored") is not False
    ):
        raise BridgeTrackError("old pre-performance lock is not outcome blind")


def _identity_crosswalk() -> tuple[dict[str, str], dict[str, str]]:
    value = json.loads(IDENTITY_MAP.read_text(encoding="utf-8-sig"))
    rows = value.get("mapping")
    if not isinstance(rows, list) or len(rows) != 1029:
        raise BridgeTrackError("sealed identity map coverage changed")
    code_to_anon = {str(row["code"]): str(row["anonymous_id"]) for row in rows}
    anon_to_code = {anonymous: code for code, anonymous in code_to_anon.items()}
    if len(code_to_anon) != 1029 or len(anon_to_code) != 1029:
        raise BridgeTrackError("sealed identity map is not one-to-one")
    return code_to_anon, anon_to_code


def _old_positive_keys() -> set[tuple[str, str]]:
    result: set[tuple[str, str]] = set()
    forbidden_fragments = ("mfe", "mae", "pnl", "profit", "return_on")
    for stock in _jsonl(OLD_LEDGER):
        lowered_keys = {str(key).lower() for key in stock}
        if any(any(fragment in key for fragment in forbidden_fragments) for key in lowered_keys):
            raise BridgeTrackError("performance field entered old decision ledger")
        for trigger in (stock.get("v3") or {}).get("triggers") or []:
            if (
                trigger.get("scenario") == base.TARGET_SCENARIO
                and trigger.get("v3_route") == base.TARGET_ROUTE
            ):
                result.add((str(stock["code"]), str(trigger["signal_date"])))
    if len(result) != 34:
        raise BridgeTrackError("old formal Scenario-1 positive count changed")
    return result


def _old_no_trade_keys() -> set[tuple[str, str]]:
    approvals: set[str] = set()
    for path in sorted(OLD_REVIEW_DIR.glob("*.json"), key=lambda item: item.name):
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        if value.get("future_performance_visible_in_input") is not False:
            raise BridgeTrackError("old review batch was not outcome blind")
        if value.get("default_decision_for_unlisted_review_ids") != "NO_TRADE":
            raise BridgeTrackError("old review batch default changed")
        for approval in value.get("approvals") or []:
            approvals.add(str(approval["review_id"]))
    result: set[tuple[str, str]] = set()
    for row in _jsonl(OLD_QUEUE):
        if str(row["review_id"]) not in approvals:
            result.add((str(row["code"]), str(row["review_as_of"])))
    if len(result) < 1000:
        raise BridgeTrackError("old formal NO_TRADE coverage is unexpectedly small")
    return result


def _question_capacity(source_path: Path) -> dict[str, dict[str, int]]:
    result: dict[str, dict[str, int]] = {}
    for row in _jsonl(source_path):
        manifest = (row.get("packet") or {}).get("question_manifest") or {}
        result[str(row["review_id"])] = {
            key: len(manifest.get(key) or []) for key in QUESTION_MINIMUM
        }
    return result


def classify_bridge_candidates(
    records: Sequence[Mapping[str, Any]],
    *,
    anon_to_code: Mapping[str, str],
    positive_keys: set[tuple[str, str]],
    no_trade_keys: set[tuple[str, str]],
    question_capacity: Mapping[str, Mapping[str, int]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Add validation-only bridge strata without modifying packet contents."""

    result: list[dict[str, Any]] = []
    exact_positive_hits = 0
    for source in records:
        row = deepcopy(dict(source))
        actual_focuses = list(row.get("eligible_stage_focuses") or [])
        row["source_stage_focuses"] = actual_focuses
        review_id = str(row["review_id"])
        code = anon_to_code.get(str(row["anonymous_stock_id"]))
        if code is None:
            raise BridgeTrackError("anonymous source stock is absent from sealed map")
        key = (code, str(row["as_of"]))
        capacity = question_capacity.get(review_id)
        if capacity is None:
            raise BridgeTrackError("source review is absent from question-capacity index")
        complete = all(int(capacity.get(name, 0)) >= minimum for name, minimum in QUESTION_MINIMUM.items())

        bridge_focuses: list[str] = []
        if key in positive_keys:
            exact_positive_hits += 1
            if complete:
                bridge_focuses.append(POSITIVE)
        if key in no_trade_keys and complete:
            has_mature = any(focus.startswith("MATURE_SYMBOLIC_") for focus in actual_focuses)
            has_competing_or_invalid = any(
                focus.startswith("COMPETING_") or focus == "MACRO_DEFENSE_REMOVE"
                for focus in actual_focuses
            )
            if has_mature:
                bridge_focuses.append(BOUNDARY)
            elif has_competing_or_invalid:
                bridge_focuses.append(NEGATIVE)
        row["eligible_stage_focuses"] = bridge_focuses
        result.append(row)

    if exact_positive_hits != 30:
        raise BridgeTrackError("old-positive/V7 exact-date intersection changed")
    capacity = Counter(
        focus for row in result for focus in row.get("eligible_stage_focuses") or []
    )
    for focus, quota in QUOTAS.items():
        if capacity[focus] < quota:
            raise BridgeTrackError(f"insufficient bridge capacity for {focus}")
    return result, dict(capacity)


def bridge_protocol() -> dict[str, Any]:
    protocol = deepcopy(base._stage_protocol())
    sample = protocol["consistency_sample"]
    sample.update(
        {
            "seed": "v4-s1-v7-legacy-bridge-v1",
            "focus_order": list(FOCUS_ORDER),
            "quotas": dict(QUOTAS),
            "cases": 36,
            "runs": 3,
            "one_case_per_anonymous_stock": True,
            "minimum_distinct_months": 6,
            "maximum_cases_per_month": 9,
            "sampling_focus_must_not_enter_ai_packet": True,
        }
    )
    return protocol


def _provenance_document(capacity: Mapping[str, int]) -> dict[str, Any]:
    core = {
        "provenance_version": "hybrid-v4-s1-v7-bridge-sampling-provenance-v1",
        "status": "FROZEN_SAMPLING_ONLY_NEVER_AI_VISIBLE",
        "purpose": "BRIDGE_OLD_FORMAL_S1_DECISIONS_TO_UNCHANGED_V4_S1_AI_PROCESS",
        "old_labels_are_expected_answers": False,
        "old_labels_visible_to_ai": False,
        "identity_visible_to_ai": False,
        "future_or_performance_visible": False,
        "sampling_only_fields_removed_from_primary_packets": True,
        "positive_definition": "OLD_FORMAL_V3_MATURE_TREND_PULLBACK_AND_V2_CORE_EXACT_CODE_DATE",
        "boundary_definition": "OLD_FORMAL_V3_QUEUE_DEFAULT_NO_TRADE_WITH_CURRENT_MATURE_CHALLENGE",
        "negative_definition": "OLD_FORMAL_V3_QUEUE_DEFAULT_NO_TRADE_WITH_COMPETING_OR_INVALID_CHALLENGE",
        "question_manifest_minimum": dict(QUESTION_MINIMUM),
        "quotas": dict(QUOTAS),
        "capacity": dict(sorted(capacity.items())),
        "pinned_sources": {
            "old_v3_decision_ledger": {"path": str(OLD_LEDGER), "sha256": EXPECTED_HASHES["old_v3_decision_ledger"]},
            "old_v3_review_queue": {"path": str(OLD_QUEUE), "sha256": EXPECTED_HASHES["old_v3_review_queue"]},
            "old_v3_review_directory_manifest_sha256": EXPECTED_HASHES["old_review_directory_manifest"],
            "sealed_identity_map": {"path": str(IDENTITY_MAP), "sha256": EXPECTED_HASHES["sealed_identity_map"]},
            "old_v3_decision_validation": {"path": str(OLD_VALIDATION), "sha256": EXPECTED_HASHES["old_v3_decision_validation"]},
            "old_preperformance_decision_lock": {"path": str(OLD_PREPERFORMANCE_LOCK), "sha256": EXPECTED_HASHES["old_preperformance_decision_lock"]},
        },
    }
    return {**core, "provenance_sha256": canonical_sha256(core)}


def _bridge_components(preflight_path: Path, provenance_path: Path) -> list[dict[str, Any]]:
    return [
        v7._component("stage_protocol", v7.STAGE_PROTOCOL, status="FINAL_RESEARCH_LOCKED"),
        v7._component(
            "reachability_preflight",
            preflight_path,
            status=v7.EXPECTED_PREFLIGHT_STATUS,
            role="SAMPLING_ONLY_NEVER_AI_VISIBLE",
        ),
        v7._component(
            "reachability_preflight_builder",
            v7.REACHABILITY_PREFLIGHT_BUILDER,
            role="OUTCOME_BLIND_V7_SYMBOLIC_CLASSIFIER_PRODUCER",
        ),
        v7._component(
            "bridge_sampling_provenance",
            provenance_path,
            role="OLD_OUTCOME_BLIND_AI_LABELS_FOR_STRATIFIED_SAMPLING_ONLY",
        ),
        v7._component(
            "policy",
            v7.V4_S1_POLICY,
            status="DRAFT_FOR_OUTCOME_BLIND_RESEARCH",
            role="UNCHANGED_V4_S1_DECISION_REDUCER",
        ),
        v7._component("track_builder", Path(__file__).resolve()),
        v7._component("evaluator", EVALUATOR),
        v7._component("execution_orchestrator", EXECUTION_ORCHESTRATOR),
    ]


def prepare(
    *,
    source_path: Path = v7.SOURCE,
    source_manifest_path: Path = v7.SOURCE_MANIFEST,
    preflight_path: Path = v7.REACHABILITY_PREFLIGHT,
    output_dir: Path = DEFAULT_OUTPUT,
    shard_count: int = 4,
) -> dict[str, Any]:
    _validate_sampling_provenance()
    records, _, _ = v7.scan_source(source_path, source_manifest_path, preflight_path)
    _, anon_to_code = _identity_crosswalk()
    classified, capacity = classify_bridge_candidates(
        records,
        anon_to_code=anon_to_code,
        positive_keys=_old_positive_keys(),
        no_trade_keys=_old_no_trade_keys(),
        question_capacity=_question_capacity(source_path),
    )
    protocol = bridge_protocol()
    selected = base.allocate(classified, protocol)

    output_dir = Path(output_dir).resolve()
    provenance_path = output_dir / "bridge_sampling_provenance.json"
    provenance = _provenance_document(capacity)
    _publish_immutable(provenance_path, canonical_json_bytes(provenance) + b"\n")

    original_allocate = base.allocate
    original_stage_protocol = base._stage_protocol
    original_components = v7._v4_components
    original_values = {
        name: getattr(v7, name)
        for name in (
            "TRACK_VERSION",
            "FREEZE_VERSION",
            "SELECTION_VERSION",
            "PACKET_MANIFEST_VERSION",
            "TRACK_STATUS",
            "STOP_REMAINING_PERCENT",
            "EVALUATOR",
            "EXECUTION_ORCHESTRATOR",
        )
    }

    def allocate_once(_records: Sequence[Mapping[str, Any]], _protocol: Mapping[str, Any]) -> list[dict[str, Any]]:
        return deepcopy(selected)

    try:
        base.allocate = allocate_once
        base._stage_protocol = lambda: deepcopy(protocol)
        v7._v4_components = lambda path: _bridge_components(path, provenance_path)
        v7.TRACK_VERSION = TRACK_VERSION
        v7.FREEZE_VERSION = FREEZE_VERSION
        v7.SELECTION_VERSION = SELECTION_VERSION
        v7.PACKET_MANIFEST_VERSION = PACKET_MANIFEST_VERSION
        v7.TRACK_STATUS = TRACK_STATUS
        v7.STOP_REMAINING_PERCENT = STOP_REMAINING_PERCENT
        v7.EVALUATOR = EVALUATOR
        v7.EXECUTION_ORCHESTRATOR = EXECUTION_ORCHESTRATOR
        result = v7.prepare(
            source_path=Path(source_path).resolve(),
            source_manifest_path=Path(source_manifest_path).resolve(),
            preflight_path=Path(preflight_path).resolve(),
            output_dir=output_dir,
            shard_count=shard_count,
        )
    finally:
        base.allocate = original_allocate
        base._stage_protocol = original_stage_protocol
        v7._v4_components = original_components
        for name, value in original_values.items():
            setattr(v7, name, value)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=v7.SOURCE)
    parser.add_argument("--source-manifest", type=Path, default=v7.SOURCE_MANIFEST)
    parser.add_argument("--preflight", type=Path, default=v7.REACHABILITY_PREFLIGHT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--shard-count", type=int, default=4)
    args = parser.parse_args(argv)
    result = prepare(
        source_path=args.source.resolve(),
        source_manifest_path=args.source_manifest.resolve(),
        preflight_path=args.preflight.resolve(),
        output_dir=args.output_dir.resolve(),
        shard_count=args.shard_count,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
