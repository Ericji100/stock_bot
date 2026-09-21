"""Create the immutable execution/pipeline manifest for formal hybrid V3."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "reports/course_backtest/2024-02-02/historical_scan_2023h2_formal_ai_v3_daily_scan/hybrid_monitoring_v1"
OUTPUT = RUN / "execution_freeze_manifest.json"
FILES = [
    "docs/hybrid-v3-execution-freeze-v1.md",
    "config/hybrid_v3_execution_freeze_v1.json",
    "scripts/hybrid_v3_execution_freeze.py",
    "scripts/hybrid_v3_ai_review.py",
    "scripts/hybrid_v3_consistency.py",
    "scripts/hybrid_v3_replay.py",
    "scripts/formal_ai_historical_2023_replay.py",
    "scripts/course_tg_enlightenment_ai_v2_backtest.py",
    "scripts/hybrid_v3_consistency_sequence.ps1",
]
ARTIFACTS = [
    RUN / "protocol_freeze_manifest.json",
    RUN / "anonymous_policy_boundary_packets.jsonl",
    RUN / "policy_boundary_manifest.json",
    RUN / "consistency_sample.jsonl",
    RUN / "reselection_candidates.jsonl",
    RUN.parent / "packet_manifest.json",
    RUN.parent / "daily_scan_index.jsonl",
    RUN.parent / "sealed_identity_map.json",
    RUN.parent.parent / "historical_scan_2023h2_formal_ai_v1_v2" / "input_manifest.json",
]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def freeze() -> dict:
    files = []
    for relative in FILES:
        path = ROOT / relative
        if not path.exists():
            raise FileNotFoundError(path)
        files.append({"relative_path": relative, "sha256": _sha(path)})
    artifacts = []
    for path in ARTIFACTS:
        if not path.exists():
            raise FileNotFoundError(path)
        artifacts.append({"absolute_path": str(path.resolve()), "sha256": _sha(path)})
    payload = {
        "execution_version": "hybrid-v3-execution-freeze-v1",
        "status": "LOCKED_BEFORE_FORMAL_CONSISTENCY_RESTART_AND_PERFORMANCE_UNSEAL",
        "model": "gpt-5.6-sol", "reasoning_effort": "xhigh",
        "files": files, "artifacts": artifacts,
    }
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


if __name__ == "__main__":
    result = freeze()
    print(json.dumps({"execution_version": result["execution_version"], "files": len(result["files"]), "artifacts": len(result["artifacts"])}, ensure_ascii=False, indent=2))
