from __future__ import annotations

import copy
import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from scripts.hybrid_v3_atomic_policy_v2 import expected_question_manifest, validate_atomic
from scripts.hybrid_v3_atomic_runner_v2 import AtomicCaseRunner, ReviewerResult
from scripts.hybrid_v3_codex_reviewer_v2 import (
    ADAPTER_STATUS,
    ADAPTER_VERSION,
    CodexAtomicReviewerV2,
    POLICY_PATH,
    PROMPT_PATH,
    PROTOCOL_PATH,
    SCHEMA_PATH,
    ReviewContractError,
    RetryableReviewError,
    build_prompt,
    canonicalize_transport_output,
    structured_output_schema,
    validate_transport_output,
    verify_execution_contract,
)
from scripts.hybrid_v3_sharding_v2 import build_case_records, canonical_sha256, file_sha256


def _packet() -> dict:
    endpoints = ("2023-04-03", "2023-04-14", "2023-05-01", "2023-05-15")
    windows = {}
    window_evidence = []
    for scale in ("LARGE", "SMALL"):
        window = {
            "scale": scale, "direction": "UP",
            "selection_policy": "LATEST_TWO_COMPLETED_SAME_DIRECTION_LEGACY_PIVOT_LEGS",
            "baseline": {
                "start_date": endpoints[0], "end_date": endpoints[1],
                "start_ref": f"BAR:{endpoints[0]}", "end_ref": f"BAR:{endpoints[1]}",
                "start_price": 8.0, "end_price": 9.0, "bar_count": 10,
                "price_change_pct": 12.5, "slope_pct_per_bar": 1.25,
                "mean_true_range_pct": 2.0, "realized_close_volatility_pct": 1.5,
            },
            "current": {
                "start_date": endpoints[2], "end_date": endpoints[3],
                "start_ref": f"BAR:{endpoints[2]}", "end_ref": f"BAR:{endpoints[3]}",
                "start_price": 9.0, "end_price": 10.0, "bar_count": 11,
                "price_change_pct": 11.111, "slope_pct_per_bar": 1.01,
                "mean_true_range_pct": 2.2, "realized_close_volatility_pct": 1.7,
            },
            "baseline_available_on": endpoints[1], "current_available_on": endpoints[3],
            "available_on": endpoints[3],
        }
        windows[scale] = window
        window_evidence.append({
            "ref": f"COMPARISON_WINDOW:{scale}:fixture", "kind": "CAUSAL_COMPARISON_WINDOW",
            "date": endpoints[3], "values": window,
        })
    packet = {
        "review_id": "D-" + "1" * 24,
        "anonymous_stock_id": "S-" + "2" * 16,
        "as_of": "2023-06-01",
        "question_manifest": {
            "anchor_candidates": [],
            "relation_candidates": [],
            "stop_candidates": [],
            "global_question_ids": [],
        },
        "evidence": [
            *[{"ref": f"BAR:{day}", "kind": "BAR", "date": day, "values": {"close": 10.0}} for day in endpoints],
            *window_evidence,
            {"ref": "BAR:2023-06-01", "date": "2023-06-01", "values": {"close": 10.0}},
        ],
        "objective_facts": {
            "causal_cutoff_as_of": "2023-06-01",
            "full_history_visible_bar_count": 750,
            "data_sufficiency_by_route": {
                scenario: {
                    "status": False, "required_visible_bars": 200 if scenario == "FRESH_Q1_EXPANSION" else 750,
                    "actual_visible_bars": 750,
                    "reason_code": "INSUFFICIENT_ROUTE_OBJECTIVE_EVIDENCE",
                }
                for scenario in (
                    "MATURE_TREND_PULLBACK", "MACRO_COPY_RESONANCE",
                    "BEAR_REVERSAL_LEFT_RIGHT", "FRESH_Q1_EXPANSION",
                )
            },
            "working_comparison_windows": windows,
            "scenario_hypotheses": {
            "MATURE_TREND_PULLBACK": [], "MACRO_COPY_RESONANCE": [],
            "BEAR_REVERSAL_LEFT_RIGHT": [], "FRESH_Q1_EXPANSION": [],
        }},
    }
    from scripts.hybrid_v3_sharding_v2 import canonical_sha256

    packet["question_manifest_sha256"] = canonical_sha256(packet["question_manifest"])
    packet["evidence_catalog_sha256"] = canonical_sha256(packet["evidence"])
    packet["objective_facts"]["ai_visible_evidence_sha256"] = packet["evidence_catalog_sha256"]
    core = dict(packet)
    packet["input_packet_sha256"] = canonical_sha256(core)
    return packet


def test_reviewer_component_is_final_for_formal_freeze():
    assert ADAPTER_STATUS == "FINAL"


def _multi_packet() -> dict:
    packet = _packet()
    packet["objective_facts"]["scenario_hypotheses"] = {
        "MATURE_TREND_PULLBACK": [{
            "hypothesis_id": "H-MATURE",
            "anchor_ref": "ANCHOR_CANDIDATE:A-MATURE",
            "relation_ref": "RELATION_CANDIDATE:R-MATURE",
            "episode_stop_ref": "STOP_CANDIDATE:T-MATURE",
            "campaign_stop_ref": "STOP_CANDIDATE:T-MATURE",
            "position_role": "MOTHER",
            "taiji_generation": "COPY_LEG_3",
            "same_direction_attack_number": 1,
            "completed_prior_copy_count": 1,
        }],
        "MACRO_COPY_RESONANCE": [],
        "BEAR_REVERSAL_LEFT_RIGHT": [],
        "FRESH_Q1_EXPANSION": [{
            "hypothesis_id": "H-FRESH",
            "anchor_ref": "ANCHOR_CANDIDATE:A-FRESH",
            "relation_ref": "RELATION_CANDIDATE:R-FRESH",
            "episode_stop_ref": "STOP_CANDIDATE:T-FRESH",
            "campaign_stop_ref": "STOP_CANDIDATE:T-FRESH",
            "position_role": "MOTHER",
            "taiji_generation": "ANCHOR_LEG_1",
            "same_direction_attack_number": 1,
            "completed_prior_copy_count": 0,
        }],
    }
    packet["question_manifest"] = expected_question_manifest(packet)
    packet["question_manifest_sha256"] = canonical_sha256(packet["question_manifest"])
    packet["evidence_catalog_sha256"] = canonical_sha256(packet["evidence"])
    packet["objective_facts"]["ai_visible_evidence_sha256"] = packet["evidence_catalog_sha256"]
    core = dict(packet)
    core.pop("input_packet_sha256", None)
    packet["input_packet_sha256"] = canonical_sha256(core)
    return packet


def _verdict(ref: str = "BAR:2023-06-01") -> dict:
    return {
        "result": "PASS",
        "supporting_evidence_refs": [ref],
        "contradicting_evidence_refs": [],
        "missing_evidence_codes": [],
        "reason_code": "VISIBLE_EVIDENCE_SATISFIES_DEFINITION",
    }


def _multi_output(packet: dict) -> dict:
    output = _output(packet)
    groups = {
        "anchor_candidates": "anchor_candidates",
        "relation_candidates": "relation_candidates",
        "stop_candidates": "stop_candidates",
    }
    output["candidate_answers"] = {
        output_group: [
            {
                "subject_ref": entry["subject_ref"],
                "answers": {question_id: _verdict() for question_id in entry["required_question_ids"]},
            }
            for entry in packet["question_manifest"][manifest_group]
        ]
        for output_group, manifest_group in groups.items()
    }
    output["global_answers"] = {
        question_id: _verdict()
        for question_id in packet["question_manifest"]["global_question_ids"]
    }
    return output


def _flatten_output(packet: dict, canonical: dict, *, reverse: bool = False) -> dict:
    rows = []
    for group, kind in (
        ("anchor_candidates", "anchor"),
        ("relation_candidates", "relation"),
        ("stop_candidates", "stop"),
    ):
        for candidate in canonical["candidate_answers"][group]:
            rows.extend({
                "subject_kind": kind,
                "subject_ref": candidate["subject_ref"],
                "question_id": question_id,
                "verdict": verdict,
            } for question_id, verdict in candidate["answers"].items())
    rows.extend({
        "subject_kind": "global",
        "subject_ref": "__GLOBAL__",
        "question_id": question_id,
        "verdict": verdict,
    } for question_id, verdict in canonical["global_answers"].items())
    if reverse:
        rows.reverse()
    return {
        key: canonical[key]
        for key in (
            "schema_version", "protocol_version", "contract_status", "review_id",
            "anonymous_stock_id", "as_of", "input_packet_sha256",
            "question_manifest_sha256", "evidence_catalog_sha256",
        )
    } | {"answers": rows, "causal_attestation": canonical["causal_attestation"]}


def _output(packet: dict) -> dict:
    return {
        "schema_version": "hybrid-atomic-semantics-v2",
        "protocol_version": "hybrid-monitoring-protocol-v2",
        "contract_status": "FINAL",
        "review_id": packet["review_id"],
        "anonymous_stock_id": packet["anonymous_stock_id"],
        "as_of": packet["as_of"],
        "input_packet_sha256": packet["input_packet_sha256"],
        "question_manifest_sha256": packet["question_manifest_sha256"],
        "evidence_catalog_sha256": packet["evidence_catalog_sha256"],
        "candidate_answers": {
            "anchor_candidates": [], "relation_candidates": [], "stop_candidates": [],
        },
        "global_answers": {},
        "causal_attestation": {
            "latest_visible_bar": packet["as_of"],
            "used_future_data": False,
            "identity_visible": False,
            "performance_visible": False,
            "invented_evidence_ref": False,
            "invented_candidate_ref": False,
            "answered_complete_manifest": True,
            "selected_scenario": False,
            "selected_phase": False,
            "selected_route": False,
            "decided_permission": False,
            "issued_trade_instruction": False,
        },
    }


def _transport_output(packet: dict) -> dict:
    canonical = _output(packet)
    return {
        key: canonical[key]
        for key in (
            "schema_version", "protocol_version", "contract_status", "review_id",
            "anonymous_stock_id", "as_of", "input_packet_sha256",
            "question_manifest_sha256", "evidence_catalog_sha256",
        )
    } | {"answers": [], "causal_attestation": canonical["causal_attestation"]}


def _schema(tmp_path: Path) -> Path:
    source = Path("config/hybrid_atomic_semantics_v2.schema.json")
    target = tmp_path / "schema.json"
    target.write_bytes(source.read_bytes())
    return target


def _execution_contract(tmp_path: Path, *, omit: str | None = None) -> Path:
    root = Path(__file__).resolve().parents[1]
    component_paths = {
        "reviewer": root / "scripts/hybrid_v3_codex_reviewer_v2.py",
        "policy": POLICY_PATH,
        "prompt": PROMPT_PATH,
        "schema": SCHEMA_PATH,
        "protocol": PROTOCOL_PATH,
    }
    payload = {
        "status": "FROZEN_IMMUTABLE_BEFORE_AI_EXECUTION",
        "v2_components": [
            {
                "name": name,
                "relative_path": str(path.resolve().relative_to(root.resolve())).replace("\\", "/"),
                "status": "FINAL",
                "sha256": file_sha256(path),
            }
            for name, path in component_paths.items()
            if name != omit
        ],
        "execution_contract": {"model": "gpt-5.6-sol", "reasoning_effort": "xhigh"},
    }
    path = tmp_path / "freeze_manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _formal_records(packet: dict, contract_path: Path) -> list[dict]:
    return build_case_records(
        [packet],
        source_manifest_sha256=hashlib.sha256(b"source").hexdigest(),
        protocol_sha256=file_sha256(PROTOCOL_PATH),
        prompt_sha256=file_sha256(PROMPT_PATH),
        schema_sha256=file_sha256(SCHEMA_PATH),
        execution_contract_sha256=file_sha256(contract_path),
        model="gpt-5.6-sol",
        reasoning_effort="xhigh",
    )


def test_structured_schema_removes_runtime_metadata_but_keeps_contract():
    source = json.loads(Path("config/hybrid_atomic_semantics_v2.schema.json").read_text(encoding="utf-8"))
    result = structured_output_schema(source)
    serialized = json.dumps(result)
    assert "$id" not in result
    assert not any(key.startswith("x-") for key in result)
    assert '"format"' not in serialized
    assert '"uniqueItems"' not in serialized
    assert '"allOf"' not in serialized
    assert '"anyOf"' not in serialized
    assert result["additionalProperties"] is False
    assert "candidate_answers" in result["required"]
    verdict = result["$defs"]["atomicVerdict"]
    assert set(verdict["required"]) == set(verdict["properties"])


def test_packet_specific_transport_schema_is_flat_and_exact():
    source = json.loads(Path("config/hybrid_atomic_semantics_v2.schema.json").read_text(encoding="utf-8"))
    result = structured_output_schema(source, _packet())
    assert "answers" in result["properties"]
    assert "candidate_answers" not in result["properties"]
    assert {"evidenceRef", "evidenceRefs", "missingEvidenceCodes", "atomicVerdict"}.issubset(
        result["$defs"]
    )
    assert result["properties"]["answers"]["minItems"] == 0
    assert result["properties"]["answers"]["maxItems"] == 0
    assert "non_policy_note" not in result["$defs"]["atomicVerdict"]["properties"]


def test_transport_output_canonicalization_rejects_missing_or_extra_pairs():
    packet = _packet()
    assert canonicalize_transport_output(packet, _transport_output(packet)) == _output(packet)
    broken = _transport_output(packet)
    broken["answers"] = [{
        "subject_kind": "global", "subject_ref": "__GLOBAL__",
        "question_id": "EXTRA", "verdict": {},
    }]
    with pytest.raises(ReviewContractError, match="differ from question manifest"):
        canonicalize_transport_output(packet, broken)


def test_multi_subject_transport_shuffled_roundtrip_and_local_validation():
    packet = _multi_packet()
    expected = _multi_output(packet)
    transport = _flatten_output(packet, expected, reverse=True)
    schema = structured_output_schema(
        json.loads(SCHEMA_PATH.read_text(encoding="utf-8")), packet
    )
    validate_transport_output(schema, transport)
    actual = canonicalize_transport_output(packet, transport)
    assert actual == expected
    assert validate_atomic(packet, actual) == []


@pytest.mark.parametrize("failure", ["wrong_pair", "duplicate_missing", "extra_top", "extra_row", "extra_verdict"])
def test_multi_subject_transport_rejects_wrong_pairs_and_extra_fields(failure):
    packet = _multi_packet()
    transport = _flatten_output(packet, _multi_output(packet))
    schema = structured_output_schema(
        json.loads(SCHEMA_PATH.read_text(encoding="utf-8")), packet
    )
    if failure == "wrong_pair":
        anchor_rows = [row for row in transport["answers"] if row["subject_kind"] == "anchor"]
        different = next(row for row in anchor_rows if row["subject_ref"] != anchor_rows[0]["subject_ref"])
        anchor_rows[0]["subject_ref"] = different["subject_ref"]
    elif failure == "duplicate_missing":
        transport["answers"][0] = copy.deepcopy(transport["answers"][1])
    elif failure == "extra_top":
        transport["route"] = "TRADE"
    elif failure == "extra_row":
        transport["answers"][0]["route"] = "TRADE"
    else:
        transport["answers"][0]["verdict"]["trade_plan"] = "BUY"
    if failure in {"wrong_pair", "duplicate_missing"}:
        validate_transport_output(schema, transport)
        with pytest.raises(ReviewContractError):
            canonicalize_transport_output(packet, transport)
    else:
        with pytest.raises(ReviewContractError):
            validate_transport_output(schema, transport)


def test_local_atomic_validation_rejects_unknown_evidence_after_roundtrip():
    packet = _multi_packet()
    transport = _flatten_output(packet, _multi_output(packet))
    transport["answers"][0]["verdict"]["supporting_evidence_refs"] = ["BAR:1999-01-01"]
    schema = structured_output_schema(
        json.loads(SCHEMA_PATH.read_text(encoding="utf-8")), packet
    )
    validate_transport_output(schema, transport)
    canonical = canonicalize_transport_output(packet, transport)
    assert any("unknown evidence ref" in error for error in validate_atomic(packet, canonical))


def test_prompt_is_single_case_anonymous_and_rejects_future():
    packet = _packet()
    prompt = build_prompt(packet, "SYSTEM")
    assert packet["review_id"] in prompt
    assert "不得猜股票身分" in prompt
    tampered = copy.deepcopy(packet)
    tampered["evidence"].append({"ref": "BAR:2023-06-02", "date": "2023-06-02", "values": {"close": 11}})
    with pytest.raises(Exception, match="future date"):
        build_prompt(tampered, "SYSTEM")


def test_reviewer_uses_isolated_read_only_codex_and_returns_json(tmp_path):
    packet = _packet()
    expected = _output(packet)
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("SYSTEM", encoding="utf-8")
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text(json.dumps(_transport_output(packet)), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    reviewer = CodexAtomicReviewerV2(
        prompt_path=prompt_path,
        schema_path=_schema(tmp_path),
        run_command=fake_run,
    )
    actual = reviewer(packet)
    assert isinstance(actual, ReviewerResult)
    assert actual.output == expected
    assert actual.adapter_version == ADAPTER_VERSION
    assert actual.adapter_status == ADAPTER_STATUS
    assert validate_atomic(packet, actual.output) == []
    assert all(len(value) == 64 for value in (
        actual.adapter_code_sha256,
        actual.dynamic_transport_schema_sha256,
        actual.rendered_prompt_sha256,
        actual.raw_transport_output_sha256,
    ))
    command, kwargs = calls[0]
    assert command[0:3] == ["codex", "exec", "-"]
    assert "--ephemeral" in command and "--ignore-user-config" in command and "--ignore-rules" in command
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert packet["review_id"] in kwargs["input"]


def test_reviewer_rejects_prompt_change_before_model_call(tmp_path):
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("SYSTEM", encoding="utf-8")
    calls = []
    reviewer = CodexAtomicReviewerV2(
        prompt_path=prompt_path,
        schema_path=_schema(tmp_path),
        run_command=lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    prompt_path.write_text("CHANGED", encoding="utf-8")
    with pytest.raises(ReviewContractError, match="prompt changed"):
        reviewer(_packet())
    assert calls == []


@pytest.mark.parametrize("failure", ["timeout", "transient_nonzero"])
def test_reviewer_retries_only_explicit_transient_failures(tmp_path, failure):
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("SYSTEM", encoding="utf-8")

    def fake_run(command, **_kwargs):
        output_path = Path(command[command.index("--output-last-message") + 1])
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 1)
        if failure == "transient_nonzero":
            return subprocess.CompletedProcess(command, 9, stdout="", stderr="HTTP 503 service unavailable")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    reviewer = CodexAtomicReviewerV2(
        prompt_path=prompt_path,
        schema_path=_schema(tmp_path),
        run_command=fake_run,
    )
    with pytest.raises(RetryableReviewError):
        reviewer(_packet())


@pytest.mark.parametrize("failure", ["deterministic_nonzero", "missing", "bad_json"])
def test_reviewer_does_not_retry_deterministic_config_or_output_contract_failures(tmp_path, failure):
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("SYSTEM", encoding="utf-8")

    def fake_run(command, **_kwargs):
        output_path = Path(command[command.index("--output-last-message") + 1])
        if failure == "deterministic_nonzero":
            return subprocess.CompletedProcess(
                command, 2, stdout="", stderr="invalid output schema configuration"
            )
        if failure == "bad_json":
            output_path.write_text("not-json", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    reviewer = CodexAtomicReviewerV2(
        prompt_path=prompt_path,
        schema_path=_schema(tmp_path),
        run_command=fake_run,
    )
    with pytest.raises(ReviewContractError):
        reviewer(_packet())


def test_non_object_output_is_contract_error(tmp_path):
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("SYSTEM", encoding="utf-8")

    def fake_run(command, **_kwargs):
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text("[]", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    reviewer = CodexAtomicReviewerV2(
        prompt_path=prompt_path,
        schema_path=_schema(tmp_path),
        run_command=fake_run,
    )
    with pytest.raises(ReviewContractError, match="not a JSON object"):
        reviewer(_packet())


def test_execution_contract_pins_actual_files_and_record_hashes(tmp_path):
    contract_path = _execution_contract(tmp_path)
    records = _formal_records(_multi_packet(), contract_path)
    verified = verify_execution_contract(records, execution_contract_path=contract_path)
    assert verified["execution_contract_sha256"] == file_sha256(contract_path)

    changed = copy.deepcopy(records)
    changed[0]["prompt_sha256"] = "0" * 64
    with pytest.raises(ReviewContractError, match="prompt_sha256 differs"):
        verify_execution_contract(changed, execution_contract_path=contract_path)

    missing_policy = _execution_contract(tmp_path / "missing", omit="policy")
    missing_records = _formal_records(_multi_packet(), missing_policy)
    with pytest.raises(ReviewContractError, match="does not pin component: policy"):
        verify_execution_contract(missing_records, execution_contract_path=missing_policy)


def test_nonempty_reviewer_runner_e2e_records_formal_transport_audit(tmp_path):
    packet = _multi_packet()
    canonical = _multi_output(packet)
    transport = _flatten_output(packet, canonical, reverse=True)
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("SYSTEM", encoding="utf-8")

    def fake_run(command, **_kwargs):
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text(json.dumps(transport), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    reviewer = CodexAtomicReviewerV2(
        prompt_path=prompt_path,
        schema_path=_schema(tmp_path),
        run_command=fake_run,
    )
    records = build_case_records(
        [packet],
        source_manifest_sha256=hashlib.sha256(b"source").hexdigest(),
        protocol_sha256=hashlib.sha256(b"protocol").hexdigest(),
        prompt_sha256=file_sha256(prompt_path),
        schema_sha256=file_sha256(reviewer.schema_path),
        execution_contract_sha256=hashlib.sha256(b"execution-contract").hexdigest(),
        model="gpt-5.6-sol",
        reasoning_effort="xhigh",
    )
    runner = AtomicCaseRunner(
        output_dir=tmp_path / "run",
        reviewer=reviewer,
        validator=validate_atomic,
        run_number=1,
        expected_reviewer_identity={
            "adapter_version": ADAPTER_VERSION,
            "adapter_status": ADAPTER_STATUS,
            "adapter_code_sha256": file_sha256(
                Path(__file__).resolve().parents[1] / "scripts/hybrid_v3_codex_reviewer_v2.py"
            ),
        },
    )
    result = runner.run_case(records[0])
    assert result.envelope["output"] == canonical
    assert result.envelope["reviewer_audit"]["adapter_version"] == ADAPTER_VERSION
    assert len(result.envelope["reviewer_audit_sha256"]) == 64
    resumed = runner.run_case(records[0])
    assert resumed.resumed is True
    assert resumed.envelope == result.envelope
