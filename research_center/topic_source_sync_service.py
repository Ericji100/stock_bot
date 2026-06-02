"""Sync external industry/topic source indexes into local caches."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import hashlib
from typing import Callable
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests.exceptions import SSLError

from .topic_source_cache import (
    TPEX_CACHE_PATH,
    UDN_CACHE_PATH,
    save_tpex_industry_chain,
    save_udn_industry_topics,
)
from .topic_models import TopicCompanyRelation, TopicProfile, TopicSupplyChainNode
from .topic_repository import (
    load_company_topic_map,
    load_supply_chain_nodes,
    load_topic_profiles,
    save_company_topic_map,
    save_supply_chain_nodes,
    save_topic_profiles,
)

TPEX_URL = "https://ic.tpex.org.tw/"
UDN_INDUSTRY_URL = "https://money.udn.com/industry/index"

HtmlFetcher = Callable[[str], str]

_MOJIBAKE_MARKERS = ("ç", "æ", "å", "è", "é", "ä", "¶", "\x80", "\x81", "\x82", "\x83", "\x84")

_TPEX_CODE_NAME_MAP = {
    "D000": "半導體",
    "C100": "製藥",
    "C200": "醫療器材",
    "C300": "食品生技",
    "C400": "再生醫療",
    "5100": "區塊鏈",
    "5200": "金融科技",
    "5300": "人工智慧",
    "5400": "雲端運算",
    "5500": "資通訊安全",
    "5600": "大數據",
    "5700": "體驗科技",
    "5800": "運動科技",
    "4100": "太空衛星科技",
    "6000": "自動化",
    "R300": "電子商務",
    "J000": "被動元件",
    "I000": "通信網路",
    "K000": "連接器",
    "F000": "電腦週邊",
    "G000": "平面顯示器",
    "H000": "觸控面板",
    "L000": "印刷電路板",
    "B000": "休閒娛樂",
    "1000": "水泥",
    "M000": "食品",
    "N000": "石化及塑橡膠",
    "O000": "紡織",
    "P000": "電機機械",
    "2000": "造紙",
    "Q000": "鋼鐵",
    "3000": "汽車",
    "R000": "軟體服務",
    "S000": "建材營造",
    "T000": "交通運輸及航運",
    "U000": "金融",
    "V000": "貿易百貨",
    "W000": "油電燃氣",
    "Y000": "文化創意",
    "X000": "其他",
}

_TPEX_NON_TOPIC_NAMES = {
    "產業價值鏈資訊平台 企業籌資更便捷 大眾投資更穩當",
    "使用條款",
    "隱私權保護說明",
    "網站地圖",
    "其他",
}


@dataclass
class TopicSourceSyncResult:
    success: bool
    synced_sources: list[str] = field(default_factory=list)
    failed_sources: dict[str, str] = field(default_factory=dict)
    tpex_items: int = 0
    udn_industries: int = 0
    udn_topics: int = 0
    tpex_cache_path: str = str(TPEX_CACHE_PATH)
    udn_cache_path: str = str(UDN_CACHE_PATH)
    formal_profiles_created: int = 0
    formal_profiles_updated: int = 0
    formal_company_relations_updated: int = 0
    formal_supply_chain_nodes_updated: int = 0


def _default_fetcher(url: str) -> str:
    return _requests_get_text(url, verify=True)


def _requests_get_text(url: str, *, verify: bool) -> str:
    response = requests.get(
        url,
        timeout=30,
        verify=verify,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36"
            )
        },
    )
    response.raise_for_status()
    if url.startswith(TPEX_URL) or not response.encoding or response.encoding.lower() == "iso-8859-1":
        response.encoding = "utf-8"
    else:
        response.encoding = response.encoding or "utf-8"
    return response.text


def _fetch_tpex_html(fetcher: HtmlFetcher | None) -> tuple[str, dict]:
    """Fetch TPEx HTML.

    TPEx occasionally presents a certificate chain that fails Python/OpenSSL
    verification with "Missing Subject Key Identifier". Keep normal SSL
    verification first; only retry this known public TPEx source with
    verify=False when the first request fails specifically at SSL validation.
    """
    if fetcher is not None:
        return fetcher(TPEX_URL), {"ssl_verify": True, "ssl_fallback": False}
    try:
        return _requests_get_text(TPEX_URL, verify=True), {"ssl_verify": True, "ssl_fallback": False}
    except SSLError as exc:
        html = _requests_get_text(TPEX_URL, verify=False)
        return html, {
            "ssl_verify": False,
            "ssl_fallback": True,
            "ssl_fallback_reason": str(exc),
        }


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _normalize_spaces(text: str) -> str:
    return " ".join((text or "").split())


def _repair_mojibake_text(text: str) -> str:
    normalized = _normalize_spaces(str(text or ""))
    if not normalized or not any(marker in normalized for marker in _MOJIBAKE_MARKERS):
        return normalized
    try:
        repaired = normalized.encode("latin1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return normalized
    return _normalize_spaces(repaired)


def _clean_text(text: str) -> str:
    return _repair_mojibake_text(text)


def _tpex_code_from_url(url: str) -> str:
    parsed = urlparse(str(url or ""))
    return (parse_qs(parsed.query).get("ic") or [""])[0]


def _clean_tpex_name(name: str, href_or_url: str = "") -> str:
    code = _tpex_code_from_url(href_or_url)
    if code in _TPEX_CODE_NAME_MAP:
        return _TPEX_CODE_NAME_MAP[code]
    cleaned = _clean_text(name)
    if "è\x83½æº\x90" in str(name or ""):
        return "綠色能源"
    return cleaned


def _is_valid_tpex_topic_name(name: str, href_or_url: str = "") -> bool:
    cleaned = _clean_tpex_name(name, href_or_url)
    url = str(href_or_url or "")
    if not cleaned or cleaned in _TPEX_NON_TOPIC_NAMES:
        return False
    if any(part in url for part in ("disclaimer", "privacy_rights", "sitemap", "index.php")):
        return False
    return True


def _clean_tpex_item(item: dict) -> dict | None:
    source_url = str(item.get("source_url") or TPEX_URL)
    name = _clean_tpex_name(str(item.get("name") or item.get("industry") or ""), source_url)
    industry = _clean_tpex_name(str(item.get("industry") or name), source_url)
    if not _is_valid_tpex_topic_name(industry or name, source_url):
        return None
    cleaned = dict(item)
    cleaned["name"] = name or industry
    cleaned["industry"] = industry or name
    cleaned["chain_stage"] = _clean_text(str(item.get("chain_stage") or ""))
    cleaned["role"] = _clean_text(str(item.get("role") or ""))
    cleaned["company_code"] = _clean_text(str(item.get("company_code") or ""))
    cleaned["company_name"] = _clean_text(str(item.get("company_name") or ""))
    cleaned["source_url"] = source_url
    return cleaned


def _dedupe_records(records: list[dict], keys: tuple[str, ...]) -> list[dict]:
    seen: set[tuple[str, ...]] = set()
    output: list[dict] = []
    for record in records:
        marker = tuple(str(record.get(key, "")) for key in keys)
        if marker in seen:
            continue
        seen.add(marker)
        output.append(record)
    return output


def _stable_source_theme_id(prefix: str, name: str) -> str:
    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:10]
    return f"{prefix}_{digest}"


def _evidence(source_name: str, url: str, content: str, level: str) -> dict:
    return {
        "source": source_name,
        "source_level": level,
        "content": content,
        "url": url,
        "publish_date": None,
        "score_contribution": 0.0,
        "synced_at": _now_iso(),
        "sync_method": "topic_source_sync",
        "source_confidence": "verified",
    }


def _merge_unique(values: list[str], additions: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in [*values, *additions]:
        text = _clean_text(str(value or ""))
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _merge_evidence(existing: list, evidence: dict) -> list:
    items = [item for item in existing if isinstance(item, dict)]
    marker = (evidence.get("source"), evidence.get("url"), evidence.get("content"))
    for item in items:
        if (item.get("source"), item.get("url"), item.get("content")) == marker:
            item["last_seen_at"] = evidence["synced_at"]
            return items
    items.append(evidence)
    return items


def _upsert_profile(
    profiles: list[TopicProfile],
    *,
    theme_name: str,
    theme_id_prefix: str,
    keywords: list[str],
    industries: list[str],
    supply_chain_role: str,
    source_level: str,
    source_name: str,
    source_url: str,
) -> tuple[str, bool]:
    name = _clean_text(theme_name)
    if not name:
        return "", False
    now = _now_iso()
    profile = next((item for item in profiles if item.theme_name == name), None)
    created = False
    if profile is None:
        profile = TopicProfile(
            theme_id=_stable_source_theme_id(theme_id_prefix, name),
            theme_name=name,
            created_at=now,
            confidence="high",
            source_level=source_level,
            status="active",
        )
        profiles.append(profile)
        created = True
    profile.keywords = _merge_unique(profile.keywords, [name, *keywords])
    profile.industries = _merge_unique(profile.industries, industries)
    profile.supply_chain_role = profile.supply_chain_role or supply_chain_role
    profile.confidence = "high"
    profile.source_level = source_level
    profile.status = "active"
    profile.updated_at = now
    sync_sources = profile.extra.get("source_sync") if isinstance(profile.extra.get("source_sync"), list) else []
    source_record = {
        "source": source_name,
        "source_url": source_url,
        "synced_at": now,
        "last_seen_at": now,
        "sync_method": "topic_source_sync",
        "source_confidence": "verified",
    }
    if not any(item.get("source") == source_name and item.get("source_url") == source_url for item in sync_sources if isinstance(item, dict)):
        sync_sources.append(source_record)
    else:
        for item in sync_sources:
            if isinstance(item, dict) and item.get("source") == source_name and item.get("source_url") == source_url:
                item["last_seen_at"] = now
    profile.extra["source_sync"] = sync_sources
    return profile.theme_id, created


def apply_topic_source_caches_to_formal_library(
    *,
    tpex_data: dict | None = None,
    udn_data: dict | None = None,
) -> dict[str, int]:
    """Directly apply trusted external source caches to the formal topic library."""
    profiles = load_topic_profiles()
    company_map = load_company_topic_map()
    supply_nodes = load_supply_chain_nodes()
    stats = {
        "profiles_created": 0,
        "profiles_updated": 0,
        "company_relations_updated": 0,
        "supply_chain_nodes_updated": 0,
    }

    def profile_upserted(created: bool) -> None:
        if created:
            stats["profiles_created"] += 1
        else:
            stats["profiles_updated"] += 1

    if udn_data:
        for item in udn_data.get("industries") or []:
            theme_id, created = _upsert_profile(
                profiles,
                theme_name=item.get("name", ""),
                theme_id_prefix="udn_industry",
                keywords=[],
                industries=[item.get("name", "")],
                supply_chain_role="產業分類",
                source_level="L2_media",
                source_name="UDN 產業資料庫",
                source_url=item.get("url", UDN_INDUSTRY_URL),
            )
            if theme_id:
                profile_upserted(created)
        for item in udn_data.get("topics") or []:
            theme_id, created = _upsert_profile(
                profiles,
                theme_name=item.get("name", ""),
                theme_id_prefix="udn_topic",
                keywords=[item.get("category", "")],
                industries=[item.get("category", "")],
                supply_chain_role="產業題材索引",
                source_level="L2_media",
                source_name="UDN 產業資料庫",
                source_url=item.get("url", UDN_INDUSTRY_URL),
            )
            if theme_id:
                profile_upserted(created)

    if tpex_data:
        for raw_item in tpex_data.get("items") or []:
            item = _clean_tpex_item(raw_item)
            if not item:
                continue
            industry = item.get("industry") or item.get("name") or ""
            source_url = item.get("source_url") or TPEX_URL
            theme_id, created = _upsert_profile(
                profiles,
                theme_name=industry,
                theme_id_prefix="tpex_chain",
                keywords=[item.get("chain_stage", ""), item.get("role", "")],
                industries=[industry],
                supply_chain_role=item.get("chain_stage", "") or "TPEx 產業鏈",
                source_level="L1_official",
                source_name="TPEx 產業鏈資訊平台",
                source_url=source_url,
            )
            if theme_id:
                profile_upserted(created)
            code = _clean_text(str(item.get("company_code") or ""))
            if not code:
                continue
            company_name = _clean_text(item.get("company_name") or item.get("name") or code)
            evidence = _evidence(
                "TPEx 產業鏈資訊平台",
                source_url,
                " / ".join(part for part in [industry, item.get("chain_stage"), item.get("role"), company_name] if part),
                "L1_official",
            )
            relation = company_map.get(code) or TopicCompanyRelation(company_code=code, company_name=company_name)
            relation.company_name = relation.company_name or company_name
            relation.themes = _merge_unique(relation.themes, [theme_id] if theme_id else [])
            relation.primary_theme = relation.primary_theme or theme_id
            relation.relation_strength = relation.relation_strength or "high"
            relation.relation_type = relation.relation_type or "official_industry_chain"
            relation.role = relation.role or item.get("role") or item.get("chain_stage") or ""
            relation.products = _merge_unique(relation.products, [item.get("role", ""), item.get("chain_stage", "")])
            relation.evidence = _merge_evidence(relation.evidence, evidence)
            relation.updated_at = _now_iso()
            relation.extra["source_sync_status"] = "verified"
            relation.extra["source_sync_method"] = "topic_source_sync"
            company_map[code] = relation
            stats["company_relations_updated"] += 1

            node_id = f"tpex_{theme_id}_{code}_{hashlib.sha1((item.get('role') or item.get('chain_stage') or industry).encode('utf-8')).hexdigest()[:8]}"
            node = next((n for n in supply_nodes if n.node_id == node_id), None)
            if node is None:
                node = TopicSupplyChainNode(node_id=node_id, company_code=code, company_name=company_name)
                supply_nodes.append(node)
            node.theme_id = theme_id
            node.role = node.role or item.get("role") or item.get("chain_stage") or "TPEx 產業鏈節點"
            node.product_keywords = _merge_unique(node.product_keywords, [item.get("role", ""), item.get("chain_stage", ""), industry])
            node.confidence = "high"
            node.source_level = "L1_official"
            node.evidence = _merge_evidence(node.evidence, evidence)
            node.updated_at = _now_iso()
            node.extra["source_sync_status"] = "verified"
            node.extra["source_sync_method"] = "topic_source_sync"
            stats["supply_chain_nodes_updated"] += 1

    save_topic_profiles(profiles)
    save_company_topic_map(company_map)
    save_supply_chain_nodes(supply_nodes)
    return stats


def parse_tpex_industry_chain(html: str, base_url: str = TPEX_URL) -> dict:
    """Parse TPEx industry-chain pages into a conservative local index.

    The parser supports two shapes:
    - visible links/categories on index pages
    - table rows containing company codes and role/stage text
    """
    soup = BeautifulSoup(html or "", "html.parser")
    items: list[dict] = []

    for link in soup.find_all("a"):
        raw_name = link.get_text(" ")
        href = str(link.get("href") or "").strip()
        name = _clean_tpex_name(raw_name, href)
        if not name or not href or href.startswith("#"):
            continue
        if not _is_valid_tpex_topic_name(name, href):
            continue
        items.append({
            "name": name,
            "industry": name,
            "chain_stage": "",
            "role": "",
            "company_code": "",
            "company_name": "",
            "source_url": urljoin(base_url, href),
            "source_type": "tpex_link",
        })

    for row in soup.find_all("tr"):
        cells = [_clean_text(cell.get_text(" ")) for cell in row.find_all(["td", "th"])]
        cells = [cell for cell in cells if cell]
        if len(cells) < 2:
            continue
        code = next((cell for cell in cells if cell.isdigit() and 4 <= len(cell) <= 6), "")
        if not code:
            continue
        company_name = cells[cells.index(code) + 1] if cells.index(code) + 1 < len(cells) else ""
        item = {
            "name": company_name or code,
            "industry": cells[0] if cells else "",
            "chain_stage": cells[1] if len(cells) > 1 else "",
            "role": cells[2] if len(cells) > 2 else "",
            "company_code": code,
            "company_name": company_name,
            "source_url": base_url,
            "source_type": "tpex_table_row",
            "raw_cells": cells,
        }
        cleaned_item = _clean_tpex_item(item)
        if cleaned_item:
            items.append(cleaned_item)

    return {
        "source": "tpex_industry_chain",
        "updated_at": _now_iso(),
        "source_url": base_url,
        "items": _dedupe_records(items, ("source_url", "company_code", "name", "role")),
        "metadata": {
            "parser": "html_links_and_tables",
            "note": "TPEx cache is an industry-chain reference, not an investment conclusion.",
        },
    }


def parse_udn_industry_topics(html: str, base_url: str = UDN_INDUSTRY_URL) -> dict:
    """Parse UDN industry database index links into industry/topic caches."""
    soup = BeautifulSoup(html or "", "html.parser")
    industries: list[dict] = []
    topics: list[dict] = []

    industry_names = {
        "半導體", "記憶體", "被動元件", "太陽能", "LED", "光學產業", "平面顯示器",
        "觸控面板", "電腦及週邊", "連接器", "印刷電路板", "網通", "電商", "軟體",
        "人工智慧", "區塊鏈", "數位雲端", "資訊安全", "石化", "紡織", "水泥",
        "營建", "油電燃氣", "鋼鐵", "機電", "交通運輸", "汽車", "電動車",
        "製藥", "醫材", "保健食品", "再生醫療", "食品", "百貨零售", "休閒娛樂",
        "造紙", "文創", "居家生活", "氫能", "金融", "其他",
    }

    for link in soup.find_all("a"):
        name = _clean_text(link.get_text(" "))
        href = str(link.get("href") or "").strip()
        if not name or not href or href.startswith("#"):
            continue
        record = {
            "name": name,
            "url": urljoin(base_url, href),
            "source_level": "L2_media",
        }
        if name in industry_names:
            industries.append(record)
        elif _looks_like_topic_name(name):
            topic = dict(record)
            topic["category"] = _infer_udn_topic_category(name)
            topics.append(topic)

    return {
        "source": "udn_industry_topics",
        "updated_at": _now_iso(),
        "source_url": base_url,
        "industries": _dedupe_records(industries, ("name", "url")),
        "topics": _dedupe_records(topics, ("name", "url")),
        "metadata": {
            "parser": "industry_index_links",
            "note": "UDN cache stores only metadata/URLs and should be used as media-level topic activity reference.",
        },
    }


def _looks_like_topic_name(name: str) -> bool:
    if len(name) < 2 or len(name) > 30:
        return False
    blocked = {"首頁", "即時", "要聞", "產業", "證券", "國際", "兩岸", "金融", "期貨", "理財", "房市", "專欄", "專題", "品味", "商情"}
    if name in blocked:
        return False
    topic_hints = ("AI", "CoWoS", "CPO", "伺服器", "供應鏈", "衛星", "機器人", "電池", "重電", "儲能", "車用", "半導體", "散熱", "BBU", "無人機")
    return any(hint in name for hint in topic_hints)


def _infer_udn_topic_category(name: str) -> str:
    tech_hints = ("AI", "CoWoS", "CPO", "伺服器", "半導體", "散熱", "BBU", "衛星", "機器人", "無人機", "5G", "Nvidia")
    return "科技" if any(hint in name for hint in tech_hints) else "主題"


def sync_topic_sources(
    *,
    include_tpex: bool = True,
    include_udn: bool = True,
    fetcher: HtmlFetcher | None = None,
    progress: Callable[[str], None] | None = None,
) -> TopicSourceSyncResult:
    """Fetch external topic source indexes and write local caches."""
    fetch = fetcher or _default_fetcher
    result = TopicSourceSyncResult(success=True)
    synced_tpex_data: dict | None = None
    synced_udn_data: dict | None = None

    def emit(message: str) -> None:
        if progress:
            progress(f"[題材來源同步] {message}")

    if include_tpex:
        try:
            emit("同步 TPEx 產業鏈資料")
            html, fetch_metadata = _fetch_tpex_html(fetcher)
            data = parse_tpex_industry_chain(html, TPEX_URL)
            data.setdefault("metadata", {}).update(fetch_metadata)
            save_tpex_industry_chain(data)
            synced_tpex_data = data
            result.synced_sources.append("tpex")
            result.tpex_items = len(data.get("items") or [])
            emit(f"TPEx 完成：{result.tpex_items} 筆")
        except Exception as exc:
            result.success = False
            result.failed_sources["tpex"] = str(exc)
            emit(f"TPEx 失敗：{exc}")

    if include_udn:
        try:
            emit("同步 UDN 產業資料庫")
            html = fetch(UDN_INDUSTRY_URL)
            data = parse_udn_industry_topics(html, UDN_INDUSTRY_URL)
            save_udn_industry_topics(data)
            synced_udn_data = data
            result.synced_sources.append("udn")
            result.udn_industries = len(data.get("industries") or [])
            result.udn_topics = len(data.get("topics") or [])
            emit(f"UDN 完成：產業 {result.udn_industries}，主題 {result.udn_topics}")
        except Exception as exc:
            result.success = False
            result.failed_sources["udn"] = str(exc)
            emit(f"UDN 失敗：{exc}")

    if synced_tpex_data or synced_udn_data:
        try:
            emit("套用可信外部來源到正式題材庫")
            stats = apply_topic_source_caches_to_formal_library(
                tpex_data=synced_tpex_data,
                udn_data=synced_udn_data,
            )
            result.formal_profiles_created = stats["profiles_created"]
            result.formal_profiles_updated = stats["profiles_updated"]
            result.formal_company_relations_updated = stats["company_relations_updated"]
            result.formal_supply_chain_nodes_updated = stats["supply_chain_nodes_updated"]
            emit(
                "正式題材庫更新："
                f"profiles +{result.formal_profiles_created}/~{result.formal_profiles_updated}，"
                f"company_relations {result.formal_company_relations_updated}，"
                f"supply_chain_nodes {result.formal_supply_chain_nodes_updated}"
            )
        except Exception as exc:
            result.success = False
            result.failed_sources["formal_library"] = str(exc)
            emit(f"正式題材庫套用失敗：{exc}")

    return result
