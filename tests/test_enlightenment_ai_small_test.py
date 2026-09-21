import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "reports/course_backtest/2026-09-05/enlightenment_ai_small_test_v2"


def read(name):
    return json.loads((RUN / name).read_text(encoding="utf-8"))


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def test_frozen_sources_match_manifest():
    manifest = read("input_manifest.json")
    assert len(manifest["items"]) == 6
    for item in manifest["items"]:
        assert item["pre_monitor_bars"] >= 750
        assert sha256(item["price_path"]) == item["price_sha256"]
        assert sha256(item["packet_path"]) == item["packet_sha256"]


def test_every_ai_approved_signal_is_a_causal_candidate_after_monitoring():
    manifest = read("input_manifest.json")
    decisions = read("ai_authored_trigger_review.json")
    items = {item["code"]: item for item in manifest["items"]}
    for code, rows in decisions["approved_events"].items():
        packet = json.loads(Path(items[code]["packet_path"]).read_text(encoding="utf-8"))
        candidates = {row["date"]: row for row in packet["candidate_events"]}
        for row in rows:
            assert row["signal_date"] >= items[code]["monitor_on"]
            assert row["signal_date"] in candidates
            assert 0 < row["stop"] < candidates[row["signal_date"]]["ohlc"][3]


def test_replay_accounting_and_position_limits_reconcile():
    result = read("backtest.json")
    for variant in result["variants"].values():
        episodes = [episode for stock in variant["stocks"] for episode in stock["episodes"]]
        assert variant["summary"]["trade_episodes"] == len(episodes)
        assert variant["summary"]["tranches"] == sum(len(episode["tranches"]) for episode in episodes)
        assert round(sum(episode["net_pnl"] for episode in episodes), 2) == variant["summary"]["net_pnl"]
        assert all(1 <= len(episode["tranches"]) <= 3 for episode in episodes)
        for episode in episodes:
            assert episode["tranches"][0]["role"] == "MOTHER（母單）"
            assert all(tranche["entry_date"] > tranche["signal_date"] for tranche in episode["tranches"])


def test_jinju_is_monitored_and_has_reentries_not_permanently_rejected():
    decisions = read("ai_authored_trigger_review.json")
    assert decisions["rejected_key_opportunities"]["8358"][0]["status"] == "WATCHING（監控中）"
    dates = [row["signal_date"] for row in decisions["approved_events"]["8358"]]
    assert dates == ["2025-10-28", "2026-01-14", "2026-04-07", "2026-08-25"]
    result = read("backtest.json")
    mother = result["variants"]["MOTHER_ONLY_10K（母單一筆一萬元）"]
    episodes = next(stock["episodes"] for stock in mother["stocks"] if stock["code"] == "8358")
    assert len(episodes) == 3
    assert episodes[-1]["status"] == "OPEN（持有中）"


def test_report_explicitly_marks_retrospective_non_blind_limit():
    report = (RUN / "backtest.md").read_text(encoding="utf-8")
    assert "RETROSPECTIVE_NOT_BLIND（事後研究、不是樣本外盲測）" in report
    assert "金居不是被永久排除" in report


def test_small_pivot_break_warns_but_does_not_force_hoggin_mother_exit():
    result = read("backtest.json")
    mother = result["variants"]["MOTHER_ONLY_10K（母單一筆一萬元）"]
    stock = next(stock for stock in mother["stocks"] if stock["code"] == "6182")
    episode = next(episode for episode in stock["episodes"] if episode["entry_date"] == "2026-04-15")
    assert episode["exit_signal_date"] == "2026-07-21"
    assert episode["exit_reason"] == "MA21_TWO_CLOSES（波段期連續兩日跌破21MA）"
    assert any(
        row["date"] == "2026-06-05" and row["event"] == "SMALL_PIVOT_BROKEN（小級樞紐失守警告）"
        for row in stock["audit"]
    )


def test_fongyi_large_structure_failure_removes_watchlist_until_reselected():
    result = read("backtest.json")
    for variant in result["variants"].values():
        stock = next(stock for stock in variant["stocks"] if stock["code"] == "6189")
        assert stock["monitor_until"] == "2024-12-16"
        assert stock["watch_status"] == "REMOVED_FROM_WATCHLIST（已移出監控）"
        assert all(episode["entry_date"] <= "2024-12-16" for episode in stock["episodes"])
        assert any(row["event"].startswith("SIGNAL_BLOCKED_REMOVED_FROM_WATCHLIST") for row in stock["audit"])


def test_one_tick_tolerance_fills_fongyi_but_not_jinju_large_gap():
    result = read("backtest.json")
    mother = result["variants"]["MOTHER_ONLY_10K（母單一筆一萬元）"]
    fongyi = next(stock for stock in mother["stocks"] if stock["code"] == "6189")
    assert any(episode["entry_date"] == "2024-02-02" for episode in fongyi["episodes"])
    jinju = next(stock for stock in mother["stocks"] if stock["code"] == "8358")
    assert any(
        row["date"] == "2026-04-08" and row["event"] == "BUY_SKIPPED（取消進場）"
        for row in jinju["audit"]
    )
