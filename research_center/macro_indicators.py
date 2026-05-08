from __future__ import annotations

from collections import defaultdict
from typing import Any, Callable

import pandas as pd
import yfinance as yf

from stock_scanner import load_stock_universe

from .price_fallbacks import load_price_metrics_with_fallback

from .official_connectors import (
    fetch_taifex_futures_institutional,
    fetch_taifex_vix,
    fetch_twse_institutional_flow,
)


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
    industry_flow = _industry_flow_context(official_cash_flow, progress=progress)
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


def _industry_flow_context(official_cash_flow: dict[str, Any] | None = None, progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    try:
        universe = load_stock_universe(False)
        if progress:
            progress(f"宏觀研究：下載全市場價量供類股 proxy 使用（{len(universe)} 檔）")
        price_metrics, price_policy = load_price_metrics_with_fallback(universe, progress=progress, fallback_limit=80)
    except Exception as exc:
        return {"status": f"取得失敗：{exc}", "official_cash_flow": official_cash_flow or {}, "groups": []}

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
    return {
        "status": "official_cash_plus_sector_proxy",
        "official_cash_flow": official_cash_flow or {},
        "price_data_policy": price_policy,
        "groups": rows[:20],
        "method": "TWSE法人買賣金額作為大盤資金流；類股層級以本地股票宇宙20日均量彙總作為關注度proxy。",
    }


def _index_metrics(history: pd.DataFrame) -> dict[str, Any]:
    if history.empty:
        return {"status": "no data"}
    close = pd.to_numeric(history["Close"], errors="coerce").dropna()
    latest = float(close.iloc[-1])
    result: dict[str, Any] = {"latest_close": round(latest, 2), "latest_date": str(close.index[-1].date())}
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





