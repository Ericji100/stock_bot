"""Build deterministic objective relations between shortlisted M2A segments."""

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


CATALOG_VERSION = "v2-core-objective-relation-catalog-r1-draft"
MANIFEST_VERSION = "v2-core-objective-relation-manifest-r1-draft"
SCHEMA_FILE = "objective_relation_catalog.schema.candidate_r1.json"
SEGMENT_MANIFEST_FILE = "objective_candidate_catalog_manifest_r1.json"
OUTPUT_DIRECTORY = "objective_relation_catalogs_r1"


def stable_id(*parts: Any) -> str:
    payload = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "RELC-" + hashlib.sha256(
        f"{CATALOG_VERSION}|{payload}".encode("utf-8")
    ).hexdigest()[:20]


def direction_pattern(left: dict[str, Any], right: dict[str, Any]) -> str:
    return f"{left['direction']}_THEN_{right['direction']}"


def build_relations(segment_catalog: dict[str, Any], segment_catalog_sha256: str) -> dict[str, Any]:
    all_segments = {
        str(row["candidate_id"]): row
        for row in segment_catalog["objective_segment_candidates"]
    }
    shortlist_ids = list(segment_catalog["prompt_shortlist_segment_ids"])
    segments = [all_segments[candidate_id] for candidate_id in shortlist_ids]
    relations: list[dict[str, Any]] = []

    for left in segments:
        boundary = left["confirmed_end_date"]
        if boundary is None:
            continue
        for right in segments:
            if left["candidate_id"] == right["candidate_id"] or boundary != right["start_date"]:
                continue
            if left["start_date"] >= right["observed_through"]:
                continue
            identity = (
                "SHARED_BOUNDARY",
                left["candidate_id"],
                right["candidate_id"],
                boundary,
            )
            relations.append(
                {
                    "candidate_id": stable_id(*identity),
                    "relation_basis": "SHARED_BOUNDARY",
                    "left_segment_id": left["candidate_id"],
                    "right_segment_id": right["candidate_id"],
                    "direction_pattern": direction_pattern(left, right),
                    "shared_boundary_date": boundary,
                    "outer_segment_id": None,
                    "inner_segment_id": None,
                    "not_course_relation_judgement": True,
                }
            )

    outer_segments = [row for row in segments if row["scale"] in {"LARGE", "AUXILIARY"}]
    inner_segments = [row for row in segments if row["scale"] == "SMALL"]
    for outer in outer_segments:
        for inner in inner_segments:
            if not (
                outer["start_date"] <= inner["start_date"]
                and inner["observed_through"] <= outer["observed_through"]
            ):
                continue
            if (
                outer["start_date"] == inner["start_date"]
                and outer["observed_through"] == inner["observed_through"]
            ):
                continue
            identity = (
                "INTERVAL_CONTAINS",
                outer["candidate_id"],
                inner["candidate_id"],
            )
            relations.append(
                {
                    "candidate_id": stable_id(*identity),
                    "relation_basis": "INTERVAL_CONTAINS",
                    "left_segment_id": None,
                    "right_segment_id": None,
                    "direction_pattern": "NOT_APPLICABLE",
                    "shared_boundary_date": None,
                    "outer_segment_id": outer["candidate_id"],
                    "inner_segment_id": inner["candidate_id"],
                    "not_course_relation_judgement": True,
                }
            )

    unique = {row["candidate_id"]: row for row in relations}
    ordered = sorted(
        unique.values(),
        key=lambda row: (
            row["relation_basis"],
            row["shared_boundary_date"] or "",
            row["candidate_id"],
        ),
    )
    shared = [row for row in ordered if row["relation_basis"] == "SHARED_BOUNDARY"]
    contains = [row for row in ordered if row["relation_basis"] == "INTERVAL_CONTAINS"]
    shared_shortlist = sorted(
        shared,
        key=lambda row: (row["shared_boundary_date"], row["candidate_id"]),
        reverse=True,
    )[:20]
    contains_shortlist = sorted(
        contains,
        key=lambda row: (
            all_segments[row["inner_segment_id"]]["observed_through"],
            all_segments[row["inner_segment_id"]]["start_date"],
            row["candidate_id"],
        ),
        reverse=True,
    )[:10]
    catalog = {
        "catalog_version": CATALOG_VERSION,
        "status": "OBJECTIVE_RELATIONS_ONLY（僅客觀關係、非課程判讀）",
        "review_id": str(segment_catalog["review_id"]),
        "as_of": str(segment_catalog["as_of"]),
        "source_segment_catalog_sha256": segment_catalog_sha256,
        "generator_contract": {
            "candidate_only": True,
            "ai_course_relation_judgement_included": False,
            "future_performance_used": False,
            "sealed_labels_used": False,
            "identity_used": False,
            "source_segment_catalog_unchanged": True,
        },
        "objective_relation_candidates": ordered,
        "prompt_shortlist_relation_ids": [
            row["candidate_id"] for row in shared_shortlist + contains_shortlist
        ],
    }
    return catalog


def validate_relation_catalog(
    catalog: dict[str, Any],
    segment_catalog: dict[str, Any],
    schema: dict[str, Any],
) -> None:
    errors = sorted(
        Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER).iter_errors(catalog),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        detail = "; ".join(f"{'/'.join(map(str, error.absolute_path))}: {error.message}" for error in errors[:10])
        raise ValueError(f"relation catalog schema invalid: {detail}")
    segment_ids = {
        str(row["candidate_id"])
        for row in segment_catalog["objective_segment_candidates"]
    }
    shortlist_segment_ids = set(segment_catalog["prompt_shortlist_segment_ids"])
    relation_ids = [str(row["candidate_id"]) for row in catalog["objective_relation_candidates"]]
    if len(relation_ids) != len(set(relation_ids)):
        raise ValueError("relation candidate IDs are not unique")
    if not set(catalog["prompt_shortlist_relation_ids"]).issubset(relation_ids):
        raise ValueError("relation shortlist references missing candidate")
    as_of = str(catalog["as_of"])
    for row in catalog["objective_relation_candidates"]:
        if row["relation_basis"] == "SHARED_BOUNDARY":
            if None in (row["left_segment_id"], row["right_segment_id"], row["shared_boundary_date"]):
                raise ValueError("shared-boundary relation lacks required fields")
            if row["outer_segment_id"] is not None or row["inner_segment_id"] is not None:
                raise ValueError("shared-boundary relation contains interval fields")
            if row["shared_boundary_date"] > as_of:
                raise ValueError("future boundary date in relation")
            referenced = {row["left_segment_id"], row["right_segment_id"]}
        else:
            if None in (row["outer_segment_id"], row["inner_segment_id"]):
                raise ValueError("containment relation lacks required fields")
            if row["left_segment_id"] is not None or row["right_segment_id"] is not None or row["shared_boundary_date"] is not None:
                raise ValueError("containment relation contains boundary fields")
            referenced = {row["outer_segment_id"], row["inner_segment_id"]}
        if not referenced.issubset(segment_ids) or not referenced.issubset(shortlist_segment_ids):
            raise ValueError("relation references a non-shortlisted segment")


def build_all(artifact_dir: Path) -> dict[str, Any]:
    segment_manifest_path = artifact_dir / SEGMENT_MANIFEST_FILE
    segment_manifest = json.loads(segment_manifest_path.read_text(encoding="utf-8"))
    schema_path = artifact_dir / SCHEMA_FILE
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    rows = []
    for item in segment_manifest["rows"]:
        segment_path = artifact_dir / SEGMENT_DIRECTORY / str(item["catalog_file"])
        before = sha256_path(segment_path)
        if before != str(item["catalog_sha256"]):
            raise RuntimeError(f"segment catalog hash mismatch: {segment_path}")
        segment_catalog = json.loads(segment_path.read_text(encoding="utf-8"))
        relation_catalog = build_relations(segment_catalog, before)
        validate_relation_catalog(relation_catalog, segment_catalog, schema)
        output_path = artifact_dir / OUTPUT_DIRECTORY / f"{relation_catalog['review_id']}.json"
        write_new_or_identical(output_path, canonical_bytes(relation_catalog))
        if sha256_path(segment_path) != before:
            raise RuntimeError(f"segment catalog changed during relation build: {segment_path}")
        basis_counts = {
            basis: sum(
                row["relation_basis"] == basis
                for row in relation_catalog["objective_relation_candidates"]
            )
            for basis in ("SHARED_BOUNDARY", "INTERVAL_CONTAINS")
        }
        rows.append(
            {
                "review_id": relation_catalog["review_id"],
                "as_of": relation_catalog["as_of"],
                "source_segment_catalog_file": segment_path.name,
                "source_segment_catalog_sha256": before,
                "relation_catalog_file": output_path.name,
                "relation_catalog_sha256": sha256_path(output_path),
                "relation_candidate_count": len(relation_catalog["objective_relation_candidates"]),
                "relation_shortlist_count": len(relation_catalog["prompt_shortlist_relation_ids"]),
                "basis_counts": basis_counts,
            }
        )
    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "status": "DRAFT_OBJECTIVE_RELATIONS_COMPLETE（客觀關係草案完成）",
        "source_segment_manifest_file": segment_manifest_path.name,
        "source_segment_manifest_sha256": sha256_path(segment_manifest_path),
        "source_segment_catalog_directory": SEGMENT_DIRECTORY,
        "relation_catalog_directory": OUTPUT_DIRECTORY,
        "relation_schema_file": SCHEMA_FILE,
        "relation_schema_sha256": sha256_path(schema_path),
        "case_count": len(rows),
        "formal_ai_calls": 0,
        "future_performance_used": False,
        "sealed_labels_used": False,
        "identity_used": False,
        "course_relation_judgement_generated_by_program": False,
        "ready_for_formal_ai": False,
        "rows": rows,
    }
    write_new_or_identical(
        artifact_dir / "objective_relation_catalog_manifest_r1.json",
        canonical_bytes(manifest),
    )
    return manifest


def markdown(manifest: dict[str, Any]) -> str:
    rows = manifest["rows"]
    shared = sum(int(row["basis_counts"]["SHARED_BOUNDARY"]) for row in rows)
    contains = sum(int(row["basis_counts"]["INTERVAL_CONTAINS"]) for row in rows)
    return "\n".join(
        [
            "# V2核心客觀關係目錄草案 R1",
            "",
            f"- 狀態：`{manifest['status']}`",
            f"- 匿名AS-OF案例：{manifest['case_count']}",
            "- 正式AI呼叫：0",
            "- 未讀取股票身分、sealed標籤或未來績效。",
            "- 程式只描述片段共用邊界與時間區間包含關係；沒有判定父代、修正、複製、太極或左右階段。",
            "",
            "## 數量",
            "",
            "| 客觀關係 | 數量 |",
            "|---|---:|",
            f"| 共用轉折邊界 | {shared} |",
            f"| 大／輔助區間包含小級片段 | {contains} |",
            f"| Prompt短名單 | {sum(int(row['relation_shortlist_count']) for row in rows)} |",
            "",
            "## 限制",
            "",
            "- 關係候選只從片段prompt短名單建立，不從完整片段目錄做組合爆炸。",
            "- `UP_THEN_DOWN`不自動等於父代修正；`DOWN_THEN_UP`不自動等於複製或左右翻多。",
            "- `INTERVAL_CONTAINS`只表示日期區間包含，不證明控制尺度或太極世代。",
            "- AI引用契約、課程tie-break與小型smoke test尚未完成，因此不得送正式AI或用於交易。",
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
        artifact_dir / "objective_relation_catalog_manifest_r1.md",
        markdown(manifest).encode("utf-8"),
    )
    print(
        json.dumps(
            {
                "status": manifest["status"],
                "case_count": manifest["case_count"],
                "formal_ai_calls": 0,
                "manifest": str((artifact_dir / "objective_relation_catalog_manifest_r1.json").resolve()),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
