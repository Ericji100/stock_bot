"""50%-guarded execution adapter for the immutable V4-S1 V7 track.

The existing shard dispatcher and AI launcher remain unchanged.  This adapter
binds them to the V7 track verifier and the user's current 50% remaining-usage
stop threshold.
"""
from __future__ import annotations

from typing import Sequence

try:
    from . import hybrid_v4_s1_execution_orchestrator_v1 as _base
    from . import hybrid_v4_s1_v7_consistency_v1 as _consistency
except ImportError:  # pragma: no cover
    from scripts import hybrid_v4_s1_execution_orchestrator_v1 as _base
    from scripts import hybrid_v4_s1_v7_consistency_v1 as _consistency


ORCHESTRATOR_VERSION = "hybrid-v4-s1-v7-execution-orchestrator-v1"
RECEIPT_VERSION = "hybrid-v4-s1-v7-budget-receipt-v1"
EXPECTED_THRESHOLD = 50.0


def _bind() -> None:
    _base.consistency_v1 = _consistency
    _base.ORCHESTRATOR_VERSION = ORCHESTRATOR_VERSION
    _base.RECEIPT_VERSION = RECEIPT_VERSION
    _base.EXPECTED_THRESHOLD = EXPECTED_THRESHOLD


def validate_usage_attestation(*args, **kwargs):
    _bind()
    return _base.validate_usage_attestation(*args, **kwargs)


def execute_shard(**kwargs):
    _bind()
    return _base.execute_shard(**kwargs)


def main(argv: Sequence[str] | None = None) -> int:
    _bind()
    return _base.main(argv)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
