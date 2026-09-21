#!/usr/bin/env python3
"""Audit V2 gate evidence for templating and unsupported assertions.

This is a ledger-only QA tool.  It deliberately does not load prices, trades,
replay outputs, or performance fields.  It does not modify the ledgers.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Iterable


DATE_RE = re.compile(
    r"(?<!\d)(?:(?:19|20)\d{2}-\d{2}-\d{2}|"
    r"(?:1[0-2]|0?[1-9])/(?:3[01]|[12]\d|0?[1-9]))(?!\d)"
)
NUMBER_RE = re.compile(r"(?<![A-Za-z])[-+]?\d+(?:\.\d+)?")
SPACE_RE = re.compile(r"\s+")
CLAUSE_SPLIT_RE = re.compile(r"[；。]\s*")
PRICE_RE = re.compile(
    r"(?:@|收(?:盤)?|高|低|防線|price\s*[:=]?|close\s*[:=]?|"
    r"open\s*[:=]?|high\s*[:=]?|low\s*[:=]?|MA\d+(?:/\d+)*\s*[:=]?)"
    r"\s*[-+]?\d+(?:\.\d+)?",
    re.IGNORECASE,
)

CIRCULAR_PATTERNS: dict[str, re.Pattern[str]] = {
    "AI_CLASSIFICATION_RESTATEMENT": re.compile(
        r"AI(?:於|沿|依|按|以)?.{0,24}?(?:判|辨識)"
    ),
    "DIRECTION_TAIJI_RESTATEMENT": re.compile(r"方向(?:UP|DOWN).*?太極"),
    "GENERATION_RESTATEMENT": re.compile(r"截至.*?映射(?:ANCHOR|COPY)_LEG"),
    "QUADRANT_DOW_RESTATEMENT": re.compile(
        r"(?:大級|小級).*?Q[1-4].*?(?:BULL|BEAR|同向|取得向上控制)"
    ),
    "SPACE_ASSERTED_NOT_MEASURED": re.compile(r"逐代判讀仍有剩餘空間"),
    "PATH_RESTATEMENT": re.compile(r"路徑="),
}


def pct(numerator: int, denominator: int) -> float:
    return round(100.0 * numerator / denominator, 2) if denominator else 0.0


def normalize_template(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).strip().lower()
    text = DATE_RE.sub("<date>", text)
    text = re.sub(r"(?<!\d)\d{4}(?!\d)", "<code>", text)
    text = NUMBER_RE.sub("<n>", text)
    return SPACE_RE.sub(" ", text)


def has_date(text: str) -> bool:
    return bool(DATE_RE.search(text))


def has_price(text: str, code: str) -> bool:
    without_code = re.sub(rf"(?<!\d){re.escape(code)}(?!\d)", "", text)
    without_dates = DATE_RE.sub("", without_code)
    return bool(PRICE_RE.search(without_code) or re.search(r"\d+\.\d+", without_dates))


def clauses(text: str) -> list[str]:
    return [part.strip() for part in CLAUSE_SPLIT_RE.split(text) if len(part.strip()) >= 8]


def load_records(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            rows.append(row)
            code = str(row.get("code", ""))
            index = row.get("index")
            for trigger_number, trigger in enumerate(row.get("v2", {}).get("triggers") or [], 1):
                for gate, payload in (trigger.get("required_gates") or {}).items():
                    for evidence_number, evidence in enumerate(payload.get("evidence") or [], 1):
                        text = str(evidence)
                        records.append(
                            {
                                "line": line_number,
                                "index": index,
                                "code": code,
                                "signal_date": trigger.get("signal_date"),
                                "scenario": trigger.get("scenario"),
                                "trigger_number": trigger_number,
                                "gate": gate,
                                "evidence_number": evidence_number,
                                "text": text,
                                "template": normalize_template(text),
                                "template_prefix_80": normalize_template(text)[:80],
                                "has_date": has_date(text),
                                "has_price": has_price(text, code),
                            }
                        )
    return rows, records


def top_groups(
    groups: dict[Any, list[dict[str, Any]]],
    limit: int,
    min_stocks: int = 2,
) -> list[dict[str, Any]]:
    eligible = [
        (key, values)
        for key, values in groups.items()
        if len({item["code"] for item in values}) >= min_stocks
    ]
    eligible.sort(key=lambda item: (-len(item[1]), str(item[0])))
    output: list[dict[str, Any]] = []
    for key, values in eligible[:limit]:
        examples = values[:3]
        output.append(
            {
                "fingerprint": key,
                "occurrences": len(values),
                "stock_count": len({item["code"] for item in values}),
                "gate_count": len({item["gate"] for item in values}),
                "examples": [
                    {
                        "index": item["index"],
                        "code": item["code"],
                        "signal_date": item["signal_date"],
                        "gate": item["gate"],
                        "text": item["text"],
                    }
                    for item in examples
                ],
            }
        )
    return output


def audit(path: Path, top: int) -> dict[str, Any]:
    sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    rows, records = load_records(path)
    triggers = sum(len(row.get("v2", {}).get("triggers") or []) for row in rows)
    gate_instances = {
        (r["index"], r["trigger_number"], r["signal_date"], r["gate"])
        for r in records
    }

    by_template: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    by_prefix: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    by_raw: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for record in records:
        by_template[record["template"]].append(record)
        by_prefix[record["template_prefix_80"]].append(record)
        by_raw[record["text"]].append(record)

    repeated_2 = {
        key for key, vals in by_template.items() if len({v["code"] for v in vals}) >= 2
    }
    repeated_5 = {
        key for key, vals in by_template.items() if len({v["code"] for v in vals}) >= 5
    }
    repeated_prefix_2 = {
        key for key, vals in by_prefix.items() if len({v["code"] for v in vals}) >= 2
    }
    repeated_prefix_5 = {
        key for key, vals in by_prefix.items() if len({v["code"] for v in vals}) >= 5
    }

    clause_records: list[dict[str, Any]] = []
    for record in records:
        for clause_number, clause in enumerate(clauses(record["text"]), 1):
            clause_records.append(
                {
                    **record,
                    "clause_number": clause_number,
                    "clause": clause,
                    "clause_template": normalize_template(clause),
                }
            )
    by_clause_template: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for record in clause_records:
        by_clause_template[record["clause_template"]].append(record)
    repeated_clause_2 = {
        key
        for key, vals in by_clause_template.items()
        if len({v["code"] for v in vals}) >= 2
    }
    repeated_clause_5 = {
        key
        for key, vals in by_clause_template.items()
        if len({v["code"] for v in vals}) >= 5
    }

    reused_raw_records: set[tuple[Any, ...]] = set()
    reused_raw_triggers: set[tuple[Any, ...]] = set()
    reused_raw_gate_instances: set[tuple[Any, ...]] = set()
    within_trigger_examples: list[dict[str, Any]] = []
    raw_by_trigger: dict[tuple[Any, ...], dict[str, list[dict[str, Any]]]] = collections.defaultdict(
        lambda: collections.defaultdict(list)
    )
    for record in records:
        trigger_key = (record["index"], record["trigger_number"], record["signal_date"])
        raw_by_trigger[trigger_key][record["text"]].append(record)
    for trigger_key, groups in raw_by_trigger.items():
        for text, values in groups.items():
            gates = {value["gate"] for value in values}
            if len(gates) < 2:
                continue
            reused_raw_triggers.add(trigger_key)
            for value in values:
                reused_raw_gate_instances.add(
                    (value["index"], value["trigger_number"], value["signal_date"], value["gate"])
                )
                reused_raw_records.add(
                    (
                        value["index"],
                        value["trigger_number"],
                        value["gate"],
                        value["evidence_number"],
                    )
                )
            if len(within_trigger_examples) < top:
                first = values[0]
                within_trigger_examples.append(
                    {
                        "index": first["index"],
                        "code": first["code"],
                        "signal_date": first["signal_date"],
                        "gates": sorted(gates),
                        "text": text,
                    }
                )

    reused_clause_triggers: set[tuple[Any, ...]] = set()
    reused_clause_gate_instances: set[tuple[Any, ...]] = set()
    reused_clause_records: set[tuple[Any, ...]] = set()
    within_trigger_clause_examples: list[dict[str, Any]] = []
    clause_by_trigger: dict[tuple[Any, ...], dict[str, list[dict[str, Any]]]] = (
        collections.defaultdict(lambda: collections.defaultdict(list))
    )
    for record in clause_records:
        trigger_key = (record["index"], record["trigger_number"], record["signal_date"])
        clause_by_trigger[trigger_key][record["clause"]].append(record)
    for trigger_key, groups in clause_by_trigger.items():
        for clause, values in groups.items():
            gates = {value["gate"] for value in values}
            if len(gates) < 2:
                continue
            reused_clause_triggers.add(trigger_key)
            for value in values:
                reused_clause_gate_instances.add(
                    (value["index"], value["trigger_number"], value["signal_date"], value["gate"])
                )
                reused_clause_records.add(
                    (
                        value["index"],
                        value["trigger_number"],
                        value["gate"],
                        value["evidence_number"],
                        value["clause_number"],
                    )
                )
            if len(within_trigger_clause_examples) < top:
                first = values[0]
                within_trigger_clause_examples.append(
                    {
                        "index": first["index"],
                        "code": first["code"],
                        "signal_date": first["signal_date"],
                        "gates": sorted(gates),
                        "clause": clause,
                    }
                )

    circular_counts: dict[str, int] = {}
    circular_examples: dict[str, list[dict[str, Any]]] = {}
    for label, pattern in CIRCULAR_PATTERNS.items():
        matches = [record for record in records if pattern.search(record["text"])]
        circular_counts[label] = len(matches)
        circular_examples[label] = [
            {
                "index": r["index"],
                "code": r["code"],
                "signal_date": r["signal_date"],
                "gate": r["gate"],
                "text": r["text"],
            }
            for r in matches[: min(3, top)]
        ]

    missing_both = [r for r in records if not r["has_date"] and not r["has_price"]]
    missing_either = [r for r in records if not r["has_date"] or not r["has_price"]]
    gate_fact_flags: dict[tuple[Any, ...], list[dict[str, Any]]] = collections.defaultdict(list)
    for record in records:
        key = (record["index"], record["trigger_number"], record["signal_date"], record["gate"])
        gate_fact_flags[key].append(record)
    gates_without_combined_fact = [
        values
        for values in gate_fact_flags.values()
        if not any(r["has_date"] and r["has_price"] for r in values)
    ]

    per_gate: dict[str, dict[str, Any]] = {}
    for gate in sorted({r["gate"] for r in records}):
        subset = [r for r in records if r["gate"] == gate]
        templates = collections.defaultdict(list)
        for record in subset:
            templates[record["template"]].append(record)
        repeated = {
            key for key, vals in templates.items() if len({v["code"] for v in vals}) >= 2
        }
        per_gate[gate] = {
            "evidence_items": len(subset),
            "normalized_unique": len(templates),
            "cross_stock_template_item_pct": pct(
                sum(1 for r in subset if r["template"] in repeated), len(subset)
            ),
            "missing_date_or_price_item_pct": pct(
                sum(1 for r in subset if not r["has_date"] or not r["has_price"]), len(subset)
            ),
        }

    return {
        "ledger": str(path),
        "sha256": sha256,
        "scope_note": "V2 required_gates only; no price, replay, trade, or performance files loaded",
        "rows": len(rows),
        "v2_triggers": triggers,
        "gate_instances": len(gate_instances),
        "evidence_items": len(records),
        "normalized_unique_items": len(by_template),
        "cross_stock_template_reuse": {
            "items_in_2plus_stock_templates": sum(
                1 for r in records if r["template"] in repeated_2
            ),
            "items_in_2plus_stock_templates_pct": pct(
                sum(1 for r in records if r["template"] in repeated_2), len(records)
            ),
            "items_in_5plus_stock_templates": sum(
                1 for r in records if r["template"] in repeated_5
            ),
            "items_in_5plus_stock_templates_pct": pct(
                sum(1 for r in records if r["template"] in repeated_5), len(records)
            ),
        },
        "cross_stock_prefix_reuse": {
            "items_in_2plus_stock_prefixes": sum(
                1 for r in records if r["template_prefix_80"] in repeated_prefix_2
            ),
            "items_in_2plus_stock_prefixes_pct": pct(
                sum(1 for r in records if r["template_prefix_80"] in repeated_prefix_2),
                len(records),
            ),
            "items_in_5plus_stock_prefixes": sum(
                1 for r in records if r["template_prefix_80"] in repeated_prefix_5
            ),
            "items_in_5plus_stock_prefixes_pct": pct(
                sum(1 for r in records if r["template_prefix_80"] in repeated_prefix_5),
                len(records),
            ),
        },
        "cross_stock_clause_reuse": {
            "clauses": len(clause_records),
            "clauses_in_2plus_stock_templates": sum(
                1 for r in clause_records if r["clause_template"] in repeated_clause_2
            ),
            "clauses_in_2plus_stock_templates_pct": pct(
                sum(1 for r in clause_records if r["clause_template"] in repeated_clause_2),
                len(clause_records),
            ),
            "clauses_in_5plus_stock_templates": sum(
                1 for r in clause_records if r["clause_template"] in repeated_clause_5
            ),
            "clauses_in_5plus_stock_templates_pct": pct(
                sum(1 for r in clause_records if r["clause_template"] in repeated_clause_5),
                len(clause_records),
            ),
        },
        "within_trigger_raw_reuse": {
            "triggers": len(reused_raw_triggers),
            "triggers_pct": pct(len(reused_raw_triggers), triggers),
            "gate_instances": len(reused_raw_gate_instances),
            "gate_instances_pct": pct(len(reused_raw_gate_instances), len(gate_instances)),
            "evidence_items": len(reused_raw_records),
            "evidence_items_pct": pct(len(reused_raw_records), len(records)),
            "examples": within_trigger_examples,
        },
        "within_trigger_clause_reuse": {
            "triggers": len(reused_clause_triggers),
            "triggers_pct": pct(len(reused_clause_triggers), triggers),
            "gate_instances": len(reused_clause_gate_instances),
            "gate_instances_pct": pct(len(reused_clause_gate_instances), len(gate_instances)),
            "clauses": len(reused_clause_records),
            "clauses_pct": pct(len(reused_clause_records), len(clause_records)),
            "examples": within_trigger_clause_examples,
        },
        "fact_presence": {
            "items_missing_date_and_price": len(missing_both),
            "items_missing_date_and_price_pct": pct(len(missing_both), len(records)),
            "items_missing_date_or_price": len(missing_either),
            "items_missing_date_or_price_pct": pct(len(missing_either), len(records)),
            "gate_instances_without_any_same_line_date_and_price": len(gates_without_combined_fact),
            "gate_instances_without_any_same_line_date_and_price_pct": pct(
                len(gates_without_combined_fact), len(gate_instances)
            ),
            "missing_both_examples": [
                {
                    "index": r["index"],
                    "code": r["code"],
                    "signal_date": r["signal_date"],
                    "gate": r["gate"],
                    "text": r["text"],
                }
                for r in missing_both[:top]
            ],
        },
        "circular_phrase_occurrences": circular_counts,
        "circular_phrase_examples": circular_examples,
        "per_gate": per_gate,
        "top_cross_stock_templates": top_groups(by_template, top),
        "top_cross_stock_prefixes": top_groups(by_prefix, top),
        "top_cross_stock_clause_templates": top_groups(by_clause_template, top),
        "top_raw_cross_stock_duplicates": top_groups(by_raw, top),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("ledgers", nargs="+", type=Path)
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = {str(path): audit(path, args.top) for path in args.ledgers}
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
