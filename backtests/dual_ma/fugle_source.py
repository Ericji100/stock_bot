from __future__ import annotations

import csv
import hashlib
import json
import math
import random
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import httpx


FUGLE_ENDPOINT = (
    "https://api.fugle.tw/marketdata/v1.0/stock/historical/candles/{symbol}"
)
FUGLE_DOCS_URL = "https://developer.fugle.tw/docs/data/http-api/historical/candles/"
FUGLE_FIELDS = "open,high,low,close,volume"
FUGLE_MIN_INTERVAL_SECONDS = 1.1
FUGLE_TIMEOUT_SECONDS = 30.0
PROBE_SELECTION_VERSION = "dual-ma-fugle-probe-selection-v1"
PROBE_STRATA: tuple[tuple[str, str, int], ...] = (
    ("2021_WARMUP", "CLOSE_ABOVE_HIGH", 3),
    ("2021_WARMUP", "CLOSE_BELOW_LOW", 2),
    ("2022_FORMAL", "CLOSE_ABOVE_HIGH", 3),
    ("2022_FORMAL", "CLOSE_BELOW_LOW", 2),
)

# Keep the historical route cutovers needed by the probe self-contained.  The
# 0050 replay must not import the stock-selection feasibility pipeline merely
# to decide whether four legacy symbols were already on their main board.
MAINBOARD_STARTS: Mapping[str, date] = {
    "4923.TWO": date(2022, 3, 14),
    "6763.TWO": date(2022, 3, 15),
    "6829.TWO": date(2022, 3, 11),
    "6585.TW": date(2022, 5, 20),
}


class FugleSourceError(RuntimeError):
    """Base class for formal-source failures with secret-safe messages."""


class FugleAccessError(FugleSourceError):
    """Authentication, authorization, or plan-access failure."""


class FugleCoverageError(FugleSourceError):
    """A required historical observation is unavailable."""


class FugleRateLimitError(FugleSourceError):
    """The bounded retry policy could not clear a rate limit."""


@dataclass(frozen=True)
class ProbeCase:
    symbol: str
    date: date
    market: str
    period: str
    reason: str
    v1_open: float
    v1_high: float
    v1_low: float
    v1_close: float
    frozen_location: str

    @property
    def code(self) -> str:
        return self.symbol.split(".", 1)[0]


@dataclass(frozen=True)
class FugleResponse:
    content: bytes
    status_code: int
    fetched_at: str
    attempts: int


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    if not rows:
        raise ValueError(f"refusing to write empty CSV: {path}")
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def load_fugle_api_key(config_path: Path) -> str:
    """Load the key without putting its value, length, or config hash in outputs."""
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FugleAccessError("Fugle credential configuration is unavailable") from exc
    if not isinstance(payload, dict):
        raise FugleAccessError("Fugle credential configuration is invalid")
    key = payload.get("fugle_api_key")
    if not isinstance(key, str) or not key.strip():
        raise FugleAccessError("Fugle API credential is not configured")
    return key.strip()


def _mainboard_eligible(symbol: str, observed_on: date) -> bool:
    mainboard_start = MAINBOARD_STARTS.get(symbol)
    return mainboard_start is None or observed_on >= mainboard_start


def _probe_rank(row: Mapping[str, str]) -> str:
    material = "|".join(
        (
            PROBE_SELECTION_VERSION,
            row["symbol"],
            row["date"],
            row["period"],
            row["reason"],
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def select_probe_cases(issue_csv: Path) -> list[ProbeCase]:
    """Select exactly ten route-eligible anomalies by a frozen hash ranking."""
    with issue_csv.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    selected: list[Mapping[str, str]] = []
    used_symbols: set[str] = set()
    for period, reason, count in PROBE_STRATA:
        candidates = [
            row
            for row in rows
            if row["period"] == period
            and row["reason"] == reason
            and _mainboard_eligible(row["symbol"], date.fromisoformat(row["date"]))
        ]
        candidates.sort(key=lambda row: (_probe_rank(row), row["symbol"], row["date"]))
        distinct = [row for row in candidates if row["symbol"] not in used_symbols]
        if len(distinct) < count:
            raise ValueError(f"insufficient distinct route-eligible cases for {period}/{reason}")
        chosen = distinct[:count]
        selected.extend(chosen)
        used_symbols.update(row["symbol"] for row in chosen)
    if len(selected) != 10 or len({row["symbol"] for row in selected}) != 10:
        raise AssertionError("formal probe selection must contain ten distinct cases")
    return [
        ProbeCase(
            symbol=row["symbol"],
            date=date.fromisoformat(row["date"]),
            market=row["market"],
            period=row["period"],
            reason=row["reason"],
            v1_open=float(row["open"]),
            v1_high=float(row["high"]),
            v1_low=float(row["low"]),
            v1_close=float(row["close"]),
            frozen_location=row["frozen_location"],
        )
        for row in selected
    ]


def safe_query_params(start: date, end: date) -> dict[str, str]:
    if end < start:
        raise ValueError("Fugle query end precedes start")
    if (end - start).days >= 365:
        raise ValueError("Fugle query range must be less than one year")
    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "timeframe": "D",
        "adjusted": "false",
        "fields": FUGLE_FIELDS,
        "sort": "asc",
    }


def _retry_after_seconds(value: str | None, now: datetime | None = None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        retry_at = parsedate_to_datetime(value)
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        current = now or datetime.now(timezone.utc)
        return max(0.0, (retry_at - current).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


class FugleHistoricalClient:
    def __init__(
        self,
        api_key: str,
        *,
        min_interval_seconds: float = FUGLE_MIN_INTERVAL_SECONDS,
        timeout_seconds: float = FUGLE_TIMEOUT_SECONDS,
        max_attempts: int = 4,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise FugleAccessError("Fugle API credential is not configured")
        self._api_key = api_key
        self._min_interval_seconds = min_interval_seconds
        self._max_attempts = max_attempts
        self._sleep = sleep
        self._monotonic = monotonic
        self._last_request_at: float | None = None
        self._client = httpx.Client(
            timeout=timeout_seconds,
            follow_redirects=True,
            transport=transport,
        )

    def __enter__(self) -> "FugleHistoricalClient":
        return self

    def __exit__(self, *_: object) -> None:
        self._client.close()

    def _wait_for_slot(self) -> None:
        now = self._monotonic()
        if self._last_request_at is not None:
            remaining = self._min_interval_seconds - (now - self._last_request_at)
            if remaining > 0:
                self._sleep(remaining)
        self._last_request_at = self._monotonic()

    def fetch(self, symbol: str, start: date, end: date) -> FugleResponse:
        code = symbol.split(".", 1)[0]
        params = safe_query_params(start, end)
        for attempt in range(1, self._max_attempts + 1):
            self._wait_for_slot()
            try:
                response = self._client.get(
                    FUGLE_ENDPOINT.format(symbol=code),
                    params=params,
                    headers={"X-API-KEY": self._api_key},
                )
            except httpx.HTTPError as exc:
                if attempt == self._max_attempts:
                    raise FugleSourceError("Fugle request failed after bounded retries") from exc
                self._sleep(min(8.0, 2.0 ** (attempt - 1)) + random.uniform(0.0, 0.25))
                continue
            fetched_at = datetime.now(timezone.utc).isoformat()
            if response.status_code in (401, 403):
                raise FugleAccessError(
                    f"Fugle access rejected with HTTP {response.status_code}"
                )
            if response.status_code == 404:
                raise FugleCoverageError("Fugle returned no required historical observation")
            if response.status_code == 429:
                if attempt == self._max_attempts:
                    raise FugleRateLimitError("Fugle rate limit persisted after bounded retries")
                retry_after = _retry_after_seconds(response.headers.get("Retry-After"))
                delay = retry_after if retry_after is not None else 2.0 ** (attempt - 1)
                self._sleep(delay + random.uniform(0.0, 0.25))
                continue
            if 500 <= response.status_code < 600:
                if attempt == self._max_attempts:
                    raise FugleSourceError(
                        "Fugle server error persisted after bounded retries"
                    )
                retry_after = _retry_after_seconds(response.headers.get("Retry-After"))
                delay = retry_after if retry_after is not None else 2.0 ** (attempt - 1)
                self._sleep(delay + random.uniform(0.0, 0.25))
                continue
            if response.status_code >= 400:
                raise FugleSourceError(f"Fugle returned HTTP {response.status_code}")
            return FugleResponse(
                content=response.content,
                status_code=response.status_code,
                fetched_at=fetched_at,
                attempts=attempt,
            )
        raise AssertionError("unreachable retry state")


def _expected_exchange_market(market: str) -> tuple[str, str]:
    if market == "TWSE":
        return "TWSE", "TSE"
    if market == "TPEX":
        return "TPEx", "OTC"
    raise ValueError(f"unsupported market label: {market}")


def validate_probe_payload(case: ProbeCase, payload: Mapping[str, Any]) -> dict[str, Any]:
    expected_exchange, expected_market = _expected_exchange_market(case.market)
    if str(payload.get("symbol")) != case.code:
        raise FugleCoverageError("Fugle response symbol does not match the requested symbol")
    exchange = str(payload.get("exchange") or "")
    if exchange.casefold() != expected_exchange.casefold():
        raise FugleCoverageError("Fugle response exchange disagrees with the frozen route")
    if str(payload.get("market")) != expected_market:
        raise FugleCoverageError("Fugle response market disagrees with the frozen route")
    if str(payload.get("timeframe")) != "D" or str(payload.get("sort")) != "asc":
        raise FugleCoverageError("Fugle response timeframe or ordering is not the requested basis")
    if payload.get("adjusted") not in (None, False):
        raise FugleCoverageError("Fugle response is not demonstrably unadjusted")
    data = payload.get("data")
    if not isinstance(data, list):
        raise FugleCoverageError("Fugle response data is not a list")
    dates = [str(item.get("date")) for item in data if isinstance(item, dict)]
    if dates != sorted(dates) or len(dates) != len(set(dates)):
        raise FugleCoverageError("Fugle response dates are not unique ascending dates")
    matches = [item for item in data if item.get("date") == case.date.isoformat()]
    if len(matches) != 1:
        raise FugleCoverageError("Fugle response lacks exactly one required trading date")
    item = matches[0]
    values: dict[str, float] = {}
    for field in ("open", "high", "low", "close", "volume"):
        value = item.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise FugleCoverageError(f"Fugle {field} is not numeric")
        number = float(value)
        if not math.isfinite(number) or number < 0 or (field != "volume" and number == 0):
            raise FugleCoverageError(f"Fugle {field} is outside the valid domain")
        values[field] = number
    if values["high"] < max(values["open"], values["low"], values["close"]):
        raise FugleCoverageError("Fugle high does not envelope OHLC")
    if values["low"] > min(values["open"], values["high"], values["close"]):
        raise FugleCoverageError("Fugle low does not envelope OHLC")
    return {
        "exchange": exchange,
        "market": str(payload["market"]),
        **values,
    }


def run_formal_probe(
    *,
    issue_csv: Path,
    config_path: Path,
    output_dir: Path,
    client_factory: Callable[[str], FugleHistoricalClient] = FugleHistoricalClient,
) -> dict[str, Any]:
    if output_dir.exists():
        raise FileExistsError(f"refusing to overwrite formal probe: {output_dir}")
    cases = select_probe_cases(issue_csv)
    api_key = load_fugle_api_key(config_path)
    output_dir.mkdir(parents=True)
    raw_dir = output_dir / "raw"
    raw_dir.mkdir()
    rows: list[dict[str, Any]] = []
    with client_factory(api_key) as client:
        for index, case in enumerate(cases, start=1):
            response = client.fetch(case.symbol, case.date, case.date)
            if api_key.encode("utf-8") in response.content:
                raise FugleSourceError("refusing to persist a response containing credential material")
            raw_name = f"{index:02d}_{case.code}_{case.date.isoformat()}.json"
            raw_path = raw_dir / raw_name
            raw_path.write_bytes(response.content)
            try:
                payload = json.loads(response.content)
            except json.JSONDecodeError as exc:
                raise FugleCoverageError("Fugle response is not valid JSON") from exc
            if not isinstance(payload, dict):
                raise FugleCoverageError("Fugle response root is not an object")
            validated = validate_probe_payload(case, payload)
            rows.append(
                {
                    "case_id": f"FUGLE-PROBE-{index:02d}",
                    "symbol": case.symbol,
                    "date": case.date.isoformat(),
                    "period": case.period,
                    "anomaly_reason": case.reason,
                    "route_decision": "INCLUDE_OFFICIAL_MAINBOARD_OHLC",
                    "expected_market": case.market,
                    "response_exchange": validated["exchange"],
                    "response_market": validated["market"],
                    "open": validated["open"],
                    "high": validated["high"],
                    "low": validated["low"],
                    "close": validated["close"],
                    "volume": validated["volume"],
                    "v1_open": case.v1_open,
                    "v1_high": case.v1_high,
                    "v1_low": case.v1_low,
                    "v1_close": case.v1_close,
                    "v1_frozen_location": case.frozen_location,
                    "http_status": response.status_code,
                    "attempts": response.attempts,
                    "fetched_at": response.fetched_at,
                    "raw_path": f"raw/{raw_name}",
                    "raw_sha256": sha256_bytes(response.content),
                    "validation": "PASS",
                }
            )
    results_path = output_dir / "probe_results.csv"
    _write_csv(results_path, rows)
    query_contract = {
        "timeframe": "D",
        "adjusted": "false",
        "fields": FUGLE_FIELDS,
        "sort": "asc",
        "range": "one requested anomaly date per call",
    }
    manifest = {
        "schema_version": "dual-ma-fugle-formal-probe-v1",
        "status": "PASS",
        "source": "Fugle Historical Candles v1.0",
        "official_docs_url": FUGLE_DOCS_URL,
        "endpoint_template": FUGLE_ENDPOINT,
        "credential_source": "explicit local config path; secret excluded from all artifacts",
        "selection_version": PROBE_SELECTION_VERSION,
        "selection_rule": "frozen SHA-256 rank within four period/reason strata; ten distinct route-eligible symbols",
        "query_contract": query_contract,
        "case_count": len(rows),
        "all_pass": all(row["validation"] == "PASS" for row in rows),
        "issue_ledger": {
            "path": str(issue_csv.resolve()),
            "sha256": sha256_file(issue_csv),
        },
        "raw_responses": [
            {
                "case_id": row["case_id"],
                "path": row["raw_path"],
                "sha256": row["raw_sha256"],
                "fetched_at": row["fetched_at"],
                "query": {
                    "symbol": row["symbol"].split(".", 1)[0],
                    "from": row["date"],
                    "to": row["date"],
                    **query_contract,
                },
            }
            for row in rows
        ],
        "transition_route_assertion": {
            "6585.TW@2022-01-11": "EXCLUDE_PRE_MAINBOARD_NO_COMPARABLE_OHLC",
            "reason": "original OPEN_ABOVE_HIGH anomaly predates the official 2022-05-20 TWSE listing",
        },
    }
    manifest_path = output_dir / "probe_manifest.json"
    _write_json(manifest_path, manifest)
    report = f"""# 雙均線 2022 Fugle 正式來源 probe

狀態：`PASS`

- 依凍結規則選取 10 個不同股票的原始 OHLC 異常，2021／2022 各 5 筆，且涵蓋 close 高於 high 與 close 低於 low。
- 10 筆均以 Fugle Historical Candles v1.0、`adjusted=false`、日 K、OHLCV、升冪查詢通過日期、市場與 OHLC 包絡驗證。
- 每次回應原始 bytes 已落地並記錄 SHA-256 與 UTC 擷取時間；API key 未寫入任何產物。
- `6585.TW@2022-01-11` 的唯一 `OPEN_ABOVE_HIGH` 異常在正式上市日前，依已凍結市場路由排除，不以興櫃均價／末價合成 OHLC。

下一閘門：可擴展至 135 檔，按 2021、2022 各自小於一年的區段擷取未還原 OHLC。
"""
    (output_dir / "probe_report.md").write_text(report, encoding="utf-8")
    artifact_rows: list[dict[str, Any]] = []
    for path in sorted(item for item in output_dir.rglob("*") if item.is_file()):
        if path.name == "artifact_manifest.json":
            continue
        artifact_rows.append(
            {
                "path": path.relative_to(output_dir).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    _write_json(
        output_dir / "artifact_manifest.json",
        {
            "schema_version": "dual-ma-artifact-manifest-v1",
            "status": "PASS",
            "artifacts": artifact_rows,
        },
    )
    return manifest
