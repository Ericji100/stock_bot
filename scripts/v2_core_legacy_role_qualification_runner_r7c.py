"""R7C fixed-key output runner; role semantics remain the frozen R7B prompt."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import v2_core_codex_staged_runner_v1 as base
from scripts import v2_core_legacy_role_qualification_runner_r7b as r7b_runner
from scripts.v2_core_legacy_role_qualification_r7c import parse_raw_without_duplicate_keys, transport_for, validate_and_resolve
from scripts.v2_core_legacy_role_shortlist_r7 import ARTIFACT_DIR, canonical_bytes, load_json, sha256_path


VERSION = "v2-core-legacy-role-qualification-runner-r7c-candidate-r1"
ADDENDUM_FILE = "v2_core_legacy_role_qualification.transport_addendum.candidate_r7c.md"
MANIFEST_DIRECTORY = "legacy_role_qualification_manifests_candidate_r7c"
OUTPUT_DIRECTORY = "legacy_role_qualification_runs_candidate_r7c"
RunCommand = Callable[..., subprocess.CompletedProcess[str]]
RunnerError = base.StagedRunnerError


def manifest_path(artifact_dir: Path, review_id: str) -> Path:
    return artifact_dir / MANIFEST_DIRECTORY / f"{review_id}.json"


def output_paths(artifact_dir: Path, review_id: str, role: str) -> tuple[Path, Path]:
    output = artifact_dir / OUTPUT_DIRECTORY / role / f"{review_id}.json"
    return output, output.with_name(output.stem + ".receipt.json")


def build_manifest(artifact_dir: Path, review_id: str) -> dict[str, Any]:
    source = r7b_runner.build_manifest(artifact_dir, review_id)
    _r7_manifest, _row, packet, shortlist = r7b_runner._bound_inputs(artifact_dir, review_id, r7b_runner.EPISODE_ROLE)
    episode, _paths = r7b_runner.frozen_episode(artifact_dir, review_id)
    rows = []
    for role in sorted(shortlist["roles"]):
        if role == r7b_runner.EPISODE_ROLE:
            continue
        schema = transport_for(packet, shortlist, role, episode)
        rows.append({
            "role": role,
            "candidate_count": shortlist["roles"][role]["shortlist_count"],
            "transport_schema_sha256": hashlib.sha256(canonical_bytes(schema)).hexdigest(),
        })
    contract_path = ROOT / "scripts" / "v2_core_legacy_role_qualification_r7c.py"
    return {
        **source,
        "manifest_version": VERSION,
        "source_r7b_manifest_sha256": hashlib.sha256(canonical_bytes(source)).hexdigest(),
        "base_prompt_file": source["prompt_file"],
        "base_prompt_sha256": source["prompt_sha256"],
        "transport_addendum_file": ADDENDUM_FILE,
        "transport_addendum_sha256": sha256_path(artifact_dir / ADDENDUM_FILE),
        "contract_file": contract_path.name,
        "contract_sha256": sha256_path(contract_path),
        "role_rows": rows,
    }


def prepare(artifact_dir: Path, review_id: str) -> dict[str, Any]:
    manifest = build_manifest(artifact_dir, review_id)
    base.write_new_or_identical(manifest_path(artifact_dir, review_id), canonical_bytes(manifest))
    return manifest


def _bound_manifest(artifact_dir: Path, review_id: str, role: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    stored = load_json(manifest_path(artifact_dir, review_id))
    if stored != build_manifest(artifact_dir, review_id):
        raise RunnerError("R7C manifest bindings changed")
    matches = [row for row in stored["role_rows"] if row["role"] == role]
    if len(matches) != 1:
        raise RunnerError("R7C role not uniquely listed")
    _r7_manifest, _row, packet, shortlist = r7b_runner._bound_inputs(artifact_dir, review_id, role)
    episode, _paths = r7b_runner.frozen_episode(artifact_dir, review_id)
    schema = transport_for(packet, shortlist, role, episode)
    if hashlib.sha256(canonical_bytes(schema)).hexdigest() != matches[0]["transport_schema_sha256"]:
        raise RunnerError("R7C transport schema changed")
    return stored, packet, shortlist, episode, schema


def rendered_prompt(artifact_dir: Path, packet: dict[str, Any], shortlist: dict[str, Any], role: str, episode: dict[str, Any]) -> str:
    base_text = (artifact_dir / r7b_runner.PROMPT_FILE).read_text(encoding="utf-8")
    addendum = (artifact_dir / ADDENDUM_FILE).read_text(encoding="utf-8")
    return r7b_runner.rendered_prompt(base_text.rstrip() + "\n\n" + addendum, packet, shortlist, role, episode)


def run_role(
    *, artifact_dir: Path, review_id: str, role: str, usage_attestation_path: Path,
    timeout_seconds: int = 1200, run_command: RunCommand = subprocess.run,
) -> dict[str, Any]:
    manifest, packet, shortlist, episode, schema = _bound_manifest(artifact_dir, review_id, role)
    output, receipt_path = output_paths(artifact_dir, review_id, role)
    raw, invalid = base.raw_output_path(output), base.invalid_output_path(output)
    if any(path.exists() for path in (output, receipt_path, raw, invalid)):
        raise RunnerError("R7C output exists; validate instead of re-running")
    attestation = base.parse_usage_attestation(usage_attestation_path)
    base.assert_budget_allows(attestation, manifest["stop_remaining_percent_lte"])
    prompt = rendered_prompt(artifact_dir, packet, shortlist, role, episode)
    response, receipt, raw_bytes = base.call_codex(
        prompt=prompt, transport_schema=schema, timeout_seconds=timeout_seconds,
        invalid_raw_path=invalid, run_command=run_command,
    )
    try:
        duplicate_checked = parse_raw_without_duplicate_keys(raw_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        base.write_new_or_identical(invalid, raw_bytes)
        raise RunnerError("R7C raw JSON duplicate/parse validation failed") from exc
    if duplicate_checked != response:
        base.write_new_or_identical(invalid, raw_bytes)
        raise RunnerError("R7C raw/transport response mismatch")
    validation = validate_and_resolve(response, packet=packet, shortlist=shortlist, role=role, episode_result=episode)
    if validation["status"] != "VALID":
        base.write_new_or_identical(invalid, raw_bytes)
        raise RunnerError("R7C local validation failed: " + json.dumps(validation, ensure_ascii=False))
    receipt = {
        **receipt,
        "runner_version": VERSION,
        "stage": "R7C_EPISODE_BOUND_FIXED_KEY_QUALIFICATION",
        "review_id": review_id,
        "as_of": packet["as_of"],
        "role": role,
        "manifest_sha256": sha256_path(manifest_path(artifact_dir, review_id)),
        "bound_episode_candidate_id": manifest["frozen_episode_candidate_id"],
        "base_prompt_sha256": manifest["base_prompt_sha256"],
        "transport_addendum_sha256": manifest["transport_addendum_sha256"],
        "contract_sha256": manifest["contract_sha256"],
        "usage_attestation_sha256": sha256_path(usage_attestation_path),
        "stop_remaining_percent_lte": manifest["stop_remaining_percent_lte"],
        "validation": validation,
    }
    base.write_new_or_identical(raw, raw_bytes)
    base.write_new_or_identical(output, canonical_bytes(response))
    base.write_new_or_identical(receipt_path, canonical_bytes(receipt))
    return validation


def validate_existing(*, artifact_dir: Path, review_id: str, role: str) -> dict[str, Any]:
    manifest, packet, shortlist, episode, schema = _bound_manifest(artifact_dir, review_id, role)
    output, receipt_path = output_paths(artifact_dir, review_id, role)
    raw = base.raw_output_path(output)
    if not all(path.is_file() for path in (output, receipt_path, raw)):
        raise RunnerError("R7C output/raw/receipt incomplete")
    response = load_json(output)
    if parse_raw_without_duplicate_keys(raw.read_bytes()) != response:
        raise RunnerError("R7C raw/normalized mismatch")
    validation = validate_and_resolve(response, packet=packet, shortlist=shortlist, role=role, episode_result=episode)
    if validation["status"] != "VALID":
        raise RunnerError("R7C existing output invalid")
    prompt = rendered_prompt(artifact_dir, packet, shortlist, role, episode)
    receipt = load_json(receipt_path)
    checks = {
        "runner_version": VERSION,
        "model": r7b_runner.MODEL,
        "reasoning_effort": r7b_runner.REASONING,
        "stage": "R7C_EPISODE_BOUND_FIXED_KEY_QUALIFICATION",
        "review_id": review_id,
        "as_of": packet["as_of"],
        "role": role,
        "manifest_sha256": sha256_path(manifest_path(artifact_dir, review_id)),
        "bound_episode_candidate_id": manifest["frozen_episode_candidate_id"],
        "base_prompt_sha256": manifest["base_prompt_sha256"],
        "transport_addendum_sha256": manifest["transport_addendum_sha256"],
        "contract_sha256": manifest["contract_sha256"],
        "transport_schema_sha256": hashlib.sha256(canonical_bytes(schema)).hexdigest(),
        "rendered_prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "raw_output_sha256": hashlib.sha256(raw.read_bytes()).hexdigest(),
        "normalized_output_sha256": hashlib.sha256(canonical_bytes(response)).hexdigest(),
    }
    for key, expected in checks.items():
        if receipt.get(key) != expected:
            raise RunnerError(f"R7C receipt mismatch: {key}")
    if receipt.get("validation") != validation:
        raise RunnerError("R7C receipt validation mismatch")
    return validation


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-dir", type=Path, default=ARTIFACT_DIR)
    parser.add_argument("--review-id", required=True)
    parser.add_argument("--role")
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--validate-existing", action="store_true")
    parser.add_argument("--usage-attestation", type=Path)
    parser.add_argument("--timeout-seconds", type=int, default=1200)
    args = parser.parse_args()
    if args.prepare:
        result = prepare(args.artifact_dir, args.review_id)
    else:
        if not args.role:
            parser.error("--role required unless preparing")
        if args.validate_existing:
            result = validate_existing(artifact_dir=args.artifact_dir, review_id=args.review_id, role=args.role)
        else:
            if args.usage_attestation is None:
                parser.error("--usage-attestation required for formal call")
            result = run_role(
                artifact_dir=args.artifact_dir, review_id=args.review_id, role=args.role,
                usage_attestation_path=args.usage_attestation, timeout_seconds=args.timeout_seconds,
            )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
