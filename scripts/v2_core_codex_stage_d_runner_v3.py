"""v8 STAGE_D runner with a receipt hash over the final normalized output."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile
from typing import Any, Sequence

from scripts import v2_core_codex_stage_d_runner_v2 as previous
from scripts import v2_core_codex_staged_runner_v1 as base


RUNNER_VERSION = "v2-core-codex-stage-d-runner-r6"
STAGE_D_PROMPT_FILENAME = previous.STAGE_D_PROMPT_FILENAME
_PREVIOUS_RUN = previous.run_stage_d_case
_PREVIOUS_VALIDATE = previous.validate_existing_stage_d_artifacts


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
    run_command: base.RunCommand = base.subprocess.run,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="v2-core-stage-d-receipt-") as temporary:
        temporary_receipt = Path(temporary) / "receipt.json"
        old_version = previous.RUNNER_VERSION
        previous.RUNNER_VERSION = RUNNER_VERSION
        try:
            result = _PREVIOUS_RUN(
                packet_path=packet_path,
                stage_b_path=stage_b_path,
                stage_b_schema_path=stage_b_schema_path,
                stage_b_prompt_path=stage_b_prompt_path,
                schema_path=schema_path,
                prompt_path=prompt_path,
                truth_table_path=truth_table_path,
                output_path=output_path,
                route_path=route_path,
                permission_path=permission_path,
                receipt_path=temporary_receipt,
                timeout_seconds=timeout_seconds,
                run_command=run_command,
            )
        finally:
            previous.RUNNER_VERSION = old_version
        if result["status"] == "PROGRAM_UNKNOWN_NO_STAGE_D_CALL":
            if temporary_receipt.exists():
                raise base.StagedRunnerError("unresolved route unexpectedly wrote a receipt")
            return result
        if not temporary_receipt.is_file() or not output_path.is_file():
            raise base.StagedRunnerError("STAGE_D did not publish normalized output and receipt")
        receipt = base.load_json(temporary_receipt)
        receipt["runner_version"] = RUNNER_VERSION
        receipt["normalized_output_sha256"] = base.canonical_sha256(
            base.load_json(output_path)
        )
        base.write_new_or_identical(receipt_path, base.canonical_bytes(receipt))
        return result


def validate_existing_stage_d_artifacts(**kwargs: Any) -> dict[str, Any]:
    old_version = previous.RUNNER_VERSION
    previous.RUNNER_VERSION = RUNNER_VERSION
    try:
        return _PREVIOUS_VALIDATE(**kwargs)
    finally:
        previous.RUNNER_VERSION = old_version


def main(argv: Sequence[str] | None = None) -> int:
    original_run = previous.run_stage_d_case
    original_validate = previous.validate_existing_stage_d_artifacts
    original_version = previous.RUNNER_VERSION
    previous.run_stage_d_case = run_stage_d_case
    previous.validate_existing_stage_d_artifacts = validate_existing_stage_d_artifacts
    previous.RUNNER_VERSION = RUNNER_VERSION
    try:
        return previous.main(argv)
    finally:
        previous.run_stage_d_case = original_run
        previous.validate_existing_stage_d_artifacts = original_validate
        previous.RUNNER_VERSION = original_version


if __name__ == "__main__":
    raise SystemExit(main())
