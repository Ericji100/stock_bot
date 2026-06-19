from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import ROOT_DIR
from .models import CommandRequest

PROMPT_BUNDLE_SCHEMA_VERSION = "prompt_bundle_v1"
PROMPT_MANIFEST_PATH = ROOT_DIR / "prompt" / "manifest.json"

DEFAULT_COMMAND_PROMPTS: dict[str, dict[str, list[str] | str]] = {
    "research": {
        "base": "prompt/base/base.md",
        "reports": [
            "prompt/report/research_summary.md",
            "prompt/report/research_deep.md",
            "prompt/report/research_score.md",
        ],
        "rules": [
            "prompt/rules/local_scoring_and_ai_final_scoring.md",
            "prompt/rules/discovery_research.md",
            "prompt/rules/rerating_snapshot_rules.md",
            "prompt/rules/risk_and_counter_evidence_rules.md",
            "prompt/rules/source_quality_rules.md",
        ],
        "scoring": [
            "prompt/scoring/financial_hard_metrics.md",
            "prompt/scoring/theme_soft_metrics.md",
            "prompt/scoring/high_growth_gene.md",
            "prompt/scoring/rerating_model.md",
            "prompt/scoring/final_research_score.md",
        ],
    },
    "value_scan": {
        "base": "prompt/base/base.md",
        "reports": ["prompt/report/value_scan.md", "prompt/report/value_scan_deep.md"],
        "rules": [
            "prompt/rules/rerating_snapshot_rules.md",
            "prompt/rules/discovery_value_scan.md",
            "prompt/rules/risk_and_counter_evidence_rules.md",
            "prompt/rules/source_quality_rules.md",
        ],
        "scoring": [
            "prompt/scoring/rerating_model.md",
            "prompt/scoring/theme_soft_metrics.md",
            "prompt/scoring/financial_hard_metrics.md",
        ],
    },
    "macro": {
        "base": "prompt/base/base.md",
        "reports": ["prompt/report/macro.md", "prompt/report/macro_deep.md"],
        "rules": ["prompt/rules/discovery_macro.md", "prompt/rules/source_quality_rules.md"],
        "scoring": [],
    },
    "theme": {
        "base": "prompt/base/base.md",
        "reports": ["prompt/report/theme.md", "prompt/report/theme_deep.md"],
        "rules": ["prompt/rules/discovery_theme.md", "prompt/rules/source_quality_rules.md"],
        "scoring": ["prompt/scoring/theme_soft_metrics.md"],
    },
    "news": {
        "base": "prompt/base/base.md",
        "reports": ["prompt/news/news_summary.md"],
        "rules": ["prompt/rules/source_quality_rules.md"],
        "scoring": [],
    },
}


def build_prompt_manifest(root_dir: Path | None = None) -> dict[str, Any]:
    root = root_dir or ROOT_DIR
    commands = {}
    for command, bundle in DEFAULT_COMMAND_PROMPTS.items():
        commands[command] = _bundle_with_status(root, command, bundle)
    return {
        "schema_version": PROMPT_BUNDLE_SCHEMA_VERSION,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "commands": commands,
    }


def load_prompt_manifest(root_dir: Path | None = None) -> dict[str, Any]:
    root = root_dir or ROOT_DIR
    path = root / "prompt" / "manifest.json"
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            if data.get("schema_version"):
                return _normalize_loaded_manifest(root, data)
        except Exception:
            pass
    return build_prompt_manifest(root)


def prompt_bundle_for_request(request: CommandRequest, root_dir: Path | None = None) -> dict[str, Any]:
    manifest = load_prompt_manifest(root_dir)
    bundle = (manifest.get("commands") or {}).get(request.command) or {}
    return {
        "schema_version": manifest.get("schema_version") or PROMPT_BUNDLE_SCHEMA_VERSION,
        "command": request.command,
        "mode": request.mode,
        "bundle": bundle,
    }


def write_prompt_manifest(root_dir: Path | None = None) -> Path:
    root = root_dir or ROOT_DIR
    path = root / "prompt" / "manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(build_prompt_manifest(root), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _normalize_loaded_manifest(root: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    commands = manifest.get("commands") or {}
    normalized = {
        "schema_version": manifest.get("schema_version") or PROMPT_BUNDLE_SCHEMA_VERSION,
        "prompt_version": manifest.get("prompt_version"),
        "generated_at": manifest.get("generated_at"),
        "commands": {},
    }
    for command, bundle in commands.items():
        if isinstance(bundle, dict):
            normalized["commands"][command] = _bundle_with_status(root, str(command), _prefix_prompt_paths(bundle))
    return normalized


def _prefix_prompt_paths(bundle: dict[str, Any]) -> dict[str, list[str] | str]:
    prefixed: dict[str, list[str] | str] = {}
    for key, value in bundle.items():
        if key in {"bundle_version", "files"}:
            continue
        if isinstance(value, str):
            prefixed[key] = value if value.startswith("prompt/") else f"prompt/{value}"
        elif isinstance(value, list):
            prefixed[key] = [
                item if str(item).startswith("prompt/") else f"prompt/{item}"
                for item in value
            ]
    return prefixed


def _bundle_with_status(root: Path, command: str, bundle: dict[str, list[str] | str]) -> dict[str, Any]:
    files: list[str] = []
    for value in bundle.values():
        if isinstance(value, str):
            files.append(value)
        elif isinstance(value, list):
            files.extend(str(item) for item in value)
    return {
        "bundle_version": f"{command}_prompt_bundle_v1",
        **bundle,
        "files": [
            {
                "path": item,
                "exists": (root / item).exists(),
            }
            for item in files
        ],
    }
