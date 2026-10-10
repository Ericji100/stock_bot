"""Immutable exchange-calendar evidence packs for causal availability v2.

The legacy v1 availability snapshots referenced the shared
`.cache/historical_scan/daily_prices/^TWII.csv` file directly.  That file is
rewritten wholesale by the routine price fetcher, so a frozen v1 snapshot can
later fail on a byte-level receipt even when the trading-day semantics inside
the snapshot window never changed.

A v2 snapshot instead carries its own frozen calendar evidence pack covering
exactly the window it claims.  Read-back depends only on that pack, so later
rewrites of the shared cache cannot invalidate frozen evidence.

Contract: docs/strategies/dual_ma/causal-availability-calendar-evidence-v2-contract.md
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from stock_ai_bot.data_sources.causal_price_contract import (
    DataQualityError,
    SchemaVersionError,
    canonical_sha256,
)


TAIPEI = ZoneInfo("Asia/Taipei")

CALENDAR_EVIDENCE_SCHEMA = "dual-ma-exchange-calendar-evidence-v1"
CALENDAR_EVIDENCE_PRODUCER_VERSION = "dual-ma-calendar-evidence-v2-1"
DEFAULT_CALENDAR_ID = "TSE_TWII_DAILY_CACHE_V1"
DEFAULT_MARKET_SCOPE = ("TWSE", "TPEX")
ROW_POLICY = "ROWS_WITH_COMPLETE_OHLC"
_OHLC_FIELDS = ("open", "high", "low", "close")


@dataclass(frozen=True)
class FrozenCalendarEvidence:
    """A verified, self-contained trading-calendar evidence pack."""

    content_sha256: str
    file_sha256: str
    evidence_id: str
    calendar_id: str
    market_scope: tuple[str, ...]
    window_start: date
    window_end: date
    open_dates: tuple[date, ...]
    open_dates_sha256: str
    source_path_at_freeze: str
    source_file_sha256_at_freeze: str
    source_window_rows_sha256: str
    frozen_at: str
    producer_version: str
    git_commit: str
    relative_path: str

    def contains(self, day: date) -> bool:
        return self.window_start <= day <= self.window_end and day in set(self.open_dates)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _immutable_json(path: Path, payload: Mapping[str, Any]) -> None:
    encoded = (
        json.dumps(
            payload, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False
        )
        + "\n"
    ).encode("utf-8")
    if path.exists():
        if path.read_bytes() != encoded:
            raise DataQualityError(
                f"refusing to overwrite immutable calendar evidence: {path}"
            )
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(encoded)
    temporary.replace(path)


def canonical_open_dates_sha256(days: Iterable[date]) -> str:
    """Hash the sequence semantics that replay actually depends on."""

    return canonical_sha256([day.isoformat() for day in days])


def read_source_window_rows(
    source_path: str | Path, *, window_start: date, window_end: date
) -> tuple[dict[str, str | None], ...]:
    """Read canonical rows for the window from a daily-price cache CSV."""

    path = Path(source_path)
    if not path.is_file():
        raise DataQualityError(f"calendar source file is absent: {path}")
    text = path.read_text(encoding="utf-8-sig")
    reader = csv.DictReader(text.splitlines())
    fieldnames = tuple(name.strip() for name in (reader.fieldnames or ()))
    if "date" not in fieldnames or not set(_OHLC_FIELDS).issubset(fieldnames):
        raise DataQualityError(
            f"calendar source is missing date/OHLC columns: {path}"
        )
    rows: list[dict[str, str | None]] = []
    seen: set[str] = set()
    for raw in reader:
        value = (raw.get("date") or "").strip()
        if not value:
            continue
        try:
            day = date.fromisoformat(value)
        except ValueError as exc:
            raise DataQualityError(f"calendar source row has invalid date: {value!r}") from exc
        if day < window_start or day > window_end:
            continue
        if value in seen:
            raise DataQualityError(f"calendar source repeats date {value}")
        seen.add(value)
        row: dict[str, str | None] = {"date": value}
        for name in (*_OHLC_FIELDS, "volume", "adj_close"):
            cell = raw.get(name)
            row[name] = None if cell is None else str(cell).strip() or None
        rows.append(row)
    if not rows:
        raise DataQualityError(
            f"calendar source has no rows in window {window_start}..{window_end}"
        )
    rows.sort(key=lambda item: str(item["date"]))
    return tuple(rows)


def _open_dates_from_rows(rows: Sequence[Mapping[str, Any]]) -> tuple[date, ...]:
    days: list[date] = []
    for row in rows:
        if any(row.get(name) in (None, "") for name in _OHLC_FIELDS):
            continue
        days.append(date.fromisoformat(str(row["date"])))
    return tuple(days)


def freeze_exchange_calendar_evidence(
    *,
    source_path: str | Path,
    window_start: date,
    window_end: date,
    output_dir: str | Path,
    workdata_root: str | Path,
    calendar_id: str = DEFAULT_CALENDAR_ID,
    market_scope: Sequence[str] = DEFAULT_MARKET_SCOPE,
    git_commit: str = "UNKNOWN",
    producer_version: str = CALENDAR_EVIDENCE_PRODUCER_VERSION,
    frozen_at: datetime | None = None,
) -> Path:
    """Freeze the window's trading days into a content-addressed evidence pack."""

    root = Path(workdata_root).resolve()
    if window_end < window_start:
        raise DataQualityError("calendar evidence window is inverted")
    if not str(calendar_id).strip():
        raise DataQualityError("calendar evidence calendar_id is empty")
    scope = tuple(str(item) for item in market_scope if str(item).strip())
    if not scope:
        raise DataQualityError("calendar evidence market_scope is empty")
    source = Path(source_path).resolve()
    if not source.is_file():
        raise DataQualityError(f"calendar source file is absent: {source}")
    rows = read_source_window_rows(source, window_start=window_start, window_end=window_end)
    open_dates = _open_dates_from_rows(rows)
    if not open_dates:
        raise DataQualityError("calendar evidence has no open dates in window")
    if tuple(sorted(set(open_dates))) != open_dates:
        raise DataQualityError("calendar evidence open dates are not strictly increasing")
    try:
        source_relative = source.relative_to(root).as_posix()
    except ValueError:
        source_relative = str(source)
    core: dict[str, Any] = {
        "schema_version": CALENDAR_EVIDENCE_SCHEMA,
        "status": "PASS",
        "calendar_id": calendar_id,
        "market_scope": list(scope),
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "row_policy": ROW_POLICY,
        "open_dates": [day.isoformat() for day in open_dates],
        "open_dates_sha256": canonical_open_dates_sha256(open_dates),
        "source_path_at_freeze": source_relative,
        "source_file_sha256_at_freeze": _sha256_file(source),
        "source_window_rows": [dict(row) for row in rows],
        "source_window_rows_sha256": canonical_sha256([dict(row) for row in rows]),
        "producer_version": producer_version,
        "git_commit": git_commit,
        "performance_read_or_computed": False,
    }
    if frozen_at is not None:
        # Only an explicitly supplied, already-frozen timestamp may enter the
        # content-addressed core; the default path stays deterministic.
        core["frozen_at"] = frozen_at.isoformat()
    digest = canonical_sha256(core)
    payload = {
        **core,
        "evidence_id": f"dual-ma-exchange-calendar-evidence#{digest[:16]}",
        "content_sha256": digest,
    }
    destination = Path(output_dir).resolve()
    if not destination.is_relative_to(root):
        raise DataQualityError("calendar evidence must remain inside isolated workdata")
    path = destination / f"dual-ma-exchange-calendar-evidence#{digest[:16]}.json"
    _immutable_json(path, payload)
    loaded = load_frozen_calendar_evidence(path, workdata_root=root)
    if loaded.content_sha256 != digest or loaded.open_dates != open_dates:
        raise DataQualityError("calendar evidence readback mismatch")
    return path


def load_frozen_calendar_evidence(
    path: str | Path, *, workdata_root: str | Path
) -> FrozenCalendarEvidence:
    """Verify a frozen calendar evidence pack from its own bytes."""

    root = Path(workdata_root).resolve()
    manifest = Path(path).resolve()
    if not manifest.is_file():
        raise DataQualityError(f"calendar evidence is absent: {manifest}")
    if not manifest.is_relative_to(root):
        raise DataQualityError("calendar evidence must live inside isolated workdata")
    payload = json.loads(manifest.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise DataQualityError("calendar evidence must be an object")
    if payload.get("schema_version") != CALENDAR_EVIDENCE_SCHEMA:
        raise SchemaVersionError(
            f"unsupported calendar evidence schema {payload.get('schema_version')!r}"
        )
    if payload.get("status") != "PASS":
        raise DataQualityError("calendar evidence is not PASS")
    core = {
        key: value
        for key, value in payload.items()
        if key not in {"content_sha256", "evidence_id"}
    }
    digest = canonical_sha256(core)
    if payload.get("content_sha256") != digest:
        raise DataQualityError("calendar evidence content hash mismatch")
    raw_dates = payload.get("open_dates")
    if not isinstance(raw_dates, list) or not raw_dates:
        raise DataQualityError("calendar evidence open_dates are empty")
    try:
        open_dates = tuple(date.fromisoformat(str(value)) for value in raw_dates)
        window_start = date.fromisoformat(str(payload["window_start"]))
        window_end = date.fromisoformat(str(payload["window_end"]))
    except (KeyError, ValueError, TypeError) as exc:
        raise DataQualityError("calendar evidence window or dates are invalid") from exc
    if window_end < window_start:
        raise DataQualityError("calendar evidence window is inverted")
    if tuple(sorted(set(open_dates))) != open_dates:
        raise DataQualityError("calendar evidence open dates are not strictly increasing")
    if any(day < window_start or day > window_end for day in open_dates):
        raise DataQualityError("calendar evidence open date falls outside its window")
    if payload.get("open_dates_sha256") != canonical_open_dates_sha256(open_dates):
        raise DataQualityError("calendar evidence open_dates hash mismatch")
    rows = payload.get("source_window_rows")
    if not isinstance(rows, list) or not rows:
        raise DataQualityError("calendar evidence source rows are empty")
    if payload.get("source_window_rows_sha256") != canonical_sha256(
        [dict(row) for row in rows]
    ):
        raise DataQualityError("calendar evidence source row hash mismatch")
    if payload.get("row_policy") != ROW_POLICY:
        raise DataQualityError("calendar evidence row policy is unsupported")
    if _open_dates_from_rows(rows) != open_dates:
        raise DataQualityError("calendar evidence open dates disagree with source rows")
    if payload.get("evidence_id") != f"dual-ma-exchange-calendar-evidence#{digest[:16]}":
        raise DataQualityError("calendar evidence id does not match its content hash")
    scope = payload.get("market_scope")
    if not isinstance(scope, list) or not scope:
        raise DataQualityError("calendar evidence market_scope is empty")
    calendar_id = str(payload.get("calendar_id") or "")
    if not calendar_id.strip():
        raise DataQualityError("calendar evidence calendar_id is empty")
    return FrozenCalendarEvidence(
        content_sha256=digest,
        file_sha256=_sha256_file(manifest),
        evidence_id=str(payload["evidence_id"]),
        calendar_id=calendar_id,
        market_scope=tuple(str(item) for item in scope),
        window_start=window_start,
        window_end=window_end,
        open_dates=open_dates,
        open_dates_sha256=str(payload["open_dates_sha256"]),
        source_path_at_freeze=str(payload.get("source_path_at_freeze") or ""),
        source_file_sha256_at_freeze=str(payload.get("source_file_sha256_at_freeze") or ""),
        source_window_rows_sha256=str(payload["source_window_rows_sha256"]),
        frozen_at=str(payload.get("frozen_at") or ""),
        producer_version=str(payload.get("producer_version") or ""),
        git_commit=str(payload.get("git_commit") or ""),
        relative_path=manifest.relative_to(root).as_posix(),
    )
