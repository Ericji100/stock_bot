from __future__ import annotations

from typing import Any

from stock_ai_bot.data_sources.causal_price_contract import CorporateActionType

from .errors import EtfMonitorDataError
from .models import MarketDataSnapshot
from .store import EtfMonitorStore


SUPPORTED_ACTIONS = {
    CorporateActionType.CASH_DIVIDEND,
    CorporateActionType.STOCK_DIVIDEND,
    CorporateActionType.CASH_AND_STOCK_DIVIDEND,
    CorporateActionType.SPLIT,
    CorporateActionType.REVERSE_SPLIT,
    CorporateActionType.CAPITAL_REDUCTION,
    CorporateActionType.PAR_VALUE_CHANGE,
    CorporateActionType.CASH_CAPITAL_INCREASE_RIGHTS,
}


class CorporateActionAccounting:
    """Validates and journals accepted corporate-action accounting evidence.

    The accepted core remains the sole owner of technical-price and stop
    rebasing.  This journal makes dividends, share transforms, reductions and
    par-value changes durable without inventing a live portfolio quantity.
    """

    def validate(self, snapshot: MarketDataSnapshot) -> dict[str, Any]:
        considered = []
        for event in snapshot.corporate_actions:
            if event.effective_date > snapshot.as_of:
                continue
            if event.action_type not in SUPPORTED_ACTIONS:
                raise EtfMonitorDataError(
                    f"unsupported corporate action in accepted snapshot: {event.action_id}"
                )
            try:
                event.validate_position_lifecycle()
            except Exception as exc:
                raise EtfMonitorDataError(
                    f"corporate action lifecycle is unresolved: {event.action_id}"
                ) from exc
            considered.append(
                {
                    "action_id": event.action_id,
                    "action_type": event.action_type.value,
                    "effective_date": event.effective_date,
                    "cash_price_factor": event.cash_price_factor,
                    "share_price_factor": event.share_price_factor,
                    "share_volume_factor": event.share_volume_factor,
                    "rights_price_factor": event.rights_price_factor,
                    "cash_dividend": event.cash_dividend,
                    "stock_dividend_shares": event.stock_dividend_shares,
                }
            )
        return {"eligible_count": len(considered), "events": considered}

    def reconcile(self, snapshot: MarketDataSnapshot, store: EtfMonitorStore) -> dict[str, Any]:
        summary = self.validate(snapshot)
        return {**summary, "newly_journaled": store.record_corporate_actions(snapshot)}
