from __future__ import annotations

import json
from pathlib import Path

from scripts.v2_core_conflict_packet_builder_v2 import (
    CONTEXT_BARS,
    FORBIDDEN_KEYS,
    build_packets,
    contains_forbidden_key,
)


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "reports" / "course_backtest" / "2026-09-10" / "v2_core_reproducible_goal_v1"
CASES = OUT / "calibration_daily_cases_v2.jsonl"


def test_forbidden_key_scan_is_recursive() -> None:
    assert contains_forbidden_key({"safe": [{"future_outcome": "x"}]}) == {"future_outcome"}


def test_full_history_packets_are_causal_blind_and_long_enough(tmp_path: Path) -> None:
    manifest = build_packets(CASES, tmp_path)
    assert manifest["packet_count"] == 6
    assert manifest["supersedes_packet_version"] == "v2-core-conflict-packet-v1"
    for row in manifest["rows"]:
        packet = json.loads((tmp_path / row["packet_file"]).read_text(encoding="utf-8"))
        daily = packet["daily_structure_context_to_as_of"]
        assert not contains_forbidden_key(packet)
        assert packet["as_of"] == row["as_of"]
        assert len(daily) == CONTEXT_BARS
        assert daily[0]["date"] == row["context_start"]
        assert daily[-1]["date"] == packet["as_of"] == row["context_end"]
        assert all(item["date"] <= packet["as_of"] for item in daily)
        assert all(not item["selection_sources"] for item in daily)
        assert all("UPSTREAM_SELECTED_TODAY" not in item["facts"] for item in daily)
        assert all(item["confirmation_date"] <= packet["as_of"] for item in packet["confirmed_pivots_to_as_of"])
        assert packet["review_constraints"]["monitor_start_and_upstream_selection_hidden"] is True
        serialized = json.dumps(packet, ensure_ascii=False).lower()
        for key in FORBIDDEN_KEYS:
            assert f'"{key}"' not in serialized
