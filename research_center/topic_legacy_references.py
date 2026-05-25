from __future__ import annotations

from typing import Any


def _text_list(value: Any) -> list[str]:
    if isinstance(value, list):
        items = value
    elif value:
        items = [value]
    else:
        items = []
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        if isinstance(item, dict):
            for key in ("value", "name", "role", "description"):
                if item.get(key):
                    text = str(item.get(key)).strip()
                    break
            else:
                continue
        else:
            text = str(item).strip()
        if text and text not in seen:
            result.append(text)
            seen.add(text)
    return result


def _append_unique(target: list[str], values: Any) -> None:
    seen = set(target)
    for value in _text_list(values):
        if value not in seen:
            target.append(value)
            seen.add(value)


def build_legacy_theme_references(
    profiles: list[dict[str, Any]] | Any,
    supply_chain_nodes: list[dict[str, Any]] | Any,
) -> dict[str, dict[str, list[str]]]:
    """Build legacy theme_supply_chain-style references from the formal topic library.

    The returned shape intentionally matches the old read-only helper format:
    {
        "題材名稱": {
            "keywords": [...],
            "industries": [...],
            "supply_chain": [...],
            "rerating_labels": [...],
        }
    }
    """
    if not isinstance(profiles, list):
        profiles = []
    if not isinstance(supply_chain_nodes, list):
        supply_chain_nodes = []

    nodes_by_theme: dict[str, list[dict[str, Any]]] = {}
    for node in supply_chain_nodes:
        if not isinstance(node, dict):
            continue
        theme_id = str(node.get("theme_id") or "").strip()
        if theme_id:
            nodes_by_theme.setdefault(theme_id, []).append(node)

    references: dict[str, dict[str, list[str]]] = {}
    for profile in profiles:
        if not isinstance(profile, dict):
            continue
        theme_id = str(profile.get("theme_id") or "").strip()
        theme_name = str(profile.get("theme_name") or theme_id).strip()
        if not theme_name:
            continue

        keywords: list[str] = []
        industries: list[str] = []
        supply_chain: list[str] = []
        rerating_labels: list[str] = []

        _append_unique(keywords, profile.get("keywords") or [])
        _append_unique(keywords, [theme_name, theme_id] if theme_id else [theme_name])
        _append_unique(industries, profile.get("industries") or [])
        _append_unique(supply_chain, profile.get("supply_chain_role") or [])
        _append_unique(rerating_labels, ["新版題材庫", profile.get("confidence") or ""])

        for node in nodes_by_theme.get(theme_id, []):
            _append_unique(supply_chain, node.get("role") or [])
            _append_unique(supply_chain, node.get("product_keywords") or [])
            _append_unique(supply_chain, node.get("upstream") or [])
            _append_unique(supply_chain, node.get("downstream") or [])

        references[theme_name] = {
            "keywords": keywords,
            "industries": industries,
            "supply_chain": supply_chain,
            "rerating_labels": rerating_labels,
        }

    return references
