from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping

from stock_ai_bot.data_sources.causal_price_adjustment import (
    build_causal_technical_bars,
)
from stock_ai_bot.data_sources.causal_price_contract import (
    CONTRACT_VERSION,
    CorporateActionLedger,
    CausalTechnicalBar,
    DataQualityError,
    InstrumentIdentity,
    RawDailyBar,
    canonical_sha256,
    validate_unique_ascending_bars,
)
from stock_ai_bot.data_sources.causal_price_repository import (
    load_official_submarket_evidence,
    load_market_metadata_reconciliation,
    validate_source_segments,
)

from backtests.dual_ma.fugle_source import FUGLE_ENDPOINT, sha256_file
from stock_ai_bot.selection.historical_universe_service import (
    load_official_universe_sources,
)
from backtests.dual_ma.official_route_identity import (
    load_tib_route_identities_from_universe_source,
)
from backtests.dual_ma.official_route_lineage_sidecar import (
    load_tib_route_lineage_sidecar,
)
from backtests.dual_ma.official_suspension_evidence import (
    load_official_suspension_evidence,
)


FUGLE_SNAPSHOT_SCHEMA = "dual-ma-fugle-price-snapshot-v1"


@dataclass(frozen=True)
class CausalRawBundle:
    snapshot_id: str
    snapshot_sha256: str
    identities: Mapping[str, InstrumentIdentity]
    bars_by_symbol: Mapping[str, tuple[RawDailyBar, ...]]
    warnings: tuple[dict[str, str], ...]
    route_lineage_path: str | None = None
    route_lineage_sha256: str | None = None


@dataclass(frozen=True)
class CausalSymbolView:
    symbol: str
    as_of_date: date
    raw_as_of: RawDailyBar | None
    technical_bars: tuple[CausalTechnicalBar, ...]


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _identity_rows(path: Path) -> dict[str, dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    result = {row["symbol"]: row for row in rows}
    if len(result) != len(rows):
        raise DataQualityError("market identity ledger contains duplicate symbols")
    return result


def _expected_source_market(exchange: str) -> str:
    try:
        return {"TWSE": "TSE", "TPEX": "OTC"}[exchange]
    except KeyError as exc:
        raise DataQualityError(f"unsupported official exchange {exchange!r}") from exc


def _build_identities(
    *, identity_csv: Path, official_universe_source: Path,
    route_identities: Mapping[str, Any] | None = None,
) -> dict[str, InstrumentIdentity]:
    rows = _identity_rows(identity_csv)
    universe = load_official_universe_sources(official_universe_source)
    observed_at = datetime.fromisoformat(universe.collected_at)
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise DataQualityError("official identity observed_at is not timezone-aware")
    source_sha256 = sha256_file(official_universe_source)
    if route_identities is None:
        route_identities = load_tib_route_identities_from_universe_source(
            official_universe_source, expected_sha256=source_sha256
        )
    identities: dict[str, InstrumentIdentity] = {}
    for symbol, row in rows.items():
        exchange = row["official_current_market"]
        code = row["code"]
        route_identity = route_identities.get(symbol)
        if route_identity is not None and (
            route_identity.exchange != exchange
            or route_identity.effective_start
            != date.fromisoformat(row["official_mainboard_start"])
        ):
            raise DataQualityError(f"official route overlay conflicts with ledger: {symbol}")
        identity = InstrumentIdentity(
            instrument_id=f"{exchange}:{code}",
            code=code,
            symbol=symbol,
            exchange=exchange,
            market=(
                route_identity.market
                if route_identity is not None
                else _expected_source_market(exchange)
            ),
            instrument_type="STOCK",
            mainboard_start=date.fromisoformat(row["official_mainboard_start"]),
            mainboard_end=(
                date.fromisoformat(row["official_mainboard_end"])
                if row.get("official_mainboard_end")
                else None
            ),
            route_status=(
                route_identity.route_status
                if route_identity is not None
                else row["historical_route"]
            ),
            identity_source=(
                str(route_identity.source_path)
                if route_identity is not None
                else str(official_universe_source.resolve())
            ),
            identity_source_sha256=(
                str(route_identity.source_sha256)
                if route_identity is not None else source_sha256
            ),
            observed_at=observed_at,
        )
        identity.validate()
        identities[symbol] = identity
    return identities


def _parse_fetched_at(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DataQualityError("Fugle receipt fetched_at is not timezone-aware")
    return parsed


def _bar_from_row(
    *,
    row: Mapping[str, Any],
    identity: InstrumentIdentity,
    source_market: str | None,
    receipt: Mapping[str, Any],
    snapshot_id: str,
    snapshot_sha256: str,
) -> RawDailyBar:
    query = receipt["query"]
    provisional = RawDailyBar(
        instrument_id=identity.instrument_id,
        symbol=identity.symbol,
        exchange=identity.exchange,
        trade_date=date.fromisoformat(str(row["date"])),
        raw_open=float(row["open"]),
        raw_high=float(row["high"]),
        raw_low=float(row["low"]),
        raw_close=float(row["close"]),
        raw_volume=float(row["volume"]),
        source_name=str(receipt.get("provider_name") or "Fugle"),
        source_endpoint=str(
            receipt.get("provider_endpoint")
            or FUGLE_ENDPOINT.format(symbol=identity.code)
        ),
        source_market=source_market,
        source_segment_id=str(receipt["request_id"]),
        fetched_at=_parse_fetched_at(receipt["fetched_at"]),
        query_from=date.fromisoformat(str(query["from"])),
        query_to=date.fromisoformat(str(query["to"])),
        raw_response_sha256=str(receipt["raw_sha256"]),
        row_sha256="0" * 64,
        snapshot_id=snapshot_id,
        snapshot_sha256=snapshot_sha256,
        mainboard_start=identity.mainboard_start,
        adjusted=False,
    )
    bar = replace(provisional, row_sha256=canonical_sha256(provisional.row_lineage_payload()))
    return bar


def load_fugle_causal_raw_bundle(
    *,
    snapshot_manifest_path: Path,
    identity_csv: Path,
    official_universe_source: Path,
    route_lineage_sidecar: Path | None = None,
    route_lineage_sha256: str | None = None,
    route_lineage_as_of: date | None = None,
) -> CausalRawBundle:
    snapshot = json.loads(snapshot_manifest_path.read_text(encoding="utf-8-sig"))
    if snapshot.get("schema_version") != FUGLE_SNAPSHOT_SCHEMA:
        raise DataQualityError("unsupported Fugle snapshot schema")
    if snapshot.get("status") != "PASS" or snapshot.get("invalid_ohlc_count") != 0:
        raise DataQualityError("Fugle snapshot has not passed its source gate")
    query_contract = snapshot.get("query_contract") or {}
    if (
        query_contract.get("timeframe") != "D"
        or str(query_contract.get("adjusted")).lower() != "false"
        or query_contract.get("sort") != "asc"
    ):
        raise DataQualityError("Fugle snapshot query contract is not unadjusted daily ascending")
    snapshot_id = str(snapshot["snapshot_id"])
    snapshot_sha256 = str(snapshot["content_sha256"])
    if (route_lineage_sidecar is None) != (route_lineage_sha256 is None):
        raise DataQualityError("route lineage sidecar path/hash must be provided together")
    if route_lineage_sidecar is not None and route_lineage_as_of is None:
        raise DataQualityError("route lineage sidecar requires an explicit as-of date")
    if route_lineage_sidecar is None and route_lineage_as_of is not None:
        raise DataQualityError("route lineage as-of date requires a sidecar")
    route_receipt_ends = tuple(
        date.fromisoformat(str((receipt.get("query") or {})["to"]))
        for receipt in snapshot["receipts"]
        if receipt.get("official_route_identity_id") is not None
    )
    if (
        route_lineage_as_of is not None
        and route_receipt_ends
        and route_lineage_as_of < max(route_receipt_ends)
    ):
        raise DataQualityError("route lineage as-of date precedes raw snapshot coverage")
    route_identities = (
        load_tib_route_lineage_sidecar(
            route_lineage_sidecar,
            expected_sha256=route_lineage_sha256,
            target_source_path=official_universe_source,
            as_of_date=route_lineage_as_of,
        )
        if route_lineage_sidecar is not None else
        load_tib_route_identities_from_universe_source(
            official_universe_source,
            expected_sha256=sha256_file(official_universe_source),
        )
    )
    identities = _build_identities(
        identity_csv=identity_csv,
        official_universe_source=official_universe_source,
        route_identities=route_identities,
    )
    bars_by_symbol: dict[str, list[RawDailyBar]] = {symbol: [] for symbol in identities}
    warnings: list[dict[str, str]] = []
    for receipt in snapshot["receipts"]:
        if receipt.get("status") == "SKIPPED_PRE_MAINBOARD":
            continue
        if receipt.get("status") == "SKIPPED_OFFICIAL_SUSPENSION":
            request_id = str(receipt["request_id"])
            symbol = request_id.rsplit(":", 1)[0]
            identity = identities.get(symbol)
            if identity is None:
                raise DataQualityError(
                    f"suspension receipt has no official identity: {symbol}"
                )
            evidence_path = receipt.get("suspension_evidence_path")
            if not evidence_path:
                raise DataQualityError(
                    f"suspension receipt lacks frozen evidence: {request_id}"
                )
            evidence = load_official_suspension_evidence(
                snapshot_manifest_path.parent / str(evidence_path)
            )
            if (
                evidence.request_id != request_id
                or evidence.symbol != symbol
                or evidence.code != identity.code
                or evidence.exchange != identity.exchange
                or evidence.evidence_id != receipt.get("suspension_evidence_id")
                or evidence.content_sha256
                != receipt.get("suspension_evidence_sha256")
            ):
                raise DataQualityError(
                    f"suspension evidence lineage mismatch: {request_id}"
                )
            continue
        if receipt.get("status") != "PASS":
            raise DataQualityError(f"Fugle receipt did not pass: {receipt.get('request_id')}")
        request_id = str(receipt["request_id"])
        symbol = request_id.rsplit(":", 1)[0]
        identity = identities.get(symbol)
        if identity is None:
            raise DataQualityError(f"Fugle receipt has no official identity: {symbol}")
        route_identity = route_identities.get(symbol)
        if route_identity is not None and (
            receipt.get("expected_submarket") != route_identity.market
            or receipt.get("official_route_identity_id") != route_identity.identity_id
        ):
            raise DataQualityError(
                f"Fugle receipt route identity lineage mismatch: {request_id}"
            )
        if route_identity is None and (
            receipt.get("expected_submarket") is not None
            or receipt.get("official_route_identity_id") is not None
        ):
            raise DataQualityError(f"unexpected Fugle submarket identity: {request_id}")
        query = receipt.get("query") or {}
        if (
            query.get("timeframe") != "D"
            or str(query.get("adjusted")).lower() != "false"
            or query.get("sort") != "asc"
        ):
            raise DataQualityError(f"Fugle receipt query basis changed: {request_id}")
        raw_path = snapshot_manifest_path.parent / str(receipt["raw_path"])
        raw_bytes = raw_path.read_bytes()
        if _sha256_bytes(raw_bytes) != receipt["raw_sha256"]:
            raise DataQualityError(f"Fugle raw response hash mismatch: {request_id}")
        payload = json.loads(raw_bytes)
        if not isinstance(payload, dict):
            raise DataQualityError(f"Fugle raw response root is not an object: {request_id}")
        if str(payload.get("symbol")) != identity.code:
            raise DataQualityError(f"Fugle response symbol disagrees with identity: {request_id}")
        if str(payload.get("exchange") or "").casefold() != identity.exchange.casefold():
            raise DataQualityError(f"Fugle response exchange disagrees with identity: {request_id}")
        source_market = payload.get("market")
        market_evidence = None
        evidence_path = receipt.get("market_conflict_evidence_path")
        if evidence_path:
            market_evidence = load_market_metadata_reconciliation(
                snapshot_manifest_path.parent / str(evidence_path)
            )
            if (
                market_evidence.evidence_id
                != receipt.get("market_conflict_evidence_id")
                or market_evidence.content_sha256
                != receipt.get("market_conflict_evidence_sha256")
            ):
                raise DataQualityError(
                    f"market reconciliation lineage mismatch: {request_id}"
                )
        submarket_evidence = None
        submarket_evidence_path = receipt.get("official_submarket_evidence_path")
        if submarket_evidence_path:
            submarket_evidence = load_official_submarket_evidence(
                snapshot_manifest_path.parent / str(submarket_evidence_path)
            )
            if (
                submarket_evidence.evidence_id
                != receipt.get("official_submarket_evidence_id")
                or submarket_evidence.content_sha256
                != receipt.get("official_submarket_evidence_sha256")
            ):
                raise DataQualityError(
                    f"submarket evidence lineage mismatch: {request_id}"
                )
            submarket_identity = replace(
                identity,
                market=submarket_evidence.official_submarket,
                mainboard_start=submarket_evidence.route_start,
                mainboard_end=submarket_evidence.route_end_exclusive,
                route_status=submarket_evidence.route_status,
                identity_source=str(
                    (
                        snapshot_manifest_path.parent
                        / str(submarket_evidence_path)
                    ).resolve()
                ),
                identity_source_sha256=(
                    submarket_evidence.route_source_artifact_sha256
                ),
            )
            submarket_evidence.validate_identity(submarket_identity)
            previous_identity = identities[symbol]
            if previous_identity.market not in {
                _expected_source_market(previous_identity.exchange),
                submarket_identity.market,
            }:
                raise DataQualityError(
                    f"conflicting submarket identities: {request_id}"
                )
            identities[symbol] = submarket_identity
            identity = submarket_identity
        data = payload.get("data")
        if not isinstance(data, list) or not data:
            raise DataQualityError(f"Fugle raw response contains no rows: {request_id}")
        for row in data:
            if not isinstance(row, dict):
                raise DataQualityError(f"Fugle raw response contains non-object row: {request_id}")
            bar = _bar_from_row(
                row=row,
                identity=identity,
                source_market=str(source_market) if source_market is not None else None,
                receipt=receipt,
                snapshot_id=snapshot_id,
                snapshot_sha256=snapshot_sha256,
            )
            route_proof = submarket_evidence or route_identity
            for warning in bar.validate(identity, market_evidence, route_proof):
                warnings.append(
                    {
                        "symbol": symbol,
                        "date": bar.trade_date.isoformat(),
                        "source_segment_id": request_id,
                        "warning": warning,
                    }
                )
            bars_by_symbol[symbol].append(bar)
    frozen_bars: dict[str, tuple[RawDailyBar, ...]] = {}
    all_bars: list[RawDailyBar] = []
    for symbol, rows in bars_by_symbol.items():
        if not rows:
            raise DataQualityError(f"Fugle snapshot produced no comparable rows: {symbol}")
        rows.sort(key=lambda item: item.trade_date)
        frozen_bars[symbol] = tuple(rows)
        all_bars.extend(rows)
    validate_unique_ascending_bars(all_bars)
    validate_source_segments(all_bars)
    if len(all_bars) != int(snapshot["normalized_row_count"]):
        raise DataQualityError("adapter row count disagrees with Fugle snapshot manifest")
    return CausalRawBundle(
        snapshot_id=snapshot_id,
        snapshot_sha256=snapshot_sha256,
        identities=identities,
        bars_by_symbol=frozen_bars,
        warnings=tuple(warnings),
        route_lineage_path=(
            str(route_lineage_sidecar.resolve())
            if route_lineage_sidecar is not None else None
        ),
        route_lineage_sha256=route_lineage_sha256,
    )


def build_symbol_as_of_view(
    *,
    bundle: CausalRawBundle,
    ledger: CorporateActionLedger,
    symbol: str,
    as_of_date: date,
) -> CausalSymbolView:
    try:
        bars = bundle.bars_by_symbol[symbol]
    except KeyError as exc:
        raise DataQualityError(f"symbol is absent from causal raw bundle: {symbol}") from exc
    technical = build_causal_technical_bars(
        raw_bars=bars,
        ledger=ledger,
        as_of_date=as_of_date,
        validated_source_lineage=True,
    )
    raw_as_of = next((bar for bar in bars if bar.trade_date == as_of_date), None)
    return CausalSymbolView(
        symbol=symbol,
        as_of_date=as_of_date,
        raw_as_of=raw_as_of,
        technical_bars=tuple(technical),
    )


def write_raw_adapter_audit(
    *,
    bundle: CausalRawBundle,
    output_dir: Path,
    snapshot_manifest_path: Path,
    identity_csv: Path,
    official_universe_source: Path,
) -> dict[str, Any]:
    if output_dir.exists():
        raise FileExistsError(f"refusing to overwrite causal adapter audit: {output_dir}")
    output_dir.mkdir(parents=True)
    symbols: list[dict[str, Any]] = []
    for symbol in sorted(bundle.bars_by_symbol):
        bars = bundle.bars_by_symbol[symbol]
        symbols.append(
            {
                "symbol": symbol,
                "instrument_id": bundle.identities[symbol].instrument_id,
                "row_count": len(bars),
                "first_date": bars[0].trade_date.isoformat(),
                "last_date": bars[-1].trade_date.isoformat(),
                "row_lineage_sha256": canonical_sha256(
                    [bar.row_sha256 for bar in bars]
                ),
            }
        )
    with (output_dir / "symbol_lineage.csv").open(
        "w", encoding="utf-8", newline=""
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=list(symbols[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(symbols)
    warning_rows = list(bundle.warnings)
    with (output_dir / "warnings.csv").open(
        "w", encoding="utf-8", newline=""
    ) as handle:
        fields = ["symbol", "date", "source_segment_id", "warning"]
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(warning_rows)
    manifest = {
        "schema_version": "dual-ma-causal-raw-adapter-audit-v1",
        "status": "PASS_RAW_CONTRACT_ONLY_PENDING_CORPORATE_ACTION_LEDGER",
        "shared_contract_version": CONTRACT_VERSION,
        "snapshot_id": bundle.snapshot_id,
        "snapshot_sha256": bundle.snapshot_sha256,
        "symbol_count": len(symbols),
        "raw_bar_count": sum(row["row_count"] for row in symbols),
        "warning_row_count": len(warning_rows),
        "warning_symbol_count": len({row["symbol"] for row in warning_rows}),
        "bundle_lineage_sha256": canonical_sha256(symbols),
        "corporate_action_gate": (
            "WAITING_REQUIRED_OFFICIAL_LEDGER; this audit does not authorize technical "
            "features, engine replay, or performance inference"
        ),
        "inputs": {
            "snapshot_manifest": {
                "path": str(snapshot_manifest_path.resolve()),
                "sha256": sha256_file(snapshot_manifest_path),
            },
            "identity_csv": {
                "path": str(identity_csv.resolve()),
                "sha256": sha256_file(identity_csv),
            },
            "official_universe_source": {
                "path": str(official_universe_source.resolve()),
                "sha256": sha256_file(official_universe_source),
            },
        },
    }
    manifest_path = output_dir / "adapter_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    report = f"""# 雙均線 Fugle → 共用因果 raw contract adapter 稽核

狀態：`{manifest['status']}`

- Shared contract：`{CONTRACT_VERSION}`。
- Snapshot：`{bundle.snapshot_id}`。
- 股票：{manifest['symbol_count']}；RawDailyBar：{manifest['raw_bar_count']}。
- Source metadata warning：{manifest['warning_row_count']} 列／{manifest['warning_symbol_count']} 檔。
- Bundle lineage：`{manifest['bundle_lineage_sha256']}`。

此 checkpoint 只證明未還原 raw price 已轉入共用契約；official corporate-action ledger 未通過前，不得建立 technical features、回測或績效推論。
"""
    (output_dir / "adapter_report.md").write_text(report, encoding="utf-8")
    artifacts = []
    for path in sorted(item for item in output_dir.iterdir() if item.is_file()):
        if path.name == "artifact_manifest.json":
            continue
        artifacts.append(
            {"path": path.name, "bytes": path.stat().st_size, "sha256": sha256_file(path)}
        )
    (output_dir / "artifact_manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "dual-ma-artifact-manifest-v1",
                "status": manifest["status"],
                "artifacts": artifacts,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return manifest
