"""Final infrastructure for blinded historical course-gold adjudication.

This module deliberately does **not** create gold labels.  It can only:

* select anonymous cases from a complete, outcome-blind V2 review-point
  universe using pre-locked objective-only strata and a fixed seed;
* remove review points/stocks already exposed to V1 AI, V2 AI, or research
  calibration;
* emit identity-, prior-AI-, and outcome-isolated annotation packets plus
  empty human-rubric templates with immutable hashes; and
* fail closed when a caller tries to score an incomplete or unfrozen rubric.

Historical course gold tests semantic correctness only.  It is not a future
performance holdout and must never be presented as one.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


COURSE_GOLD_VERSION = "hybrid-v3-course-gold-v2"
COURSE_GOLD_STATUS = "FINAL"
SELECTION_CONFIG_VERSION = "hybrid-v3-course-gold-selection-v2"
EXCLUSION_MANIFEST_VERSION = "hybrid-v3-course-gold-exclusions-v2"
ANNOTATION_PACKET_VERSION = "hybrid-v3-course-gold-annotation-packet-v2"
ANNOTATION_MANIFEST_VERSION = "hybrid-v3-course-gold-annotation-manifest-v2"
RUBRIC_VERSION = "hybrid-v3-course-gold-rubric-v2"
RUBRIC_SCHEMA_VERSION = "hybrid-v3-course-gold-rubric-schema-v2"
BUNDLE_MANIFEST_VERSION = "hybrid-v3-course-gold-bundle-v2"

EXPECTED_STOCKS = 1029
EXPECTED_STOCK_DAYS = 151804
REQUIRED_EXCLUSION_SCOPES = (
    "V1_AI_REVIEWED",
    "V2_AI_REVIEWED",
    "RESEARCH_CALIBRATION",
)
REQUIRED_FORMAL_UNIVERSE_ATTESTATIONS = {
    "outcome_blind": True,
    "identity_visible": False,
    "performance_visible": False,
    "future_data_visible": False,
    "source_order_locked": True,
}
FORBIDDEN_PACKET_KEYS = {
    "code",
    "name",
    "symbol",
    "ticker",
    "market",
    "company",
    "company_name",
    "private_code",
    "identity_map",
    "ai_output",
    "model_output",
    "old_ai_output",
    "prior_ai_output",
    "prior_decision",
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
    "outcome",
    "realized_return",
    "unrealized_return",
    "max_favorable_excursion",
    "max_adverse_excursion",
}
ALLOWED_PACKET_TOP_LEVEL = {
    "review_id",
    "anonymous_stock_id",
    "as_of",
    "question_manifest",
    "evidence",
    "objective_facts",
    "question_manifest_sha256",
    "evidence_catalog_sha256",
    "input_packet_sha256",
}
ALLOWED_SOURCE_ROW_TOP_LEVEL = {
    "source_ordinal",
    "review_id",
    "anonymous_stock_id",
    "sampling_stratum",
    "packet_sha256",
    "packet",
}


class CourseGoldError(ValueError):
    """A source, selection, rubric, or evaluation violates the blind contract."""


class FormalUniverseError(CourseGoldError):
    """The supplied source is not the complete locked V2 review-point universe."""


class GoldEvaluationError(CourseGoldError):
    """Gold scoring cannot proceed safely or completely."""


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


def canonical_jsonl_bytes(rows: Iterable[Mapping[str, Any]]) -> bytes:
    return b"".join(canonical_json_bytes(dict(row)) + b"\n" for row in rows)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _require_sha256(value: Any, label: str) -> str:
    normalized = str(value or "").lower()
    if len(normalized) != 64 or any(char not in "0123456789abcdef" for char in normalized):
        raise CourseGoldError(f"{label} must be a lowercase SHA-256")
    return normalized


def _forbidden_key(key: Any) -> bool:
    normalized = str(key).lower()
    return (
        normalized in FORBIDDEN_PACKET_KEYS
        or normalized.startswith("future_")
        or normalized.startswith("forward_")
        or normalized.startswith("old_ai_")
        or normalized.startswith("prior_ai_")
    )


def assert_no_identity_ai_or_outcome(value: Any, *, path: str = "$") -> None:
    """Reject rather than silently ignore contaminated fields."""

    if isinstance(value, Mapping):
        for key, nested in value.items():
            if _forbidden_key(key):
                raise CourseGoldError(f"forbidden identity/AI/outcome key at {path}: {key}")
            assert_no_identity_ai_or_outcome(nested, path=f"{path}/{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            assert_no_identity_ai_or_outcome(nested, path=f"{path}/{index}")


def _assert_causal_dates(value: Any, *, as_of: str, path: str = "$") -> None:
    date_keys = {
        "date",
        "start",
        "end",
        "low_date",
        "high_date",
        "source_date",
        "confirmation_date",
        "available_on",
        "confirmed_on",
        "breached_on",
        "origin_pivot_source_date",
        "parent_available_on",
        "correction_available_on",
        "current_available_on",
        "causal_cutoff_as_of",
    }
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if str(key).lower() in date_keys and nested is not None and str(nested) > as_of:
                raise CourseGoldError(f"future date at {path}/{key}: {nested} > {as_of}")
            _assert_causal_dates(nested, as_of=as_of, path=f"{path}/{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _assert_causal_dates(nested, as_of=as_of, path=f"{path}/{index}")


def validate_source_packet(packet: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the one official anonymous V2 packet contract."""

    packet = dict(packet)
    extra = set(packet) - ALLOWED_PACKET_TOP_LEVEL
    missing = ALLOWED_PACKET_TOP_LEVEL - set(packet)
    if extra or missing:
        raise CourseGoldError(
            f"V2 packet shape mismatch; missing={sorted(missing)}, extra={sorted(extra)}"
        )
    assert_no_identity_ai_or_outcome(packet)
    as_of = str(packet.get("as_of") or "")
    if len(as_of) != 10 or as_of[4:5] != "-" or as_of[7:8] != "-":
        raise CourseGoldError("packet as_of must be an ISO calendar date")
    objective = packet.get("objective_facts")
    if not isinstance(objective, Mapping) or objective.get("causal_cutoff_as_of") != as_of:
        raise CourseGoldError("packet objective causal cutoff differs from as_of")
    _assert_causal_dates(packet, as_of=as_of)
    if packet["question_manifest_sha256"] != canonical_sha256(packet["question_manifest"]):
        raise CourseGoldError("packet question manifest hash mismatch")
    if packet["evidence_catalog_sha256"] != canonical_sha256(packet["evidence"]):
        raise CourseGoldError("packet evidence hash mismatch")
    if objective.get("ai_visible_evidence_sha256") != canonical_sha256(packet["evidence"]):
        raise CourseGoldError("packet objective evidence hash mismatch")
    core = dict(packet)
    supplied = core.pop("input_packet_sha256")
    if supplied != canonical_sha256(core):
        raise CourseGoldError("packet input hash mismatch")
    return packet


def validate_formal_universe_manifest(
    manifest: Mapping[str, Any], *, manifest_path: Path | None = None
) -> dict[str, Any]:
    """Fail closed unless this is the complete locked V2 review-point universe."""

    if manifest.get("manifest_version") != "hybrid-v3-review-points-v2":
        raise FormalUniverseError("wrong V2 review-point manifest version")
    if manifest.get("status") != "LOCKED_OUTCOME_BLIND":
        raise FormalUniverseError("review-point universe is not LOCKED_OUTCOME_BLIND")
    if manifest.get("universe_scope") != "FULL_V2_REVIEW_POINT_UNIVERSE":
        raise FormalUniverseError("substitute/fixture universe cannot be used as a formal sample")
    for key, expected in REQUIRED_FORMAL_UNIVERSE_ATTESTATIONS.items():
        if manifest.get(key) is not expected:
            raise FormalUniverseError(f"review-point universe violates {key}={expected!r}")
    if manifest.get("sampling_strata_basis") != "OBJECTIVE_ONLY_AS_OF":
        raise FormalUniverseError("sampling strata are not attested objective-only as-of")
    if manifest.get("stocks") != EXPECTED_STOCKS or manifest.get("stock_days_scanned") != EXPECTED_STOCK_DAYS:
        raise FormalUniverseError("formal universe must cover exactly 1,029 stocks / 151,804 stock-days")
    review_points = int(manifest.get("review_points") or 0)
    if review_points <= 0:
        raise FormalUniverseError("formal universe has no V2 review points")
    artifact = manifest.get("review_points_artifact")
    if not isinstance(artifact, Mapping):
        raise FormalUniverseError("formal universe does not declare its review-point artifact")
    if int(artifact.get("rows") or 0) != review_points:
        raise FormalUniverseError("review-point artifact row count differs from universe")
    result = dict(manifest)
    if manifest_path is not None:
        path_value = artifact.get("path")
        if not path_value:
            raise FormalUniverseError("review-point artifact path is missing")
        artifact_path = Path(str(path_value))
        if not artifact_path.is_absolute():
            artifact_path = manifest_path.resolve().parent / artifact_path
        artifact_path = artifact_path.resolve()
        if not artifact_path.is_file():
            raise FormalUniverseError(f"review-point artifact is missing: {artifact_path}")
        if file_sha256(artifact_path) != _require_sha256(artifact.get("sha256"), "artifact sha256"):
            raise FormalUniverseError("review-point artifact hash mismatch")
        result["resolved_artifact_path"] = str(artifact_path)
    return result


def validate_selection_config(config: Mapping[str, Any], *, formal: bool) -> dict[str, Any]:
    if config.get("config_version") != SELECTION_CONFIG_VERSION:
        raise CourseGoldError("wrong course-gold selection config version")
    if config.get("status") != "LOCKED_BEFORE_SAMPLE":
        raise CourseGoldError("selection config must be LOCKED_BEFORE_SAMPLE")
    seed = str(config.get("seed") or "")
    quotas_raw = config.get("objective_stratum_quotas")
    if not seed or not isinstance(quotas_raw, Mapping) or not quotas_raw:
        raise CourseGoldError("fixed seed and objective_stratum_quotas are required")
    quotas = {str(key): int(value) for key, value in quotas_raw.items()}
    if any(value <= 0 for value in quotas.values()):
        raise CourseGoldError("all objective stratum quotas must be positive")
    minimum = int(config.get("minimum_cases") or 0)
    if sum(quotas.values()) != minimum:
        raise CourseGoldError("minimum_cases differs from objective stratum quotas")
    if config.get("strata_basis") != "OBJECTIVE_ONLY_AS_OF":
        raise CourseGoldError("selection config strata_basis must be OBJECTIVE_ONLY_AS_OF")
    if config.get("future_performance_used") is not False:
        raise CourseGoldError("selection config must explicitly exclude future performance")
    if config.get("old_ai_output_used") is not False:
        raise CourseGoldError("selection config must explicitly exclude old AI output")
    if config.get("real_identity_used") is not False:
        raise CourseGoldError("selection config must explicitly exclude real identity")
    if formal and minimum < 20:
        raise CourseGoldError("formal blinded historical course gold requires at least 20 cases")
    if formal and len(quotas) < 4:
        raise CourseGoldError(
            "formal blinded historical course gold requires at least four objective strata"
        )
    result = dict(config)
    result["normalized_quotas"] = quotas
    return result


def validate_exclusion_manifest(
    manifest: Mapping[str, Any], *, scope: str, universe_manifest_sha256: str
) -> list[dict[str, str]]:
    if manifest.get("manifest_version") != EXCLUSION_MANIFEST_VERSION:
        raise CourseGoldError(f"wrong exclusion manifest version for {scope}")
    if manifest.get("status") != "LOCKED_COMPLETE_BEFORE_SAMPLE":
        raise CourseGoldError(f"{scope} exclusions are not locked complete")
    if manifest.get("scope") != scope:
        raise CourseGoldError(f"wrong exclusion scope: expected {scope}")
    if manifest.get("universe_manifest_sha256") != universe_manifest_sha256:
        raise CourseGoldError(f"{scope} exclusions are linked to a different universe")
    for key in ("contains_ai_outputs", "contains_real_identity", "contains_future_or_performance"):
        if manifest.get(key) is not False:
            raise CourseGoldError(f"{scope} must attest {key}=false")
    rows = manifest.get("rows")
    if not isinstance(rows, list):
        raise CourseGoldError(f"{scope} exclusion rows are missing")
    if manifest.get("rows_sha256") != canonical_sha256(rows):
        raise CourseGoldError(f"{scope} exclusion rows hash mismatch")
    normalized: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != {"review_id", "anonymous_stock_id"}:
            raise CourseGoldError(f"{scope} rows must contain only anonymous review/stock IDs")
        review_id = str(row.get("review_id") or "")
        stock_id = str(row.get("anonymous_stock_id") or "")
        if not review_id and not stock_id:
            raise CourseGoldError(f"{scope} exclusion rows require an anonymous review or stock ID")
        if scope in {"V1_AI_REVIEWED", "V2_AI_REVIEWED"} and (not review_id or not stock_id):
            raise CourseGoldError(f"{scope} AI-review rows require both anonymous IDs")
        normalized.append({"review_id": review_id, "anonymous_stock_id": stock_id})
    return normalized


def _normalize_source_row(row: Mapping[str, Any]) -> dict[str, Any]:
    extra = set(row) - ALLOWED_SOURCE_ROW_TOP_LEVEL
    missing = ALLOWED_SOURCE_ROW_TOP_LEVEL - set(row)
    if extra or missing:
        raise CourseGoldError(
            f"review-point row shape mismatch; missing={sorted(missing)}, extra={sorted(extra)}"
        )
    try:
        ordinal = int(row["source_ordinal"])
    except (TypeError, ValueError) as exc:
        raise CourseGoldError("source_ordinal must be a nonnegative integer") from exc
    if ordinal < 0:
        raise CourseGoldError("source_ordinal must be a nonnegative integer")
    review_id = str(row.get("review_id") or "")
    stock_id = str(row.get("anonymous_stock_id") or "")
    stratum = str(row.get("sampling_stratum") or "")
    if not review_id or not stock_id or not stratum:
        raise CourseGoldError("review-point row lacks anonymous IDs or objective stratum")
    packet = validate_source_packet(row["packet"])
    if packet["review_id"] != review_id or packet["anonymous_stock_id"] != stock_id:
        raise CourseGoldError("source row anonymous IDs differ from embedded packet")
    packet_sha = canonical_sha256(packet)
    if _require_sha256(row["packet_sha256"], "packet_sha256") != packet_sha:
        raise CourseGoldError("source row packet hash mismatch")
    return {
        "source_ordinal": ordinal,
        "review_id": review_id,
        "anonymous_stock_id": stock_id,
        "sampling_stratum": stratum,
        "packet_sha256": packet_sha,
        "packet": packet,
    }


def _selection_rank(seed: str, stratum: str, row: Mapping[str, Any]) -> str:
    # Deliberately excludes source ordinal, stock identity, market outcome, and
    # any model output.  The anonymous review ID and immutable packet content
    # are the complete ranking material.
    material = "|".join((seed, stratum, str(row["review_id"]), str(row["packet_sha256"])))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _gold_case_id(seed: str, review_id: str, packet_sha256: str) -> str:
    digest = hashlib.sha256(f"{seed}|{review_id}|{packet_sha256}|COURSE_GOLD".encode()).hexdigest()
    return "G-" + digest[:24]


def build_selection_plan(
    records: Sequence[Mapping[str, Any]],
    *,
    selection_config: Mapping[str, Any],
    exclusion_manifests: Mapping[str, Mapping[str, Any]],
    universe_manifest_sha256: str,
    protocol_sha256: str,
    formal: bool = False,
    formal_universe_manifest: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Select a deterministic, hard-excluded anonymous sample."""

    if formal:
        if formal_universe_manifest is None:
            raise FormalUniverseError("formal selection requires the full V2 universe manifest")
        validate_formal_universe_manifest(formal_universe_manifest)
    config = validate_selection_config(selection_config, formal=formal)
    universe_sha = _require_sha256(universe_manifest_sha256, "universe_manifest_sha256")
    protocol_sha = _require_sha256(protocol_sha256, "protocol_sha256")
    if set(exclusion_manifests) != set(REQUIRED_EXCLUSION_SCOPES):
        raise CourseGoldError("all V1/V2/research-calibration exclusion manifests are required")
    excluded_reviews: set[str] = set()
    excluded_stocks: set[str] = set()
    exclusion_hashes: dict[str, str] = {}
    for scope in REQUIRED_EXCLUSION_SCOPES:
        manifest = exclusion_manifests[scope]
        rows = validate_exclusion_manifest(manifest, scope=scope, universe_manifest_sha256=universe_sha)
        excluded_reviews.update(row["review_id"] for row in rows if row["review_id"])
        excluded_stocks.update(row["anonymous_stock_id"] for row in rows if row["anonymous_stock_id"])
        exclusion_hashes[scope] = canonical_sha256(manifest)

    normalized = [_normalize_source_row(row) for row in records]
    review_ids = [row["review_id"] for row in normalized]
    ordinals = [row["source_ordinal"] for row in normalized]
    if len(review_ids) != len(set(review_ids)):
        raise CourseGoldError("duplicate review_id in universe")
    if len(ordinals) != len(set(ordinals)):
        raise CourseGoldError("duplicate source_ordinal in universe")
    source_identity = [
        {
            "source_ordinal": row["source_ordinal"],
            "review_id": row["review_id"],
            "anonymous_stock_id": row["anonymous_stock_id"],
            "sampling_stratum": row["sampling_stratum"],
            "packet_sha256": row["packet_sha256"],
        }
        for row in sorted(normalized, key=lambda item: item["source_ordinal"])
    ]
    source_rows_sha256 = canonical_sha256(source_identity)
    seed = str(config["seed"])
    selected: list[dict[str, Any]] = []
    for stratum, quota in config["normalized_quotas"].items():
        pool = [
            row
            for row in normalized
            if row["sampling_stratum"] == stratum
            and row["review_id"] not in excluded_reviews
            and row["anonymous_stock_id"] not in excluded_stocks
        ]
        pool.sort(key=lambda row: _selection_rank(seed, stratum, row))
        if len(pool) < quota:
            raise CourseGoldError(
                f"objective stratum {stratum} has {len(pool)} clean cases; {quota} required"
            )
        selected.extend(pool[:quota])
    selected.sort(key=lambda row: (row["source_ordinal"], row["review_id"]))
    cases = [
        {
            "gold_case_id": _gold_case_id(seed, row["review_id"], row["packet_sha256"]),
            "source_ordinal": row["source_ordinal"],
            "review_id": row["review_id"],
            "anonymous_stock_id": row["anonymous_stock_id"],
            "sampling_stratum": row["sampling_stratum"],
            "packet_sha256": row["packet_sha256"],
        }
        for row in selected
    ]
    plan_core = {
        "course_gold_version": COURSE_GOLD_VERSION,
        "course_gold_status": COURSE_GOLD_STATUS,
        "classification": "BLINDED_HISTORICAL_COURSE_GOLD_NOT_PERFORMANCE_HOLDOUT",
        "visibility": "EVALUATOR_ONLY_ANONYMOUS_LINKAGE_NOT_ANNOTATOR_INPUT",
        "formal_source": bool(formal),
        "seed": seed,
        "objective_stratum_quotas": config["normalized_quotas"],
        "case_count": len(cases),
        "stratum_counts": dict(sorted(Counter(row["sampling_stratum"] for row in cases).items())),
        "universe_manifest_sha256": universe_sha,
        "protocol_sha256": protocol_sha,
        "universe_rows_sha256": source_rows_sha256,
        "selection_config_sha256": canonical_sha256(selection_config),
        "exclusion_manifest_sha256": exclusion_hashes,
        "hard_excluded_review_count": len(excluded_reviews),
        "hard_excluded_stock_count": len(excluded_stocks),
        "cases": cases,
    }
    plan = dict(plan_core)
    plan["selection_plan_sha256"] = canonical_sha256(plan_core)
    return plan


def validate_selection_plan(
    selection_plan: Mapping[str, Any], *, protocol: Mapping[str, Any], require_formal: bool = False
) -> dict[str, Any]:
    plan = dict(selection_plan)
    supplied = _require_sha256(plan.pop("selection_plan_sha256", None), "selection_plan_sha256")
    if supplied != canonical_sha256(plan):
        raise GoldEvaluationError("selection plan immutable hash mismatch")
    if plan.get("course_gold_version") != COURSE_GOLD_VERSION:
        raise GoldEvaluationError("selection plan course-gold version mismatch")
    if plan.get("classification") != "BLINDED_HISTORICAL_COURSE_GOLD_NOT_PERFORMANCE_HOLDOUT":
        raise GoldEvaluationError("selection plan classification mismatch")
    if require_formal and plan.get("formal_source") is not True:
        raise GoldEvaluationError("formal evaluation requires a selection from the full formal universe")
    protocol_sha = canonical_sha256(protocol)
    if plan.get("protocol_sha256") != protocol_sha:
        raise GoldEvaluationError("selection plan is linked to a different protocol")
    cases = plan.get("cases")
    if not isinstance(cases, list) or int(plan.get("case_count") or 0) != len(cases):
        raise GoldEvaluationError("selection plan case count is incomplete")
    observed = dict(sorted(Counter(str(row.get("sampling_stratum") or "") for row in cases).items()))
    if "" in observed or observed != dict(plan.get("stratum_counts") or {}):
        raise GoldEvaluationError("selection plan objective strata differ from selected cases")
    contract = ((protocol.get("correctness_gate") or {}).get("blinded_course_gold_holdout") or {})
    if len(cases) < int(contract.get("minimum_cases") or 20):
        raise GoldEvaluationError("selection plan has fewer than the protocol minimum cases")
    if len(observed) < int(contract.get("minimum_distinct_objective_strata") or 4):
        raise GoldEvaluationError("selection plan has fewer than the protocol minimum objective strata")
    return {**plan, "selection_plan_sha256": supplied}


def _selected_source_rows(
    records: Sequence[Mapping[str, Any]], selection_plan: Mapping[str, Any]
) -> list[tuple[dict[str, Any], Mapping[str, Any]]]:
    normalized = {_normalize_source_row(row)["review_id"]: _normalize_source_row(row) for row in records}
    output: list[tuple[dict[str, Any], Mapping[str, Any]]] = []
    for selected in selection_plan.get("cases") or []:
        review_id = str(selected.get("review_id") or "")
        source = normalized.get(review_id)
        if source is None:
            raise CourseGoldError(f"selected review point disappeared from universe: {review_id}")
        for key in ("source_ordinal", "anonymous_stock_id", "sampling_stratum", "packet_sha256"):
            if selected.get(key) != source.get(key):
                raise CourseGoldError(f"selected review point changed {key}: {review_id}")
        output.append((source, selected))
    return output


def build_annotation_packet(source: Mapping[str, Any], selected: Mapping[str, Any]) -> dict[str, Any]:
    """Re-key a source packet so the human annotation surface has no join identity."""

    packet = validate_source_packet(source["packet"])
    result = {
        "annotation_packet_version": ANNOTATION_PACKET_VERSION,
        "gold_case_id": str(selected["gold_case_id"]),
        "as_of": packet["as_of"],
        "question_manifest": packet["question_manifest"],
        "evidence": packet["evidence"],
        "objective_facts": packet["objective_facts"],
        "source_question_manifest_sha256": packet["question_manifest_sha256"],
        "source_evidence_catalog_sha256": packet["evidence_catalog_sha256"],
        "source_input_packet_sha256": packet["input_packet_sha256"],
        "isolation": {
            "real_identity_visible": False,
            "old_ai_visible": False,
            "future_or_performance_visible": False,
            "sampling_stratum_visible": False,
        },
    }
    # review_id, anonymous_stock_id, source ordinal, and sampling stratum are
    # intentionally absent from the human-visible packet.
    return result


def rubric_schema() -> dict[str, Any]:
    """Return the schema humans must fill; it contains no expected labels."""

    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": RUBRIC_SCHEMA_VERSION,
        "title": "Blinded historical course-gold rubric",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "rubric_version",
            "status",
            "gold_case_id",
            "annotation_packet_sha256",
            "classification",
            "reviewer_attestations",
            "course_citations",
            "assertions",
            "adjudication_complete",
            "frozen_at_utc",
            "rubric_sha256",
        ],
        "properties": {
            "rubric_version": {"const": RUBRIC_VERSION},
            "status": {"const": "HUMAN_REVIEWED_FROZEN"},
            "gold_case_id": {"type": "string", "pattern": "^G-[0-9a-f]{24}$"},
            "annotation_packet_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "classification": {
                "enum": ["POSITIVE_OR_CONSTRUCTIVE", "NEGATIVE_OR_AMBIGUOUS"]
            },
            "reviewer_attestations": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "reviewer_ref",
                        "reviewed_at_utc",
                        "course_and_as_of_only",
                        "old_ai_and_outcomes_not_viewed",
                    ],
                    "properties": {
                        "reviewer_ref": {"type": "string", "minLength": 1},
                        "reviewed_at_utc": {"type": "string", "minLength": 1},
                        "course_and_as_of_only": {"const": True},
                        "old_ai_and_outcomes_not_viewed": {"const": True},
                    },
                },
            },
            "course_citations": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["citation_id", "source_path", "section", "proposition"],
                    "properties": {
                        "citation_id": {"type": "string", "minLength": 1},
                        "source_path": {"type": "string", "minLength": 1},
                        "section": {"type": "string", "minLength": 1},
                        "proposition": {"type": "string", "minLength": 1},
                    },
                },
            },
            "assertions": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "assertion_id",
                        "target_artifact",
                        "json_pointer",
                        "course_citation_ids",
                        "as_of_evidence_refs",
                    ],
                    "properties": {
                        "assertion_id": {"type": "string", "minLength": 1},
                        "target_artifact": {
                            "enum": ["SEMANTIC_OUTPUT", "POLICY_DECISION"]
                        },
                        "json_pointer": {"type": "string", "pattern": "^/"},
                        "allowed_values": {"type": "array", "minItems": 1},
                        "forbidden_values": {"type": "array", "minItems": 1},
                        "course_citation_ids": {
                            "type": "array",
                            "minItems": 1,
                            "items": {"type": "string", "minLength": 1},
                        },
                        "as_of_evidence_refs": {
                            "type": "array",
                            "minItems": 1,
                            "items": {"type": "string", "minLength": 1},
                        },
                    },
                    "anyOf": [
                        {"required": ["allowed_values"]},
                        {"required": ["forbidden_values"]},
                    ],
                },
            },
            "adjudication_complete": {"const": True},
            "frozen_at_utc": {"type": "string", "minLength": 1},
            "rubric_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        },
    }


def build_rubric_template(gold_case_id: str, annotation_packet_sha256: str) -> dict[str, Any]:
    """Create an intentionally unscorable empty template."""

    core = {
        "rubric_version": RUBRIC_VERSION,
        "status": "DRAFT_UNREVIEWED",
        "gold_case_id": str(gold_case_id),
        "annotation_packet_sha256": _require_sha256(
            annotation_packet_sha256, "annotation_packet_sha256"
        ),
        "classification": None,
        "reviewer_attestations": [],
        "course_citations": [],
        "assertions": [],
        "adjudication_complete": False,
        "frozen_at_utc": None,
    }
    result = dict(core)
    result["rubric_sha256"] = canonical_sha256(core)
    return result


def build_annotation_bundle(
    records: Sequence[Mapping[str, Any]],
    selection_plan: Mapping[str, Any],
    *,
    protocol: Mapping[str, Any],
) -> dict[str, Any]:
    validated_plan = validate_selection_plan(selection_plan, protocol=protocol)
    selected = _selected_source_rows(records, validated_plan)
    packets = [build_annotation_packet(source, case) for source, case in selected]
    packets.sort(key=lambda row: row["gold_case_id"])
    packet_rows = [
        {
            "gold_case_id": packet["gold_case_id"],
            "annotation_packet_sha256": canonical_sha256(packet),
            "packet": packet,
        }
        for packet in packets
    ]
    templates = [
        build_rubric_template(row["gold_case_id"], row["annotation_packet_sha256"])
        for row in packet_rows
    ]
    schema = rubric_schema()
    manifest_core = {
        "manifest_version": ANNOTATION_MANIFEST_VERSION,
        "status": "DRAFT_UNLABELED",
        "classification": "BLINDED_HISTORICAL_COURSE_GOLD_NOT_PERFORMANCE_HOLDOUT",
        "case_count": len(packet_rows),
        "selection_plan_sha256": str(validated_plan["selection_plan_sha256"]),
        "protocol_sha256": str(validated_plan["protocol_sha256"]),
        "annotation_packets_sha256": hashlib.sha256(canonical_jsonl_bytes(packet_rows)).hexdigest(),
        "rubric_templates_sha256": hashlib.sha256(canonical_jsonl_bytes(templates)).hexdigest(),
        "rubric_schema_sha256": canonical_sha256(schema),
        "isolation": {
            "real_identity_visible": False,
            "old_ai_visible": False,
            "future_or_performance_visible": False,
        },
        "gold_labels_present": False,
        "human_annotation_surface": "annotation_packets.jsonl_AND_rubric_templates.jsonl_ONLY",
    }
    manifest = dict(manifest_core)
    manifest["annotation_manifest_sha256"] = canonical_sha256(manifest_core)
    return {
        "selection_plan": dict(validated_plan),
        "annotation_packets": packet_rows,
        "rubric_schema": schema,
        "rubric_templates": templates,
        "annotation_manifest": manifest,
    }


def _validate_frozen_rubric_shape(rubric: Mapping[str, Any]) -> None:
    required = {
        "rubric_version",
        "status",
        "gold_case_id",
        "annotation_packet_sha256",
        "classification",
        "reviewer_attestations",
        "course_citations",
        "assertions",
        "adjudication_complete",
        "frozen_at_utc",
        "rubric_sha256",
    }
    if set(rubric) != required:
        raise GoldEvaluationError("rubric shape differs from the frozen schema")
    if rubric.get("rubric_version") != RUBRIC_VERSION:
        raise GoldEvaluationError("wrong rubric version")
    if rubric.get("status") != "HUMAN_REVIEWED_FROZEN":
        raise GoldEvaluationError("rubric is not HUMAN_REVIEWED_FROZEN")
    if rubric.get("classification") not in {
        "POSITIVE_OR_CONSTRUCTIVE",
        "NEGATIVE_OR_AMBIGUOUS",
    }:
        raise GoldEvaluationError("rubric classification is missing")
    _require_sha256(rubric.get("annotation_packet_sha256"), "annotation_packet_sha256")
    reviewers = rubric.get("reviewer_attestations")
    if not isinstance(reviewers, list) or not reviewers:
        raise GoldEvaluationError("rubric has no human reviewer attestation")
    for reviewer in reviewers:
        if not isinstance(reviewer, Mapping):
            raise GoldEvaluationError("invalid human reviewer attestation")
        if not reviewer.get("reviewer_ref") or not reviewer.get("reviewed_at_utc"):
            raise GoldEvaluationError("human reviewer attestation is incomplete")
        if reviewer.get("course_and_as_of_only") is not True:
            raise GoldEvaluationError("reviewer did not attest course/as-of-only review")
        if reviewer.get("old_ai_and_outcomes_not_viewed") is not True:
            raise GoldEvaluationError("reviewer did not attest AI/outcome blindness")
    citations = rubric.get("course_citations")
    if not isinstance(citations, list) or not citations:
        raise GoldEvaluationError("rubric has no course citation")
    citation_ids = {str(row.get("citation_id") or "") for row in citations if isinstance(row, Mapping)}
    if "" in citation_ids or len(citation_ids) != len(citations):
        raise GoldEvaluationError("rubric course citation IDs are blank or duplicated")
    for citation in citations:
        if not all(citation.get(field) for field in ("source_path", "section", "proposition")):
            raise GoldEvaluationError("rubric course citation is incomplete")
        normalized_path = str(citation["source_path"]).replace("\\", "/")
        if not (
            normalized_path.startswith("course_knowledge_base/")
            or normalized_path.startswith("docs/enlightenment-ai-judgement-")
            or normalized_path.startswith("config/enlightenment_ai_rules_")
        ):
            raise GoldEvaluationError("rubric citation is not a course/rule source")
    assertions = rubric.get("assertions")
    if not isinstance(assertions, list) or not assertions:
        raise GoldEvaluationError("rubric has no machine-readable assertion")
    assertion_ids: set[str] = set()
    for assertion in assertions:
        if not isinstance(assertion, Mapping):
            raise GoldEvaluationError("invalid rubric assertion")
        assertion_id = str(assertion.get("assertion_id") or "")
        if not assertion_id or assertion_id in assertion_ids:
            raise GoldEvaluationError("rubric assertion IDs are blank or duplicated")
        assertion_ids.add(assertion_id)
        if assertion.get("target_artifact") not in {"SEMANTIC_OUTPUT", "POLICY_DECISION"}:
            raise GoldEvaluationError("rubric assertion target is invalid")
        if not str(assertion.get("json_pointer") or "").startswith("/"):
            raise GoldEvaluationError("rubric assertion JSON pointer is invalid")
        allowed = assertion.get("allowed_values")
        forbidden = assertion.get("forbidden_values")
        if (not isinstance(allowed, list) or not allowed) and (
            not isinstance(forbidden, list) or not forbidden
        ):
            raise GoldEvaluationError("rubric assertion has no allowed/forbidden values")
        refs = assertion.get("course_citation_ids")
        if not isinstance(refs, list) or not refs or not set(map(str, refs)).issubset(citation_ids):
            raise GoldEvaluationError("rubric assertion has invalid course citation refs")
        evidence = assertion.get("as_of_evidence_refs")
        if not isinstance(evidence, list) or not evidence:
            raise GoldEvaluationError("rubric assertion has no as-of evidence refs")
    if rubric.get("adjudication_complete") is not True or not rubric.get("frozen_at_utc"):
        raise GoldEvaluationError("rubric human adjudication is incomplete")
    core = dict(rubric)
    supplied = core.pop("rubric_sha256")
    if supplied != canonical_sha256(core):
        raise GoldEvaluationError("rubric immutable hash mismatch")


def validate_frozen_rubric(
    rubric: Mapping[str, Any], *, gold_case_id: str, annotation_packet_sha256: str
) -> None:
    _validate_frozen_rubric_shape(rubric)
    if rubric.get("gold_case_id") != gold_case_id:
        raise GoldEvaluationError("rubric is linked to the wrong gold case")
    if rubric.get("annotation_packet_sha256") != annotation_packet_sha256:
        raise GoldEvaluationError("rubric is linked to a different annotation packet")


def _json_pointer(value: Any, pointer: str) -> tuple[bool, Any]:
    current = value
    if pointer == "":
        return True, current
    if not pointer.startswith("/"):
        return False, None
    for raw in pointer[1:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, Mapping) and token in current:
            current = current[token]
        elif isinstance(current, list) and token.isdigit() and int(token) < len(current):
            current = current[int(token)]
        else:
            return False, None
    return True, current


def validate_evaluation_provenance(
    annotation_rows: Sequence[Mapping[str, Any]],
    *,
    selection_plan: Mapping[str, Any],
    annotation_manifest: Mapping[str, Any],
    bundle_manifest: Mapping[str, Any],
    protocol: Mapping[str, Any],
    formal: bool,
) -> dict[str, Any]:
    """Bind scoring to the pre-label sample, annotation bundle, and protocol."""

    plan = validate_selection_plan(
        selection_plan, protocol=protocol, require_formal=formal
    )
    if formal and (
        protocol.get("protocol_version") != "hybrid-monitoring-protocol-v2"
        or str(protocol.get("status") or "").upper() != "FINAL"
    ):
        raise GoldEvaluationError("formal evaluation requires the FINAL V2 protocol")
    annotation = dict(annotation_manifest)
    annotation_hash = _require_sha256(
        annotation.pop("annotation_manifest_sha256", None), "annotation_manifest_sha256"
    )
    if annotation_hash != canonical_sha256(annotation):
        raise GoldEvaluationError("annotation manifest immutable hash mismatch")
    if annotation.get("manifest_version") != ANNOTATION_MANIFEST_VERSION:
        raise GoldEvaluationError("wrong annotation manifest version")
    if annotation.get("status") != "DRAFT_UNLABELED" or annotation.get("gold_labels_present") is not False:
        raise GoldEvaluationError("annotation manifest is not the original unlabeled freeze")
    if annotation.get("selection_plan_sha256") != plan["selection_plan_sha256"]:
        raise GoldEvaluationError("annotation manifest is linked to another selection plan")
    if annotation.get("protocol_sha256") != plan["protocol_sha256"]:
        raise GoldEvaluationError("annotation manifest is linked to another protocol")
    if int(annotation.get("case_count") or 0) != len(annotation_rows):
        raise GoldEvaluationError("annotation manifest case count differs from evaluation input")
    if annotation.get("annotation_packets_sha256") != hashlib.sha256(
        canonical_jsonl_bytes(annotation_rows)
    ).hexdigest():
        raise GoldEvaluationError("annotation packet artifact differs from its frozen manifest")
    selected_ids = {str(row.get("gold_case_id") or "") for row in plan["cases"]}
    annotation_ids = {str(row.get("gold_case_id") or "") for row in annotation_rows}
    if "" in selected_ids or len(selected_ids) != len(plan["cases"]) or annotation_ids != selected_ids:
        raise GoldEvaluationError("annotation cases differ from the frozen selection plan")

    bundle = dict(bundle_manifest)
    bundle_hash = _require_sha256(
        bundle.pop("bundle_manifest_sha256", None), "bundle_manifest_sha256"
    )
    if bundle_hash != canonical_sha256(bundle):
        raise GoldEvaluationError("bundle manifest immutable hash mismatch")
    if bundle.get("manifest_version") != BUNDLE_MANIFEST_VERSION:
        raise GoldEvaluationError("wrong annotation bundle manifest version")
    if bundle.get("status") != "DRAFT_UNLABELED" or bundle.get("gold_labels_present") is not False:
        raise GoldEvaluationError("bundle manifest is not the original unlabeled freeze")
    for field, expected in (
        ("selection_plan_sha256", plan["selection_plan_sha256"]),
        ("protocol_sha256", plan["protocol_sha256"]),
        ("annotation_manifest_sha256", annotation_hash),
    ):
        if bundle.get(field) != expected:
            raise GoldEvaluationError(f"bundle manifest changed provenance field: {field}")
    artifacts = bundle.get("artifacts") or {}
    expected_artifact_hashes = {
        "selection_plan.json": hashlib.sha256(
            canonical_json_bytes(selection_plan) + b"\n"
        ).hexdigest(),
        "annotation_packets.jsonl": hashlib.sha256(
            canonical_jsonl_bytes(annotation_rows)
        ).hexdigest(),
        "annotation_manifest.json": hashlib.sha256(
            canonical_json_bytes(annotation_manifest) + b"\n"
        ).hexdigest(),
    }
    for name, expected in expected_artifact_hashes.items():
        if not isinstance(artifacts.get(name), Mapping) or artifacts[name].get("sha256") != expected:
            raise GoldEvaluationError(f"bundle artifact hash mismatch: {name}")
    return {
        "selection_plan_sha256": plan["selection_plan_sha256"],
        "annotation_manifest_sha256": annotation_hash,
        "bundle_manifest_sha256": bundle_hash,
        "protocol_sha256": plan["protocol_sha256"],
        "stratum_counts": dict(plan["stratum_counts"]),
        "case_count": len(plan["cases"]),
        "formal_source": plan.get("formal_source") is True,
    }


def evaluate_gold(
    annotation_rows: Sequence[Mapping[str, Any]],
    rubrics: Sequence[Mapping[str, Any]],
    results: Sequence[Mapping[str, Any]],
    *,
    selection_plan: Mapping[str, Any],
    annotation_manifest: Mapping[str, Any],
    bundle_manifest: Mapping[str, Any],
    protocol: Mapping[str, Any],
    formal: bool = False,
) -> dict[str, Any]:
    """Score only complete, frozen human rubrics; otherwise raise immediately."""

    provenance = validate_evaluation_provenance(
        annotation_rows,
        selection_plan=selection_plan,
        annotation_manifest=annotation_manifest,
        bundle_manifest=bundle_manifest,
        protocol=protocol,
        formal=formal,
    )
    annotations = {str(row.get("gold_case_id") or ""): row for row in annotation_rows}
    rubric_by_id = {str(row.get("gold_case_id") or ""): row for row in rubrics}
    result_by_id = {str(row.get("gold_case_id") or ""): row for row in results}
    if not annotations or len(annotations) != len(annotation_rows):
        raise GoldEvaluationError("annotation cases are empty or duplicated")
    if set(rubric_by_id) != set(annotations) or set(result_by_id) != set(annotations):
        raise GoldEvaluationError("rubric/result coverage differs from annotation cases")
    classifications: Counter[str] = Counter()
    cases: list[dict[str, Any]] = []
    assertion_total = assertion_passed = 0
    permission_total = permission_passed = 0
    for case_id in sorted(annotations):
        annotation = annotations[case_id]
        packet = annotation.get("packet") if isinstance(annotation.get("packet"), Mapping) else annotation
        packet_sha = canonical_sha256(packet)
        rubric = rubric_by_id[case_id]
        validate_frozen_rubric(rubric, gold_case_id=case_id, annotation_packet_sha256=packet_sha)
        evidence_refs = {
            str(row.get("ref"))
            for row in (packet.get("evidence") or [])
            if isinstance(row, Mapping) and row.get("ref")
        }
        for assertion in rubric["assertions"]:
            if not set(map(str, assertion["as_of_evidence_refs"])).issubset(evidence_refs):
                raise GoldEvaluationError("rubric cites evidence absent from the as-of annotation packet")
        classifications[str(rubric["classification"])] += 1
        result = result_by_id[case_id]
        assert_no_identity_ai_or_outcome(
            {key: value for key, value in result.items() if key != "gold_case_id"}
        )
        assertions: list[dict[str, Any]] = []
        for assertion in rubric["assertions"]:
            target_key = (
                "semantic_output"
                if assertion["target_artifact"] == "SEMANTIC_OUTPUT"
                else "policy_decision"
            )
            exists, actual = _json_pointer(result.get(target_key), assertion["json_pointer"])
            allowed = assertion.get("allowed_values")
            forbidden = assertion.get("forbidden_values")
            passed = exists
            if passed and isinstance(allowed, list) and allowed:
                passed = any(canonical_json_bytes(actual) == canonical_json_bytes(value) for value in allowed)
            if passed and isinstance(forbidden, list) and forbidden:
                passed = not any(
                    canonical_json_bytes(actual) == canonical_json_bytes(value) for value in forbidden
                )
            assertions.append(
                {
                    "assertion_id": assertion["assertion_id"],
                    "pass": bool(passed),
                    "value_present": bool(exists),
                    "actual_sha256": canonical_sha256(actual) if exists else None,
                }
            )
            assertion_total += 1
            assertion_passed += int(bool(passed))
            if (
                assertion["target_artifact"] == "POLICY_DECISION"
                and assertion["json_pointer"] == "/permission"
            ):
                permission_total += 1
                permission_passed += int(bool(passed))
        if not any(
            assertion["target_artifact"] == "POLICY_DECISION"
            and assertion["json_pointer"] == "/permission"
            for assertion in rubric["assertions"]
        ):
            raise GoldEvaluationError(
                "every blinded gold case requires a material /permission assertion"
            )
        cases.append(
            {
                "gold_case_id": case_id,
                "classification": rubric["classification"],
                "pass": all(row["pass"] for row in assertions),
                "assertions": assertions,
            }
        )
    contract = ((protocol.get("correctness_gate") or {}).get("blinded_course_gold_holdout") or {})
    atomic_rate = assertion_passed / assertion_total if assertion_total else 0.0
    permission_rate = permission_passed / permission_total if permission_total else 0.0
    required_atomic_rate = float(contract.get("required_atomic_assertion_agreement_rate") or 0.9)
    required_permission_rate = float(
        contract.get("required_material_permission_agreement_rate") or 1.0
    )
    atomic_pass = atomic_rate >= required_atomic_rate
    permission_pass = permission_rate >= required_permission_rate
    course_gold_pass = atomic_pass and permission_pass
    report_core = {
        "course_gold_version": COURSE_GOLD_VERSION,
        "course_gold_status": COURSE_GOLD_STATUS,
        "classification": "BLINDED_HISTORICAL_COURSE_GOLD_NOT_PERFORMANCE_HOLDOUT",
        "case_count": len(cases),
        "stratum_counts": provenance["stratum_counts"],
        "classification_counts": dict(sorted(classifications.items())),
        "research_balance_rule_applied": False,
        "required_atomic_assertion_agreement_rate": required_atomic_rate,
        "atomic_assertion_count": assertion_total,
        "atomic_assertion_passed": assertion_passed,
        "atomic_assertion_agreement_rate": atomic_rate,
        "atomic_assertion_agreement_pass": atomic_pass,
        "required_material_permission_agreement_rate": required_permission_rate,
        "material_permission_assertion_count": permission_total,
        "material_permission_assertion_passed": permission_passed,
        "material_permission_agreement_rate": permission_rate,
        "material_permission_agreement_pass": permission_pass,
        "passed_cases": sum(row["pass"] for row in cases),
        "selection_plan_sha256": provenance["selection_plan_sha256"],
        "annotation_manifest_sha256": provenance["annotation_manifest_sha256"],
        "bundle_manifest_sha256": provenance["bundle_manifest_sha256"],
        "protocol_sha256": provenance["protocol_sha256"],
        "provenance_pass": True,
        "formal_source": provenance["formal_source"],
        "course_gold_pass": course_gold_pass,
        "cases": cases,
    }
    report = dict(report_core)
    report["evaluation_sha256"] = canonical_sha256(report_core)
    return report


def _immutable_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() == payload:
            return
        raise CourseGoldError(f"refusing to overwrite immutable artifact: {path}")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != payload:
                raise CourseGoldError(f"conflicting immutable artifact: {path}")
        temporary.unlink(missing_ok=True)
    finally:
        temporary.unlink(missing_ok=True)


def write_annotation_bundle(bundle: Mapping[str, Any], output_dir: Path) -> dict[str, Any]:
    """Write unlabeled artifacts; the bundle manifest is written last."""

    output_dir = output_dir.resolve()
    artifacts = {
        "selection_plan.json": canonical_json_bytes(bundle["selection_plan"]) + b"\n",
        "annotation_packets.jsonl": canonical_jsonl_bytes(bundle["annotation_packets"]),
        "rubric.schema.json": canonical_json_bytes(bundle["rubric_schema"]) + b"\n",
        "rubric_templates.jsonl": canonical_jsonl_bytes(bundle["rubric_templates"]),
        "annotation_manifest.json": canonical_json_bytes(bundle["annotation_manifest"]) + b"\n",
    }
    written: dict[str, dict[str, Any]] = {}
    for name, payload in artifacts.items():
        path = output_dir / name
        _immutable_write(path, payload)
        written[name] = {
            "path": str(path),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "bytes": len(payload),
            "audience": "HUMAN_ANNOTATOR"
            if name in {"annotation_packets.jsonl", "rubric.schema.json", "rubric_templates.jsonl"}
            else "EVALUATOR_ONLY",
        }
    manifest_core = {
        "manifest_version": BUNDLE_MANIFEST_VERSION,
        "status": "DRAFT_UNLABELED",
        "gold_labels_present": False,
        "selection_plan_sha256": bundle["selection_plan"]["selection_plan_sha256"],
        "protocol_sha256": bundle["selection_plan"]["protocol_sha256"],
        "annotation_manifest_sha256": bundle["annotation_manifest"][
            "annotation_manifest_sha256"
        ],
        "artifacts": written,
    }
    manifest = dict(manifest_core)
    manifest["bundle_manifest_sha256"] = canonical_sha256(manifest_core)
    payload = canonical_json_bytes(manifest) + b"\n"
    path = output_dir / "bundle_manifest.json"
    _immutable_write(path, payload)
    return manifest


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise CourseGoldError(f"expected JSON object: {path}")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise CourseGoldError(f"JSONL line {line_number} is not an object: {path}")
        rows.append(value)
    return rows


def _prepare(args: argparse.Namespace) -> int:
    universe_path = args.universe_manifest.resolve()
    universe = _read_json(universe_path)
    validated = validate_formal_universe_manifest(universe, manifest_path=universe_path)
    artifact_path = Path(validated["resolved_artifact_path"])
    records = _read_jsonl(artifact_path)
    if len(records) != int(universe["review_points"]):
        raise FormalUniverseError("review-point artifact actual rows differ from formal universe")
    universe_sha = file_sha256(universe_path)
    config = _read_json(args.selection_config)
    protocol = _read_json(args.protocol)
    exclusion_paths = {
        "V1_AI_REVIEWED": args.v1_exclusions,
        "V2_AI_REVIEWED": args.v2_exclusions,
        "RESEARCH_CALIBRATION": args.research_calibration_exclusions,
    }
    exclusions = {scope: _read_json(path) for scope, path in exclusion_paths.items()}
    plan = build_selection_plan(
        records,
        selection_config=config,
        exclusion_manifests=exclusions,
        universe_manifest_sha256=universe_sha,
        protocol_sha256=canonical_sha256(protocol),
        formal=True,
        formal_universe_manifest=universe,
    )
    bundle = build_annotation_bundle(records, plan, protocol=protocol)
    manifest = write_annotation_bundle(bundle, args.output_dir)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


def _evaluate(args: argparse.Namespace) -> int:
    annotations = _read_jsonl(args.annotation_packets)
    rubrics = _read_jsonl(args.rubrics)
    results = _read_jsonl(args.results)
    report = evaluate_gold(
        annotations,
        rubrics,
        results,
        selection_plan=_read_json(args.selection_plan),
        annotation_manifest=_read_json(args.annotation_manifest),
        bundle_manifest=_read_json(args.bundle_manifest),
        protocol=_read_json(args.protocol),
        formal=True,
    )
    payload = canonical_json_bytes(report) + b"\n"
    _immutable_write(args.output.resolve(), payload)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["course_gold_pass"] else 2


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare", help="Prepare an unlabeled blind annotation bundle")
    prepare.add_argument("--universe-manifest", type=Path, required=True)
    prepare.add_argument("--selection-config", type=Path, required=True)
    prepare.add_argument("--v1-exclusions", type=Path, required=True)
    prepare.add_argument("--v2-exclusions", type=Path, required=True)
    prepare.add_argument("--research-calibration-exclusions", type=Path, required=True)
    prepare.add_argument(
        "--protocol", type=Path, default=Path(__file__).resolve().parents[1] / "config/hybrid_monitoring_protocol_v2.json"
    )
    prepare.add_argument("--output-dir", type=Path, required=True)
    prepare.set_defaults(handler=_prepare)

    evaluate = subparsers.add_parser("evaluate", help="Score only frozen human rubrics")
    evaluate.add_argument("--annotation-packets", type=Path, required=True)
    evaluate.add_argument("--rubrics", type=Path, required=True)
    evaluate.add_argument("--results", type=Path, required=True)
    evaluate.add_argument("--selection-plan", type=Path, required=True)
    evaluate.add_argument("--annotation-manifest", type=Path, required=True)
    evaluate.add_argument("--bundle-manifest", type=Path, required=True)
    evaluate.add_argument(
        "--protocol", type=Path, default=Path(__file__).resolve().parents[1] / "config/hybrid_monitoring_protocol_v2.json"
    )
    evaluate.add_argument("--output", type=Path, required=True)
    evaluate.set_defaults(handler=_evaluate)
    args = parser.parse_args()
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(_main())
