from __future__ import annotations

import json
from pathlib import Path

from scripts.v2_core_blind_review_compare_v1 import compare_reviews, normalize, render_markdown


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def _row(review_id: str, scenario: str, permission: str = "TRADE") -> dict:
    return {
        "review_id": review_id,
        "as_of": "2023-06-01",
        "model": "gpt-5.6-sol",
        "reasoning_effort": "xhigh",
        "primary_scenario": scenario,
        "canonical_trigger_route": "BREAK_THEN_RETEST",
        "trigger_state": "TRIGGERED",
        "anchor_start": {"date": "2023-01-01", "price": 10.0, "direction": "UP"},
        "anchor_end_or_forming": {"status": "CONFIRMED", "date": "2023-04-01", "price": 15.0},
        "campaign_maturity": "G1",
        "taiji_phase": "COPY_ATTACK",
        "taiji_leg_index": 3,
        "large_direction": "UP",
        "small_direction": "BULL",
        "episode_stop_date": "2023-05-20",
        "episode_stop_price": 12.0,
        "permission": permission,
        "evidence": ["visible fact"],
        "uncertainties": [],
        "parent_correction_replication_relationship": {"assessment": "PASS"},
    }


def test_normalize_aliases() -> None:
    row = normalize(_row("A", "MACRO_COPY_RESONANCE"))
    assert row["permission"] == "TRADE_APPROVED"
    assert row["large_direction"] == "BULL"
    assert row["trigger_state"] == "TRIGGERED"
    assert row["canonical_trigger_route"] == "MACRO_BREAK_THEN_RETEST"


def test_compare_exposes_scenario_disagreement(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"rows": [{"review_id": "A", "as_of": "2023-06-01", "anonymous_stock_id": "S-1"}]}), encoding="utf-8")
    left = tmp_path / "left.jsonl"
    right = tmp_path / "right.jsonl"
    _write_jsonl(left, [_row("A", "MACRO_COPY_RESONANCE")])
    _write_jsonl(right, [_row("A", "MATURE_TREND_PULLBACK", "TRADE_APPROVED")])
    result = compare_reviews(manifest, {"left": left, "right": right})
    assert result["validation"]["passed"] is True
    assert result["pairwise"]["left__vs__right"]["primary_scenario"]["agreement_pct"] == 0.0
    assert result["pairwise"]["left__vs__right"]["permission"]["agreement_pct"] == 100.0
    assert result["unanimous_counts"]["primary_scenario"] == 0
    assert "不是正式三輪一致性測試" in render_markdown(result)


def test_validation_rejects_model_and_future_fields(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"rows": [{"review_id": "A", "as_of": "2023-06-01"}]}), encoding="utf-8")
    left = tmp_path / "left.jsonl"
    right = tmp_path / "right.jsonl"
    bad = _row("A", "MACRO_COPY_RESONANCE")
    bad["model"] = "other-model"
    bad["mfe"] = 99.0
    _write_jsonl(left, [bad])
    _write_jsonl(right, [_row("A", "MACRO_COPY_RESONANCE")])
    result = compare_reviews(manifest, {"left": left, "right": right})
    issues = {item["issue"] for item in result["validation"]["issues"]}
    assert "MODEL_MISMATCH" in issues
    assert "FORBIDDEN_KEY:mfe" in issues


def test_all_manifest_cases_are_required(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"rows": [
        {"review_id": "A", "as_of": "2023-06-01"},
        {"review_id": "B", "as_of": "2023-06-01"},
    ]}), encoding="utf-8")
    left = tmp_path / "left.jsonl"
    right = tmp_path / "right.jsonl"
    _write_jsonl(left, [_row("A", "MACRO_COPY_RESONANCE")])
    _write_jsonl(right, [_row("A", "MACRO_COPY_RESONANCE")])
    result = compare_reviews(manifest, {"left": left, "right": right})
    assert any(item["issue"] == "MISSING_MANIFEST_CASE" for item in result["validation"]["issues"])


def test_calibration_aliases_can_be_compared_but_are_contract_failures(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"rows": [{"review_id": "A", "as_of": "2023-06-01"}]}), encoding="utf-8")
    left = tmp_path / "left.jsonl"
    right = tmp_path / "right.jsonl"
    aliased = _row("A", "MACRO_COPY_RESONANCE")
    aliased["canonical_route"] = aliased.pop("canonical_trigger_route")
    aliased["controlling_anchor"] = aliased.pop("anchor_start")
    aliased.pop("anchor_end_or_forming")
    _write_jsonl(left, [aliased])
    _write_jsonl(right, [_row("A", "MACRO_COPY_RESONANCE")])
    result = compare_reviews(manifest, {"left": left, "right": right})
    assert result["cases"][0]["reviewers"]["left"]["canonical_trigger_route"] == "MACRO_BREAK_THEN_RETEST"
    issues = {item["issue"] for item in result["validation"]["issues"]}
    assert "MISSING_REQUIRED_KEY:canonical_trigger_route" in issues
    assert "MISSING_REQUIRED_KEY:anchor_start" in issues
    assert "MISSING_REQUIRED_KEY:anchor_end_or_forming" in issues
