"""Build stock-grouped calibration and locked-reproduction source manifests.

The builder never exposes locked legacy answers in the public manifest.  It
places the identity map and the legacy answer copy below ``sealed/`` for later
evaluation after the reproducible specification has been frozen.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import secrets
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

try:
    from scripts.v2_core_source_audit_v1 import BATCHES, read_jsonl, sha256
except ModuleNotFoundError:  # Direct ``python scripts\\...`` execution.
    from v2_core_source_audit_v1 import BATCHES, read_jsonl, sha256


SPLIT_VERSION = "v2-core-stock-group-split-v1"
LOCKED_FRACTION = 0.30
SCENARIOS = (
    "MATURE_TREND_PULLBACK",
    "MACRO_COPY_RESONANCE",
    "BEAR_REVERSAL_LEFT_RIGHT",
    "FRESH_Q1_EXPANSION",
)
FORMAL_BATCHES = ("889", "1029")


def canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def stable_score(text: str) -> int:
    return int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")


def anonymous_id(key: bytes, namespace: str, value: str) -> str:
    digest = hmac.new(key, f"{namespace}|{value}".encode("utf-8"), hashlib.sha256)
    return digest.hexdigest()[:24]


def audit_complete(row: dict[str, Any]) -> bool:
    audit = row.get("audit") or {}
    return (
        bool(audit.get("method"))
        and bool(audit.get("review_design"))
        and audit.get("outcome_visible_to_ai") is False
        and audit.get("future_outcomes_used_in_final_evidence") is False
        and bool(audit.get("packet_sha256"))
    )


def row_scenarios(row: dict[str, Any]) -> set[str]:
    return {
        str(trigger["scenario"])
        for trigger in (row.get("v2") or {}).get("triggers") or []
        if trigger.get("scenario") in SCENARIOS
    }


def assign_groups(groups: dict[str, list[dict[str, Any]]]) -> dict[str, str]:
    """Assign a stock code wholly to calibration or locked reproduction.

    Rare positive scenarios are handled first so both partitions retain at
    least one example when at least two independent stock groups exist.
    """

    assignment: dict[str, str] = {}
    scenario_groups = {
        scenario: sorted(
            [
                code
                for code, rows in groups.items()
                if any(scenario in row_scenarios(row["decision"]) for row in rows)
            ],
            key=lambda code: stable_score(f"{SPLIT_VERSION}|{scenario}|{code}"),
        )
        for scenario in SCENARIOS
    }

    for scenario in sorted(SCENARIOS, key=lambda item: len(scenario_groups[item])):
        codes = scenario_groups[scenario]
        if len(codes) < 2:
            continue
        target = max(1, min(len(codes) - 1, math.ceil(len(codes) * LOCKED_FRACTION)))
        already_locked = sum(assignment.get(code) == "LOCKED_REPRODUCTION_SET" for code in codes)
        for code in codes:
            if already_locked >= target:
                break
            if code not in assignment:
                assignment[code] = "LOCKED_REPRODUCTION_SET"
                already_locked += 1

    threshold = int(LOCKED_FRACTION * 10_000)
    for code in sorted(groups):
        if code in assignment:
            continue
        bucket = stable_score(f"{SPLIT_VERSION}|general|{code}") % 10_000
        assignment[code] = (
            "LOCKED_REPRODUCTION_SET" if bucket < threshold else "CALIBRATION_SET"
        )

    # Preserve at least one calibration group for every scenario with two or
    # more groups, even when overlap between rare scenarios selected all of it.
    for scenario, codes in scenario_groups.items():
        if len(codes) >= 2 and all(
            assignment[code] == "LOCKED_REPRODUCTION_SET" for code in codes
        ):
            code = max(
                codes,
                key=lambda item: stable_score(
                    f"{SPLIT_VERSION}|calibration-rescue|{scenario}|{item}"
                ),
            )
            assignment[code] = "CALIBRATION_SET"
    return assignment


def load_formal_rows(repo_root: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    included: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for batch_id in FORMAL_BATCHES:
        run = (repo_root / BATCHES[batch_id]).resolve()
        for line_index, decision in enumerate(
            read_jsonl(run / "ai_decisions_merged.jsonl"), 1
        ):
            item = {
                "batch_id": batch_id,
                "line_index": line_index,
                "run": str(run),
                "decision": decision,
            }
            if audit_complete(decision):
                included.append(item)
            else:
                excluded.append(item)
    return included, excluded


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    groups = {str(row["decision"]["code"]) for row in rows}
    scenarios: Counter[str] = Counter()
    v2_trigger_count = 0
    for row in rows:
        triggers = (row["decision"].get("v2") or {}).get("triggers") or []
        v2_trigger_count += len(triggers)
        scenarios.update(
            str(trigger["scenario"])
            for trigger in triggers
            if trigger.get("scenario") in SCENARIOS
        )
    return {
        "stock_groups": len(groups),
        "source_rows": len(rows),
        "v2_trigger_count": v2_trigger_count,
        "v2_scenario_counts": {
            scenario: scenarios.get(scenario, 0) for scenario in SCENARIOS
        },
        "batch_rows": dict(sorted(Counter(row["batch_id"] for row in rows).items())),
    }


def build_payloads(repo_root: Path, key: bytes) -> dict[str, Any]:
    included, excluded = load_formal_rows(repo_root)
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in included:
        groups[str(row["decision"]["code"])].append(row)
    assignments = assign_groups(dict(groups))

    calibration_rows = [
        row
        for row in included
        if assignments[str(row["decision"]["code"])] == "CALIBRATION_SET"
    ]
    locked_rows = [
        row
        for row in included
        if assignments[str(row["decision"]["code"])]
        == "LOCKED_REPRODUCTION_SET"
    ]

    def source_ref(row: dict[str, Any], include_identity: bool) -> dict[str, Any]:
        decision = row["decision"]
        audit = decision.get("audit") or {}
        base = {
            "batch_id": row["batch_id"],
            "line_index": row["line_index"],
            "packet_sha256": audit.get("packet_sha256"),
            "preferred_750_met": audit.get("preferred_750_met"),
        }
        if include_identity:
            base.update(
                {
                    "code": str(decision["code"]),
                    "name": decision.get("name"),
                    "first_selected_on": decision.get("first_selected_on"),
                    "legacy_v1_status": (decision.get("v1") or {}).get("stock_status"),
                    "legacy_v2_status": (decision.get("v2") or {}).get("stock_status"),
                    "legacy_v2_triggers": [
                        {
                            key: trigger.get(key)
                            for key in (
                                "signal_date",
                                "scenario",
                                "trigger_path",
                                "episode_or_add_candidate",
                                "stop_date",
                                "stop_price",
                            )
                        }
                        for trigger in (decision.get("v2") or {}).get("triggers") or []
                    ],
                }
            )
        return base

    calibration = {
        "manifest_version": SPLIT_VERSION,
        "partition": "CALIBRATION_SET（校準集）",
        "grouping_rule": "STOCK_CODE_ACROSS_ALL_OVERLAPPING_BATCHES",
        "legacy_answers_visible": True,
        "future_performance_included": False,
        "summary": summarize(calibration_rows),
        "rows": [source_ref(row, True) for row in calibration_rows],
    }

    locked_public_rows = []
    sealed_rows = []
    for row in locked_rows:
        decision = row["decision"]
        code = str(decision["code"])
        review_id = "R-" + anonymous_id(
            key, "review", f"{row['batch_id']}|{row['line_index']}|{code}"
        )
        stock_id = "S-" + anonymous_id(key, "stock", code)
        locked_public_rows.append(
            {
                "review_id": review_id,
                "anonymous_stock_id": stock_id,
                "batch_id": row["batch_id"],
                "packet_sha256": (decision.get("audit") or {}).get("packet_sha256"),
                "preferred_750_met": (decision.get("audit") or {}).get(
                    "preferred_750_met"
                ),
            }
        )
        sealed_rows.append(
            {
                "review_id": review_id,
                "anonymous_stock_id": stock_id,
                "batch_id": row["batch_id"],
                "line_index": row["line_index"],
                "code": code,
                "name": decision.get("name"),
                "first_selected_on": decision.get("first_selected_on"),
                "packet_sha256": (decision.get("audit") or {}).get("packet_sha256"),
                "legacy_v1": decision.get("v1"),
                "legacy_v2": decision.get("v2"),
            }
        )

    locked = {
        "manifest_version": SPLIT_VERSION,
        "partition": "LOCKED_REPRODUCTION_SET（鎖定重現集）",
        "grouping_rule": "STOCK_CODE_ACROSS_ALL_OVERLAPPING_BATCHES",
        "identity_visible": False,
        "legacy_answers_visible": False,
        "future_performance_included": False,
        "identity_key_sha256": hashlib.sha256(key).hexdigest(),
        "summary_without_per_row_labels": {
            key: value
            for key, value in summarize(locked_rows).items()
            if key not in {"v2_trigger_count", "v2_scenario_counts"}
        },
        "rows": locked_public_rows,
    }

    retrospective = []
    run_747 = (repo_root / BATCHES["747"]).resolve()
    for line_index, decision in enumerate(
        read_jsonl(run_747 / "ai_decisions_merged.jsonl"), 1
    ):
        retrospective.append(
            {
                "batch_id": "747",
                "line_index": line_index,
                "code": str(decision["code"]),
                "name": decision.get("name"),
                "classification": "PARTIAL（部分有效）",
                "use": "RETROSPECTIVE_REFERENCE_ONLY（僅供回溯參考）",
                "packet_sha256": (decision.get("audit") or {}).get("packet_sha256"),
            }
        )

    excluded_public = [
        {
            "batch_id": row["batch_id"],
            "line_index": row["line_index"],
            "code": str(row["decision"]["code"]),
            "reason": "INCOMPLETE_AUDIT_METADATA（稽核中繼資料不完整）",
        }
        for row in excluded
    ]
    return {
        "calibration": calibration,
        "locked": locked,
        "sealed": sealed_rows,
        "retrospective": retrospective,
        "excluded": excluded_public,
        "assignments": assignments,
        "calibration_internal_summary": summarize(calibration_rows),
        "locked_internal_summary": summarize(locked_rows),
    }


def write_new_or_identical(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_bytes() != payload:
        raise RuntimeError(f"refusing to overwrite changed artifact: {path}")
    path.write_bytes(payload)


def markdown_summary(payloads: dict[str, Any], locked: bool) -> str:
    title = (
        "鎖定重現來源清單" if locked else "校準來源清單"
    )
    summary = (
        payloads["locked_internal_summary"]
        if locked
        else payloads["calibration_internal_summary"]
    )
    partition = (
        "LOCKED_REPRODUCTION_SET（鎖定重現集）"
        if locked
        else "CALIBRATION_SET（校準集）"
    )
    lines = [
        f"# {title}",
        "",
        f"版本：`{SPLIT_VERSION}`  ",
        f"分區：`{partition}`",
        "",
        "## 摘要",
        "",
        f"- 股票群組：{summary['stock_groups']}",
        f"- 來源列：{summary['source_rows']}",
        f"- 來源批次：{summary['batch_rows']}",
    ]
    if locked:
        lines.extend(
            [
                "- 公開manifest不含股票代號、名稱、舊AI答案或逐列情境標籤。",
                "- 同一股票即使跨889與1,029批次出現，也只能落在同一分區。",
                "- sealed資料只能在新規格凍結並完成三輪一致性後解封。",
                "- 同一研究thread先前已接觸歷史總計與少數案例，因此這是流程鎖定，不宣稱研究者從未見過任何舊資料。",
            ]
        )
    else:
        lines.extend(
            [
                f"- 舊V2觸發：{summary['v2_trigger_count']}",
                f"- 四情境：{summary['v2_scenario_counts']}",
                "- 本清單可用於舊AI正例／相近反例語意萃取，不含未來績效。",
            ]
        )
    lines.extend(
        [
            "",
            "## 分割規則",
            "",
            "- 只納入1,029批全部完整列與889批749個完整audit列。",
            "- 依股票代號跨批次群組，防止同股重疊時段分到兩側。",
            "- 以固定版本雜湊分割，並優先確保稀有情境在兩側均有股票群組。",
            "- 747批另列為回溯參考，不進入正式鎖定重現分割。",
            "- 這是來源股票層分割；逐日正例／反例仍須在MILESTONE 2建立。",
            "",
        ]
    )
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
    key_path = output / "sealed" / "identity_key.txt"
    if key_path.exists():
        key = bytes.fromhex(key_path.read_text(encoding="ascii").strip())
    else:
        key = secrets.token_bytes(32)
        write_new_or_identical(key_path, key.hex().encode("ascii") + b"\n")

    payloads = build_payloads(args.repo_root.resolve(), key)
    write_new_or_identical(
        output / "calibration_manifest.json", canonical_bytes(payloads["calibration"])
    )
    write_new_or_identical(
        output / "locked_validation_manifest.json", canonical_bytes(payloads["locked"])
    )
    write_new_or_identical(
        output / "retrospective_reference_manifest.json",
        canonical_bytes(
            {
                "manifest_version": SPLIT_VERSION,
                "partition": "RETROSPECTIVE_REFERENCE_ONLY（僅供回溯參考）",
                "rows": payloads["retrospective"],
            }
        ),
    )
    write_new_or_identical(
        output / "excluded_source_rows.json",
        canonical_bytes(
            {
                "manifest_version": SPLIT_VERSION,
                "classification": "REFERENCE_ONLY（僅供參考）",
                "rows": payloads["excluded"],
            }
        ),
    )
    sealed_text = b"".join(canonical_bytes(row) for row in payloads["sealed"])
    write_new_or_identical(output / "sealed" / "locked_legacy_answers.jsonl", sealed_text)
    write_new_or_identical(
        output / "calibration_manifest.md",
        markdown_summary(payloads, locked=False).encode("utf-8"),
    )
    write_new_or_identical(
        output / "locked_validation_manifest.md",
        markdown_summary(payloads, locked=True).encode("utf-8"),
    )
    print(
        json.dumps(
            {
                "status": "CREATED_OR_IDENTICAL（已建立或內容相同）",
                "split_version": SPLIT_VERSION,
                "calibration": payloads["calibration_internal_summary"],
                "locked": payloads["locked_internal_summary"],
                "excluded_rows": len(payloads["excluded"]),
                "retrospective_rows": len(payloads["retrospective"]),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
