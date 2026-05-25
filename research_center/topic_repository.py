"""Repository for topic change packs, formal topic library, and audit logs."""
from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from .topic_models import (
    TopicApplyResult,
    TopicAuditEntry,
    TopicChangePack,
    TopicChangeStatus,
    TopicCompanyRelation,
    TopicProfile,
    TopicSupplyChainNode,
)

ROOT = Path(__file__).resolve().parents[1]

# Change pack storage
_CHANGE_PACK_DIR = ROOT / "data" / "topic" / "change_packs"
_AUDIT_DIR = ROOT / "data" / "topic" / "audit_logs"
_PROMPT_LOG_DIR = ROOT / "logs" / "ai_prompts"
_RAW_RESP_DIR = ROOT / "logs" / "topic_ai_raw"
_BACKUP_DIR = ROOT / "data" / "topic" / "backup"

# Formal topic library paths (reuse existing theme profile paths)
_TOPIC_PROFILES_PATH = ROOT / "config" / "theme_profiles.json"
_COMPANY_TOPIC_MAP_PATH = ROOT / "config" / "company_theme_map.json"
_SUPPLY_CHAIN_PATH = ROOT / "config" / "supply_chain_nodes.json"
_COMPANY_KNOWLEDGE_PATH = ROOT / "config" / "company_knowledge.json"


def _ensure_dirs() -> None:
    for d in (_CHANGE_PACK_DIR, _AUDIT_DIR, _PROMPT_LOG_DIR, _RAW_RESP_DIR, _BACKUP_DIR):
        d.mkdir(parents=True, exist_ok=True)


# ── Change Pack I/O ──────────────────────────────────────────────────────────────

def save_change_pack(pack: TopicChangePack) -> str:
    _ensure_dirs()
    if pack.change_id:
        pack.updated_at = datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
    path = _CHANGE_PACK_DIR / f"{pack.change_id}.json"
    path.write_text(json.dumps(pack.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return pack.change_id


def load_change_pack(change_id: str) -> TopicChangePack | None:
    path = _CHANGE_PACK_DIR / f"{change_id}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return TopicChangePack.from_dict(data)
    except Exception:
        return None


def list_change_packs(status: TopicChangeStatus | None = None) -> list[TopicChangePack]:
    _ensure_dirs()
    packs = []
    for path in _CHANGE_PACK_DIR.glob("change_*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            pack = TopicChangePack.from_dict(data)
            if status is None or pack.status == status:
                packs.append(pack)
        except Exception:
            continue
    packs.sort(key=lambda p: p.created_at, reverse=True)
    return packs


def update_change_pack_status(change_id: str, status: TopicChangeStatus) -> bool:
    pack = load_change_pack(change_id)
    if pack is None:
        return False
    pack.status = status
    pack.updated_at = datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
    save_change_pack(pack)
    return True


# ── Audit Log ────────────────────────────────────────────────────────────────────

def write_topic_audit_log(entry: TopicAuditEntry) -> None:
    _ensure_dirs()
    audit_file = _AUDIT_DIR / f"{entry.timestamp[:10]}.json"
    logs = []
    if audit_file.exists():
        try:
            logs = json.loads(audit_file.read_text(encoding="utf-8"))
        except Exception:
            logs = []
    logs.append(entry.to_dict())
    audit_file.write_text(json.dumps(logs, ensure_ascii=False, indent=2), encoding="utf-8")


# ── Backup ────────────────────────────────────────────────────────────────────────

def backup_topic_files(reason: str) -> dict:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_root = _BACKUP_DIR / f"pre_apply_{timestamp}"
    backup_root.mkdir(parents=True, exist_ok=True)

    backed = {}
    files_to_backup = [
        (_TOPIC_PROFILES_PATH, "theme_profiles.json"),
        (_COMPANY_TOPIC_MAP_PATH, "company_theme_map.json"),
        (_SUPPLY_CHAIN_PATH, "supply_chain_nodes.json"),
        (_COMPANY_KNOWLEDGE_PATH, "company_knowledge.json"),
    ]
    for src, name in files_to_backup:
        if src.exists():
            dest = backup_root / name
            shutil.copy2(src, dest)
            backed[name] = str(dest)

    return {
        "backup_root": str(backup_root),
        "backed": backed,
        "timestamp": timestamp,
        "reason": reason,
    }


# ── Formal Topic Library I/O ──────────────────────────────────────────────────

def load_topic_profiles() -> list[TopicProfile]:
    if not _TOPIC_PROFILES_PATH.exists():
        return []
    try:
        data = json.loads(_TOPIC_PROFILES_PATH.read_text(encoding="utf-8"))
        return [TopicProfile.from_dict(item) for item in data]
    except Exception:
        return []


def save_topic_profiles(profiles: list[TopicProfile]) -> None:
    _TOPIC_PROFILES_PATH.parent.mkdir(parents=True, exist_ok=True)
    _TOPIC_PROFILES_PATH.write_text(
        json.dumps([p.to_dict() for p in profiles], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_company_topic_map() -> dict[str, TopicCompanyRelation]:
    if not _COMPANY_TOPIC_MAP_PATH.exists():
        return {}
    try:
        raw = json.loads(_COMPANY_TOPIC_MAP_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            result = {}
            for code, mapping in raw.items():
                if isinstance(mapping, dict):
                    result[code] = TopicCompanyRelation.from_dict({"company_code": code, **mapping})
            return result
        return {}
    except Exception:
        return {}


def save_company_topic_map(data: dict[str, TopicCompanyRelation]) -> None:
    obj = {
        code: {k: v for k, v in m.to_dict().items() if k != "company_code"}
        for code, m in data.items()
    }
    _COMPANY_TOPIC_MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
    _COMPANY_TOPIC_MAP_PATH.write_text(
        json.dumps(obj, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_supply_chain_nodes() -> list[TopicSupplyChainNode]:
    if not _SUPPLY_CHAIN_PATH.exists():
        return []
    try:
        data = json.loads(_SUPPLY_CHAIN_PATH.read_text(encoding="utf-8"))
        return [TopicSupplyChainNode.from_dict(item) for item in data]
    except Exception:
        return []


def save_supply_chain_nodes(nodes: list[TopicSupplyChainNode]) -> None:
    _SUPPLY_CHAIN_PATH.parent.mkdir(parents=True, exist_ok=True)
    _SUPPLY_CHAIN_PATH.write_text(
        json.dumps([n.to_dict() for n in nodes], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_company_knowledge_data() -> dict[str, Any]:
    if not _COMPANY_KNOWLEDGE_PATH.exists():
        return {"metadata": {}, "companies": {}}
    try:
        data = json.loads(_COMPANY_KNOWLEDGE_PATH.read_text(encoding="utf-8-sig"))
    except Exception:
        return {"metadata": {}, "companies": {}}
    if not isinstance(data, dict):
        return {"metadata": {}, "companies": {}}
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    companies = data.get("companies") if isinstance(data.get("companies"), dict) else {}
    normalized = dict(data)
    normalized["metadata"] = metadata
    normalized["companies"] = companies
    return normalized


def save_company_knowledge_data(data: dict[str, Any]) -> None:
    normalized = dict(data or {})
    if not isinstance(normalized.get("metadata"), dict):
        normalized["metadata"] = {}
    if not isinstance(normalized.get("companies"), dict):
        normalized["companies"] = {}
    _COMPANY_KNOWLEDGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _COMPANY_KNOWLEDGE_PATH.write_text(
        json.dumps(normalized, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ── Prompt / Raw Response Paths ─────────────────────────────────────────────────

def prompt_log_path(change_id: str) -> Path:
    _ensure_dirs()
    return _PROMPT_LOG_DIR / f"{change_id}.json"


def raw_response_path(change_id: str) -> Path:
    _ensure_dirs()
    return _RAW_RESP_DIR / f"{change_id}.json"


def is_formal_library_empty() -> bool:
    profiles = load_topic_profiles()
    return len(profiles) == 0
