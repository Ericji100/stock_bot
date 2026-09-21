"""Build identity-blind, AS-OF role snapshots for *step-level calibration*.

This reads old V2 executed fills only up to each calibration signal close.
It must never be used as the position ledger for end-to-end new-policy replay.
The old current-day label and all future outcome fields are ignored.
"""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from typing import Any

from scripts.v2_core_trade_role_preflight_r11 import preflight_trade_role_asof


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = ROOT / "reports/course_backtest/2026-09-10/v2_core_reproducible_goal_v1"
BACKTESTS = {
    "889": ROOT / "reports/course_backtest/2023-09-04/historical_scan_formal_ai_v1_v2",
    "1029": ROOT / "reports/course_backtest/2024-02-02/historical_scan_2023h2_formal_ai_v1_v2",
}
VERSION = "v2-core-legacy-position-snapshot-r11-candidate-r1"
OUTPUT_DIR = ARTIFACT / "legacy_position_snapshots_candidate_r11"
MANIFEST = ARTIFACT / "legacy_position_snapshot_manifest_candidate_r11.json"


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def raw_close_asof(price_csv: Path, as_of: str) -> float:
    """Read a single dated mark; never return a later row's raw price."""
    with price_csv.open("r", encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            if row["date"] == as_of:
                return float(row["raw_close"])
            if row["date"] > as_of:
                break
    raise ValueError(f"missing AS-OF raw close: {as_of}")


def source_identity_for_calibration(directory: Path, source_file: str, source_index: int) -> tuple[str, str, str]:
    """Project only identity and selection date from the offline old ledger.

    The source also contains old AI answers; no answer field is returned or
    read by downstream snapshot/role code. The source index is the ledger's
    explicit `index`, not the split manifest's independent line ordinal.
    """
    if Path(source_file).name != source_file or not source_file.endswith("_ai_decisions.jsonl"):
        raise ValueError("unexpected old ledger source file")
    path = directory / source_file
    matches = []
    with path.open("r", encoding="utf-8-sig") as stream:
        for line in stream:
            if line.strip():
                row = json.loads(line)
                if row.get("index") == source_index:
                    matches.append(row)
    if len(matches) != 1:
        raise ValueError("source_index did not uniquely resolve in old ledger")
    row = matches[0]
    return str(row["code"]), str(row["first_selected_on"]), _sha(path)


def snapshot_from_executed_stock(
    stock: dict[str, Any], *, as_of: str, raw_close: float,
    sell_commission: float, sell_tax: float, watchlist_active: bool,
) -> dict[str, Any]:
    """Sanitize a complete old backtest into only prior fills/current mark.

    The complete source has future exits/MFE and future add fills; no such
    fields or values enter the returned snapshot. This is an offline study
    adapter, not a new trading policy's own causal ledger.
    """
    if raw_close <= 0 or not 0 <= sell_commission < 1 or not 0 <= sell_tax < 1:
        raise ValueError("invalid mark or costs")
    episodes = stock["episodes"]
    open_episodes = [
        episode for episode in episodes
        if episode["entry_date"] <= as_of and (
            episode.get("exit_date") is None or episode["exit_date"] > as_of
        )
    ]
    if len(open_episodes) > 1:
        raise ValueError("multiple open episodes in one-stock ledger")
    closed_count = sum(
        1 for episode in episodes if episode.get("exit_date") is not None and episode["exit_date"] <= as_of
    )
    audit = [event for event in stock["audit"] if event["date"] <= as_of]
    invalidated = any(event.get("source_event") == "REMOVED_FROM_WATCHLIST" for event in audit)
    pending_order = "NONE"
    position: dict[str, Any] | None = None
    if open_episodes:
        episode = open_episodes[0]
        if episode.get("exit_signal_date") is not None and episode["exit_signal_date"] <= as_of:
            pending_order = "SELL"
        filled = sorted(
            (leg for leg in episode["tranches"] if leg["entry_date"] <= as_of),
            key=lambda leg: leg["entry_date"],
        )
        if not filled or filled[0]["entry_date"] != episode["entry_date"]:
            raise ValueError("open episode has no causal mother fill")
        legs = []
        net = sum(float(event["cash"]) for event in audit if event.get("event", "").startswith("CASH_DIVIDEND") and event.get("episode") == episode["episode_id"])
        for index, leg in enumerate(filled):
            role = "MOTHER" if index == 0 else f"ADD_{index}"
            legs.append({"role": role, "fill_date": leg["entry_date"]})
            shares = float(leg["entry_shares"])
            for adjustment in leg.get("share_adjustments", []):
                if adjustment["date"] <= as_of:
                    shares *= float(adjustment["ratio"])
            net += shares * raw_close * (1.0 - sell_commission - sell_tax) - float(leg["buy_cost"])
        position = {
            "episode_id": episode["episode_id"],
            "filled_legs": legs,
            "net_liquidation_pnl_asof_close": round(net, 8),
        }
    else:
        # Only an already-approved earlier signal can leave a pending order.
        # A teacher signal on `as_of` is the answer under test, not prior state.
        if any(
            leg["signal_date"] < as_of < leg["entry_date"]
            for episode in episodes for leg in episode["tranches"]
        ):
            pending_order = "BUY"
    return {
        "as_of": as_of,
        "source": "CAUSAL_EXECUTION_LEDGER",
        "watchlist_active": bool(watchlist_active) and not invalidated,
        "campaign_state": "INVALIDATED" if invalidated else "ACTIVE",
        "open_position": position,
        "closed_episode_count": closed_count,
        "pending_order": pending_order,
    }


def build_calibration_snapshots() -> dict[str, Any]:
    """Produce 14 anonymous snapshots; keep source identities in memory."""
    integrity_path = ARTIFACT / "legacy_reference_integrity_audit_candidate_r1.json"
    split_path = ARTIFACT / "calibration_manifest.json"
    integrity = _load(integrity_path)
    split = _load(split_path)
    calibration_codes = {(str(row["batch_id"]), str(row["code"])): row for row in split["rows"]}
    batches = {}
    for batch, directory in BACKTESTS.items():
        backtest_path = directory / "backtest.json"
        backtest = _load(backtest_path)
        variant = backtest["variants"]["V2_MOTHER_PLUS_2"]
        batches[batch] = {
            "stocks": {str(stock["code"]): stock for stock in variant["stocks"]},
            "costs": backtest["costs"],
            "directory": directory,
            "backtest_sha256": _sha(backtest_path),
        }
    output_rows = []
    for source in integrity["rows"]:
        review_id, batch, as_of = source["review_id"], str(source["source_batch"]), source["signal_date"]
        context = batches[batch]
        code, first_selected_on, old_ledger_sha = source_identity_for_calibration(
            context["directory"], source["source_ledger_file"], int(source["source_index"]),
        )
        split_row = calibration_codes.get((batch, code))
        if split_row is None or split_row["first_selected_on"] != first_selected_on:
            raise ValueError("old ledger identity is not in the frozen calibration stock split")
        stock = context["stocks"].get(code, {"episodes": [], "audit": []})
        price_path = context["directory"] / "sources/prices" / f"{code}.csv"
        mark = raw_close_asof(price_path, as_of)
        snapshot = snapshot_from_executed_stock(
            stock, as_of=as_of, raw_close=mark,
            sell_commission=float(context["costs"]["sell_commission"]),
            sell_tax=float(context["costs"]["sell_tax"]),
            watchlist_active=first_selected_on <= as_of,
        )
        role_result = preflight_trade_role_asof(as_of, snapshot)
        output_path = OUTPUT_DIR / f"{review_id}.json"
        _write(output_path, {
            "packet_version": VERSION, "review_id": review_id, "as_of": as_of,
            "calibration_only_prior_legacy_fills": True,
            "end_to_end_new_policy_ledger": False,
            "position_snapshot_as_of": snapshot,
            "role_preflight": role_result,
        })
        output_rows.append({
            "review_id": review_id, "as_of": as_of,
            "packet_file": output_path.name, "packet_sha256": _sha(output_path),
            "source_price_sha256": _sha(price_path),
            "source_backtest_sha256": context["backtest_sha256"],
            "source_old_ledger_sha256": old_ledger_sha,
            "snapshot_sha256": role_result["snapshot_sha256"],
            "review_role": role_result["review_role"], "preflight_status": role_result["status"],
        })
    report = {
        "manifest_version": VERSION,
        "purpose": "STEP_LEVEL_CALIBRATION_ONLY",
        "future_outcome_values_in_output": False,
        "legacy_current_day_answer_used_for_role": False,
        "end_to_end_new_policy_position_ledger": False,
        "case_count": len(output_rows),
        "source_integrity_sha256": _sha(integrity_path),
        "source_split_sha256": _sha(split_path),
        "rows": sorted(output_rows, key=lambda item: item["review_id"]),
    }
    _write(MANIFEST, report)
    return report


if __name__ == "__main__":
    print(json.dumps(build_calibration_snapshots(), ensure_ascii=False, indent=2))
