"""Fail-closed Candidate3-to-Candidate4 review-point packet diff audit.

The audit is outcome blind.  It aligns two ``review_points.jsonl`` files by
``review_id``, verifies each packet's anonymous/causal integrity, and permits
the expected builder-version plus integrity-hash cascade.  For packets that
provably contained a comparison leg with fewer than two visible bars, it also
permits the narrowly scoped comparison-window repair, the comparison
endpoints and endpoint bars that the repaired window directly requires, and
its deterministic derived fields.  Every other change makes the top-level
status ``FAIL``.

The implementation indexes byte offsets rather than retaining the large
review-point universes in memory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, BinaryIO, Iterable, Sequence


AUDIT_VERSION = "hybrid-v3-candidate3-candidate4-packet-audit-v3"
MAX_GUARD_EXAMPLES = 50
MAX_PATH_EXAMPLES = 40
MAX_REF_EXAMPLES = 30
MAX_MD_CASES = 100

_REVIEW_ID = re.compile(r"^D-[0-9a-f]{24}$")
_ANONYMOUS_STOCK_ID = re.compile(r"^S-[0-9a-f]{16}$")
_MISSING = object()

_FORBIDDEN_PACKET_KEYS = {
    "code",
    "name",
    "symbol",
    "market",
    "mfe",
    "mae",
    "pnl",
    "profit",
    "return_after",
    "forward_return",
    "future_return",
    "future_open",
    "future_high",
    "future_low",
    "future_close",
    "exit",
    "exit_date",
    "exit_price",
    "outcome",
    "realized_return",
    "unrealized_return",
    "max_favorable_excursion",
    "max_adverse_excursion",
}

_DATE_KEYS = {
    "date",
    "start",
    "end",
    "low_date",
    "high_date",
    "source_date",
    "confirmation_date",
    "available_on",
    "confirmed_on",
    "breached_on",
    "origin_pivot_source_date",
    "parent_available_on",
    "correction_available_on",
    "current_available_on",
    "baseline_available_on",
    "causal_cutoff_as_of",
    "start_date",
    "end_date",
}


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _packet(row: dict[str, Any]) -> dict[str, Any]:
    packet = row.get("packet")
    if isinstance(packet, dict):
        return packet
    if {"review_id", "anonymous_stock_id", "as_of", "objective_facts"}.issubset(row):
        return row
    return {}


def _parse_iso_date(value: Any) -> date:
    text = str(value)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        raise ValueError("not an ISO calendar date")
    return datetime.strptime(text, "%Y-%m-%d").date()


def validate_row_guard(row: dict[str, Any]) -> list[str]:
    """Validate hashes, anonymity, and no-future-data invariants for one row."""

    errors: list[str] = []
    packet = _packet(row)
    if not packet:
        return ["packet is missing or is not an object"]

    review_id = packet.get("review_id")
    anonymous_stock_id = packet.get("anonymous_stock_id")
    as_of = packet.get("as_of")
    if not isinstance(review_id, str) or not _REVIEW_ID.fullmatch(review_id):
        errors.append("packet review_id is not a valid anonymous review id")
    if not isinstance(anonymous_stock_id, str) or not _ANONYMOUS_STOCK_ID.fullmatch(
        anonymous_stock_id
    ):
        errors.append("packet anonymous_stock_id is invalid")
    if row is not packet:
        if row.get("review_id") != review_id:
            errors.append("row review_id differs from packet")
        if row.get("anonymous_stock_id") != anonymous_stock_id:
            errors.append("row anonymous_stock_id differs from packet")

    try:
        cutoff = _parse_iso_date(as_of)
    except (TypeError, ValueError):
        cutoff = None
        errors.append("packet as_of is not an ISO calendar date")

    objective = packet.get("objective_facts")
    if not isinstance(objective, dict):
        errors.append("packet objective_facts is missing or is not an object")
        objective = {}
    elif objective.get("causal_cutoff_as_of") != as_of:
        errors.append("objective_facts.causal_cutoff_as_of differs from packet as_of")
    if objective.get("performance_used_for_ordering_or_truncation") not in (None, False):
        errors.append("performance_used_for_ordering_or_truncation is not false")

    def walk(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                normalized = str(key).lower()
                child_path = f"{path}.{key}" if path else str(key)
                if (
                    normalized in _FORBIDDEN_PACKET_KEYS
                    or normalized.startswith("future_")
                    or normalized.startswith("forward_")
                ):
                    errors.append(f"forbidden identity/performance key: {child_path}")
                if normalized in _DATE_KEYS and nested is not None and cutoff is not None:
                    try:
                        visible_date = _parse_iso_date(nested)
                    except (TypeError, ValueError):
                        errors.append(f"invalid visible date at {child_path}: {nested}")
                    else:
                        if visible_date > cutoff:
                            errors.append(
                                f"future date at {child_path}: {visible_date.isoformat()} > {as_of}"
                            )
                walk(nested, child_path)
        elif isinstance(value, list):
            for index, nested in enumerate(value):
                walk(nested, f"{path}[{index}]")

    walk(packet, "packet")

    evidence = packet.get("evidence")
    if not isinstance(evidence, list):
        errors.append("packet evidence is missing or is not an array")
        evidence = []
    seen_refs: set[str] = set()
    for index, evidence_row in enumerate(evidence):
        if not isinstance(evidence_row, dict):
            errors.append(f"evidence[{index}] is not an object")
            continue
        ref = str(evidence_row.get("ref") or "")
        if not ref:
            errors.append(f"evidence[{index}] has no ref")
        elif ref in seen_refs:
            errors.append(f"duplicate evidence ref: {ref}")
        seen_refs.add(ref)
        evidence_date = evidence_row.get("date")
        if evidence_date is None:
            errors.append(f"evidence[{index}] has no date")
        if ref.startswith("BAR:") and ref.split(":", 1)[1] != str(evidence_date):
            errors.append(f"BAR evidence ref/date mismatch: {ref}")

    manifest = packet.get("question_manifest")
    if not isinstance(manifest, dict):
        errors.append("packet question_manifest is missing or is not an object")
    elif packet.get("question_manifest_sha256") != canonical_sha256(manifest):
        errors.append("question_manifest_sha256 mismatch")
    if packet.get("evidence_catalog_sha256") != canonical_sha256(evidence):
        errors.append("evidence_catalog_sha256 mismatch")
    if objective.get("ai_visible_evidence_sha256") != canonical_sha256(evidence):
        errors.append("objective_facts.ai_visible_evidence_sha256 mismatch")

    packet_core = dict(packet)
    supplied_input_sha256 = packet_core.pop("input_packet_sha256", None)
    if supplied_input_sha256 != canonical_sha256(packet_core):
        errors.append("input_packet_sha256 mismatch")
    if row is not packet and row.get("packet_sha256") != canonical_sha256(packet):
        errors.append("row packet_sha256 mismatch")
    return sorted(set(errors))


@dataclass(frozen=True)
class _IndexedRow:
    position: int
    line_number: int
    offset: int
    review_id: str
    source_ordinal: Any
    anonymous_stock_id: Any
    as_of: Any


class _GuardSummary:
    def __init__(self) -> None:
        self.rows_checked = 0
        self.rows_failed = 0
        self.error_count = 0
        self.examples: list[dict[str, Any]] = []

    def add(
        self,
        *,
        line_number: int,
        review_id: str | None,
        errors: Sequence[str],
    ) -> None:
        self.rows_checked += 1
        if not errors:
            return
        self.rows_failed += 1
        self.error_count += len(errors)
        if len(self.examples) < MAX_GUARD_EXAMPLES:
            self.examples.append(
                {
                    "line_number": line_number,
                    "review_id": review_id,
                    "errors": list(errors),
                }
            )

    def result(self) -> dict[str, Any]:
        return {
            "status": "PASS" if self.error_count == 0 else "FAIL",
            "rows_checked": self.rows_checked,
            "rows_failed": self.rows_failed,
            "error_count": self.error_count,
            "examples": self.examples,
            "examples_truncated": max(0, self.rows_failed - len(self.examples)),
        }


def _decode_json_line(raw: bytes, *, path: Path, line_number: int) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"JSONL row is not an object at {path}:{line_number}")
    return value


def _scan_old(
    path: Path,
) -> tuple[dict[str, _IndexedRow], list[str], list[dict[str, Any]], _GuardSummary]:
    index: dict[str, _IndexedRow] = {}
    order: list[str] = []
    duplicates: list[dict[str, Any]] = []
    guards = _GuardSummary()
    with Path(path).open("rb") as handle:
        position = 0
        line_number = 0
        while True:
            offset = handle.tell()
            raw = handle.readline()
            if not raw:
                break
            line_number += 1
            if not raw.strip():
                continue
            try:
                row = _decode_json_line(raw, path=path, line_number=line_number)
            except ValueError as exc:
                guards.add(line_number=line_number, review_id=None, errors=[str(exc)])
                position += 1
                continue
            packet = _packet(row)
            review_id = str(row.get("review_id") or packet.get("review_id") or "")
            guards.add(
                line_number=line_number,
                review_id=review_id or None,
                errors=validate_row_guard(row),
            )
            if not review_id:
                position += 1
                continue
            if review_id in index:
                duplicates.append(
                    {
                        "review_id": review_id,
                        "first_line": index[review_id].line_number,
                        "duplicate_line": line_number,
                    }
                )
            else:
                index[review_id] = _IndexedRow(
                    position=position,
                    line_number=line_number,
                    offset=offset,
                    review_id=review_id,
                    source_ordinal=row.get("source_ordinal"),
                    anonymous_stock_id=packet.get("anonymous_stock_id"),
                    as_of=packet.get("as_of"),
                )
                order.append(review_id)
            position += 1
    return index, order, duplicates, guards


def _read_indexed(handle: BinaryIO, path: Path, indexed: _IndexedRow) -> dict[str, Any]:
    handle.seek(indexed.offset)
    return _decode_json_line(handle.readline(), path=path, line_number=indexed.line_number)


def _diff_paths(
    old: Any,
    new: Any,
    *,
    prefix: str = "",
    limit: int = MAX_PATH_EXAMPLES,
) -> tuple[int, list[str]]:
    """Return leaf-difference count plus a capped list of paths."""

    paths: list[str] = []
    count = 0

    def walk(left: Any, right: Any, path: str) -> None:
        nonlocal count
        if left is not _MISSING and right is not _MISSING and left == right:
            return
        if isinstance(left, dict) and isinstance(right, dict):
            for key in sorted(set(left) | set(right), key=str):
                child = f"{path}.{key}" if path else str(key)
                walk(left.get(key, _MISSING), right.get(key, _MISSING), child)
            return
        if isinstance(left, list) and isinstance(right, list):
            for index in range(max(len(left), len(right))):
                child = f"{path}[{index}]"
                walk(
                    left[index] if index < len(left) else _MISSING,
                    right[index] if index < len(right) else _MISSING,
                    child,
                )
            return
        count += 1
        if len(paths) < limit:
            paths.append(path or "<root>")

    walk(old, new, prefix)
    return count, paths


def _question_manifest_diff(old: Any, new: Any) -> dict[str, Any]:
    count, paths = _diff_paths(old, new, prefix="question_manifest")
    return {
        "changed": count > 0,
        "old_sha256": canonical_sha256(old),
        "new_sha256": canonical_sha256(new),
        "difference_count": count,
        "difference_paths": paths,
        "difference_paths_truncated": max(0, count - len(paths)),
    }


def _evidence_map(value: Any) -> tuple[list[str], dict[str, Any]]:
    rows = value if isinstance(value, list) else []
    order: list[str] = []
    mapped: dict[str, Any] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            ref = f"__INVALID_EVIDENCE_ROW_{index}__"
        else:
            ref = str(row.get("ref") or f"__MISSING_EVIDENCE_REF_{index}__")
        order.append(ref)
        mapped[ref] = row
    return order, mapped


def _evidence_diff(old: Any, new: Any) -> dict[str, Any]:
    old_order, old_map = _evidence_map(old)
    new_order, new_map = _evidence_map(new)
    added = sorted(set(new_map) - set(old_map))
    removed = sorted(set(old_map) - set(new_map))
    changed: list[dict[str, Any]] = []
    for ref in sorted(set(old_map) & set(new_map)):
        if old_map[ref] == new_map[ref]:
            continue
        count, paths = _diff_paths(old_map[ref], new_map[ref], prefix=f"evidence[{ref}]")
        changed.append(
            {
                "ref": ref,
                "old_kind": (old_map[ref] or {}).get("kind") if isinstance(old_map[ref], dict) else None,
                "new_kind": (new_map[ref] or {}).get("kind") if isinstance(new_map[ref], dict) else None,
                "difference_count": count,
                "difference_paths": paths,
                "difference_paths_truncated": max(0, count - len(paths)),
            }
        )
    changed_count = len(changed)
    changed_examples = changed[:MAX_REF_EXAMPLES]
    order_changed = old_order != new_order
    return {
        "changed": bool(added or removed or changed or order_changed),
        "old_sha256": canonical_sha256(old),
        "new_sha256": canonical_sha256(new),
        "old_rows": len(old_order),
        "new_rows": len(new_order),
        "added_count": len(added),
        "added_refs": added[:MAX_REF_EXAMPLES],
        "added_refs_truncated": max(0, len(added) - MAX_REF_EXAMPLES),
        "removed_count": len(removed),
        "removed_refs": removed[:MAX_REF_EXAMPLES],
        "removed_refs_truncated": max(0, len(removed) - MAX_REF_EXAMPLES),
        "changed_count": changed_count,
        "changed_refs": changed_examples,
        "changed_refs_truncated": max(0, changed_count - len(changed_examples)),
        "order_changed": order_changed,
        "old_order_sha256": canonical_sha256(old_order),
        "new_order_sha256": canonical_sha256(new_order),
    }


def _leg_summary(leg: Any) -> Any:
    if not isinstance(leg, dict):
        return leg
    return {
        key: leg.get(key)
        for key in (
            "start_date",
            "end_date",
            "start_ref",
            "end_ref",
            "start_price",
            "end_price",
            "bar_count",
            "price_change_pct",
            "slope_pct_per_bar",
            "mean_true_range_pct",
            "realized_close_volatility_pct",
        )
    }


def _window_summary(window: Any) -> Any:
    if not isinstance(window, dict):
        return window
    return {
        "scale": window.get("scale"),
        "direction": window.get("direction"),
        "selection_policy": window.get("selection_policy"),
        "baseline_available_on": window.get("baseline_available_on"),
        "current_available_on": window.get("current_available_on"),
        "available_on": window.get("available_on"),
        "baseline": _leg_summary(window.get("baseline")),
        "current": _leg_summary(window.get("current")),
    }


def _comparison_diff(old: Any, new: Any) -> dict[str, Any]:
    old_windows = old if isinstance(old, dict) else {}
    new_windows = new if isinstance(new, dict) else {}
    scales: list[dict[str, Any]] = []
    for scale in sorted(set(old_windows) | set(new_windows) | {"LARGE", "SMALL"}):
        old_window = old_windows.get(scale)
        new_window = new_windows.get(scale)
        if old_window == new_window:
            continue
        count, paths = _diff_paths(
            old_window,
            new_window,
            prefix=f"objective_facts.working_comparison_windows.{scale}",
        )
        scales.append(
            {
                "scale": scale,
                "old_sha256": canonical_sha256(old_window),
                "new_sha256": canonical_sha256(new_window),
                "difference_count": count,
                "difference_paths": paths,
                "difference_paths_truncated": max(0, count - len(paths)),
                "old": _window_summary(old_window),
                "new": _window_summary(new_window),
            }
        )
    return {"changed": bool(scales), "scales": scales}


def _invalid_comparison_legs(packet: dict[str, Any]) -> list[dict[str, Any]]:
    windows = ((packet.get("objective_facts") or {}).get("working_comparison_windows") or {})
    invalid: list[dict[str, Any]] = []
    for scale in ("LARGE", "SMALL"):
        window = windows.get(scale)
        if not isinstance(window, dict):
            continue
        for role in ("baseline", "current"):
            leg = window.get(role)
            if not isinstance(leg, dict):
                continue
            bar_count = leg.get("bar_count")
            same_or_reverse_day = str(leg.get("start_date") or "") >= str(
                leg.get("end_date") or ""
            )
            if isinstance(bar_count, bool) or not isinstance(bar_count, int):
                invalid.append({"scale": scale, "role": role, "reason": "INVALID_BAR_COUNT"})
            elif bar_count < 2 or same_or_reverse_day:
                invalid.append(
                    {
                        "scale": scale,
                        "role": role,
                        "bar_count": bar_count,
                        "start_date": leg.get("start_date"),
                        "end_date": leg.get("end_date"),
                        "reason": "FEWER_THAN_TWO_VISIBLE_BARS",
                    }
                )
    return invalid


def _comparison_dependency_refs(windows: Any) -> dict[str, set[str]]:
    """Return the exact evidence closure introduced by comparison windows.

    V4/V5 evidence construction makes comparison endpoint pivots visible even
    when they fall outside the ordinary bounded pivot catalogue.  Making a
    pivot visible also makes its source/confirmation bars visible.  Therefore
    replacing an invalid one-bar leg can legitimately change three evidence
    kinds, but only when every changed ref is in this direct closure.
    """

    result = {"comparison": set(), "pivot": set(), "bar": set()}
    if not isinstance(windows, dict):
        return result
    for scale in ("LARGE", "SMALL"):
        window = windows.get(scale)
        if not isinstance(window, dict):
            continue
        result["comparison"].add(
            f"COMPARISON_WINDOW:{scale}:{canonical_sha256(window)[:16]}"
        )
        for role in ("baseline", "current"):
            leg = window.get(role)
            if not isinstance(leg, dict):
                continue
            for key in ("start_date", "end_date"):
                value = leg.get(key)
                if isinstance(value, str) and value:
                    result["bar"].add(f"BAR:{value}")
            for key in ("start_ref", "end_ref"):
                ref = leg.get(key)
                if not isinstance(ref, str) or not ref.startswith("PIVOT:"):
                    continue
                result["pivot"].add(ref)
                parts = ref.split(":")
                if len(parts) == 5:
                    result["bar"].update((f"BAR:{parts[3]}", f"BAR:{parts[4]}"))
    return result


def _evidence_change_is_comparison_dependency_only(
    old: Any,
    new: Any,
    *,
    old_windows: Any,
    new_windows: Any,
) -> bool:
    if not isinstance(old, list) or not isinstance(new, list):
        return False
    if any(not isinstance(row, dict) or not isinstance(row.get("ref"), str) for row in [*old, *new]):
        return False
    old_by_ref = {str(row["ref"]): row for row in old}
    new_by_ref = {str(row["ref"]): row for row in new}
    if len(old_by_ref) != len(old) or len(new_by_ref) != len(new):
        return False

    old_dependencies = _comparison_dependency_refs(old_windows)
    new_dependencies = _comparison_dependency_refs(new_windows)
    allowed = {
        kind: old_dependencies[kind] | new_dependencies[kind]
        for kind in ("comparison", "pivot", "bar")
    }
    changed_refs = {
        ref
        for ref in set(old_by_ref) | set(new_by_ref)
        if old_by_ref.get(ref) != new_by_ref.get(ref)
    }
    for ref in changed_refs:
        old_row = old_by_ref.get(ref)
        new_row = new_by_ref.get(ref)
        # A comparison-window selection adds/removes dependencies.  It never
        # mutates a stable evidence ref in place.
        if old_row is not None and new_row is not None:
            return False
        row = new_row if new_row is not None else old_row
        kind = str(row.get("kind"))
        if kind == "CAUSAL_COMPARISON_WINDOW" and ref in allowed["comparison"]:
            continue
        if kind == "CONFIRMED_PIVOT" and ref in allowed["pivot"]:
            continue
        if kind == "BAR" and ref in allowed["bar"]:
            continue
        return False
    return True


def _ai_visible_bar_count_consistent(packet: dict[str, Any]) -> bool:
    objective = packet.get("objective_facts") or {}
    declared = objective.get("ai_visible_bar_count")
    if declared is None:
        return True
    if isinstance(declared, bool) or not isinstance(declared, int):
        return False
    evidence = packet.get("evidence")
    if not isinstance(evidence, list):
        return False
    return declared == sum(
        isinstance(row, dict) and row.get("kind") == "BAR" for row in evidence
    )


def _paths_are_scoped(paths: Sequence[str], allowed_prefixes: Sequence[str]) -> bool:
    return all(
        any(
            path == prefix
            or path.startswith(prefix + ".")
            or path.startswith(prefix + "[")
            for prefix in allowed_prefixes
        )
        for path in paths
    )


def _normalized_packet_remainder(packet: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(packet)
    for field in (
        "input_packet_sha256",
        "question_manifest_sha256",
        "evidence_catalog_sha256",
        "question_manifest",
        "evidence",
    ):
        normalized.pop(field, None)
    objective = dict(normalized.get("objective_facts") or {})
    for field in (
        "builder_version",
        "ai_visible_evidence_sha256",
        "working_comparison_windows",
    ):
        objective.pop(field, None)
    normalized["objective_facts"] = objective
    return normalized


def _normalized_row_metadata(row: dict[str, Any], packet: dict[str, Any]) -> dict[str, Any]:
    if row is packet:
        return {}
    return {
        key: value
        for key, value in row.items()
        if key
        not in {
            "packet",
            "packet_sha256",
            "review_id",
            "anonymous_stock_id",
            "source_ordinal",
        }
    }


def _compare_case(old_row: dict[str, Any], new_row: dict[str, Any]) -> dict[str, Any]:
    old_packet = _packet(old_row)
    new_packet = _packet(new_row)
    review_id = str(old_packet.get("review_id") or old_row.get("review_id") or "")
    old_objective = old_packet.get("objective_facts") or {}
    new_objective = new_packet.get("objective_facts") or {}

    identity_fields = {
        "anonymous_stock_id": (
            old_packet.get("anonymous_stock_id"),
            new_packet.get("anonymous_stock_id"),
        ),
        "as_of": (old_packet.get("as_of"), new_packet.get("as_of")),
    }
    identity_differences = {
        field: {"old": values[0], "new": values[1]}
        for field, values in identity_fields.items()
        if values[0] != values[1]
    }
    source_ordinal_changed = old_row.get("source_ordinal") != new_row.get("source_ordinal")

    question = _question_manifest_diff(
        old_packet.get("question_manifest"), new_packet.get("question_manifest")
    )
    evidence = _evidence_diff(old_packet.get("evidence"), new_packet.get("evidence"))
    comparison = _comparison_diff(
        old_objective.get("working_comparison_windows"),
        new_objective.get("working_comparison_windows"),
    )
    other_packet_count, other_packet_paths = _diff_paths(
        _normalized_packet_remainder(old_packet),
        _normalized_packet_remainder(new_packet),
        prefix="packet",
    )
    old_metadata = _normalized_row_metadata(old_row, old_packet)
    new_metadata = _normalized_row_metadata(new_row, new_packet)
    metadata_count, metadata_paths = _diff_paths(old_metadata, new_metadata, prefix="row")

    old_invalid_legs = _invalid_comparison_legs(old_packet)
    new_invalid_legs = _invalid_comparison_legs(new_packet)
    builder_transition = (
        old_objective.get("builder_version") == "hybrid-v3-atomic-packets-v4"
        and new_objective.get("builder_version") == "hybrid-v3-atomic-packets-v5"
    )
    allowed_other_prefixes = (
        "packet.objective_facts.ai_visible_bar_count",
        "packet.objective_facts.data_sufficiency_by_route",
        "packet.objective_facts.material_objective_hypothesis_conflict",
        "packet.objective_facts.material_objective_signature_count",
        "packet.objective_facts.wait_boundary_reasons",
    )
    allowed_metadata_prefixes = (
        "row.eligible_sampling_strata",
        "row.eligible_sampling_strata_sha256",
        "row.primary_sampling_focus",
    )
    comparison_dependency_evidence_only = _evidence_change_is_comparison_dependency_only(
        old_packet.get("evidence"),
        new_packet.get("evidence"),
        old_windows=old_objective.get("working_comparison_windows"),
        new_windows=new_objective.get("working_comparison_windows"),
    )
    ai_visible_bar_count_consistent = _ai_visible_bar_count_consistent(
        old_packet
    ) and _ai_visible_bar_count_consistent(new_packet)
    scoped_repair = bool(
        old_invalid_legs
        and not new_invalid_legs
        and builder_transition
        and not identity_differences
        and not source_ordinal_changed
        and not question["changed"]
        and comparison["changed"]
        and comparison_dependency_evidence_only
        and ai_visible_bar_count_consistent
        and _paths_are_scoped(other_packet_paths, allowed_other_prefixes)
        and _paths_are_scoped(metadata_paths, allowed_metadata_prefixes)
    )

    substantive = bool(
        identity_differences
        or source_ordinal_changed
        or question["changed"]
        or evidence["changed"]
        or comparison["changed"]
        or other_packet_count
        or metadata_count
    )
    return {
        "review_id": review_id,
        "expected_changes": {
            "builder_version": {
                "changed": old_objective.get("builder_version")
                != new_objective.get("builder_version"),
                "old": old_objective.get("builder_version"),
                "new": new_objective.get("builder_version"),
            },
            "input_packet_sha256": {
                "changed": old_packet.get("input_packet_sha256")
                != new_packet.get("input_packet_sha256"),
                "old": old_packet.get("input_packet_sha256"),
                "new": new_packet.get("input_packet_sha256"),
            },
            "row_packet_sha256": {
                "changed": old_row.get("packet_sha256") != new_row.get("packet_sha256"),
                "old": old_row.get("packet_sha256"),
                "new": new_row.get("packet_sha256"),
            },
        },
        "identity_differences": identity_differences,
        "source_ordinal": {
            "changed": source_ordinal_changed,
            "old": old_row.get("source_ordinal"),
            "new": new_row.get("source_ordinal"),
        },
        "question_manifest": question,
        "evidence": evidence,
        "comparison_legs": comparison,
        "other_packet_differences": {
            "changed": other_packet_count > 0,
            "difference_count": other_packet_count,
            "difference_paths": other_packet_paths,
            "difference_paths_truncated": max(0, other_packet_count - len(other_packet_paths)),
        },
        "row_metadata_differences": {
            "changed": metadata_count > 0,
            "difference_count": metadata_count,
            "difference_paths": metadata_paths,
            "difference_paths_truncated": max(0, metadata_count - len(metadata_paths)),
        },
        "one_bar_contract_repair": {
            "eligible": scoped_repair,
            "builder_transition_valid": builder_transition,
            "old_invalid_legs": old_invalid_legs,
            "new_invalid_legs": new_invalid_legs,
            "comparison_dependency_evidence_only": comparison_dependency_evidence_only,
            "ai_visible_bar_count_consistent": ai_visible_bar_count_consistent,
            "other_packet_paths_scoped": _paths_are_scoped(
                other_packet_paths, allowed_other_prefixes
            ),
            "row_metadata_paths_scoped": _paths_are_scoped(
                metadata_paths, allowed_metadata_prefixes
            ),
        },
        "substantive_difference": substantive,
    }


def audit(old_path: Path, new_path: Path) -> dict[str, Any]:
    old_path = Path(old_path)
    new_path = Path(new_path)
    old_index, old_order, old_duplicates, old_guards = _scan_old(old_path)
    new_guards = _GuardSummary()
    new_order: list[str] = []
    new_positions: dict[str, int] = {}
    new_duplicates: list[dict[str, Any]] = []
    added: list[dict[str, Any]] = []
    seen_shared: set[str] = set()
    substantive_cases: list[dict[str, Any]] = []
    scoped_repair_cases: list[dict[str, Any]] = []
    unexpected_substantive_cases: list[dict[str, Any]] = []
    expected_only_cases = 0
    builder_version_change_count = 0
    input_packet_hash_change_count = 0
    row_packet_hash_change_count = 0
    comparison_change_count = 0
    question_change_count = 0
    evidence_change_count = 0
    identity_change_count = 0
    invalid_builder_transition_count = 0

    with old_path.open("rb") as old_handle, new_path.open("rb") as new_handle:
        position = 0
        line_number = 0
        while True:
            raw = new_handle.readline()
            if not raw:
                break
            line_number += 1
            if not raw.strip():
                continue
            try:
                new_row = _decode_json_line(raw, path=new_path, line_number=line_number)
            except ValueError as exc:
                new_guards.add(line_number=line_number, review_id=None, errors=[str(exc)])
                position += 1
                continue
            new_packet = _packet(new_row)
            review_id = str(new_row.get("review_id") or new_packet.get("review_id") or "")
            new_guards.add(
                line_number=line_number,
                review_id=review_id or None,
                errors=validate_row_guard(new_row),
            )
            if not review_id:
                position += 1
                continue
            if review_id in new_positions:
                new_duplicates.append(
                    {
                        "review_id": review_id,
                        "first_position": new_positions[review_id],
                        "duplicate_position": position,
                        "duplicate_line": line_number,
                    }
                )
                position += 1
                continue
            new_positions[review_id] = position
            new_order.append(review_id)
            old_entry = old_index.get(review_id)
            if old_entry is None:
                added.append(
                    {
                        "review_id": review_id,
                        "new_position": position,
                        "source_ordinal": new_row.get("source_ordinal"),
                        "anonymous_stock_id": new_packet.get("anonymous_stock_id"),
                        "as_of": new_packet.get("as_of"),
                    }
                )
                position += 1
                continue
            seen_shared.add(review_id)
            old_row = _read_indexed(old_handle, old_path, old_entry)
            case = _compare_case(old_row, new_row)
            expected = case["expected_changes"]
            builder_version_change_count += int(expected["builder_version"]["changed"])
            input_packet_hash_change_count += int(expected["input_packet_sha256"]["changed"])
            row_packet_hash_change_count += int(expected["row_packet_sha256"]["changed"])
            comparison_change_count += int(case["comparison_legs"]["changed"])
            question_change_count += int(case["question_manifest"]["changed"])
            evidence_change_count += int(case["evidence"]["changed"])
            identity_change_count += int(bool(case["identity_differences"]))
            invalid_builder_transition_count += int(
                not case["one_bar_contract_repair"]["builder_transition_valid"]
            )
            if case["substantive_difference"]:
                substantive_cases.append(case)
                if case["one_bar_contract_repair"]["eligible"]:
                    scoped_repair_cases.append(case)
                else:
                    unexpected_substantive_cases.append(case)
            else:
                expected_only_cases += 1
            position += 1

    deleted = [
        {
            "review_id": review_id,
            "old_position": entry.position,
            "source_ordinal": entry.source_ordinal,
            "anonymous_stock_id": entry.anonymous_stock_id,
            "as_of": entry.as_of,
        }
        for review_id, entry in old_index.items()
        if review_id not in seen_shared
    ]
    deleted.sort(key=lambda row: int(row["old_position"]))

    shared_ids = set(old_index) & set(new_positions)
    old_shared_order = [review_id for review_id in old_order if review_id in shared_ids]
    new_shared_order = [review_id for review_id in new_order if review_id in shared_ids]
    order_changed = old_shared_order != new_shared_order
    old_shared_positions = {review_id: index for index, review_id in enumerate(old_shared_order)}
    new_shared_positions = {review_id: index for index, review_id in enumerate(new_shared_order)}
    moved = [
        {
            "review_id": review_id,
            "old_shared_position": old_shared_positions[review_id],
            "new_shared_position": new_shared_positions[review_id],
        }
        for review_id in old_shared_order
        if old_shared_positions[review_id] != new_shared_positions[review_id]
    ]

    old_guard_result = old_guards.result()
    new_guard_result = new_guards.result()
    fail_reasons: list[str] = []
    if old_guard_result["status"] != "PASS" or new_guard_result["status"] != "PASS":
        fail_reasons.append("IDENTITY_OR_FUTURE_GUARD_FAILURE")
    if old_duplicates or new_duplicates:
        fail_reasons.append("DUPLICATE_REVIEW_ID")
    if added:
        fail_reasons.append("REVIEW_IDS_ADDED")
    if deleted:
        fail_reasons.append("REVIEW_IDS_DELETED")
    if order_changed:
        fail_reasons.append("SHARED_REVIEW_ID_ORDER_CHANGED")
    if identity_change_count:
        fail_reasons.append("ANONYMOUS_IDENTITY_OR_AS_OF_CHANGED")
    if any(case["question_manifest"]["changed"] for case in unexpected_substantive_cases):
        fail_reasons.append("QUESTION_MANIFEST_CHANGED")
    if any(case["evidence"]["changed"] for case in unexpected_substantive_cases):
        fail_reasons.append("EVIDENCE_CHANGED")
    if any(case["comparison_legs"]["changed"] for case in unexpected_substantive_cases):
        fail_reasons.append("COMPARISON_LEGS_CHANGED")
    if any(case["other_packet_differences"]["changed"] for case in unexpected_substantive_cases):
        fail_reasons.append("OTHER_PACKET_FIELDS_CHANGED")
    if any(case["row_metadata_differences"]["changed"] for case in unexpected_substantive_cases):
        fail_reasons.append("ROW_METADATA_CHANGED")
    if any(case["source_ordinal"]["changed"] for case in unexpected_substantive_cases):
        fail_reasons.append("SOURCE_ORDINAL_CHANGED")

    if invalid_builder_transition_count:
        fail_reasons.append("BUILDER_VERSION_TRANSITION_INVALID")

    fail_reasons = sorted(set(fail_reasons))
    status = "PASS" if not fail_reasons else "FAIL"
    return {
        "audit_version": AUDIT_VERSION,
        "status": status,
        "performance_or_future_fields_read": False,
        "outcome_blind": True,
        "allowed_changes": [
            "packet.objective_facts.builder_version",
            "packet.input_packet_sha256",
            "row.packet_sha256",
            "for old one-bar comparison cases only: working comparison window and its direct comparison/pivot/bar evidence closure",
            "for old one-bar comparison cases only: fixed derived objective/sampling fields",
        ],
        "old": {
            "path": str(old_path.resolve()),
            "sha256": file_sha256(old_path),
            "rows": old_guards.rows_checked,
            "unique_review_ids": len(old_index),
        },
        "new": {
            "path": str(new_path.resolve()),
            "sha256": file_sha256(new_path),
            "rows": new_guards.rows_checked,
            "unique_review_ids": len(new_positions),
        },
        "summary": {
            "shared_review_ids": len(shared_ids),
            "added_review_ids": len(added),
            "deleted_review_ids": len(deleted),
            "substantive_difference_cases": len(substantive_cases),
            "scoped_one_bar_contract_repair_cases": len(scoped_repair_cases),
            "unexpected_substantive_difference_cases": len(unexpected_substantive_cases),
            "expected_only_cases": expected_only_cases,
            "builder_version_change_cases": builder_version_change_count,
            "invalid_builder_version_transition_cases": invalid_builder_transition_count,
            "input_packet_sha256_change_cases": input_packet_hash_change_count,
            "row_packet_sha256_change_cases": row_packet_hash_change_count,
            "identity_or_as_of_change_cases": identity_change_count,
            "question_manifest_change_cases": question_change_count,
            "evidence_change_cases": evidence_change_count,
            "comparison_leg_change_cases": comparison_change_count,
            "fail_reason_count": len(fail_reasons),
        },
        "fail_reasons": fail_reasons,
        "guards": {"old": old_guard_result, "new": new_guard_result},
        "review_id_coverage": {
            "added": added,
            "deleted": deleted,
            "old_duplicates": old_duplicates,
            "new_duplicates": new_duplicates,
        },
        "order": {
            "changed": order_changed,
            "old_review_id_order_sha256": canonical_sha256(old_order),
            "new_review_id_order_sha256": canonical_sha256(new_order),
            "old_shared_order_sha256": canonical_sha256(old_shared_order),
            "new_shared_order_sha256": canonical_sha256(new_shared_order),
            "moved_count": len(moved),
            "moved": moved[:MAX_REF_EXAMPLES],
            "moved_truncated": max(0, len(moved) - MAX_REF_EXAMPLES),
        },
        "substantive_case_differences": substantive_cases,
        "scoped_one_bar_contract_repairs": scoped_repair_cases,
        "unexpected_substantive_case_differences": unexpected_substantive_cases,
    }


def _markdown_escape(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "# Candidate3 → Candidate4 封包差異稽核",
        "",
        f"- Status: `{report['status']}`",
        f"- Audit version: `{report['audit_version']}`",
        "- Outcome blind: `true`",
        "- Performance or future fields read: `false`",
        f"- Old: `{report['old']['path']}`",
        f"- New: `{report['new']['path']}`",
        "",
        "## 判定",
        "",
        "一般案例只允許 `objective_facts.builder_version`、有效重算的 "
        "`input_packet_sha256` 與外層 `packet_sha256` 改變。只有舊封包可證明含少於兩根K的比較段時，"
        "才允許比較窗、比較窗證據與固定衍生欄位改變；其餘差異一律 fail-closed。",
        "",
        "| 指標 | 數量 |",
        "|---|---:|",
        f"| Shared review IDs | {summary['shared_review_ids']} |",
        f"| Added review IDs | {summary['added_review_ids']} |",
        f"| Deleted review IDs | {summary['deleted_review_ids']} |",
        f"| Expected-only cases | {summary['expected_only_cases']} |",
        f"| Substantive-difference cases | {summary['substantive_difference_cases']} |",
        f"| Scoped one-bar repairs | {summary['scoped_one_bar_contract_repair_cases']} |",
        f"| Unexpected substantive differences | {summary['unexpected_substantive_difference_cases']} |",
        f"| Identity/as_of changes | {summary['identity_or_as_of_change_cases']} |",
        f"| Question-manifest changes | {summary['question_manifest_change_cases']} |",
        f"| Evidence changes | {summary['evidence_change_cases']} |",
        f"| Comparison-leg changes | {summary['comparison_leg_change_cases']} |",
        "",
        "## Fail reasons",
        "",
    ]
    if report["fail_reasons"]:
        lines.extend(f"- `{reason}`" for reason in report["fail_reasons"])
    else:
        lines.append("- None")

    lines.extend(["", "## Identity / future guards", ""])
    for side in ("old", "new"):
        guard = report["guards"][side]
        lines.append(
            f"- {side}: `{guard['status']}`; checked={guard['rows_checked']}, "
            f"failed={guard['rows_failed']}, errors={guard['error_count']}"
        )
        for example in guard["examples"][:10]:
            lines.append(
                f"  - line {example['line_number']} / "
                f"{_markdown_escape(example.get('review_id'))}: "
                f"{_markdown_escape('; '.join(example['errors']))}"
            )

    coverage = report["review_id_coverage"]
    lines.extend(["", "## Coverage and order", ""])
    lines.append(f"- Shared order changed: `{str(report['order']['changed']).lower()}`")
    lines.append(f"- Moved shared IDs: {report['order']['moved_count']}")
    if coverage["added"]:
        lines.append("- Added: " + ", ".join(row["review_id"] for row in coverage["added"][:30]))
    if coverage["deleted"]:
        lines.append("- Deleted: " + ", ".join(row["review_id"] for row in coverage["deleted"][:30]))

    cases = report["substantive_case_differences"]
    lines.extend(["", "## Substantive case differences", ""])
    if not cases:
        lines.append("None.")
    else:
        lines.extend(
            [
                "| review_id | identity/as_of | source ordinal | question manifest | evidence | comparison legs | other packet | row metadata |",
                "|---|---:|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for case in cases[:MAX_MD_CASES]:
            lines.append(
                "| {review_id} | {identity} | {ordinal} | {question} | {evidence} | "
                "{comparison} | {other} | {metadata} |".format(
                    review_id=_markdown_escape(case["review_id"]),
                    identity=int(bool(case["identity_differences"])),
                    ordinal=int(case["source_ordinal"]["changed"]),
                    question=int(case["question_manifest"]["changed"]),
                    evidence=int(case["evidence"]["changed"]),
                    comparison=int(case["comparison_legs"]["changed"]),
                    other=int(case["other_packet_differences"]["changed"]),
                    metadata=int(case["row_metadata_differences"]["changed"]),
                )
            )
        if len(cases) > MAX_MD_CASES:
            lines.append("")
            lines.append(
                f"Markdown 僅列前 {MAX_MD_CASES} 件；JSON 保留全部 {len(cases)} 件差異。"
            )

        comparison_cases = [case for case in cases if case["comparison_legs"]["changed"]]
        if comparison_cases:
            lines.extend(["", "## Comparison-leg details", ""])
            for case in comparison_cases[:MAX_MD_CASES]:
                lines.append(f"### `{case['review_id']}`")
                lines.append("")
                for scale in case["comparison_legs"]["scales"]:
                    lines.append(
                        f"- {scale['scale']}: `{scale['old_sha256']}` → `{scale['new_sha256']}`; "
                        f"paths: {_markdown_escape(', '.join(scale['difference_paths']))}"
                    )

    lines.append("")
    return "\n".join(lines)


def publish_reports(
    report: dict[str, Any], *, output_json: Path, output_md: Path
) -> None:
    output_json = Path(output_json)
    output_md = Path(output_md)
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_md.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_bytes(json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8") + b"\n")
    output_md.write_text(render_markdown(report), encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old", type=Path, required=True)
    parser.add_argument("--new", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-md", type=Path, required=True)
    args = parser.parse_args(argv)

    report = audit(args.old, args.new)
    publish_reports(report, output_json=args.output_json, output_md=args.output_md)
    print(
        json.dumps(
            {
                "status": report["status"],
                "performance_or_future_fields_read": False,
                "shared_review_ids": report["summary"]["shared_review_ids"],
                "substantive_difference_cases": report["summary"][
                    "substantive_difference_cases"
                ],
                "output_json": str(args.output_json.resolve()),
                "output_md": str(args.output_md.resolve()),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
