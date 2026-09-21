"""Reproduce the Milestone-1 audit for historical formal-AI ledgers.

The command is deliberately read-only.  It checks immutable artifacts and emits
one JSON document to stdout; it never repairs or rewrites historical decisions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any, Iterable


BATCHES = {
    "747": Path("reports/course_backtest/2026-09-06/tg_formal_ai_v1_v2"),
    "889": Path(
        "reports/course_backtest/2023-09-04/"
        "historical_scan_formal_ai_v1_v2"
    ),
    "1029": Path(
        "reports/course_backtest/2024-02-02/"
        "historical_scan_2023h2_formal_ai_v1_v2"
    ),
}

TEMPORAL_HOLDOUT = "TEMPORAL_HOLDOUT_CAUSAL_REPLAY"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig") as handle:
        for line_number, raw in enumerate(handle, 1):
            text = raw.strip()
            if not text:
                continue
            value = json.loads(text)
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number}: expected object")
            rows.append(value)
    return rows


def count_scenarios(
    rows: Iterable[dict[str, Any]], version: str
) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for row in rows:
        section = row.get(version) or {}
        for trigger in section.get("triggers") or []:
            scenario = trigger.get("scenario")
            if scenario:
                counts[str(scenario)] += 1
    return dict(sorted(counts.items()))


def classify(
    *,
    structural_errors: int,
    future_outcome_rows: int,
    temporal_holdout_rows: int,
    row_count: int,
    complete_audit_rows: int,
) -> str:
    if structural_errors or future_outcome_rows:
        return "INVALID（無效）"
    if temporal_holdout_rows != row_count or complete_audit_rows != row_count:
        return "PARTIAL（部分有效）"
    return "VALID（有效）"


def audit_batch(repo_root: Path, batch_id: str, relative_run: Path) -> dict[str, Any]:
    run = (repo_root / relative_run).resolve()
    ledger_path = run / "ai_decisions_merged.jsonl"
    manifest_path = run / "input_manifest.json"
    validation_path = run / "decision_validation.json"
    packet_manifest_path = run / "packet_manifest.json"

    validation = read_json(validation_path)
    manifest = read_json(manifest_path)
    rows = read_jsonl(ledger_path)
    expected = int(validation["expected_stocks"])

    codes = [str(row.get("code", "")) for row in rows]
    duplicate_codes = sum(count - 1 for count in Counter(codes).values() if count > 1)
    structural_errors: list[str] = []
    if len(rows) != expected:
        structural_errors.append(f"row_count:{len(rows)}!={expected}")
    if len(manifest.get("items") or []) != expected:
        structural_errors.append("input_manifest_item_count")
    if duplicate_codes:
        structural_errors.append(f"duplicate_codes:{duplicate_codes}")

    shard_checks: list[dict[str, Any]] = []
    for item in validation.get("ledger_files") or []:
        path = Path(item["path"])
        if not path.is_absolute():
            path = run / path
        exists = path.is_file()
        actual = sha256(path) if exists else None
        matched = exists and actual == str(item.get("sha256", "")).lower()
        shard_checks.append(
            {
                "file": path.name,
                "exists": exists,
                "expected_sha256": item.get("sha256"),
                "actual_sha256": actual,
                "matches": matched,
            }
        )
        if not matched:
            structural_errors.append(f"shard_hash:{path.name}")

    audit_counts = {
        "method_present": 0,
        "review_design_present": 0,
        "outcome_visible_false": 0,
        "future_outcomes_false": 0,
        "future_outcomes_true": 0,
        "packet_hash_present": 0,
        "packet_hash_matches": 0,
        "temporal_holdout": 0,
        "retrospective_not_blind": 0,
    }
    packet_missing = 0
    packet_hash_mismatch = 0
    complete_audit_rows = 0

    for row in rows:
        audit = row.get("audit") or {}
        if audit.get("method"):
            audit_counts["method_present"] += 1
        if audit.get("review_design"):
            audit_counts["review_design_present"] += 1
        if audit.get("outcome_visible_to_ai") is False:
            audit_counts["outcome_visible_false"] += 1
        if audit.get("future_outcomes_used_in_final_evidence") is False:
            audit_counts["future_outcomes_false"] += 1
        if audit.get("future_outcomes_used_in_final_evidence") is True:
            audit_counts["future_outcomes_true"] += 1
        if audit.get("outcome_blindness") == TEMPORAL_HOLDOUT:
            audit_counts["temporal_holdout"] += 1
        if audit.get("outcome_blindness") == "RETROSPECTIVE_NOT_BLIND":
            audit_counts["retrospective_not_blind"] += 1

        packet_hash = audit.get("packet_sha256")
        packet_path = run / "review_packets" / f"{row.get('code')}.json"
        if not packet_path.is_file():
            packet_missing += 1
        if packet_hash:
            audit_counts["packet_hash_present"] += 1
            if packet_path.is_file() and sha256(packet_path) == packet_hash:
                audit_counts["packet_hash_matches"] += 1
            else:
                packet_hash_mismatch += 1

        complete = (
            bool(audit.get("method"))
            and bool(audit.get("review_design"))
            and audit.get("outcome_visible_to_ai") is False
            and audit.get("future_outcomes_used_in_final_evidence") is False
            and bool(packet_hash)
            and packet_path.is_file()
            and sha256(packet_path) == packet_hash
        )
        if complete:
            complete_audit_rows += 1

    if packet_missing:
        structural_errors.append(f"missing_packets:{packet_missing}")
    if packet_hash_mismatch:
        structural_errors.append(f"packet_hash_mismatch:{packet_hash_mismatch}")

    classification = classify(
        structural_errors=len(structural_errors),
        future_outcome_rows=audit_counts["future_outcomes_true"],
        temporal_holdout_rows=audit_counts["temporal_holdout"],
        row_count=len(rows),
        complete_audit_rows=complete_audit_rows,
    )

    result: dict[str, Any] = {
        "batch_id": batch_id,
        "run": str(run),
        "classification": classification,
        "expected_rows": expected,
        "actual_rows": len(rows),
        "input_manifest_items": len(manifest.get("items") or []),
        "input_manifest_sha256": sha256(manifest_path),
        "packet_manifest_sha256": (
            sha256(packet_manifest_path) if packet_manifest_path.is_file() else None
        ),
        "merged_ledger_sha256": sha256(ledger_path),
        "complete_audit_rows": complete_audit_rows,
        "audit_counts": audit_counts,
        "packet_missing": packet_missing,
        "packet_hash_mismatch": packet_hash_mismatch,
        "scenario_counts": {
            "v1": count_scenarios(rows, "v1"),
            "v2": count_scenarios(rows, "v2"),
        },
        "shards": shard_checks,
        "structural_errors": structural_errors,
    }
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument(
        "--batch",
        choices=["all", *BATCHES],
        default="all",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    selected = BATCHES if args.batch == "all" else {args.batch: BATCHES[args.batch]}
    results = [
        audit_batch(args.repo_root.resolve(), batch_id, run)
        for batch_id, run in selected.items()
    ]
    payload = {
        "audit_version": "v2-core-source-audit-v1",
        "read_only": True,
        "batches": results,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if any(row["classification"].startswith("INVALID") for row in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
