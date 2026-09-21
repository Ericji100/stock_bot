"""Outcome-blind V4-S1 reachability preflight for a merged V7 candidate source.

This module is deliberately separate from every frozen V1/V2/V3/V4-S1/V7
component.  It accepts only the non-final V7 merge-adapter manifest state and
uses the frozen V4-S1 symbolic witness and reducer to test the Mature/V2-Core
software path.  Witness values are hashed into the report and never written
back to, or embedded in, an AI-visible packet.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import date
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PREFLIGHT_BUILDER_PATH = Path(__file__).resolve()
V7_BUILDER_PATH = ROOT / "scripts/hybrid_v3_atomic_packets_v7.py"
MERGE_ADAPTER_PATH = ROOT / "scripts/hybrid_v3_atomic_packets_v7_merge_adapter_v1.py"
POLICY_MODULE_PATH = ROOT / "scripts/hybrid_v4_s1_atomic_policy_v1.py"
SOURCE_VALIDATOR_PATH = ROOT / "scripts/hybrid_v3_consistency_v3.py"
SOURCE_SAMPLING_CONTRACT = ROOT / "config/hybrid_multilabel_sampling_v3.json"

try:
    from . import hybrid_v3_atomic_packets_v7 as v7_builder
    from . import hybrid_v3_atomic_packets_v7_merge_adapter_v1 as merge_adapter
    from . import hybrid_v4_s1_reachability_preflight_v1 as frozen_preflight
    from .hybrid_v3_consistency_v3 import (
        load_sampling_contract,
        validate_source_record,
    )
    from .hybrid_v3_sharding_v2 import _publish_immutable
    from .hybrid_v4_s1_atomic_policy_v1 import (
        POLICY_VERSION,
        reduce_atomic_v4_s1,
        stage_permission_v4_s1,
        validate_atomic,
    )
except ImportError:  # pragma: no cover
    from scripts import hybrid_v3_atomic_packets_v7 as v7_builder
    from scripts import hybrid_v3_atomic_packets_v7_merge_adapter_v1 as merge_adapter
    from scripts import hybrid_v4_s1_reachability_preflight_v1 as frozen_preflight
    from scripts.hybrid_v3_consistency_v3 import (
        load_sampling_contract,
        validate_source_record,
    )
    from scripts.hybrid_v3_sharding_v2 import _publish_immutable
    from scripts.hybrid_v4_s1_atomic_policy_v1 import (
        POLICY_VERSION,
        reduce_atomic_v4_s1,
        stage_permission_v4_s1,
        validate_atomic,
    )


PREFLIGHT_VERSION = "hybrid-v4-s1-v7-reachability-preflight-v1"
EXPECTED_BUILDER_VERSION = "hybrid-v3-atomic-packets-v7"
EXPECTED_BUILDER_STATUS = "CANDIDATE_FOR_OUTCOME_BLIND_EVIDENCE_VALIDATION"
EXPECTED_MANIFEST_STATUS = "BUILT_OUTCOME_BLIND_CANDIDATE_NOT_FORMAL_ACCEPTANCE"
EXPECTED_V7_SHA256 = "09d61c36efdf6f19052f18a594f74a2278cc92ca217b3e4b3c5af2f51e8f6034"
EXPECTED_MERGE_ADAPTER_VERSION = "hybrid-v3-atomic-packets-v7-merge-adapter-v1"
EXPECTED_MERGE_ADAPTER_STATUS = "FINAL_TECHNICAL_ADAPTER"
EXPECTED_POLICY_VERSION = "hybrid-v4-s1-atomic-policy-v1"
EXPECTED_POLICY_SHA256 = "7d181ad38a542b2559492ec3acebe2859aec0c4ec1b7532cdc13143aed5ecc1b"
EXPECTED_STAGE_VERSION = "hybrid-v4-s1-mature-stage-v1"
EXPECTED_STAGE_SHA256 = "0372803b966a7dd47d44350838a1ed79c3be40a5e331f27a69db38f99bc0745e"
EXPECTED_VALIDATOR_SHA256 = "07b76d07b62510e5226824aec61b496a8c0cf36ecd9d36546dd76638938e5855"
EXPECTED_SAMPLING_CONTRACT_SHA256 = "b22c9212a880f45896d60d80e6aaee77e5cb881e19cf9139a8075f462772bff1"
EXPECTED_STOCKS = 1029
EXPECTED_STOCK_DAYS = 151804
EXPECTED_MONITORING_START = "2023-06-01"
EXPECTED_MONITORING_END = "2024-02-02"
MINIMUM_UNIQUE_STOCKS = 18
MINIMUM_DISTINCT_MONTHS = 6
TARGET_SCENARIO = "MATURE_TREND_PULLBACK"
TARGET_ROUTE = "V2_CORE"
TARGET_SAMPLING_STRATUM = "V2_CORE_OBJECTIVE_PROXY"
ANONYMOUS_STOCK_RE = re.compile(r"^S-[0-9a-f]{16}$")

FORBIDDEN_SOURCE_KEYS = {
    "ai_output",
    "company_name",
    "code",
    "exit_date",
    "exit_price",
    "forward_return",
    "future_close",
    "future_high",
    "future_low",
    "future_open",
    "future_return",
    "isin",
    "mae",
    "mfe",
    "model_output",
    "next_20d_return",
    "outcome",
    "pnl",
    "private_code",
    "profit",
    "profit_factor",
    "realized_return",
    "return_after",
    "security_code",
    "security_name",
    "semantic_output",
    "stock_code",
    "stock_name",
    "symbol",
    "symbolic_witness",
    "ticker",
    "unrealized_return",
    "winner",
    "_preflight_symbolic_only",
}


class V7ReachabilityPreflightError(ValueError):
    """The V7 candidate source or frozen reachability contract is invalid."""


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise V7ReachabilityPreflightError(f"cannot read JSON: {path}") from exc
    if not isinstance(value, dict):
        raise V7ReachabilityPreflightError(f"expected JSON object: {path}")
    return value


def _jsonl(path: Path) -> Iterable[dict[str, Any]]:
    try:
        with Path(path).open(encoding="utf-8-sig") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise V7ReachabilityPreflightError(
                        f"expected object at {path}:{line_number}"
                    )
                yield value
    except (OSError, json.JSONDecodeError) as exc:
        raise V7ReachabilityPreflightError(f"cannot read JSONL: {path}") from exc


def _contains_forbidden_key(value: Any) -> str | None:
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
            found = _contains_forbidden_key(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _contains_forbidden_key(child)
            if found:
                return found
    return None


def _validate_frozen_components(policy_path: Path, stage_path: Path) -> dict[str, Any]:
    if v7_builder.BUILDER_VERSION != EXPECTED_BUILDER_VERSION:
        raise V7ReachabilityPreflightError("unexpected V7 packet builder version")
    if v7_builder.BUILDER_STATUS != EXPECTED_BUILDER_STATUS:
        raise V7ReachabilityPreflightError("unexpected V7 packet builder status")
    if file_sha256(V7_BUILDER_PATH) != EXPECTED_V7_SHA256:
        raise V7ReachabilityPreflightError("V7 packet builder hash changed")
    try:
        v7_builder.assert_base_builder_frozen()
    except Exception as exc:
        raise V7ReachabilityPreflightError("V7 frozen base chain changed") from exc
    if (
        merge_adapter.VERSION != EXPECTED_MERGE_ADAPTER_VERSION
        or merge_adapter.STATUS != EXPECTED_MERGE_ADAPTER_STATUS
        or merge_adapter.PINNED_V7_SHA256 != EXPECTED_V7_SHA256
        or merge_adapter.CANDIDATE_MANIFEST_STATUS != EXPECTED_MANIFEST_STATUS
    ):
        raise V7ReachabilityPreflightError("unexpected V7 merge adapter contract")
    if POLICY_VERSION != EXPECTED_POLICY_VERSION:
        raise V7ReachabilityPreflightError("unexpected V4-S1 policy version")
    if file_sha256(policy_path) != EXPECTED_POLICY_SHA256:
        raise V7ReachabilityPreflightError("V4-S1 policy hash changed")
    if file_sha256(POLICY_MODULE_PATH) != EXPECTED_POLICY_SHA256:
        raise V7ReachabilityPreflightError("imported V4-S1 policy hash changed")
    if file_sha256(stage_path) != EXPECTED_STAGE_SHA256:
        raise V7ReachabilityPreflightError("V4-S1 stage protocol hash changed")
    if file_sha256(SOURCE_VALIDATOR_PATH) != EXPECTED_VALIDATOR_SHA256:
        raise V7ReachabilityPreflightError("source validator hash changed")
    if file_sha256(SOURCE_SAMPLING_CONTRACT) != EXPECTED_SAMPLING_CONTRACT_SHA256:
        raise V7ReachabilityPreflightError("source sampling contract hash changed")
    return {
        "preflight_builder": {
            "relative_path": PREFLIGHT_BUILDER_PATH.relative_to(ROOT).as_posix(),
            "version": PREFLIGHT_VERSION,
            "sha256": file_sha256(PREFLIGHT_BUILDER_PATH),
        },
        "v7_packet_builder": {
            "relative_path": V7_BUILDER_PATH.relative_to(ROOT).as_posix(),
            "version": v7_builder.BUILDER_VERSION,
            "status": v7_builder.BUILDER_STATUS,
            "sha256": file_sha256(V7_BUILDER_PATH),
        },
        "merge_adapter": {
            "relative_path": MERGE_ADAPTER_PATH.relative_to(ROOT).as_posix(),
            "version": merge_adapter.VERSION,
            "status": merge_adapter.STATUS,
            "sha256": file_sha256(MERGE_ADAPTER_PATH),
            "pinned_v7_sha256": merge_adapter.PINNED_V7_SHA256,
        },
        "v4_s1_policy": {
            "relative_path": POLICY_MODULE_PATH.relative_to(ROOT).as_posix(),
            "version": POLICY_VERSION,
            "sha256": file_sha256(POLICY_MODULE_PATH),
        },
        "stage_protocol": {
            "relative_path": stage_path.relative_to(ROOT).as_posix()
            if stage_path.is_relative_to(ROOT)
            else str(stage_path),
            "sha256": file_sha256(stage_path),
        },
        "source_validator": {
            "relative_path": SOURCE_VALIDATOR_PATH.relative_to(ROOT).as_posix(),
            "sha256": file_sha256(SOURCE_VALIDATOR_PATH),
        },
        "source_sampling_contract": {
            "relative_path": SOURCE_SAMPLING_CONTRACT.relative_to(ROOT).as_posix(),
            "sha256": file_sha256(SOURCE_SAMPLING_CONTRACT),
        },
    }


def _validate_stage(stage: Mapping[str, Any]) -> Mapping[str, Any]:
    if (
        stage.get("stage_protocol_version") != EXPECTED_STAGE_VERSION
        or stage.get("status") != "FINAL_RESEARCH_LOCKED"
    ):
        raise V7ReachabilityPreflightError("V4-S1 stage protocol is not locked")
    target = stage.get("target") or {}
    if (
        target.get("primary_scenario") != TARGET_SCENARIO
        or target.get("trade_route") != TARGET_ROUTE
        or target.get("other_v3_routes_suspended") is not True
    ):
        raise V7ReachabilityPreflightError("V4-S1 target route changed")
    lineage = stage.get("rule_lineage") or {}
    if (
        lineage.get("v1_v2_v3_are_read_only") is not True
        or lineage.get("policy_version") != EXPECTED_POLICY_VERSION
    ):
        raise V7ReachabilityPreflightError("V4-S1 frozen rule lineage changed")
    source = stage.get("source") or {}
    if (
        int(source.get("stocks", -1)) != EXPECTED_STOCKS
        or source.get("monitoring_start") != EXPECTED_MONITORING_START
        or source.get("monitoring_end") != EXPECTED_MONITORING_END
        or source.get("outcome_blind") is not True
        or source.get("identity_visible") is not False
        or source.get("future_or_performance_visible") is not False
    ):
        raise V7ReachabilityPreflightError("V4-S1 stage source scope changed")
    sample = stage.get("consistency_sample") or {}
    quotas = sample.get("quotas") or {}
    if (
        int(quotas.get("MATURE_SYMBOLIC_REACHABLE", -1)) < MINIMUM_UNIQUE_STOCKS
        or int(sample.get("minimum_distinct_months", -1)) < MINIMUM_DISTINCT_MONTHS
        or sample.get("one_case_per_anonymous_stock") is not True
    ):
        raise V7ReachabilityPreflightError("V4-S1 reachability capacity changed")
    return source


def _validate_manifest_header(
    manifest: Mapping[str, Any], source_path: Path, stage_source: Mapping[str, Any]
) -> int:
    expected = {
        "builder_version": EXPECTED_BUILDER_VERSION,
        "builder_status": EXPECTED_BUILDER_STATUS,
        "status": EXPECTED_MANIFEST_STATUS,
        "source_order_locked": False,
        "outcome_blind": True,
        "identity_visible": False,
        "future_data_visible": False,
        "performance_visible": False,
        "identity_isolated": True,
    }
    for field, value in expected.items():
        if manifest.get(field) != value:
            raise V7ReachabilityPreflightError(f"source manifest changed {field}")
    if manifest.get("builder_code_sha256") != EXPECTED_V7_SHA256:
        raise V7ReachabilityPreflightError("source manifest V7 builder hash changed")
    if int(manifest.get("stocks", -1)) != EXPECTED_STOCKS:
        raise V7ReachabilityPreflightError("source manifest stock coverage changed")
    if int(manifest.get("stock_days_scanned", -1)) != EXPECTED_STOCK_DAYS:
        raise V7ReachabilityPreflightError("source manifest stock-day coverage changed")
    if manifest.get("as_of_ceiling") != EXPECTED_MONITORING_END:
        raise V7ReachabilityPreflightError("source manifest as-of ceiling changed")
    review_points = int(manifest.get("review_points", -1))
    if review_points <= 0 or review_points != int(stage_source.get("review_points", -1)):
        raise V7ReachabilityPreflightError("source manifest review-point scope changed")
    if int(manifest.get("expected_candidate3_review_points", -1)) != review_points:
        raise V7ReachabilityPreflightError("source manifest expected review points changed")
    artifact_sha256 = file_sha256(source_path)
    if manifest.get("artifact_sha256") != artifact_sha256:
        raise V7ReachabilityPreflightError("source artifact differs from manifest")
    artifact = manifest.get("review_points_artifact") or {}
    try:
        artifact_path = Path(str(artifact.get("path") or "")).resolve()
    except OSError as exc:
        raise V7ReachabilityPreflightError("source artifact path is invalid") from exc
    if (
        artifact_path != source_path
        or artifact.get("sha256") != artifact_sha256
        or int(artifact.get("rows", -1)) != review_points
    ):
        raise V7ReachabilityPreflightError("source artifact descriptor changed")
    if manifest.get("sampling_contract_sha256") != EXPECTED_SAMPLING_CONTRACT_SHA256:
        raise V7ReachabilityPreflightError("source manifest sampling contract changed")
    return review_points


def _validate_packet(packet: Mapping[str, Any], *, source_ordinal: int) -> str:
    anonymous_stock_id = str(packet.get("anonymous_stock_id") or "")
    if not ANONYMOUS_STOCK_RE.fullmatch(anonymous_stock_id):
        raise V7ReachabilityPreflightError(
            f"invalid anonymous stock id at ordinal {source_ordinal}"
        )
    as_of = str(packet.get("as_of") or "")
    try:
        as_of_date = date.fromisoformat(as_of)
    except ValueError as exc:
        raise V7ReachabilityPreflightError(
            f"invalid packet as_of at ordinal {source_ordinal}"
        ) from exc
    if not (
        date.fromisoformat(EXPECTED_MONITORING_START)
        <= as_of_date
        <= date.fromisoformat(EXPECTED_MONITORING_END)
    ):
        raise V7ReachabilityPreflightError(
            f"packet date outside frozen stage at ordinal {source_ordinal}"
        )
    objective = packet.get("objective_facts") or {}
    if (
        objective.get("builder_version") != EXPECTED_BUILDER_VERSION
        or objective.get("builder_status") != EXPECTED_BUILDER_STATUS
        or objective.get("relation_comparison_contract_version")
        != v7_builder.EVIDENCE_CONTRACT_VERSION
    ):
        raise V7ReachabilityPreflightError(
            f"packet is not an exact V7 candidate at ordinal {source_ordinal}"
        )
    evidence = packet.get("evidence")
    if not isinstance(evidence, list):
        raise V7ReachabilityPreflightError(
            f"packet evidence is invalid at ordinal {source_ordinal}"
        )
    comparison_count = sum(
        isinstance(row, Mapping) and row.get("kind") == "CAUSAL_RELATION_COMPARISON"
        for row in evidence
    )
    if int(objective.get("relation_comparison_evidence_count", -1)) != comparison_count:
        raise V7ReachabilityPreflightError(
            f"V7 relation comparison count changed at ordinal {source_ordinal}"
        )
    if packet.get("question_manifest_sha256") != canonical_sha256(
        packet.get("question_manifest") or {}
    ):
        raise V7ReachabilityPreflightError(
            f"question manifest hash changed at ordinal {source_ordinal}"
        )
    evidence_sha256 = canonical_sha256(evidence)
    if (
        packet.get("evidence_catalog_sha256") != evidence_sha256
        or objective.get("ai_visible_evidence_sha256") != evidence_sha256
    ):
        raise V7ReachabilityPreflightError(
            f"evidence catalog hash changed at ordinal {source_ordinal}"
        )
    core = dict(packet)
    input_packet_sha256 = core.pop("input_packet_sha256", None)
    if input_packet_sha256 != canonical_sha256(core):
        raise V7ReachabilityPreflightError(
            f"input packet hash changed at ordinal {source_ordinal}"
        )
    return as_of


def symbolic_witness(
    packet: dict[str, Any], hypothesis: Mapping[str, Any]
) -> dict[str, Any]:
    """Reuse the frozen V4-S1 witness and apply this producer's watermark."""

    symbolic = frozen_preflight.symbolic_witness(packet, hypothesis)
    marker = symbolic.get("_preflight_symbolic_only") or {}
    if marker.get("classification") != "NOT_AI_OUTPUT_NOT_COURSE_GOLD":
        raise V7ReachabilityPreflightError("frozen symbolic witness watermark changed")
    marker["preflight_version"] = PREFLIGHT_VERSION
    symbolic["_preflight_symbolic_only"] = marker
    return symbolic


def _symbolic_validation_projection(symbolic: Mapping[str, Any]) -> dict[str, Any]:
    marker = symbolic.get("_preflight_symbolic_only")
    if marker != {
        "preflight_version": PREFLIGHT_VERSION,
        "classification": "NOT_AI_OUTPUT_NOT_COURSE_GOLD",
    }:
        raise V7ReachabilityPreflightError("symbolic witness watermark is missing")
    return {
        key: child
        for key, child in symbolic.items()
        if key != "_preflight_symbolic_only"
    }


def preflight(
    *,
    source_path: Path,
    source_manifest_path: Path,
    policy_path: Path,
    stage_path: Path,
) -> dict[str, Any]:
    source_path = Path(source_path).resolve()
    manifest_path = Path(source_manifest_path).resolve()
    policy_path = Path(policy_path).resolve()
    stage_path = Path(stage_path).resolve()
    manifest = _read_json(manifest_path)
    stage = _read_json(stage_path)
    provenance = _validate_frozen_components(policy_path, stage_path)
    stage_source = _validate_stage(stage)
    expected_rows = _validate_manifest_header(manifest, source_path, stage_source)
    source_contract = load_sampling_contract(SOURCE_SAMPLING_CONTRACT)

    counts: Counter[str] = Counter()
    blockers: Counter[str] = Counter()
    candidates: list[dict[str, Any]] = []
    seen_review_ids: set[str] = set()
    seen_stocks: set[str] = set()
    seen_as_of: list[str] = []
    source_index: list[dict[str, Any]] = []
    review_ids: list[str] = []
    packet_hashes: list[str] = []

    for source_ordinal, source in enumerate(_jsonl(source_path)):
        counts["source_rows"] += 1
        forbidden = _contains_forbidden_key(source)
        if forbidden:
            raise V7ReachabilityPreflightError(
                f"identity/outcome/future/witness key in source ordinal "
                f"{source_ordinal}: {forbidden}"
            )
        try:
            validate_source_record(source, source_contract)
        except Exception as exc:
            raise V7ReachabilityPreflightError(
                f"source contract invalid at ordinal {source_ordinal}: {exc}"
            ) from exc
        if int(source.get("source_ordinal", -1)) != source_ordinal:
            raise V7ReachabilityPreflightError("source ordinal order changed")
        review_id = str(source.get("review_id") or "")
        if not review_id or review_id in seen_review_ids:
            raise V7ReachabilityPreflightError(
                "source review identity is missing or duplicate"
            )
        seen_review_ids.add(review_id)
        packet = source.get("packet") or {}
        if (
            not isinstance(packet, dict)
            or packet.get("review_id") != review_id
            or packet.get("anonymous_stock_id") != source.get("anonymous_stock_id")
            or canonical_sha256(packet) != source.get("packet_sha256")
        ):
            raise V7ReachabilityPreflightError(
                "source row identity or packet hash changed"
            )
        as_of = _validate_packet(packet, source_ordinal=source_ordinal)
        anonymous_stock_id = str(source["anonymous_stock_id"])
        seen_stocks.add(anonymous_stock_id)
        seen_as_of.append(as_of)
        source_index.append({key: value for key, value in source.items() if key != "packet"})
        review_ids.append(review_id)
        packet_hashes.append(str(source["packet_sha256"]))

        if TARGET_SAMPLING_STRATUM not in (source.get("eligible_sampling_strata") or []):
            counts["rows_outside_v2_core"] += 1
            continue
        counts["v2_core_rows"] += 1
        hypotheses = (
            ((packet.get("objective_facts") or {}).get("scenario_hypotheses") or {}).get(
                TARGET_SCENARIO
            )
            or []
        )
        if not hypotheses:
            counts["v2_core_rows_without_mature_hypothesis"] += 1
            continue
        counts["rows_with_mature_v2_core_hypothesis"] += 1
        reachable: tuple[Mapping[str, Any], dict[str, Any], dict[str, Any]] | None = None
        for hypothesis in hypotheses:
            counts["mature_v2_core_hypotheses_tested"] += 1
            packet_before = canonical_sha256(packet)
            symbolic = symbolic_witness(packet, hypothesis)
            if canonical_sha256(packet) != packet_before:
                raise V7ReachabilityPreflightError("symbolic witness mutated packet")
            semantic = _symbolic_validation_projection(symbolic)
            errors = validate_atomic(packet, semantic)
            if errors:
                counts["invalid_symbolic_witnesses"] += 1
                blockers.update("VALIDATION:" + str(error) for error in errors)
                continue
            counts["valid_symbolic_witnesses"] += 1
            decision = reduce_atomic_v4_s1(packet, semantic)
            if stage_permission_v4_s1(decision) == "TRADE":
                reachable = (hypothesis, symbolic, decision)
                break
            blockers.update(
                "DECISION:" + str(code)
                for code in (decision.get("reason_codes") or ["NO_REASON_CODE"])
            )
        if reachable is None:
            continue
        hypothesis, symbolic, decision = reachable
        counts["reachable_rows"] += 1
        candidates.append(
            {
                "source_ordinal": source_ordinal,
                "review_id": review_id,
                "anonymous_stock_id": anonymous_stock_id,
                "as_of": as_of,
                "packet_sha256": str(source["packet_sha256"]),
                "hypothesis_id": hypothesis["hypothesis_id"],
                "symbolic_witness_sha256": canonical_sha256(symbolic),
                "action_signature_sha256": canonical_sha256(
                    decision["action_signature"]
                ),
                "sampling_stratum": TARGET_SAMPLING_STRATUM,
                "classification": "SYMBOLIC_REACHABLE_NOT_SEMANTIC_GOLD",
            }
        )

    if counts["source_rows"] != expected_rows:
        raise V7ReachabilityPreflightError("source row coverage changed")
    if len(seen_stocks) != EXPECTED_STOCKS:
        raise V7ReachabilityPreflightError("source stock coverage changed")
    if (
        not seen_as_of
        or min(seen_as_of) != EXPECTED_MONITORING_START
        or max(seen_as_of) != EXPECTED_MONITORING_END
    ):
        raise V7ReachabilityPreflightError("source date coverage changed")
    coverage = manifest.get("source_ordinal_coverage") or {}
    expected_coverage = {
        "expected": expected_rows,
        "covered": expected_rows,
        "missing": 0,
        "overlap": 0,
        "unexpected": 0,
        "first": 0,
        "last": expected_rows - 1,
    }
    if coverage != expected_coverage:
        raise V7ReachabilityPreflightError("source ordinal manifest coverage changed")
    if manifest.get("source_ordinal_index_sha256") != canonical_sha256(source_index):
        raise V7ReachabilityPreflightError("source ordinal index hash changed")
    if manifest.get("review_ids_sha256") != canonical_sha256(review_ids):
        raise V7ReachabilityPreflightError("source review id hash changed")
    if manifest.get("packet_hashes_sha256") != canonical_sha256(packet_hashes):
        raise V7ReachabilityPreflightError("source packet hash index changed")

    unique_candidate_stocks = len(
        {row["anonymous_stock_id"] for row in candidates}
    )
    distinct_candidate_months = len({str(row["as_of"])[:7] for row in candidates})
    passed = (
        unique_candidate_stocks >= MINIMUM_UNIQUE_STOCKS
        and distinct_candidate_months >= MINIMUM_DISTINCT_MONTHS
    )
    status = (
        "PASS_SYMBOLIC_TARGET_ROUTE_REACHABLE"
        if passed
        else "FAIL_INSUFFICIENT_SYMBOLIC_TARGET_ROUTE_CAPACITY"
    )
    core = {
        "preflight_version": PREFLIGHT_VERSION,
        "status": status,
        "source_acceptance": EXPECTED_MANIFEST_STATUS,
        "formal_acceptance_claimed": False,
        "target": {
            "scenario": TARGET_SCENARIO,
            "route": TARGET_ROUTE,
            "sampling_stratum": TARGET_SAMPLING_STRATUM,
        },
        "classification": "SOFTWARE_REACHABILITY_ONLY_NOT_AI_LABEL_NOT_COURSE_GOLD",
        "outcome_blind": True,
        "identity_visible": False,
        "future_or_performance_visible": False,
        "ai_output_read": False,
        "symbolic_witness_inside_ai_packet": False,
        "symbolic_witness_has_schema_poison_pill": True,
        "source": {
            "path": str(source_path),
            "sha256": file_sha256(source_path),
            "manifest_path": str(manifest_path),
            "manifest_sha256": file_sha256(manifest_path),
            "builder_version": manifest["builder_version"],
            "builder_status": manifest["builder_status"],
            "manifest_status": manifest["status"],
            "source_order_locked": manifest["source_order_locked"],
        },
        "policy": {
            "path": str(policy_path),
            "version": POLICY_VERSION,
            "sha256": file_sha256(policy_path),
        },
        "stage_protocol": {
            "path": str(stage_path),
            "version": stage["stage_protocol_version"],
            "sha256": file_sha256(stage_path),
        },
        "producer_provenance": provenance,
        "source_scope": {
            "stocks": EXPECTED_STOCKS,
            "stock_days": EXPECTED_STOCK_DAYS,
            "review_points": expected_rows,
            "monitoring_start": EXPECTED_MONITORING_START,
            "monitoring_end": EXPECTED_MONITORING_END,
        },
        "requirements": {
            "minimum_unique_stocks": MINIMUM_UNIQUE_STOCKS,
            "minimum_distinct_months": MINIMUM_DISTINCT_MONTHS,
        },
        "capacity": {
            "reachable_rows": counts["reachable_rows"],
            "unique_anonymous_stocks": unique_candidate_stocks,
            "distinct_months": distinct_candidate_months,
        },
        "counts": dict(sorted(counts.items())),
        "blockers": dict(blockers.most_common()),
        "candidates": candidates,
        "candidates_sha256": canonical_sha256(candidates),
        "limitations": [
            "This accepts only a V7 candidate source, not a formal accepted source.",
            "A symbolic witness proves only that the frozen software gates can be satisfied.",
            "It is not an AI answer, course-gold label, trade list, or performance claim.",
            "AI-visible packets never contain the symbolic witness or its classification.",
        ],
    }
    return {**core, "preflight_sha256": canonical_sha256(core)}


def markdown(report: Mapping[str, Any]) -> str:
    capacity = report["capacity"]
    return "\n".join(
        [
            "# V4-S1 V7 結果盲化可達性預檢",
            "",
            f"- 狀態：`{report['status']}`",
            "- V7來源仍是 candidate，不代表 formal acceptance。",
            "- 只測 MATURE_TREND_PULLBACK / V2_CORE 軟體路徑。",
            "- 未讀取股票身分、AI輸出、未來行情或績效。",
            "- symbolic witness 未寫入任何 AI packet。",
            "",
            "## 容量",
            "",
            f"- 可達候選日：{capacity['reachable_rows']:,}",
            f"- 不同匿名股票：{capacity['unique_anonymous_stocks']:,}",
            f"- 涵蓋月份：{capacity['distinct_months']:,}",
            "",
            f"Preflight SHA-256: `{report['preflight_sha256']}`",
            "",
        ]
    )


def publish_report(report: Mapping[str, Any], output_dir: Path) -> dict[str, str]:
    output_dir = Path(output_dir).resolve()
    payloads = {
        output_dir / "preflight.json": canonical_json_bytes(dict(report)) + b"\n",
        output_dir / "preflight.md": markdown(report).encode("utf-8"),
    }
    for path, payload in payloads.items():
        try:
            _publish_immutable(path, payload)
        except Exception as exc:
            raise V7ReachabilityPreflightError(
                f"refusing to overwrite changed preflight: {path}"
            ) from exc
    return {path.name: file_sha256(path) for path in payloads}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--stage", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    report = preflight(
        source_path=args.source,
        source_manifest_path=args.source_manifest,
        policy_path=args.policy,
        stage_path=args.stage,
    )
    output_dir = args.output_dir.resolve()
    publish_report(report, output_dir)
    print(
        json.dumps(
            {
                "status": report["status"],
                "capacity": report["capacity"],
                "preflight_sha256": report["preflight_sha256"],
                "output_dir": str(output_dir),
            },
            ensure_ascii=False,
        )
    )
    return 0 if report["status"].startswith("PASS") else 2


if __name__ == "__main__":
    raise SystemExit(main())
