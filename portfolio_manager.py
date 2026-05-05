from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime
from pathlib import Path

import httpx
import pytz


PORTFOLIO_PATH = Path(__file__).with_name("portfolio.json")
STOCK_LIST_PATH = Path(__file__).with_name("stock_list.json")
TWSE_NAME_API_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
TPEX_NAME_API_URL = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O"
TWSE_INSTITUTIONAL_URL = "https://www.twse.com.tw/fund/T86"
TPEX_QFII_URL = "https://www.tpex.org.tw/www/zh-tw/insti/qfiiStat"
TPEX_SITC_URL = "https://www.tpex.org.tw/www/zh-tw/insti/sitcStat"
TPEX_DEALER_URL = "https://www.tpex.org.tw/www/zh-tw/insti/dealerStat"
FINMIND_API_URL = "https://api.finmindtrade.com/api/v4/data"
TAIPEI_TZ = pytz.timezone("Asia/Taipei")
MARKDOWN_V2_SPECIALS = re.compile(r"([_\*\[\]\(\)~`>#+\-=|{}\.!\\])")
PORTFOLIO_PUSH_RETRY_DELAY_SECONDS = 300
PORTFOLIO_PUSH_MAX_RETRIES = 3

_STOCK_CACHE: dict[str, dict[str, object]] = {
    "code_to_stock": {},
    "name_to_stock": {},
}


@dataclass(frozen=True)
class ResolvedStock:
    code: str
    name: str
    market: str
    symbol: str


@dataclass(frozen=True)
class InstitutionalRecord:
    code: str
    name: str
    foreign: float
    investment_trust: float
    dealer: float

    @property
    def total(self) -> int:
        return self.foreign + self.investment_trust + self.dealer


def get_tw_now() -> datetime:
    return datetime.now(TAIPEI_TZ)


def load_portfolio() -> dict[str, str]:
    if not PORTFOLIO_PATH.exists():
        return {}

    try:
        with PORTFOLIO_PATH.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, json.JSONDecodeError):
        return {}

    if not isinstance(data, dict):
        return {}

    normalized = {}
    for code, name in data.items():
        normalized_code = str(code).strip()
        normalized_name = str(name).strip()
        if normalized_code and normalized_name:
            normalized[normalized_code] = normalized_name
    return normalized


def save_portfolio(portfolio: dict[str, str]) -> None:
    with PORTFOLIO_PATH.open("w", encoding="utf-8") as file:
        json.dump(dict(sorted(portfolio.items())), file, indent=4, ensure_ascii=False)


def list_portfolio() -> list[ResolvedStock]:
    portfolio = load_portfolio()
    code_to_stock = _get_stock_cache()["code_to_stock"]
    results = []
    for code, name in sorted(portfolio.items()):
        stock = code_to_stock.get(code)
        if isinstance(stock, ResolvedStock):
            results.append(ResolvedStock(code=stock.code, name=name or stock.name, market=stock.market, symbol=stock.symbol))
        else:
            results.append(ResolvedStock(code=code, name=name, market="UNKNOWN", symbol=code))
    return results


def add_portfolio_stock(user_input: str) -> tuple[str, ResolvedStock | None]:
    stock = resolve_stock_reference(user_input)
    if stock is None:
        return "invalid", None

    portfolio = load_portfolio()
    if stock.code in portfolio:
        return "exists", ResolvedStock(code=stock.code, name=portfolio[stock.code], market=stock.market, symbol=stock.symbol)

    portfolio[stock.code] = stock.name
    save_portfolio(portfolio)
    return "added", stock


def remove_portfolio_stock(user_input: str) -> tuple[str, ResolvedStock | None]:
    portfolio = load_portfolio()
    if not portfolio:
        return "missing", None

    match_code = _find_portfolio_code(user_input, portfolio)
    if not match_code:
        resolved = resolve_stock_reference(user_input)
        if resolved and resolved.code in portfolio:
            match_code = resolved.code

    if not match_code:
        return "missing", None

    removed_name = portfolio.pop(match_code)
    save_portfolio(portfolio)

    stock = _get_stock_cache()["code_to_stock"].get(match_code)
    if isinstance(stock, ResolvedStock):
        return "removed", ResolvedStock(code=match_code, name=removed_name, market=stock.market, symbol=stock.symbol)
    return "removed", ResolvedStock(code=match_code, name=removed_name, market="UNKNOWN", symbol=match_code)


def resolve_stock_reference(user_input: str) -> ResolvedStock | None:
    lookup = str(user_input or "").strip()
    if not lookup:
        return None

    cache = _get_stock_cache()
    code_to_stock = cache["code_to_stock"]
    name_to_stock = cache["name_to_stock"]

    compact_lookup = lookup.upper().strip()
    base_code = compact_lookup.split(".", 1)[0]
    if base_code in code_to_stock:
        stock = code_to_stock[base_code]
        if isinstance(stock, ResolvedStock):
            return stock

    normalized_name = _normalize_lookup_text(lookup)
    stock = name_to_stock.get(normalized_name)
    if isinstance(stock, ResolvedStock):
        return stock

    return None


def build_portfolio_report(date_to_query: datetime | None = None) -> dict[str, object]:
    portfolio = load_portfolio()
    if not portfolio:
        return {"status": "empty"}

    target_date = (date_to_query or get_tw_now()).astimezone(TAIPEI_TZ)
    twse_records, twse_date = _fetch_twse_records(target_date)
    tpex_records, tpex_date = _fetch_tpex_records(target_date)
    code_to_stock = _get_stock_cache()["code_to_stock"]
    missing_twse_codes = [
        code
        for code in portfolio
        if isinstance(code_to_stock.get(code), ResolvedStock)
        and code_to_stock[code].market == "TWSE"
        and code not in twse_records
    ]
    if missing_twse_codes:
        finmind_records = _fetch_finmind_twse_records(target_date, missing_twse_codes, portfolio)
        if finmind_records:
            twse_records.update(finmind_records)
            twse_date = twse_date or target_date.strftime("%Y%m%d")

    if not twse_records and not tpex_records:
        return {"status": "retry"}

    report_date = _format_report_date(twse_date or tpex_date or target_date.strftime("%Y%m%d"))
    records = []

    for code, saved_name in sorted(portfolio.items()):
        stock = code_to_stock.get(code)
        market = stock.market if isinstance(stock, ResolvedStock) else "UNKNOWN"
        if market == "TWSE":
            record = twse_records.get(code)
        elif market == "TPEX":
            record = tpex_records.get(code)
        else:
            record = twse_records.get(code) or tpex_records.get(code)

        if record is None:
            record = InstitutionalRecord(
                code=code,
                name=saved_name,
                foreign=0,
                investment_trust=0,
                dealer=0,
            )
        else:
            record = InstitutionalRecord(
                code=record.code,
                name=saved_name or record.name,
                foreign=record.foreign,
                investment_trust=record.investment_trust,
                dealer=record.dealer,
            )

        records.append(record)

    return {
        "status": "ok",
        "date": report_date,
        "message": format_portfolio_report_message(report_date, records),
    }


def format_portfolio_report_message(report_date: str, records: list[InstitutionalRecord]) -> str:
    lines = [
        "💼 【本日庫存籌碼總結】",
        f"📅 日期：{report_date}",
        "",
    ]

    for index, record in enumerate(records):
        lines.append(f"🔸 {record.code} {record.name}")
        lines.append(
            "   外資：{foreign} | 投信：{investment} | 自營商：{dealer}".format(
                foreign=_format_signed_shares(record.foreign),
                investment=_format_signed_shares(record.investment_trust),
                dealer=_format_signed_shares(record.dealer),
            )
        )
        lines.append(f"   👉 法人合計：{_format_signed_shares(record.total)}")
        if index != len(records) - 1:
            lines.append("")

    return "\n".join(lines).strip()


def escape_markdown_v2(text: str) -> str:
    return MARKDOWN_V2_SPECIALS.sub(r"\\\1", text)


def _get_stock_cache() -> dict[str, dict[str, object]]:
    if _STOCK_CACHE["code_to_stock"]:
        return _STOCK_CACHE

    stocks: list[ResolvedStock] = []

    if STOCK_LIST_PATH.exists():
        try:
            with STOCK_LIST_PATH.open("r", encoding="utf-8") as file:
                data = json.load(file)
            for item in data.get("stocks", []):
                code = str(item.get("code", "")).strip()
                name = str(item.get("name", "")).strip()
                market = str(item.get("market", "")).strip().upper()
                symbol = str(item.get("symbol", "")).strip().upper() or code
                if code and name:
                    stocks.append(ResolvedStock(code=code, name=name, market=market, symbol=symbol))
        except (OSError, json.JSONDecodeError, AttributeError):
            stocks = []

    stocks.extend(_fetch_official_stock_catalog())

    code_to_stock: dict[str, ResolvedStock] = {}
    name_to_stock: dict[str, ResolvedStock] = {}
    for stock in stocks:
        code_to_stock.setdefault(stock.code, stock)
        if stock.symbol:
            code_to_stock.setdefault(stock.symbol.split(".", 1)[0], stock)
        name_to_stock.setdefault(_normalize_lookup_text(stock.name), stock)

    _STOCK_CACHE["code_to_stock"] = code_to_stock
    _STOCK_CACHE["name_to_stock"] = name_to_stock
    return _STOCK_CACHE


def _fetch_official_stock_catalog() -> list[ResolvedStock]:
    stocks = []
    try:
        with httpx.Client(timeout=20.0, follow_redirects=True, verify=False) as client:
            twse_response = client.get(TWSE_NAME_API_URL)
            twse_response.raise_for_status()
            for item in twse_response.json():
                code = str(item.get("公司代號", "")).strip()
                name = str(item.get("公司簡稱") or item.get("公司名稱") or "").strip()
                if code and name:
                    stocks.append(ResolvedStock(code=code, name=name, market="TWSE", symbol=f"{code}.TW"))

            tpex_response = client.get(TPEX_NAME_API_URL)
            tpex_response.raise_for_status()
            for item in tpex_response.json():
                code = str(item.get("SecuritiesCompanyCode", "")).strip()
                name = str(item.get("CompanyAbbreviation") or item.get("CompanyName") or "").strip()
                if code and name:
                    stocks.append(ResolvedStock(code=code, name=name, market="TPEX", symbol=f"{code}.TWO"))
    except Exception:
        return []

    return stocks


def _find_portfolio_code(user_input: str, portfolio: dict[str, str]) -> str | None:
    lookup = str(user_input or "").strip()
    if not lookup:
        return None

    direct_code = lookup.upper().split(".", 1)[0]
    if direct_code in portfolio:
        return direct_code

    normalized_name = _normalize_lookup_text(lookup)
    for code, name in portfolio.items():
        if _normalize_lookup_text(name) == normalized_name:
            return code

    return None


def _fetch_twse_records(target_date: datetime) -> tuple[dict[str, InstitutionalRecord], str | None]:
    records: dict[str, InstitutionalRecord] = {}
    try:
        response = httpx.get(
            TWSE_INSTITUTIONAL_URL,
            params={"response": "json", "date": target_date.strftime("%Y%m%d"), "selectType": "ALL"},
            timeout=20.0,
            follow_redirects=True,
            verify=False,
        )
        response.raise_for_status()
        data = response.json()
    except Exception:
        return {}, None

    rows = data.get("data") or []
    if not rows:
        return {}, None

    for row in rows:
        if len(row) < 18:
            continue
        code = str(row[0]).strip()
        name = str(row[1]).strip()
        foreign = _parse_twse_lots(row[4]) + _parse_twse_lots(row[7])
        investment_trust = _parse_twse_lots(row[10])
        dealer = _parse_twse_lots(row[11])
        if code:
            records[code] = InstitutionalRecord(
                code=code,
                name=name,
                foreign=foreign,
                investment_trust=investment_trust,
                dealer=dealer,
            )

    return records, data.get("date")


def _fetch_finmind_twse_records(
    target_date: datetime,
    codes: list[str],
    fallback_names: dict[str, str],
) -> dict[str, InstitutionalRecord]:
    records: dict[str, InstitutionalRecord] = {}
    target_text = target_date.strftime("%Y-%m-%d")
    with httpx.Client(timeout=20.0, follow_redirects=True, verify=False, headers={"User-Agent": "Mozilla/5.0"}) as client:
        for code in sorted(set(codes)):
            try:
                response = client.get(
                    FINMIND_API_URL,
                    params={
                        "dataset": "TaiwanStockInstitutionalInvestorsBuySell",
                        "data_id": code,
                        "start_date": target_text,
                        "end_date": target_text,
                    },
                )
                response.raise_for_status()
                payload = response.json()
            except Exception:
                continue

            if payload.get("status") != 200:
                continue

            foreign = 0.0
            investment_trust = 0.0
            dealer = 0.0
            has_row = False
            for row in payload.get("data") or []:
                if str(row.get("date")) != target_text:
                    continue
                name = str(row.get("name") or "")
                buy = _parse_signed_number(row.get("buy"))
                sell = _parse_signed_number(row.get("sell"))
                net_lots = (buy - sell) / 1000.0
                if name == "Foreign_Investor":
                    foreign += net_lots
                    has_row = True
                elif name == "Investment_Trust":
                    investment_trust += net_lots
                    has_row = True
                elif name in {"Dealer_self", "Dealer_Hedging"}:
                    dealer += net_lots
                    has_row = True

            if has_row:
                records[code] = InstitutionalRecord(
                    code=code,
                    name=fallback_names.get(code, ""),
                    foreign=foreign,
                    investment_trust=investment_trust,
                    dealer=dealer,
                )
    return records


def _fetch_tpex_records(target_date: datetime) -> tuple[dict[str, InstitutionalRecord], str | None]:
    foreign_data, report_date = _fetch_tpex_sorted_records(TPEX_QFII_URL, target_date, sort_param_name="searchType", net_index=11)
    investment_data, investment_date = _fetch_tpex_sorted_records(TPEX_SITC_URL, target_date, sort_param_name="searchType", net_index=5)
    dealer_data, dealer_date = _fetch_tpex_sorted_records(TPEX_DEALER_URL, target_date, sort_param_name="stype", net_index=9)

    merged: dict[str, InstitutionalRecord] = {}
    for code in set(foreign_data) | set(investment_data) | set(dealer_data):
        name = foreign_data.get(code, {}).get("name") or investment_data.get(code, {}).get("name") or dealer_data.get(code, {}).get("name") or ""
        merged[code] = InstitutionalRecord(
            code=code,
            name=str(name).strip(),
            foreign=int(foreign_data.get(code, {}).get("net", 0)),
            investment_trust=int(investment_data.get(code, {}).get("net", 0)),
            dealer=int(dealer_data.get(code, {}).get("net", 0)),
        )

    return merged, report_date or investment_date or dealer_date


def _fetch_tpex_sorted_records(
    url: str,
    target_date: datetime,
    *,
    sort_param_name: str,
    net_index: int,
) -> tuple[dict[str, dict[str, object]], str | None]:
    merged: dict[str, dict[str, object]] = {}
    report_date = None

    for sort_value in ("buy", "sell"):
        try:
            response = httpx.get(
                url,
                params={
                    "response": "json",
                    "date": target_date.strftime("%Y/%m/%d"),
                    "type": "Daily",
                    sort_param_name: sort_value,
                },
                timeout=20.0,
                follow_redirects=True,
                verify=False,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception:
            continue

        tables = payload.get("tables") or []
        if not tables:
            continue

        table = tables[0]
        report_date = report_date or table.get("date")
        for row in table.get("data") or []:
            if len(row) <= net_index:
                continue
            code = str(row[1]).strip()
            name = str(row[2]).strip()
            if not code:
                continue
            merged[code] = {
                "name": name,
                "net": _parse_signed_number(row[net_index]),
            }

    return merged, report_date


def _parse_signed_number(value: object) -> float:
    text = str(value or "").strip().replace(",", "")
    if text in {"", "-", "--"}:
        return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def _parse_twse_lots(value: object) -> float:
    return _parse_signed_number(value) / 1000.0


def _format_signed_shares(value: float) -> str:
    rounded_value = int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))

    if rounded_value > 0:
        display_value = f"{rounded_value:,}"
        return f"+{display_value} 張"
    if rounded_value < 0:
        display_value = f"{abs(rounded_value):,}"
        return f"-{display_value} 張"
    return "0 張"


def _normalize_lookup_text(text: str) -> str:
    return re.sub(r"\s+", "", str(text or "")).upper()


def _format_report_date(raw_date: str) -> str:
    text = str(raw_date or "").strip()
    if re.fullmatch(r"\d{8}", text):
        return f"{text[0:4]}-{text[4:6]}-{text[6:8]}"
    if re.fullmatch(r"\d{3}/\d{2}/\d{2}", text):
        roc_year, month, day = text.split("/")
        year = int(roc_year) + 1911
        return f"{year:04d}-{month}-{day}"
    return text
