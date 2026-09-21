import hashlib
import json
from pathlib import Path

import pytest

from scripts.hybrid_v3_atomic_runner_v2 import (
    AtomicCaseRunner,
    ExecutionIntegrityError,
    ReviewerResult,
    RUNNER_STATUS,
    RetryableReviewError,
    ReviewContractError,
    merge_envelopes,
)
from scripts.hybrid_v3_sharding_v2 import (
    ShardIntegrityError,
    assign_shards,
    build_case_records,
    canonical_json_bytes,
    canonical_sha256,
    source_content_sha256,
    SHARDING_STATUS,
    validate_shards,
    write_shards,
)


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def test_execution_components_are_final_for_formal_freeze():
    assert RUNNER_STATUS == "FINAL"
    assert SHARDING_STATUS == "FINAL"


def _packets(count: int = 9):
    return [
        {
            "review_id": f"D-{index:03d}",
            "anonymous_stock_id": f"S-{index % 4}",
            "as_of": f"2023-06-{index + 1:02d}",
            "evidence": [{"ref": f"BAR:{index}"}],
        }
        for index in range(count)
    ]


def _records(count: int = 9):
    packets = _packets(count)
    return build_case_records(
        packets,
        source_manifest_sha256=source_content_sha256(packets),
        protocol_sha256=_digest("protocol"),
        prompt_sha256=_digest("prompt"),
        schema_sha256=_digest("schema"),
        execution_contract_sha256=_digest("execution-contract"),
        model="test-model",
        reasoning_effort="xhigh",
    )


def _decision(packet, *, value="PASS"):
    return {
        "review_id": packet["review_id"],
        "anonymous_stock_id": packet["anonymous_stock_id"],
        "as_of": packet["as_of"],
        "atomic_gate": value,
    }


def _reviewer_result(packet, *, value="PASS"):
    return ReviewerResult(
        output=_decision(packet, value=value),
        adapter_version="test-reviewer-v1",
        adapter_status="FINAL",
        adapter_code_sha256=_digest("adapter"),
        dynamic_transport_schema_sha256=_digest("transport-schema"),
        rendered_prompt_sha256=_digest(f"prompt:{packet['review_id']}"),
        raw_transport_output_sha256=_digest(f"raw:{packet['review_id']}:{value}"),
    )


def test_canonical_json_and_case_identity_are_order_stable_but_input_sensitive():
    left = {"b": 2, "a": {"x": 1}}
    right = {"a": {"x": 1}, "b": 2}
    assert canonical_json_bytes(left) == canonical_json_bytes(right)
    assert canonical_sha256(left) == canonical_sha256(right)

    first = _records(1)[0]
    packets = _packets(1)
    changed = build_case_records(
        packets,
        source_manifest_sha256=source_content_sha256(packets),
        protocol_sha256=_digest("protocol"),
        prompt_sha256=_digest("prompt"),
        schema_sha256=_digest("different-schema"),
        execution_contract_sha256=_digest("execution-contract"),
        model="test-model",
        reasoning_effort="xhigh",
    )[0]
    assert first["case_key"] != changed["case_key"]


def test_official_enriched_review_point_source_feeds_runner_without_wrapping_packet_twice():
    packets = _packets(6)
    source = [
        {
            "source_ordinal": ordinal,
            "review_id": packet["review_id"],
            "anonymous_stock_id": packet["anonymous_stock_id"],
            "sampling_stratum": "WAIT_POLICY_BOUNDARY",
            "packet_sha256": canonical_sha256(packet),
            "packet": packet,
        }
        for ordinal, packet in enumerate(packets)
    ]
    records = build_case_records(
        source,
        source_manifest_sha256=source_content_sha256(source),
        protocol_sha256=_digest("protocol"),
        prompt_sha256=_digest("prompt"),
        schema_sha256=_digest("schema"),
        execution_contract_sha256=_digest("execution-contract"),
        model="test-model",
        reasoning_effort="xhigh",
    )
    assert [row["packet"] for row in records] == packets
    assert {row["sampling_stratum"] for row in records} == {"WAIT_POLICY_BOUNDARY"}
    broken = [dict(row) for row in source]
    broken[0]["source_ordinal"] = 1
    with pytest.raises(ShardIntegrityError, match="ordinals are not exact"):
        build_case_records(
            broken,
            source_manifest_sha256=source_content_sha256(broken),
            protocol_sha256=_digest("protocol"), prompt_sha256=_digest("prompt"),
            schema_sha256=_digest("schema"),
            execution_contract_sha256=_digest("execution-contract"),
            model="test-model", reasoning_effort="xhigh",
        )

    changed_contract = build_case_records(
        packets,
        source_manifest_sha256=source_content_sha256(packets),
        protocol_sha256=_digest("protocol"),
        prompt_sha256=_digest("prompt"),
        schema_sha256=_digest("schema"),
        execution_contract_sha256=_digest("different-execution-contract"),
        model="test-model",
        reasoning_effort="xhigh",
    )[0]
    assert records[0]["case_key"] != changed_contract["case_key"]


def test_three_shards_are_deterministic_disjoint_and_complete(tmp_path):
    records = _records(60)
    first = assign_shards(records, shard_count=3)
    second = assign_shards(records, shard_count=3)
    assert [[row["case_key"] for row in shard] for shard in first] == [
        [row["case_key"] for row in shard] for shard in second
    ]
    assert validate_shards(records, first, shard_count=3) == {
        "expected": 60,
        "covered": 60,
        "missing": 0,
        "overlap": 0,
        "unexpected": 0,
    }
    manifest = write_shards(records, tmp_path / "shards", shard_count=3)
    assert manifest["shard_count"] == 3
    assert sum(row["rows"] for row in manifest["shards"]) == 60


def test_shard_validation_rejects_overlap_and_missing():
    records = _records(12)
    shards = assign_shards(records, shard_count=3)
    occupied = next(index for index, shard in enumerate(shards) if shard)
    target = (occupied + 1) % 3
    duplicated = [list(shard) for shard in shards]
    duplicate = dict(duplicated[occupied][0])
    duplicate["shard_id"] = target
    duplicated[target].append(duplicate)
    duplicated[target].sort(key=lambda row: row["source_ordinal"])
    with pytest.raises(ShardIntegrityError, match="overlap"):
        validate_shards(records, duplicated, shard_count=3)

    missing = [list(shard) for shard in shards]
    missing[occupied] = missing[occupied][1:]
    with pytest.raises(ShardIntegrityError, match="missing"):
        validate_shards(records, missing, shard_count=3)


def test_atomic_runner_retries_only_technical_failures_and_resumes(tmp_path):
    record = _records(1)[0]
    calls = []

    def reviewer(packet):
        calls.append(packet["review_id"])
        if len(calls) < 3:
            raise RetryableReviewError("temporary transport failure")
        return _reviewer_result(packet)

    runner = AtomicCaseRunner(
        output_dir=tmp_path / "run",
        reviewer=reviewer,
        validator=lambda packet, output: [],
        run_number=1,
        retry_delays=(0, 0),
    )
    first = runner.run_case(record)
    assert first.resumed is False
    assert len(calls) == 3
    assert first.envelope["successful_attempt"] == 3

    second = runner.run_case(record)
    assert second.resumed is True
    assert len(calls) == 3
    assert second.envelope == first.envelope


def test_validation_and_unclassified_errors_are_not_retried(tmp_path):
    record = _records(1)[0]
    calls = 0

    def reviewer(packet):
        nonlocal calls
        calls += 1
        return _reviewer_result(packet)

    runner = AtomicCaseRunner(
        output_dir=tmp_path / "invalid",
        reviewer=reviewer,
        validator=lambda packet, output: ["gate evidence is invalid"],
        run_number=1,
    )
    with pytest.raises(ReviewContractError, match="non-retryable"):
        runner.run_case(record)
    assert calls == 1

    generic_calls = 0

    def broken(_packet):
        nonlocal generic_calls
        generic_calls += 1
        raise ValueError("reviewer bug")

    broken_runner = AtomicCaseRunner(
        output_dir=tmp_path / "broken",
        reviewer=broken,
        validator=None,
        run_number=1,
    )
    with pytest.raises(ReviewContractError, match="non-retryable"):
        broken_runner.run_case(record)
    assert generic_calls == 1


def test_technical_failure_becomes_terminal_after_fixed_attempt_limit(tmp_path):
    record = _records(1)[0]
    calls = 0

    def unavailable(_packet):
        nonlocal calls
        calls += 1
        raise TimeoutError("service timeout")

    runner = AtomicCaseRunner(
        output_dir=tmp_path / "run",
        reviewer=unavailable,
        validator=None,
        run_number=2,
        max_attempts=3,
        retry_delays=(0, 0),
    )
    with pytest.raises(RetryableReviewError, match="exhausted"):
        runner.run_case(record)
    assert calls == 3
    attempts = sorted((tmp_path / "run" / "attempts").rglob("attempt_*.json"))
    assert [json.loads(path.read_text())["status"] for path in attempts] == [
        "RETRYABLE_ERROR",
        "RETRYABLE_ERROR",
        "TERMINAL_ERROR",
    ]


def test_merge_is_in_source_order_and_rejects_missing_or_overlap(tmp_path):
    records = _records(15)
    shards = assign_shards(records, shard_count=3)
    roots = []
    for shard_index, shard in enumerate(shards):
        root = tmp_path / f"worker-{shard_index}"
        roots.append(root)
        runner = AtomicCaseRunner(
            output_dir=root,
            reviewer=lambda packet: _reviewer_result(packet, value=packet["review_id"]),
            validator=lambda packet, output: [],
            run_number=1,
        )
        runner.run_cases(list(reversed(shard)))

    ledger = tmp_path / "merged.jsonl"
    manifest = merge_envelopes(records, envelope_dirs=list(reversed(roots)), run_number=1, output_path=ledger)
    merged = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    assert [row["review_id"] for row in merged] == [record["review_id"] for record in records]
    assert manifest["valid_rows"] == len(records)
    assert manifest["execution_contract_sha256"] == _digest("execution-contract")
    assert len(manifest["reviewer_audit_order_sha256"]) == 64
    assert hashlib.sha256(ledger.read_bytes()).hexdigest() == manifest["ledger_sha256"]

    one_case_file = next((roots[0] / "cases").glob("*.json"))
    with pytest.raises(ExecutionIntegrityError, match="overlapping"):
        merge_envelopes(
            records,
            envelope_dirs=[*roots, roots[0]],
            run_number=1,
            output_path=tmp_path / "overlap.jsonl",
        )
    one_case_file.unlink()
    with pytest.raises(ExecutionIntegrityError, match="missing"):
        merge_envelopes(
            records,
            envelope_dirs=roots,
            run_number=1,
            output_path=tmp_path / "missing.jsonl",
        )


def test_resume_rejects_tampered_immutable_envelope(tmp_path):
    record = _records(1)[0]
    runner = AtomicCaseRunner(
        output_dir=tmp_path / "run",
        reviewer=lambda packet: _reviewer_result(packet),
        validator=lambda packet, output: [],
        run_number=3,
    )
    result = runner.run_case(record)
    envelope_path = next((tmp_path / "run" / "cases").glob("*.json"))
    tampered = dict(result.envelope)
    tampered["output_sha256"] = "0" * 64
    envelope_path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ExecutionIntegrityError, match="output hash mismatch"):
        runner.run_case(record)


def test_resume_rejects_tampered_reviewer_audit(tmp_path):
    record = _records(1)[0]
    runner = AtomicCaseRunner(
        output_dir=tmp_path / "run",
        reviewer=lambda packet: _reviewer_result(packet),
        validator=lambda packet, output: [],
        run_number=4,
    )
    result = runner.run_case(record)
    envelope_path = next((tmp_path / "run" / "cases").glob("*.json"))
    tampered = dict(result.envelope)
    tampered["reviewer_audit"] = dict(tampered["reviewer_audit"])
    tampered["reviewer_audit"]["raw_transport_output_sha256"] = "0" * 64
    envelope_path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ExecutionIntegrityError, match="reviewer audit hash mismatch"):
        runner.run_case(record)
