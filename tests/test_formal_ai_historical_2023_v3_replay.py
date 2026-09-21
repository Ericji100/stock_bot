import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "reports/course_backtest/2024-02-02/historical_scan_2023h2_formal_ai_v1_v2_v3"
PARENT = ROOT / "reports/course_backtest/2024-02-02/historical_scan_2023h2_formal_ai_v1_v2"


def _read(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _jsonl(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]


def test_v3_validation_is_complete_and_causal():
    result = _read(RUN / "backtest_summary.json")
    validation = result["validation"]
    assert validation["valid"] is True
    assert validation["errors"] == []
    assert validation["reviewed_stocks"] == 1029
    assert validation["v3_extra_events"] == 14
    assert len(validation["approved_asof_packets"]) == 14
    assert validation["causal_attestation"]["candidate_level_future_outcome_visible"] is False


def test_v3_core_is_identical_to_frozen_v2():
    result = _read(RUN / "backtest_summary.json")
    assert result["core_identity"]["valid"] is True
    assert all(check["identical"] and not check["differences"] for check in result["core_identity"]["checks"])


def test_v3_manual_approvals_do_not_overlap_v2_core_and_are_on_active_watchlists():
    approvals = _read(RUN / "v3_ai_manual_approvals.json")["extra_approvals"]
    parent = {str(row["code"]): row for row in _jsonl(PARENT / "ai_decisions_merged.jsonl")}
    assert len({str(row["code"]) for row in approvals}) == len(approvals) == 14
    for approval in approvals:
        row = parent[str(approval["code"])]
        assert not row["v2"]["triggers"]
        active = False
        for event in sorted(row["v2"]["watchlist_events"], key=lambda value: value["date"]):
            if event["date"] > approval["signal_date"]:
                break
            if event["event"].startswith(("WATCHING", "RESELECTED")):
                active = True
            elif event["event"].startswith(("CAMPAIGN_INVALIDATED", "REMOVED_FROM_WATCHLIST")):
                active = False
        assert active, approval["code"]


def test_v3_equal_position_profit_recomposes_from_v2_core_and_extras():
    result = _read(RUN / "backtest_summary.json")
    parent = result["parent_variants"]
    for v3_key, v2_key in (
        ("V3_MOTHER_ONLY_10K", "V2_MOTHER_ONLY_10K"),
        ("V3_MOTHER_PLUS_2", "V2_MOTHER_PLUS_2"),
    ):
        summary = result["variants"][v3_key]
        extras = sum(row["net_pnl"] for row in summary["v3_route_breakdown"] if row["route"] != "V2_CORE")
        assert abs((summary["net_pnl"] - parent[v2_key]["net_pnl"]) - extras) <= 0.02


def test_tiered_probe_position_uses_less_peak_capital_than_equal_position():
    result = _read(RUN / "backtest_summary.json")["variants"]
    assert result["V3_TIERED_MOTHER_PLUS_2"]["peak_concurrent_deployed_cash"] < result["V3_MOTHER_PLUS_2"]["peak_concurrent_deployed_cash"]
    assert result["V3_TIERED_MOTHER_PLUS_2"]["maximum_concurrent_stocks"] == result["V3_MOTHER_PLUS_2"]["maximum_concurrent_stocks"]
