"""Codex CLI adapter for one outcome-blind Hybrid Monitoring V2 case.

This module deliberately reviews exactly one packet per Codex invocation.  It
does not read identities, later prices, old AI ledgers, trading outcomes or
strategy performance.  The atomic runner owns retry/resume and immutable
publication; this adapter only performs the isolated model call.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, Sequence

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

try:
    from .hybrid_v3_atomic_packets_v2 import assert_anonymous_and_causal
    from .hybrid_v3_atomic_policy_v2 import validate_atomic
    from .hybrid_v3_atomic_runner_v2 import (
        AtomicCaseRunner,
        ReviewerResult,
        RetryableReviewError,
        ReviewContractError,
    )
except ImportError:  # pragma: no cover - direct script execution
    from hybrid_v3_atomic_packets_v2 import assert_anonymous_and_causal
    from hybrid_v3_atomic_policy_v2 import validate_atomic
    from hybrid_v3_atomic_runner_v2 import (
        AtomicCaseRunner,
        ReviewerResult,
        RetryableReviewError,
        ReviewContractError,
    )


ROOT = Path(__file__).resolve().parents[1]
PROMPT_PATH = ROOT / "config/hybrid_semantic_prompt_v2.md"
SCHEMA_PATH = ROOT / "config/hybrid_atomic_semantics_v2.schema.json"
PROTOCOL_PATH = ROOT / "config/hybrid_monitoring_protocol_v2.json"
POLICY_PATH = ROOT / "scripts/hybrid_v3_atomic_policy_v2.py"
ADAPTER_VERSION = "hybrid-v3-codex-reviewer-v2"
ADAPTER_STATUS = "FINAL"
DEFAULT_MODEL = "gpt-5.6-sol"
DEFAULT_REASONING = "xhigh"
FROZEN_EXECUTION_STATUS = "FROZEN_IMMUTABLE_BEFORE_AI_EXECUTION"
def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _bytes_sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ReviewContractError(f"expected JSON object: {path}")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8-sig").splitlines()
        if line.strip()
    ]
    if not all(isinstance(row, dict) for row in rows):
        raise ReviewContractError(f"expected JSON objects in: {path}")
    return rows


def verify_execution_contract(
    records: Sequence[dict[str, Any]],
    *,
    execution_contract_path: Path,
    prompt_path: Path = PROMPT_PATH,
    schema_path: Path = SCHEMA_PATH,
    protocol_path: Path = PROTOCOL_PATH,
    reviewer_path: Path = Path(__file__).resolve(),
    policy_path: Path = POLICY_PATH,
) -> dict[str, Any]:
    """Fail closed unless records and the formal freeze pin actual execution files."""

    paths = {
        "reviewer": Path(reviewer_path).resolve(),
        "policy": Path(policy_path).resolve(),
        "prompt": Path(prompt_path).resolve(),
        "schema": Path(schema_path).resolve(),
        "protocol": Path(protocol_path).resolve(),
    }
    contract_path = Path(execution_contract_path).resolve()
    contract = _read_object(contract_path)
    contract_sha256 = _file_sha256(contract_path)
    if contract.get("status") != FROZEN_EXECUTION_STATUS:
        raise ReviewContractError("execution contract is not the formal frozen manifest")
    components = contract.get("v2_components")
    if not isinstance(components, list):
        raise ReviewContractError("execution contract has no v2_components")
    component_by_name: dict[str, dict[str, Any]] = {}
    for row in components:
        if not isinstance(row, dict) or not row.get("name"):
            raise ReviewContractError("execution contract contains an invalid component entry")
        name = str(row["name"])
        if name in component_by_name:
            raise ReviewContractError(f"execution contract duplicates component: {name}")
        component_by_name[name] = row
    for name, actual_path in paths.items():
        entry = component_by_name.get(name)
        if entry is None:
            raise ReviewContractError(f"execution contract does not pin component: {name}")
        if str(entry.get("status") or "").upper() != "FINAL":
            raise ReviewContractError(f"execution contract component is not FINAL: {name}")
        expected_sha = str(entry.get("sha256") or "").lower()
        if expected_sha != _file_sha256(actual_path):
            raise ReviewContractError(f"execution contract component hash differs: {name}")
        relative_path = entry.get("relative_path")
        if relative_path:
            frozen_path = (ROOT / str(relative_path)).resolve()
            if frozen_path != actual_path:
                raise ReviewContractError(f"execution contract component path differs: {name}")

    execution = contract.get("execution_contract")
    if not isinstance(execution, dict):
        raise ReviewContractError("execution contract settings are missing")
    actual_hashes = {
        "prompt_sha256": _file_sha256(paths["prompt"]),
        "schema_sha256": _file_sha256(paths["schema"]),
        "protocol_sha256": _file_sha256(paths["protocol"]),
        "execution_contract_sha256": contract_sha256,
    }
    environment_hashes = {
        **actual_hashes,
        "reviewer_code_sha256": _file_sha256(paths["reviewer"]),
        "policy_code_sha256": _file_sha256(paths["policy"]),
    }
    if not records:
        return {
            **environment_hashes,
            "model": execution.get("model"),
            "reasoning_effort": execution.get("reasoning_effort"),
        }
    for record in records:
        for field, actual in actual_hashes.items():
            if str(record.get(field) or "").lower() != actual:
                raise ReviewContractError(f"case record {field} differs from actual execution file")
        if record.get("model") != execution.get("model"):
            raise ReviewContractError("case record model differs from frozen execution contract")
        if record.get("reasoning_effort") != execution.get("reasoning_effort"):
            raise ReviewContractError("case record reasoning differs from frozen execution contract")
    return {
        **environment_hashes,
        "model": execution.get("model"),
        "reasoning_effort": execution.get("reasoning_effort"),
    }


def structured_output_schema(
    schema: dict[str, Any], packet: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Remove metadata/keywords unsupported by Codex structured output.

    The complete local Draft 2020-12 schema remains authoritative and is
    always applied after the model call by ``validate_atomic``.
    """

    # Codex Responses structured output accepts only a strict JSON-Schema
    # subset.  Conditional/composition rules remain enforced by the complete
    # local Draft 2020-12 validator after the call.
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

    def clean(value: Any, *, top_level: bool = False) -> Any:
        if isinstance(value, list):
            return [clean(item) for item in value]
        if not isinstance(value, dict):
            return value
        output: dict[str, Any] = {}
        for key, item in value.items():
            if key in unsupported or key.startswith("x-"):
                continue
            output[key] = clean(item)
        if "type" not in output and "const" in output:
            constant = output["const"]
            output["type"] = (
                "boolean"
                if isinstance(constant, bool)
                else "number"
                if isinstance(constant, (int, float))
                else "string"
            )
        if "type" not in output and output.get("enum"):
            values = output["enum"]
            if all(isinstance(item, str) for item in values):
                output["type"] = "string"
        # Strict Responses schemas require every declared property to appear.
        # Locally optional diagnostic fields therefore become required only in
        # the transport schema; the authoritative local schema still decides
        # semantic validity.
        if output.get("type") == "object" and isinstance(output.get("properties"), dict):
            output["required"] = list(output["properties"])
        return output

    cleaned = clean(schema, top_level=True)
    if not isinstance(cleaned, dict):  # defensive; input is already typed
        raise ReviewContractError("structured output schema is not an object")
    if packet is None:
        return cleaned

    manifest = packet["question_manifest"]
    required_pairs: list[tuple[str, str, str]] = []
    for group_name, kind in (
        ("anchor_candidates", "anchor"),
        ("relation_candidates", "relation"),
        ("stop_candidates", "stop"),
    ):
        for entry in manifest[group_name]:
            for question_id in entry["required_question_ids"]:
                required_pairs.append((kind, str(entry["subject_ref"]), str(question_id)))
    required_pairs.extend(
        ("global", "__GLOBAL__", str(question_id))
        for question_id in manifest["global_question_ids"]
    )
    question_ids = sorted({question_id for _, _, question_id in required_pairs}) or ["__NO_QUESTIONS__"]
    subject_refs = sorted({subject_ref for _, subject_ref, _ in required_pairs}) or ["__GLOBAL__"]
    root_properties = cleaned["properties"]
    transport_verdict = json.loads(json.dumps(cleaned["$defs"]["atomicVerdict"]))
    transport = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "protocol_version",
            "contract_status",
            "review_id",
            "anonymous_stock_id",
            "as_of",
            "input_packet_sha256",
            "question_manifest_sha256",
            "evidence_catalog_sha256",
            "answers",
            "causal_attestation",
        ],
        "properties": {
            key: {
                "const": (
                    root_properties[key].get("const")
                    if key in {"schema_version", "protocol_version", "contract_status"}
                    else packet[key]
                ),
                "type": "string",
            }
            for key in (
                "schema_version",
                "protocol_version",
                "contract_status",
                "review_id",
                "anonymous_stock_id",
                "as_of",
                "input_packet_sha256",
                "question_manifest_sha256",
                "evidence_catalog_sha256",
            )
        },
        "$defs": {
            key: cleaned["$defs"][key]
            for key in (
                "evidenceRef",
                "evidenceRefs",
                "missingEvidenceCodes",
            )
        },
    }
    transport["$defs"]["atomicVerdict"] = transport_verdict
    transport["properties"]["answers"] = {
        "type": "array",
        "minItems": len(required_pairs),
        "maxItems": len(required_pairs),
        "items": {
            "type": "object",
            "additionalProperties": False,
            "required": ["subject_kind", "subject_ref", "question_id", "verdict"],
            "properties": {
                "subject_kind": {"type": "string", "enum": ["anchor", "relation", "stop", "global"]},
                "subject_ref": {"type": "string", "enum": subject_refs},
                "question_id": {"type": "string", "enum": question_ids},
                "verdict": {"$ref": "#/$defs/atomicVerdict"},
            },
        },
    }
    transport["properties"]["causal_attestation"] = cleaned["$defs"]["causalAttestation"]
    return transport


def validate_transport_output(
    transport_schema: dict[str, Any], transport: dict[str, Any]
) -> None:
    """Apply the exact dynamic transport schema locally before canonicalizing."""

    errors = sorted(
        Draft202012Validator(transport_schema).iter_errors(transport),
        key=lambda error: (list(error.absolute_path), error.message),
    )
    if not errors:
        return
    first = errors[0]
    location = "/".join(str(value) for value in first.absolute_path) or "<root>"
    raise ReviewContractError(
        f"raw transport output violates dynamic schema at {location}: {first.message}"
    )


def canonicalize_transport_output(
    packet: dict[str, Any], transport: dict[str, Any]
) -> dict[str, Any]:
    """Convert flattened transport rows to the frozen grouped local contract."""

    manifest = packet["question_manifest"]
    expected: dict[tuple[str, str], list[str]] = {}
    for group_name, kind in (
        ("anchor_candidates", "anchor"),
        ("relation_candidates", "relation"),
        ("stop_candidates", "stop"),
    ):
        for entry in manifest[group_name]:
            expected[(kind, str(entry["subject_ref"]))] = [
                str(value) for value in entry["required_question_ids"]
            ]
    expected[("global", "__GLOBAL__")] = [
        str(value) for value in manifest["global_question_ids"]
    ]
    actual: dict[tuple[str, str, str], dict[str, Any]] = {}
    rows = transport.get("answers")
    if not isinstance(rows, list):
        raise ReviewContractError("transport output answers is not an array")
    for row in rows:
        if not isinstance(row, dict):
            raise ReviewContractError("transport answer row is not an object")
        key = (
            str(row.get("subject_kind") or ""),
            str(row.get("subject_ref") or ""),
            str(row.get("question_id") or ""),
        )
        if key in actual:
            raise ReviewContractError(f"duplicate transport answer: {key}")
        actual[key] = row.get("verdict")
    required = {
        (kind, subject_ref, question_id)
        for (kind, subject_ref), question_ids_for_subject in expected.items()
        for question_id in question_ids_for_subject
    }
    if set(actual) != required:
        missing = sorted(required - set(actual))
        extra = sorted(set(actual) - required)
        raise ReviewContractError(
            f"transport answers differ from question manifest; missing={missing[:3]} extra={extra[:3]}"
        )
    candidate_answers: dict[str, list[dict[str, Any]]] = {
        "anchor_candidates": [],
        "relation_candidates": [],
        "stop_candidates": [],
    }
    for group_name, kind in (
        ("anchor_candidates", "anchor"),
        ("relation_candidates", "relation"),
        ("stop_candidates", "stop"),
    ):
        for entry in manifest[group_name]:
            subject_ref = str(entry["subject_ref"])
            candidate_answers[group_name].append(
                {
                    "subject_ref": subject_ref,
                    "answers": {
                        question_id: actual[(kind, subject_ref, question_id)]
                        for question_id in entry["required_question_ids"]
                    },
                }
            )
    return {
        key: transport[key]
        for key in (
            "schema_version",
            "protocol_version",
            "contract_status",
            "review_id",
            "anonymous_stock_id",
            "as_of",
            "input_packet_sha256",
            "question_manifest_sha256",
            "evidence_catalog_sha256",
        )
    } | {
        "candidate_answers": candidate_answers,
        "global_answers": {
            question_id: actual[("global", "__GLOBAL__", question_id)]
            for question_id in manifest["global_question_ids"]
        },
        "causal_attestation": transport["causal_attestation"],
    }


def build_prompt(packet: dict[str, Any], prompt_text: str) -> str:
    assert_anonymous_and_causal(packet, as_of=str(packet.get("as_of") or ""))
    return (
        prompt_text.rstrip()
        + "\n\n你只審核下列一個匿名、截至日封包。"
        + "不得猜股票身分，不得推測或引用截至日以後行情，也不得輸出買賣決定。"
        + "請完整回答 question_manifest 指定的每個 subject/question，並只引用 evidence 中現有 ref。\n"
        + "傳輸輸出會使用 answers 扁平陣列；每個指定的 subject_ref/question_id 恰好輸出一次，不得多答或漏答。\n"
        + json.dumps(packet, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    )


RunCommand = Callable[..., subprocess.CompletedProcess[str]]

_EXPLICIT_TRANSIENT_MARKERS = (
    "timed out",
    "timeout",
    "temporarily unavailable",
    "temporary failure",
    "connection reset",
    "connection aborted",
    "service unavailable",
    "server overloaded",
    "rate limit",
    "too many requests",
    "http 429",
    "status 429",
    "http 500",
    "status 500",
    "http 502",
    "status 502",
    "http 503",
    "status 503",
    "http 504",
    "status 504",
)


def _nonzero_codex_error(returncode: int, detail: str) -> Exception:
    message = f"Codex exited with {returncode}: {detail}"
    lowered = detail.lower()
    if any(marker in lowered for marker in _EXPLICIT_TRANSIENT_MARKERS):
        return RetryableReviewError(message)
    return ReviewContractError(message)


class CodexAtomicReviewerV2:
    """Callable single-case reviewer suitable for ``AtomicCaseRunner``."""

    def __init__(
        self,
        *,
        prompt_path: Path = PROMPT_PATH,
        schema_path: Path = SCHEMA_PATH,
        model: str = DEFAULT_MODEL,
        reasoning_effort: str = DEFAULT_REASONING,
        timeout_seconds: int = 1200,
        run_command: RunCommand = subprocess.run,
        expected_prompt_sha256: str | None = None,
        expected_schema_sha256: str | None = None,
        expected_adapter_code_sha256: str | None = None,
    ) -> None:
        self.prompt_path = Path(prompt_path)
        self.schema_path = Path(schema_path)
        self.model = str(model)
        self.reasoning_effort = str(reasoning_effort)
        self.timeout_seconds = int(timeout_seconds)
        self.run_command = run_command
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.prompt_sha256 = _file_sha256(self.prompt_path)
        self.schema_sha256 = _file_sha256(self.schema_path)
        self.adapter_code_sha256 = _file_sha256(Path(__file__).resolve())
        for label, expected, actual in (
            ("prompt", expected_prompt_sha256, self.prompt_sha256),
            ("schema", expected_schema_sha256, self.schema_sha256),
            ("adapter", expected_adapter_code_sha256, self.adapter_code_sha256),
        ):
            if expected is not None and str(expected).lower() != actual:
                raise ReviewContractError(f"{label} changed after execution-contract verification")

    def __call__(self, packet: dict[str, Any]) -> ReviewerResult:
        prompt_bytes = self.prompt_path.read_bytes()
        schema_bytes = self.schema_path.read_bytes()
        if _bytes_sha256(prompt_bytes) != self.prompt_sha256:
            raise ReviewContractError("prompt changed during formal execution")
        if _bytes_sha256(schema_bytes) != self.schema_sha256:
            raise ReviewContractError("schema changed during formal execution")
        try:
            prompt_text = prompt_bytes.decode("utf-8-sig")
            local_schema = json.loads(schema_bytes.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReviewContractError(f"formal prompt/schema is unreadable: {exc}") from exc
        if not isinstance(local_schema, dict):
            raise ReviewContractError(f"expected JSON object: {self.schema_path}")
        prompt = build_prompt(packet, prompt_text)
        transport_schema = structured_output_schema(local_schema, packet)
        try:
            Draft202012Validator.check_schema(transport_schema)
        except SchemaError as exc:
            raise ReviewContractError(f"dynamic transport schema is invalid: {exc.message}") from exc
        transport_schema_bytes = json.dumps(
            transport_schema,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        with tempfile.TemporaryDirectory(prefix="hybrid-v2-atomic-") as temporary:
            isolated = Path(temporary)
            output_schema_path = isolated / "output_schema.json"
            output_path = isolated / "last_message.json"
            output_schema_path.write_bytes(transport_schema_bytes)
            command = [
                "codex",
                "exec",
                "-",
                "--model",
                self.model,
                "-c",
                f'model_reasoning_effort="{self.reasoning_effort}"',
                "--sandbox",
                "read-only",
                "--ephemeral",
                "--ignore-user-config",
                "--ignore-rules",
                "--skip-git-repo-check",
                "--output-schema",
                str(output_schema_path),
                "--output-last-message",
                str(output_path),
                "--json",
                "--color",
                "never",
                "--cd",
                str(isolated),
            ]
            environment = os.environ.copy()
            environment["PYTHONUTF8"] = "1"
            environment["NO_COLOR"] = "1"
            try:
                completed = self.run_command(
                    command,
                    input=prompt,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    capture_output=True,
                    timeout=self.timeout_seconds,
                    check=False,
                    env=environment,
                )
            except (subprocess.TimeoutExpired, TimeoutError, ConnectionError) as exc:
                raise RetryableReviewError(f"Codex transport failure: {exc}") from exc
            if completed.returncode != 0:
                detail = ((completed.stderr or "") + "\n" + (completed.stdout or ""))[-4000:]
                raise _nonzero_codex_error(completed.returncode, detail)
            if not output_path.exists():
                raise ReviewContractError("Codex did not publish a last-message JSON file")
            try:
                output = json.loads(output_path.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ReviewContractError(f"Codex output is not readable JSON: {exc}") from exc
            if not isinstance(output, dict):
                raise ReviewContractError("Codex output is not a JSON object")
            validate_transport_output(transport_schema, output)
            canonical = canonicalize_transport_output(packet, output)
            return ReviewerResult(
                output=canonical,
                adapter_version=ADAPTER_VERSION,
                adapter_status=ADAPTER_STATUS,
                adapter_code_sha256=self.adapter_code_sha256,
                dynamic_transport_schema_sha256=_bytes_sha256(transport_schema_bytes),
                rendered_prompt_sha256=_bytes_sha256(prompt.encode("utf-8")),
                raw_transport_output_sha256=_bytes_sha256(
                    json.dumps(
                        output,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    ).encode("utf-8")
                ),
            )


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case-records", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--run-number", type=int, required=True)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--reasoning-effort", default=DEFAULT_REASONING)
    parser.add_argument("--protocol", type=Path, default=PROTOCOL_PATH)
    parser.add_argument("--prompt", type=Path, default=PROMPT_PATH)
    parser.add_argument("--schema", type=Path, default=SCHEMA_PATH)
    parser.add_argument("--execution-contract", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=1200)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--limit-cases", type=int)
    args = parser.parse_args()

    records = _read_jsonl(args.case_records)
    if args.limit_cases is not None:
        records = records[: max(0, int(args.limit_cases))]
    for record in records:
        if record.get("model") != args.model or record.get("reasoning_effort") != args.reasoning_effort:
            raise ReviewContractError("case record model/reasoning differs from reviewer execution")
    verified_execution = verify_execution_contract(
        records,
        execution_contract_path=args.execution_contract,
        prompt_path=args.prompt,
        schema_path=args.schema,
        protocol_path=args.protocol,
    )
    reviewer = CodexAtomicReviewerV2(
        prompt_path=args.prompt,
        schema_path=args.schema,
        model=args.model,
        reasoning_effort=args.reasoning_effort,
        timeout_seconds=args.timeout_seconds,
        expected_prompt_sha256=verified_execution["prompt_sha256"],
        expected_schema_sha256=verified_execution["schema_sha256"],
        expected_adapter_code_sha256=verified_execution["reviewer_code_sha256"],
    )
    runner = AtomicCaseRunner(
        output_dir=args.output_dir,
        reviewer=reviewer,
        validator=validate_atomic,
        run_number=args.run_number,
        max_attempts=args.max_attempts,
        expected_reviewer_identity={
            "adapter_version": ADAPTER_VERSION,
            "adapter_status": ADAPTER_STATUS,
            "adapter_code_sha256": verified_execution["reviewer_code_sha256"],
        },
    )
    result = runner.run_cases(records)
    result.update(
        {
            "adapter_version": ADAPTER_VERSION,
            "adapter_status": ADAPTER_STATUS,
            "model": args.model,
            "reasoning_effort": args.reasoning_effort,
        }
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
