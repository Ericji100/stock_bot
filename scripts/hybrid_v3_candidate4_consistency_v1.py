"""Candidate4/V5 three-run repeatability gate for the frozen V3 research track.

This evaluator is deliberately outcome blind.  It reads the 120 locked primary
packets and the three validated AI-run envelope sets, verifies their complete
hash chain, applies the unchanged deterministic V3 reducer, and evaluates only
the repeatability thresholds frozen in the research protocol.  It never reads
stock identity, future prices, or performance.

Passing this gate is permission to lock a common semantic ledger; it is not
proof of course correctness, production readiness, or profitability.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

try:
    from .hybrid_v3_atomic_policy_v3 import reduce_atomic_v3, validate_atomic
    from .hybrid_v3_consistency_v2 import critical_question_ids
    from .hybrid_v3_conservative_merge_v1 import conservative_merge_atomic_outputs
    from .hybrid_v3_sharding_v2 import build_case_key, canonical_sha256
    from .hybrid_v3_triplicate_smoke_audit import decision_signature, result_map
except ImportError:  # pragma: no cover - direct script execution
    from scripts.hybrid_v3_atomic_policy_v3 import reduce_atomic_v3, validate_atomic
    from scripts.hybrid_v3_consistency_v2 import critical_question_ids
    from scripts.hybrid_v3_conservative_merge_v1 import conservative_merge_atomic_outputs
    from scripts.hybrid_v3_sharding_v2 import build_case_key, canonical_sha256
    from scripts.hybrid_v3_triplicate_smoke_audit import decision_signature, result_map


REPORT_VERSION = "hybrid-v3-candidate4-consistency-v1"
EXPECTED_CASES = 120
EXPECTED_RUNS = 3
ROOT = Path(__file__).resolve().parents[1]


class Candidate4ConsistencyError(ValueError):
    """The locked source, run coverage, or hash chain is incomplete or changed."""


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise Candidate4ConsistencyError(f"expected JSON object: {path}")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = [
        json.loads(line)
        for line in Path(path).read_text(encoding="utf-8-sig").splitlines()
        if line.strip()
    ]
    if any(not isinstance(row, dict) for row in rows):
        raise Candidate4ConsistencyError(f"expected JSON objects in: {path}")
    return rows


def _component(freeze: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    matches = [row for row in freeze.get("v2_components", []) if row.get("name") == name]
    if len(matches) != 1:
        raise Candidate4ConsistencyError(f"execution freeze must pin one {name} component")
    return matches[0]


def _verify_frozen_inputs(
    *,
    source: Path,
    source_manifest_path: Path,
    track_manifest_path: Path,
    assignment_manifest_path: Path,
    execution_freeze_path: Path,
    protocol_path: Path,
    schema_path: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    source = Path(source).resolve()
    source_manifest_path = Path(source_manifest_path).resolve()
    track_manifest_path = Path(track_manifest_path).resolve()
    assignment_manifest_path = Path(assignment_manifest_path).resolve()
    execution_freeze_path = Path(execution_freeze_path).resolve()
    protocol_path = Path(protocol_path).resolve()
    schema_path = Path(schema_path).resolve()

    source_manifest = _read_json(source_manifest_path)
    track = _read_json(track_manifest_path)
    assignment = _read_json(assignment_manifest_path)
    freeze = _read_json(execution_freeze_path)
    protocol = _read_json(protocol_path)
    schema = _read_json(schema_path)

    if source_manifest.get("status") != "LOCKED_OUTCOME_BLIND":
        raise Candidate4ConsistencyError("primary source manifest is not LOCKED_OUTCOME_BLIND")
    if source_manifest.get("block_id") != "PRIMARY":
        raise Candidate4ConsistencyError("consistency source must be the PRIMARY block")
    if track.get("status") != "FROZEN_OUTCOME_BLIND_RESEARCH_ONLY":
        raise Candidate4ConsistencyError("research track is not frozen outcome blind")
    if not track.get("outcome_blind"):
        raise Candidate4ConsistencyError("research track does not assert outcome blindness")
    source_manifest_core = dict(source_manifest)
    source_manifest_self_hash = source_manifest_core.pop("manifest_sha256", None)
    if source_manifest_self_hash != canonical_sha256(source_manifest_core):
        raise Candidate4ConsistencyError("primary source manifest self-hash changed")
    if source_manifest.get("mapping_sha256") != canonical_sha256(source_manifest.get("mapping") or []):
        raise Candidate4ConsistencyError("primary source mapping self-hash changed")
    track_core = dict(track)
    track_self_hash = track_core.pop("track_manifest_sha256", None)
    if track_self_hash != canonical_sha256(track_core):
        raise Candidate4ConsistencyError("research track manifest self-hash changed")
    if freeze.get("status") != "FROZEN_IMMUTABLE_BEFORE_AI_EXECUTION":
        raise Candidate4ConsistencyError("execution contract is not frozen")
    causal_flags = {
        "outcome_blind": True,
        "identity_visible": False,
        "future_data_visible": False,
        "performance_visible": False,
        "old_ai_output_visible": False,
    }
    for key, expected in causal_flags.items():
        if freeze.get(key) is not expected:
            raise Candidate4ConsistencyError(f"execution freeze causal flag changed: {key}")

    source_sha = file_sha256(source)
    source_manifest_sha = file_sha256(source_manifest_path)
    freeze_sha = file_sha256(execution_freeze_path)
    primary_blocks = [row for row in track.get("blocks", []) if row.get("block_id") == "PRIMARY"]
    if len(primary_blocks) != 1:
        raise Candidate4ConsistencyError("research track must contain exactly one PRIMARY block")
    primary = primary_blocks[0]
    if Path(str(primary.get("packet_path") or "")).resolve() != source:
        raise Candidate4ConsistencyError("PRIMARY packet path differs from requested source")
    if Path(str(primary.get("manifest_path") or "")).resolve() != source_manifest_path:
        raise Candidate4ConsistencyError("PRIMARY manifest path differs from requested source manifest")
    expected_source_sha = str(source_manifest.get("packets_canonical_jsonl_sha256") or "")
    if source_sha != expected_source_sha or source_sha != str(primary.get("packet_sha256") or ""):
        raise Candidate4ConsistencyError("primary source artifact hash changed")
    if source_manifest_sha != str(primary.get("manifest_sha256") or ""):
        raise Candidate4ConsistencyError("primary source manifest hash changed")
    if freeze_sha != str(track.get("execution_freeze_sha256") or ""):
        raise Candidate4ConsistencyError("execution freeze hash changed")
    if freeze_sha != str(assignment.get("execution_contract_sha256") or ""):
        raise Candidate4ConsistencyError("shard assignment uses a different execution freeze")
    if source_sha != str(assignment.get("source_manifest_sha256") or ""):
        raise Candidate4ConsistencyError("shard assignment uses a different primary source")
    if Path(str(track.get("execution_freeze_path") or "")).resolve() != execution_freeze_path:
        raise Candidate4ConsistencyError("track points to a different execution freeze")
    if str(track.get("execution_freeze_sha256") or "") != freeze_sha:
        raise Candidate4ConsistencyError("track execution freeze hash changed")
    assigned_track = track.get("primary_case_assignment")
    if assigned_track is not None:
        if not isinstance(assigned_track, dict):
            raise Candidate4ConsistencyError("track primary_case_assignment is invalid")
        if Path(str(assigned_track.get("path") or "")).resolve() != assignment_manifest_path:
            raise Candidate4ConsistencyError("track points to a different shard assignment")
        if str(assigned_track.get("sha256") or "") != file_sha256(assignment_manifest_path):
            raise Candidate4ConsistencyError("track shard assignment hash changed")
        if str(assigned_track.get("assignment_sha256") or "") != str(
            assignment.get("assignment_sha256") or ""
        ):
            raise Candidate4ConsistencyError("track shard membership hash changed")

    coverage = assignment.get("coverage") or {}
    if (
        int(assignment.get("expected_rows", -1)) != EXPECTED_CASES
        or int(coverage.get("covered", -1)) != EXPECTED_CASES
        or any(int(coverage.get(key, -1)) != 0 for key in ("missing", "overlap", "unexpected"))
    ):
        raise Candidate4ConsistencyError("primary shard assignment is not exact 120-case coverage")
    assigned_rows: list[dict[str, Any]] = []
    assignment_pairs: list[dict[str, Any]] = []
    for shard in assignment.get("shards", []):
        shard_path = Path(str(shard.get("path"))).resolve()
        if file_sha256(shard_path) != str(shard.get("sha256") or ""):
            raise Candidate4ConsistencyError(f"assigned shard changed: {shard_path}")
        rows = _read_jsonl(shard_path)
        if len(rows) != int(shard.get("rows", -1)):
            raise Candidate4ConsistencyError(f"assigned shard row count changed: {shard_path}")
        if canonical_sha256([row.get("case_key") for row in rows]) != shard.get("case_keys_sha256"):
            raise Candidate4ConsistencyError(f"assigned shard case-key hash changed: {shard_path}")
        assignment_pairs.extend(
            {"case_key": row.get("case_key"), "shard_id": int(shard.get("shard_id", -1))}
            for row in rows
        )
        assigned_rows.extend(rows)
    if len(assigned_rows) != EXPECTED_CASES:
        raise Candidate4ConsistencyError("assigned shard rows do not total 120")
    if canonical_sha256(assignment_pairs) != assignment.get("assignment_sha256"):
        raise Candidate4ConsistencyError("primary shard assignment self-hash changed")

    packets = _read_jsonl(source)
    mapping = source_manifest.get("mapping") or []
    if (
        len(packets) != EXPECTED_CASES
        or len(mapping) != EXPECTED_CASES
        or int(source_manifest.get("rows", -1)) != EXPECTED_CASES
    ):
        raise Candidate4ConsistencyError("primary block must contain exactly 120 cases")
    mapping_by_ordinal = {int(row["selected_ordinal"]): row for row in mapping}
    if set(mapping_by_ordinal) != set(range(EXPECTED_CASES)):
        raise Candidate4ConsistencyError("primary source mapping ordinals are incomplete")
    packet_by_ordinal: dict[int, dict[str, Any]] = {}
    for ordinal, packet in enumerate(packets):
        source_map = mapping_by_ordinal[ordinal]
        if str(packet.get("review_id") or "") != str(source_map.get("review_id") or ""):
            raise Candidate4ConsistencyError(f"primary packet mapping differs at ordinal {ordinal}")
        if canonical_sha256(packet) != str(source_map.get("packet_sha256") or ""):
            raise Candidate4ConsistencyError(f"primary packet hash changed at ordinal {ordinal}")
        packet_by_ordinal[ordinal] = packet

    assigned_by_ordinal: dict[int, dict[str, Any]] = {}
    for row in assigned_rows:
        ordinal = int(row.get("source_ordinal", -1))
        if ordinal in assigned_by_ordinal:
            raise Candidate4ConsistencyError(f"duplicate assigned source ordinal: {ordinal}")
        if ordinal not in packet_by_ordinal:
            raise Candidate4ConsistencyError(f"unexpected assigned source ordinal: {ordinal}")
        packet = row.get("packet")
        source_map = mapping_by_ordinal[ordinal]
        if not isinstance(packet, dict) or packet != packet_by_ordinal[ordinal]:
            raise Candidate4ConsistencyError(f"assigned packet differs at ordinal {ordinal}")
        if canonical_sha256(packet) != row.get("packet_sha256"):
            raise Candidate4ConsistencyError(f"assigned packet hash changed: {row.get('review_id')}")
        if row.get("review_id") != source_map.get("review_id"):
            raise Candidate4ConsistencyError(f"assigned review ID differs at ordinal {ordinal}")
        if row.get("source_manifest_sha256") != source_sha:
            raise Candidate4ConsistencyError(f"assigned source hash differs at ordinal {ordinal}")
        expected_case_key = build_case_key(
            source_manifest_sha256=source_sha,
            source_ordinal=ordinal,
            review_id=str(row.get("review_id") or ""),
            packet_sha256=str(row.get("packet_sha256") or ""),
            protocol_sha256=str(row.get("protocol_sha256") or ""),
            prompt_sha256=str(row.get("prompt_sha256") or ""),
            schema_sha256=str(row.get("schema_sha256") or ""),
            execution_contract_sha256=str(row.get("execution_contract_sha256") or ""),
            model=str(row.get("model") or ""),
            reasoning_effort=str(row.get("reasoning_effort") or ""),
        )
        if row.get("case_key") != expected_case_key:
            raise Candidate4ConsistencyError(f"assigned case key changed at ordinal {ordinal}")
        enriched = dict(row)
        enriched["sampling_focus"] = source_map.get("sampling_focus")
        enriched["eligible_sampling_strata"] = source_map.get("eligible_sampling_strata") or []
        assigned_by_ordinal[ordinal] = enriched
    if set(assigned_by_ordinal) != set(range(EXPECTED_CASES)):
        raise Candidate4ConsistencyError("assigned source ordinals are incomplete")
    rows = [assigned_by_ordinal[index] for index in range(EXPECTED_CASES)]
    review_ids = [str(row.get("review_id") or "") for row in rows]
    if any(not value for value in review_ids) or len(set(review_ids)) != EXPECTED_CASES:
        raise Candidate4ConsistencyError("primary review IDs are empty or duplicated")

    execution = freeze.get("execution_contract") or {}
    if track.get("model") != execution.get("model") or track.get("reasoning_effort") != execution.get("reasoning_effort"):
        raise Candidate4ConsistencyError("track model/reasoning differs from execution freeze")
    component_paths = {
        "protocol": protocol_path,
        "schema": schema_path,
    }
    for row in freeze.get("v2_components", []):
        name = str(row.get("name") or "")
        relative = str(row.get("relative_path") or "")
        if not name or not relative:
            raise Candidate4ConsistencyError("execution freeze contains an invalid component")
        path = (ROOT / relative).resolve()
        if name in component_paths and component_paths[name] != path:
            raise Candidate4ConsistencyError(f"requested {name} path differs from frozen component")
        component_paths[name] = path
    required_components = {"launcher", "reviewer", "runner", "policy", "prompt", "schema", "protocol"}
    if set(component_paths) != required_components:
        raise Candidate4ConsistencyError("execution freeze component set is incomplete or unexpected")
    for name, path in sorted(component_paths.items()):
        component = _component(freeze, name)
        if str(component.get("status") or "").upper() != "FINAL":
            raise Candidate4ConsistencyError(f"{name} component is not FINAL")
        if file_sha256(path) != str(component.get("sha256") or ""):
            raise Candidate4ConsistencyError(f"frozen {name} component changed")
    if protocol.get("status") != "FINAL_RESEARCH_LOCKED":
        raise Candidate4ConsistencyError("research protocol is not FINAL_RESEARCH_LOCKED")
    return rows, protocol, schema


def _load_run(run_root: Path, expected_run_number: int) -> dict[str, dict[str, Any]]:
    root = Path(run_root).resolve()
    paths = sorted(root.glob("s*/cases/*.json"))
    by_review: dict[str, dict[str, Any]] = {}
    for path in paths:
        envelope = _read_json(path)
        review_id = str(envelope.get("review_id") or "")
        if not review_id or review_id in by_review:
            raise Candidate4ConsistencyError(
                f"empty or duplicate review ID in run {expected_run_number}: {review_id}"
            )
        if int(envelope.get("run_number", -1)) != expected_run_number:
            raise Candidate4ConsistencyError(
                f"run number differs in {path}: {envelope.get('run_number')}"
            )
        if envelope.get("status") != "VALID":
            raise Candidate4ConsistencyError(f"run envelope is not VALID: {path}")
        output = envelope.get("output")
        if not isinstance(output, dict) or canonical_sha256(output) != envelope.get("output_sha256"):
            raise Candidate4ConsistencyError(f"run output hash changed: {path}")
        by_review[review_id] = envelope
    return by_review


def _rate(exact: int, total: int, threshold: float | None = None) -> dict[str, Any]:
    rate = exact / total if total else 0.0
    value: dict[str, Any] = {"exact": exact, "total": total, "rate": rate}
    if threshold is not None:
        value.update({"threshold": threshold, "passed": rate >= threshold})
    return value


def evaluate_acceptance(
    *, schema_causal_rate: float, permission_rate: float, scenario_left_right_rate: float,
    thresholds: Mapping[str, float],
) -> dict[str, Any]:
    checks = {
        "schema_and_causal_fields": schema_causal_rate >= float(thresholds["schema_and_causal_fields"]),
        "material_permission": permission_rate >= float(thresholds["material_permission"]),
        "scenario_and_left_right_phase": scenario_left_right_rate
        >= float(thresholds["scenario_and_left_right_phase"]),
    }
    return {"checks": checks, "passed": all(checks.values())}


def _decision_exact(signatures: Sequence[Mapping[str, Any]], fields: Sequence[str]) -> bool:
    return all(len({signature.get(field) for signature in signatures}) == 1 for field in fields)


def _counter(values: Iterable[str]) -> dict[str, int]:
    return dict(sorted(Counter(values).items()))


def evaluate(
    *,
    source: Path,
    source_manifest: Path,
    track_manifest: Path,
    assignment_manifest: Path,
    execution_freeze: Path,
    run_roots: Sequence[Path],
    protocol_path: Path,
    schema_path: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if len(run_roots) != EXPECTED_RUNS:
        raise Candidate4ConsistencyError("Candidate4 repeatability requires exactly three runs")
    records, protocol, schema = _verify_frozen_inputs(
        source=source,
        source_manifest_path=source_manifest,
        track_manifest_path=track_manifest,
        assignment_manifest_path=assignment_manifest,
        execution_freeze_path=execution_freeze,
        protocol_path=protocol_path,
        schema_path=schema_path,
    )
    expected_ids = [str(row["review_id"]) for row in records]
    expected_set = set(expected_ids)
    runs = [_load_run(path, number) for number, path in enumerate(run_roots, 1)]
    run_coverage: list[dict[str, Any]] = []
    for number, rows in enumerate(runs, 1):
        actual = set(rows)
        missing = sorted(expected_set - actual)
        unexpected = sorted(actual - expected_set)
        run_coverage.append(
            {
                "run_number": number,
                "cases": len(rows),
                "missing": len(missing),
                "unexpected": len(unexpected),
            }
        )
        if missing or unexpected or len(rows) != EXPECTED_CASES:
            raise Candidate4ConsistencyError(
                f"run {number} is not exact 120-case coverage: "
                f"cases={len(rows)}, missing={len(missing)}, unexpected={len(unexpected)}"
            )

    freeze = _read_json(execution_freeze)
    expected_identity = {
        "source_manifest_sha256": file_sha256(source),
        "protocol_sha256": file_sha256(protocol_path),
        "schema_sha256": file_sha256(schema_path),
        "execution_contract_sha256": file_sha256(execution_freeze),
        "model": (freeze.get("execution_contract") or {}).get("model"),
        "reasoning_effort": (freeze.get("execution_contract") or {}).get("reasoning_effort"),
    }
    critical_ids = critical_question_ids(protocol, schema)
    permission_exact = 0
    route_exact = 0
    scenario_lr_exact = 0
    scenario_stage_lr_exact = 0
    schema_causal_exact = 0
    pooled_atom_exact = 0
    pooled_atom_total = 0
    pooled_critical_exact = 0
    pooled_critical_total = 0
    per_case: list[dict[str, Any]] = []
    merged_ledger: list[dict[str, Any]] = []
    permissions_by_run: list[list[str]] = [[] for _ in range(EXPECTED_RUNS)]
    scenarios_by_run: list[list[str]] = [[] for _ in range(EXPECTED_RUNS)]
    focus_accumulator: dict[str, dict[str, int]] = defaultdict(
        lambda: {"cases": 0, "permission_exact": 0, "scenario_left_right_exact": 0}
    )

    identity_fields = (
        "review_id",
        "case_key",
        "packet_sha256",
        "source_manifest_sha256",
        "protocol_sha256",
        "prompt_sha256",
        "schema_sha256",
        "execution_contract_sha256",
        "model",
        "reasoning_effort",
    )
    for record in records:
        review_id = str(record["review_id"])
        packet = record["packet"]
        envelopes = [run[review_id] for run in runs]
        for field in identity_fields:
            values = {str(envelope.get(field)) for envelope in envelopes}
            if len(values) != 1:
                raise Candidate4ConsistencyError(f"three-run identity differs for {review_id}: {field}")
        if envelopes[0].get("packet_sha256") != record.get("packet_sha256"):
            raise Candidate4ConsistencyError(f"run packet differs from source: {review_id}")
        for field, expected in expected_identity.items():
            if envelopes[0].get(field) != expected:
                raise Candidate4ConsistencyError(f"frozen run identity changed for {review_id}: {field}")

        outputs = [envelope["output"] for envelope in envelopes]
        validation = [list(validate_atomic(packet, output)) for output in outputs]
        if any(validation):
            raise Candidate4ConsistencyError(f"atomic validation failed for {review_id}: {validation}")
        schema_causal_exact += 1
        signatures = [decision_signature(reduce_atomic_v3(packet, output)) for output in outputs]
        maps = [result_map(output) for output in outputs]
        paths = sorted(set().union(*(set(mapping) for mapping in maps)))
        if any(set(mapping) != set(paths) for mapping in maps):
            raise Candidate4ConsistencyError(f"three-run atomic manifest differs: {review_id}")
        critical_paths = [path for path in paths if path.rsplit(".", 1)[-1] in critical_ids]
        atom_exact = sum(len({mapping[path] for mapping in maps}) == 1 for path in paths)
        critical_exact = sum(
            len({mapping[path] for mapping in maps}) == 1 for path in critical_paths
        )
        pooled_atom_exact += atom_exact
        pooled_atom_total += len(paths)
        pooled_critical_exact += critical_exact
        pooled_critical_total += len(critical_paths)

        permission_is_exact = _decision_exact(signatures, ("permission",))
        route_is_exact = _decision_exact(signatures, ("route",))
        scenario_lr_is_exact = _decision_exact(signatures, ("scenario", "left_right_phase"))
        scenario_stage_lr_is_exact = _decision_exact(
            signatures, ("scenario", "stage", "left_right_phase")
        )
        permission_exact += int(permission_is_exact)
        route_exact += int(route_is_exact)
        scenario_lr_exact += int(scenario_lr_is_exact)
        scenario_stage_lr_exact += int(scenario_stage_lr_is_exact)
        for idx, signature in enumerate(signatures):
            permissions_by_run[idx].append(str(signature.get("permission")))
            scenarios_by_run[idx].append(str(signature.get("scenario")))

        merged, merge_metrics = conservative_merge_atomic_outputs(outputs, critical_ids=critical_ids)
        merged_validation = list(validate_atomic(packet, merged))
        if merged_validation:
            raise Candidate4ConsistencyError(
                f"conservative merged output is invalid for {review_id}: {merged_validation}"
            )
        merged_signature = decision_signature(reduce_atomic_v3(packet, merged))
        focus = str(record.get("sampling_focus") or "UNSPECIFIED")
        focus_accumulator[focus]["cases"] += 1
        focus_accumulator[focus]["permission_exact"] += int(permission_is_exact)
        focus_accumulator[focus]["scenario_left_right_exact"] += int(scenario_lr_is_exact)
        per_case.append(
            {
                "review_id": review_id,
                "source_ordinal": record.get("source_ordinal"),
                "sampling_focus": focus,
                "atom_agreement": _rate(atom_exact, len(paths)),
                "critical_atom_agreement": _rate(critical_exact, len(critical_paths)),
                "permission_exact": permission_is_exact,
                "route_exact": route_is_exact,
                "scenario_left_right_exact": scenario_lr_is_exact,
                "scenario_stage_left_right_exact": scenario_stage_lr_is_exact,
                "run_decisions": signatures,
                "conservative_merge_decision": merged_signature,
                "critical_disagreement_count": len(merge_metrics["critical_disagreement_paths"]),
            }
        )
        merged_ledger.append(
            {
                "review_id": review_id,
                "source_ordinal": record.get("source_ordinal"),
                "sampling_focus": focus,
                "packet_sha256": record.get("packet_sha256"),
                "merge_policy": "UNANIMOUS_ELSE_UNKNOWN",
                "atomic_semantics": merged,
                "decision": merged_signature,
                "critical_disagreement_paths": merge_metrics["critical_disagreement_paths"],
            }
        )

    thresholds_raw = protocol.get("repeatability_acceptance") or {}
    thresholds = {
        "schema_and_causal_fields": float(thresholds_raw["schema_and_causal_fields"]),
        "material_permission": float(thresholds_raw["material_permission"]),
        "scenario_and_left_right_phase": float(
            thresholds_raw["scenario_and_left_right_phase"]
        ),
    }
    schema_metric = _rate(schema_causal_exact, EXPECTED_CASES, thresholds["schema_and_causal_fields"])
    permission_metric = _rate(permission_exact, EXPECTED_CASES, thresholds["material_permission"])
    scenario_lr_metric = _rate(
        scenario_lr_exact, EXPECTED_CASES, thresholds["scenario_and_left_right_phase"]
    )
    acceptance = evaluate_acceptance(
        schema_causal_rate=schema_metric["rate"],
        permission_rate=permission_metric["rate"],
        scenario_left_right_rate=scenario_lr_metric["rate"],
        thresholds=thresholds,
    )
    focus_metrics: dict[str, Any] = {}
    for focus, counts in sorted(focus_accumulator.items()):
        total = counts["cases"]
        focus_metrics[focus] = {
            "cases": total,
            "permission": _rate(counts["permission_exact"], total),
            "scenario_and_left_right_phase": _rate(
                counts["scenario_left_right_exact"], total
            ),
        }
    distinct_permissions = sorted(set().union(*(set(values) for values in permissions_by_run)))
    warnings: list[str] = []
    if "TRADE" not in distinct_permissions:
        warnings.append("NO_TRADE_PERMISSION_OBSERVED_IN_ANY_RUN")
    if len(distinct_permissions) < 2:
        warnings.append("MATERIAL_PERMISSION_COVERAGE_HAS_ONLY_ONE_CLASS")

    report = {
        "report_version": REPORT_VERSION,
        "status": "REPEATABILITY_PASS" if acceptance["passed"] else "REPEATABILITY_FAIL",
        "classification": "OUTCOME_BLIND_RESEARCH_REPEATABILITY_ONLY",
        "course_correctness_evaluated": False,
        "performance_evaluated": False,
        "identity_visible": False,
        "future_or_performance_visible": False,
        "expected_cases": EXPECTED_CASES,
        "run_count": EXPECTED_RUNS,
        "run_coverage": run_coverage,
        "model": expected_identity["model"],
        "reasoning_effort": expected_identity["reasoning_effort"],
        "thresholds": thresholds,
        "metrics": {
            "schema_and_causal_fields": schema_metric,
            "material_permission": permission_metric,
            "route": _rate(route_exact, EXPECTED_CASES),
            "scenario_and_left_right_phase": scenario_lr_metric,
            "scenario_stage_and_left_right_phase_diagnostic": _rate(
                scenario_stage_lr_exact, EXPECTED_CASES
            ),
            "all_atomic_answers_pooled": _rate(pooled_atom_exact, pooled_atom_total),
            "critical_atomic_answers_pooled": _rate(
                pooled_critical_exact, pooled_critical_total
            ),
        },
        "acceptance": acceptance,
        "permission_distributions_by_run": [
            {"run_number": idx + 1, "counts": _counter(values)}
            for idx, values in enumerate(permissions_by_run)
        ],
        "scenario_distributions_by_run": [
            {"run_number": idx + 1, "counts": _counter(values)}
            for idx, values in enumerate(scenarios_by_run)
        ],
        "sampling_focus_metrics": focus_metrics,
        "coverage_warnings": warnings,
        "per_case": per_case,
        "next_step": (
            "LOCK_COMMON_OUTCOME_BLIND_SEMANTIC_LEDGER"
            if acceptance["passed"]
            else "DO_NOT_UNSEAL_PERFORMANCE; VERSION_AND_RETEST_PROTOCOL"
        ),
        "limitations": [
            "REPEATABILITY_IS_NOT_COURSE_CORRECTNESS",
            "REPEATABILITY_IS_NOT_TRADING_PERFORMANCE",
            "FUTURE_RESULTS_REMAIN_SEALED",
        ],
    }
    report["report_sha256"] = canonical_sha256(report)
    return report, merged_ledger


def _canonical_jsonl(rows: Sequence[Mapping[str, Any]]) -> bytes:
    return b"".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        + b"\n"
        for row in rows
    )


def _publish_immutable(path: Path, payload: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != payload:
            raise Candidate4ConsistencyError(f"immutable artifact already differs: {path}")
        return
    with path.open("xb") as handle:
        handle.write(payload)


def render_markdown(report: Mapping[str, Any], ledger_path: Path | None) -> str:
    metrics = report["metrics"]
    lines = [
        "# V3 Candidate4 正式三輪一致性報告",
        "",
        f"- 結果：`{report['status']}`",
        "- 性質：盲化研究一致性驗收；不是課程正確性、交易績效或正式上線證明。",
        f"- 案例：{report['expected_cases']} 例 × {report['run_count']} 輪",
        f"- 模型／推理：`{report['model']}`／`{report['reasoning_effort']}`",
        "- 股票身分、未來行情與績效：未讀取。",
        "",
        "| 驗收項目 | 實際 | 門檻 | 結果 |",
        "| --- | ---: | ---: | --- |",
    ]
    for key, label in (
        ("schema_and_causal_fields", "Schema與因果欄位"),
        ("material_permission", "最終交易／等待／移除權限"),
        ("scenario_and_left_right_phase", "情境與左右階段"),
    ):
        value = metrics[key]
        lines.append(
            f"| {label} | {value['rate']:.2%}（{value['exact']}/{value['total']}） | "
            f"{value['threshold']:.0%} | {'通過' if value['passed'] else '未通過'} |"
        )
    lines.extend(
        [
            "",
            "## 診斷指標（不改變凍結門檻）",
            "",
            f"- 全部原子答案一致率：{metrics['all_atomic_answers_pooled']['rate']:.2%} "
            f"（{metrics['all_atomic_answers_pooled']['exact']}/{metrics['all_atomic_answers_pooled']['total']}）",
            f"- 關鍵原子答案一致率：{metrics['critical_atomic_answers_pooled']['rate']:.2%} "
            f"（{metrics['critical_atomic_answers_pooled']['exact']}/{metrics['critical_atomic_answers_pooled']['total']}）",
            f"- 情境＋位階＋左右階段一致率：{metrics['scenario_stage_and_left_right_phase_diagnostic']['rate']:.2%}",
        ]
    )
    if report.get("coverage_warnings"):
        lines.extend(["", "## 覆蓋警示", ""])
        lines.extend(f"- `{warning}`" for warning in report["coverage_warnings"])
    lines.extend(["", "## 後續", ""])
    if report["acceptance"]["passed"]:
        lines.append("三項凍結門檻均通過；可鎖定保守合併的共同結構ledger，但仍不可宣稱課程正確或具獲利能力。")
        if ledger_path is not None:
            lines.append(f"共同結構ledger：`{Path(ledger_path).resolve()}`")
    else:
        lines.append("一致性未通過；維持績效封存，另建新協定版本改善後重測，不能在本輪邊跑邊改。")
    lines.extend(["", f"報告SHA-256：`{report['report_sha256']}`", ""])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--track-manifest", type=Path, required=True)
    parser.add_argument("--assignment-manifest", type=Path, required=True)
    parser.add_argument("--execution-freeze", type=Path, required=True)
    parser.add_argument("--run-root", type=Path, action="append", required=True)
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-md", type=Path, required=True)
    parser.add_argument("--merged-ledger", type=Path, required=True)
    args = parser.parse_args()
    report, merged_ledger = evaluate(
        source=args.source,
        source_manifest=args.source_manifest,
        track_manifest=args.track_manifest,
        assignment_manifest=args.assignment_manifest,
        execution_freeze=args.execution_freeze,
        run_roots=args.run_root,
        protocol_path=args.protocol,
        schema_path=args.schema,
    )
    ledger_path: Path | None = None
    if report["acceptance"]["passed"]:
        ledger_payload = _canonical_jsonl(merged_ledger)
        ledger_sha = hashlib.sha256(ledger_payload).hexdigest()
        report["merged_ledger"] = {
            "status": "LOCKED_OUTCOME_BLIND_COMMON_LEDGER",
            "rows": len(merged_ledger),
            "sha256": ledger_sha,
            "path": str(args.merged_ledger.resolve()),
        }
        report.pop("report_sha256", None)
        report["report_sha256"] = canonical_sha256(report)
        _publish_immutable(args.merged_ledger, ledger_payload)
        ledger_path = args.merged_ledger
    json_payload = json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    md_payload = render_markdown(report, ledger_path).encode("utf-8")
    _publish_immutable(args.output_json, json_payload)
    _publish_immutable(args.output_md, md_payload)
    print(
        json.dumps(
            {
                "status": report["status"],
                "passed": report["acceptance"]["passed"],
                "report_sha256": report["report_sha256"],
                "output_json": str(args.output_json.resolve()),
                "output_md": str(args.output_md.resolve()),
                "merged_ledger": str(ledger_path.resolve()) if ledger_path else None,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if report["acceptance"]["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
