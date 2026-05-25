"""偏好來源清單工具 — 讀取、分類、加權、domain 判斷。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .config import ROOT_DIR

_CONFIG_PATH = ROOT_DIR / "config" / "preferred_sources.json"


def _normalize_host(url_or_domain: str) -> str:
    """Extract lower-case host from a URL or return the domain itself."""
    stripped = (url_or_domain or "").strip()
    if not stripped:
        return ""
    if "://" in stripped:
        return urlparse(stripped).netloc.lower()
    return stripped.lower()


def load_preferred_sources() -> dict[str, list[dict[str, Any]]]:
    """Load preferred_sources.json; return empty dict on any error."""
    try:
        text = _CONFIG_PATH.read_text(encoding="utf-8")
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def _all_entries() -> list[dict[str, Any]]:
    """Flatten all category lists into a single list of entries."""
    data = load_preferred_sources()
    entries: list[dict[str, Any]] = []
    for cat in ("official", "financial_media", "industry_media", "community"):
        entries.extend(data.get(cat, []))
    return entries


def match_preferred_source(url_or_domain: str) -> dict[str, Any] | None:
    """Match a URL or domain against the preferred source list.

    Supports sub-domain matching: ``www.moneydj.com`` matches ``moneydj.com``.
    """
    host = _normalize_host(url_or_domain)
    if not host:
        return None
    for entry in _all_entries():
        domain = (entry.get("domain") or "").lower()
        if not domain:
            continue
        if host == domain or host.endswith("." + domain):
            return dict(entry)
    return None


def preferred_source_weight(url: str) -> int:
    """Return the preferred-source weight for *url* (0 if not matched)."""
    entry = match_preferred_source(url)
    return int(entry.get("weight", 0)) if entry else 0


def preferred_source_level(url: str) -> str | None:
    """Return the preferred-source level for *url* (None if not matched)."""
    entry = match_preferred_source(url)
    return str(entry.get("level")) if entry else None


def build_site_queries(base_query: str, max_domains: int = 6) -> list[str]:
    """Generate ``site:`` queries for high-quality preferred sources.

    Priority order:
      1. official
      2. financial_media
      3. industry_media

    ``community`` sources are intentionally omitted.
    """
    data = load_preferred_sources()
    domains: list[str] = []
    for cat in ("official", "financial_media", "industry_media"):
        for entry in data.get(cat, []):
            domain = entry.get("domain")
            if domain and domain not in domains:
                domains.append(domain)
            if len(domains) >= max_domains:
                break
        if len(domains) >= max_domains:
            break
    return [f"{base_query} site:{domain}" for domain in domains]
