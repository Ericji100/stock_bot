"""Point-in-time TWSE/TPEX stock-universe snapshots for dual-MA replays.

This module is deliberately sidecar-only.  It reads official exchange data,
builds market-membership intervals, and freezes immutable snapshots without
changing the live scanner's stock-list cache or command paths.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

import httpx

from stock_ai_bot.scanning.stock_scanner import (
    StockUniverseEntry,
    UNCLASSIFIED_INDUSTRY,
    _format_industry,
)


TAIPEI = ZoneInfo("Asia/Taipei")
SCHEMA_VERSION = 1
DEFAULT_SOURCE_ROOT = Path("data") / "dual_ma" / "universe_sources"
DEFAULT_SNAPSHOT_ROOT = Path("data") / "dual_ma" / "universe_snapshots"

TWSE_CURRENT_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
TPEX_CURRENT_URL = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O"
TWSE_DELISTED_URL = "https://www.twse.com.tw/rwd/zh/company/suspendListing?response=json"
TPEX_DELISTED_URL = "https://www.tpex.org.tw/www/zh-tw/company/deListed"
TWSE_NEW_LISTING_URL = "https://www.twse.com.tw/rwd/zh/company/newlisting?response=json"
TPEX_NEW_LISTING_URL = "https://www.tpex.org.tw/www/zh-tw/company/latest"
TWSE_MONTHLY_PRICE_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY"
TPEX_MONTHLY_PRICE_URL = "https://www.tpex.org.tw/www/zh-tw/afterTrading/tradingStock"

SOURCE_URLS = {
    "twse_current": TWSE_CURRENT_URL,
    "tpex_current": TPEX_CURRENT_URL,
    "twse_delisted": TWSE_DELISTED_URL,
    "tpex_delisted": TPEX_DELISTED_URL,
    "twse_new_listings": TWSE_NEW_LISTING_URL,
    "tpex_new_listings": TPEX_NEW_LISTING_URL,
}


class IncompleteHistoricalUniverseError(RuntimeError):
    """Raised when a strict as-of query could silently omit a past member."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _normalise_code(value: Any) -> str:
    code = str(value or "").strip()
    return code if re.fullmatch(r"\d{4,6}", code) else ""


def parse_exchange_date(value: Any) -> date | None:
    """Parse Gregorian or ROC exchange dates without guessing partial values."""
    raw = str(value or "").strip()
    if not raw:
        return None
    raw = raw.replace("年", "/").replace("月", "/").replace("日", "")
    raw = raw.replace(".", "/").replace("-", "/")
    parts = [part for part in raw.split("/") if part]
    if len(parts) == 1 and parts[0].isdigit():
        digits = parts[0]
        if len(digits) == 8:
            parts = [digits[:4], digits[4:6], digits[6:8]]
        elif len(digits) == 7:
            parts = [digits[:3], digits[3:5], digits[5:7]]
        else:
            return None
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        return None
    year, month, day = (int(part) for part in parts)
    if year < 1911:
        year += 1911
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _table_rows(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    tables = payload.get("tables") or []
    if not tables:
        return []
    table = tables[0]
    fields = [str(value) for value in table.get("fields") or []]
    return [dict(zip(fields, row)) for row in table.get("data") or []]


def _flat_rows(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    fields = [str(value) for value in payload.get("fields") or []]
    return [dict(zip(fields, row)) for row in payload.get("data") or []]


def _ordered_mappings(rows: Iterable[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    return tuple(dict(row) for row in rows)


def _trade_dates(rows: Iterable[Mapping[str, Any]]) -> list[date]:
    parsed_dates: list[date] = []
    for row in rows:
        raw_date = next(
            (
                value
                for key, value in row.items()
                if re.sub(r"\s+", "", str(key)) == "日期"
            ),
            None,
        )
        parsed = parse_exchange_date(raw_date)
        if parsed is not None:
            parsed_dates.append(parsed)
    return sorted(parsed_dates)


@dataclass(frozen=True)
class OfficialUniverseSources:
    collected_at: str
    coverage_start: date
    coverage_end: date
    twse_current: tuple[dict[str, Any], ...]
    tpex_current: tuple[dict[str, Any], ...]
    twse_delisted: tuple[dict[str, Any], ...]
    tpex_delisted: tuple[dict[str, Any], ...]
    twse_new_listings: tuple[dict[str, Any], ...]
    tpex_new_listings: tuple[dict[str, Any], ...]
    source_urls: dict[str, str] = field(default_factory=lambda: dict(SOURCE_URLS))
    schema_version: int = SCHEMA_VERSION

    def content_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "coverage_start": self.coverage_start.isoformat(),
            "coverage_end": self.coverage_end.isoformat(),
            "source_urls": dict(sorted(self.source_urls.items())),
            "twse_current": list(self.twse_current),
            "tpex_current": list(self.tpex_current),
            "twse_delisted": list(self.twse_delisted),
            "tpex_delisted": list(self.tpex_delisted),
            "twse_new_listings": list(self.twse_new_listings),
            "tpex_new_listings": list(self.tpex_new_listings),
        }

    @property
    def content_sha256(self) -> str:
        return _sha256(self.content_payload())

    @property
    def source_id(self) -> str:
        return f"dual-ma-universe-sources#{self.content_sha256[:16]}"

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.content_payload(),
            "source_id": self.source_id,
            "content_sha256": self.content_sha256,
            "collected_at": self.collected_at,
            "counts": {
                "twse_current": len(self.twse_current),
                "tpex_current": len(self.tpex_current),
                "twse_delisted": len(self.twse_delisted),
                "tpex_delisted": len(self.tpex_delisted),
                "twse_new_listings": len(self.twse_new_listings),
                "tpex_new_listings": len(self.tpex_new_listings),
            },
        }


def fetch_official_universe_sources(
    coverage_start: date,
    coverage_end: date,
    *,
    listing_archive_start_year: int = 1994,
    now: datetime | None = None,
    client: httpx.Client | None = None,
) -> OfficialUniverseSources:
    """Read official exchange sources; this never writes ``stock_list.json``."""
    if coverage_end < coverage_start:
        raise ValueError("coverage_end must not be before coverage_start")
    owns_client = client is None
    session = client or httpx.Client(
        timeout=30.0,
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0 dual-ma-historical-universe"},
    )
    try:
        twse_current_response = session.get(TWSE_CURRENT_URL)
        twse_current_response.raise_for_status()
        tpex_current_response = session.get(TPEX_CURRENT_URL)
        tpex_current_response.raise_for_status()
        twse_delisted_response = session.get(TWSE_DELISTED_URL)
        twse_delisted_response.raise_for_status()
        twse_new_response = session.get(TWSE_NEW_LISTING_URL)
        twse_new_response.raise_for_status()

        tpex_delisted: list[dict[str, Any]] = []
        for year in range(coverage_start.year, coverage_end.year + 1):
            delisted_response = session.post(
                TPEX_DELISTED_URL,
                data={
                    "code": "",
                    "date": str(year),
                    "reason": "-1",
                    "response": "json",
                    "paging-offset": "0",
                    "paging-size": "1000",
                },
            )
            delisted_response.raise_for_status()
            tpex_delisted.extend(_table_rows(delisted_response.json()))

        tpex_new_listings: list[dict[str, Any]] = []
        for year in range(min(listing_archive_start_year, coverage_start.year), coverage_end.year + 1):
            listing_response = session.post(
                TPEX_NEW_LISTING_URL,
                data={"code": "", "date": str(year), "response": "json"},
            )
            listing_response.raise_for_status()
            tpex_new_listings.extend(_table_rows(listing_response.json()))

        timestamp = now or datetime.now(TAIPEI)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=TAIPEI)
        return OfficialUniverseSources(
            collected_at=timestamp.isoformat(),
            coverage_start=coverage_start,
            coverage_end=coverage_end,
            twse_current=_ordered_mappings(twse_current_response.json()),
            tpex_current=_ordered_mappings(tpex_current_response.json()),
            twse_delisted=_ordered_mappings(_flat_rows(twse_delisted_response.json())),
            tpex_delisted=_ordered_mappings(tpex_delisted),
            twse_new_listings=_ordered_mappings(_flat_rows(twse_new_response.json())),
            tpex_new_listings=_ordered_mappings(tpex_new_listings),
        )
    finally:
        if owns_client:
            session.close()


def _freeze_json(payload: dict[str, Any], target: Path, expected_digest: str) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        existing = json.loads(target.read_text(encoding="utf-8"))
        if existing.get("content_sha256") != expected_digest:
            raise ValueError(f"content digest mismatch at existing path: {target}")
        return target
    temporary = target.with_suffix(f".tmp-{os.getpid()}")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, target)
    return target


def freeze_official_universe_sources(
    sources: OfficialUniverseSources,
    output_root: str | Path = DEFAULT_SOURCE_ROOT,
) -> Path:
    collected_date = datetime.fromisoformat(sources.collected_at).date()
    target = (
        Path(output_root)
        / f"{collected_date.year:04d}"
        / collected_date.isoformat()
        / f"{sources.source_id}.json"
    )
    return _freeze_json(sources.to_dict(), target, sources.content_sha256)


def load_official_universe_sources(path: str | Path) -> OfficialUniverseSources:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    sources = OfficialUniverseSources(
        collected_at=str(payload["collected_at"]),
        coverage_start=date.fromisoformat(str(payload["coverage_start"])),
        coverage_end=date.fromisoformat(str(payload["coverage_end"])),
        twse_current=_ordered_mappings(payload.get("twse_current") or []),
        tpex_current=_ordered_mappings(payload.get("tpex_current") or []),
        twse_delisted=_ordered_mappings(payload.get("twse_delisted") or []),
        tpex_delisted=_ordered_mappings(payload.get("tpex_delisted") or []),
        twse_new_listings=_ordered_mappings(payload.get("twse_new_listings") or []),
        tpex_new_listings=_ordered_mappings(payload.get("tpex_new_listings") or []),
        source_urls=dict(payload.get("source_urls") or SOURCE_URLS),
        schema_version=int(payload.get("schema_version") or SCHEMA_VERSION),
    )
    if payload.get("content_sha256") != sources.content_sha256:
        raise ValueError(f"universe source digest mismatch: {path}")
    return sources


@dataclass(frozen=True)
class OfficialMembershipEvidence:
    """Frozen official-price observations used only to prove start membership."""

    collected_at: str
    evidence_month: date
    observations: tuple[dict[str, Any], ...]
    schema_version: int = SCHEMA_VERSION

    def content_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "evidence_month": self.evidence_month.isoformat(),
            "observations": list(self.observations),
            "source_urls": {
                "twse": TWSE_MONTHLY_PRICE_URL,
                "tpex": TPEX_MONTHLY_PRICE_URL,
            },
        }

    @property
    def content_sha256(self) -> str:
        return _sha256(self.content_payload())

    @property
    def evidence_id(self) -> str:
        return f"dual-ma-membership-evidence#{self.content_sha256[:16]}"

    @property
    def first_trade_dates(self) -> dict[str, date]:
        return {
            str(item["symbol"]): date.fromisoformat(str(item["first_trade_date"]))
            for item in self.observations
            if item.get("status") == "FOUND" and item.get("first_trade_date")
        }

    def to_dict(self) -> dict[str, Any]:
        statuses: dict[str, int] = {}
        for item in self.observations:
            status = str(item.get("status") or "UNKNOWN")
            statuses[status] = statuses.get(status, 0) + 1
        return {
            **self.content_payload(),
            "evidence_id": self.evidence_id,
            "content_sha256": self.content_sha256,
            "collected_at": self.collected_at,
            "status_counts": dict(sorted(statuses.items())),
        }


def fetch_official_precoverage_trade_evidence(
    symbols: Iterable[str],
    coverage_start: date,
    *,
    now: datetime | None = None,
    client: httpx.Client | None = None,
) -> OfficialMembershipEvidence:
    """Query the month before coverage from official monthly-price reports."""
    previous_month_end = coverage_start.replace(day=1) - timedelta(days=1)
    evidence_month = previous_month_end.replace(day=1)
    owns_client = client is None
    session = client or httpx.Client(
        timeout=30.0,
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0 dual-ma-membership-evidence"},
    )
    observations: list[dict[str, Any]] = []
    try:
        for symbol in sorted({str(value).strip() for value in symbols if str(value).strip()}):
            code, separator, suffix = symbol.partition(".")
            market = "TWSE" if suffix == "TW" else "TPEX" if suffix == "TWO" else ""
            observation: dict[str, Any] = {
                "symbol": symbol,
                "code": code,
                "market": market,
                "requested_month": evidence_month.isoformat(),
                "status": "EMPTY",
                "first_trade_date": None,
                "row_count": 0,
            }
            if not separator or not _normalise_code(code) or not market:
                observation["status"] = "FAILED"
                observation["error"] = "unsupported symbol"
                observations.append(observation)
                continue
            try:
                if market == "TWSE":
                    response = session.get(
                        TWSE_MONTHLY_PRICE_URL,
                        params={
                            "date": evidence_month.strftime("%Y%m%d"),
                            "stockNo": code,
                            "response": "json",
                        },
                    )
                    response.raise_for_status()
                    payload = response.json()
                    rows = _flat_rows(payload)
                else:
                    response = session.post(
                        TPEX_MONTHLY_PRICE_URL,
                        data={
                            "code": code,
                            "date": evidence_month.strftime("%Y/%m/%d"),
                            "response": "json",
                        },
                    )
                    response.raise_for_status()
                    payload = response.json()
                    rows = _table_rows(payload)
                dates = _trade_dates(rows)
                observation["row_count"] = len(rows)
                observation["response_date"] = payload.get("date")
                observation["response_title"] = payload.get("title")
                if dates:
                    observation["status"] = "FOUND"
                    observation["first_trade_date"] = dates[0].isoformat()
            except Exception as exc:
                observation["status"] = "FAILED"
                observation["error"] = f"{type(exc).__name__}: {exc}"
            observations.append(observation)
    finally:
        if owns_client:
            session.close()

    timestamp = now or datetime.now(TAIPEI)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=TAIPEI)
    return OfficialMembershipEvidence(
        collected_at=timestamp.isoformat(),
        evidence_month=evidence_month,
        observations=tuple(observations),
    )


def freeze_official_membership_evidence(
    evidence: OfficialMembershipEvidence,
    output_root: str | Path = DEFAULT_SOURCE_ROOT,
) -> Path:
    collected_date = datetime.fromisoformat(evidence.collected_at).date()
    target = (
        Path(output_root)
        / f"{collected_date.year:04d}"
        / collected_date.isoformat()
        / f"{evidence.evidence_id}.json"
    )
    return _freeze_json(evidence.to_dict(), target, evidence.content_sha256)


def load_official_membership_evidence(path: str | Path) -> OfficialMembershipEvidence:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    evidence = OfficialMembershipEvidence(
        collected_at=str(payload["collected_at"]),
        evidence_month=date.fromisoformat(str(payload["evidence_month"])),
        observations=_ordered_mappings(payload.get("observations") or []),
        schema_version=int(payload.get("schema_version") or SCHEMA_VERSION),
    )
    if payload.get("content_sha256") != evidence.content_sha256:
        raise ValueError(f"membership evidence digest mismatch: {path}")
    return evidence


@dataclass(frozen=True)
class OfficialListingSupplement:
    """Small, auditable supplement for official archive rows absent from APIs."""

    collected_at: str
    observations: tuple[dict[str, Any], ...]
    schema_version: int = SCHEMA_VERSION

    def content_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "observations": list(self.observations),
        }

    @property
    def content_sha256(self) -> str:
        return _sha256(self.content_payload())

    @property
    def supplement_id(self) -> str:
        return f"dual-ma-listing-supplement#{self.content_sha256[:16]}"

    @property
    def listing_dates(self) -> dict[str, date]:
        return {
            str(item["symbol"]): date.fromisoformat(str(item["listed_on"]))
            for item in self.observations
            if item.get("symbol") and item.get("listed_on") and item.get("source_url")
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.content_payload(),
            "supplement_id": self.supplement_id,
            "content_sha256": self.content_sha256,
            "collected_at": self.collected_at,
            "observation_count": len(self.observations),
        }


def freeze_official_listing_supplement(
    supplement: OfficialListingSupplement,
    output_root: str | Path = DEFAULT_SOURCE_ROOT,
) -> Path:
    collected_date = datetime.fromisoformat(supplement.collected_at).date()
    target = (
        Path(output_root)
        / f"{collected_date.year:04d}"
        / collected_date.isoformat()
        / f"{supplement.supplement_id}.json"
    )
    return _freeze_json(supplement.to_dict(), target, supplement.content_sha256)


def load_official_listing_supplement(path: str | Path) -> OfficialListingSupplement:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    supplement = OfficialListingSupplement(
        collected_at=str(payload["collected_at"]),
        observations=_ordered_mappings(payload.get("observations") or []),
        schema_version=int(payload.get("schema_version") or SCHEMA_VERSION),
    )
    if payload.get("content_sha256") != supplement.content_sha256:
        raise ValueError(f"listing supplement digest mismatch: {path}")
    return supplement


@dataclass(frozen=True)
class MarketMembershipInterval:
    code: str
    symbol: str
    market: str
    name: str
    industry: str
    listed_on: date
    delisted_on: date | None
    listed_on_basis: str
    source_keys: tuple[str, ...]

    def contains(self, as_of_date: date) -> bool:
        return self.listed_on <= as_of_date and (
            self.delisted_on is None or as_of_date < self.delisted_on
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "symbol": self.symbol,
            "market": self.market,
            "name": self.name,
            "industry": self.industry,
            "listed_on": self.listed_on.isoformat(),
            "delisted_on": self.delisted_on.isoformat() if self.delisted_on else None,
            "interval_semantics": "[listed_on, delisted_on)",
            "listed_on_basis": self.listed_on_basis,
            "source_keys": list(self.source_keys),
        }


@dataclass(frozen=True)
class UnresolvedMembership:
    code: str
    symbol: str
    market: str
    name: str
    delisted_on: date
    reason: str
    source_key: str

    def may_affect(self, as_of_date: date, coverage_start: date) -> bool:
        return coverage_start <= as_of_date < self.delisted_on

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "symbol": self.symbol,
            "market": self.market,
            "name": self.name,
            "delisted_on": self.delisted_on.isoformat(),
            "reason": self.reason,
            "source_key": self.source_key,
        }


@dataclass(frozen=True)
class HistoricalUniverse:
    coverage_start: date
    coverage_end: date
    source_id: str
    intervals: tuple[MarketMembershipInterval, ...]
    unresolved: tuple[UnresolvedMembership, ...] = ()

    def unresolved_for(self, as_of_date: date) -> list[UnresolvedMembership]:
        return [item for item in self.unresolved if item.may_affect(as_of_date, self.coverage_start)]

    def members_as_of(self, as_of_date: date, *, strict: bool = True) -> list[StockUniverseEntry]:
        if not self.coverage_start <= as_of_date <= self.coverage_end:
            raise ValueError("as_of_date is outside frozen universe coverage")
        unresolved = self.unresolved_for(as_of_date)
        if strict and unresolved:
            labels = ", ".join(f"{item.code}.{item.market}" for item in unresolved[:8])
            extra = "..." if len(unresolved) > 8 else ""
            raise IncompleteHistoricalUniverseError(
                f"historical universe is incomplete for {as_of_date}: {labels}{extra}"
            )
        members = [item for item in self.intervals if item.contains(as_of_date)]
        by_code: dict[str, MarketMembershipInterval] = {}
        for item in members:
            if item.code in by_code:
                previous = by_code[item.code]
                raise ValueError(
                    f"overlapping market memberships for {item.code}: "
                    f"{previous.market} and {item.market}"
                )
            by_code[item.code] = item
        return [
            StockUniverseEntry(
                code=item.code,
                symbol=item.symbol,
                market=item.market,
                name=item.name,
                industry=item.industry,
            )
            for item in sorted(by_code.values(), key=lambda value: (value.market, value.code))
        ]

    @property
    def formal_backtest_ready(self) -> bool:
        return not self.unresolved

    def readiness(self) -> dict[str, Any]:
        return {
            "formal_backtest_ready": self.formal_backtest_ready,
            "interval_count": len(self.intervals),
            "unresolved_count": len(self.unresolved),
            "unresolved": [item.to_dict() for item in self.unresolved],
        }


def _current_records(sources: OfficialUniverseSources) -> list[MarketMembershipInterval]:
    intervals: list[MarketMembershipInterval] = []
    for market, suffix, rows in (
        ("TWSE", ".TW", sources.twse_current),
        ("TPEX", ".TWO", sources.tpex_current),
    ):
        for row in rows:
            if market == "TWSE":
                code = _normalise_code(row.get("公司代號"))
                name = str(row.get("公司簡稱") or row.get("公司名稱") or "").strip()
                industry = str(row.get("產業別") or UNCLASSIFIED_INDUSTRY).strip()
                listed_on = parse_exchange_date(row.get("上市日期"))
                source_key = "twse_current"
            else:
                code = _normalise_code(row.get("SecuritiesCompanyCode"))
                name = str(row.get("CompanyAbbreviation") or row.get("CompanyName") or "").strip()
                industry = str(row.get("SecuritiesIndustryCode") or UNCLASSIFIED_INDUSTRY).strip()
                listed_on = parse_exchange_date(row.get("DateOfListing"))
                source_key = "tpex_current"
            if not code or listed_on is None:
                continue
            industry = _format_industry(industry)
            intervals.append(
                MarketMembershipInterval(
                    code=code,
                    symbol=f"{code}{suffix}",
                    market=market,
                    name=name,
                    industry=industry or UNCLASSIFIED_INDUSTRY,
                    listed_on=listed_on,
                    delisted_on=None,
                    listed_on_basis="OFFICIAL_CURRENT_COMPANY_RECORD",
                    source_keys=(source_key,),
                )
            )
    return intervals


def _new_listing_index(sources: OfficialUniverseSources) -> dict[tuple[str, str], date]:
    result: dict[tuple[str, str], date] = {}
    for row in sources.twse_new_listings:
        code = _normalise_code(row.get("公司代號"))
        listed_on = parse_exchange_date(row.get("股票上市買賣日期"))
        if code and listed_on:
            result[("TWSE", code)] = listed_on
    for row in sources.tpex_new_listings:
        code = _normalise_code(row.get("股票代號"))
        listed_on = parse_exchange_date(row.get("上櫃日期"))
        if code and listed_on:
            result[("TPEX", code)] = listed_on
    return result


def _delisted_records(sources: OfficialUniverseSources) -> list[tuple[str, str, str, date, str]]:
    records: list[tuple[str, str, str, date, str]] = []
    for row in sources.twse_delisted:
        code = _normalise_code(row.get("上市編號"))
        delisted_on = parse_exchange_date(row.get("終止上市日期"))
        if code and delisted_on and sources.coverage_start <= delisted_on <= sources.coverage_end:
            records.append(("TWSE", code, str(row.get("公司名稱") or "").strip(), delisted_on, "twse_delisted"))
    for row in sources.tpex_delisted:
        code = _normalise_code(row.get("股票代號"))
        delisted_on = parse_exchange_date(row.get("終止上櫃日期"))
        if code and delisted_on and sources.coverage_start <= delisted_on <= sources.coverage_end:
            records.append(("TPEX", code, str(row.get("公司名稱") or "").strip(), delisted_on, "tpex_delisted"))
    return records


def _validate_non_overlapping(intervals: Iterable[MarketMembershipInterval]) -> None:
    by_code: dict[str, list[MarketMembershipInterval]] = {}
    for interval in intervals:
        by_code.setdefault(interval.code, []).append(interval)
    for code, values in by_code.items():
        ordered = sorted(values, key=lambda item: item.listed_on)
        for previous, current in zip(ordered, ordered[1:]):
            if previous.delisted_on is None or current.listed_on < previous.delisted_on:
                raise ValueError(
                    f"overlapping membership intervals for {code}: "
                    f"{previous.market} and {current.market}"
                )


def build_historical_universe(
    sources: OfficialUniverseSources,
    *,
    first_trade_dates: Mapping[str, date] | OfficialMembershipEvidence | None = None,
    supplemental_listing_dates: Mapping[str, date] | OfficialListingSupplement | None = None,
) -> HistoricalUniverse:
    """Build intervals, requiring evidence for delisted pre-coverage members.

    ``first_trade_dates`` keys are Yahoo-style symbols (for example
    ``2443.TW``).  A first observation on or before ``coverage_start`` proves
    only that the stock belonged to the starting universe; it is not presented
    as the original legal listing date.
    """
    if isinstance(first_trade_dates, OfficialMembershipEvidence):
        evidence = first_trade_dates.first_trade_dates
        evidence_digest = first_trade_dates.content_sha256
    else:
        evidence = dict(first_trade_dates or {})
        evidence_digest = _sha256(
            {key: value.isoformat() for key, value in sorted(evidence.items())}
        ) if evidence else None
    if isinstance(supplemental_listing_dates, OfficialListingSupplement):
        listing_supplement = supplemental_listing_dates.listing_dates
        supplement_digest = supplemental_listing_dates.content_sha256
    else:
        listing_supplement = dict(supplemental_listing_dates or {})
        supplement_digest = _sha256(
            {key: value.isoformat() for key, value in sorted(listing_supplement.items())}
        ) if listing_supplement else None
    intervals = _current_records(sources)
    current_keys = {(item.market, item.code, item.listed_on) for item in intervals}
    listing_index = _new_listing_index(sources)
    unresolved: list[UnresolvedMembership] = []

    for market, code, name, delisted_on, delisted_source in _delisted_records(sources):
        suffix = ".TW" if market == "TWSE" else ".TWO"
        symbol = f"{code}{suffix}"
        listed_on = listing_index.get((market, code))
        basis = "OFFICIAL_NEW_LISTING_RECORD"
        source_keys = (delisted_source, "twse_new_listings" if market == "TWSE" else "tpex_new_listings")
        if listed_on is None and symbol in listing_supplement:
            listed_on = listing_supplement[symbol]
            basis = "OFFICIAL_SUPPLEMENTAL_LISTING_RECORD"
            source_keys = (delisted_source, "official_listing_supplement")
        if listed_on is None:
            first_trade = evidence.get(symbol)
            if first_trade is not None and first_trade <= sources.coverage_start:
                listed_on = sources.coverage_start
                basis = "PRICE_OBSERVED_ON_OR_BEFORE_COVERAGE_START"
                source_keys = (delisted_source, "historical_price_cache")
        if listed_on is None:
            unresolved.append(
                UnresolvedMembership(
                    code=code,
                    symbol=symbol,
                    market=market,
                    name=name,
                    delisted_on=delisted_on,
                    reason="missing official listing date and no pre-coverage trade evidence",
                    source_key=delisted_source,
                )
            )
            continue
        if listed_on >= delisted_on:
            unresolved.append(
                UnresolvedMembership(
                    code=code,
                    symbol=symbol,
                    market=market,
                    name=name,
                    delisted_on=delisted_on,
                    reason="listing evidence is not before delisting date",
                    source_key=delisted_source,
                )
            )
            continue
        if (market, code, listed_on) in current_keys:
            continue
        intervals.append(
            MarketMembershipInterval(
                code=code,
                symbol=symbol,
                market=market,
                name=name,
                industry=UNCLASSIFIED_INDUSTRY,
                listed_on=listed_on,
                delisted_on=delisted_on,
                listed_on_basis=basis,
                source_keys=source_keys,
            )
        )

    _validate_non_overlapping(intervals)
    return HistoricalUniverse(
        coverage_start=sources.coverage_start,
        coverage_end=sources.coverage_end,
        source_id="+".join(
            part
            for part in (
                sources.source_id,
                f"membership-evidence#{evidence_digest[:16]}" if evidence_digest else "",
                f"listing-supplement#{supplement_digest[:16]}" if supplement_digest else "",
            )
            if part
        ),
        intervals=tuple(sorted(intervals, key=lambda item: (item.code, item.listed_on, item.market))),
        unresolved=tuple(sorted(unresolved, key=lambda item: (item.delisted_on, item.market, item.code))),
    )


def _snapshot_payload(universe: HistoricalUniverse, as_of_date: date) -> dict[str, Any]:
    members = universe.members_as_of(as_of_date, strict=True)
    content = {
        "schema_version": SCHEMA_VERSION,
        "as_of_date": as_of_date.isoformat(),
        "source_id": universe.source_id,
        "members": [
            {
                "code": item.code,
                "symbol": item.symbol,
                "market": item.market,
                "name": item.name,
                "industry": item.industry,
            }
            for item in members
        ],
    }
    digest = _sha256(content)
    return {
        **content,
        "snapshot_id": f"dual-ma-universe@{as_of_date.isoformat()}#{digest[:16]}",
        "content_sha256": digest,
        "member_count": len(members),
    }


def freeze_daily_universe_snapshot(
    universe: HistoricalUniverse,
    as_of_date: date,
    output_root: str | Path = DEFAULT_SNAPSHOT_ROOT,
) -> Path:
    payload = _snapshot_payload(universe, as_of_date)
    target = (
        Path(output_root)
        / f"{as_of_date.year:04d}"
        / as_of_date.isoformat()
        / f"{payload['snapshot_id']}.json"
    )
    return _freeze_json(payload, target, str(payload["content_sha256"]))
