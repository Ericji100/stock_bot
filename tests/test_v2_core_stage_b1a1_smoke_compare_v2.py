from __future__ import annotations

import json
from pathlib import Path

from scripts.v2_core_stage_b1a1_smoke_compare_v2 import compare_smoke


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_DIR = (
    ROOT
    / "reports"
    / "course_backtest"
    / "2026-09-10"
    / "v2_core_reproducible_goal_v1"
)


def test_r2_preflight_does_not_publish_partial_metrics(tmp_path: Path):
    source = json.loads(
        (ARTIFACT_DIR / "stage_b1a1_smoke_execution_manifest_candidate_r2.json").read_text(
            encoding="utf-8"
        )
    )
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
    report = compare_smoke(
        manifest_path=manifest_path,
        artifact_dir=ARTIFACT_DIR,
        run_dir=tmp_path / "empty_runs",
    )
    assert report["status"] == "NOT_READY_AI_RUNS_INCOMPLETE（AI三輪尚未完成）"
    assert report["completed_case_rounds"] == 0
    assert report["metrics_published"] is False
    assert report["metrics"] is None
    assert report["error_count"] == 0
