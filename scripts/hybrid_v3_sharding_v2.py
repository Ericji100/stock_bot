"""Deterministic case identities and shards for hybrid monitoring V2.

This module is deliberately strategy-agnostic.  It never reads performance,
calls a model, or interprets semantic gates.  It turns an ordered anonymous
JSONL source into immutable case records and deterministic worker shards.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable, Sequence


SHARDING_VERSION = "hybrid-v3-sharding-v2"
SHARDING_STATUS = "FINAL"
DEFAULT_SHARD_COUNT = 3


class ShardIntegrityError(ValueError):
    """Raised when shard membership is incomplete, duplicated, or changed."""


def canonical_json_bytes(value: Any) -> bytes:
    """Return the one canonical JSON representation used by V2 identities."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ShardIntegrityError(f"JSONL row {line_number} is not an object: {path}")
            rows.append(value)
    return rows


def _canonical_jsonl_bytes(rows: Iterable[dict[str, Any]]) -> bytes:
    return b"".join(canonical_json_bytes(row) + b"\n" for row in rows)


def _publish_immutable(path: Path, payload: bytes) -> None:
    """Atomically create *path*; an existing different artifact is an error."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() == payload:
            return
        raise ShardIntegrityError(f"refusing to overwrite immutable artifact: {path}")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != payload:
                raise ShardIntegrityError(f"conflicting concurrent artifact: {path}")
        temporary.unlink(missing_ok=True)
    finally:
        temporary.unlink(missing_ok=True)


def _require_sha256(name: str, value: str) -> str:
    normalized = str(value).lower()
    if len(normalized) != 64 or any(character not in "0123456789abcdef" for character in normalized):
        raise ShardIntegrityError(f"{name} is not a SHA-256 hex digest")
    return normalized


def build_case_key(
    *,
    source_manifest_sha256: str,
    source_ordinal: int,
    review_id: str,
    packet_sha256: str,
    protocol_sha256: str,
    prompt_sha256: str,
    schema_sha256: str,
    execution_contract_sha256: str,
    model: str,
    reasoning_effort: str,
) -> str:
    """Build a stable identity that changes whenever an execution input changes."""
    identity = {
        "source_manifest_sha256": _require_sha256("source_manifest_sha256", source_manifest_sha256),
        "source_ordinal": int(source_ordinal),
        "review_id": str(review_id),
        "packet_sha256": _require_sha256("packet_sha256", packet_sha256),
        "protocol_sha256": _require_sha256("protocol_sha256", protocol_sha256),
        "prompt_sha256": _require_sha256("prompt_sha256", prompt_sha256),
        "schema_sha256": _require_sha256("schema_sha256", schema_sha256),
        "execution_contract_sha256": _require_sha256(
            "execution_contract_sha256", execution_contract_sha256
        ),
        "model": str(model),
        "reasoning_effort": str(reasoning_effort),
    }
    return canonical_sha256(identity)


def build_run_case_key(case_key: str, run_number: int) -> str:
    return canonical_sha256(
        {
            "case_key": _require_sha256("case_key", case_key),
            "run_number": int(run_number),
        }
    )


def source_content_sha256(packets: Sequence[dict[str, Any]]) -> str:
    """Hash an in-memory ordered source in canonical JSONL form."""
    return hashlib.sha256(_canonical_jsonl_bytes(packets)).hexdigest()


def build_case_records(
    packets: Sequence[dict[str, Any]],
    *,
    source_manifest_sha256: str,
    protocol_sha256: str,
    prompt_sha256: str,
    schema_sha256: str,
    execution_contract_sha256: str,
    model: str,
    reasoning_effort: str,
) -> list[dict[str, Any]]:
    """Attach immutable identities to an already ordered anonymous source."""
    source_manifest_sha256 = _require_sha256("source_manifest_sha256", source_manifest_sha256)
    seen_review_ids: set[str] = set()
    records: list[dict[str, Any]] = []
    for ordinal, source_row in enumerate(packets):
        if isinstance(source_row.get("packet"), dict):
            if source_row.get("source_ordinal") != ordinal:
                raise ShardIntegrityError("formal review-point source ordinals are not exact file order")
            packet = source_row["packet"]
            supplied_packet_sha = source_row.get("packet_sha256")
            if supplied_packet_sha != canonical_sha256(packet):
                raise ShardIntegrityError(f"formal source packet hash mismatch at ordinal {ordinal}")
            for field in ("review_id", "anonymous_stock_id"):
                if source_row.get(field) != packet.get(field):
                    raise ShardIntegrityError(f"formal source {field} differs from packet at ordinal {ordinal}")
            sampling_stratum = str(source_row.get("sampling_stratum") or "")
            if not sampling_stratum:
                raise ShardIntegrityError(f"formal source sampling_stratum is missing at ordinal {ordinal}")
        else:
            packet = source_row
            sampling_stratum = None
        review_id = str(packet.get("review_id") or "")
        if not review_id:
            raise ShardIntegrityError(f"source ordinal {ordinal} has no review_id")
        if review_id in seen_review_ids:
            raise ShardIntegrityError(f"duplicate review_id in source: {review_id}")
        seen_review_ids.add(review_id)
        packet_digest = canonical_sha256(packet)
        case_key = build_case_key(
            source_manifest_sha256=source_manifest_sha256,
            source_ordinal=ordinal,
            review_id=review_id,
            packet_sha256=packet_digest,
            protocol_sha256=protocol_sha256,
            prompt_sha256=prompt_sha256,
            schema_sha256=schema_sha256,
            execution_contract_sha256=execution_contract_sha256,
            model=model,
            reasoning_effort=reasoning_effort,
        )
        record = {
                "source_ordinal": ordinal,
                "review_id": review_id,
                "anonymous_stock_id": packet.get("anonymous_stock_id"),
                "packet_sha256": packet_digest,
                "case_key": case_key,
                "source_manifest_sha256": source_manifest_sha256,
                "protocol_sha256": _require_sha256("protocol_sha256", protocol_sha256),
                "prompt_sha256": _require_sha256("prompt_sha256", prompt_sha256),
                "schema_sha256": _require_sha256("schema_sha256", schema_sha256),
                "execution_contract_sha256": _require_sha256(
                    "execution_contract_sha256", execution_contract_sha256
                ),
                "model": str(model),
                "reasoning_effort": str(reasoning_effort),
                "packet": packet,
            }
        if sampling_stratum is not None:
            record["sampling_stratum"] = sampling_stratum
        records.append(record)
    return records


def shard_id_for(*, source_manifest_sha256: str, review_id: str, shard_count: int = DEFAULT_SHARD_COUNT) -> int:
    if shard_count <= 0:
        raise ShardIntegrityError("shard_count must be positive")
    source_digest = _require_sha256("source_manifest_sha256", source_manifest_sha256)
    digest = hashlib.sha256(f"{source_digest}|{review_id}".encode("utf-8")).hexdigest()
    return int(digest[:16], 16) % shard_count


def assign_shards(
    records: Sequence[dict[str, Any]],
    *,
    shard_count: int = DEFAULT_SHARD_COUNT,
) -> list[list[dict[str, Any]]]:
    shards: list[list[dict[str, Any]]] = [[] for _ in range(shard_count)]
    for record in records:
        shard_id = shard_id_for(
            source_manifest_sha256=str(record["source_manifest_sha256"]),
            review_id=str(record["review_id"]),
            shard_count=shard_count,
        )
        member = dict(record)
        member["shard_id"] = shard_id
        shards[shard_id].append(member)
    for shard in shards:
        shard.sort(key=lambda row: int(row["source_ordinal"]))
    validate_shards(records, shards, shard_count=shard_count)
    return shards


def validate_shards(
    records: Sequence[dict[str, Any]],
    shards: Sequence[Sequence[dict[str, Any]]],
    *,
    shard_count: int = DEFAULT_SHARD_COUNT,
) -> dict[str, int]:
    if len(shards) != shard_count:
        raise ShardIntegrityError(f"expected {shard_count} shards, found {len(shards)}")
    expected = {str(row["case_key"]): row for row in records}
    if len(expected) != len(records):
        raise ShardIntegrityError("duplicate case_key in source records")
    seen: dict[str, int] = {}
    for shard_index, shard in enumerate(shards):
        ordinals = [int(row["source_ordinal"]) for row in shard]
        if ordinals != sorted(ordinals):
            raise ShardIntegrityError(f"shard {shard_index} is not in source order")
        for row in shard:
            case_key = str(row.get("case_key") or "")
            if case_key in seen:
                raise ShardIntegrityError(
                    f"overlap: case {case_key} appears in shards {seen[case_key]} and {shard_index}"
                )
            if case_key not in expected:
                raise ShardIntegrityError(f"unexpected case in shard {shard_index}: {case_key}")
            expected_row = expected[case_key]
            for field in (
                "source_ordinal", "review_id", "packet_sha256", "source_manifest_sha256",
                "protocol_sha256", "prompt_sha256", "schema_sha256",
                "execution_contract_sha256", "model", "reasoning_effort",
            ):
                if row.get(field) != expected_row.get(field):
                    raise ShardIntegrityError(f"case {case_key} changed field {field}")
            calculated = shard_id_for(
                source_manifest_sha256=str(row["source_manifest_sha256"]),
                review_id=str(row["review_id"]),
                shard_count=shard_count,
            )
            if shard_index != calculated or int(row.get("shard_id", shard_index)) != calculated:
                raise ShardIntegrityError(f"case {case_key} is assigned to the wrong shard")
            seen[case_key] = shard_index
    missing = sorted(set(expected) - set(seen))
    if missing:
        raise ShardIntegrityError(f"missing {len(missing)} cases from shards; first={missing[0]}")
    return {
        "expected": len(expected),
        "covered": len(seen),
        "missing": 0,
        "overlap": 0,
        "unexpected": 0,
    }


def write_shards(
    records: Sequence[dict[str, Any]],
    output_dir: Path,
    *,
    shard_count: int = DEFAULT_SHARD_COUNT,
) -> dict[str, Any]:
    """Publish deterministic shard JSONL files and their immutable manifest."""
    shards = assign_shards(records, shard_count=shard_count)
    output_dir = Path(output_dir)
    shard_entries: list[dict[str, Any]] = []
    for shard_id, rows in enumerate(shards):
        path = output_dir / f"shard_{shard_id:02d}.jsonl"
        payload = _canonical_jsonl_bytes(rows)
        _publish_immutable(path, payload)
        shard_entries.append(
            {
                "shard_id": shard_id,
                "path": str(path.resolve()),
                "rows": len(rows),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "case_keys_sha256": canonical_sha256([row["case_key"] for row in rows]),
            }
        )
    coverage = validate_shards(records, shards, shard_count=shard_count)
    manifest = {
        "sharding_version": SHARDING_VERSION,
        "shard_count": shard_count,
        "expected_rows": len(records),
        "source_manifest_sha256": records[0]["source_manifest_sha256"] if records else None,
        "execution_contract_sha256": records[0]["execution_contract_sha256"] if records else None,
        "assignment_sha256": canonical_sha256(
            [
                {"case_key": row["case_key"], "shard_id": shard_id}
                for shard_id, shard in enumerate(shards)
                for row in shard
            ]
        ),
        "coverage": coverage,
        "shards": shard_entries,
    }
    manifest_path = output_dir / "assignment.manifest.json"
    _publish_immutable(manifest_path, canonical_json_bytes(manifest) + b"\n")
    return manifest


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--prompt", type=Path, required=True)
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--execution-contract", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--reasoning-effort", required=True)
    parser.add_argument("--shard-count", type=int, default=DEFAULT_SHARD_COUNT)
    args = parser.parse_args()

    packets = read_jsonl(args.source)
    source_manifest_sha256 = source_content_sha256(packets)
    records = build_case_records(
        packets,
        source_manifest_sha256=source_manifest_sha256,
        protocol_sha256=file_sha256(args.protocol),
        prompt_sha256=file_sha256(args.prompt),
        schema_sha256=file_sha256(args.schema),
        execution_contract_sha256=file_sha256(args.execution_contract),
        model=args.model,
        reasoning_effort=args.reasoning_effort,
    )
    result = write_shards(records, args.output_dir, shard_count=args.shard_count)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
