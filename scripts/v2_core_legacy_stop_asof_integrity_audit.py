"""Audit whether a legacy episode stop had been closed through by signal date.

This is an offline AS-OF source-integrity check, not a trading backtest.
No post-signal bars or PnL are read.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.v2_core_legacy_role_shortlist_r7 import ARTIFACT_DIR, canonical_bytes, load_json, write_new_or_identical


VERSION = "v2-core-legacy-stop-asof-integrity-audit-r1"


def first_closing_breach(
    daily_rows: list[dict[str, Any]], *, stop_price: float,
    after_date: str, before_date: str,
) -> dict[str, Any] | None:
    rows = [
        {"date": item["date"], "close": item["close"], "low": item["low"]}
        for item in daily_rows
        if after_date < item["date"] < before_date and item["close"] < stop_price
    ]
    return min(rows, key=lambda item: item["date"]) if rows else None


def build_report(artifact_dir: Path) -> dict[str, Any]:
    cases = load_json(artifact_dir / "legacy_teacher_decision_trace_candidate_r1.json")["cases"]
    rows = []
    for item in cases:
        review_id = item["review_id"]
        teacher = item["teacher"]
        packet = load_json(artifact_dir / "legacy_anchor_alignment_input_packets_candidate_r1" / f"{review_id}.json")
        as_of = packet["as_of"]
        if teacher["signal_date"] != as_of or item["as_of"] != as_of:
            raise ValueError("teacher/packet AS-OF mismatch")
        daily = packet["daily_context_to_as_of"]
        if any(bar["date"] > as_of for bar in daily):
            raise ValueError("future bar in AS-OF packet")
        stop_date, stop_price = teacher["stop_date"], teacher["stop_price"]
        pivots = [
            pivot for pivot in packet["confirmed_pivots_to_as_of"]
            if pivot["side"] == "LOW" and pivot["source_date"] == stop_date
            and pivot["price"] == stop_price
        ]
        if not pivots:
            raise ValueError(f"teacher stop not a confirmed low: {review_id}")
        # The same dated low can be confirmed at SMALL and LARGE scales. A
        # trade-episode defense uses the earliest confirmed SMALL witness.
        pivot = min(pivots, key=lambda item: (item["scale"] != "SMALL", item["confirmation_date"]))
        if pivot["confirmation_date"] > as_of:
            raise ValueError("stop confirmed after signal")
        breach = first_closing_breach(
            daily, stop_price=stop_price,
            after_date=pivot["confirmation_date"], before_date=as_of,
        )
        signal = next(bar for bar in daily if bar["date"] == as_of)
        rows.append({
            "review_id": review_id,
            "as_of": as_of,
            "legacy_scenario": teacher["scenario"],
            "legacy_trade_role": teacher["episode_or_add_candidate"],
            "stop_source_date": stop_date,
            "stop_confirmation_date": pivot["confirmation_date"],
            "matching_pivot_scales": sorted({item["scale"] for item in pivots}),
            "stop_price": stop_price,
            "first_closing_breach_after_confirmation_before_signal": breach,
            "had_prior_closing_breach": breach is not None,
            "signal_close": signal["close"],
            "signal_close_below_stop": signal["close"] < stop_price,
            "future_bars_used": False,
        })
    return {
        "report_version": VERSION,
        "scope": "FOURTEEN_LEGACY_FINAL_AI_POSITIVE_CALIBRATION_CASES",
        "case_count": len(rows),
        "prior_closing_breach_count": sum(row["had_prior_closing_breach"] for row in rows),
        "signal_close_below_stop_count": sum(row["signal_close_below_stop"] for row in rows),
        "future_performance_used": False,
        "locked_reproduction_set_opened": False,
        "teacher_answers_used_offline_after_formal_outputs": True,
        "not_a_backtest_or_formal_reproduction_rate": True,
        "rows": rows,
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# 舊最終 AI 14 筆防線的 AS-OF 收盤失效稽核", "",
        "這是來源完整性稽核，不是新回測。逐筆只讀訊號日及之前的匿名日 K、舊最終紀錄的防線與確認樞紐；不讀期後行情或損益。`先前收盤跌破`指**防線確認後、訊號日前**至少一日收盤嚴格小於所選價位。", "",
        f"- {report['case_count']} 筆中，選定防線曾於訊號日前被收盤跌破：{report['prior_closing_breach_count']} 筆。",
        f"- 訊號日收盤本身低於所選防線：{report['signal_close_below_stop_count']} 筆。", "",
        "| 案例／訊號日 | 舊交易角色／情境 | 舊防線（來源／確認／價格） | 確認後至訊號前首次收盤跌破 | 訊號收盤 |",
        "| --- | --- | --- | --- | ---: |",
    ]
    for row in report["rows"]:
        breach = row["first_closing_breach_after_confirmation_before_signal"]
        breach_text = f"{breach['date']} 收 {breach['close']}（低 {breach['low']}）" if breach else "無"
        lines.append(
            f"| {row['review_id']}／{row['as_of']} | {row['legacy_trade_role']}／{row['legacy_scenario']} | "
            f"{row['stop_source_date']}／{row['stop_confirmation_date']}／{row['stop_price']} | {breach_text} | {row['signal_close']} |"
        )
    lines.extend([
        "", "若舊防線先前已被收盤跌破，不能未經新 episode、防線升級或重置的證據，直接把該舊答案當不可違背的標準；但本稽核也**不自動證明當天完全沒有其他合法新防線可交易**。加碼案例另需原持倉資料，不能只靠防線價評斷加碼資格。", "",
    ])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-dir", type=Path, default=ARTIFACT_DIR)
    args = parser.parse_args()
    report = build_report(args.artifact_dir)
    output = args.artifact_dir / "legacy_stop_asof_integrity_audit_candidate_r1.json"
    write_new_or_identical(output, canonical_bytes(report))
    write_new_or_identical(output.with_suffix(".md"), render_markdown(report).encode("utf-8"))
    print(json.dumps({key: report[key] for key in (
        "case_count", "prior_closing_breach_count", "signal_close_below_stop_count",
    )}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
