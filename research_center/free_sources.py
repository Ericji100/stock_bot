from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import httpx
import pandas as pd

ROOT_DIR = Path(__file__).resolve().parents[1]
CACHE_DIR = ROOT_DIR / ".cache"
USER_AGENT = "Mozilla/5.0 (compatible; stock-ai-bot/1.0; free-sources)"

TWSE_VALUATION_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/BWIBBU_d"
TWSE_INDUSTRY_INDEX_URL = "https://www.twse.com.tw/rwd/zh/TAIEX/MI_INDEX"
TPEx_VALUATION_URL = "https://www.tpex.org.tw/www/zh-tw/afterTrading/peQryDate"
MOPS_ANNUAL_REPORT_URL = "https://mops.twse.com.tw/mops/web/t57sb01_q5"
MOPS_INVESTOR_CONFERENCE_URL = "https://mops.twse.com.tw/mops/web/t100sb07_1"


def build_free_research_sources(stock_code: str, symbol: str | None = None, report_date: date | None = None) -> dict[str, Any]:
    return {
        "valuation": fetch_stock_valuation(stock_code, report_date),
        "tdcc": load_tdcc_snapshot(stock_code, report_date),
        "gross_margin_cache": load_gross_margin_snapshot(symbol or stock_code),
        "mops_documents": build_mops_document_references(stock_code, report_date),
        "data_policy": "免費公開來源與本地快取；若官方頁面改版或沒有資料，會回傳 unavailable/reference，不中斷報告。",
    }


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
                "dividend_yield_pct": _number(row.get("殖利率(%)")),
                "pe_ratio": _number(row.get("本益比")),
                "pb_ratio": _number(row.get("股價淨值比")),
                "financial_year_quarter": row.get("財報年/季"),
            }
        )
    return rows


def parse_tpex_valuation_json(payload: dict[str, Any], stock_code: str | None = None) -> list[dict[str, Any]]:
    fields = payload.get("fields") or []
    tables = payload.get("tables")
    if not fields and isinstance(tables, list) and tables:
        fields = tables[0].get("fields", [])
    data = payload.get("data") or payload.get("aaData") or []
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
                "dividend_yield_pct": _first_number(row, ("殖利率", "殖利率(%)")),
                "pe_ratio": _first_number(row, ("本益比", "PE")),
                "pb_ratio": _first_number(row, ("股價淨值比", "PBR")),
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
    with httpx.Client(timeout=12.0, follow_redirects=True, verify=False, headers={"User-Agent": USER_AGENT}) as client:
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

