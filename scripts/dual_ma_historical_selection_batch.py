"""Freeze daily dual-MA selection manifests over a historical date range.

This is an isolated sidecar entry point.  It does not register bot commands,
change the scheduler, or write to the live monitor list.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from zoneinfo import ZoneInfo


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from stock_ai_bot.data_sources import historical_price_service
from stock_ai_bot.selection import dual_ma_selection_collection_service as collection_service
from stock_ai_bot.selection import historical_universe_service


TAIPEI = ZoneInfo("Asia/Taipei")
DEFAULT_RUN_ROOT = Path("data") / "dual_ma" / "selection_runs"
DEFAULT_SOURCE_PATH = (
    Path("data")
    / "dual_ma"
    / "universe_sources"
    / "2026"
    / "2026-09-21"
    / "dual-ma-universe-sources#9af0cbe1b8d22529.json"
)
DEFAULT_EVIDENCE_PATH = DEFAULT_SOURCE_PATH.with_name(
    "dual-ma-membership-evidence#0a6660ba88b5a499.json"
)
DEFAULT_SUPPLEMENT_PATH = DEFAULT_SOURCE_PATH.with_name(
    "dual-ma-listing-supplement#dc0c4bd69c4fb106.json"
)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".tmp-{os.getpid()}")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def load_scan_settings(config_path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(config_path).read_text(encoding="utf-8-sig"))
    settings = payload.get("scan_settings") or {}
    if not isinstance(settings, dict):
        raise ValueError("config scan_settings must be a JSON object")
    return dict(settings)


def load_frozen_universe(
    source_path: str | Path,
    evidence_path: str | Path,
    supplement_path: str | Path,
) -> historical_universe_service.HistoricalUniverse:
    sources = historical_universe_service.load_official_universe_sources(source_path)
    evidence = historical_universe_service.load_official_membership_evidence(evidence_path)
    supplement = historical_universe_service.load_official_listing_supplement(supplement_path)
    universe = historical_universe_service.build_historical_universe(
        sources,
        first_trade_dates=evidence,
        supplemental_listing_dates=supplement,
    )
    if not universe.formal_backtest_ready:
        raise historical_universe_service.IncompleteHistoricalUniverseError(
            f"historical universe has {len(universe.unresolved)} unresolved memberships"
        )
    return universe


def historical_trading_dates(start_date: date, end_date: date) -> list[date]:
    if end_date < start_date:
        raise ValueError("end_date must not precede start_date")
    frame = historical_price_service.load_cached_history("^TWII", end_date)
    if frame.empty or "date" not in frame.columns:
        raise ValueError("frozen ^TWII history is unavailable")
    dates = sorted(
        {
            value.date()
            for value in frame["date"]
            if start_date <= value.date() <= end_date
        }
    )
    if not dates:
        raise ValueError("no frozen ^TWII trading dates in requested range")
    return dates


def _manifest_content_digest(payload: Mapping[str, Any]) -> str:
    freeze_payload = {
        key: value
        for key, value in payload.items()
        if key not in {"manifest_id", "content_sha256", "generated_at"}
    }
    return _sha256(freeze_payload)


def find_completed_manifest(
    report_date: date,
    output_root: str | Path,
    *,
    universe_source_id: str,
    scan_settings: Mapping[str, Any],
    selector_source_sha256: Mapping[str, str],
) -> Path | None:
    directory = Path(output_root) / f"{report_date.year:04d}" / report_date.isoformat()
    for path in sorted(directory.glob(f"dual-ma-selection@{report_date.isoformat()}#*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        digest = _manifest_content_digest(payload)
        expected_id = f"dual-ma-selection@{report_date.isoformat()}#{digest[:16]}"
        universe = (payload.get("provenance") or {}).get("universe") or {}
        fingerprints = (payload.get("provenance") or {}).get("selector_source_sha256") or {}
        programs = payload.get("programs") or {}
        if (
            payload.get("content_sha256") == digest
            and payload.get("manifest_id") == expected_id
            and path.stem == expected_id
            and payload.get("report_date") == report_date.isoformat()
            and payload.get("historical_replay") is True
            and payload.get("scan_settings") == dict(scan_settings)
            and universe.get("status") == "VERIFIED_AS_OF_MEMBERSHIP"
            and universe.get("source_id") == universe_source_id
            and fingerprints == dict(selector_source_sha256)
            and set(programs) == set(collection_service.PROGRAM_ORDER)
        ):
            return path
    return None


def selector_fingerprints() -> dict[str, str]:
    result: dict[str, str] = {}
    for key, path in collection_service.SELECTOR_SOURCE_FILES.items():
        result[key] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


@contextmanager
def memoized_daily_inputs():
    """Reuse identical immutable price/revenue inputs within one replay date."""
    stock_scanner = collection_service.stock_scanner
    chip_strategies = collection_service.chip_strategies
    technical_scanner = collection_service.technical_scanner
    laoxiao_scan_service = collection_service.laoxiao_scan_service
    original_price = stock_scanner.load_price_metrics
    original_revenue = stock_scanner.load_recent_revenue_history
    originals = (
        (stock_scanner, "load_price_metrics", stock_scanner.load_price_metrics),
        (stock_scanner, "load_recent_revenue_history", stock_scanner.load_recent_revenue_history),
        (chip_strategies, "load_price_metrics", chip_strategies.load_price_metrics),
        (chip_strategies, "load_recent_revenue_history", chip_strategies.load_recent_revenue_history),
        (technical_scanner, "load_price_metrics", technical_scanner.load_price_metrics),
        (technical_scanner, "load_recent_revenue_history", technical_scanner.load_recent_revenue_history),
        (laoxiao_scan_service, "load_recent_revenue_history", laoxiao_scan_service.load_recent_revenue_history),
    )
    cache: dict[tuple[Any, ...], Any] = {}

    def cached_prices(universe: list[Any], *args: Any, **kwargs: Any) -> Any:
        as_of = kwargs.get("as_of_date", args[3] if len(args) >= 4 else None)
        if as_of is None:
            return original_price(universe, *args, **kwargs)
        key = ("price", as_of)
        if key not in cache:
            cache[key] = original_price(universe, *args, **kwargs)
        return cache[key]

    def cached_revenue(universe: list[Any], *args: Any, **kwargs: Any) -> Any:
        months = kwargs.get("months_to_fetch", args[0] if args else 24)
        as_of = kwargs.get("as_of_date", args[1] if len(args) >= 2 else None)
        if as_of is None:
            return original_revenue(universe, *args, **kwargs)
        key = ("revenue", int(months), as_of)
        if key not in cache:
            cache[key] = original_revenue(universe, *args, **kwargs)
        return cache[key]

    try:
        stock_scanner.load_price_metrics = cached_prices
        stock_scanner.load_recent_revenue_history = cached_revenue
        chip_strategies.load_price_metrics = cached_prices
        chip_strategies.load_recent_revenue_history = cached_revenue
        technical_scanner.load_price_metrics = cached_prices
        technical_scanner.load_recent_revenue_history = cached_revenue
        laoxiao_scan_service.load_recent_revenue_history = cached_revenue
        yield cache
    finally:
        for module, name, original in originals:
            setattr(module, name, original)


@dataclass(frozen=True)
class BatchResult:
    run_path: Path
    status: str
    total_dates: int
    completed: int
    skipped: int
    failed: int


def run_batch(
    *,
    start_date: date,
    end_date: date,
    universe: historical_universe_service.HistoricalUniverse,
    scan_settings: Mapping[str, Any],
    output_root: str | Path = collection_service.DEFAULT_OUTPUT_ROOT,
    snapshot_root: str | Path = historical_universe_service.DEFAULT_SNAPSHOT_ROOT,
    run_root: str | Path = DEFAULT_RUN_ROOT,
    resume: bool = True,
    max_days: int | None = None,
    fail_fast: bool = False,
    stop_file: str | Path | None = None,
    progress: Callable[[str], None] | None = print,
    dates: Iterable[date] | None = None,
) -> BatchResult:
    emit = progress or (lambda _message: None)
    all_dates = list(dates) if dates is not None else historical_trading_dates(start_date, end_date)
    requested_dates = [value for value in all_dates if start_date <= value <= end_date]
    if max_days is not None:
        if max_days < 1:
            raise ValueError("max_days must be positive")
        requested_dates = requested_dates[:max_days]
    if not requested_dates:
        raise ValueError("no trading dates selected")

    baseline_fingerprints = selector_fingerprints()
    started_at = datetime.now(TAIPEI)
    run_id = (
        f"dual-ma-selection-batch@{requested_dates[0].isoformat()}_"
        f"{requested_dates[-1].isoformat()}#{started_at.strftime('%Y%m%dT%H%M%S%f%z')}"
    )
    run_path = Path(run_root) / f"{run_id}.json"
    records: list[dict[str, Any]] = []

    def checkpoint(status: str) -> None:
        _atomic_write_json(
            run_path,
            {
                "schema_version": 1,
                "run_id": run_id,
                "status": status,
                "started_at": started_at.isoformat(),
                "updated_at": datetime.now(TAIPEI).isoformat(),
                "requested_start": start_date.isoformat(),
                "requested_end": end_date.isoformat(),
                "selected_date_count": len(requested_dates),
                "universe_source_id": universe.source_id,
                "scan_settings": dict(scan_settings),
                "selector_source_sha256": baseline_fingerprints,
                "records": records,
            },
        )

    checkpoint("RUNNING")
    failures = 0
    paused = False
    for index, report_date in enumerate(requested_dates, start=1):
        if stop_file is not None and Path(stop_file).exists():
            paused = True
            checkpoint("PAUSED_STOP_REQUESTED")
            emit(f"batch paused before {report_date}: stop file present")
            break
        if selector_fingerprints() != baseline_fingerprints:
            checkpoint("ABORTED_SELECTOR_VERSION_CHANGED")
            raise RuntimeError("selector source files changed during historical batch")

        existing = (
            find_completed_manifest(
                report_date,
                output_root,
                universe_source_id=universe.source_id,
                scan_settings=scan_settings,
                selector_source_sha256=baseline_fingerprints,
            )
            if resume
            else None
        )
        if existing is not None:
            records.append(
                {
                    "report_date": report_date.isoformat(),
                    "status": "SKIPPED_EXISTING",
                    "manifest_path": str(existing.resolve()),
                }
            )
            checkpoint("RUNNING")
            emit(f"[{index}/{len(requested_dates)}] {report_date} skipped (verified existing manifest)")
            continue

        started = time.monotonic()
        emit(f"[{index}/{len(requested_dates)}] {report_date} collecting")
        day_log_path = run_path.parent / "logs" / run_id / f"{report_date.isoformat()}.log"
        day_log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with day_log_path.open("w", encoding="utf-8") as selector_log:
                with redirect_stdout(selector_log), redirect_stderr(selector_log):
                    with memoized_daily_inputs():
                        manifest, snapshot_path = collection_service.collect_historical_daily_selection(
                            report_date,
                            universe,
                            dict(scan_settings),
                            universe_snapshot_root=snapshot_root,
                            progress=None,
                        )
            if manifest.provenance.get("selector_source_sha256") != baseline_fingerprints:
                raise RuntimeError("selector source fingerprint mismatch")
            manifest_path = collection_service.freeze_daily_selection_manifest(manifest, output_root)
            records.append(
                {
                    "report_date": report_date.isoformat(),
                    "status": "COMPLETED",
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "overall_status": manifest.overall_status,
                    "union_count": len(manifest.union_codes),
                    "program_statuses": {
                        key: manifest.programs[key].status for key in collection_service.PROGRAM_ORDER
                    },
                    "manifest_path": str(manifest_path.resolve()),
                    "snapshot_path": str(snapshot_path.resolve()),
                    "selector_log_path": str(day_log_path.resolve()),
                    "content_sha256": manifest.content_sha256,
                }
            )
            emit(
                f"[{index}/{len(requested_dates)}] {report_date} frozen "
                f"({manifest.overall_status}, {len(manifest.union_codes)} codes)"
            )
        except Exception as exc:
            failures += 1
            records.append(
                {
                    "report_date": report_date.isoformat(),
                    "status": "FAILED",
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "error": f"{type(exc).__name__}: {exc}",
                    "selector_log_path": str(day_log_path.resolve()),
                }
            )
            checkpoint("RUNNING_WITH_FAILURES")
            emit(f"[{index}/{len(requested_dates)}] {report_date} failed: {type(exc).__name__}: {exc}")
            if fail_fast:
                checkpoint("FAILED")
                raise
            continue
        checkpoint("RUNNING_WITH_FAILURES" if failures else "RUNNING")

    final_status = (
        "PAUSED_STOP_REQUESTED"
        if paused
        else "COMPLETE_WITH_FAILURES"
        if failures
        else "COMPLETE"
    )
    checkpoint(final_status)
    return BatchResult(
        run_path=run_path,
        status=final_status,
        total_dates=len(requested_dates),
        completed=sum(item["status"] == "COMPLETED" for item in records),
        skipped=sum(item["status"] == "SKIPPED_EXISTING" for item in records),
        failed=failures,
    )


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=_parse_date, default=date(2022, 1, 1))
    parser.add_argument("--end", type=_parse_date, default=date(2025, 12, 31))
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE_PATH)
    parser.add_argument("--evidence", type=Path, default=DEFAULT_EVIDENCE_PATH)
    parser.add_argument("--supplement", type=Path, default=DEFAULT_SUPPLEMENT_PATH)
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    parser.add_argument("--output-root", type=Path, default=collection_service.DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--snapshot-root", type=Path, default=historical_universe_service.DEFAULT_SNAPSHOT_ROOT)
    parser.add_argument("--run-root", type=Path, default=DEFAULT_RUN_ROOT)
    parser.add_argument("--max-days", type=int)
    parser.add_argument(
        "--stop-file",
        type=Path,
        help="pause safely between trading dates when this file exists",
    )
    parser.add_argument("--no-resume", action="store_true")
    parser.add_argument("--fail-fast", action="store_true")
    args = parser.parse_args()

    universe = load_frozen_universe(args.source, args.evidence, args.supplement)
    result = run_batch(
        start_date=args.start,
        end_date=args.end,
        universe=universe,
        scan_settings=load_scan_settings(args.config),
        output_root=args.output_root,
        snapshot_root=args.snapshot_root,
        run_root=args.run_root,
        resume=not args.no_resume,
        max_days=args.max_days,
        fail_fast=args.fail_fast,
        stop_file=args.stop_file,
    )
    print(
        f"batch status={result.status}: dates={result.total_dates} completed={result.completed} "
        f"skipped={result.skipped} failed={result.failed}"
    )
    print(f"run journal: {result.run_path}")


if __name__ == "__main__":
    main()
