"""Canonical V2 entry point for calibration-case extraction.

The implementation was originally developed in
``v2_core_calibration_cases_v1.py`` before the first generated artifact was
invalidated.  That module now emits only ``v2-core-calibration-cases-v2``.
This entry point makes the effective artifact version explicit while retaining
the invalidated V1 outputs and failure analysis for audit.
"""

from __future__ import annotations

try:
    from scripts.v2_core_calibration_cases_v1 import *  # noqa: F403
    from scripts.v2_core_calibration_cases_v1 import main
except ModuleNotFoundError:  # Direct ``python scripts\\...`` execution.
    from v2_core_calibration_cases_v1 import *  # type: ignore  # noqa: F403
    from v2_core_calibration_cases_v1 import main


if __name__ == "__main__":
    raise SystemExit(main())
