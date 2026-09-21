from __future__ import annotations

import json
from pathlib import Path

from scripts.v2_core_conflict_packet_builder_v1 import FORBIDDEN_KEYS, build_packets, contains_forbidden_key


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "reports" / "course_backtest" / "2026-09-10" / "v2_core_reproducible_goal_v1"
CASES = OUT / "calibration_daily_cases_v2.jsonl"


def test_forbidden_key_scan_is_recursive() -> None:
    assert contains_forbidden_key({"safe": [{"scenario": "x"}]}) == {"scenario"}


def test_blind_conflict_packets_are_causal_and_anonymous(tmp_path: Path) -> None:
    manifest = build_packets(CASES, tmp_path)
    assert manifest["packet_count"] == 6
    assert len({row["anonymous_stock_id"] for row in manifest["rows"]}) == 6
    for row in manifest["rows"]:
        packet = json.loads((tmp_path / row["packet_file"]).read_text(encoding="utf-8"))
        assert not contains_forbidden_key(packet)
        assert packet["as_of"] == row["as_of"]
        assert packet["review_constraints"]["identity_blind"] is True
        assert packet["review_constraints"]["legacy_answer_blind"] is True
        assert packet["review_constraints"]["future_performance_blind"] is True
        assert all(item["end"] <= packet["as_of"] for item in packet["completed_macd_21_55_55_cycles_to_as_of"])
        assert all(item["confirmation_date"] <= packet["as_of"] for item in packet["confirmed_pivots_to_as_of"])
        assert all(item["date"] <= packet["as_of"] for item in packet["daily_visible_facts_to_as_of"])
        serialized = json.dumps(packet, ensure_ascii=False).lower()
        for key in FORBIDDEN_KEYS:
            assert f'"{key}"' not in serialized
