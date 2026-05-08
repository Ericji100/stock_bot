from __future__ import annotations

from datetime import date, datetime
from typing import Any
from io import StringIO
from urllib.parse import urlencode

import httpx
import pandas as pd

MOPS_BASE = "https://mops.twse.com.tw/mops/web"
MATERIAL_EVENT_PAGE = f"{MOPS_BASE}/t05st02"
ANNOUNCEMENT_PAGE = f"{MOPS_BASE}/t146sb05"


def build_mops_reference_events(stock_code: str, report_date: date | None = None) -> list[dict[str, Any]]:
    """Return official MOPS reference events and optional scraped rows.

    MOPS pages change form fields often. This function keeps a stable official
    link event even when scraping is unavailable, so value_scan can distinguish
    between "not checked" and "official source available for review".
    """
    target_date = report_date.isoformat() if report_date else None
    events = [
        {
            "event_type": "mops_material_reference",
            "target": stock_code,
            "title": f"MOPS重大訊息查詢入口 {stock_code}",
            "source_url": _mops_query_url(MATERIAL_EVENT_PAGE, stock_code, report_date),
            "source_level": "Level 1",
            "published_date": target_date,
            "payload": {"status": "reference_link", "kind": "material_event"},
        },
        {
            "event_type": "mops_announcement_reference",
            "target": stock_code,
            "title": f"MOPS公告查詢入口 {stock_code}",
            "source_url": _mops_query_url(ANNOUNCEMENT_PAGE, stock_code, report_date),
            "source_level": "Level 1",
            "published_date": target_date,
            "payload": {"status": "reference_link", "kind": "announcement"},
        },
    ]
    events.extend(_try_fetch_recent_material_events(stock_code, report_date))
    events.extend(_try_fetch_announcement_rows(stock_code, report_date))
    return events


def _try_fetch_announcement_rows(stock_code: str, report_date: date | None = None) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for page, kind, event_type in (
        (MATERIAL_EVENT_PAGE, "material_event", "mops_material_parsed"),
        (ANNOUNCEMENT_PAGE, "announcement", "mops_announcement_parsed"),
    ):
        try:
            rows = _fetch_mops_tables(page, stock_code, report_date)
            for row in rows[:8]:
                title = _row_title(row, stock_code, kind)
                published = _row_date(row) or (report_date.isoformat() if report_date else None)
                events.append(
                    {
                        "event_type": event_type,
                        "target": stock_code,
                        "title": title,
                        "source_url": _mops_query_url(page, stock_code, report_date),
                        "source_level": "Level 1",
                        "published_date": published,
                        "payload": {"status": "parsed", "kind": kind, "row": row},
                    }
                )
        except Exception as exc:
            events.append(
                {
                    "event_type": f"{event_type}_parse_status",
                    "target": stock_code,
                    "title": f"MOPS {kind} parser unavailable",
                    "source_url": _mops_query_url(page, stock_code, report_date),
                    "source_level": "Level 1",
                    "published_date": report_date.isoformat() if report_date else None,
                    "payload": {"status": "unavailable", "error": str(exc)},
                }
            )
    return events


def _fetch_mops_tables(page: str, stock_code: str, report_date: date | None = None) -> list[dict[str, Any]]:
    params = {"co_id": stock_code}
    if report_date:
        params.update({"year": str(report_date.year - 1911), "month": f"{report_date.month:02d}"})
    with httpx.Client(timeout=10.0, follow_redirects=True) as client:
        response = client.get(page, params=params)
        response.raise_for_status()
        html = response.text
    tables = pd.read_html(StringIO(html))
    rows: list[dict[str, Any]] = []
    for table in tables:
        if table.empty:
            continue
        frame = table.copy()
        frame.columns = [str(col).strip() for col in frame.columns]
        for _, raw in frame.iterrows():
            item = {str(key): _clean_cell(value) for key, value in raw.to_dict().items()}
            text = " ".join(str(value) for value in item.values())
            if stock_code not in text and len(rows) > 0:
                continue
            if report_date:
                parsed = _row_date(item)
                if parsed and parsed > report_date.isoformat():
                    continue
            if any(value not in ("", "nan", "None") for value in item.values()):
                rows.append(item)
    return rows


def _row_title(row: dict[str, Any], stock_code: str, kind: str) -> str:
    for key, value in row.items():
        if any(token in str(key) for token in ("主旨", "說明", "公告", "標題", "subject")) and value:
            return str(value)[:120]
    values = [str(value) for value in row.values() if str(value).strip()]
    return f"MOPS {kind} {stock_code} " + " ".join(values[:3])[:100]


def _row_date(row: dict[str, Any]) -> str | None:
    for value in row.values():
        text = str(value).strip()
        for fmt in ("%Y/%m/%d", "%Y-%m-%d"):
            try:
                return datetime.strptime(text[:10], fmt).date().isoformat()
            except ValueError:
                pass
        if len(text) >= 7 and text[:3].isdigit() and "/" in text:
            parts = text.split("/")
            try:
                year = int(parts[0]) + 1911
                month = int(parts[1])
                day = int(parts[2]) if len(parts) > 2 else 1
                return date(year, month, day).isoformat()
            except Exception:
                pass
    return None


def _clean_cell(value: Any) -> str:
    text = str(value).replace("\u3000", " ").strip()
    return " ".join(text.split())

def financial_detail_snapshot(financial_rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not financial_rows:
        return {"status": "missing", "fields": [], "latest_period": None, "score_points": 0}
    latest = financial_rows[-1]
    fields = [key for key, value in latest.items() if value not in (None, "", "-")]
    useful = [key for key in fields if any(token in str(key).lower() for token in ("eps", "margin", "profit", "revenue", "毛利", "營益", "淨利", "每股"))]
    status = "covered" if len(useful) >= 2 else "partial"
    return {
        "status": status,
        "latest_period": latest.get("Quarter") or latest.get("period") or latest.get("季度"),
        "fields": useful[:12],
        "available_field_count": len(fields),
        "score_points": 24 if status == "covered" else 12,
        "sample": {key: latest.get(key) for key in useful[:8]},
    }


def _try_fetch_recent_material_events(stock_code: str, report_date: date | None = None) -> list[dict[str, Any]]:
    # Conservative placeholder for future parser work. MOPS anti-automation and
    # form token changes should not break report generation.
    try:
        with httpx.Client(timeout=8.0, follow_redirects=True) as client:
            response = client.get(MATERIAL_EVENT_PAGE)
            response.raise_for_status()
        return [
            {
                "event_type": "mops_connectivity_check",
                "target": stock_code,
                "title": "MOPS重大訊息頁連線檢查",
                "source_url": MATERIAL_EVENT_PAGE,
                "source_level": "Level 1",
                "published_date": report_date.isoformat() if report_date else None,
                "payload": {"status": "reachable", "http_status": response.status_code},
            }
        ]
    except Exception as exc:
        return [
            {
                "event_type": "mops_connectivity_check",
                "target": stock_code,
                "title": "MOPS重大訊息頁連線檢查失敗",
                "source_url": MATERIAL_EVENT_PAGE,
                "source_level": "Level 1",
                "published_date": report_date.isoformat() if report_date else None,
                "payload": {"status": "unavailable", "error": str(exc)},
            }
        ]


def _mops_query_url(base: str, stock_code: str, report_date: date | None) -> str:
    params = {"co_id": stock_code}
    if report_date:
        params.update({"year": str(report_date.year - 1911), "month": f"{report_date.month:02d}"})
    return f"{base}?{urlencode(params)}"



