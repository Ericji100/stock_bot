from __future__ import annotations

import json
import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from stock_ai_bot.market.market_risk_service import MarketRiskResult, load_market_risk_map
from research_center.recent_scans import load_recent_scan_results
from research_center.topic_context import build_stock_topic_context
from stock_ai_bot.scanning.stock_scanner import StockUniverseEntry, load_recent_revenue_history, load_stock_universe
from stock_ai_bot.scanning.technical_scanner import fetch_daily_history
from stock_ai_bot.telegram.telegram_stock_formatting import mark_stock_text


ROOT_DIR = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT_DIR / "config" / "laoxiao_scoring.json"
SCAN_TYPE = "老蕭選股"
SCORING_VERSION = "laoxiao_v1"
REPORT_MESSAGE_MAX_CHARS = 3500


@dataclass
class LaoXiaoCandidate:
    code: str
    symbol: str
    market: str
    name: str
    industry: str
    price: float
    avg_volume_20d: float
    history_rows: int
    price_source: str
    adjusted_price_status: str
    peer_group: str = ""
    peer_group_source: str = ""
    peer_count: int = 0
    setup_type: str = ""
    features: dict[str, Any] = field(default_factory=dict)
    component_scores: dict[str, int] = field(default_factory=dict)
    component_details: dict[str, dict[str, Any]] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    revenue_history: list[dict[str, Any]] = field(default_factory=list)
    theme_context: dict[str, Any] = field(default_factory=dict)
    market_risk: dict[str, Any] = field(default_factory=dict)
    score_cap: int | None = None
    total_score: int = 0


@dataclass(frozen=True)
class LaoXiaoScanResult:
    report_date: date
    selected_codes: list[str]
    candidates: list[LaoXiaoCandidate]
    observation_candidates: list[LaoXiaoCandidate]
    diagnostics: dict[str, Any]
    report_messages: list[str]
    report_text: str


def build_laoxiao_scan_result(
    scan_settings: dict[str, Any] | None = None,
    report_date: date | None = None,
    *,
    progress: Callable[[str], None] | None = None,
    historical_replay: bool = False,
) -> LaoXiaoScanResult:
    target_date = report_date or date.today()
    config = load_laoxiao_config()
    hard = dict(config.get("hard_filters") or {})
    thresholds = dict(config.get("thresholds") or {})
    weights = dict(config.get("weights") or {})
    _apply_scan_setting_overrides(hard, scan_settings or {})

    emit = progress or (lambda _message: None)
    emit("老蕭選股：讀取股票清單與日線快取")
    universe = load_stock_universe(False)
    formal_groups = _load_formal_peer_groups()
    hard_filter_failures: Counter[str] = Counter()
    stage_one: list[LaoXiaoCandidate] = []
    source_counts: Counter[str] = Counter()

    for index, entry in enumerate(universe, start=1):
        if not entry.symbol:
            hard_filter_failures["missing_symbol"] += 1
            continue
        try:
            history, source = fetch_daily_history(entry.symbol, target_date)
        except Exception:
            history, source = pd.DataFrame(), "unavailable"
        history = _history_to_date(history, target_date)
        if history.empty or history["date"].dt.date.max() != target_date:
            hard_filter_failures["missing_target_date_bar"] += 1
            continue
        failure = _hard_filter_failure(history, hard)
        if failure:
            hard_filter_failures[failure] += 1
            continue
        features = _technical_features(history, thresholds)
        candidate = LaoXiaoCandidate(
            code=entry.code,
            symbol=entry.symbol,
            market=entry.market,
            name=entry.name,
            industry=entry.industry,
            price=float(features["close"]),
            avg_volume_20d=float(features["avg_volume_20d"]),
            history_rows=len(history),
            price_source=source,
            adjusted_price_status=str(features["adjusted_price_status"]),
            features=features,
        )
        candidate.features["formal_theme_group"] = formal_groups.get(entry.code)
        stage_one.append(candidate)
        source_counts[source] += 1
        if index % 100 == 0:
            emit(f"老蕭選股：日線檢查 {index}/{len(universe)}，通過硬篩 {len(stage_one)} 檔")

    emit(f"老蕭選股：計算 {len(stage_one)} 檔同業相對強弱與創高順序")
    _attach_peer_metrics(stage_one, int(hard.get("min_peer_count") or 3))

    if historical_replay:
        risk_result = MarketRiskResult(
            target_date,
            {},
            {"all": "historical_unavailable:no_point_in_time_cache"},
        )
    else:
        try:
            risk_result = load_market_risk_map(target_date)
        except Exception as exc:
            risk_result = MarketRiskResult(target_date, {}, {"all": f"unavailable:{type(exc).__name__}"})

    formal_candidates: list[LaoXiaoCandidate] = []
    observations: list[LaoXiaoCandidate] = []
    for candidate in stage_one:
        candidate.market_risk = dict(risk_result.by_code.get(candidate.code) or {})
        _score_market_stage(candidate, hard, thresholds, weights, risk_result)
        if candidate.setup_type:
            formal_candidates.append(candidate)
        elif _is_observation_candidate(candidate, thresholds):
            observations.append(candidate)

    formal_candidates.sort(key=_stage_one_sort_key, reverse=True)
    fetch_limit = max(0, int(thresholds.get("fundamental_fetch_limit") or 50))
    shortlist = formal_candidates[:fetch_limit]
    emit(f"老蕭選股：Top {len(shortlist)} 補齊正式營收、財報與題材資料")
    try:
        if historical_replay:
            _attach_historical_fundamental_scores(shortlist, universe, target_date, weights)
        else:
            _attach_fundamental_and_theme_scores(shortlist, universe, target_date, weights, emit)
    except Exception as exc:
        for candidate in shortlist:
            candidate.component_scores["fundamental_support"] = 0
            candidate.component_scores["theme_catalyst"] = 0
            candidate.risks.append(f"營收財報或題材補齊失敗：{type(exc).__name__}")

    for candidate in shortlist:
        _finalize_candidate_score(candidate, hard, thresholds, weights)
    shortlist.sort(key=lambda item: (item.total_score, item.features.get("relative_strength_percentile") or 0, item.code), reverse=True)

    minimum_score = int(thresholds.get("minimum_score") or 50)
    report_limit = max(1, int(thresholds.get("report_limit") or 30))
    selected = [item for item in shortlist if item.total_score >= minimum_score][:report_limit]
    selected_codes = [item.code for item in selected]
    diagnostics = {
        "scoring_version": SCORING_VERSION,
        "universe_count": len(universe),
        "hard_filter_passed": len(stage_one),
        "formal_setup_count": len(formal_candidates),
        "fundamental_prefetch_count": len(shortlist),
        "selected_count": len(selected),
        "hard_filter_failures": dict(hard_filter_failures),
        "price_sources": dict(source_counts),
        "adjusted_price_covered": sum(item.adjusted_price_status == "adjusted" for item in stage_one),
        "risk_source_status": dict(risk_result.source_status),
        "risk_source_complete": risk_result.complete,
        "hard_filters": hard,
        "minimum_score": minimum_score,
        "historical_replay": historical_replay,
    }
    messages = format_laoxiao_scan_messages(target_date, selected, observations, diagnostics, config)
    return LaoXiaoScanResult(
        report_date=target_date,
        selected_codes=selected_codes,
        candidates=selected,
        observation_candidates=observations[:10],
        diagnostics=diagnostics,
        report_messages=messages,
        report_text="\n\n".join(messages),
    )


def load_laoxiao_config() -> dict[str, Any]:
    try:
        payload = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise RuntimeError(f"老蕭選股設定讀取失敗：{exc}") from exc
    required = {"hard_filters", "thresholds", "weights"}
    if not isinstance(payload, dict) or not required.issubset(payload):
        raise RuntimeError("老蕭選股設定格式不完整")
    return payload


def find_cached_laoxiao_scan(report_date: date) -> dict[str, Any] | None:
    for record in load_recent_scan_results(limit=30):
        if str(record.get("scan_type") or "") not in {SCAN_TYPE, "laoxiao"}:
            continue
        if str(record.get("report_date") or "") != report_date.isoformat():
            continue
        if str(record.get("scoring_version") or "") != SCORING_VERSION:
            continue
        if record.get("selected_codes") or record.get("codes"):
            return record
    return None


def format_laoxiao_scan_messages(
    report_date: date,
    candidates: list[LaoXiaoCandidate],
    observations: list[LaoXiaoCandidate],
    diagnostics: dict[str, Any],
    config: dict[str, Any],
) -> list[str]:
    hard = diagnostics.get("hard_filters") or {}
    header = [
        f"📈 老蕭選股｜{report_date.isoformat()}",
        "定位：獨立策略候選排序，不改寫既有財報、技術、精選或 Radar 評分。",
        (
            f"硬篩：股價 ≥ {_fmt(hard.get('min_price'))}、不設最高價、"
            f"20 日均量 ≥ {_fmt(hard.get('min_avg_volume_20d_lots'))} 張、"
            f"日線 ≥ {int(hard.get('min_daily_rows') or 120)} 日、同業至少 {int(hard.get('min_peer_count') or 3)} 家。"
        ),
        (
            f"結果：{len(candidates)} 檔｜硬篩通過 {diagnostics.get('hard_filter_passed', 0)}｜"
            f"型態成立 {diagnostics.get('formal_setup_count', 0)}｜評分版本 {SCORING_VERSION}"
        ),
        "權重：技術 30／同業領先 20／量能 15／既有統一營收財報 20／題材催化 10／風險與資料品質 5。",
        "註：權重與精確門檻是程式回測初始值；課程中的主觀判斷、部位與自動下單未列為正式規則。",
    ]
    if not diagnostics.get("risk_source_complete"):
        header.append("⚠️ 官方注意／處置資料源未全部成功，本次未查到不代表沒有風險標記。")

    if not candidates:
        header.append(f"本次沒有達到 {diagnostics.get('minimum_score', 50)} 分的正式候選。")
        if observations:
            preview = "、".join(f"{item.code} {item.name}" for item in observations[:5])
            header.append(f"接近型態但未正式成立：{preview}")
        return ["\n".join(header)]

    blocks = [_candidate_report_block(index, item) for index, item in enumerate(candidates, start=1)]
    messages: list[str] = []
    current = "\n".join(header)
    for block in blocks:
        proposal = f"{current}\n\n{block}" if current else block
        if len(proposal) > REPORT_MESSAGE_MAX_CHARS and current:
            messages.append(current)
            current = block
        else:
            current = proposal
    if current:
        messages.append(current)
    return messages


def _candidate_report_block(index: int, item: LaoXiaoCandidate) -> str:
    setup_label = "強勢突破型" if item.setup_type == "strong_breakout" else "拉回轉強型"
    component = item.component_scores
    rs = item.features.get("relative_strength_percentile")
    drawdown = item.features.get("drawdown_252_pct")
    volume_ratio = item.features.get("volume_ratio")
    reasons = "；".join(_unique(item.reasons)[:4]) or "型態成立"
    risks = "；".join(_unique(item.risks)[:3]) or "未見主要規則風險"
    marked = mark_stock_text(f"{item.code} {item.name}")
    return "\n".join(
        [
            f"{index}. {marked}｜{item.total_score} 分｜{setup_label}",
            (
                "   分項："
                f"技術 {component.get('technical_stage', 0)}/30、"
                f"同業 {component.get('sector_leadership', 0)}/20、"
                f"量能 {component.get('volume_liquidity', 0)}/15、"
                f"基本面 {component.get('fundamental_support', 0)}/20、"
                f"題材 {component.get('theme_catalyst', 0)}/10、"
                f"風險品質 {component.get('risk_data_quality', 0)}/5"
            ),
            (
                f"   狀態：相對強弱 {_fmt(rs)} 百分位、距一年高點 {_fmt(drawdown)}%、"
                f"量比 {_fmt(volume_ratio)}、同業 {item.peer_count} 家"
            ),
            f"   理由：{reasons}",
            f"   風險：{risks}",
        ]
    )


def _apply_scan_setting_overrides(hard: dict[str, Any], scan_settings: dict[str, Any]) -> None:
    # Only the two compatible global filters are inherited.  The global maximum
    # price and monthly-revenue gate deliberately do not apply to this strategy.
    mapping = {"min_price": "min_price", "min_avg_volume_20d": "min_avg_volume_20d_lots"}
    for source_key, target_key in mapping.items():
        if source_key not in scan_settings:
            continue
        try:
            hard[target_key] = float(scan_settings[source_key])
        except (TypeError, ValueError):
            continue


def _history_to_date(history: pd.DataFrame, target_date: date) -> pd.DataFrame:
    if history is None or history.empty or "date" not in history.columns:
        return pd.DataFrame()
    frame = history.copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame = frame[frame["date"].dt.date <= target_date]
    return frame.sort_values("date").drop_duplicates("date").reset_index(drop=True)


def _hard_filter_failure(history: pd.DataFrame, hard: dict[str, Any]) -> str | None:
    min_rows = int(hard.get("min_daily_rows") or 120)
    if history.empty or len(history) < min_rows:
        return "insufficient_daily_history"
    close = pd.to_numeric(history.get("close"), errors="coerce")
    volume = pd.to_numeric(history.get("volume"), errors="coerce")
    if close.empty or pd.isna(close.iloc[-1]) or float(close.iloc[-1]) <= 0:
        return "missing_price"
    price = float(close.iloc[-1])
    if price < float(hard.get("min_price") or 0):
        return "price_below_min"
    max_price = _float_or_none(hard.get("max_price"))
    if max_price is not None and price > max_price:
        return "price_above_max"
    avg_volume = float(volume.tail(20).mean() / 1000.0) if volume.notna().sum() >= 20 else math.nan
    if math.isnan(avg_volume):
        return "missing_avg_volume_20d"
    if avg_volume < float(hard.get("min_avg_volume_20d_lots") or 0):
        return "avg_volume_below_min"
    return None


def _technical_features(history: pd.DataFrame, thresholds: dict[str, Any]) -> dict[str, Any]:
    close = pd.to_numeric(history["close"], errors="coerce")
    high = pd.to_numeric(history["high"], errors="coerce")
    volume = pd.to_numeric(history["volume"], errors="coerce")
    adjusted = pd.to_numeric(history.get("adj_close"), errors="coerce") if "adj_close" in history.columns else pd.Series(dtype=float)
    if len(adjusted) == len(close) and adjusted.notna().mean() >= 0.9 and (adjusted.dropna() > 0).all():
        performance_close = adjusted.ffill().bfill()
        adjustment_status = "adjusted"
    else:
        performance_close = close
        adjustment_status = "raw_fallback"

    ma = {window: float(close.tail(window).mean()) if len(close) >= window else None for window in (20, 21, 60)}
    previous_ma20 = float(close.iloc[:-1].tail(20).mean()) if len(close) >= 21 else None
    previous_ma21 = float(close.iloc[:-1].tail(21).mean()) if len(close) >= 22 else None
    latest = float(close.iloc[-1])
    previous = float(close.iloc[-2])
    avg_volume = float(volume.tail(20).mean() / 1000.0)
    latest_volume = float(volume.iloc[-1] / 1000.0)
    ratio = latest_volume / avg_volume if avg_volume > 0 else None
    up_mask = close.diff() > 0
    recent_volume = volume.tail(20)
    recent_up = up_mask.tail(20)
    up_volume = recent_volume[recent_up].mean()
    down_volume = recent_volume[~recent_up].mean()
    up_down_volume_ratio = float(up_volume / down_volume) if pd.notna(up_volume) and pd.notna(down_volume) and down_volume > 0 else None

    breakouts = {
        str(window): _breakout_context(history, window, int(thresholds.get("recent_breakout_lookback_days") or 20), int(thresholds.get("breakout_confirmation_days") or 3))
        for window in (20, 60, 252)
    }
    high_252 = float(high.tail(252).max()) if len(high) >= 120 else None
    drawdown = max(0.0, (1.0 - latest / high_252) * 100.0) if high_252 and high_252 > 0 else None
    return {
        "close": latest,
        "previous_close": previous,
        "avg_volume_20d": avg_volume,
        "latest_volume_lots": latest_volume,
        "volume_ratio": round(ratio, 4) if ratio is not None else None,
        "up_down_volume_ratio": round(up_down_volume_ratio, 4) if up_down_volume_ratio is not None else None,
        "ma20": ma[20],
        "ma21": ma[21],
        "ma60": ma[60],
        "above_ma20": bool(ma[20] and latest >= ma[20]),
        "above_ma21": bool(ma[21] and latest >= ma[21]),
        "above_ma60": bool(ma[60] and latest >= ma[60]),
        "reclaim_ma20": bool(ma[20] and previous_ma20 and previous < previous_ma20 and latest >= ma[20]),
        "reclaim_ma21": bool(ma[21] and previous_ma21 and previous < previous_ma21 and latest >= ma[21]),
        "ma20_above_ma60": bool(ma[20] and ma[60] and ma[20] >= ma[60]),
        "ma20_rising": bool(len(close) >= 25 and close.tail(20).mean() > close.iloc[:-5].tail(20).mean()),
        "price_up": latest > previous,
        "return_20d_pct": _return_pct(performance_close, 20),
        "return_60d_pct": _return_pct(performance_close, 60),
        "return_120d_pct": _return_pct(performance_close, 120),
        "return_252d_pct": _return_pct(performance_close, 252),
        "high_252": high_252,
        "drawdown_252_pct": round(drawdown, 4) if drawdown is not None else None,
        "breakouts": breakouts,
        "adjusted_price_status": adjustment_status,
        "latest_date": pd.Timestamp(history["date"].iloc[-1]).date().isoformat(),
    }


def _breakout_context(history: pd.DataFrame, window: int, lookback: int, confirmation_days: int) -> dict[str, Any]:
    if len(history) <= window:
        return {"status": "insufficient", "window": window}
    close = pd.to_numeric(history["close"], errors="coerce")
    high = pd.to_numeric(history["high"], errors="coerce")
    prior_high = high.shift(1).rolling(window, min_periods=window).max()
    flags = close > prior_high
    positions = [int(position) for position in range(max(window, len(history) - lookback), len(history)) if bool(flags.iloc[position])]
    if not positions:
        near_pct = (1.0 - float(close.iloc[-1]) / float(prior_high.iloc[-1])) * 100.0 if pd.notna(prior_high.iloc[-1]) and prior_high.iloc[-1] > 0 else None
        return {"status": "none", "window": window, "near_high_pct": near_pct}
    position = positions[-1]
    level = float(prior_high.iloc[position])
    holding = False
    # A stock can print several consecutive new highs.  Treat that sequence as
    # one breakout wave and retain the earliest breakout that has stayed valid.
    for candidate_position in positions:
        candidate_level = float(prior_high.iloc[candidate_position])
        if bool((close.iloc[candidate_position:] >= candidate_level).all()):
            position = candidate_position
            level = candidate_level
            holding = True
            break
    days_since = len(history) - 1 - position
    confirmed = bool(holding and days_since >= max(0, confirmation_days - 1))
    return {
        "status": "confirmed" if confirmed else "holding" if holding else "failed",
        "window": window,
        "breakout_date": pd.Timestamp(history["date"].iloc[position]).date().isoformat(),
        "breakout_level": level,
        "days_since": days_since,
        "current_breakout": bool(flags.iloc[-1]),
        "holding": holding,
        "confirmed": confirmed,
    }


def _return_pct(values: pd.Series, sessions: int) -> float | None:
    clean = pd.to_numeric(values, errors="coerce").dropna()
    if len(clean) <= sessions or clean.iloc[-sessions - 1] <= 0:
        return None
    return round((float(clean.iloc[-1]) / float(clean.iloc[-sessions - 1]) - 1.0) * 100.0, 4)


def _load_formal_peer_groups() -> dict[str, str]:
    path = ROOT_DIR / "config" / "company_theme_map.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}
    groups: dict[str, str] = {}
    for code, entry in payload.items() if isinstance(payload, dict) else []:
        if not isinstance(entry, dict):
            continue
        primary = str(entry.get("primary_theme") or "").strip()
        statuses = entry.get("theme_statuses") if isinstance(entry.get("theme_statuses"), dict) else {}
        if primary and str(statuses.get(primary) or "formal").lower() != "candidate":
            groups[str(code)] = primary
    return groups


def _attach_peer_metrics(candidates: list[LaoXiaoCandidate], min_peer_count: int) -> None:
    industry_counts = Counter(item.industry for item in candidates if item.industry and "未分類" not in item.industry)
    fine_counts = Counter(
        (item.industry, item.features.get("formal_theme_group"))
        for item in candidates
        if item.industry and item.features.get("formal_theme_group")
    )
    groups: dict[str, list[LaoXiaoCandidate]] = {}
    for item in candidates:
        theme = item.features.get("formal_theme_group")
        fine_key = (item.industry, theme)
        if theme and fine_counts[fine_key] >= min_peer_count + 1:
            item.peer_group = f"{item.industry}/{theme}"
            item.peer_group_source = "official_industry+formal_theme"
        else:
            item.peer_group = item.industry
            item.peer_group_source = "official_industry"
        groups.setdefault(item.peer_group, []).append(item)

    for group_items in groups.values():
        peer_count = len(group_items) - 1
        for item in group_items:
            item.peer_count = peer_count
        frame = pd.DataFrame(
            {
                "code": [item.code for item in group_items],
                "r20": [item.features.get("return_20d_pct") for item in group_items],
                "r60": [item.features.get("return_60d_pct") for item in group_items],
                "r120": [item.features.get("return_120d_pct") for item in group_items],
                "drawdown": [item.features.get("drawdown_252_pct") for item in group_items],
            }
        ).set_index("code")
        percentiles: dict[str, pd.Series] = {}
        for column in ("r20", "r60", "r120"):
            percentiles[column] = frame[column].rank(method="average", pct=True) * 100.0
        percentiles["drawdown"] = (-frame["drawdown"]).rank(method="average", pct=True) * 100.0

        breakout_items = []
        for item in group_items:
            contexts = item.features.get("breakouts") or {}
            valid = [
                context for key, context in contexts.items()
                if key in {"60", "252"} and context.get("holding") and context.get("breakout_date")
            ]
            if valid:
                breakout_items.append((item.code, min(context["breakout_date"] for context in valid)))
        breakout_items.sort(key=lambda pair: (pair[1], pair[0]))
        breakout_percentile = {
            code: 100.0 * (len(breakout_items) - index) / max(1, len(breakout_items))
            for index, (code, _breakout_date) in enumerate(breakout_items)
        }

        for item in group_items:
            values = [
                (percentiles["r20"].get(item.code), 0.30),
                (percentiles["r60"].get(item.code), 0.40),
                (percentiles["r120"].get(item.code), 0.30),
            ]
            valid_values = [(float(value), weight) for value, weight in values if pd.notna(value)]
            rs = sum(value * weight for value, weight in valid_values) / sum(weight for _, weight in valid_values) if valid_values else None
            item.features["relative_strength_percentile"] = round(rs, 4) if rs is not None else None
            item.features["return_120d_percentile"] = _safe_rank_value(percentiles["r120"].get(item.code))
            item.features["drawdown_percentile"] = _safe_rank_value(percentiles["drawdown"].get(item.code))
            item.features["first_breakout_percentile"] = breakout_percentile.get(item.code)
            item.features["peer_group_valid"] = peer_count >= min_peer_count and industry_counts[item.industry] >= min_peer_count + 1


def _score_market_stage(
    item: LaoXiaoCandidate,
    hard: dict[str, Any],
    thresholds: dict[str, Any],
    weights: dict[str, Any],
    risk_result: MarketRiskResult,
) -> None:
    features = item.features
    rs = _float_or_none(features.get("relative_strength_percentile"))
    drawdown = _float_or_none(features.get("drawdown_252_pct"))
    breakouts = features.get("breakouts") or {}
    active_breakouts = [context for context in breakouts.values() if context.get("holding")]
    breakout_ready = bool(active_breakouts)
    top_rs = float(thresholds.get("relative_strength_top_percentile") or 75)
    watch_rs = float(thresholds.get("relative_strength_watch_percentile") or 60)
    severe_drawdown = float(thresholds.get("drawdown_severe_pct") or 30)

    if features.get("peer_group_valid") and rs is not None and rs >= top_rs and features.get("above_ma20") and breakout_ready:
        item.setup_type = "strong_breakout"
    elif (
        features.get("peer_group_valid")
        and rs is not None
        and rs >= watch_rs
        and drawdown is not None
        and drawdown <= severe_drawdown
        and features.get("above_ma60")
        and (features.get("reclaim_ma20") or features.get("reclaim_ma21"))
        and (_float_or_none(features.get("return_120d_pct")) or 0) > 0
    ):
        item.setup_type = "pullback_reclaim"

    item.component_scores["technical_stage"] = _score_technical_stage(item, thresholds)
    item.component_scores["sector_leadership"] = _score_sector_leadership(item, thresholds)
    item.component_scores["volume_liquidity"] = _score_volume_liquidity(item, thresholds)
    item.component_scores["risk_data_quality"] = _score_risk_quality(item, hard, thresholds, risk_result)
    item.component_scores.setdefault("fundamental_support", 0)
    item.component_scores.setdefault("theme_catalyst", 0)


def _score_technical_stage(item: LaoXiaoCandidate, thresholds: dict[str, Any]) -> int:
    f = item.features
    score = 0.0
    if f.get("above_ma20"):
        score += 3
    if f.get("above_ma60"):
        score += 3
    if f.get("ma20_above_ma60"):
        score += 2
    if f.get("ma20_rising"):
        score += 2
    if item.setup_type == "strong_breakout":
        contexts = [value for value in (f.get("breakouts") or {}).values() if value.get("holding")]
        windows = {int(value.get("window") or 0) for value in contexts}
        score += 8 if 252 in windows else 6 if 60 in windows else 4
        score += 6 if any(value.get("confirmed") for value in contexts) else 3
        score += 3 if any(value.get("current_breakout") for value in contexts) else 2
    elif item.setup_type == "pullback_reclaim":
        score += 10 if f.get("reclaim_ma20") or f.get("reclaim_ma21") else 0
        drawdown = _float_or_none(f.get("drawdown_252_pct"))
        if drawdown is not None and drawdown <= float(thresholds.get("drawdown_warning_pct") or 20):
            score += 6
        elif drawdown is not None and drawdown <= float(thresholds.get("drawdown_severe_pct") or 30):
            score += 3
    if (_float_or_none(f.get("return_20d_pct")) or 0) > 0:
        score += 2
    if (_float_or_none(f.get("return_60d_pct")) or 0) > 0:
        score += 2
    return min(30, int(round(score)))


def _score_sector_leadership(item: LaoXiaoCandidate, thresholds: dict[str, Any]) -> int:
    if not item.features.get("peer_group_valid"):
        item.risks.append("同業樣本不足，未列正式領先判斷")
        return 0
    rs = _float_or_none(item.features.get("relative_strength_percentile")) or 0
    score = 12 if rs >= 90 else 10 if rs >= 75 else 6 if rs >= 60 else 2 if rs >= 50 else 0
    first = _float_or_none(item.features.get("first_breakout_percentile"))
    if first is not None:
        score += 4 if first >= 75 else 2 if first >= 50 else 0
    if (_float_or_none(item.features.get("return_120d_percentile")) or 0) >= 75:
        score += 2
    if (_float_or_none(item.features.get("drawdown_percentile")) or 0) >= 75:
        score += 2
    if rs >= float(thresholds.get("relative_strength_top_percentile") or 75):
        item.reasons.append("同業相對強弱居前段")
    if first is not None and first >= 75:
        item.reasons.append("同業中較早創高且仍守住突破")
    return min(20, int(round(score)))


def _score_volume_liquidity(item: LaoXiaoCandidate, thresholds: dict[str, Any]) -> int:
    score = 5.0
    ratio = _float_or_none(item.features.get("volume_ratio"))
    if ratio is not None and ratio >= float(thresholds.get("volume_ratio_confirm") or 1.2) and item.features.get("price_up"):
        score += 6
        item.reasons.append("價漲且成交量高於 20 日均量")
    elif ratio is not None and ratio >= 1.0:
        score += 3
    up_down = _float_or_none(item.features.get("up_down_volume_ratio"))
    if up_down is not None and up_down >= 1.2:
        score += 4
    if ratio is not None and ratio >= float(thresholds.get("volume_ratio_hot") or 3):
        item.risks.append("單日量能超過 20 日均量三倍，留意短線過熱")
    return min(15, int(round(score)))


def _score_risk_quality(
    item: LaoXiaoCandidate,
    hard: dict[str, Any],
    thresholds: dict[str, Any],
    risk_result: MarketRiskResult,
) -> int:
    score = 0
    if item.history_rows >= int(hard.get("full_history_rows") or 240):
        score += 2
    else:
        item.risks.append("日線介於 120 至 239 日，正式分數上限 75")
        item.score_cap = 75
    if item.adjusted_price_status == "adjusted":
        score += 1
    else:
        item.risks.append("缺少還原價，報酬暫以原始收盤價計算")
        item.score_cap = min(item.score_cap or 100, 90)
    drawdown = _float_or_none(item.features.get("drawdown_252_pct"))
    if drawdown is not None and drawdown <= float(thresholds.get("drawdown_warning_pct") or 20):
        score += 2
    elif drawdown is not None and drawdown > float(thresholds.get("drawdown_severe_pct") or 30):
        item.risks.append("距一年高點回撤超過 30%")
    if item.market_risk.get("attention"):
        score = max(0, score - 1)
        item.risks.append("官方公布注意股票")
    if item.market_risk.get("disposition"):
        score = max(0, score - 2)
        item.risks.append("官方公布處置股票")
    if not risk_result.complete:
        item.risks.append("官方注意／處置資料源不完整")
    return min(5, int(score))


def _attach_fundamental_and_theme_scores(
    candidates: list[LaoXiaoCandidate],
    universe: list[StockUniverseEntry],
    target_date: date,
    weights: dict[str, Any],
    emit: Callable[[str], None],
) -> None:
    if not candidates:
        return
    try:
        history_map = load_recent_revenue_history(universe, months_to_fetch=24, as_of_date=target_date)
    except Exception:
        history_map = {}
    for item in candidates:
        item.revenue_history = _normalise_revenue_history(history_map.get(item.code) or [])

    from stock_ai_bot.monitoring.radar_service import RadarCandidate, _attach_local_news, prepare_radar_scoring_data, score_radar_candidates

    radar_candidates = [
        RadarCandidate(
            code=item.code,
            name=item.name,
            symbol=item.symbol,
            industry=item.industry,
            price=item.price,
            source_labels=[SCAN_TYPE, item.setup_type],
            revenue_history=list(item.revenue_history),
        )
        for item in candidates
    ]
    _attach_local_news(radar_candidates, target_date)
    prepare_radar_scoring_data(
        radar_candidates,
        target_date,
        emit,
        financial_fetch_limit=len(radar_candidates),
        scoring_version="v3",
    )
    score_radar_candidates(radar_candidates, target_date, scoring_version="v3", reason_limit=8, risk_limit=8)
    radar_by_code = {item.code: item for item in radar_candidates}

    for item in candidates:
        radar_item = radar_by_code[item.code]
        revenue_detail = dict(radar_item.score_details.get("revenue") or {})
        financial_detail = dict(radar_item.score_details.get("financial") or {})
        revenue_score = int(revenue_detail.get("score") or 0)
        financial_score = int(financial_detail.get("score") or 0)
        financial_status = str((financial_detail.get("details") or {}).get("data_status") or "unknown")
        revenue_status = str((revenue_detail.get("details") or {}).get("data_status") or "unknown")
        denominator = 20 if financial_status == "not_applicable" else 35
        custom_max = int(weights.get("fundamental_support") or 20)
        fundamental_score = int(round((revenue_score + financial_score) / denominator * custom_max)) if denominator else 0
        item.component_scores["fundamental_support"] = min(custom_max, fundamental_score)
        item.component_details["fundamental_support"] = {
            "unified_v3_revenue_score": revenue_score,
            "unified_v3_financial_score": financial_score,
            "revenue_status": revenue_status,
            "financial_status": financial_status,
        }
        item.reasons.extend((revenue_detail.get("reasons") or [])[:2])
        item.reasons.extend((financial_detail.get("reasons") or [])[:2])
        item.risks.extend((revenue_detail.get("risks") or [])[:2])
        item.risks.extend((financial_detail.get("risks") or [])[:2])
        if financial_status == "not_applicable":
            item.risks.append("金融業一般企業財報規則不適用，基本面分數僅依營收換算")

        structured = (radar_item.evidence_pack.get("research_structured_data") or {})
        item.theme_context = dict(structured.get("topic_context") or build_stock_topic_context(item.code, item.name))
        item.component_scores["theme_catalyst"] = _score_theme_catalyst(item, radar_item.news_items, weights)


def _attach_historical_fundamental_scores(
    candidates: list[LaoXiaoCandidate],
    universe: list[StockUniverseEntry],
    target_date: date,
    weights: dict[str, Any],
) -> None:
    """Use causal, locally reproducible facts for a historical replay.

    Current topic maps and live news databases are deliberately excluded.  The
    same unified v3 revenue/financial rules are applied, but unavailable
    historical statement fields receive no points rather than a fabricated
    neutral value.
    """

    if not candidates:
        return
    try:
        history_map = load_recent_revenue_history(universe, months_to_fetch=24, as_of_date=target_date)
    except Exception:
        history_map = {}

    from stock_ai_bot.scanning.stock_scanner import _load_historical_gross_margin_series
    from unified_financial_scoring import score_unified_financial, score_unified_revenue

    for item in candidates:
        item.revenue_history = _normalise_revenue_history(history_map.get(item.code) or [])
        margin_points = _load_historical_gross_margin_series(item.symbol, target_date)
        financial_rows = [
            {
                "Quarter": point.quarter,
                "Gross_Margin": point.gross_margin,
                "gross_margin": point.gross_margin,
            }
            for point in reversed(margin_points)
        ]
        snapshot = {
            "analysis_date": target_date.isoformat(),
            "stock": {"code": item.code, "name": item.name, "industry": item.industry},
            "revenue": {"history": item.revenue_history},
            "financial": {"financial_data": financial_rows},
        }
        revenue_detail = score_unified_revenue(item, snapshot)
        financial_detail = score_unified_financial(item, snapshot)
        revenue_score = int(revenue_detail.get("score") or 0)
        financial_score = int(financial_detail.get("score") or 0)
        financial_status = str((financial_detail.get("details") or {}).get("data_status") or "unknown")
        revenue_status = str((revenue_detail.get("details") or {}).get("data_status") or "unknown")
        denominator = 20 if financial_status == "not_applicable" else 35
        custom_max = int(weights.get("fundamental_support") or 20)
        converted = int(round((revenue_score + financial_score) / denominator * custom_max)) if denominator else 0
        item.component_scores["fundamental_support"] = min(custom_max, converted)
        item.component_scores["theme_catalyst"] = 0
        item.component_details["fundamental_support"] = {
            "unified_v3_revenue_score": revenue_score,
            "unified_v3_financial_score": financial_score,
            "revenue_status": revenue_status,
            "financial_status": financial_status,
            "historical_statement_scope": "gross_margin_only_when_available",
        }
        item.reasons.extend((revenue_detail.get("reasons") or [])[:2])
        item.reasons.extend((financial_detail.get("reasons") or [])[:2])
        item.risks.extend((revenue_detail.get("risks") or [])[:2])
        item.risks.extend((financial_detail.get("risks") or [])[:2])
        item.risks.append("歷史回放未使用目前題材對照表，題材分數為 0")


def _score_theme_catalyst(item: LaoXiaoCandidate, news_items: list[dict[str, Any]], weights: dict[str, Any]) -> int:
    max_score = int(weights.get("theme_catalyst") or 10)
    topics = item.theme_context.get("matched_topics") if isinstance(item.theme_context, dict) else []
    formal = [
        topic for topic in topics or []
        if isinstance(topic, dict)
        and topic.get("confidence") == "high"
        and topic.get("usage_policy") != "hypothesis_only"
        and not topic.get("not_representative")
    ]
    score = 0
    if formal:
        score += 3
        item.reasons.append("已有正式題材關聯")
    if any(str(topic.get("supply_chain_role") or "").strip() for topic in formal):
        score += 2
    latest_date = _date_or_none(item.features.get("latest_date")) or date.today()
    recent_cutoff = latest_date - timedelta(days=30)
    relevant_news = []
    for news in news_items:
        if not isinstance(news, dict) or not news.get("title"):
            continue
        published = _date_or_none(news.get("published_at"))
        if published is not None and published >= recent_cutoff:
            relevant_news.append(news)
    if relevant_news:
        score += 3
        item.reasons.append("本機新聞庫有近期催化事件")
    if len(relevant_news) >= 2:
        score += 2
    return min(max_score, score)


def _finalize_candidate_score(
    item: LaoXiaoCandidate,
    hard: dict[str, Any],
    thresholds: dict[str, Any],
    weights: dict[str, Any],
) -> None:
    for key in weights:
        item.component_scores.setdefault(key, 0)
    total = sum(int(item.component_scores.get(key) or 0) for key in weights)
    if item.score_cap is not None:
        total = min(total, item.score_cap)
    item.total_score = max(0, min(100, int(total)))
    if item.setup_type == "strong_breakout":
        item.reasons.insert(0, "相對強勢且突破後仍守住前高")
    elif item.setup_type == "pullback_reclaim":
        item.reasons.insert(0, "原強勢結構拉回後重新站回均線")


def _is_observation_candidate(item: LaoXiaoCandidate, thresholds: dict[str, Any]) -> bool:
    rs = _float_or_none(item.features.get("relative_strength_percentile")) or 0
    return bool(item.features.get("peer_group_valid") and rs >= float(thresholds.get("relative_strength_watch_percentile") or 60))


def _stage_one_sort_key(item: LaoXiaoCandidate) -> tuple[int, float, float, str]:
    subtotal = sum(item.component_scores.values())
    return (
        subtotal,
        _float_or_none(item.features.get("relative_strength_percentile")) or 0,
        -(_float_or_none(item.features.get("drawdown_252_pct")) or 999),
        item.code,
    )


def _normalise_revenue_history(points: Any) -> list[dict[str, Any]]:
    rows = []
    for point in points or []:
        month = getattr(point, "month", None) if not isinstance(point, dict) else point.get("month")
        if not month:
            continue
        rows.append(
            {
                "month": str(month),
                "revenue": _float_or_none(getattr(point, "revenue", None) if not isinstance(point, dict) else point.get("revenue")),
                "yoy": _float_or_none(getattr(point, "yoy", None) if not isinstance(point, dict) else point.get("yoy")),
                "published_at": getattr(point, "published_at", None) if not isinstance(point, dict) else point.get("published_at"),
                "published_at_source": getattr(point, "published_at_source", None) if not isinstance(point, dict) else point.get("published_at_source"),
            }
        )
    return sorted(rows, key=lambda row: row["month"])


def _float_or_none(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) else number


def _date_or_none(value: Any) -> date | None:
    text = str(value or "").strip()[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _safe_rank_value(value: Any) -> float | None:
    number = _float_or_none(value)
    return round(number, 4) if number is not None else None


def _fmt(value: Any) -> str:
    number = _float_or_none(value)
    if number is None:
        return "N/A"
    if number.is_integer():
        return str(int(number))
    return f"{number:.1f}"


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(str(value) for value in values if str(value).strip()))
