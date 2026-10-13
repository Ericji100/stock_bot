from __future__ import annotations

import hashlib
import html
import json
import os
import re
import time
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import httpx

from stock_ai_bot.data_sources.causal_price_contract import (
    ACTION_LEDGER_SCHEMA,
    ActionKind,
    CorporateActionEvent,
    CorporateActionLedger,
    CorporateActionType,
    DataQualityError,
    canonical_bytes,
    canonical_sha256,
)
from stock_ai_bot.data_sources.causal_price_repository import write_immutable_json


OFFICIAL_ACTION_SOURCE_SCHEMA = "stock-official-corporate-action-source-v1"
OFFICIAL_ACTION_BUNDLE_SCHEMA = "stock-official-corporate-action-bundle-v1"


@dataclass(frozen=True)
class OfficialRequest:
    request_id: str
    exchange: str
    family: str
    method: str
    url: str
    query: Mapping[str, str]


@dataclass(frozen=True)
class OfficialHttpResponse:
    content: bytes
    status_code: int
    fetched_at: datetime
    content_type: str = "application/json"


class OfficialCorporateActionClient:
    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        minimum_interval_seconds: float = 1.1,
        sleeper: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=30.0, follow_redirects=True)
        self._minimum_interval_seconds = minimum_interval_seconds
        self._sleeper = sleeper
        self._monotonic = monotonic
        self._last_request_at: float | None = None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "OfficialCorporateActionClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def fetch(self, request: OfficialRequest) -> OfficialHttpResponse:
        now = self._monotonic()
        if self._last_request_at is not None:
            wait = self._minimum_interval_seconds - (now - self._last_request_at)
            if wait > 0:
                self._sleeper(wait)
        if request.method == "GET":
            response = self._client.get(request.url, params=dict(request.query))
        elif request.method == "POST":
            response = self._client.post(request.url, data=dict(request.query))
        else:
            raise ValueError(f"unsupported HTTP method: {request.method}")
        self._last_request_at = self._monotonic()
        return OfficialHttpResponse(
            content=response.content,
            status_code=response.status_code,
            fetched_at=datetime.now(timezone.utc),
            content_type=response.headers.get("content-type", ""),
        )


def build_main_request_plan(start: date, end: date) -> list[OfficialRequest]:
    if start > end:
        raise ValueError("start is after end")
    compact = {"startDate": start.strftime("%Y%m%d"), "endDate": end.strftime("%Y%m%d"), "response": "json"}
    slash = {"startDate": start.strftime("%Y/%m/%d"), "endDate": end.strftime("%Y/%m/%d")}
    return [
        OfficialRequest("twse-ex-right", "TWSE", "EX_RIGHT", "GET", "https://www.twse.com.tw/rwd/zh/exRight/TWT49U", compact),
        OfficialRequest("twse-reduction", "TWSE", "CAPITAL_REDUCTION", "GET", "https://www.twse.com.tw/rwd/zh/reducation/TWTAUU", compact),
        OfficialRequest("twse-par-value", "TWSE", "PAR_VALUE_CHANGE", "GET", "https://www.twse.com.tw/rwd/zh/change/TWTB8U", compact),
        OfficialRequest("twse-split", "TWSE", "SPLIT", "GET", "https://www.twse.com.tw/rwd/zh/split/TWTCAU", compact),
        OfficialRequest("tpex-ex-right", "TPEX", "EX_RIGHT", "POST", "https://www.tpex.org.tw/www/zh-tw/bulletin/exDailyQ", slash),
        OfficialRequest("tpex-reduction", "TPEX", "CAPITAL_REDUCTION", "POST", "https://www.tpex.org.tw/www/zh-tw/bulletin/revivt", slash),
        OfficialRequest("tpex-par-value", "TPEX", "PAR_VALUE_CHANGE", "POST", "https://www.tpex.org.tw/www/zh-tw/bulletin/pvChgRslt", slash),
    ]


def build_twse_detail_request_plan(
    *,
    ex_right_payload: Mapping[str, Any],
    reduction_payload: Mapping[str, Any],
    universe_codes: set[str] | None = None,
) -> list[OfficialRequest]:
    requests: list[OfficialRequest] = []
    for row in _records(ex_right_payload, tpex=False):
        code = str(row["股票代號"]).strip()
        if universe_codes is not None and code not in universe_codes:
            continue
        observed_on = parse_roc_date(row["資料日期"])
        requests.append(
            OfficialRequest(
                request_id=f"twse-ex-right-detail-{code}-{observed_on:%Y%m%d}",
                exchange="TWSE",
                family="EX_RIGHT_DETAIL",
                method="GET",
                url="https://www.twse.com.tw/rwd/zh/exRight/TWT49UDetail",
                query={"STK_NO": code, "T1": observed_on.strftime("%Y%m%d"), "response": "json"},
            )
        )
    for row in _records(reduction_payload, tpex=False):
        code = str(row["股票代號"]).strip()
        if universe_codes is not None and code not in universe_codes:
            continue
        detail_key = str(row.get("詳細資料") or "")
        values = [value.strip() for value in detail_key.split(",")]
        file_date = values[1] if len(values) > 1 and re.fullmatch(r"\d{8}", values[1]) else None
        if file_date is None:
            raise DataQualityError(f"TWSE reduction detail key changed: {detail_key!r}")
        requests.append(
            OfficialRequest(
                request_id=f"twse-reduction-detail-{code}-{file_date}",
                exchange="TWSE",
                family="CAPITAL_REDUCTION_DETAIL",
                method="GET",
                url="https://www.twse.com.tw/rwd/zh/reducation/TWTAVUDetail",
                query={"STK_NO": code, "FILE_DATE": file_date, "response": "json"},
            )
        )
    return requests


def _passed_evidence(
    snapshot_dir: Path, request_id: str
) -> tuple[dict[str, Any], datetime, str]:
    state = json.loads((snapshot_dir / "fetch_state.json").read_text(encoding="utf-8-sig"))
    checkpoint = state["receipts"].get(request_id)
    if not checkpoint or checkpoint.get("status") != "PASS":
        raise DataQualityError(f"official request is not PASS: {request_id}")
    attempt = checkpoint["attempts"][-1]
    response = OfficialHttpResponse(
        content=(snapshot_dir / attempt["raw_path"]).read_bytes(),
        status_code=attempt["http_status"],
        fetched_at=datetime.fromisoformat(attempt["fetched_at"]),
        content_type=attempt["content_type"],
    )
    return _decode_and_validate_response(response), response.fetched_at, attempt["raw_sha256"]


def _passed_payload(snapshot_dir: Path, request_id: str) -> dict[str, Any]:
    return _passed_evidence(snapshot_dir, request_id)[0]


def fetch_official_action_bundle(
    *,
    output_dir: Path,
    start: date,
    end: date,
    universe_codes: set[str],
    client: OfficialCorporateActionClient,
    max_attempts_per_request: int = 3,
    progress: Callable[[str, str], None] | None = None,
    stop_requested: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    final_path = output_dir / "bundle_manifest.json"
    if final_path.is_file():
        manifest = json.loads(final_path.read_text(encoding="utf-8-sig"))
        if manifest.get("schema_version") != OFFICIAL_ACTION_BUNDLE_SCHEMA:
            raise DataQualityError("official action bundle schema changed")
        return manifest
    output_dir.mkdir(parents=True, exist_ok=True)
    main_dir = output_dir / "main"
    main = fetch_official_source_snapshot(
        output_dir=main_dir,
        start=start,
        end=end,
        client=client,
        max_attempts_per_request=max_attempts_per_request,
        progress=progress,
        stop_requested=stop_requested,
    )
    if main.get("status") == "PAUSED":
        return {"schema_version": OFFICIAL_ACTION_BUNDLE_SCHEMA, "status": "PAUSED", "main": main}
    if main.get("status") != "PASS":
        return {
            "schema_version": OFFICIAL_ACTION_BUNDLE_SCHEMA,
            "status": "UNRESOLVED_MAIN_SOURCE",
            "main": main,
        }
    detail_plan = build_twse_detail_request_plan(
        ex_right_payload=_passed_payload(main_dir, "twse-ex-right"),
        reduction_payload=_passed_payload(main_dir, "twse-reduction"),
        universe_codes=universe_codes,
    )
    details_dir = output_dir / "details"
    details = fetch_official_source_snapshot(
        output_dir=details_dir,
        start=start,
        end=end,
        client=client,
        requests=detail_plan,
        max_attempts_per_request=max_attempts_per_request,
        progress=progress,
        stop_requested=stop_requested,
    )
    if details.get("status") == "PAUSED":
        return {
            "schema_version": OFFICIAL_ACTION_BUNDLE_SCHEMA,
            "status": "PAUSED",
            "main": main,
            "details": details,
        }
    if details.get("status") != "PASS":
        return {
            "schema_version": OFFICIAL_ACTION_BUNDLE_SCHEMA,
            "status": "UNRESOLVED_DETAIL_SOURCE",
            "main": main,
            "details": details,
        }
    core = {
        "schema_version": OFFICIAL_ACTION_BUNDLE_SCHEMA,
        "status": "PASS",
        "coverage": {"from": start.isoformat(), "through": end.isoformat()},
        "universe_codes": sorted(universe_codes),
        "universe_sha256": canonical_sha256(sorted(universe_codes)),
        "main_snapshot": {
            "path": "main/source_manifest.json",
            "snapshot_id": main["snapshot_id"],
            "content_sha256": main["content_sha256"],
        },
        "detail_snapshot": {
            "path": "details/source_manifest.json",
            "snapshot_id": details["snapshot_id"],
            "content_sha256": details["content_sha256"],
        },
    }
    content_sha256 = canonical_sha256(core)
    manifest = {
        **core,
        "snapshot_id": f"stock-official-action-bundle#{content_sha256[:16]}",
        "content_sha256": content_sha256,
    }
    write_immutable_json(final_path, manifest)
    artifacts = []
    for path in sorted(item for item in output_dir.rglob("*") if item.is_file()):
        if path.name == "bundle_artifact_manifest.json":
            continue
        artifacts.append(
            {
                "path": path.relative_to(output_dir).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": _sha256_bytes(path.read_bytes()),
            }
        )
    write_immutable_json(
        output_dir / "bundle_artifact_manifest.json",
        {"schema_version": "stock-artifact-manifest-v1", "status": "PASS", "artifacts": artifacts},
    )
    return manifest


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _remote_protection_detected(response: OfficialHttpResponse) -> bool:
    """Recognise exchange anti-automation challenges and pause fail-closed."""
    if response.status_code == 428:
        return True
    sample = response.content[:16_384].lower()
    return response.status_code == 307 or b"anti-ddos flood protection" in sample


def _atomic_json(path: Path, value: Any) -> None:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(encoded, encoding="utf-8")
    os.replace(temporary, path)


def fetch_official_source_snapshot(
    *,
    output_dir: Path,
    start: date,
    end: date,
    client: OfficialCorporateActionClient,
    requests: Sequence[OfficialRequest] | None = None,
    max_attempts_per_request: int = 3,
    progress: Callable[[str, str], None] | None = None,
    stop_requested: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    if max_attempts_per_request < 1:
        raise ValueError("max_attempts_per_request must be positive")
    plan = list(requests or build_main_request_plan(start, end))
    plan_hash = canonical_sha256([request.__dict__ for request in plan])
    state_path = output_dir / "fetch_state.json"
    raw_dir = output_dir / "raw"
    if output_dir.exists():
        if not state_path.is_file():
            raise FileExistsError("refusing to reuse a non-checkpoint official snapshot")
        state = json.loads(state_path.read_text(encoding="utf-8-sig"))
        if state.get("plan_sha256") != plan_hash:
            raise DataQualityError("official source checkpoint plan hash changed")
    else:
        raw_dir.mkdir(parents=True)
        state = {
            "schema_version": OFFICIAL_ACTION_SOURCE_SCHEMA,
            "status": "IN_PROGRESS",
            "coverage": {"from": start.isoformat(), "through": end.isoformat()},
            "plan_sha256": plan_hash,
            "plan": [request.__dict__ for request in plan],
            "receipts": {},
        }
        _atomic_json(state_path, state)
    for request in plan:
        checkpoint = state["receipts"].get(request.request_id) or {
            "status": "PENDING",
            "attempts": [],
        }
        if checkpoint.get("status") == "PASS":
            passed = checkpoint["attempts"][-1]
            raw_path = output_dir / passed["raw_path"]
            if not raw_path.is_file() or _sha256_bytes(raw_path.read_bytes()) != passed["raw_sha256"]:
                raise DataQualityError(f"official PASS raw hash mismatch: {request.request_id}")
            continue
        if stop_requested is not None and stop_requested():
            state["status"] = "PAUSED"
            _atomic_json(state_path, state)
            return {
                "schema_version": OFFICIAL_ACTION_SOURCE_SCHEMA,
                "status": "PAUSED",
                "coverage": state["coverage"],
                "next_request_id": request.request_id,
            }
        remote_protection = False
        for _ in range(max_attempts_per_request):
            response = client.fetch(request)
            attempt_number = len(checkpoint["attempts"]) + 1
            pass_candidate = response.status_code == 200
            validation_error: str | None = None
            if pass_candidate:
                try:
                    _decode_and_validate_response(response)
                except DataQualityError as exc:
                    validation_error = str(exc)
                    pass_candidate = False
            if pass_candidate:
                raw_path = raw_dir / f"{request.request_id}.json"
            else:
                raw_path = raw_dir / f"{request.request_id}.attempt-{attempt_number}.json"
            if raw_path.exists() and raw_path.read_bytes() != response.content:
                raise FileExistsError(f"refusing to overwrite official raw response: {raw_path}")
            raw_path.write_bytes(response.content)
            attempt = {
                "attempt": attempt_number,
                "fetched_at": response.fetched_at.isoformat(),
                "http_status": response.status_code,
                "content_type": response.content_type,
                "raw_path": raw_path.relative_to(output_dir).as_posix(),
                "raw_sha256": _sha256_bytes(response.content),
                "validation_error": validation_error,
            }
            checkpoint["attempts"].append(attempt)
            if pass_candidate:
                checkpoint["status"] = "PASS"
                break
            checkpoint["status"] = "FAILED"
            if _remote_protection_detected(response):
                remote_protection = True
                break
            if response.status_code not in {429, 500, 502, 503, 504}:
                break
        checkpoint.update(
            {
                "request_id": request.request_id,
                "exchange": request.exchange,
                "family": request.family,
                "method": request.method,
                "url": request.url,
                "query": dict(request.query),
            }
        )
        state["receipts"][request.request_id] = checkpoint
        _atomic_json(state_path, state)
        if progress is not None:
            progress(request.request_id, checkpoint["status"])
        if remote_protection:
            state["status"] = "PAUSED"
            state["pause_reason"] = "REMOTE_PROTECTION"
            state["next_request_id"] = request.request_id
            _atomic_json(state_path, state)
            return {
                "schema_version": OFFICIAL_ACTION_SOURCE_SCHEMA,
                "status": "PAUSED",
                "coverage": state["coverage"],
                "pause_reason": "REMOTE_PROTECTION",
                "next_request_id": request.request_id,
            }
    failures = [receipt for receipt in state["receipts"].values() if receipt["status"] != "PASS"]
    if failures:
        state["status"] = "UNRESOLVED_SOURCE"
        _atomic_json(state_path, state)
        return {
            "schema_version": OFFICIAL_ACTION_SOURCE_SCHEMA,
            "status": state["status"],
            "coverage": state["coverage"],
            "failed_request_ids": sorted(item["request_id"] for item in failures),
        }
    state["status"] = "PASS"
    _atomic_json(state_path, state)
    manifest_core = {
        "schema_version": OFFICIAL_ACTION_SOURCE_SCHEMA,
        "status": "PASS",
        "coverage": state["coverage"],
        "plan_sha256": plan_hash,
        "receipts": [state["receipts"][key] for key in sorted(state["receipts"])],
    }
    manifest_hash = canonical_sha256(manifest_core)
    manifest = {
        **manifest_core,
        "snapshot_id": f"stock-official-actions#{manifest_hash[:16]}",
        "content_sha256": manifest_hash,
    }
    write_immutable_json(output_dir / "source_manifest.json", manifest)
    artifacts = []
    for path in sorted(item for item in output_dir.rglob("*") if item.is_file()):
        if path.name == "artifact_manifest.json":
            continue
        artifacts.append(
            {
                "path": path.relative_to(output_dir).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": _sha256_bytes(path.read_bytes()),
            }
        )
    write_immutable_json(
        output_dir / "artifact_manifest.json",
        {"schema_version": "stock-artifact-manifest-v1", "status": "PASS", "artifacts": artifacts},
    )
    return manifest


def _decode_and_validate_response(response: OfficialHttpResponse) -> dict[str, Any]:
    if "json" not in response.content_type.casefold():
        raise DataQualityError("official response content type is not JSON")
    try:
        payload = json.loads(response.content)
    except json.JSONDecodeError as exc:
        raise DataQualityError("official response is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise DataQualityError("official response root is not an object")
    status = str(payload.get("stat") or payload.get("status") or "").strip().casefold()
    if status != "ok":
        raise DataQualityError(f"official response status is not OK: {status!r}")
    return payload


def parse_roc_date(value: Any) -> date:
    text = str(value).strip().replace("年", "/").replace("月", "/").replace("日", "")
    if re.fullmatch(r"\d{8}", text):
        return datetime.strptime(text, "%Y%m%d").date()
    if re.fullmatch(r"\d{7}", text):
        return date(int(text[:3]) + 1911, int(text[3:5]), int(text[5:7]))
    parts = text.split("/")
    if len(parts) == 3:
        year = int(parts[0])
        if year < 1911:
            year += 1911
        return date(year, int(parts[1]), int(parts[2]))
    raise DataQualityError(f"unsupported official date: {value!r}")


def parse_number(value: Any) -> float | None:
    if value is None:
        return None
    text = html.unescape(str(value)).replace(",", "").strip()
    if not text or text.upper() in {"--", "NA", "N/A", "NULL"}:
        return None
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    return float(match.group(0)) if match else None


def _records(payload: Mapping[str, Any], *, tpex: bool) -> list[dict[str, Any]]:
    status = str(payload.get("stat") or payload.get("status") or "").strip().casefold()
    if status != "ok":
        raise DataQualityError(f"official response status is not OK: {status!r}")
    if tpex:
        tables = payload.get("tables")
        if not isinstance(tables, list) or not tables:
            raise DataQualityError("TPEx response has no table")
        table = tables[0]
        fields, data = table.get("fields"), table.get("data")
    else:
        fields, data = payload.get("fields"), payload.get("data")
    if not isinstance(fields, list) or not isinstance(data, list):
        raise DataQualityError("official response fields/data schema changed")
    rows: list[dict[str, Any]] = []
    for values in data:
        if not isinstance(values, list) or len(values) != len(fields):
            raise DataQualityError("official response row width differs from fields")
        rows.append(dict(zip((str(field) for field in fields), values)))
    return rows


def _detail_record(payload: Mapping[str, Any]) -> dict[str, Any]:
    status = str(payload.get("stat") or payload.get("status") or "").strip().casefold()
    if status != "ok":
        raise DataQualityError(f"TWSE detail response status is not OK: {status!r}")
    fields, data = payload.get("fields"), payload.get("data")
    if not isinstance(fields, list) or not isinstance(data, list):
        raise DataQualityError("TWSE detail response fields/data schema changed")
    if data and isinstance(data[0], list):
        values = data[0]
    else:
        values = data
    if len(values) != len(fields):
        raise DataQualityError("TWSE detail row width differs from fields")
    return dict(zip((str(field) for field in fields), values))


def _row_value(row: Mapping[str, Any], *aliases: str) -> Any:
    present = [alias for alias in aliases if alias in row]
    if len(present) != 1:
        raise DataQualityError(
            f"official field schema changed; expected exactly one of {aliases!r}, found {present!r}"
        )
    return row[present[0]]


def _finish_event_hash(event: CorporateActionEvent) -> CorporateActionEvent:
    return replace(event, event_sha256=canonical_sha256(event.event_lineage_payload()))


def _build_distribution_event(
    *,
    exchange: str,
    code: str,
    observed_on: date,
    previous_close: float,
    reference_price: float,
    cash_dividend: float,
    stock_shares_per_thousand: float,
    cash_subscription_shares: float,
    source_name: str,
    source_endpoint: str,
    fetched_at: datetime,
    raw_sha256: str,
) -> CorporateActionEvent:
    if previous_close <= 0 or reference_price <= 0:
        raise DataQualityError("official previous/reference price must be positive")
    has_cash = cash_dividend > 0
    has_share = stock_shares_per_thousand > 0
    unsupported_rights = cash_subscription_shares > 0
    cash_factor = (previous_close - cash_dividend) / previous_close if has_cash else 1.0
    total_factor = reference_price / previous_close
    if cash_factor <= 0:
        technical_status = "UNRESOLVED_FOR_TECHNICAL"
        technical_reason = "cash distribution is not compatible with a positive price transform"
        share_factor = None
        volume_factor = None
        rights_factor = None
    else:
        share_factor = (
            1.0 / (1.0 + stock_shares_per_thousand / 1000.0)
            if has_share and unsupported_rights
            else total_factor / cash_factor if has_share else 1.0
        )
        volume_factor = 1.0 / share_factor if has_share else 1.0
        rights_factor = (
            total_factor / (cash_factor * share_factor) if unsupported_rights else 1.0
        )
        if rights_factor <= 0:
            technical_status = "UNRESOLVED_FOR_TECHNICAL"
            technical_reason = "official rights reference yields a non-positive transform"
        else:
            technical_status = "RESOLVED_FOR_TECHNICAL"
            technical_reason = None
    if unsupported_rights:
        action_type = CorporateActionType.CASH_CAPITAL_INCREASE_RIGHTS
        kind = ActionKind.COMBINED if has_cash or has_share else ActionKind.RIGHTS
        rights_policy = "QUARANTINED_UNSUPPORTED_POSITION"
        position_status = "QUARANTINED_UNSUPPORTED"
        position_reason = (
            "cash-capital-increase rights lack a verified exercise/payment/disposal model"
        )
    elif has_cash and has_share:
        action_type = CorporateActionType.CASH_AND_STOCK_DIVIDEND
        kind = ActionKind.COMBINED
        rights_policy = "NONE"
        position_status = "RECEIVABLE_TRACKED"
        position_reason = None
    elif has_share:
        action_type = CorporateActionType.STOCK_DIVIDEND
        kind = ActionKind.SHARE
        rights_policy = "NONE"
        position_status = "RECEIVABLE_TRACKED"
        position_reason = None
    elif has_cash:
        action_type = CorporateActionType.CASH_DIVIDEND
        kind = ActionKind.CASH
        rights_policy = "NONE"
        position_status = "RECEIVABLE_TRACKED"
        position_reason = None
    else:
        action_type = CorporateActionType.OTHER
        kind = ActionKind.OTHER
        rights_policy = "UNSUPPORTED_UNMAPPED_ACTION"
        position_status = "QUARANTINED_UNSUPPORTED"
        position_reason = "official action is not mapped"
        technical_status = "UNRESOLVED_FOR_TECHNICAL"
        technical_reason = "official row has no mapped cash/share/rights distribution"
    event = CorporateActionEvent(
        action_id=f"{exchange}:{code}:{observed_on.isoformat()}:DISTRIBUTION",
        instrument_id=f"{exchange}:{code}",
        action_type=action_type,
        action_kind=kind,
        effective_date=observed_on,
        ex_date=observed_on,
        available_on=observed_on,
        announcement_date=None,
        entitlement_date=observed_on,
        payment_date=None,
        position_lifecycle_status=position_status,
        position_lifecycle_reason=position_reason,
        rights_policy=rights_policy,
        halt_date=None,
        resume_date=None,
        cash_dividend=cash_dividend or None,
        stock_dividend_shares=stock_shares_per_thousand or None,
        split_ratio=None,
        cash_price_factor=cash_factor if has_cash else None,
        share_price_factor=share_factor if has_share else None,
        share_volume_factor=volume_factor if has_share else None,
        rights_price_factor=rights_factor if unsupported_rights else None,
        previous_close=previous_close,
        reference_price=reference_price,
        technical_resolution_status=technical_status,
        technical_resolution_reason=technical_reason,
        source_name=source_name,
        source_endpoint=source_endpoint,
        fetched_at=fetched_at,
        raw_response_sha256=raw_sha256,
        event_sha256="0" * 64,
    )
    return _finish_event_hash(event)


def _build_share_basis_event(
    *,
    exchange: str,
    code: str,
    observed_on: date,
    action_type: CorporateActionType,
    previous_close: float,
    reference_price: float,
    new_shares_per_thousand: float | None,
    cash_refund_per_share: float,
    cash_subscription_shares: float,
    halt_date: date | None,
    source_name: str,
    source_endpoint: str,
    fetched_at: datetime,
    raw_sha256: str,
) -> CorporateActionEvent:
    if previous_close <= 0 or reference_price <= 0:
        raise DataQualityError("official previous/reference price must be positive")
    total_factor = reference_price / previous_close
    unsupported_rights = cash_subscription_shares > 0
    has_cash = cash_refund_per_share > 0
    if new_shares_per_thousand is None or new_shares_per_thousand <= 0:
        technical_status = "UNRESOLVED_FOR_TECHNICAL"
        technical_reason = "share-basis event has no verified new-share ratio"
        share_factor = None
        volume_factor = None
        cash_factor = None
        rights_factor = None
    else:
        volume_factor = new_shares_per_thousand / 1000.0
        share_factor = 1.0 / volume_factor
        cash_factor = (
            (previous_close - cash_refund_per_share) / previous_close
            if has_cash and unsupported_rights
            else total_factor / share_factor if has_cash else 1.0
        )
        rights_factor = (
            total_factor / (cash_factor * share_factor) if unsupported_rights else 1.0
        )
        if cash_factor <= 0:
            technical_status = "UNRESOLVED_FOR_TECHNICAL"
            technical_reason = "cash refund yields a non-positive transform"
        elif rights_factor <= 0:
            technical_status = "UNRESOLVED_FOR_TECHNICAL"
            technical_reason = "official rights reference yields a non-positive transform"
        else:
            technical_status = "RESOLVED_FOR_TECHNICAL"
            technical_reason = None
    kind = (
        ActionKind.COMBINED
        if has_cash or unsupported_rights
        else ActionKind.SHARE
    )
    event = CorporateActionEvent(
        action_id=f"{exchange}:{code}:{observed_on.isoformat()}:{action_type.value}",
        instrument_id=f"{exchange}:{code}",
        action_type=(
            CorporateActionType.CASH_CAPITAL_INCREASE_RIGHTS
            if unsupported_rights
            else action_type
        ),
        action_kind=kind,
        effective_date=observed_on,
        ex_date=None,
        available_on=observed_on,
        announcement_date=None,
        entitlement_date=observed_on,
        payment_date=None,
        position_lifecycle_status=(
            "QUARANTINED_UNSUPPORTED"
            if unsupported_rights
            else "RECEIVABLE_TRACKED" if has_cash else "SETTLED"
        ),
        position_lifecycle_reason=(
            "cash-capital-increase rights lack a verified exercise/payment/disposal model"
            if unsupported_rights
            else None
        ),
        rights_policy=(
            "QUARANTINED_UNSUPPORTED_POSITION" if unsupported_rights else "NONE"
        ),
        halt_date=halt_date,
        resume_date=observed_on,
        cash_dividend=cash_refund_per_share or None,
        stock_dividend_shares=new_shares_per_thousand,
        split_ratio=(new_shares_per_thousand / 1000.0) if new_shares_per_thousand else None,
        cash_price_factor=cash_factor if has_cash else None,
        share_price_factor=share_factor,
        share_volume_factor=volume_factor,
        rights_price_factor=rights_factor if unsupported_rights else None,
        previous_close=previous_close,
        reference_price=reference_price,
        technical_resolution_status=technical_status,
        technical_resolution_reason=technical_reason,
        source_name=source_name,
        source_endpoint=source_endpoint,
        fetched_at=fetched_at,
        raw_response_sha256=raw_sha256,
        event_sha256="0" * 64,
    )
    return _finish_event_hash(event)


def normalize_twse_ex_rights(
    payload: Mapping[str, Any],
    *,
    details: Mapping[tuple[str, date], Mapping[str, Any]],
    detail_sha256s: Mapping[tuple[str, date], str] | None = None,
    fetched_at: datetime,
    raw_sha256: str,
) -> list[CorporateActionEvent]:
    events: list[CorporateActionEvent] = []
    for row in _records(payload, tpex=False):
        observed_on = parse_roc_date(_row_value(row, "資料日期"))
        code = str(_row_value(row, "股票代號")).strip()
        detail_payload = details.get((code, observed_on))
        if detail_payload is None:
            raise DataQualityError(f"missing TWSE ex-right detail for {code} {observed_on}")
        detail = _detail_record(detail_payload)
        event_raw_sha256 = (
            canonical_sha256(
                {"main_raw_sha256": raw_sha256, "detail_raw_sha256": detail_sha256s[(code, observed_on)]}
            )
            if detail_sha256s is not None
            else raw_sha256
        )
        events.append(
            _build_distribution_event(
                exchange="TWSE",
                code=code,
                observed_on=observed_on,
                previous_close=float(parse_number(_row_value(row, "除權息前收盤價")) or 0),
                reference_price=float(parse_number(_row_value(row, "除權息參考價")) or 0),
                cash_dividend=float(parse_number(_row_value(detail, "(每股配發現金股利)除息")) or 0),
                stock_shares_per_thousand=float(parse_number(_row_value(detail, "A. 按普通股股東持股比例每千股無償配股")) or 0),
                cash_subscription_shares=float(
                    (parse_number(_row_value(detail, " c. 原股東認購", "c. 原股東認購")) or 0)
                    + (parse_number(_row_value(detail, "按股東持股比例每千股認購")) or 0)
                ),
                source_name="TWSE TWT49U/TWT49UDetail",
                source_endpoint="https://www.twse.com.tw/rwd/zh/exRight/",
                fetched_at=fetched_at,
                raw_sha256=event_raw_sha256,
            )
        )
    return events


def normalize_tpex_ex_rights(
    payload: Mapping[str, Any], *, fetched_at: datetime, raw_sha256: str
) -> list[CorporateActionEvent]:
    events: list[CorporateActionEvent] = []
    for row in _records(payload, tpex=True):
        events.append(
            _build_distribution_event(
                exchange="TPEX",
                code=str(_row_value(row, "代號", "股票代號")).strip(),
                observed_on=parse_roc_date(_row_value(row, "除權息日期")),
                previous_close=float(parse_number(_row_value(row, "除權息前收盤價")) or 0),
                reference_price=float(parse_number(_row_value(row, "除權息參考價")) or 0),
                cash_dividend=float(parse_number(_row_value(row, "現金股利")) or 0),
                stock_shares_per_thousand=float(parse_number(_row_value(row, "每仟股無償配股", "每千股無償配股")) or 0),
                cash_subscription_shares=float(
                    (parse_number(_row_value(row, "原股東認購股數")) or 0)
                    + (parse_number(_row_value(row, "按持股比例仟股認購", "按股東持股比例每千股認購")) or 0)
                ),
                source_name="TPEx exDailyQ",
                source_endpoint="https://www.tpex.org.tw/www/zh-tw/bulletin/exDailyQ",
                fetched_at=fetched_at,
                raw_sha256=raw_sha256,
            )
        )
    return events


def normalize_twse_reductions(
    payload: Mapping[str, Any],
    *,
    details: Mapping[tuple[str, str], Mapping[str, Any]],
    detail_sha256s: Mapping[tuple[str, str], str] | None = None,
    fetched_at: datetime,
    raw_sha256: str,
) -> list[CorporateActionEvent]:
    events: list[CorporateActionEvent] = []
    for row in _records(payload, tpex=False):
        code = str(_row_value(row, "股票代號")).strip()
        detail_key = str(row.get("詳細資料") or "")
        values = [value.strip() for value in detail_key.split(",")]
        file_date = values[1] if len(values) > 1 else ""
        detail_payload = details.get((code, file_date))
        if detail_payload is None:
            raise DataQualityError(f"missing TWSE reduction detail for {code} {file_date}")
        detail = _detail_record(detail_payload)
        event_raw_sha256 = (
            canonical_sha256(
                {"main_raw_sha256": raw_sha256, "detail_raw_sha256": detail_sha256s[(code, file_date)]}
            )
            if detail_sha256s is not None
            else raw_sha256
        )
        events.append(
            _build_share_basis_event(
                exchange="TWSE",
                code=code,
                observed_on=parse_roc_date(_row_value(row, "恢復買賣日期")),
                action_type=CorporateActionType.CAPITAL_REDUCTION,
                previous_close=float(parse_number(_row_value(row, "停止買賣前收盤價格")) or 0),
                reference_price=float(parse_number(_row_value(row, "恢復買賣參考價")) or 0),
                new_shares_per_thousand=parse_number(_row_value(detail, "每壹仟股換發新股票：", "每壹仟股換發新股票")),
                cash_refund_per_share=float(parse_number(_row_value(detail, "每股退還股款：", "每股退還股款")) or 0),
                cash_subscription_shares=float(
                    (parse_number(_row_value(detail, "c. 原股東認購：", "c. 原股東認購")) or 0)
                    + (parse_number(_row_value(detail, "按股東持股比例每千股認購：", "按股東持股比例每千股認購")) or 0)
                ),
                halt_date=parse_roc_date(_row_value(detail, "停止買賣日期：", "停止買賣日期")),
                source_name="TWSE TWTAUU/TWTAVUDetail",
                source_endpoint="https://www.twse.com.tw/rwd/zh/reducation/",
                fetched_at=fetched_at,
                raw_sha256=event_raw_sha256,
            )
        )
    return events


def normalize_tpex_reductions(
    payload: Mapping[str, Any], *, fetched_at: datetime, raw_sha256: str
) -> list[CorporateActionEvent]:
    events: list[CorporateActionEvent] = []
    for row in _records(payload, tpex=True):
        detail_html = str(_row_value(row, "詳細資料"))
        events.append(
            _build_share_basis_event(
                exchange="TPEX",
                code=str(_row_value(row, "股票代號", "證券代號")).strip(),
                observed_on=parse_roc_date(_row_value(row, "恢復買賣日期")),
                action_type=CorporateActionType.CAPITAL_REDUCTION,
                previous_close=float(parse_number(_row_value(row, "最後交易日之收盤價格", "停止買賣前收盤價格")) or 0),
                reference_price=float(parse_number(_row_value(row, "減資恢復買賣開始日參考價格", "恢復買賣參考價")) or 0),
                new_shares_per_thousand=extract_html_number(detail_html, "每壹仟股換發新股票"),
                cash_refund_per_share=float(extract_html_number(detail_html, "每股退還股款") or 0),
                cash_subscription_shares=float(extract_html_number(detail_html, "現金增資總股數") or 0),
                halt_date=(
                    parse_roc_date(_extract_html_text(detail_html, "停止買賣日期"))
                    if _extract_html_text(detail_html, "停止買賣日期")
                    else None
                ),
                source_name="TPEx revivt",
                source_endpoint="https://www.tpex.org.tw/www/zh-tw/bulletin/revivt",
                fetched_at=fetched_at,
                raw_sha256=raw_sha256,
            )
        )
    return events


def _normalize_par_value_changes(
    payload: Mapping[str, Any],
    *,
    exchange: str,
    tpex: bool,
    fetched_at: datetime,
    raw_sha256: str,
) -> list[CorporateActionEvent]:
    events: list[CorporateActionEvent] = []
    for row in _records(payload, tpex=tpex):
        code_key = "證券代號" if tpex else "股票代號"
        previous_key = "最後交易日之收盤價格" if tpex else "停止買賣前收盤價格"
        reference_key = "恢復買賣開始參考價" if tpex else "恢復買賣參考價"
        detail_html = str(row.get("詳細資料") or "")
        ratio = extract_html_number(detail_html, "變更股票面額換股率") if tpex else None
        previous_close = float(parse_number(_row_value(row, previous_key)) or 0)
        reference_price = float(parse_number(_row_value(row, reference_key)) or 0)
        if ratio is None and previous_close > 0 and reference_price > 0:
            ratio = previous_close / reference_price
        events.append(
            _build_share_basis_event(
                exchange=exchange,
                code=str(_row_value(row, code_key)).strip(),
                observed_on=parse_roc_date(_row_value(row, "恢復買賣日期")),
                action_type=CorporateActionType.PAR_VALUE_CHANGE,
                previous_close=previous_close,
                reference_price=reference_price,
                new_shares_per_thousand=(ratio * 1000.0) if ratio else None,
                cash_refund_per_share=0,
                cash_subscription_shares=0,
                halt_date=(
                    parse_roc_date(_extract_html_text(detail_html, "停止買賣日期"))
                    if tpex and _extract_html_text(detail_html, "停止買賣日期")
                    else None
                ),
                source_name=f"{exchange} par-value change",
                source_endpoint=(
                    "https://www.tpex.org.tw/www/zh-tw/bulletin/pvChgRslt"
                    if tpex
                    else "https://www.twse.com.tw/rwd/zh/change/TWTB8U"
                ),
                fetched_at=fetched_at,
                raw_sha256=raw_sha256,
            )
        )
    return events


def normalize_twse_par_value_changes(
    payload: Mapping[str, Any], *, fetched_at: datetime, raw_sha256: str
) -> list[CorporateActionEvent]:
    return _normalize_par_value_changes(
        payload, exchange="TWSE", tpex=False, fetched_at=fetched_at, raw_sha256=raw_sha256
    )


def normalize_tpex_par_value_changes(
    payload: Mapping[str, Any], *, fetched_at: datetime, raw_sha256: str
) -> list[CorporateActionEvent]:
    return _normalize_par_value_changes(
        payload, exchange="TPEX", tpex=True, fetched_at=fetched_at, raw_sha256=raw_sha256
    )


def _filter_payload_codes(
    payload: Mapping[str, Any],
    *,
    tpex: bool,
    code_aliases: tuple[str, ...],
    allowed_codes: set[str],
) -> dict[str, Any]:
    filtered = json.loads(json.dumps(payload, ensure_ascii=False))
    table = filtered["tables"][0] if tpex else filtered
    fields = [str(field) for field in table["fields"]]
    present = [alias for alias in code_aliases if alias in fields]
    if len(present) != 1:
        raise DataQualityError(
            f"official code field changed; expected one of {code_aliases!r}, found {present!r}"
        )
    index = fields.index(present[0])
    table["data"] = [
        row for row in table["data"] if str(row[index]).strip() in allowed_codes
    ]
    return filtered


def normalize_universe_routes(
    universe_routes: Mapping[str, str | Sequence[str]],
) -> dict[str, tuple[str, ...]]:
    """Keep every historical exchange route for codes that changed markets."""

    normalized: dict[str, tuple[str, ...]] = {}
    for raw_code, raw_routes in universe_routes.items():
        code = str(raw_code).strip()
        if not code:
            raise DataQualityError("ledger universe contains an empty code")
        values = (raw_routes,) if isinstance(raw_routes, str) else tuple(raw_routes)
        routes = tuple(sorted({str(value).strip().upper() for value in values}))
        if not routes or set(routes) - {"TWSE", "TPEX"}:
            raise DataQualityError(
                f"ledger universe contains unsupported routes for {code}: {routes!r}"
            )
        normalized[code] = routes
    if not normalized:
        raise DataQualityError("ledger universe is empty")
    return normalized


def build_official_corporate_action_ledger(
    *,
    bundle_dir: Path,
    output_dir: Path,
    universe_routes: Mapping[str, str | Sequence[str]],
) -> tuple[CorporateActionLedger, dict[str, Any]]:
    bundle = json.loads((bundle_dir / "bundle_manifest.json").read_text(encoding="utf-8-sig"))
    if bundle.get("schema_version") != OFFICIAL_ACTION_BUNDLE_SCHEMA or bundle.get("status") != "PASS":
        raise DataQualityError("official action bundle is not a supported PASS snapshot")
    routes = normalize_universe_routes(universe_routes)
    if canonical_sha256(sorted(routes)) != bundle.get("universe_sha256"):
        raise DataQualityError("ledger universe differs from the frozen source bundle")
    if output_dir.exists() and not (output_dir / "ledger_manifest.json").is_file():
        raise FileExistsError("refusing to reuse a non-final ledger output directory")
    main_dir = bundle_dir / "main"
    details_dir = bundle_dir / "details"
    twse_codes = {code for code, values in routes.items() if "TWSE" in values}
    tpex_codes = {code for code, values in routes.items() if "TPEX" in values}

    twse_ex, twse_ex_at, twse_ex_hash = _passed_evidence(main_dir, "twse-ex-right")
    twse_red, twse_red_at, twse_red_hash = _passed_evidence(main_dir, "twse-reduction")
    twse_pv, twse_pv_at, twse_pv_hash = _passed_evidence(main_dir, "twse-par-value")
    twse_split, _, _ = _passed_evidence(main_dir, "twse-split")
    tpex_ex, tpex_ex_at, tpex_ex_hash = _passed_evidence(main_dir, "tpex-ex-right")
    tpex_red, tpex_red_at, tpex_red_hash = _passed_evidence(main_dir, "tpex-reduction")
    tpex_pv, tpex_pv_at, tpex_pv_hash = _passed_evidence(main_dir, "tpex-par-value")

    twse_ex = _filter_payload_codes(
        twse_ex, tpex=False, code_aliases=("股票代號",), allowed_codes=twse_codes
    )
    twse_red = _filter_payload_codes(
        twse_red, tpex=False, code_aliases=("股票代號",), allowed_codes=twse_codes
    )
    twse_pv = _filter_payload_codes(
        twse_pv, tpex=False, code_aliases=("股票代號",), allowed_codes=twse_codes
    )
    twse_split = _filter_payload_codes(
        twse_split, tpex=False, code_aliases=("ETF代號",), allowed_codes=set(routes)
    )
    if _records(twse_split, tpex=False):
        raise DataQualityError("ordinary-stock universe unexpectedly overlaps TWSE ETF split rows")
    tpex_ex = _filter_payload_codes(
        tpex_ex, tpex=True, code_aliases=("代號", "股票代號"), allowed_codes=tpex_codes
    )
    tpex_red = _filter_payload_codes(
        tpex_red, tpex=True, code_aliases=("股票代號", "證券代號"), allowed_codes=tpex_codes
    )
    tpex_pv = _filter_payload_codes(
        tpex_pv, tpex=True, code_aliases=("證券代號", "股票代號"), allowed_codes=tpex_codes
    )

    ex_details: dict[tuple[str, date], Mapping[str, Any]] = {}
    ex_detail_hashes: dict[tuple[str, date], str] = {}
    ex_detail_times: list[datetime] = []
    for row in _records(twse_ex, tpex=False):
        code = str(_row_value(row, "股票代號")).strip()
        observed_on = parse_roc_date(_row_value(row, "資料日期"))
        request_id = f"twse-ex-right-detail-{code}-{observed_on:%Y%m%d}"
        payload, fetched_at, raw_hash = _passed_evidence(details_dir, request_id)
        ex_details[(code, observed_on)] = payload
        ex_detail_hashes[(code, observed_on)] = raw_hash
        ex_detail_times.append(fetched_at)

    reduction_details: dict[tuple[str, str], Mapping[str, Any]] = {}
    reduction_detail_hashes: dict[tuple[str, str], str] = {}
    reduction_detail_times: list[datetime] = []
    for row in _records(twse_red, tpex=False):
        code = str(_row_value(row, "股票代號")).strip()
        values = [item.strip() for item in str(_row_value(row, "詳細資料")).split(",")]
        if len(values) < 2 or not re.fullmatch(r"\d{8}", values[1]):
            raise DataQualityError("TWSE reduction detail key changed")
        file_date = values[1]
        request_id = f"twse-reduction-detail-{code}-{file_date}"
        payload, fetched_at, raw_hash = _passed_evidence(details_dir, request_id)
        reduction_details[(code, file_date)] = payload
        reduction_detail_hashes[(code, file_date)] = raw_hash
        reduction_detail_times.append(fetched_at)

    events: list[CorporateActionEvent] = []
    events.extend(
        normalize_twse_ex_rights(
            twse_ex,
            details=ex_details,
            detail_sha256s=ex_detail_hashes,
            fetched_at=max([twse_ex_at, *ex_detail_times]),
            raw_sha256=twse_ex_hash,
        )
    )
    events.extend(
        normalize_twse_reductions(
            twse_red,
            details=reduction_details,
            detail_sha256s=reduction_detail_hashes,
            fetched_at=max([twse_red_at, *reduction_detail_times]),
            raw_sha256=twse_red_hash,
        )
    )
    events.extend(
        normalize_twse_par_value_changes(
            twse_pv, fetched_at=twse_pv_at, raw_sha256=twse_pv_hash
        )
    )
    events.extend(
        normalize_tpex_ex_rights(
            tpex_ex, fetched_at=tpex_ex_at, raw_sha256=tpex_ex_hash
        )
    )
    events.extend(
        normalize_tpex_reductions(
            tpex_red, fetched_at=tpex_red_at, raw_sha256=tpex_red_hash
        )
    )
    events.extend(
        normalize_tpex_par_value_changes(
            tpex_pv, fetched_at=tpex_pv_at, raw_sha256=tpex_pv_hash
        )
    )
    events.sort(key=lambda event: (event.event_date, event.instrument_id, event.action_id))
    action_ids = [event.action_id for event in events]
    if len(action_ids) != len(set(action_ids)):
        raise DataQualityError("official ledger contains duplicate action ids")
    for event in events:
        if event.technical_resolution_status == "RESOLVED_FOR_TECHNICAL":
            event.validate()

    coverage_from = date.fromisoformat(bundle["coverage"]["from"])
    coverage_through = date.fromisoformat(bundle["coverage"]["through"])
    event_payload = [json.loads(canonical_bytes(event)) for event in events]
    events_sha256 = canonical_sha256(event_payload)
    unresolved = [
        event.action_id
        for event in events
        if event.technical_resolution_status != "RESOLVED_FOR_TECHNICAL"
    ]
    quarantined_positions = [
        event.action_id
        for event in events
        if event.position_lifecycle_status == "QUARANTINED_UNSUPPORTED"
    ]
    status = "PASS" if not unresolved else "UNRESOLVED_FOR_TECHNICAL"
    core = {
        "schema_version": ACTION_LEDGER_SCHEMA,
        "status": status,
        "coverage": {"from": coverage_from.isoformat(), "through": coverage_through.isoformat()},
        "source_bundle": {
            "path": str((bundle_dir / "bundle_manifest.json").resolve()),
            "snapshot_id": bundle["snapshot_id"],
            "content_sha256": bundle["content_sha256"],
        },
        "universe_sha256": bundle["universe_sha256"],
        "universe_routes_sha256": canonical_sha256(routes),
        "event_count": len(events),
        "technical_resolution": {
            "status": status,
            "resolved_event_count": len(events) - len(unresolved),
            "unresolved_event_ids": unresolved,
        },
        "position_lifecycle": {
            "status": "QUARANTINED" if quarantined_positions else "PASS",
            "supported_event_count": len(events) - len(quarantined_positions),
            "quarantined_event_ids": quarantined_positions,
            "reason_code": (
                "UNSUPPORTED_CASH_CAPITAL_INCREASE_RIGHTS"
                if quarantined_positions
                else None
            ),
        },
        "events_path": "events.json",
        "events_sha256": events_sha256,
        "source_names": ["TWSE", "TPEx"],
    }
    content_sha256 = canonical_sha256(core)
    manifest = {
        **core,
        "snapshot_id": f"stock-official-action-ledger#{content_sha256[:16]}",
        "content_sha256": content_sha256,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    write_immutable_json(output_dir / "events.json", event_payload)
    write_immutable_json(output_dir / "ledger_manifest.json", manifest)
    artifacts = []
    for path in sorted(item for item in output_dir.iterdir() if item.is_file()):
        if path.name == "artifact_manifest.json":
            continue
        artifacts.append(
            {"path": path.name, "bytes": path.stat().st_size, "sha256": _sha256_bytes(path.read_bytes())}
        )
    write_immutable_json(
        output_dir / "artifact_manifest.json",
        {"schema_version": "stock-artifact-manifest-v1", "status": status, "artifacts": artifacts},
    )
    ledger = CorporateActionLedger(
        schema_version=ACTION_LEDGER_SCHEMA,
        snapshot_id=manifest["snapshot_id"],
        coverage_from=coverage_from,
        coverage_through=coverage_through,
        status=status,
        source_names=("TWSE", "TPEx"),
        events=tuple(events),
        manifest_sha256=content_sha256,
    )
    return ledger, manifest


def extract_html_number(detail_html: str, label: str) -> float | None:
    plain = html.unescape(detail_html).replace("&nbsp", " ")
    pattern = rf"{re.escape(label)}\s*:?\s*</th>\s*<td>(.*?)</td>"
    match = re.search(pattern, plain, flags=re.IGNORECASE | re.DOTALL)
    return parse_number(re.sub(r"<[^>]+>", "", match.group(1))) if match else None


def _extract_html_text(detail_html: str, label: str) -> str | None:
    plain = html.unescape(detail_html).replace("&nbsp", " ")
    pattern = rf"{re.escape(label)}\s*:?\s*</th>\s*<td>(.*?)</td>"
    match = re.search(pattern, plain, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    return re.sub(r"<[^>]+>", "", match.group(1)).strip()
