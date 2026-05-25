"""Local cache helpers for external topic/industry sources."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import ROOT_DIR

CONFIG_DIR = ROOT_DIR / "config"
TPEX_CACHE_PATH = CONFIG_DIR / "tpex_industry_chain.json"
UDN_CACHE_PATH = CONFIG_DIR / "udn_industry_topics.json"


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def empty_tpex_cache() -> dict[str, Any]:
    return {
        "source": "tpex_industry_chain",
        "updated_at": None,
        "items": [],
        "metadata": {},
    }


def empty_udn_cache() -> dict[str, Any]:
    return {
        "source": "udn_industry_topics",
        "updated_at": None,
        "industries": [],
        "topics": [],
        "metadata": {},
    }


def load_json_cache(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return dict(default)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return dict(default)
    return data if isinstance(data, dict) else dict(default)


def save_json_cache(path: Path, data: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(data)
    payload["updated_at"] = payload.get("updated_at") or _now_iso()
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_tpex_industry_chain() -> dict[str, Any]:
    return load_json_cache(TPEX_CACHE_PATH, empty_tpex_cache())


def save_tpex_industry_chain(data: dict[str, Any]) -> Path:
    return save_json_cache(TPEX_CACHE_PATH, data)


def load_udn_industry_topics() -> dict[str, Any]:
    return load_json_cache(UDN_CACHE_PATH, empty_udn_cache())


def save_udn_industry_topics(data: dict[str, Any]) -> Path:
    return save_json_cache(UDN_CACHE_PATH, data)


def load_topic_source_caches() -> dict[str, Any]:
    """Load all external topic source caches.

    Missing or invalid caches are returned as empty structures so callers can
    inject the context into prompts without failing the main flow.
    """
    return {
        "tpex_industry_chain": load_tpex_industry_chain(),
        "udn_industry_topics": load_udn_industry_topics(),
    }
