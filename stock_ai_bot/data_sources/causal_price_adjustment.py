from __future__ import annotations

from datetime import date
from typing import Iterable, Sequence

from stock_ai_bot.data_sources.causal_price_contract import (
    CONTRACT_VERSION,
    ActionKind,
    CausalTechnicalBar,
    CorporateActionLedger,
    CorporateActionEvent,
    CorporateActionUnresolvedError,
    DataQualityError,
    PriceBasis,
    RawDailyBar,
    StoredPriceCoordinate,
    canonical_sha256,
    validate_unique_ascending_bars,
)


def _event_transform(event: CorporateActionEvent) -> tuple[float, float, float, float]:
    event.validate()
    cash = 1.0
    share = 1.0
    rights = 1.0
    volume = 1.0
    if event.cash_price_factor is not None:
        cash = float(event.cash_price_factor)
    if event.share_price_factor is not None:
        share = float(event.share_price_factor)
        volume = float(event.share_volume_factor)
    if event.rights_price_factor is not None:
        rights = float(event.rights_price_factor)
    return cash, share, rights, volume


def factors_for_bar(
    *,
    instrument_id: str,
    trade_date: date,
    as_of_date: date,
    events: Iterable[CorporateActionEvent],
) -> tuple[float, float, float, float, tuple[str, ...], tuple[str, ...]]:
    if trade_date > as_of_date:
        raise DataQualityError("a future raw bar cannot enter an as-of technical series")
    cash_factor = 1.0
    share_factor = 1.0
    rights_factor = 1.0
    volume_factor = 1.0
    applied: list[str] = []
    event_hashes: list[str] = []
    relevant = sorted(
        (event for event in events if event.instrument_id == instrument_id),
        key=lambda event: (event.event_date, event.action_id),
    )
    for event in relevant:
        if not (trade_date < event.event_date <= as_of_date):
            continue
        if event.available_on > as_of_date:
            continue
        cash, share, rights, volume = _event_transform(event)
        cash_factor *= cash
        share_factor *= share
        rights_factor *= rights
        volume_factor *= volume
        applied.append(event.action_id)
        event_hashes.append(event.event_sha256)
    return (
        cash_factor,
        share_factor,
        rights_factor,
        volume_factor,
        tuple(applied),
        tuple(event_hashes),
    )


def build_causal_technical_bars(
    *,
    raw_bars: Sequence[RawDailyBar],
    ledger: CorporateActionLedger,
    as_of_date: date,
    validated_source_lineage: bool = False,
) -> list[CausalTechnicalBar]:
    validate_unique_ascending_bars(raw_bars)
    if not raw_bars:
        return []
    ledger.validate_for(
        from_date=min(row.trade_date for row in raw_bars), through_date=as_of_date
    )
    events = ledger.events
    unresolved = [
        event.action_id
        for event in events
        if event.effective_date <= as_of_date
        and event.technical_resolution_status != "RESOLVED_FOR_TECHNICAL"
    ]
    if unresolved:
        raise CorporateActionUnresolvedError(
            "effective-dated corporate-action ledger contains unresolved events: "
            + ", ".join(sorted(unresolved))
        )
    output: list[CausalTechnicalBar] = []
    for raw in raw_bars:
        if raw.trade_date > as_of_date:
            continue
        raw.validate(source_lineage_prevalidated=validated_source_lineage)
        cash, share, rights, volume, event_ids, event_hashes = factors_for_bar(
            instrument_id=raw.instrument_id,
            trade_date=raw.trade_date,
            as_of_date=as_of_date,
            events=events,
        )
        price_factor = cash * share * rights
        lineage_payload = {
            "contract_version": CONTRACT_VERSION,
            "as_of_date": as_of_date,
            "raw_row_sha256": raw.row_sha256,
            "event_ids": event_ids,
            "event_sha256s": event_hashes,
            "cash_price_factor": cash,
            "share_price_factor": share,
            "rights_price_factor": rights,
            "share_volume_factor": volume,
        }
        output.append(
            CausalTechnicalBar(
                instrument_id=raw.instrument_id,
                symbol=raw.symbol,
                trade_date=raw.trade_date,
                as_of_date=as_of_date,
                technical_open=raw.raw_open * price_factor,
                technical_high=raw.raw_high * price_factor,
                technical_low=raw.raw_low * price_factor,
                technical_close=raw.raw_close * price_factor,
                technical_volume=raw.raw_volume * volume,
                cash_price_factor=cash,
                share_price_factor=share,
                share_volume_factor=volume,
                rights_price_factor=rights,
                price_factor=price_factor,
                volume_factor=volume,
                factor_event_ids=event_ids,
                raw_row_sha256=raw.row_sha256,
                contract_version=CONTRACT_VERSION,
                lineage_sha256=canonical_sha256(lineage_payload),
            )
        )
        output[-1].validate()
    for row in output:
        if row.trade_date == as_of_date:
            raw = next(item for item in raw_bars if item.trade_date == as_of_date)
            if (
                row.technical_open != raw.raw_open
                or row.technical_high != raw.raw_high
                or row.technical_low != raw.raw_low
                or row.technical_close != raw.raw_close
                or row.technical_volume != raw.raw_volume
            ):
                raise DataQualityError("as-of technical OHLCV must equal same-day raw OHLCV")
    return output


def reproject_structure_price_to_raw_comparison(
    coordinate: StoredPriceCoordinate,
    *,
    current_factor_for_anchor: float,
) -> float:
    coordinate.validate()
    if current_factor_for_anchor <= 0:
        raise DataQualityError("current_factor_for_anchor must be positive")
    return coordinate.value * current_factor_for_anchor / coordinate.price_factor_at_storage
