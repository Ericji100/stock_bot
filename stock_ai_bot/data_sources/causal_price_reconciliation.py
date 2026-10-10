from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Mapping, Sequence

from stock_ai_bot.data_sources.causal_price_adjustment import (
    build_causal_technical_bars,
)
from stock_ai_bot.data_sources.causal_price_contract import (
    CorporateActionLedger,
    DataQualityError,
    RawDailyBar,
    canonical_sha256,
)
from stock_ai_bot.data_sources.causal_price_repository import write_immutable_json


def taiwan_equity_tick_size(price: float) -> float:
    if price <= 0:
        raise DataQualityError("tick-size price must be positive")
    if price < 10:
        return 0.01
    if price < 50:
        return 0.05
    if price < 100:
        return 0.1
    if price < 500:
        return 0.5
    if price < 1000:
        return 1.0
    return 5.0


def reconcile_corporate_actions_with_raw(
    *,
    ledger: CorporateActionLedger,
    bars_by_instrument: Mapping[str, Sequence[RawDailyBar]],
) -> list[dict[str, object]]:
    ledger.validate_for(
        from_date=ledger.coverage_from, through_date=ledger.coverage_through
    )
    results: list[dict[str, object]] = []
    for event in ledger.events:
        bars = tuple(bars_by_instrument.get(event.instrument_id) or ())
        prior = [bar for bar in bars if bar.trade_date < event.event_date]
        after = [bar for bar in bars if bar.trade_date >= event.event_date]
        issues: list[str] = []
        prior_bar = prior[-1] if prior else None
        first_after = after[0] if after else None
        if prior_bar is None:
            issues.append("MISSING_PRIOR_RAW_BAR")
        if first_after is None:
            issues.append("MISSING_ON_OR_AFTER_RAW_BAR")
        previous_diff: float | None = None
        reference_diff: float | None = None
        price_factor = (
            float(event.cash_price_factor or 1.0)
            * float(event.share_price_factor or 1.0)
            * float(event.rights_price_factor or 1.0)
        )
        if event.previous_close is None or event.reference_price is None:
            issues.append("MISSING_OFFICIAL_PREVIOUS_OR_REFERENCE")
        elif prior_bar is not None:
            previous_diff = abs(prior_bar.raw_close - event.previous_close)
            previous_tolerance = taiwan_equity_tick_size(event.previous_close) + 1e-9
            if previous_diff > previous_tolerance:
                issues.append("OFFICIAL_PREVIOUS_CLOSE_MISMATCH")
            derived_reference = prior_bar.raw_close * price_factor
            reference_diff = abs(derived_reference - event.reference_price)
            reference_tolerance = taiwan_equity_tick_size(event.reference_price) + 1e-9
            if reference_diff > reference_tolerance:
                issues.append("OFFICIAL_REFERENCE_FACTOR_MISMATCH")

        future_invariance = False
        raw_immutability = False
        if prior_bar is not None:
            symbol_bars = tuple(bars)
            raw_hash_before = canonical_sha256(
                [bar.row_lineage_payload() for bar in symbol_bars]
            )
            full = build_causal_technical_bars(
                raw_bars=symbol_bars, ledger=ledger, as_of_date=prior_bar.trade_date
            )
            truncated = replace(
                ledger,
                events=tuple(
                    item for item in ledger.events if item.event_date <= prior_bar.trade_date
                ),
            )
            causal = build_causal_technical_bars(
                raw_bars=symbol_bars,
                ledger=truncated,
                as_of_date=prior_bar.trade_date,
            )
            future_invariance = canonical_sha256(full) == canonical_sha256(causal)
            raw_hash_after = canonical_sha256(
                [bar.row_lineage_payload() for bar in symbol_bars]
            )
            raw_immutability = raw_hash_before == raw_hash_after
            if not future_invariance:
                issues.append("FUTURE_EVENT_INVARIANCE_FAILED")
            if not raw_immutability:
                issues.append("RAW_EXECUTION_MUTATED")
        results.append(
            {
                "action_id": event.action_id,
                "instrument_id": event.instrument_id,
                "event_date": event.event_date.isoformat(),
                "technical_resolution_status": event.technical_resolution_status,
                "position_lifecycle_status": event.position_lifecycle_status,
                "prior_raw_date": prior_bar.trade_date.isoformat() if prior_bar else None,
                "first_on_or_after_raw_date": (
                    first_after.trade_date.isoformat() if first_after else None
                ),
                "official_previous_close": event.previous_close,
                "prior_raw_close": prior_bar.raw_close if prior_bar else None,
                "previous_close_abs_diff": previous_diff,
                "official_reference_price": event.reference_price,
                "price_factor": price_factor,
                "reference_abs_diff": reference_diff,
                "future_event_invariance": future_invariance,
                "raw_execution_immutable": raw_immutability,
                "status": "PASS" if not issues else "FAIL",
                "issues": issues,
            }
        )
    return results


def freeze_reconciliation_audit(
    *,
    output_dir: Path,
    ledger: CorporateActionLedger,
    raw_snapshot_id: str,
    results: Sequence[dict[str, object]],
) -> dict[str, object]:
    failures = [result for result in results if result["status"] != "PASS"]
    result_sha256 = canonical_sha256(list(results))
    core: dict[str, object] = {
        "schema_version": "stock-causal-price-reconciliation-v1",
        "status": "PASS" if not failures else "FAIL",
        "ledger_snapshot_id": ledger.snapshot_id,
        "raw_snapshot_id": raw_snapshot_id,
        "event_count": len(results),
        "pass_count": len(results) - len(failures),
        "failure_count": len(failures),
        "failed_action_ids": [result["action_id"] for result in failures],
        "results_path": "results.json",
        "results_sha256": result_sha256,
    }
    content_sha256 = canonical_sha256(core)
    manifest = {
        **core,
        "audit_id": f"stock-causal-price-reconciliation#{content_sha256[:16]}",
        "content_sha256": content_sha256,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    write_immutable_json(output_dir / "results.json", list(results))
    write_immutable_json(output_dir / "audit_manifest.json", manifest)
    return manifest
