from __future__ import annotations

import json
from calendar import monthrange
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pandas as pd

ROOT_DIR = Path(__file__).resolve().parents[1]
CACHE_DIR = ROOT_DIR / ".cache"
VALUATION_HISTORY_CACHE_DIR = CACHE_DIR / "valuation_history"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128 Safari/537.36"

TWSE_VALUATION_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/BWIBBU_d"
TWSE_INDUSTRY_INDEX_URL = "https://www.twse.com.tw/rwd/zh/TAIEX/MI_INDEX"
TPEx_VALUATION_URL = "https://www.tpex.org.tw/www/zh-tw/afterTrading/peQryDate"
MOPS_ANNUAL_REPORT_URL = "https://mops.twse.com.tw/mops/web/t57sb01_q5"
MOPS_INVESTOR_CONFERENCE_URL = "https://mops.twse.com.tw/mops/web/t100sb07_1"


def build_free_research_sources(stock_code: str, symbol: str | None = None, report_date: date | None = None) -> dict[str, Any]:
    valuation_history = fetch_stock_valuation_history(stock_code, symbol=symbol, report_date=report_date)
    valuation = fetch_stock_valuation(stock_code, report_date)
    if valuation.get("status") != "official_public" and valuation_history.get("latest"):
        valuation = {
            "status": "official_public",
            "source": valuation_history.get("source"),
            "market": valuation_history.get("market"),
            "latest": valuation_history.get("latest"),
            "rows": [valuation_history.get("latest")],
            "fallback": "valuation_history_latest",
        }
    valuation["history"] = list(valuation_history.get("history") or [])
    valuation["history_status"] = valuation_history.get("status")
    return {
        "valuation": valuation,
        "valuation_history": valuation_history,
        "tdcc": load_tdcc_snapshot(stock_code, report_date),
        "gross_margin_cache": load_gross_margin_snapshot(symbol or stock_code),
        "mops_documents": build_mops_document_references(stock_code, report_date),
        "data_policy": "免費公開來源與本地快取；若官方頁面改版或沒有資料，會回傳 unavailable/reference，不中斷報告。",
    }


def fetch_stock_valuation_history(
    stock_code: str,
    *,
    symbol: str | None = None,
    report_date: date | None = None,
    months: int = 60,
) -> dict[str, Any]:
    """Load point-in-time monthly PE/PB observations from official exchanges.

    Exchange snapshots contain the whole market.  They are cached by market and
    date, so warming one stock also warms the same observations for every peer.
    """

    market = "TPEX" if str(symbol or "").upper().endswith(".TWO") else "TWSE"
    end_date = report_date or datetime.now().date()
    history: list[dict[str, Any]] = []
    sources: list[str] = []
    seen_dates: set[str] = set()
    for anchor in _month_end_anchors(end_date, months):
        snapshot = fetch_market_valuation_snapshot(market, anchor)
        rows = snapshot.get("rows") or []
        for row in rows:
            if str(row.get("code") or "").strip() != str(stock_code):
                continue
            value = dict(row)
            value["date"] = snapshot.get("data_date")
            data_date = str(value.get("date") or "")
            if data_date and data_date not in seen_dates:
                history.append(value)
                seen_dates.add(data_date)
            break
        source = str(snapshot.get("source") or "")
        if source and source not in sources:
            sources.append(source)
    history.sort(key=lambda row: str(row.get("date") or ""))
    return {
        "status": "official_public" if history else "unavailable",
        "market": market,
        "source": sources[0] if sources else None,
        "history": history,
        "latest": history[-1] if history else None,
        "observation_count": len(history),
        "requested_months": months,
    }


def fetch_market_valuation_snapshot(market: str, report_date: date) -> dict[str, Any]:
    """Return the latest official daily valuation snapshot on/before a date."""

    market_name = "TPEX" if str(market).upper() == "TPEX" else "TWSE"
    for offset in range(8):
        target = report_date - timedelta(days=offset)
        cache_path = VALUATION_HISTORY_CACHE_DIR / f"{market_name.lower()}_{target:%Y%m%d}.json"
        cached = _read_json_cache(cache_path)
        if cached is not None:
            if cached.get("rows"):
                return cached
            continue
        snapshot = _fetch_market_valuation_snapshot_live(market_name, target)
        if snapshot.get("status") != "unavailable":
            _write_json_cache(cache_path, snapshot)
        if snapshot.get("rows"):
            return snapshot
    return {
        "status": "unavailable",
        "market": market_name,
        "data_date": report_date.isoformat(),
        "rows": [],
        "note": "指定日期往前 7 日均無官方估值資料。",
    }


def build_valuation_context_map(
    stock_codes: list[str],
    universe: list[Any],
    report_date: date,
    *,
    months: int = 60,
) -> dict[str, dict[str, Any]]:
    """Build own-history and official-industry peer valuation context in one pass."""

    targets = {str(code).strip() for code in stock_codes if str(code).strip()}
    entry_by_code = {str(getattr(entry, "code", "")).strip(): entry for entry in universe}
    history_by_code: dict[str, list[dict[str, Any]]] = {code: [] for code in targets}
    latest_by_code: dict[str, dict[str, Any]] = {}
    source_by_market: dict[str, str | None] = {}

    for market in ("TWSE", "TPEX"):
        market_targets = {
            code
            for code in targets
            if str(getattr(entry_by_code.get(code), "market", "")).upper() == market
        }
        if not market_targets:
            continue
        first_rows: list[dict[str, Any]] | None = None
        for anchor in _month_end_anchors(report_date, months):
            snapshot = fetch_market_valuation_snapshot(market, anchor)
            rows = list(snapshot.get("rows") or [])
            if rows and first_rows is None:
                first_rows = rows
                source_by_market[market] = snapshot.get("source")
            row_map = {str(row.get("code") or "").strip(): row for row in rows}
            for code in market_targets:
                row = row_map.get(code)
                if not row:
                    continue
                value = dict(row)
                value["date"] = snapshot.get("data_date") or value.get("date")
                history_by_code[code].append(value)
        for row in first_rows or []:
            code = str(row.get("code") or "").strip()
            if code:
                latest_by_code[code] = dict(row)

    result: dict[str, dict[str, Any]] = {}
    for code in targets:
        entry = entry_by_code.get(code)
        industry = str(getattr(entry, "industry", "") or "")
        market = str(getattr(entry, "market", "") or "").upper()
        history = history_by_code.get(code) or []
        history.sort(key=lambda row: str(row.get("date") or ""))
        peers: list[dict[str, Any]] = []
        for peer_code, peer_row in latest_by_code.items():
            if peer_code == code:
                continue
            peer_entry = entry_by_code.get(peer_code)
            if peer_entry is None or str(getattr(peer_entry, "industry", "") or "") != industry:
                continue
            peers.append({**peer_row, "industry": industry})
        latest = history[-1] if history else latest_by_code.get(code)
        result[code] = {
            "status": "official_public" if latest else "unavailable",
            "market": market,
            "industry": industry,
            "source": source_by_market.get(market),
            "latest": latest,
            "history": history,
            "peers": peers,
            "history_observation_count": len(history),
            "peer_count": len(peers),
        }
    return result


def warmup_valuation_history_cache(
    report_date: date,
    *,
    months: int = 60,
    workers: int = 4,
) -> dict[str, Any]:
    """Warm market-level valuation snapshots used by every stock and peer."""

    tasks = [
        (market, anchor)
        for market in ("TWSE", "TPEX")
        for anchor in _month_end_anchors(report_date, months)
    ]
    covered = 0
    unavailable = 0
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(tasks)))) as executor:
        futures = {
            executor.submit(fetch_market_valuation_snapshot, market, anchor): (market, anchor)
            for market, anchor in tasks
        }
        for future in as_completed(futures):
            try:
                snapshot = future.result()
            except Exception:
                unavailable += 1
                continue
            if snapshot.get("rows"):
                covered += 1
            else:
                unavailable += 1
    return {
        "requested_snapshots": len(tasks),
        "covered_snapshots": covered,
        "unavailable_snapshots": unavailable,
        "months": months,
    }


def _fetch_market_valuation_snapshot_live(market: str, report_date: date) -> dict[str, Any]:
    try:
        if market == "TWSE":
            payload, source = _get_json(
                TWSE_VALUATION_URL,
                {"response": "json", "date": report_date.strftime("%Y%m%d")},
            )
            rows = parse_twse_valuation_json(payload)
        else:
            payload, source = _get_json(
                TPEx_VALUATION_URL,
                {"response": "json", "date": report_date.strftime("%Y/%m/%d")},
            )
            rows = parse_tpex_valuation_json(payload)
        for row in rows:
            row["date"] = report_date.isoformat()
        return {
            "status": "official_public" if rows else "empty",
            "market": market,
            "data_date": report_date.isoformat(),
            "source": source,
            "rows": rows,
        }
    except Exception as exc:
        return {
            "status": "unavailable",
            "market": market,
            "data_date": report_date.isoformat(),
            "source": TWSE_VALUATION_URL if market == "TWSE" else TPEx_VALUATION_URL,
            "rows": [],
            "error": str(exc),
        }


def _month_end_anchors(end_date: date, months: int) -> list[date]:
    anchors: list[date] = []
    year = end_date.year
    month = end_date.month
    for index in range(max(0, months)):
        last_day = monthrange(year, month)[1]
        anchor = date(year, month, last_day)
        if index == 0 and anchor > end_date:
            anchor = end_date
        anchors.append(anchor)
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    return anchors


def _read_json_cache(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def _write_json_cache(path: Path, payload: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except Exception:
        return


def build_free_macro_sources(report_date: date | None = None) -> dict[str, Any]:
    return {
        "twse_industry_index": fetch_twse_industry_index(report_date),
        "data_policy": "TWSE 類股指數公開頁 best-effort；失敗時保留狀態供 AI 保守判斷。",
    }


def fetch_stock_valuation(stock_code: str, report_date: date | None = None) -> dict[str, Any]:
    twse = _fetch_twse_valuation(stock_code, report_date)
    if twse.get("status") == "official_public":
        return twse
    tpex = _fetch_tpex_valuation(stock_code, report_date)
    if tpex.get("status") == "official_public":
        return tpex
    return {"status": "unavailable", "twse": twse, "tpex": tpex, "note": "TWSE/TPEx 免費估值資料未取得。"}


def _fetch_twse_valuation(stock_code: str, report_date: date | None = None) -> dict[str, Any]:
    params = {"response": "json", "stockNo": stock_code}
    if report_date:
        params["date"] = report_date.strftime("%Y%m%d")
    try:
        payload, url = _get_json(TWSE_VALUATION_URL, params)
        rows = parse_twse_valuation_json(payload, stock_code)
        if rows:
            return {"status": "official_public", "source": url, "market": "TWSE", "rows": rows[:10], "latest": rows[0]}
        return {"status": "empty", "source": url, "payload_status": payload.get("stat")}
    except Exception as exc:
        return {"status": "unavailable", "source": TWSE_VALUATION_URL, "error": str(exc)}


def _fetch_tpex_valuation(stock_code: str, report_date: date | None = None) -> dict[str, Any]:
    params = {"response": "json", "stockNo": stock_code}
    if report_date:
        params["date"] = report_date.strftime("%Y/%m/%d")
    try:
        payload, url = _get_json(TPEx_VALUATION_URL, params)
        rows = parse_tpex_valuation_json(payload, stock_code)
        if rows:
            return {"status": "official_public", "source": url, "market": "TPEx", "rows": rows[:10], "latest": rows[0]}
        return {"status": "empty", "source": url, "payload_status": payload.get("stat") or payload.get("message")}
    except Exception as exc:
        return {"status": "unavailable", "source": TPEx_VALUATION_URL, "error": str(exc)}


def parse_twse_valuation_json(payload: dict[str, Any], stock_code: str | None = None) -> list[dict[str, Any]]:
    fields = payload.get("fields") or []
    rows = []
    for raw in payload.get("data") or []:
        row = dict(zip(fields, raw))
        code = row.get("證券代號") or (raw[0] if raw else None)
        if stock_code and str(code).strip() != str(stock_code):
            continue
        rows.append(
            {
                "code": str(code).strip() if code is not None else None,
                "name": row.get("證券名稱") or (raw[1] if len(raw) > 1 else None),
                "dividend_yield_pct": _number(row.get("殖利率(%)")) or (_number(raw[3]) if len(raw) > 3 else None),
                "pe_ratio": _number(row.get("本益比")) or (_number(raw[5]) if len(raw) > 5 else None),
                "pb_ratio": _number(row.get("股價淨值比")) or (_number(raw[6]) if len(raw) > 6 else None),
                "financial_year_quarter": row.get("財報年/季"),
            }
        )
    return rows


def parse_tpex_valuation_json(payload: dict[str, Any], stock_code: str | None = None) -> list[dict[str, Any]]:
    fields = payload.get("fields") or []
    tables = payload.get("tables")
    if not fields and isinstance(tables, list) and tables:
        fields = tables[0].get("fields", [])
    table_data = tables[0].get("data", []) if isinstance(tables, list) and tables else []
    data = payload.get("data") or payload.get("aaData") or table_data or []
    rows = []
    for raw in data:
        row = dict(zip(fields, raw)) if fields else {}
        code = row.get("股票代號") or row.get("代號") or row.get("證券代號") or (raw[0] if raw else None)
        if stock_code and str(code).strip() != str(stock_code):
            continue
        rows.append(
            {
                "code": str(code).strip() if code is not None else None,
                "name": row.get("名稱") or row.get("股票名稱") or row.get("證券名稱") or (raw[1] if len(raw) > 1 else None),
                "dividend_yield_pct": _first_number(row, ("殖利率", "殖利率(%)")) or (_number(raw[5]) if len(raw) > 5 else None),
                "pe_ratio": _first_number(row, ("本益比", "PE")) or (_number(raw[2]) if len(raw) > 2 else None),
                "pb_ratio": _first_number(row, ("股價淨值比", "PBR")) or (_number(raw[6]) if len(raw) > 6 else None),
            }
        )
    return rows


def fetch_twse_industry_index(report_date: date | None = None) -> dict[str, Any]:
    params = {"response": "json", "type": "ALL"}
    if report_date:
        params["date"] = report_date.strftime("%Y%m%d")
    try:
        payload, url = _get_json(TWSE_INDUSTRY_INDEX_URL, params)
        rows = parse_twse_industry_index_json(payload)
        if rows:
            return {"status": "official_public", "source": url, "rows": rows[:40], "note": "TWSE MI_INDEX 類股/大盤公開資料。"}
        return {"status": "empty", "source": url, "payload_status": payload.get("stat")}
    except Exception as exc:
        return {"status": "unavailable", "source": TWSE_INDUSTRY_INDEX_URL, "error": str(exc)}


def parse_twse_industry_index_json(payload: dict[str, Any]) -> list[dict[str, Any]]:
    fields = payload.get("fields") or []
    rows = []
    for raw in payload.get("data") or []:
        row = dict(zip(fields, raw))
        name = row.get("指數") or row.get("類型") or (raw[0] if raw else None)
        if not name:
            continue
        rows.append(
            {
                "name": str(name),
                "close": _first_number(row, ("收盤指數", "收盤價", "指數")),
                "change": _first_number(row, ("漲跌(+/-)", "漲跌")),
                "change_pct": _first_number(row, ("漲跌百分比(%)", "漲跌幅(%)")),
            }
        )
    return rows


def load_tdcc_snapshot(stock_code: str, report_date: date | None = None) -> dict[str, Any]:
    path = _latest_cache_file(CACHE_DIR / "tdcc", "*.csv", report_date)
    if not path:
        return {"status": "missing", "source": str(CACHE_DIR / "tdcc"), "note": "找不到 TDCC 快取。"}
    try:
        frame = pd.read_csv(path, dtype=str)
        return parse_tdcc_frame(frame, stock_code, path.name)
    except Exception as exc:
        return {"status": "unavailable", "source": str(path), "error": str(exc)}


def parse_tdcc_frame(frame: pd.DataFrame, stock_code: str, source_name: str = "tdcc") -> dict[str, Any]:
    code_col = _find_column(frame, ("證券代號", "stock", "code"))
    level_col = _find_column(frame, ("持股分級", "level"))
    people_col = _find_column(frame, ("人數", "people"))
    shares_col = _find_column(frame, ("股數", "shares"))
    pct_col = _find_column(frame, ("占集保庫存數比例", "比例", "%"))
    date_col = _find_column(frame, ("資料日期", "date"))
    if not code_col or not level_col:
        return {"status": "unavailable", "source": source_name, "error": "TDCC 欄位不足"}
    rows = frame[frame[code_col].astype(str).str.strip() == str(stock_code)].copy()
    if rows.empty:
        return {"status": "empty", "source": source_name, "code": stock_code}
    level_summary = []
    large_holder_pct = 0.0
    retail_holder_pct = 0.0
    total_people = 0
    for _, row in rows.iterrows():
        level = str(row.get(level_col, "")).strip()
        pct = _number(row.get(pct_col)) or 0.0
        people = int(_number(row.get(people_col), 0) or 0)
        shares = int(_number(row.get(shares_col), 0) or 0)
        total_people += people
        if level in {"15", "16", "17"}:
            large_holder_pct += pct
        if level in {"1", "2", "3", "4", "5"}:
            retail_holder_pct += pct
        level_summary.append({"level": level, "people": people, "shares": shares, "pct": pct})
    data_date = str(rows.iloc[0].get(date_col, "")) if date_col else None
    return {
        "status": "covered",
        "source": source_name,
        "code": stock_code,
        "data_date": data_date,
        "total_people": total_people,
        "large_holder_pct": round(large_holder_pct, 2),
        "retail_holder_pct": round(retail_holder_pct, 2),
        "concentration_signal": _tdcc_signal(large_holder_pct, retail_holder_pct),
        "levels": level_summary,
    }


def load_gross_margin_snapshot(symbol_or_code: str) -> dict[str, Any]:
    path = CACHE_DIR / "gross_margin.json"
    if not path.exists():
        return {"status": "missing", "source": str(path)}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        metrics = payload.get("metrics") or {}
        candidates = [symbol_or_code, f"{symbol_or_code}.TW", f"{symbol_or_code}.TWO"]
        for key in candidates:
            if key in metrics:
                series = metrics[key].get("series") or []
                return {"status": "covered", "source": str(path), "symbol": key, "series": series[:8], "latest": series[0] if series else None}
        return {"status": "empty", "source": str(path), "symbol": symbol_or_code}
    except Exception as exc:
        return {"status": "unavailable", "source": str(path), "error": str(exc)}


def build_mops_document_references(stock_code: str, report_date: date | None = None) -> dict[str, Any]:
    params = {"co_id": stock_code}
    if report_date:
        params["year"] = str(report_date.year - 1911)
    return {
        "status": "official_reference",
        "annual_report": {"title": "MOPS年報查詢", "url": _url(MOPS_ANNUAL_REPORT_URL, params), "source_level": "Level 1"},
        "investor_conference": {"title": "MOPS法說會查詢", "url": _url(MOPS_INVESTOR_CONFERENCE_URL, params), "source_level": "Level 1"},
        "note": "MOPS PDF/HTML 內容需依個別公司公告格式解析；目前先納入官方入口與查詢參數。",
    }


def _latest_cache_file(directory: Path, pattern: str, report_date: date | None) -> Path | None:
    if not directory.exists():
        return None
    files = sorted(directory.glob(pattern))
    if report_date:
        cutoff = report_date.strftime("%Y%m%d")
        files = [path for path in files if path.stem <= cutoff]
    return files[-1] if files else None


def _get_json(url: str, params: dict[str, str]) -> tuple[dict[str, Any], str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
        "Referer": "https://www.twse.com.tw/zh/trading/historical/bwibbu.html",
    }
    with httpx.Client(timeout=12.0, follow_redirects=True, verify=False, headers=headers) as client:
        response = client.get(url, params=params)
        response.raise_for_status()
        return response.json(), str(response.url)


def _url(base: str, params: dict[str, str]) -> str:
    from urllib.parse import urlencode

    return f"{base}?{urlencode(params)}"


def _find_column(frame: pd.DataFrame, keywords: tuple[str, ...]) -> str | None:
    for column in frame.columns:
        text = str(column)
        if any(keyword in text for keyword in keywords):
            return column
    return None


def _number(value: Any, default: float | None = None) -> float | None:
    if value is None:
        return default
    text = str(value).replace(",", "").replace("%", "").strip()
    if text in ("", "-", "nan", "None"):
        return default
    try:
        return float(text)
    except ValueError:
        return default


def _first_number(row: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key, value in row.items():
        if any(token in str(key) for token in keys):
            parsed = _number(value)
            if parsed is not None:
                return parsed
    return None


def _tdcc_signal(large_holder_pct: float, retail_holder_pct: float) -> str:
    if large_holder_pct >= 55 and retail_holder_pct <= 15:
        return "high_concentration"
    if large_holder_pct >= 40:
        return "moderate_concentration"
    if retail_holder_pct >= 30:
        return "retail_heavy"
    return "neutral"

