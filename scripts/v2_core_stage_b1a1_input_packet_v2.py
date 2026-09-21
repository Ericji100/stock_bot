"""Add fixed objective relation context to Stage B1a1 candidate R2 packets."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.v2_core_objective_candidate_catalog_v1 import (
    ARTIFACT_DIR,
    canonical_bytes,
    sha256_path,
    write_new_or_identical,
)


PACKET_VERSION = "v2-core-stage-b1a1-input-r2-candidate"
MANIFEST_VERSION = "v2-core-stage-b1a1-input-manifest-r2-candidate"
SOURCE_INPUT_MANIFEST_FILE = "stage_b1a_input_manifest_candidate_r1.json"
SOURCE_INPUT_DIRECTORY = "stage_b1a_input_packets_candidate_r1"
RELATION_MANIFEST_FILE = "objective_relation_catalog_manifest_r1.json"
RELATION_CATALOG_DIRECTORY = "objective_relation_catalogs_r1"
FOCUS_MANIFEST_FILE = "objective_focus_catalog_manifest_r1.json"
FOCUS_CATALOG_DIRECTORY = "objective_focus_catalogs_r1"
OUTPUT_DIRECTORY = "stage_b1a1_input_packets_candidate_r2"
PROMPT_FILE = "v2_core_stage_b1a1.prompt.candidate_r2.md"
OUTPUT_SCHEMA_FILE = "v2_core_stage_b1a1.schema.candidate_r2.json"


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def build_stage_b1a1_input(
    *,
    source_input: dict[str, Any],
    source_input_sha256: str,
    relation_catalog: dict[str, Any],
    relation_catalog_sha256: str,
    focus_catalog: dict[str, Any],
    focus_catalog_sha256: str,
) -> dict[str, Any]:
    review_id = str(source_input["review_id"])
    as_of = str(source_input["as_of"])
    if relation_catalog.get("review_id") != review_id or focus_catalog.get("review_id") != review_id:
        raise ValueError(f"review_id mismatch for {review_id}")
    if relation_catalog.get("as_of") != as_of or focus_catalog.get("as_of") != as_of:
        raise ValueError(f"as_of mismatch for {review_id}")
    if focus_catalog.get("source_relation_catalog_sha256") != relation_catalog_sha256:
        raise ValueError(f"relation catalog hash mismatch for {review_id}")
    if source_input["source_bindings"]["focus_catalog_sha256"] != focus_catalog_sha256:
        raise ValueError(f"focus catalog hash mismatch for {review_id}")

    relation_index = {
        str(row["candidate_id"]): row
        for row in relation_catalog["objective_relation_candidates"]
    }
    focus_relations: list[dict[str, Any]] = []
    for relation_id in focus_catalog["focus_relation_ids"]:
        if relation_id not in relation_index:
            raise ValueError(f"unknown focus relation {relation_id} for {review_id}")
        focus_relations.append(relation_index[relation_id])

    focus_segment_ids = {
        str(row["candidate_id"]) for row in source_input["focus_segments"]
    }
    for relation in focus_relations:
        related = {
            value
            for key, value in relation.items()
            if key.endswith("_segment_id") and value is not None
        }
        if not related.issubset(focus_segment_ids):
            raise ValueError(
                f"focus relation {relation['candidate_id']} references non-focus segment"
            )

    existing_refs = {str(row["ref"]) for row in source_input["evidence_catalog"]}
    relation_evidence = []
    for index, relation in enumerate(focus_relations):
        relation_id = str(relation["candidate_id"])
        if relation_id in existing_refs:
            raise ValueError(f"duplicate evidence ref {relation_id}")
        relation_evidence.append(
            {
                "ref": relation_id,
                "kind": "RELATION",
                "path": f"/focus_relations/{index}",
            }
        )

    return {
        **source_input,
        "packet_version": PACKET_VERSION,
        "status": "CANDIDATE_INPUT_NOT_FORMALLY_RUN（候選輸入、尚未正式執行）",
        "task": "Assess every fixed focus segment under Stage B1a1 eligibility only; do not assign roles.",
        "source_bindings": {
            **source_input["source_bindings"],
            "source_b1a_input_sha256": source_input_sha256,
            "relation_catalog_sha256": relation_catalog_sha256,
        },
        "review_constraints": {
            **source_input["review_constraints"],
            "fixed_relation_ids_only": True,
            "role_assignment_forbidden": True,
        },
        "evidence_catalog": [
            *source_input["evidence_catalog"],
            *relation_evidence,
        ],
        "focus_relations": focus_relations,
    }


def generate_all(artifact_dir: Path = ARTIFACT_DIR) -> dict[str, Any]:
    source_manifest_path = artifact_dir / SOURCE_INPUT_MANIFEST_FILE
    relation_manifest_path = artifact_dir / RELATION_MANIFEST_FILE
    focus_manifest_path = artifact_dir / FOCUS_MANIFEST_FILE
    source_manifest = load_json(source_manifest_path)
    relation_manifest = load_json(relation_manifest_path)
    focus_manifest = load_json(focus_manifest_path)
    relation_rows = {str(row["review_id"]): row for row in relation_manifest["rows"]}
    focus_rows = {str(row["review_id"]): row for row in focus_manifest["rows"]}

    output_dir = artifact_dir / OUTPUT_DIRECTORY
    output_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    total_segments = 0
    total_relations = 0
    total_context_bars = 0
    for source_row in source_manifest["rows"]:
        review_id = str(source_row["review_id"])
        relation_row = relation_rows[review_id]
        focus_row = focus_rows[review_id]
        source_path = artifact_dir / SOURCE_INPUT_DIRECTORY / source_row["input_packet_file"]
        relation_path = (
            artifact_dir
            / RELATION_CATALOG_DIRECTORY
            / relation_row["relation_catalog_file"]
        )
        focus_path = artifact_dir / FOCUS_CATALOG_DIRECTORY / focus_row["focus_catalog_file"]
        source_hash = sha256_path(source_path)
        relation_hash = sha256_path(relation_path)
        focus_hash = sha256_path(focus_path)
        if source_hash != source_row["input_packet_sha256"]:
            raise ValueError(f"source input hash mismatch for {review_id}")
        if relation_hash != relation_row["relation_catalog_sha256"]:
            raise ValueError(f"relation manifest hash mismatch for {review_id}")
        if focus_hash != focus_row["focus_catalog_sha256"]:
            raise ValueError(f"focus manifest hash mismatch for {review_id}")

        packet = build_stage_b1a1_input(
            source_input=load_json(source_path),
            source_input_sha256=source_hash,
            relation_catalog=load_json(relation_path),
            relation_catalog_sha256=relation_hash,
            focus_catalog=load_json(focus_path),
            focus_catalog_sha256=focus_hash,
        )
        output_path = output_dir / f"{review_id}.json"
        write_new_or_identical(output_path, canonical_bytes(packet))
        segment_count = len(packet["focus_segments"])
        relation_count = len(packet["focus_relations"])
        context_bars = len(packet["daily_structure_context_to_as_of"])
        total_segments += segment_count
        total_relations += relation_count
        total_context_bars += context_bars
        rows.append(
            {
                "review_id": review_id,
                "anonymous_stock_id": packet["anonymous_stock_id"],
                "as_of": packet["as_of"],
                "input_packet_file": output_path.name,
                "input_packet_sha256": sha256_path(output_path),
                "focus_segment_count": segment_count,
                "focus_relation_count": relation_count,
                "context_bars": context_bars,
                "latest_visible_date": packet["daily_structure_context_to_as_of"][-1]["date"],
                "source_b1a_input_sha256": source_hash,
                "relation_catalog_sha256": relation_hash,
                "focus_catalog_sha256": focus_hash,
            }
        )

    rows.sort(key=lambda row: row["review_id"])
    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "status": "DRAFT_B1A1_R2_INPUTS_COMPLETE（B1a1 R2輸入草案完成）",
        "case_count": len(rows),
        "formal_ai_calls": 0,
        "ready_for_formal_ai": False,
        "future_performance_used": False,
        "identity_used": False,
        "sealed_labels_used": False,
        "course_judgement_generated_by_program": False,
        "formal_model": "gpt-5.6-sol",
        "reasoning_effort": "xhigh",
        "prompt_file": PROMPT_FILE,
        "prompt_sha256": sha256_path(artifact_dir / PROMPT_FILE),
        "output_schema_file": OUTPUT_SCHEMA_FILE,
        "output_schema_sha256": sha256_path(artifact_dir / OUTPUT_SCHEMA_FILE),
        "source_input_manifest_file": SOURCE_INPUT_MANIFEST_FILE,
        "source_input_manifest_sha256": sha256_path(source_manifest_path),
        "source_relation_manifest_file": RELATION_MANIFEST_FILE,
        "source_relation_manifest_sha256": sha256_path(relation_manifest_path),
        "source_focus_manifest_file": FOCUS_MANIFEST_FILE,
        "source_focus_manifest_sha256": sha256_path(focus_manifest_path),
        "input_packet_directory": OUTPUT_DIRECTORY,
        "total_focus_segments": total_segments,
        "total_focus_relations": total_relations,
        "average_focus_segments": round(total_segments / len(rows), 6),
        "average_focus_relations": round(total_relations / len(rows), 6),
        "total_context_bars": total_context_bars,
        "rows": rows,
    }
    manifest_path = artifact_dir / "stage_b1a1_input_manifest_candidate_r2.json"
    write_new_or_identical(manifest_path, canonical_bytes(manifest))
    report_lines = [
        "# V2 核心 Stage B1a1 候選輸入封包 R2",
        "",
        f"- 狀態：`{manifest['status']}`",
        f"- 匿名AS-OF案例：{manifest['case_count']}",
        f"- 固定線段：{manifest['total_focus_segments']}（平均 {manifest['average_focus_segments']:.2f}／案）",
        f"- 固定關係：{manifest['total_focus_relations']}（平均 {manifest['average_focus_relations']:.2f}／案）",
        f"- 可見日K：{manifest['total_context_bars']}",
        "- 正式AI呼叫：0",
        "- 未使用股票身分、sealed舊答案或未來績效；程式未產生課程判讀。",
        "- 本批仍為候選輸入，需通過prompt／schema／validator／runner契約後才能建立Smoke執行清單。",
    ]
    write_new_or_identical(
        artifact_dir / "stage_b1a1_input_manifest_candidate_r2.md",
        ("\n".join(report_lines) + "\n").encode("utf-8"),
    )
    return manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path, default=ARTIFACT_DIR)
    return parser.parse_args()


def main() -> int:
    manifest = generate_all(parse_args().artifact_dir)
    print(
        json.dumps(
            {
                key: manifest[key]
                for key in (
                    "status",
                    "case_count",
                    "formal_ai_calls",
                    "total_focus_segments",
                    "total_focus_relations",
                    "ready_for_formal_ai",
                )
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
