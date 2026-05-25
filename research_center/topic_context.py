from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]


def _load_json_safe(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _theme_profiles() -> list[dict[str, Any]]:
    return _load_json_safe(_ROOT / "config" / "theme_profiles.json", [])


def _company_theme_map() -> dict[str, Any]:
    return _load_json_safe(_ROOT / "config" / "company_theme_map.json", {})


def _supply_chain_nodes() -> list[dict[str, Any]]:
    return _load_json_safe(_ROOT / "config" / "supply_chain_nodes.json", [])


def _find_supply_chain_role(stock_code: str, theme_id: str, nodes: list[dict[str, Any]]) -> str:
    for n in nodes:
        if n.get("company_code") == stock_code and n.get("theme_id") == theme_id:
            return str(n.get("role", ""))
    return ""


def _company_theme_refs(entry: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    formal: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    if isinstance(entry, dict):
        theme_statuses = entry.get("theme_statuses") if isinstance(entry.get("theme_statuses"), dict) else {}
        theme_refs = entry.get("themes") or []
        if not isinstance(theme_refs, list):
            theme_refs = [theme_refs] if theme_refs else []
        for ref in theme_refs:
            normalized = _normalize_theme_ref(ref, default_status="")
            if not normalized.get("theme_id"):
                continue
            status = str(theme_statuses.get(normalized["theme_id"]) or normalized.get("verification_status") or "").lower()
            if status == "candidate" or normalized.get("usage_policy") == "hypothesis_only":
                normalized["verification_status"] = "candidate"
                normalized["usage_policy"] = "hypothesis_only"
                normalized["not_representative"] = True
                candidates.append(normalized)
            else:
                normalized["verification_status"] = status or "formal"
                formal.append(normalized)
        raw_candidates = entry.get("candidate_themes") or []
        if isinstance(raw_candidates, list):
            for ref in raw_candidates:
                normalized = _normalize_theme_ref(ref, default_status="candidate")
                if normalized.get("theme_id"):
                    normalized["usage_policy"] = "hypothesis_only"
                    normalized["not_representative"] = True
                    candidates.append(normalized)
        return formal, candidates
    if isinstance(entry, list):
        for ref in entry:
            normalized = _normalize_theme_ref(ref, default_status="")
            if normalized.get("theme_id"):
                formal.append(normalized)
        return formal, candidates
    normalized = _normalize_theme_ref(entry, default_status="")
    if normalized.get("theme_id"):
        formal.append(normalized)
    return formal, candidates


def _normalize_theme_ref(ref: Any, default_status: str = "") -> dict[str, Any]:
    if isinstance(ref, str):
        return {"theme_id": ref, "verification_status": default_status}
    if isinstance(ref, dict):
        theme_id = str(ref.get("theme_id") or ref.get("id") or "").strip()
        return {
            "theme_id": theme_id,
            "theme_name": ref.get("theme_name") or ref.get("name"),
            "verification_status": ref.get("verification_status") or ref.get("status") or default_status,
            "usage_policy": ref.get("usage_policy"),
            "not_representative": bool(ref.get("not_representative") or False),
            "evidence": ref.get("evidence") if isinstance(ref.get("evidence"), list) else [],
            "missing_data": ref.get("missing_data") if isinstance(ref.get("missing_data"), list) else [],
        }
    return {}


def build_stock_topic_context(stock_code: str, stock_name: str | None = None) -> dict[str, Any]:
    """Build topic context for a single stock for /research prompt injection.

    Topics are background reference only — AI must re-validate with current data.
    """
    company_map = _company_theme_map()
    profiles = _theme_profiles()
    nodes = _supply_chain_nodes()

    profile_by_id: dict[str, dict[str, Any]] = {}
    for p in profiles:
        tid = p.get("theme_id")
        if tid:
            profile_by_id[tid] = p

    matched: list[dict[str, Any]] = []

    # Direct match from company_theme_map. Candidate themes are kept as
    # hypothesis-only clues and must not be treated as representative stocks.
    formal_refs, candidate_refs = _company_theme_refs(company_map.get(stock_code))

    for theme_ref in formal_refs:
        theme_id = theme_ref.get("theme_id", "")
        theme_name = theme_ref.get("theme_name", profile_by_id.get(theme_id, {}).get("theme_name", theme_id))

        profile = profile_by_id.get(theme_id, {})
        matched.append({
            "theme_id": theme_id,
            "theme_name": theme_name,
            "confidence": "high",
            "keywords": (profile.get("keywords", []) or [])[:5],
            "supply_chain_role": _find_supply_chain_role(stock_code, theme_id, nodes),
            "affected_companies": (profile.get("affected_companies", []) or [])[:5],
            "risk_notes": (profile.get("risk_notes", []) or [])[:3],
            "missing_data": (profile.get("missing_data", []) or [])[:3],
            "evidence_summary": "",
            "verification_status": theme_ref.get("verification_status") or "formal",
            "usage_policy": "formal_topic_reference",
        })

    for theme_ref in candidate_refs[:3]:
        theme_id = theme_ref.get("theme_id", "")
        profile = profile_by_id.get(theme_id, {})
        matched.append({
            "theme_id": theme_id,
            "theme_name": theme_ref.get("theme_name", profile.get("theme_name", theme_id)),
            "confidence": "candidate",
            "keywords": (profile.get("keywords", []) or [])[:5],
            "supply_chain_role": _find_supply_chain_role(stock_code, theme_id, nodes),
            "affected_companies": [],
            "risk_notes": (profile.get("risk_notes", []) or [])[:3],
            "missing_data": (theme_ref.get("missing_data") or profile.get("missing_data", []) or [])[:3],
            "evidence_summary": "",
            "verification_status": "candidate",
            "usage_policy": "hypothesis_only",
            "not_representative": True,
        })

    # Weak match: by stock_name keywords in theme keywords
    if stock_name and len(matched) < 8:
        stock_name_lower = stock_name.lower()
        for p in profiles:
            tid = p.get("theme_id", "")
            if any(m["theme_id"] == tid for m in matched):
                continue
            keywords = [str(k).lower() for k in (p.get("keywords", []) or []) if k]
            if any(k in stock_name_lower for k in keywords):
                matched.append({
                    "theme_id": tid,
                    "theme_name": p.get("theme_name", tid),
                    "confidence": "low",
                    "keywords": (p.get("keywords", []) or [])[:5],
                    "supply_chain_role": _find_supply_chain_role(stock_code, tid, nodes),
                    "affected_companies": (p.get("affected_companies", []) or [])[:5],
                    "risk_notes": (p.get("risk_notes", []) or [])[:3],
                    "missing_data": (p.get("missing_data", []) or [])[:3],
                    "evidence_summary": "",
                })
                if len(matched) >= 8:
                    break

    # Cap at 5-8 themes
    matched = matched[:8]

    return {
        "matched_topics": matched,
        "company_topic_relations": {
            "stock_code": stock_code,
            "stock_name": stock_name or "",
            "direct_matches": len([m for m in matched if m["confidence"] == "high"]),
            "weak_matches": len([m for m in matched if m["confidence"] == "low"]),
        },
        "supply_chain_nodes": [n for n in nodes if n.get("company_code") == stock_code][:5],
        "risk_notes": list(dict.fromkeys(r for m in matched for r in m.get("risk_notes", []) if r))[:5],
        "missing_data": list(dict.fromkeys(d for m in matched for d in m.get("missing_data", []) if d))[:5],
        "usage_policy": {
            "role": "背景參考與候選假設",
            "rules": [
                "題材庫資料僅供背景參考，不得直接當成最終結論。",
                "不得僅因題材庫標記就認定公司受惠。",
                "必須用本次公告、財報、營收、法說會、新聞、價量與法人資料重新驗證。",
                "若最新證據不足，必須降低題材受惠判斷信心。",
                "若題材庫與最新證據衝突，需指出衝突，並以最新證據為準。",
            ],
        },
    }


def build_candidates_topic_context(candidates: list[dict[str, Any]], limit: int = 30) -> dict[str, Any]:
    """Build topic context for /value_scan candidate pool.

    Each candidate gets up to 3 themes to keep prompt size reasonable.
    """
    company_map = _company_theme_map()
    profiles = _theme_profiles()

    profile_by_id: dict[str, dict[str, Any]] = {}
    for p in profiles:
        tid = p.get("theme_id")
        if tid:
            profile_by_id[tid] = p

    candidate_topic_map: list[dict[str, Any]] = []
    all_matched_theme_ids: set[str] = set()

    for cand in candidates[:limit]:
        code = str(cand.get("code", ""))
        name = str(cand.get("name", ""))
        if not code:
            continue

        themes: list[dict[str, Any]] = []
        formal_refs, candidate_refs = _company_theme_refs(company_map.get(code))

        for theme_ref in formal_refs[:3]:
            theme_id = theme_ref.get("theme_id", "")
            theme_name = theme_ref.get("theme_name", profile_by_id.get(theme_id, {}).get("theme_name", theme_id))

            profile = profile_by_id.get(theme_id, {})
            themes.append({
                "theme_id": theme_id,
                "theme_name": theme_name,
                "keywords": (profile.get("keywords", []) or [])[:5],
                "risk_notes": (profile.get("risk_notes", []) or [])[:2],
                "verification_status": theme_ref.get("verification_status") or "formal",
                "usage_policy": "formal_topic_reference",
            })
            all_matched_theme_ids.add(theme_id)

        for theme_ref in candidate_refs[: max(0, 3 - len(themes))]:
            theme_id = theme_ref.get("theme_id", "")
            profile = profile_by_id.get(theme_id, {})
            themes.append({
                "theme_id": theme_id,
                "theme_name": theme_ref.get("theme_name", profile.get("theme_name", theme_id)),
                "keywords": (profile.get("keywords", []) or [])[:5],
                "risk_notes": (profile.get("risk_notes", []) or [])[:2],
                "verification_status": "candidate",
                "usage_policy": "hypothesis_only",
                "not_representative": True,
            })
            all_matched_theme_ids.add(theme_id)

        # Weak match by name if needed
        if len(themes) < 3 and name:
            name_lower = name.lower()
            for p in profiles:
                tid = p.get("theme_id", "")
                if any(t["theme_id"] == tid for t in themes):
                    continue
                keywords = [str(k).lower() for k in (p.get("keywords", []) or []) if k]
                if any(k in name_lower for k in keywords):
                    themes.append({
                        "theme_id": tid,
                        "theme_name": p.get("theme_name", tid),
                        "keywords": (p.get("keywords", []) or [])[:5],
                        "risk_notes": (p.get("risk_notes", []) or [])[:2],
                    })
                    all_matched_theme_ids.add(tid)
                    if len(themes) >= 3:
                        break

        candidate_topic_map.append({
            "code": code,
            "name": name,
            "themes": themes,
        })

    topic_summary: list[dict[str, Any]] = []
    for tid in sorted(all_matched_theme_ids):
        p = profile_by_id.get(tid)
        if p:
            topic_summary.append({
                "theme_id": tid,
                "theme_name": p.get("theme_name", tid),
                "keywords": (p.get("keywords", []) or [])[:5],
            })

    return {
        "candidate_topic_map": candidate_topic_map,
        "topic_summary": topic_summary,
        "usage_policy": {
            "role": "候選股題材背景參考",
            "rules": [
                "題材庫只作為候選股題材背景，不得只因某股票命中熱門題材就給高分。",
                "重估分數仍需依財報、營收、公告、新聞、籌碼、價量與反證判斷。",
                "題材庫可幫助辨識題材受惠鏈、供應鏈角色、需要驗證的缺口與可能反證。",
                "若題材庫資料不足，應寫入資料缺口，不得自動補高分。",
            ],
        },
    }


def build_theme_topic_context(theme_query: str) -> dict[str, Any]:
    """Build topic-library context for /theme prompt injection.

    The returned context is reference-only. The final /theme report must re-check
    the current sources, discovery results, and company evidence.
    """
    query = (theme_query or "").strip()
    query_lower = query.lower()
    profiles = _theme_profiles()
    nodes = _supply_chain_nodes()

    matched_topics: list[dict[str, Any]] = []
    matched_theme_ids: set[str] = set()

    for profile in profiles:
        theme_id = str(profile.get("theme_id", ""))
        theme_name = str(profile.get("theme_name", theme_id))
        keywords = [str(k) for k in (profile.get("keywords", []) or []) if k]
        search_text = " ".join([theme_id, theme_name, *keywords]).lower()
        if not query_lower or query_lower not in search_text:
            continue

        confidence = "high" if query_lower in {theme_id.lower(), theme_name.lower()} else "medium"
        matched_topics.append({
            "theme_id": theme_id,
            "theme_name": theme_name,
            "confidence": confidence,
            "keywords": keywords[:8],
            "industries": (profile.get("industries", []) or [])[:8],
            "affected_companies": (profile.get("affected_companies", []) or [])[:10],
            "supply_chain_role": profile.get("supply_chain_role", ""),
            "risk_notes": (profile.get("risk_notes", []) or [])[:5],
            "missing_data": (profile.get("missing_data", []) or [])[:5],
        })
        if theme_id:
            matched_theme_ids.add(theme_id)

    matched_topics = matched_topics[:8]
    related_nodes = [n for n in nodes if n.get("theme_id") in matched_theme_ids][:20]

    return {
        "query": query,
        "matched_topics": matched_topics,
        "related_supply_chain_nodes": related_nodes,
        "risk_notes": list(dict.fromkeys(r for m in matched_topics for r in m.get("risk_notes", []) if r))[:8],
        "missing_data": list(dict.fromkeys(d for m in matched_topics for d in m.get("missing_data", []) if d))[:8],
        "usage_policy": {
            "role": "題材庫背景參考與相近題材提示",
            "rules": [
                "題材庫資料僅供背景參考，不得直接當成最終結論。",
                "不得只因題材庫已有相近題材，就認定本次題材研究結論成立。",
                "必須用本次搜尋、新聞、公告、法說會、財報、價量與法人資料重新驗證。",
                "若題材庫與最新證據衝突，需指出衝突，並以最新證據為準。",
                "可使用題材庫協助辨識相近題材、供應鏈角色、風險與資料缺口。",
            ],
        },
    }
