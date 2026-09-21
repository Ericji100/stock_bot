import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from scripts.hybrid_v3_atomic_runner_v2 import RetryableReviewError
from scripts.hybrid_v3_codex_reviewer_v3 import (
    ADAPTER_STATUS,
    exact_evidence_structured_output_schema,
    validate_model_transport,
)


ROOT = Path(__file__).resolve().parents[1]
PRIMARY = (
    ROOT
    / "reports/course_backtest/2024-02-02"
    / "historical_scan_2023h2_formal_ai_v3_daily_scan"
    / "hybrid_monitoring_candidate4_v5/research_track_v3/primary_packets.jsonl"
)
SCHEMA = ROOT / "config/hybrid_atomic_semantics_v2.schema.json"


def _first_packet() -> dict:
    return json.loads(PRIMARY.read_text(encoding="utf-8-sig").splitlines()[0])


def test_reviewer_v3_is_final() -> None:
    assert ADAPTER_STATUS == "FINAL"


def test_transport_schema_enumerates_only_packet_evidence_refs() -> None:
    packet = _first_packet()
    schema = json.loads(SCHEMA.read_text(encoding="utf-8-sig"))
    transport = exact_evidence_structured_output_schema(schema, packet)
    expected = sorted(row["ref"] for row in packet["evidence"])
    assert transport["$defs"]["evidenceRef"] == {
        "type": "string",
        "enum": expected,
    }
    assert "REL_CANDIDATE:R-b57731032559c12d" not in expected


def test_packet_specific_evidence_enum_rejects_alias_typo() -> None:
    packet = _first_packet()
    schema = json.loads(SCHEMA.read_text(encoding="utf-8-sig"))
    transport = exact_evidence_structured_output_schema(schema, packet)
    evidence_validator = Draft202012Validator(transport["$defs"]["evidenceRef"])
    assert not list(evidence_validator.iter_errors(packet["evidence"][0]["ref"]))
    errors = list(evidence_validator.iter_errors("REL_CANDIDATE:R-b57731032559c12d"))
    assert errors


def test_model_transport_validation_failure_is_retryable() -> None:
    packet = _first_packet()
    schema = json.loads(SCHEMA.read_text(encoding="utf-8-sig"))
    transport = exact_evidence_structured_output_schema(schema, packet)
    with pytest.raises(RetryableReviewError, match="model output contract validation failed"):
        validate_model_transport(
            packet=packet,
            transport_schema=transport,
            transport_output={},
        )
