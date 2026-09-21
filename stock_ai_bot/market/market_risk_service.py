from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx


ROOT_DIR = Path(__file__).resolve().parent
CACHE_DIR = ROOT_DIR / ".cache" / "market_risk"
SOURCE_URLS = {
    "twse_attention": "https://openapi.twse.com.tw/v1/announcement/notice",
    "twse_disposition": "https://openapi.twse.com.tw/v1/announcement/punish",
    "tpex_attention": "https://www.tpex.org.tw/openapi/v1/tpex_trading_warning_information",
    "tpex_disposition": "https://www.tpex.org.tw/openapi/v1/tpex_disposal_information",
}


@dataclass(frozen=True)
class MarketRiskResult:
    report_date: date
    by_code: dict[str, dict[str, Any]]
    source_status: dict[str, str]
    from_cache: bool = False

    @property
    def complete(self) -> bool:
        return bool(self.source_status) and all(value == "ok" for value in self.source_status.values())


def load_market_risk_map(report_date: date, *, force_refresh: bool = False) -> MarketRiskResult:
    """Load official TWSE/TPEx attention and disposition flags for a date.

    The official OpenAPI feeds are current/recent feeds, not a complete historical
    point-in-time database.  A dated local cache is therefore preferred whenever
    it exists.  Source failure is retained in ``source_status`` and never treated
    as proof that a stock has no risk flag.
    """

    cache_path = CACHE_DIR / f"{report_date.isoformat()}.json"
    if not force_refresh:
        cached = _read_cache(cache_path)
        if cached is not None and _cache_is_usable(cached, report_date):
            return MarketRiskResult(
                report_date=report_date,
                by_code=dict(cached.get("by_code") or {}),
                source_status=dict(cached.get("source_status") or {}),
                from_cache=True,
            )

    by_code: dict[str, dict[str, Any]] = {}
    source_status: dict[str, str] = {}
    raw_by_source: dict[str, list[dict[str, Any]]] = {}
    with httpx.Client(timeout=20.0, follow_redirects=True, verify=False, headers={"User-Agent": "Mozilla/5.0"}) as client:
        for source, url in SOURCE_URLS.items():
            try:
                rows = _fetch_json(client, url)
                raw_by_source[source] = rows
                source_status[source] = "ok"
            except Exception as exc:
                raw_by_source[source] = []
                source_status[source] = f"unavailable:{type(exc).__name__}"

    for source, rows in raw_by_source.items():
        kind = "attention" if source.endswith("attention") else "disposition"
        market = "TWSE" if source.startswith("twse") else "TPEX"
        for row in rows:
            code = _stock_code(row)
            if not re.fullmatch(r"\d{4}", code):
                continue
            if kind == "attention" and _roc_date(row.get("Date")) != report_date:
                continue
            period = _disposition_period(row) if kind == "disposition" else (report_date, report_date)
            if kind == "disposition" and not _date_in_period(report_date, period):
                continue
            item = by_code.setdefault(
                code,
                {
                    "code": code,
                    "market": market,
                    "attention": False,
                    "disposition": False,
                    "details": [],
                },
            )
            item[kind] = True
            item["details"].append(
                {
                    "kind": kind,
                    "source": source,
                    "date": str(row.get("Date") or ""),
                    "period": str(row.get("DispositionPeriod") or ""),
                    "reason": str(
                        row.get("TradingInfoForAttention")
                        or row.get("TradingInformation")
                        or row.get("ReasonsOfDisposition")
                        or row.get("DispositionReasons")
                        or ""
                    )[:500],
                }
            )

    payload = {
        "schema_version": 1,
        "report_date": report_date.isoformat(),
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_urls": SOURCE_URLS,
        "source_status": source_status,
        "by_code": by_code,
    }
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass
    return MarketRiskResult(report_date, by_code, source_status, False)


def _fetch_json(client: httpx.Client, url: str) -> list[dict[str, Any]]:
    response = client.get(url)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise ValueError("official risk feed did not return a list")
    return [dict(item) for item in payload if isinstance(item, dict)]


def _read_cache(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return None
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        return None
    return payload


def _cache_is_usable(payload: dict[str, Any], report_date: date) -> bool:
    if report_date < date.today():
        return True
    try:
        generated_at = datetime.fromisoformat(str(payload.get("generated_at") or ""))
        if generated_at.tzinfo is not None:
            now = datetime.now().astimezone()
        else:
            now = datetime.now()
        return now - generated_at <= timedelta(minutes=60)
    except (TypeError, ValueError):
        return False


def _stock_code(row: dict[str, Any]) -> str:
    return str(row.get("Code") or row.get("SecuritiesCompanyCode") or "").strip()


def _roc_date(value: Any) -> date | None:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) not in {7, 8}:
        return None
    try:
        if len(digits) == 7:
            year, month, day = int(digits[:3]) + 1911, int(digits[3:5]), int(digits[5:7])
        else:
            year, month, day = int(digits[:4]), int(digits[4:6]), int(digits[6:8])
        return date(year, month, day)
    except ValueError:
        return None


def _disposition_period(row: dict[str, Any]) -> tuple[date | None, date | None]:
    text = str(row.get("DispositionPeriod") or "")
    parts = re.split(r"[~～－—至迄-]", text)
    if len(parts) < 2:
        return (None, None)
    return (_roc_date(parts[0]), _roc_date(parts[-1]))


def _date_in_period(target: date, period: tuple[date | None, date | None]) -> bool:
    start, end = period
    return bool(start and end and start <= target <= end)
