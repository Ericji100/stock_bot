from __future__ import annotations

import os
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
WORKDATA_ROOT = Path(
    os.environ.get("STOCK_AI_BOT_WORKDATA_ROOT", REPOSITORY_ROOT / ".workdata")
).expanduser().resolve()
DUAL_MA_DEV_ROOT = WORKDATA_ROOT / "dev" / "dual-ma"
DUAL_MA_REPORT_ROOT = WORKDATA_ROOT / "reports" / "dual-ma"
ETF_REPORT_ROOT = DUAL_MA_REPORT_ROOT / "etf"


def dev_run(name: str) -> Path:
    return DUAL_MA_DEV_ROOT / name


def report_path(*parts: str) -> Path:
    return DUAL_MA_REPORT_ROOT.joinpath(*parts)
