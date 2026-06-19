from __future__ import annotations

import json
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

import pandas as pd
import yfinance as yf

from stock_scanner import load_stock_universe

from .config import ROOT_DIR
from .price_fallbacks import load_price_metrics_with_fallback

from .official_connectors import (
    fetch_taifex_futures_institutional,
    fetch_taifex_vix,
    fetch_twse_institutional_flow,
)

MACRO_PROXY_CACHE_DIR = ROOT_DIR / ".cache" / "macro_indicators"
MACRO_PROXY_TIMEOUT_SECONDS = 120
MACRO_PROXY_MAX_PRICE_SYMBOLS = 360
MACRO_PROXY_MAX_PRICE_SYMBOLS_PER_INDUSTRY = 12


def build_macro_indicators(report_date: Any = None, progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    if progress:
        progress("宏觀研究：下載台股/櫃買指數資料")
    indices = _index_context(report_date)
    if progress:
        progress("宏觀研究：下載 VIX / TAIFEX 波動率資料")
    volatility = _volatility_context(report_date)
    if progress:
        progress("宏觀研究：下載 TAIFEX 期貨法人籌碼")
    official_futures = fetch_taifex_futures_institutional(report_date)
    if progress:
        progress("宏觀研究：下載 TWSE 三大法人現貨資金流")
    official_cash_flow = fetch_twse_institutional_flow(report_date)
    if progress:
        progress("宏觀研究：彙整類股流動性 proxy")
    industry_flow = _industry_flow_context(official_cash_flow, report_date=report_date, progress=progress)
    if progress:
        progress("宏觀研究：下載全球公開總經 proxy")
    global_public = _global_public_context(report_date, progress=progress)
    if progress:
        progress("宏觀研究：計算 fear/greed 分數")
    fear_greed = _fear_greed_score(indices, volatility, industry_flow, official_futures, official_cash_flow)
    return {
        "indices": indices,
        "volatility": volatility,
        "official_futures_institutional": official_futures,
        "official_cash_institutional_flow": official_cash_flow,
        "industry_flow": industry_flow,
        "fear_greed": fear_greed,
        "global_public_macro": global_public,
        "data_policy": {
            "status": "official_public_plus_proxy",
            "notes": [
                "TAIFEX Taiwan VIX public page is used when available; full licensed intraday/history files still require TAIFEX data shop or a paid feed.",
                "TAIFEX three-institution futures table is parsed best-effort from the public download page.",
                "TWSE three-institution cash flow is fetched from public BFI82U when available.",
                "Sector-level fund flow remains a local liquidity proxy until a sector fund-flow source is configured.",
            ],
        },
    }

def _global_public_context(report_date: Any = None, progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    symbols = {
        "USD/TWD": "TWD=X",
        "US10Y": "^TNX",
        "NASDAQ": "^IXIC",
        "S&P500": "^GSPC",
        "SOX": "^SOX",
        "WTI": "CL=F",
        "Gold": "GC=F",
    }
    result: dict[str, Any] = {}
    for index, (label, symbol) in enumerate(symbols.items(), 1):
        if progress:
            progress(f"宏觀研究：全球 proxy {index}/{len(symbols)} {label}")
        try:
            history = yf.Ticker(symbol).history(period="180d", interval="1d", auto_adjust=False).dropna(subset=["Close"])
            if report_date is not None and not history.empty:
                cutoff = pd.to_datetime(report_date)
                history = history[history.index.tz_localize(None) <= cutoff]
            result[label] = _index_metrics(history)
        except Exception as exc:
            result[label] = {"status": f"取得失敗：{exc}", "symbol": symbol}
    result["policy"] = "使用 Yahoo Finance 公開市場 proxy；正式利率、匯率或商品資料可改接官方/付費資料源。"
    return result

def _index_context(report_date: Any = None) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for symbol, label in (("^TWII", "加權指數"), ("^TWOII", "櫃買指數")):
        try:
            history = yf.Ticker(symbol).history(period="180d", interval="1d", auto_adjust=False).dropna(subset=["Close"])
            if report_date is not None and not history.empty:
                cutoff = pd.to_datetime(report_date)
                history = history[history.index.tz_localize(None) <= cutoff]
            output[label] = _index_metrics(history)
        except Exception as exc:
            output[label] = {"status": f"取得失敗：{exc}"}
    return output


def _volatility_context(report_date: Any = None) -> dict[str, Any]:
    result: dict[str, Any] = {"taifex_option_iv": fetch_taifex_vix(report_date)}
    try:
        vix = yf.Ticker("^VIX").history(period="120d", interval="1d", auto_adjust=False).dropna(subset=["Close"])
        if report_date is not None and not vix.empty:
            cutoff = pd.to_datetime(report_date)
            vix = vix[vix.index.tz_localize(None) <= cutoff]
        if vix.empty:
            result["global_vix"] = {"status": "no data"}
        else:
            close = pd.to_numeric(vix["Close"], errors="coerce").dropna()
            latest = float(close.iloc[-1])
            result["global_vix"] = {
                "latest": round(latest, 2),
                "latest_date": str(close.index[-1].date()),
                "risk_zone": _vix_zone(latest),
                "ma20": round(float(close.tail(20).mean()), 2) if len(close) >= 20 else None,
            }
    except Exception as exc:
        result["global_vix"] = {"status": f"取得失敗：{exc}"}
    return result


def _industry_flow_context(
    official_cash_flow: dict[str, Any] | None = None,
    report_date: Any = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    target_date = _coerce_report_date(report_date)
    cache_path = _industry_flow_cache_path(target_date)
    started = time.perf_counter()
    cached = _read_industry_flow_cache(cache_path)
    if cached:
        elapsed = time.perf_counter() - started
        if progress:
            progress(
                "宏觀研究：類股 proxy 快取命中 "
                f"date={target_date.isoformat()} loaded={cached.get('loaded_symbols', 0)} "
                f"elapsed={elapsed:.1f}s fallback=no"
            )
        return _attach_industry_flow_runtime_fields(
            cached,
            official_cash_flow=official_cash_flow,
            cache_hit=True,
            elapsed_seconds=elapsed,
        )

    try:
        universe = load_stock_universe(False)
        price_universe = _macro_proxy_price_universe(universe)
        if progress:
            progress(
                "宏觀研究：下載類股 proxy 必要價量樣本 "
                f"（price_sample={len(price_universe)}/{len(universe)} 檔，timeout={MACRO_PROXY_TIMEOUT_SECONDS}s）"
            )
        price_metrics, price_policy, timeout_used = _load_price_metrics_with_timeout(price_universe, progress=progress)
    except Exception as exc:
        stale = _read_latest_industry_flow_cache()
        if stale:
            elapsed = time.perf_counter() - started
            if progress:
                progress(f"宏觀研究：類股 proxy 載入失敗，改用最近快取 elapsed={elapsed:.1f}s reason={exc}")
            return _attach_industry_flow_runtime_fields(
                stale,
                official_cash_flow=official_cash_flow,
                cache_hit=False,
                elapsed_seconds=elapsed,
                degraded_reason=f"load_failed:{exc}",
            )
        return {"status": f"取得失敗：{exc}", "official_cash_flow": official_cash_flow or {}, "groups": []}

    if timeout_used:
        stale = _read_latest_industry_flow_cache()
        elapsed = time.perf_counter() - started
        if stale:
            if progress:
                progress(f"宏觀研究：類股 proxy 逾時，改用最近快取 loaded={stale.get('loaded_symbols', 0)} elapsed={elapsed:.1f}s")
            return _attach_industry_flow_runtime_fields(
                stale,
                official_cash_flow=official_cash_flow,
                cache_hit=False,
                elapsed_seconds=elapsed,
                degraded_reason="price_metrics_timeout_latest_cache",
            )
        simplified = _simplified_industry_flow(universe, price_policy, target_date, elapsed)
        if progress:
            progress(f"宏觀研究：類股 proxy 逾時且無快取，使用簡化 proxy elapsed={elapsed:.1f}s")
        return _attach_industry_flow_runtime_fields(
            simplified,
            official_cash_flow=official_cash_flow,
            cache_hit=False,
            elapsed_seconds=elapsed,
            degraded_reason="price_metrics_timeout_simplified_proxy",
        )

    groups: dict[str, dict[str, Any]] = defaultdict(lambda: {"symbols": 0, "priced_symbols": 0, "volume_sum": 0.0, "price_sum": 0.0})
    for entry in universe:
        key = entry.industry or "未分類"
        metric = price_metrics.get(entry.symbol) or {}
        groups[key]["symbols"] += 1
        price = metric.get("price")
        volume = metric.get("avg_volume_20d")
        if price is not None:
            groups[key]["priced_symbols"] += 1
            groups[key]["price_sum"] += float(price)
        if volume is not None:
            groups[key]["volume_sum"] += float(volume)

    rows: list[dict[str, Any]] = []
    total_volume = sum(row["volume_sum"] for row in groups.values()) or 1.0
    for industry, values in groups.items():
        priced = values["priced_symbols"]
        rows.append(
            {
                "industry": industry,
                "symbols": values["symbols"],
                "priced_symbols": priced,
                "liquidity_proxy": round(values["volume_sum"], 2),
                "liquidity_share_pct": round(values["volume_sum"] / total_volume * 100, 2),
                "avg_price": round(values["price_sum"] / priced, 2) if priced else None,
            }
        )
    rows.sort(key=lambda row: row["liquidity_proxy"], reverse=True)
    elapsed = time.perf_counter() - started
    result = {
        "status": "official_cash_plus_sector_proxy",
        "report_date": target_date.isoformat(),
        "price_data_policy": price_policy,
        "groups": rows[:20],
        "loaded_symbols": len(price_universe),
        "universe_symbols": len(universe),
        "priced_symbols": sum(1 for item in price_metrics.values() if isinstance(item, dict) and item),
        "cache_hit": False,
        "degraded": False,
        "elapsed_seconds": round(elapsed, 2),
        "method": "TWSE法人買賣金額作為大盤資金流；類股層級以本地股票宇宙20日均量彙總作為關注度proxy。",
    }
    _write_industry_flow_cache(cache_path, result)
    if progress:
        progress(f"宏觀研究：類股 proxy 載入完成 loaded={result['loaded_symbols']} priced={result['priced_symbols']} elapsed={elapsed:.1f}s")
    return _attach_industry_flow_runtime_fields(
        result,
        official_cash_flow=official_cash_flow,
        cache_hit=False,
        elapsed_seconds=elapsed,
    )


def _load_price_metrics_with_timeout(
    universe: list[Any],
    *,
    progress: Callable[[str], None] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], bool]:
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(load_price_metrics_with_fallback, universe, progress=progress, fallback_limit=80)
    try:
        price_metrics, price_policy = future.result(timeout=MACRO_PROXY_TIMEOUT_SECONDS)
        executor.shutdown(wait=False, cancel_futures=True)
        return price_metrics, price_policy, False
    except FuturesTimeoutError:
        future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
        return {}, {"status": "timeout", "timeout_seconds": MACRO_PROXY_TIMEOUT_SECONDS}, True


def _macro_proxy_price_universe(universe: list[Any]) -> list[Any]:
    by_industry: dict[str, list[Any]] = defaultdict(list)
    for entry in universe:
        by_industry[getattr(entry, "industry", None) or "未分類"].append(entry)

    selected: list[Any] = []
    for industry in sorted(by_industry, key=lambda key: len(by_industry[key]), reverse=True):
        selected.extend(by_industry[industry][:MACRO_PROXY_MAX_PRICE_SYMBOLS_PER_INDUSTRY])
        if len(selected) >= MACRO_PROXY_MAX_PRICE_SYMBOLS:
            break
    return selected[:MACRO_PROXY_MAX_PRICE_SYMBOLS]


def _simplified_industry_flow(
    universe: list[Any],
    price_policy: dict[str, Any],
    target_date: date,
    elapsed_seconds: float,
) -> dict[str, Any]:
    groups: dict[str, dict[str, Any]] = defaultdict(lambda: {"symbols": 0})
    for entry in universe:
        groups[getattr(entry, "industry", None) or "未分類"]["symbols"] += 1
    rows = [
        {
            "industry": industry,
            "symbols": values["symbols"],
            "priced_symbols": 0,
            "liquidity_proxy": None,
            "liquidity_share_pct": None,
            "avg_price": None,
        }
        for industry, values in groups.items()
    ]
    rows.sort(key=lambda row: row["symbols"], reverse=True)
    return {
        "status": "simplified_sector_proxy",
        "report_date": target_date.isoformat(),
        "price_data_policy": price_policy,
        "groups": rows[:20],
        "loaded_symbols": len(universe),
        "priced_symbols": 0,
        "cache_hit": False,
        "degraded": True,
        "degraded_reason": "price_metrics_timeout_simplified_proxy",
        "elapsed_seconds": round(elapsed_seconds, 2),
        "method": "價量載入逾時時，以本地股票宇宙產業分布作為簡化類股 proxy；不代表實際資金流。",
    }


def _attach_industry_flow_runtime_fields(
    data: dict[str, Any],
    *,
    official_cash_flow: dict[str, Any] | None,
    cache_hit: bool,
    elapsed_seconds: float,
    degraded_reason: str | None = None,
) -> dict[str, Any]:
    result = dict(data)
    result["official_cash_flow"] = official_cash_flow or {}
    result["cache_hit"] = cache_hit
    result["elapsed_seconds"] = round(elapsed_seconds, 2)
    if degraded_reason:
        result["degraded"] = True
        result["degraded_reason"] = degraded_reason
    return result


def _coerce_report_date(value: Any = None) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value:
        try:
            return pd.to_datetime(value).date()
        except Exception:
            pass
    return datetime.now().date()


def _industry_flow_cache_path(target_date: date) -> Path:
    return MACRO_PROXY_CACHE_DIR / f"industry_flow_{target_date.isoformat()}.json"


def _read_industry_flow_cache(path: Path) -> dict[str, Any] | None:
    try:
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and data.get("groups") else None
    except Exception:
        return None


def _read_latest_industry_flow_cache() -> dict[str, Any] | None:
    try:
        paths = sorted(MACRO_PROXY_CACHE_DIR.glob("industry_flow_*.json"), key=lambda item: item.stat().st_mtime, reverse=True)
    except Exception:
        return None
    for path in paths:
        cached = _read_industry_flow_cache(path)
        if cached:
            return cached
    return None


def _write_industry_flow_cache(path: Path, data: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {key: value for key, value in data.items() if key != "official_cash_flow"}
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    except Exception:
        return


def _index_metrics(history: pd.DataFrame) -> dict[str, Any]:
    if history.empty:
        return {"status": "no data"}
    close = pd.to_numeric(history["Close"], errors="coerce").dropna()
    latest = float(close.iloc[-1])
    result: dict[str, Any] = {"latest_close": round(latest, 2), "latest_date": str(close.index[-1].date())}
    if len(close) >= 2:
        previous = float(close.iloc[-2])
        result["one_day_change_points"] = round(latest - previous, 2)
        result["one_day_return_pct"] = round((latest / previous - 1) * 100, 2) if previous else None
    if len(close) >= 5:
        five_day_base = float(close.iloc[-5])
        result["five_day_change_points"] = round(latest - five_day_base, 2)
        result["five_day_return_pct"] = round((latest / five_day_base - 1) * 100, 2) if five_day_base else None
    for window in (5, 10, 21, 60):
        if len(close) >= window:
            ma = float(close.tail(window).mean())
            result[f"ma{window}"] = round(ma, 2)
            result[f"above_ma{window}"] = latest >= ma
    if len(close) >= 21:
        result["twenty_day_return_pct"] = round((latest / float(close.iloc[-21]) - 1) * 100, 2)
        result["realized_volatility_20d_pct"] = round(float(close.pct_change().tail(20).std()) * (252 ** 0.5) * 100, 2)
    return result


def _fear_greed_score(
    indices: dict[str, Any],
    volatility: dict[str, Any],
    industry_flow: dict[str, Any],
    official_futures: dict[str, Any] | None = None,
    official_cash_flow: dict[str, Any] | None = None,
) -> dict[str, Any]:
    score = 50.0
    reasons: list[str] = []
    for label, metrics in indices.items():
        for key, points in (("above_ma5", 3), ("above_ma10", 3), ("above_ma21", 5), ("above_ma60", 7)):
            if metrics.get(key) is True:
                score += points
                reasons.append(f"{label} 站上 {key.replace('above_', '').upper()} +{points}")
            elif metrics.get(key) is False:
                score -= points * 0.6
        ret = metrics.get("twenty_day_return_pct")
        if isinstance(ret, (int, float)):
            adjustment = max(-8, min(8, ret * 0.8))
            score += adjustment
            reasons.append(f"{label} 20日報酬 {ret}% 調整 {adjustment:.1f}")

    taifex_vix = ((volatility.get("taifex_option_iv") or {}).get("latest") or {}).get("value")
    if isinstance(taifex_vix, (int, float)):
        if taifex_vix >= 30:
            score -= 12
            reasons.append("TAIFEX VIX 高於30 -12")
        elif taifex_vix <= 16:
            score += 5
            reasons.append("TAIFEX VIX 低於16 +5")

    vix = (volatility.get("global_vix") or {}).get("latest")
    if isinstance(vix, (int, float)):
        if vix >= 30:
            score -= 10
            reasons.append("Global VIX 高於30 -10")
        elif vix >= 22:
            score -= 5
            reasons.append("Global VIX 高於22 -5")
        elif vix <= 15:
            score += 4
            reasons.append("Global VIX 低於15 +4")

    cash_net = (official_cash_flow or {}).get("net_amount_total")
    if isinstance(cash_net, (int, float)):
        if cash_net > 0:
            score += min(8, abs(cash_net) / 10_000_000_000)
            reasons.append("TWSE三大法人現貨合計買超")
        elif cash_net < 0:
            score -= min(8, abs(cash_net) / 10_000_000_000)
            reasons.append("TWSE三大法人現貨合計賣超")

    for row in (official_futures or {}).get("tx_futures_rows") or []:
        if row.get("identity") == "外資" and isinstance(row.get("open_interest_net_contracts"), (int, float)):
            net = float(row["open_interest_net_contracts"])
            if net > 0:
                score += min(6, net / 5000)
                reasons.append("TAIFEX外資台指期未平倉偏多")
            elif net < 0:
                score -= min(6, abs(net) / 5000)
                reasons.append("TAIFEX外資台指期未平倉偏空")
            break

    top_groups = (industry_flow.get("groups") or [])[:3]
    if top_groups:
        concentration = sum(float(row.get("liquidity_share_pct") or 0) for row in top_groups)
        if concentration >= 55:
            score -= 4
            reasons.append("前三大類股流動性集中度偏高 -4")
        elif concentration <= 35:
            score += 4
            reasons.append("類股流動性分散 +4")

    score = round(max(0, min(100, score)), 1)
    return {
        "score": score,
        "zone": _fear_greed_zone(score),
        "reasons": reasons[:18],
        "method": "指數趨勢、TAIFEX VIX、Global VIX、TAIFEX期貨法人、TWSE現貨法人與類股流動性proxy組成。",
    }


def _vix_zone(value: float) -> str:
    if value >= 30:
        return "stress"
    if value >= 22:
        return "caution"
    if value <= 15:
        return "calm"
    return "neutral"


def _fear_greed_zone(score: float) -> str:
    if score >= 75:
        return "greed"
    if score >= 60:
        return "risk_on"
    if score >= 40:
        return "neutral"
    if score >= 25:
        return "risk_off"
    return "fear"





