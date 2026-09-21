"""R6B bootstrap-only revision of the R6A legacy lifecycle runner."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sys
from typing import Any, Iterator


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import v2_core_legacy_lifecycle_runner_v6 as r6
from scripts import v2_core_legacy_lifecycle_runner_v6a as r6a


RUNNER_VERSION = "v2-core-legacy-lifecycle-runner-r6b-candidate"
MODEL = r6.MODEL
REASONING = r6.REASONING
RunnerError = r6.RunnerError
transport_schema = r6a.transport_schema
assert_no_ref_siblings = r6a.assert_no_ref_siblings


@contextmanager
def _patched_r6_transport() -> Iterator[None]:
    original_transport = r6._transport_schema
    original_version = r6.RUNNER_VERSION
    r6._transport_schema = transport_schema
    r6.RUNNER_VERSION = RUNNER_VERSION
    try:
        yield
    finally:
        r6._transport_schema = original_transport
        r6.RUNNER_VERSION = original_version


def run_b0a_case(**kwargs: Any) -> dict[str, Any]:
    with _patched_r6_transport():
        return r6.run_b0a_case(**kwargs)


def validate_existing_b0a(**kwargs: Any) -> dict[str, Any]:
    with _patched_r6_transport():
        return r6.validate_existing_b0a(**kwargs)


def run_b0b_case(**kwargs: Any) -> dict[str, Any]:
    with _patched_r6_transport():
        return r6.run_b0b_case(**kwargs)


def validate_existing_b0b(**kwargs: Any) -> dict[str, Any]:
    with _patched_r6_transport():
        return r6.validate_existing_b0b(**kwargs)


def main() -> int:
    with _patched_r6_transport():
        return r6.main()


if __name__ == "__main__":
    raise SystemExit(main())
