"""Build immutable anonymous AS-OF input packets for Stage B1a candidate R1."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.v2_core_objective_candidate_catalog_v1 import (  # noqa: E402
    ARTIFACT_DIR,
    canonical_bytes,
    sha256_path,
    write_new_or_identical,
)


PACKET_VERSION = "v2-core-stage-b1a-input-r1-candidate"
MANIFEST_VERSION = "v2-core-stage-b1a-input-manifest-r1-candidate"
FEASIBILITY_MANIFEST_FILE = "feasibility_probe_manifest.json"
FOCUS_MANIFEST_FILE = "objective_focus_catalog_manifest_r1.json"
SOURCE_PACKET_DIRECTORY = "feasibility_probe_packets_r2"
SEGMENT_CATALOG_DIRECTORY = "objective_candidate_catalogs_r1"
FOCUS_CATALOG_DIRECTORY = "objective_focus_catalogs_r1"
OUTPUT_DIRECTORY = "stage_b1a_input_packets_candidate_r1"
PROMPT_FILE = "v2_core_stage_b1a.prompt.candidate_r1.md"
OUTPUT_SCHEMA_FILE = "v2_core_stage_b1a.schema.candidate_r1.json"


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def build_stage_b1a_input(
    *,
    source_packet: dict[str, Any],
    source_packet_sha256: str,
    segment_catalog: dict[str, Any],
    segment_catalog_sha256: str,
    focus_catalog: dict[str, Any],
    focus_catalog_sha256: str,
) -> dict[str, Any]:
    review_id = str(source_packet["review_id"])
    as_of = str(source_packet["as_of"])
    if str(segment_catalog["review_id"]) != review_id or str(focus_catalog["review_id"]) != review_id:
        raise ValueError(f"review_id mismatch for {review_id}")
    if str(segment_catalog["as_of"]) != as_of or str(focus_catalog["as_of"]) != as_of:
        raise ValueError(f"as_of mismatch for {review_id}")
    if str(segment_catalog["source_packet_sha256"]) != source_packet_sha256:
        raise ValueError(f"source packet hash mismatch for {review_id}")
    if str(focus_catalog["source_segment_catalog_sha256"]) != segment_catalog_sha256:
        raise ValueError(f"segment catalog hash mismatch for {review_id}")

    segment_index = {
        str(row["candidate_id"]): row
        for row in segment_catalog["objective_segment_candidates"]
    }
    focus_segments: list[dict[str, Any]] = []
    for focus_row in focus_catalog["focus_segments"]:
        candidate_id = str(focus_row["segment_id"])
        if candidate_id not in segment_index:
            raise ValueError(f"unknown focus segment {candidate_id} for {review_id}")
        focus_segments.append(
            {
                **segment_index[candidate_id],
                "objective_focus_reasons": list(focus_row["selection_reasons"]),
            }
        )

    latest_date = str(source_packet["daily_structure_context_to_as_of"][-1]["date"])
    if latest_date != as_of:
        raise ValueError(f"latest visible date {latest_date} != as_of {as_of} for {review_id}")

    return {
        "packet_version": PACKET_VERSION,
        "status": "CANDIDATE_INPUT_NOT_FORMALLY_RUN（候選輸入、尚未正式執行）",
        "task": "Assess every fixed focus segment under Stage B1a only; return schema-valid atomic semantics and role candidates.",
        "review_id": review_id,
        "anonymous_stock_id": str(source_packet["anonymous_stock_id"]),
        "as_of": as_of,
        "source_bindings": {
            "source_packet_sha256": source_packet_sha256,
            "segment_catalog_sha256": segment_catalog_sha256,
            "focus_catalog_sha256": focus_catalog_sha256,
        },
        "review_constraints": {
            "formal_model": "gpt-5.6-sol",
            "reasoning_effort": "xhigh",
            "future_performance_blind": True,
            "identity_blind": True,
            "sealed_labels_blind": True,
            "monitor_start_and_upstream_selection_hidden": True,
            "fixed_segment_ids_only": True,
            "free_dates_and_prices_forbidden": True,
            "scenario_trigger_stop_and_permission_forbidden": True,
        },
        "data_quality": source_packet["data_quality"],
        "daily_structure_context_to_as_of": source_packet["daily_structure_context_to_as_of"],
        "completed_macd_21_55_55_cycles_to_as_of": source_packet[
            "completed_macd_21_55_55_cycles_to_as_of"
        ],
        "confirmed_pivots_to_as_of": source_packet["confirmed_pivots_to_as_of"],
        "proxy_evidence": source_packet["proxy_evidence"],
        "evidence_catalog": source_packet["evidence_catalog"],
        "focus_segments": focus_segments,
    }


def generate_all(artifact_dir: Path = ARTIFACT_DIR) -> dict[str, Any]:
    feasibility_path = artifact_dir / FEASIBILITY_MANIFEST_FILE
    focus_manifest_path = artifact_dir / FOCUS_MANIFEST_FILE
    feasibility = load_json(feasibility_path)
    focus_manifest = load_json(focus_manifest_path)
    focus_rows = {str(row["review_id"]): row for row in focus_manifest["rows"]}

    output_dir = artifact_dir / OUTPUT_DIRECTORY
    output_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    total_segments = 0
    total_context_bars = 0
    for source_row in feasibility["rows"]:
        review_id = str(source_row["review_id"])
        focus_row = focus_rows.get(review_id)
        if focus_row is None:
            raise ValueError(f"missing focus row for {review_id}")

        source_path = artifact_dir / SOURCE_PACKET_DIRECTORY / str(source_row["packet_file"])
        segment_path = artifact_dir / SEGMENT_CATALOG_DIRECTORY / str(
            focus_row["source_segment_catalog_file"]
        )
        focus_path = artifact_dir / FOCUS_CATALOG_DIRECTORY / str(focus_row["focus_catalog_file"])
        source_hash = sha256_path(source_path)
        segment_hash = sha256_path(segment_path)
        focus_hash = sha256_path(focus_path)
        if source_hash != str(source_row["packet_sha256"]):
            raise ValueError(f"manifest packet hash mismatch for {review_id}")
        if segment_hash != str(focus_row["source_segment_catalog_sha256"]):
            raise ValueError(f"manifest segment hash mismatch for {review_id}")
        if focus_hash != str(focus_row["focus_catalog_sha256"]):
            raise ValueError(f"manifest focus hash mismatch for {review_id}")

        packet = build_stage_b1a_input(
            source_packet=load_json(source_path),
            source_packet_sha256=source_hash,
            segment_catalog=load_json(segment_path),
            segment_catalog_sha256=segment_hash,
            focus_catalog=load_json(focus_path),
            focus_catalog_sha256=focus_hash,
        )
        output_path = output_dir / f"{review_id}.json"
        write_new_or_identical(output_path, canonical_bytes(packet))
        packet_hash = sha256_path(output_path)
        focus_count = len(packet["focus_segments"])
        context_bars = len(packet["daily_structure_context_to_as_of"])
        total_segments += focus_count
        total_context_bars += context_bars
        rows.append(
            {
                "review_id": review_id,
                "anonymous_stock_id": packet["anonymous_stock_id"],
                "as_of": packet["as_of"],
                "input_packet_file": output_path.name,
                "input_packet_sha256": packet_hash,
                "focus_segment_count": focus_count,
                "context_bars": context_bars,
                "latest_visible_date": packet["daily_structure_context_to_as_of"][-1]["date"],
                "source_packet_sha256": source_hash,
                "segment_catalog_sha256": segment_hash,
                "focus_catalog_sha256": focus_hash,
            }
        )

    rows.sort(key=lambda row: row["review_id"])
    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "status": "DRAFT_B1A_INPUTS_COMPLETE（B1a 輸入草案完成）",
        "case_count": len(rows),
        "formal_ai_calls": 0,
        "ready_for_formal_ai": False,
        "future_performance_used": False,
        "identity_used": False,
        "sealed_labels_used": False,
        "free_dates_or_prices_added": False,
        "course_judgement_generated_by_program": False,
        "formal_model": "gpt-5.6-sol",
        "reasoning_effort": "xhigh",
        "prompt_file": PROMPT_FILE,
        "prompt_sha256": sha256_path(artifact_dir / PROMPT_FILE),
        "output_schema_file": OUTPUT_SCHEMA_FILE,
        "output_schema_sha256": sha256_path(artifact_dir / OUTPUT_SCHEMA_FILE),
        "source_feasibility_manifest_file": FEASIBILITY_MANIFEST_FILE,
        "source_feasibility_manifest_sha256": sha256_path(feasibility_path),
        "source_focus_manifest_file": FOCUS_MANIFEST_FILE,
        "source_focus_manifest_sha256": sha256_path(focus_manifest_path),
        "input_packet_directory": OUTPUT_DIRECTORY,
        "total_focus_segments": total_segments,
        "average_focus_segments": round(total_segments / len(rows), 6),
        "total_context_bars": total_context_bars,
        "rows": rows,
    }
    manifest_path = artifact_dir / "stage_b1a_input_manifest_candidate_r1.json"
    write_new_or_identical(manifest_path, canonical_bytes(manifest))
    report_lines = [
        "# V2 核心 Stage B1a 候選輸入封包 R1",
        "",
        f"- 狀態：`{manifest['status']}`",
        f"- 匿名 AS-OF 案例：{manifest['case_count']}",
        f"- 固定線段候選：{manifest['total_focus_segments']}（平均 {manifest['average_focus_segments']:.2f}／案）",
        f"- 可見日 K：{manifest['total_context_bars']}",
        f"- 正式 AI 呼叫：{manifest['formal_ai_calls']}",
        f"- 正式模型／推理：`{manifest['formal_model']}／{manifest['reasoning_effort']}`",
        f"- prompt SHA256：`{manifest['prompt_sha256']}`",
        f"- output schema SHA256：`{manifest['output_schema_sha256']}`",
        "",
        "## 護欄",
        "",
        "- 未使用股票身分、sealed 舊答案、未來績效、MFE、MAE 或損益。",
        "- 程式只建立與聚焦客觀候選，沒有產生課程合格判斷。",
        "- AI 後續只能引用既有 `SEG-*`，不得自行新增日期或價格。",
        "- 本批仍為候選輸入，`ready_for_formal_ai=false`；在契約與 smoke gate 完成前不啟動正式三輪。",
        "",
        "## 案例範圍",
        "",
        "| 案例 | AS-OF | 候選數 | 日 K | 最新可見日 |",
        "|---|---:|---:|---:|---:|",
    ]
    report_lines.extend(
        f"| `{row['review_id']}` | {row['as_of']} | {row['focus_segment_count']} | {row['context_bars']} | {row['latest_visible_date']} |"
        for row in rows
    )
    report_path = artifact_dir / "stage_b1a_input_manifest_candidate_r1.md"
    write_new_or_identical(report_path, ("\n".join(report_lines) + "\n").encode("utf-8"))
    return manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path, default=ARTIFACT_DIR)
    return parser.parse_args()


def main() -> int:
    manifest = generate_all(parse_args().artifact_dir)
    print(json.dumps({key: manifest[key] for key in (
        "status", "case_count", "formal_ai_calls", "total_focus_segments",
        "average_focus_segments", "total_context_bars", "ready_for_formal_ai",
    )}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
