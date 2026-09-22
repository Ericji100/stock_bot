from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
import re
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

import pandas as pd

import stock_ai_bot.scanning.stock_scanner as stock_scanner
import stock_ai_bot.scanning.technical_scanner as technical_scanner
import stock_ai_bot.selection.curated_scan_service as curated_scan_service
import stock_ai_bot.selection.historical_universe_service as historical_universe_service
import stock_ai_bot.selection.laoxiao_scan_service as laoxiao_scan_service
import stock_ai_bot.strategies.chip_strategies as chip_strategies
from stock_ai_bot.data_sources import historical_price_service
from stock_ai_bot.scanning.stock_scanner import scan_tw_market
from stock_ai_bot.strategies.chip_strategies import build_chip_grade_maps, build_market_context


SCHEMA_VERSION = 2
COLLECTOR_VERSION = "dual_ma_daily_selection_v2"
TAIPEI = ZoneInfo("Asia/Taipei")
DEFAULT_OUTPUT_ROOT = Path("data") / "dual_ma" / "selection_manifests"
ROOT_DIR = Path(__file__).resolve().parents[2]
HISTORICAL_CHIP_CACHE_DIR = ROOT_DIR / ".cache" / "historical_scan" / "chip_daily"
HISTORICAL_TDCC_CACHE_DIR = ROOT_DIR / ".cache" / "historical_scan" / "tdcc"

SELECTOR_SOURCE_FILES: dict[str, Path] = {
    "financial": ROOT_DIR / "stock_ai_bot" / "scanning" / "stock_scanner.py",
    "chip": ROOT_DIR / "stock_ai_bot" / "strategies" / "chip_strategies.py",
    "technical": ROOT_DIR / "stock_ai_bot" / "scanning" / "technical_scanner.py",
    "curated": ROOT_DIR / "stock_ai_bot" / "selection" / "curated_scan_service.py",
    "laoxiao": ROOT_DIR / "stock_ai_bot" / "selection" / "laoxiao_scan_service.py",
}

PROGRAM_LABELS: dict[str, str] = {
    "financial": "財報營收選股",
    "chip_1": "60 日法人動態選股",
    "chip_2": "投信認養股",
    "chip_3": "法人持股比例增加",
    "chip_4": "每週大戶持股選股",
    "technical": "技術面選股",
    "curated": "精選選股",
    "laoxiao": "老蕭選股",
}
PROGRAM_ORDER = tuple(PROGRAM_LABELS)

STATUS_SUCCESS = "SUCCESS"
STATUS_FAILED = "FAILED"
STATUS_MISSING_DEPENDENCY = "MISSING_DEPENDENCY"
STATUS_MISSING_SOURCE = "MISSING_SOURCE"


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if pd.isna(value):
            return None
        return value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        items = [_json_safe(item) for item in value]
        return sorted(items, key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True)) if isinstance(value, set) else items
    if hasattr(value, "item"):
        try:
            return _json_safe(value.item())
        except (TypeError, ValueError):
            pass
    return str(value)


def _normalise_code(value: Any) -> str:
    code = str(value or "").strip().split(maxsplit=1)[0]
    return code if re.fullmatch(r"\d{4,6}", code) else ""


def _ordered_codes(values: Iterable[Any]) -> list[str]:
    return sorted({code for value in values if (code := _normalise_code(value))})


def _file_sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _collection_provenance(historical_replay: bool) -> dict[str, Any]:
    stock_list_path = ROOT_DIR / "stock_list.json"
    stock_list: dict[str, Any] = {}
    try:
        loaded = json.loads(stock_list_path.read_text(encoding="utf-8-sig"))
        if isinstance(loaded, dict):
            stock_list = loaded
    except (OSError, json.JSONDecodeError):
        pass
    members = stock_list.get("stocks") or stock_list.get("items") or stock_list.get("data") or []
    universe = {
        "status": "UNVERIFIED_AS_OF_MEMBERSHIP" if historical_replay else "CURRENT_OPERATIONAL",
        "source_file": "stock_list.json",
        "source_sha256": _file_sha256(stock_list_path),
        "source_generated_at": stock_list.get("generated_at"),
        "observed_member_count": len(members) if isinstance(members, list) else None,
        "formal_historical_backtest_ready": not historical_replay,
    }
    return {
        "selector_source_sha256": {
            key: digest
            for key, path in SELECTOR_SOURCE_FILES.items()
            if (digest := _file_sha256(path)) is not None
        },
        "universe": universe,
    }


@dataclass(frozen=True)
class ProgramSelection:
    key: str
    label: str
    status: str
    codes: tuple[str, ...] = ()
    candidate_details: dict[str, dict[str, Any]] = field(default_factory=dict)
    diagnostics: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    @classmethod
    def success(
        cls,
        key: str,
        codes: Iterable[Any],
        *,
        candidate_details: dict[str, dict[str, Any]] | None = None,
        diagnostics: dict[str, Any] | None = None,
    ) -> "ProgramSelection":
        ordered = tuple(_ordered_codes(codes))
        allowed = set(ordered)
        details = {
            code: _json_safe(dict(value))
            for raw_code, value in (candidate_details or {}).items()
            if (code := _normalise_code(raw_code)) in allowed
        }
        return cls(
            key=key,
            label=PROGRAM_LABELS[key],
            status=STATUS_SUCCESS,
            codes=ordered,
            candidate_details=dict(sorted(details.items())),
            diagnostics=_json_safe(diagnostics or {}),
        )

    @classmethod
    def failed(cls, key: str, exc: BaseException) -> "ProgramSelection":
        return cls(
            key=key,
            label=PROGRAM_LABELS[key],
            status=STATUS_FAILED,
            error=f"{type(exc).__name__}: {exc}",
        )

    @classmethod
    def missing_dependency(cls, key: str, dependencies: Iterable[str]) -> "ProgramSelection":
        missing = sorted(set(dependencies))
        return cls(
            key=key,
            label=PROGRAM_LABELS[key],
            status=STATUS_MISSING_DEPENDENCY,
            error=f"missing successful dependencies: {', '.join(missing)}",
            diagnostics={"missing_dependencies": missing},
        )

    @classmethod
    def missing_source(cls, key: str, source: str) -> "ProgramSelection":
        return cls(
            key=key,
            label=PROGRAM_LABELS[key],
            status=STATUS_MISSING_SOURCE,
            error=f"required historical source unavailable: {source}",
            diagnostics={"missing_source": source},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "status": self.status,
            "candidate_count": len(self.codes),
            "codes": list(self.codes),
            "candidate_details": _json_safe(self.candidate_details),
            "diagnostics": _json_safe(self.diagnostics),
            "error": self.error,
        }


@dataclass(frozen=True)
class DailySelectionManifest:
    report_date: date
    historical_replay: bool
    programs: dict[str, ProgramSelection]
    generated_at: str
    scan_settings: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION
    collector_version: str = COLLECTOR_VERSION

    @property
    def overall_status(self) -> str:
        statuses = [self.programs[key].status for key in PROGRAM_ORDER]
        if all(status == STATUS_SUCCESS for status in statuses):
            return "COMPLETE"
        if any(status == STATUS_SUCCESS for status in statuses):
            return "DEGRADED"
        return "FAILED"

    @property
    def union_codes(self) -> list[str]:
        return sorted({code for program in self.programs.values() for code in program.codes})

    def candidate_memberships(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for code in self.union_codes:
            memberships = [key for key in PROGRAM_ORDER if code in self.programs[key].codes]
            rows.append(
                {
                    "code": code,
                    "programs": memberships,
                    "program_labels": [PROGRAM_LABELS[key] for key in memberships],
                }
            )
        return rows

    def freeze_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "collector_version": self.collector_version,
            "report_date": self.report_date.isoformat(),
            "historical_replay": self.historical_replay,
            "scan_settings": _json_safe(self.scan_settings),
            "provenance": _json_safe(self.provenance),
            "overall_status": self.overall_status,
            "program_order": list(PROGRAM_ORDER),
            "programs": {key: self.programs[key].to_dict() for key in PROGRAM_ORDER},
            "union_count": len(self.union_codes),
            "union_codes": self.union_codes,
            "candidates": self.candidate_memberships(),
            "universe_notice": (
                "historical membership supplied by a verified immutable daily universe snapshot"
                if self.historical_replay
                and self.provenance.get("universe", {}).get("status") == "VERIFIED_AS_OF_MEMBERSHIP"
                else "historical membership must be supplied and verified separately"
                if self.historical_replay
                else "uses the operational universe resolved by the existing selectors"
            ),
        }

    @property
    def content_sha256(self) -> str:
        encoded = json.dumps(
            self.freeze_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @property
    def manifest_id(self) -> str:
        return f"dual-ma-selection@{self.report_date.isoformat()}#{self.content_sha256[:16]}"

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.freeze_payload(),
            "manifest_id": self.manifest_id,
            "content_sha256": self.content_sha256,
            "generated_at": self.generated_at,
        }


def _candidate_meta(value: Any) -> dict[str, Any]:
    return {
        key: _json_safe(getattr(value, key, None))
        for key in ("symbol", "market", "name", "industry")
        if getattr(value, key, None) not in (None, "")
    }


def _collect_financial(scan_settings: dict[str, Any], report_date: date, historical_replay: bool) -> tuple[Any, ProgramSelection]:
    result = scan_tw_market(
        False,
        None,
        scan_settings,
        report_date,
        historical_replay=historical_replay,
    )
    details: dict[str, dict[str, Any]] = {}
    for candidate in result.candidates:
        details[candidate.code] = {
            **_candidate_meta(candidate),
            "revenue_group": candidate.revenue_group,
            "gross_margin_rating": candidate.gross_margin_rating,
        }
    diagnostics = {
        "total_symbols": getattr(result, "total_symbols", None),
        "hard_filter_passed": getattr(result, "hard_filter_passed", None),
    }
    return result, ProgramSelection.success("financial", details, candidate_details=details, diagnostics=diagnostics)


def _dataframe_metadata(frame: pd.DataFrame) -> dict[str, dict[str, Any]]:
    if frame is None or frame.empty or "code" not in frame.columns:
        return {}
    result: dict[str, dict[str, Any]] = {}
    for row in frame.to_dict(orient="records"):
        code = _normalise_code(row.get("code"))
        if not code:
            continue
        result[code] = {
            key: _json_safe(row.get(key))
            for key in ("symbol", "market", "name", "industry")
            if row.get(key) not in (None, "") and not pd.isna(row.get(key))
        }
    return result


def _collect_chip(
    scan_settings: dict[str, Any], report_date: date, historical_replay: bool
) -> tuple[Any, dict[str, ProgramSelection]]:
    trading_calendar: list[date] | None = None
    if historical_replay:
        index_history = historical_price_service.load_cached_history("^TWII", report_date)
        if not index_history.empty:
            trading_calendar = sorted(
                set(index_history["date"].dt.date.tolist())
            )
    context = build_market_context(
        False,
        report_date,
        include_daily_data=True,
        include_foreign_ratio=not historical_replay,
        scan_settings=scan_settings,
        trading_calendar=trading_calendar,
        cached_only=historical_replay,
        historical_replay=historical_replay,
    )
    grade_maps = build_chip_grade_maps(context, ["chip_1", "chip_2", "chip_3", "chip_4"])
    metadata = _dataframe_metadata(context.candidates)
    programs: dict[str, ProgramSelection] = {}
    has_candidates = context.candidates is not None and not context.candidates.empty
    has_daily_attribute = hasattr(context, "daily_data")
    daily_data = getattr(context, "daily_data", pd.DataFrame())
    daily_dates = pd.Series(dtype="datetime64[ns]")
    if has_daily_attribute and daily_data is not None and not daily_data.empty and "date" in daily_data.columns:
        daily_dates = pd.to_datetime(daily_data["date"], errors="coerce").dropna().drop_duplicates()
    required_daily_dates = int(chip_strategies.TARGET_DAILY_TRADING_DAYS)
    daily_latest = daily_dates.max().date() if not daily_dates.empty else None
    daily_source_missing = has_daily_attribute and has_candidates and (
        len(daily_dates) < required_daily_dates or daily_latest != report_date
    )
    has_weekly_attribute = hasattr(context, "weekly_data")
    weekly_data = getattr(context, "weekly_data", pd.DataFrame())
    weekly_dates = pd.Series(dtype="datetime64[ns]")
    if has_weekly_attribute and weekly_data is not None and not weekly_data.empty and "snapshot_date" in weekly_data.columns:
        weekly_dates = pd.to_datetime(weekly_data["snapshot_date"], errors="coerce").dropna().drop_duplicates()
    weekly_source_missing = has_weekly_attribute and has_candidates and len(weekly_dates) < 3
    for key in ("chip_1", "chip_2", "chip_3", "chip_4"):
        if key != "chip_4" and daily_source_missing:
            programs[key] = ProgramSelection.missing_source(key, "institutional_daily_history")
            continue
        if key == "chip_4" and weekly_source_missing:
            programs[key] = ProgramSelection.missing_source(key, "tdcc_weekly_history")
            continue
        grades = grade_maps.get(key, {})
        details = {code: {**metadata.get(code, {}), "grade": grade} for code, grade in grades.items()}
        programs[key] = ProgramSelection.success(
            key,
            grades,
            candidate_details=details,
            diagnostics={
                "total_symbols": getattr(context, "total_symbols", None),
                "latest_trading_date": getattr(context, "latest_trading_date", None),
            },
        )
    return context, programs


def _add_technical_detail(
    details: dict[str, dict[str, Any]],
    code_value: Any,
    signal: dict[str, Any],
) -> None:
    code = _normalise_code(code_value)
    if not code:
        return
    entry = details.setdefault(code, {"signals": []})
    name = signal.get("stock_name")
    industry = signal.get("industry")
    if name:
        entry.setdefault("name", str(name))
    if industry:
        entry.setdefault("industry", str(industry))
    entry["signals"].append(_json_safe(signal))


def _compact_signal(signal: dict[str, Any], *, category: str, strategy_code: str | None = None) -> dict[str, Any]:
    keys = (
        "signal_date",
        "technical_signal_type",
        "sub_signal_type",
        "primary_group",
        "matched_groups",
        "matched_target_mas",
        "triggers",
        "aligned_long_mas",
        "long_position",
        "recent_golden_cross_days",
        "close",
        "notes",
        "industry",
    )
    compact = {key: _json_safe(signal.get(key)) for key in keys if signal.get(key) not in (None, "", [], {})}
    compact["category"] = category
    if strategy_code:
        compact["strategy_code"] = strategy_code
    return compact


def _technical_details(result: Any) -> dict[str, dict[str, Any]]:
    details: dict[str, dict[str, Any]] = {}
    for direction, groups in (("bullish", result.bullish), ("bearish", result.bearish)):
        for signal_name, industries in groups.items():
            for industry, displays in industries.items():
                for display in displays:
                    _add_technical_detail(
                        details,
                        display,
                        {
                            "category": "primitive",
                            "direction": direction,
                            "signal": signal_name,
                            "industry": industry,
                        },
                    )
    for strategy_code, signals in result.strategy_signals.items():
        for signal in signals:
            _add_technical_detail(
                details,
                signal.get("stock_id") or signal.get("code"),
                _compact_signal(signal, category="strategy", strategy_code=strategy_code),
            )
    for signal in result.dual_ma_signals:
        _add_technical_detail(
            details,
            signal.get("stock_id") or signal.get("code"),
            _compact_signal(signal, category="dual_ma"),
        )
    for signal in result.kd_ma_signals:
        _add_technical_detail(
            details,
            signal.get("stock_id") or signal.get("code"),
            _compact_signal(signal, category="kd_ma"),
        )
    return details


def _collect_technical(scan_settings: dict[str, Any], report_date: date, historical_replay: bool) -> tuple[Any, ProgramSelection]:
    result = technical_scanner.run_technical_scan(
        scan_settings,
        report_date,
        historical_replay=historical_replay,
    )
    codes = technical_scanner.collect_technical_selected_codes(result)
    return result, ProgramSelection.success(
        "technical",
        codes,
        candidate_details=_technical_details(result),
        diagnostics={
            "total_symbols": result.total_symbols,
            "hard_filter_passed": result.hard_filter_passed,
            "matched_symbols": result.matched_symbols,
            "sources": sorted(result.sources),
        },
    )


def _collect_curated(
    scan_settings: dict[str, Any],
    report_date: date,
    historical_replay: bool,
    financial_result: Any,
    chip_context: Any,
    technical_result: Any,
) -> ProgramSelection:
    result = curated_scan_service.build_curated_scan_result(
        scan_settings,
        report_date,
        financial_report=financial_result,
        chip_context=chip_context,
        technical_result=technical_result,
        include_scoring=False,
        historical_replay=historical_replay,
    )
    details = {}
    for code in result.selected_codes:
        info = result.stock_info.get(code, {})
        details[code] = {
            key: _json_safe(info.get(key))
            for key in ("symbol", "market", "name", "industry", "financial_group", "gross_margin_rating")
            if info.get(key) not in (None, "")
        }
        details[code].update(
            {
                "hits": _json_safe(result.hits.get(code, [])),
                "selected_signals": sorted(
                    signal for signal, codes in result.selected_by_signal.items() if code in codes
                ),
            }
        )
    return ProgramSelection.success(
        "curated",
        result.selected_codes,
        candidate_details=details,
        diagnostics={"selected_signal_groups": len(result.selected_by_signal)},
    )


def _collect_laoxiao(scan_settings: dict[str, Any], report_date: date, historical_replay: bool) -> ProgramSelection:
    result = laoxiao_scan_service.build_laoxiao_scan_result(
        scan_settings,
        report_date,
        historical_replay=historical_replay,
    )
    by_code = {candidate.code: candidate for candidate in result.candidates}
    details = {
        code: {
            **_candidate_meta(by_code[code]),
            "setup_type": by_code[code].setup_type,
            "total_score": by_code[code].total_score,
            "reasons": _json_safe(by_code[code].reasons),
            "risks": _json_safe(by_code[code].risks),
        }
        for code in result.selected_codes
        if code in by_code
    }
    return ProgramSelection.success(
        "laoxiao",
        result.selected_codes,
        candidate_details=details,
        diagnostics=result.diagnostics,
    )


def collect_daily_selection(
    report_date: date,
    scan_settings: dict[str, Any] | None = None,
    *,
    historical_replay: bool = False,
    progress: Callable[[str], None] | None = None,
    now: datetime | None = None,
) -> DailySelectionManifest:
    """Run existing selectors without changing their Telegram or scheduling paths."""
    settings = dict(scan_settings or {})
    emit = progress or (lambda _message: None)
    programs: dict[str, ProgramSelection] = {}
    financial_result = None
    chip_context = None
    technical_result = None

    emit(f"{report_date.isoformat()} 財報營收選股")
    try:
        financial_result, programs["financial"] = _collect_financial(settings, report_date, historical_replay)
    except Exception as exc:
        programs["financial"] = ProgramSelection.failed("financial", exc)

    emit(f"{report_date.isoformat()} 四組籌碼選股")
    try:
        chip_context, chip_programs = _collect_chip(settings, report_date, historical_replay)
        programs.update(chip_programs)
    except Exception as exc:
        for key in ("chip_1", "chip_2", "chip_3", "chip_4"):
            programs[key] = ProgramSelection.failed(key, exc)

    emit(f"{report_date.isoformat()} 技術面選股")
    try:
        technical_result, programs["technical"] = _collect_technical(settings, report_date, historical_replay)
    except Exception as exc:
        programs["technical"] = ProgramSelection.failed("technical", exc)

    chip_complete = chip_context is not None and all(
        programs[key].status == STATUS_SUCCESS
        for key in ("chip_1", "chip_2", "chip_3", "chip_4")
    )
    dependencies = {
        "financial": financial_result,
        "chip": chip_context if chip_complete else None,
        "technical": technical_result,
    }
    missing_dependencies = [key for key, value in dependencies.items() if value is None]
    emit(f"{report_date.isoformat()} 精選選股")
    if missing_dependencies:
        programs["curated"] = ProgramSelection.missing_dependency("curated", missing_dependencies)
    else:
        try:
            programs["curated"] = _collect_curated(
                settings,
                report_date,
                historical_replay,
                financial_result,
                chip_context,
                technical_result,
            )
        except Exception as exc:
            programs["curated"] = ProgramSelection.failed("curated", exc)

    emit(f"{report_date.isoformat()} 老蕭選股")
    try:
        programs["laoxiao"] = _collect_laoxiao(settings, report_date, historical_replay)
    except Exception as exc:
        programs["laoxiao"] = ProgramSelection.failed("laoxiao", exc)

    generated_at = now or datetime.now(TAIPEI)
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=TAIPEI)
    return DailySelectionManifest(
        report_date=report_date,
        historical_replay=historical_replay,
        programs={key: programs[key] for key in PROGRAM_ORDER},
        generated_at=generated_at.isoformat(),
        scan_settings=_json_safe(settings),
        provenance=_collection_provenance(historical_replay),
    )


@contextmanager
def _isolated_historical_universe_override(
    entries: Iterable[stock_scanner.StockUniverseEntry],
):
    """Temporarily inject an explicit universe into existing selector modules.

    This is process-local monkeypatching and is therefore intended only for a
    dedicated, single-purpose historical batch process.  All bindings are
    restored even when a selector raises.
    """
    frozen_entries = tuple(entries)

    def load_explicit_universe(*_args: Any, **_kwargs: Any) -> list[stock_scanner.StockUniverseEntry]:
        return list(frozen_entries)

    targets = (
        (stock_scanner, "load_stock_universe"),
        (technical_scanner, "load_stock_universe"),
        (chip_strategies, "load_stock_universe"),
        (laoxiao_scan_service, "load_stock_universe"),
    )
    originals = [(module, name, getattr(module, name)) for module, name in targets]
    original_chip_cache_dir = chip_strategies.DAILY_CHIP_CACHE_DIR
    original_tdcc_cache_dir = chip_strategies.TDCC_CACHE_DIR
    try:
        for module, name, _original in originals:
            setattr(module, name, load_explicit_universe)
        chip_strategies.DAILY_CHIP_CACHE_DIR = HISTORICAL_CHIP_CACHE_DIR
        chip_strategies.TDCC_CACHE_DIR = HISTORICAL_TDCC_CACHE_DIR
        yield
    finally:
        for module, name, original in originals:
            setattr(module, name, original)
        chip_strategies.DAILY_CHIP_CACHE_DIR = original_chip_cache_dir
        chip_strategies.TDCC_CACHE_DIR = original_tdcc_cache_dir


def collect_historical_daily_selection(
    report_date: date,
    universe: historical_universe_service.HistoricalUniverse,
    scan_settings: dict[str, Any] | None = None,
    *,
    universe_snapshot_root: str | Path = historical_universe_service.DEFAULT_SNAPSHOT_ROOT,
    progress: Callable[[str], None] | None = None,
    now: datetime | None = None,
) -> tuple[DailySelectionManifest, Path]:
    """Run all existing selectors against one verified point-in-time universe.

    Run this entry point in a standalone historical-batch process, never inside
    the live bot process.
    """
    entries = universe.members_as_of(report_date, strict=True)
    snapshot_path = historical_universe_service.freeze_daily_universe_snapshot(
        universe,
        report_date,
        universe_snapshot_root,
    )
    snapshot_payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    with _isolated_historical_universe_override(entries):
        collected = collect_daily_selection(
            report_date,
            scan_settings,
            historical_replay=True,
            progress=progress,
            now=now,
        )
    provenance = dict(collected.provenance)
    provenance["universe"] = {
        "status": "VERIFIED_AS_OF_MEMBERSHIP",
        "formal_historical_backtest_ready": True,
        "as_of_date": report_date.isoformat(),
        "member_count": len(entries),
        "source_id": universe.source_id,
        "snapshot_id": snapshot_payload["snapshot_id"],
        "snapshot_content_sha256": snapshot_payload["content_sha256"],
        "snapshot_path": str(snapshot_path.resolve()),
    }
    return (
        DailySelectionManifest(
            report_date=collected.report_date,
            historical_replay=True,
            programs=collected.programs,
            generated_at=collected.generated_at,
            scan_settings=collected.scan_settings,
            provenance=provenance,
            schema_version=collected.schema_version,
            collector_version=collected.collector_version,
        ),
        snapshot_path,
    )


def freeze_daily_selection_manifest(
    manifest: DailySelectionManifest,
    output_root: str | Path = DEFAULT_OUTPUT_ROOT,
) -> Path:
    """Write one content-addressed snapshot; existing snapshots are never overwritten."""
    root = Path(output_root)
    target_dir = root / f"{manifest.report_date.year:04d}" / manifest.report_date.isoformat()
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{manifest.manifest_id}.json"
    if target.exists():
        existing = json.loads(target.read_text(encoding="utf-8"))
        if existing.get("content_sha256") != manifest.content_sha256:
            raise ValueError(f"manifest digest mismatch at existing path: {target}")
        return target

    payload = json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temporary = target.with_suffix(f".tmp-{os.getpid()}")
    temporary.write_text(payload, encoding="utf-8")
    os.replace(temporary, target)
    return target
