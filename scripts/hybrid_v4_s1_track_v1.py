"""Build the frozen, outcome-blind V4-S1 36-case research track.

V4-S1 is deliberately limited to ``MATURE_TREND_PULLBACK / V2_CORE``.
Unlike the earlier V3 stage split, this track *does* pin a policy-decision
correction relative to V3.  V1/V2/V3 code and artifacts remain read-only.

The reachability report is used only as an external sampling classification.
Neither a symbolic witness nor any sampling label is copied into an AI-visible
packet.  This module never reads model output, future data, or performance and
never invokes an AI reviewer.
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
    from .hybrid_v3_consistency_v3 import (
        canonical_sha256,
        load_sampling_contract,
        validate_source_record,
    )
    from .hybrid_v3_sharding_v2 import (
        _publish_immutable,
        build_case_records,
        canonical_json_bytes,
        file_sha256,
        write_shards,
    )
except ImportError:  # pragma: no cover - direct script execution
    from scripts.hybrid_v3_consistency_v3 import (
        canonical_sha256,
        load_sampling_contract,
        validate_source_record,
    )
    from scripts.hybrid_v3_sharding_v2 import (
        _publish_immutable,
        build_case_records,
        canonical_json_bytes,
        file_sha256,
        write_shards,
    )

REPORT_BASE = (
    ROOT
    / "reports/course_backtest/2024-02-02"
    / "historical_scan_2023h2_formal_ai_v3_daily_scan"
)
SOURCE_ROOT = REPORT_BASE / "hybrid_monitoring_candidate4_v5"
V4_ROOT = REPORT_BASE / "hybrid_monitoring_v4_s1_mature_v1"

TRACK_VERSION = "hybrid-v4-s1-research-track-v3-cli-entry"
FREEZE_VERSION = "hybrid-v4-s1-execution-freeze-v3-cli-entry"
SELECTION_VERSION = "hybrid-v4-s1-selection-v1"
PACKET_MANIFEST_VERSION = "hybrid-v4-s1-packet-block-v1"
TRACK_STATUS = "FROZEN_OUTCOME_BLIND_V4_S1_SINGLE_ROUTE_RESEARCH_ONLY"
BUILDER_STATUS = "FINAL"
MODEL = "gpt-5.6-sol"
REASONING = "xhigh"

STAGE_PROTOCOL = ROOT / "config/hybrid_v4_s1_mature_stage_v1.json"
SOURCE_SAMPLING_CONTRACT = ROOT / "config/hybrid_multilabel_sampling_v3.json"
SOURCE = SOURCE_ROOT / "review_points.jsonl"
SOURCE_MANIFEST = SOURCE_ROOT / "review_point_manifest.json"
REACHABILITY_PREFLIGHT = (
    V4_ROOT / "reachability_preflight_v3_provenance/preflight.json"
)
REACHABILITY_PREFLIGHT_BUILDER = (
    ROOT / "scripts/hybrid_v4_s1_reachability_preflight_v1.py"
)

PROMPT = ROOT / "config/hybrid_semantic_prompt_v3.md"
SCHEMA = ROOT / "config/hybrid_atomic_semantics_v2.schema.json"
RESEARCH_PROTOCOL = ROOT / "config/hybrid_monitoring_research_protocol_v1.json"
LAUNCHER = ROOT / "scripts/hybrid_v3_codex_launcher_v2.py"
REVIEWER = ROOT / "scripts/hybrid_v3_codex_reviewer_v3.py"
RUNNER = ROOT / "scripts/hybrid_v3_atomic_runner_v2.py"
V3_SEMANTIC_POLICY = ROOT / "scripts/hybrid_v3_atomic_policy_v3.py"
V4_S1_POLICY = ROOT / "scripts/hybrid_v4_s1_atomic_policy_v1.py"
SHARDING = ROOT / "scripts/hybrid_v3_sharding_v2.py"
SOURCE_VALIDATOR = ROOT / "scripts/hybrid_v3_consistency_v3.py"
CONSISTENCY_EVALUATOR = ROOT / "scripts/hybrid_v4_s1_consistency_v1.py"
EXECUTION_ORCHESTRATOR = (
    ROOT / "scripts/hybrid_v4_s1_execution_orchestrator_v1.py"
)

TARGET_SCENARIO = "MATURE_TREND_PULLBACK"
TARGET_ROUTE = "V2_CORE"
PREFLIGHT_VERSION = "hybrid-v4-s1-reachability-preflight-v1"
EXPECTED_PREFLIGHT_STATUS = "PASS_SYMBOLIC_TARGET_ROUTE_REACHABLE"
EXPECTED_PREFLIGHT_CLASSIFICATION = (
    "SOFTWARE_REACHABILITY_ONLY_NOT_AI_LABEL_NOT_COURSE_GOLD"
)
EXPECTED_SOURCE_SHA256 = "49e5125ad57c55b1536b53746705a60a7c505e49ed1b85c2a72764a61df104bb"
EXPECTED_SOURCE_MANIFEST_SHA256 = "a2c63ec49e236049313354795b1c164a1dfe28be473a1808c024168681ebc7eb"
EXPECTED_PREFLIGHT_FILE_SHA256 = "4d3505a684a2cd4fbbb67a3b67097a15f436244dbfd7bf1503cacfca544590e3"
EXPECTED_PREFLIGHT_BUILDER_SHA256 = "211dd92a6272a35a482e634011e59708125f81a284cbbc6c3458a54c66d284e9"
EXPECTED_SOURCE_VALIDATOR_SHA256 = "07b76d07b62510e5226824aec61b496a8c0cf36ecd9d36546dd76638938e5855"
EXPECTED_SOURCE_SAMPLING_CONTRACT_SHA256 = "b22c9212a880f45896d60d80e6aaee77e5cb881e19cf9139a8075f462772bff1"
EXPECTED_QUOTAS = {
    "MATURE_SYMBOLIC_REACHABLE": 18,
    "MATURE_SYMBOLIC_UNREACHABLE": 6,
    "COMPETING_MACRO_ONLY": 2,
    "COMPETING_FRESH_ONLY": 2,
    "COMPETING_BEAR_ONLY": 2,
    "MACRO_DEFENSE_REMOVE": 2,
    "WAIT_NO_CAUSAL_STOP": 2,
    "WAIT_MATERIAL_CONFLICT": 2,
}
EXPECTED_SOURCE_SCOPE = {
    "stocks": 1029,
    "monitoring_start": "2023-06-01",
    "monitoring_end": "2024-02-02",
    "review_points": 7180,
    "event_driven": True,
    "outcome_blind": True,
    "identity_visible": False,
    "future_or_performance_visible": False,
}

SOURCE_STRATA = {
    "MATURE": "V2_CORE_OBJECTIVE_PROXY",
    "MACRO": "MACRO_COPY_OBJECTIVE_PROXY",
    "FRESH": "FRESH_Q1_OBJECTIVE_PROXY",
    "BEAR": "BEAR_REVERSAL_OBJECTIVE_PROXY",
    "REMOVE": "MACRO_DEFENSE_REMOVE_PROXY",
    "WAIT": "WAIT_POLICY_BOUNDARY",
}
FORBIDDEN_SOURCE_KEYS = {
    "outcome",
    "mfe",
    "mae",
    "pnl",
    "profit",
    "return_after",
    "forward_return",
    "future_return",
    "future_open",
    "future_high",
    "future_low",
    "future_close",
    "exit_date",
    "exit_price",
    "realized_return",
    "unrealized_return",
    "stock_name",
    "stock_code",
    "ticker",
    "company_name",
    "security_name",
    "isin",
    "winner",
    "outcome",
    "next_20d_return",
    "profit_factor",
}
FORBIDDEN_AI_PACKET_METADATA_KEYS = {
    "eligible_sampling_strata",
    "eligible_stage_focuses",
    "primary_sampling_focus",
    "sampling_stratum",
    "sampling_focus",
    "symbolic_witness",
    "symbolic_witness_sha256",
    "symbolic_reachability",
    "symbolic_classification",
    "_preflight_symbolic_only",
    "action_signature_sha256",
}
MAX_ALLOCATION_SEARCH_NODES = 250_000


class V4S1TrackError(ValueError):
    """The V4-S1 source, preflight, sample, or freeze is not exact."""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise V4S1TrackError(f"cannot read JSON: {path}") from exc
    if not isinstance(value, dict):
        raise V4S1TrackError(f"expected JSON object: {path}")
    return value


def _jsonl(path: Path) -> Iterable[dict[str, Any]]:
    try:
        with Path(path).open("r", encoding="utf-8-sig") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise V4S1TrackError(
                        f"expected JSON object at {path}:{line_number}"
                    )
                yield value
    except (OSError, json.JSONDecodeError) as exc:
        raise V4S1TrackError(f"cannot read JSONL: {path}") from exc


def _canonical_jsonl(rows: Sequence[Mapping[str, Any]]) -> bytes:
    return b"".join(canonical_json_bytes(dict(row)) + b"\n" for row in rows)


def _find_key(value: Any, forbidden: set[str]) -> str | None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).lower()
            if normalized in forbidden:
                return str(key)
            found = _find_key(child, forbidden)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_key(child, forbidden)
            if found is not None:
                return found
    return None


def _find_source_leak(value: Any) -> str | None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).lower()
            if (
                normalized in FORBIDDEN_SOURCE_KEYS
                or normalized.startswith(("future_", "forward_"))
                or "winner" in normalized
                or normalized.endswith(("_mfe", "_mae", "_pnl"))
            ):
                return str(key)
            found = _find_source_leak(child)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_source_leak(child)
            if found is not None:
                return found
    return None


def _self_hash(value: Mapping[str, Any], field: str) -> bool:
    expected = value.get(field)
    core = {key: child for key, child in value.items() if key != field}
    return isinstance(expected, str) and expected == canonical_sha256(core)


def _stage_protocol(path: Path = STAGE_PROTOCOL) -> dict[str, Any]:
    value = _read_json(path)
    if value.get("stage_protocol_version") != "hybrid-v4-s1-mature-stage-v1":
        raise V4S1TrackError("unexpected V4-S1 stage protocol version")
    if value.get("status") != "FINAL_RESEARCH_LOCKED":
        raise V4S1TrackError("V4-S1 stage protocol is not locked")
    if value.get("classification") != (
        "OUTCOME_BLIND_SINGLE_ROUTE_RESEARCH_NOT_COURSE_VALIDATED"
    ):
        raise V4S1TrackError("V4-S1 stage classification changed")
    target = value.get("target") or {}
    if target.get("primary_scenario") != TARGET_SCENARIO or target.get(
        "trade_route"
    ) != TARGET_ROUTE:
        raise V4S1TrackError("V4-S1 target changed")
    lineage = value.get("rule_lineage") or {}
    if (
        lineage.get("v1_v2_v3_are_read_only") is not True
        or lineage.get("v4_s1_changes_v3") is not True
        or lineage.get("policy_version") != "hybrid-v4-s1-atomic-policy-v1"
    ):
        raise V4S1TrackError("V4-S1 rule lineage is not explicit")
    if value.get("source") != EXPECTED_SOURCE_SCOPE:
        raise V4S1TrackError("V4-S1 authoritative source scope changed")
    reachability = value.get("reachability_preflight") or {}
    if (
        reachability.get("required_status") != EXPECTED_PREFLIGHT_STATUS
        or reachability.get("symbolic_witness_must_not_enter_ai_packet") is not True
        or reachability.get("symbolic_classification_must_not_be_treated_as_expected_answer")
        is not True
    ):
        raise V4S1TrackError("V4-S1 reachability contract changed")
    sample = value.get("consistency_sample") or {}
    focuses = list(sample.get("focus_order") or [])
    quotas = sample.get("quotas") or {}
    if list(quotas) != focuses:
        raise V4S1TrackError("V4-S1 quota order differs from focus order")
    if sum(int(child) for child in quotas.values()) != int(sample.get("cases", -1)):
        raise V4S1TrackError("V4-S1 quotas do not exactly cover the case count")
    if int(sample.get("cases", -1)) != 36 or int(sample.get("runs", -1)) != 3:
        raise V4S1TrackError("V4-S1 must remain a 36-case, three-run track")
    if dict(quotas) != EXPECTED_QUOTAS:
        raise V4S1TrackError("V4-S1 exact focus quotas changed")
    if (
        sample.get("one_case_per_anonymous_stock") is not True
        or int(sample.get("minimum_distinct_months", -1)) != 6
        or int(sample.get("maximum_cases_per_month", -1)) != 9
        or sample.get("sampling_focus_must_not_enter_ai_packet") is not True
        or sample.get("all_scenario_hypotheses_remain_visible_to_ai") is not True
    ):
        raise V4S1TrackError("V4-S1 sample isolation or concentration rule changed")
    return value


def load_reachability_preflight(
    preflight_path: Path,
    *,
    source_path: Path,
    source_manifest_path: Path,
    policy_path: Path = V4_S1_POLICY,
) -> dict[str, dict[str, Any]]:
    """Validate the preflight and return only minimal external sample identities.

    Symbolic witness hashes and action signatures are intentionally discarded.
    They cannot flow from this return value into any packet or case record.
    """

    if file_sha256(Path(source_path)) != EXPECTED_SOURCE_SHA256:
        raise V4S1TrackError("authoritative source hash changed")
    if file_sha256(Path(source_manifest_path)) != EXPECTED_SOURCE_MANIFEST_SHA256:
        raise V4S1TrackError("authoritative source-manifest hash changed")
    if file_sha256(Path(preflight_path)) != EXPECTED_PREFLIGHT_FILE_SHA256:
        raise V4S1TrackError("authoritative preflight artifact hash changed")

    report = _read_json(preflight_path)
    if report.get("preflight_version") != PREFLIGHT_VERSION:
        raise V4S1TrackError("unexpected reachability preflight version")
    if report.get("status") != EXPECTED_PREFLIGHT_STATUS:
        raise V4S1TrackError("target route did not pass symbolic reachability")
    if report.get("classification") != EXPECTED_PREFLIGHT_CLASSIFICATION:
        raise V4S1TrackError("preflight classification changed")
    if (
        report.get("outcome_blind") is not True
        or report.get("identity_visible") is not False
        or report.get("future_or_performance_visible") is not False
        or report.get("ai_output_read") is not False
        or report.get("symbolic_witness_inside_ai_packet") is not False
        or report.get("symbolic_witness_has_schema_poison_pill") is not True
    ):
        raise V4S1TrackError("preflight outcome-blind isolation changed")
    if report.get("target") != {"scenario": TARGET_SCENARIO, "route": TARGET_ROUTE}:
        raise V4S1TrackError("preflight target differs from V4-S1")
    if not _self_hash(report, "preflight_sha256"):
        raise V4S1TrackError("preflight self-hash changed")

    source = report.get("source") or {}
    policy = report.get("policy") or {}
    if source.get("sha256") != file_sha256(Path(source_path)):
        raise V4S1TrackError("preflight source hash differs from the selected source")
    if source.get("manifest_sha256") != file_sha256(Path(source_manifest_path)):
        raise V4S1TrackError("preflight source-manifest hash differs")
    if (
        policy.get("version") != "hybrid-v4-s1-atomic-policy-v1"
        or policy.get("sha256") != file_sha256(Path(policy_path))
    ):
        raise V4S1TrackError("preflight V4-S1 policy pin differs")
    stage = report.get("stage_protocol") or {}
    if (
        stage.get("version") != "hybrid-v4-s1-mature-stage-v1"
        or stage.get("sha256") != file_sha256(STAGE_PROTOCOL)
    ):
        raise V4S1TrackError("preflight stage-protocol binding differs")
    provenance = report.get("producer_provenance") or {}
    expected_provenance = {
        "preflight_builder": {
            "relative_path": REACHABILITY_PREFLIGHT_BUILDER.relative_to(ROOT).as_posix(),
            "sha256": EXPECTED_PREFLIGHT_BUILDER_SHA256,
        },
        "source_validator": {
            "relative_path": SOURCE_VALIDATOR.relative_to(ROOT).as_posix(),
            "sha256": EXPECTED_SOURCE_VALIDATOR_SHA256,
        },
        "source_sampling_contract": {
            "relative_path": SOURCE_SAMPLING_CONTRACT.relative_to(ROOT).as_posix(),
            "sha256": EXPECTED_SOURCE_SAMPLING_CONTRACT_SHA256,
        },
    }
    if provenance != expected_provenance:
        raise V4S1TrackError("preflight producer provenance differs")
    for name, path, expected_hash in (
        (
            "preflight builder",
            REACHABILITY_PREFLIGHT_BUILDER,
            EXPECTED_PREFLIGHT_BUILDER_SHA256,
        ),
        ("source validator", SOURCE_VALIDATOR, EXPECTED_SOURCE_VALIDATOR_SHA256),
        (
            "source sampling contract",
            SOURCE_SAMPLING_CONTRACT,
            EXPECTED_SOURCE_SAMPLING_CONTRACT_SHA256,
        ),
    ):
        if file_sha256(path) != expected_hash:
            raise V4S1TrackError(f"{name} hash changed")
    if report.get("source_scope") != {
        key: EXPECTED_SOURCE_SCOPE[key]
        for key in ("stocks", "review_points", "monitoring_start", "monitoring_end")
    }:
        raise V4S1TrackError("preflight source-scope binding differs")
    if report.get("requirements") != {
        "minimum_unique_stocks": 12,
        "minimum_distinct_months": 6,
    }:
        raise V4S1TrackError("preflight minimum capacity requirements changed")

    candidates = report.get("candidates")
    if not isinstance(candidates, list):
        raise V4S1TrackError("preflight candidates are missing")
    if report.get("candidates_sha256") != canonical_sha256(candidates):
        raise V4S1TrackError("preflight candidate index hash changed")
    capacity = report.get("capacity") or {}

    minimal: dict[str, dict[str, Any]] = {}
    seen_ordinals: set[int] = set()
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise V4S1TrackError("preflight candidate is not an object")
        if candidate.get("classification") != "SYMBOLIC_REACHABLE_NOT_SEMANTIC_GOLD":
            raise V4S1TrackError("preflight candidate classification changed")
        review_id = str(candidate.get("review_id") or "")
        try:
            source_ordinal = int(candidate["source_ordinal"])
        except (KeyError, TypeError, ValueError) as exc:
            raise V4S1TrackError("preflight candidate has no source ordinal") from exc
        if not review_id or review_id in minimal or source_ordinal in seen_ordinals:
            raise V4S1TrackError("preflight candidate identity is missing or duplicate")
        packet_sha256 = str(candidate.get("packet_sha256") or "")
        if len(packet_sha256) != 64:
            raise V4S1TrackError("preflight candidate packet hash is invalid")
        seen_ordinals.add(source_ordinal)
        minimal[review_id] = {
            "source_ordinal": source_ordinal,
            "anonymous_stock_id": str(candidate.get("anonymous_stock_id") or ""),
            "as_of": str(candidate.get("as_of") or ""),
            "packet_sha256": packet_sha256,
        }
    actual_capacity = {
        "reachable_rows": len(minimal),
        "unique_anonymous_stocks": len(
            {row["anonymous_stock_id"] for row in minimal.values()}
        ),
        "distinct_months": len({row["as_of"][:7] for row in minimal.values()}),
    }
    if capacity != actual_capacity:
        raise V4S1TrackError("preflight reachable capacity differs from candidates")
    requirements = report["requirements"]
    if (
        actual_capacity["reachable_rows"] < int(requirements["minimum_unique_stocks"])
        or actual_capacity["unique_anonymous_stocks"]
        < int(requirements["minimum_unique_stocks"])
        or actual_capacity["unique_anonymous_stocks"]
        < EXPECTED_QUOTAS["MATURE_SYMBOLIC_REACHABLE"]
        or actual_capacity["distinct_months"]
        < int(requirements["minimum_distinct_months"])
    ):
        raise V4S1TrackError("preflight PASS has insufficient target-route capacity")
    return minimal


def _has_mature_hypothesis(record: Mapping[str, Any]) -> bool:
    packet = record.get("packet")
    if not isinstance(packet, Mapping):
        raise V4S1TrackError("source row has no packet")
    facts = packet.get("objective_facts")
    hypotheses = facts.get("scenario_hypotheses") if isinstance(facts, Mapping) else None
    mature = hypotheses.get(TARGET_SCENARIO) if isinstance(hypotheses, Mapping) else None
    return isinstance(mature, list) and bool(mature)


def stage_focuses(
    record: Mapping[str, Any], reachable_review_ids: set[str] | frozenset[str]
) -> list[str]:
    """Derive validation-only challenge roles from as-of objective facts."""

    packet = record.get("packet")
    if not isinstance(packet, Mapping):
        raise V4S1TrackError("source row has no packet")
    facts = packet.get("objective_facts")
    if not isinstance(facts, Mapping):
        raise V4S1TrackError("source packet has no objective facts")
    eligible = set(record.get("eligible_sampling_strata") or [])
    waits = set(facts.get("wait_boundary_reasons") or [])
    review_id = str(record.get("review_id") or "")
    has_mature = _has_mature_hypothesis(record)

    result: list[str] = []
    if has_mature:
        result.append(
            "MATURE_SYMBOLIC_REACHABLE"
            if review_id in reachable_review_ids
            else "MATURE_SYMBOLIC_UNREACHABLE"
        )
    if SOURCE_STRATA["MACRO"] in eligible and not has_mature:
        result.append("COMPETING_MACRO_ONLY")
    if SOURCE_STRATA["FRESH"] in eligible and not has_mature:
        result.append("COMPETING_FRESH_ONLY")
    if SOURCE_STRATA["BEAR"] in eligible and not has_mature:
        result.append("COMPETING_BEAR_ONLY")
    if SOURCE_STRATA["REMOVE"] in eligible:
        result.append("MACRO_DEFENSE_REMOVE")
    if "UP_ATTACK_WITHOUT_CAUSAL_STOP" in waits:
        result.append("WAIT_NO_CAUSAL_STOP")
    if "MATERIAL_OBJECTIVE_HYPOTHESIS_CONFLICT" in waits:
        result.append("WAIT_MATERIAL_CONFLICT")
    return result


def _validate_reachable_identity(
    row: Mapping[str, Any], candidate: Mapping[str, Any]
) -> None:
    packet = row["packet"]
    actual = {
        "source_ordinal": int(row["source_ordinal"]),
        "anonymous_stock_id": str(row["anonymous_stock_id"]),
        "as_of": str(packet["as_of"]),
        "packet_sha256": str(row["packet_sha256"]),
    }
    if actual != dict(candidate):
        raise V4S1TrackError("preflight candidate identity differs from source")
    if not _has_mature_hypothesis(row):
        raise V4S1TrackError("reachable candidate has no mature hypothesis")


def _validate_authoritative_source_scope(
    manifest: Mapping[str, Any], records: Sequence[Mapping[str, Any]]
) -> None:
    if int(manifest.get("stocks", -1)) != EXPECTED_SOURCE_SCOPE["stocks"]:
        raise V4S1TrackError("source stock-universe coverage changed")
    if int(manifest.get("review_points", -1)) != EXPECTED_SOURCE_SCOPE["review_points"]:
        raise V4S1TrackError("source review-point coverage changed")
    if manifest.get("as_of_ceiling") != EXPECTED_SOURCE_SCOPE["monitoring_end"]:
        raise V4S1TrackError("source as-of ceiling changed")
    if len(records) != EXPECTED_SOURCE_SCOPE["review_points"]:
        raise V4S1TrackError("source record count differs from locked stage scope")
    if len({str(row.get("anonymous_stock_id") or "") for row in records}) != (
        EXPECTED_SOURCE_SCOPE["stocks"]
    ):
        raise V4S1TrackError("source anonymous-stock coverage differs from stage")
    dates = [str(row.get("as_of") or "") for row in records]
    if (
        not dates
        or min(dates) != EXPECTED_SOURCE_SCOPE["monitoring_start"]
        or max(dates) != EXPECTED_SOURCE_SCOPE["monitoring_end"]
    ):
        raise V4S1TrackError("source date coverage differs from locked stage scope")
    for row in records:
        as_of = str(row.get("as_of") or "")
        if not (
            EXPECTED_SOURCE_SCOPE["monitoring_start"]
            <= as_of
            <= EXPECTED_SOURCE_SCOPE["monitoring_end"]
        ):
            raise V4S1TrackError("source row falls outside the locked monitoring range")


def scan_source(
    source_path: Path,
    source_manifest_path: Path,
    preflight_path: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, dict[str, Any]]]:
    """Scan only the locked outcome-blind source and preflight classifications."""

    manifest = _read_json(source_manifest_path)
    if manifest.get("status") != "LOCKED_OUTCOME_BLIND":
        raise V4S1TrackError("source is not locked outcome blind")
    for field, expected in {
        "outcome_blind": True,
        "identity_visible": False,
        "identity_isolated": True,
        "performance_visible": False,
        "future_data_visible": False,
        "source_order_locked": True,
    }.items():
        if manifest.get(field) is not expected:
            raise V4S1TrackError(f"source manifest changed {field}")
    if file_sha256(Path(source_path)) != manifest.get("artifact_sha256"):
        raise V4S1TrackError("source artifact hash differs from manifest")
    if manifest.get("sampling_contract_sha256") != file_sha256(
        SOURCE_SAMPLING_CONTRACT
    ):
        raise V4S1TrackError("source sampling contract pin changed")

    reachable = load_reachability_preflight(
        preflight_path,
        source_path=source_path,
        source_manifest_path=source_manifest_path,
    )
    reachable_ids = frozenset(reachable)
    source_contract = load_sampling_contract(SOURCE_SAMPLING_CONTRACT)
    records: list[dict[str, Any]] = []
    seen_reviews: set[str] = set()
    seen_ordinals: set[int] = set()
    matched_reachable: set[str] = set()
    for row in _jsonl(source_path):
        forbidden = _find_source_leak(row)
        if forbidden is not None:
            raise V4S1TrackError(f"outcome/future key present in source: {forbidden}")
        validate_source_record(row, source_contract)
        ordinal = int(row["source_ordinal"])
        review_id = str(row["review_id"])
        if ordinal in seen_ordinals or review_id in seen_reviews:
            raise V4S1TrackError("source contains duplicate identity")
        seen_ordinals.add(ordinal)
        seen_reviews.add(review_id)
        if review_id in reachable:
            _validate_reachable_identity(row, reachable[review_id])
            matched_reachable.add(review_id)
        records.append(
            {
                "source_ordinal": ordinal,
                "review_id": review_id,
                "anonymous_stock_id": row["anonymous_stock_id"],
                "packet_sha256": row["packet_sha256"],
                "as_of": row["packet"]["as_of"],
                "eligible_sampling_strata": list(row["eligible_sampling_strata"]),
                "eligible_stage_focuses": stage_focuses(row, reachable_ids),
            }
        )
    expected_rows = int(manifest.get("review_points", -1))
    if len(records) != expected_rows or [row["source_ordinal"] for row in records] != list(
        range(expected_rows)
    ):
        raise V4S1TrackError("source coverage is not exact")
    if matched_reachable != reachable_ids:
        raise V4S1TrackError("preflight candidates are not exactly covered by source")
    _validate_authoritative_source_scope(manifest, records)
    return records, manifest, reachable


def _rank(seed: str, record: Mapping[str, Any], suffix: str = "") -> str:
    return canonical_sha256(
        {
            "seed": seed,
            "source_ordinal": record["source_ordinal"],
            "review_id": record["review_id"],
            "packet_sha256": record["packet_sha256"],
            "suffix": suffix,
        }
    )


def allocate(
    records: Sequence[Mapping[str, Any]], protocol: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Fill exact quotas with stock uniqueness and exact month constraints.

    The deterministic bounded search treats focus, anonymous stock, and month
    as simultaneous constraints.  This avoids accepting a max-flow allocation
    that fills quotas but only discovers an avoidable month violation later.
    """

    sample = protocol["consistency_sample"]
    seed = str(sample["seed"])
    focuses = list(sample["focus_order"])
    quotas = {focus: int(sample["quotas"][focus]) for focus in focuses}
    minimum_months = int(sample["minimum_distinct_months"])
    month_cap = int(sample["maximum_cases_per_month"])
    candidates = [dict(row) for row in records if row.get("eligible_stage_focuses")]
    if len({str(row["review_id"]) for row in candidates}) != len(candidates):
        raise V4S1TrackError("allocation candidates repeat review identity")

    by_focus: dict[str, list[dict[str, Any]]] = {}
    for focus in focuses:
        rows = [row for row in candidates if focus in row["eligible_stage_focuses"]]
        rows.sort(key=lambda row: (_rank(seed, row, focus), int(row["source_ordinal"])))
        by_focus[focus] = rows
        if len({str(row["anonymous_stock_id"]) for row in rows}) < quotas[focus]:
            raise V4S1TrackError(f"insufficient distinct-stock capacity for {focus}")

    remaining = dict(quotas)
    used_stocks: set[str] = set()
    month_counts: Counter[str] = Counter()
    selected: list[dict[str, Any]] = []
    search_nodes = 0

    def feasible(focus: str) -> list[dict[str, Any]]:
        return [
            row
            for row in by_focus[focus]
            if str(row["anonymous_stock_id"]) not in used_stocks
            and month_counts[str(row["as_of"])[:7]] < month_cap
        ]

    def search() -> bool:
        nonlocal search_nodes
        search_nodes += 1
        if search_nodes > MAX_ALLOCATION_SEARCH_NODES:
            raise V4S1TrackError("deterministic allocation search budget exhausted")
        active = [focus for focus in focuses if remaining[focus] > 0]
        if not active:
            return len(month_counts) >= minimum_months

        feasible_by_focus = {focus: feasible(focus) for focus in active}
        if any(len(feasible_by_focus[focus]) < remaining[focus] for focus in active):
            return False
        possible_months = set(month_counts)
        for rows in feasible_by_focus.values():
            possible_months.update(str(row["as_of"])[:7] for row in rows)
        if len(possible_months) < minimum_months:
            return False

        focus = min(
            active,
            key=lambda name: (
                len(feasible_by_focus[name]) / remaining[name],
                len(feasible_by_focus[name]),
                focuses.index(name),
            ),
        )
        rows = feasible_by_focus[focus]
        need_new_month = len(month_counts) < minimum_months
        rows.sort(
            key=lambda row: (
                1
                if need_new_month and str(row["as_of"])[:7] in month_counts
                else 0,
                month_counts[str(row["as_of"])[:7]],
                _rank(seed, row, focus),
                int(row["source_ordinal"]),
            )
        )
        for source_row in rows:
            stock = str(source_row["anonymous_stock_id"])
            month = str(source_row["as_of"])[:7]
            row = deepcopy(source_row)
            row["sampling_focus"] = focus
            used_stocks.add(stock)
            month_counts[month] += 1
            remaining[focus] -= 1
            selected.append(row)
            if search():
                return True
            selected.pop()
            remaining[focus] += 1
            month_counts[month] -= 1
            if month_counts[month] == 0:
                del month_counts[month]
            used_stocks.remove(stock)
        return False

    if not search():
        capacity = Counter(
            focus for row in candidates for focus in row["eligible_stage_focuses"]
        )
        raise V4S1TrackError(
            "cannot fill V4-S1 quotas with stock/month constraints; "
            f"capacity={dict(capacity)}"
        )
    selected.sort(
        key=lambda row: (focuses.index(row["sampling_focus"]), row["source_ordinal"])
    )
    if len(selected) != sum(quotas.values()):
        raise V4S1TrackError("V4-S1 allocation case count changed")
    if len({str(row["anonymous_stock_id"]) for row in selected}) != len(selected):
        raise V4S1TrackError("V4-S1 allocation repeats an anonymous stock")
    if Counter(row["sampling_focus"] for row in selected) != Counter(quotas):
        raise V4S1TrackError("V4-S1 allocation did not fill exact quotas")
    months = Counter(str(row["as_of"])[:7] for row in selected)
    if len(months) < minimum_months or max(months.values(), default=0) > month_cap:
        raise V4S1TrackError("V4-S1 allocation violates month constraints")
    return selected


def _assert_ai_packet_clean(packet: Mapping[str, Any]) -> None:
    forbidden = _find_source_leak(packet)
    if forbidden is not None:
        raise V4S1TrackError(f"outcome/future key present in AI packet: {forbidden}")
    leaked = _find_key(packet, FORBIDDEN_AI_PACKET_METADATA_KEYS)
    if leaked is not None:
        raise V4S1TrackError(f"sampling/witness metadata leaked into AI packet: {leaked}")


def extract_packets(
    source_path: Path, selected: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    wanted = {str(row["review_id"]): row for row in selected}
    if len(wanted) != len(selected):
        raise V4S1TrackError("selected review identity is duplicated")
    found: dict[str, dict[str, Any]] = {}
    for source in _jsonl(source_path):
        review_id = str(source.get("review_id") or "")
        if review_id not in wanted:
            continue
        expected = wanted[review_id]
        if source.get("packet_sha256") != expected.get("packet_sha256"):
            raise V4S1TrackError("selected packet hash changed")
        packet = source.get("packet")
        if not isinstance(packet, dict) or canonical_sha256(packet) != expected["packet_sha256"]:
            raise V4S1TrackError("selected packet content changed")
        _assert_ai_packet_clean(packet)
        found[review_id] = deepcopy(packet)
    if set(found) != set(wanted):
        raise V4S1TrackError("selected packets are missing from source")
    packets = [found[str(row["review_id"])] for row in selected]
    for packet in packets:
        _assert_ai_packet_clean(packet)
    return packets


def _component(
    name: str,
    path: Path,
    *,
    status: str = "FINAL",
    role: str | None = None,
) -> dict[str, Any]:
    resolved = Path(path).resolve()
    if not resolved.is_file():
        raise V4S1TrackError(f"missing frozen component: {resolved}")
    try:
        relative_path = resolved.relative_to(ROOT.resolve()).as_posix()
    except ValueError as exc:
        raise V4S1TrackError(f"component is outside repository: {resolved}") from exc
    result = {
        "name": name,
        "relative_path": relative_path,
        "status": status,
        "sha256": file_sha256(resolved),
    }
    if role is not None:
        result["role"] = role
    return result


def _v2_components() -> list[dict[str, Any]]:
    """Pins required by the unchanged formal V3 launcher/transport verifier."""

    return [
        _component("launcher", LAUNCHER),
        _component("reviewer", REVIEWER),
        _component("runner", RUNNER),
        _component(
            "policy",
            V3_SEMANTIC_POLICY,
            role="UNCHANGED_ATOMIC_SCHEMA_VALIDATOR_NOT_V4_DECISION_REDUCER",
        ),
        _component("prompt", PROMPT),
        _component("schema", SCHEMA),
        _component("protocol", RESEARCH_PROTOCOL),
    ]


def _v4_s1_components(preflight_path: Path) -> list[dict[str, Any]]:
    return [
        _component(
            "stage_protocol",
            STAGE_PROTOCOL,
            status="FINAL_RESEARCH_LOCKED",
        ),
        _component(
            "reachability_preflight",
            preflight_path,
            status=EXPECTED_PREFLIGHT_STATUS,
            role="SAMPLING_ONLY_NEVER_AI_VISIBLE",
        ),
        _component(
            "reachability_preflight_builder",
            REACHABILITY_PREFLIGHT_BUILDER,
            role="OUTCOME_BLIND_SYMBOLIC_CLASSIFIER_PRODUCER",
        ),
        _component(
            "policy",
            V4_S1_POLICY,
            status="DRAFT_FOR_OUTCOME_BLIND_RESEARCH",
            role="V4_S1_DECISION_REDUCER",
        ),
        _component("track_builder", Path(__file__).resolve()),
        _component("evaluator", CONSISTENCY_EVALUATOR),
        _component("execution_orchestrator", EXECUTION_ORCHESTRATOR),
    ]


def prepare(
    *,
    source_path: Path,
    source_manifest_path: Path,
    preflight_path: Path,
    output_dir: Path,
    shard_count: int = 4,
) -> dict[str, Any]:
    """Create immutable research inputs only; no model or performance step runs."""

    protocol = _stage_protocol()
    records, source_manifest, reachable = scan_source(
        Path(source_path), Path(source_manifest_path), Path(preflight_path)
    )
    selected = allocate(records, protocol)
    packets = extract_packets(Path(source_path), selected)
    output_dir = Path(output_dir).resolve()

    focus_counts = Counter(row["sampling_focus"] for row in selected)
    month_counts = Counter(str(row["as_of"])[:7] for row in selected)
    plan_core = {
        "selection_version": SELECTION_VERSION,
        "status": "LOCKED_OUTCOME_BLIND",
        "classification": protocol["classification"],
        "stage_protocol_sha256": file_sha256(STAGE_PROTOCOL),
        "reachability_preflight_sha256": file_sha256(Path(preflight_path)),
        "source_artifact_sha256": file_sha256(Path(source_path)),
        "source_manifest_sha256": file_sha256(Path(source_manifest_path)),
        "source_rows": int(source_manifest["review_points"]),
        "preflight_reachable_capacity": len(reachable),
        "cases": len(selected),
        "one_case_per_anonymous_stock": True,
        "minimum_distinct_months": int(
            protocol["consistency_sample"]["minimum_distinct_months"]
        ),
        "maximum_cases_per_month": int(
            protocol["consistency_sample"]["maximum_cases_per_month"]
        ),
        "sampling_focus_is_validation_only": True,
        "symbolic_classification_is_not_expected_answer": True,
        "symbolic_witness_present": False,
        "identity_visible_inside_ai_packet": False,
        "future_or_performance_visible": False,
        "focus_counts": {focus: focus_counts[focus] for focus in protocol["consistency_sample"]["focus_order"]},
        "month_counts": dict(sorted(month_counts.items())),
        "rows": selected,
        "rows_sha256": canonical_sha256(selected),
    }
    selection_plan = {**plan_core, "selection_sha256": canonical_sha256(plan_core)}
    selection_path = output_dir / "selection_plan.json"
    _publish_immutable(selection_path, canonical_json_bytes(selection_plan) + b"\n")

    packet_payload = _canonical_jsonl(packets)
    packet_path = output_dir / "primary_packets.jsonl"
    _publish_immutable(packet_path, packet_payload)
    mapping = [
        {
            "selected_ordinal": index,
            "source_ordinal": int(row["source_ordinal"]),
            "review_id": row["review_id"],
            "anonymous_stock_id": row["anonymous_stock_id"],
            "packet_sha256": row["packet_sha256"],
            "sampling_focus": row["sampling_focus"],
            "eligible_stage_focuses": row["eligible_stage_focuses"],
        }
        for index, row in enumerate(selected)
    ]
    packet_manifest_core = {
        "manifest_version": PACKET_MANIFEST_VERSION,
        "status": "LOCKED_OUTCOME_BLIND",
        "block_id": "V4_S1_MATURE_TREND_PULLBACK_V2_CORE",
        "rows": len(packets),
        "packets_canonical_jsonl_sha256": hashlib.sha256(packet_payload).hexdigest(),
        "sampling_metadata_inside_packet": False,
        "symbolic_witness_inside_packet": False,
        "symbolic_classification_inside_packet": False,
        "all_scenario_hypotheses_preserved_inside_packet": True,
        "identity_visible_inside_packet": False,
        "future_or_performance_visible_inside_packet": False,
        "mapping": mapping,
        "mapping_sha256": canonical_sha256(mapping),
    }
    packet_manifest = {
        **packet_manifest_core,
        "manifest_sha256": canonical_sha256(packet_manifest_core),
    }
    packet_manifest_path = output_dir / "primary_packets.manifest.json"
    _publish_immutable(
        packet_manifest_path, canonical_json_bytes(packet_manifest) + b"\n"
    )

    freeze_core = {
        "freeze_version": FREEZE_VERSION,
        "status": "FROZEN_IMMUTABLE_BEFORE_AI_EXECUTION",
        "track_version": TRACK_VERSION,
        "track_status": TRACK_STATUS,
        "classification": protocol["classification"],
        "expected_cases": 36,
        "expected_runs": 3,
        "outcome_blind": True,
        "identity_visible": False,
        "future_or_performance_visible": False,
        "old_ai_output_visible": False,
        "sampling_metadata_inside_ai_packet": False,
        "symbolic_witness_inside_ai_packet": False,
        "symbolic_classification_inside_ai_packet": False,
        "symbolic_classification_is_expected_answer": False,
        "policy_decision_changed_relative_to_v3": True,
        "strategy_or_gate_change": True,
        "v4_s1_is_rule_correction_relative_to_v3": True,
        "v4_s1_changes_v3": True,
        "v1_v2_v3_read_only": True,
        "v1_v2_v3_are_read_only": True,
        "v1_v2_v3_rules_unchanged": True,
        "v1_v2_v3_artifacts_modified": False,
        "ai_atomic_questions_changed": False,
        "ai_evidence_format_changed": False,
        "execution_or_position_rules_changed": False,
        "single_route_scope": {
            "primary_scenario": TARGET_SCENARIO,
            "trade_route": TARGET_ROUTE,
            "other_scenarios_are_negative_controls": True,
            "other_routes_suspended": True,
        },
        "repeatability_acceptance": deepcopy(
            protocol["repeatability_acceptance"]
        ),
        "disagreement_policy": protocol["disagreement_policy"],
        "budget_guard": {
            **deepcopy(protocol["budget_guard"]),
            "enforcement_boundary": "EXTERNAL_EXECUTION_ORCHESTRATOR",
            "enforcement_component": "execution_orchestrator",
        },
        "stage_sequence": list(protocol["sequence"]),
        "stage_protocol_sha256": file_sha256(STAGE_PROTOCOL),
        "reachability_preflight_sha256": file_sha256(Path(preflight_path)),
        "source_artifact_sha256": file_sha256(Path(source_path)),
        "source_manifest_sha256": file_sha256(Path(source_manifest_path)),
        "selection_plan_sha256": file_sha256(selection_path),
        "primary_packet_sha256": file_sha256(packet_path),
        "primary_packet_manifest_sha256": file_sha256(packet_manifest_path),
        "v2_components": _v2_components(),
        "v4_s1_components": _v4_s1_components(Path(preflight_path)),
        "support_components": [
            _component("source_sampling_contract", SOURCE_SAMPLING_CONTRACT),
            _component("source_validator", SOURCE_VALIDATOR),
            _component("sharding", SHARDING),
        ],
        "execution_contract": {"model": MODEL, "reasoning_effort": REASONING},
    }
    freeze = {**freeze_core, "freeze_sha256": canonical_sha256(freeze_core)}
    freeze_path = output_dir / "research_execution_freeze.json"
    _publish_immutable(freeze_path, canonical_json_bytes(freeze) + b"\n")

    case_records = build_case_records(
        packets,
        source_manifest_sha256=file_sha256(packet_path),
        protocol_sha256=file_sha256(RESEARCH_PROTOCOL),
        prompt_sha256=file_sha256(PROMPT),
        schema_sha256=file_sha256(SCHEMA),
        execution_contract_sha256=file_sha256(freeze_path),
        model=MODEL,
        reasoning_effort=REASONING,
    )
    assignment = write_shards(
        case_records, output_dir / "primary_cases", shard_count=shard_count
    )
    assignment_path = output_dir / "primary_cases/assignment.manifest.json"
    track_core = {
        "track_version": TRACK_VERSION,
        "status": TRACK_STATUS,
        "classification": protocol["classification"],
        "stage": "MATURE_TREND_PULLBACK_V2_CORE_ONLY",
        "outcome_blind": True,
        "identity_visible": False,
        "future_or_performance_visible": False,
        "model": MODEL,
        "reasoning_effort": REASONING,
        "cases": len(packets),
        "runs": int(protocol["consistency_sample"]["runs"]),
        "selection_plan_path": str(selection_path),
        "selection_plan_sha256": file_sha256(selection_path),
        "packet_path": str(packet_path),
        "packet_sha256": file_sha256(packet_path),
        "packet_manifest_path": str(packet_manifest_path),
        "packet_manifest_sha256": file_sha256(packet_manifest_path),
        "execution_freeze_path": str(freeze_path),
        "execution_freeze_sha256": file_sha256(freeze_path),
        "assignment_manifest_path": str(assignment_path),
        "assignment_manifest_sha256": file_sha256(assignment_path),
        "assignment_sha256": assignment["assignment_sha256"],
        "policy_decision_changed_relative_to_v3": True,
        "v4_s1_changes_v3": True,
        "v1_v2_v3_read_only": True,
        "performance_must_remain_sealed_until_prior_gates_pass": True,
    }
    track = {**track_core, "track_manifest_sha256": canonical_sha256(track_core)}
    track_path = output_dir / "research_track_manifest.json"
    _publish_immutable(track_path, canonical_json_bytes(track) + b"\n")
    return track


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--source-manifest", type=Path, default=SOURCE_MANIFEST)
    parser.add_argument("--preflight", type=Path, default=REACHABILITY_PREFLIGHT)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--shard-count", type=int, default=4)
    args = parser.parse_args()
    result = prepare(
        source_path=args.source.resolve(),
        source_manifest_path=args.source_manifest.resolve(),
        preflight_path=args.preflight.resolve(),
        output_dir=args.output_dir.resolve(),
        shard_count=args.shard_count,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
