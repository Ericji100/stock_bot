"""Dispatch v8 through the 20% operational usage guard."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from pathlib import Path
import sys
from typing import Any, Iterator, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import v2_core_operational_budget_guard_v2 as previous


GUARD_VERSION = "v2-core-operational-budget-guard-r3"
EXPECTED_MANIFEST_VERSION = "v2-core-m2a-triplicate-execution-r8-candidate"
EXPECTED_POLICY_VERSION = "v2-core-operational-budget-override-v4"
EXPECTED_RUNNERS = {
    "STAGE_B": "scripts/v2_core_codex_staged_runner_v1.py",
    "STAGE_D": "scripts/v2_core_codex_stage_d_runner_v3.py",
}
_PREVIOUS_RUNNER_COMMAND = previous.runner_command


@contextmanager
def _v8_contract() -> Iterator[None]:
    saved = {
        "GUARD_VERSION": previous.GUARD_VERSION,
        "EXPECTED_MANIFEST_VERSION": previous.EXPECTED_MANIFEST_VERSION,
        "EXPECTED_POLICY_VERSION": previous.EXPECTED_POLICY_VERSION,
        "EXPECTED_RUNNERS": previous.EXPECTED_RUNNERS,
    }
    previous.GUARD_VERSION = GUARD_VERSION
    previous.EXPECTED_MANIFEST_VERSION = EXPECTED_MANIFEST_VERSION
    previous.EXPECTED_POLICY_VERSION = EXPECTED_POLICY_VERSION
    previous.EXPECTED_RUNNERS = EXPECTED_RUNNERS
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(previous, name, value)


def preflight(**kwargs: Any) -> dict[str, Any]:
    with _v8_contract():
        return previous.preflight(**kwargs)


def runner_command(args: argparse.Namespace) -> list[str]:
    with _v8_contract():
        command = _PREVIOUS_RUNNER_COMMAND(args)
    if args.stage == "stage_d":
        return [
            command[0],
            "-m",
            "scripts.v2_core_codex_stage_d_runner_v3",
            *command[2:],
        ]
    return command


def main(argv: Sequence[str] | None = None) -> int:
    with _v8_contract():
        original_runner_command = previous.runner_command
        previous.runner_command = runner_command
        try:
            return previous.main(argv)
        finally:
            previous.runner_command = original_runner_command


if __name__ == "__main__":
    raise SystemExit(main())
