"""Validated first-stage Q2/Q4 calibration case definitions."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any


DEFAULT_CATALOG = (
    Path(__file__).resolve().parent
    / "rules"
    / "course-state-v2.1.8-replay-adapter-v34-long-only-q2-q4"
    / "stage1-case-catalog.json"
)


class Stage1CaseError(ValueError):
    pass


def load_stage1_cases(path: Path = DEFAULT_CATALOG) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise Stage1CaseError("第一階段案例目錄無法讀取。") from exc
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise Stage1CaseError("第一階段案例目錄版本無效。")
    scope = payload.get("scope")
    if not isinstance(scope, dict) or scope != {
        "instrument": "TMF",
        "session": "DAY",
        "trade_direction_policy": "LONG_ONLY",
        "trade_setup_policy": "LONG_Q2_Q4_ONLY",
        "dataset_role": "CALIBRATION",
    }:
        raise Stage1CaseError("第一階段案例目錄範圍不是固定的TMF日盤多方Q2／Q4校正集。")
    raw_cases = payload.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise Stage1CaseError("第一階段案例目錄不得為空。")
    cases = [_validate_case(item) for item in raw_cases]
    ids = [str(item["case_id"]) for item in cases]
    if len(ids) != len(set(ids)):
        raise Stage1CaseError("第一階段case_id不得重複。")
    return cases


def analysis_times(case: dict[str, Any]) -> list[time]:
    start = _hhmm(case["analysis_start"], "analysis_start")
    end = _hhmm(case["analysis_end"], "analysis_end")
    step = int(case["analysis_every_bars"])
    cursor = datetime.combine(date(2000, 1, 1), start)
    finish = datetime.combine(date(2000, 1, 1), end)
    values: list[time] = []
    while cursor <= finish:
        values.append(cursor.time())
        cursor += timedelta(minutes=step)
    return values


def _validate_case(value: Any) -> dict[str, Any]:
    required = {
        "case_id",
        "target_date",
        "locator_time",
        "family_hypothesis",
        "analysis_start",
        "analysis_end",
        "analysis_every_bars",
        "audit_focus",
    }
    forbidden = {
        "expected_action",
        "expected_entry_price",
        "expected_exit_price",
        "expected_profit",
        "future_bars",
    }
    if not isinstance(value, dict) or set(value) != required or forbidden.intersection(value):
        raise Stage1CaseError("案例只能保存定位與審核範圍，不得硬編碼交易答案或未來資料。")
    result = dict(value)
    try:
        date.fromisoformat(str(result["target_date"]))
    except ValueError as exc:
        raise Stage1CaseError("target_date必須是YYYY-MM-DD。") from exc
    start = _hhmm(result["analysis_start"], "analysis_start")
    locator = _hhmm(result["locator_time"], "locator_time")
    end = _hhmm(result["analysis_end"], "analysis_end")
    if not start <= locator <= end:
        raise Stage1CaseError("定位時間必須位於案例因果分析窗內。")
    family = str(result["family_hypothesis"])
    if family not in {"Q2", "Q4", "Q2_OR_Q4"}:
        raise Stage1CaseError("family_hypothesis只允許Q2、Q4或Q2_OR_Q4。")
    cadence = result["analysis_every_bars"]
    if isinstance(cadence, bool) or not isinstance(cadence, int) or not 1 <= cadence <= 5:
        raise Stage1CaseError("案例分析間隔必須是1至5根。")
    focus = result["audit_focus"]
    if not isinstance(focus, list) or not focus or not all(isinstance(item, str) and item.strip() for item in focus):
        raise Stage1CaseError("audit_focus必須是非空文字陣列。")
    return result


def _hhmm(value: Any, field: str) -> time:
    try:
        return datetime.strptime(str(value), "%H:%M").time()
    except ValueError as exc:
        raise Stage1CaseError(f"{field}必須是HH:MM。") from exc


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--case", dest="case_id", default=None)
    args = parser.parse_args()
    cases = load_stage1_cases(args.catalog)
    if args.case_id is not None:
        cases = [item for item in cases if item["case_id"] == args.case_id]
        if not cases:
            raise Stage1CaseError("找不到指定case_id。")
    output = []
    for item in cases:
        output.append(
            {
                **item,
                "analysis_times": [value.strftime("%H:%M") for value in analysis_times(item)],
            }
        )
    print(json.dumps({"ok": True, "cases": output}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
