"""Write the immutable semantic-protocol manifest used by the hybrid V3 run."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "reports/course_backtest/2024-02-02/historical_scan_2023h2_formal_ai_v3_daily_scan/hybrid_monitoring_v1"
OUTPUT = RUN / "protocol_freeze_manifest.json"
FILES = [
    "docs/enlightenment-ai-judgement-v1.md",
    "docs/enlightenment-ai-judgement-v2.md",
    "docs/enlightenment-ai-judgement-v3.md",
    "config/enlightenment_ai_rules_v2.json",
    "config/enlightenment_ai_rules_v3.json",
    "docs/hybrid-monitoring-protocol-v1.md",
    "docs/hybrid-monitoring-lazy-evaluation-v1.md",
    "config/hybrid_monitoring_protocol_v1.json",
    "config/hybrid_common_structure_v1.schema.json",
    "config/hybrid_semantic_prompt_v1.md",
    "scripts/hybrid_v3_policy.py",
    "scripts/hybrid_v3_event_packets.py",
    "scripts/hybrid_v3_actionable_packets.py",
]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def freeze() -> dict:
    files = []
    for relative in FILES:
        path = ROOT / relative
        if not path.exists():
            raise FileNotFoundError(path)
        files.append({"relative_path": relative, "absolute_path": str(path.resolve()), "sha256": _sha(path)})
    event_manifest = RUN / "event_manifest.json"
    boundary_manifest = RUN / "policy_boundary_manifest.json"
    payload = {
        "freeze_version": "hybrid-monitoring-protocol-v1-freeze-20260907",
        "status": "LOCKED_BEFORE_CONSISTENCY_ACCEPTANCE_AND_PERFORMANCE_UNSEAL",
        "model": "gpt-5.6-sol", "reasoning_effort": "xhigh",
        "files": files,
        "event_manifest": {"path": str(event_manifest.resolve()), "sha256": _sha(event_manifest)},
        "policy_boundary_manifest": {"path": str(boundary_manifest.resolve()), "sha256": _sha(boundary_manifest)},
    }
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


if __name__ == "__main__":
    value = freeze()
    print(json.dumps({"freeze_version": value["freeze_version"], "files": len(value["files"]), "model": value["model"], "reasoning_effort": value["reasoning_effort"]}, ensure_ascii=False, indent=2))
