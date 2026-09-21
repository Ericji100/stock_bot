"""Replay the production `/scan 7` strategies over a historical date range.

The runner is intentionally side-effect isolated: it does not import Telegram,
write recent-scan state, or register a scheduled job.  Selection formulas and
production data services are reused directly and every run is checkpointed.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import sys
from typing import Any, Iterable

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import chip_strategies
import stock_ai_bot.selection.curated_scan_service as curated_scan_service
from stock_ai_bot.data_sources.historical_price_service import fetch_history, load_cached_history, prefetch_histories
import stock_ai_bot.selection.laoxiao_scan_service as laoxiao_scan_service
import stock_ai_bot.scanning.stock_scanner as stock_scanner
from stock_ai_bot.scanning.stock_scanner import ScanReport, load_stock_universe, scan_tw_market
import stock_ai_bot.scanning.technical_scanner as technical_scanner


STRATEGY_LABELS = {
    "financial": "財報營收選股",
    "chip_1": "60日法人動態",
    "chip_2": "投信認養股",
    "chip_3": "法人持股比例增加",
    "chip_4": "每週大戶持股",
    "technical": "技術面選股",
    "curated": "精選選股",
    "laoxiao": "老蕭選股",
}

CHIP_REASONS = {
    "chip_1": {
        "S": "今日法人買超，60日買超日數、近10日延續性與最大賣超均符合S級",
        "A": "今日法人買超，60日與近10日法人動態符合A級",
        "B": "今日法人買超，60日及近10日買超日數符合B級",
    },
    "chip_2": {
        "S": "今日投信買超，前段持股低檔且近15日認養日數符合S級",
        "A": "今日投信買超，前段持股低檔且近20日認養日數符合A級",
        "B": "今日投信買超，前段持股低檔且近10日出現買超，符合B級",
    },
    "chip_3": {
        "S": "今日法人買超，60日估計持股比例增幅、距高點與回落均符合S級",
        "A": "今日法人買超，60日估計持股比例增幅、距高點與回落均符合A級",
        "B": "今日法人買超，60日估計持股比例增加且高於20日平均，符合B級",
    },
    "chip_4": {
        "S": "大戶與散戶近週持股變化符合S級",
        "A": "大戶持股連續增加且散戶下降，符合A級",
        "B": "大戶單週明顯增加且散戶下降，符合B級",
    },
}


def _json_default(value: Any) -> Any:
    if isinstance(value, (date, datetime, pd.Timestamp)):
        return value.isoformat()
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    return str(value)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default, allow_nan=False),
        encoding="utf-8",
    )
    temporary.replace(path)


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return default


def _load_scan_settings() -> dict[str, Any]:
    payload = _read_json(ROOT / "config.json", {})
    return dict(payload.get("scan_settings") or {})


def _trading_dates(start_date: date, end_date: date) -> tuple[list[date], str]:
    for symbol, label in (("^TWII", "Yahoo Finance TAIEX"), ("0050.TW", "Yahoo Finance 0050")):
        frame, _ = fetch_history(symbol, end_date, min_rows=20, lookback_days=max(560, (end_date - start_date).days + 500))
        if frame.empty:
            continue
        dates = sorted(
            value
            for value in frame["date"].dt.date.unique().tolist()
            if start_date <= value <= end_date
        )
        if dates:
            return dates, label
    fallback = [value.date() for value in pd.bdate_range(start_date, end_date)]
    return fallback, "工作日備援（指數日線不可用）"


def _chip_trading_calendar(end_date: date) -> list[date]:
    frame, _ = fetch_history("^TWII", end_date, min_rows=60, lookback_days=800)
    if frame.empty:
        return [value.date() for value in pd.bdate_range(end_date - timedelta(days=800), end_date)]
    return sorted({value for value in frame["date"].dt.date.unique().tolist() if value <= end_date})


def _event_key(item: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(item.get("code") or ""),
        str(item.get("date") or ""),
        str(item.get("strategy") or ""),
        str(item.get("signal") or ""),
    )


def _append_event(events: list[dict[str, Any]], seen: set[tuple[str, str, str, str]], item: dict[str, Any]) -> None:
    key = _event_key(item)
    if not key[0] or key in seen:
        return
    seen.add(key)
    events.append(item)


def _base_event(code: str, target: date, strategy: str, stock_map: dict[str, Any]) -> dict[str, Any]:
    stock = stock_map.get(code)
    return {
        "code": code,
        "name": getattr(stock, "name", "") if stock is not None else "",
        "industry": getattr(stock, "industry", "") if stock is not None else "",
        "date": target.isoformat(),
        "strategy": strategy,
        "strategy_label": STRATEGY_LABELS[strategy],
    }


def _collect_financial_events(
    report: ScanReport,
    target: date,
    stock_map: dict[str, Any],
    events: list[dict[str, Any]],
    seen: set[tuple[str, str, str, str]],
) -> None:
    for item in report.candidates:
        group = "G1" if item.revenue_group == "group_1" else "G2"
        latest = item.revenue_history[0] if item.revenue_history else None
        reason = (
            f"營收{group}；毛利率{item.gross_margin_rating}級；"
            f"股價{item.price:.2f}；20日均量{item.avg_volume_20d:.0f}張"
        )
        event = {
            **_base_event(item.code, target, "financial", stock_map),
            "signal": f"{group}/毛利率{item.gross_margin_rating}",
            "reason": reason,
            "metrics": {
                "price": item.price,
                "avg_volume_20d_lots": item.avg_volume_20d,
                "latest_monthly_revenue": item.latest_monthly_revenue,
                "latest_revenue_month": getattr(latest, "month", None),
                "latest_revenue_yoy_pct": getattr(latest, "yoy", None),
                "gross_margin_quarters": [asdict(point) for point in item.gross_margins],
            },
        }
        _append_event(events, seen, event)


def _collect_chip_events(
    grade_maps: dict[str, dict[str, str]],
    target: date,
    stock_map: dict[str, Any],
    events: list[dict[str, Any]],
    seen: set[tuple[str, str, str, str]],
) -> None:
    for strategy, grades in grade_maps.items():
        for code, grade in grades.items():
            event = {
                **_base_event(str(code), target, strategy, stock_map),
                "signal": f"{grade}級",
                "reason": CHIP_REASONS.get(strategy, {}).get(str(grade), f"符合{grade}級規則"),
                "metrics": {"grade": grade},
            }
            _append_event(events, seen, event)


def _technical_code_map(result: technical_scanner.TechnicalScanResult) -> dict[str, set[str]]:
    by_signal: dict[str, set[str]] = {}
    for signal, industries in result.bullish.items():
        codes: set[str] = set()
        for displays in industries.values():
            for display in displays:
                code = str(display).strip().split(maxsplit=1)[0]
                if code:
                    codes.add(code)
        by_signal[signal] = codes
    return by_signal


def _collect_technical_events(
    result: technical_scanner.TechnicalScanResult,
    target: date,
    stock_map: dict[str, Any],
    events: list[dict[str, Any]],
    seen: set[tuple[str, str, str, str]],
) -> None:
    for signal, codes in _technical_code_map(result).items():
        for code in codes:
            _append_event(
                events,
                seen,
                {
                    **_base_event(code, target, "technical", stock_map),
                    "signal": signal,
                    "reason": f"當日正式技術掃描偵測：{signal}",
                    "metrics": {"signal_family": "bullish"},
                },
            )
    for strategy_code, signals in result.strategy_signals.items():
        for raw in signals:
            code = str(raw.get("code") or raw.get("stock_code") or "")
            if not code:
                continue
            label = str(raw.get("strategy_name") or raw.get("signal_name") or raw.get("setup_name") or f"技術策略{strategy_code}")
            details = {
                key: value
                for key, value in raw.items()
                if key not in {"code", "stock_code", "name", "stock_name", "industry"}
            }
            _append_event(
                events,
                seen,
                {
                    **_base_event(code, target, "technical", stock_map),
                    "signal": f"策略{strategy_code}：{label}",
                    "reason": str(raw.get("reason") or raw.get("notes") or label),
                    "metrics": details,
                },
            )


def _collect_curated_events(
    result: curated_scan_service.CuratedScanResult,
    target: date,
    stock_map: dict[str, Any],
    events: list[dict[str, Any]],
    seen: set[tuple[str, str, str, str]],
) -> None:
    signals_by_code: dict[str, list[str]] = defaultdict(list)
    for signal, codes in result.selected_by_signal.items():
        for code in codes:
            signals_by_code[str(code)].append(signal)
    for code in result.selected_codes:
        hits = list(result.hits.get(code) or [])
        signals = signals_by_code.get(code) or []
        _append_event(
            events,
            seen,
            {
                **_base_event(code, target, "curated", stock_map),
                "signal": "、".join(signals) or "交叉命中",
                "reason": f"技術觸發：{'、'.join(signals)}；交叉策略：{'、'.join(hits)}",
                "metrics": {"technical_signals": signals, "cross_hits": hits, "radar_ai_score": "skipped"},
            },
        )


def _collect_laoxiao_events(
    result: laoxiao_scan_service.LaoXiaoScanResult,
    target: date,
    stock_map: dict[str, Any],
    events: list[dict[str, Any]],
    seen: set[tuple[str, str, str, str]],
) -> None:
    setup_labels = {"strong_breakout": "強勢突破型", "pullback_reclaim": "拉回轉強型"}
    for item in result.candidates:
        setup = setup_labels.get(item.setup_type, item.setup_type)
        _append_event(
            events,
            seen,
            {
                **_base_event(item.code, target, "laoxiao", stock_map),
                "signal": setup,
                "reason": "；".join(item.reasons[:6]) or setup,
                "risks": list(item.risks[:6]),
                "metrics": {
                    "rule_score": item.total_score,
                    "components": dict(item.component_scores),
                    "relative_strength_percentile": item.features.get("relative_strength_percentile"),
                    "drawdown_252_pct": item.features.get("drawdown_252_pct"),
                    "volume_ratio": item.features.get("volume_ratio"),
                    "peer_count": item.peer_count,
                },
            },
        )


def _escape(value: Any) -> str:
    text = str(value if value is not None else "")
    return text.replace("|", "\\|").replace("\r", " ").replace("\n", " ").strip()


def _compact_metrics(metrics: dict[str, Any]) -> str:
    preferred = (
        "grade",
        "price",
        "avg_volume_20d_lots",
        "latest_revenue_yoy_pct",
        "rule_score",
        "relative_strength_percentile",
        "volume_ratio",
    )
    labels = {
        "grade": "等級",
        "price": "股價",
        "avg_volume_20d_lots": "均量",
        "latest_revenue_yoy_pct": "營收YoY%",
        "rule_score": "規則分",
        "relative_strength_percentile": "相對強弱百分位",
        "volume_ratio": "量比",
    }
    parts = []
    for key in preferred:
        value = metrics.get(key)
        if value is None or value == "":
            continue
        if isinstance(value, float):
            value = round(value, 2)
        parts.append(f"{labels[key]}={value}")
    return "；".join(parts)


def _render_markdown(payload: dict[str, Any]) -> str:
    events = list(payload.get("events") or [])
    diagnostics = list(payload.get("diagnostics") or [])
    events.sort(key=lambda item: (item["code"], item["date"], item["strategy"], item["signal"]))
    by_code: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in events:
        by_code[item["code"]].append(item)

    strategy_counts = Counter(item["strategy"] for item in events)
    lines = [
        "# 全部選股歷史重播去重名單",
        "",
        f"- 模擬期間：{payload['start_date']} ～ {payload['end_date']}",
        f"- 產生時間：{payload.get('generated_at')}",
        f"- 交易日來源：{payload.get('trading_calendar_source')}",
        f"- 完成交易日：{len(payload.get('completed_dates') or [])}/{len(payload.get('trading_dates') or [])}",
        f"- 去重股票數：{len(by_code)}",
        f"- 原始去重事件數：{len(events)}",
        "- 執行範圍：財報營收、籌碼1～4、技術面、精選、老蕭選股。",
        "- AI處理：未呼叫語言模型；精選 Radar/AI 排序分跳過。老蕭保留策略本身必要的規則分。",
        "- 歷史隔離：沒有發送 Telegram、沒有寫入最近選股狀態、沒有修改排程。",
        "",
        "## 資料與限制",
        "",
        "- 股價使用獨立歷史日線快取，所有日線均截到模擬日。",
        "- 月營收依法定次月10日作為保守可用日；季報依法定申報截止日作為保守可用日。",
        "- 籌碼策略3在歷史回放使用正式程式既有的『法人買賣超累積估算持股比例』備援路徑，避免逐日讀取目前持股快照。",
        "- TDCC公開批次來源沒有本機留存2023快照時，策略4仍有執行，但不會以2026資料代替，結果記為資料不足。",
        "- 官方注意／處置OpenAPI不是完整歷史資料庫；沒有2023快取時記為歷史資料不可用，不代表當時沒有風險標記。",
        "- 股票母體沿用目前機器人的股票清單；已下市且不在目前清單內的公司可能遺漏，存在倖存者偏誤。",
        "- 歷史季報輕量路徑目前正式使用可取得的毛利率欄位；其他無法可靠回到當時的財報欄位不給分。",
        "",
        "## 策略命中統計",
        "",
        "| 策略 | 事件數 | 股票數 |",
        "|---|---:|---:|",
    ]
    for key in STRATEGY_LABELS:
        codes = {item["code"] for item in events if item["strategy"] == key}
        lines.append(f"| {STRATEGY_LABELS[key]} | {strategy_counts.get(key, 0)} | {len(codes)} |")

    lines.extend(
        [
            "",
            "## 去重股票總表",
            "",
            "| 股票 | 產業 | 首次選中 | 最後選中 | 選中交易日 | 事件數 | 命中策略 |",
            "|---|---|---|---|---:|---:|---|",
        ]
    )
    summary_rows = []
    for code, stock_events in by_code.items():
        dates = sorted({item["date"] for item in stock_events})
        strategies = sorted({item["strategy"] for item in stock_events}, key=lambda key: list(STRATEGY_LABELS).index(key))
        summary_rows.append(
            (
                -len(strategies),
                -len(dates),
                code,
                stock_events,
                dates,
                strategies,
            )
        )
    for _strategy_count, _day_count, code, stock_events, dates, strategies in sorted(summary_rows):
        first = stock_events[0]
        stock_label = f"{code} {first.get('name') or ''}".strip()
        lines.append(
            f"| {_escape(stock_label)} | {_escape(first.get('industry') or '未分類')} | {dates[0]} | {dates[-1]} | "
            f"{len(dates)} | {len(stock_events)} | {_escape('、'.join(STRATEGY_LABELS[key] for key in strategies))} |"
        )

    lines.extend(["", "## 各股票選中明細", ""])
    for _strategy_count, _day_count, code, stock_events, dates, strategies in sorted(summary_rows):
        first = stock_events[0]
        lines.extend(
            [
                f"### {code} {_escape(first.get('name') or '')}",
                "",
                f"產業：{_escape(first.get('industry') or '未分類')}；選中 {len(dates)} 個交易日；命中策略："
                + "、".join(STRATEGY_LABELS[key] for key in strategies),
                "",
                "| 日期 | 策略 | 訊號／等級 | 選中原因 | 關鍵數值 |",
                "|---|---|---|---|---|",
            ]
        )
        for item in stock_events:
            lines.append(
                f"| {item['date']} | {_escape(item['strategy_label'])} | {_escape(item['signal'])} | "
                f"{_escape(item.get('reason'))} | {_escape(_compact_metrics(item.get('metrics') or {}))} |"
            )
        lines.append("")

    failed = list(payload.get("failures") or [])
    lines.extend(["## 執行稽核", ""])
    lines.append(f"- 重複事件鍵：{payload.get('validation', {}).get('duplicate_keys', 0)}")
    lines.append(f"- 範圍外事件：{payload.get('validation', {}).get('out_of_range_events', 0)}")
    lines.append(f"- 失敗交易日／階段：{len(failed)}")
    if failed:
        lines.extend(["", "| 日期 | 階段 | 錯誤 |", "|---|---|---|"])
        for item in failed:
            lines.append(f"| {_escape(item.get('date'))} | {_escape(item.get('stage'))} | {_escape(item.get('error'))} |")

    chip4_days = sum(1 for item in diagnostics if int(item.get("chip_4_count") or 0) == 0)
    lines.extend(
        [
            "",
            f"- 策略4無命中或無歷史TDCC資料的交易日：{chip4_days}/{len(diagnostics)}",
            f"- 原始事件JSON：`{payload.get('event_json_name')}`",
            "",
        ]
    )
    return "\n".join(lines)


def _validate(payload: dict[str, Any]) -> dict[str, int]:
    events = list(payload.get("events") or [])
    keys = [_event_key(item) for item in events]
    start = date.fromisoformat(payload["start_date"])
    end = date.fromisoformat(payload["end_date"])
    return {
        "duplicate_keys": len(keys) - len(set(keys)),
        "out_of_range_events": sum(
            not (start <= date.fromisoformat(str(item["date"])) <= end)
            for item in events
        ),
    }


def _memoize_issued_shares(universe: list[Any]) -> tuple[Any, dict[str, float]]:
    original = chip_strategies._load_issued_shares_map
    shares = original(universe)

    def cached(entries: list[Any]) -> dict[str, float]:
        return {entry.code: shares[entry.code] for entry in entries if entry.code in shares}

    chip_strategies._load_issued_shares_map = cached
    return original, shares


def _install_daily_input_memoization() -> tuple[dict[tuple[Any, ...], Any], dict[str, Any]]:
    """Share immutable point-in-time inputs within one replay date.

    Financial, chip, technical and LaoXiao services independently request the
    same revenue and price snapshots.  The wrappers preserve their production
    calls while avoiding repeated construction during a single replay day.
    The caller clears the returned cache before every date.
    """

    originals = {
        "price": stock_scanner.load_price_metrics,
        "revenue": stock_scanner.load_recent_revenue_history,
    }
    daily_cache: dict[tuple[Any, ...], Any] = {}

    def cached_prices(universe: list[Any], *args: Any, **kwargs: Any) -> Any:
        as_of = kwargs.get("as_of_date", args[3] if len(args) >= 4 else None)
        if as_of is None:
            return originals["price"](universe, *args, **kwargs)
        key = ("price", as_of)
        if key not in daily_cache:
            daily_cache[key] = originals["price"](universe, *args, **kwargs)
        return daily_cache[key]

    def cached_revenue(universe: list[Any], *args: Any, **kwargs: Any) -> Any:
        months = kwargs.get("months_to_fetch", args[0] if args else 24)
        as_of = kwargs.get("as_of_date", args[1] if len(args) >= 2 else None)
        if as_of is None:
            return originals["revenue"](universe, *args, **kwargs)
        key = ("revenue", int(months), as_of)
        if key not in daily_cache:
            daily_cache[key] = originals["revenue"](universe, *args, **kwargs)
        return daily_cache[key]

    stock_scanner.load_price_metrics = cached_prices
    stock_scanner.load_recent_revenue_history = cached_revenue
    chip_strategies.load_price_metrics = cached_prices
    chip_strategies.load_recent_revenue_history = cached_revenue
    technical_scanner.load_price_metrics = cached_prices
    technical_scanner.load_recent_revenue_history = cached_revenue
    laoxiao_scan_service.load_recent_revenue_history = cached_revenue
    return daily_cache, originals


def _restore_daily_input_loaders(originals: dict[str, Any]) -> None:
    stock_scanner.load_price_metrics = originals["price"]
    stock_scanner.load_recent_revenue_history = originals["revenue"]
    chip_strategies.load_price_metrics = originals["price"]
    chip_strategies.load_recent_revenue_history = originals["revenue"]
    technical_scanner.load_price_metrics = originals["price"]
    technical_scanner.load_recent_revenue_history = originals["revenue"]
    laoxiao_scan_service.load_recent_revenue_history = originals["revenue"]


def _prefetch_chip_history(
    trading_dates: list[date],
    chip_calendar: list[date],
    settings: dict[str, Any],
    universe: list[Any],
    earliest_trading_dates: dict[str, date],
) -> dict[str, Any]:
    # The official TWSE/TPEX daily endpoints return a market-wide table.  Use
    # the current bot universe for this one-time cache fill instead of running
    # the hard filter once for every replay date merely to build a code union.
    # Each actual replay day still executes the production hard filter and only
    # evaluates that day's eligible candidates.
    issued_shares = chip_strategies._load_issued_shares_map(universe)
    rows: list[dict[str, Any]] = []
    for entry in universe:
        shares = issued_shares.get(entry.code)
        first_trading_date = earliest_trading_dates.get(entry.code)
        if not shares or first_trading_date is None:
            continue
        rows.append(
            {
                "code": entry.code,
                "market": entry.market,
                "issued_shares": shares,
            }
        )
    candidates = pd.DataFrame(rows)
    if candidates.empty:
        return {"candidate_union": 0, "anchors": []}
    candidates.attrs["total_symbols"] = len(universe)
    candidates.attrs["scan_settings"] = settings
    anchors = [
        (trading_dates[0], chip_strategies.TARGET_DAILY_TRADING_DAYS),
        (trading_dates[-1], chip_strategies.TRADING_DAY_LOOKBACK),
    ]
    completed = []
    for target, days in anchors:
        print(f"[歷史重播] 預抓法人資料：截至 {target}，目標 {days} 個交易日", flush=True)
        frame, latest = chip_strategies._fetch_recent_daily_chip_data(
            target,
            candidates,
            progress_label=f"歷史籌碼預抓 {target}",
            target_trading_days=days,
            include_foreign_ratio=False,
            scope="historical_scan",
            trading_calendar=chip_calendar,
            earliest_trading_dates=earliest_trading_dates,
        )
        completed.append(
            {
                "target": target.isoformat(),
                "requested_days": days,
                "rows": len(frame),
                "latest": latest.isoformat() if latest else None,
            }
        )
    return {"candidate_union": len(candidates), "anchors": completed}


def replay(
    start_date: date,
    end_date: date,
    *,
    output_dir: Path,
    resume: bool = True,
    prefetch_chip: bool = True,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_path = output_dir / "checkpoint.json"
    event_json_path = output_dir / f"all_scan_{start_date}_{end_date}_events.json"
    markdown_path = output_dir / f"all_scan_{start_date}_{end_date}_deduplicated.md"
    payload = _read_json(checkpoint_path, {}) if resume else {}
    universe = load_stock_universe(False)
    stock_map = {entry.code: entry for entry in universe}
    settings = _load_scan_settings()

    trading_dates, calendar_source = _trading_dates(start_date, end_date)
    chip_calendar = _chip_trading_calendar(end_date)
    if not trading_dates:
        raise RuntimeError("指定期間沒有可確認的台股交易日")
    print(f"[歷史重播] 交易日 {len(trading_dates)} 天，來源 {calendar_source}", flush=True)

    history_start = start_date - timedelta(days=600)
    price_stats = prefetch_histories(
        [entry.symbol for entry in universe],
        start_date=history_start,
        end_date=end_date,
        chunk_size=40,
        retry_workers=4,
        progress=lambda message: print(f"[歷史重播] {message}", flush=True),
    )
    print(f"[歷史重播] 日線快取結果：{price_stats}", flush=True)

    earliest_trading_dates: dict[str, date] = {}
    for entry in universe:
        history = load_cached_history(entry.symbol, end_date)
        if not history.empty:
            earliest_trading_dates[entry.code] = history["date"].dt.date.min()

    original_issued_loader, issued_shares = _memoize_issued_shares(universe)
    chip_prefetch_stats: dict[str, Any] = {}
    daily_input_cache: dict[tuple[Any, ...], Any] = {}
    original_input_loaders: dict[str, Any] | None = None
    try:
        if prefetch_chip and not payload.get("chip_prefetch_complete"):
            chip_prefetch_stats = _prefetch_chip_history(
                trading_dates,
                chip_calendar,
                settings,
                universe,
                earliest_trading_dates,
            )
            payload["chip_prefetch_complete"] = True
            payload["chip_prefetch_stats"] = chip_prefetch_stats
            _write_json(checkpoint_path, payload)

        daily_input_cache, original_input_loaders = _install_daily_input_memoization()

        events = list(payload.get("events") or [])
        seen = {_event_key(item) for item in events}
        completed_dates = set(payload.get("completed_dates") or [])
        diagnostics = list(payload.get("diagnostics") or [])
        failures = list(payload.get("failures") or [])

        for index, target in enumerate(trading_dates, start=1):
            if target.isoformat() in completed_dates:
                continue
            daily_input_cache.clear()
            print(f"[歷史重播] {index}/{len(trading_dates)} 執行 {target}", flush=True)
            day_errors: list[dict[str, str]] = []
            financial_report: ScanReport | None = None
            chip_context = None
            technical_result = None
            curated_result = None
            laoxiao_result = None

            try:
                financial_report = scan_tw_market(
                    False,
                    None,
                    settings,
                    report_date=target,
                    historical_replay=True,
                )
                _collect_financial_events(financial_report, target, stock_map, events, seen)
            except Exception as exc:
                day_errors.append({"date": target.isoformat(), "stage": "financial", "error": f"{type(exc).__name__}: {exc}"})

            try:
                chip_context = chip_strategies.build_market_context(
                    False,
                    target,
                    include_daily_data=True,
                    include_foreign_ratio=False,
                    progress_label=f"歷史重播籌碼 {target}",
                    scan_settings=settings,
                    scope="historical_scan",
                    trading_calendar=chip_calendar,
                    earliest_trading_dates=earliest_trading_dates,
                    cached_only=True,
                    historical_replay=True,
                )
                grade_maps = chip_strategies.build_chip_grade_maps(
                    chip_context, ["chip_1", "chip_2", "chip_3", "chip_4"]
                )
                _collect_chip_events(grade_maps, target, stock_map, events, seen)
            except Exception as exc:
                grade_maps = {key: {} for key in ("chip_1", "chip_2", "chip_3", "chip_4")}
                day_errors.append({"date": target.isoformat(), "stage": "chip", "error": f"{type(exc).__name__}: {exc}"})

            try:
                technical_result = technical_scanner.run_technical_scan(
                    settings,
                    target,
                    historical_replay=True,
                )
                _collect_technical_events(technical_result, target, stock_map, events, seen)
            except Exception as exc:
                day_errors.append({"date": target.isoformat(), "stage": "technical", "error": f"{type(exc).__name__}: {exc}"})

            if financial_report is not None and chip_context is not None and technical_result is not None:
                try:
                    curated_result = curated_scan_service.build_curated_scan_result(
                        settings,
                        target,
                        financial_report=financial_report,
                        chip_context=chip_context,
                        technical_result=technical_result,
                        include_scoring=False,
                        historical_replay=True,
                    )
                    _collect_curated_events(curated_result, target, stock_map, events, seen)
                except Exception as exc:
                    day_errors.append({"date": target.isoformat(), "stage": "curated", "error": f"{type(exc).__name__}: {exc}"})

            try:
                laoxiao_result = laoxiao_scan_service.build_laoxiao_scan_result(
                    settings,
                    target,
                    progress=lambda message: print(f"[歷史重播] {message}", flush=True),
                    historical_replay=True,
                )
                _collect_laoxiao_events(laoxiao_result, target, stock_map, events, seen)
            except Exception as exc:
                day_errors.append({"date": target.isoformat(), "stage": "laoxiao", "error": f"{type(exc).__name__}: {exc}"})

            diagnostics.append(
                {
                    "date": target.isoformat(),
                    "financial_count": len(financial_report.candidates) if financial_report else 0,
                    "chip_hard_filter_count": len(chip_context.candidates) if chip_context is not None else 0,
                    "chip_1_count": len(grade_maps.get("chip_1") or {}),
                    "chip_2_count": len(grade_maps.get("chip_2") or {}),
                    "chip_3_count": len(grade_maps.get("chip_3") or {}),
                    "chip_4_count": len(grade_maps.get("chip_4") or {}),
                    "technical_count": technical_result.matched_symbols if technical_result else 0,
                    "curated_count": len(curated_result.selected_codes) if curated_result else 0,
                    "laoxiao_count": len(laoxiao_result.selected_codes) if laoxiao_result else 0,
                    "errors": day_errors,
                }
            )
            failures.extend(day_errors)
            completed_dates.add(target.isoformat())
            payload.update(
                {
                    "schema_version": 1,
                    "start_date": start_date.isoformat(),
                    "end_date": end_date.isoformat(),
                    "trading_dates": [value.isoformat() for value in trading_dates],
                    "trading_calendar_source": calendar_source,
                    "completed_dates": sorted(completed_dates),
                    "events": events,
                    "diagnostics": diagnostics,
                    "failures": failures,
                    "price_prefetch_stats": price_stats,
                    "issued_shares_coverage": len(issued_shares),
                    "ai_calls": 0,
                    "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                }
            )
            _write_json(checkpoint_path, payload)
            print(f"[歷史重播] {target} 完成，累計事件 {len(events)}，本日錯誤 {len(day_errors)}", flush=True)
    finally:
        if original_input_loaders is not None:
            _restore_daily_input_loaders(original_input_loaders)
        chip_strategies._load_issued_shares_map = original_issued_loader

    payload["validation"] = _validate(payload)
    payload["event_json_name"] = event_json_path.name
    payload["generated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    _write_json(event_json_path, payload)
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    payload["markdown_path"] = str(markdown_path.resolve())
    payload["event_json_path"] = str(event_json_path.resolve())
    _write_json(checkpoint_path, payload)
    print(f"[歷史重播] 完成：{markdown_path}", flush=True)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2023-01-01")
    parser.add_argument("--end", default="2023-05-03")
    parser.add_argument("--output-dir", default="reports/historical_scan/2023-01-01_2023-05-03")
    parser.add_argument("--no-resume", action="store_true")
    parser.add_argument("--skip-chip-prefetch", action="store_true")
    args = parser.parse_args()
    result = replay(
        date.fromisoformat(args.start),
        date.fromisoformat(args.end),
        output_dir=(ROOT / args.output_dir).resolve(),
        resume=not args.no_resume,
        prefetch_chip=not args.skip_chip_prefetch,
    )
    print(
        json.dumps(
            {
                "completed_dates": len(result.get("completed_dates") or []),
                "events": len(result.get("events") or []),
                "unique_stocks": len({item["code"] for item in result.get("events") or []}),
                "validation": result.get("validation"),
                "markdown_path": result.get("markdown_path"),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
