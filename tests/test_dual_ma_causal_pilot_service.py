from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from stock_ai_bot.selection import dual_ma_causal_pilot_service as service


def _day(day: date, codes: list[str], programs: dict[str, list[str]]) -> service.SelectionDay:
    payload = {
        "content_sha256": day.isoformat(),
        "union_codes": codes,
        "candidates": [
            {"code": code, "programs": programs.get(code, [])}
            for code in codes
        ],
    }
    return service.SelectionDay(day, Path(f"{day}.json"), payload)


def _prices(count: int, effective_on: str) -> pd.DataFrame:
    effective = pd.Timestamp(effective_on)
    dates = pd.bdate_range(end=effective, periods=count + 1)
    return pd.DataFrame(
        {
            "date": dates,
            "open": 1.0,
            "high": 2.0,
            "low": 0.5,
            "close": 1.5,
            "volume": 100,
            "adj_close": 1.5,
        }
    )


def test_find_entry_events_excludes_left_censored_first_day(monkeypatch):
    days = [
        _day(date(2022, 1, 3), ["1101"], {"1101": ["financial"]}),
        _day(date(2022, 1, 4), ["1101", "2330"], {"2330": ["technical"]}),
    ]
    monkeypatch.setattr(
        service,
        "_snapshot_members",
        lambda _day: {
            "1101": {"symbol": "1101.TW", "market": "TWSE", "name": "A"},
            "2330": {"symbol": "2330.TW", "market": "TWSE", "name": "B"},
        },
    )

    events = service.find_entry_events(
        days,
        [date(2022, 1, 3), date(2022, 1, 4), date(2022, 1, 5)],
    )

    assert [event.code for event in events] == ["2330"]
    assert events[0].selected_on == date(2022, 1, 4)
    assert events[0].effective_on == date(2022, 1, 5)
    assert events[0].previous_completed_on == date(2022, 1, 3)


def test_select_pilot_uses_earliest_fixed_core_then_coverage_supplement():
    previous = _day(date(2022, 1, 3), [], {})
    events = []
    frames = {}
    selected_dates = [value.date() for value in pd.bdate_range("2022-06-01", periods=14)]
    for number, selected_on in enumerate(selected_dates, start=1):
        code = f"{number:04d}"
        effective_on = (pd.Timestamp(selected_on) + pd.offsets.BDay(1)).date()
        current = _day(selected_on, [code], {code: ["financial"]})
        market = "TWSE"
        programs = ("financial",)
        if number == 14:
            market = "TPEX"
            programs = ("laoxiao",)
        event = service.EntryEvent(
            code=code,
            selected_on=selected_on,
            effective_on=effective_on,
            previous_completed_on=previous.report_date,
            programs=programs,
            current_day=current,
            previous_day=previous,
            symbol=f"{code}.TW",
            market=market,
            name=code,
        )
        events.append(event)
        frames[event.symbol] = _prices(250, event.effective_on.isoformat())

    selected, roles, coverage = service.select_pilot_events(
        events,
        frames,
        core_count=12,
        max_count=13,
    )

    assert len(selected) == 13
    assert {event.code for event in selected[:12]} == {f"{number:04d}" for number in range(1, 13)}
    assert selected[-1].code == "0014"
    assert roles[f"{selected_dates[-1].isoformat()}:0014"] == "COVERAGE_SUPPLEMENT"
    assert coverage["uncovered_tokens"] == []


def test_target_warmup_requires_effective_day_bar():
    event = SimpleNamespace(effective_on=date(2022, 1, 5))
    assert service._eligible_for_target_warmup(event, _prices(250, "2022-01-05"))
    assert not service._eligible_for_target_warmup(event, _prices(249, "2022-01-05"))
    frame_without_effective = _prices(250, "2022-01-04")
    assert not service._eligible_for_target_warmup(event, frame_without_effective)


def test_write_immutable_is_idempotent_and_rejects_collision(tmp_path):
    path = tmp_path / "frozen.csv"
    service._write_immutable(path, b"a\n")
    service._write_immutable(path, b"a\n")
    try:
        service._write_immutable(path, b"b\n")
    except ValueError as exc:
        assert "collision" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("collision must be rejected")
