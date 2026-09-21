"""Transport-hardened Codex adapter for one outcome-blind V3 atomic case.

V3 keeps the frozen V2 semantic schema and V3 deterministic policy unchanged.
It narrows the per-packet transport schema so every cited evidence reference
must be one of the exact references present in that packet.  Model-produced
transport or semantic validation failures are explicitly retryable within the
runner's already-frozen maximum-attempt policy; frozen input/hash failures are
still non-retryable.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any, Callable, Sequence

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

try:
    from . import hybrid_v3_codex_reviewer_v2 as v2
    from .hybrid_v3_atomic_policy_v3 import validate_atomic
    from .hybrid_v3_atomic_runner_v2 import (
        ReviewerResult,
        RetryableReviewError,
        ReviewContractError,
    )
except ImportError:  # pragma: no cover - direct script execution
    import hybrid_v3_codex_reviewer_v2 as v2
    from hybrid_v3_atomic_policy_v3 import validate_atomic
    from hybrid_v3_atomic_runner_v2 import (
        ReviewerResult,
        RetryableReviewError,
        ReviewContractError,
    )


ROOT = Path(__file__).resolve().parents[1]
PROMPT_PATH = ROOT / "config/hybrid_semantic_prompt_v3.md"
SCHEMA_PATH = ROOT / "config/hybrid_atomic_semantics_v2.schema.json"
PROTOCOL_PATH = ROOT / "config/hybrid_monitoring_research_protocol_v1.json"
POLICY_PATH = ROOT / "scripts/hybrid_v3_atomic_policy_v3.py"
ADAPTER_VERSION = "hybrid-v3-codex-reviewer-v3-exact-evidence"
ADAPTER_STATUS = "FINAL"
DEFAULT_MODEL = "gpt-5.6-sol"
DEFAULT_REASONING = "xhigh"


RunCommand = Callable[..., subprocess.CompletedProcess[str]]


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
    return v2.verify_execution_contract(
        records,
        execution_contract_path=execution_contract_path,
        prompt_path=prompt_path,
        schema_path=schema_path,
        protocol_path=protocol_path,
        reviewer_path=reviewer_path,
        policy_path=policy_path,
    )


def exact_evidence_structured_output_schema(
    schema: dict[str, Any], packet: dict[str, Any]
) -> dict[str, Any]:
    """Build the V2 transport shape with a packet-specific evidence enum."""

    transport = v2.structured_output_schema(schema, packet)
    evidence = packet.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise ReviewContractError("packet evidence catalog is empty")
    refs = [str(row.get("ref") or "") for row in evidence if isinstance(row, dict)]
    if any(not ref for ref in refs) or len(refs) != len(evidence):
        raise ReviewContractError("packet evidence catalog contains an empty ref")
    if len(set(refs)) != len(refs):
        raise ReviewContractError("packet evidence catalog contains duplicate refs")
    transport["$defs"]["evidenceRef"] = {
        "type": "string",
        "enum": sorted(refs),
    }
    return transport


def validate_model_transport(
    *,
    packet: dict[str, Any],
    transport_schema: dict[str, Any],
    transport_output: dict[str, Any],
) -> dict[str, Any]:
    """Return canonical semantics or classify model-output defects as retryable."""

    try:
        v2.validate_transport_output(transport_schema, transport_output)
        canonical = v2.canonicalize_transport_output(packet, transport_output)
    except ReviewContractError as exc:
        raise RetryableReviewError(f"model output contract validation failed: {exc}") from exc
    errors = sorted(set(str(error) for error in validate_atomic(packet, canonical)))
    if errors:
        raise RetryableReviewError(
            "model atomic semantic validation failed: " + "; ".join(errors)
        )
    return canonical


class CodexAtomicReviewerV3(v2.CodexAtomicReviewerV2):
    """Single-case reviewer with exact evidence refs and repairable output validation."""

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
        super().__init__(
            prompt_path=prompt_path,
            schema_path=schema_path,
            model=model,
            reasoning_effort=reasoning_effort,
            timeout_seconds=timeout_seconds,
            run_command=run_command,
            expected_prompt_sha256=expected_prompt_sha256,
            expected_schema_sha256=expected_schema_sha256,
            expected_adapter_code_sha256=None,
        )
        self.adapter_code_sha256 = v2._file_sha256(Path(__file__).resolve())
        if (
            expected_adapter_code_sha256 is not None
            and str(expected_adapter_code_sha256).lower() != self.adapter_code_sha256
        ):
            raise ReviewContractError("adapter changed after execution-contract verification")

    def __call__(self, packet: dict[str, Any]) -> ReviewerResult:
        prompt_bytes = self.prompt_path.read_bytes()
        schema_bytes = self.schema_path.read_bytes()
        if v2._bytes_sha256(prompt_bytes) != self.prompt_sha256:
            raise ReviewContractError("prompt changed during formal execution")
        if v2._bytes_sha256(schema_bytes) != self.schema_sha256:
            raise ReviewContractError("schema changed during formal execution")
        try:
            prompt_text = prompt_bytes.decode("utf-8-sig")
            local_schema = json.loads(schema_bytes.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReviewContractError(f"formal prompt/schema is unreadable: {exc}") from exc
        if not isinstance(local_schema, dict):
            raise ReviewContractError(f"expected JSON object: {self.schema_path}")
        prompt = v2.build_prompt(packet, prompt_text)
        transport_schema = exact_evidence_structured_output_schema(local_schema, packet)
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
        with tempfile.TemporaryDirectory(prefix="hybrid-v3-exact-evidence-") as temporary:
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
                raise v2._nonzero_codex_error(completed.returncode, detail)
            if not output_path.exists():
                raise RetryableReviewError("Codex did not publish a last-message JSON file")
            try:
                transport_output = json.loads(output_path.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RetryableReviewError(f"Codex output is not readable JSON: {exc}") from exc
            if not isinstance(transport_output, dict):
                raise RetryableReviewError("Codex output is not a JSON object")
            canonical = validate_model_transport(
                packet=packet,
                transport_schema=transport_schema,
                transport_output=transport_output,
            )
            return ReviewerResult(
                output=canonical,
                adapter_version=ADAPTER_VERSION,
                adapter_status=ADAPTER_STATUS,
                adapter_code_sha256=self.adapter_code_sha256,
                dynamic_transport_schema_sha256=v2._bytes_sha256(transport_schema_bytes),
                rendered_prompt_sha256=v2._bytes_sha256(prompt.encode("utf-8")),
                raw_transport_output_sha256=v2._bytes_sha256(
                    json.dumps(
                        transport_output,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    ).encode("utf-8")
                ),
            )
