"""Topic library reset service - backup and clear formal topic library."""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


# Paths must match those in topic_repository.py
ROOT = Path(__file__).resolve().parents[1]
_TOPIC_PROFILES_PATH = ROOT / "config" / "theme_profiles.json"
_COMPANY_TOPIC_MAP_PATH = ROOT / "config" / "company_theme_map.json"
_SUPPLY_CHAIN_PATH = ROOT / "config" / "supply_chain_nodes.json"
_RESET_BACKUP_DIR = ROOT / "backups" / "topic_reset"


@dataclass(frozen=True)
class TopicResetResult:
    success: bool
    backup_path: str | None
    cleared_files: list[str]
    error: str | None = None


def reset_topic_library(confirm: bool) -> TopicResetResult:
    """Reset formal topic library: backup first, then clear.

    Args:
        confirm: Must be True to proceed. If False, refuses to act.

    Returns:
        TopicResetResult with success status, backup path, and cleared files.
    """
    if not confirm:
        return TopicResetResult(
            success=False,
            backup_path=None,
            cleared_files=[],
            error="需要 --confirm 確認才能執行重置。請輸入 /topic_reset --confirm",
        )

    # Create backup
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_root = _RESET_BACKUP_DIR / timestamp
    backup_root.mkdir(parents=True, exist_ok=True)

    files_to_backup = [
        (_TOPIC_PROFILES_PATH, "theme_profiles.json"),
        (_COMPANY_TOPIC_MAP_PATH, "company_theme_map.json"),
        (_SUPPLY_CHAIN_PATH, "supply_chain_nodes.json"),
    ]

    backed_files: list[str] = []
    for src, name in files_to_backup:
        if src.exists():
            dest = backup_root / name
            shutil.copy2(src, dest)
            backed_files.append(name)

    # Clear formal library
    cleared_files: list[str] = []
    for src, name in files_to_backup:
        if src.exists():
            src.write_text("[]" if "profiles" in name or "nodes" in name else "{}", encoding="utf-8")
            cleared_files.append(name)

    return TopicResetResult(
        success=True,
        backup_path=str(backup_root),
        cleared_files=cleared_files,
        error=None,
    )