"""Repository for theme profiles, drafts, and audit logs."""
from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from .theme_models import (
    CompanyThemeMapping,
    DynamicThemeCacheEntry,
    SupplyChainNode,
    ThemeAuditAction,
    ThemeAuditEntry,
    ThemeDraft,
    ThemeDraftStatus,
    ThemeProfile,
)

ROOT = Path(__file__).resolve().parents[1]
THEME_PROFILES_PATH = ROOT / "config" / "theme_profiles.json"
SUPPLY_CHAIN_PATH = ROOT / "config" / "supply_chain_nodes.json"
COMPANY_THEME_MAP_PATH = ROOT / "config" / "company_theme_map.json"
DYNAMIC_CACHE_PATH = ROOT / "data" / "theme" / "dynamic_theme_cache.json"
DRAFT_DIR = ROOT / "data" / "theme" / "drafts"
AUDIT_DIR = ROOT / "data" / "theme" / "audit"


def _ensure_dirs() -> None:
    DRAFT_DIR.mkdir(parents=True, exist_ok=True)
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)


# ── Profile I/O ────────────────────────────────────────────────────────────────

def load_theme_profiles() -> list[ThemeProfile]:
    if not THEME_PROFILES_PATH.exists():
        return []
    try:
        data = json.loads(THEME_PROFILES_PATH.read_text(encoding="utf-8"))
        return [ThemeProfile.from_dict(item) for item in data]
    except Exception:
        return []


def save_theme_profiles(profiles: list[ThemeProfile]) -> None:
    THEME_PROFILES_PATH.parent.mkdir(parents=True, exist_ok=True)
    THEME_PROFILES_PATH.write_text(
        json.dumps([p.to_dict() for p in profiles], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_supply_chain_nodes() -> list[SupplyChainNode]:
    if not SUPPLY_CHAIN_PATH.exists():
        return []
    try:
        data = json.loads(SUPPLY_CHAIN_PATH.read_text(encoding="utf-8"))
        return [_supply_chain_node_from_dict(item) for item in data]
    except Exception:
        return []


def _supply_chain_node_from_dict(data: dict[str, Any]) -> SupplyChainNode:
    return SupplyChainNode(
        node_id=data.get("node_id", ""),
        company_code=data.get("company_code", ""),
        company_name=data.get("company_name", ""),
        role=data.get("role", ""),
        upstream=data.get("upstream", []),
        downstream=data.get("downstream", []),
        product_keywords=data.get("product_keywords", []),
    )


def load_company_theme_map() -> list[CompanyThemeMapping]:
    if not COMPANY_THEME_MAP_PATH.exists():
        return []
    try:
        raw = json.loads(COMPANY_THEME_MAP_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            result = []
            for code, mapping in raw.items():
                result.append(CompanyThemeMapping(
                    company_code=code,
                    company_name=mapping.get("company_name", ""),
                    themes=mapping.get("themes", []),
                    primary_theme=mapping.get("primary_theme", ""),
                    evidence=mapping.get("evidence", []),
                ))
            return result
        return []
    except Exception:
        return []


def save_company_theme_map(mappings: list[CompanyThemeMapping]) -> None:
    obj = {m.company_code: {
        "company_name": m.company_name,
        "themes": m.themes,
        "primary_theme": m.primary_theme,
        "evidence": m.evidence,
    } for m in mappings}
    COMPANY_THEME_MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
    COMPANY_THEME_MAP_PATH.write_text(
        json.dumps(obj, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ── Draft I/O ─────────────────────────────────────────────────────────────────

def create_draft(draft: ThemeDraft) -> str:
    """Save a new draft and return its draft_id."""
    _ensure_dirs()
    draft.status = ThemeDraftStatus.PENDING
    path = DRAFT_DIR / f"{draft.draft_id}.json"
    path.write_text(json.dumps(draft.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return draft.draft_id


def get_draft(draft_id: str) -> ThemeDraft | None:
    path = DRAFT_DIR / f"{draft_id}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return ThemeDraft.from_dict(data)
    except Exception:
        return None


def update_draft(draft: ThemeDraft) -> bool:
    path = DRAFT_DIR / f"{draft.draft_id}.json"
    if not path.exists():
        return False
    path.write_text(json.dumps(draft.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return True


def list_drafts(status: ThemeDraftStatus | None = None) -> list[ThemeDraft]:
    _ensure_dirs()
    drafts = []
    for path in DRAFT_DIR.glob("draft_*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            draft = ThemeDraft.from_dict(data)
            if status is None or draft.status == status:
                drafts.append(draft)
        except Exception:
            continue
    drafts.sort(key=lambda d: d.created_at, reverse=True)
    return drafts


def delete_draft(draft_id: str) -> bool:
    path = (DRAFT_DIR / f"{draft_id}.json").resolve()
    if not path.exists():
        return False
    if not path.is_file():
        return False
    for attempt in range(3):
        try:
            path.chmod(0o666)
        except OSError:
            pass
        try:
            path.unlink()
            return True
        except PermissionError:
            if attempt < 2:
                import time
                time.sleep(0.1 * (attempt + 1))
                continue
            # Third attempt failed — check if file actually gone
            if not path.exists():
                return True
            raise PermissionError(f"Cannot delete draft file after 3 attempts: {path}")


# ── Backup & Write ─────────────────────────────────────────────────────────────

def backup_and_write(profiles: list[ThemeProfile], backup_suffix: str | None = None) -> bool:
    """Backup existing profiles before writing."""
    if not THEME_PROFILES_PATH.exists():
        pass
    else:
        suffix = backup_suffix or datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
        backup_path = ROOT / "config" / f"theme_profiles.bak_{suffix}.json"
        shutil.copy2(THEME_PROFILES_PATH, backup_path)
    save_theme_profiles(profiles)
    return True


# ── Dynamic Cache I/O ──────────────────────────────────────────────────────────

def load_dynamic_cache() -> list[DynamicThemeCacheEntry]:
    if not DYNAMIC_CACHE_PATH.exists():
        return []
    try:
        data = json.loads(DYNAMIC_CACHE_PATH.read_text(encoding="utf-8"))
        return [_cache_entry_from_dict(item) for item in data.get("drafts", [])]
    except Exception:
        return []


def _cache_entry_from_dict(data: dict[str, Any]) -> DynamicThemeCacheEntry:
    return DynamicThemeCacheEntry(
        theme_name=data.get("theme_name", ""),
        keywords=data.get("keywords", []),
        industries=data.get("industries", []),
        supply_chain_role=data.get("supply_chain_role", ""),
        matched_companies=data.get("matched_companies", []),
        evidence_list=data.get("evidence_list", []),
        theme_relation_score=data.get("theme_relation_score", 0.0),
        confidence=data.get("confidence", "medium"),
        relation_type=data.get("relation_type", "unclear"),
        cache_time=data.get("cache_time", ""),
    )


def save_dynamic_cache(entries: list[DynamicThemeCacheEntry]) -> None:
    DYNAMIC_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    DYNAMIC_CACHE_PATH.write_text(
        json.dumps({"drafts": [e.__dict__ for e in entries], "profiles": []}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ── Audit Log I/O ─────────────────────────────────────────────────────────────

def write_audit_log(entry: ThemeAuditEntry) -> None:
    _ensure_dirs()
    audit_file = AUDIT_DIR / f"{entry.timestamp[:10]}.json"
    logs = []
    if audit_file.exists():
        try:
            logs = json.loads(audit_file.read_text(encoding="utf-8"))
        except Exception:
            logs = []
    logs.append(entry.to_dict())
    audit_file.write_text(json.dumps(logs, ensure_ascii=False, indent=2), encoding="utf-8")


def list_audit_logs(date: str | None = None) -> list[ThemeAuditEntry]:
    _ensure_dirs()
    if date:
        audit_file = AUDIT_DIR / f"{date}.json"
        if not audit_file.exists():
            return []
        try:
            logs = json.loads(audit_file.read_text(encoding="utf-8"))
        except Exception:
            return []
    else:
        logs = []
        for f in sorted(AUDIT_DIR.glob("*.json"), reverse=True):
            try:
                logs.extend(json.loads(f.read_text(encoding="utf-8")))
            except Exception:
                continue
    return [ThemeAuditEntry.from_dict(item) for item in logs]


# ── Profile helpers ─────────────────────────────────────────────────────────────

def find_theme_profile(theme_id: str) -> ThemeProfile | None:
    """Find a theme profile by theme_id, return None if not found."""
    profiles = load_theme_profiles()
    for p in profiles:
        if p.theme_id == theme_id:
            return p
    return None


def upsert_theme_profile(profile: ThemeProfile) -> None:
    """Insert or update a theme profile in the formal library."""
    profiles = load_theme_profiles()
    profiles = [p for p in profiles if p.theme_id != profile.theme_id]
    profiles.append(profile)
    save_theme_profiles(profiles)


def backup_theme_files(reason: str) -> dict:
    """Backup theme profile files before making changes.

    Returns a dict with backup paths and timestamp info.
    """
    import shutil
    from datetime import datetime

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_root = ROOT / "data" / "theme" / "backup" / f"pre_merge_{timestamp}"
    backup_root.mkdir(parents=True, exist_ok=True)

    backed = {}
    files_to_backup = [
        (THEME_PROFILES_PATH, "theme_profiles.json"),
        (SUPPLY_CHAIN_PATH, "supply_chain_nodes.json"),
        (COMPANY_THEME_MAP_PATH, "company_theme_map.json"),
    ]
    for path, name in files_to_backup:
        if path.exists():
            dest = backup_root / name
            shutil.copy2(path, dest)
            backed[name] = str(dest)

    backed["reason"] = reason
    backed["timestamp"] = timestamp
    backed["backup_root"] = str(backup_root)
    return backed