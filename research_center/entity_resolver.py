from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .config import ROOT_DIR

ENTITY_RESOLVER_SCHEMA_VERSION = "entity_resolver_v1"


@dataclass(frozen=True)
class ResolvedEntity:
    query: str
    code: str | None = None
    name: str | None = None
    symbol: str | None = None
    market: str | None = None
    industry: str | None = None
    sector: dict[str, Any] | None = None
    themes: list[str] = field(default_factory=list)
    primary_theme: str | None = None
    supply_chain_nodes: list[dict[str, Any]] = field(default_factory=list)
    supply_chain_summary: dict[str, Any] = field(default_factory=dict)
    aliases: list[str] = field(default_factory=list)
    schema_version: str = ENTITY_RESOLVER_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def resolve_entity(query: str | int | None, *, root_dir: Path | None = None) -> ResolvedEntity:
    root = root_dir or ROOT_DIR
    raw = "" if query is None else str(query).strip()
    code = normalize_stock_code(raw)
    stocks = _load_stock_universe(root)
    by_code = {str(item.get("code") or ""): item for item in stocks}
    by_name = {str(item.get("name") or "").strip(): item for item in stocks if item.get("name")}
    stock = by_code.get(code or "") or by_name.get(raw)
    if not stock and raw:
        stock = _find_stock_by_symbol(raw, stocks)
    final_code = str(stock.get("code")) if stock else code
    theme_map = _load_json(root / "config" / "company_theme_map.json", {})
    theme_info = theme_map.get(final_code or "") if isinstance(theme_map, dict) else {}
    nodes = resolve_supply_chain_nodes(company_code=final_code, root_dir=root)
    industry = str(stock.get("industry")) if stock and stock.get("industry") else None
    sector = resolve_sector_alias(industry, root_dir=root) if industry else None
    return ResolvedEntity(
        query=raw,
        code=final_code,
        name=str(stock.get("name")) if stock and stock.get("name") else None,
        symbol=str(stock.get("symbol")) if stock and stock.get("symbol") else format_tw_symbol(final_code, str(stock.get("market") or "")) if final_code else None,
        market=str(stock.get("market")) if stock and stock.get("market") else infer_market_from_symbol(str(stock.get("symbol") or "")) if stock else None,
        industry=industry,
        sector=sector,
        themes=list(theme_info.get("themes") or []) if isinstance(theme_info, dict) else [],
        primary_theme=str(theme_info.get("primary_theme")) if isinstance(theme_info, dict) and theme_info.get("primary_theme") else None,
        supply_chain_nodes=nodes,
        supply_chain_summary=summarize_supply_chain_nodes(nodes),
        aliases=_aliases_for_stock(stock, theme_info, sector),
    )


def normalize_stock_code(value: str | int | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip().upper()
    match = re.search(r"(\d{4})", text)
    return match.group(1) if match else None


def format_tw_symbol(code: str | None, market: str | None = None) -> str | None:
    if not code:
        return None
    market_text = (market or "").upper()
    suffix = "TWO" if market_text in {"TPEX", "OTC", "上櫃"} else "TW"
    return f"{code}.{suffix}"


def infer_market_from_symbol(symbol: str | None) -> str | None:
    text = (symbol or "").upper()
    if text.endswith(".TWO"):
        return "TPEX"
    if text.endswith(".TW"):
        return "TWSE"
    return None


def resolve_topic_alias(topic: str, *, root_dir: Path | None = None) -> dict[str, Any]:
    root = root_dir or ROOT_DIR
    query = str(topic or "").strip()
    profiles = _load_json(root / "config" / "theme_profiles.json", [])
    if isinstance(profiles, dict):
        profiles = profiles.get("themes") or profiles.get("profiles") or []
    for profile in profiles if isinstance(profiles, list) else []:
        aliases = [profile.get("theme_id"), profile.get("theme_name"), profile.get("theme"), *(profile.get("aliases") or [])]
        if query and any(str(item).lower() == query.lower() for item in aliases if item):
            return {
                "schema_version": ENTITY_RESOLVER_SCHEMA_VERSION,
                "query": query,
                "canonical": profile.get("theme_name") or profile.get("theme") or profile.get("theme_id"),
                "theme_id": profile.get("theme_id"),
                "theme_name": profile.get("theme_name"),
                "aliases": [item for item in aliases if item],
                "profile": profile,
            }
    return {
        "schema_version": ENTITY_RESOLVER_SCHEMA_VERSION,
        "query": query,
        "canonical": query or None,
        "theme_id": query or None,
        "aliases": [query] if query else [],
    }


def resolve_sector_alias(industry: str | None, *, root_dir: Path | None = None) -> dict[str, Any]:
    root = root_dir or ROOT_DIR
    query = str(industry or "").strip()
    data = _load_json(root / "config" / "sector_alias_map.json", {})
    sectors = data.get("sectors") if isinstance(data, dict) else {}
    redirects = data.get("alias_redirects") if isinstance(data, dict) else {}
    sectors = sectors if isinstance(sectors, dict) else {}
    redirects = redirects if isinstance(redirects, dict) else {}
    canonical = str(redirects.get(query) or query)
    profile = sectors.get(canonical)
    if not isinstance(profile, dict):
        for sector, candidate in sectors.items():
            aliases = [sector, *(candidate.get("aliases") or [])] if isinstance(candidate, dict) else [sector]
            if query and any(_alias_match(query, str(alias)) for alias in aliases if alias):
                canonical = str(sector)
                profile = candidate if isinstance(candidate, dict) else {}
                break
    if not isinstance(profile, dict):
        profile = {}
    aliases = [canonical, profile.get("display_name"), *(profile.get("aliases") or [])]
    return {
        "schema_version": ENTITY_RESOLVER_SCHEMA_VERSION,
        "query": query,
        "canonical": canonical or None,
        "display_name": profile.get("display_name") or canonical or None,
        "aliases": _unique_texts(aliases),
        "rerating_label": profile.get("rerating_label"),
        "rerating_bonus": profile.get("rerating_bonus"),
        "subsector_count": len(profile.get("subsectors") or []) if isinstance(profile.get("subsectors"), list) else 0,
        "profile": profile,
    }


def resolve_supply_chain_nodes(
    *,
    company_code: str | None = None,
    theme_id: str | None = None,
    root_dir: Path | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    root = root_dir or ROOT_DIR
    nodes = _load_json(root / "config" / "supply_chain_nodes.json", [])
    if not isinstance(nodes, list):
        return []
    result: list[dict[str, Any]] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        if company_code and str(node.get("company_code") or "") != str(company_code):
            continue
        if theme_id and str(node.get("theme_id") or "") != str(theme_id):
            continue
        result.append(node)
    return result[: max(0, limit)]


def summarize_supply_chain_nodes(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    layers = _unique_texts([node.get("layer") for node in nodes if isinstance(node, dict)])
    themes = _unique_texts([node.get("theme_id") for node in nodes if isinstance(node, dict)])
    roles = _unique_texts([
        node.get("role") or node.get("supply_chain_role")
        for node in nodes
        if isinstance(node, dict)
    ])
    return {
        "schema_version": ENTITY_RESOLVER_SCHEMA_VERSION,
        "node_count": len(nodes),
        "theme_ids": themes,
        "layers": layers,
        "roles": roles,
    }


def _load_stock_universe(root: Path) -> list[dict[str, Any]]:
    data = _load_json(root / "stock_list.json", {})
    if isinstance(data, dict):
        stocks = data.get("stocks") or data.get("data") or []
    else:
        stocks = data
    return [item for item in stocks if isinstance(item, dict)]


def _find_stock_by_symbol(query: str, stocks: list[dict[str, Any]]) -> dict[str, Any] | None:
    upper = query.upper()
    for stock in stocks:
        if str(stock.get("symbol") or "").upper() == upper:
            return stock
    return None


def _aliases_for_stock(stock: dict[str, Any] | None, theme_info: Any, sector: dict[str, Any] | None = None) -> list[str]:
    values: list[str] = []
    if stock:
        values.extend([stock.get("code"), stock.get("symbol"), stock.get("name"), stock.get("industry")])
    if isinstance(theme_info, dict):
        values.extend(theme_info.get("themes") or [])
        values.append(theme_info.get("primary_theme"))
    if isinstance(sector, dict):
        values.extend(sector.get("aliases") or [])
        values.append(sector.get("canonical"))
        values.append(sector.get("display_name"))
    return _unique_texts(values)


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return default


def _alias_match(query: str, alias: str) -> bool:
    left = query.strip().lower()
    right = alias.strip().lower()
    return bool(left and right and (left == right or left in right or right in left))


def _unique_texts(values: list[Any]) -> list[str]:
    seen = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            result.append(text)
            seen.add(text)
    return result
