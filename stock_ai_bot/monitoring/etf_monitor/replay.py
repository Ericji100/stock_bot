from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import sqlite3
import tempfile
from contextlib import closing
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from backtests.dual_ma import etf_0050_2006_fixed_methods_v1 as accepted_package
from backtests.dual_ma import etf_0050_execution_stability_v1 as execution
from backtests.dual_ma import etf_0050_ma21_ma144_regime_v1 as regime
from backtests.dual_ma.etf_0050_base_v1 import CashDividendEvent, SplitEvent
from backtests.dual_ma.etf_0050_formal_v1 import PackageData
from backtests.dual_ma.etf_0050_formal_v2 import CONTINUOUS_CAPITAL, replay_cash_safe_strategy
from backtests.dual_ma.etf_0050_ten_year_same_method_v1 import _d, _event, normalize_value
from backtests.dual_ma.fugle_source import sha256_file
from backtests.dual_ma.models import Bar

from stock_ai_bot.data_sources.etf_snapshot_registry import resolve_snapshot, validate_snapshot

from .config import load_etf_monitor_config
from .core import ACCEPTED_START_DATE, AcceptedFinalV17Core, RESEARCH_SLIPPAGE_BPS, STRATEGY_CORE_ID
from .errors import EtfMonitorDataError
from .models import AcceptedCoreState, MarketDataSnapshot, MonitorPhase, accepted_core_payload, canonical_json, stable_sha256
from .service import EtfMonitorService
from .store import EtfMonitorStore


FINAL_ROOT = (
    Path(__file__).resolve().parents[3]
    / "docs"
    / "research"
    / "dual_ma"
    / "0050"
    / "final-v1.7"
)
EXPECTED_COUNTS = {
    "next-session-open-daily-equity.csv": 5093,
    "next-session-open-transactions.csv": 181,
    "next-session-open-corporate-actions.csv": 63,
    "next-session-open-trade-details.csv": 91,
}


def _rows(path: Path) -> list[dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))
    except OSError as exc:
        raise EtfMonitorDataError(f"accepted replay artifact unavailable: {path.name}") from exc


def verify_final_v17_artifacts(root: Path | None = None) -> dict[str, Any]:
    """Read-only replay gate for the exact accepted final-v1.7 artifacts."""

    base = (root or FINAL_ROOT).resolve()
    try:
        parity = json.loads((base / "core-parity.json").read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EtfMonitorDataError("accepted core parity artifact is unreadable") from exc
    if parity.get("status") != "PASS_EXACT":
        raise EtfMonitorDataError("accepted final-v1.7 core parity is not PASS_EXACT")

    checked: dict[str, int] = {}
    loaded: dict[str, list[dict[str, str]]] = {}
    for name, expected in EXPECTED_COUNTS.items():
        rows = _rows(base / name)
        if len(rows) != expected:
            raise EtfMonitorDataError(f"{name} row count changed: {len(rows)} != {expected}")
        if any(row.get("strategy_id") != STRATEGY_CORE_ID for row in rows):
            raise EtfMonitorDataError(f"{name} contains a different strategy id")
        if any(row.get("execution_timing") != "NEXT_SESSION_OPEN" for row in rows):
            raise EtfMonitorDataError(f"{name} contains a different execution timing")
        if any(int(row.get("slippage_bps") or -1) != RESEARCH_SLIPPAGE_BPS for row in rows):
            raise EtfMonitorDataError(f"{name} contains a different research slippage")
        checked[name] = len(rows)
        loaded[name] = rows

    transactions = loaded["next-session-open-transactions.csv"]
    prior_fill: date | None = None
    for row in transactions:
        signal_date = date.fromisoformat(row["signal_date"])
        fill_date = date.fromisoformat(row["date"])
        if row.get("fill_timing") != "NEXT_SESSION_OPEN" or signal_date >= fill_date:
            raise EtfMonitorDataError("accepted next-session transaction timing changed")
        if row.get("role") not in {"MOTHER", "EXIT"} or row.get("status") != "FILLED":
            raise EtfMonitorDataError("accepted transaction role/status changed")
        if prior_fill is not None and fill_date < prior_fill:
            raise EtfMonitorDataError("accepted transactions are not chronological")
        prior_fill = fill_date

    return {
        "status": "PASS_EXACT",
        "strategy_id": STRATEGY_CORE_ID,
        "execution_timing": "NEXT_SESSION_OPEN",
        "slippage_bps": RESEARCH_SLIPPAGE_BPS,
        "row_counts": checked,
    }


def _load_frozen_package(market_data_root: Path) -> tuple[Any, PackageData, list[dict[str, Any]], tuple[Any, ...]]:
    snapshot = resolve_snapshot("0050", market_data_root=market_data_root)
    validate_snapshot(snapshot, verify_artifacts=True)
    package_root = snapshot.package_root
    raw_rows = json.loads(
        (package_root / "data/0050/daily_raw.json").read_text(encoding="utf-8-sig")
    )["rows"]
    events = tuple(
        _event(row)
        for row in json.loads(
            (package_root / "events/0050_corporate_action_events.json").read_text(
                encoding="utf-8-sig"
            )
        )
    )
    bars = tuple(
        Bar.from_values(
            date.fromisoformat(str(row["date"])),
            _d(row["open"]),
            _d(row["high"]),
            _d(row["low"]),
            _d(row["close"]),
        )
        for row in raw_rows
    )
    split_doc = json.loads(
        (package_root / "events/0050_split.json").read_text(encoding="utf-8-sig")
    )
    split = SplitEvent(
        effective_date=date.fromisoformat(split_doc["milestones"]["resumption_base_date"]),
        ratio=_d(split_doc["ratio"]),
        official_pre_split_reference=Decimal("188.65"),
        official_post_split_reference=_d(split_doc["official_reference_price"]),
        exact_post_split_reference=_d(split_doc["exact_reference_price"]),
        tick_size=_d(split_doc["tick_policy"]["tick_size"]),
        primary_source_url=split_doc["lineage"]["result"]["url"],
        primary_source_sha256=split_doc["lineage"]["result"]["sha256"],
    )
    dividend_doc = json.loads(
        (package_root / "events/0050_dividends.json").read_text(encoding="utf-8-sig")
    )
    dividends = tuple(
        CashDividendEvent(
            date.fromisoformat(row["ex_date"]),
            date.fromisoformat(row["payment_date"]),
            _d(row["amount_per_unit_original"]),
        )
        for row in dividend_doc["events"]
    )
    marker_path = package_root / "STOP_WAIT_00_DATA_QA.json"
    marker = json.loads(marker_path.read_text(encoding="utf-8-sig"))
    package = PackageData(
        marker=marker,
        raw_bars=bars,
        signal_bars=bars,
        sessions=tuple(bar.date for bar in bars),
        raw_by_date={bar.date: bar for bar in bars},
        factors_by_date={},
        split=split,
        dividends=dividends,
        package_hash=sha256_file(snapshot.package_manifest),
        source_master_hash=sha256_file(package_root / "sources/source_master_table.json"),
    )
    return snapshot, package, raw_rows, events


def _normalized_rows(rows: list[dict[str, Any]], fields: list[str]) -> list[list[str]]:
    return [
        [normalize_value(row.get(field)) for field in fields]
        for row in rows
    ]


def _compare_table(
    *,
    name: str,
    actual: list[dict[str, Any]],
    expected_path: Path,
) -> dict[str, Any]:
    expected = _rows(expected_path)
    fields = list(expected[0]) if expected else []
    actual_rows = _normalized_rows(actual, fields)
    expected_rows = _normalized_rows(expected, fields)
    mismatches: list[dict[str, Any]] = []
    for index in range(max(len(actual_rows), len(expected_rows))):
        left = actual_rows[index] if index < len(actual_rows) else None
        right = expected_rows[index] if index < len(expected_rows) else None
        if left != right:
            mismatches.append({"row": index + 1, "actual": left, "expected": right})
            if len(mismatches) == 5:
                break
    encoded = canonical_json({"fields": fields, "rows": actual_rows}).encode("utf-8")
    return {
        "name": name,
        "actual_rows": len(actual_rows),
        "expected_rows": len(expected_rows),
        "fields": fields,
        "mismatch_count": sum(
            1
            for index in range(max(len(actual_rows), len(expected_rows)))
            if (actual_rows[index] if index < len(actual_rows) else None)
            != (expected_rows[index] if index < len(expected_rows) else None)
        ),
        "first_mismatches": mismatches,
        "actual_normalized_sha256": hashlib.sha256(encoded).hexdigest(),
        "expected_file_sha256": sha256_file(expected_path),
    }


def _run_causal_service_lifecycle(
    *,
    raw_rows: list[dict[str, Any]],
    events: tuple[Any, ...],
    end: date,
) -> tuple[list[AcceptedCoreState], dict[str, Any], list[dict[str, Any]]]:
    """Drive the real service phases while the accepted engine reveals one day at a time."""

    current: dict[str, Any] = {}

    class ReplayDataProvider:
        async def load(self, phase: MonitorPhase, as_of: date) -> MarketDataSnapshot:
            state = current["state"]
            prefix = current["prefix"]
            if as_of != state.as_of or prefix[-1]["date"] != as_of:
                raise EtfMonitorDataError("service replay attempted non-current/future data access")
            complete = phase is MonitorPhase.RECONCILIATION
            kind = "complete" if complete else "provisional"
            digest = stable_sha256({"day": as_of, "kind": kind, "rows": len(prefix)})
            return MarketDataSnapshot(
                symbol="0050",
                as_of=as_of,
                raw_rows=prefix,
                corporate_actions=tuple(
                    event for event in events if event.event_date <= as_of
                ),
                source=f"FROZEN_CAUSAL_SERVICE_REPLAY_{kind.upper()}",
                price_basis="UNADJUSTED_MAINBOARD_OHLCV",
                snapshot_id=f"causal-{as_of.isoformat()}-{kind}",
                snapshot_version="frozen-service-replay-v1",
                snapshot_sha256=digest,
                fetched_at=datetime(
                    as_of.year,
                    as_of.month,
                    as_of.day,
                    22 if complete else 13,
                    0 if complete else 55,
                    tzinfo=ZoneInfo("Asia/Taipei"),
                ),
                is_complete=complete,
                is_provisional=not complete,
            )

    class ReplayCore:
        def evaluate(self, snapshot: MarketDataSnapshot) -> AcceptedCoreState:
            state = current["state"]
            if snapshot.as_of != state.as_of or snapshot.latest_date != state.as_of:
                raise EtfMonitorDataError("service core replay crossed its causal boundary")
            return state

    config = replace(
        load_etf_monitor_config(),
        enabled=True,
        daily_shadow_status=False,
    )
    states: list[AcceptedCoreState] = []
    state_mismatches = 0
    restart_checks = 0
    restart_mismatches = 0
    prior_day: date | None = None
    action_days = {event.event_date for event in events}
    trading_sessions = {
        value if isinstance(value, date) else date.fromisoformat(str(value))
        for value in (row["date"] for row in raw_rows)
    }

    def replay_trading_day(day: date) -> Mapping[str, Any]:
        return {
            "is_trading_day": day in trading_sessions,
            "source": "FROZEN_ACCEPTED_SESSION_CALENDAR",
            "trading_date": day.isoformat(),
            "calendar_reason": (
                "ACCEPTED_SESSION" if day in trading_sessions else "NO_ACCEPTED_SESSION"
            ),
        }

    with tempfile.TemporaryDirectory(prefix="etf-causal-service-replay-") as temp:
        database = Path(temp) / "monitor.sqlite3"
        store = EtfMonitorStore(database)
        provider = ReplayDataProvider()

        def make_service(active_store: EtfMonitorStore) -> EtfMonitorService:
            return EtfMonitorService(
                config=config,
                store=active_store,
                data_provider=provider,
                router=None,
                core=ReplayCore(),
                trading_day_predicate=replay_trading_day,
            )

        service = make_service(store)
        loop = asyncio.new_event_loop()

        def on_state(
            state: AcceptedCoreState,
            prefix: tuple[dict[str, Any], ...],
        ) -> None:
            nonlocal service, store, state_mismatches, restart_checks
            nonlocal restart_mismatches, prior_day
            current["state"] = state
            current["prefix"] = prefix
            states.append(state)
            day = state.as_of
            for phase, hour, minute in (
                (MonitorPhase.PREVIEW, 12, 45),
                (MonitorPhase.DECISION, 13, 55),
            ):
                outcome = loop.run_until_complete(service.run(
                    phase,
                    now=datetime(day.year, day.month, day.day, hour, minute, tzinfo=ZoneInfo("Asia/Taipei")),
                ))
                if outcome.status != "COMPLETED":
                    raise EtfMonitorDataError(
                        f"causal service replay {phase.value} failed on {day}: {outcome.status}"
                    )
            persisted = store.latest_state()
            expected = json.loads(canonical_json(accepted_core_payload(state)))
            actual = None if persisted is None else json.loads(str(persisted["core_state_json"]))["core"]
            if actual != expected:
                state_mismatches += 1

            restart_due = (
                state.decision.value != "HOLD"
                or day in action_days
                or prior_day is None
                or day.year != prior_day.year
            )
            if restart_due:
                restart_checks += 1
                before = store.latest_state()
                store = EtfMonitorStore(database)
                store.recover_after_restart()
                service = make_service(store)
                after = store.latest_state()
                if before != after:
                    restart_mismatches += 1

            outcome = loop.run_until_complete(service.run(
                MonitorPhase.RECONCILIATION,
                now=datetime(day.year, day.month, day.day, 22, 0, tzinfo=ZoneInfo("Asia/Taipei")),
            ))
            if outcome.status != "COMPLETED":
                raise EtfMonitorDataError(
                    f"causal service replay reconciliation failed on {day}: {outcome.status}"
                )
            persisted = store.latest_state()
            actual = None if persisted is None else json.loads(str(persisted["core_state_json"]))["core"]
            if actual != expected:
                state_mismatches += 1
            prior_day = day

        try:
            AcceptedFinalV17Core().stream_causal_history(
                raw_rows=raw_rows,
                corporate_actions=events,
                end=end,
                on_state=on_state,
            )
        finally:
            loop.close()

        with closing(sqlite3.connect(database)) as connection:
            connection.row_factory = sqlite3.Row
            counts = {
                table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "events", "outbox", "job_runs", "corrections",
                    "corporate_actions", "pending_actions",
                )
            }
            non_succeeded_jobs = connection.execute(
                "SELECT COUNT(*) FROM job_runs WHERE status != 'SUCCEEDED'"
            ).fetchone()[0]
            non_pending_outbox = connection.execute(
                "SELECT COUNT(*) FROM outbox WHERE status != 'PENDING' OR attempts != 0"
            ).fetchone()[0]
            pending_rows = [
                dict(row) for row in connection.execute(
                    "SELECT * FROM pending_actions ORDER BY signal_date, action_id"
                )
            ]
        audit = {
            "service_days": len(states),
            "phase_sequence": [
                MonitorPhase.PREVIEW.value,
                MonitorPhase.DECISION.value,
                MonitorPhase.RECONCILIATION.value,
            ],
            "counts": counts,
            "non_succeeded_job_runs": non_succeeded_jobs,
            "outbox_delivery_attempts_or_non_pending": non_pending_outbox,
            "state_mismatch_count": state_mismatches,
            "restart_checks": restart_checks,
            "restart_mismatch_count": restart_mismatches,
            "future_rows_exposed": 0,
            "telegram_send_attempts": 0,
        }
    return states, audit, pending_rows


def run_full_live_replay_parity(
    *,
    market_data_root: Path,
    artifact_root: Path | None = None,
) -> dict[str, Any]:
    """Replay frozen raw data day-by-day through LIVE and exact-match v1.7 ledgers."""

    accepted_root = (artifact_root or FINAL_ROOT).resolve()
    registry_snapshot, package, raw_rows, events = _load_frozen_package(
        market_data_root.resolve()
    )
    as_of = accepted_package.CUTOFF
    monitor_snapshot = MarketDataSnapshot(
        symbol="0050",
        as_of=as_of,
        raw_rows=tuple(raw_rows),
        corporate_actions=events,
        source="FROZEN_REGISTRY_FULL_HISTORY_REPLAY",
        price_basis="UNADJUSTED_MAINBOARD_OHLCV",
        snapshot_id=registry_snapshot.snapshot_id,
        snapshot_version=str(registry_snapshot.metadata.get("version") or registry_snapshot.snapshot_id),
        snapshot_sha256=registry_snapshot.package_manifest_sha256,
        fetched_at=datetime(2026, 9, 30, 23, 59, tzinfo=ZoneInfo("Asia/Taipei")),
        is_complete=True,
    )
    states, service_lifecycle, pending_rows = _run_causal_service_lifecycle(
        raw_rows=raw_rows,
        events=events,
        end=as_of,
    )
    if not states or states[0].as_of != ACCEPTED_START_DATE or states[-1].as_of != as_of:
        raise EtfMonitorDataError("LIVE adapter full-history coverage is incomplete")
    if any(
        state.as_of != state.core_daily_row["date"]
        or (index and state.as_of <= states[index - 1].as_of)
        for index, state in enumerate(states)
    ):
        raise EtfMonitorDataError("LIVE adapter replay is not strict causal day order")

    frozen_run_transactions = list(states[-1].core_transactions)
    indicators = regime.build_indicator_rows(raw_rows, events)
    same_day, _daily, _events = regime.build_regime_run(
        indicator_rows=indicators,
        raw_by_date=package.raw_by_date,
        strategy_id=STRATEGY_CORE_ID,
        moving_stop=False,
        start=accepted_package.FORMAL_START,
        end=accepted_package.CUTOFF,
        slippage_bps=RESEARCH_SLIPPAGE_BPS,
    )
    if _normalized_rows(frozen_run_transactions, [
        "date", "role", "status", "leg_id", "reference_price", "initial_stop", "exit_reason"
    ]) != _normalized_rows(list(same_day.transactions), [
        "date", "role", "status", "leg_id", "reference_price", "initial_stop", "exit_reason"
    ]):
        raise EtfMonitorDataError("LIVE adapter signal stream differs from accepted frozen run")
    delayed, scheduling_audit = execution.schedule_frozen_orders(
        run=same_day,
        sessions=package.sessions,
        raw_by_date=package.raw_by_date,
        indicator_by_date={row["date"]: row for row in indicators},
        timing=execution.TIMING_NEXT_OPEN,
    )
    pending_by_key = {
        (
            date.fromisoformat(str(row["signal_date"])),
            str(row["leg_id"] or ""),
            str(row["action"]),
        ): row
        for row in pending_rows
    }
    pending_mismatches: list[dict[str, Any]] = []
    for expected in scheduling_audit:
        action = "BUY_NEXT_OPEN" if expected.get("role") == "MOTHER" else "SELL_NEXT_OPEN"
        key = (expected["signal_date"], str(expected.get("leg_id") or ""), action)
        actual = pending_by_key.get(key)
        matches = actual is not None
        if matches and expected["status"] == "SCHEDULED":
            matches = (
                actual["status"] == "FILLED_SHADOW_NEXT_OPEN"
                and actual["fill_date"] == expected["fill_date"].isoformat()
                and Decimal(str(actual["fill_price"])) == Decimal(str(expected["fill_reference_price"]))
            )
        elif matches:
            matches = (
                actual["status"] == "PENDING_NEXT_SESSION_OPEN"
                and actual["fill_date"] is None
                and actual["fill_price"] is None
            )
        if not matches:
            pending_mismatches.append({
                "signal_date": expected["signal_date"],
                "leg_id": expected.get("leg_id"),
                "role": expected.get("role"),
                "expected_status": expected["status"],
                "actual": actual,
            })
    expected_service_counts = {
        "events": len(states) * 3,
        "job_runs": len(states) * 3,
        "corrections": 0,
        "corporate_actions": len(events),
        "pending_actions": len(scheduling_audit),
        "outbox": len(states) * 2 + len(scheduling_audit),
    }
    service_count_mismatches = {
        key: {"actual": service_lifecycle["counts"].get(key), "expected": value}
        for key, value in expected_service_counts.items()
        if service_lifecycle["counts"].get(key) != value
    }
    service_lifecycle.update({
        "expected_counts": expected_service_counts,
        "count_mismatches": service_count_mismatches,
        "pending_schedule_mismatch_count": len(pending_mismatches),
        "first_pending_schedule_mismatches": pending_mismatches[:5],
        "filled_shadow_next_open": sum(
            row["status"] == "FILLED_SHADOW_NEXT_OPEN" for row in pending_rows
        ),
        "still_pending_at_cutoff": sum(
            row["status"] == "PENDING_NEXT_SESSION_OPEN" for row in pending_rows
        ),
    })
    replay = replay_cash_safe_strategy(
        package=package,
        run=delayed,
        start=accepted_package.FORMAL_START,
        end=accepted_package.CUTOFF,
        slippage_bps=RESEARCH_SLIPPAGE_BPS,
        initial_capital=CONTINUOUS_CAPITAL,
        budget_mode="all_available",
        research_slippage=True,
    )
    prefix = {
        "execution_timing": execution.TIMING_NEXT_OPEN,
        "scenario_id": "NEXT_SESSION_OPEN_20BPS",
        "slippage_bps": RESEARCH_SLIPPAGE_BPS,
        "strategy_id": STRATEGY_CORE_ID,
    }
    transactions = [{**prefix, **row} for row in replay["transactions"]]
    curves = [{**prefix, **row} for row in replay["curve"]]
    actions = [
        {**prefix, "action_kind": "DIVIDEND", **row}
        for row in replay["dividends"]
    ] + [
        {**prefix, "action_kind": "SPLIT", **row}
        for row in replay["splits"]
    ]
    stops = {
        row.get("leg_id", ""): row.get("initial_stop_raw")
        for row in replay["transactions"]
        if row.get("role") == "MOTHER"
    }
    details = [
        {**prefix, **row, "initial_stop": stops.get(row.get("leg_id", ""))}
        for row in replay["details"]
    ]
    comparisons = [
        _compare_table(
            name="next-session-open-transactions.csv",
            actual=transactions,
            expected_path=accepted_root / "next-session-open-transactions.csv",
        ),
        _compare_table(
            name="next-session-open-daily-equity.csv",
            actual=curves,
            expected_path=accepted_root / "next-session-open-daily-equity.csv",
        ),
        _compare_table(
            name="next-session-open-corporate-actions.csv",
            actual=actions,
            expected_path=accepted_root / "next-session-open-corporate-actions.csv",
        ),
        _compare_table(
            name="next-session-open-trade-details.csv",
            actual=details,
            expected_path=accepted_root / "next-session-open-trade-details.csv",
        ),
    ]
    signal_expected = [
        row
        for row in _rows(accepted_root / "strategy-transactions.csv")
        if row.get("execution_timing") == "SIGNAL_CLOSE"
        and row.get("strategy_id") == STRATEGY_CORE_ID
        and row.get("slippage_bps") == str(RESEARCH_SLIPPAGE_BPS)
    ]
    signal_fields = ["date", "role", "status", "leg_id", "reference_price", "exit_reason"]
    signal_mismatch_count = 0 if _normalized_rows(
        frozen_run_transactions, signal_fields
    ) == _normalized_rows(signal_expected, signal_fields) else 1
    daily_state_rows = [
        {
            "date": state.as_of,
            "decision": state.decision,
            "regime": state.regime,
            "active_position": state.active_position,
            "active_leg_id": state.active_leg_id,
            "fixed_initial_stop": state.fixed_initial_stop,
            "macd_armed": state.macd_armed,
            "exit_reason": state.exit_reason,
            "raw_close": state.raw_close,
            "technical_close": state.technical_close,
            "sma21": state.sma21,
            "sma144": state.sma144,
            "histogram": state.histogram,
            "previous_histogram": state.previous_histogram,
        }
        for state in states
    ]
    lifecycle_mismatch_count = (
        len(service_count_mismatches)
        + len(pending_mismatches)
        + int(service_lifecycle["non_succeeded_job_runs"])
        + int(service_lifecycle["outbox_delivery_attempts_or_non_pending"])
        + int(service_lifecycle["state_mismatch_count"])
        + int(service_lifecycle["restart_mismatch_count"])
        + int(service_lifecycle["future_rows_exposed"])
        + int(service_lifecycle["telegram_send_attempts"])
    )
    mismatch_count = (
        signal_mismatch_count
        + sum(row["mismatch_count"] for row in comparisons)
        + lifecycle_mismatch_count
    )
    result = {
        "schema_version": "stock-ai-bot-etf-live-replay-parity-v2",
        "status": "PASS_EXACT" if mismatch_count == 0 else "FAIL",
        "frozen_snapshot_id": registry_snapshot.snapshot_id,
        "frozen_manifest_sha256": registry_snapshot.package_manifest_sha256,
        "period": {"start": states[0].as_of, "through": states[-1].as_of},
        "live_adapter_daily_rows": len(states),
        "live_adapter_daily_state_sha256": hashlib.sha256(
            canonical_json(daily_state_rows).encode("utf-8")
        ).hexdigest(),
        "signal_rows": len(frozen_run_transactions),
        "signal_vs_frozen_artifact_mismatch_count": signal_mismatch_count,
        "schedule_rows": len(scheduling_audit),
        "schedule_unscheduled_at_cutoff": sum(
            row.get("status") == "NO_NEXT_SESSION_BEFORE_CUTOFF"
            for row in scheduling_audit
        ),
        "service_lifecycle": service_lifecycle,
        "comparisons": comparisons,
        "total_mismatch_count": mismatch_count,
        "causality": {
            "mode": "ONE_OFFICIAL_SESSION_AT_A_TIME_THROUGH_SERVICE_PHASES",
            "service_phase_order": "12:45_PREVIEW_TO_13:55_DECISION_TO_RESTART_CHECK_TO_22:00_RECONCILIATION_TO_NEXT_OPEN",
            "strictly_increasing_dates": True,
            "future_rows_exposed_to_daily_output": False,
        },
    }
    if mismatch_count:
        raise EtfMonitorDataError(f"full LIVE/REPLAY parity failed: {canonical_json(result)}")
    return result
