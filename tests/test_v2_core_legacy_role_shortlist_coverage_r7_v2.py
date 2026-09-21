"""R7 R2 coverage is a post-freeze representation audit, not a model score."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.v2_core_legacy_role_shortlist_coverage_r7_v2 import build_report
from scripts.v2_core_legacy_role_shortlist_r7 import ARTIFACT_DIR
from scripts.v2_core_legacy_role_shortlist_r7_v2 import OUTPUT_MANIFEST


def test_calibration_teacher_working_anchors_are_representable() -> None:
    report = build_report(ARTIFACT_DIR)
    assert report["status"] == "ROLE_SHORTLIST_COVERAGE_READY"
    assert report["case_count"] == 14
    assert report["exact_role_working_anchor_covered_count"] == 14
    assert report["formal_ai_calls"] == 0
    assert report["teacher_values_sent_to_ai"] is False


def test_hash_gate_runs_before_teacher_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source_manifest = json.loads((ARTIFACT_DIR / OUTPUT_MANIFEST).read_text(encoding="utf-8"))
    first_row = source_manifest["rows"][0]
    fake_dir = tmp_path / source_manifest["shortlist_directory"]
    fake_dir.mkdir()
    (tmp_path / OUTPUT_MANIFEST).write_text(json.dumps(source_manifest), encoding="utf-8")
    (fake_dir / first_row["shortlist_file"]).write_bytes(b"tampered shortlist")

    def forbidden_teacher_load(_artifact_dir: Path) -> object:
        raise AssertionError("teacher opened before hash validation")

    monkeypatch.setattr(
        "scripts.v2_core_legacy_role_shortlist_coverage_r7_v2.load_reference_cases",
        forbidden_teacher_load,
    )
    with pytest.raises(ValueError, match="shortlist hash mismatch"):
        build_report(tmp_path)
