from __future__ import annotations

import argparse
import json
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .pivot_replay import Candle, LocalPivot, detect_local_pivots, load_candles_from_html, pair_pivots, split_sessions


@dataclass(frozen=True)
class ReplayLeg:
    direction: str
    start_index: int
    end_index: int
    known_at_index: int
    amplitude: float
    duration_bars: int
    slope: float
    cleanliness: float
    end_price: float


def build_causal_legs(candles: list[Candle], n: int) -> list[ReplayLeg]:
    local, _ = detect_local_pivots(candles, n)
    paired, _, _ = pair_pivots(local)
    finalized = paired[:-1]
    legs: list[ReplayLeg] = []
    for start, end in zip(finalized, finalized[1:]):
        if start.kind == end.kind or end.bar_index <= start.bar_index:
            continue
        direction = "BULL" if end.price > start.price else "BEAR"
        duration = max(1, end.bar_index - start.bar_index)
        changes = [
            candles[index].close - candles[index - 1].close
            for index in range(start.bar_index + 1, end.bar_index + 1)
        ]
        aligned = sum(change > 0 if direction == "BULL" else change < 0 for change in changes)
        amplitude = abs(end.price - start.price)
        legs.append(
            ReplayLeg(
                direction=direction,
                start_index=start.bar_index,
                end_index=end.bar_index,
                known_at_index=max(start.confirmation_index, end.confirmation_index),
                amplitude=amplitude,
                duration_bars=duration,
                slope=amplitude / duration,
                cleanliness=aligned / len(changes) if changes else 0.0,
                end_price=end.price,
            )
        )
    return sorted(legs, key=lambda item: (item.known_at_index, item.end_index))


def assess_copy_quality(base: ReplayLeg, copy: ReplayLeg) -> dict[str, str]:
    if base.direction != copy.direction:
        raise ValueError("copy legs must have the same direction")
    relations = {
        "amplitude": _ratio_relation(copy.amplitude, base.amplitude, higher_is_stronger=True),
        "duration": _ratio_relation(copy.duration_bars, base.duration_bars, higher_is_stronger=False),
        "slope": _ratio_relation(copy.slope, base.slope, higher_is_stronger=True),
        "cleanliness": _ratio_relation(copy.cleanliness, base.cleanliness, higher_is_stronger=True),
        "destructive": _destructive_relation(base, copy),
    }
    stronger = sum(value == "STRONGER" for value in relations.values())
    weaker = sum(value == "WEAKER" for value in relations.values())
    if relations["destructive"] == "WEAKER" and weaker >= 3:
        quality = "FAILED"
    elif weaker >= 3:
        quality = "WEAK"
    elif stronger >= 3:
        quality = "STRONG"
    else:
        quality = "ACCEPTABLE"
    return {"quality": quality, **relations}


def replay_session(candles: list[Candle], n: int) -> dict[str, Any]:
    legs = build_causal_legs(candles, n)
    copy_assessments: list[dict[str, Any]] = []
    for offset in range(0, len(legs), 5):
        dynasty = legs[offset : offset + 5]
        if len(dynasty) >= 3 and dynasty[0].direction == dynasty[2].direction:
            copy_assessments.append({"sequence": "COPY_3", **assess_copy_quality(dynasty[0], dynasty[2])})
        if len(dynasty) >= 5 and dynasty[2].direction == dynasty[4].direction:
            copy_assessments.append({"sequence": "COPY_5", **assess_copy_quality(dynasty[2], dynasty[4])})

    momentum = _replay_momentum(candles, n)
    return {
        "bar_count": len(candles),
        "taiji_leg_count": len(legs),
        "taiji_full_five_leg_sequences": len(legs) // 5,
        "copy_assessments": len(copy_assessments),
        "copy_strong": sum(item["quality"] == "STRONG" for item in copy_assessments),
        "copy_acceptable": sum(item["quality"] == "ACCEPTABLE" for item in copy_assessments),
        "copy_weak": sum(item["quality"] == "WEAK" for item in copy_assessments),
        "copy_failed": sum(item["quality"] == "FAILED" for item in copy_assessments),
        "latest_leg_known_delay_bars": None
        if not legs
        else round(float(statistics.median(item.known_at_index - item.end_index for item in legs)), 3),
        **momentum,
    }


def compare_cclass_parameters(candles: list[Candle], parameters: tuple[int, ...] = (2, 3)) -> dict[str, Any]:
    sessions = split_sessions(candles)
    aggregate: dict[str, dict[str, Any]] = {}
    session_rows: list[dict[str, Any]] = []
    sum_fields = (
        "bar_count",
        "taiji_leg_count",
        "taiji_full_five_leg_sequences",
        "copy_assessments",
        "copy_strong",
        "copy_acceptable",
        "copy_weak",
        "copy_failed",
        "centrifugal_confirmed",
        "life_death_gate_forming",
        "life_death_gate_armed",
        "momentum_failed",
        "exhaustion_warning",
        "structural_pivot_pressure",
        "mode_switches",
    )
    for n in parameters:
        totals = {field: 0 for field in sum_fields}
        delays: list[float] = []
        for key, rows in sorted(sessions.items()):
            result = replay_session(rows, n)
            session_rows.append({"session_key": key, "n": n, **result})
            for field in sum_fields:
                totals[field] += int(result[field])
            if result["latest_leg_known_delay_bars"] is not None:
                delays.append(float(result["latest_leg_known_delay_bars"]))
        assessments = totals["copy_assessments"]
        events = totals["centrifugal_confirmed"]
        aggregate[str(n)] = {
            **totals,
            "session_count": len(sessions),
            "copy_weak_or_failed_rate": None
            if not assessments
            else round((totals["copy_weak"] + totals["copy_failed"]) / assessments, 4),
            "gate_armed_per_momentum_event": None
            if not events
            else round(totals["life_death_gate_armed"] / events, 4),
            "median_session_leg_known_delay_bars": None if not delays else round(float(statistics.median(delays)), 3),
        }
    return {
        "parameters": list(parameters),
        "aggregate": aggregate,
        "sessions": session_rows,
        "discipline": [
            "Pivots enter the replay only at their causal confirmation index; no final-day pivot is backfilled.",
            "Momentum baselines use only the preceding 20 bars; gate decisions occur only after their forming bars close.",
            "Day High/Low and final session outcome are never used to rewrite an earlier event.",
        ],
        "limitations": [
            "This is a deterministic structural proxy, not a profitability backtest or a substitute for screenshot review.",
            "Taiji quality uses five retained dimensions, but visual anchor meat/cleanliness and grade context remain qualitative in live analysis.",
            "Centrifugal and gate thresholds are replay diagnostics, not formal fixed trading parameters.",
            "Four-pattern admission, fills, costs, slippage, stops, exits and R are not simulated.",
        ],
    }


def render_markdown(report: dict[str, Any], *, source: Path, meta: dict[str, Any]) -> str:
    lines = [
        "# 戰法 C 班 RC 因果回放：n=2／n=3 結構比較",
        "",
        f"- 來源：`{source}`",
        f"- 資料標題：{meta.get('title', '未知')}",
        f"- 原始 1 分 K：{meta.get('barCount', '未知')}",
        f"- 納入交易時段：{report['aggregate']['2']['bar_count']}",
        "- 性質：結構與狀態密度驗證，**不是獲利回測**。",
        "",
        "| 指標 | n=2 | n=3 |",
        "|---|---:|---:|",
    ]
    labels = (
        ("taiji_leg_count", "因果已知太極 legs"),
        ("taiji_full_five_leg_sequences", "完整五段序列"),
        ("copy_assessments", "COPY_3／COPY_5 比較"),
        ("copy_weak_or_failed_rate", "複製弱／失敗率"),
        ("median_session_leg_known_delay_bars", "leg 已知延遲中位數（根）"),
        ("centrifugal_confirmed", "離心力確認 proxy"),
        ("life_death_gate_forming", "生死門形成 proxy"),
        ("life_death_gate_armed", "生死門 armed proxy"),
        ("momentum_failed", "動能失效 proxy"),
        ("exhaustion_warning", "耗竭警告 proxy"),
        ("structural_pivot_pressure", "結構樞紐壓力"),
        ("mode_switches", "模式切換"),
    )
    for key, label in labels:
        lines.append(f"| {label} | {report['aggregate']['2'].get(key)} | {report['aggregate']['3'].get(key)} |")
    lines += ["", "## 因果紀律", ""]
    lines.extend(f"- {item}" for item in report["discipline"])
    lines += ["", "## 限制", ""]
    lines.extend(f"- {item}" for item in report["limitations"])
    return "\n".join(lines) + "\n"


def _replay_momentum(candles: list[Candle], n: int) -> dict[str, int]:
    pivots, _ = detect_local_pivots(candles, n)
    by_confirmation: dict[int, list[LocalPivot]] = {}
    for pivot in pivots:
        by_confirmation.setdefault(pivot.confirmation_index, []).append(pivot)
    counts = {
        "centrifugal_confirmed": 0,
        "life_death_gate_forming": 0,
        "life_death_gate_armed": 0,
        "momentum_failed": 0,
        "exhaustion_warning": 0,
        "structural_pivot_pressure": 0,
        "mode_switches": 0,
    }
    active: dict[str, Any] | None = None
    cooldown_until = -1
    for index, candle in enumerate(candles):
        if active is not None:
            opposite_kind = "HIGH" if active["direction"] == "BEAR" else "LOW"
            if any(item.kind == opposite_kind for item in by_confirmation.get(index, [])):
                counts["structural_pivot_pressure"] += 1
            direction = active["direction"]
            failed = candle.close < active["pre_boundary"] if direction == "BULL" else candle.close > active["pre_boundary"]
            if failed:
                counts["momentum_failed"] += 1
                counts["mode_switches"] += 1
                active = None
                cooldown_until = index + 2
                continue
            if active["gate"] is not None and index > active["gate"]["formed_at"]:
                gate = active["gate"]
                continuation = candle.close > gate["high"] if direction == "BULL" else candle.close < gate["low"]
                reversal = candle.close < gate["low"] if direction == "BULL" else candle.close > gate["high"]
                if continuation:
                    counts["life_death_gate_armed"] += 1
                    counts["mode_switches"] += 1
                    active = None
                    cooldown_until = index + 2
                    continue
                if reversal:
                    counts["momentum_failed"] += 1
                    counts["mode_switches"] += 1
                    active = None
                    cooldown_until = index + 2
                    continue
            age = index - active["confirmed_at"]
            if active["gate"] is None and age >= 3:
                recent = candles[index - 2 : index + 1]
                recent_median = statistics.median(item.high - item.low for item in recent)
                if recent_median <= active["impulse_range"] * 0.75:
                    active["gate"] = {
                        "formed_at": index,
                        "high": max(item.high for item in recent),
                        "low": min(item.low for item in recent),
                    }
                    counts["life_death_gate_forming"] += 1
            if age >= 12:
                counts["exhaustion_warning"] += 1
                counts["mode_switches"] += 1
                active = None
                cooldown_until = index + 2
            continue

        if index < 20 or index <= cooldown_until:
            continue
        prior = candles[index - 1]
        direction = _same_direction(prior, candle)
        if direction is None:
            continue
        baseline = candles[index - 20 : index]
        median_body = statistics.median(abs(item.close - item.open) for item in baseline)
        median_range = statistics.median(item.high - item.low for item in baseline)
        if median_body <= 0 or median_range <= 0:
            continue
        expanded = all(
            abs(item.close - item.open) >= median_body * 1.25
            and item.high - item.low >= median_range * 1.2
            for item in (prior, candle)
        )
        history = candles[index - 10 : index]
        breaks = candle.close > max(item.high for item in history) if direction == "BULL" else candle.close < min(item.low for item in history)
        if not expanded or not breaks:
            continue
        counts["centrifugal_confirmed"] += 1
        counts["mode_switches"] += 1
        active = {
            "direction": direction,
            "confirmed_at": index,
            "pre_boundary": min(prior.low, candle.low) if direction == "BULL" else max(prior.high, candle.high),
            "impulse_range": statistics.median([prior.high - prior.low, candle.high - candle.low]),
            "gate": None,
        }
    return counts


def _same_direction(left: Candle, right: Candle) -> str | None:
    if left.close > left.open and right.close > right.open and right.close > left.close:
        return "BULL"
    if left.close < left.open and right.close < right.open and right.close < left.close:
        return "BEAR"
    return None


def _ratio_relation(current: float, base: float, *, higher_is_stronger: bool) -> str:
    if base <= 0:
        return "UNKNOWN"
    ratio = current / base
    if 0.9 <= ratio <= 1.1:
        return "SIMILAR"
    stronger = ratio > 1.1 if higher_is_stronger else ratio < 0.9
    return "STRONGER" if stronger else "WEAKER"


def _destructive_relation(base: ReplayLeg, copy: ReplayLeg) -> str:
    difference = copy.end_price - base.end_price
    if abs(difference) < 1e-9:
        return "SIMILAR"
    stronger = difference > 0 if copy.direction == "BULL" else difference < 0
    return "STRONGER" if stronger else "WEAKER"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Causal 戰法 C 班 n=2/n=3 replay diagnostics")
    parser.add_argument("--html", type=Path, required=True)
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    args = parser.parse_args(argv)
    meta, candles = load_candles_from_html(args.html)
    report = compare_cclass_parameters(candles)
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
