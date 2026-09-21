import json
from pathlib import Path

from scripts.hybrid_v3_sharding_v2 import canonical_sha256, file_sha256, read_jsonl


ROOT = Path(__file__).resolve().parents[1]
BASE = (
    ROOT
    / "reports/course_backtest/2024-02-02"
    / "historical_scan_2023h2_formal_ai_v3_daily_scan"
    / "hybrid_monitoring_candidate4_v5"
)
OLD_TRACK = BASE / "research_track_v3"
NEW_TRACK = BASE / "research_track_v4_exact_evidence"


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def test_candidate4_failure_is_preserved_and_performance_remains_sealed() -> None:
    path = BASE / "consistency/candidate4_abort_incident.json"
    report = _json(path)
    core = dict(report)
    expected = core.pop("report_sha256")
    assert expected == canonical_sha256(core)
    assert report["status"] == "ABORTED_REPEATABILITY_SCHEMA_CAUSAL_GATE_FAIL"
    assert report["valid_envelopes_before_abort"] == 43
    assert report["three_run_common_cases_before_abort"] == 13
    assert report["attempt_status_counts"] == {"CONTRACT_ERROR": 1, "VALIDATED": 43}
    assert report["failure"]["error"] == (
        "unknown evidence ref: REL_CANDIDATE:R-b57731032559c12d"
    )
    assert report["gate_effect"] == {
        "schema_and_causal_threshold": 1.0,
        "mathematically_passable": False,
        "performance_must_remain_sealed": True,
    }


def test_v4_changes_only_transport_components_and_pins_same_model() -> None:
    old = _json(OLD_TRACK / "research_execution_freeze.json")
    new_path = NEW_TRACK / "research_execution_freeze.json"
    new = _json(new_path)
    assert new["freeze_version"] == "hybrid-v3-research-execution-freeze-v4-exact-evidence"
    assert new["strategy_or_gate_change"] is False
    assert new["v3_strategy_rules_unchanged"] is True
    assert new["execution_contract"] == {
        "model": "gpt-5.6-sol",
        "reasoning_effort": "xhigh",
    }
    assert new["supersedes_execution_freeze_sha256"] == file_sha256(
        OLD_TRACK / "research_execution_freeze.json"
    )
    old_components = {row["name"]: row for row in old["v2_components"]}
    new_components = {row["name"]: row for row in new["v2_components"]}
    assert set(old_components) == set(new_components)
    for name in ("runner", "policy", "prompt", "schema", "protocol"):
        assert new_components[name]["sha256"] == old_components[name]["sha256"]
    assert new_components["launcher"]["relative_path"].endswith(
        "hybrid_v3_codex_launcher_v2.py"
    )
    assert new_components["reviewer"]["relative_path"].endswith(
        "hybrid_v3_codex_reviewer_v3.py"
    )
    for component in new_components.values():
        assert component["status"] == "FINAL"
        assert component["sha256"] == file_sha256(ROOT / component["relative_path"])


def test_v4_track_and_shards_have_exact_120_case_coverage() -> None:
    track_path = NEW_TRACK / "research_track_manifest.json"
    track = _json(track_path)
    core = dict(track)
    expected = core.pop("track_manifest_sha256")
    assert expected == canonical_sha256(core)
    assert track["status"] == "FROZEN_OUTCOME_BLIND_RESEARCH_ONLY"
    assert track["outcome_blind"] is True
    assert track["strategy_or_gate_change"] is False
    assert track["execution_freeze_sha256"] == file_sha256(
        NEW_TRACK / "research_execution_freeze.json"
    )

    assignment_path = NEW_TRACK / "primary_cases/assignment.manifest.json"
    assignment = _json(assignment_path)
    assert assignment["coverage"] == {
        "expected": 120,
        "covered": 120,
        "missing": 0,
        "overlap": 0,
        "unexpected": 0,
    }
    assert assignment["shard_count"] == 4
    assert assignment["execution_contract_sha256"] == file_sha256(
        NEW_TRACK / "research_execution_freeze.json"
    )
    records = []
    for shard in assignment["shards"]:
        path = Path(shard["path"])
        rows = read_jsonl(path)
        assert len(rows) == shard["rows"]
        assert file_sha256(path) == shard["sha256"]
        records.extend(rows)
    assert len(records) == 120
    assert len({row["case_key"] for row in records}) == 120
    assert {row["model"] for row in records} == {"gpt-5.6-sol"}
    assert {row["reasoning_effort"] for row in records} == {"xhigh"}
    assert {row["execution_contract_sha256"] for row in records} == {
        file_sha256(NEW_TRACK / "research_execution_freeze.json")
    }
