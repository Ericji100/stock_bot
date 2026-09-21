"""Frozen launcher for outcome-blind Hybrid V3 atomic AI review.

The semantic reviewer remains the byte-pinned V2 adapter.  This launcher only
selects the repaired V3 packet validator and verifies that the execution
contract pins both the reviewer and this launcher before any model call.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

try:
    from . import hybrid_v3_codex_reviewer_v2 as reviewer_v2
    from .hybrid_v3_atomic_policy_v3 import validate_atomic
    from .hybrid_v3_atomic_runner_v2 import AtomicCaseRunner, ReviewContractError
except ImportError:  # pragma: no cover - direct script execution
    from scripts import hybrid_v3_codex_reviewer_v2 as reviewer_v2
    from scripts.hybrid_v3_atomic_policy_v3 import validate_atomic
    from scripts.hybrid_v3_atomic_runner_v2 import AtomicCaseRunner, ReviewContractError


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER_VERSION = "hybrid-v3-codex-launcher-v1"
LAUNCHER_STATUS = "FINAL"
POLICY_PATH = ROOT / "scripts/hybrid_v3_atomic_policy_v3.py"
REVIEWER_PATH = ROOT / "scripts/hybrid_v3_codex_reviewer_v2.py"


def _verify_launcher_pin(execution_contract_path: Path) -> str:
    contract = json.loads(Path(execution_contract_path).read_text(encoding="utf-8-sig"))
    components = contract.get("v2_components")
    if not isinstance(components, list):
        raise ReviewContractError("execution contract has no v2_components")
    launcher = next(
        (row for row in components if isinstance(row, dict) and row.get("name") == "launcher"),
        None,
    )
    if launcher is None:
        raise ReviewContractError("execution contract does not pin component: launcher")
    if str(launcher.get("status") or "").upper() != LAUNCHER_STATUS:
        raise ReviewContractError("execution contract launcher is not FINAL")
    actual_path = Path(__file__).resolve()
    relative_path = launcher.get("relative_path")
    if relative_path and (ROOT / str(relative_path)).resolve() != actual_path:
        raise ReviewContractError("execution contract launcher path differs")
    actual_sha = reviewer_v2._file_sha256(actual_path)
    if str(launcher.get("sha256") or "").lower() != actual_sha:
        raise ReviewContractError("execution contract launcher hash differs")
    return actual_sha


def run_from_args(args: Any) -> dict[str, Any]:
    records = reviewer_v2._read_jsonl(args.case_records)
    if args.limit_cases is not None:
        records = records[: max(0, int(args.limit_cases))]
    for record in records:
        if record.get("model") != args.model or record.get("reasoning_effort") != args.reasoning_effort:
            raise ReviewContractError("case record model/reasoning differs from launcher execution")

    launcher_sha = _verify_launcher_pin(args.execution_contract)
    verified = reviewer_v2.verify_execution_contract(
        records,
        execution_contract_path=args.execution_contract,
        prompt_path=args.prompt,
        schema_path=args.schema,
        protocol_path=args.protocol,
        reviewer_path=REVIEWER_PATH,
        policy_path=POLICY_PATH,
    )
    reviewer = reviewer_v2.CodexAtomicReviewerV2(
        prompt_path=args.prompt,
        schema_path=args.schema,
        model=args.model,
        reasoning_effort=args.reasoning_effort,
        timeout_seconds=args.timeout_seconds,
        expected_prompt_sha256=verified["prompt_sha256"],
        expected_schema_sha256=verified["schema_sha256"],
        expected_adapter_code_sha256=verified["reviewer_code_sha256"],
    )
    runner = AtomicCaseRunner(
        output_dir=args.output_dir,
        reviewer=reviewer,
        validator=validate_atomic,
        run_number=args.run_number,
        max_attempts=args.max_attempts,
        expected_reviewer_identity={
            "adapter_version": reviewer_v2.ADAPTER_VERSION,
            "adapter_status": reviewer_v2.ADAPTER_STATUS,
            "adapter_code_sha256": verified["reviewer_code_sha256"],
        },
    )
    result = runner.run_cases(records)
    result.update(
        {
            "launcher_version": LAUNCHER_VERSION,
            "launcher_status": LAUNCHER_STATUS,
            "launcher_code_sha256": launcher_sha,
            "adapter_version": reviewer_v2.ADAPTER_VERSION,
            "adapter_status": reviewer_v2.ADAPTER_STATUS,
            "policy_code_sha256": verified["policy_code_sha256"],
            "model": args.model,
            "reasoning_effort": args.reasoning_effort,
        }
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case-records", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--run-number", type=int, required=True)
    parser.add_argument("--model", default=reviewer_v2.DEFAULT_MODEL)
    parser.add_argument("--reasoning-effort", default=reviewer_v2.DEFAULT_REASONING)
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--prompt", type=Path, default=reviewer_v2.PROMPT_PATH)
    parser.add_argument("--schema", type=Path, default=reviewer_v2.SCHEMA_PATH)
    parser.add_argument("--execution-contract", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=1200)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--limit-cases", type=int)
    args = parser.parse_args()
    print(json.dumps(run_from_args(args), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
