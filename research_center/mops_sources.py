from __future__ import annotations

from datetime import date, datetime
from typing import Any
from io import StringIO
import re
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
            "published_date": None,
            "document_kind": "reference_link",
            "payload": {"status": "reference_link", "kind": "material_event", "requested_date":target_date},
        },
        {
            "event_type": "mops_announcement_reference",
            "target": stock_code,
            "title": f"MOPS公告查詢入口 {stock_code}",
            "source_url": _mops_query_url(ANNOUNCEMENT_PAGE, stock_code, report_date),
            "source_level": "Level 1",
            "published_date": None,
            "document_kind": "reference_link",
            "payload": {"status": "reference_link", "kind": "announcement", "requested_date":target_date},
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
            for row in rows:
                title = _row_title(row, stock_code, kind)
                published = _row_date(row)
                events.append(
                    {
                        "event_type": event_type,
                        "target": stock_code,
                        "title": title,
                        "source_url": _mops_query_url(page, stock_code, report_date),
                        "source_level": "Level 1",
                        "published_date": published,
                        "document_kind": "parsed_record",
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
                    "published_date": None,
                    "document_kind": "unavailable",
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
            identity_fields = [value for key, value in item.items() if any(label in key for label in ('公司代號', '股票代號', '公司代碼', 'co_id'))]
            if not identity_fields:
                identity_fields = list(item.values())
            if not any(str(value).strip().removesuffix('.0') == stock_code for value in identity_fields):
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
                if len(parts) < 3:
                    continue
                day = int(parts[2])
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
    useful = [key for key in fields if any(token in str(key).lower() for token in ("eps", "margin", "profit", "revenue", "cash", "flow", "inventory", "turnover", "ytd", "ocf", "capex", "毛利", "營益", "淨利", "每股", "現金", "存貨", "週轉"))]
    status = "covered" if len(useful) >= 2 else "partial"
    return {
        "status": status,
        "latest_period": latest.get("Quarter") or latest.get("period") or latest.get("季度"),
        "fields": useful[:12],
        "available_field_count": len(fields),
        "score_points": 24 if status == "covered" else 12,
        "sample": {key: latest.get(key) for key in useful[:8]},
    }


def financial_period_audit(financial_rows, report_date=None) -> dict[str, Any]:
    """Expose period ambiguity, never turn quarterly/YTD EPS into invented TTM."""
    rows = financial_rows if isinstance(financial_rows, list) else []
    as_of = (report_date or datetime.now().date()).isoformat()
    periods, warnings = [], []
    for row in rows:
        if not isinstance(row, dict):
            continue
        quarter = str(row.get('Quarter') or row.get('period') or '')
        published = str(row.get('published_at') or row.get('Published_At') or '')[:10]
        if published and published > as_of:
            warnings.append(f'{quarter} 的可用日期晚於指定日期，不得用於當時估值。')
        match = re.fullmatch(r'(\d{4})Q([1-4])', quarter)
        periods.append({'period': quarter, 'published_at': published or None,
                        'currency': row.get('Currency') or row.get('currency'),
                        'eps_period_basis': row.get('EPS_Period_Basis') or row.get('eps_period_basis') or 'unverified',
                        'quarter_recognized': bool(match)})
    if not periods:
        warnings.append('未取得可核對的結構化季財報，搜尋摘要不等於完整財報。')
    if any(row['eps_period_basis'] == 'unverified' for row in periods):
        warnings.append('EPS 單季、累計或全年口徑未證實，不得直接當成近四季 EPS 或年化獲利。')
    return {'status': 'partial' if warnings else 'covered', 'periods': periods,
            'report_date': as_of, 'warnings': list(dict.fromkeys(warnings)),
            'policy': '官方新聞稿與結構化財報分開核對；日期、幣別、期間及單位未對齊不得互相替代。'}


def normalize_research_financial_periods(financial_rows, report_date=None):
    """Research-only projection; preserve raw figures, never infer EPS basis by size."""
    from copy import deepcopy
    rows = deepcopy(financial_rows)
    originals = {str(r.get('Quarter') or ''): r for r in financial_rows if isinstance(r, dict)}
    for row in rows:
        if not isinstance(row, dict):
            continue
        quarter = str(row.get('Quarter') or '')
        match = re.fullmatch(r'(\d{4})Q([1-4])', quarter)
        if not match or row.get('EPS') is None:
            continue
        year, q = match.groups()
        basis = row.get('EPS_Period_Basis') or row.get('eps_period_basis') or 'unverified'
        row['EPS_Period_Basis'] = basis
        row.setdefault('EPS_Raw', row['EPS'])
        if basis not in {'annual', 'ytd', 'cumulative'} or q == '1':
            continue
        previous = originals.get(f'{year}Q{int(q)-1}')
        try:
            if previous and (previous.get('EPS_Period_Basis') or previous.get('eps_period_basis')) in {'ytd', 'cumulative'}:
                previous_value = float(previous['EPS_Raw'] if 'EPS_Raw' in previous else previous['EPS'])
            else:
                prior = [originals.get(f'{year}Q{i}') for i in range(1, int(q))]
                if not prior or any(not r or (r.get('EPS_Period_Basis') or r.get('eps_period_basis')) not in {'quarterly', 'single_quarter'} for r in prior):
                    continue
                previous_value = sum(float(r['EPS']) for r in prior)
            row['EPS'] = round(float(row['EPS_Raw']) - previous_value, 6)
            row['EPS_Period_Basis'] = 'quarterly'
            row['EPS_Derivation'] = f'{quarter} 累計／全年減去同年先前期間；原值保留於 EPS_Raw'
        except (ValueError, TypeError, KeyError):
            continue
    audit = financial_period_audit(rows, report_date)
    audit['normalization_scope'] = '僅投研輸入副本，不改原始快取、交易規則或既有回測'
    return rows, audit


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
                "published_date": None,
                "document_kind": "reference_link",
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
                "published_date": None,
                "document_kind": "unavailable",
                "payload": {"status": "unavailable", "error": str(exc)},
            }
        ]


def _mops_query_url(base: str, stock_code: str, report_date: date | None) -> str:
    params = {"co_id": stock_code}
    if report_date:
        params.update({"year": str(report_date.year - 1911), "month": f"{report_date.month:02d}"})
    return f"{base}?{urlencode(params)}"



