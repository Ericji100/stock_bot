"""Atomic, resumable reviewer execution and ordered merge for monitoring V2.

The reviewer and semantic validator are injected callables.  This module does
not select a model, read performance, or implement a trading strategy.
"""
from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

try:
    from .hybrid_v3_sharding_v2 import (
        ShardIntegrityError,
        _publish_immutable,
        build_case_key,
        build_run_case_key,
        canonical_json_bytes,
        canonical_sha256,
    )
except ImportError:  # direct script import
    from hybrid_v3_sharding_v2 import (
        ShardIntegrityError,
        _publish_immutable,
        build_case_key,
        build_run_case_key,
        canonical_json_bytes,
        canonical_sha256,
    )


RUNNER_VERSION = "hybrid-v3-atomic-runner-v2"
RUNNER_STATUS = "FINAL"
Validator = Callable[[dict[str, Any], dict[str, Any]], Sequence[str]]


class ExecutionIntegrityError(ShardIntegrityError):
    """An existing artifact or case identity does not match the frozen input."""


class RetryableReviewError(RuntimeError):
    """Explicitly retryable transport, timeout, or service failure."""


class ReviewContractError(RuntimeError):
    """Non-retryable reviewer or semantic validation failure."""


@dataclass(frozen=True)
class ReviewerResult:
    """Canonical output plus an auditable record of its raw model transport."""

    output: dict[str, Any]
    adapter_version: str
    adapter_status: str
    adapter_code_sha256: str
    dynamic_transport_schema_sha256: str
    rendered_prompt_sha256: str
    raw_transport_output_sha256: str


Reviewer = Callable[[dict[str, Any]], ReviewerResult]


@dataclass(frozen=True)
class CaseRunResult:
    envelope: dict[str, Any]
    resumed: bool


_REVIEWER_AUDIT_FIELDS = {
    "adapter_version",
    "adapter_status",
    "adapter_code_sha256",
    "dynamic_transport_schema_sha256",
    "rendered_prompt_sha256",
    "raw_transport_output_sha256",
}


def _is_sha256(value: Any) -> bool:
    text = str(value or "")
    return len(text) == 64 and all(character in "0123456789abcdef" for character in text)


def _reviewer_audit(result: ReviewerResult) -> dict[str, str]:
    audit = {
        "adapter_version": str(result.adapter_version),
        "adapter_status": str(result.adapter_status),
        "adapter_code_sha256": str(result.adapter_code_sha256),
        "dynamic_transport_schema_sha256": str(result.dynamic_transport_schema_sha256),
        "rendered_prompt_sha256": str(result.rendered_prompt_sha256),
        "raw_transport_output_sha256": str(result.raw_transport_output_sha256),
    }
    _validate_reviewer_audit(audit)
    return audit


def _validate_reviewer_audit(audit: Any) -> None:
    if not isinstance(audit, dict) or set(audit) != _REVIEWER_AUDIT_FIELDS:
        raise ExecutionIntegrityError("reviewer audit fields are incomplete or unexpected")
    if not audit.get("adapter_version") or not audit.get("adapter_status"):
        raise ExecutionIntegrityError("reviewer audit adapter identity is incomplete")
    for field in _REVIEWER_AUDIT_FIELDS - {"adapter_version", "adapter_status"}:
        if not _is_sha256(audit.get(field)):
            raise ExecutionIntegrityError(f"reviewer audit {field} is not SHA-256")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ExecutionIntegrityError(f"expected JSON object: {path}")
    return value


def _validate_case_record(record: dict[str, Any]) -> None:
    packet = record.get("packet")
    if not isinstance(packet, dict):
        raise ExecutionIntegrityError("case record packet is missing or not an object")
    if canonical_sha256(packet) != record.get("packet_sha256"):
        raise ExecutionIntegrityError(f"packet hash mismatch: {record.get('review_id')}")
    if str(packet.get("review_id") or "") != str(record.get("review_id") or ""):
        raise ExecutionIntegrityError("packet review_id differs from case record")
    expected = build_case_key(
        source_manifest_sha256=str(record["source_manifest_sha256"]),
        source_ordinal=int(record["source_ordinal"]),
        review_id=str(record["review_id"]),
        packet_sha256=str(record["packet_sha256"]),
        protocol_sha256=str(record["protocol_sha256"]),
        prompt_sha256=str(record["prompt_sha256"]),
        schema_sha256=str(record["schema_sha256"]),
        execution_contract_sha256=str(record["execution_contract_sha256"]),
        model=str(record["model"]),
        reasoning_effort=str(record["reasoning_effort"]),
    )
    if expected != record.get("case_key"):
        raise ExecutionIntegrityError(f"case key mismatch: {record.get('review_id')}")


def _identity_errors(packet: dict[str, Any], output: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for field in ("review_id", "anonymous_stock_id", "as_of"):
        if field in packet and output.get(field) != packet.get(field):
            errors.append(f"{field} differs from packet")
    return errors


class AtomicCaseRunner:
    """Execute cases independently and publish one immutable envelope per case."""

    def __init__(
        self,
        *,
        output_dir: Path,
        reviewer: Reviewer,
        validator: Validator | None,
        run_number: int,
        max_attempts: int = 3,
        retry_delays: Sequence[float] = (0.0, 0.0),
        sleeper: Callable[[float], None] = time.sleep,
        expected_reviewer_identity: Mapping[str, str] | None = None,
    ) -> None:
        if run_number <= 0:
            raise ValueError("run_number must be positive")
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        self.output_dir = Path(output_dir)
        self.reviewer = reviewer
        self.validator = validator
        self.run_number = int(run_number)
        self.max_attempts = int(max_attempts)
        self.retry_delays = tuple(float(value) for value in retry_delays)
        self.sleeper = sleeper
        self.expected_reviewer_identity = dict(expected_reviewer_identity or {})
        unexpected_identity = set(self.expected_reviewer_identity) - {
            "adapter_version", "adapter_status", "adapter_code_sha256"
        }
        if unexpected_identity:
            raise ValueError(f"unexpected reviewer identity fields: {sorted(unexpected_identity)}")

    def _envelope_path(self, run_case_key: str) -> Path:
        return self.output_dir / "cases" / f"{run_case_key}.json"

    def _attempt_dir(self, run_case_key: str) -> Path:
        return self.output_dir / "attempts" / run_case_key

    def _attempts(self, run_case_key: str) -> list[dict[str, Any]]:
        directory = self._attempt_dir(run_case_key)
        return [_read_json(path) for path in sorted(directory.glob("attempt_*.json"))] if directory.exists() else []

    def _publish_attempt(self, run_case_key: str, attempt: int, value: dict[str, Any]) -> None:
        path = self._attempt_dir(run_case_key) / f"attempt_{attempt:03d}.json"
        _publish_immutable(path, canonical_json_bytes(value) + b"\n")

    def _verify_expected_reviewer(self, audit: Mapping[str, Any]) -> None:
        for field, expected in self.expected_reviewer_identity.items():
            if audit.get(field) != expected:
                raise ExecutionIntegrityError(f"reviewer audit differs from frozen {field}")

    def _envelope_from_attempt(
        self,
        record: dict[str, Any],
        run_case_key: str,
        attempt: dict[str, Any],
    ) -> dict[str, Any]:
        output = attempt.get("output")
        if not isinstance(output, dict) or canonical_sha256(output) != attempt.get("output_sha256"):
            raise ExecutionIntegrityError("validated attempt output is missing or changed")
        audit = attempt.get("reviewer_audit")
        _validate_reviewer_audit(audit)
        if canonical_sha256(audit) != attempt.get("reviewer_audit_sha256"):
            raise ExecutionIntegrityError("validated attempt reviewer audit is missing or changed")
        self._verify_expected_reviewer(audit)
        return {
            "runner_version": RUNNER_VERSION,
            "status": "VALID",
            "run_number": self.run_number,
            "source_ordinal": int(record["source_ordinal"]),
            "review_id": str(record["review_id"]),
            "case_key": str(record["case_key"]),
            "run_case_key": run_case_key,
            "packet_sha256": str(record["packet_sha256"]),
            "source_manifest_sha256": str(record["source_manifest_sha256"]),
            "protocol_sha256": str(record["protocol_sha256"]),
            "prompt_sha256": str(record["prompt_sha256"]),
            "schema_sha256": str(record["schema_sha256"]),
            "execution_contract_sha256": str(record["execution_contract_sha256"]),
            "model": str(record["model"]),
            "reasoning_effort": str(record["reasoning_effort"]),
            "successful_attempt": int(attempt["attempt"]),
            "output_sha256": str(attempt["output_sha256"]),
            "reviewer_audit_sha256": str(attempt["reviewer_audit_sha256"]),
            "reviewer_audit": attempt["reviewer_audit"],
            "output": output,
        }

    def _validate_envelope(
        self,
        record: dict[str, Any],
        run_case_key: str,
        envelope: dict[str, Any],
    ) -> None:
        expected = {
            "runner_version": RUNNER_VERSION,
            "status": "VALID",
            "run_number": self.run_number,
            "source_ordinal": int(record["source_ordinal"]),
            "review_id": str(record["review_id"]),
            "case_key": str(record["case_key"]),
            "run_case_key": run_case_key,
            "packet_sha256": str(record["packet_sha256"]),
            "source_manifest_sha256": str(record["source_manifest_sha256"]),
            "protocol_sha256": str(record["protocol_sha256"]),
            "prompt_sha256": str(record["prompt_sha256"]),
            "schema_sha256": str(record["schema_sha256"]),
            "execution_contract_sha256": str(record["execution_contract_sha256"]),
            "model": str(record["model"]),
            "reasoning_effort": str(record["reasoning_effort"]),
        }
        for field, value in expected.items():
            if envelope.get(field) != value:
                raise ExecutionIntegrityError(f"envelope field changed: {field}")
        output = envelope.get("output")
        if not isinstance(output, dict) or canonical_sha256(output) != envelope.get("output_sha256"):
            raise ExecutionIntegrityError("envelope output hash mismatch")
        audit = envelope.get("reviewer_audit")
        _validate_reviewer_audit(audit)
        if canonical_sha256(audit) != envelope.get("reviewer_audit_sha256"):
            raise ExecutionIntegrityError("envelope reviewer audit hash mismatch")
        self._verify_expected_reviewer(audit)
        errors = _identity_errors(record["packet"], output)
        if self.validator is not None:
            errors.extend(str(error) for error in self.validator(record["packet"], output))
        if errors:
            raise ExecutionIntegrityError("; ".join(errors))

    def run_case(self, record: dict[str, Any]) -> CaseRunResult:
        _validate_case_record(record)
        run_case_key = build_run_case_key(str(record["case_key"]), self.run_number)
        envelope_path = self._envelope_path(run_case_key)
        if envelope_path.exists():
            envelope = _read_json(envelope_path)
            self._validate_envelope(record, run_case_key, envelope)
            return CaseRunResult(envelope=envelope, resumed=True)

        attempts = self._attempts(run_case_key)
        for attempt in attempts:
            if attempt.get("case_key") != record["case_key"] or attempt.get("run_case_key") != run_case_key:
                raise ExecutionIntegrityError("attempt identity differs from case")
        validated = [row for row in attempts if row.get("status") == "VALIDATED"]
        if validated:
            envelope = self._envelope_from_attempt(record, run_case_key, validated[-1])
            self._validate_envelope(record, run_case_key, envelope)
            _publish_immutable(envelope_path, canonical_json_bytes(envelope) + b"\n")
            return CaseRunResult(envelope=envelope, resumed=True)
        if any(row.get("status") in {"CONTRACT_ERROR", "NON_RETRYABLE_ERROR", "TERMINAL_ERROR"} for row in attempts):
            raise ReviewContractError(f"case already ended without a valid output: {record['review_id']}")

        start_attempt = len(attempts) + 1
        retryable_types = (RetryableReviewError, TimeoutError, ConnectionError, subprocess.TimeoutExpired)
        for attempt_number in range(start_attempt, self.max_attempts + 1):
            started_at = _utc_now()
            started = time.perf_counter()
            try:
                reviewer_result = self.reviewer(record["packet"])
                if not isinstance(reviewer_result, ReviewerResult):
                    raise ReviewContractError("reviewer did not return ReviewerResult")
                output = reviewer_result.output
                if not isinstance(output, dict):
                    raise ReviewContractError("reviewer output is not an object")
                reviewer_audit = _reviewer_audit(reviewer_result)
                self._verify_expected_reviewer(reviewer_audit)
                errors = _identity_errors(record["packet"], output)
                if self.validator is not None:
                    errors.extend(str(error) for error in self.validator(record["packet"], output))
                errors = sorted(set(errors))
                if errors:
                    raise ReviewContractError("; ".join(errors))
            except retryable_types as exc:
                terminal = attempt_number >= self.max_attempts
                attempt_row = {
                    "runner_version": RUNNER_VERSION,
                    "case_key": record["case_key"],
                    "run_case_key": run_case_key,
                    "run_number": self.run_number,
                    "attempt": attempt_number,
                    "status": "TERMINAL_ERROR" if terminal else "RETRYABLE_ERROR",
                    "technical_failure": True,
                    "started_at": started_at,
                    "finished_at": _utc_now(),
                    "elapsed_seconds": round(time.perf_counter() - started, 6),
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
                self._publish_attempt(run_case_key, attempt_number, attempt_row)
                if terminal:
                    raise RetryableReviewError(
                        f"technical retries exhausted for {record['review_id']}: {exc}"
                    ) from exc
                delay_index = attempt_number - 1
                delay = self.retry_delays[delay_index] if delay_index < len(self.retry_delays) else 0.0
                if delay > 0:
                    self.sleeper(delay)
                continue
            except Exception as exc:
                status = "CONTRACT_ERROR" if isinstance(exc, ReviewContractError) else "NON_RETRYABLE_ERROR"
                attempt_row = {
                    "runner_version": RUNNER_VERSION,
                    "case_key": record["case_key"],
                    "run_case_key": run_case_key,
                    "run_number": self.run_number,
                    "attempt": attempt_number,
                    "status": status,
                    "technical_failure": False,
                    "started_at": started_at,
                    "finished_at": _utc_now(),
                    "elapsed_seconds": round(time.perf_counter() - started, 6),
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
                self._publish_attempt(run_case_key, attempt_number, attempt_row)
                raise ReviewContractError(f"non-retryable review failure for {record['review_id']}: {exc}") from exc

            attempt_row = {
                "runner_version": RUNNER_VERSION,
                "case_key": record["case_key"],
                "run_case_key": run_case_key,
                "run_number": self.run_number,
                "attempt": attempt_number,
                "status": "VALIDATED",
                "technical_failure": False,
                "started_at": started_at,
                "finished_at": _utc_now(),
                "elapsed_seconds": round(time.perf_counter() - started, 6),
                "output_sha256": canonical_sha256(output),
                "reviewer_audit_sha256": canonical_sha256(reviewer_audit),
                "reviewer_audit": reviewer_audit,
                "output": output,
            }
            self._publish_attempt(run_case_key, attempt_number, attempt_row)
            envelope = self._envelope_from_attempt(record, run_case_key, attempt_row)
            _publish_immutable(envelope_path, canonical_json_bytes(envelope) + b"\n")
            return CaseRunResult(envelope=envelope, resumed=False)
        raise RetryableReviewError(f"technical retries already exhausted for {record['review_id']}")

    def run_cases(self, records: Sequence[dict[str, Any]]) -> dict[str, Any]:
        results = [self.run_case(record) for record in sorted(records, key=lambda row: int(row["source_ordinal"]))]
        return {
            "runner_version": RUNNER_VERSION,
            "run_number": self.run_number,
            "expected_rows": len(records),
            "valid_rows": len(results),
            "resumed_rows": sum(result.resumed for result in results),
            "new_rows": sum(not result.resumed for result in results),
            "complete": len(results) == len(records),
        }


def _validate_merge_envelope(record: dict[str, Any], envelope: dict[str, Any], run_number: int) -> None:
    _validate_case_record(record)
    expected_run_key = build_run_case_key(str(record["case_key"]), run_number)
    checks = {
        "runner_version": RUNNER_VERSION,
        "status": "VALID",
        "run_number": run_number,
        "source_ordinal": record["source_ordinal"],
        "review_id": record["review_id"],
        "case_key": record["case_key"],
        "run_case_key": expected_run_key,
        "packet_sha256": record["packet_sha256"],
        "source_manifest_sha256": record["source_manifest_sha256"],
        "protocol_sha256": record["protocol_sha256"],
        "prompt_sha256": record["prompt_sha256"],
        "schema_sha256": record["schema_sha256"],
        "execution_contract_sha256": record["execution_contract_sha256"],
        "model": record["model"],
        "reasoning_effort": record["reasoning_effort"],
    }
    for field, expected in checks.items():
        if envelope.get(field) != expected:
            raise ExecutionIntegrityError(f"merge envelope changed {field}: {record['review_id']}")
    output = envelope.get("output")
    if not isinstance(output, dict) or canonical_sha256(output) != envelope.get("output_sha256"):
        raise ExecutionIntegrityError(f"merge output hash mismatch: {record['review_id']}")
    audit = envelope.get("reviewer_audit")
    _validate_reviewer_audit(audit)
    if canonical_sha256(audit) != envelope.get("reviewer_audit_sha256"):
        raise ExecutionIntegrityError(f"merge reviewer audit hash mismatch: {record['review_id']}")


def merge_envelopes(
    records: Sequence[dict[str, Any]],
    *,
    envelope_dirs: Sequence[Path],
    run_number: int,
    output_path: Path,
    manifest_path: Path | None = None,
) -> dict[str, Any]:
    """Merge VALID envelopes in source order after exact coverage validation."""
    expected = {str(record["case_key"]): record for record in records}
    if len(expected) != len(records):
        raise ExecutionIntegrityError("duplicate expected case_key")
    found: dict[str, dict[str, Any]] = {}
    for requested in envelope_dirs:
        directory = Path(requested)
        case_dir = directory / "cases" if (directory / "cases").is_dir() else directory
        for path in sorted(case_dir.glob("*.json")):
            envelope = _read_json(path)
            case_key = str(envelope.get("case_key") or "")
            if case_key not in expected:
                raise ExecutionIntegrityError(f"unexpected envelope: {path}")
            if case_key in found:
                raise ExecutionIntegrityError(f"overlapping envelope for case: {case_key}")
            _validate_merge_envelope(expected[case_key], envelope, run_number)
            found[case_key] = envelope
    missing = sorted(set(expected) - set(found))
    if missing:
        raise ExecutionIntegrityError(f"missing {len(missing)} envelopes; first={missing[0]}")

    ordered_records = sorted(records, key=lambda row: int(row["source_ordinal"]))
    ordinals = [int(record["source_ordinal"]) for record in ordered_records]
    if ordinals != list(range(len(records))):
        raise ExecutionIntegrityError("source ordinals are not an exact zero-based sequence")
    uniform_fields = (
        "source_manifest_sha256", "protocol_sha256", "prompt_sha256", "schema_sha256",
        "execution_contract_sha256", "model", "reasoning_effort",
    )
    for field in uniform_fields:
        if len({record.get(field) for record in ordered_records}) > 1:
            raise ExecutionIntegrityError(f"merge records differ in formal field: {field}")
    reviewer_identity_fields = ("adapter_version", "adapter_status", "adapter_code_sha256")
    for field in reviewer_identity_fields:
        values = {
            found[str(record["case_key"])]["reviewer_audit"].get(field)
            for record in ordered_records
        }
        if len(values) > 1:
            raise ExecutionIntegrityError(f"merge envelopes differ in reviewer audit: {field}")
    outputs = [found[str(record["case_key"])]["output"] for record in ordered_records]
    payload = b"".join(canonical_json_bytes(output) + b"\n" for output in outputs)
    _publish_immutable(Path(output_path), payload)
    ledger_sha256 = __import__("hashlib").sha256(payload).hexdigest()
    manifest = {
        "runner_version": RUNNER_VERSION,
        "status": "COMPLETE",
        "run_number": int(run_number),
        "expected_rows": len(records),
        "valid_rows": len(found),
        "unique_rows": len(found),
        "missing_rows": 0,
        "overlap_rows": 0,
        "unexpected_rows": 0,
        "source_manifest_sha256": ordered_records[0]["source_manifest_sha256"] if ordered_records else None,
        "protocol_sha256": ordered_records[0]["protocol_sha256"] if ordered_records else None,
        "prompt_sha256": ordered_records[0]["prompt_sha256"] if ordered_records else None,
        "schema_sha256": ordered_records[0]["schema_sha256"] if ordered_records else None,
        "execution_contract_sha256": (
            ordered_records[0]["execution_contract_sha256"] if ordered_records else None
        ),
        "model": ordered_records[0]["model"] if ordered_records else None,
        "reasoning_effort": ordered_records[0]["reasoning_effort"] if ordered_records else None,
        "reviewer_identity": (
            {
                field: found[str(ordered_records[0]["case_key"])]["reviewer_audit"][field]
                for field in reviewer_identity_fields
            }
            if ordered_records else None
        ),
        "ledger_path": str(Path(output_path).resolve()),
        "ledger_sha256": ledger_sha256,
        "case_order_sha256": canonical_sha256([record["case_key"] for record in ordered_records]),
        "reviewer_audit_order_sha256": canonical_sha256(
            [found[str(record["case_key"])]["reviewer_audit_sha256"] for record in ordered_records]
        ),
    }
    manifest_path = Path(manifest_path) if manifest_path is not None else Path(output_path).with_suffix(".manifest.json")
    _publish_immutable(manifest_path, canonical_json_bytes(manifest) + b"\n")
    return manifest
