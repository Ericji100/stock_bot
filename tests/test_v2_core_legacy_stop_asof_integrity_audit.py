from scripts.v2_core_legacy_role_shortlist_r7 import ARTIFACT_DIR
from scripts.v2_core_legacy_stop_asof_integrity_audit import (
    build_report,
    first_closing_breach,
    render_markdown,
)


def test_closing_breach_ignores_intraday_low_and_signal_day():
    bars = [
        {"date": "2023-08-21", "close": 41.0, "low": 40.0},
        {"date": "2023-08-28", "close": 39.65, "low": 39.35},
        {"date": "2023-08-29", "close": 40.65, "low": 38.05},
        {"date": "2023-08-31", "close": 43.35, "low": 41.65},
    ]
    assert first_closing_breach(bars, stop_price=39.5, after_date="2023-08-21", before_date="2023-08-31") is None
    assert first_closing_breach(bars, stop_price=40.25, after_date="2023-08-21", before_date="2023-08-31")["date"] == "2023-08-28"


def test_legacy_stop_audit_preserves_prior_breach():
    report = build_report(ARTIFACT_DIR)
    assert report["case_count"] == 14
    assert report["prior_closing_breach_count"] == 1
    assert report["signal_close_below_stop_count"] == 0
    breached = [row for row in report["rows"] if row["had_prior_closing_breach"]]
    assert [row["review_id"] for row in breached] == ["FP-a956dbdf5f936dfdb64d45ec"]
    assert breached[0]["first_closing_breach_after_confirmation_before_signal"]["date"] == "2023-08-28"
    assert "不是新回測" in render_markdown(report)
