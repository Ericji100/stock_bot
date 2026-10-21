from __future__ import annotations

import asyncio
import hashlib
import json
import os
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import pytest
import requests

from stock_ai_bot.monitoring.etf_monitor import data as etf_data
from stock_ai_bot.monitoring.etf_monitor import integration
from stock_ai_bot.monitoring.etf_monitor.config import (
    EtfMonitorConfigError,
    load_etf_monitor_config,
)
from stock_ai_bot.monitoring.etf_monitor.data import (
    OfficialDailyDeltaUpdater,
    OfficialTwseTradingCalendar,
    OfficialTwseQuoteProvider,
    RegistrySnapshotProvider,
    install_daily_delta_file,
    load_daily_delta_file,
    resolve_frozen_market_data_root,
)
from stock_ai_bot.monitoring.etf_monitor.errors import EtfMonitorDataError
from stock_ai_bot.monitoring.etf_monitor.models import IntradayQuote, MonitorPhase
from stock_ai_bot.monitoring.etf_monitor.models import stable_sha256


class _Jobs:
    def __init__(self):
        self.calls = []

    def run_daily(self, callback, **kwargs):
        self.calls.append((callback, kwargs))


class _App:
    def __init__(self):
        self.job_queue = _Jobs()
        self.bot_data = {}
        self.bot = object()


def _enabled_config(tmp_path: Path) -> Path:
    source = Path("config/etf_monitor.json")
    payload = json.loads(source.read_text(encoding="utf-8-sig"))
    payload["enabled"] = True
    path = tmp_path / "etf_monitor.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_disabled_feature_is_strict_startup_noop() -> None:
    app = _App()
    assert integration.register_etf_monitor(app) is None
    assert app.job_queue.calls == []
    assert app.bot_data == {}


@pytest.mark.parametrize("failure", ["missing", "malformed", "wrong_chat_id"])
def test_invalid_config_disables_only_etf_without_leaking_details(
    tmp_path, capsys, failure
) -> None:
    path = tmp_path / "private-etf-config.json"
    secret = "never-log-this-config-value"
    if failure == "malformed":
        path.write_text('{"api_token": "' + secret, encoding="utf-8")
    elif failure == "wrong_chat_id":
        payload = json.loads(
            Path("config/etf_monitor.json").read_text(encoding="utf-8-sig")
        )
        payload["chat_id"] = "-9988776655"
        path.write_text(json.dumps(payload), encoding="utf-8")

    app = _App()
    assert integration.register_etf_monitor(app, config_path=path) is None
    output = capsys.readouterr().out
    assert "disabled by invalid configuration: EtfMonitorConfigError" in output
    assert str(path) not in output
    assert "-9988776655" not in output
    assert secret not in output
    assert app.job_queue.calls == []
    assert app.bot_data == {}


def test_enabled_feature_registers_three_jobs_on_existing_queue(tmp_path, monkeypatch) -> None:
    app = _App()
    monkeypatch.setattr(integration, "get_workdata_path", lambda _: tmp_path / "monitor.sqlite3")
    runtime = integration.register_etf_monitor(app, config_path=_enabled_config(tmp_path))
    assert runtime is not None
    assert runtime.router.bot is app.bot
    assert len(app.job_queue.calls) == 3
    assert [call[1]["name"] for call in app.job_queue.calls] == [
        "etf-0050-final-v1.7-preview-1245",
        "etf-0050-final-v1.7-decision-1355",
        "etf-0050-final-v1.7-reconciliation-2200",
    ]
    assert [(call[1]["time"].hour, call[1]["time"].minute) for call in app.job_queue.calls] == [
        (12, 45),
        (13, 55),
        (22, 0),
    ]
    assert all(
        datetime.combine(date(2026, 10, 21), call[1]["time"]).utcoffset().total_seconds()
        == 8 * 3600
        for call in app.job_queue.calls
    )


def test_runtime_retries_data_unavailable_without_duplicate_scheduler_job(monkeypatch) -> None:
    class _Service:
        def __init__(self):
            self.calls = 0

        async def run(self, phase):
            self.calls += 1
            return SimpleNamespace(
                status="DATA_UNAVAILABLE" if self.calls == 1 else "COMPLETED"
            )

    service = _Service()
    config = replace(load_etf_monitor_config(), enabled=True)
    runtime = integration.EtfMonitorRuntime(
        config=config,
        store=object(),
        router=object(),
        service=service,
        locks={phase: asyncio.Lock() for phase in integration.JOB_NAMES},
    )
    monkeypatch.setitem(
        integration.RETRY_DELAYS_SECONDS,
        MonitorPhase.DECISION,
        (0,),
    )
    asyncio.run(runtime.dispatch(MonitorPhase.DECISION))
    assert service.calls == 2


def test_startup_runs_official_delta_update_inside_existing_process() -> None:
    class _Updater:
        def __init__(self):
            self.days = []

        def ensure_through(self, day):
            self.days.append(day)

    class _Store:
        def __init__(self):
            self.recovered = False

        def recover_after_restart(self):
            self.recovered = True

    class _Router:
        def __init__(self):
            self.verified = False
            self.drained = False

        async def verify_route(self):
            self.verified = True

        async def drain(self, limit):
            self.drained = limit == 20

    updater = _Updater()
    store = _Store()
    router = _Router()
    service = SimpleNamespace(
        data_provider=SimpleNamespace(delta_updater=updater)
    )
    config = replace(load_etf_monitor_config(), enabled=True)
    app = _App()
    app.bot_data[integration.RUNTIME_KEY] = integration.EtfMonitorRuntime(
        config=config,
        store=store,
        router=router,
        service=service,
        locks={phase: asyncio.Lock() for phase in integration.JOB_NAMES},
    )
    asyncio.run(integration.initialize_etf_monitor(app))
    assert store.recovered is True
    assert len(updater.days) == 1
    assert updater.days[0] == date.today() - timedelta(days=1)
    assert router.verified is True
    assert router.drained is True


def test_config_rejects_route_bypass_and_legacy_route_key(tmp_path) -> None:
    payload = json.loads(Path("config/etf_monitor.json").read_text(encoding="utf-8-sig"))
    payload["require_get_chat"] = False
    path = tmp_path / "unsafe.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(EtfMonitorConfigError):
        load_etf_monitor_config(path)
    payload["require_get_chat"] = True
    payload["chat_id"] = "-5527055846"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(EtfMonitorConfigError):
        load_etf_monitor_config(path)
    payload["chat_id"] = "-5527055845"
    payload["trade_monitor_chat_id"] = "-1"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(EtfMonitorConfigError):
        load_etf_monitor_config(path)


def test_main_and_bat_keep_single_existing_start_chain() -> None:
    main = Path("main.py").read_text(encoding="utf-8-sig")
    launcher = Path("啟動機器人.bat").read_text(encoding="utf-8-sig")
    runner = Path("啟動機器人_runner.bat").read_text(encoding="utf-8-sig")
    assert main.count("ApplicationBuilder()") == 1
    assert main.count("run_polling(") == 1
    assert main.count("register_etf_monitor(app)") == 1
    assert "啟動機器人_runner.bat" in launcher
    assert "main.py" in runner
    assert "etf_monitor" not in launcher + runner


def test_frozen_registry_root_is_independent_from_bat_workdata_root(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("STOCK_AI_BOT_WORKDATA_ROOT", str(tmp_path / "mutable"))
    monkeypatch.delenv("STOCK_AI_BOT_ETF_FROZEN_MARKET_DATA_ROOT", raising=False)
    config = load_etf_monitor_config()
    root = resolve_frozen_market_data_root(config)
    assert root.as_posix().endswith(
        "stock_data/versions/2026-09-30-v3/market-data"
    )
    assert tmp_path not in root.parents


def _write_registry(root: Path) -> None:
    package = root / "etf/0050/snapshots/test/package"
    daily = package / "data/0050/daily_raw.json"
    actions = package / "events/0050_corporate_action_events.json"
    dividends = package / "events/0050_dividends.json"
    daily.parent.mkdir(parents=True)
    actions.parent.mkdir(parents=True)
    daily.write_text(
        json.dumps(
            {
                "rows": [
                    {"date": "2026-10-20", "open": 49, "high": 51, "low": 48, "close": 50, "volume": 100},
                    {"date": "2026-10-21", "open": 50, "high": 52, "low": 49, "close": 51, "volume": 120},
                ]
            }
        ),
        encoding="utf-8",
    )
    actions.write_text("[]", encoding="utf-8")
    dividends.write_text('{"events": []}', encoding="utf-8")
    artifacts = []
    for artifact in (daily, actions, dividends):
        raw = artifact.read_bytes()
        artifacts.append(
            {
                "relative_path": artifact.relative_to(package).as_posix(),
                "sha256": hashlib.sha256(raw).hexdigest().upper(),
                "bytes": len(raw),
            }
        )
    manifest = package / "artifact_manifest.json"
    manifest.write_text(json.dumps({"artifacts": artifacts}), encoding="utf-8")
    manifest_sha = hashlib.sha256(manifest.read_bytes()).hexdigest().upper()
    registry = root / "etf/registry.json"
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(
        json.dumps(
            {
                "schema_version": "stock-ai-bot-etf-market-data-registry-v1",
                "symbols": {
                    "0050": {
                        "current_snapshot_id": "test",
                        "snapshots": {
                            "test": {
                                "package_path": "etf/0050/snapshots/test/package",
                                "package_manifest_sha256": manifest_sha,
                            }
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )


def test_registry_snapshot_complete_stale_and_provisional_paths(tmp_path) -> None:
    _write_registry(tmp_path)
    config = replace(load_etf_monitor_config(), enabled=True)
    provider = RegistrySnapshotProvider(config, market_data_root=tmp_path)
    complete = provider.load_complete(date(2026, 10, 21))
    assert complete.is_complete and complete.latest_date == date(2026, 10, 21)
    with pytest.raises(EtfMonitorDataError):
        provider.load_complete(date(2026, 10, 22))

    quote = IntradayQuote(
        symbol="0050",
        observed_at=datetime(2026, 10, 22, 13, 55, tzinfo=ZoneInfo("Asia/Taipei")),
        trade_date=date(2026, 10, 22),
        raw_open=Decimal("51"),
        raw_high=Decimal("53"),
        raw_low=Decimal("50"),
        raw_last=Decimal("52"),
        raw_volume=Decimal("130"),
        source="TWSE_MIS_OFFICIAL",
        source_endpoint="official",
        receipt_sha256="c" * 64,
        is_market_closed=True,
    )
    provisional = provider.load_provisional(date(2026, 10, 22), quote)
    assert provisional.is_provisional
    assert provisional.latest_date == date(2026, 10, 22)
    assert provisional.source == "HASH_VERIFIED_REGISTRY_PLUS_TWSE_MIS"


def _write_delta(
    root: Path, market_root: Path, day: date, *, status: str, close: str = "52"
) -> None:
    roc = f"{day.year - 1911}/{day.month:02d}/{day.day:02d}"
    stock_raw = json.dumps({
        "stat": "OK",
        "data": [] if status == "CLOSED" else [[
            roc, "130", "0", "51", "53", "50", close, "0", "1", ""
        ]],
    }).encode()
    holidays = [{"Name": "new year", "Date": f"{day.year - 1911}0101"}]
    if status == "CLOSED" and day.weekday() < 5:
        holidays.append({
            "Name": "closed", "Date": f"{day.year - 1911}{day.month:02d}{day.day:02d}"
        })
    holiday_raw = json.dumps(holidays).encode()
    action_raw = b"[]"
    dividend_raw = b"<table></table>"
    receipts = root / "receipts"
    receipts.mkdir(parents=True, exist_ok=True)

    def evidence(raw, endpoint, request_url, parser):
        digest = hashlib.sha256(raw).hexdigest()
        (receipts / f"{digest}.raw").write_bytes(raw)
        return {
            "source_endpoint": endpoint,
            "request_url": request_url,
            "fetched_at": f"{day.isoformat()}T22:10:00+08:00",
            "receipt_sha256": digest,
            "receipt_relative_path": f"receipts/{digest}.raw",
            "media_type": "application/json",
            "parser": parser,
        }

    calendar_evidence = evidence(
        holiday_raw,
        "https://openapi.twse.com.tw/v1/holidaySchedule/holidaySchedule",
        "https://openapi.twse.com.tw/v1/holidaySchedule/holidaySchedule",
        "TWSE_HOLIDAY_SCHEDULE_V1",
    )
    bar_evidence = evidence(
        stock_raw,
        "https://www.twse.com.tw/exchangeReport/STOCK_DAY",
        f"https://www.twse.com.tw/exchangeReport/STOCK_DAY?response=json&date={day:%Y%m}01&stockNo=0050",
        "TWSE_STOCK_DAY_V1",
    )
    corporate_action_evidence = [
        evidence(
            action_raw,
            "https://openapi.twse.com.tw/v1/exchangeReport/TWT48U_ALL",
            "https://openapi.twse.com.tw/v1/exchangeReport/TWT48U_ALL",
            "TWSE_TWT48U_ALL_V1",
        ),
        evidence(
            dividend_raw,
            "https://www.twse.com.tw/zh/ETFortune/dividendList",
            "https://www.twse.com.tw/zh/ETFortune/dividendList?startDate=2026&endDate=2026&stkNo=0050",
            "TWSE_ETFORTUNE_DIVIDEND_LIST_V1",
        ),
    ]
    payload = {
        "schema_version": "stock-ai-bot-etf-daily-delta-v1",
        "symbol": "0050",
        "trade_date": day.isoformat(),
        "previous_calendar_date": (day - timedelta(days=1)).isoformat(),
        "frozen_snapshot_id": "test",
        "frozen_manifest_sha256": "PLACEHOLDER",
        "market_status": status,
        "calendar_evidence": calendar_evidence,
        "bar": None if status == "CLOSED" else {
            "open": "51", "high": "53", "low": "50", "close": close, "volume": "130"
        },
        "bar_evidence": bar_evidence,
        "corporate_action_evidence": corporate_action_evidence,
        "corporate_actions": [],
    }
    registry = json.loads((market_root / "etf/registry.json").read_text(encoding="utf-8"))
    payload["frozen_manifest_sha256"] = registry["symbols"]["0050"]["snapshots"]["test"]["package_manifest_sha256"]
    payload["content_sha256"] = stable_sha256(payload)
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{day.isoformat()}.json").write_text(json.dumps(payload), encoding="utf-8")


def _registry_identity(market_root: Path) -> tuple[str, str]:
    registry = json.loads(
        (market_root / "etf/registry.json").read_text(encoding="utf-8")
    )
    snapshot = registry["symbols"]["0050"]["snapshots"]["test"]
    return "test", snapshot["package_manifest_sha256"]


def _install_test_delta(source: Path, destination: Path, market_root: Path) -> None:
    snapshot_id, manifest_sha256 = _registry_identity(market_root)
    install_daily_delta_file(
        source / "2026-10-22.json",
        destination_root=destination,
        expected_date=date(2026, 10, 22),
        snapshot_id=snapshot_id,
        manifest_sha256=manifest_sha256,
    )


def _assert_installed_test_delta(destination: Path, market_root: Path) -> None:
    snapshot_id, manifest_sha256 = _registry_identity(market_root)
    payload = load_daily_delta_file(
        destination / "2026-10-22.json",
        expected_date=date(2026, 10, 22),
        snapshot_id=snapshot_id,
        manifest_sha256=manifest_sha256,
    )
    assert payload["bar"]["close"] == "52"


def test_receipt_half_write_interruption_recovers_on_restart(
    tmp_path, monkeypatch
) -> None:
    market_root = tmp_path / "market"
    source = tmp_path / "source"
    destination = tmp_path / "installed"
    _write_registry(market_root)
    _write_delta(source, market_root, date(2026, 10, 22), status="OPEN")
    original = etf_data._write_fully_synced_temp
    interrupted: list[tuple[Path, Path, bytes]] = []

    def interrupt_receipt(target: Path, raw: bytes) -> Path:
        if target.suffix == ".raw" and not interrupted:
            temporary = target.parent / f".{target.name}.fault.tmp"
            half = raw[: max(1, len(raw) // 2)]
            with temporary.open("wb") as handle:
                handle.write(half)
                handle.flush()
                os.fsync(handle.fileno())
            interrupted.append((target, temporary, half))
            raise RuntimeError("simulated receipt power loss")
        return original(target, raw)

    monkeypatch.setattr(etf_data, "_write_fully_synced_temp", interrupt_receipt)
    with pytest.raises(RuntimeError, match="receipt power loss"):
        _install_test_delta(source, destination, market_root)
    target, temporary, half = interrupted[0]
    assert not target.exists()
    assert temporary.parent == target.parent
    assert temporary.read_bytes() == half

    monkeypatch.setattr(etf_data, "_write_fully_synced_temp", original)
    _install_test_delta(source, destination, market_root)
    _assert_installed_test_delta(destination, market_root)


def test_delta_half_write_interruption_recovers_on_restart(
    tmp_path, monkeypatch
) -> None:
    market_root = tmp_path / "market"
    source = tmp_path / "source"
    destination = tmp_path / "installed"
    _write_registry(market_root)
    _write_delta(source, market_root, date(2026, 10, 22), status="OPEN")
    original = etf_data._write_fully_synced_temp
    interrupted: list[tuple[Path, Path, bytes]] = []

    def interrupt_delta(target: Path, raw: bytes) -> Path:
        if target.suffix == ".json" and not interrupted:
            temporary = target.parent / f".{target.name}.fault.tmp"
            half = raw[: len(raw) // 2]
            with temporary.open("wb") as handle:
                handle.write(half)
                handle.flush()
                os.fsync(handle.fileno())
            interrupted.append((target, temporary, half))
            raise RuntimeError("simulated delta power loss")
        return original(target, raw)

    monkeypatch.setattr(etf_data, "_write_fully_synced_temp", interrupt_delta)
    with pytest.raises(RuntimeError, match="delta power loss"):
        _install_test_delta(source, destination, market_root)
    target, temporary, half = interrupted[0]
    assert not target.exists()
    assert temporary.parent == target.parent
    assert temporary.read_bytes() == half

    monkeypatch.setattr(etf_data, "_write_fully_synced_temp", original)
    _install_test_delta(source, destination, market_root)
    _assert_installed_test_delta(destination, market_root)


def test_interrupted_legacy_final_is_quarantined_then_recovered(tmp_path) -> None:
    market_root = tmp_path / "market"
    source = tmp_path / "source"
    destination = tmp_path / "installed"
    _write_registry(market_root)
    _write_delta(source, market_root, date(2026, 10, 22), status="OPEN")
    destination.mkdir()
    final = destination / "2026-10-22.json"
    final.write_bytes(b'{"schema_version": "stock-ai-bot-etf-daily-delta-v1"')

    _install_test_delta(source, destination, market_root)

    _assert_installed_test_delta(destination, market_root)
    quarantined = list(destination.glob(".2026-10-22.json.invalid-*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_bytes().startswith(b'{"schema_version"')


def test_interrupted_legacy_receipt_is_quarantined_then_recovered(tmp_path) -> None:
    market_root = tmp_path / "market"
    source = tmp_path / "source"
    destination = tmp_path / "installed"
    _write_registry(market_root)
    _write_delta(source, market_root, date(2026, 10, 22), status="OPEN")
    payload = json.loads(
        (source / "2026-10-22.json").read_text(encoding="utf-8")
    )
    relative = Path(payload["calendar_evidence"]["receipt_relative_path"])
    source_receipt = source / relative
    final = destination / relative
    final.parent.mkdir(parents=True)
    partial = source_receipt.read_bytes()[:5]
    final.write_bytes(partial)

    _install_test_delta(source, destination, market_root)

    _assert_installed_test_delta(destination, market_root)
    assert final.read_bytes() == source_receipt.read_bytes()
    quarantined = list(final.parent.glob(f".{final.name}.invalid-*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_bytes() == partial


def test_different_valid_delta_is_never_overwritten(tmp_path) -> None:
    market_root = tmp_path / "market"
    first = tmp_path / "first"
    second = tmp_path / "second"
    destination = tmp_path / "installed"
    _write_registry(market_root)
    _write_delta(first, market_root, date(2026, 10, 22), status="OPEN")
    _write_delta(
        second,
        market_root,
        date(2026, 10, 22),
        status="OPEN",
        close="52.5",
    )
    _install_test_delta(first, destination, market_root)
    final = destination / "2026-10-22.json"
    original = final.read_bytes()

    with pytest.raises(EtfMonitorDataError, match="refusing to replace immutable delta"):
        _install_test_delta(second, destination, market_root)

    assert final.read_bytes() == original
    _assert_installed_test_delta(destination, market_root)


def test_daily_delta_requires_every_calendar_day_and_accepts_verified_closed_day(tmp_path) -> None:
    market_root = tmp_path / "market"
    delta_root = tmp_path / "delta"
    _write_registry(market_root)
    config = replace(load_etf_monitor_config(), enabled=True)
    provider = RegistrySnapshotProvider(
        config, market_data_root=market_root, delta_root=delta_root
    )
    _write_delta(delta_root, market_root, date(2026, 10, 23), status="OPEN")
    with pytest.raises(EtfMonitorDataError, match="missing 2026-10-22"):
        provider.load_complete(date(2026, 10, 23))
    _write_delta(delta_root, market_root, date(2026, 10, 22), status="CLOSED")
    complete = provider.load_complete(date(2026, 10, 23))
    assert complete.latest_date == date(2026, 10, 23)
    assert complete.metadata["delta_count"] == 2


def test_daily_delta_rejects_forged_bar_even_with_recomputed_content_hash(tmp_path) -> None:
    market_root = tmp_path / "market"
    delta_root = tmp_path / "delta"
    _write_registry(market_root)
    _write_delta(delta_root, market_root, date(2026, 10, 22), status="OPEN")
    path = delta_root / "2026-10-22.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["bar"]["close"] = "99"
    payload["bar"]["high"] = "99"
    payload.pop("content_sha256")
    payload["content_sha256"] = stable_sha256(payload)
    path.write_text(json.dumps(payload), encoding="utf-8")
    provider = RegistrySnapshotProvider(
        replace(load_etf_monitor_config(), enabled=True),
        market_data_root=market_root,
        delta_root=delta_root,
    )
    with pytest.raises(EtfMonitorDataError, match="differs from raw TWSE STOCK_DAY"):
        provider.load_complete(date(2026, 10, 22))


def test_daily_delta_rejects_tampered_raw_receipt_even_with_valid_looking_sha_field(
    tmp_path,
) -> None:
    market_root = tmp_path / "market"
    delta_root = tmp_path / "delta"
    _write_registry(market_root)
    day = date(2026, 10, 22)
    _write_delta(delta_root, market_root, day, status="OPEN")
    payload = json.loads((delta_root / f"{day}.json").read_text(encoding="utf-8"))
    receipt = delta_root / payload["bar_evidence"]["receipt_relative_path"]
    receipt.write_bytes(b'{"stat":"OK","data":[]}')
    provider = RegistrySnapshotProvider(
        replace(load_etf_monitor_config(), enabled=True),
        market_data_root=market_root,
        delta_root=delta_root,
    )
    with pytest.raises(EtfMonitorDataError, match="raw receipt SHA-256 mismatch"):
        provider.load_complete(day)


def test_daily_delta_requires_both_raw_corporate_action_sources(tmp_path) -> None:
    market_root = tmp_path / "market"
    delta_root = tmp_path / "delta"
    _write_registry(market_root)
    day = date(2026, 10, 22)
    _write_delta(delta_root, market_root, day, status="OPEN")
    path = delta_root / f"{day}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["corporate_action_evidence"] = payload["corporate_action_evidence"][:1]
    payload.pop("content_sha256")
    payload["content_sha256"] = stable_sha256(payload)
    path.write_text(json.dumps(payload), encoding="utf-8")
    provider = RegistrySnapshotProvider(
        replace(load_etf_monitor_config(), enabled=True),
        market_data_root=market_root,
        delta_root=delta_root,
    )
    with pytest.raises(EtfMonitorDataError, match="both official corporate-action receipts"):
        provider.load_complete(day)


def test_automatic_daily_delta_update_captures_validates_and_immutably_installs(
    tmp_path,
) -> None:
    market_root = tmp_path / "market"
    delta_root = tmp_path / "delta"
    _write_registry(market_root)

    class _Response:
        def __init__(self, url, raw):
            self.url = url
            self.content = raw
            self.headers = {"content-type": "application/json"}

        def raise_for_status(self):
            return None

    class _Session:
        def __init__(self):
            self.calls = []

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, params=None, timeout=None):
            self.calls.append((url, params))
            final = url if not params else f"{url}?{urlencode(params)}"
            if "holidaySchedule" in url:
                raw = json.dumps([{"Name": "new year", "Date": "1150101"}]).encode()
            elif "TWT48U_ALL" in url:
                raw = b"[]"
            elif "dividendList" in url:
                raw = b"<table></table>"
            elif "STOCK_DAY" in url:
                raw = json.dumps({
                    "stat": "OK",
                    "data": [[
                        "115/10/22", "200", "0", "52", "54", "51", "53", "0", "1", ""
                    ]],
                }).encode()
            else:
                raise AssertionError(url)
            return _Response(final, raw)

    session = _Session()
    provider = RegistrySnapshotProvider(
        replace(load_etf_monitor_config(), enabled=True),
        market_data_root=market_root,
        delta_root=delta_root,
    )
    updater = OfficialDailyDeltaUpdater(
        provider,
        session_factory=lambda: session,
    )
    result = updater.ensure_through(date(2026, 10, 22))
    calls_after_install = len(session.calls)
    repeated = updater.ensure_through(date(2026, 10, 22))
    snapshot = provider.load_complete(date(2026, 10, 22))
    assert result["status"] == "CAPTURED_VALIDATED_IMMUTABLY_INSTALLED"
    assert result["installed"] == 1
    assert repeated["status"] == "VERIFIED_ALREADY_COMPLETE"
    assert len(session.calls) == calls_after_install
    assert snapshot.latest_date == date(2026, 10, 22)
    assert snapshot.raw_row_for(date(2026, 10, 22))["close"] == "53"
    assert list((delta_root / "receipts").glob("*.raw"))
    assert list((delta_root / "manifests").glob("*.json"))


def test_automatic_delta_update_keeps_production_empty_when_capture_requires_review(
    tmp_path, monkeypatch
) -> None:
    market_root = tmp_path / "market"
    delta_root = tmp_path / "delta"
    _write_registry(market_root)
    provider = RegistrySnapshotProvider(
        replace(load_etf_monitor_config(), enabled=True),
        market_data_root=market_root,
        delta_root=delta_root,
    )

    def requires_review(**kwargs):
        raise EtfMonitorDataError(
            "official corporate action requires reviewed event construction"
        )

    monkeypatch.setattr(
        "stock_ai_bot.monitoring.etf_monitor.data.build_daily_delta_bundle",
        requires_review,
    )
    with pytest.raises(EtfMonitorDataError, match="requires reviewed"):
        OfficialDailyDeltaUpdater(provider).ensure_through(date(2026, 10, 22))
    assert not list(delta_root.glob("*.json"))
    assert not (delta_root / "receipts").exists()
    assert not (delta_root / "manifests").exists()


def test_official_quote_identity_and_close_gate(monkeypatch) -> None:
    class _Response:
        content = b"official-receipt"

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "rtcode": "0000",
                "rtmessage": "OK",
                "queryTime": {"sysDate": "20261021", "sysTime": "13:55:00"},
                "msgArray": [
                    {
                        "c": "0050",
                        "d": "20261021",
                        "o": "50.0",
                        "h": "52.0",
                        "l": "49.0",
                        "z": "51.0",
                        "v": "123456",
                        "t": "13:30:00",
                        "ts": "0",
                        "mt": "000000",
                    }
                ]
            }

    monkeypatch.setattr(
        "stock_ai_bot.monitoring.etf_monitor.data.requests.get",
        lambda *args, **kwargs: _Response(),
    )
    observed = datetime(2026, 10, 21, 13, 55, tzinfo=ZoneInfo("Asia/Taipei"))
    quote = OfficialTwseQuoteProvider()._fetch(observed)
    assert quote.symbol == "0050"
    assert quote.trade_date == date(2026, 10, 21)
    assert quote.raw_last == Decimal("51.0")
    assert quote.is_market_closed is True
    assert quote.market_status == "REGULAR_SESSION_CLOSED_CONFIRMED"
    assert quote.source_timestamp == observed
    assert len(quote.receipt_sha256) == 64


def test_official_quote_rejects_stale_source_clock(monkeypatch) -> None:
    class _Response:
        content = b"stale-official-receipt"

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "rtcode": "0000",
                "rtmessage": "OK",
                "queryTime": {"sysDate": "20261021", "sysTime": "13:40:00"},
                "msgArray": [{
                    "c": "0050", "d": "20261021", "o": "50", "h": "52",
                    "l": "49", "z": "51", "v": "123456", "t": "13:30:00",
                    "ts": "0", "mt": "000000",
                }],
            }

    monkeypatch.setattr(
        "stock_ai_bot.monitoring.etf_monitor.data.requests.get",
        lambda *args, **kwargs: _Response(),
    )
    observed = datetime(2026, 10, 21, 13, 55, tzinfo=ZoneInfo("Asia/Taipei"))
    with pytest.raises(EtfMonitorDataError, match="stale"):
        OfficialTwseQuoteProvider()._fetch(observed)


def test_official_etf_calendar_verifies_holiday_and_preserves_receipt(
    tmp_path, monkeypatch
) -> None:
    raw = json.dumps([
        {"Name": "fixture holiday", "Date": "1151021"},
        {"Name": "new year", "Date": "1150101"},
    ]).encode()

    class _Response:
        content = raw
        url = "https://openapi.twse.com.tw/v1/holidaySchedule/holidaySchedule"

        def raise_for_status(self):
            return None

    monkeypatch.setattr(
        "stock_ai_bot.monitoring.etf_monitor.data.requests.get",
        lambda *args, **kwargs: _Response(),
    )
    calendar = OfficialTwseTradingCalendar(receipt_root=tmp_path / "calendar")
    holiday = calendar.verify(date(2026, 10, 21))
    open_day = calendar.verify(date(2026, 10, 22))
    assert holiday["is_trading_day"] is False
    assert holiday["calendar_reason"] == "OFFICIAL_HOLIDAY"
    assert open_day["is_trading_day"] is True
    assert open_day["receipt_sha256"] == hashlib.sha256(raw).hexdigest()
    assert (tmp_path / "calendar" / f"{open_day['receipt_sha256']}.raw").read_bytes() == raw


def test_official_etf_calendar_network_outage_is_fail_closed(monkeypatch) -> None:
    def fail(*args, **kwargs):
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(
        "stock_ai_bot.monitoring.etf_monitor.data.requests.get",
        fail,
    )
    with pytest.raises(EtfMonitorDataError, match="holiday request failed"):
        OfficialTwseTradingCalendar().verify(date(2026, 10, 22))
