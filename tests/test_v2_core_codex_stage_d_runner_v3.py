from __future__ import annotations

import json
from pathlib import Path
import subprocess

from scripts import v2_core_codex_staged_runner_v1 as base
from scripts.v2_core_codex_stage_d_runner_v3 import (
    RUNNER_VERSION,
    run_stage_d_case,
    validate_existing_stage_d_artifacts,
)


OUT = base.OUT
PROBE = json.loads((OUT / "feasibility_probe_manifest.json").read_text(encoding="utf-8"))
REVIEW_ID = "FP-004ab6003b74044860aa6f03"
PACKET_ROW = next(row for row in PROBE["rows"] if row["review_id"] == REVIEW_ID)
PACKET_PATH = OUT / PROBE["packet_directory"] / PACKET_ROW["packet_file"]
V7_ROOT = OUT / "feasibility_probe_runs_r7" / "round_1"
STAGE_B_PATH = V7_ROOT / "stage_b" / f"{REVIEW_ID}.json"
SEMANTIC_OUTPUT = json.loads(
    (V7_ROOT / "stage_d" / f"{REVIEW_ID}.raw.json").read_text(encoding="utf-8")
)
STAGE_B_SCHEMA = OUT / "v2_core_stage_b.schema.candidate.json"
STAGE_B_PROMPT = OUT / "v2_core_stage_b.prompt.candidate_r2.md"
STAGE_D_SCHEMA = OUT / "v2_core_stage_d.schema.candidate.json"
STAGE_D_PROMPT = OUT / "v2_core_stage_d.prompt.candidate_r3.md"
TRUTH_TABLE = OUT / "permission_truth_table.json"


def _fake_codex(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
    output_index = command.index("--output-last-message") + 1
    Path(command[output_index]).write_text(
        json.dumps(SEMANTIC_OUTPUT, ensure_ascii=False),
        encoding="utf-8",
    )
    return subprocess.CompletedProcess(command, 0, stdout="", stderr="")


def test_full_fake_transport_persists_and_revalidates_final_receipt(tmp_path: Path) -> None:
    output = tmp_path / "stage_d" / f"{REVIEW_ID}.json"
    route = tmp_path / "program_route" / f"{REVIEW_ID}.json"
    permission = tmp_path / "final_permission" / f"{REVIEW_ID}.json"
    receipt = tmp_path / "receipts" / f"{REVIEW_ID}.stage_d.json"
    kwargs = {
        "packet_path": PACKET_PATH,
        "stage_b_path": STAGE_B_PATH,
        "stage_b_schema_path": STAGE_B_SCHEMA,
        "stage_b_prompt_path": STAGE_B_PROMPT,
        "schema_path": STAGE_D_SCHEMA,
        "prompt_path": STAGE_D_PROMPT,
        "truth_table_path": TRUTH_TABLE,
        "output_path": output,
        "route_path": route,
        "permission_path": permission,
        "receipt_path": receipt,
    }
    result = run_stage_d_case(
        **kwargs,
        timeout_seconds=30,
        run_command=_fake_codex,
    )
    assert result["status"] == "COMPLETED"
    saved_receipt = json.loads(receipt.read_text(encoding="utf-8"))
    assert saved_receipt["runner_version"] == RUNNER_VERSION
    assert saved_receipt["normalized_output_sha256"] == base.canonical_sha256(
        json.loads(output.read_text(encoding="utf-8"))
    )
    resumed = validate_existing_stage_d_artifacts(**kwargs)
    assert resumed["status"] == "RESUMED_VALID_EXISTING"
