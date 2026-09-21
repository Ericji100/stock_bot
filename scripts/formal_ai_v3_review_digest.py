"""Create outcome-free, causal review digests for the full V3 AI audit.

This script never chooses a route and never approves a trade.  It only turns
the immutable JSONL queue into compact batches.  A code appears at most once
in a batch, so reviewing a row cannot expose a later checkpoint for the same
stock.  Rows are grouped by the ordinal checkpoint of each stock and retain
only facts that were available at that checkpoint's close.
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "reports/course_backtest/2024-02-02/historical_scan_2023h2_formal_ai_v3_full_review"
QUEUE = RUN / "ai_review_queue.jsonl"
BATCH_DIR = RUN / "ai_review_batches"
MANIFEST = RUN / "ai_review_batch_manifest.json"
EXPOSED_CODES = {"6140", "6535", "8054", "8059", "8096"}


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _seq(rows: list[dict[str, Any]]) -> str:
    return ">".join(f"{row['source_date']}@{float(row['price']):.2f}" for row in rows)


def _pct_delta(now: float | None, then: float | None) -> float | None:
    if now is None or then in (None, 0):
        return None
    return (float(now) / float(then) - 1.0) * 100.0


def _ma_slope(packet: dict[str, Any], key: str) -> float | None:
    rows = packet["recent_daily_visible_facts_asof"]
    usable = [row for row in rows if row.get(key) is not None]
    if len(usable) < 2:
        return None
    lookback = usable[-21] if len(usable) >= 21 else usable[0]
    return _pct_delta(usable[-1][key], lookback[key])


def _position(value: float, low: float, high: float) -> float | None:
    if high <= low:
        return None
    return (value - low) / (high - low) * 100.0


def _compact(row: dict[str, Any], ordinal: int) -> dict[str, Any]:
    packet = json.loads(Path(row["packet_path"]).read_text(encoding="utf-8-sig"))
    market = row["market"]
    structure = row["structure"]
    close = float(market["close"])
    large_highs = structure["confirmed_large_highs"]
    large_lows = structure["confirmed_large_lows"]
    small_highs = structure["confirmed_small_highs"]
    small_lows = structure["confirmed_small_lows"]
    ma_order = [key for key in ("ma5", "ma13", "ma21", "ma55", "ma105", "ma144") if close > float(market[key])]
    last_large_high = large_highs[-1]
    last_large_low = large_lows[-1]
    orientation = "DOWN_LAST" if last_large_high["source_date"] < last_large_low["source_date"] else "UP_LAST"
    cycles = [
        {
            "sign": cycle["sign"],
            "start": cycle["start"],
            "end": cycle["end"],
            "bars": cycle["bars"],
            "low": f"{cycle['low_date']}@{float(cycle['low']):.2f}",
            "high": f"{cycle['high_date']}@{float(cycle['high']):.2f}",
        }
        for cycle in row["completed_macd_cycles_asof"][-3:]
    ]
    return {
        "review_id": row["review_id"],
        "ordinal_for_stock": ordinal,
        "index": row["index"],
        "code": row["code"],
        "name": row["name"],
        "first_selected_on": row["first_selected_on"],
        "review_as_of": row["review_as_of"],
        "outcome_exposure": "OUTCOME_EXPOSED_DIAGNOSTIC" if row["code"] in EXPOSED_CODES else "BLIND_PRIMARY",
        "selected_today": row["selection_asof"]["selected_today"],
        "market": {
            "close": close,
            "return_1d_pct": market["return_1d_pct"],
            "volume_ratio_20": market["volume_ratio_20"],
            "atr14": market["atr14"],
            "above_mas": ma_order,
            "ma105_slope_20d_pct": _ma_slope(packet, "ma105"),
            "ma144_slope_20d_pct": _ma_slope(packet, "ma144"),
            "macd_hist": market["macd_hist"],
            "macd_hist_delta": market["macd_hist_delta"],
            "range30_position_pct": _position(close, float(row["recent_30d_range"]["low"]), float(row["recent_30d_range"]["high"])),
            "facts": market["facts"],
        },
        "structure": {
            "orientation": orientation,
            "breaks_last_large_high": close > float(last_large_high["price"]),
            "small_lows": _seq(small_lows),
            "small_highs": _seq(small_highs),
            "large_lows": _seq(large_lows),
            "large_highs": _seq(large_highs),
        },
        "cycles": cycles,
        "risk": row["risk"],
        "packet_path": row["packet_path"],
        "packet_sha256": row["packet_sha256"],
    }


def _fmt(value: float | None) -> str:
    return "na" if value is None else f"{value:.1f}"


def _markdown(rows: list[dict[str, Any]], batch_name: str) -> str:
    lines = [
        f"# {batch_name}",
        "",
        "此檔只含各判讀日收盤前可見資訊；不含期後價格、損益、MFE、V1訊號或最終V2理由。",
        "同一批每檔股票最多一個候選日；OUTCOME_EXPOSED_DIAGNOSTIC 不得列入盲測主統計。",
        "",
    ]
    for row in rows:
        m = row["market"]
        s = row["structure"]
        r = row["risk"]
        cycles = ";".join(f"{x['sign'][0]}:{x['start']}~{x['end']}({x['bars']}) L{x['low']} H{x['high']}" for x in row["cycles"])
        lines.extend([
            f"## {row['review_id']} {row['code']} {row['name']} [{row['outcome_exposure']}]",
            f"- first={row['first_selected_on']} day={row['review_as_of']} selected={','.join(row['selected_today']) or '-'}",
            f"- px={m['close']:.2f} ret={m['return_1d_pct']:.2f}% vol={m['volume_ratio_20']:.2f} ATR={m['atr14']:.2f} above={','.join(m['above_mas']) or '-'} ma105s={_fmt(m['ma105_slope_20d_pct'])}% ma144s={_fmt(m['ma144_slope_20d_pct'])}% macd={m['macd_hist']:.4f}/{m['macd_hist_delta']:.4f} range30={_fmt(m['range30_position_pct'])}%",
            f"- orient={s['orientation']} largeBreak={s['breaks_last_large_high']} risk={r['distance_pct']:.2f}%/{r['distance_atr']:.2f}ATR stop={r['stop_date']}@{r['stop_price']:.2f}(confirmed {r['stop_confirmed_on']})",
            f"- SL={s['small_lows']} | SH={s['small_highs']}",
            f"- LL={s['large_lows']} | LH={s['large_highs']}",
            f"- cycles={cycles}",
            f"- facts={','.join(m['facts'])}",
            f"- packet={row['packet_path']}",
            "",
        ])
    return "\n".join(lines)


def build(batch_size: int = 80) -> dict[str, Any]:
    source = _jsonl(QUEUE)
    by_code: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in source:
        by_code[str(row["code"])].append(row)
    for rows in by_code.values():
        rows.sort(key=lambda value: str(value["review_as_of"]))
    rounds: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for rows in by_code.values():
        for ordinal, row in enumerate(rows):
            rounds[ordinal].append(_compact(row, ordinal))
    BATCH_DIR.mkdir(parents=True, exist_ok=True)
    files = []
    for ordinal, rows in sorted(rounds.items()):
        rows.sort(key=lambda value: (int(value["index"]), str(value["review_as_of"])))
        for start in range(0, len(rows), batch_size):
            chunk = rows[start:start + batch_size]
            stem = f"round_{ordinal:02d}_batch_{start // batch_size:02d}"
            jsonl_path = BATCH_DIR / f"{stem}.jsonl"
            md_path = BATCH_DIR / f"{stem}.md"
            jsonl_path.write_text("".join(json.dumps(row, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n" for row in chunk), encoding="utf-8")
            md_path.write_text(_markdown(chunk, stem), encoding="utf-8")
            files.append({
                "round": ordinal,
                "batch": start // batch_size,
                "rows": len(chunk),
                "jsonl": str(jsonl_path.resolve()),
                "jsonl_sha256": _sha(jsonl_path),
                "markdown": str(md_path.resolve()),
                "markdown_sha256": _sha(md_path),
            })
    manifest = {
        "method": "OUTCOME_FREE_CAUSAL_AI_REVIEW_DIGEST_V1",
        "queue": str(QUEUE.resolve()),
        "queue_sha256": _sha(QUEUE),
        "rows": len(source),
        "stocks": len(by_code),
        "batch_size": batch_size,
        "outcome_exposed_codes": sorted(EXPOSED_CODES),
        "files": files,
    }
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


if __name__ == "__main__":
    print(json.dumps(build(), ensure_ascii=True, indent=2))
