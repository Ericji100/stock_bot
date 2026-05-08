from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
RECENT_SCAN_PATH = ROOT_DIR / ".cache" / "recent_scan_results.json"


def save_recent_scan_result(scan_type: str, report_date: date, report_text: str) -> dict[str, Any]:
    records = load_recent_scan_results(limit=20)
    codes = extract_stock_codes(report_text)
    record = {
        "scan_id": f"{_safe(scan_type)}_{report_date.strftime('%Y%m%d')}_{datetime.now().strftime('%H%M%S')}",
        "scan_type": scan_type,
        "report_date": report_date.isoformat(),
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "candidate_count": len(codes),
        "codes": codes,
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
    return [item for item in data if isinstance(item, dict)][:limit]


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
    codes: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(r"(?<!\d)(\d{4})(?!\d)", text or ""):
        code = match.group(1)
        if code in seen:
            continue
        seen.add(code)
        codes.append(code)
    return codes


def _safe(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_.-]+", "_", str(value)).strip("_")[:40] or "scan"
