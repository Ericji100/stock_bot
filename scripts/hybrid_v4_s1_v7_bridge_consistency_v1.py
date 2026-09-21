"""Consistency adapter for the frozen V4-S1 V7 bridge track."""
from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from . import hybrid_v4_s1_consistency_v1 as _v1
    from . import hybrid_v4_s1_consistency_v2 as _v2
    from .hybrid_v3_sharding_v2 import canonical_sha256, file_sha256
    from .hybrid_v4_s1_v7_bridge_track_v1 import (
        BOUNDARY,
        FOCUS_ORDER,
        NEGATIVE,
        POSITIVE,
        QUOTAS,
        EXPECTED_HASHES,
        review_directory_manifest_sha256,
    )
except ImportError:  # pragma: no cover
    from scripts import hybrid_v4_s1_consistency_v1 as _v1
    from scripts import hybrid_v4_s1_consistency_v2 as _v2
    from scripts.hybrid_v3_sharding_v2 import canonical_sha256, file_sha256
    from scripts.hybrid_v4_s1_v7_bridge_track_v1 import (
        BOUNDARY,
        FOCUS_ORDER,
        NEGATIVE,
        POSITIVE,
        QUOTAS,
        EXPECTED_HASHES,
        review_directory_manifest_sha256,
    )


REPORT_VERSION = "hybrid-v4-s1-v7-bridge-consistency-v3"
TRACK_VERSION = "hybrid-v4-s1-v7-bridge-research-track-v3"
FREEZE_VERSION = "hybrid-v4-s1-v7-bridge-execution-freeze-v3"
TRACK_STATUS = "FROZEN_OUTCOME_BLIND_V4_S1_V7_BRIDGE_V3_RESEARCH_ONLY"

V4_COMPONENT_PATHS = {
    "stage_protocol": "config/hybrid_v4_s1_mature_stage_v1.json",
    "reachability_preflight_builder": "scripts/hybrid_v4_s1_v7_reachability_preflight_v1.py",
    "policy": "scripts/hybrid_v4_s1_atomic_policy_v1.py",
    "track_builder": "scripts/hybrid_v4_s1_v7_bridge_track_v1.py",
    "evaluator": "scripts/hybrid_v4_s1_v7_bridge_consistency_v1.py",
    "execution_orchestrator": "scripts/hybrid_v4_s1_v7_bridge_execution_orchestrator_v1.py",
}


def _bind() -> None:
    _v1.REPORT_VERSION = REPORT_VERSION
    _v1.TRACK_VERSION = TRACK_VERSION
    _v1.FREEZE_VERSION = FREEZE_VERSION
    _v1.TRACK_STATUS = TRACK_STATUS
    _v1.V4_COMPONENT_PATHS = dict(V4_COMPONENT_PATHS)
    _v1.V4_DYNAMIC_COMPONENTS = {"reachability_preflight", "bridge_sampling_provenance"}


def _validate_bridge_selection(track_manifest: Path) -> None:
    track = json.loads(Path(track_manifest).read_text(encoding="utf-8-sig"))
    selection_path = Path(str(track["selection_plan_path"]))
    selection = json.loads(selection_path.read_text(encoding="utf-8-sig"))
    if selection.get("selection_version") != "hybrid-v4-s1-v7-bridge-selection-v3":
        raise _v1.V4S1ConsistencyError("bridge selection version changed")
    if set((selection.get("focus_counts") or {}).keys()) != set(FOCUS_ORDER):
        raise _v1.V4S1ConsistencyError("bridge focus membership changed")
    if selection.get("focus_counts") != QUOTAS:
        raise _v1.V4S1ConsistencyError("bridge quotas changed")
    rows = selection.get("rows") or []
    if len(rows) != 36 or any(row.get("sampling_focus") not in QUOTAS for row in rows):
        raise _v1.V4S1ConsistencyError("bridge selection rows changed")

    freeze = json.loads(Path(str(track["execution_freeze_path"])).read_text(encoding="utf-8-sig"))
    components = {row["name"]: row for row in freeze.get("v4_s1_components") or []}
    provenance_row = components.get("bridge_sampling_provenance")
    if not isinstance(provenance_row, Mapping):
        raise _v1.V4S1ConsistencyError("bridge sampling provenance is missing")
    provenance_path = Path(str(provenance_row["relative_path"]))
    if not provenance_path.is_absolute():
        provenance_path = Path(__file__).resolve().parents[1] / provenance_path
    if not provenance_path.is_file() or file_sha256(provenance_path) != provenance_row.get("sha256"):
        raise _v1.V4S1ConsistencyError("bridge sampling provenance changed")
    provenance = json.loads(provenance_path.read_text(encoding="utf-8-sig"))
    supplied = provenance.pop("provenance_sha256", None)
    if supplied != canonical_sha256(provenance):
        raise _v1.V4S1ConsistencyError("bridge provenance self-hash changed")
    if (
        provenance.get("old_labels_visible_to_ai") is not False
        or provenance.get("future_or_performance_visible") is not False
        or provenance.get("quotas") != QUOTAS
        or review_directory_manifest_sha256() != EXPECTED_HASHES["old_review_directory_manifest"]
    ):
        raise _v1.V4S1ConsistencyError("bridge provenance contract changed")


def verify_frozen_track(**kwargs):
    _bind()
    _validate_bridge_selection(Path(kwargs["track_manifest"]))
    return _v1.verify_frozen_track(**kwargs)


def evaluate(**kwargs):
    _bind()
    _validate_bridge_selection(Path(kwargs["track_manifest"]))
    return _v2.evaluate(**kwargs)


def render_markdown(*args, **kwargs):
    _bind()
    return _v2.render_markdown(*args, **kwargs)


def main(argv: Sequence[str] | None = None) -> int:
    _bind()
    return _v2.main(argv)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
