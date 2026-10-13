"""Build the immutable 0050 2016-2026 research data package.

2016 is warm-up only.  Formal performance use starts on 2017-01-03 and ends
on 2026-09-30.  This producer never computes or reads performance.
"""

from __future__ import annotations

import argparse
import calendar
import hashlib
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_ai_bot.data_sources.causal_price_adjustment import (  # noqa: E402
    build_causal_technical_bars,
)
from stock_ai_bot.data_sources.causal_price_contract import (  # noqa: E402
    ACTION_LEDGER_SCHEMA,
    CONTRACT_VERSION,
    ActionKind,
    CorporateActionEvent,
    CorporateActionLedger,
    CorporateActionType,
    RawDailyBar,
    canonical_sha256,
)

WORKDATA = Path(
    os.environ.get("STOCK_AI_BOT_WORKDATA_ROOT", ROOT / ".workdata")
).expanduser().resolve() / "dev" / "dual-ma"
OUT = Path(
    os.environ.get(
        "DUAL_MA_ETF_PACKAGE_OUT",
        str(WORKDATA / "04-etf-0050-research-package-2016-2026-v1"),
    )
)
V3 = WORKDATA / "03-etf-research-package-v3"
SECRETS = Path(os.environ.get("STOCK_AI_BOT_SECRETS_PATH", ROOT / "config" / "secrets.json"))
CONTRACT = ROOT / "docs/strategies/dual_ma/0050-2017-2026-ten-year-same-method-v1.md"

SCHEMA = "dual-ma-etf-0050-research-package-2016-2026-v1"
CONTRACT_SHA256 = "B1A30A8E57025007FAD59E6578C4F408C020AAA03AA27673842A1C2F7828E663"
V3_READY_SHA256 = "8339B3322E7D51EE9906ECF4CB04A09D4AA76735D1F390555894733805683CD5"
CODE = "0050"
INSTRUMENT_ID = "TWSE:0050"
SYMBOL = "0050.TW"
WARMUP_FROM = "2016-01-04"
WARMUP_THROUGH = "2016-12-30"
FORMAL_FROM = "2017-01-03"
THROUGH = "2026-09-30"
TWSE_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY"
FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"
DIVIDEND_URL = "https://www.twse.com.tw/zh/ETFortune/dividendList"
UA = "stock_ai_bot-research/0050-ten-year-v1"
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


def month_list(first: str, last: str) -> list[str]:
    year, month = map(int, first.split("-"))
    end_year, end_month = map(int, last.split("-"))
    result: list[str] = []
    while (year, month) <= (end_year, end_month):
        result.append(f"{year:04d}-{month:02d}")
        month += 1
        if month == 13:
            year, month = year + 1, 1
    return result


def load_finmind_token() -> str:
    payload = json.loads(SECRETS.read_text(encoding="utf-8-sig"))
    token = str(payload.get("finmind_api_key") or "").strip()
    if not token:
        raise ValueError("FINMIND_API_KEY_MISSING")
    return token


def http_get(url: str, params: dict[str, Any], *, token: str | None = None) -> tuple[int, bytes, str]:
    full_url = url + "?" + urllib.parse.urlencode(params)
    headers = {"User-Agent": UA, "Accept": "application/json,text/html,*/*"}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(full_url, headers=headers)
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
    token: str | None = None,
    require_json: bool = True,
) -> dict[str, Any]:
    raw_path = OUT / "sources" / kind / "raw" / (key + ".raw")
    receipt_path = OUT / "sources" / kind / "receipts" / (key + ".json")
    if raw_path.is_file() and receipt_path.is_file():
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if receipt.get("body_sha256") != file_sha256(raw_path):
            raise ValueError("SOURCE_RECEIPT_SHA_MISMATCH:" + kind + ":" + key)
        if receipt.get("status") != "OK":
            raise ValueError("SOURCE_RECEIPT_NOT_OK:" + kind + ":" + key)
        return receipt
    if receipt_path.is_file() and not raw_path.exists():
        failed_raw = receipt_path.read_bytes()
        failed = json.loads(failed_raw.decode("utf-8"))
        if failed.get("error") == "HTTP_307":
            failure_path = (
                OUT
                / "sources"
                / kind
                / "failures"
                / (key + "#" + sha256_bytes(failed_raw)[:16] + ".json")
            )
            immutable_write(failure_path, failed_raw)
            receipt_path.unlink()
        else:
            raise ValueError("SOURCE_RAW_RECEIPT_PAIR_INCOMPLETE:" + kind + ":" + key)
    if raw_path.exists() or receipt_path.exists():
        raise ValueError("SOURCE_RAW_RECEIPT_PAIR_INCOMPLETE:" + kind + ":" + key)

    status, body, full_url = http_get(url, params, token=token)
    receipt: dict[str, Any] = {
        "kind": kind,
        "key": key,
        "url": url,
        "params": params,
        "full_url": full_url,
        "http_status": status,
        "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
        "body_bytes": len(body),
        "body_sha256": sha256_bytes(body),
    }
    if token:
        receipt["authentication"] = "BEARER_TOKEN_REDACTED"
    if status != 200:
        receipt["status"] = "FAIL_CLOSED"
        receipt["error"] = "HTTP_" + str(status)
        immutable_write(receipt_path, canonical(receipt))
        raise RuntimeError(receipt["error"] + ":" + kind + ":" + key)
    if not body:
        receipt["status"] = "FAIL_CLOSED"
        receipt["error"] = "EMPTY_RESPONSE"
        immutable_write(receipt_path, canonical(receipt))
        raise RuntimeError("EMPTY_RESPONSE:" + kind + ":" + key)
    if require_json:
        try:
            payload = json.loads(body.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            receipt["status"] = "FAIL_CLOSED"
            receipt["error"] = "NON_JSON_RESPONSE"
            immutable_write(receipt_path, canonical(receipt))
            raise RuntimeError("NON_JSON_RESPONSE:" + kind + ":" + key) from exc
        source_status = payload.get("status") if isinstance(payload, dict) else None
        if isinstance(payload, dict) and payload.get("stat") not in {None, "OK", "ok"}:
            receipt["status"] = "FAIL_CLOSED"
            receipt["error"] = "SOURCE_STAT:" + str(payload.get("stat"))
            immutable_write(receipt_path, canonical(receipt))
            raise RuntimeError(receipt["error"] + ":" + kind + ":" + key)
        if source_status not in {None, 200, "200"}:
            receipt["status"] = "FAIL_CLOSED"
            receipt["error"] = "SOURCE_STATUS:" + str(source_status)
            immutable_write(receipt_path, canonical(receipt))
            raise RuntimeError(receipt["error"] + ":" + kind + ":" + key)
    receipt["status"] = "OK"
    immutable_write(raw_path, body)
    immutable_write(receipt_path, canonical(receipt))
    return receipt


def copy_immutable(source: Path, target: Path) -> None:
    immutable_write(target, source.read_bytes())


def fetch_sources(stop_after: int | None = None) -> dict[str, Any]:
    if file_sha256(CONTRACT) != CONTRACT_SHA256:
        raise ValueError("CONTRACT_SHA_MISMATCH")
    if file_sha256(V3 / "READY_FOR_02.json") != V3_READY_SHA256:
        raise ValueError("V3_READY_SHA_MISMATCH")
    completed = 0
    for month in month_list("2016-01", "2020-12"):
        archive_request(
            "twse_stock_day",
            CODE + "_" + month,
            TWSE_URL,
            {"date": month.replace("-", "") + "01", "stockNo": CODE, "response": "json"},
        )
        completed += 1
        if stop_after and completed >= stop_after:
            return {"status": "CHECKPOINT", "completed_requests": completed}
        time.sleep(0.15)

    v3_price = V3 / "sources" / "twse_stock_day"
    for folder in ("raw", "receipts"):
        for source in sorted((v3_price / folder).glob("0050_*." + ("raw" if folder == "raw" else "json"))):
            copy_immutable(source, OUT / "sources" / "twse_stock_day" / folder / source.name)

    archive_request(
        "twse_etfortune_dividend_list",
        "0050_2016_2026",
        DIVIDEND_URL,
        {"startDate": "2016", "endDate": "2026", "stkNo": CODE},
        require_json=False,
    )

    for name in (
        "twse_etfortune_announcement_20250509",
        "twse_etfortune_announcement_20250617",
        "isin_list_mode2",
    ):
        for filename in ("page.raw", "receipt.json"):
            copy_immutable(V3 / "sources" / name / filename, OUT / "sources" / name / filename)

    token = load_finmind_token()
    for dataset in FINMIND_DATASETS:
        archive_request(
            "finmind",
            dataset + "_0050_20160101_20260930",
            FINMIND_URL,
            {"dataset": dataset, "data_id": CODE, "start_date": "2016-01-01", "end_date": THROUGH},
            token=token,
        )
        completed += 1
        if stop_after and completed >= stop_after:
            return {"status": "CHECKPOINT", "completed_requests": completed}
        time.sleep(0.15)
    return {"status": "SOURCES_READY", "completed_requests": completed}


def roc_to_iso(text: str) -> str | None:
    value = str(text).strip()
    gregorian = re.fullmatch(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", value)
    if gregorian:
        year, month, day = map(int, gregorian.groups())
        return f"{year:04d}-{month:02d}-{day:02d}"
    roc = re.search(r"(\d{2,3})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", value)
    if roc:
        year, month, day = map(int, roc.groups())
        return f"{year + 1911:04d}-{month:02d}-{day:02d}"
    slash = re.fullmatch(r"(\d{2,3})[/-](\d{1,2})[/-](\d{1,2})", value)
    if slash:
        year, month, day = map(int, slash.groups())
        return f"{year + 1911:04d}-{month:02d}-{day:02d}"
    return None


def parse_twse_month(body: bytes, raw_sha: str, key: str) -> list[dict[str, Any]]:
    payload = json.loads(body.decode("utf-8-sig"))
    if payload.get("stat") != "OK":
        raise ValueError("TWSE_STAT_NOT_OK:" + key)
    fields = payload.get("fields") or []
    expected = {"日期", "成交股數", "開盤價", "最高價", "最低價", "收盤價"}
    if not expected.issubset(set(fields)):
        raise ValueError("TWSE_FIELD_CHANGE:" + key)
    index = {field: pos for pos, field in enumerate(fields)}

    def number(row: list[Any], field: str) -> float | None:
        raw = str(row[index[field]]).replace(",", "").strip()
        if raw in {"", "-", "--", "X"}:
            return None
        return float(raw)

    rows: list[dict[str, Any]] = []
    for item in payload.get("data") or []:
        trade_date = roc_to_iso(str(item[index["日期"]]))
        if not trade_date:
            raise ValueError("TWSE_DATE_UNPARSEABLE:" + key)
        values = {field: number(item, field) for field in ("開盤價", "最高價", "最低價", "收盤價", "成交股數")}
        if any(values[field] is None for field in ("開盤價", "最高價", "最低價", "收盤價", "成交股數")):
            raise ValueError("TWSE_NON_TRADABLE_ROW:" + trade_date)
        rows.append(
            {
                "date": trade_date,
                "code": CODE,
                "market": "TWSE",
                "open": values["開盤價"],
                "high": values["最高價"],
                "low": values["最低價"],
                "close": values["收盤價"],
                "volume": values["成交股數"],
                "amount": number(item, "成交金額") if "成交金額" in index else None,
                "trades": number(item, "成交筆數") if "成交筆數" in index else None,
                "note": str(item[index["註記"]]).strip() if "註記" in index else "",
                "source": "TWSE_STOCK_DAY",
                "source_key": key,
                "raw_response_sha256": raw_sha,
            }
        )
    return rows


def load_twse_rows() -> list[dict[str, Any]]:
    raw_dir = OUT / "sources" / "twse_stock_day" / "raw"
    receipt_dir = OUT / "sources" / "twse_stock_day" / "receipts"
    rows: list[dict[str, Any]] = []
    for month in month_list("2016-01", "2026-09"):
        key = CODE + "_" + month
        raw_path = raw_dir / (key + ".raw")
        receipt_path = receipt_dir / (key + ".json")
        if not raw_path.is_file() or not receipt_path.is_file():
            raise ValueError("TWSE_MONTH_MISSING:" + key)
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        raw = raw_path.read_bytes()
        expected_sha = receipt.get("body_sha256") or receipt.get("sha256")
        if receipt.get("http_status") != 200 or expected_sha != sha256_bytes(raw):
            raise ValueError("TWSE_MONTH_RECEIPT_INVALID:" + key)
        rows.extend(parse_twse_month(raw, sha256_bytes(raw), key))
    rows.sort(key=lambda item: item["date"])
    dates = [item["date"] for item in rows]
    if len(dates) != len(set(dates)):
        raise ValueError("TWSE_DUPLICATE_DATES")
    return [item for item in rows if WARMUP_FROM <= item["date"] <= THROUGH]


def load_finmind_payload(dataset: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    key = dataset + "_0050_20160101_20260930"
    raw_path = OUT / "sources" / "finmind" / "raw" / (key + ".raw")
    receipt_path = OUT / "sources" / "finmind" / "receipts" / (key + ".json")
    if not raw_path.is_file() or not receipt_path.is_file():
        raise ValueError("FINMIND_SOURCE_MISSING:" + dataset)
    raw = raw_path.read_bytes()
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if receipt.get("body_sha256") != sha256_bytes(raw) or receipt.get("status") != "OK":
        raise ValueError("FINMIND_SOURCE_INVALID:" + dataset)
    payload = json.loads(raw.decode("utf-8-sig"))
    if payload.get("status") not in {200, "200"} or not isinstance(payload.get("data"), list):
        raise ValueError("FINMIND_PAYLOAD_INVALID:" + dataset)
    return payload["data"], receipt


def normalize_finmind_price(rows: list[dict[str, Any]], dataset: str) -> list[dict[str, Any]]:
    result = []
    for item in rows:
        trade_date = str(item.get("date") or "")[:10]
        if not trade_date:
            raise ValueError("FINMIND_PRICE_DATE_MISSING:" + dataset)
        result.append(
            {
                "date": trade_date,
                "open": float(item["open"]),
                "high": float(item["max"] if "max" in item else item["high"]),
                "low": float(item["min"] if "min" in item else item["low"]),
                "close": float(item["close"]),
                "volume": float(item.get("Trading_Volume", item.get("volume", 0))),
            }
        )
    return sorted(result, key=lambda item: item["date"])


def clean_html(html: str) -> str:
    text = re.sub(r"<script.*?</script>", " ", html, flags=re.S)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text.replace("&nbsp;", " ").replace("&amp;", "&")).strip()


def parse_twse_dividends() -> list[dict[str, Any]]:
    key = "0050_2016_2026"
    raw_path = OUT / "sources" / "twse_etfortune_dividend_list" / "raw" / (key + ".raw")
    receipt_path = OUT / "sources" / "twse_etfortune_dividend_list" / "receipts" / (key + ".json")
    raw = raw_path.read_bytes()
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if receipt.get("body_sha256") != sha256_bytes(raw):
        raise ValueError("DIVIDEND_RECEIPT_SHA_MISMATCH")
    html = raw.decode("utf-8", "replace")
    events = []
    for block in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        cells = [clean_html(cell) for cell in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", block, re.S)]
        if not cells or cells[0].strip() != CODE:
            continue
        if len(cells) < 6:
            raise ValueError("DIVIDEND_FIELD_CHANGE")
        ex_date, base_date, payment_date = roc_to_iso(cells[2]), roc_to_iso(cells[3]), roc_to_iso(cells[4])
        if not ex_date or not payment_date:
            raise ValueError("DIVIDEND_DATE_UNPARSEABLE")
        events.append(
            {
                "code": CODE,
                "name": cells[1],
                "ex_date": ex_date,
                "base_date": base_date,
                "payment_date": payment_date,
                "amount_per_unit_original": float(cells[5]),
                "amount_per_unit_raw_text": cells[5],
                "source": {
                    "path": "sources/twse_etfortune_dividend_list/raw/" + key + ".raw",
                    "sha256": sha256_bytes(raw),
                    "url": receipt.get("full_url"),
                },
            }
        )
    events.sort(key=lambda item: item["ex_date"])
    if not events:
        raise ValueError("DIVIDEND_EVENTS_EMPTY")
    return events


def build_action_events(
    raw_rows: list[dict[str, Any]], dividends: list[dict[str, Any]], fetched_at: datetime
) -> tuple[list[CorporateActionEvent], dict[str, Any]]:
    by_date = {item["date"]: item for item in raw_rows}
    dates = [item["date"] for item in raw_rows]
    events: list[CorporateActionEvent] = []
    for item in dividends:
        index = dates.index(item["ex_date"])
        if index <= 0:
            raise ValueError("DIVIDEND_PREVIOUS_CLOSE_MISSING:" + item["ex_date"])
        previous_close = float(by_date[dates[index - 1]]["close"])
        amount = float(item["amount_per_unit_original"])
        factor = (previous_close - amount) / previous_close
        payload = {
            "action_id": "TWSE:0050:CASH:" + item["ex_date"],
            "instrument_id": INSTRUMENT_ID,
            "action_type": CorporateActionType.CASH_DIVIDEND,
            "action_kind": ActionKind.CASH,
            "effective_date": date.fromisoformat(item["ex_date"]),
            "ex_date": date.fromisoformat(item["ex_date"]),
            "available_on": date.fromisoformat(item["ex_date"]),
            "announcement_date": None,
            "entitlement_date": date.fromisoformat(item["base_date"]) if item.get("base_date") else None,
            "payment_date": date.fromisoformat(item["payment_date"]),
            "position_lifecycle_status": "SETTLED",
            "position_lifecycle_reason": None,
            "rights_policy": "NONE",
            "halt_date": None,
            "resume_date": None,
            "cash_dividend": amount,
            "stock_dividend_shares": None,
            "split_ratio": None,
            "cash_price_factor": factor,
            "share_price_factor": None,
            "share_volume_factor": None,
            "rights_price_factor": None,
            "previous_close": previous_close,
            "reference_price": previous_close - amount,
            "technical_resolution_status": "RESOLVED_FOR_TECHNICAL",
            "technical_resolution_reason": None,
            "source_name": "TWSE_ETFORTUNE_DIVIDEND_LIST",
            "source_endpoint": DIVIDEND_URL,
            "fetched_at": fetched_at,
            "raw_response_sha256": item["source"]["sha256"].lower(),
        }
        event = CorporateActionEvent(**payload, event_sha256=canonical_sha256(payload))
        event.validate()
        events.append(event)

    split_source = json.loads((V3 / "events" / "0050_split.json").read_text(encoding="utf-8"))
    split_payload = {
        "action_id": "TWSE:0050:SPLIT:2025-06-18",
        "instrument_id": INSTRUMENT_ID,
        "action_type": CorporateActionType.SPLIT,
        "action_kind": ActionKind.SHARE,
        "effective_date": date(2025, 6, 18),
        "ex_date": date(2025, 6, 18),
        "available_on": date(2025, 6, 17),
        "announcement_date": date(2025, 5, 9),
        "entitlement_date": None,
        "payment_date": None,
        "position_lifecycle_status": "SETTLED",
        "position_lifecycle_reason": None,
        "rights_policy": "NONE",
        "halt_date": date(2025, 6, 11),
        "resume_date": date(2025, 6, 18),
        "cash_dividend": None,
        "stock_dividend_shares": None,
        "split_ratio": 4.0,
        "cash_price_factor": None,
        "share_price_factor": 0.25,
        "share_volume_factor": 4.0,
        "rights_price_factor": None,
        "previous_close": 188.65,
        "reference_price": 47.16,
        "technical_resolution_status": "RESOLVED_FOR_TECHNICAL",
        "technical_resolution_reason": None,
        "source_name": "TWSE_ETFORTUNE_ANNOUNCEMENT",
        "source_endpoint": split_source["lineage"]["result"]["url"],
        "fetched_at": fetched_at,
        "raw_response_sha256": split_source["lineage"]["result"]["sha256"].lower(),
    }
    split_event = CorporateActionEvent(**split_payload, event_sha256=canonical_sha256(split_payload))
    split_event.validate()
    events.append(split_event)
    events.sort(key=lambda item: (item.event_date, item.action_id))
    return events, split_source


def raw_bar(row: dict[str, Any], fetched_at: datetime, snapshot_sha256: str) -> RawDailyBar:
    segment = "twse-stock-day-2016-2020-v1" if row["date"] < "2021-01-01" else "twse-stock-day-v3-2021-2026"
    payload = {
        "instrument_id": INSTRUMENT_ID,
        "symbol": SYMBOL,
        "exchange": "TWSE",
        "trade_date": date.fromisoformat(row["date"]),
        "raw_open": float(row["open"]),
        "raw_high": float(row["high"]),
        "raw_low": float(row["low"]),
        "raw_close": float(row["close"]),
        "raw_volume": float(row["volume"]),
        "source_name": "TWSE_STOCK_DAY",
        "source_endpoint": TWSE_URL,
        "source_market": "TWSE",
        "source_segment_id": segment,
        "fetched_at": fetched_at,
        "query_from": date.fromisoformat(row["date"][:7] + "-01"),
        "query_to": date(
            int(row["date"][:4]),
            int(row["date"][5:7]),
            calendar.monthrange(int(row["date"][:4]), int(row["date"][5:7]))[1],
        ),
        "raw_response_sha256": str(row["raw_response_sha256"]).lower(),
        "snapshot_id": "0050-ten-year-raw-v1",
        "snapshot_sha256": snapshot_sha256,
        "mainboard_start": date(2003, 6, 30),
    }
    row_hash_payload = {
        key: value
        for key, value in payload.items()
        if key not in {"fetched_at", "query_from", "query_to", "snapshot_sha256"}
    }
    row_hash_payload["adjusted"] = False
    row_hash_payload["execution_price_basis"] = "UNADJUSTED_MAINBOARD_OHLCV"
    payload["row_sha256"] = canonical_sha256(row_hash_payload)
    bar = RawDailyBar(**payload)
    bar.validate(source_lineage_prevalidated=True)
    return bar


def technical_rows_as_of(
    rows: list[dict[str, Any]],
    events: list[CorporateActionEvent],
    as_of: str,
    fetched_at: datetime,
) -> list[dict[str, Any]]:
    as_of_date = date.fromisoformat(as_of)
    eligible = [row for row in rows if row["date"] <= as_of]
    snapshot_sha = canonical_sha256(
        [
            {field: row[field] for field in ("date", "open", "high", "low", "close", "volume", "raw_response_sha256")}
            for row in eligible
        ]
    )
    bars = [raw_bar(row, fetched_at, snapshot_sha) for row in eligible]
    event_payload = []
    for event in events:
        item = asdict(event)
        item["action_type"] = event.action_type.value
        item["action_kind"] = event.action_kind.value
        for key, value in list(item.items()):
            if isinstance(value, (date, datetime)):
                item[key] = value.isoformat()
        event_payload.append(item)
    ledger = CorporateActionLedger(
        schema_version=ACTION_LEDGER_SCHEMA,
        snapshot_id="0050-corporate-action-ledger-runtime",
        coverage_from=date.fromisoformat(WARMUP_FROM),
        coverage_through=date.fromisoformat(THROUGH),
        status="PASS",
        source_names=("TWSE_ETFORTUNE_DIVIDEND_LIST", "TWSE_ETFORTUNE_ANNOUNCEMENT"),
        events=tuple(events),
        manifest_sha256=canonical_sha256(event_payload),
    )
    technical = build_causal_technical_bars(
        raw_bars=bars,
        ledger=ledger,
        as_of_date=as_of_date,
        validated_source_lineage=True,
    )
    output = []
    for item in technical:
        row = asdict(item)
        row["trade_date"] = item.trade_date.isoformat()
        row["as_of_date"] = item.as_of_date.isoformat()
        row["factor_event_ids"] = list(item.factor_event_ids)
        output.append(row)
    return output


def source_master_table() -> dict[str, Any]:
    entries = []
    for path in sorted((OUT / "sources").rglob("*")):
        if path.is_file() and path.name != "source_master_table.json":
            raw = path.read_bytes()
            entries.append(
                {"relative_path": path.relative_to(OUT).as_posix(), "bytes": len(raw), "sha256": sha256_bytes(raw)}
            )
    return {"schema_version": SCHEMA + "-source-master", "entry_count": len(entries), "entries": entries}


def compare_prices(twse: list[dict[str, Any]], finmind: list[dict[str, Any]]) -> dict[str, Any]:
    by_twse = {item["date"]: item for item in twse}
    by_finmind = {item["date"]: item for item in finmind}
    mismatches = []
    for trade_date in sorted(set(by_twse) & set(by_finmind)):
        left, right = by_twse[trade_date], by_finmind[trade_date]
        different = {}
        for field in ("open", "high", "low", "close", "volume"):
            if float(left[field]) != float(right[field]):
                different[field] = {"twse": left[field], "finmind": right[field]}
        if different:
            mismatches.append({"date": trade_date, "fields": different})
    return {
        "schema_version": SCHEMA + "-price-cross-validation",
        "twse_rows": len(twse),
        "finmind_rows": len(finmind),
        "intersection_rows": len(set(by_twse) & set(by_finmind)),
        "twse_only_dates": sorted(set(by_twse) - set(by_finmind)),
        "finmind_only_dates": sorted(set(by_finmind) - set(by_twse)),
        "mismatch_count": len(mismatches),
        "mismatches": mismatches,
        "policy": "FINMIND_CROSS_VALIDATION_ONLY_TWSE_REMAINS_EXECUTION_SOURCE",
    }


def v3_overlap_check(rows: list[dict[str, Any]]) -> dict[str, Any]:
    old = json.loads((V3 / "data" / CODE / "daily_raw.json").read_text(encoding="utf-8"))["rows"]
    by_new = {item["date"]: item for item in rows}
    mismatches = []
    for item in old:
        current = by_new.get(item["date"])
        if current is None:
            mismatches.append({"date": item["date"], "reason": "MISSING_IN_NEW"})
            continue
        fields = [field for field in ("open", "high", "low", "close", "volume", "amount", "trades", "note") if current.get(field) != item.get(field)]
        if fields:
            mismatches.append({"date": item["date"], "fields": fields})
    return {
        "schema_version": SCHEMA + "-v3-overlap",
        "v3_rows": len(old),
        "checked_rows": len(old),
        "mismatch_count": len(mismatches),
        "mismatches": mismatches,
        "v3_ready_sha256": file_sha256(V3 / "READY_FOR_02.json"),
    }


def build() -> dict[str, Any]:
    if file_sha256(CONTRACT) != CONTRACT_SHA256:
        raise ValueError("CONTRACT_SHA_MISMATCH")
    rows = load_twse_rows()
    if len(rows) != 2612 or rows[0]["date"] != WARMUP_FROM or rows[-1]["date"] != THROUGH:
        raise ValueError("TWSE_PRICE_COVERAGE_UNEXPECTED:" + str((len(rows), rows[0]["date"], rows[-1]["date"])))

    price_rows, _ = load_finmind_payload("TaiwanStockPrice")
    adjusted_rows, _ = load_finmind_payload("TaiwanStockPriceAdj")
    finmind_prices = normalize_finmind_price(price_rows, "TaiwanStockPrice")
    finmind_adjusted = normalize_finmind_price(adjusted_rows, "TaiwanStockPriceAdj")
    if len(finmind_prices) != 2612 or len(finmind_adjusted) != 2612:
        raise ValueError("FINMIND_PRICE_ROW_COUNT_UNEXPECTED")

    dividends = parse_twse_dividends()
    finmind_dividend, _ = load_finmind_payload("TaiwanStockDividend")
    finmind_dividend_result, _ = load_finmind_payload("TaiwanStockDividendResult")
    finmind_split, _ = load_finmind_payload("TaiwanStockSplitPrice")
    if len(finmind_dividend) != 21 or len(finmind_dividend_result) != 21:
        raise ValueError("FINMIND_DIVIDEND_ROW_COUNT_UNEXPECTED")
    split_0050 = [item for item in finmind_split if str(item.get("stock_id") or item.get("data_id") or CODE) == CODE]
    if len(split_0050) != 1:
        raise ValueError("FINMIND_SPLIT_EVENT_COUNT_UNEXPECTED:" + str(len(split_0050)))

    source_receipt = json.loads(
        (OUT / "sources" / "twse_etfortune_dividend_list" / "receipts" / "0050_2016_2026.json").read_text(encoding="utf-8")
    )
    fetched_at = datetime.fromisoformat(source_receipt["retrieved_at_utc"])
    action_events, split = build_action_events(rows, dividends, fetched_at)

    action_payload = []
    for event in action_events:
        item = asdict(event)
        item["action_type"] = event.action_type.value
        item["action_kind"] = event.action_kind.value
        for key, value in list(item.items()):
            if isinstance(value, (date, datetime)):
                item[key] = value.isoformat()
        action_payload.append(item)
    actions_sha = canonical_sha256(action_payload)
    ledger_core = {
        "schema_version": ACTION_LEDGER_SCHEMA,
        "coverage": {"from": WARMUP_FROM, "through": THROUGH},
        "status": "PASS",
        "source_names": ["TWSE_ETFORTUNE_DIVIDEND_LIST", "TWSE_ETFORTUNE_ANNOUNCEMENT"],
        "events_path": "0050_corporate_action_events.json",
        "events_sha256": actions_sha,
        "event_count": len(action_payload),
        "technical_resolution": {"status": "PASS", "unresolved_event_ids": []},
        "position_lifecycle": {"status": "PASS", "quarantined_event_ids": []},
    }
    ledger_sha = canonical_sha256(ledger_core)
    ledger_manifest = {**ledger_core, "snapshot_id": "0050-corporate-action-ledger#" + ledger_sha[:16], "content_sha256": ledger_sha}
    immutable_write(OUT / "events" / "0050_corporate_action_events.json", canonical(action_payload))
    immutable_write(OUT / "events" / "0050_corporate_action_ledger.json", canonical(ledger_manifest))
    immutable_write(
        OUT / "events" / "0050_dividends.json",
        canonical(
            {
                "schema_version": SCHEMA + "-dividends",
                "code": CODE,
                "events": dividends,
                "twse_event_count": len(dividends),
                "finmind_dividend_rows": len(finmind_dividend),
                "finmind_dividend_result_rows": len(finmind_dividend_result),
                "historical_amount_policy": "KEEP_EVENT_TIME_ORIGINAL_PER_UNIT_AMOUNT_NEVER_POST_SPLIT_REWRITE",
            }
        ),
    )
    immutable_write(
        OUT / "events" / "0050_split.json",
        canonical({"schema_version": SCHEMA + "-split", "code": CODE, **split, "finmind_cross_validation_rows": split_0050}),
    )

    immutable_write(
        OUT / "data" / CODE / "daily_raw.json",
        canonical(
            {
                "schema_version": SCHEMA + "-daily-raw",
                "code": CODE,
                "execution_price_policy": "RAW_UNADJUSTED_TWSE_STOCK_DAY_ONLY",
                "warmup": {"from": WARMUP_FROM, "through": WARMUP_THROUGH, "performance_allowed": False},
                "formal": {"from": FORMAL_FROM, "through": THROUGH},
                "rows": rows,
            }
        ),
    )

    price_cross = compare_prices(rows, finmind_prices)
    adjusted_cross = {
        "schema_version": SCHEMA + "-adjusted-cross-validation",
        "dataset": "TaiwanStockPriceAdj",
        "rows": len(finmind_adjusted),
        "from": finmind_adjusted[0]["date"],
        "through": finmind_adjusted[-1]["date"],
        "policy": "CROSS_VALIDATION_ONLY_NOT_EXECUTION_AND_NOT_UNVERIFIED_SIGNAL_INPUT",
        "raw_source": "sources/finmind/raw/TaiwanStockPriceAdj_0050_20160101_20260930.raw",
    }
    overlap = v3_overlap_check(rows)
    immutable_write(OUT / "validation" / "finmind_price_cross_validation.json", canonical(price_cross))
    immutable_write(OUT / "validation" / "finmind_adjusted_cross_validation.json", canonical(adjusted_cross))
    immutable_write(OUT / "validation" / "v3_2021_2026_overlap.json", canonical(overlap))

    raw_bar_rows = []
    for row in rows:
        raw_bar_rows.append(
            {
                "date": row["date"],
                "raw_row_sha256": canonical_sha256(
                    {field: row[field] for field in ("date", "open", "high", "low", "close", "volume", "raw_response_sha256")}
                ),
                "source_segment_id": "twse-stock-day-2016-2020-v1" if row["date"] < "2021-01-01" else "twse-stock-day-v3-2021-2026",
            }
        )
    technical_contract = {
        "schema_version": SCHEMA + "-causal-technical-contract",
        "contract_version": CONTRACT_VERSION,
        "code": CODE,
        "raw_rows": raw_bar_rows,
        "action_ledger": "events/0050_corporate_action_ledger.json",
        "consumer_rule": "FOR_EACH_AS_OF_REBUILD_HISTORY_USING_ONLY_TRADE_DATE_LT_EVENT_DATE_LE_AS_OF_AND_AVAILABLE_ON_LE_AS_OF",
        "same_day_anchor": "TECHNICAL_OHLCV_EQUALS_RAW_OHLCV_ON_AS_OF_DATE",
        "vendor_adjusted_policy": "CROSS_VALIDATION_ONLY",
        "cash_and_share_transforms_separate": True,
        "future_event_invariance_required": True,
        "adapter": "scripts/dual_ma_etf_0050_ten_year_package_v1.py::technical_rows_as_of",
    }
    immutable_write(OUT / "factors" / "0050_causal_technical_contract.json", canonical(technical_contract))

    technical_snapshots = {}
    for as_of in (FORMAL_FROM, "2025-06-10", "2025-06-18", THROUGH):
        technical_rows = technical_rows_as_of(rows, action_events, as_of, fetched_at)
        relative = "technical/as_of_" + as_of + ".json"
        immutable_write(
            OUT / relative,
            canonical(
                {
                    "schema_version": SCHEMA + "-causal-technical-as-of",
                    "contract_version": CONTRACT_VERSION,
                    "code": CODE,
                    "as_of_date": as_of,
                    "rows": technical_rows,
                }
            ),
        )
        technical_snapshots[as_of] = relative

    boundary_raw = [row for row in rows if "2016-12-20" <= row["date"] <= "2017-01-10"]
    boundary_fixture = {
        "schema_version": SCHEMA + "-fixture-2016-2017-boundary",
        "warmup_from": WARMUP_FROM,
        "warmup_through": WARMUP_THROUGH,
        "formal_from": FORMAL_FROM,
        "performance_or_transactions_before_formal_from": "PROHIBITED",
        "rows": boundary_raw,
    }
    immutable_write(OUT / "fixtures" / "warmup_to_formal_2016_2017.json", canonical(boundary_fixture))
    split_fixture = {
        "schema_version": SCHEMA + "-fixture-split-crossing-2025",
        "event_date": "2025-06-18",
        "halt_dates": ["2025-06-11", "2025-06-12", "2025-06-13", "2025-06-16", "2025-06-17"],
        "transaction_count_on_event_date": 0,
        "position_before": {"shares": 1000, "unit_cost": 100.0, "stop": 90.0, "addon": 110.0, "r": 10.0, "book_cost": 100000.0},
        "position_after": {"shares": 4000, "unit_cost": 25.0, "stop": 22.5, "addon": 27.5, "r": 2.5, "book_cost": 100000.0},
        "book_cost_conserved": True,
        "historical_dividend_amounts_rewritten": False,
    }
    immutable_write(OUT / "fixtures" / "split_crossing_2025.json", canonical(split_fixture))

    coverage_checks = {
        "twse_raw_2612": len(rows) == 2612,
        "twse_range_exact": rows[0]["date"] == WARMUP_FROM and rows[-1]["date"] == THROUGH,
        "finmind_raw_2612": len(finmind_prices) == 2612,
        "finmind_adjusted_2612": len(finmind_adjusted) == 2612,
        "finmind_dividend_21": len(finmind_dividend) == 21,
        "finmind_dividend_result_21": len(finmind_dividend_result) == 21,
        "finmind_split_0050_one": len(split_0050) == 1,
        "v3_overlap_exact": overlap["mismatch_count"] == 0 and overlap["checked_rows"] == 1388,
        "formal_start_present": FORMAL_FROM in {row["date"] for row in rows},
        "split_first_party_verified": split.get("status") == "FIRST_PARTY_VERIFIED",
        "price_date_sets_equal": not price_cross["twse_only_dates"] and not price_cross["finmind_only_dates"],
    }
    coverage = {
        "schema_version": SCHEMA + "-coverage",
        "code": CODE,
        "warmup": {"from": WARMUP_FROM, "through": WARMUP_THROUGH, "performance_allowed": False},
        "formal": {"from": FORMAL_FROM, "through": THROUGH, "partial_2026": True},
        "twse_trading_days": len(rows),
        "finmind_price_rows": len(finmind_prices),
        "finmind_adjusted_rows": len(finmind_adjusted),
        "twse_dividend_events": len(dividends),
        "corporate_action_events": len(action_payload),
        "checks": coverage_checks,
        "research_only": True,
        "point_in_time_verified": False,
        "performance_read_or_computed": False,
    }
    immutable_write(OUT / "coverage.json", canonical(coverage))
    immutable_write(OUT / "sources" / "source_master_table.json", canonical(source_master_table()))

    summary = "\n".join(
        [
            "# 0050 2016～2026 十年同方法回測資料包 v1",
            "",
            f"- 原價成交序列：TWSE STOCK_DAY，共 {len(rows)} 個交易日（{WARMUP_FROM}～{THROUGH}）。",
            f"- 暖機：{WARMUP_FROM}～{WARMUP_THROUGH}，不得計入績效或成交；正式期間自 {FORMAL_FROM} 起。",
            f"- FinMind 原價與付費還原價各 {len(finmind_prices)}／{len(finmind_adjusted)} 筆；還原價僅交叉驗證。",
            f"- TWSE 第一方配息 {len(dividends)} 筆；FinMind Dividend／DividendResult 各 21 筆交叉驗證。",
            "- 2025-06-18 4:1 分割沿用 v3 第一方公告：股數 ×4，單位成本／停損／加碼／R ÷4，總成本守恆，分割日零交易。",
            f"- 2021～2026 與舊 v3 逐筆核對 {overlap['checked_rows']} 筆，差異 {overlap['mismatch_count']} 筆。",
            "- 技術價必須由 raw＋事件 ledger 逐 as-of 重建；FinMind adjusted 不得直接成交或作未驗訊號。",
            "- research_only=true；本資料包不含也未讀取績效。",
            "",
        ]
    )
    immutable_write(OUT / "summary_zh_TW.md", summary.encode("utf-8"))
    immutable_write(
        OUT / "tests_result.txt",
        os.environ.get("DUAL_MA_ETF_TEST_RESULT", "UNRECORDED").encode("utf-8"),
    )

    ready_checks = {**coverage_checks, "all_source_receipts_readback": True}
    ready = {
        "schema_version": SCHEMA + "-ready-for-02",
        "status": "STOP_WAIT_00_QA",
        "ready": all(ready_checks.values()),
        "ready_checks": ready_checks,
        "code": CODE,
        "contract": {"path": str(CONTRACT), "sha256": CONTRACT_SHA256},
        "date_range": {"warmup_from": WARMUP_FROM, "formal_from": FORMAL_FROM, "through": THROUGH},
        "trading_days": len(rows),
        "consumer_entry": {
            "execution_prices": "data/0050/daily_raw.json",
            "causal_technical_contract": "factors/0050_causal_technical_contract.json",
            "causal_technical_adapter": "scripts/dual_ma_etf_0050_ten_year_package_v1.py::technical_rows_as_of",
            "causal_technical_snapshots": technical_snapshots,
            "corporate_action_ledger": "events/0050_corporate_action_ledger.json",
            "corporate_action_events": "events/0050_corporate_action_events.json",
            "dividend_events": "events/0050_dividends.json",
            "split_event": "events/0050_split.json",
            "coverage": "coverage.json",
            "source_master_table": "sources/source_master_table.json",
            "price_cross_validation": "validation/finmind_price_cross_validation.json",
            "adjusted_cross_validation": "validation/finmind_adjusted_cross_validation.json",
            "v3_overlap": "validation/v3_2021_2026_overlap.json",
            "warmup_boundary_fixture": "fixtures/warmup_to_formal_2016_2017.json",
            "split_fixture": "fixtures/split_crossing_2025.json",
            "artifact_manifest": "artifact_manifest.json",
            "readback": "readback.json",
        },
        "research_only": True,
        "point_in_time_verified": False,
        "performance_read_or_computed": False,
    }
    immutable_write(OUT / "READY_FOR_02.json", canonical(ready))
    artifacts = []
    for path in sorted(OUT.rglob("*")):
        if path.is_file() and path.name not in {"artifact_manifest.json", "readback.json"}:
            raw = path.read_bytes()
            artifacts.append({"relative_path": path.relative_to(OUT).as_posix(), "bytes": len(raw), "sha256": sha256_bytes(raw)})
    manifest = {
        "schema_version": SCHEMA + "-manifest",
        "artifact_count": len(artifacts),
        "artifacts": artifacts,
        "research_only": True,
        "point_in_time_verified": False,
        "performance_read_or_computed": False,
    }
    immutable_write(OUT / "artifact_manifest.json", canonical(manifest))
    readback = recompute_readback()
    immutable_write(OUT / "readback.json", canonical(readback))
    return {
        "status": "STOP_WAIT_00_QA",
        "ready": ready["ready"],
        "trading_days": len(rows),
        "twse_dividends": len(dividends),
        "corporate_actions": len(action_payload),
        "v3_overlap_mismatches": overlap["mismatch_count"],
        "readback": readback["status"],
    }


def recompute_readback() -> dict[str, Any]:
    manifest_path = OUT / "artifact_manifest.json"
    source_master_path = OUT / "sources" / "source_master_table.json"
    mismatches = []
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for item in manifest.get("artifacts") or []:
        path = OUT / item["relative_path"]
        if not path.is_file():
            mismatches.append({"relative_path": item["relative_path"], "reason": "MISSING"})
        elif path.stat().st_size != item["bytes"]:
            mismatches.append({"relative_path": item["relative_path"], "reason": "BYTES"})
        elif file_sha256(path) != item["sha256"]:
            mismatches.append({"relative_path": item["relative_path"], "reason": "SHA256"})
    source_master = json.loads(source_master_path.read_text(encoding="utf-8"))
    source_mismatches = []
    for item in source_master.get("entries") or []:
        path = OUT / item["relative_path"]
        if not path.is_file() or file_sha256(path) != item["sha256"]:
            source_mismatches.append(item["relative_path"])
    ready = json.loads((OUT / "READY_FOR_02.json").read_text(encoding="utf-8"))
    return {
        "schema_version": SCHEMA + "-readback",
        "status": "PASS" if not mismatches and not source_mismatches and ready.get("ready") is True else "FAIL",
        "artifact_count": manifest.get("artifact_count"),
        "artifact_hashes_verified": not mismatches,
        "source_entry_count": source_master.get("entry_count"),
        "source_hashes_verified": not source_mismatches,
        "mismatches": mismatches,
        "source_mismatches": source_mismatches,
        "ready_for_02": ready.get("ready"),
        "research_only": True,
        "point_in_time_verified": False,
        "performance_read_or_computed": False,
    }


def validate_only() -> dict[str, Any]:
    stored = json.loads((OUT / "readback.json").read_text(encoding="utf-8"))
    fresh = recompute_readback()
    return {
        "schema_version": SCHEMA + "-validate",
        "status": "PASS" if stored.get("status") == "PASS" and fresh.get("status") == "PASS" else "FAIL",
        "stored_status": stored.get("status"),
        "recomputed": fresh,
        "research_only": True,
        "point_in_time_verified": False,
        "performance_read_or_computed": False,
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
    return 0 if result.get("status") in {"CHECKPOINT", "SOURCES_READY", "STOP_WAIT_00_QA", "PASS"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
