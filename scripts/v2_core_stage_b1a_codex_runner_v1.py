"""Immutable, usage-guarded Codex runner for Stage B1a candidate R1.

This runner invokes only gpt-5.6-sol/xhigh.  It preserves raw model semantics,
performs no repair, and binds every output to an anonymous AS-OF input packet,
prompt, schema, and source catalogs.
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

from scripts.v2_core_stage_b1a_candidate_validator_v1 import (
    load_json,
    sha256_file,
    validate_stage_b1a_response,
)


RUNNER_VERSION = "v2-core-stage-b1a-codex-runner-r1-candidate"
MODEL = "gpt-5.6-sol"
REASONING = "xhigh"
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


class StageB1aRunnerError(RuntimeError):
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
        raise StageB1aRunnerError(f"refusing to overwrite immutable output: {path}")
    path.write_bytes(payload)


def raw_output_path(output_path: Path) -> Path:
    return output_path.with_name(output_path.stem + ".raw.json")


def invalid_output_path(output_path: Path) -> Path:
    return output_path.with_name(output_path.stem + ".invalid.raw")


def parse_usage_attestation(path: Path, *, now: datetime | None = None) -> dict[str, Any]:
    value = load_json(path)
    required = {
        "source",
        "limit_id",
        "used_percent",
        "remaining_percent",
        "checked_at",
        "check_id",
    }
    if set(value) != required:
        raise StageB1aRunnerError("usage attestation fields differ from contract")
    if value["source"] != USAGE_SOURCE or value["limit_id"] != "codex":
        raise StageB1aRunnerError("usage attestation source/limit is not Codex primary")
    used = value["used_percent"]
    remaining = value["remaining_percent"]
    if isinstance(used, bool) or isinstance(remaining, bool):
        raise StageB1aRunnerError("usage percentage must be numeric")
    if not isinstance(used, (int, float)) or not isinstance(remaining, (int, float)):
        raise StageB1aRunnerError("usage percentage must be numeric")
    if not 0 <= float(used) <= 100 or not 0 <= float(remaining) <= 100:
        raise StageB1aRunnerError("usage percentage outside 0..100")
    if abs(float(used) + float(remaining) - 100.0) > 0.001:
        raise StageB1aRunnerError("used and remaining percentages do not sum to 100")
    checked = str(value["checked_at"])
    try:
        timestamp = datetime.fromisoformat(
            checked[:-1] + "+00:00" if checked.endswith("Z") else checked
        )
    except ValueError as exc:
        raise StageB1aRunnerError("usage checked_at is not ISO-8601") from exc
    if timestamp.tzinfo is None:
        raise StageB1aRunnerError("usage checked_at must include timezone")
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    age = (current - timestamp.astimezone(timezone.utc)).total_seconds()
    if age < -30 or age > MAX_USAGE_AGE_SECONDS:
        raise StageB1aRunnerError("usage attestation is stale or from the future")
    if not str(value["check_id"]).strip():
        raise StageB1aRunnerError("usage check_id is empty")
    return value


def assert_budget_allows(attestation: dict[str, Any], threshold: float) -> None:
    if float(attestation["remaining_percent"]) <= float(threshold):
        raise StageB1aRunnerError("BUDGET_STOP（額度暫停）")


def strict_transport_schema(
    local_schema: dict[str, Any], *, evidence_ref_values: list[str]
) -> dict[str, Any]:
    schema = json.loads(json.dumps(local_schema))
    schema["$defs"]["evidenceRef"] = {
        "type": "string",
        "enum": sorted(set(evidence_ref_values)),
    }
    unsupported = {
        "$id",
        "format",
        "uniqueItems",
        "allOf",
        "anyOf",
        "oneOf",
        "not",
        "if",
        "then",
        "else",
    }

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
            output["type"] = (
                "boolean"
                if isinstance(constant, bool)
                else "number"
                if isinstance(constant, (int, float))
                else "string"
            )
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


def compact_stage_b1a_packet(packet: dict[str, Any]) -> dict[str, Any]:
    rendered = json.dumps(packet, ensure_ascii=False).lower()
    for forbidden in FORBIDDEN_PACKET_KEYS:
        if f'"{forbidden}"' in rendered:
            raise StageB1aRunnerError(f"forbidden packet key visible: {forbidden}")
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
        "daily_rows": [
            [row.get(column) for column in columns]
            for row in packet["daily_structure_context_to_as_of"]
        ],
        "price_ref_rule": "Each daily row is cited as PRICE:<date> using the first column.",
        "quality_ref": "QUALITY:SUMMARY",
        "corporate_action_refs": [
            item for item in packet["evidence_catalog"] if item["kind"] == "ACTION"
        ],
        "focus_segments": packet["focus_segments"],
        "review_constraints": packet["review_constraints"],
    }


def rendered_prompt(prompt_text: str, packet: dict[str, Any]) -> str:
    return (
        prompt_text.rstrip()
        + "\n\n以下是唯一允許使用的匿名AS-OF輸入。只輸出Schema JSON。\n"
        + json.dumps(
            {"packet": compact_stage_b1a_packet(packet)},
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
    )


def call_codex(
    *,
    prompt: str,
    transport_schema: dict[str, Any],
    timeout_seconds: int,
    invalid_raw_path: Path,
    run_command: RunCommand = subprocess.run,
) -> tuple[dict[str, Any], dict[str, Any], bytes]:
    with tempfile.TemporaryDirectory(prefix="v2-core-b1a-") as temporary:
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
            if raw_bytes:
                write_new_or_identical(invalid_raw_path, raw_bytes)
            detail = ((completed.stderr or "") + "\n" + (completed.stdout or ""))[-4000:]
            raise StageB1aRunnerError(f"Codex exited {completed.returncode}: {detail}")
        if not output_path.is_file():
            raise StageB1aRunnerError("Codex did not publish last-message JSON")
        try:
            output = json.loads(raw_bytes.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            write_new_or_identical(invalid_raw_path, raw_bytes)
            raise StageB1aRunnerError("model output is not valid UTF-8 JSON") from exc
        transport_errors = sorted(
            error.message
            for error in Draft202012Validator(transport_schema).iter_errors(output)
        )
        if transport_errors:
            write_new_or_identical(invalid_raw_path, raw_bytes)
            raise StageB1aRunnerError(
                "model transport output invalid: " + "; ".join(transport_errors)
            )
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


def _assert_input_bindings(
    packet: dict[str, Any],
    *,
    candidate_catalog_path: Path,
    focus_catalog_path: Path,
) -> None:
    bindings = packet.get("source_bindings", {})
    if bindings.get("segment_catalog_sha256") != sha256_file(candidate_catalog_path):
        raise StageB1aRunnerError("input segment catalog binding mismatch")
    if bindings.get("focus_catalog_sha256") != sha256_file(focus_catalog_path):
        raise StageB1aRunnerError("input focus catalog binding mismatch")
    candidates = load_json(candidate_catalog_path)
    focus = load_json(focus_catalog_path)
    if candidates.get("source_packet_sha256") != bindings.get("source_packet_sha256"):
        raise StageB1aRunnerError("input source packet binding mismatch")
    if not (
        packet.get("review_id") == candidates.get("review_id") == focus.get("review_id")
        and packet.get("as_of") == candidates.get("as_of") == focus.get("as_of")
    ):
        raise StageB1aRunnerError("input review/as_of binding mismatch")


def run_stage_b1a_case(
    *,
    input_packet_path: Path,
    candidate_catalog_path: Path,
    focus_catalog_path: Path,
    schema_path: Path,
    prompt_path: Path,
    usage_attestation_path: Path,
    stop_remaining_percent_lte: float,
    output_path: Path,
    receipt_path: Path,
    timeout_seconds: int,
    run_command: RunCommand = subprocess.run,
) -> dict[str, Any]:
    attestation = parse_usage_attestation(usage_attestation_path)
    assert_budget_allows(attestation, stop_remaining_percent_lte)
    packet = load_json(input_packet_path)
    candidates = load_json(candidate_catalog_path)
    focus = load_json(focus_catalog_path)
    schema = load_json(schema_path)
    _assert_input_bindings(
        packet,
        candidate_catalog_path=candidate_catalog_path,
        focus_catalog_path=focus_catalog_path,
    )
    refs = [str(item["ref"]) for item in packet["evidence_catalog"]]
    transport = strict_transport_schema(schema, evidence_ref_values=refs)
    prompt = rendered_prompt(prompt_path.read_text(encoding="utf-8"), packet)
    output, receipt, raw_bytes = call_codex(
        prompt=prompt,
        transport_schema=transport,
        timeout_seconds=timeout_seconds,
        invalid_raw_path=invalid_output_path(output_path),
        run_command=run_command,
    )
    validation = validate_stage_b1a_response(
        response=output,
        schema=schema,
        focus=focus,
        candidates=candidates,
        packet=packet,
        candidate_catalog_path=candidate_catalog_path,
    )
    if validation["status"] != "VALID":
        write_new_or_identical(invalid_output_path(output_path), raw_bytes)
        raise StageB1aRunnerError(
            "STAGE_B1A local validation failed: "
            + json.dumps(validation, ensure_ascii=False)
        )
    write_new_or_identical(raw_output_path(output_path), raw_bytes)
    write_new_or_identical(output_path, canonical_bytes(output))
    bound_receipt = {
        **receipt,
        "stage": "STAGE_B1A",
        "review_id": packet["review_id"],
        "as_of": packet["as_of"],
        "input_packet_sha256": sha256_file(input_packet_path),
        "candidate_catalog_sha256": sha256_file(candidate_catalog_path),
        "focus_catalog_sha256": sha256_file(focus_catalog_path),
        "prompt_source_sha256": sha256_file(prompt_path),
        "local_schema_sha256": sha256_file(schema_path),
        "usage_attestation_sha256": sha256_file(usage_attestation_path),
        "stop_remaining_percent_lte": stop_remaining_percent_lte,
        "validation": validation,
    }
    write_new_or_identical(receipt_path, canonical_bytes(bound_receipt))
    return validation


def validate_existing_stage_b1a_artifacts(
    *,
    input_packet_path: Path,
    candidate_catalog_path: Path,
    focus_catalog_path: Path,
    schema_path: Path,
    prompt_path: Path,
    output_path: Path,
    receipt_path: Path,
) -> dict[str, Any]:
    raw_path = raw_output_path(output_path)
    if not output_path.is_file() or not raw_path.is_file() or not receipt_path.is_file():
        raise StageB1aRunnerError("existing B1a output/raw/receipt set is incomplete")
    packet = load_json(input_packet_path)
    candidates = load_json(candidate_catalog_path)
    focus = load_json(focus_catalog_path)
    schema = load_json(schema_path)
    _assert_input_bindings(
        packet,
        candidate_catalog_path=candidate_catalog_path,
        focus_catalog_path=focus_catalog_path,
    )
    output = load_json(output_path)
    validation = validate_stage_b1a_response(
        response=output,
        schema=schema,
        focus=focus,
        candidates=candidates,
        packet=packet,
        candidate_catalog_path=candidate_catalog_path,
    )
    if validation["status"] != "VALID":
        raise StageB1aRunnerError("existing B1a output is invalid")
    receipt = load_json(receipt_path)
    checks = {
        "runner_version": RUNNER_VERSION,
        "model": MODEL,
        "reasoning_effort": REASONING,
        "stage": "STAGE_B1A",
        "review_id": packet["review_id"],
        "as_of": packet["as_of"],
        "input_packet_sha256": sha256_file(input_packet_path),
        "candidate_catalog_sha256": sha256_file(candidate_catalog_path),
        "focus_catalog_sha256": sha256_file(focus_catalog_path),
        "prompt_source_sha256": sha256_file(prompt_path),
        "local_schema_sha256": sha256_file(schema_path),
        "raw_output_sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
        "normalized_output_sha256": canonical_sha256(output),
    }
    for key, expected in checks.items():
        if receipt.get(key) != expected:
            raise StageB1aRunnerError(f"existing B1a receipt mismatch: {key}")
    if receipt.get("validation") != validation:
        raise StageB1aRunnerError("existing B1a receipt validation mismatch")
    return validation


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-packet", type=Path, required=True)
    parser.add_argument("--candidate-catalog", type=Path, required=True)
    parser.add_argument("--focus-catalog", type=Path, required=True)
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--prompt", type=Path, required=True)
    parser.add_argument("--usage-attestation", type=Path, required=True)
    parser.add_argument("--stop-remaining-percent-lte", type=float, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    validation = run_stage_b1a_case(
        input_packet_path=args.input_packet,
        candidate_catalog_path=args.candidate_catalog,
        focus_catalog_path=args.focus_catalog,
        schema_path=args.schema,
        prompt_path=args.prompt,
        usage_attestation_path=args.usage_attestation,
        stop_remaining_percent_lte=args.stop_remaining_percent_lte,
        output_path=args.output,
        receipt_path=args.receipt,
        timeout_seconds=args.timeout_seconds,
    )
    print(json.dumps(validation, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
