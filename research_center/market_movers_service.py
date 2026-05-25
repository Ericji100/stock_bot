from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from stock_scanner import load_price_metrics, load_stock_universe

from .config import ROOT_DIR

ProgressCallback = Callable[[str], None]

CACHE_DIR = ROOT_DIR / ".cache" / "market_movers"
DEFAULT_LIMIT = 30
SNAPSHOT_TTL_SECONDS = 30 * 60


def build_market_movers(
    report_date: date | None = None,
    *,
    universe: list[Any] | None = None,
    price_metrics: dict[str, Any] | None = None,
    force_refresh: bool = False,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Build a full-market mover snapshot without applying strategy hard filters.

    This service is intentionally independent from /scan hard filters. It
    answers "what moved in the market" rather than "what is tradable for a
    strategy". Missing fields are surfaced in data_quality instead of guessed.
    """
    target_date = report_date or datetime.now().date()
    cache_path = _snapshot_path(target_date)
    if not force_refresh and universe is None and price_metrics is None:
        cached = _read_snapshot(cache_path)
        if cached and _snapshot_is_fresh(cached, target_date):
            return cached
        if cached:
            return _mark_stale_snapshot(cached)

    if progress:
        progress(f"Market movers：載入全市場股票宇宙，date={target_date.isoformat()}")
    universe = universe if universe is not None else load_stock_universe(False)
    if price_metrics is None:
        if progress:
            progress(f"Market movers：抓取/讀取全市場價量資料 {len(universe)} 檔")
        price_metrics = _safe_price_metrics(universe)

    rows = [_build_stock_mover_row(entry, price_metrics) for entry in universe]
    rows = [row for row in rows if row.get("has_price_metric")]
    data = _assemble_market_movers(target_date, rows)

    if universe is not None and price_metrics is not None:
        _write_snapshot(cache_path, data)
    return data


def rows_from_market_movers(market_movers: dict[str, Any], key: str = "active_movers") -> list[dict[str, Any]]:
    rows = market_movers.get(key) or []
    return [dict(row) for row in rows if isinstance(row, dict)]


def _assemble_market_movers(target_date: date, rows: list[dict[str, Any]]) -> dict[str, Any]:
    top_gainers = _top_with_value(rows, "change_pct", reverse=True, positive_only=True)
    top_losers = _top_with_value(rows, "change_pct", reverse=False, negative_only=True)
    top_volume_surge = _top_with_value(rows, "volume_ratio", reverse=True, positive_only=True)
    top_turnover = _top_with_value(rows, "turnover", reverse=True, positive_only=True)
    new_highs = _top_with_value(rows, "new_high_days", reverse=True, positive_only=True)
    new_lows = _top_with_value(rows, "new_low_days", reverse=True, positive_only=True)
    limit_up = [row for row in rows if row.get("limit_up")][:DEFAULT_LIMIT]
    limit_down = [row for row in rows if row.get("limit_down")][:DEFAULT_LIMIT]
    top_active = sorted(rows, key=lambda row: (_num(row.get("avg_volume_20d")), _num(row.get("price"))), reverse=True)[:DEFAULT_LIMIT]

    active = _dedupe_rows(
        [
            *top_gainers,
            *top_volume_surge,
            *top_turnover,
            *new_highs,
            *limit_up,
            *(top_active if not (top_gainers or top_volume_surge or top_turnover or new_highs or limit_up) else []),
        ],
        limit=160,
    )
    downside = _dedupe_rows([*top_losers, *new_lows, *limit_down], limit=80)
    sector_rankings = _sector_rankings(rows)
    missing_fields = _missing_fields(rows)
    source_mode = "price_change_available" if "change_pct" not in missing_fields else "price_volume_proxy"
    market_data_date = _market_data_date(rows) or target_date.isoformat()
    generated_at = datetime.now().isoformat(timespec="seconds")
    return {
        "command_role": "market_movers",
        "report_date": target_date.isoformat(),
        "market_data_date": market_data_date,
        "report_generated_at": generated_at,
        "generated_at": generated_at,
        "source_mode": source_mode,
        "hard_filter_policy": "全市場上市櫃股票，不套用 /scan 股價、均量、營收等硬篩。",
        "mover_universe_count": len(rows),
        "active_movers": active,
        "downside_movers": downside,
        "top_gainers": top_gainers,
        "top_losers": top_losers,
        "top_volume_surge": top_volume_surge,
        "top_turnover": top_turnover,
        "new_highs": new_highs,
        "new_lows": new_lows,
        "limit_up": limit_up,
        "limit_down": limit_down,
        "top_active_by_liquidity": top_active,
        "sector_mover_rankings": sector_rankings,
        "data_quality": {
            "input_stock_count": len(rows),
            "missing_fields": missing_fields,
            "change_pct_coverage_pct": _coverage(rows, "change_pct"),
            "volume_ratio_coverage_pct": _coverage(rows, "volume_ratio"),
            "turnover_coverage_pct": _coverage(rows, "turnover"),
            "new_high_low_coverage_pct": max(_coverage(rows, "new_high_days"), _coverage(rows, "new_low_days")),
            "known_limitations": [
                "market_movers 不套用選股硬篩，因此會保留低價股、小型股與投機股作為市場訊號。",
                "若價量快取缺少 change_pct、turnover、volume_ratio 或新高新低欄位，該排行會標示不足並以流動性 proxy 輔助。",
                "歷史盤中排行若未事先保存，僅能由可取得的日線 OHLCV 重建，不假裝存在即時盤中排行。",
            ],
        },
    }


def _build_stock_mover_row(entry: Any, price_metrics: dict[str, Any]) -> dict[str, Any]:
    symbol = str(getattr(entry, "symbol", ""))
    code = str(getattr(entry, "code", ""))
    metric = price_metrics.get(symbol) or price_metrics.get(code) or {}
    price = _first(metric, "price", "close", "Close", "latest_close")
    prev_close = _first(metric, "previous_close", "prev_close", "yesterday_close")
    change_pct = _first(metric, "change_pct", "pct_change", "return_1d_pct", "day_change_pct")
    if change_pct is None and price is not None and prev_close:
        change_pct = round((float(price) / float(prev_close) - 1) * 100, 2)
    volume = _first(metric, "volume", "Volume", "latest_volume")
    avg_volume = _first(metric, "avg_volume_20d", "average_volume_20d")
    volume_ratio = _first(metric, "volume_ratio", "rvol")
    if volume_ratio is None and volume is not None and avg_volume:
        volume_ratio = round(float(volume) / float(avg_volume), 2)
    turnover = _first(metric, "turnover", "amount", "trading_value")
    if turnover is None and price is not None and volume is not None:
        turnover = round(float(price) * float(volume), 2)
    return {
        "code": code,
        "name": str(getattr(entry, "name", "")),
        "symbol": symbol,
        "industry": str(getattr(entry, "industry", "")),
        "price": price,
        "previous_close": prev_close,
        "price_date": _first(metric, "price_date", "date", "trade_date"),
        "change_pct": change_pct,
        "volume": volume,
        "avg_volume_20d": avg_volume,
        "volume_ratio": volume_ratio,
        "turnover": turnover,
        "new_high_days": _first(metric, "new_high_days", "high_days"),
        "new_low_days": _first(metric, "new_low_days", "low_days"),
        "limit_up": bool(metric.get("limit_up") or metric.get("is_limit_up") or False),
        "limit_down": bool(metric.get("limit_down") or metric.get("is_limit_down") or False),
        "has_price_metric": bool(metric),
    }


def _market_data_date(rows: list[dict[str, Any]]) -> str | None:
    dates = sorted(
        str(row.get("price_date") or "").strip()
        for row in rows
        if str(row.get("price_date") or "").strip()
    )
    return dates[-1] if dates else None


def _sector_rankings(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_sector: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_sector[str(row.get("industry") or "未分類")].append(row)
    result = []
    for sector, sector_rows in by_sector.items():
        gainers = [row for row in sector_rows if _num(row.get("change_pct")) > 0]
        losers = [row for row in sector_rows if _num(row.get("change_pct")) < 0]
        volume_surge = [row for row in sector_rows if _num(row.get("volume_ratio")) >= 1.5]
        new_highs = [row for row in sector_rows if _num(row.get("new_high_days")) > 0]
        avg_change = _avg(_num(row.get("change_pct")) for row in sector_rows if row.get("change_pct") is not None)
        median_change = _median([_num(row.get("change_pct")) for row in sector_rows if row.get("change_pct") is not None])
        turnover_sum = sum(_num(row.get("turnover")) for row in sector_rows)
        avg_volume = _avg(_num(row.get("avg_volume_20d")) for row in sector_rows)
        score = min(
            100.0,
            max(0.0, avg_change or 0.0) * 4
            + len(gainers) * 3
            + len(volume_surge) * 4
            + len(new_highs) * 5
            + min(20.0, turnover_sum / 1_000_000_000)
            + min(15.0, (avg_volume or 0.0) / 800),
        )
        result.append({
            "sector": sector,
            "sector_score": round(score, 2),
            "stock_count": len(sector_rows),
            "advancers": len(gainers),
            "decliners": len(losers),
            "avg_change_pct": round(avg_change, 2) if avg_change is not None else None,
            "median_change_pct": round(median_change, 2) if median_change is not None else None,
            "volume_surge_count": len(volume_surge),
            "new_high_count": len(new_highs),
            "new_low_count": sum(1 for row in sector_rows if _num(row.get("new_low_days")) > 0),
            "limit_up_count": sum(1 for row in sector_rows if row.get("limit_up")),
            "limit_down_count": sum(1 for row in sector_rows if row.get("limit_down")),
            "turnover_sum": round(turnover_sum, 2) if turnover_sum else None,
            "top_gainers": _top_with_value(sector_rows, "change_pct", reverse=True, positive_only=True, limit=5),
            "top_losers": _top_with_value(sector_rows, "change_pct", reverse=False, negative_only=True, limit=5),
            "top_volume_surge": _top_with_value(sector_rows, "volume_ratio", reverse=True, positive_only=True, limit=5),
            "top_turnover": _top_with_value(sector_rows, "turnover", reverse=True, positive_only=True, limit=5),
        })
    result.sort(key=lambda row: (row["sector_score"], row["advancers"], row["volume_surge_count"]), reverse=True)
    return result[:30]


def _top_with_value(
    rows: list[dict[str, Any]],
    field: str,
    *,
    reverse: bool,
    positive_only: bool = False,
    negative_only: bool = False,
    limit: int = DEFAULT_LIMIT,
) -> list[dict[str, Any]]:
    selected = [row for row in rows if row.get(field) is not None]
    if positive_only:
        selected = [row for row in selected if _num(row.get(field)) > 0]
    if negative_only:
        selected = [row for row in selected if _num(row.get(field)) < 0]
    selected.sort(key=lambda row: _num(row.get(field)), reverse=reverse)
    return [dict(row) for row in selected[:limit]]


def _dedupe_rows(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    seen = set()
    result = []
    for row in rows:
        code = str(row.get("code") or "")
        if not code or code in seen:
            continue
        result.append(dict(row))
        seen.add(code)
        if len(result) >= limit:
            break
    return result


def _missing_fields(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["all"]
    fields = ["change_pct", "volume_ratio", "turnover", "new_high_days", "new_low_days"]
    return [field for field in fields if not any(row.get(field) is not None for row in rows)]


def _coverage(rows: list[dict[str, Any]], field: str) -> float:
    if not rows:
        return 0.0
    ready = sum(1 for row in rows if row.get(field) is not None)
    return round(ready / len(rows) * 100, 2)


def _first(metric: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = metric.get(key)
        if value in (None, ""):
            continue
        try:
            return float(value)
        except Exception:
            continue
    return None


def _avg(values: Any) -> float | None:
    nums = [float(v) for v in values if v is not None]
    return sum(nums) / len(nums) if nums else None


def _median(values: list[float]) -> float | None:
    nums = sorted(v for v in values if v is not None)
    if not nums:
        return None
    mid = len(nums) // 2
    if len(nums) % 2:
        return nums[mid]
    return (nums[mid - 1] + nums[mid]) / 2


def _num(value: Any) -> float:
    try:
        if value in (None, ""):
            return 0.0
        return float(value)
    except Exception:
        return 0.0


def _safe_price_metrics(universe: list[Any]) -> dict[str, Any]:
    try:
        return load_price_metrics(universe)
    except Exception:
        return {}


def _snapshot_path(target_date: date) -> Path:
    return CACHE_DIR / f"market_movers_{target_date.isoformat()}.json"


def _read_snapshot(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return None


def _snapshot_is_fresh(payload: dict[str, Any], target_date: date) -> bool:
    generated_at = str(payload.get("generated_at") or "")
    try:
        generated_dt = datetime.fromisoformat(generated_at)
    except Exception:
        return True
    if target_date == datetime.now().date() and datetime.now() - generated_dt > timedelta(seconds=SNAPSHOT_TTL_SECONDS):
        return False
    return True


def _mark_stale_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    data = dict(payload)
    data["stale_snapshot_used"] = True
    data_quality = dict(data.get("data_quality") or {})
    data_quality["snapshot_stale"] = True
    warnings = list(data_quality.get("warnings") or [])
    if "使用同日舊 market_movers 快照，避免外部價量抓取失敗造成報告中斷。" not in warnings:
        warnings.append("使用同日舊 market_movers 快照，避免外部價量抓取失敗造成報告中斷。")
    data_quality["warnings"] = warnings
    data["data_quality"] = data_quality
    return data


def _write_snapshot(path: Path, data: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    except Exception:
        return
