from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from scripts.v2_core_codex_staged_runner_v1 import OUT, StagedRunnerError
from scripts.v2_core_operational_budget_guard_v1 import load_policy, preflight


POLICY = OUT / "operational_budget_override_v2.json"
MANIFEST = OUT / "feasibility_probe_execution_manifest_v6.json"


def _attestation(path: Path, remaining: int) -> Path:
    path.write_text(
        json.dumps(
            {
                "source": "CODEX_APP_GET_USAGE_LIMITS",
                "limit_id": "codex",
                "used_percent": 100 - remaining,
                "remaining_percent": remaining,
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "check_id": f"test-{remaining}",
            }
        ),
        encoding="utf-8",
    )
    return path


def test_active_policy_uses_twenty_percent_without_changing_manifest() -> None:
    policy = load_policy(POLICY)
    assert policy["stop_new_ai_calls_when_remaining_percent_lte"] == 20
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert manifest["budget_stop_remaining_percent_lte"] == 5
    assert policy["preserves_frozen_experiment_inputs"] is True


def test_operational_guard_allows_above_twenty(tmp_path: Path) -> None:
    result = preflight(
        artifact_dir=OUT,
        manifest_path=MANIFEST,
        policy_path=POLICY,
        usage_attestation_path=_attestation(tmp_path / "usage.json", 21),
    )
    assert result["status"] == "BUDGET_OK（額度允許）"
    assert result["operational_stop_threshold_percent"] == 20


@pytest.mark.parametrize("remaining", [20, 5, 0])
def test_operational_guard_stops_at_or_below_twenty(
    tmp_path: Path, remaining: int
) -> None:
    with pytest.raises(StagedRunnerError, match="BUDGET_STOP"):
        preflight(
            artifact_dir=OUT,
            manifest_path=MANIFEST,
            policy_path=POLICY,
            usage_attestation_path=_attestation(tmp_path / f"usage-{remaining}.json", remaining),
        )
