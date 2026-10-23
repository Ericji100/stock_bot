"""Offline transition matrix and downtime regressions for ETF next-open fills."""

from __future__ import annotations

import asyncio
import json
import runpy
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from stock_ai_bot.monitoring.etf_monitor.errors import EtfMonitorRoutingError


H = runpy.run_path(str(Path(__file__).with_name("test_etf_monitor_service.py")))
Decision = H["Decision"]
Phase = H["MonitorPhase"]
DAY = H["DAY"]
NOW = H["NOW"]
NEXT_DAY = date(2026, 10, 22)


class DailyData:
    async def load(self, phase, as_of):
        return H["_snapshot"](
            complete=phase is Phase.RECONCILIATION,
            day=as_of,
            raw_open=Decimal("52.5"),
        )


def _active_pending(store):
    return [
        row
        for row in H["_pending"](store.path)
        if row["status"] == "PENDING_NEXT_SESSION_OPEN"
    ]


def _transition_outcome(prior, eod):
    if eod is Decision.HOLD:
        return (
            "CANCELLED_BY_RECONCILIATION"
            if prior in {Decision.BUY_NEXT_OPEN, Decision.SELL_NEXT_OPEN}
            else "UNCHANGED"
        )
    if prior in {None, Decision.HOLD}:
        return "CREATED_BY_RECONCILIATION"
    if prior is eod:
        return "UNCHANGED"
    return "SUPERSEDED_BY_RECONCILIATION"


@pytest.mark.parametrize(
    "prior",
    [None, Decision.HOLD, Decision.BUY_NEXT_OPEN, Decision.SELL_NEXT_OPEN],
    ids=["NONE", "HOLD", "BUY", "SELL"],
)
@pytest.mark.parametrize(
    "eod",
    [Decision.HOLD, Decision.BUY_NEXT_OPEN, Decision.SELL_NEXT_OPEN],
    ids=["to-HOLD", "to-BUY", "to-SELL"],
)
def test_eod_transition_matrix_has_complete_durable_outcome(tmp_path, prior, eod):
    states = []
    if prior is not None:
        states.append(H["_core"](prior, day=DAY))
    states.extend(
        [
            H["_core"](eod, day=DAY),
            H["_core"](Decision.HOLD, day=NEXT_DAY),
        ]
    )
    service, store = H["_service"](tmp_path, states)
    service.data_provider = DailyData()

    decision_event_id = None
    if prior is not None:
        decision = asyncio.run(service.run(Phase.DECISION, now=NOW))
        decision_event_id = decision.event_ids[0]
    eod_outcome = asyncio.run(
        service.run(Phase.RECONCILIATION, now=NOW.replace(hour=22))
    )
    assert eod_outcome.status == (
        "CORRECTED" if prior is not None and prior is not eod else "COMPLETED"
    )
    event = store.event(eod_outcome.event_ids[0])
    payload = json.loads(event["payload_json"])
    transition = payload["pending_transition"]
    assert transition["outcome"] == _transition_outcome(prior, eod)
    assert transition["new_action"] == eod.value
    if prior is not None and prior is not eod:
        assert event["supersedes_event_id"] == decision_event_id

    active = _active_pending(store)
    if eod is Decision.HOLD:
        assert active == []
    else:
        assert len(active) == 1
        pending = active[0]
        assert pending["action"] == eod.value
        assert pending["expected_fill_date"] == NEXT_DAY.isoformat()
        assert pending["reconciled_by_event_id"] == eod_outcome.event_ids[0]
        assert pending["expected_fill_calendar_json"] is not None

    next_outcome = asyncio.run(
        service.run(Phase.DECISION, now=NOW.replace(day=22, hour=13, minute=55))
    )
    assert next_outcome.status == "COMPLETED"
    rows = H["_pending"](store.path)
    if eod is Decision.HOLD:
        assert not any(row["status"] == "FILLED_SHADOW_NEXT_OPEN" for row in rows)
    else:
        filled = [
            row for row in rows if row["status"] == "FILLED_SHADOW_NEXT_OPEN"
        ]
        assert len(filled) == 1
        assert filled[0]["action"] == eod.value
        assert filled[0]["fill_date"] == NEXT_DAY.isoformat()
        assert Decimal(filled[0]["fill_price"]) == Decimal("52.5")


@pytest.mark.parametrize(
    "prior",
    [None, Decision.HOLD, Decision.BUY_NEXT_OPEN, Decision.SELL_NEXT_OPEN],
    ids=["NONE", "HOLD", "BUY", "SELL"],
)
@pytest.mark.parametrize(
    "eod",
    [Decision.HOLD, Decision.BUY_NEXT_OPEN, Decision.SELL_NEXT_OPEN],
    ids=["to-HOLD", "to-BUY", "to-SELL"],
)
def test_every_eod_transition_is_restart_safe_idempotent_and_fill_eligible(
    tmp_path, prior, eod
):
    class ChangingCalendar:
        def __init__(self, generation):
            self.generation = generation
            self.calls = 0

        def __call__(self, day):
            self.calls += 1
            return {
                "is_trading_day": True,
                "source": "TEST_OFFICIAL_CALENDAR",
                "trading_date": day.isoformat(),
                "receipt_sha256": "a" * 64,
                "fetched_at": f"{self.generation}-{self.calls}",
            }

    class FailingRouter:
        async def deliver(self, event_id):
            raise EtfMonitorRoutingError("simulated interruption after durable writes")

    initial_states = [] if prior is None else [H["_core"](prior, day=DAY)]
    initial_states.append(H["_core"](eod, day=DAY))
    service, store = H["_service"](tmp_path, initial_states)
    service.data_provider = DailyData()
    service.trading_day_predicate = ChangingCalendar("before-restart")
    if prior is not None:
        asyncio.run(service.run(Phase.DECISION, now=NOW))
    service.router = FailingRouter()
    with pytest.raises(EtfMonitorRoutingError):
        asyncio.run(service.run(Phase.RECONCILIATION, now=NOW.replace(hour=22)))

    assert len(_active_pending(store)) == (0 if eod is Decision.HOLD else 1)
    if eod is not Decision.HOLD:
        pending = _active_pending(store)[0]
        assert pending["reconciled_by_event_id"] is not None
        assert pending["expected_fill_date"] == NEXT_DAY.isoformat()
        assert store.settle_pending_actions_at_open(
            trading_date=NEXT_DAY,
            raw_open=Decimal("52.5"),
            snapshot_id="failed-job-must-not-fill",
        ) == ()

    reopened = H["EtfMonitorStore"](store.path)
    reopened.recover_after_restart()
    retry_service = H["EtfMonitorService"](
        config=service.config,
        store=reopened,
        data_provider=DailyData(),
        router=None,
        core=H["_Core"]([H["_core"](eod, day=DAY)]),
        trading_day_predicate=ChangingCalendar("after-restart"),
    )
    retry = asyncio.run(
        retry_service.run(
            Phase.RECONCILIATION,
            now=NOW.replace(hour=22, minute=5),
        )
    )
    assert retry.status in {"COMPLETED", "CORRECTED"}
    duplicate = asyncio.run(
        retry_service.run(
            Phase.RECONCILIATION,
            now=NOW.replace(hour=22, minute=10),
        )
    )
    assert duplicate.status == "ALREADY_COMPLETED"
    assert len(_active_pending(reopened)) == (0 if eod is Decision.HOLD else 1)

    next_service = H["EtfMonitorService"](
        config=service.config,
        store=reopened,
        data_provider=DailyData(),
        router=None,
        core=H["_Core"]([H["_core"](Decision.HOLD, day=NEXT_DAY)]),
        trading_day_predicate=ChangingCalendar("next-session"),
    )
    result = asyncio.run(
        next_service.run(
            Phase.DECISION,
            now=NOW.replace(day=22, hour=13, minute=55),
        )
    )
    assert result.status == "COMPLETED"
    filled = [
        row
        for row in H["_pending"](reopened.path)
        if row["status"] == "FILLED_SHADOW_NEXT_OPEN"
    ]
    assert len(filled) == (0 if eod is Decision.HOLD else 1)
    if filled:
        assert filled[0]["action"] == eod.value


@pytest.mark.parametrize(
    "action",
    [Decision.BUY_NEXT_OPEN, Decision.SELL_NEXT_OPEN],
    ids=["BUY", "SELL"],
)
def test_next_open_fill_then_delivery_failure_retries_without_collision_or_duplicate(
    tmp_path, action
):
    class FailingRouter:
        async def deliver(self, event_id):
            raise EtfMonitorRoutingError(
                "simulated delivery interruption after durable next-open fill"
            )

    service, store = H["_service"](
        tmp_path,
        [
            H["_core"](action, day=DAY),
            H["_core"](action, day=DAY),
            H["_core"](Decision.HOLD, day=NEXT_DAY),
        ],
    )
    service.data_provider = DailyData()
    asyncio.run(service.run(Phase.DECISION, now=NOW))
    asyncio.run(service.run(Phase.RECONCILIATION, now=NOW.replace(hour=22)))
    service.router = FailingRouter()
    with pytest.raises(EtfMonitorRoutingError):
        asyncio.run(service.run(Phase.DECISION, now=NOW.replace(day=22)))
    before = H["_pending"](store.path)
    assert len(
        [row for row in before if row["status"] == "FILLED_SHADOW_NEXT_OPEN"]
    ) == 1

    reopened = H["EtfMonitorStore"](store.path)
    reopened.recover_after_restart()
    retry_service = H["EtfMonitorService"](
        config=service.config,
        store=reopened,
        data_provider=DailyData(),
        router=None,
        core=H["_Core"]([H["_core"](Decision.HOLD, day=NEXT_DAY)]),
        trading_day_predicate=service.trading_day_predicate,
    )
    retry = asyncio.run(
        retry_service.run(Phase.DECISION, now=NOW.replace(day=22, minute=59))
    )
    assert retry.status == "COMPLETED"
    assert H["_pending"](store.path) == before


def test_preview_does_not_change_confirmed_formal_state(tmp_path):
    service, store = H["_service"](
        tmp_path,
        [
            H["_core"](Decision.BUY_NEXT_OPEN, day=DAY),
            H["_core"](Decision.BUY_NEXT_OPEN, day=DAY),
            H["_core"](Decision.HOLD, day=NEXT_DAY),
        ],
    )
    service.data_provider = DailyData()
    asyncio.run(service.run(Phase.DECISION, now=NOW))
    asyncio.run(service.run(Phase.RECONCILIATION, now=NOW.replace(hour=22)))
    before_pending = H["_pending"](store.path)
    before_state = store.latest_state()
    before_actions = H["_count"](store.path, "corporate_actions")

    preview = asyncio.run(
        service.run(
            Phase.PREVIEW,
            now=NOW.replace(day=22, hour=12, minute=45),
        )
    )
    assert preview.status == "COMPLETED"
    assert preview.metadata["shadow_next_open_fills"] == ()
    assert H["_pending"](store.path) == before_pending
    assert store.latest_state() == before_state
    assert H["_count"](store.path, "corporate_actions") == before_actions


def test_unconfirmed_instruction_has_no_fill_gate(tmp_path):
    service, store = H["_service"](
        tmp_path,
        [H["_core"](Decision.BUY_NEXT_OPEN, day=DAY)],
    )
    asyncio.run(service.run(Phase.DECISION, now=NOW))
    assert store.settle_pending_actions_at_open(
        trading_date=NEXT_DAY,
        raw_open=Decimal("52.5"),
        snapshot_id="unconfirmed",
    ) == ()
    pending = H["_pending"](store.path)[0]
    assert pending["reconciled_by_event_id"] is None
    assert pending["expected_fill_date"] is None
    assert pending["fill_price"] is None


def test_missed_session_uses_fixed_historical_open_not_resume_open(tmp_path):
    resume_day = date(2026, 10, 23)

    class HistoricalData(DailyData):
        async def load(self, phase, as_of):
            snapshot = await super().load(phase, as_of)
            if as_of == resume_day:
                expected_row = {
                    "date": NEXT_DAY,
                    "open": Decimal("51"),
                    "high": Decimal("53"),
                    "low": Decimal("49"),
                    "close": Decimal("51"),
                }
                return replace(snapshot, raw_rows=(expected_row,) + snapshot.raw_rows)
            return snapshot

    service, store = H["_service"](
        tmp_path,
        [
            H["_core"](Decision.BUY_NEXT_OPEN, day=DAY),
            H["_core"](Decision.BUY_NEXT_OPEN, day=DAY),
            H["_core"](Decision.HOLD, day=resume_day),
        ],
    )
    service.data_provider = HistoricalData()
    asyncio.run(service.run(Phase.DECISION, now=NOW))
    asyncio.run(service.run(Phase.RECONCILIATION, now=NOW.replace(hour=22)))
    asyncio.run(service.run(Phase.DECISION, now=NOW.replace(day=23)))
    filled = H["_pending"](store.path)[0]
    assert filled["fill_date"] == NEXT_DAY.isoformat()
    assert Decimal(filled["fill_price"]) == Decimal("51")


def test_missing_fixed_session_open_fails_closed_then_recovers(tmp_path):
    resume_day = date(2026, 10, 23)

    class RecoverableData(DailyData):
        def __init__(self, recovered=False):
            self.recovered = recovered

        async def load(self, phase, as_of):
            snapshot = await super().load(phase, as_of)
            if self.recovered and as_of == resume_day:
                expected_row = {
                    "date": NEXT_DAY,
                    "open": Decimal("51"),
                    "high": Decimal("53"),
                    "low": Decimal("49"),
                    "close": Decimal("51"),
                }
                return replace(snapshot, raw_rows=(expected_row,) + snapshot.raw_rows)
            return snapshot

    service, store = H["_service"](
        tmp_path,
        [
            H["_core"](Decision.BUY_NEXT_OPEN, day=DAY),
            H["_core"](Decision.BUY_NEXT_OPEN, day=DAY),
            H["_core"](Decision.HOLD, day=resume_day),
        ],
    )
    service.data_provider = RecoverableData()
    asyncio.run(service.run(Phase.DECISION, now=NOW))
    asyncio.run(service.run(Phase.RECONCILIATION, now=NOW.replace(hour=22)))
    blocked = asyncio.run(service.run(Phase.DECISION, now=NOW.replace(day=23)))
    assert blocked.status == "DATA_UNAVAILABLE"
    assert blocked.message == "ETFMONITORNEXTSESSIONOPENUNAVAILABLE"
    assert H["_pending"](store.path)[0]["fill_date"] is None

    reopened = H["EtfMonitorStore"](store.path)
    reopened.recover_after_restart()
    recovered_service = H["EtfMonitorService"](
        config=service.config,
        store=reopened,
        data_provider=RecoverableData(recovered=True),
        router=None,
        core=H["_Core"]([H["_core"](Decision.HOLD, day=resume_day)]),
        trading_day_predicate=lambda _: True,
    )
    recovered = asyncio.run(
        recovered_service.run(Phase.DECISION, now=NOW.replace(day=23))
    )
    assert recovered.status == "COMPLETED"
    filled = H["_pending"](reopened.path)[0]
    assert filled["fill_date"] == NEXT_DAY.isoformat()
    assert Decimal(filled["fill_price"]) == Decimal("51")


def test_weekend_and_holiday_pin_the_first_official_session(tmp_path):
    signal_day = date(2026, 10, 23)
    holiday = date(2026, 10, 26)
    next_session = date(2026, 10, 27)
    service, store = H["_service"](
        tmp_path,
        [
            H["_core"](Decision.BUY_NEXT_OPEN, day=signal_day),
            H["_core"](Decision.BUY_NEXT_OPEN, day=signal_day),
            H["_core"](Decision.HOLD, day=next_session),
        ],
    )
    service.data_provider = DailyData()
    service.trading_day_predicate = lambda day: {
        "is_trading_day": day.weekday() < 5 and day != holiday,
        "source": "TEST_OFFICIAL_CALENDAR",
        "trading_date": day.isoformat(),
    }
    signal_now = NOW.replace(day=23)
    asyncio.run(service.run(Phase.DECISION, now=signal_now))
    asyncio.run(service.run(Phase.RECONCILIATION, now=signal_now.replace(hour=22)))
    assert _active_pending(store)[0]["expected_fill_date"] == next_session.isoformat()
    assert asyncio.run(
        service.run(Phase.DECISION, now=signal_now.replace(day=25))
    ).status == "SKIPPED_OFFICIAL_NON_TRADING_DAY"
    assert asyncio.run(
        service.run(Phase.DECISION, now=signal_now.replace(day=26))
    ).status == "SKIPPED_OFFICIAL_NON_TRADING_DAY"
    asyncio.run(service.run(Phase.DECISION, now=signal_now.replace(day=27)))
    assert H["_pending"](store.path)[0]["fill_date"] == next_session.isoformat()


@pytest.mark.parametrize("action_kind", ["SPLIT", "CASH_DIVIDEND"])
def test_fill_uses_raw_open_at_corporate_action_coordinate(tmp_path, action_kind):
    class Accounting:
        def __init__(self):
            self.seen = []

        def validate(self, snapshot):
            self.seen.extend(snapshot.corporate_actions)
            return {"eligible_count": len(snapshot.corporate_actions), "events": []}

        def reconcile(self, snapshot, store):
            return self.validate(snapshot)

    class CorporateActionData(DailyData):
        async def load(self, phase, as_of):
            snapshot = await super().load(phase, as_of)
            snapshot = replace(
                snapshot,
                raw_rows=(
                    {
                        **snapshot.raw_rows[0],
                        "open": Decimal("25") if as_of == NEXT_DAY else Decimal("50"),
                    },
                ),
                corporate_actions=(
                    {"action_type": action_kind, "effective_date": NEXT_DAY},
                ),
            )
            return snapshot

    service, store = H["_service"](
        tmp_path,
        [
            H["_core"](Decision.BUY_NEXT_OPEN, day=DAY),
            H["_core"](Decision.BUY_NEXT_OPEN, day=DAY),
            H["_core"](Decision.HOLD, day=NEXT_DAY),
        ],
    )
    accounting = Accounting()
    service.accounting = accounting
    service.data_provider = CorporateActionData()
    asyncio.run(service.run(Phase.DECISION, now=NOW))
    asyncio.run(service.run(Phase.RECONCILIATION, now=NOW.replace(hour=22)))
    asyncio.run(service.run(Phase.DECISION, now=NOW.replace(day=22)))
    filled = H["_pending"](store.path)[0]
    assert filled["fill_date"] == NEXT_DAY.isoformat()
    assert Decimal(filled["fill_price"]) == Decimal("25")
    assert any(item["action_type"] == action_kind for item in accounting.seen)
