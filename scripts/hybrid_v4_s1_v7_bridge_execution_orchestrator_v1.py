"""70%-guarded execution adapter for the V4-S1 V7 bridge track."""
from __future__ import annotations

from pathlib import Path
import sys
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from . import hybrid_v4_s1_execution_orchestrator_v1 as _base
    from . import hybrid_v4_s1_v7_bridge_consistency_v1 as _consistency
except ImportError:  # pragma: no cover
    from scripts import hybrid_v4_s1_execution_orchestrator_v1 as _base
    from scripts import hybrid_v4_s1_v7_bridge_consistency_v1 as _consistency


ORCHESTRATOR_VERSION = "hybrid-v4-s1-v7-bridge-execution-orchestrator-v3"
RECEIPT_VERSION = "hybrid-v4-s1-v7-bridge-budget-receipt-v3"
EXPECTED_THRESHOLD = 70.0


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
