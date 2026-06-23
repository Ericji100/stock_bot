from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .convergence_service import candidate_snapshot_from_row

ROOT_DIR = Path(__file__).resolve().parents[1]
RECENT_SCAN_PATH = ROOT_DIR / ".cache" / "recent_scan_results.json"
STOCK_LIST_PATH = ROOT_DIR / "stock_list.json"


def save_recent_scan_result(
    scan_type: str,
    report_date: date,
    report_text: str,
    selected_codes: list[str] | None = None,
) -> dict[str, Any]:
    records = load_recent_scan_results(limit=20)
    codes = _normalise_stock_codes(selected_codes) if selected_codes is not None else extract_stock_codes(report_text)
    record = {
        "scan_id": f"{_safe(scan_type)}_{report_date.strftime('%Y%m%d')}_{datetime.now().strftime('%H%M%S')}",
        "scan_type": scan_type,
        "report_date": report_date.isoformat(),
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "candidate_count": len(codes),
        "codes": codes,
        "selected_codes": codes,
        "candidate_snapshot": _build_recent_scan_candidate_snapshots(scan_type, report_date, codes),
        "summary": report_text[:3000],
    }
    records.insert(0, record)
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in records:
        key = str(item.get("scan_id") or "")
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    RECENT_SCAN_PATH.parent.mkdir(parents=True, exist_ok=True)
    RECENT_SCAN_PATH.write_text(json.dumps(deduped[:30], ensure_ascii=False, indent=2), encoding="utf-8")
    return record


def load_recent_scan_results(limit: int = 10) -> list[dict[str, Any]]:
    if not RECENT_SCAN_PATH.exists():
        return []
    try:
        data = json.loads(RECENT_SCAN_PATH.read_text(encoding="utf-8-sig"))
    except Exception:
        return []
    return [_sanitize_recent_scan_record(item) for item in data if isinstance(item, dict)][:limit]


def find_recent_scan(scan_id: str | None = None) -> dict[str, Any] | None:
    records = load_recent_scan_results(limit=30)
    if not records:
        return None
    if not scan_id:
        return records[0]
    for item in records:
        if item.get("scan_id") == scan_id:
            return item
    return None


def extract_stock_codes(text: str) -> list[str]:
    valid_codes = _load_valid_stock_codes()
    codes: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(r"(?:^|[|】])\s*(\d{4})(?!\d)\s+[^\s|()（）,，:：]+", text or "", re.MULTILINE):
        code = match.group(1)
        if valid_codes is not None and code not in valid_codes:
            continue
        if code in seen:
            continue
        seen.add(code)
        codes.append(code)
    return codes


def _normalise_stock_codes(values: list[str] | None) -> list[str]:
    valid_codes = _load_valid_stock_codes()
    codes: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        code = str(value).strip()
        if not code or code in seen:
            continue
        if not re.fullmatch(r"\d{4}", code):
            continue
        if valid_codes is not None and code not in valid_codes:
            continue
        seen.add(code)
        codes.append(code)
    return codes


def _load_valid_stock_codes() -> set[str] | None:
    try:
        payload = json.loads(STOCK_LIST_PATH.read_text(encoding="utf-8-sig"))
    except Exception:
        return None
    stocks = payload.get("stocks") if isinstance(payload, dict) else None
    if not isinstance(stocks, list):
        return None
    codes = {
        str(item.get("code", "")).strip()
        for item in stocks
        if isinstance(item, dict) and re.fullmatch(r"\d{4}", str(item.get("code", "")).strip())
    }
    return codes or None


def _sanitize_recent_scan_record(item: dict[str, Any]) -> dict[str, Any]:
    record = dict(item)
    raw_codes = record.get("selected_codes") or record.get("codes")
    codes = _normalise_stock_codes(raw_codes)
    if not codes and record.get("summary"):
        codes = extract_stock_codes(str(record.get("summary") or ""))
    record["codes"] = codes
    record["selected_codes"] = codes
    record["candidate_count"] = len(codes)
    if "candidate_snapshot" not in record:
        report_date = _parse_report_date(record.get("report_date"))
        record["candidate_snapshot"] = _build_recent_scan_candidate_snapshots(
            str(record.get("scan_type") or "scan"),
            report_date,
            codes,
        )
    return record


def _safe(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_.-]+", "_", str(value)).strip("_")[:40] or "scan"


def _build_recent_scan_candidate_snapshots(scan_type: str, report_date: date, codes: list[str]) -> list[dict[str, Any]]:
    return [
        candidate_snapshot_from_row(
            {
                "code": code,
                "scan_type": scan_type,
                "report_date": report_date.isoformat(),
            },
            source_command="scan",
            source_pool=scan_type,
            data_date=report_date.isoformat(),
        )
        for code in codes
    ]


def _parse_report_date(value: Any) -> date:
    try:
        return date.fromisoformat(str(value))
    except Exception:
        return datetime.now().date()
