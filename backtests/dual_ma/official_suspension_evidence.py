"""Freeze and verify official TWSE evidence for a fully suspended fetch segment.

The contract is intentionally narrow.  It accepts only date-specific TWSE
TWT93U reports whose legend defines ``!`` as stopped trading and whose target
row carries that marker.  A target may disappear on the final mainboard day
only when the frozen official universe source says it is delisted the next
calendar day and the preceding trading day is explicitly suspended.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping

import httpx


EVIDENCE_SCHEMA = "dual-ma-official-suspension-segment-v1"
TWSE_TWT93U_URL = (
    "https://www.twse.com.tw/exchangeReport/TWT93U?date={date}&response=json"
)
SUSPENDED_MARKER = "SUSPENDED_MARKER"
TERMINAL_DELISTING_ABSENT = "TERMINAL_DELISTING_ABSENT"


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _immutable_bytes(path: Path, content: bytes) -> None:
    if path.exists():
        if not path.is_file() or path.read_bytes() != content:
            raise FileExistsError(f"refusing to overwrite immutable evidence: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(content)
    os.replace(temporary, path)


def _immutable_json(path: Path, value: Any) -> None:
    _immutable_bytes(
        path,
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        + b"\n",
    )


def _copy_immutable(source: Path, target: Path) -> str:
    digest = sha256_file(source)
    if target.exists():
        if sha256_file(target) != digest:
            raise FileExistsError(f"refusing to overwrite immutable source: {target}")
        return digest
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    shutil.copyfile(source, temporary)
    if sha256_file(temporary) != digest:
        temporary.unlink(missing_ok=True)
        raise ValueError("copied suspension source hash mismatch")
    os.replace(temporary, target)
    return digest


def _resolve_inside(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        raise ValueError("suspension evidence artifact is absent or outside its root")
    return candidate


def _calendar_dates(path: Path, start: date, end: date) -> tuple[date, ...]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, list):
        raise ValueError("daily inputs must be a list")
    dates = tuple(
        date.fromisoformat(str(item["report_date"]))
        for item in payload
        if isinstance(item, dict)
        and start <= date.fromisoformat(str(item["report_date"])) <= end
    )
    if not dates or dates != tuple(sorted(set(dates))):
        raise ValueError("daily inputs do not provide a unique ordered trading calendar")
    return dates


def _twse_delisting_date(path: Path, code: str) -> date:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError("official universe source must be an object")
    endpoint = str((payload.get("source_urls") or {}).get("twse_delisted") or "")
    if "twse.com.tw" not in endpoint or "suspendListing" not in endpoint:
        raise ValueError("official universe source does not bind the TWSE delisting endpoint")
    matches = [
        item
        for item in payload.get("twse_delisted") or []
        if str(item.get("上市編號") or item.get("Code") or "").strip() == code
    ]
    if len(matches) != 1:
        raise ValueError("official universe source lacks one exact delisting row")
    raw = str(matches[0].get("終止上市日期") or matches[0].get("DelistingDate") or "")
    parts = raw.replace(".", "/").split("/")
    if len(parts) != 3:
        raise ValueError("official delisting date is invalid")
    year = int(parts[0]) + 1911 if int(parts[0]) < 1911 else int(parts[0])
    return date(year, int(parts[1]), int(parts[2]))


def _validate_twse_report(
    payload: Mapping[str, Any], *, trade_date: date, code: str, classification: str
) -> None:
    if payload.get("stat") != "OK" or payload.get("date") != trade_date.strftime("%Y%m%d"):
        raise ValueError("TWSE suspension report identity mismatch")
    fields = payload.get("fields")
    data = payload.get("data")
    notes = payload.get("notes")
    if (
        not isinstance(fields, list)
        or not fields
        or fields[-1] != "備註"
        or not isinstance(data, list)
        or not isinstance(notes, list)
    ):
        raise ValueError("TWSE suspension report schema mismatch")
    if not any("!─停止買賣" in str(note) or "!-停止買賣" in str(note) for note in notes):
        raise ValueError("TWSE suspension report does not define the stopped-trading marker")
    matches = [row for row in data if isinstance(row, list) and row and str(row[0]) == code]
    if classification == SUSPENDED_MARKER:
        if len(matches) != 1 or len(matches[0]) != len(fields) or "!" not in str(matches[0][-1]):
            raise ValueError("TWSE report does not mark the target security as stopped")
    elif classification == TERMINAL_DELISTING_ABSENT:
        if matches:
            raise ValueError("terminal delisting classification requires an absent target row")
    else:
        raise ValueError("unsupported suspension classification")


@dataclass(frozen=True)
class OfficialSuspensionSegmentEvidence:
    evidence_id: str
    content_sha256: str
    request_id: str
    symbol: str
    code: str
    exchange: str
    query_from: date
    query_to: date
    trade_dates: tuple[date, ...]
    manifest_path: Path


def load_official_suspension_evidence(
    path: str | Path,
) -> OfficialSuspensionSegmentEvidence:
    manifest_path = Path(path).resolve()
    root = manifest_path.parent
    payload = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict) or payload.get("schema_version") != EVIDENCE_SCHEMA:
        raise ValueError("unsupported official suspension evidence schema")
    if payload.get("status") != "PASS":
        raise ValueError("official suspension evidence is not PASS")
    content = {
        key: value
        for key, value in payload.items()
        if key not in {"content_sha256", "evidence_id"}
    }
    digest = canonical_sha256(content)
    if payload.get("content_sha256") != digest:
        raise ValueError("official suspension evidence content hash mismatch")
    if payload.get("evidence_id") != f"dual-ma-suspension#{digest[:16]}":
        raise ValueError("official suspension evidence id mismatch")

    start = date.fromisoformat(str(payload["query_from"]))
    end = date.fromisoformat(str(payload["query_to"]))
    if end < start:
        raise ValueError("official suspension evidence interval is invalid")
    calendar = payload.get("calendar") or {}
    calendar_path = _resolve_inside(root, str(calendar.get("path") or ""))
    if sha256_file(calendar_path) != calendar.get("sha256"):
        raise ValueError("suspension trading calendar hash mismatch")
    expected_dates = _calendar_dates(calendar_path, start, end)
    if calendar.get("trade_dates_sha256") != canonical_sha256(
        [item.isoformat() for item in expected_dates]
    ):
        raise ValueError("suspension trading calendar dates hash mismatch")

    universe = payload.get("official_universe_source") or {}
    universe_path = _resolve_inside(root, str(universe.get("path") or ""))
    if sha256_file(universe_path) != universe.get("sha256"):
        raise ValueError("suspension official universe source hash mismatch")
    code = str(payload["code"])
    delisted_on = _twse_delisting_date(universe_path, code)
    if delisted_on.isoformat() != universe.get("delisted_on"):
        raise ValueError("suspension delisting date disagrees with official source")

    reports = payload.get("daily_reports")
    if not isinstance(reports, list) or len(reports) != len(expected_dates):
        raise ValueError("official suspension reports do not cover every trading day")
    report_dates = tuple(date.fromisoformat(str(item["trade_date"])) for item in reports)
    if report_dates != expected_dates:
        raise ValueError("official suspension report dates mismatch the frozen calendar")
    for index, item in enumerate(reports):
        report_path = _resolve_inside(root, str(item.get("path") or ""))
        if sha256_file(report_path) != item.get("sha256"):
            raise ValueError("official suspension report hash mismatch")
        endpoint = str(item.get("endpoint") or "")
        expected_endpoint = TWSE_TWT93U_URL.format(date=report_dates[index].strftime("%Y%m%d"))
        if endpoint != expected_endpoint:
            raise ValueError("official suspension report endpoint mismatch")
        raw = json.loads(report_path.read_text(encoding="utf-8-sig"))
        _validate_twse_report(
            raw,
            trade_date=report_dates[index],
            code=code,
            classification=str(item.get("classification") or ""),
        )
    classifications = [str(item.get("classification") or "") for item in reports]
    terminal_indexes = [
        index for index, value in enumerate(classifications) if value == TERMINAL_DELISTING_ABSENT
    ]
    if terminal_indexes:
        if terminal_indexes != [len(reports) - 1]:
            raise ValueError("terminal delisting absence is allowed only on the final trading day")
        if len(reports) < 2 or classifications[-2] != SUSPENDED_MARKER:
            raise ValueError("terminal delisting absence requires prior explicit suspension")
        if expected_dates[-1] != end or delisted_on != end + timedelta(days=1):
            raise ValueError("terminal delisting absence is not closed by next-day delisting")
    elif any(value != SUSPENDED_MARKER for value in classifications):
        raise ValueError("official suspension report classification is incomplete")

    symbol = str(payload["symbol"])
    request_id = str(payload["request_id"])
    if (
        payload.get("exchange") != "TWSE"
        or symbol != f"{code}.TW"
        or not request_id.startswith(f"{symbol}:")
    ):
        raise ValueError("official suspension route identity mismatch")
    return OfficialSuspensionSegmentEvidence(
        evidence_id=str(payload["evidence_id"]),
        content_sha256=digest,
        request_id=request_id,
        symbol=symbol,
        code=code,
        exchange="TWSE",
        query_from=start,
        query_to=end,
        trade_dates=expected_dates,
        manifest_path=manifest_path,
    )


def freeze_twse_suspension_evidence(
    *,
    request_id: str,
    symbol: str,
    query_from: date,
    query_to: date,
    daily_inputs_path: Path,
    official_universe_source_path: Path,
    output_dir: Path,
    fetch: Callable[[str], bytes] | None = None,
) -> OfficialSuspensionSegmentEvidence:
    """Fetch immutable date-specific TWSE reports and freeze a verified manifest."""

    if not symbol.endswith(".TW") or request_id != f"{symbol}:{query_from.year}":
        raise ValueError("TWSE suspension request identity is invalid")
    code = symbol.removesuffix(".TW")
    output_dir = output_dir.resolve()
    source_dir = output_dir / "sources"
    calendar_target = source_dir / f"daily_inputs#{sha256_file(daily_inputs_path)[:16]}.json"
    universe_target = source_dir / (
        f"official_universe#{sha256_file(official_universe_source_path)[:16]}.json"
    )
    calendar_sha = _copy_immutable(daily_inputs_path.resolve(), calendar_target)
    universe_sha = _copy_immutable(
        official_universe_source_path.resolve(), universe_target
    )
    trade_dates = _calendar_dates(calendar_target, query_from, query_to)
    delisted_on = _twse_delisting_date(universe_target, code)

    def default_fetch(url: str) -> bytes:
        for attempt in range(1, 5):
            time.sleep(1.1 if attempt == 1 else min(8.0, 2.0 ** (attempt - 1)))
            response = httpx.get(
                url,
                timeout=30,
                follow_redirects=True,
            )
            if response.status_code == 200:
                return response.content
            if response.status_code not in {428, 429, 500, 502, 503, 504}:
                break
        raise ValueError(
            f"TWSE suspension request failed: HTTP {response.status_code}"
        )

    fetch_bytes = fetch or default_fetch
    reports: list[dict[str, Any]] = []
    for index, trade_date in enumerate(trade_dates):
        endpoint = TWSE_TWT93U_URL.format(date=trade_date.strftime("%Y%m%d"))
        cached = sorted(
            (output_dir / "raw").glob(f"TWT93U_{trade_date:%Y%m%d}#*.json")
        )
        if len(cached) > 1:
            raise ValueError("multiple immutable TWSE suspension reports exist")
        raw = cached[0].read_bytes() if cached else fetch_bytes(endpoint)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("TWSE suspension response is not JSON") from exc
        matches = [
            row
            for row in payload.get("data") or []
            if isinstance(row, list) and row and str(row[0]) == code
        ]
        classification = (
            SUSPENDED_MARKER if matches else TERMINAL_DELISTING_ABSENT
        )
        if classification == TERMINAL_DELISTING_ABSENT and index != len(trade_dates) - 1:
            raise ValueError("TWSE target disappeared before the terminal trading day")
        _validate_twse_report(
            payload,
            trade_date=trade_date,
            code=code,
            classification=classification,
        )
        digest = hashlib.sha256(raw).hexdigest()
        target = output_dir / "raw" / f"TWT93U_{trade_date:%Y%m%d}#{digest[:16]}.json"
        if cached and cached[0] != target:
            raise ValueError("cached TWSE suspension report hash/name mismatch")
        _immutable_bytes(target, raw)
        reports.append(
            {
                "trade_date": trade_date.isoformat(),
                "endpoint": endpoint,
                "path": target.relative_to(output_dir).as_posix(),
                "sha256": digest,
                "classification": classification,
            }
        )
    if reports[-1]["classification"] == TERMINAL_DELISTING_ABSENT and (
        delisted_on != query_to + timedelta(days=1)
        or len(reports) < 2
        or reports[-2]["classification"] != SUSPENDED_MARKER
    ):
        raise ValueError("terminal TWSE absence is not closed by official delisting")

    core = {
        "schema_version": EVIDENCE_SCHEMA,
        "status": "PASS",
        "request_id": request_id,
        "symbol": symbol,
        "code": code,
        "exchange": "TWSE",
        "query_from": query_from.isoformat(),
        "query_to": query_to.isoformat(),
        "calendar": {
            "path": calendar_target.relative_to(output_dir).as_posix(),
            "sha256": calendar_sha,
            "trade_dates_sha256": canonical_sha256(
                [item.isoformat() for item in trade_dates]
            ),
        },
        "official_universe_source": {
            "path": universe_target.relative_to(output_dir).as_posix(),
            "sha256": universe_sha,
            "delisted_on": delisted_on.isoformat(),
        },
        "daily_reports": reports,
        "decision": "FULL_SEGMENT_HAS_NO_TRADABLE_OBSERVATION",
        "performance_read_or_computed": False,
    }
    digest = canonical_sha256(core)
    manifest = {
        **core,
        "evidence_id": f"dual-ma-suspension#{digest[:16]}",
        "content_sha256": digest,
    }
    # Use a stable filename; immutable writes reject a different second freeze.
    manifest_path = output_dir / "evidence.json"
    _immutable_json(manifest_path, manifest)
    return load_official_suspension_evidence(manifest_path)
