"""Build a disjoint assigned-stock range for the frozen V7 formal plan.

This is a throughput-only helper.  It publishes the exact per-stock fragments
used by ``build_formal_shard`` but never publishes a shard manifest.  The
ordinary three shard builders remain authoritative: they validate and resume
these fragments before publishing their complete shard manifests.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd

from . import hybrid_v3_atomic_packets_v4 as _v4
from . import hybrid_v3_atomic_packets_v7 as _v7


VERSION = "hybrid-v3-atomic-packets-v7-range-worker-v1"
STATUS = "FINAL_THROUGHPUT_ONLY_NO_SEMANTIC_CHANGE"
PINNED_V7_SHA256 = "09d61c36efdf6f19052f18a594f74a2278cc92ca217b3e4b3c5af2f51e8f6034"


def select_assigned_range(
    inventory: Sequence[Mapping[str, Any]],
    *,
    shard_id: int,
    assigned_start: int,
    assigned_end: int,
) -> list[Mapping[str, Any]]:
    if shard_id not in range(_v4.FORMAL_BUILD_SHARD_COUNT):
        raise ValueError("shard_id must be 0, 1, or 2")
    assigned = [
        row
        for row in inventory
        if int(row["stock_source_ordinal"]) % _v4.FORMAL_BUILD_SHARD_COUNT
        == shard_id
    ]
    if not (0 <= assigned_start < assigned_end <= len(assigned)):
        raise ValueError("assigned range is outside the shard inventory")
    return assigned[assigned_start:assigned_end]


def _build_or_resume_stock(
    *,
    stock: Mapping[str, Any],
    shard_id: int,
    plan_sha256: str,
    work_dir: Path,
    schema: Mapping[str, Any],
    salt: bytes,
) -> tuple[int, bool]:
    fragment_path, fragment_manifest_path = _v4._formal_fragment_paths(
        work_dir,
        int(stock["stock_source_ordinal"]),
        str(stock["anonymous_stock_id"]),
    )
    if fragment_path.exists() != fragment_manifest_path.exists():
        raise _v4.FormalBuildIntegrityError(
            "partial stock fragment exists; refusing a potentially racing write"
        )
    if fragment_path.exists():
        rows, _ = _v4._validate_fragment(
            fragment_path=fragment_path,
            manifest_path=fragment_manifest_path,
            expected_stock=dict(stock),
            plan_sha256=plan_sha256,
        )
        return len(rows), True

    if _v4.file_sha256(Path(stock["price_path"])) != stock["price_sha256"]:
        raise _v4.FormalBuildIntegrityError("assigned frozen price source hash mismatch")
    if (
        _v4.file_sha256(Path(stock["review_packet_path"]))
        != stock["review_packet_sha256"]
    ):
        raise _v4.FormalBuildIntegrityError(
            "assigned frozen review packet hash mismatch"
        )
    packets, summary = _v4.build_stock_packets(
        price_frame=pd.read_csv(stock["price_path"]),
        review_packet=_v4._read_json(Path(stock["review_packet_path"])),
        anonymous_stock_id=str(stock["anonymous_stock_id"]),
        identity_salt=salt,
        private_code=str(stock["private_code"]),
        monitor_on=str(stock["monitor_on"]),
        as_of=str(stock["as_of"]),
        atomic_schema_metadata=dict(schema),
    )
    if int(summary["stock_days_scanned"]) != int(stock["monitored_sessions"]):
        raise _v4.FormalBuildIntegrityError(
            "builder stock-day coverage differs from frozen manifest"
        )
    rows = [
        {
            "stock_source_ordinal": int(stock["stock_source_ordinal"]),
            "stock_review_ordinal": review_ordinal,
            "review_id": packet["review_id"],
            "packet_sha256": _v4.canonical_sha256(packet),
            "packet": packet,
        }
        for review_ordinal, packet in enumerate(packets)
    ]
    payload = _v4._canonical_jsonl_bytes(rows)
    fragment_manifest = {
        "builder_version": _v4.BUILDER_VERSION,
        "builder_status": _v4.BUILDER_STATUS,
        "status": "COMPLETE_ANONYMOUS_STOCK_FRAGMENT",
        "plan_sha256": plan_sha256,
        "build_shard_id": shard_id,
        "stock_source_ordinal": int(stock["stock_source_ordinal"]),
        "anonymous_stock_id": str(stock["anonymous_stock_id"]),
        "price_sha256": str(stock["price_sha256"]),
        "review_packet_sha256": str(stock["review_packet_sha256"]),
        "monitored_sessions": int(stock["monitored_sessions"]),
        **_v4._sampling_contract_pin(),
        "review_points": len(rows),
        "review_ids_sha256": _v4.canonical_sha256(
            [row["review_id"] for row in rows]
        ),
        "packet_hashes_sha256": _v4.canonical_sha256(
            [row["packet_sha256"] for row in rows]
        ),
        "fragment_sha256": hashlib.sha256(payload).hexdigest(),
    }
    _v4._publish_immutable(fragment_path, payload)
    _v4._publish_immutable(
        fragment_manifest_path,
        _v4.canonical_json_bytes(fragment_manifest) + b"\n",
    )
    checked, _ = _v4._validate_fragment(
        fragment_path=fragment_path,
        manifest_path=fragment_manifest_path,
        expected_stock=dict(stock),
        plan_sha256=plan_sha256,
    )
    return len(checked), False


def run(args: argparse.Namespace) -> dict[str, Any]:
    if _v7._file_sha256(Path(_v7.__file__).resolve()) != PINNED_V7_SHA256:
        raise RuntimeError("V7 packet builder bytes changed")
    _v7.activate()
    _v4._assert_identity_map_isolated(
        args.identity_map, args.formal_work_dir, args.formal_output_dir
    )
    inventory, metadata = _v4.load_formal_inventory(
        input_manifest_path=args.input_manifest,
        packet_manifest_path=args.packet_manifest,
        identity_map_path=args.identity_map,
        expected_stock_count=_v4.FORMAL_STOCK_COUNT,
        expected_stock_days=_v4.FORMAL_MONITORED_STOCK_DAYS,
        verify_all_source_files=False,
    )
    _, plan_sha256 = _v4._read_and_validate_formal_plan(
        args.formal_work_dir,
        metadata,
        args.atomic_schema,
        args.freeze_source_manifest,
    )
    selected = select_assigned_range(
        inventory,
        shard_id=args.shard_id,
        assigned_start=args.assigned_start,
        assigned_end=args.assigned_end,
    )
    schema = _v4._read_json(Path(args.atomic_schema))
    salt = bytes.fromhex(str(_v4._read_json(Path(args.identity_map))["salt_hex"]))
    resumed = review_points = 0
    for stock in selected:
        count, was_resumed = _build_or_resume_stock(
            stock=stock,
            shard_id=args.shard_id,
            plan_sha256=plan_sha256,
            work_dir=args.formal_work_dir,
            schema=schema,
            salt=salt,
        )
        review_points += count
        resumed += int(was_resumed)
    return {
        "worker_version": VERSION,
        "status": STATUS,
        "builder_version": _v4.BUILDER_VERSION,
        "builder_status": _v4.BUILDER_STATUS,
        "plan_sha256": plan_sha256,
        "shard_id": args.shard_id,
        "assigned_start": args.assigned_start,
        "assigned_end": args.assigned_end,
        "stocks": len(selected),
        "resumed_stocks": resumed,
        "new_stocks": len(selected) - resumed,
        "review_points": review_points,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-manifest", type=Path, default=_v4.DEFAULT_INPUT_MANIFEST)
    parser.add_argument("--packet-manifest", type=Path, default=_v4.DEFAULT_PACKET_MANIFEST)
    parser.add_argument("--identity-map", type=Path, default=_v4.DEFAULT_IDENTITY_MAP)
    parser.add_argument("--atomic-schema", type=Path, default=_v4.DEFAULT_ATOMIC_SCHEMA)
    parser.add_argument(
        "--freeze-source-manifest",
        type=Path,
        default=_v4.DEFAULT_FREEZE_SOURCE_MANIFEST,
    )
    parser.add_argument("--formal-work-dir", type=Path, required=True)
    parser.add_argument("--formal-output-dir", type=Path, required=True)
    parser.add_argument("--shard-id", type=int, required=True)
    parser.add_argument("--assigned-start", type=int, required=True)
    parser.add_argument("--assigned-end", type=int, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
