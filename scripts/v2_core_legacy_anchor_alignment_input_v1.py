"""Build blind AS-OF packets for the one-pass legacy anchor alignment subgate.

The calibration selection is intentionally known to contain structured legacy
anchor references, but this builder never reads those answer values.  It reads
only an anonymous review-id selection, the frozen Campaign R2 pool, its R1
catalog, and the existing anonymous AS-OF packet.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_DIR = (
    ROOT
    / "reports"
    / "course_backtest"
    / "2026-09-10"
    / "v2_core_reproducible_goal_v1"
)
PACKET_VERSION = "v2-core-legacy-anchor-alignment-input-r1-candidate"
OUTPUT_DIRECTORY = "legacy_anchor_alignment_input_packets_candidate_r1"
OUTPUT_MANIFEST = "legacy_anchor_alignment_input_manifest_candidate_r1.json"
SELECTION_FILE = "legacy_anchor_alignment_selection_candidate_r1.json"
POOL_MANIFEST_FILE = "campaign_candidate_pool_manifest_candidate_r2.json"
CATALOG_MANIFEST_FILE = "campaign_candidate_catalog_manifest_candidate_r1.json"

FORBIDDEN_KEYS = {
    "code",
    "name",
    "symbol",
    "case_role",
    "intended_scenario",
    "expected_permission",
    "legacy_v2_trigger",
    "legacy_v2_no_trade_reason",
    "future_outcome",
    "future_performance",
    "mfe",
    "mae",
    "pnl",
    "profit",
    "return_pct",
    "exit_date",
    "exit_price",
}

DAILY_FIELDS = (
    "date",
    "open",
    "high",
    "low",
    "close",
    "atr14",
    "ma21",
    "ma55",
    "ma105",
    "ma144",
    "macd_hist",
    "macd_hist_delta",
    "return_1d_pct",
    "volume_ratio_20",
    "large_pivot_high",
    "large_pivot_high_date",
    "large_pivot_low",
    "large_pivot_low_date",
    "small_pivot_high",
    "small_pivot_high_date",
    "small_pivot_low",
    "small_pivot_low_date",
    "facts",
)


def canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def sha256_path(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def stable_id(prefix: str, *parts: str) -> str:
    payload = "\x1f".join(parts).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(payload).hexdigest()[:20]}"


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_new_or_identical(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_bytes() != payload:
        raise FileExistsError(f"refusing to overwrite non-identical artifact: {path}")
    path.write_bytes(payload)


def _forbidden_paths(value: Any, path: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}/{key}"
            normalized = str(key).lower()
            if normalized in FORBIDDEN_KEYS or any(
                token in normalized
                for token in ("legacy_answer", "future_pnl", "future_return", "stock_code")
            ):
                found.append(child_path)
            found.extend(_forbidden_paths(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_forbidden_paths(child, f"{path}/{index}"))
    return found


def _compact_daily_context(
    rows: list[dict[str, Any]], candidates: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not rows:
        return [], {"visible_bar_count": 0, "retained_bar_count": 0}
    important_dates: set[str] = set()
    for candidate in candidates:
        for key in ("start_date", "confirmed_end_date", "observed_through"):
            value = candidate.get(key)
            if value:
                important_dates.add(str(value))
        for ref in candidate.get("source_evidence_refs", []):
            if str(ref).startswith("PRICE:"):
                important_dates.add(str(ref).split(":", 1)[1])
    recent_start = max(0, len(rows) - 252)
    kept: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        keep = index >= recent_start or index % 5 == 0 or str(row["date"]) in important_dates
        if not keep:
            continue
        kept.append({key: row.get(key) for key in DAILY_FIELDS})
    return kept, {
        "visible_bar_count": len(rows),
        "retained_bar_count": len(kept),
        "recent_full_resolution_bars": len(rows) - recent_start,
        "older_sampling_stride": 5,
        "candidate_boundary_dates_forced": True,
    }


def _candidate_records(
    pool: dict[str, Any], catalog: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    catalog_map = {
        str(item["candidate_id"]): item for item in catalog["campaign_candidates"]
    }
    candidates: list[dict[str, Any]] = []
    evidence_options: list[dict[str, Any]] = []
    for pool_row in pool["candidate_pool"]:
        candidate_id = str(pool_row["candidate_id"])
        source = catalog_map.get(candidate_id)
        if source is None:
            raise ValueError(f"pool candidate missing from catalog: {candidate_id}")
        option_id = stable_id("CAOPT", str(pool["review_id"]), candidate_id)
        row = {
            **source,
            "selection_reasons": list(pool_row["selection_reasons"]),
            "evidence_option_id": option_id,
        }
        candidates.append(row)
        evidence_options.append(
            {
                "evidence_option_id": option_id,
                "candidate_id": candidate_id,
                "source_evidence_refs": list(source["source_evidence_refs"]),
            }
        )
    candidates.sort(key=lambda item: item["candidate_id"])
    evidence_options.sort(key=lambda item: item["candidate_id"])
    return candidates, evidence_options


def build_packet(
    *,
    source_packet: dict[str, Any],
    catalog: dict[str, Any],
    pool: dict[str, Any],
    source_packet_sha256: str,
    catalog_sha256: str,
    pool_sha256: str,
    selection_sha256: str,
) -> dict[str, Any]:
    if not (
        source_packet["review_id"] == catalog["review_id"] == pool["review_id"]
        and source_packet["as_of"] == catalog["as_of"] == pool["as_of"]
    ):
        raise ValueError("source identity mismatch")
    candidates, evidence_options = _candidate_records(pool, catalog)
    daily_context, resolution = _compact_daily_context(
        list(source_packet["daily_structure_context_to_as_of"]), candidates
    )
    packet = {
        "packet_version": PACKET_VERSION,
        "status": "BLIND_ASOF_ALIGNMENT_INPUT（盲化AS-OF對齊輸入）",
        "task": "SELECT_ONE_CONTROLLING_ANCHOR_OR_UNRESOLVED",
        "review_id": source_packet["review_id"],
        "anonymous_stock_id": source_packet["anonymous_stock_id"],
        "as_of": source_packet["as_of"],
        "data_quality": source_packet["data_quality"],
        "context_resolution": resolution,
        "daily_context_to_as_of": daily_context,
        "confirmed_pivots_to_as_of": source_packet["confirmed_pivots_to_as_of"],
        "completed_macd_21_55_55_cycles_to_as_of": source_packet[
            "completed_macd_21_55_55_cycles_to_as_of"
        ],
        "proxy_evidence": source_packet["proxy_evidence"],
        "candidate_pool": candidates,
        "candidate_evidence_options": evidence_options,
        "source_bindings": {
            "source_packet_sha256": source_packet_sha256,
            "campaign_catalog_sha256": catalog_sha256,
            "campaign_pool_sha256": pool_sha256,
            "selection_sha256": selection_sha256,
        },
        "blindness_contract": {
            "identity_included": False,
            "historical_reference_values_included": False,
            "future_performance_included": False,
            "future_bars_included": False,
            "locked_reproduction_set_opened": False,
            "candidate_selection_is_course_judgement": False,
        },
    }
    forbidden = _forbidden_paths(packet)
    if forbidden:
        raise ValueError(f"forbidden keys in packet: {forbidden[:10]}")
    if any(str(row["date"]) > str(packet["as_of"]) for row in daily_context):
        raise ValueError("future daily row found")
    if not candidates:
        raise ValueError("empty candidate pool")
    return packet


def build_all(artifact_dir: Path) -> dict[str, Any]:
    selection_path = artifact_dir / SELECTION_FILE
    pool_manifest_path = artifact_dir / POOL_MANIFEST_FILE
    catalog_manifest_path = artifact_dir / CATALOG_MANIFEST_FILE
    selection = load_json(selection_path)
    pool_manifest = load_json(pool_manifest_path)
    catalog_manifest = load_json(catalog_manifest_path)
    source_report_path = artifact_dir / str(selection["source_report_file"])
    if sha256_path(source_report_path) != selection["source_report_sha256"]:
        raise ValueError("selection source report hash mismatch")
    selected_ids = [str(value) for value in selection["review_ids"]]
    if len(selected_ids) != len(set(selected_ids)):
        raise ValueError("duplicate selected review id")
    pool_rows = {str(row["review_id"]): row for row in pool_manifest["rows"]}
    catalog_rows = {str(row["review_id"]): row for row in catalog_manifest["rows"]}
    missing = sorted(set(selected_ids) - set(pool_rows) | (set(selected_ids) - set(catalog_rows)))
    if missing:
        raise ValueError(f"selected review ids missing source rows: {missing}")

    output_dir = artifact_dir / OUTPUT_DIRECTORY
    output_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    selection_sha256 = sha256_path(selection_path)
    for review_id in selected_ids:
        pool_row = pool_rows[review_id]
        catalog_row = catalog_rows[review_id]
        pool_path = artifact_dir / pool_manifest["pool_directory"] / pool_row["pool_file"]
        catalog_path = (
            artifact_dir / catalog_manifest["catalog_directory"] / catalog_row["catalog_file"]
        )
        source_path = (
            artifact_dir
            / catalog_manifest["source_packet_directory"]
            / catalog_row["source_packet_file"]
        )
        for path, expected in (
            (pool_path, pool_row["pool_sha256"]),
            (catalog_path, catalog_row["catalog_sha256"]),
            (source_path, catalog_row["source_packet_sha256"]),
        ):
            if sha256_path(path) != expected:
                raise ValueError(f"source hash mismatch: {path}")
        packet = build_packet(
            source_packet=load_json(source_path),
            catalog=load_json(catalog_path),
            pool=load_json(pool_path),
            source_packet_sha256=sha256_path(source_path),
            catalog_sha256=sha256_path(catalog_path),
            pool_sha256=sha256_path(pool_path),
            selection_sha256=selection_sha256,
        )
        output_path = output_dir / f"{review_id}.json"
        write_new_or_identical(output_path, canonical_bytes(packet))
        rows.append(
            {
                "review_id": review_id,
                "as_of": packet["as_of"],
                "input_packet_file": output_path.name,
                "input_packet_sha256": sha256_path(output_path),
                "candidate_count": len(packet["candidate_pool"]),
                "retained_daily_bar_count": len(packet["daily_context_to_as_of"]),
                "source_packet_sha256": packet["source_bindings"]["source_packet_sha256"],
                "campaign_catalog_sha256": packet["source_bindings"]["campaign_catalog_sha256"],
                "campaign_pool_sha256": packet["source_bindings"]["campaign_pool_sha256"],
            }
        )
    manifest = {
        "manifest_version": PACKET_VERSION,
        "status": "INPUTS_COMPLETE_NOT_YET_EXECUTED（輸入完成、尚未執行）",
        "milestone": "MILESTONE_2A／ONE_PASS_LEGACY_ALIGNMENT",
        "case_count": len(rows),
        "formal_model": "gpt-5.6-sol",
        "reasoning_effort": "xhigh",
        "required_rounds": 1,
        "selection_file": SELECTION_FILE,
        "selection_sha256": selection_sha256,
        "pool_manifest_file": POOL_MANIFEST_FILE,
        "pool_manifest_sha256": sha256_path(pool_manifest_path),
        "catalog_manifest_file": CATALOG_MANIFEST_FILE,
        "catalog_manifest_sha256": sha256_path(catalog_manifest_path),
        "input_packet_directory": OUTPUT_DIRECTORY,
        "legacy_answer_values_exposed_to_ai": False,
        "future_performance_used": False,
        "identity_used": False,
        "locked_reproduction_set_opened": False,
        "formal_ai_calls": 0,
        "rows": sorted(rows, key=lambda item: item["review_id"]),
    }
    manifest_path = artifact_dir / OUTPUT_MANIFEST
    write_new_or_identical(manifest_path, canonical_bytes(manifest))
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path, default=ARTIFACT_DIR)
    args = parser.parse_args()
    manifest = build_all(args.artifact_dir)
    print(
        json.dumps(
            {
                "status": manifest["status"],
                "case_count": manifest["case_count"],
                "input_packet_directory": manifest["input_packet_directory"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
