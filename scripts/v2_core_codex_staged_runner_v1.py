"""Immutable, budget-guarded Codex runner for one M2A stage/shard.

The runner invokes exactly ``gpt-5.6-sol`` with ``xhigh`` reasoning, validates
the raw structured output, and never repairs model fields.  It does not know
or open sealed calibration labels.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any, Callable

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.v2_core_source_audit_v1 import sha256
from scripts.v2_core_staged_output_validator_v1 import (
    load_json,
    validate_stage_b,
    validate_stage_d,
)


OUT = ROOT / "reports" / "course_backtest" / "2026-09-10" / "v2_core_reproducible_goal_v1"
RUNNER_VERSION = "v2-core-codex-staged-runner-r4"
MODEL = "gpt-5.6-sol"
REASONING = "xhigh"
STAGE_B_PROMPT_FILENAME = "v2_core_stage_b.prompt.candidate_r2.md"
STAGE_D_PROMPT_FILENAME = "v2_core_stage_d.prompt.candidate_r2.md"
USAGE_SOURCE = "CODEX_APP_GET_USAGE_LIMITS"
MAX_USAGE_AGE_SECONDS = 300
FORBIDDEN_PACKET_KEYS = {
    "code",
    "name",
    "symbol",
    "case_role",
    "intended_scenario",
    "expected_permission",
    "legacy_v2_trigger",
    "legacy_v2_no_trade_reason",
    "future_outcome",
    "mfe",
    "mae",
    "pnl",
    "profit",
    "return_pct",
    "exit_date",
    "exit_price",
}
RunCommand = Callable[..., subprocess.CompletedProcess[str]]


class StagedRunnerError(RuntimeError):
    pass


def canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def write_new_or_identical(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_bytes() != payload:
        raise StagedRunnerError(f"refusing to overwrite immutable output: {path}")
    path.write_bytes(payload)


def raw_output_path(output_path: Path) -> Path:
    return output_path.with_name(output_path.stem + ".raw.json")


def invalid_output_path(output_path: Path) -> Path:
    return output_path.with_name(output_path.stem + ".invalid.raw")


def parse_usage_attestation(path: Path, *, now: datetime | None = None) -> dict[str, Any]:
    value = load_json(path)
    required = {"source", "limit_id", "used_percent", "remaining_percent", "checked_at", "check_id"}
    if set(value) != required:
        raise StagedRunnerError("usage attestation fields differ from frozen contract")
    if value["source"] != USAGE_SOURCE or value["limit_id"] != "codex":
        raise StagedRunnerError("usage attestation source/limit is not Codex primary")
    used = value["used_percent"]
    remaining = value["remaining_percent"]
    if isinstance(used, bool) or isinstance(remaining, bool):
        raise StagedRunnerError("usage percentage must be numeric")
    if not isinstance(used, (int, float)) or not isinstance(remaining, (int, float)):
        raise StagedRunnerError("usage percentage must be numeric")
    if not 0 <= float(used) <= 100 or not 0 <= float(remaining) <= 100:
        raise StagedRunnerError("usage percentage outside 0..100")
    if abs(float(used) + float(remaining) - 100.0) > 0.001:
        raise StagedRunnerError("used and remaining percentages do not sum to 100")
    checked = str(value["checked_at"])
    try:
        timestamp = datetime.fromisoformat(
            checked[:-1] + "+00:00" if checked.endswith("Z") else checked
        )
    except ValueError as exc:
        raise StagedRunnerError("usage checked_at is not ISO-8601") from exc
    if timestamp.tzinfo is None:
        raise StagedRunnerError("usage checked_at must include timezone")
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    age = (current - timestamp.astimezone(timezone.utc)).total_seconds()
    if age < -30 or age > MAX_USAGE_AGE_SECONDS:
        raise StagedRunnerError("usage attestation is stale or from the future")
    if not str(value["check_id"]).strip():
        raise StagedRunnerError("usage check_id is empty")
    return value


def assert_budget_allows(attestation: dict[str, Any], threshold: float) -> None:
    if float(attestation["remaining_percent"]) <= float(threshold):
        raise StagedRunnerError("BUDGET_STOP（額度暫停）")


def strict_transport_schema(
    local_schema: dict[str, Any],
    *,
    evidence_ref_values: list[str],
    primary_scenario: str | None = None,
) -> dict[str, Any]:
    schema = json.loads(json.dumps(local_schema))
    if primary_scenario is not None:
        ref_by_scenario = {
            "MATURE_TREND_PULLBACK": "matureEvaluation",
            "MACRO_COPY_RESONANCE": "macroEvaluation",
            "BEAR_REVERSAL_LEFT_RIGHT": "bearEvaluation",
            "FRESH_Q1_EXPANSION": "freshEvaluation",
        }
        try:
            ref = ref_by_scenario[primary_scenario]
        except KeyError as exc:
            raise StagedRunnerError(f"unsupported primary scenario: {primary_scenario}") from exc
        schema["properties"]["scenario_evaluation"] = {"$ref": f"#/$defs/{ref}"}
        schema["properties"]["primary_scenario"] = {
            "type": "string",
            "const": primary_scenario,
        }
        schema.pop("allOf", None)
        required_defs = {
            "sha256",
            "evidenceRef",
            "evidenceRefs",
            "missingEvidenceCodes",
            "atomicVerdict",
            ref,
            "trigger",
            "causalAttestation",
        }
        schema["$defs"] = {
            name: value
            for name, value in schema["$defs"].items()
            if name in required_defs
        }
    schema["$defs"]["evidenceRef"] = {
        "type": "string",
        "enum": sorted(evidence_ref_values),
    }
    unsupported = {"$id", "format", "uniqueItems", "allOf", "anyOf", "oneOf", "not", "if", "then", "else"}

    def clean(value: Any) -> Any:
        if isinstance(value, list):
            return [clean(item) for item in value]
        if not isinstance(value, dict):
            return value
        output: dict[str, Any] = {}
        for key, child in value.items():
            if key in unsupported or key.startswith("x-"):
                continue
            output[key] = clean(child)
        if "type" not in output and "const" in output:
            constant = output["const"]
            output["type"] = "boolean" if isinstance(constant, bool) else "number" if isinstance(constant, (int, float)) else "string"
        if "type" not in output and output.get("enum") and all(
            isinstance(item, str) for item in output["enum"]
        ):
            output["type"] = "string"
        if output.get("type") == "object" and isinstance(output.get("properties"), dict):
            output["required"] = list(output["properties"])
            output["additionalProperties"] = False
        return output

    transport = clean(schema)
    Draft202012Validator.check_schema(transport)
    return transport


def compact_packet(packet: dict[str, Any]) -> dict[str, Any]:
    for forbidden in FORBIDDEN_PACKET_KEYS:
        if forbidden in json.dumps(packet, ensure_ascii=False).lower():
            raise StagedRunnerError(f"forbidden packet key visible: {forbidden}")
    daily = packet["daily_structure_context_to_as_of"]
    columns = [
        "date",
        "open",
        "high",
        "low",
        "close",
        "adjustment_factor",
        "ma5",
        "ma13",
        "ma21",
        "ma55",
        "ma105",
        "ma144",
        "macd_hist",
        "macd_hist_delta",
        "atr14",
        "return_1d_pct",
        "volume_ratio_20",
        "small_pivot_high_date",
        "small_pivot_high",
        "small_pivot_low_date",
        "small_pivot_low",
        "large_pivot_high_date",
        "large_pivot_high",
        "large_pivot_low_date",
        "large_pivot_low",
        "facts",
    ]
    compact_daily = [[row.get(column) for column in columns] for row in daily]
    pivot_refs = {
        item["path"]: item["ref"]
        for item in packet["evidence_catalog"]
        if item["kind"] == "PIVOT"
    }
    cycle_refs = {
        item["path"]: item["ref"]
        for item in packet["evidence_catalog"]
        if item["kind"] == "MACD"
    }
    return {
        "packet_version": packet["packet_version"],
        "review_id": packet["review_id"],
        "anonymous_stock_id": packet["anonymous_stock_id"],
        "as_of": packet["as_of"],
        "data_quality": packet["data_quality"],
        "proxy_evidence": packet["proxy_evidence"],
        "completed_macd_cycles": [
            {"ref": cycle_refs[f"/completed_macd_21_55_55_cycles_to_as_of/{index}"], **cycle}
            for index, cycle in enumerate(packet["completed_macd_21_55_55_cycles_to_as_of"])
        ],
        "confirmed_pivots": [
            {"ref": pivot_refs[f"/confirmed_pivots_to_as_of/{index}"], **pivot}
            for index, pivot in enumerate(packet["confirmed_pivots_to_as_of"])
        ],
        "daily_columns": columns,
        "daily_rows": compact_daily,
        "price_ref_rule": "Each daily row is cited as PRICE:<date> using the date in its first column.",
        "quality_ref": "QUALITY:SUMMARY",
        "corporate_action_refs": [
            item for item in packet["evidence_catalog"] if item["kind"] == "ACTION"
        ],
        "review_constraints": packet["review_constraints"],
    }


def selected_stage_d_prompt(prompt_text: str, primary_scenario: str) -> str:
    marker = "## 情境模板"
    ai_marker = "## AI建議權限"
    if marker not in prompt_text or ai_marker not in prompt_text:
        raise StagedRunnerError("STAGE_D prompt template markers missing")
    prefix = prompt_text.split(marker, 1)[0]
    templates_and_tail = prompt_text.split(marker, 1)[1]
    templates, tail = templates_and_tail.split(ai_marker, 1)
    start = f"### `{primary_scenario}`"
    if start not in templates:
        raise StagedRunnerError(f"STAGE_D scenario template missing: {primary_scenario}")
    section = templates.split(start, 1)[1]
    section = section.split("\n### `", 1)[0]
    return prefix.rstrip() + "\n\n" + start + section.rstrip() + "\n\n" + ai_marker + tail


def rendered_prompt(
    *,
    stage: str,
    prompt_text: str,
    packet: dict[str, Any],
    packet_path: Path,
    schema_path: Path,
    prompt_source_sha256: str,
    stage_b_output: dict[str, Any] | None = None,
    stage_b_path: Path | None = None,
    truth_table_path: Path | None = None,
    primary_scenario: str | None = None,
) -> str:
    metadata = {
        "schema_version": "v2-core-stage-b-r1-candidate" if stage == "stage_b" else "v2-core-stage-d-r1-candidate",
        "review_id": packet["review_id"],
        "anonymous_stock_id": packet["anonymous_stock_id"],
        "as_of": packet["as_of"],
        "packet_sha256": sha256(packet_path),
        "prompt_sha256": prompt_source_sha256,
        "schema_sha256": sha256(schema_path),
        "model": MODEL,
        "reasoning_effort": REASONING,
        "independent_review": False,
    }
    payload: dict[str, Any] = {"required_output_metadata": metadata, "packet": compact_packet(packet)}
    if stage == "stage_d":
        if stage_b_output is None or stage_b_path is None or truth_table_path is None or primary_scenario is None:
            raise StagedRunnerError("STAGE_D requires Stage B, truth table, and primary scenario")
        metadata["stage_b_output_sha256"] = sha256(stage_b_path)
        metadata["truth_table_sha256"] = sha256(truth_table_path)
        payload["program_primary_scenario"] = primary_scenario
        payload["stage_b_output"] = stage_b_output
    return prompt_text.rstrip() + "\n\n以下是唯一允許使用的匿名AS-OF輸入。只輸出Schema JSON。\n" + json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )


def call_codex(
    *,
    prompt: str,
    transport_schema: dict[str, Any],
    timeout_seconds: int,
    invalid_raw_path: Path | None = None,
    run_command: RunCommand = subprocess.run,
) -> tuple[dict[str, Any], dict[str, Any], bytes]:
    with tempfile.TemporaryDirectory(prefix="v2-core-m2a-") as temporary:
        isolated = Path(temporary)
        schema_path = isolated / "output_schema.json"
        output_path = isolated / "last_message.json"
        schema_bytes = canonical_bytes(transport_schema)
        schema_path.write_bytes(schema_bytes)
        command = [
            "codex",
            "exec",
            "-",
            "--model",
            MODEL,
            "-c",
            f'model_reasoning_effort="{REASONING}"',
            "--sandbox",
            "read-only",
            "--ephemeral",
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
            str(isolated),
        ]
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["NO_COLOR"] = "1"
        completed = run_command(
            command,
            input=prompt,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
            env=env,
        )
        raw_bytes = output_path.read_bytes() if output_path.is_file() else b""
        if completed.returncode != 0:
            if raw_bytes and invalid_raw_path is not None:
                write_new_or_identical(invalid_raw_path, raw_bytes)
            detail = ((completed.stderr or "") + "\n" + (completed.stdout or ""))[-4000:]
            raise StagedRunnerError(f"Codex exited {completed.returncode}: {detail}")
        if not output_path.is_file():
            raise StagedRunnerError("Codex did not publish last-message JSON")
        try:
            output = json.loads(raw_bytes.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            if invalid_raw_path is not None:
                write_new_or_identical(invalid_raw_path, raw_bytes)
            raise StagedRunnerError("model output is not valid UTF-8 JSON") from exc
        transport_errors = sorted(
            error.message
            for error in Draft202012Validator(transport_schema).iter_errors(output)
        )
        if transport_errors:
            if invalid_raw_path is not None:
                write_new_or_identical(invalid_raw_path, raw_bytes)
            raise StagedRunnerError("model transport output invalid: " + "; ".join(transport_errors))
        receipt = {
            "runner_version": RUNNER_VERSION,
            "model": MODEL,
            "reasoning_effort": REASONING,
            "rendered_prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "transport_schema_sha256": hashlib.sha256(schema_bytes).hexdigest(),
            "raw_output_sha256": hashlib.sha256(raw_bytes).hexdigest(),
            "normalized_output_sha256": canonical_sha256(output),
        }
        return output, receipt, raw_bytes


def build_route_record(
    packet: dict[str, Any],
    stage_b_path: Path,
    truth_table_path: Path,
    stage_b_validation: dict[str, Any],
) -> dict[str, Any]:
    return {
        "review_id": packet["review_id"],
        "as_of": packet["as_of"],
        "stage_b_output_sha256": sha256(stage_b_path),
        "program_derived_scenario": str(stage_b_validation["program_derived_scenario"]),
        "routing_status": stage_b_validation["routing_status"],
        "ai_recommended_scenario": stage_b_validation["ai_recommended_scenario"],
        "recommendation_matches_program": stage_b_validation[
            "recommendation_matches_program"
        ],
        "truth_table_sha256": sha256(truth_table_path),
    }


def build_unresolved_permission(
    packet: dict[str, Any],
    truth_table_path: Path,
    stage_b_validation: dict[str, Any],
) -> dict[str, Any]:
    return {
        "review_id": packet["review_id"],
        "as_of": packet["as_of"],
        "program_derived_permission": "UNKNOWN",
        "permission_reasons": [stage_b_validation["routing_status"]],
        "stage_d_called": False,
        "truth_table_sha256": sha256(truth_table_path),
    }


def build_resolved_permission(
    packet: dict[str, Any],
    scenario: str,
    output_path: Path,
    truth_table_path: Path,
    validation: dict[str, Any],
) -> dict[str, Any]:
    return {
        "review_id": packet["review_id"],
        "as_of": packet["as_of"],
        "program_derived_scenario": scenario,
        "program_derived_permission": validation["program_derived_permission"],
        "permission_reasons": validation["permission_reasons"],
        "ai_recommended_permission": validation["ai_recommended_permission"],
        "recommendation_matches_program": validation[
            "recommendation_matches_program"
        ],
        "stage_d_called": True,
        "stage_d_output_sha256": sha256(output_path),
        "truth_table_sha256": sha256(truth_table_path),
    }


def verify_execution_manifest(path: Path, artifact_dir: Path) -> dict[str, Any]:
    manifest = load_json(path)
    if manifest.get("formal_model") != MODEL or manifest.get("reasoning_effort") != REASONING:
        raise StagedRunnerError("execution manifest model/effort mismatch")
    if manifest.get("budget_stop_remaining_percent_lte") != 5:
        raise StagedRunnerError("execution manifest budget threshold mismatch")
    run_directory = manifest.get("run_directory")
    if not isinstance(run_directory, str) or not run_directory.startswith("feasibility_probe_runs_r"):
        raise StagedRunnerError("execution manifest run directory is not version isolated")
    for filename, expected in (manifest.get("components") or {}).items():
        component = artifact_dir / filename
        if not component.is_file() or sha256(component) != expected:
            raise StagedRunnerError(f"execution component changed: {filename}")
    for filename, expected in (manifest.get("repo_components") or {}).items():
        component = ROOT / filename
        if not component.is_file() or sha256(component) != expected:
            raise StagedRunnerError(f"execution repo component changed: {filename}")
    return manifest


def run_stage_b_case(
    *,
    packet_path: Path,
    schema_path: Path,
    prompt_path: Path,
    truth_table_path: Path,
    output_path: Path,
    receipt_path: Path,
    timeout_seconds: int,
    run_command: RunCommand = subprocess.run,
) -> dict[str, Any]:
    packet = load_json(packet_path)
    local_schema = load_json(schema_path)
    refs = [str(item["ref"]) for item in packet["evidence_catalog"]]
    transport = strict_transport_schema(local_schema, evidence_ref_values=refs)
    prompt_text = prompt_path.read_text(encoding="utf-8")
    prompt = rendered_prompt(
        stage="stage_b",
        prompt_text=prompt_text,
        packet=packet,
        packet_path=packet_path,
        schema_path=schema_path,
        prompt_source_sha256=sha256(prompt_path),
    )
    output, receipt, raw_bytes = call_codex(
        prompt=prompt,
        transport_schema=transport,
        timeout_seconds=timeout_seconds,
        invalid_raw_path=invalid_output_path(output_path),
        run_command=run_command,
    )
    validation = validate_stage_b(
        output,
        packet_path=packet_path,
        schema_path=schema_path,
        prompt_path=prompt_path,
        truth_table_path=truth_table_path,
    )
    if validation["status"] != "PASS":
        write_new_or_identical(invalid_output_path(output_path), raw_bytes)
        raise StagedRunnerError("STAGE_B local validation failed: " + json.dumps(validation, ensure_ascii=False))
    write_new_or_identical(raw_output_path(output_path), raw_bytes)
    write_new_or_identical(output_path, canonical_bytes(output))
    write_new_or_identical(
        receipt_path,
        canonical_bytes({**receipt, "stage": "STAGE_B", "validation": validation}),
    )
    return validation


def validate_existing_stage_b_artifacts(
    *,
    packet_path: Path,
    schema_path: Path,
    prompt_path: Path,
    truth_table_path: Path,
    output_path: Path,
    receipt_path: Path,
) -> dict[str, Any]:
    if not output_path.is_file() or not receipt_path.is_file():
        raise StagedRunnerError("existing STAGE_B output lacks receipt")
    raw_path = raw_output_path(output_path)
    if not raw_path.is_file():
        raise StagedRunnerError("existing STAGE_B raw output is missing")
    output = load_json(output_path)
    validation = validate_stage_b(
        output,
        packet_path=packet_path,
        schema_path=schema_path,
        prompt_path=prompt_path,
        truth_table_path=truth_table_path,
    )
    if validation["status"] != "PASS":
        raise StagedRunnerError("existing STAGE_B output is invalid")
    receipt = load_json(receipt_path)
    receipt_checks = {
        "runner_version": RUNNER_VERSION,
        "model": MODEL,
        "reasoning_effort": REASONING,
        "stage": "STAGE_B",
        "raw_output_sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
        "normalized_output_sha256": canonical_sha256(output),
    }
    for key, expected in receipt_checks.items():
        if receipt.get(key) != expected:
            raise StagedRunnerError(f"existing STAGE_B receipt mismatch: {key}")
    if receipt.get("validation") != validation:
        raise StagedRunnerError("existing STAGE_B receipt validation mismatch")
    return validation


def run_stage_d_case(
    *,
    packet_path: Path,
    stage_b_path: Path,
    stage_b_schema_path: Path,
    stage_b_prompt_path: Path,
    schema_path: Path,
    prompt_path: Path,
    truth_table_path: Path,
    output_path: Path,
    route_path: Path,
    permission_path: Path,
    receipt_path: Path,
    timeout_seconds: int,
    run_command: RunCommand = subprocess.run,
) -> dict[str, Any]:
    packet = load_json(packet_path)
    stage_b = load_json(stage_b_path)
    stage_b_validation = validate_stage_b(
        stage_b,
        packet_path=packet_path,
        schema_path=stage_b_schema_path,
        prompt_path=stage_b_prompt_path,
        truth_table_path=truth_table_path,
    )
    if stage_b_validation["status"] != "PASS":
        raise StagedRunnerError("STAGE_D dependency STAGE_B is invalid")
    scenario = str(stage_b_validation["program_derived_scenario"])
    route_record = build_route_record(
        packet, stage_b_path, truth_table_path, stage_b_validation
    )
    write_new_or_identical(route_path, canonical_bytes(route_record))
    if scenario == "UNRESOLVED_NO_TRADE":
        permission = build_unresolved_permission(
            packet, truth_table_path, stage_b_validation
        )
        write_new_or_identical(permission_path, canonical_bytes(permission))
        return {
            "status": "PROGRAM_UNKNOWN_NO_STAGE_D_CALL",
            "program_derived_scenario": scenario,
            "program_derived_permission": "UNKNOWN",
        }

    local_schema = load_json(schema_path)
    refs = [str(item["ref"]) for item in packet["evidence_catalog"]]
    transport = strict_transport_schema(
        local_schema,
        evidence_ref_values=refs,
        primary_scenario=scenario,
    )
    full_prompt_text = prompt_path.read_text(encoding="utf-8")
    prompt_text = selected_stage_d_prompt(full_prompt_text, scenario)
    prompt = rendered_prompt(
        stage="stage_d",
        prompt_text=prompt_text,
        packet=packet,
        packet_path=packet_path,
        schema_path=schema_path,
        prompt_source_sha256=sha256(prompt_path),
        stage_b_output=stage_b,
        stage_b_path=stage_b_path,
        truth_table_path=truth_table_path,
        primary_scenario=scenario,
    )
    output, receipt, raw_bytes = call_codex(
        prompt=prompt,
        transport_schema=transport,
        timeout_seconds=timeout_seconds,
        invalid_raw_path=invalid_output_path(output_path),
        run_command=run_command,
    )
    validation = validate_stage_d(
        output,
        packet_path=packet_path,
        stage_b_path=stage_b_path,
        stage_b_schema_path=stage_b_schema_path,
        stage_b_prompt_path=stage_b_prompt_path,
        schema_path=schema_path,
        prompt_path=prompt_path,
        truth_table_path=truth_table_path,
    )
    if validation["status"] != "PASS":
        write_new_or_identical(invalid_output_path(output_path), raw_bytes)
        raise StagedRunnerError(
            "STAGE_D local validation failed: "
            + json.dumps(validation, ensure_ascii=False)
        )
    write_new_or_identical(raw_output_path(output_path), raw_bytes)
    write_new_or_identical(output_path, canonical_bytes(output))
    write_new_or_identical(
        receipt_path,
        canonical_bytes(
            {
                **receipt,
                "stage": "STAGE_D",
                "selected_primary_scenario": scenario,
                "source_prompt_sha256": sha256(prompt_path),
                "validation": validation,
            }
        ),
    )
    permission = build_resolved_permission(
        packet, scenario, output_path, truth_table_path, validation
    )
    write_new_or_identical(permission_path, canonical_bytes(permission))
    return {
        "status": "COMPLETED",
        "program_derived_scenario": scenario,
        "program_derived_permission": validation["program_derived_permission"],
    }


def validate_existing_stage_d_artifacts(
    *,
    packet_path: Path,
    stage_b_path: Path,
    stage_b_schema_path: Path,
    stage_b_prompt_path: Path,
    schema_path: Path,
    prompt_path: Path,
    truth_table_path: Path,
    output_path: Path,
    route_path: Path,
    permission_path: Path,
    receipt_path: Path,
) -> dict[str, Any]:
    packet = load_json(packet_path)
    stage_b = load_json(stage_b_path)
    stage_b_validation = validate_stage_b(
        stage_b,
        packet_path=packet_path,
        schema_path=stage_b_schema_path,
        prompt_path=stage_b_prompt_path,
        truth_table_path=truth_table_path,
    )
    if stage_b_validation["status"] != "PASS":
        raise StagedRunnerError("existing STAGE_D has invalid STAGE_B dependency")
    scenario = str(stage_b_validation["program_derived_scenario"])
    expected_route = build_route_record(
        packet, stage_b_path, truth_table_path, stage_b_validation
    )
    if not route_path.is_file() or load_json(route_path) != expected_route:
        raise StagedRunnerError("existing STAGE_D program route is missing or inconsistent")
    if scenario == "UNRESOLVED_NO_TRADE":
        expected_permission = build_unresolved_permission(
            packet, truth_table_path, stage_b_validation
        )
        if load_json(permission_path) != expected_permission:
            raise StagedRunnerError("existing unresolved permission is inconsistent")
        if output_path.exists() or receipt_path.exists():
            raise StagedRunnerError("unresolved route must not have STAGE_D AI artifacts")
        return {
            "status": "RESUMED_VALID_EXISTING",
            "program_derived_scenario": scenario,
            "program_derived_permission": "UNKNOWN",
        }
    if not output_path.is_file() or not receipt_path.is_file():
        raise StagedRunnerError("existing resolved permission lacks STAGE_D artifacts")
    raw_path = raw_output_path(output_path)
    if not raw_path.is_file():
        raise StagedRunnerError("existing STAGE_D raw output is missing")
    output = load_json(output_path)
    validation = validate_stage_d(
        output,
        packet_path=packet_path,
        stage_b_path=stage_b_path,
        stage_b_schema_path=stage_b_schema_path,
        stage_b_prompt_path=stage_b_prompt_path,
        schema_path=schema_path,
        prompt_path=prompt_path,
        truth_table_path=truth_table_path,
    )
    if validation["status"] != "PASS":
        raise StagedRunnerError("existing STAGE_D output is invalid")
    expected_permission = build_resolved_permission(
        packet, scenario, output_path, truth_table_path, validation
    )
    if load_json(permission_path) != expected_permission:
        raise StagedRunnerError("existing STAGE_D permission is inconsistent")
    receipt = load_json(receipt_path)
    receipt_checks = {
        "runner_version": RUNNER_VERSION,
        "model": MODEL,
        "reasoning_effort": REASONING,
        "stage": "STAGE_D",
        "selected_primary_scenario": scenario,
        "source_prompt_sha256": sha256(prompt_path),
        "raw_output_sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
        "normalized_output_sha256": canonical_sha256(output),
    }
    for key, expected in receipt_checks.items():
        if receipt.get(key) != expected:
            raise StagedRunnerError(f"existing STAGE_D receipt mismatch: {key}")
    if receipt.get("validation") != validation:
        raise StagedRunnerError("existing STAGE_D receipt validation mismatch")
    return {
        "status": "RESUMED_VALID_EXISTING",
        "program_derived_scenario": scenario,
        "program_derived_permission": validation["program_derived_permission"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-dir", type=Path, default=OUT)
    parser.add_argument("--execution-manifest", type=Path, required=True)
    parser.add_argument("--usage-attestation", type=Path, required=True)
    parser.add_argument("--round", type=int, choices=(1, 2, 3), required=True)
    parser.add_argument("--shard-id", type=int, required=True)
    parser.add_argument("--stage", choices=("stage_b", "stage_d"), required=True)
    parser.add_argument("--timeout-seconds", type=int, default=1200)
    args = parser.parse_args()
    artifact_dir = args.artifact_dir.resolve()
    manifest = verify_execution_manifest(args.execution_manifest.resolve(), artifact_dir)
    usage = parse_usage_attestation(args.usage_attestation.resolve())
    assert_budget_allows(usage, float(manifest["budget_stop_remaining_percent_lte"]))
    shards = [row for row in manifest["shards"] if int(row["shard_id"]) == args.shard_id]
    if len(shards) != 1:
        raise StagedRunnerError("shard missing or duplicated")
    case_map = {row["review_id"]: row for row in manifest["cases"]}
    probe = load_json(artifact_dir / "feasibility_probe_manifest.json")
    packet_dir = artifact_dir / str(probe["packet_directory"])
    output_root = artifact_dir / str(manifest["run_directory"]) / f"round_{args.round}"
    completed = []
    for review_id in shards[0]["review_ids"]:
        row = case_map[review_id]
        packet_path = packet_dir / row["packet_file"]
        if sha256(packet_path) != row["packet_sha256"]:
            raise StagedRunnerError(f"packet changed: {review_id}")
        stage_b_path = output_root / "stage_b" / f"{review_id}.json"
        if args.stage == "stage_b":
            receipt_path = output_root / "receipts" / f"{review_id}.stage_b.json"
            if stage_b_path.exists():
                validation = validate_existing_stage_b_artifacts(
                    packet_path=packet_path,
                    schema_path=artifact_dir / "v2_core_stage_b.schema.candidate.json",
                    prompt_path=artifact_dir / STAGE_B_PROMPT_FILENAME,
                    truth_table_path=artifact_dir / "permission_truth_table.json",
                    output_path=stage_b_path,
                    receipt_path=receipt_path,
                )
                completed.append({"review_id": review_id, "status": "RESUMED_VALID_EXISTING"})
                continue
            validation = run_stage_b_case(
                packet_path=packet_path,
                schema_path=artifact_dir / "v2_core_stage_b.schema.candidate.json",
                prompt_path=artifact_dir / STAGE_B_PROMPT_FILENAME,
                truth_table_path=artifact_dir / "permission_truth_table.json",
                output_path=stage_b_path,
                receipt_path=receipt_path,
                timeout_seconds=args.timeout_seconds,
            )
            completed.append({"review_id": review_id, "status": "COMPLETED", "validation": validation})
            continue

        if not stage_b_path.is_file():
            raise StagedRunnerError(f"STAGE_D requires completed STAGE_B: {review_id}")
        stage_d_path = output_root / "stage_d" / f"{review_id}.json"
        route_path = output_root / "program_route" / f"{review_id}.json"
        permission_path = output_root / "final_permission" / f"{review_id}.json"
        receipt_path = output_root / "receipts" / f"{review_id}.stage_d.json"
        if permission_path.exists():
            resumed = validate_existing_stage_d_artifacts(
                packet_path=packet_path,
                stage_b_path=stage_b_path,
                stage_b_schema_path=artifact_dir / "v2_core_stage_b.schema.candidate.json",
                stage_b_prompt_path=artifact_dir / STAGE_B_PROMPT_FILENAME,
                schema_path=artifact_dir / "v2_core_stage_d.schema.candidate.json",
                prompt_path=artifact_dir / STAGE_D_PROMPT_FILENAME,
                truth_table_path=artifact_dir / "permission_truth_table.json",
                output_path=stage_d_path,
                route_path=route_path,
                permission_path=permission_path,
                receipt_path=receipt_path,
            )
            completed.append({"review_id": review_id, **resumed})
            continue
        result = run_stage_d_case(
            packet_path=packet_path,
            stage_b_path=stage_b_path,
            stage_b_schema_path=artifact_dir / "v2_core_stage_b.schema.candidate.json",
            stage_b_prompt_path=artifact_dir / STAGE_B_PROMPT_FILENAME,
            schema_path=artifact_dir / "v2_core_stage_d.schema.candidate.json",
            prompt_path=artifact_dir / STAGE_D_PROMPT_FILENAME,
            truth_table_path=artifact_dir / "permission_truth_table.json",
            output_path=stage_d_path,
            route_path=route_path,
            permission_path=permission_path,
            receipt_path=receipt_path,
            timeout_seconds=args.timeout_seconds,
        )
        completed.append({"review_id": review_id, **result})
    print(json.dumps({"status": "SHARD_COMPLETE", "round": args.round, "shard_id": args.shard_id, "cases": completed}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
