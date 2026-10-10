"""Official TWSE/TPEx trading-calendar authority evidence (v1).
# reader hardening probe

Contract: docs/strategies/dual_ma/official-trading-calendar-authority-v1.md

Primary authority: TWSE FMTQIK monthly trading statistics (raw bytes frozen).
Market cross-check: official TPEx dailyQuotes receipts (raw + receipt hashes).
^TWII is a secondary diagnostic only and can never add or remove an open day.
"""

from __future__ import annotations

import csv
import hashlib
import json
from calendar import monthrange
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from stock_ai_bot.data_sources.causal_price_contract import (
    DataQualityError,
    SchemaVersionError,
    canonical_sha256,
)


OFFICIAL_CALENDAR_SCHEMA = "dual-ma-official-trading-calendar-v1"
OFFICIAL_CALENDAR_PRODUCER_VERSION = "dual-ma-official-calendar-authority-v1-1"
TWSE_FMTQIK_ENDPOINT = "https://www.twse.com.tw/rwd/zh/afterTrading/FMTQIK"
TWSE_FMTQIK_FIELDS = (
    "日期",
    "成交股數",
    "成交金額",
    "成交筆數",
    "發行量加權股價指數",
    "漲跌點數",
)
TWSE_FMTQIK_ROW_POLICY = "FMTQIK_MONTH_ROWS_ROC_DATE"
TPEX_RECEIPT_SCHEMA = "dual-ma-tpex-daily-quote-receipt-v2"
TPEX_RECEIPT_STATUS = "PASS_RAW_RECEIPT"
TPEX_DAILY_QUOTES_ENDPOINT_MARKER = "afterTrading/dailyQuotes"


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: str | Path) -> str:
    return _sha256_bytes(Path(path).read_bytes())


def _roc_date(value: str) -> date:
    parts = str(value).strip().split("/")
    if len(parts) != 3:
        raise DataQualityError("TWSE calendar row date is invalid")
    try:
        return date(int(parts[0]) + 1911, int(parts[1]), int(parts[2]))
    except ValueError as exc:
        raise DataQualityError("TWSE calendar row date is invalid") from exc


def parse_monthly_report(raw_body: bytes, request_date: date) -> tuple[date, ...]:
    """Strict FMTQIK month parser: identity, fields and ordering all fail closed."""

    try:
        payload = json.loads(raw_body)
        fields = tuple(str(item) for item in payload["fields"])
        rows = payload["data"]
    except (ValueError, TypeError, KeyError) as exc:
        raise DataQualityError("TWSE monthly calendar report is unreadable") from exc
    if (
        payload.get("stat") != "OK"
        or str(payload.get("date")) != request_date.strftime("%Y%m%d")
        or fields != TWSE_FMTQIK_FIELDS
        or not isinstance(rows, list)
        or not rows
    ):
        raise DataQualityError("TWSE monthly calendar report identity differs")
    if any(not isinstance(row, list) or len(row) != len(fields) for row in rows):
        raise DataQualityError("TWSE monthly calendar row schema differs")
    dates = tuple(_roc_date(row[0]) for row in rows if isinstance(row, list) and row)
    if (
        len(dates) != len(rows)
        or tuple(sorted(set(dates))) != dates
        or any(
            item.year != request_date.year or item.month != request_date.month
            for item in dates
        )
    ):
        raise DataQualityError("TWSE monthly trading dates are incomplete or unordered")
    return dates


def request_months(start: date, end: date) -> tuple[date, ...]:
    values: list[date] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        last = date(year, month, monthrange(year, month)[1])
        values.append(min(last, end) if (year, month) == (end.year, end.month) else last)
        month += 1
        if month == 13:
            year, month = year + 1, 1
    return tuple(values)


def _immutable_bytes(path: Path, payload: bytes) -> Path:
    if path.exists():
        if path.read_bytes() != payload:
            raise DataQualityError(f"refusing to overwrite immutable artifact: {path}")
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)
    return path


def freeze_month_raw(raw_dir: str | Path, raw_body: bytes) -> Path:
    """Freeze one FMTQIK month response at its content-addressed path."""

    digest = _sha256_bytes(raw_body)
    target = Path(raw_dir) / f"twse-fmtqik#{digest[:16]}.json"
    return _immutable_bytes(target, raw_body)


def freeze_secondary_calendar(raw_dir: str | Path, source_body: bytes) -> Path:
    """Keep the diagnostic index bytes stable after the shared cache changes."""

    digest = _sha256_bytes(source_body)
    target = Path(raw_dir) / f"twii-secondary#{digest[:16]}.csv"
    return _immutable_bytes(target, source_body)


def load_frozen_month(path: str | Path, *, request_date: date) -> tuple[date, ...]:
    """Re-parse a frozen month body so a manifest claim is never trusted alone."""

    body = Path(path).read_bytes()
    return parse_monthly_report(body, request_date)


@dataclass(frozen=True)
class TpexReceiptRef:
    receipt_relative_path: str
    receipt_sha256: str
    raw_relative_path: str
    raw_sha256: str
    trade_date: date
    response_date: str
    trading_row_count: int
    has_trading_rows: bool


def verify_tpex_receipt(path: str | Path, *, workdata_root: str | Path) -> TpexReceiptRef:
    """Verify one official TPEx dailyQuotes receipt and its raw response."""

    root = Path(workdata_root).resolve()
    receipt = Path(path).resolve()
    if not receipt.is_relative_to(root) or not receipt.is_file():
        raise DataQualityError("TPEx receipt is absent or outside workdata")
    payload = json.loads(receipt.read_text(encoding="utf-8-sig"))
    if payload.get("schema_version") != TPEX_RECEIPT_SCHEMA:
        raise SchemaVersionError("unsupported TPEx receipt schema")
    if payload.get("status") != TPEX_RECEIPT_STATUS:
        raise DataQualityError("TPEx receipt is not a passing raw receipt")
    if TPEX_DAILY_QUOTES_ENDPOINT_MARKER not in str(payload.get("endpoint") or ""):
        raise DataQualityError("TPEx receipt endpoint is not official dailyQuotes")
    if str(payload.get("http_status")) != "200":
        raise DataQualityError("TPEx receipt HTTP status is not 200")
    request_identity = payload.get("request")
    if not isinstance(request_identity, dict):
        raise DataQualityError("TPEx receipt lacks its official request identity")
    requested = str(request_identity.get("date") or "").replace("/", "").replace("-", "")
    if len(requested) != 8 or not requested.isdigit():
        raise DataQualityError("TPEx receipt request date is invalid")
    try:
        trade_date = date.fromisoformat(str(payload["trade_date"]))
    except (KeyError, ValueError) as exc:
        raise DataQualityError("TPEx receipt trade date is invalid") from exc
    raw_field = str(payload.get("raw_path") or "")
    if not raw_field:
        raise DataQualityError("TPEx receipt lacks a raw response path")
    raw_candidate = Path(raw_field)
    if raw_candidate.is_absolute():
        candidates = [raw_candidate]
    else:
        # Official runs keep raw responses beside the receipts directory; accept
        # any of the documented anchors, then verify the bytes by hash.
        candidates = [
            receipt.parent / raw_candidate,
            receipt.parent.parent / raw_candidate,
            root / raw_candidate,
        ]
    resolved = [item.resolve() for item in candidates]
    raw_path = next(
        (
            item
            for item in resolved
            if item.is_relative_to(root) and item.is_file()
        ),
        None,
    )
    if raw_path is None:
        raise DataQualityError("TPEx raw response is absent or outside workdata")
    raw_body = raw_path.read_bytes()
    raw_sha = _sha256_bytes(raw_body)
    if raw_sha != payload.get("raw_sha256"):
        raise DataQualityError("TPEx raw response hash mismatch")
    try:
        raw_payload = json.loads(raw_body)
    except ValueError as exc:
        raise DataQualityError("TPEx raw response is unreadable") from exc
    raw_date = str(raw_payload.get("date") or "")
    expected = {trade_date.strftime("%Y%m%d"), trade_date.strftime("%Y/%m/%d")}
    if raw_date not in expected:
        raise DataQualityError("TPEx raw response date disagrees with its receipt")
    if str(raw_payload.get("stat") or "").lower() != "ok":
        raise DataQualityError("TPEx raw response stat is not ok")
    tables = raw_payload.get("tables")
    if not isinstance(tables, list) or not tables:
        raise DataQualityError("TPEx raw response has no tables")
    rows: list[Any] = []
    for table in tables:
        if not isinstance(table, dict) or not isinstance(table.get("data"), list):
            raise DataQualityError("TPEx raw response table schema is invalid")
        for row in table["data"]:
            if not isinstance(row, list) or not row:
                raise DataQualityError("TPEx raw response row schema is invalid")
            rows.append(row)
    # An official response with no rows is verified evidence that the market did
    # not trade that day; it must never create an open date.
    return TpexReceiptRef(
        receipt_relative_path=receipt.relative_to(root).as_posix(),
        receipt_sha256=sha256_file(receipt),
        raw_relative_path=raw_path.relative_to(root).as_posix(),
        raw_sha256=raw_sha,
        trade_date=trade_date,
        response_date=raw_date,
        trading_row_count=len(rows),
        has_trading_rows=bool(rows),
    )


def read_secondary_calendar(path: str | Path, *, start: date, end: date) -> tuple[date, ...]:
    """Read a secondary (^TWII) calendar purely for diagnostics."""

    body = Path(path).read_text(encoding="utf-8-sig")
    days = set()
    for row in csv.DictReader(body.splitlines()):
        value = (row.get("date") or "").strip()
        if not value:
            continue
        try:
            day = date.fromisoformat(value[:10])
        except ValueError:
            continue
        if start <= day <= end:
            days.add(day)
    return tuple(sorted(days))


def build_calendar_core(
    *,
    twse_months: Sequence[Mapping[str, Any]],
    tpex_receipts: Sequence[TpexReceiptRef],
    coverage_start: date,
    coverage_end: date,
    secondary_twii: Mapping[str, Any],
    require_tpex_receipt_coverage: bool = True,
    git_commit: str = "UNKNOWN",
    producer_version: str = OFFICIAL_CALENDAR_PRODUCER_VERSION,
) -> dict[str, Any]:
    """Assemble the deterministic content-addressed calendar payload body."""

    if coverage_end < coverage_start:
        raise DataQualityError("official calendar coverage window is inverted")
    if not twse_months:
        raise DataQualityError("official calendar requires frozen TWSE months")
    expected_request_dates = {
        item.strftime("%Y-%m"): item for item in request_months(coverage_start, coverage_end)
    }
    twse_dates: list[date] = []
    month_rows: list[dict[str, Any]] = []
    seen_months: dict[str, date] = {}
    for month in twse_months:
        request_date = date.fromisoformat(str(month["request_date"]))
        month_key = request_date.strftime("%Y-%m")
        if month_key in seen_months:
            raise DataQualityError(f"duplicate TWSE month in calendar evidence: {month_key}")
        if expected_request_dates.get(month_key) != request_date:
            raise DataQualityError(
                f"TWSE month is not part of the requested coverage: {month_key}"
            )
        if str(month.get("endpoint") or "") != TWSE_FMTQIK_ENDPOINT:
            raise DataQualityError("TWSE month does not come from the official FMTQIK endpoint")
        dates = tuple(date.fromisoformat(str(item)) for item in month["trading_dates"])
        if not dates:
            raise DataQualityError("official TWSE month has no trading dates")
        if any(item.strftime("%Y-%m") != month_key for item in dates):
            raise DataQualityError("official TWSE month contains dates from another month")
        seen_months[month_key] = request_date
        twse_dates.extend(item for item in dates if coverage_start <= item <= coverage_end)
        month_rows.append(
            {
                "request_month": month_key,
                "request_date": request_date.isoformat(),
                "endpoint": TWSE_FMTQIK_ENDPOINT,
                "source_url": str(month["source_url"]),
                "raw_path": str(month["raw_path"]),
                "raw_sha256": str(month["raw_sha256"]),
                "trading_dates": [item.isoformat() for item in dates],
            }
        )
    missing_months = sorted(set(expected_request_dates) - set(seen_months))
    if missing_months:
        raise DataQualityError(
            "TWSE months missing for the requested coverage: " + ", ".join(missing_months)
        )
    twse_open = tuple(sorted(set(twse_dates)))
    if not twse_open:
        raise DataQualityError("official TWSE calendar is empty")
    extra = [item for item in twse_open if item < coverage_start or item > coverage_end]
    if extra:
        raise DataQualityError("official TWSE dates fall outside the declared coverage")
    grouped: dict[date, list[TpexReceiptRef]] = {}
    for ref in tpex_receipts:
        grouped.setdefault(ref.trade_date, []).append(ref)
    canonical_refs: list[TpexReceiptRef] = []
    alias_paths: dict[date, list[str]] = {}
    for day, refs in sorted(grouped.items()):
        ordered = sorted(refs, key=lambda item: item.receipt_relative_path)
        first = ordered[0]
        for other in ordered[1:]:
            if (
                other.has_trading_rows != first.has_trading_rows
                or other.raw_sha256 != first.raw_sha256
                or other.response_date != first.response_date
                or other.trading_row_count != first.trading_row_count
            ):
                raise DataQualityError(
                    "conflicting TPEx evidence for "
                    f"{day.isoformat()}: {first.receipt_relative_path} vs "
                    f"{other.receipt_relative_path}"
                )
        canonical_refs.append(first)
        alias_paths[day] = [item.receipt_relative_path for item in ordered[1:]]
    open_refs = [ref for ref in canonical_refs if ref.has_trading_rows]
    open_dates = {ref.trade_date for ref in open_refs}
    closed_refs = [
        ref
        for ref in canonical_refs
        if not ref.has_trading_rows and ref.trade_date not in open_dates
    ]
    tpex_open = sorted(open_dates)
    exceptions = []
    cross_checks = []
    for ref in sorted(open_refs, key=lambda item: (item.trade_date, item.receipt_relative_path)):
        entry = {
            "trade_date": ref.trade_date.isoformat(),
            "response_date": ref.response_date,
            "receipt_path": ref.receipt_relative_path,
            "receipt_sha256": ref.receipt_sha256,
            "alias_receipt_paths": alias_paths.get(ref.trade_date, []),
            "raw_path": ref.raw_relative_path,
            "raw_sha256": ref.raw_sha256,
            "trading_row_count": ref.trading_row_count,
        }
        if ref.trade_date in set(twse_open):
            cross_checks.append(entry)
        else:
            if ref.trade_date < coverage_start or ref.trade_date > coverage_end:
                raise DataQualityError(
                    "TPEx exception falls outside the declared coverage window"
                )
            exceptions.append(
                {
                    **entry,
                    "reason": "OFFICIAL_TPEX_DAILY_QUOTES_OPEN_NOT_IN_TWSE_FMTQIK",
                }
            )
    union = tuple(sorted(set(twse_open) | set(tpex_open)))
    receipt_dates = {ref.trade_date for ref in canonical_refs}
    missing_receipts = sorted(set(twse_open) - receipt_dates)
    conflict_dates = sorted({ref.trade_date for ref in closed_refs} & set(twse_open))
    coverage_gate = {
        "require_tpex_receipt_for_every_twse_open": require_tpex_receipt_coverage,
        "twse_open_count": len(twse_open),
        "verified_receipt_date_count": len(receipt_dates),
        "missing_tpex_receipt_dates": [item.isoformat() for item in missing_receipts],
        "conflicting_tpex_no_trade_dates": [item.isoformat() for item in conflict_dates],
        "status": "PASS" if not missing_receipts and not conflict_dates else "HOLD",
    }
    if require_tpex_receipt_coverage and coverage_gate["status"] != "PASS":
        raise DataQualityError(
            "TPEx coverage proof failed: "
            + json.dumps(coverage_gate, ensure_ascii=False, sort_keys=True)
        )
    missing_in_secondary = sorted(set(union) - set(secondary_twii["dates"]))
    extra_in_secondary = sorted(set(secondary_twii["dates"]) - set(union))
    return {
        "schema_version": OFFICIAL_CALENDAR_SCHEMA,
        "status": "PASS",
        "coverage_start": coverage_start.isoformat(),
        "coverage_end": coverage_end.isoformat(),
        "row_policy": TWSE_FMTQIK_ROW_POLICY,
        "twse_months": sorted(month_rows, key=lambda item: item["request_month"]),
        "open_dates_by_market": {
            "TWSE": [item.isoformat() for item in twse_open],
            "TPEX": [item.isoformat() for item in tpex_open],
        },
        "open_dates_union": [item.isoformat() for item in union],
        "open_dates_sha256_by_market": {
            "TWSE": canonical_sha256([item.isoformat() for item in twse_open]),
            "TPEX": canonical_sha256([item.isoformat() for item in tpex_open]),
        },
        "open_dates_union_sha256": canonical_sha256([item.isoformat() for item in union]),
        "coverage_gate": coverage_gate,
        "market_specific_exceptions": exceptions,
        "market_cross_checks": cross_checks,
        "tpex_non_trading_evidence": [
            {
                "trade_date": ref.trade_date.isoformat(),
                "response_date": ref.response_date,
                "receipt_path": ref.receipt_relative_path,
                "receipt_sha256": ref.receipt_sha256,
                "alias_receipt_paths": alias_paths.get(ref.trade_date, []),
                "raw_path": ref.raw_relative_path,
                "raw_sha256": ref.raw_sha256,
                "trading_row_count": ref.trading_row_count,
                "reason": "OFFICIAL_TPEX_RESPONSE_WITHOUT_TRADED_ROWS",
            }
            for ref in sorted(
                closed_refs, key=lambda item: (item.trade_date, item.receipt_relative_path)
            )
        ],
        "secondary_diagnostics": {
            "twii": {
                "path": str(secondary_twii["path"]),
                "file_sha256": str(secondary_twii["file_sha256"]),
                "open_date_count": len(secondary_twii["dates"]),
                "missing_official_dates": [item.isoformat() for item in missing_in_secondary],
                "extra_dates_not_official": [item.isoformat() for item in extra_in_secondary],
            }
        },
        "producer_version": producer_version,
        "git_commit": git_commit,
        "performance_read_or_computed": False,
    }


def write_official_calendar(
    core: Mapping[str, Any],
    *,
    output_dir: str | Path,
    workdata_root: str | Path,
    require_formal_coverage: bool = True,
) -> Path:
    """Write the calendar evidence pack at its content-addressed path."""

    root = Path(workdata_root).resolve()
    destination = Path(output_dir).resolve()
    if not destination.is_relative_to(root):
        raise DataQualityError("official calendar must remain inside isolated workdata")
    digest = canonical_sha256(dict(core))
    payload = {
        **dict(core),
        "calendar_id": f"dual-ma-official-calendar#{digest[:16]}",
        "content_sha256": digest,
    }
    encoded = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")
    path = destination / f"dual-ma-official-trading-calendar#{digest[:16]}.json"
    _immutable_bytes(path, encoded)
    loaded = load_official_calendar(
        path, workdata_root=root, require_formal_coverage=require_formal_coverage
    )
    if loaded.content_sha256 != digest:
        raise DataQualityError("official calendar readback mismatch")
    return path


@dataclass(frozen=True)
class FrozenOfficialCalendar:
    content_sha256: str
    file_sha256: str
    relative_path: str
    coverage_start: date
    coverage_end: date
    open_dates_twse: tuple[date, ...]
    open_dates_tpex: tuple[date, ...]
    open_dates_union: tuple[date, ...]
    union_sha256: str
    twse_sha256: str
    tpex_sha256: str
    formal_coverage: bool
    payload: Mapping[str, Any]

    def covers(self, start: date, end: date) -> bool:
        return self.coverage_start <= start and end <= self.coverage_end


def _resolve_secondary_path(value: str, *, root: Path) -> Path:
    """Resolve the ^TWII diagnostic path with an explicit boundary.

    Relative paths must stay inside workdata; absolute paths are read as-is but
    must be a real file and are still hash-verified by the caller.
    """

    if not value:
        raise DataQualityError("secondary ^TWII path is empty")
    candidate = Path(value)
    if candidate.is_absolute():
        resolved = candidate.resolve()
        if not resolved.is_file():
            raise DataQualityError("secondary ^TWII file is absent")
        return resolved
    resolved = (root / candidate).resolve()
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise DataQualityError("secondary ^TWII file is absent or outside workdata")
    return resolved


def _verify_receipt_entry(
    entry: Mapping[str, Any],
    *,
    root: Path,
    expect_trading_rows: bool,
    label: str,
) -> TpexReceiptRef:
    """Re-verify one canonical TPEx entry together with every alias of it."""

    receipt_path = (root / str(entry["receipt_path"])).resolve()
    if not receipt_path.is_relative_to(root) or not receipt_path.is_file():
        raise DataQualityError(f"{label} receipt is absent or outside workdata")
    if sha256_file(receipt_path) != entry.get("receipt_sha256"):
        raise DataQualityError(f"{label} receipt hash mismatch")
    verified = verify_tpex_receipt(receipt_path, workdata_root=root)
    if verified.raw_relative_path != entry.get("raw_path"):
        raise DataQualityError(f"{label} raw path disagrees with receipt")
    if verified.raw_sha256 != entry.get("raw_sha256"):
        raise DataQualityError(f"{label} raw hash disagrees with receipt")
    if verified.has_trading_rows is not expect_trading_rows:
        raise DataQualityError(f"{label} traded-row state disagrees with the manifest")
    if str(entry.get("trade_date") or "") != verified.trade_date.isoformat():
        raise DataQualityError(f"{label} trade date disagrees with the manifest")
    if str(entry.get("response_date") or "") != verified.response_date:
        raise DataQualityError(f"{label} response date disagrees with the manifest")
    try:
        recorded_rows = int(entry.get("trading_row_count"))
    except (TypeError, ValueError) as exc:
        raise DataQualityError(f"{label} traded-row count is invalid") from exc
    if recorded_rows != verified.trading_row_count:
        raise DataQualityError(f"{label} traded-row count disagrees with the manifest")
    for alias in entry.get("alias_receipt_paths") or []:
        alias_path = (root / str(alias)).resolve()
        if not alias_path.is_relative_to(root) or not alias_path.is_file():
            raise DataQualityError(f"{label} alias receipt is absent or outside workdata")
        alias_ref = verify_tpex_receipt(alias_path, workdata_root=root)
        if (
            alias_ref.trade_date != verified.trade_date
            or alias_ref.raw_sha256 != verified.raw_sha256
            or alias_ref.response_date != verified.response_date
            or alias_ref.trading_row_count != verified.trading_row_count
            or alias_ref.has_trading_rows is not verified.has_trading_rows
        ):
            raise DataQualityError(f"{label} alias receipt is not equivalent evidence")
    return verified


def load_official_calendar(
    path: str | Path,
    *,
    workdata_root: str | Path,
    require_formal_coverage: bool = True,
) -> FrozenOfficialCalendar:
    """Re-verify raw months, receipts, date sets and manifest hash from bytes.

    Only a manifest whose own gate required a TPEx receipt for every TWSE open
    date and passed may be returned as a formal official calendar.
    """

    root = Path(workdata_root).resolve()
    manifest = Path(path).resolve()
    if not manifest.is_relative_to(root) or not manifest.is_file():
        raise DataQualityError("official calendar is absent or outside workdata")
    payload = json.loads(manifest.read_text(encoding="utf-8-sig"))
    if payload.get("schema_version") != OFFICIAL_CALENDAR_SCHEMA:
        raise SchemaVersionError("unsupported official calendar schema")
    if payload.get("status") != "PASS":
        raise DataQualityError("official calendar is not PASS")
    core = {
        key: value
        for key, value in payload.items()
        if key not in {"content_sha256", "calendar_id"}
    }
    digest = canonical_sha256(core)
    if payload.get("content_sha256") != digest:
        raise DataQualityError("official calendar content hash mismatch")
    if payload.get("calendar_id") != f"dual-ma-official-calendar#{digest[:16]}":
        raise DataQualityError("official calendar id does not match its content hash")
    try:
        coverage_start = date.fromisoformat(str(payload["coverage_start"]))
        coverage_end = date.fromisoformat(str(payload["coverage_end"]))
    except (KeyError, ValueError) as exc:
        raise DataQualityError("official calendar coverage is invalid") from exc
    if coverage_end < coverage_start:
        raise DataQualityError("official calendar coverage is inverted")
    months = payload.get("twse_months")
    if not isinstance(months, list) or not months:
        raise DataQualityError("official calendar has no TWSE months")
    expected_months = {
        item.strftime("%Y-%m"): item for item in request_months(coverage_start, coverage_end)
    }
    seen_months: dict[str, date] = {}
    twse_dates: list[date] = []
    for month in months:
        raw_path = (root / str(month["raw_path"])).resolve()
        if not raw_path.is_relative_to(root) or not raw_path.is_file():
            raise DataQualityError("official TWSE raw month is absent or outside workdata")
        if sha256_file(raw_path) != month.get("raw_sha256"):
            raise DataQualityError("official TWSE raw month hash mismatch")
        request_date = date.fromisoformat(str(month["request_date"]))
        month_key = request_date.strftime("%Y-%m")
        if month_key in seen_months:
            raise DataQualityError(f"duplicate TWSE month in calendar evidence: {month_key}")
        if expected_months.get(month_key) != request_date:
            raise DataQualityError(f"TWSE month is outside the requested coverage: {month_key}")
        if str(month.get("endpoint") or "") != TWSE_FMTQIK_ENDPOINT:
            raise DataQualityError("official TWSE month endpoint is not FMTQIK")
        if str(month.get("source_url") or "") != TWSE_FMTQIK_ENDPOINT:
            raise DataQualityError("official TWSE month source_url is not FMTQIK")
        if str(month.get("request_month") or "") != month_key:
            raise DataQualityError("official TWSE month identity disagrees with its request date")
        seen_months[month_key] = request_date
        parsed = parse_monthly_report(raw_path.read_bytes(), request_date)
        if [item.isoformat() for item in parsed] != month.get("trading_dates"):
            raise DataQualityError("official TWSE raw month dates disagree with manifest")
        twse_dates.extend(item for item in parsed if coverage_start <= item <= coverage_end)
    missing_months = sorted(set(expected_months) - set(seen_months))
    if missing_months:
        raise DataQualityError(
            "TWSE months missing for the requested coverage: " + ", ".join(missing_months)
        )
    twse_open = tuple(sorted(set(twse_dates)))
    recorded_twse = tuple(date.fromisoformat(str(item)) for item in payload["open_dates_by_market"]["TWSE"])
    if twse_open != recorded_twse:
        raise DataQualityError("official TWSE date set disagrees with frozen raw months")
    tpex_dates: list[date] = []
    all_receipt_dates: set[date] = set()
    non_trading_dates: set[date] = set()
    for entry in payload.get("market_cross_checks") or []:
        verified = _verify_receipt_entry(
            entry, root=root, expect_trading_rows=True, label="TPEx cross-check"
        )
        if verified.trade_date in all_receipt_dates:
            raise DataQualityError("duplicate canonical TPEx receipt date")
        if verified.trade_date not in set(twse_open):
            raise DataQualityError("TPEx cross-check date is not a TWSE open date")
        tpex_dates.append(verified.trade_date)
        all_receipt_dates.add(verified.trade_date)
    for entry in payload.get("market_specific_exceptions") or []:
        verified = _verify_receipt_entry(
            entry, root=root, expect_trading_rows=True, label="TPEx exception"
        )
        if verified.trade_date in all_receipt_dates:
            raise DataQualityError("duplicate canonical TPEx receipt date")
        if verified.trade_date in set(twse_open):
            raise DataQualityError("TPEx exception date is already a TWSE open date")
        if not str(entry.get("reason") or "").strip():
            raise DataQualityError("TPEx exception lacks a reason")
        tpex_dates.append(verified.trade_date)
        all_receipt_dates.add(verified.trade_date)
    for entry in payload.get("tpex_non_trading_evidence") or []:
        verified = _verify_receipt_entry(
            entry, root=root, expect_trading_rows=False, label="TPEx non-trading"
        )
        if verified.trade_date in all_receipt_dates:
            raise DataQualityError("duplicate canonical TPEx receipt date")
        all_receipt_dates.add(verified.trade_date)
        non_trading_dates.add(verified.trade_date)
    tpex_open = tuple(sorted(set(tpex_dates)))
    if tpex_open != tuple(
        date.fromisoformat(str(item)) for item in payload["open_dates_by_market"]["TPEX"]
    ):
        raise DataQualityError("official TPEx date set disagrees with frozen receipts")
    union = tuple(sorted(set(twse_open) | set(tpex_open)))
    if union != tuple(date.fromisoformat(str(item)) for item in payload["open_dates_union"]):
        raise DataQualityError("official union disagrees with market date sets")
    gate = payload.get("coverage_gate")
    if not isinstance(gate, dict):
        raise DataQualityError("official calendar lacks a TPEx coverage gate")
    missing_receipts = sorted(set(twse_open) - all_receipt_dates)
    conflict_dates = sorted(non_trading_dates & set(twse_open))
    if gate.get("missing_tpex_receipt_dates") != [item.isoformat() for item in missing_receipts]:
        raise DataQualityError("TPEx coverage gate missing-date list mismatch")
    if gate.get("conflicting_tpex_no_trade_dates") != [
        item.isoformat() for item in conflict_dates
    ]:
        raise DataQualityError("TPEx coverage gate conflict list mismatch")
    expected_status = "PASS" if not missing_receipts and not conflict_dates else "HOLD"
    if gate.get("status") != expected_status:
        raise DataQualityError("TPEx coverage gate status mismatch")
    if gate.get("twse_open_count") != len(twse_open):
        raise DataQualityError("TPEx coverage gate TWSE open count mismatch")
    if gate.get("verified_receipt_date_count") != len(all_receipt_dates):
        raise DataQualityError("TPEx coverage gate receipt date count mismatch")
    gate_required = gate.get("require_tpex_receipt_for_every_twse_open") is True
    formal_coverage = gate_required and expected_status == "PASS"
    if require_formal_coverage and not formal_coverage:
        raise DataQualityError(
            "official calendar is validation-only, not a formal coverage proof"
        )
    diagnostics = payload.get("secondary_diagnostics")
    if not isinstance(diagnostics, dict) or not isinstance(diagnostics.get("twii"), dict):
        raise DataQualityError("official calendar lacks secondary ^TWII diagnostics")
    twii = diagnostics["twii"]
    twii_path = _resolve_secondary_path(str(twii.get("path") or ""), root=root)
    if sha256_file(twii_path) != twii.get("file_sha256"):
        raise DataQualityError("secondary ^TWII file hash mismatch")
    twii_dates = read_secondary_calendar(twii_path, start=coverage_start, end=coverage_end)
    if twii.get("open_date_count") != len(twii_dates):
        raise DataQualityError("secondary ^TWII open date count mismatch")
    if twii.get("missing_official_dates") != [
        item.isoformat() for item in sorted(set(union) - set(twii_dates))
    ]:
        raise DataQualityError("secondary ^TWII missing-date list mismatch")
    if twii.get("extra_dates_not_official") != [
        item.isoformat() for item in sorted(set(twii_dates) - set(union))
    ]:
        raise DataQualityError("secondary ^TWII extra-date list mismatch")
    if payload.get("open_dates_union_sha256") != canonical_sha256(
        [item.isoformat() for item in union]
    ):
        raise DataQualityError("official union semantic hash mismatch")
    if payload["open_dates_sha256_by_market"]["TWSE"] != canonical_sha256(
        [item.isoformat() for item in twse_open]
    ):
        raise DataQualityError("official TWSE semantic hash mismatch")
    if payload["open_dates_sha256_by_market"]["TPEX"] != canonical_sha256(
        [item.isoformat() for item in tpex_open]
    ):
        raise DataQualityError("official TPEx semantic hash mismatch")
    return FrozenOfficialCalendar(
        content_sha256=digest,
        file_sha256=sha256_file(manifest),
        relative_path=manifest.relative_to(root).as_posix(),
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        open_dates_twse=twse_open,
        open_dates_tpex=tpex_open,
        open_dates_union=union,
        union_sha256=str(payload["open_dates_union_sha256"]),
        twse_sha256=str(payload["open_dates_sha256_by_market"]["TWSE"]),
        tpex_sha256=str(payload["open_dates_sha256_by_market"]["TPEX"]),
        formal_coverage=formal_coverage,
        payload=payload,
    )
