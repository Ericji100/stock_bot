from __future__ import annotations

from datetime import date, datetime
from io import StringIO
from typing import Any

import httpx
import pandas as pd

USER_AGENT = "Mozilla/5.0 (compatible; stock-ai-bot/1.0; official-data)"

TAIFEX_VIX_URLS = [
    "https://www.taifex.com.tw/cht/7/vixMinNew",
    "https://www.bq888.taifex.com.tw/cht/7/vixMinNew",
]
TAIFEX_FUTURES_CONTRACTS_URLS = [
    "https://www.taifex.com.tw/cht/3/futContractsDateExcel",
    "https://www.bq888.taifex.com.tw/cht/3/futContractsDateExcel",
]
TWSE_INSTITUTIONAL_URL = "https://www.twse.com.tw/fund/BFI82U"


def fetch_taifex_vix(report_date: date | None = None) -> dict[str, Any]:
    errors: list[str] = []
    reachable_url: str | None = None
    for url in TAIFEX_VIX_URLS:
        try:
            html = _get_text(url)
            reachable_url = url
            rows = parse_taifex_vix_html(html, report_date)
            if rows:
                return {
                    "status": "official_public",
                    "source": url,
                    "latest": rows[0],
                    "rows": rows[:30],
                    "note": "TAIFEX public VIX page; licensed full intraday/history files still require TAIFEX data shop or another paid feed.",
                }
        except Exception as exc:
            errors.append(f"{url}: {exc}")
    if reachable_url:
        return {
            "status": "official_public_reference",
            "source": reachable_url,
            "latest": None,
            "rows": [],
            "note": "TAIFEX VIX page is reachable, but table values were not parseable in this environment.",
            "paid_feed_ready": True,
        }
    return {
        "status": "unavailable",
        "source": "TAIFEX VIX public page",
        "errors": errors,
        "paid_feed_ready": True,
        "paid_feed_note": "TAIFEX data shop provides licensed Taiwan VIX datasets; configure a paid importer when available.",
    }


def fetch_taifex_futures_institutional(report_date: date | None = None) -> dict[str, Any]:
    errors: list[str] = []
    for url in TAIFEX_FUTURES_CONTRACTS_URLS:
        try:
            html = _get_text(url)
            rows = parse_taifex_futures_contracts_html(html)
            if rows:
                tx_rows = [row for row in rows if _looks_like_taiex_future(row.get("product"))]
                return {
                    "status": "official_public",
                    "source": url,
                    "report_date": report_date.isoformat() if report_date else None,
                    "tx_futures_rows": tx_rows[:6],
                    "all_rows_sample": rows[:20],
                    "note": "TAIFEX public three-institution futures table parsed best-effort.",
                }
        except Exception as exc:
            errors.append(f"{url}: {exc}")
    return {"status": "unavailable", "source": "TAIFEX futures institutional", "errors": errors}


def fetch_twse_institutional_flow(report_date: date | None = None) -> dict[str, Any]:
    params = {"response": "json", "type": "day"}
    if report_date:
        params["dayDate"] = report_date.strftime("%Y%m%d")
    try:
        with httpx.Client(timeout=12.0, follow_redirects=True, verify=False, headers={"User-Agent": USER_AGENT}) as client:
            response = client.get(TWSE_INSTITUTIONAL_URL, params=params)
            response.raise_for_status()
            payload = response.json()
        rows = parse_twse_institutional_json(payload)
        if rows:
            return {
                "status": "official_public",
                "source": str(response.url),
                "title": payload.get("title"),
                "rows": rows,
                "net_amount_total": _sum_net_amount(rows),
                "note": "TWSE BFI82U three-institution buy/sell amount table.",
            }
        return {"status": "empty", "source": str(response.url), "payload_status": payload.get("stat")}
    except Exception as exc:
        return {"status": "unavailable", "source": TWSE_INSTITUTIONAL_URL, "error": str(exc)}


def parse_taifex_vix_html(html: str, report_date: date | None = None) -> list[dict[str, Any]]:
    tables = pd.read_html(StringIO(html))
    rows: list[dict[str, Any]] = []
    for table in tables:
        if table.shape[1] < 2:
            continue
        for _, raw in table.iterrows():
            raw_date = _parse_date(raw.iloc[0])
            value = _parse_number(raw.iloc[1])
            if raw_date is None or value is None:
                continue
            if report_date and raw_date > report_date:
                continue
            rows.append({"date": raw_date.isoformat(), "value": value})
    rows.sort(key=lambda row: row["date"], reverse=True)
    return rows


def parse_taifex_futures_contracts_html(html: str) -> list[dict[str, Any]]:
    tables = pd.read_html(StringIO(html))
    rows: list[dict[str, Any]] = []
    for table in tables:
        if table.empty or table.shape[1] < 4:
            continue
        current_product = None
        for _, raw in table.iterrows():
            values = [str(value).strip() for value in raw.tolist()]
            if not values or values[0] in ("序號", "nan"):
                continue
            product = values[1] if len(values) > 1 and values[1] not in ("nan", "-") else current_product
            identity = values[2] if len(values) > 2 else None
            if product and product != "nan":
                current_product = product
            if identity not in ("自營商", "投信", "外資"):
                continue
            rows.append(
                {
                    "product": current_product,
                    "identity": identity,
                    "trade_long_contracts": _parse_number(values[3]) if len(values) > 3 else None,
                    "trade_short_contracts": _parse_number(values[5]) if len(values) > 5 else None,
                    "trade_net_contracts": _parse_number(values[7]) if len(values) > 7 else None,
                    "open_interest_long_contracts": _parse_number(values[9]) if len(values) > 9 else None,
                    "open_interest_short_contracts": _parse_number(values[11]) if len(values) > 11 else None,
                    "open_interest_net_contracts": _parse_number(values[13]) if len(values) > 13 else None,
                }
            )
    return rows


def parse_twse_institutional_json(payload: dict[str, Any]) -> list[dict[str, Any]]:
    fields = payload.get("fields") or []
    data = payload.get("data") or []
    rows: list[dict[str, Any]] = []
    for raw in data:
        row = dict(zip(fields, raw))
        rows.append(
            {
                "name": row.get("單位名稱") or row.get("name") or (raw[0] if raw else None),
                "buy_amount": _parse_number(row.get("買進金額") or (raw[1] if len(raw) > 1 else None)),
                "sell_amount": _parse_number(row.get("賣出金額") or (raw[2] if len(raw) > 2 else None)),
                "net_amount": _parse_number(row.get("買賣差額") or (raw[3] if len(raw) > 3 else None)),
            }
        )
    return rows


def _get_text(url: str) -> str:
    with httpx.Client(timeout=12.0, follow_redirects=True, verify=False, headers={"User-Agent": USER_AGENT}) as client:
        response = client.get(url)
        response.raise_for_status()
        return response.text


def _parse_date(value: Any) -> date | None:
    text = str(value).strip()
    for fmt in ("%Y/%m/%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            pass
    return None


def _parse_number(value: Any) -> float | None:
    text = str(value).replace(",", "").strip()
    if text in ("", "-", "nan", "None"):
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return int(number) if number.is_integer() else number


def _sum_net_amount(rows: list[dict[str, Any]]) -> float | None:
    values = [float(row["net_amount"]) for row in rows if row.get("net_amount") is not None and "合計" not in str(row.get("name"))]
    return round(sum(values), 2) if values else None


def _looks_like_taiex_future(value: Any) -> bool:
    text = str(value or "")
    return "臺股" in text or "台股" in text or "TX" == text.strip().upper()
