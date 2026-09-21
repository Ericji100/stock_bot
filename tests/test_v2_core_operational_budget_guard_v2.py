from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from scripts.v2_core_codex_staged_runner_v1 import StagedRunnerError
from scripts.v2_core_operational_budget_guard_v2 import (
    OUT,
    preflight,
    runner_command,
)


MANIFEST = OUT / "feasibility_probe_execution_manifest_v7.json"
POLICY = OUT / "operational_budget_override_v3.json"


def _attestation(tmp_path: Path, remaining: int) -> Path:
    path = tmp_path / f"usage-{remaining}.json"
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    path.write_text(
        json.dumps(
            {
                "source": "CODEX_APP_GET_USAGE_LIMITS",
                "limit_id": "codex",
                "used_percent": 100 - remaining,
                "remaining_percent": remaining,
                "checked_at": now,
                "check_id": f"test-{remaining}",
            }
        ),
        encoding="utf-8",
    )
    return path


def test_v7_guard_allows_above_20_and_stops_at_or_below(tmp_path: Path) -> None:
    allowed = preflight(
        artifact_dir=OUT,
        manifest_path=MANIFEST,
        policy_path=POLICY,
        usage_attestation_path=_attestation(tmp_path, 21),
    )
    assert allowed["remaining_percent"] == 21
    assert allowed["operational_stop_threshold_percent"] == 20
    for remaining in (20, 0):
        with pytest.raises(StagedRunnerError, match="BUDGET_STOP"):
            preflight(
                artifact_dir=OUT,
                manifest_path=MANIFEST,
                policy_path=POLICY,
                usage_attestation_path=_attestation(tmp_path, remaining),
            )


def test_v7_guard_dispatches_pinned_runner_per_stage(tmp_path: Path) -> None:
    common = dict(
        artifact_dir=OUT,
        execution_manifest=MANIFEST,
        operational_policy=POLICY,
        usage_attestation=_attestation(tmp_path, 50),
        round=1,
        shard_id=1,
        timeout_seconds=1200,
    )
    stage_b = runner_command(argparse.Namespace(stage="stage_b", **common))
    stage_d = runner_command(argparse.Namespace(stage="stage_d", **common))
    assert stage_b[1].endswith("v2_core_codex_staged_runner_v1.py")
    assert stage_b[-2:] == ["--stage", "stage_b"]
    assert stage_d[1].endswith("v2_core_codex_stage_d_runner_v2.py")
    assert "--stage" not in stage_d
