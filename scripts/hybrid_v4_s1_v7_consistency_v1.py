"""V7-evidence adapter for the frozen V4-S1 repeatability evaluator.

The atomic questions, reducer, acceptance thresholds, and evaluator logic are
unchanged.  This adapter only binds that logic to the immutable V7 evidence
track and its versioned producer components.
"""
from __future__ import annotations

from typing import Sequence

try:
    from . import hybrid_v4_s1_consistency_v1 as _base
except ImportError:  # pragma: no cover
    from scripts import hybrid_v4_s1_consistency_v1 as _base


REPORT_VERSION = "hybrid-v4-s1-v7-consistency-v1"
TRACK_VERSION = "hybrid-v4-s1-v7-evidence-research-track-v1"
FREEZE_VERSION = "hybrid-v4-s1-v7-evidence-execution-freeze-v1"
TRACK_STATUS = "FROZEN_OUTCOME_BLIND_V4_S1_V7_EVIDENCE_RESEARCH_ONLY"

V4_COMPONENT_PATHS = {
    "stage_protocol": "config/hybrid_v4_s1_mature_stage_v1.json",
    "reachability_preflight_builder":
        "scripts/hybrid_v4_s1_v7_reachability_preflight_v1.py",
    "policy": "scripts/hybrid_v4_s1_atomic_policy_v1.py",
    "track_builder": "scripts/hybrid_v4_s1_v7_track_v1.py",
    "evaluator": "scripts/hybrid_v4_s1_v7_consistency_v1.py",
    "execution_orchestrator":
        "scripts/hybrid_v4_s1_v7_execution_orchestrator_v1.py",
}


def _bind() -> None:
    _base.REPORT_VERSION = REPORT_VERSION
    _base.TRACK_VERSION = TRACK_VERSION
    _base.FREEZE_VERSION = FREEZE_VERSION
    _base.TRACK_STATUS = TRACK_STATUS
    _base.V4_COMPONENT_PATHS = dict(V4_COMPONENT_PATHS)
    _base.V4_DYNAMIC_COMPONENTS = {"reachability_preflight"}


def verify_frozen_track(**kwargs):
    _bind()
    return _base.verify_frozen_track(**kwargs)


def evaluate(**kwargs):
    _bind()
    return _base.evaluate(**kwargs)


def render_markdown(*args, **kwargs):
    _bind()
    return _base.render_markdown(*args, **kwargs)


def main(argv: Sequence[str] | None = None) -> int:
    _bind()
    return _base.main(argv)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
