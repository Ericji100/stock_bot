"""Apply the 20% operational guard and dispatch v7's pinned stage runner."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
from typing import Any, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.v2_core_codex_staged_runner_v1 import (
    OUT,
    StagedRunnerError,
    assert_budget_allows,
    parse_usage_attestation,
    verify_execution_manifest,
)
from scripts.v2_core_source_audit_v1 import sha256


GUARD_VERSION = "v2-core-operational-budget-guard-r2"
EXPECTED_MANIFEST_VERSION = "v2-core-m2a-triplicate-execution-r7-candidate"
EXPECTED_POLICY_VERSION = "v2-core-operational-budget-override-v3"
EXPECTED_THRESHOLD = 20.0
EXPECTED_RUNNERS = {
    "STAGE_B": "scripts/v2_core_codex_staged_runner_v1.py",
    "STAGE_D": "scripts/v2_core_codex_stage_d_runner_v2.py",
}


def load_policy(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    required = {
        "policy_version",
        "status",
        "effective_date",
        "scope",
        "usage_source",
        "usage_limit_id",
        "stop_new_ai_calls_when_remaining_percent_lte",
        "allow_started_single_call_to_finish",
        "preserves_frozen_experiment_inputs",
        "supersedes_operational_threshold_percent",
        "does_not_change",
    }
    if set(value) != required:
        raise StagedRunnerError("operational budget policy fields differ from contract")
    if value["policy_version"] != EXPECTED_POLICY_VERSION or value["status"] != "ACTIVE":
        raise StagedRunnerError("operational budget policy is not the active v3 override")
    if value["usage_source"] != "CODEX_APP_GET_USAGE_LIMITS" or value["usage_limit_id"] != "codex":
        raise StagedRunnerError("operational budget source/limit mismatch")
    threshold = value["stop_new_ai_calls_when_remaining_percent_lte"]
    if isinstance(threshold, bool) or float(threshold) != EXPECTED_THRESHOLD:
        raise StagedRunnerError("operational budget threshold must be 20 percent")
    if value["preserves_frozen_experiment_inputs"] is not True:
        raise StagedRunnerError("operational override must preserve frozen experiment inputs")
    return value


def preflight(
    *,
    artifact_dir: Path,
    manifest_path: Path,
    policy_path: Path,
    usage_attestation_path: Path,
) -> dict[str, Any]:
    manifest = verify_execution_manifest(manifest_path.resolve(), artifact_dir.resolve())
    if manifest.get("execution_manifest_version") != EXPECTED_MANIFEST_VERSION:
        raise StagedRunnerError("execution manifest is not v7")
    if manifest.get("stage_runners") != EXPECTED_RUNNERS:
        raise StagedRunnerError("v7 stage runner map mismatch")
    policy = load_policy(policy_path.resolve())
    if policy["scope"] != manifest_path.name:
        raise StagedRunnerError("operational budget policy scope mismatch")
    embedded_threshold = float(manifest["budget_stop_remaining_percent_lte"])
    threshold = float(policy["stop_new_ai_calls_when_remaining_percent_lte"])
    if threshold < embedded_threshold:
        raise StagedRunnerError("operational threshold must be at least as strict as manifest")
    attestation = parse_usage_attestation(usage_attestation_path.resolve())
    assert_budget_allows(attestation, threshold)
    return {
        "status": "BUDGET_OK（額度允許）",
        "guard_version": GUARD_VERSION,
        "policy_sha256": sha256(policy_path.resolve()),
        "execution_manifest_sha256": sha256(manifest_path.resolve()),
        "remaining_percent": attestation["remaining_percent"],
        "operational_stop_threshold_percent": threshold,
        "embedded_manifest_threshold_percent": embedded_threshold,
    }


def runner_command(args: argparse.Namespace) -> list[str]:
    common = [
        "--artifact-dir",
        str(args.artifact_dir.resolve()),
        "--execution-manifest",
        str(args.execution_manifest.resolve()),
        "--usage-attestation",
        str(args.usage_attestation.resolve()),
        "--round",
        str(args.round),
        "--shard-id",
        str(args.shard_id),
        "--timeout-seconds",
        str(args.timeout_seconds),
    ]
    if args.stage == "stage_b":
        return [
            sys.executable,
            str(ROOT / EXPECTED_RUNNERS["STAGE_B"]),
            *common,
            "--stage",
            "stage_b",
        ]
    return [sys.executable, str(ROOT / EXPECTED_RUNNERS["STAGE_D"]), *common]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-dir", type=Path, default=OUT)
    parser.add_argument("--execution-manifest", type=Path, required=True)
    parser.add_argument("--operational-policy", type=Path, required=True)
    parser.add_argument("--usage-attestation", type=Path, required=True)
    parser.add_argument("--round", type=int, choices=(1, 2, 3), required=True)
    parser.add_argument("--shard-id", type=int, required=True)
    parser.add_argument("--stage", choices=("stage_b", "stage_d"), required=True)
    parser.add_argument("--timeout-seconds", type=int, default=1200)
    args = parser.parse_args(argv)
    check = preflight(
        artifact_dir=args.artifact_dir,
        manifest_path=args.execution_manifest,
        policy_path=args.operational_policy,
        usage_attestation_path=args.usage_attestation,
    )
    print(json.dumps(check, ensure_ascii=False, indent=2), flush=True)
    result = subprocess.run(runner_command(args), cwd=ROOT, check=False)
    return int(result.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
