from __future__ import annotations

from typing import Any, Callable, Iterable

import pandas as pd

from data_fetcher import StockDataFetcher
from stock_scanner import StockUniverseEntry, load_price_metrics
from .data_source_gateway import run_provider_chain

ProgressCallback = Callable[[str], None]


def load_price_metrics_with_fallback(
    universe: list[StockUniverseEntry],
    progress: ProgressCallback | None = None,
    *,
    fallback_limit: int | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Load price metrics with a bounded official/Fugle/Yahoo per-stock fallback.

    `stock_scanner.load_price_metrics` is fast when Yahoo batch/cache works, but it
    can return an empty dict when Yahoo is unstable. For AI research commands we
    then retry missing names through StockDataFetcher, which already tries the
    stock-specific official/Fugle/Yahoo chain. Large universes are bounded to
    avoid making Telegram commands look frozen for a long time.
    """
    primary_result = run_provider_chain(
        [("stock_scanner.load_price_metrics", lambda: load_price_metrics(universe))],
        operation="load_price_metrics",
    )
    metrics = primary_result.data if isinstance(primary_result.data, dict) else {}
    primary_error = None
    if primary_result.status != "success":
        failed = [attempt for attempt in primary_result.attempts if attempt.error]
        primary_error = (failed[-1].error or {}).get("message") if failed else primary_result.status
    gateway_attempts = primary_result.to_dict().get("attempts", [])

    missing = [entry for entry in universe if entry.symbol not in metrics]
    if not missing:
        return metrics, {
            "status": "primary_complete",
            "primary_source": "stock_scanner.load_price_metrics",
            "requested": len(universe),
            "covered": len(metrics),
            "fallback_attempted": 0,
            "fallback_covered": 0,
            "primary_error": primary_error,
            "gateway_attempts": gateway_attempts,
        }

    if fallback_limit is None:
        fallback_limit = len(missing) if len(universe) <= 50 else 80
    fallback_targets = missing[: max(0, fallback_limit)]
    if progress:
        progress(f"價量備援：主要價量來源涵蓋 {len(metrics)}/{len(universe)} 檔，準備逐檔備援 {len(fallback_targets)} 檔")

    fallback_covered = 0
    fallback_errors: list[dict[str, str]] = []
    if fallback_targets:
        with StockDataFetcher() as fetcher:
            total = len(fallback_targets)
            for index, entry in enumerate(fallback_targets, 1):
                if progress and (index == 1 or index == total or index % 10 == 0):
                    progress(f"價量備援：逐檔抓取 {index}/{total} {entry.code} {entry.name}")
                try:
                    meta = fetcher.resolve_stock(entry.code)
                    history = fetcher.fetch_price_history(meta, months=4)
                    metric = price_metric_from_history(history)
                    if metric:
                        metric["source"] = "StockDataFetcher official/Fugle/Yahoo fallback"
                        metrics[entry.symbol] = metric
                        fallback_covered += 1
                    else:
                        fallback_errors.append({"code": entry.code, "error": "price history insufficient"})
                except Exception as exc:
                    fallback_errors.append({"code": entry.code, "error": str(exc)[:180]})

    skipped = max(0, len(missing) - len(fallback_targets))
    policy = {
        "status": "fallback_used" if fallback_covered else "partial_or_missing",
        "primary_source": "stock_scanner.load_price_metrics",
        "fallback_source": "StockDataFetcher official/Fugle/Yahoo chain",
        "requested": len(universe),
        "covered": len(metrics),
        "primary_covered": len(universe) - len(missing),
        "fallback_attempted": len(fallback_targets),
        "fallback_covered": fallback_covered,
        "fallback_skipped_for_runtime": skipped,
        "primary_error": primary_error,
        "gateway_attempts": gateway_attempts,
        "sample_errors": fallback_errors[:8],
    }
    if progress:
        progress(f"價量備援：完成，總涵蓋 {len(metrics)}/{len(universe)} 檔，備援補到 {fallback_covered} 檔，略過 {skipped} 檔")
    return metrics, policy


def price_metric_from_history(frame: pd.DataFrame) -> dict[str, Any] | None:
    if frame.empty or "Close" not in frame.columns:
        return None
    volume_col = "Volume_Lots" if "Volume_Lots" in frame.columns else "Volume" if "Volume" in frame.columns else None
    if volume_col is None:
        return None
    candidate = frame[["Close", volume_col]].copy()
    candidate["Close"] = pd.to_numeric(candidate["Close"], errors="coerce")
    candidate[volume_col] = pd.to_numeric(candidate[volume_col], errors="coerce")
    candidate = candidate.dropna()
    if candidate.empty:
        return None
    tail = candidate.tail(min(20, len(candidate)))
    avg_volume = float(tail[volume_col].mean())
    if volume_col == "Volume":
        avg_volume = avg_volume / 1000.0
    return {
        "price": float(candidate["Close"].iloc[-1]),
        "avg_volume_20d": avg_volume,
        "history_points": int(len(candidate)),
    }
