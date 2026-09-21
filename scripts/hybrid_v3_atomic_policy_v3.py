"""V3 atomic policy contract repair with unchanged trading reduction.

The frozen V2 validator omitted ``REVERSAL_PROBE`` from the allowed
``taiji_generation`` values even though the frozen V3 packet builder and the
V3 route itself use that exact value for ``BEAR_REVERSAL_LEFT_RIGHT``.  This
wrapper removes only that false-positive validation error.  All atomic schema,
gate construction, route eligibility, action signatures, and course invariants
continue to use the byte-pinned V2 implementation.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

try:
    from . import hybrid_v3_atomic_policy_v2 as _v2
except ImportError:  # direct script import
    from scripts import hybrid_v3_atomic_policy_v2 as _v2


POLICY_VERSION = "hybrid-v3-atomic-policy-v3"
POLICY_STATUS = "FINAL"
BASE_POLICY_VERSION = "hybrid-v3-atomic-policy-v2"
BASE_POLICY_SHA256 = "b7504b8a1414dc3baab17e047f1827e96c3fa2fdc62b3ce2e6845f18a5cb40b9"
CONTRACT_REPAIR = "ALLOW_REVERSAL_PROBE_ONLY_FOR_BEAR_REVERSAL_LEFT_RIGHT"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def assert_base_policy_frozen() -> None:
    path = Path(_v2.__file__).resolve()
    if _v2.POLICY_VERSION != BASE_POLICY_VERSION:
        raise RuntimeError("V2 base policy version changed")
    if _file_sha256(path) != BASE_POLICY_SHA256:
        raise RuntimeError("V2 base policy bytes changed")


def _allowed_reversal_probe_errors(packet: dict[str, Any]) -> set[str]:
    objective = packet.get("objective_facts") or {}
    rows = (objective.get("scenario_hypotheses") or {}).get(
        "BEAR_REVERSAL_LEFT_RIGHT"
    )
    if not isinstance(rows, list):
        return set()
    return {
        "packet: BEAR_REVERSAL_LEFT_RIGHT "
        f"{hypothesis.get('hypothesis_id')} taiji_generation is invalid"
        for hypothesis in rows
        if isinstance(hypothesis, dict)
        and hypothesis.get("hypothesis_id")
        and hypothesis.get("taiji_generation") == "REVERSAL_PROBE"
    }


def validate_atomic(packet: dict[str, Any], semantic: dict[str, Any]) -> list[str]:
    """Use V2 validation and remove only the pinned reversal-probe mismatch."""

    assert_base_policy_frozen()
    allowed = _allowed_reversal_probe_errors(packet)
    return [error for error in _v2.validate_atomic(packet, semantic) if error not in allowed]


# Trading semantics are deliberately identical to V2.
reduce_atomic_v3 = _v2.reduce_atomic_v3
validate_course_invariants = _v2.validate_course_invariants
build_gate_matrix = _v2.build_gate_matrix
derive_primary_scenario = _v2.derive_primary_scenario
derive_quadrants = _v2.derive_quadrants
derive_scale_relationship = _v2.derive_scale_relationship
derive_stage = _v2.derive_stage
derive_left_right_phase = _v2.derive_left_right_phase


assert_base_policy_frozen()
