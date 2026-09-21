from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from scripts.v2_core_codex_staged_runner_v1 import (
    OUT,
    StagedRunnerError,
    assert_budget_allows,
    compact_packet,
    parse_usage_attestation,
    selected_stage_d_prompt,
    strict_transport_schema,
)


PROBE = json.loads((OUT / "feasibility_probe_manifest.json").read_text(encoding="utf-8"))
PACKET_PATH = OUT / PROBE["packet_directory"] / PROBE["rows"][0]["packet_file"]
PACKET = json.loads(PACKET_PATH.read_text(encoding="utf-8"))


def test_compact_packet_is_deterministic_and_keeps_750_asof_rows() -> None:
    first = compact_packet(PACKET)
    second = compact_packet(PACKET)
    assert first == second
    assert len(first["daily_rows"]) == 750
    assert first["daily_rows"][-1][0] == PACKET["as_of"]
    assert "code" not in json.dumps(first, ensure_ascii=False).lower()
    assert "name" not in json.dumps(first, ensure_ascii=False).lower()


def test_transport_schema_uses_exact_packet_evidence_refs() -> None:
    schema = json.loads((OUT / "v2_core_stage_b.schema.candidate.json").read_text(encoding="utf-8"))
    refs = [row["ref"] for row in PACKET["evidence_catalog"]]
    transport = strict_transport_schema(schema, evidence_ref_values=refs)
    Draft202012Validator.check_schema(transport)
    assert transport["$defs"]["evidenceRef"]["enum"] == sorted(refs)


def test_stage_d_prompt_contains_only_routed_scenario_template() -> None:
    text = (OUT / "v2_core_stage_d.prompt.candidate_r2.md").read_text(encoding="utf-8")
    selected = selected_stage_d_prompt(text, "MACRO_COPY_RESONANCE")
    assert "### `MACRO_COPY_RESONANCE`" in selected
    assert "### `MATURE_TREND_PULLBACK`" not in selected
    assert "### `BEAR_REVERSAL_LEFT_RIGHT`" not in selected
    assert "### `FRESH_Q1_EXPANSION`" not in selected


def test_stage_d_transport_pins_only_one_evaluation_shape() -> None:
    schema = json.loads((OUT / "v2_core_stage_d.schema.candidate.json").read_text(encoding="utf-8"))
    refs = [row["ref"] for row in PACKET["evidence_catalog"]]
    transport = strict_transport_schema(
        schema,
        evidence_ref_values=refs,
        primary_scenario="FRESH_Q1_EXPANSION",
    )
    assert transport["properties"]["primary_scenario"]["const"] == "FRESH_Q1_EXPANSION"
    assert transport["properties"]["scenario_evaluation"]["$ref"].endswith("freshEvaluation")
    assert "evaluationBase" not in transport["$defs"]
    assert "matureEvaluation" not in transport["$defs"]
    assert "macroEvaluation" not in transport["$defs"]
    assert "bearEvaluation" not in transport["$defs"]
    assert "freshEvaluation" in transport["$defs"]


def test_usage_attestation_is_fresh_and_budget_fails_closed(tmp_path: Path) -> None:
    now = datetime(2026, 9, 11, 10, 0, tzinfo=timezone.utc)
    path = tmp_path / "usage.json"
    path.write_text(
        json.dumps(
            {
                "source": "CODEX_APP_GET_USAGE_LIMITS",
                "limit_id": "codex",
                "used_percent": 82,
                "remaining_percent": 18,
                "checked_at": "2026-09-11T09:59:00Z",
                "check_id": "usage-test-001",
            }
        ),
        encoding="utf-8",
    )
    attestation = parse_usage_attestation(path, now=now)
    assert_budget_allows(attestation, 5)
    attestation["remaining_percent"] = 5
    with pytest.raises(StagedRunnerError, match="BUDGET_STOP"):
        assert_budget_allows(attestation, 5)


def test_stale_or_non_primary_usage_attestation_is_rejected(tmp_path: Path) -> None:
    now = datetime(2026, 9, 11, 10, 0, tzinfo=timezone.utc)
    value = {
        "source": "CODEX_APP_GET_USAGE_LIMITS",
        "limit_id": "codex_bengalfox",
        "used_percent": 50,
        "remaining_percent": 50,
        "checked_at": "2026-09-11T09:59:00Z",
        "check_id": "usage-test-002",
    }
    path = tmp_path / "usage.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(StagedRunnerError, match="source/limit"):
        parse_usage_attestation(path, now=now)

    value["limit_id"] = "codex"
    value["checked_at"] = "2026-09-11T09:40:00Z"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(StagedRunnerError, match="stale"):
        parse_usage_attestation(path, now=now)
