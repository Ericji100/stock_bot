"""Repair material TPEX OHLC ordering anomalies with official daily quotes.

The Yahoo Taiwan history occasionally carries one OHLC field on a different
normalisation basis.  This utility detects only material impossible bars in the
monitoring window, fetches the contemporaneous TPEX OHLC, preserves Yahoo's
per-stock historical normalisation and adjustment factor, and writes a full
facts-only correction ledger.  It never reads trading performance.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import statistics
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
RUN = Path(
    os.environ.get(
        "FORMAL_AI_RUN",
        ROOT / "reports/course_backtest/2024-02-02/historical_scan_2023h2_formal_ai_v1_v2",
    )
).resolve()
MANIFEST = RUN / "input_manifest.json"
CORRECTIONS = RUN / "source_corrections.json"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def number(value: Any) -> float | None:
    text = str(value or "").strip().replace(",", "")
    if not text or text in {"---", "--", "-", "除權", "除息"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def material_gap(row: pd.Series, prefix: str = "raw_") -> float:
    open_, high, low, close = (float(row[f"{prefix}{name}"]) for name in ("open", "high", "low", "close"))
    gap = max(max(open_, low, close) - high, low - min(open_, high, close), 0.0)
    tolerance = max(0.01, abs(close) * 0.001)
    return gap if gap > tolerance else 0.0


def official_day(day: str) -> dict[str, dict[str, float]]:
    query = urllib.parse.urlencode({"date": day.replace("-", "/"), "response": "json"})
    url = f"https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes?{query}"
    request = urllib.request.Request(url, headers={"User-Agent": "stock-ai-bot-formal-backtest/1.0"})
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.loads(response.read().decode("utf-8"))
    table = payload["tables"][0]
    fields = [str(value).strip() for value in table["fields"]]
    indexes = {name: fields.index(name) for name in ("代號", "開盤", "最高", "最低", "收盤")}
    result: dict[str, dict[str, float]] = {}
    for row in table["data"]:
        values = {
            "open": number(row[indexes["開盤"]]),
            "high": number(row[indexes["最高"]]),
            "low": number(row[indexes["最低"]]),
            "close": number(row[indexes["收盤"]]),
        }
        if all(value is not None and value > 0 for value in values.values()):
            result[str(row[indexes["代號"]]).strip()] = {key: float(value) for key, value in values.items()}
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    manifest = read_json(MANIFEST)
    monitor_floor = str(manifest["monitor_floor"])
    as_of = str(manifest["as_of"])
    candidates: list[tuple[dict[str, Any], pd.DataFrame, int, float]] = []
    for item in manifest["items"]:
        frame = pd.read_csv(item["price_path"])
        for index, row in frame.iterrows():
            day = str(row["date"])
            if monitor_floor <= day <= as_of:
                gap = material_gap(row)
                if gap:
                    if str(item.get("market")) != "TPEX":
                        raise RuntimeError(f"unsupported market anomaly: {item['code']} {day} {item.get('market')}")
                    candidates.append((item, frame, int(index), gap))

    by_day = {day: official_day(day) for day in sorted({str(frame.at[index, "date"]) for _, frame, index, _ in candidates})}
    missing = [
        (str(item["code"]), str(frame.at[index, "date"]))
        for item, frame, index, _ in candidates
        if str(item["code"]) not in by_day[str(frame.at[index, "date"])]
    ]
    if missing:
        raise RuntimeError(f"official TPEX rows missing: {missing}")

    frames = {str(item["code"]): frame for item, frame, _, _ in candidates}
    corrections: list[dict[str, Any]] = []
    for item, frame, index, gap in candidates:
        code = str(item["code"])
        day = str(frame.at[index, "date"])
        official = by_day[day][code]
        old = {
            key: float(frame.at[index, key])
            for key in ("raw_open", "raw_high", "raw_low", "raw_close", "adj_close", "open", "high", "low", "close")
        }
        ratios = [
            old[f"raw_{key}"] / official[key]
            for key in ("open", "high", "low", "close")
            if official[key] > 0 and math.isfinite(old[f"raw_{key}"])
        ]
        raw_scale = float(statistics.median(ratios))
        adjustment_factor = old["adj_close"] / old["raw_close"]
        repaired: dict[str, float] = {}
        for key in ("open", "high", "low", "close"):
            repaired[f"raw_{key}"] = official[key] * raw_scale
            repaired[key] = repaired[f"raw_{key}"] * adjustment_factor
            frame.at[index, f"raw_{key}"] = repaired[f"raw_{key}"]
            frame.at[index, key] = repaired[key]
        frame.at[index, "adj_close"] = repaired["close"]
        repaired["adj_close"] = repaired["close"]
        if material_gap(frame.loc[index]):
            raise RuntimeError(f"repair did not produce valid OHLC: {code} {day}")
        corrections.append({
            "code": code,
            "name": str(item["name"]),
            "date": day,
            "reason": "Yahoo OHLC 欄位尺度不一致並形成不可能K棒；以同日櫃買中心官方OHLC修復。",
            "authoritative_source": (
                "https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes?"
                + urllib.parse.urlencode({"date": day.replace("-", "/"), "response": "json"})
            ),
            "official_ohlc": official,
            "vendor_before": old,
            "normalization": {
                "raw_scale_median_of_ohlc_ratios": raw_scale,
                "yahoo_adjustment_factor_preserved": adjustment_factor,
            },
            "repaired": repaired,
            "material_ordering_gap": gap,
            "performance_visible_when_decided": False,
        })

    summary = {
        "candidate_bars": len(corrections),
        "candidate_stocks": len({row["code"] for row in corrections}),
        "dates": len({row["date"] for row in corrections}),
        "applied": bool(args.apply),
    }
    if not args.apply:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return

    for item in manifest["items"]:
        code = str(item["code"])
        if code not in frames:
            continue
        path = Path(item["price_path"])
        frames[code].to_csv(path, index=False, encoding="utf-8-sig")
        item["price_sha256"] = digest(path)
        if "WITH_TPEX_OHLC_REPAIR" not in str(item.get("source_origin") or ""):
            item["source_origin"] = f"{item.get('source_origin')}_WITH_TPEX_OHLC_REPAIR"

    correction_payload = {
        "schema_version": 1,
        "scope": {"from": monitor_floor, "through": as_of, "market": "TPEX"},
        "detection_tolerance": "gap > max(NT$0.01, 0.1% of vendor raw close)",
        "performance_visible_when_decided": False,
        "corrections": sorted(corrections, key=lambda row: (row["date"], row["code"])),
    }
    write_json(CORRECTIONS, correction_payload)
    manifest["source_corrections_path"] = str(CORRECTIONS.resolve())
    manifest["source_corrections_sha256"] = digest(CORRECTIONS)
    write_json(MANIFEST, manifest)
    summary["source_corrections_sha256"] = manifest["source_corrections_sha256"]
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
