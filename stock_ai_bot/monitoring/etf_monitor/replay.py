from __future__ import annotations

import csv
import json
from datetime import date
from pathlib import Path
from typing import Any

from .core import RESEARCH_SLIPPAGE_BPS, STRATEGY_CORE_ID
from .errors import EtfMonitorDataError


FINAL_ROOT = (
    Path(__file__).resolve().parents[3]
    / "docs"
    / "research"
    / "dual_ma"
    / "0050"
    / "final-v1.7"
)
EXPECTED_COUNTS = {
    "next-session-open-daily-equity.csv": 5093,
    "next-session-open-transactions.csv": 181,
    "next-session-open-corporate-actions.csv": 63,
    "next-session-open-trade-details.csv": 91,
}


def _rows(path: Path) -> list[dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))
    except OSError as exc:
        raise EtfMonitorDataError(f"accepted replay artifact unavailable: {path.name}") from exc


def verify_final_v17_artifacts(root: Path | None = None) -> dict[str, Any]:
    """Read-only replay gate for the exact accepted final-v1.7 artifacts."""

    base = (root or FINAL_ROOT).resolve()
    try:
        parity = json.loads((base / "core-parity.json").read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EtfMonitorDataError("accepted core parity artifact is unreadable") from exc
    if parity.get("status") != "PASS_EXACT":
        raise EtfMonitorDataError("accepted final-v1.7 core parity is not PASS_EXACT")

    checked: dict[str, int] = {}
    loaded: dict[str, list[dict[str, str]]] = {}
    for name, expected in EXPECTED_COUNTS.items():
        rows = _rows(base / name)
        if len(rows) != expected:
            raise EtfMonitorDataError(f"{name} row count changed: {len(rows)} != {expected}")
        if any(row.get("strategy_id") != STRATEGY_CORE_ID for row in rows):
            raise EtfMonitorDataError(f"{name} contains a different strategy id")
        if any(row.get("execution_timing") != "NEXT_SESSION_OPEN" for row in rows):
            raise EtfMonitorDataError(f"{name} contains a different execution timing")
        if any(int(row.get("slippage_bps") or -1) != RESEARCH_SLIPPAGE_BPS for row in rows):
            raise EtfMonitorDataError(f"{name} contains a different research slippage")
        checked[name] = len(rows)
        loaded[name] = rows

    transactions = loaded["next-session-open-transactions.csv"]
    prior_fill: date | None = None
    for row in transactions:
        signal_date = date.fromisoformat(row["signal_date"])
        fill_date = date.fromisoformat(row["date"])
        if row.get("fill_timing") != "NEXT_SESSION_OPEN" or signal_date >= fill_date:
            raise EtfMonitorDataError("accepted next-session transaction timing changed")
        if row.get("role") not in {"MOTHER", "EXIT"} or row.get("status") != "FILLED":
            raise EtfMonitorDataError("accepted transaction role/status changed")
        if prior_fill is not None and fill_date < prior_fill:
            raise EtfMonitorDataError("accepted transactions are not chronological")
        prior_fill = fill_date

    return {
        "status": "PASS_EXACT",
        "strategy_id": STRATEGY_CORE_ID,
        "execution_timing": "NEXT_SESSION_OPEN",
        "slippage_bps": RESEARCH_SLIPPAGE_BPS,
        "row_counts": checked,
    }
