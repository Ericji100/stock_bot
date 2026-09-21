"""Budget-guarded, fail-closed execution wrapper for one V4-S1 shard/run.

This local module cannot query Codex account usage.  Before *every* shard, the
parent/UI orchestrator must call the Codex app usage-limit API and supply a
fresh attestation whose source is exactly ``CODEX_APP_GET_USAGE_LIMITS``.

All frozen research inputs are verified before a receipt is written.  A
remaining percentage at or below the frozen threshold writes an immutable
resume checkpoint and never calls the AI launcher.  A value above the
threshold writes an immutable pass receipt before delegating exactly once to
the unchanged V3 launcher.  The existing runner remains responsible for
case-level immutable resume behavior.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sys
from types import SimpleNamespace
from typing import Any, Callable, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from . import hybrid_v3_codex_launcher_v2 as launcher_v2
    from . import hybrid_v4_s1_consistency_v1 as consistency_v1
    from .hybrid_v3_sharding_v2 import (
        ShardIntegrityError,
        _publish_immutable,
        canonical_json_bytes,
        canonical_sha256,
        file_sha256,
    )
except ImportError:  # pragma: no cover - direct script execution
    from scripts import hybrid_v3_codex_launcher_v2 as launcher_v2
    from scripts import hybrid_v4_s1_consistency_v1 as consistency_v1
    from scripts.hybrid_v3_sharding_v2 import (
        ShardIntegrityError,
        _publish_immutable,
        canonical_json_bytes,
        canonical_sha256,
        file_sha256,
    )


ORCHESTRATOR_VERSION = "hybrid-v4-s1-execution-orchestrator-v1"
RECEIPT_VERSION = "hybrid-v4-s1-budget-receipt-v1"
USAGE_ATTESTATION_VERSION = "hybrid-v4-s1-codex-usage-attestation-v1"
USAGE_SOURCE = "CODEX_APP_GET_USAGE_LIMITS"
EXPECTED_MODEL = "gpt-5.6-sol"
EXPECTED_REASONING = "xhigh"
EXPECTED_CASES = 36
EXPECTED_RUNS = 3
EXPECTED_THRESHOLD = 20.0
MAX_ATTESTATION_AGE_SECONDS = 300
MAX_FUTURE_SKEW_SECONDS = 30
DEFAULT_TIMEOUT_SECONDS = 1200
DEFAULT_MAX_ATTEMPTS = 3

STATUS_USAGE_BOUND = "USAGE_CHECK_BOUND / 用量檢查已綁定"
STATUS_BUDGET_STOP = "BUDGET_STOP / 額度保護停止"
STATUS_BUDGET_PASS = "BUDGET_PASS / 額度檢查通過"
STATUS_DISPATCHED = "EXECUTION_DISPATCHED / 已派送執行"

_ATTESTATION_FIELDS = frozenset(
    {"attestation_version", "usage_source", "remaining_percent", "checked_at", "check_id"}
)
_CHECK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
Launcher = Callable[[Any], Any]


class V4S1ExecutionOrchestratorError(ValueError):
    """A budget attestation or frozen single-shard execution contract failed."""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise V4S1ExecutionOrchestratorError(
            f"cannot read frozen JSON / 無法讀取凍結 JSON: {path}"
        ) from exc
    if not isinstance(value, dict):
        raise V4S1ExecutionOrchestratorError(
            f"expected JSON object / 必須是 JSON 物件: {path}"
        )
    return value


def _require_external_hash(label: str, expected: str, path: Path) -> str:
    supplied = str(expected or "").lower()
    if len(supplied) != 64 or any(char not in "0123456789abcdef" for char in supplied):
        raise V4S1ExecutionOrchestratorError(
            f"invalid expected {label} SHA-256 / {label} 預期雜湊無效"
        )
    resolved = Path(path).resolve()
    if not resolved.is_file() or file_sha256(resolved) != supplied:
        raise V4S1ExecutionOrchestratorError(
            f"changed {label} hash / {label} 雜湊已變更"
        )
    return supplied


def _parse_checked_at(value: Any) -> datetime:
    text = str(value or "")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError as exc:
        raise V4S1ExecutionOrchestratorError(
            "checked_at must be ISO-8601 / checked_at 必須是 ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise V4S1ExecutionOrchestratorError(
            "checked_at needs a timezone / checked_at 必須含時區"
        )
    return parsed.astimezone(timezone.utc)


def validate_usage_attestation(
    attestation: Mapping[str, Any], *, now: datetime | None = None
) -> dict[str, Any]:
    """Validate and canonically normalize one externally obtained usage check."""

    if not isinstance(attestation, Mapping) or set(attestation) != _ATTESTATION_FIELDS:
        raise V4S1ExecutionOrchestratorError(
            "usage attestation fields are incomplete or unexpected / 用量證明欄位不完整或多餘"
        )
    if attestation.get("attestation_version") != USAGE_ATTESTATION_VERSION:
        raise V4S1ExecutionOrchestratorError(
            "unsupported usage attestation version / 不支援的用量證明版本"
        )
    if attestation.get("usage_source") != USAGE_SOURCE:
        raise V4S1ExecutionOrchestratorError(
            "usage source must be CODEX_APP_GET_USAGE_LIMITS / 用量來源必須是 CODEX_APP_GET_USAGE_LIMITS"
        )

    remaining = attestation.get("remaining_percent")
    if (
        isinstance(remaining, bool)
        or not isinstance(remaining, (int, float))
        or not math.isfinite(float(remaining))
        or not 0 <= float(remaining) <= 100
    ):
        raise V4S1ExecutionOrchestratorError(
            "remaining_percent must be within [0,100] / 剩餘百分比必須介於 0 到 100"
        )
    check_id = str(attestation.get("check_id") or "")
    if not _CHECK_ID.fullmatch(check_id):
        raise V4S1ExecutionOrchestratorError(
            "check_id is not stable or safe / check_id 不穩定或不安全"
        )

    checked_at = _parse_checked_at(attestation.get("checked_at"))
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise V4S1ExecutionOrchestratorError(
            "internal now needs a timezone / 內部時間必須含時區"
        )
    current = current.astimezone(timezone.utc)
    age = (current - checked_at).total_seconds()
    if age < -MAX_FUTURE_SKEW_SECONDS:
        raise V4S1ExecutionOrchestratorError(
            "usage attestation is from the future / 用量證明時間在未來"
        )
    if age > MAX_ATTESTATION_AGE_SECONDS:
        raise V4S1ExecutionOrchestratorError(
            "usage attestation is stale / 用量證明已過期"
        )

    normalized_remaining: int | float = float(remaining)
    if normalized_remaining.is_integer():
        normalized_remaining = int(normalized_remaining)
    core = {
        "attestation_version": USAGE_ATTESTATION_VERSION,
        "usage_source": USAGE_SOURCE,
        "remaining_percent": normalized_remaining,
        "checked_at": checked_at.isoformat().replace("+00:00", "Z"),
        "check_id": check_id,
    }
    return {**core, "attestation_sha256": canonical_sha256(core)}


def _assignment_shard(
    assignment_manifest: Path, shard_id: int, expected_shard_sha256: str
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    if isinstance(shard_id, bool) or not isinstance(shard_id, int) or shard_id < 0:
        raise V4S1ExecutionOrchestratorError(
            "shard_id must be a non-negative integer / shard_id 必須是非負整數"
        )
    assignment = _read_json(assignment_manifest)
    shards = assignment.get("shards")
    if not isinstance(shards, list):
        raise V4S1ExecutionOrchestratorError(
            "assignment has no shards / assignment 缺少 shards"
        )
    matches = [
        dict(row)
        for row in shards
        if isinstance(row, Mapping) and row.get("shard_id") == shard_id
    ]
    if len(matches) != 1:
        raise V4S1ExecutionOrchestratorError(
            "shard is missing or duplicated / shard 缺失或重複"
        )
    entry = matches[0]
    shard_path = Path(str(entry.get("path") or "")).resolve()
    expected = _require_external_hash("shard", expected_shard_sha256, shard_path)
    if entry.get("sha256") != expected:
        raise V4S1ExecutionOrchestratorError(
            "assignment shard hash changed / assignment 的 shard 雜湊已變更"
        )
    return shard_path, entry, assignment


def _publish_record(path: Path, core: Mapping[str, Any], hash_field: str) -> dict[str, Any]:
    record = {**dict(core), hash_field: canonical_sha256(dict(core))}
    try:
        _publish_immutable(Path(path), canonical_json_bytes(record) + b"\n")
    except ShardIntegrityError as exc:
        raise V4S1ExecutionOrchestratorError(
            f"conflicting immutable receipt / 不可變收據衝突: {path}"
        ) from exc
    return record


def _bind_usage_check(
    *,
    control_dir: Path,
    attestation: Mapping[str, Any],
    run_number: int,
    shard_id: int,
    frozen_hashes: Mapping[str, str],
) -> tuple[dict[str, Any], Path]:
    check_id = str(attestation["check_id"])
    filename = hashlib.sha256(check_id.encode("utf-8")).hexdigest() + ".json"
    path = Path(control_dir) / "usage_checks" / filename
    core = {
        "registry_version": ORCHESTRATOR_VERSION,
        "status": STATUS_USAGE_BOUND,
        "check_id": check_id,
        "attestation_sha256": attestation["attestation_sha256"],
        "usage_source": attestation["usage_source"],
        "remaining_percent": attestation["remaining_percent"],
        "checked_at": attestation["checked_at"],
        "run_number": run_number,
        "shard_id": shard_id,
        "frozen_hashes": dict(frozen_hashes),
    }
    return _publish_record(path, core, "registry_sha256"), path


def _receipt_core(
    *,
    status: str,
    action: str,
    attestation: Mapping[str, Any],
    run_number: int,
    shard_id: int,
    shard_path: Path,
    run_root: Path,
    frozen_hashes: Mapping[str, str],
    execution_contract: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "receipt_version": RECEIPT_VERSION,
        "orchestrator_version": ORCHESTRATOR_VERSION,
        "status": status,
        "action": action,
        "usage_attestation": dict(attestation),
        "run_number": run_number,
        "shard_id": shard_id,
        "shard_path": str(Path(shard_path).resolve()),
        "run_root": str(Path(run_root).resolve()),
        "frozen_hashes": dict(frozen_hashes),
        "execution_contract": dict(execution_contract),
        "budget_threshold_remaining_percent": EXPECTED_THRESHOLD,
        "parent_ui_must_supply_fresh_usage_before_every_shard": True,
        "local_script_queries_codex_usage": False,
    }


def execute_shard(
    *,
    source: Path,
    source_manifest: Path,
    track_manifest: Path,
    assignment_manifest: Path,
    execution_freeze: Path,
    stage_protocol: Path,
    research_protocol: Path,
    prompt: Path,
    schema: Path,
    policy: Path,
    run_root: Path,
    control_dir: Path,
    run_number: int,
    shard_id: int,
    usage_attestation: Mapping[str, Any],
    expected_track_sha256: str,
    expected_assignment_sha256: str,
    expected_freeze_sha256: str,
    expected_shard_sha256: str,
    unsupported_overrides: Mapping[str, Any] | None = None,
    launcher: Launcher | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Verify, budget-check, and optionally dispatch one exact frozen shard."""

    if unsupported_overrides:
        raise V4S1ExecutionOrchestratorError(
            "execution overrides are unsupported / 不允許執行覆寫"
        )
    if (
        isinstance(run_number, bool)
        or not isinstance(run_number, int)
        or not 1 <= run_number <= EXPECTED_RUNS
    ):
        raise V4S1ExecutionOrchestratorError(
            "run_number must be 1..3 / run_number 必須介於 1 到 3"
        )

    paths = [
        source,
        source_manifest,
        track_manifest,
        assignment_manifest,
        execution_freeze,
        stage_protocol,
        research_protocol,
        prompt,
        schema,
        policy,
        run_root,
        control_dir,
    ]
    (
        source,
        source_manifest,
        track_manifest,
        assignment_manifest,
        execution_freeze,
        stage_protocol,
        research_protocol,
        prompt,
        schema,
        policy,
        run_root,
        control_dir,
    ) = [Path(path).resolve() for path in paths]

    track_sha = _require_external_hash("track manifest", expected_track_sha256, track_manifest)
    assignment_sha = _require_external_hash(
        "assignment manifest", expected_assignment_sha256, assignment_manifest
    )
    freeze_sha = _require_external_hash(
        "execution freeze", expected_freeze_sha256, execution_freeze
    )
    shard_path, shard_entry, assignment = _assignment_shard(
        assignment_manifest, shard_id, expected_shard_sha256
    )
    shard_sha = str(shard_entry["sha256"])

    # This is the same native verification used before V4-S1 evaluation.  It
    # validates all 36 packets, both component chains, assignment membership,
    # self-hashes, and every case identity before any control artifact is made.
    consistency_v1.verify_frozen_track(
        source=source,
        source_manifest=source_manifest,
        track_manifest=track_manifest,
        assignment_manifest=assignment_manifest,
        execution_freeze=execution_freeze,
        stage_protocol=stage_protocol,
        research_protocol=research_protocol,
        prompt=prompt,
        schema=schema,
        policy=policy,
    )

    freeze = _read_json(execution_freeze)
    execution = freeze.get("execution_contract")
    if not isinstance(execution, Mapping) or execution != {
        "model": EXPECTED_MODEL,
        "reasoning_effort": EXPECTED_REASONING,
    }:
        raise V4S1ExecutionOrchestratorError(
            "frozen model/reasoning changed / 凍結模型或推理等級已變更"
        )
    if freeze.get("expected_cases") != EXPECTED_CASES or freeze.get(
        "expected_runs"
    ) != EXPECTED_RUNS:
        raise V4S1ExecutionOrchestratorError(
            "frozen case/run coverage changed / 凍結案例或輪次已變更"
        )
    budget = freeze.get("budget_guard")
    threshold = (
        budget.get("stop_when_remaining_percent_at_or_below")
        if isinstance(budget, Mapping)
        else None
    )
    if not isinstance(budget, Mapping) or (
        isinstance(threshold, bool)
        or not isinstance(threshold, (int, float))
        or not math.isfinite(float(threshold))
        or float(threshold) != EXPECTED_THRESHOLD
        or budget.get("preserve_resume_checkpoint") is not True
        or budget.get("enforcement_boundary") != "EXTERNAL_EXECUTION_ORCHESTRATOR"
    ):
        raise V4S1ExecutionOrchestratorError(
            "frozen budget guard changed / 凍結額度保護規則已變更"
        )
    if assignment.get("execution_contract_sha256") != freeze_sha:
        raise V4S1ExecutionOrchestratorError(
            "assignment points to another freeze / assignment 指向不同 freeze"
        )

    attestation = validate_usage_attestation(usage_attestation, now=now)
    frozen_hashes = {
        "track_manifest_sha256": track_sha,
        "assignment_manifest_sha256": assignment_sha,
        "execution_freeze_sha256": freeze_sha,
        "shard_sha256": shard_sha,
        "source_sha256": file_sha256(source),
        "source_manifest_sha256": file_sha256(source_manifest),
        "stage_protocol_sha256": file_sha256(stage_protocol),
        "research_protocol_sha256": file_sha256(research_protocol),
        "prompt_sha256": file_sha256(prompt),
        "schema_sha256": file_sha256(schema),
        "policy_sha256": file_sha256(policy),
    }
    registry, registry_path = _bind_usage_check(
        control_dir=control_dir,
        attestation=attestation,
        run_number=run_number,
        shard_id=shard_id,
        frozen_hashes=frozen_hashes,
    )
    check_filename = hashlib.sha256(
        str(attestation["check_id"]).encode("utf-8")
    ).hexdigest() + ".json"

    if float(attestation["remaining_percent"]) <= EXPECTED_THRESHOLD:
        checkpoint_path = (
            control_dir
            / "resume_checkpoints"
            / f"run_{run_number:02d}"
            / f"shard_{shard_id:02d}"
            / check_filename
        )
        core = _receipt_core(
            status=STATUS_BUDGET_STOP,
            action="DO_NOT_CALL_LAUNCHER / 不得呼叫啟動器",
            attestation=attestation,
            run_number=run_number,
            shard_id=shard_id,
            shard_path=shard_path,
            run_root=run_root,
            frozen_hashes=frozen_hashes,
            execution_contract=execution,
        )
        core.update(
            {
                "usage_registry_path": str(registry_path.resolve()),
                "usage_registry_sha256": registry["registry_sha256"],
                "launcher_called": False,
                "resume_required": True,
                "resume_instruction": (
                    "SUPPLY_A_NEW_FRESH_USAGE_CHECK / 提供新的即時用量檢查後續跑"
                ),
            }
        )
        receipt = _publish_record(checkpoint_path, core, "receipt_sha256")
        return {
            "status": STATUS_BUDGET_STOP,
            "launcher_called": False,
            "resume_required": True,
            "usage_registry_path": str(registry_path.resolve()),
            "resume_checkpoint_path": str(checkpoint_path.resolve()),
            "receipt_sha256": receipt["receipt_sha256"],
            "receipt_file_sha256": file_sha256(checkpoint_path),
        }

    receipt_path = (
        control_dir
        / "budget_pass_receipts"
        / f"run_{run_number:02d}"
        / f"shard_{shard_id:02d}"
        / check_filename
    )
    core = _receipt_core(
        status=STATUS_BUDGET_PASS,
        action="CALL_EXACT_FROZEN_SHARD / 呼叫精確凍結 SHARD",
        attestation=attestation,
        run_number=run_number,
        shard_id=shard_id,
        shard_path=shard_path,
        run_root=run_root,
        frozen_hashes=frozen_hashes,
        execution_contract=execution,
    )
    core.update(
        {
            "usage_registry_path": str(registry_path.resolve()),
            "usage_registry_sha256": registry["registry_sha256"],
            "launcher_call_authorized": True,
            "launcher_called_at_receipt_time": False,
        }
    )
    receipt = _publish_record(receipt_path, core, "receipt_sha256")

    launcher_args = SimpleNamespace(
        case_records=shard_path,
        output_dir=run_root / f"s{shard_id:02d}",
        run_number=run_number,
        model=EXPECTED_MODEL,
        reasoning_effort=EXPECTED_REASONING,
        protocol=research_protocol,
        prompt=prompt,
        schema=schema,
        execution_contract=execution_freeze,
        timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
        max_attempts=DEFAULT_MAX_ATTEMPTS,
        limit_cases=None,
    )
    launcher_result = (launcher or launcher_v2.run_from_args)(launcher_args)
    return {
        "status": STATUS_DISPATCHED,
        "budget_status": STATUS_BUDGET_PASS,
        "launcher_called": True,
        "resume_delegated_to_existing_runner": True,
        "usage_registry_path": str(registry_path.resolve()),
        "budget_receipt_path": str(receipt_path.resolve()),
        "receipt_sha256": receipt["receipt_sha256"],
        "receipt_file_sha256": file_sha256(receipt_path),
        "launcher_result": launcher_result,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--track-manifest", type=Path, required=True)
    parser.add_argument("--assignment-manifest", type=Path, required=True)
    parser.add_argument("--execution-freeze", type=Path, required=True)
    parser.add_argument("--stage-protocol", type=Path, required=True)
    parser.add_argument("--research-protocol", type=Path, required=True)
    parser.add_argument("--prompt", type=Path, required=True)
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--control-dir", type=Path, required=True)
    parser.add_argument("--run-number", type=int, required=True)
    parser.add_argument("--shard-id", type=int, required=True)
    parser.add_argument("--usage-attestation", type=Path, required=True)
    parser.add_argument("--expected-track-sha256", required=True)
    parser.add_argument("--expected-assignment-sha256", required=True)
    parser.add_argument("--expected-freeze-sha256", required=True)
    parser.add_argument("--expected-shard-sha256", required=True)
    args = parser.parse_args(argv)
    attestation = _read_json(args.usage_attestation)
    result = execute_shard(
        source=args.source,
        source_manifest=args.source_manifest,
        track_manifest=args.track_manifest,
        assignment_manifest=args.assignment_manifest,
        execution_freeze=args.execution_freeze,
        stage_protocol=args.stage_protocol,
        research_protocol=args.research_protocol,
        prompt=args.prompt,
        schema=args.schema,
        policy=args.policy,
        run_root=args.run_root,
        control_dir=args.control_dir,
        run_number=args.run_number,
        shard_id=args.shard_id,
        usage_attestation=attestation,
        expected_track_sha256=args.expected_track_sha256,
        expected_assignment_sha256=args.expected_assignment_sha256,
        expected_freeze_sha256=args.expected_freeze_sha256,
        expected_shard_sha256=args.expected_shard_sha256,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
