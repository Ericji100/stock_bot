from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from stock_ai_bot.monitoring.etf_monitor import integration
from stock_ai_bot.monitoring.etf_monitor.config import (
    EtfMonitorConfigError,
    load_etf_monitor_config,
)
from stock_ai_bot.monitoring.etf_monitor.data import (
    OfficialTwseQuoteProvider,
    RegistrySnapshotProvider,
    resolve_frozen_market_data_root,
)
from stock_ai_bot.monitoring.etf_monitor.errors import EtfMonitorDataError
from stock_ai_bot.monitoring.etf_monitor.models import IntradayQuote
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
    evidence = {
        "source_endpoint": "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL",
        "fetched_at": f"{day.isoformat()}T22:10:00+08:00",
        "receipt_sha256": "d" * 64,
    }
    payload = {
        "schema_version": "stock-ai-bot-etf-daily-delta-v1",
        "symbol": "0050",
        "trade_date": day.isoformat(),
        "previous_calendar_date": (day - timedelta(days=1)).isoformat(),
        "frozen_snapshot_id": "test",
        "frozen_manifest_sha256": "PLACEHOLDER",
        "market_status": status,
        "calendar_evidence": evidence,
        "bar": None if status == "CLOSED" else {
            "open": "51", "high": "53", "low": "50", "close": close, "volume": "130"
        },
        "bar_evidence": None if status == "CLOSED" else evidence,
        "corporate_actions": [],
    }
    registry = json.loads((market_root / "etf/registry.json").read_text(encoding="utf-8"))
    payload["frozen_manifest_sha256"] = registry["symbols"]["0050"]["snapshots"]["test"]["package_manifest_sha256"]
    payload["content_sha256"] = stable_sha256(payload)
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{day.isoformat()}.json").write_text(json.dumps(payload), encoding="utf-8")


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


def test_official_quote_identity_and_close_gate(monkeypatch) -> None:
    class _Response:
        content = b"official-receipt"

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "msgArray": [
                    {
                        "c": "0050",
                        "d": "20261021",
                        "o": "50.0",
                        "h": "52.0",
                        "l": "49.0",
                        "z": "51.0",
                        "v": "123456",
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
    assert len(quote.receipt_sha256) == 64
