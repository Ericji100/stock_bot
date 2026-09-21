from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter
from datetime import time
from pathlib import Path
from typing import Any, Iterable

from .cclass_replay import assess_copy_quality, build_causal_legs
from .pivot_replay import Candle, load_candles_from_html, split_sessions


def replay_xprocess(candles: list[Candle], parameters: tuple[int, ...] = (2, 3)) -> dict[str, Any]:
    sessions = split_sessions(candles)
    day_sessions = [(key, rows) for key, rows in sorted(sessions.items()) if key.endswith("-DAY")]
    prior_by_start = _prior_candle_map(candles, (rows[0] for _, rows in day_sessions if rows))
    aggregates: dict[str, Any] = {}
    session_rows: list[dict[str, Any]] = []

    for n in parameters:
        lens_counts: Counter[str] = Counter()
        endpoint_counts: Counter[str] = Counter()
        quality_counts: Counter[str] = Counter()
        gap_counts: Counter[str] = Counter()
        total_legs = full_five = causal_endpoint_samples = 0
        for key, rows in day_sessions:
            if not rows:
                continue
            opening = opening_evidence(rows, prior=prior_by_start.get(rows[0].at))
            legs = build_causal_legs(rows, n)
            lens_distribution = causal_lens_distribution(len(rows), legs)
            family = family_dna_proxy(legs)
            row = {
                "session_key": key,
                "n": n,
                "bar_count": len(rows),
                "opening_evidence": opening,
                "confirmed_leg_count": len(legs),
                "lens_distribution_bars": lens_distribution,
                "full_five_leg_sequences": len(legs) // 5,
                "family_dna_proxy": family,
            }
            session_rows.append(row)
            lens_counts.update(lens_distribution)
            endpoint_counts[opening["first_endpoint_style"]] += 1
            quality_counts[opening["pre_cash_open_quality"]] += 1
            gap_counts[opening["futures_gap_size"]] += 1
            total_legs += len(legs)
            full_five += len(legs) // 5
            causal_endpoint_samples += int(opening["endpoint_confirmed_at"] is not None)
        aggregates[str(n)] = {
            "day_session_count": len(day_sessions),
            "bar_count": sum(len(rows) for _, rows in day_sessions),
            "confirmed_leg_count": total_legs,
            "full_five_leg_sequences": full_five,
            "causal_first_endpoint_samples": causal_endpoint_samples,
            "first_endpoint_style_counts": dict(sorted(endpoint_counts.items())),
            "pre_cash_open_quality_counts": dict(sorted(quality_counts.items())),
            "futures_proxy_gap_size_counts": dict(sorted(gap_counts.items())),
            "lens_distribution_bars": dict(sorted(lens_counts.items())),
        }

    return {
        "parameters": list(parameters),
        "aggregate": aggregates,
        "sessions": session_rows,
        "discipline": [
            "只彙整 08:45～13:45 日盤 K；每個端點分類都記錄首次可知時間。",
            "回放不會把期貨時段缺口標成已驗證的現貨指數資料。",
            "legs 只在樞紐因果確認後才進入主鏡頭選擇；不回填盤後才明顯的轉折。",
            "第一次 DH／DL 事件固定保留，即使後續價格走勢看起來更乾淨也不改寫。",
        ],
        "limitations": [
            "HTML 只有期貨 K，沒有權威現貨前收／開盤資料，因此 cash_gap_source 維持 UNAVAILABLE。",
            "開盤品質、家族 DNA 與真／假突破風格是確定性研究 proxy，不是課程勝率宣稱。",
            "本回放只驗證時間順序與狀態覆蓋，不模擬四型態准入、成交、成本、滑價、出場或獲利。",
            "SAME_QUADRANT 與 SMALL_Q1 仍需即時多級數脈絡；回放不會杜撰這兩項共振。",
        ],
    }


def opening_evidence(rows: list[Candle], *, prior: Candle | None) -> dict[str, Any]:
    if not rows:
        raise ValueError("day session requires at least one candle")
    opening = [item for item in rows if time(8, 45) <= item.at.time().replace(tzinfo=None) <= time(8, 59)]
    if not opening:
        return {
            "cash_gap_source": "UNAVAILABLE",
            "futures_gap_source": "UNAVAILABLE",
            "futures_gap_direction": "UNDEFINED",
            "futures_gap_size": "UNDEFINED",
            "opening_direction_relation": "UNDEFINED",
            "pre_cash_open_quality": "UNAVAILABLE",
            "first_endpoint_side": "NONE",
            "first_endpoint_style": "UNAVAILABLE",
            "endpoint_first_seen_at": None,
            "endpoint_confirmed_at": None,
        }

    baseline = _baseline_before(rows[0], prior)
    gap_direction = "UNDEFINED"
    gap_size = "UNDEFINED"
    if prior is not None:
        difference = rows[0].open - prior.close
        gap_direction = "BULL" if difference > 0 else "BEAR" if difference < 0 else "FLAT"
        normalizer = max(baseline, statistics.median(item.high - item.low for item in opening), 1e-9)
        ratio = abs(difference) / normalizer
        gap_size = "SMALL" if ratio < 0.5 else "MODERATE" if ratio < 1.5 else "LARGE" if ratio < 3 else "EXHAUSTED"

    quality, first_direction = _opening_quality(opening)
    gap_relation = (
        "UNDEFINED"
        if gap_direction in {"UNDEFINED", "FLAT"} or first_direction == "UNDEFINED"
        else "SAME"
        if gap_direction == first_direction
        else "OPPOSITE"
    )
    endpoint = _first_endpoint_sample(rows, opening)
    return {
        "cash_gap_source": "UNAVAILABLE",
        "futures_gap_source": "FUTURES_PROXY" if prior is not None else "UNAVAILABLE",
        "futures_gap_direction": gap_direction,
        "futures_gap_size": gap_size,
        "opening_direction_relation": gap_relation,
        "pre_cash_open_quality": quality,
        **endpoint,
    }


def causal_lens_distribution(bar_count: int, legs: list[Any]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for index in range(bar_count):
        known_legs = [item for item in legs if item.known_at_index <= index]
        lens = select_primary_lens(known_legs)
        counts[lens] += 1
    return dict(sorted(counts.items()))


def select_primary_lens(known_legs: list[Any]) -> str:
    """Deterministic replay proxy for the two-leg clarity rule.

    Production judgment remains qualitative.  The replay uses only causal leg
    attributes and deliberately avoids a fixed four-leg quadrant gate.
    """
    if len(known_legs) < 2:
        return "OPENING_EVIDENCE_ONLY"
    previous, latest = known_legs[-2:]
    amplitude_ratio = latest.amplitude / max(previous.amplitude, 1e-9)
    slope_ratio = latest.slope / max(previous.slope, 1e-9)
    taiji_score = 0
    quadrant_score = 0
    if latest.direction != previous.direction and 0.25 <= amplitude_ratio <= 0.8:
        taiji_score += 4
    if min(previous.cleanliness, latest.cleanliness) >= 0.55:
        taiji_score += 1
    if len(known_legs) >= 3 and known_legs[-3].direction == latest.direction:
        copy_quality = assess_copy_quality(known_legs[-3], latest)["quality"]
        taiji_score += 3 if copy_quality in {"STRONG", "ACCEPTABLE"} else 1
    if amplitude_ratio >= 1.25 or amplitude_ratio <= 0.8:
        quadrant_score += 2
    if slope_ratio >= 1.15 or slope_ratio <= 0.9:
        quadrant_score += 2
    if abs(latest.cleanliness - previous.cleanliness) >= 0.15:
        quadrant_score += 1
    return "TAIJI_PRIMARY" if taiji_score > quadrant_score else "QUADRANT_PRIMARY"


def family_dna_proxy(legs: list[Any]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for index in range(2, len(legs)):
        base, current = legs[index - 2], legs[index]
        if base.direction != current.direction:
            counts["UNAVAILABLE"] += 1
            continue
        quality = assess_copy_quality(base, current)["quality"]
        if quality in {"STRONG", "ACCEPTABLE"}:
            counts["CONSISTENT"] += 1
        elif quality == "WEAK":
            counts["CHANGING"] += 1
        else:
            counts["BROKEN"] += 1
    return dict(sorted(counts.items()))


def render_markdown(report: dict[str, Any], *, source: Path, meta: dict[str, Any]) -> str:
    lines = [
        "# X 流程 RC 因果回放：n=2／n=3",
        "",
        f"- 來源：`{source}`",
        f"- 資料標題：{meta.get('title', '未知')}",
        f"- 原始 1 分 K：{meta.get('barCount', '未知')}",
        "- 範圍：日盤開盤證據、第一次當日高點／當日低點風格、因果腳數與主判讀工具切換。",
        "- 主判讀工具：兩腳以上依當時可見的段落比例／複製清晰度與趨勢／波動動態清晰度選擇；四象限不再要求四腳。",
        "- 性質：**流程與因果紀律驗證，不是獲利回測。**",
        "",
        "| 指標 | n=2 | n=3 |",
        "|---|---:|---:|",
    ]
    labels = (
        ("day_session_count", "日盤 sessions"),
        ("bar_count", "納入日盤 K"),
        ("confirmed_leg_count", "因果已知 legs"),
        ("full_five_leg_sequences", "完整五腳序列 proxy"),
        ("causal_first_endpoint_samples", "第一次端點可因果分類 sessions"),
    )
    for field, label in labels:
        lines.append(f"| {label} | {report['aggregate']['2'][field]} | {report['aggregate']['3'][field]} |")
    for field, label in (
        ("first_endpoint_style_counts", "端點風格"),
        ("pre_cash_open_quality_counts", "09:00 前品質"),
        ("futures_proxy_gap_size_counts", "期貨代理跳空級距"),
        ("lens_distribution_bars", "主鏡頭覆蓋（bars）"),
    ):
        lines.append(f"| {label} | `{json.dumps(report['aggregate']['2'][field], ensure_ascii=False)}` | `{json.dumps(report['aggregate']['3'][field], ensure_ascii=False)}` |")
    lines += ["", "## 因果紀律", ""]
    lines.extend(f"- {item}" for item in report["discipline"])
    lines += ["", "## 限制", ""]
    lines.extend(f"- {item}" for item in report["limitations"])
    return "\n".join(lines) + "\n"


def _first_endpoint_sample(rows: list[Candle], opening: list[Candle]) -> dict[str, Any]:
    opening_end = max(rows.index(item) for item in opening)
    initial_high = max(item.high for item in opening)
    initial_low = min(item.low for item in opening)
    for index in range(opening_end + 1, len(rows)):
        candle = rows[index]
        side = "DH" if candle.close > initial_high else "DL" if candle.close < initial_low else "NONE"
        if side == "NONE":
            continue
        boundary = initial_high if side == "DH" else initial_low
        following = rows[index + 1 : index + 3]
        for offset, item in enumerate(following, start=1):
            returned = item.close <= boundary if side == "DH" else item.close >= boundary
            if returned:
                return {
                    "first_endpoint_side": side,
                    "first_endpoint_style": "FALSE_LIKE",
                    "endpoint_first_seen_at": candle.at.isoformat(),
                    "endpoint_confirmed_at": item.at.isoformat(),
                }
        if len(following) == 2:
            extended = all(item.close > boundary for item in following) if side == "DH" else all(item.close < boundary for item in following)
            return {
                "first_endpoint_side": side,
                "first_endpoint_style": "TRUE_LIKE" if extended else "MIXED",
                "endpoint_first_seen_at": candle.at.isoformat(),
                "endpoint_confirmed_at": following[-1].at.isoformat(),
            }
        return {
            "first_endpoint_side": side,
            "first_endpoint_style": "PENDING",
            "endpoint_first_seen_at": candle.at.isoformat(),
            "endpoint_confirmed_at": None,
        }
    return {
        "first_endpoint_side": "NONE",
        "first_endpoint_style": "UNAVAILABLE",
        "endpoint_first_seen_at": None,
        "endpoint_confirmed_at": None,
    }


def _opening_quality(opening: list[Candle]) -> tuple[str, str]:
    ranges = [max(item.high - item.low, 1e-9) for item in opening]
    body_ratios = [abs(item.close - item.open) / span for item, span in zip(opening, ranges)]
    wick_ratios = [1.0 - ratio for ratio in body_ratios]
    changes = [opening[index].close - opening[index - 1].close for index in range(1, len(opening))]
    up = sum(item > 0 for item in changes)
    down = sum(item < 0 for item in changes)
    directional_share = max(up, down) / max(1, len(changes))
    overlaps = []
    for left, right in zip(opening, opening[1:]):
        intersection = max(0.0, min(left.high, right.high) - max(left.low, right.low))
        union = max(left.high, right.high) - min(left.low, right.low)
        overlaps.append(intersection / union if union > 0 else 1.0)
    median_body = statistics.median(body_ratios)
    median_wick = statistics.median(wick_ratios)
    median_overlap = statistics.median(overlaps) if overlaps else 1.0
    displacement = abs(opening[-1].close - opening[0].open) / max(statistics.median(ranges), 1e-9)
    if displacement >= 5 and directional_share >= 0.75:
        quality = "OVERHEATED"
    elif directional_share >= 0.65 and median_body >= 0.5 and median_wick <= 0.5 and median_overlap <= 0.65:
        quality = "CLEAN"
    elif directional_share <= 0.5 and (median_wick >= 0.55 or median_overlap >= 0.7):
        quality = "NOISY"
    else:
        quality = "MIXED"
    first_direction = "BULL" if opening[-1].close > opening[0].open else "BEAR" if opening[-1].close < opening[0].open else "UNDEFINED"
    return quality, first_direction


def _baseline_before(first: Candle, prior: Candle | None) -> float:
    if prior is None:
        return max(first.high - first.low, 1e-9)
    return max(prior.high - prior.low, first.high - first.low, 1e-9)


def _prior_candle_map(candles: Iterable[Candle], starts: Iterable[Candle]) -> dict[Any, Candle]:
    ordered = sorted(candles, key=lambda item: item.at)
    index = {item.at: position for position, item in enumerate(ordered)}
    result: dict[Any, Candle] = {}
    for start in starts:
        position = index.get(start.at)
        if position is not None and position > 0:
            result[start.at] = ordered[position - 1]
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="X process causal n=2/n=3 replay diagnostics")
    parser.add_argument("--html", type=Path, required=True)
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    args = parser.parse_args(argv)
    meta, candles = load_candles_from_html(args.html)
    report = replay_xprocess(candles)
    payload = {"ok": True, "source": str(args.html.resolve()), "meta": meta, "report": report}
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.markdown_output:
        args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
        args.markdown_output.write_text(render_markdown(report, source=args.html.resolve(), meta=meta), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
