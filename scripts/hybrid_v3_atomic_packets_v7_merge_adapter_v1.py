"""Merge V7 candidate fragments without promoting them to a FINAL source.

The frozen V4 merger deliberately only publishes FINAL or DRAFT builders.  V7
uses a more explicit candidate status while its evidence contract is being
validated.  This adapter pins the exact V7 bytes and maps that one status to a
non-locked candidate manifest state; packet bytes and fragment validation stay
unchanged.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from . import hybrid_v3_atomic_packets_v4 as _v4
from . import hybrid_v3_atomic_packets_v7 as _v7


VERSION = "hybrid-v3-atomic-packets-v7-merge-adapter-v1"
STATUS = "FINAL_TECHNICAL_ADAPTER"
PINNED_V7_SHA256 = "09d61c36efdf6f19052f18a594f74a2278cc92ca217b3e4b3c5af2f51e8f6034"
CANDIDATE_MANIFEST_STATUS = "BUILT_OUTCOME_BLIND_CANDIDATE_NOT_FORMAL_ACCEPTANCE"
_ORIGINAL_REVIEW_MANIFEST_STATE = _v4._review_manifest_state


def assert_v7_frozen() -> None:
    if _v7._file_sha256(Path(_v7.__file__).resolve()) != PINNED_V7_SHA256:
        raise RuntimeError("V7 packet builder bytes changed")


def review_manifest_state(builder_status: str) -> tuple[str, bool]:
    if str(builder_status).upper() == _v7.BUILDER_STATUS:
        return CANDIDATE_MANIFEST_STATUS, False
    return _ORIGINAL_REVIEW_MANIFEST_STATE(builder_status)


def activate() -> None:
    assert_v7_frozen()
    _v7.activate()
    _v4._review_manifest_state = review_manifest_state


def merge_from_args(args: argparse.Namespace) -> dict[str, Any]:
    activate()
    return _v4.merge_formal_build_shards(
        input_manifest_path=args.input_manifest,
        packet_manifest_path=args.packet_manifest,
        identity_map_path=args.identity_map,
        atomic_schema_path=args.atomic_schema,
        freeze_source_manifest_path=args.freeze_source_manifest,
        work_dir=args.formal_work_dir,
        formal_output_dir=args.formal_output_dir,
    )


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
    args = parser.parse_args()
    print(json.dumps(merge_from_args(args), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
