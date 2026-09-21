from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from scripts import v2_core_codex_staged_runner_v1 as base
from scripts.v2_core_codex_stage_d_runner_v2 import (
    PROGRAM_BOUND_FIELDS,
    bind_program_metadata,
    program_bound_metadata,
    rendered_stage_d_prompt,
    semantic_transport_schema,
)
from scripts.v2_core_staged_output_validator_v1 import validate_stage_d


OUT = base.OUT
PROBE = json.loads((OUT / "feasibility_probe_manifest.json").read_text(encoding="utf-8"))
REVIEW_ID = "FP-a0a473840247b7fe32be8227"
PACKET_ROW = next(row for row in PROBE["rows"] if row["review_id"] == REVIEW_ID)
PACKET_PATH = OUT / PROBE["packet_directory"] / PACKET_ROW["packet_file"]
PACKET = json.loads(PACKET_PATH.read_text(encoding="utf-8"))
RUN_ROOT = OUT / "feasibility_probe_runs_r6" / "round_1"
STAGE_B_PATH = RUN_ROOT / "stage_b" / f"{REVIEW_ID}.json"
OLD_STAGE_D_PATH = RUN_ROOT / "stage_d" / f"{REVIEW_ID}.json"
STAGE_B_SCHEMA = OUT / "v2_core_stage_b.schema.candidate.json"
STAGE_B_PROMPT = OUT / "v2_core_stage_b.prompt.candidate_r2.md"
STAGE_D_SCHEMA = OUT / "v2_core_stage_d.schema.candidate.json"
STAGE_D_PROMPT = OUT / "v2_core_stage_d.prompt.candidate_r3.md"
TRUTH_TABLE = OUT / "permission_truth_table.json"


def _semantic_output() -> dict:
    old = json.loads(OLD_STAGE_D_PATH.read_text(encoding="utf-8"))
    return {key: value for key, value in old.items() if key not in PROGRAM_BOUND_FIELDS}


def _metadata() -> dict:
    return program_bound_metadata(
        packet=PACKET,
        packet_path=PACKET_PATH,
        stage_b_path=STAGE_B_PATH,
        schema_path=STAGE_D_SCHEMA,
        prompt_path=STAGE_D_PROMPT,
        truth_table_path=TRUTH_TABLE,
    )


def test_transport_requires_only_ai_semantics_and_pins_one_scenario() -> None:
    schema = json.loads(STAGE_D_SCHEMA.read_text(encoding="utf-8"))
    refs = [row["ref"] for row in PACKET["evidence_catalog"]]
    transport = semantic_transport_schema(
        schema,
        evidence_ref_values=refs,
        primary_scenario="MATURE_TREND_PULLBACK",
    )
    Draft202012Validator.check_schema(transport)
    assert set(PROGRAM_BOUND_FIELDS).isdisjoint(transport["properties"])
    assert set(PROGRAM_BOUND_FIELDS).isdisjoint(transport["required"])
    assert transport["properties"]["primary_scenario"]["const"] == "MATURE_TREND_PULLBACK"
    assert not list(Draft202012Validator(transport).iter_errors(_semantic_output()))


def test_program_metadata_binding_preserves_semantics_and_validates_full_contract() -> None:
    semantic = _semantic_output()
    output = bind_program_metadata(semantic, _metadata())
    assert {key: output[key] for key in semantic} == semantic
    assert output["stage_b_output_sha256"] == base.sha256(STAGE_B_PATH)
    validation = validate_stage_d(
        output,
        packet_path=PACKET_PATH,
        stage_b_path=STAGE_B_PATH,
        stage_b_schema_path=STAGE_B_SCHEMA,
        stage_b_prompt_path=STAGE_B_PROMPT,
        schema_path=STAGE_D_SCHEMA,
        prompt_path=STAGE_D_PROMPT,
        truth_table_path=TRUTH_TABLE,
    )
    assert validation["status"] == "PASS"


def test_ai_cannot_submit_or_override_program_bound_metadata() -> None:
    semantic = _semantic_output()
    semantic["stage_b_output_sha256"] = "0" * 64
    with pytest.raises(base.StagedRunnerError, match="program-bound fields"):
        bind_program_metadata(semantic, _metadata())


def test_rendered_prompt_marks_metadata_as_program_only() -> None:
    stage_b = json.loads(STAGE_B_PATH.read_text(encoding="utf-8"))
    source = STAGE_D_PROMPT.read_text(encoding="utf-8")
    selected = base.selected_stage_d_prompt(source, "MATURE_TREND_PULLBACK")
    rendered = rendered_stage_d_prompt(
        prompt_text=selected,
        packet=PACKET,
        stage_b=stage_b,
        metadata=_metadata(),
        primary_scenario="MATURE_TREND_PULLBACK",
    )
    assert "program_bound_metadata_not_for_ai_output" in rendered
    assert "不得輸出program_bound_metadata_not_for_ai_output" in rendered
    assert "### `MACRO_COPY_RESONANCE`" not in rendered
