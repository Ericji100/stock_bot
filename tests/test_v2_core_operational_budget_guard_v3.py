from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from scripts.v2_core_codex_staged_runner_v1 import StagedRunnerError
from scripts import v2_core_operational_budget_guard_v2 as previous_guard
from scripts.v2_core_operational_budget_guard_v3 import main, preflight, runner_command
from scripts.v2_core_triplicate_preflight_v3 import OUT


MANIFEST = OUT / "feasibility_probe_execution_manifest_v8.json"
POLICY = OUT / "operational_budget_override_v4.json"


def _usage(tmp_path: Path, remaining: int) -> Path:
    path = tmp_path / f"usage-{remaining}.json"
    path.write_text(
        json.dumps(
            {
                "source": "CODEX_APP_GET_USAGE_LIMITS",
                "limit_id": "codex",
                "used_percent": 100 - remaining,
                "remaining_percent": remaining,
                "checked_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "check_id": f"v8-test-{remaining}",
            }
        ),
        encoding="utf-8",
    )
    return path


def test_v8_guard_enforces_20_percent_and_manifest_identity(tmp_path: Path) -> None:
    result = preflight(
        artifact_dir=OUT,
        manifest_path=MANIFEST,
        policy_path=POLICY,
        usage_attestation_path=_usage(tmp_path, 21),
    )
    assert result["remaining_percent"] == 21
    assert result["operational_stop_threshold_percent"] == 20
    with pytest.raises(StagedRunnerError, match="BUDGET_STOP"):
        preflight(
            artifact_dir=OUT,
            manifest_path=MANIFEST,
            policy_path=POLICY,
            usage_attestation_path=_usage(tmp_path, 20),
        )


def test_v8_guard_dispatches_receipt_fixed_stage_d_runner(tmp_path: Path) -> None:
    args = argparse.Namespace(
        artifact_dir=OUT,
        execution_manifest=MANIFEST,
        operational_policy=POLICY,
        usage_attestation=_usage(tmp_path, 50),
        round=1,
        shard_id=1,
        timeout_seconds=1200,
        stage="stage_d",
    )
    command = runner_command(args)
    assert command[1:3] == ["-m", "scripts.v2_core_codex_stage_d_runner_v3"]
    assert "--stage" not in command


def test_v8_guard_main_uses_module_dispatch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: dict[str, list[str]] = {}

    class Result:
        returncode = 0

    def fake_run(command: list[str], **_: object) -> Result:
        captured["command"] = command
        return Result()

    monkeypatch.setattr(previous_guard.subprocess, "run", fake_run)
    result = main(
        [
            "--artifact-dir",
            str(OUT),
            "--execution-manifest",
            str(MANIFEST),
            "--operational-policy",
            str(POLICY),
            "--usage-attestation",
            str(_usage(tmp_path, 50)),
            "--round",
            "1",
            "--shard-id",
            "1",
            "--stage",
            "stage_d",
        ]
    )
    assert result == 0
    assert captured["command"][1:3] == ["-m", "scripts.v2_core_codex_stage_d_runner_v3"]
