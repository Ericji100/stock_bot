import hashlib
import json
import re
from pathlib import Path

import pytest

from backtests.dual_ma import etf_00635u_prep_v1 as subject


def test_formal_mode_is_closed_without_00_accepted_manifest():
    with pytest.raises(subject.FormalSnapshotNotAccepted, match=subject.FORMAL_STOP_CODE):
        subject.validate_formal_snapshot_gate(
            None, accepted_manifest_sha256=None, accepted_by=None
        )


def test_formal_manifest_requires_exact_sha_00_and_all_file_hashes(tmp_path):
    data = tmp_path / "00635u.csv"
    data.write_text("date,close\n2015-04-15,20\n", encoding="utf-8")
    manifest = {
        "schema": subject.MANIFEST_SCHEMA,
        "symbol": "00635U",
        "listing_date": "2015-04-15",
        "immutable": True,
        "files": [
            {
                "path": data.name,
                "sha256": subject.sha256_file(data),
            }
        ],
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    sha = subject.sha256_file(manifest_path)

    accepted = subject.validate_formal_snapshot_gate(
        manifest_path, accepted_manifest_sha256=sha, accepted_by="00"
    )
    assert accepted["symbol"] == "00635U"

    with pytest.raises(subject.FormalSnapshotNotAccepted, match="accepted_by"):
        subject.validate_formal_snapshot_gate(
            manifest_path, accepted_manifest_sha256=sha, accepted_by="02"
        )
    data.write_text("tampered", encoding="utf-8")
    with pytest.raises(subject.FormalSnapshotNotAccepted, match="file hash mismatch"):
        subject.validate_formal_snapshot_gate(
            manifest_path, accepted_manifest_sha256=sha, accepted_by="00"
        )


def test_fixture_pack_has_chinese_report_links_anchors_manifest_and_readback(tmp_path):
    result = subject.build_fixture_pack(tmp_path)
    assert result["status"] == subject.PREP_STOP_CODE
    assert subject.validate_report_tree(tmp_path) == []
    report = (tmp_path / subject.REPORT_NAME).read_text(encoding="utf-8")
    assert "SYNTHETIC FIXTURE ONLY" in report
    assert "不得引用本文數字判斷策略期望值" in report
    assert "期貨轉倉" in report
    assert "美元／新臺幣匯率" in report
    assert "交易時區錯位" in report
    assert "TAIEX 報酬指數只放補充區" in report
    assert report.count("### 測試目的") == 14
    assert report.count("### 測試方法") == 14
    assert report.count("### 觀察結果") == 14
    assert report.count("### 白話結論") == 14
    assert report.count("### 對最終決策的影響") == 14
    assert "<a id=" not in report
    headings = re.findall(
        r"^## ((?:0[1-9]|1[0-4])｜[^\n]+)$", report, flags=re.MULTILINE
    )
    assert len(headings) == 14
    assert all(f"](#{subject._chapter_anchor(title)})" in report for title in headings)
    assert subject.validate_report_schema(tmp_path) == []
    assert all(line in report for line in subject.expected_comparison_lines())

    readback = json.loads((tmp_path / "readback.json").read_text(encoding="utf-8"))
    assert readback["status"] == "PASS"
    assert readback["manifest_sha256"] == subject.sha256_file(tmp_path / "manifest.json")


def test_report_validator_detects_attachment_mutation(tmp_path):
    subject.build_fixture_pack(tmp_path)
    (tmp_path / "fixture-summary.csv").write_text("tampered", encoding="utf-8")
    errors = subject.validate_report_tree(tmp_path)
    assert "manifest hash mismatch: fixture-summary.csv" in errors


def test_fixture_manifest_is_deterministic_across_two_roots(tmp_path):
    left = tmp_path / "left"
    right = tmp_path / "right"
    one = subject.build_fixture_pack(left)
    two = subject.build_fixture_pack(right)
    assert one["manifest_sha256"] == two["manifest_sha256"]
    assert (left / subject.REPORT_NAME).read_bytes() == (right / subject.REPORT_NAME).read_bytes()
