"""Extract outcome-free V2 calibration positives and hard negatives.

Only rows already assigned to ``CALIBRATION_SET`` are read.  The script never
opens a backtest/performance artifact or the sealed locked-answer directory.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

try:
    from scripts.v2_core_dataset_split_v1 import canonical_bytes
    from scripts.v2_core_source_audit_v1 import BATCHES, read_jsonl, sha256
except ModuleNotFoundError:  # Direct ``python scripts\\...`` execution.
    from v2_core_dataset_split_v1 import canonical_bytes
    from v2_core_source_audit_v1 import BATCHES, read_jsonl, sha256


CASE_VERSION = "v2-core-calibration-cases-v2"
FORBIDDEN_RESULT_KEYS = {
    "mfe",
    "mae",
    "pnl",
    "profit",
    "return_pct",
    "exit_date",
    "exit_price",
    "future_outcome",
}


def load_calibration_rows(
    repo_root: Path, output_dir: Path
) -> list[tuple[dict[str, Any], Path]]:
    manifest = json.loads(
        (output_dir / "calibration_manifest.json").read_text(encoding="utf-8")
    )
    allowed = {
        (str(row["batch_id"]), int(row["line_index"])) for row in manifest["rows"]
    }
    selected: list[tuple[dict[str, Any], Path]] = []
    for batch_id in ("889", "1029"):
        run = (repo_root / BATCHES[batch_id]).resolve()
        rows = read_jsonl(run / "ai_decisions_merged.jsonl")
        for line_index, decision in enumerate(rows, 1):
            if (batch_id, line_index) in allowed:
                selected.append(
                    (
                        {
                            "batch_id": batch_id,
                            "line_index": line_index,
                            "decision": decision,
                        },
                        run,
                    )
                )
    if len(selected) != len(allowed):
        raise RuntimeError(
            f"calibration row mismatch: selected={len(selected)} allowed={len(allowed)}"
        )
    return selected


def compact_trigger(trigger: dict[str, Any]) -> dict[str, Any]:
    return {
        key: trigger.get(key)
        for key in (
            "signal_date",
            "scenario",
            "trigger_path",
            "episode_or_add_candidate",
            "macro_anchor",
            "small_structure",
            "taiji_generation",
            "large_quadrant",
            "small_quadrant",
            "large_dow",
            "small_dow",
            "left_right",
            "stop_date",
            "stop_price",
            "evidence",
            "invalidation",
            "required_gates",
        )
        if key in trigger
    }


def case_id(batch_id: str, code: str, date: str, kind: str) -> str:
    import hashlib

    value = f"{CASE_VERSION}|{batch_id}|{code}|{date}|{kind}"
    return "C-" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def build_case(
    *,
    batch_id: str,
    line_index: int,
    decision: dict[str, Any],
    packet_path: Path,
    daily: dict[str, Any],
    kind: str,
    expected_permission: str,
    legacy_trigger: dict[str, Any] | None = None,
    legacy_reason: str | None = None,
) -> dict[str, Any]:
    date = str(daily["date"])
    return {
        "case_version": CASE_VERSION,
        "case_id": case_id(batch_id, str(decision["code"]), date, kind),
        "case_kind": kind,
        "expected_permission": expected_permission,
        "batch_id": batch_id,
        "source_line_index": line_index,
        "code": str(decision["code"]),
        "name": decision.get("name"),
        "as_of": date,
        "monitor_on": decision.get("first_selected_on"),
        "packet_sha256": (decision.get("audit") or {}).get("packet_sha256"),
        "packet_path": str(packet_path),
        "daily_visible_fact": daily,
        "legacy_v2_trigger": compact_trigger(legacy_trigger) if legacy_trigger else None,
        "legacy_v2_no_trade_reason": legacy_reason,
        "future_performance_included": False,
    }


def objective_challenge_score(daily: dict[str, Any]) -> int:
    """Rank trigger-like days without assigning a scenario or approval."""

    weights = {
        "CLOSE_BREAK_LAST_CONFIRMED_LARGE_PIVOT_HIGH": 8,
        "CLOSE_BREAK_LAST_CONFIRMED_SMALL_PIVOT_HIGH": 6,
        "RECLAIM_MA144": 5,
        "RECLAIM_MA105": 5,
        "RECLAIM_MA55": 4,
        "RECLAIM_MA21": 3,
        "RECLAIM_MA13": 2,
        "RECLAIM_MA5": 1,
        "UPSTREAM_SELECTED_TODAY": 1,
    }
    return sum(weights.get(str(fact), 0) for fact in daily.get("facts") or [])


def extract_cases(repo_root: Path, output_dir: Path) -> list[dict[str, Any]]:
    cases: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for source, run in load_calibration_rows(repo_root, output_dir):
        batch_id = source["batch_id"]
        line_index = source["line_index"]
        decision = source["decision"]
        code = str(decision["code"])
        packet_path = run / "review_packets" / f"{code}.json"
        if sha256(packet_path) != (decision.get("audit") or {}).get("packet_sha256"):
            raise RuntimeError(f"packet hash changed: {batch_id}:{code}")
        packet = json.loads(packet_path.read_text(encoding="utf-8-sig"))
        daily_rows = packet.get("daily_visible_facts_from_monitoring") or []
        by_date = {str(row["date"]): row for row in daily_rows}
        dates = list(by_date)
        date_index = {date: index for index, date in enumerate(dates)}

        v2 = decision.get("v2") or {}
        v2_triggers = v2.get("triggers") or []
        v2_dates = {str(trigger["signal_date"]) for trigger in v2_triggers}

        for trigger in v2_triggers:
            date = str(trigger["signal_date"])
            if date not in by_date:
                raise RuntimeError(f"missing daily fact for V2 trigger: {batch_id}:{code}:{date}")
            key = (batch_id, code, date, "POSITIVE_REFERENCE")
            cases[key] = build_case(
                batch_id=batch_id,
                line_index=line_index,
                decision=decision,
                packet_path=packet_path,
                daily=by_date[date],
                kind="POSITIVE_REFERENCE（舊V2核准正例）",
                expected_permission="TRADE_APPROVED（核准交易）",
                legacy_trigger=trigger,
            )

            center = date_index[date]
            for neighbor_index in (center - 1, center + 1):
                if neighbor_index < 0 or neighbor_index >= len(dates):
                    continue
                neighbor_date = dates[neighbor_index]
                if neighbor_date in v2_dates:
                    continue
                key = (batch_id, code, neighbor_date, "NEARBY")
                cases.setdefault(
                    key,
                    build_case(
                        batch_id=batch_id,
                        line_index=line_index,
                        decision=decision,
                        packet_path=packet_path,
                        daily=by_date[neighbor_date],
                        kind="HARD_NEGATIVE_NEARBY（同股相鄰未觸發日）",
                        expected_permission="WAIT（等待）",
                        legacy_reason="相鄰交易日未列入舊V2 triggers；只作邊界參考，不表示舊AI曾逐欄提交WAIT。",
                    ),
                )

        # V1 approved but V2 did not approve that day: a high-value V2 negative
        # and a future input to the separate opportunity-recovery research.
        for trigger in (decision.get("v1") or {}).get("triggers") or []:
            date = str(trigger["signal_date"])
            if date in v2_dates or date not in by_date:
                continue
            key = (batch_id, code, date, "V1_ONLY")
            cases.setdefault(
                key,
                build_case(
                    batch_id=batch_id,
                    line_index=line_index,
                    decision=decision,
                    packet_path=packet_path,
                    daily=by_date[date],
                    kind="HARD_NEGATIVE_V1_ONLY（V1核准但V2未核准）",
                    expected_permission="WAIT（等待）",
                    legacy_reason=(v2.get("no_trade_reason") or "V2在同日沒有核准trigger。"),
                ),
            )

        # For a stock with no V2 approval, retain only the strongest objective
        # trigger-like day.  This is a challenge selected from causal facts; it
        # is not represented as an explicit, fully structured AI WAIT answer.
        no_trade_reason = str(v2.get("no_trade_reason") or "")
        if not v2_triggers:
            challenges = [
                row for row in daily_rows if objective_challenge_score(row) > 0
            ]
            if challenges:
                chosen = max(
                    challenges,
                    key=lambda row: (objective_challenge_score(row), str(row["date"])),
                )
                date = str(chosen["date"])
                key = (batch_id, code, date, "OBJECTIVE_CHALLENGE")
                cases.setdefault(
                    key,
                    build_case(
                        batch_id=batch_id,
                        line_index=line_index,
                        decision=decision,
                        packet_path=packet_path,
                        daily=chosen,
                        kind="HARD_NEGATIVE_OBJECTIVE_CHALLENGE（客觀觸發型不交易挑戰日）",
                        expected_permission="LEGACY_NO_TRIGGER（舊V2未觸發）",
                        legacy_reason=(
                            no_trade_reason
                            + " 客觀挑戰日由因果flags排序產生；不表示舊AI曾在該日逐欄提交WAIT。"
                        ).strip(),
                    ),
                )

    return sorted(
        cases.values(),
        key=lambda row: (row["batch_id"], row["code"], row["as_of"], row["case_kind"]),
    )


def recursive_keys(value: Any) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            keys.add(str(key).lower())
            keys.update(recursive_keys(child))
    elif isinstance(value, list):
        for child in value:
            keys.update(recursive_keys(child))
    return keys


def summarize(cases: list[dict[str, Any]]) -> dict[str, Any]:
    kinds = Counter(row["case_kind"] for row in cases)
    scenarios = Counter(
        row["legacy_v2_trigger"]["scenario"]
        for row in cases
        if row.get("legacy_v2_trigger")
    )
    return {
        "cases": len(cases),
        "stocks": len({(row["batch_id"], row["code"]) for row in cases}),
        "case_kinds": dict(sorted(kinds.items())),
        "positive_scenarios": dict(sorted(scenarios.items())),
        "forbidden_result_keys_found": sorted(recursive_keys(cases) & FORBIDDEN_RESULT_KEYS),
    }


def write_new_or_identical(path: Path, data: bytes) -> None:
    if path.exists() and path.read_bytes() != data:
        raise RuntimeError(f"refusing to overwrite changed artifact: {path}")
    path.write_bytes(data)


def render_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# V2四情境校準日案例清單",
        "",
        f"版本：`{CASE_VERSION}`  ",
        "狀態：`CALIBRATION_REFERENCE（校準參考）`",
        "",
        "## 摘要",
        "",
        f"- 案例日：{summary['cases']}",
        f"- 涉及批次／股票列：{summary['stocks']}",
        f"- 類型：{summary['case_kinds']}",
        f"- V2正例情境：{summary['positive_scenarios']}",
        f"- 禁止績效欄位：{summary['forbidden_result_keys_found']}",
        "",
        "## 標籤語意",
        "",
        "- `POSITIVE_REFERENCE（舊V2核准正例）`：舊V2明確trigger及其完整gate／證據。",
        "- `HARD_NEGATIVE_NEARBY（同股相鄰未觸發日）`：正例前後一個交易日沒有V2 trigger；只作邊界參考，不冒充逐欄WAIT答案。",
        "- `HARD_NEGATIVE_V1_ONLY（V1核准但V2未核准）`：用於找出V2硬門檻與V1裁量差異，亦是V1機會回收研究來源。",
        "- `HARD_NEGATIVE_OBJECTIVE_CHALLENGE（客觀觸發型不交易挑戰日）`：無V2交易股票中，以突破／收復等因果flags選出最像觸發的一日；它不是舊AI逐欄WAIT答案。",
        "",
        "## 限制",
        "",
        "- 本清單只來自CALIBRATION_SET，不讀取sealed鎖定答案。",
        "- 不含MFE、MAE、損益、出場或其他未來績效。",
        "- 相鄰未觸發日不等於舊AI曾在該日提交完整結構化WAIT；正式語意萃取時不得過度解讀。",
        "- 每個案例仍須回到原review packet及舊AI證據，不可只以單日均線旗標判讀。",
        "",
    ]
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--repo-root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output = args.output_dir.resolve()
    cases = extract_cases(args.repo_root.resolve(), output)
    summary = summarize(cases)
    write_new_or_identical(
        output / "calibration_daily_cases_v2.jsonl",
        b"".join(canonical_bytes(row) for row in cases),
    )
    write_new_or_identical(
        output / "calibration_daily_cases_manifest_v2.json",
        canonical_bytes(
            {
                "manifest_version": CASE_VERSION,
                "classification": "CALIBRATION_REFERENCE（校準參考）",
                "future_performance_included": False,
                "summary": summary,
                "source_sha256": sha256(output / "calibration_manifest.json"),
            }
        ),
    )
    write_new_or_identical(
        output / "calibration_daily_cases_manifest_v2.md",
        render_markdown(summary).encode("utf-8"),
    )
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
