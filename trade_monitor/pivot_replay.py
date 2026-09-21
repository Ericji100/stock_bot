from __future__ import annotations

import argparse
import json
import statistics
from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo


TAIPEI = ZoneInfo("Asia/Taipei")


@dataclass(frozen=True)
class Candle:
    at: datetime
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class LocalPivot:
    pivot_id: str
    kind: str
    bar_index: int
    confirmation_index: int
    bar_time: str
    confirmation_time: str
    price: float
    delay_bars: int


def load_candles_from_html(path: Path) -> tuple[dict[str, Any], list[Candle]]:
    text = path.read_text(encoding="utf-8")
    marker = "const payload = "
    start = text.find(marker)
    if start < 0:
        raise ValueError("embedded chart payload was not found")
    payload, _ = json.JSONDecoder().raw_decode(text[start + len(marker) :])
    if not isinstance(payload, dict) or not isinstance(payload.get("candles"), list):
        raise ValueError("embedded chart payload is invalid")
    candles = [
        Candle(
            # The exported HTML intentionally formats the Unix timestamp's UTC
            # clock components as Taiwan futures session time (08:45/15:00).
            # Preserve those displayed clock components and attach +08:00.
            at=datetime.fromtimestamp(int(item["time"]), timezone.utc).replace(tzinfo=TAIPEI),
            open=float(item["open"]),
            high=float(item["high"]),
            low=float(item["low"]),
            close=float(item["close"]),
        )
        for item in payload["candles"]
    ]
    return dict(payload.get("meta") or {}), candles


def split_sessions(candles: Iterable[Candle]) -> dict[str, list[Candle]]:
    groups: dict[str, list[Candle]] = {}
    for candle in candles:
        key = session_key(candle.at)
        if key is not None:
            groups.setdefault(key, []).append(candle)
    return groups


def session_key(at: datetime) -> str | None:
    local = at.astimezone(TAIPEI)
    clock = local.time().replace(tzinfo=None)
    day = local.date()
    if time(8, 45) <= clock <= time(13, 45):
        return f"{day.isoformat()}-DAY"
    if clock >= time(15, 0):
        return f"{day.isoformat()}-NIGHT"
    if clock < time(8, 45):
        return f"{(day - timedelta(days=1)).isoformat()}-NIGHT"
    return None


def detect_local_pivots(candles: list[Candle], n: int) -> tuple[list[LocalPivot], dict[str, int]]:
    if n < 1:
        raise ValueError("pivot n must be positive")
    pivots: list[LocalPivot] = []
    candidates = invalid_right = missing_breach = 0
    for index in range(n, len(candles)):
        candle = candles[index]
        for kind in ("HIGH", "LOW"):
            left = candles[index - n : index]
            is_candidate = (
                all(item.high < candle.high for item in left)
                if kind == "HIGH"
                else all(item.low > candle.low for item in left)
            )
            if not is_candidate:
                continue
            candidates += 1
            if index + n >= len(candles):
                invalid_right += 1
                continue
            right = candles[index + 1 : index + n + 1]
            right_valid = (
                all(item.high < candle.high for item in right)
                if kind == "HIGH"
                else all(item.low > candle.low for item in right)
            )
            if not right_valid:
                invalid_right += 1
                continue
            breach = next(
                (
                    cursor
                    for cursor in range(index + 1, len(candles))
                    if (candles[cursor].close < candle.low if kind == "HIGH" else candles[cursor].close > candle.high)
                ),
                None,
            )
            if breach is None:
                missing_breach += 1
                continue
            confirmation = max(index + n, breach)
            pivots.append(
                LocalPivot(
                    pivot_id=f"P1-{kind[0]}-{int(candle.at.timestamp())}",
                    kind=kind,
                    bar_index=index,
                    confirmation_index=confirmation,
                    bar_time=candle.at.isoformat(),
                    confirmation_time=candles[confirmation].at.isoformat(),
                    price=candle.high if kind == "HIGH" else candle.low,
                    delay_bars=confirmation - index,
                )
            )
    pivots.sort(key=lambda item: (item.confirmation_index, item.bar_index, item.kind))
    return pivots, {
        "candidates": candidates,
        "invalid_or_incomplete_right": invalid_right,
        "right_valid_without_close_breach": missing_breach,
    }


def pair_pivots(local_pivots: list[LocalPivot]) -> tuple[list[LocalPivot], int, int]:
    sequence: list[LocalPivot] = []
    replacements = ignored_same_side = 0
    for pivot in local_pivots:
        if not sequence:
            sequence.append(pivot)
            continue
        last = sequence[-1]
        if pivot.kind != last.kind:
            sequence.append(pivot)
            continue
        more_extreme = pivot.price > last.price if pivot.kind == "HIGH" else pivot.price < last.price
        if more_extreme:
            sequence[-1] = pivot
            replacements += 1
        else:
            ignored_same_side += 1
    return sequence, replacements, ignored_same_side


def secondary_pivots(primary_finalized: list[LocalPivot]) -> list[LocalPivot]:
    result: list[LocalPivot] = []
    for kind in ("HIGH", "LOW"):
        same = [item for item in primary_finalized if item.kind == kind]
        for index in range(1, len(same) - 1):
            left, middle, right = same[index - 1 : index + 2]
            qualifies = (
                middle.price > left.price and middle.price > right.price
                if kind == "HIGH"
                else middle.price < left.price and middle.price < right.price
            )
            if qualifies:
                result.append(
                    LocalPivot(
                        pivot_id=f"P2-{kind[0]}-{int(datetime.fromisoformat(middle.bar_time).timestamp())}",
                        kind=kind,
                        bar_index=middle.bar_index,
                        confirmation_index=right.confirmation_index,
                        bar_time=middle.bar_time,
                        confirmation_time=right.confirmation_time,
                        price=middle.price,
                        delay_bars=right.confirmation_index - middle.bar_index,
                    )
                )
    return sorted(result, key=lambda item: (item.confirmation_index, item.bar_index, item.kind))


def dow_state(pivots: list[LocalPivot]) -> str:
    highs = [item.price for item in pivots if item.kind == "HIGH"]
    lows = [item.price for item in pivots if item.kind == "LOW"]
    if len(highs) < 2 or len(lows) < 2:
        return "UNDEFINED"
    if highs[-1] > highs[-2] and lows[-1] >= lows[-2]:
        return "BULL"
    if lows[-1] < lows[-2] and highs[-1] <= highs[-2]:
        return "BEAR"
    return "TRANSITION"


def type1_proxy(primary_sequence: list[LocalPivot]) -> dict[str, Any]:
    finalized = primary_sequence[:-1]
    snapshots: list[dict[str, Any]] = []
    for count in range(1, len(finalized) + 1):
        prefix = finalized[:count]
        small = dow_state(prefix)
        large = dow_state(secondary_pivots(prefix))
        snapshots.append({"index": prefix[-1].confirmation_index, "small": small, "large": large})
    warnings: list[dict[str, Any]] = []
    conflict_active = False
    for position, snap in enumerate(snapshots):
        conflict = snap["small"] in {"BULL", "BEAR"} and snap["large"] in {"BULL", "BEAR"} and snap["small"] != snap["large"]
        if conflict and not conflict_active:
            outcome = "UNRESOLVED"
            for later in snapshots[position + 1 :]:
                if later["large"] == snap["small"]:
                    outcome = "CONFIRMED"
                    break
                if later["small"] == snap["large"]:
                    outcome = "FALSE_WARNING"
                    break
            warnings.append({"bar_index": snap["index"], "direction": snap["small"], "outcome": outcome})
        conflict_active = conflict
    resolved = [item for item in warnings if item["outcome"] != "UNRESOLVED"]
    false_count = sum(item["outcome"] == "FALSE_WARNING" for item in resolved)
    return {
        "warning_count": len(warnings),
        "resolved_count": len(resolved),
        "false_warning_count": false_count,
        "false_warning_rate": None if not resolved else round(false_count / len(resolved), 4),
        "definition": "Proxy: primary Dow turns opposite a defined secondary Dow; false if primary returns before secondary confirms the turn.",
    }


def analyze_session(candles: list[Candle], n: int) -> dict[str, Any]:
    local, candidate_stats = detect_local_pivots(candles, n)
    paired_sequence, replacements, ignored = pair_pivots(local)
    finalized = paired_sequence[:-1]
    delays = [item.delay_bars for item in local]
    return {
        "bar_count": len(candles),
        **candidate_stats,
        "local_confirmed": len(local),
        "paired_confirmed": len(finalized),
        "secondary_pivots": len(secondary_pivots(finalized)),
        "same_side_replacements": replacements,
        "same_side_ignored": ignored,
        "replacement_rate": None if not local else round(replacements / len(local), 4),
        "local_pivots_per_1000_bars": None if not candles else round(len(local) * 1000 / len(candles), 3),
        "median_confirmation_delay_bars": None if not delays else round(float(statistics.median(delays)), 3),
        "mean_confirmation_delay_bars": None if not delays else round(float(statistics.mean(delays)), 3),
        "type1_proxy": type1_proxy(paired_sequence),
        "local_keys": {(item.kind, item.bar_time) for item in local},
    }


def compare_parameters(candles: list[Candle], parameters: tuple[int, ...] = (2, 3)) -> dict[str, Any]:
    sessions = split_sessions(candles)
    aggregate: dict[int, dict[str, Any]] = {}
    session_rows: list[dict[str, Any]] = []
    segment_bar_counts: dict[str, int] = {}
    for key, rows in sessions.items():
        for candle in rows:
            label = _segment_label(key, candle.at)
            segment_bar_counts[label] = segment_bar_counts.get(label, 0) + 1
    segment_results: dict[str, dict[str, Any]] = {}
    for n in parameters:
        totals: dict[str, float] = {
            "bar_count": 0, "candidates": 0, "local_confirmed": 0, "paired_confirmed": 0,
            "secondary_pivots": 0, "same_side_replacements": 0, "type1_warnings": 0,
            "type1_resolved": 0, "type1_false": 0,
        }
        delays: list[float] = []
        segment_events: dict[str, list[LocalPivot]] = {label: [] for label in segment_bar_counts}
        for key, rows in sorted(sessions.items()):
            stats = analyze_session(rows, n)
            session_rows.append({"session_key": key, "n": n, **_jsonable_stats(stats)})
            for field in ("bar_count", "candidates", "local_confirmed", "paired_confirmed", "secondary_pivots", "same_side_replacements"):
                totals[field] += float(stats[field])
            proxy = stats["type1_proxy"]
            totals["type1_warnings"] += proxy["warning_count"]
            totals["type1_resolved"] += proxy["resolved_count"]
            totals["type1_false"] += proxy["false_warning_count"]
            if stats["median_confirmation_delay_bars"] is not None:
                local_events = detect_local_pivots(rows, n)[0]
                delays.extend(item.delay_bars for item in local_events)
                for item in local_events:
                    segment_events[_segment_label(key, rows[item.bar_index].at)].append(item)
        bars = int(totals["bar_count"])
        local_count = int(totals["local_confirmed"])
        resolved = int(totals["type1_resolved"])
        aggregate[n] = {
            **{key: int(value) for key, value in totals.items()},
            "session_count": len(sessions),
            "local_pivots_per_1000_bars": None if not bars else round(local_count * 1000 / bars, 3),
            "median_confirmation_delay_bars": None if not delays else round(float(statistics.median(delays)), 3),
            "mean_confirmation_delay_bars": None if not delays else round(float(statistics.mean(delays)), 3),
            "type1_false_warning_rate": None if not resolved else round(int(totals["type1_false"]) / resolved, 4),
        }
        for label, events in segment_events.items():
            row = segment_results.setdefault(label, {"bar_count": segment_bar_counts[label]})
            segment_delays = [item.delay_bars for item in events]
            row[str(n)] = {
                "local_confirmed": len(events),
                "local_pivots_per_1000_bars": round(len(events) * 1000 / segment_bar_counts[label], 3),
                "median_confirmation_delay_bars": None if not segment_delays else round(float(statistics.median(segment_delays)), 3),
                "mean_confirmation_delay_bars": None if not segment_delays else round(float(statistics.mean(segment_delays)), 3),
            }

    n2_keys = set().union(*(analyze_session(rows, 2)["local_keys"] for rows in sessions.values())) if 2 in parameters else set()
    n3_keys = set().union(*(analyze_session(rows, 3)["local_keys"] for rows in sessions.values())) if 3 in parameters else set()
    overlap = {
        "common_local_pivots": len(n2_keys & n3_keys),
        "n2_only_local_pivots": len(n2_keys - n3_keys),
        "n3_only_local_pivots": len(n3_keys - n2_keys),
        "jaccard": None if not (n2_keys | n3_keys) else round(len(n2_keys & n3_keys) / len(n2_keys | n3_keys), 4),
    }
    return {
        "parameters": list(parameters),
        "aggregate": {str(key): value for key, value in aggregate.items()},
        "overlap": overlap,
        "segments": segment_results,
        "sessions": session_rows,
        "limitations": [
            "This compares causal pivot mechanics only; it is not a profitability backtest.",
            "Type 1 false-warning rate is an explicitly defined structural proxy, not a course-claimed win rate.",
            "Costs, slippage, four-pattern admission, quadrants and exits are not included.",
        ],
    }


def render_markdown(report: dict[str, Any], *, source: Path, meta: dict[str, Any]) -> str:
    lines = [
        "# 道氏樞紐參數 n=2／n=3 回放比較",
        "",
        f"- 來源：`{source}`",
        f"- 資料標題：{meta.get('title', '未知')}",
        f"- 1 分 K 根數：{meta.get('barCount', '未知')}",
        f"- 納入交易時段根數：{report['aggregate']['2']['bar_count']}",
        "",
        "| 指標 | n=2 | n=3 |",
        "|---|---:|---:|",
    ]
    labels = [
        ("local_confirmed", "局部成立樞紐"),
        ("paired_confirmed", "配對確認樞紐"),
        ("secondary_pivots", "二級樞紐"),
        ("local_pivots_per_1000_bars", "每千根局部樞紐"),
        ("median_confirmation_delay_bars", "確認延遲中位數（根）"),
        ("same_side_replacements", "同向更極端點取代"),
        ("type1_warnings", "Type 1 proxy 警告"),
        ("type1_false_warning_rate", "Type 1 proxy 假警告率"),
    ]
    for key, label in labels:
        lines.append(f"| {label} | {report['aggregate']['2'].get(key)} | {report['aggregate']['3'].get(key)} |")
    overlap = report["overlap"]
    lines += [
        "",
        "## 重疊度",
        "",
        f"- 共同局部樞紐：{overlap['common_local_pivots']}",
        f"- 僅 n=2：{overlap['n2_only_local_pivots']}",
        f"- 僅 n=3：{overlap['n3_only_local_pivots']}",
        f"- Jaccard：{overlap['jaccard']}",
        "",
        "## 日／夜盤與時段密度",
        "",
        "| 時段 | 根數 | n=2 局部樞紐／千根 | n=3 局部樞紐／千根 |",
        "|---|---:|---:|---:|",
    ]
    for label, row in report["segments"].items():
        lines.append(
            f"| {label} | {row['bar_count']} | {row['2']['local_pivots_per_1000_bars']} | {row['3']['local_pivots_per_1000_bars']} |"
        )
    lines += [
        "",
        "## 限制",
        "",
    ]
    lines.extend(f"- {item}" for item in report["limitations"])
    return "\n".join(lines) + "\n"


def _jsonable_stats(stats: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in stats.items() if key != "local_keys"}


def _segment_label(session: str, at: datetime) -> str:
    local = at.astimezone(TAIPEI)
    if session.endswith("-DAY"):
        return "DAY_FIRST_HOUR" if local.time().replace(tzinfo=None) < time(9, 45) else "DAY_GENERAL"
    clock = local.time().replace(tzinfo=None)
    return "NIGHT_FIRST_HOUR" if time(15, 0) <= clock < time(16, 0) else "NIGHT_GENERAL"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Causal n=2/n=3 pivot replay comparison")
    parser.add_argument("--html", type=Path, required=True)
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    args = parser.parse_args(argv)
    meta, candles = load_candles_from_html(args.html)
    report = compare_parameters(candles)
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
