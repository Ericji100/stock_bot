from __future__ import annotations

import hashlib
import html
import json
import re
from html.parser import HTMLParser
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import requests

from .errors import EtfMonitorDataError
from .models import canonical_json, stable_sha256


TAIPEI = ZoneInfo("Asia/Taipei")
STOCK_DAY_URL = "https://www.twse.com.tw/exchangeReport/STOCK_DAY"
HOLIDAY_URL = "https://openapi.twse.com.tw/v1/holidaySchedule/holidaySchedule"
ACTION_URL = "https://openapi.twse.com.tw/v1/exchangeReport/TWT48U_ALL"
DIVIDEND_URL = "https://www.twse.com.tw/zh/ETFortune/dividendList"
PARSER_STOCK_DAY = "TWSE_STOCK_DAY_V1"
PARSER_HOLIDAY = "TWSE_HOLIDAY_SCHEDULE_V1"
PARSER_ACTION = "TWSE_TWT48U_ALL_V1"
PARSER_DIVIDEND = "TWSE_ETFORTUNE_DIVIDEND_LIST_V1"
EMERGENCY_CLOSURE_URL = "https://eshop.twse.com.tw/zh/"
PARSER_EMERGENCY_CLOSURE = "TWSE_EMERGENCY_CLOSURE_NOTICE_V1"
ALLOWED_PARSERS = {
    PARSER_STOCK_DAY: STOCK_DAY_URL,
    PARSER_HOLIDAY: HOLIDAY_URL,
    PARSER_ACTION: ACTION_URL,
    PARSER_DIVIDEND: DIVIDEND_URL,
    PARSER_EMERGENCY_CLOSURE: EMERGENCY_CLOSURE_URL,
}


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _roc_date(value: Any) -> date:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) not in {7, 8}:
        raise EtfMonitorDataError(f"TWSE ROC date is invalid: {value!r}")
    year_digits = len(digits) - 4
    return date(int(digits[:year_digits]) + 1911, int(digits[year_digits:year_digits + 2]), int(digits[-2:]))


def _number(value: Any, field: str, *, positive: bool = True) -> Decimal:
    text = str(value or "").replace(",", "").strip()
    try:
        result = Decimal(text)
    except Exception as exc:
        raise EtfMonitorDataError(f"official TWSE receipt has invalid {field}") from exc
    if result < 0 or (positive and result == 0):
        raise EtfMonitorDataError(f"official TWSE receipt has invalid {field}")
    return result


def _receipt_bytes(
    evidence: Any,
    *,
    receipt_root: Path,
    field: str,
    parser: str,
) -> tuple[bytes, dict[str, Any]]:
    if not isinstance(evidence, Mapping):
        raise EtfMonitorDataError(f"daily delta {field} evidence is missing")
    if str(evidence.get("parser") or "") != parser:
        raise EtfMonitorDataError(f"daily delta {field} parser is not approved")
    endpoint = str(evidence.get("source_endpoint") or "")
    request_url = str(evidence.get("request_url") or "")
    approved = ALLOWED_PARSERS[parser]
    requested = urlparse(request_url)
    approved_parts = urlparse(approved)
    if (
        endpoint != approved
        or requested.scheme != approved_parts.scheme
        or requested.netloc != approved_parts.netloc
        or requested.path != approved_parts.path
        or requested.params
        or requested.fragment
        or requested.username is not None
        or requested.password is not None
    ):
        raise EtfMonitorDataError(f"daily delta {field} endpoint is not the approved TWSE source")
    query = parse_qs(requested.query)
    if parser in {PARSER_HOLIDAY, PARSER_ACTION, PARSER_EMERGENCY_CLOSURE} and query:
        raise EtfMonitorDataError(f"daily delta {field} source must not have query overrides")
    if parser == PARSER_DIVIDEND and (
        query.get("stkNo") != ["0050"]
        or not re.fullmatch(r"\d{4}", query.get("startDate", [""])[0])
        or not re.fullmatch(r"\d{4}", query.get("endDate", [""])[0])
    ):
        raise EtfMonitorDataError("daily delta ETF dividend receipt request identity is invalid")
    try:
        fetched_at = datetime.fromisoformat(str(evidence.get("fetched_at") or ""))
    except ValueError as exc:
        raise EtfMonitorDataError(f"daily delta {field} fetched_at is invalid") from exc
    if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
        raise EtfMonitorDataError(f"daily delta {field} fetched_at must be timezone-aware")
    relative = str(evidence.get("receipt_relative_path") or "")
    expected_sha = str(evidence.get("receipt_sha256") or "").lower()
    candidate = (receipt_root / relative).resolve()
    try:
        candidate.relative_to(receipt_root.resolve())
    except ValueError as exc:
        raise EtfMonitorDataError(f"daily delta {field} receipt escapes its bundle") from exc
    if not re.fullmatch(r"receipts/[a-f0-9]{64}\.raw", relative.replace("\\", "/")):
        raise EtfMonitorDataError(f"daily delta {field} receipt path is not content-addressed")
    try:
        raw = candidate.read_bytes()
    except OSError as exc:
        raise EtfMonitorDataError(f"daily delta {field} raw receipt is missing") from exc
    actual_sha = _sha256(raw)
    if expected_sha != actual_sha or Path(relative).stem != actual_sha:
        raise EtfMonitorDataError(f"daily delta {field} raw receipt SHA-256 mismatch")
    return raw, dict(evidence)


def parse_stock_day(raw: bytes, *, request_url: str) -> dict[date, dict[str, str]]:
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EtfMonitorDataError("TWSE STOCK_DAY receipt is not valid JSON") from exc
    query = parse_qs(urlparse(request_url).query)
    if query.get("stockNo") != ["0050"] or query.get("response") != ["json"]:
        raise EtfMonitorDataError("TWSE STOCK_DAY receipt request identity is not 0050/json")
    if not isinstance(payload, Mapping) or payload.get("stat") != "OK":
        raise EtfMonitorDataError("TWSE STOCK_DAY receipt status is not OK")
    output: dict[date, dict[str, str]] = {}
    for source in payload.get("data") or []:
        if not isinstance(source, list) or len(source) < 7:
            raise EtfMonitorDataError("TWSE STOCK_DAY row shape changed")
        day = _roc_date(source[0])
        output[day] = {
            "open": str(_number(source[3], "open")),
            "high": str(_number(source[4], "high")),
            "low": str(_number(source[5], "low")),
            "close": str(_number(source[6], "close")),
            "volume": str(_number(source[1], "volume", positive=False)),
        }
    return output


def parse_holidays(raw: bytes) -> set[date]:
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EtfMonitorDataError("TWSE holiday receipt is not valid JSON") from exc
    if not isinstance(payload, list):
        raise EtfMonitorDataError("TWSE holiday receipt root changed")
    # The official schedule also lists the first/last *trading* day of a
    # holiday season. Those are information rows, not market closures.
    return {
        _roc_date(row.get("Date")) for row in payload
        if isinstance(row, Mapping)
        and not re.search(r"(?:開始交易日|最後交易日)", str(row.get("Name") or ""))
    }


class _VisibleNoticeText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self.hidden:
            self.hidden -= 1

    def handle_data(self, text):
        if not self.hidden:
            self.parts.append(text)


def parse_emergency_closures(raw: bytes) -> set[date]:
    """Only accept an explicit TWSE full-day closure, not a missing quote.

    The official notice can split its ROC date across nested span elements.
    Negations, other markets, partial-day notices and script text do not match.
    """
    try:
        document = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise EtfMonitorDataError("official emergency notice is not UTF-8") from exc
    if not re.search(r"<html\b", document, re.I) or not re.search(r"</html\s*>", document, re.I):
        raise EtfMonitorDataError("official emergency notice page shape changed")
    parser = _VisibleNoticeText()
    parser.feed(document)
    text = re.sub(r"\s+", "", "".join(parser.parts))
    if "臺灣證券交易所" not in text:
        raise EtfMonitorDataError("official emergency notice page identity changed")
    days: set[date] = set()
    for match in re.finditer(
        r"臺灣證券交易所(?:於)?(\d{3,4})年(\d{1,2})月(\d{1,2})日(?:全日)?休市[。.!！]",
        text,
    ):
        year, month, day = map(int, match.groups())
        try:
            days.add(date(year + 1911 if year < 1911 else year, month, day))
        except ValueError as exc:
            raise EtfMonitorDataError("official emergency closure date is invalid") from exc
    return days


def cached_emergency_notice(root: Path | None, day: date) -> bytes | None:
    """Reuse content-addressed official notices captured by the calendar gate.

    Reparse and rehash the original document; never infer a closure from a
    filename, operator boolean or absent market data. This also survives the
    announcement rotating off the current official home page after a holiday.
    """
    if root is None or not root.is_dir():
        return None
    for path in sorted(root.glob("*.raw")):
        raw = path.read_bytes()
        if not raw.lstrip().removeprefix(b"\xef\xbb\xbf").lstrip().startswith(b"<"):
            continue
        if path.stem != _sha256(raw):
            raise EtfMonitorDataError("cached official emergency notice SHA-256 mismatch")
        if day in parse_emergency_closures(raw):
            return raw
    return None


def parse_action_preview(raw: bytes) -> dict[date, list[dict[str, str]]]:
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EtfMonitorDataError("TWSE TWT48U receipt is not valid JSON") from exc
    if not isinstance(payload, list):
        raise EtfMonitorDataError("TWSE TWT48U receipt root changed")
    output: dict[date, list[dict[str, str]]] = {}
    for row in payload:
        if not isinstance(row, Mapping) or str(row.get("Code") or "") != "0050":
            continue
        day = _roc_date(row.get("Date"))
        output.setdefault(day, []).append({str(key): str(value or "").strip() for key, value in row.items()})
    return output


def _clean_html(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", value))).strip()


def parse_dividend_list(raw: bytes) -> dict[date, list[dict[str, str]]]:
    text = raw.decode("utf-8", "replace")
    output: dict[date, list[dict[str, str]]] = {}
    for block in re.findall(r"<tr[^>]*>(.*?)</tr>", text, re.S | re.I):
        cells = [_clean_html(cell) for cell in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", block, re.S | re.I)]
        if not cells or cells[0] != "0050":
            continue
        if len(cells) < 6:
            raise EtfMonitorDataError("TWSE dividend-list row shape changed")
        ex_date = _roc_date(cells[2])
        output.setdefault(ex_date, []).append({
            "code": cells[0],
            "ex_date": ex_date.isoformat(),
            "entitlement_date": _roc_date(cells[3]).isoformat(),
            "payment_date": _roc_date(cells[4]).isoformat(),
            "cash_dividend": str(_number(cells[5], "cash dividend", positive=False)),
        })
    return output


def verify_delta_receipts(
    payload: Mapping[str, Any],
    *,
    expected_date: date,
    receipt_root: Path,
) -> dict[str, Any]:
    calendar_raw, calendar_evidence = _receipt_bytes(
        payload.get("calendar_evidence"), receipt_root=receipt_root,
        field="calendar", parser=PARSER_HOLIDAY,
    )
    bar_raw, bar_evidence = _receipt_bytes(
        payload.get("bar_evidence"), receipt_root=receipt_root,
        field="bar", parser=PARSER_STOCK_DAY,
    )
    action_evidence = payload.get("corporate_action_evidence")
    if not isinstance(action_evidence, list) or len(action_evidence) != 2:
        raise EtfMonitorDataError("daily delta requires both official corporate-action receipts")
    action_raw, _action_meta = _receipt_bytes(
        action_evidence[0], receipt_root=receipt_root,
        field="corporate-action preview", parser=PARSER_ACTION,
    )
    dividend_raw, dividend_meta = _receipt_bytes(
        action_evidence[1], receipt_root=receipt_root,
        field="ETF dividend list", parser=PARSER_DIVIDEND,
    )
    bars = parse_stock_day(bar_raw, request_url=bar_evidence["request_url"])
    holidays = parse_holidays(calendar_raw)
    stock_query = parse_qs(urlparse(str(bar_evidence["request_url"])).query)
    if stock_query.get("date") != [expected_date.strftime("%Y%m01")]:
        raise EtfMonitorDataError("TWSE STOCK_DAY receipt month does not cover the delta date")
    dividend_query = parse_qs(urlparse(str(dividend_meta["request_url"])).query)
    if not (
        int(dividend_query["startDate"][0])
        <= expected_date.year
        <= int(dividend_query["endDate"][0])
    ):
        raise EtfMonitorDataError("TWSE ETF dividend receipt years do not cover the delta date")
    if not any(day.year == expected_date.year for day in holidays):
        raise EtfMonitorDataError("TWSE holiday receipt does not cover the delta year")
    official_bar = bars.get(expected_date)
    closure_evidence = payload.get("emergency_closure_evidence")
    emergency_closed = False
    if closure_evidence is not None:
        closure_raw, _closure_meta = _receipt_bytes(
            closure_evidence, receipt_root=receipt_root,
            field="emergency closure", parser=PARSER_EMERGENCY_CLOSURE,
        )
        emergency_closed = expected_date in parse_emergency_closures(closure_raw)
        if not emergency_closed:
            raise EtfMonitorDataError("official emergency notice does not close the delta date")
    status = str(payload.get("market_status") or "")
    if official_bar is not None:
        if expected_date in holidays or emergency_closed:
            raise EtfMonitorDataError("official TWSE bar conflicts with the holiday schedule")
        if status != "OPEN" or payload.get("bar") != official_bar:
            raise EtfMonitorDataError("daily delta OHLCV/status differs from raw TWSE STOCK_DAY")
    else:
        if status != "CLOSED" or payload.get("bar") is not None:
            raise EtfMonitorDataError("daily delta claims OPEN without a raw TWSE bar")
        if expected_date.weekday() < 5 and expected_date not in holidays and not emergency_closed:
            raise EtfMonitorDataError(
                "weekday has no TWSE bar and is not an official holiday; halt/missing data needs manual evidence"
            )
    previews = parse_action_preview(action_raw).get(expected_date, [])
    dividends = parse_dividend_list(dividend_raw).get(expected_date, [])
    actions = payload.get("corporate_actions")
    if not isinstance(actions, list):
        raise EtfMonitorDataError("daily delta corporate_actions must be a list")
    if previews or dividends:
        if not previews or not dividends:
            raise EtfMonitorDataError("official corporate-action sources disagree for 0050")
        if len(previews) != len(dividends) or len(actions) != len(previews):
            raise EtfMonitorDataError("daily delta corporate-action count differs from official receipts")
        for preview, dividend, action in zip(previews, dividends, actions):
            non_cash = any(preview.get(field) for field in (
                "StockDividendRatio", "SubscriptionRatio", "SubscriptionPricePerShare",
                "SharesOffered", "SharesEmpOwner", "SharesholderOwner", "StockHoldingRatio",
            ))
            if non_cash:
                raise EtfMonitorDataError("non-cash corporate action requires a separately reviewed evidence package")
            if not isinstance(action, Mapping):
                raise EtfMonitorDataError("daily delta corporate action is malformed")
            if (
                str(action.get("action_type")) != "CASH_DIVIDEND"
                or str(action.get("ex_date")) != expected_date.isoformat()
                or Decimal(str(action.get("cash_dividend"))) != Decimal(dividend["cash_dividend"])
                or Decimal(str(action.get("cash_dividend"))) != _number(preview.get("CashDividend"), "preview cash dividend", positive=False)
                or str(action.get("payment_date")) != dividend["payment_date"]
                or str(action.get("raw_response_sha256")) != str(dividend_meta["receipt_sha256"])
            ):
                raise EtfMonitorDataError("daily delta corporate action differs from raw TWSE receipts")
    elif actions:
        raise EtfMonitorDataError("daily delta invents a corporate action absent from TWSE receipts")
    return {
        "calendar_receipt_sha256": calendar_evidence["receipt_sha256"],
        "bar_receipt_sha256": bar_evidence["receipt_sha256"],
        "corporate_action_receipt_sha256": [row["receipt_sha256"] for row in action_evidence],
    }


def _fetch_receipt(
    session: requests.Session,
    *,
    url: str,
    parser: str,
    params: Mapping[str, str] | None,
    fetched_at: datetime,
) -> tuple[bytes, dict[str, Any]]:
    response = session.get(url, params=params, timeout=30)
    response.raise_for_status()
    raw = response.content
    digest = _sha256(raw)
    return raw, {
        "source_endpoint": url,
        "request_url": response.url,
        "fetched_at": fetched_at.isoformat(),
        "receipt_sha256": digest,
        "receipt_relative_path": f"receipts/{digest}.raw",
        "media_type": str(response.headers.get("content-type") or "application/octet-stream"),
        "parser": parser,
    }


def build_daily_delta_bundle(
    *,
    start: date,
    through: date,
    output: Path,
    frozen_snapshot_id: str,
    frozen_manifest_sha256: str,
    session_factory: Callable[[], requests.Session] = requests.Session,
    fetched_at: datetime | None = None,
    closure_receipt_root: Path | None = None,
) -> dict[str, Any]:
    """Capture exact TWSE responses and derive a reviewable, not-yet-installed bundle."""

    if through < start:
        raise EtfMonitorDataError("daily delta capture range is reversed")
    output.mkdir(parents=True, exist_ok=False)
    receipts_dir = output / "receipts"
    receipts_dir.mkdir()
    captured_at = (fetched_at or datetime.now(tz=TAIPEI)).astimezone(TAIPEI)
    receipt_rows: dict[str, tuple[bytes, dict[str, Any]]] = {}
    with session_factory() as session:
        for url, parser, params in (
            (HOLIDAY_URL, PARSER_HOLIDAY, None),
            (ACTION_URL, PARSER_ACTION, None),
            (DIVIDEND_URL, PARSER_DIVIDEND, {"startDate": str(start.year), "endDate": str(through.year), "stkNo": "0050"}),
        ):
            raw, evidence = _fetch_receipt(
                session, url=url, parser=parser, params=params, fetched_at=captured_at
            )
            receipt_rows[evidence["receipt_sha256"]] = (raw, evidence)
        month = date(start.year, start.month, 1)
        while month <= through:
            raw, evidence = _fetch_receipt(
                session,
                url=STOCK_DAY_URL,
                parser=PARSER_STOCK_DAY,
                params={"response": "json", "date": month.strftime("%Y%m01"), "stockNo": "0050"},
                fetched_at=captured_at,
            )
            receipt_rows[evidence["receipt_sha256"]] = (raw, evidence)
            month = date(month.year + (month.month == 12), 1 if month.month == 12 else month.month + 1, 1)
        holiday_raw = next(raw for raw, evidence in receipt_rows.values() if evidence["parser"] == PARSER_HOLIDAY)
        holidays = parse_holidays(holiday_raw)
        observed_days: set[date] = set()
        for raw, evidence in receipt_rows.values():
            if evidence["parser"] == PARSER_STOCK_DAY:
                observed_days.update(parse_stock_day(raw, request_url=evidence["request_url"]))
        cursor = start
        fetched_notice = False
        while cursor <= through:
            cached_raw = cached_emergency_notice(closure_receipt_root, cursor)
            if cached_raw is not None:
                digest = _sha256(cached_raw)
                receipt_rows[digest] = (cached_raw, {
                    "source_endpoint": EMERGENCY_CLOSURE_URL,
                    "request_url": EMERGENCY_CLOSURE_URL,
                    "fetched_at": captured_at.isoformat(),
                    "receipt_sha256": digest,
                    "receipt_relative_path": f"receipts/{digest}.raw",
                    "parser": PARSER_EMERGENCY_CLOSURE,
                    "reused_verified_calendar_receipt": True,
                })
            elif cursor.weekday() < 5 and cursor not in holidays and cursor not in observed_days:
                if not fetched_notice:
                    raw, evidence = _fetch_receipt(
                        session, url=EMERGENCY_CLOSURE_URL,
                        parser=PARSER_EMERGENCY_CLOSURE, params=None, fetched_at=captured_at,
                    )
                    parse_emergency_closures(raw)
                    receipt_rows[evidence["receipt_sha256"]] = (raw, evidence)
                    fetched_notice = True
            cursor += timedelta(days=1)
    for digest, (raw, _evidence) in receipt_rows.items():
        (receipts_dir / f"{digest}.raw").write_bytes(raw)
    evidence_by_parser = {
        evidence["parser"]: evidence for _raw, evidence in receipt_rows.values()
        if evidence["parser"] != PARSER_STOCK_DAY
    }
    action_map = parse_action_preview(receipt_rows[evidence_by_parser[PARSER_ACTION]["receipt_sha256"]][0])
    dividend_map = parse_dividend_list(receipt_rows[evidence_by_parser[PARSER_DIVIDEND]["receipt_sha256"]][0])
    deltas: list[dict[str, Any]] = []
    cursor = start
    while cursor <= through:
        month_evidence = next(
            evidence for _raw, evidence in receipt_rows.values()
            if evidence["parser"] == PARSER_STOCK_DAY
            and parse_qs(urlparse(evidence["request_url"]).query).get("date", [""])[0][:6] == cursor.strftime("%Y%m")
        )
        month_raw = receipt_rows[month_evidence["receipt_sha256"]][0]
        bar = parse_stock_day(month_raw, request_url=month_evidence["request_url"]).get(cursor)
        if action_map.get(cursor) or dividend_map.get(cursor):
            raise EtfMonitorDataError(
                f"{cursor}: official corporate action requires reviewed event construction before import"
            )
        payload: dict[str, Any] = {
            "schema_version": "stock-ai-bot-etf-daily-delta-v1",
            "symbol": "0050",
            "trade_date": cursor.isoformat(),
            "previous_calendar_date": (cursor - timedelta(days=1)).isoformat(),
            "frozen_snapshot_id": frozen_snapshot_id,
            "frozen_manifest_sha256": frozen_manifest_sha256,
            "market_status": "OPEN" if bar is not None else "CLOSED",
            "calendar_evidence": evidence_by_parser[PARSER_HOLIDAY],
            "bar": bar,
            "bar_evidence": month_evidence,
            "corporate_action_evidence": [
                evidence_by_parser[PARSER_ACTION], evidence_by_parser[PARSER_DIVIDEND]
            ],
            "corporate_actions": [],
        }
        closure_evidence = next((
            evidence for raw, evidence in receipt_rows.values()
            if evidence["parser"] == PARSER_EMERGENCY_CLOSURE
            and cursor in parse_emergency_closures(raw)
        ), None)
        if closure_evidence is not None:
            payload["emergency_closure_evidence"] = closure_evidence
        payload["content_sha256"] = stable_sha256(payload)
        verify_delta_receipts(payload, expected_date=cursor, receipt_root=output)
        path = output / f"{cursor.isoformat()}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
        deltas.append({"trade_date": cursor.isoformat(), "path": path.name, "content_sha256": payload["content_sha256"]})
        cursor += timedelta(days=1)
    manifest = {
        "schema_version": "stock-ai-bot-etf-daily-delta-bundle-v1",
        "created_at": captured_at.isoformat(),
        "frozen_snapshot_id": frozen_snapshot_id,
        "frozen_manifest_sha256": frozen_manifest_sha256,
        "deltas": deltas,
        "receipts": [
            {"path": f"receipts/{digest}.raw", "bytes": len(raw), "sha256": digest}
            for digest, (raw, _evidence) in sorted(receipt_rows.items())
        ],
    }
    manifest["content_sha256"] = stable_sha256(manifest)
    (output / "bundle-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8", newline="\n",
    )
    return manifest
