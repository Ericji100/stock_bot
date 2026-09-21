"""Seal a partial MATURE_S1 run as a non-reusable technical incident.

The command is deliberately read-only with respect to both inputs.  It validates
the frozen ``research_track_v1`` hash chain and every discovered runner envelope
and attempt, then creates exactly one new ``research_track_v2_integrity``
directory.  Existing output is never replaced, resumed, or deleted.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
INCIDENT_VERSION = "hybrid-v3-mature-s1-incident-v1"
INCIDENT_STATUS = "ABORTED_TECHNICAL_AUDIT_FAIL"
TRACK_VERSION = "hybrid-v3-mature-s1-research-track-v1"
TRACK_STATUS = "FROZEN_OUTCOME_BLIND_SINGLE_ROUTE_RESEARCH_ONLY"
EXPECTED_RUNNER_VERSION = "hybrid-v3-atomic-runner-v2"

DEFAULT_STAGE_ROOT = (
    ROOT
    / "reports/course_backtest/2024-02-02/"
    "historical_scan_2023h2_formal_ai_v3_daily_scan/"
    "hybrid_monitoring_v3_s1_mature_v1"
)
DEFAULT_TRACK_ROOT = DEFAULT_STAGE_ROOT / "research_track_v1"
DEFAULT_RUN_ROOT = ROOT / ".codex_tmp/hv3_s1_mature_v1"
DEFAULT_OUTPUT_DIR = DEFAULT_STAGE_ROOT / "research_track_v2_integrity"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RUN = re.compile(r"^r([1-9][0-9]*)$")
_SHARD = re.compile(r"^s([0-9]+)$")
_ALLOWED_ATTEMPT_STATUSES = {
    "VALIDATED",
    "RETRYABLE_ERROR",
    "TERMINAL_ERROR",
    "CONTRACT_ERROR",
    "NON_RETRYABLE_ERROR",
}


class IncidentIntegrityError(ValueError):
    """An input is incomplete, mutable, contaminated, or internally inconsistent."""


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
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _require_sha256(value: Any, label: str) -> str:
    normalized = str(value or "").lower()
    if not _SHA256.fullmatch(normalized):
        raise IncidentIntegrityError(f"{label} is not a lowercase SHA-256")
    return normalized


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IncidentIntegrityError(f"cannot read valid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise IncidentIntegrityError(f"expected JSON object: {path}")
    return value


def _resolved_input_dir(path: Path, label: str) -> Path:
    try:
        resolved = Path(path).resolve(strict=True)
    except OSError as exc:
        raise IncidentIntegrityError(f"missing {label}: {path}") from exc
    if not resolved.is_dir() or resolved.is_symlink():
        raise IncidentIntegrityError(f"{label} must be a real directory: {resolved}")
    return resolved


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _safe_input_file(path: Path, root: Path, label: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise IncidentIntegrityError(f"missing {label}: {path}") from exc
    if not resolved.is_file() or resolved.is_symlink() or not _within(resolved, root):
        raise IncidentIntegrityError(f"{label} escapes or is not a regular input file: {resolved}")
    return resolved


def _linked_track_file(track_root: Path, manifest: Mapping[str, Any], field: str) -> Path:
    raw = manifest.get(field)
    if not isinstance(raw, str) or not raw:
        raise IncidentIntegrityError(f"track manifest is missing {field}")
    path = Path(raw)
    if not path.is_absolute():
        path = track_root / path
    return _safe_input_file(path, track_root, field)


def _component_path(track_root: Path, raw: Any) -> Path:
    if not isinstance(raw, str) or not raw:
        raise IncidentIntegrityError("frozen component has no relative_path")
    relative = Path(raw)
    if relative.is_absolute():
        candidates = [relative]
    else:
        candidates = [ROOT / relative, track_root / relative]
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if resolved.is_file() and not resolved.is_symlink():
            return resolved
    raise IncidentIntegrityError(f"frozen component is missing: {raw}")


def _verify_track(track_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest_path = _safe_input_file(
        track_root / "research_track_manifest.json", track_root, "research track manifest"
    )
    manifest = _read_json(manifest_path)
    if manifest.get("track_version") != TRACK_VERSION or manifest.get("status") != TRACK_STATUS:
        raise IncidentIntegrityError("input is not the frozen MATURE_S1 research_track_v1")
    if manifest.get("outcome_blind") is not True:
        raise IncidentIntegrityError("research track is not outcome blind")
    if manifest.get("identity_visible") is not False:
        raise IncidentIntegrityError("research track exposes identity")
    if manifest.get("future_or_performance_visible") is not False:
        raise IncidentIntegrityError("research track exposes future/performance")
    if manifest.get("performance_must_remain_sealed_until_prior_gates_pass") is not True:
        raise IncidentIntegrityError("research track does not require sealed performance")
    core = dict(manifest)
    supplied_manifest_hash = _require_sha256(
        core.pop("track_manifest_sha256", None), "track_manifest_sha256"
    )
    if supplied_manifest_hash != canonical_sha256(core):
        raise IncidentIntegrityError("research track manifest self-hash mismatch")

    linked_specs = (
        ("selection_plan_path", "selection_plan_sha256", "selection_plan"),
        ("packet_path", "packet_sha256", "primary_packets"),
        ("packet_manifest_path", "packet_manifest_sha256", "primary_packet_manifest"),
        ("execution_freeze_path", "execution_freeze_sha256", "execution_freeze"),
        ("assignment_manifest_path", "assignment_manifest_sha256", "assignment_manifest"),
    )
    linked: dict[str, dict[str, Any]] = {}
    linked_paths: dict[str, Path] = {}
    for path_field, hash_field, name in linked_specs:
        path = _linked_track_file(track_root, manifest, path_field)
        actual = file_sha256(path)
        expected = _require_sha256(manifest.get(hash_field), hash_field)
        if actual != expected:
            raise IncidentIntegrityError(f"frozen track artifact changed: {name}")
        linked[name] = {
            "relative_path": path.relative_to(track_root).as_posix(),
            "sha256": actual,
            "bytes": path.stat().st_size,
        }
        linked_paths[name] = path

    freeze = _read_json(linked_paths["execution_freeze"])
    required_freeze_flags = {
        "outcome_blind": True,
        "identity_visible": False,
        "future_data_visible": False,
        "performance_visible": False,
        "old_ai_output_visible": False,
        "strategy_or_gate_change": False,
    }
    for field, expected in required_freeze_flags.items():
        if freeze.get(field) is not expected:
            raise IncidentIntegrityError(f"execution freeze violates {field}={expected!r}")

    packet_manifest = _read_json(linked_paths["primary_packet_manifest"])
    packet_requirements = {
        "status": "LOCKED_OUTCOME_BLIND",
        "sampling_metadata_inside_packet": False,
        "identity_visible_inside_packet": False,
        "future_or_performance_visible_inside_packet": False,
    }
    for field, expected in packet_requirements.items():
        if packet_manifest.get(field) != expected:
            raise IncidentIntegrityError(f"primary packet manifest violates {field}")

    components: dict[str, str] = {}
    for row in [*(freeze.get("v2_components") or []), *(freeze.get("stage_components") or [])]:
        if not isinstance(row, Mapping) or not row.get("name"):
            raise IncidentIntegrityError("execution freeze contains an invalid component row")
        name = str(row["name"])
        if name in components:
            raise IncidentIntegrityError(f"duplicate frozen component: {name}")
        expected = _require_sha256(row.get("sha256"), f"component {name} sha256")
        actual_path = _component_path(track_root, row.get("relative_path"))
        if file_sha256(actual_path) != expected:
            raise IncidentIntegrityError(f"frozen component changed: {name}")
        components[name] = expected

    expected_cases = int(manifest.get("cases") or 0)
    expected_runs = int(manifest.get("runs") or 0)
    if expected_cases <= 0 or expected_runs <= 0:
        raise IncidentIntegrityError("research track expected case/run counts are invalid")
    if int(packet_manifest.get("rows") or -1) != expected_cases:
        raise IncidentIntegrityError("packet manifest row count differs from research track")

    return manifest, {
        "research_track_manifest": {
            "relative_path": manifest_path.relative_to(track_root).as_posix(),
            "sha256": file_sha256(manifest_path),
            "logical_sha256": supplied_manifest_hash,
            "bytes": manifest_path.stat().st_size,
        },
        "linked_artifacts": linked,
        "component_hashes": dict(sorted(components.items())),
        "expected_cases_per_run": expected_cases,
        "expected_runs": expected_runs,
    }


def _parse_run_shard(path: Path, run_root: Path) -> tuple[int, int]:
    relative = path.relative_to(run_root)
    if len(relative.parts) < 4:
        raise IncidentIntegrityError(f"unexpected runner path: {relative.as_posix()}")
    run_match = _RUN.fullmatch(relative.parts[0])
    shard_match = _SHARD.fullmatch(relative.parts[1])
    if not run_match or not shard_match:
        raise IncidentIntegrityError(f"unexpected run/shard path: {relative.as_posix()}")
    return int(run_match.group(1)), int(shard_match.group(1))


def _validate_semantic_attestation(output: Mapping[str, Any], label: str) -> None:
    attestation = output.get("causal_attestation")
    if not isinstance(attestation, Mapping):
        raise IncidentIntegrityError(f"{label} has no causal_attestation")
    for field in ("used_future_data", "identity_visible", "performance_visible"):
        if attestation.get(field) is not False:
            raise IncidentIntegrityError(f"{label} violates causal attestation: {field}")


def _expected_case_key(envelope: Mapping[str, Any]) -> str:
    identity = {
        "source_manifest_sha256": _require_sha256(
            envelope.get("source_manifest_sha256"), "source_manifest_sha256"
        ),
        "source_ordinal": int(envelope.get("source_ordinal")),
        "review_id": str(envelope.get("review_id") or ""),
        "packet_sha256": _require_sha256(envelope.get("packet_sha256"), "packet_sha256"),
        "protocol_sha256": _require_sha256(envelope.get("protocol_sha256"), "protocol_sha256"),
        "prompt_sha256": _require_sha256(envelope.get("prompt_sha256"), "prompt_sha256"),
        "schema_sha256": _require_sha256(envelope.get("schema_sha256"), "schema_sha256"),
        "execution_contract_sha256": _require_sha256(
            envelope.get("execution_contract_sha256"), "execution_contract_sha256"
        ),
        "model": str(envelope.get("model") or ""),
        "reasoning_effort": str(envelope.get("reasoning_effort") or ""),
    }
    if not identity["review_id"] or not identity["model"] or not identity["reasoning_effort"]:
        raise IncidentIntegrityError("envelope has an incomplete frozen identity")
    return canonical_sha256(identity)


def _tree_records(paths: Sequence[Path], root: Path) -> list[dict[str, Any]]:
    return [
        {
            "relative_path": path.relative_to(root).as_posix(),
            "sha256": file_sha256(path),
            "bytes": path.stat().st_size,
        }
        for path in sorted(paths, key=lambda item: item.relative_to(root).as_posix())
    ]


def _verify_runs(
    run_root: Path, track: Mapping[str, Any], track_audit: Mapping[str, Any]
) -> dict[str, Any]:
    for path in run_root.rglob("*"):
        if path.is_symlink():
            raise IncidentIntegrityError(f"runner input contains a symlink: {path}")

    case_paths = sorted(run_root.glob("r*/s*/cases/*.json"))
    attempt_paths = sorted(run_root.glob("r*/s*/attempts/*/attempt_*.json"))
    expected_cases = int(track_audit["expected_cases_per_run"])
    expected_runs = int(track_audit["expected_runs"])
    expected_total = expected_cases * expected_runs
    components = track_audit["component_hashes"]

    cases: dict[str, dict[str, Any]] = {}
    case_counts: Counter[int] = Counter()
    shard_case_counts: Counter[str] = Counter()
    run_ordinals: set[tuple[int, int]] = set()
    for path in case_paths:
        run_number, shard_number = _parse_run_shard(path, run_root)
        if run_number > expected_runs:
            raise IncidentIntegrityError(f"envelope belongs to unexpected run {run_number}")
        envelope = _read_json(path)
        if envelope.get("runner_version") != EXPECTED_RUNNER_VERSION:
            raise IncidentIntegrityError(f"wrong runner version: {path}")
        if envelope.get("status") != "VALID" or int(envelope.get("run_number") or 0) != run_number:
            raise IncidentIntegrityError(f"envelope is not VALID for its run: {path}")
        run_case_key = _require_sha256(envelope.get("run_case_key"), "run_case_key")
        if path.stem != run_case_key or run_case_key in cases:
            raise IncidentIntegrityError(f"duplicate or misnamed envelope: {path}")
        case_key = _require_sha256(envelope.get("case_key"), "case_key")
        if case_key != _expected_case_key(envelope):
            raise IncidentIntegrityError(f"envelope case_key mismatch: {path}")
        expected_run_key = canonical_sha256({"case_key": case_key, "run_number": run_number})
        if run_case_key != expected_run_key:
            raise IncidentIntegrityError(f"envelope run_case_key mismatch: {path}")
        if envelope.get("source_manifest_sha256") != track.get("packet_sha256"):
            raise IncidentIntegrityError(f"envelope source hash is outside the S1 track: {path}")
        if envelope.get("execution_contract_sha256") != track.get("execution_freeze_sha256"):
            raise IncidentIntegrityError(f"envelope execution freeze hash differs: {path}")
        for field, component in (
            ("protocol_sha256", "protocol"),
            ("prompt_sha256", "prompt"),
            ("schema_sha256", "schema"),
        ):
            if component not in components or envelope.get(field) != components[component]:
                raise IncidentIntegrityError(f"envelope {field} differs from frozen component: {path}")
        if envelope.get("model") != track.get("model") or envelope.get(
            "reasoning_effort"
        ) != track.get("reasoning_effort"):
            raise IncidentIntegrityError(f"envelope model/reasoning differs from track: {path}")
        output = envelope.get("output")
        reviewer_audit = envelope.get("reviewer_audit")
        if not isinstance(output, Mapping) or canonical_sha256(output) != envelope.get("output_sha256"):
            raise IncidentIntegrityError(f"envelope output hash mismatch: {path}")
        if not isinstance(reviewer_audit, Mapping) or canonical_sha256(
            reviewer_audit
        ) != envelope.get("reviewer_audit_sha256"):
            raise IncidentIntegrityError(f"envelope reviewer audit hash mismatch: {path}")
        _validate_semantic_attestation(output, str(path))
        ordinal_key = (run_number, int(envelope.get("source_ordinal")))
        if ordinal_key in run_ordinals:
            raise IncidentIntegrityError(f"duplicate source ordinal inside one run: {ordinal_key}")
        run_ordinals.add(ordinal_key)
        cases[run_case_key] = envelope
        case_counts[run_number] += 1
        shard_case_counts[f"r{run_number}/s{shard_number}"] += 1

    attempt_statuses: Counter[str] = Counter()
    attempt_counts: Counter[int] = Counter()
    shard_attempt_counts: Counter[str] = Counter()
    validated_attempts: dict[str, dict[str, Any]] = {}
    seen_attempt_identity: set[tuple[str, int]] = set()
    for path in attempt_paths:
        run_number, shard_number = _parse_run_shard(path, run_root)
        if run_number > expected_runs:
            raise IncidentIntegrityError(f"attempt belongs to unexpected run {run_number}")
        attempt = _read_json(path)
        status = str(attempt.get("status") or "")
        if attempt.get("runner_version") != EXPECTED_RUNNER_VERSION or status not in _ALLOWED_ATTEMPT_STATUSES:
            raise IncidentIntegrityError(f"invalid runner attempt status/version: {path}")
        if int(attempt.get("run_number") or 0) != run_number:
            raise IncidentIntegrityError(f"attempt run differs from directory: {path}")
        run_case_key = _require_sha256(attempt.get("run_case_key"), "attempt run_case_key")
        case_key = _require_sha256(attempt.get("case_key"), "attempt case_key")
        attempt_number = int(attempt.get("attempt") or 0)
        if attempt_number <= 0 or (run_case_key, attempt_number) in seen_attempt_identity:
            raise IncidentIntegrityError(f"duplicate or invalid attempt ordinal: {path}")
        seen_attempt_identity.add((run_case_key, attempt_number))
        if path.parent.name != run_case_key or path.stem != f"attempt_{attempt_number:03d}":
            raise IncidentIntegrityError(f"attempt path does not match its identity: {path}")
        if run_case_key != canonical_sha256({"case_key": case_key, "run_number": run_number}):
            raise IncidentIntegrityError(f"attempt run_case_key mismatch: {path}")
        if status == "VALIDATED":
            output = attempt.get("output")
            reviewer_audit = attempt.get("reviewer_audit")
            if not isinstance(output, Mapping) or canonical_sha256(output) != attempt.get("output_sha256"):
                raise IncidentIntegrityError(f"validated attempt output hash mismatch: {path}")
            if not isinstance(reviewer_audit, Mapping) or canonical_sha256(
                reviewer_audit
            ) != attempt.get("reviewer_audit_sha256"):
                raise IncidentIntegrityError(f"validated attempt reviewer audit hash mismatch: {path}")
            _validate_semantic_attestation(output, str(path))
            if run_case_key in validated_attempts:
                raise IncidentIntegrityError(f"multiple validated attempts for one case: {run_case_key}")
            validated_attempts[run_case_key] = attempt
        attempt_statuses[status] += 1
        attempt_counts[run_number] += 1
        shard_attempt_counts[f"r{run_number}/s{shard_number}"] += 1

    if set(cases) != set(validated_attempts):
        raise IncidentIntegrityError("VALID envelopes and VALIDATED attempts do not have exact one-to-one coverage")
    for run_case_key, envelope in cases.items():
        attempt = validated_attempts[run_case_key]
        if attempt.get("case_key") != envelope.get("case_key"):
            raise IncidentIntegrityError(f"attempt/envelope case_key mismatch: {run_case_key}")
        if int(attempt.get("attempt")) != int(envelope.get("successful_attempt")):
            raise IncidentIntegrityError(f"attempt/envelope successful ordinal mismatch: {run_case_key}")
        for field in ("output_sha256", "reviewer_audit_sha256"):
            if attempt.get(field) != envelope.get(field):
                raise IncidentIntegrityError(f"attempt/envelope {field} mismatch: {run_case_key}")

    if len(cases) >= expected_total:
        raise IncidentIntegrityError(
            "runner output is not partial; refusing to misclassify a complete/overfull run as aborted"
        )

    all_paths = [*case_paths, *attempt_paths]
    records = _tree_records(all_paths, run_root)
    return {
        "expected_validated_envelopes": expected_total,
        "validated_envelopes": len(cases),
        "validated_attempts": len(validated_attempts),
        "all_attempt_files": len(attempt_paths),
        "missing_validated_envelopes": expected_total - len(cases),
        "completion_rate": len(cases) / expected_total,
        "validated_envelopes_by_run": {
            str(run): case_counts.get(run, 0) for run in range(1, expected_runs + 1)
        },
        "attempt_files_by_run": {
            str(run): attempt_counts.get(run, 0) for run in range(1, expected_runs + 1)
        },
        "validated_envelopes_by_run_shard": dict(sorted(shard_case_counts.items())),
        "attempt_files_by_run_shard": dict(sorted(shard_attempt_counts.items())),
        "attempt_status_counts": dict(sorted(attempt_statuses.items())),
        "file_record_count": len(records),
        "file_records_sha256": canonical_sha256(records),
        "source_tree_snapshot_sha256": canonical_sha256(records),
        "file_records": records,
    }


def _render_markdown(report: Mapping[str, Any], json_file_sha256: str) -> str:
    counts = report["observed_runner_state"]
    lines = [
        "# MATURE_S1 技術事故封存",
        "",
        f"- 狀態：`{report['status']}`",
        "- 性質：技術稽核失敗事故；不是一致性、課程正確性或績效結論。",
        f"- 已驗證 envelopes：{counts['validated_envelopes']} / {counts['expected_validated_envelopes']}",
        f"- 已驗證 attempts：{counts['validated_attempts']}",
        f"- 缺少 envelopes：{counts['missing_validated_envelopes']}",
        f"- 各輪 envelopes：`{json.dumps(counts['validated_envelopes_by_run'], ensure_ascii=False, sort_keys=True)}`",
        "- 未來／績效已解封：否。",
        "- 策略或 gate 變更：否。",
        "- 正式重用：禁止；僅供技術鑑識。",
        "- 舊輸出：僅讀取，未刪除、未覆寫。",
        "",
        "## 原因",
        "",
    ]
    lines.extend(f"- `{reason}`" for reason in report["reason_codes"])
    lines.extend(
        [
            "",
            "## 完整性",
            "",
            f"- Incident logical SHA-256：`{report['incident_sha256']}`",
            f"- Incident JSON file SHA-256：`{json_file_sha256}`",
            f"- Runner source-tree SHA-256：`{counts['source_tree_snapshot_sha256']}`",
            f"- Track manifest file SHA-256：`{report['source_track']['research_track_manifest']['sha256']}`",
            "",
            "若需重新執行，必須建立新的 execution/run version；不得把本事故中的部分輸出併入正式三輪一致性 ledger。",
            "",
        ]
    )
    return "\n".join(lines)


def _exclusive_write(path: Path, payload: bytes) -> None:
    try:
        with path.open("xb") as handle:
            handle.write(payload)
            handle.flush()
    except FileExistsError as exc:
        raise IncidentIntegrityError(f"refusing to overwrite existing incident artifact: {path}") from exc


def build_incident(
    *, track_root: Path, run_root: Path, output_dir: Path, created_at_utc: str | None = None
) -> dict[str, Any]:
    """Validate old artifacts and exclusively create one forensic incident bundle."""

    track_root = _resolved_input_dir(track_root, "research_track_v1")
    run_root = _resolved_input_dir(run_root, "runner root")
    output_dir = Path(output_dir).resolve()
    if output_dir.exists():
        raise IncidentIntegrityError(f"refusing to reuse or overwrite output directory: {output_dir}")
    if _within(output_dir, track_root) or _within(output_dir, run_root):
        raise IncidentIntegrityError("incident output must not be inside either read-only input")
    if not output_dir.parent.is_dir():
        raise IncidentIntegrityError(f"incident output parent does not exist: {output_dir.parent}")

    track, track_audit = _verify_track(track_root)
    observed = _verify_runs(run_root, track, track_audit)

    # Re-scan immediately before publishing.  A concurrent input mutation fails
    # closed before the output directory is created.
    second_observed = _verify_runs(run_root, track, track_audit)
    if second_observed["source_tree_snapshot_sha256"] != observed["source_tree_snapshot_sha256"]:
        raise IncidentIntegrityError("runner inputs changed during incident audit")

    created = created_at_utc or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    report_core = {
        "incident_version": INCIDENT_VERSION,
        "status": INCIDENT_STATUS,
        "classification": "TECHNICAL_FORENSIC_ONLY_NOT_FORMAL_RESEARCH_OUTPUT",
        "created_at_utc": created,
        "target_scenario": "MATURE_TREND_PULLBACK",
        "target_route": "V2_CORE",
        "performance_unsealed": False,
        "strategy_change": False,
        "formal_reuse_allowed": False,
        "resume_authority_granted": False,
        "outputs_preserved_for_forensic_audit_only": True,
        "old_inputs_read_only": True,
        "old_outputs_deleted": False,
        "old_outputs_overwritten": False,
        "source_track_root": str(track_root),
        "source_run_root": str(run_root),
        "source_track": track_audit,
        "observed_runner_state": observed,
        "reason_codes": [
            "PARTIAL_REPEATABILITY_OUTPUT",
            "EXACT_CASE_X_RUN_COVERAGE_NOT_MET",
            "TECHNICAL_EXECUTION_AUDIT_FAILED",
            "VALIDATED_PARTIAL_OUTPUTS_FORENSIC_ONLY_NOT_FORMALLY_REUSABLE",
        ],
        "required_next_action": (
            "CREATE_A_NEW_VERSIONED_EXECUTION; DO_NOT_MERGE_OR_RESUME_THIS_PARTIAL_RUN_AS_FORMAL"
        ),
        "generator": {
            "relative_path": Path(__file__).resolve().relative_to(ROOT).as_posix(),
            "sha256": file_sha256(Path(__file__).resolve()),
        },
    }
    report = {**report_core, "incident_sha256": canonical_sha256(report_core)}
    json_payload = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8") + b"\n"
    json_file_sha256 = hashlib.sha256(json_payload).hexdigest()
    markdown_payload = _render_markdown(report, json_file_sha256).encode("utf-8")

    output_dir.mkdir(exist_ok=False)
    _exclusive_write(output_dir / "incident.json", json_payload)
    _exclusive_write(output_dir / "incident.md", markdown_payload)
    return {
        "status": report["status"],
        "validated_envelopes": observed["validated_envelopes"],
        "validated_attempts": observed["validated_attempts"],
        "incident_sha256": report["incident_sha256"],
        "incident_json_sha256": json_file_sha256,
        "incident_markdown_sha256": hashlib.sha256(markdown_payload).hexdigest(),
        "output_dir": str(output_dir),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--track-root", type=Path, default=DEFAULT_TRACK_ROOT)
    parser.add_argument("--run-root", type=Path, default=DEFAULT_RUN_ROOT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    result = build_incident(
        track_root=args.track_root,
        run_root=args.run_root,
        output_dir=args.output_dir,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
