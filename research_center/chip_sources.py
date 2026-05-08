from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pandas as pd

ROOT_DIR = Path(__file__).resolve().parents[1]
CACHE_DIR = ROOT_DIR / ".cache"
DAILY_CHIP_CACHE_DIR = CACHE_DIR / "chip_daily"
TDCC_CACHE_DIR = CACHE_DIR / "tdcc"
FINMIND_API_URL = "https://api.finmindtrade.com/api/v4/data"


def build_chip_backup_snapshot(stock_code: str, report_date: date | None = None, lookback_days: int = 60) -> dict[str, Any]:
    code = str(stock_code).strip()
    daily_cache = _load_recent_daily_chip_rows(code, report_date, lookback_days)
    finmind_backup = _fetch_finmind_recent_chip_rows(code, report_date)
    daily = _merge_daily_chip_sources(daily_cache, finmind_backup) if finmind_backup.get("rows") else daily_cache
    weekly = _load_recent_tdcc_rows(code, report_date)
    return {
        "status": _snapshot_status(daily, weekly),
        "code": code,
        "daily_chip_cache": daily,
        "finmind_live_backup": finmind_backup,
        "tdcc_weekly_cache": weekly,
        "summary": _build_summary(daily, weekly),
        "data_policy": "優先使用選股程式本機快取：.cache/chip_daily 與 .cache/tdcc；法人日資料缺口會嘗試 FinMind API 即時備援，仍失敗才標示 missing。",
    }


def build_chip_backup_events(stock_code: str, report_date: date | None = None) -> list[dict[str, Any]]:
    snapshot = build_chip_backup_snapshot(stock_code, report_date)
    events: list[dict[str, Any]] = []
    for row in snapshot.get("daily_chip_cache", {}).get("rows", [])[:12]:
        source = str(row.get("source") or "cache")
        events.append(
            {
                "event_type": "chip_daily_finmind" if "FinMind" in source else "chip_daily_cache",
                "target": stock_code,
                "title": f"法人日資料 {stock_code} {row.get('date')}",
                "source_url": FINMIND_API_URL if "FinMind" in source else str(DAILY_CHIP_CACHE_DIR),
                "source_level": "Level 2" if "FinMind" in source else "Level 1",
                "published_date": row.get("date"),
                "payload": row,
            }
        )
    for row in snapshot.get("tdcc_weekly_cache", {}).get("rows", [])[:8]:
        events.append(
            {
                "event_type": "tdcc_weekly_cache",
                "target": stock_code,
                "title": f"TDCC大戶/散戶週資料 {stock_code} {row.get('snapshot_date')}",
                "source_url": str(TDCC_CACHE_DIR),
                "source_level": "Level 1",
                "published_date": row.get("snapshot_date"),
                "payload": row,
            }
        )
    return events


def _load_recent_daily_chip_rows(stock_code: str, report_date: date | None, lookback_days: int) -> dict[str, Any]:
    files = _cache_files_before(DAILY_CHIP_CACHE_DIR, "*.csv", report_date)
    rows: list[dict[str, Any]] = []
    sources: set[str] = set()
    for path in files:
        if len(rows) >= lookback_days:
            break
        try:
            frame = pd.read_csv(path, dtype={"code": str})
        except Exception:
            continue
        if "code" not in frame.columns:
            continue
        subset = frame[frame["code"].astype(str).str.strip() == stock_code].copy()
        if subset.empty:
            continue
        for _, item in subset.iterrows():
            row = _clean_record(item.to_dict())
            row.setdefault("date", _date_from_path(path))
            rows.append(row)
            source = str(row.get("source") or "cache")
            for part in source.replace("/", "+").split("+"):
                if part.strip():
                    sources.add(part.strip())
    rows = sorted(rows, key=lambda item: str(item.get("date") or ""), reverse=True)[:lookback_days]
    return {
        "status": "covered" if rows else "missing",
        "source": str(DAILY_CHIP_CACHE_DIR),
        "source_types": sorted(sources),
        "row_count": len(rows),
        "rows": rows,
    }


def _fetch_finmind_recent_chip_rows(stock_code: str, report_date: date | None) -> dict[str, Any]:
    end_date = report_date or datetime.now().date()
    start_date = end_date - timedelta(days=14)
    rows_by_date: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    try:
        with httpx.Client(timeout=12.0, follow_redirects=True) as client:
            payload = client.get(
                FINMIND_API_URL,
                params={
                    "dataset": "TaiwanStockInstitutionalInvestorsBuySell",
                    "data_id": stock_code,
                    "start_date": start_date.isoformat(),
                    "end_date": end_date.isoformat(),
                },
            )
            payload.raise_for_status()
            data = payload.json()
            if data.get("status") == 200:
                for item in data.get("data") or []:
                    row_date = str(item.get("date") or "")[:10]
                    if not row_date:
                        continue
                    row = rows_by_date.setdefault(row_date, _empty_finmind_row(stock_code, row_date))
                    name = str(item.get("name") or "")
                    buy = _to_float(item.get("buy")) or 0.0
                    sell = _to_float(item.get("sell")) or 0.0
                    net_lots = (buy - sell) / 1000.0
                    if name == "Foreign_Investor":
                        row["foreign_net_lots"] = (row.get("foreign_net_lots") or 0.0) + net_lots
                    elif name == "Investment_Trust":
                        row["trust_net_lots"] = (row.get("trust_net_lots") or 0.0) + net_lots
            else:
                errors.append(str(data.get("msg") or data.get("status")))

            ratio_payload = client.get(
                FINMIND_API_URL,
                params={
                    "dataset": "TaiwanStockShareholding",
                    "data_id": stock_code,
                    "start_date": start_date.isoformat(),
                    "end_date": end_date.isoformat(),
                },
            )
            ratio_payload.raise_for_status()
            ratio_data = ratio_payload.json()
            if ratio_data.get("status") == 200:
                for item in ratio_data.get("data") or []:
                    row_date = str(item.get("date") or "")[:10]
                    ratio = _to_float(item.get("ForeignInvestmentSharesRatio")) or _to_float(item.get("foreign_investment_shares_ratio"))
                    if row_date and ratio is not None:
                        row = rows_by_date.setdefault(row_date, _empty_finmind_row(stock_code, row_date))
                        row["foreign_ratio_pct"] = ratio
    except Exception as exc:
        errors.append(str(exc)[:200])
    rows = sorted(rows_by_date.values(), key=lambda item: str(item.get("date") or ""), reverse=True)
    return {
        "status": "covered" if rows else "unavailable" if errors else "empty",
        "source": "FinMind API",
        "row_count": len(rows),
        "rows": rows,
        "errors": errors[:3],
    }


def _empty_finmind_row(stock_code: str, row_date: str) -> dict[str, Any]:
    return {
        "date": row_date,
        "code": stock_code,
        "market": None,
        "foreign_net_lots": 0.0,
        "trust_net_lots": 0.0,
        "foreign_ratio_pct": None,
        "source": "FinMind",
    }


def _merge_daily_chip_sources(cache_data: dict[str, Any], finmind_data: dict[str, Any]) -> dict[str, Any]:
    rows_by_date: dict[str, dict[str, Any]] = {str(row.get("date")): dict(row) for row in cache_data.get("rows") or []}
    for row in finmind_data.get("rows") or []:
        key = str(row.get("date"))
        if key in rows_by_date:
            existing = rows_by_date[key]
            for field in ("foreign_net_lots", "trust_net_lots", "foreign_ratio_pct"):
                if existing.get(field) in (None, "", "nan") and row.get(field) is not None:
                    existing[field] = row.get(field)
            source = str(existing.get("source") or "cache")
            if "FinMind" not in source:
                existing["source"] = f"{source}/FinMind"
        else:
            rows_by_date[key] = dict(row)
    rows = sorted(rows_by_date.values(), key=lambda item: str(item.get("date") or ""), reverse=True)
    sources = set(cache_data.get("source_types") or [])
    if finmind_data.get("rows"):
        sources.add("FinMind")
    return {
        "status": "covered" if rows else cache_data.get("status", "missing"),
        "source": f"{cache_data.get('source')} + FinMind API",
        "source_types": sorted(sources),
        "row_count": len(rows),
        "rows": rows,
    }


def _load_recent_tdcc_rows(stock_code: str, report_date: date | None) -> dict[str, Any]:
    files = _cache_files_before(TDCC_CACHE_DIR, "*.csv", report_date)
    rows: list[dict[str, Any]] = []
    for path in files[:8]:
        try:
            frame = pd.read_csv(path, dtype=str)
        except Exception:
            continue
        code_col = _find_column(frame, ("證券代號", "stock", "code"))
        level_col = _find_column(frame, ("持股分級", "level"))
        pct_col = _find_column(frame, ("占集保庫存數比例", "比例", "%"))
        people_col = _find_column(frame, ("人數", "people"))
        if not code_col or not level_col:
            continue
        subset = frame[frame[code_col].astype(str).str.strip() == stock_code]
        if subset.empty:
            continue
        big_pct = 0.0
        retail_pct = 0.0
        total_people = 0
        for _, item in subset.iterrows():
            level = _to_float(item.get(level_col))
            pct = _to_float(item.get(pct_col)) or 0.0
            people = int(_to_float(item.get(people_col)) or 0) if people_col else 0
            total_people += people
            if level in {12, 13, 14, 15}:
                big_pct += pct
            if level in {1, 2, 3, 4, 5, 6, 7, 8}:
                retail_pct += pct
        rows.append(
            {
                "snapshot_date": _date_from_path(path),
                "big_holder_pct": round(big_pct, 2),
                "retail_holder_pct": round(retail_pct, 2),
                "total_people": total_people,
                "source_file": path.name,
            }
        )
    return {"status": "covered" if rows else "missing", "source": str(TDCC_CACHE_DIR), "row_count": len(rows), "rows": rows}


def _build_summary(daily: dict[str, Any], weekly: dict[str, Any]) -> dict[str, Any]:
    daily_rows = daily.get("rows") or []
    weekly_rows = weekly.get("rows") or []
    summary: dict[str, Any] = {"daily_status": daily.get("status"), "weekly_status": weekly.get("status")}
    if daily_rows:
        latest = daily_rows[0]
        recent = daily_rows[:10]
        summary.update(
            {
                "latest_daily_date": latest.get("date"),
                "latest_foreign_net_lots": _to_float(latest.get("foreign_net_lots")),
                "latest_trust_net_lots": _to_float(latest.get("trust_net_lots")),
                "latest_foreign_ratio_pct": _to_float(latest.get("foreign_ratio_pct")),
                "recent_10d_foreign_net_lots": round(sum(_to_float(row.get("foreign_net_lots")) or 0.0 for row in recent), 2),
                "recent_10d_trust_net_lots": round(sum(_to_float(row.get("trust_net_lots")) or 0.0 for row in recent), 2),
                "source_types": daily.get("source_types") or [],
            }
        )
    if weekly_rows:
        latest_week = weekly_rows[0]
        previous_week = weekly_rows[1] if len(weekly_rows) > 1 else {}
        summary.update(
            {
                "latest_tdcc_date": latest_week.get("snapshot_date"),
                "latest_big_holder_pct": latest_week.get("big_holder_pct"),
                "latest_retail_holder_pct": latest_week.get("retail_holder_pct"),
                "big_holder_pct_change_1w": _delta(latest_week.get("big_holder_pct"), previous_week.get("big_holder_pct")),
                "retail_holder_pct_change_1w": _delta(latest_week.get("retail_holder_pct"), previous_week.get("retail_holder_pct")),
            }
        )
    return summary


def _snapshot_status(daily: dict[str, Any], weekly: dict[str, Any]) -> str:
    if daily.get("status") == "covered" and weekly.get("status") == "covered":
        return "covered"
    if daily.get("status") == "covered" or weekly.get("status") == "covered":
        return "partial"
    return "missing"


def _cache_files_before(directory: Path, pattern: str, report_date: date | None) -> list[Path]:
    if not directory.exists():
        return []
    files = sorted(directory.glob(pattern), key=lambda item: item.stem, reverse=True)
    if report_date is not None:
        cutoff = report_date.strftime("%Y%m%d")
        files = [path for path in files if path.stem <= cutoff]
    return files


def _date_from_path(path: Path) -> str:
    try:
        return datetime.strptime(path.stem[:8], "%Y%m%d").date().isoformat()
    except Exception:
        return path.stem


def _find_column(frame: pd.DataFrame, keywords: tuple[str, ...]) -> str | None:
    for column in frame.columns:
        text = str(column).lower()
        if any(keyword.lower() in text for keyword in keywords):
            return str(column)
    return None


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if text in {"", "--", "---", "nan", "None", "<NA>"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _delta(current: Any, previous: Any) -> float | None:
    current_value = _to_float(current)
    previous_value = _to_float(previous)
    if current_value is None or previous_value is None:
        return None
    return round(current_value - previous_value, 2)


def _clean_record(row: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in row.items():
        if pd.isna(value):
            clean[str(key)] = None
        elif isinstance(value, (pd.Timestamp, datetime)):
            clean[str(key)] = value.date().isoformat()
        else:
            clean[str(key)] = value.item() if hasattr(value, "item") else value
    return clean
