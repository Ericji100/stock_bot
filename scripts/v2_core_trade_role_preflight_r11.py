"""R11 candidate: route trade *role* from an AS-OF execution ledger.

This is a deterministic preflight, not a structural judgement or a buy signal.
The snapshot must come from actual fills and the signal-date closing mark. An
old AI answer, unfilled signal, or later backtest outcome is not a position.
"""

from __future__ import annotations

from datetime import date
from hashlib import sha256
import json
from math import isfinite
from typing import Any


VERSION = "v2-core-trade-role-preflight-r11-candidate-r1"
_LEG_ROLES = ("MOTHER", "ADD_1", "ADD_2", "ADD_3")


def _date(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{field} must be an ISO date")
    return value


def preflight_trade_role_asof(
    signal_date: str, snapshot: dict[str, Any] | None, *, max_adds: int = 2,
) -> dict[str, Any]:
    """Return the only permissible review lane, without granting a trade.

    `snapshot` is the execution ledger's stock-level state *after* the current
    close and *before* next-open orders are filled. A missing snapshot is
    explicitly unresolved; it must never be assumed to mean a flat account.
    `net_liquidation_pnl_asof_close` includes costs and known cash actions.
    """
    signal_date = _date(signal_date, "signal_date")
    if not isinstance(max_adds, int) or isinstance(max_adds, bool) or not 0 <= max_adds <= 3:
        raise ValueError("max_adds must be an integer from 0 to 3")

    snapshot_digest = (
        sha256(json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
        if isinstance(snapshot, dict) else None
    )

    def result(role: str, status: str, *, episode_id: str | None = None,
               requires_new_episode: bool = False) -> dict[str, Any]:
        return {
            "gate_version": VERSION,
            "snapshot_sha256": snapshot_digest,
            "as_of": signal_date,
            "review_role": role,
            "status": status,
            "current_episode_id": episode_id,
            "requires_new_episode": requires_new_episode,
            "trade_permission_granted": False,
            "structural_scenario_selected_by_program": False,
        }

    if snapshot is None:
        return result("ROLE_UNKNOWN", "MISSING_ASOF_POSITION_SNAPSHOT")
    if not isinstance(snapshot, dict):
        raise ValueError("snapshot must be an object")
    required = {
        "as_of", "source", "watchlist_active", "campaign_state", "open_position",
        "closed_episode_count", "pending_order",
    }
    if required - snapshot.keys():
        raise ValueError(f"snapshot missing: {sorted(required - snapshot.keys())}")
    if snapshot.keys() - required:
        raise ValueError(f"snapshot has forbidden fields: {sorted(snapshot.keys() - required)}")
    if _date(snapshot["as_of"], "snapshot.as_of") != signal_date:
        raise ValueError("position snapshot must be marked at signal close")
    if snapshot["source"] != "CAUSAL_EXECUTION_LEDGER":
        raise ValueError("position snapshot must come from causal execution ledger")
    if type(snapshot["watchlist_active"]) is not bool:
        raise ValueError("watchlist_active must be boolean")
    if snapshot["campaign_state"] not in {"ACTIVE", "INVALIDATED"}:
        raise ValueError("invalid campaign_state")
    closed = snapshot["closed_episode_count"]
    if type(closed) is not int or closed < 0:
        raise ValueError("closed_episode_count must be nonnegative integer")
    if snapshot["pending_order"] not in {"NONE", "BUY", "SELL"}:
        raise ValueError("invalid pending_order")

    position = snapshot["open_position"]
    if position is not None:
        position_fields = {"episode_id", "filled_legs", "net_liquidation_pnl_asof_close"}
        if not isinstance(position, dict) or set(position) != position_fields:
            raise ValueError("open position must have episode, actual fills, and current net mark")
        episode_id = position["episode_id"]
        if not isinstance(episode_id, str) or not episode_id:
            raise ValueError("open position needs episode_id")
        legs = position["filled_legs"]
        if not isinstance(legs, list) or not legs or len(legs) > len(_LEG_ROLES):
            raise ValueError("filled_legs must contain one mother and at most three adds")
        dates: list[str] = []
        for index, leg in enumerate(legs):
            if not isinstance(leg, dict) or set(leg) != {"role", "fill_date"} or leg.get("role") != _LEG_ROLES[index]:
                raise ValueError("filled legs must be MOTHER, ADD_1, ADD_2, ADD_3 in order")
            filled = _date(leg.get("fill_date"), "filled_leg.fill_date")
            if filled > signal_date:
                raise ValueError("future fill in AS-OF position")
            dates.append(filled)
        if dates != sorted(set(dates)):
            raise ValueError("add fills must be on distinct later trading dates")
        pnl = position["net_liquidation_pnl_asof_close"]
        if isinstance(pnl, bool) or not isinstance(pnl, (int, float)) or not isfinite(pnl):
            raise ValueError("net close-mark P&L must be finite")
    else:
        episode_id = None
        legs = []
        dates = []
        pnl = None

    if not snapshot["watchlist_active"] or snapshot["campaign_state"] == "INVALIDATED":
        return result("NONE", "WATCH_OR_CAMPAIGN_INACTIVE", episode_id=episode_id)
    if snapshot["pending_order"] != "NONE":
        return result("NONE", "PENDING_ORDER_BLOCKS_NEW_SIGNAL", episode_id=episode_id)
    if position is None:
        return result(
            "MOTHER_OR_REENTRY", "READY_FOR_MOTHER_REVIEW",
            requires_new_episode=closed > 0,
        )
    add_count = len(legs) - 1
    if add_count > max_adds:
        raise ValueError("position exceeds this portfolio variant's add cap")
    if add_count == max_adds:
        return result("NONE", "ADD_CAP_REACHED", episode_id=episode_id)
    next_role = _LEG_ROLES[add_count + 1]
    if dates[-1] == signal_date:
        return result(next_role, "SAME_DAY_FILL_BLOCKS_ADD", episode_id=episode_id)
    if pnl <= 0:
        return result(next_role, "NONPROFITABLE_POSITION_BLOCKS_ADD", episode_id=episode_id)
    return result(next_role, "READY_FOR_ADD_REVIEW", episode_id=episode_id)
