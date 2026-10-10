"""Build the immutable 0050 2005--2026 causal research data package.

The complete 2005 history is warm-up only.  Formal use starts at the first
verified 2006 trading session and ends at 2026-09-30.  This producer neither
computes nor reads strategy performance.

Source policy:
* FinMind ``TaiwanStockPrice`` supplies unadjusted daily bars for 2005--2009.
* TWSE ``STOCK_DAY`` supplies unadjusted daily bars for 2010 onward.
* FinMind unadjusted prices cross-check the entire assembled series.
* TWSE ``FMSRFK`` monthly statistics cross-check 2005--2009.
* Vendor adjusted prices are archived for cross-validation only.
"""

from __future__ import annotations

import argparse
import calendar
import hashlib
import importlib.util
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from backtests.dual_ma.paths import DUAL_MA_DEV_ROOT, REPOSITORY_ROOT

ROOT = REPOSITORY_ROOT
BASE_SCRIPT = ROOT / "scripts" / "dual_ma_etf_0050_ten_year_package_v1.py"
_SPEC = importlib.util.spec_from_file_location("dual_ma_0050_ten_year_base", BASE_SCRIPT)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError("BASE_PACKAGE_SCRIPT_UNAVAILABLE")
base = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = base
_SPEC.loader.exec_module(base)

WORKDATA = DUAL_MA_DEV_ROOT
OUT = Path(
    os.environ.get(
        "DUAL_MA_0050_2006_PACKAGE_OUT",
        str(WORKDATA / "02-etf-0050-research-package-2005-2026-v1"),
    )
)
ADOPTED = WORKDATA / "04-etf-0050-research-package-2016-2026-v1"
CONTRACT = ROOT / "docs" / "strategies" / "dual_ma" / "0050-2006-2026-data-and-backtest-v1.md"

SCHEMA = "dual-ma-etf-0050-research-package-2005-2026-v1"
CONTRACT_SHA256 = "5103731DBAC279202C0D2D8018BD35AD048900192A3BCA5796D7F4572686FAFA"
ADOPTED_READY_SHA256 = "D55FD56FFC01F3F130A83FCDC26E4D5A02ACCC617E815528C3364AAF27E5F9DC"
ADOPTED_MANIFEST_SHA256 = "71D10A719A60F4D03D6D4CCBF3CC54FDCECC251037A61EF319D895B82DB1AD57"
CODE = "0050"
WARMUP_FROM = "2005-01-03"
WARMUP_THROUGH = "2005-12-30"
FORMAL_FROM = "2006-01-02"
THROUGH = "2026-09-30"
TWSE_DAILY_FROM = "2010-01"
TWSE_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY"
TWSE_LEGACY_URL = "https://www.twse.com.tw/exchangeReport/STOCK_DAY"
TWSE_MONTHLY_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/FMSRFK"
FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"
DIVIDEND_URL = "https://www.twse.com.tw/zh/ETFortune/dividendList"
UA = "stock_ai_bot-research/0050-2006-v1"
FINMIND_DATASETS = (
    "TaiwanStockPrice",
    "TaiwanStockPriceAdj",
    "TaiwanStockDividend",
    "TaiwanStockDividendResult",
    "TaiwanStockSplitPrice",
)


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest().upper()


def file_sha256(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def immutable_write(path: Path, raw: bytes) -> Path:
    if path.exists():
        if not path.is_file() or path.read_bytes() != raw:
            raise FileExistsError("IMMUTABLE_WRITE_CONFLICT:" + str(path))
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_bytes(raw)
    os.replace(temp, path)
    return path


def mutable_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_bytes(canonical(value))
    os.replace(temp, path)


def month_list(first: str, last: str) -> list[str]:
    return base.month_list(first, last)


def _assert_pins() -> None:
    if file_sha256(CONTRACT) != CONTRACT_SHA256:
        raise ValueError("CONTRACT_SHA_MISMATCH")
    if file_sha256(ADOPTED / "READY_FOR_02.json") != ADOPTED_READY_SHA256:
        raise ValueError("ADOPTED_READY_SHA_MISMATCH")
    if file_sha256(ADOPTED / "artifact_manifest.json") != ADOPTED_MANIFEST_SHA256:
        raise ValueError("ADOPTED_MANIFEST_SHA_MISMATCH")


def http_get(url: str, params: dict[str, Any]) -> tuple[int, bytes, str]:
    full_url = url + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(full_url, headers={"User-Agent": UA, "Accept": "application/json,text/html,*/*"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return int(getattr(response, "status", 200)), response.read(), full_url
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read() or b"", full_url


def archive_request(
    kind: str,
    key: str,
    url: str,
    params: dict[str, Any],
    *,
    require_json: bool = True,
    access_tier: str = "PUBLIC_ANONYMOUS",
) -> dict[str, Any]:
    raw_path = OUT / "sources" / kind / "raw" / (key + ".raw")
    receipt_path = OUT / "sources" / kind / "receipts" / (key + ".json")
    if raw_path.is_file() and receipt_path.is_file():
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if receipt.get("body_sha256") != file_sha256(raw_path) or receipt.get("status") != "OK":
            raise ValueError("SOURCE_RECEIPT_INVALID:" + kind + ":" + key)
        return receipt
    if receipt_path.is_file() and not raw_path.exists():
        failed_raw = receipt_path.read_bytes()
        failure_path = OUT / "sources" / kind / "failures" / (key + "#" + sha256_bytes(failed_raw)[:16] + ".json")
        immutable_write(failure_path, failed_raw)
        receipt_path.unlink()
    if raw_path.exists() or receipt_path.exists():
        raise ValueError("SOURCE_RAW_RECEIPT_PAIR_INCOMPLETE:" + kind + ":" + key)

    status, body, full_url = http_get(url, params)
    receipt: dict[str, Any] = {
        "kind": kind,
        "key": key,
        "url": url,
        "params": params,
        "full_url": full_url,
        "access_tier": access_tier,
        "http_status": status,
        "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
        "body_bytes": len(body),
        "body_sha256": sha256_bytes(body),
    }
    error: str | None = None
    if status != 200:
        error = "HTTP_" + str(status)
    elif not body:
        error = "EMPTY_RESPONSE"
    elif require_json:
        try:
            payload = json.loads(body.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = None
            error = "NON_JSON_RESPONSE"
        if isinstance(payload, dict) and payload.get("stat") not in {None, "OK", "ok"}:
            error = "SOURCE_STAT:" + str(payload.get("stat"))
        if isinstance(payload, dict) and payload.get("status") not in {None, 200, "200"}:
            error = "SOURCE_STATUS:" + str(payload.get("status"))
    if error:
        receipt.update({"status": "FAIL_CLOSED", "error": error})
        immutable_write(receipt_path, canonical(receipt))
        raise RuntimeError(error + ":" + kind + ":" + key)
    receipt["status"] = "OK"
    immutable_write(raw_path, body)
    immutable_write(receipt_path, canonical(receipt))
    return receipt


def copy_accepted(relative: str) -> None:
    source = ADOPTED / relative
    immutable_write(OUT / relative, source.read_bytes())


def _checkpoint(stage: str, completed: int, total: int, last_key: str | None) -> None:
    mutable_write(
        OUT / "checkpoint.json",
        {
            "schema_version": SCHEMA + "-checkpoint",
            "stage": stage,
            "completed_requests": completed,
            "total_requests": total,
            "last_key": last_key,
            "updated_at_utc": datetime.now(timezone.utc).isoformat(),
            "continuation_command": "python backtests/dual_ma/etf_0050_2006_data_package_v1.py all",
            "performance_read_or_computed": False,
        },
    )


def fetch_sources(stop_after: int | None = None) -> dict[str, Any]:
    _assert_pins()
    requests: list[tuple[str, str, str, dict[str, Any], bool, str]] = []
    for month in month_list("2010-01", "2015-12"):
        requests.append(("twse_stock_day", CODE + "_" + month, TWSE_LEGACY_URL, {"date": month.replace("-", "") + "01", "stockNo": CODE, "response": "json"}, True, "TWSE_PUBLIC"))
    for year in range(2005, 2010):
        requests.append(("twse_fmsrfk", CODE + "_" + str(year), TWSE_MONTHLY_URL, {"date": f"{year}0101", "stockNo": CODE, "response": "json"}, True, "TWSE_PUBLIC"))
    requests.append(("twse_etfortune_dividend_list", "0050_2005_2026", DIVIDEND_URL, {"startDate": "2005", "endDate": "2026", "stkNo": CODE}, False, "TWSE_PUBLIC"))
    for dataset in FINMIND_DATASETS:
        requests.append(("finmind", dataset + "_0050_20050101_20260930", FINMIND_URL, {"dataset": dataset, "data_id": CODE, "start_date": "2005-01-01", "end_date": THROUGH}, True, "PUBLIC_ANONYMOUS"))

    completed = 0
    total = len(requests)
    for kind, key, url, params, require_json, tier in requests:
        if key == "TaiwanStockPriceAdj_0050_20050101_20260930":
            known_failure = OUT / "sources" / kind / "receipts" / (key + ".json")
            if known_failure.is_file():
                payload = json.loads(known_failure.read_text(encoding="utf-8"))
                if payload.get("status") == "FAIL_CLOSED" and payload.get("error") == "HTTP_400":
                    completed += 1
                    _checkpoint("FETCHING", completed, total, key)
                    continue
        try:
            archive_request(kind, key, url, params, require_json=require_json, access_tier=tier)
        except RuntimeError:
            if key != "TaiwanStockPriceAdj_0050_20050101_20260930":
                raise
        completed += 1
        _checkpoint("FETCHING", completed, total, key)
        if stop_after and completed >= stop_after:
            return {"status": "CHECKPOINT", "completed_requests": completed, "total_requests": total}
        time.sleep(0.1)

    for folder in ("raw", "receipts"):
        suffix = ".raw" if folder == "raw" else ".json"
        for source in sorted((ADOPTED / "sources" / "twse_stock_day" / folder).glob("0050_*" + suffix)):
            copy_accepted("sources/twse_stock_day/" + folder + "/" + source.name)
    for name in ("twse_etfortune_announcement_20250509", "twse_etfortune_announcement_20250617", "isin_list_mode2"):
        for filename in ("page.raw", "receipt.json"):
            copy_accepted("sources/" + name + "/" + filename)
    for folder, suffix in (("raw", ".raw"), ("receipts", ".json")):
        filename = "TaiwanStockPriceAdj_0050_20160101_20260930" + suffix
        copy_accepted("sources/finmind/" + folder + "/" + filename)

    capability = {
        "schema_version": SCHEMA + "-source-capability",
        "probe_results": [
            {"source": "TWSE_STOCK_DAY", "range": "2005", "result": "UNAVAILABLE_BEFORE_2010", "source_status": "查詢日期小於99年1月4日，請重新查詢!"},
            {"source": "TWSE_FMSRFK", "range": "2005-2009", "result": "AVAILABLE_MONTHLY_AGGREGATES"},
            {"source": "FinMind_TaiwanStockPrice", "range": "2005-01-03..2026-09-30", "result": "AVAILABLE", "access_tier": "PUBLIC_ANONYMOUS"},
            {"source": "FinMind_TaiwanStockPriceAdj", "range": "2005-01-01..2026-09-30", "result": "HTTP_400_PUBLIC_TIER", "impact": "NON_BLOCKING_CROSS_VALIDATION_ONLY"},
            {"source": "FinMind_TaiwanStockPriceAdj", "range": "2016-01-04..2026-09-30", "result": "REUSED_FROM_ADOPTED_IMMUTABLE_PACKAGE"},
        ],
        "semantic_switches": [
            {"through": "2009-12-31", "execution_source": "FinMind_TaiwanStockPrice", "official_validation": "TWSE_FMSRFK_MONTHLY"},
            {"from": "2010-01-04", "execution_source": "TWSE_STOCK_DAY", "validation": "FinMind_TaiwanStockPrice"},
        ],
        "paid_tier_used": False,
        "reason": "No FinMind token was available; the public endpoint returned the required archival datasets.",
    }
    immutable_write(OUT / "sources" / "source_capability_probe.json", canonical(capability))
    _checkpoint("SOURCES_READY", completed, total, requests[-1][1])
    return {"status": "SOURCES_READY", "completed_requests": completed, "total_requests": total}


def load_finmind_payload(dataset: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    key = dataset + "_0050_20050101_20260930"
    raw_path = OUT / "sources" / "finmind" / "raw" / (key + ".raw")
    receipt_path = OUT / "sources" / "finmind" / "receipts" / (key + ".json")
    raw = raw_path.read_bytes()
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if receipt.get("body_sha256") != sha256_bytes(raw) or receipt.get("status") != "OK":
        raise ValueError("FINMIND_SOURCE_INVALID:" + dataset)
    payload = json.loads(raw.decode("utf-8-sig"))
    if payload.get("status") not in {200, "200"} or not isinstance(payload.get("data"), list):
        raise ValueError("FINMIND_PAYLOAD_INVALID:" + dataset)
    return payload["data"], receipt


def load_adopted_adjusted_payload() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    key = "TaiwanStockPriceAdj_0050_20160101_20260930"
    raw_path = OUT / "sources" / "finmind" / "raw" / (key + ".raw")
    receipt_path = OUT / "sources" / "finmind" / "receipts" / (key + ".json")
    raw = raw_path.read_bytes()
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if receipt.get("body_sha256") != sha256_bytes(raw):
        raise ValueError("ADOPTED_ADJUSTED_SOURCE_INVALID")
    payload = json.loads(raw.decode("utf-8-sig"))
    return payload["data"], receipt


def normalize_finmind_price(rows: list[dict[str, Any]], dataset: str) -> list[dict[str, Any]]:
    return base.normalize_finmind_price(rows, dataset)


def _official_daily_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for month in month_list("2010-01", "2026-09"):
        key = CODE + "_" + month
        raw_path = OUT / "sources" / "twse_stock_day" / "raw" / (key + ".raw")
        receipt_path = OUT / "sources" / "twse_stock_day" / "receipts" / (key + ".json")
        raw = raw_path.read_bytes()
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        expected = receipt.get("body_sha256") or receipt.get("sha256")
        if receipt.get("http_status") != 200 or expected != sha256_bytes(raw):
            raise ValueError("TWSE_MONTH_RECEIPT_INVALID:" + key)
        rows.extend(base.parse_twse_month(raw, sha256_bytes(raw), key))
    rows.sort(key=lambda item: item["date"])
    return [item for item in rows if item["date"] <= THROUGH]


def assemble_daily_rows(finmind_prices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    finmind_raw, receipt = load_finmind_payload("TaiwanStockPrice")
    raw_sha = receipt["body_sha256"]
    by_date = {item["date"]: item for item in finmind_raw}
    early = []
    for normalized in finmind_prices:
        if normalized["date"] >= "2010-01-01":
            continue
        source = by_date[normalized["date"]]
        early.append(
            {
                "date": normalized["date"], "code": CODE, "market": "TWSE",
                "open": normalized["open"], "high": normalized["high"], "low": normalized["low"],
                "close": normalized["close"], "volume": normalized["volume"],
                "amount": float(source.get("Trading_money", 0)), "trades": float(source.get("Trading_turnover", 0)),
                "note": "", "source": "FinMind_TaiwanStockPrice", "source_key": "TaiwanStockPrice_0050_20050101_20260930",
                "raw_response_sha256": raw_sha,
            }
        )
    rows = sorted(early + _official_daily_rows(), key=lambda item: item["date"])
    dates = [item["date"] for item in rows]
    if len(dates) != len(set(dates)):
        raise ValueError("DUPLICATE_DAILY_DATES")
    if not rows or rows[0]["date"] != WARMUP_FROM or rows[-1]["date"] != THROUGH:
        raise ValueError("DAILY_RANGE_UNEXPECTED")
    return rows


def parse_twse_dividends() -> list[dict[str, Any]]:
    key = "0050_2005_2026"
    raw_path = OUT / "sources" / "twse_etfortune_dividend_list" / "raw" / (key + ".raw")
    receipt_path = OUT / "sources" / "twse_etfortune_dividend_list" / "receipts" / (key + ".json")
    raw = raw_path.read_bytes()
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if receipt.get("body_sha256") != sha256_bytes(raw):
        raise ValueError("DIVIDEND_RECEIPT_SHA_MISMATCH")
    html = raw.decode("utf-8", "replace")
    events = []
    for block in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        cells = [base.clean_html(cell) for cell in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", block, re.S)]
        if not cells or cells[0].strip() != CODE:
            continue
        if len(cells) < 6:
            raise ValueError("DIVIDEND_FIELD_CHANGE")
        ex_date, base_date, payment_date = base.roc_to_iso(cells[2]), base.roc_to_iso(cells[3]), base.roc_to_iso(cells[4])
        if not ex_date or not payment_date:
            raise ValueError("DIVIDEND_DATE_UNPARSEABLE")
        events.append(
            {
                "code": CODE, "name": cells[1], "ex_date": ex_date, "base_date": base_date,
                "payment_date": payment_date, "amount_per_unit_original": float(cells[5]),
                "amount_per_unit_raw_text": cells[5],
                "source": {"path": "sources/twse_etfortune_dividend_list/raw/" + key + ".raw", "sha256": sha256_bytes(raw), "url": receipt.get("full_url")},
            }
        )
    events.sort(key=lambda item: item["ex_date"])
    if not events:
        raise ValueError("DIVIDEND_EVENTS_EMPTY")
    return events


def monthly_official_validation(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_month: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if row["date"] >= "2010-01-01":
            continue
        by_month.setdefault(row["date"][:7], []).append(row)
    comparisons = []
    mismatches = []
    for year in range(2005, 2010):
        key = CODE + "_" + str(year)
        raw_path = OUT / "sources" / "twse_fmsrfk" / "raw" / (key + ".raw")
        receipt = json.loads((OUT / "sources" / "twse_fmsrfk" / "receipts" / (key + ".json")).read_text(encoding="utf-8"))
        raw = raw_path.read_bytes()
        if receipt.get("body_sha256") != sha256_bytes(raw):
            raise ValueError("FMSRFK_RECEIPT_SHA_MISMATCH:" + key)
        payload = json.loads(raw.decode("utf-8-sig"))
        if payload.get("stat") != "OK":
            raise ValueError("FMSRFK_STATUS:" + key)
        fields = payload.get("fields") or []
        index = {field: i for i, field in enumerate(fields)}
        required = {"年度", "月份", "最高價", "最低價", "成交金額(A)", "成交股數(B)"}
        if not required.issubset(index):
            raise ValueError("FMSRFK_FIELD_CHANGE:" + key)
        for item in payload.get("data") or []:
            month = f"{int(str(item[index['年度']]).strip()) + 1911:04d}-{int(str(item[index['月份']]).strip()):02d}"
            daily = by_month.get(month, [])
            if not daily:
                mismatches.append({"month": month, "reason": "NO_DAILY_ROWS"})
                continue
            number = lambda value: float(str(value).replace(",", "").strip())
            official = {
                "high": number(item[index["最高價"]]), "low": number(item[index["最低價"]]),
                "amount": number(item[index["成交金額(A)"]]), "volume": number(item[index["成交股數(B)"]]),
            }
            assembled = {
                "high": max(float(r["high"]) for r in daily), "low": min(float(r["low"]) for r in daily),
                "amount": sum(float(r["amount"]) for r in daily), "volume": sum(float(r["volume"]) for r in daily),
            }
            differences = {field: {"official": official[field], "assembled": assembled[field]} for field in official if abs(official[field] - assembled[field]) > 0.01}
            record = {"month": month, "trading_days": len(daily), "official": official, "assembled": assembled, "differences": differences}
            comparisons.append(record)
            if differences:
                mismatches.append(record)
    return {
        "schema_version": SCHEMA + "-twse-fmsrfk-validation", "months_checked": len(comparisons),
        "mismatch_count": len(mismatches), "mismatches": mismatches, "comparisons": comparisons,
        "policy": "FIRST_PARTY_MONTHLY_AGGREGATE_VALIDATION_FOR_FINMIND_2005_2009_DAILY_RAW",
    }


def _configure_base() -> None:
    base.OUT = OUT
    base.V3 = ADOPTED
    base.CONTRACT = CONTRACT
    base.CONTRACT_SHA256 = CONTRACT_SHA256
    base.SCHEMA = SCHEMA
    base.WARMUP_FROM = WARMUP_FROM
    base.WARMUP_THROUGH = WARMUP_THROUGH
    base.FORMAL_FROM = FORMAL_FROM
    base.THROUGH = THROUGH
    base.UA = UA
    base.DIVIDEND_URL = DIVIDEND_URL

    def raw_bar(row: dict[str, Any], fetched_at: datetime, snapshot_sha256: str):
        source_name = row["source"]
        endpoint = FINMIND_URL if source_name.startswith("FinMind") else TWSE_URL
        segment = "finmind-public-2005-2009-v1" if row["date"] < "2010-01-01" else ("twse-stock-day-2010-2015-v1" if row["date"] < "2016-01-01" else "twse-stock-day-adopted-2016-2026")
        payload = {
            "instrument_id": base.INSTRUMENT_ID, "symbol": base.SYMBOL, "exchange": "TWSE",
            "trade_date": date.fromisoformat(row["date"]), "raw_open": float(row["open"]),
            "raw_high": float(row["high"]), "raw_low": float(row["low"]), "raw_close": float(row["close"]),
            "raw_volume": float(row["volume"]), "source_name": source_name, "source_endpoint": endpoint,
            "source_market": "TWSE", "source_segment_id": segment, "fetched_at": fetched_at,
            "query_from": date.fromisoformat(row["date"][:7] + "-01"),
            "query_to": date(int(row["date"][:4]), int(row["date"][5:7]), calendar.monthrange(int(row["date"][:4]), int(row["date"][5:7]))[1]),
            "raw_response_sha256": str(row["raw_response_sha256"]).lower(), "snapshot_id": "0050-2005-2026-raw-v1",
            "snapshot_sha256": snapshot_sha256, "mainboard_start": date(2003, 6, 30),
        }
        row_hash_payload = {k: v for k, v in payload.items() if k not in {"fetched_at", "query_from", "query_to", "snapshot_sha256"}}
        row_hash_payload.update({"adjusted": False, "execution_price_basis": "UNADJUSTED_MAINBOARD_OHLCV"})
        payload["row_sha256"] = base.canonical_sha256(row_hash_payload)
        bar = base.RawDailyBar(**payload)
        bar.validate(source_lineage_prevalidated=True)
        return bar

    base.raw_bar = raw_bar


def _event_payload(events: list[Any]) -> list[dict[str, Any]]:
    result = []
    for event in events:
        item = asdict(event)
        item["action_type"] = event.action_type.value
        item["action_kind"] = event.action_kind.value
        for key, value in list(item.items()):
            if isinstance(value, (date, datetime)):
                item[key] = value.isoformat()
        result.append(item)
    return result


def _compare_prices(assembled: list[dict[str, Any]], finmind: list[dict[str, Any]]) -> dict[str, Any]:
    result = base.compare_prices(assembled, finmind)
    result["policy"] = "FINMIND_IS_EXECUTION_SOURCE_2005_2009_AND_CROSS_VALIDATION_ONLY_2010_2026"
    return result


def _accepted_overlap(rows: list[dict[str, Any]], action_payload: list[dict[str, Any]]) -> dict[str, Any]:
    old_rows = json.loads((ADOPTED / "data" / CODE / "daily_raw.json").read_text(encoding="utf-8"))["rows"]
    by_new = {row["date"]: row for row in rows}
    price_mismatches = []
    for old in old_rows:
        current = by_new.get(old["date"])
        fields = [field for field in ("open", "high", "low", "close", "volume", "amount", "trades", "note") if current is None or current.get(field) != old.get(field)]
        if fields:
            price_mismatches.append({"date": old["date"], "fields": fields})
    old_events = json.loads((ADOPTED / "events" / "0050_corporate_action_events.json").read_text(encoding="utf-8"))
    new_events = [event for event in action_payload if event["effective_date"] >= "2016-01-01"]
    comparable_fields = ("action_id", "action_type", "effective_date", "ex_date", "cash_dividend", "split_ratio", "share_price_factor", "share_volume_factor")
    compact_old = [{k: e.get(k) for k in comparable_fields} for e in old_events]
    compact_new = [{k: e.get(k) for k in comparable_fields} for e in new_events]
    return {
        "schema_version": SCHEMA + "-adopted-overlap", "coverage": "2016-01-04..2026-09-30",
        "price_rows_checked": len(old_rows), "price_mismatch_count": len(price_mismatches), "price_mismatches": price_mismatches,
        "event_rows_checked": len(compact_old), "event_mismatch_count": 0 if compact_old == compact_new else 1,
        "event_mismatches": [] if compact_old == compact_new else [{"reason": "COMPACT_EVENT_SEQUENCE_DIFFERS", "adopted": compact_old, "new": compact_new}],
        "adopted_ready_sha256": file_sha256(ADOPTED / "READY_FOR_02.json"),
        "adopted_manifest_sha256": file_sha256(ADOPTED / "artifact_manifest.json"),
    }


def source_master_table() -> dict[str, Any]:
    entries = []
    for path in sorted((OUT / "sources").rglob("*")):
        if path.is_file() and path.name != "source_master_table.json":
            raw = path.read_bytes()
            entries.append({"relative_path": path.relative_to(OUT).as_posix(), "bytes": len(raw), "sha256": sha256_bytes(raw)})
    return {"schema_version": SCHEMA + "-source-master", "entry_count": len(entries), "entries": entries}


def build() -> dict[str, Any]:
    _assert_pins()
    _configure_base()
    finmind_raw, finmind_receipt = load_finmind_payload("TaiwanStockPrice")
    adjusted_raw, _ = load_adopted_adjusted_payload()
    finmind_prices = normalize_finmind_price(finmind_raw, "TaiwanStockPrice")
    finmind_adjusted = normalize_finmind_price(adjusted_raw, "TaiwanStockPriceAdj")
    rows = assemble_daily_rows(finmind_prices)
    if len(rows) != len(finmind_prices):
        raise ValueError("ASSEMBLED_AND_FINMIND_DATE_COUNT_DIFFER")
    expected_dates = [row["date"] for row in finmind_prices]
    if [row["date"] for row in rows] != expected_dates:
        raise ValueError("ASSEMBLED_AND_FINMIND_CALENDAR_DIFFER")
    if FORMAL_FROM not in set(expected_dates):
        raise ValueError("FORMAL_START_NOT_PRESENT")

    dividends = parse_twse_dividends()
    finmind_dividend, _ = load_finmind_payload("TaiwanStockDividend")
    finmind_dividend_result, _ = load_finmind_payload("TaiwanStockDividendResult")
    finmind_split, _ = load_finmind_payload("TaiwanStockSplitPrice")
    split_rows = [item for item in finmind_split if str(item.get("stock_id") or item.get("data_id") or CODE) == CODE]
    receipt = json.loads((OUT / "sources" / "twse_etfortune_dividend_list" / "receipts" / "0050_2005_2026.json").read_text(encoding="utf-8"))
    fetched_at = datetime.fromisoformat(receipt["retrieved_at_utc"])
    action_events, split = base.build_action_events(rows, dividends, fetched_at)
    action_payload = _event_payload(action_events)

    actions_sha = base.canonical_sha256(action_payload)
    ledger_core = {
        "schema_version": base.ACTION_LEDGER_SCHEMA, "coverage": {"from": WARMUP_FROM, "through": THROUGH},
        "status": "PASS", "source_names": ["TWSE_ETFORTUNE_DIVIDEND_LIST", "TWSE_ETFORTUNE_ANNOUNCEMENT"],
        "events_path": "0050_corporate_action_events.json", "events_sha256": actions_sha,
        "event_count": len(action_payload), "technical_resolution": {"status": "PASS", "unresolved_event_ids": []},
        "position_lifecycle": {"status": "PASS", "quarantined_event_ids": []},
    }
    ledger_sha = base.canonical_sha256(ledger_core)
    ledger = {**ledger_core, "snapshot_id": "0050-corporate-action-ledger-2005-2026#" + ledger_sha[:16], "content_sha256": ledger_sha}
    immutable_write(OUT / "events" / "0050_corporate_action_events.json", canonical(action_payload))
    immutable_write(OUT / "events" / "0050_corporate_action_ledger.json", canonical(ledger))
    immutable_write(OUT / "events" / "0050_dividends.json", canonical({
        "schema_version": SCHEMA + "-dividends", "code": CODE, "events": dividends,
        "twse_event_count": len(dividends), "finmind_dividend_rows": len(finmind_dividend),
        "finmind_dividend_result_rows": len(finmind_dividend_result),
        "historical_amount_policy": "KEEP_EVENT_TIME_ORIGINAL_PER_UNIT_AMOUNT_NEVER_POST_SPLIT_REWRITE",
    }))
    immutable_write(OUT / "events" / "0050_split.json", canonical({"schema_version": SCHEMA + "-split", "code": CODE, **split, "finmind_cross_validation_rows": split_rows}))
    immutable_write(OUT / "data" / CODE / "daily_raw.json", canonical({
        "schema_version": SCHEMA + "-daily-raw", "code": CODE,
        "execution_price_policy": "RAW_UNADJUSTED_FINMIND_2005_2009_THEN_TWSE_STOCK_DAY_2010_2026",
        "warmup": {"from": WARMUP_FROM, "through": WARMUP_THROUGH, "performance_allowed": False},
        "formal": {"from": FORMAL_FROM, "through": THROUGH}, "rows": rows,
    }))

    price_cross = _compare_prices(rows, finmind_prices)
    monthly_cross = monthly_official_validation(rows)
    overlap = _accepted_overlap(rows, action_payload)
    adjusted_cross = {
        "schema_version": SCHEMA + "-adjusted-cross-validation", "dataset": "TaiwanStockPriceAdj",
        "rows": len(finmind_adjusted), "from": finmind_adjusted[0]["date"], "through": finmind_adjusted[-1]["date"],
        "policy": "CROSS_VALIDATION_ONLY_NOT_EXECUTION_AND_NOT_UNVERIFIED_SIGNAL_INPUT",
        "coverage_limitation": "2005-2015 adjusted vendor series unavailable on current public tier; not required for execution or causal technical construction",
        "raw_source": "sources/finmind/raw/TaiwanStockPriceAdj_0050_20160101_20260930.raw",
    }
    immutable_write(OUT / "validation" / "finmind_price_cross_validation.json", canonical(price_cross))
    immutable_write(OUT / "validation" / "twse_fmsrfk_2005_2009_validation.json", canonical(monthly_cross))
    immutable_write(OUT / "validation" / "finmind_adjusted_cross_validation.json", canonical(adjusted_cross))
    immutable_write(OUT / "validation" / "adopted_2016_2026_overlap.json", canonical(overlap))

    raw_contract_rows = [{
        "date": row["date"],
        "raw_row_sha256": base.canonical_sha256({field: row[field] for field in ("date", "open", "high", "low", "close", "volume", "raw_response_sha256")}),
        "source_segment_id": "finmind-public-2005-2009-v1" if row["date"] < "2010-01-01" else ("twse-stock-day-2010-2015-v1" if row["date"] < "2016-01-01" else "twse-stock-day-adopted-2016-2026"),
    } for row in rows]
    immutable_write(OUT / "factors" / "0050_causal_technical_contract.json", canonical({
        "schema_version": SCHEMA + "-causal-technical-contract", "contract_version": base.CONTRACT_VERSION,
        "code": CODE, "raw_rows": raw_contract_rows, "action_ledger": "events/0050_corporate_action_ledger.json",
        "consumer_rule": "FOR_EACH_AS_OF_REBUILD_HISTORY_USING_ONLY_TRADE_DATE_LT_EVENT_DATE_LE_AS_OF_AND_AVAILABLE_ON_LE_AS_OF",
        "same_day_anchor": "TECHNICAL_OHLCV_EQUALS_RAW_OHLCV_ON_AS_OF_DATE", "vendor_adjusted_policy": "CROSS_VALIDATION_ONLY",
        "cash_and_share_transforms_separate": True, "future_event_invariance_required": True,
        "adapter": "backtests/dual_ma/etf_0050_2006_data_package_v1.py::technical_rows_as_of",
    }))
    technical_snapshots = {}
    for as_of in (FORMAL_FROM, "2016-01-04", "2025-06-10", "2025-06-18", THROUGH):
        technical_rows = base.technical_rows_as_of(rows, action_events, as_of, fetched_at)
        relative = "technical/as_of_" + as_of + ".json"
        immutable_write(OUT / relative, canonical({"schema_version": SCHEMA + "-causal-technical-as-of", "contract_version": base.CONTRACT_VERSION, "code": CODE, "as_of_date": as_of, "rows": technical_rows}))
        technical_snapshots[as_of] = relative

    warmup_fixture = {
        "schema_version": SCHEMA + "-fixture-2005-2006-boundary", "warmup_from": WARMUP_FROM,
        "warmup_through": WARMUP_THROUGH, "formal_from": FORMAL_FROM,
        "performance_or_transactions_before_formal_from": "PROHIBITED",
        "rows": [row for row in rows if "2005-12-20" <= row["date"] <= "2006-01-10"],
    }
    immutable_write(OUT / "fixtures" / "warmup_to_formal_2005_2006.json", canonical(warmup_fixture))
    copy_accepted("fixtures/split_crossing_2025.json")

    gaps = []
    if monthly_cross["mismatch_count"]:
        gaps.append({"id": "EARLY_OFFICIAL_MONTHLY_MISMATCH", "severity": "HOLD", "detail": monthly_cross["mismatches"]})
    if price_cross["mismatch_count"]:
        gaps.append({"id": "DAILY_SOURCE_MISMATCH", "severity": "HOLD", "detail": price_cross["mismatches"][:100]})
    if overlap["price_mismatch_count"] or overlap["event_mismatch_count"]:
        gaps.append({"id": "ADOPTED_OVERLAP_MISMATCH", "severity": "HOLD", "detail": overlap})
    checks = {
        "range_exact": rows[0]["date"] == WARMUP_FROM and rows[-1]["date"] == THROUGH,
        "calendar_equal_to_finmind": [r["date"] for r in rows] == expected_dates,
        "formal_start_verified": FORMAL_FROM == next(d for d in expected_dates if d >= "2006-01-01"),
        "twse_monthly_2005_2009_exact": monthly_cross["mismatch_count"] == 0 and monthly_cross["months_checked"] == 60,
        "finmind_daily_cross_exact": price_cross["mismatch_count"] == 0 and not price_cross["twse_only_dates"] and not price_cross["finmind_only_dates"],
        "adopted_price_overlap_exact": overlap["price_mismatch_count"] == 0,
        "adopted_event_overlap_exact": overlap["event_mismatch_count"] == 0,
        "dividend_cross_count_equal": len(dividends) == len(finmind_dividend) == len(finmind_dividend_result),
        "split_first_party_verified": split.get("status") == "FIRST_PARTY_VERIFIED" and len(split_rows) == 1,
        "warmup_excluded": WARMUP_THROUGH < FORMAL_FROM,
    }
    coverage = {
        "schema_version": SCHEMA + "-coverage", "code": CODE,
        "warmup": {"from": WARMUP_FROM, "through": WARMUP_THROUGH, "trading_days": sum(r["date"] < "2006-01-01" for r in rows), "performance_allowed": False},
        "formal": {"from": FORMAL_FROM, "through": THROUGH, "trading_days": sum(r["date"] >= FORMAL_FROM for r in rows), "partial_2026": True},
        "total_trading_days": len(rows), "finmind_price_rows": len(finmind_prices), "finmind_adjusted_rows": len(finmind_adjusted),
        "twse_dividend_events": len(dividends), "corporate_action_events": len(action_payload), "checks": checks,
        "named_gaps": gaps, "status": "PASS" if all(checks.values()) and not gaps else "PARTIAL_HOLD",
        "research_only": True, "performance_read_or_computed": False,
    }
    immutable_write(OUT / "coverage.json", canonical(coverage))
    immutable_write(OUT / "gaps.json", canonical({"schema_version": SCHEMA + "-gaps", "status": coverage["status"], "gaps": gaps}))
    immutable_write(OUT / "sources" / "source_master_table.json", canonical(source_master_table()))

    summary = "\n".join([
        "# 0050 2005～2026 因果研究資料包 v1", "",
        f"- 原價交易日：{len(rows)} 筆（{WARMUP_FROM}～{THROUGH}）。2005 全年只暖機；正式起日為經驗證的 {FORMAL_FROM}。",
        "- 2005～2009 日線來自 FinMind 公開未還原價；TWSE 在該期間不提供 STOCK_DAY，改以第一方 FMSRFK 月統計逐月驗證。",
        "- 2010～2026 日線來自 TWSE STOCK_DAY；2016～2026 沿用已採納資料包的原始 bytes 與 receipt。",
        f"- 官方配息 {len(dividends)} 筆；FinMind Dividend／DividendResult 各 {len(finmind_dividend)}／{len(finmind_dividend_result)} 筆交叉驗證。",
        "- 調整價只作交叉驗證；成交永遠使用未還原原價。技術序列由 raw＋當時已知事件逐 as-of 因果重建。",
        "- 2025-06-18 4:1 分割沿用已驗第一方公告；分割為零交易，股數 ×4，單位成本與防守價 ÷4。",
        f"- 2016～2026 舊包重疊：價格差異 {overlap['price_mismatch_count']}，事件差異 {overlap['event_mismatch_count']}。",
        f"- 資料候選狀態：{coverage['status']}；尚待 00 嚴格驗收，未授權正式績效。", "",
        "## 精確續跑", "", "```powershell", "python backtests/dual_ma/etf_0050_2006_data_package_v1.py all", "python backtests/dual_ma/etf_0050_2006_data_package_v1.py validate-only", "```", "",
    ])
    immutable_write(OUT / "summary_zh_TW.md", summary.encode("utf-8"))
    tests_result_path = OUT / "tests_result.txt"
    tests_result = (
        tests_result_path.read_bytes()
        if tests_result_path.is_file()
        else os.environ.get("DUAL_MA_0050_2006_TEST_RESULT", "UNRECORDED").encode("utf-8")
    )
    immutable_write(tests_result_path, tests_result)

    gate = {
        "schema_version": SCHEMA + "-data-qa-gate", "status": "STOP_WAIT_00_DATA_QA",
        "data_complete_candidate": all(checks.values()) and not gaps, "ready_for_performance": False,
        "reason": "00 must explicitly adopt this data package before Phase 2.", "checks": checks, "named_gaps": gaps,
        "contract": {"path": str(CONTRACT), "sha256": CONTRACT_SHA256},
        "date_range": {"warmup_from": WARMUP_FROM, "formal_from": FORMAL_FROM, "through": THROUGH},
        "consumer_entry": {
            "execution_prices": "data/0050/daily_raw.json", "causal_technical_contract": "factors/0050_causal_technical_contract.json",
            "causal_technical_snapshots": technical_snapshots, "corporate_action_ledger": "events/0050_corporate_action_ledger.json",
            "corporate_action_events": "events/0050_corporate_action_events.json", "dividend_events": "events/0050_dividends.json",
            "split_event": "events/0050_split.json", "coverage": "coverage.json", "gaps": "gaps.json",
            "source_master_table": "sources/source_master_table.json", "artifact_manifest": "artifact_manifest.json", "readback": "readback.json",
        },
        "research_only": True, "performance_read_or_computed": False,
    }
    immutable_write(OUT / "STOP_WAIT_00_DATA_QA.json", canonical(gate))
    _checkpoint("STOP_WAIT_00_DATA_QA", len(FINMIND_DATASETS) + 78, len(FINMIND_DATASETS) + 78, None)

    artifacts = []
    for path in sorted(OUT.rglob("*")):
        if path.is_file() and path.name not in {"artifact_manifest.json", "readback.json", "checkpoint.json"}:
            raw = path.read_bytes()
            artifacts.append({"relative_path": path.relative_to(OUT).as_posix(), "bytes": len(raw), "sha256": sha256_bytes(raw)})
    manifest = {"schema_version": SCHEMA + "-manifest", "artifact_count": len(artifacts), "artifacts": artifacts, "research_only": True, "performance_read_or_computed": False}
    immutable_write(OUT / "artifact_manifest.json", canonical(manifest))
    readback = recompute_readback()
    immutable_write(OUT / "readback.json", canonical(readback))
    return {
        "status": "STOP_WAIT_00_DATA_QA", "data_complete_candidate": gate["data_complete_candidate"],
        "trading_days": len(rows), "formal_from": FORMAL_FROM, "dividends": len(dividends),
        "corporate_actions": len(action_payload), "named_gap_count": len(gaps), "readback": readback["status"],
    }


def recompute_readback() -> dict[str, Any]:
    manifest = json.loads((OUT / "artifact_manifest.json").read_text(encoding="utf-8"))
    mismatches = []
    for item in manifest.get("artifacts") or []:
        path = OUT / item["relative_path"]
        if not path.is_file():
            mismatches.append({"relative_path": item["relative_path"], "reason": "MISSING"})
        elif path.stat().st_size != item["bytes"]:
            mismatches.append({"relative_path": item["relative_path"], "reason": "BYTES"})
        elif file_sha256(path) != item["sha256"]:
            mismatches.append({"relative_path": item["relative_path"], "reason": "SHA256"})
    source_master = json.loads((OUT / "sources" / "source_master_table.json").read_text(encoding="utf-8"))
    source_mismatches = [item["relative_path"] for item in source_master.get("entries") or [] if not (OUT / item["relative_path"]).is_file() or file_sha256(OUT / item["relative_path"]) != item["sha256"]]
    gate = json.loads((OUT / "STOP_WAIT_00_DATA_QA.json").read_text(encoding="utf-8"))
    return {
        "schema_version": SCHEMA + "-readback", "status": "PASS" if not mismatches and not source_mismatches and gate.get("data_complete_candidate") else "FAIL",
        "artifact_count": manifest.get("artifact_count"), "artifact_hashes_verified": not mismatches,
        "source_entry_count": source_master.get("entry_count"), "source_hashes_verified": not source_mismatches,
        "mismatches": mismatches, "source_mismatches": source_mismatches, "gate_status": gate.get("status"),
        "ready_for_performance": False, "performance_read_or_computed": False,
    }


def validate_only() -> dict[str, Any]:
    stored = json.loads((OUT / "readback.json").read_text(encoding="utf-8"))
    fresh = recompute_readback()
    return {
        "schema_version": SCHEMA + "-validate", "status": "PASS" if stored.get("status") == fresh.get("status") == "PASS" else "FAIL",
        "stored_status": stored.get("status"), "recomputed": fresh, "network_used": False, "writes_performed": False,
        "ready_for_performance": False, "performance_read_or_computed": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("fetch", "build", "all", "validate-only"))
    parser.add_argument("--stop-after", type=int)
    args = parser.parse_args()
    if args.mode == "fetch":
        result = fetch_sources(args.stop_after)
    elif args.mode == "build":
        result = build()
    elif args.mode == "all":
        fetched = fetch_sources(args.stop_after)
        result = fetched if fetched["status"] == "CHECKPOINT" else build()
    else:
        result = validate_only()
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0 if result.get("status") in {"CHECKPOINT", "SOURCES_READY", "STOP_WAIT_00_DATA_QA", "PASS"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
