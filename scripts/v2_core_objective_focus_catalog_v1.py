"""Select a small objective focus set without making course judgements."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.v2_core_objective_candidate_catalog_v1 import (
    ARTIFACT_DIR,
    OUTPUT_DIRECTORY as SEGMENT_DIRECTORY,
    canonical_bytes,
    sha256_path,
    write_new_or_identical,
)
from scripts.v2_core_objective_relation_catalog_v1 import (
    OUTPUT_DIRECTORY as RELATION_DIRECTORY,
)


CATALOG_VERSION = "v2-core-objective-focus-catalog-r1-draft"
MANIFEST_VERSION = "v2-core-objective-focus-manifest-r1-draft"
SCHEMA_FILE = "objective_focus_catalog.schema.candidate_r1.json"
SEGMENT_MANIFEST_FILE = "objective_candidate_catalog_manifest_r1.json"
RELATION_MANIFEST_FILE = "objective_relation_catalog_manifest_r1.json"
OUTPUT_DIRECTORY = "objective_focus_catalogs_r1"


def _magnitude(segment: dict[str, Any]) -> tuple[float, float, str, str]:
    metrics = segment["objective_metrics"]
    return (
        abs(metrics["directional_move_atr_at_start"] or 0.0),
        abs(metrics["directional_move_pct"]),
        str(segment["observed_through"]),
        str(segment["candidate_id"]),
    )


def select_focus_segments(segment_catalog: dict[str, Any]) -> list[dict[str, Any]]:
    shortlist = set(segment_catalog["prompt_shortlist_segment_ids"])
    candidates = [
        row
        for row in segment_catalog["objective_segment_candidates"]
        if row["candidate_id"] in shortlist
    ]
    selected: dict[str, set[str]] = {}

    def add(rows: list[dict[str, Any]], reason: str) -> None:
        for row in rows:
            selected.setdefault(str(row["candidate_id"]), set()).add(reason)

    forming = [row for row in candidates if row["status"] == "FORMING"]
    add(sorted(forming, key=lambda row: (row["start_date"], row["candidate_id"]), reverse=True)[:4], "FORMING_VISIBLE_AS_OF")

    macd = [row for row in candidates if row["basis"].startswith("MACD_")]
    add(sorted(macd, key=lambda row: (row["observed_through"], row["start_date"], row["candidate_id"]), reverse=True)[:4], "RECENT_MACD_PAIR")
    add(sorted(macd, key=_magnitude, reverse=True)[:2], "LARGE_MACD_MOVE")

    large = [row for row in candidates if row["basis"] == "CONFIRMED_PIVOT_LEG" and row["scale"] == "LARGE"]
    add(sorted(large, key=lambda row: (row["observed_through"], row["start_date"], row["candidate_id"]), reverse=True)[:4], "RECENT_LARGE_PIVOT_LEG")
    add(sorted(large, key=_magnitude, reverse=True)[:2], "LARGE_LARGE_PIVOT_MOVE")

    small = [row for row in candidates if row["basis"] == "CONFIRMED_PIVOT_LEG" and row["scale"] == "SMALL"]
    add(sorted(small, key=lambda row: (row["observed_through"], row["start_date"], row["candidate_id"]), reverse=True)[:3], "RECENT_SMALL_PIVOT_LEG")
    add(sorted(small, key=_magnitude, reverse=True)[:1], "LARGE_SMALL_PIVOT_MOVE")

    result = [
        {"segment_id": segment_id, "selection_reasons": sorted(reasons)}
        for segment_id, reasons in selected.items()
    ]
    result.sort(key=lambda row: row["segment_id"])
    if len(result) > 20:
        raise ValueError(f"focus segment contract exceeded: {len(result)}")
    return result


def _relation_segment_ids(relation: dict[str, Any]) -> set[str]:
    if relation["relation_basis"] == "SHARED_BOUNDARY":
        return {str(relation["left_segment_id"]), str(relation["right_segment_id"])}
    return {str(relation["outer_segment_id"]), str(relation["inner_segment_id"])}


def select_focus_relations(
    relation_catalog: dict[str, Any],
    segment_catalog: dict[str, Any],
    focus_segment_ids: set[str],
) -> list[str]:
    segment_map = {
        str(row["candidate_id"]): row
        for row in segment_catalog["objective_segment_candidates"]
    }
    eligible = [
        row
        for row in relation_catalog["objective_relation_candidates"]
        if _relation_segment_ids(row).issubset(focus_segment_ids)
    ]
    shared = [row for row in eligible if row["relation_basis"] == "SHARED_BOUNDARY"]
    contains = [row for row in eligible if row["relation_basis"] == "INTERVAL_CONTAINS"]
    shared = sorted(
        shared,
        key=lambda row: (row["shared_boundary_date"], row["candidate_id"]),
        reverse=True,
    )[:10]
    contains = sorted(
        contains,
        key=lambda row: (
            segment_map[str(row["inner_segment_id"])]["observed_through"],
            segment_map[str(row["inner_segment_id"])]["start_date"],
            row["candidate_id"],
        ),
        reverse=True,
    )[:6]
    return [str(row["candidate_id"]) for row in shared + contains]


def select_focus_stops(
    segment_catalog: dict[str, Any], focus_segment_ids: set[str]
) -> list[str]:
    segment_map = {
        str(row["candidate_id"]): row
        for row in segment_catalog["objective_segment_candidates"]
    }
    stops = list(segment_catalog["objective_stop_candidates"])
    relevant_pivot_refs = {
        ref
        for segment_id in focus_segment_ids
        for ref in segment_map[segment_id]["source_evidence_refs"]
        if str(ref).startswith("PIVOT:")
    }
    selected: dict[str, dict[str, Any]] = {}
    relevant = [row for row in stops if row["source_pivot_ref"] in relevant_pivot_refs]
    for row in sorted(relevant, key=lambda item: (item["confirmed_on"], item["source_date"], item["candidate_id"]), reverse=True)[:8]:
        selected[str(row["candidate_id"])] = row
    for scale in ("LARGE", "SMALL"):
        for side in ("LOW", "HIGH"):
            rows = [row for row in stops if row["scale"] == scale and row["side"] == side]
            if rows:
                row = max(rows, key=lambda item: (item["confirmed_on"], item["source_date"], item["candidate_id"]))
                selected[str(row["candidate_id"])] = row
    ordered = sorted(
        selected.values(),
        key=lambda row: (row["confirmed_on"], row["source_date"], row["candidate_id"]),
        reverse=True,
    )
    if len(ordered) > 12:
        raise ValueError(f"focus stop contract exceeded: {len(ordered)}")
    return [str(row["candidate_id"]) for row in ordered]


def build_focus_catalog(
    segment_catalog: dict[str, Any],
    segment_hash: str,
    relation_catalog: dict[str, Any],
    relation_hash: str,
) -> dict[str, Any]:
    focus_segments = select_focus_segments(segment_catalog)
    focus_segment_ids = {row["segment_id"] for row in focus_segments}
    return {
        "catalog_version": CATALOG_VERSION,
        "status": "OBJECTIVE_FOCUS_ONLY（僅客觀聚焦、非課程判讀）",
        "review_id": str(segment_catalog["review_id"]),
        "as_of": str(segment_catalog["as_of"]),
        "source_segment_catalog_sha256": segment_hash,
        "source_relation_catalog_sha256": relation_hash,
        "generator_contract": {
            "candidate_only": True,
            "ai_course_judgement_included": False,
            "future_performance_used": False,
            "sealed_labels_used": False,
            "identity_used": False,
            "selection_uses_only_objective_recency_scale_and_magnitude": True,
        },
        "focus_segments": focus_segments,
        "focus_relation_ids": select_focus_relations(
            relation_catalog, segment_catalog, focus_segment_ids
        ),
        "focus_trigger_ids": [
            str(row["candidate_id"])
            for row in segment_catalog["objective_trigger_candidates"]
        ],
        "focus_stop_ids": select_focus_stops(segment_catalog, focus_segment_ids),
    }


def validate_focus_catalog(
    focus: dict[str, Any],
    segment_catalog: dict[str, Any],
    relation_catalog: dict[str, Any],
    schema: dict[str, Any],
) -> None:
    errors = sorted(
        Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER).iter_errors(focus),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        detail = "; ".join(f"{'/'.join(map(str, error.absolute_path))}: {error.message}" for error in errors[:10])
        raise ValueError(f"focus catalog schema invalid: {detail}")
    segment_ids = {
        str(row["candidate_id"])
        for row in segment_catalog["objective_segment_candidates"]
    }
    relation_ids = {
        str(row["candidate_id"])
        for row in relation_catalog["objective_relation_candidates"]
    }
    trigger_ids = {
        str(row["candidate_id"])
        for row in segment_catalog["objective_trigger_candidates"]
    }
    stop_ids = {
        str(row["candidate_id"])
        for row in segment_catalog["objective_stop_candidates"]
    }
    focus_segment_ids = {str(row["segment_id"]) for row in focus["focus_segments"]}
    if not focus_segment_ids.issubset(segment_ids):
        raise ValueError("focus references missing segment")
    if not set(focus["focus_relation_ids"]).issubset(relation_ids):
        raise ValueError("focus references missing relation")
    if not set(focus["focus_trigger_ids"]).issubset(trigger_ids):
        raise ValueError("focus references missing trigger")
    if not set(focus["focus_stop_ids"]).issubset(stop_ids):
        raise ValueError("focus references missing stop")
    relation_map = {
        str(row["candidate_id"]): row
        for row in relation_catalog["objective_relation_candidates"]
    }
    for relation_id in focus["focus_relation_ids"]:
        if not _relation_segment_ids(relation_map[relation_id]).issubset(focus_segment_ids):
            raise ValueError("focus relation references non-focus segment")


def build_all(artifact_dir: Path) -> dict[str, Any]:
    segment_manifest_path = artifact_dir / SEGMENT_MANIFEST_FILE
    relation_manifest_path = artifact_dir / RELATION_MANIFEST_FILE
    segment_manifest = json.loads(segment_manifest_path.read_text(encoding="utf-8"))
    relation_manifest = json.loads(relation_manifest_path.read_text(encoding="utf-8"))
    schema_path = artifact_dir / SCHEMA_FILE
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    relation_rows = {str(row["review_id"]): row for row in relation_manifest["rows"]}
    rows = []
    for segment_row in segment_manifest["rows"]:
        review_id = str(segment_row["review_id"])
        relation_row = relation_rows[review_id]
        segment_path = artifact_dir / SEGMENT_DIRECTORY / str(segment_row["catalog_file"])
        relation_path = artifact_dir / RELATION_DIRECTORY / str(relation_row["relation_catalog_file"])
        segment_hash = sha256_path(segment_path)
        relation_hash = sha256_path(relation_path)
        if segment_hash != str(segment_row["catalog_sha256"]):
            raise RuntimeError(f"segment catalog hash mismatch: {segment_path}")
        if relation_hash != str(relation_row["relation_catalog_sha256"]):
            raise RuntimeError(f"relation catalog hash mismatch: {relation_path}")
        segment_catalog = json.loads(segment_path.read_text(encoding="utf-8"))
        relation_catalog = json.loads(relation_path.read_text(encoding="utf-8"))
        focus = build_focus_catalog(
            segment_catalog, segment_hash, relation_catalog, relation_hash
        )
        validate_focus_catalog(focus, segment_catalog, relation_catalog, schema)
        output_path = artifact_dir / OUTPUT_DIRECTORY / f"{review_id}.json"
        write_new_or_identical(output_path, canonical_bytes(focus))
        rows.append(
            {
                "review_id": review_id,
                "as_of": focus["as_of"],
                "source_segment_catalog_file": segment_path.name,
                "source_segment_catalog_sha256": segment_hash,
                "source_relation_catalog_file": relation_path.name,
                "source_relation_catalog_sha256": relation_hash,
                "focus_catalog_file": output_path.name,
                "focus_catalog_sha256": sha256_path(output_path),
                "focus_segment_count": len(focus["focus_segments"]),
                "focus_relation_count": len(focus["focus_relation_ids"]),
                "focus_trigger_count": len(focus["focus_trigger_ids"]),
                "focus_stop_count": len(focus["focus_stop_ids"]),
            }
        )
    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "status": "DRAFT_OBJECTIVE_FOCUS_COMPLETE（客觀聚焦草案完成）",
        "source_segment_manifest_file": segment_manifest_path.name,
        "source_segment_manifest_sha256": sha256_path(segment_manifest_path),
        "source_relation_manifest_file": relation_manifest_path.name,
        "source_relation_manifest_sha256": sha256_path(relation_manifest_path),
        "segment_catalog_directory": SEGMENT_DIRECTORY,
        "relation_catalog_directory": RELATION_DIRECTORY,
        "focus_catalog_directory": OUTPUT_DIRECTORY,
        "focus_schema_file": SCHEMA_FILE,
        "focus_schema_sha256": sha256_path(schema_path),
        "case_count": len(rows),
        "formal_ai_calls": 0,
        "future_performance_used": False,
        "sealed_labels_used": False,
        "identity_used": False,
        "course_judgement_generated_by_program": False,
        "ready_for_formal_ai": False,
        "rows": rows,
    }
    write_new_or_identical(
        artifact_dir / "objective_focus_catalog_manifest_r1.json",
        canonical_bytes(manifest),
    )
    return manifest


def markdown(manifest: dict[str, Any]) -> str:
    rows = manifest["rows"]
    total = lambda key: sum(int(row[key]) for row in rows)
    return "\n".join(
        [
            "# V2核心客觀AI聚焦目錄草案 R1",
            "",
            f"- 狀態：`{manifest['status']}`",
            f"- 匿名AS-OF案例：{manifest['case_count']}",
            "- 正式AI呼叫：0",
            "- 聚焦只依客觀最近性、片段尺度、形成中狀態與幅度；未使用課程答案或未來績效。",
            "",
            "## 聚焦後輸入量",
            "",
            "| 項目 | 總數 | 每案平均 |",
            "|---|---:|---:|",
            f"| 片段 | {total('focus_segment_count')} | {total('focus_segment_count') / len(rows):.2f} |",
            f"| 關係 | {total('focus_relation_count')} | {total('focus_relation_count') / len(rows):.2f} |",
            f"| 當日客觀事件 | {total('focus_trigger_count')} | {total('focus_trigger_count') / len(rows):.2f} |",
            f"| 防線候選 | {total('focus_stop_count')} | {total('focus_stop_count') / len(rows):.2f} |",
            "",
            "## 限制",
            "",
            "- 聚焦入選不代表PASS、定錨、觸發或因果防線合格。",
            "- 下一步仍須建立AI只能引用固定ID的B1／B2契約及程式tie-break。",
            "- 在新契約與smoke test通過前，本目錄不得送正式AI或用於交易。",
            "",
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-dir", type=Path, default=ARTIFACT_DIR)
    args = parser.parse_args()
    artifact_dir = args.artifact_dir.resolve()
    manifest = build_all(artifact_dir)
    write_new_or_identical(
        artifact_dir / "objective_focus_catalog_manifest_r1.md",
        markdown(manifest).encode("utf-8"),
    )
    print(
        json.dumps(
            {
                "status": manifest["status"],
                "case_count": manifest["case_count"],
                "formal_ai_calls": 0,
                "manifest": str((artifact_dir / "objective_focus_catalog_manifest_r1.json").resolve()),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
