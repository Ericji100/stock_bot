from scripts.v2_core_legacy_role_shortlist_r7 import ARTIFACT_DIR
from scripts.v2_core_r10b_transfer_diagnostic import build_report, render_markdown


def test_transfer_diagnostic_preserves_invalid_as_invalid():
    report = build_report(ARTIFACT_DIR)
    assert report["case_count"] == 2
    assert report["full_chain_valid_count"] == 1
    assert report["invalid_formal_count"] == 1
    assert report["signal_permission_comparable_count"] == 1
    assert report["signal_permission_matches_among_full_chain_valid"] == 0
    assert report["defense_exact_matches"] == 2
    assert report["rows"][0]["new_nonpass_gates"] == {
        "EARLY_TAIJI_GENERATION": "FAIL",
        "EARLY_LOCATION_AND_SPACE": "FAIL",
    }
    assert report["rows"][1]["new_signal_disposition"] is None
    assert report["rows"][1]["duplicate_evidence_reference_count"] == 1
    markdown = render_markdown(report)
    assert "不能把另一筆失效當成不進場" in markdown
    assert "沒有隔日開盤成交" in markdown
