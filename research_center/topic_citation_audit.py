"""Trace topic citations to supplied evidence, without claiming factual verification."""
from __future__ import annotations

from collections import Counter
import re
from typing import Any
from urllib.parse import parse_qs, urlsplit, urlunsplit

from .source_date_normalizer import normalize_published_date
from .topic_input_package import merge_topic_sources


def _url(value: Any) -> str:
    try:
        parts = urlsplit(str(value or "").strip())
    except ValueError:
        return ""
    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
        return ""
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, ""))


def _rows(value: Any, path: str = ""):
    if isinstance(value, dict):
        if value.get("url") or value.get("source_url"):
            yield path, value
        for key, item in value.items():
            yield from _rows(item, f"{path}/{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _rows(item, f"{path}/{index}")


def _non_document(url: str) -> bool:
    parts = urlsplit(url)
    # PDFs and official data APIs are evidence too; do not reuse a news-only filter.
    segments = parts.path.lower().strip("/").split("/")
    return (not parts.path.strip("/") or
            any(segment in {"search", "query", "tag", "tags"} for segment in segments) or
            bool(set(parse_qs(parts.query)) & {"q", "query", "search"}))


def quarantine_topic_source_leads(pack, structured_data: dict[str, Any]) -> None:
    """Preserve weak top-level citations as leads, never as formal support."""
    known = {_url(row.get('url')) for row in merge_topic_sources(structured_data)}
    for key in ('existing_topic_profiles','company_topic_map','company_knowledge','supply_chain_nodes'):
        known.update(_url(row.get('url') or row.get('source_url')) for _,row in _rows(structured_data.get(key)))
    known.discard('')
    for action in pack.actions:
        support = []
        for evidence in action.evidence:
            url = _url(evidence.url)
            if url and url in known and not _non_document(url):
                support.append(evidence)
                continue
            lead = evidence.to_dict()
            lead.update(verification_status='candidate',evidence_role='unverified_lead',
                        quarantine_reason='未取得可支持具體主張的獨立文件，僅保留待查線索')
            leads = action.extra.setdefault('unverified_source_leads', [])
            if lead not in leads:
                leads.append(lead)
            warning='部分來源僅是待查線索，不可作為已證實的客戶、訂單或營收證據。'
            if warning not in action.missing_data:
                action.missing_data.append(warning)
        action.evidence = support


def audit_topic_citations(pack, structured_data: dict[str, Any]) -> dict[str, Any]:
    catalogue: dict[str, list[dict]] = {}
    collected = merge_topic_sources(structured_data)
    for row in collected:
        if url := _url(row.get("url")):
            catalogue.setdefault(url, []).append(row)
    for key in ("existing_topic_profiles", "company_topic_map", "company_knowledge", "supply_chain_nodes"):
        for _, row in _rows(structured_data.get(key)):
            if url := _url(row.get("url") or row.get("source_url")):
                catalogue.setdefault(url, []).append(row)
    bodies = "\n".join(str(row.get("content") or row.get("snippet") or "") for row in collected)
    as_of = normalize_published_date(structured_data.get("report_date"), pack.created_at)
    issues = []
    counts: Counter[str] = Counter()
    citations = list(_rows({"actions": [a.to_dict() for a in pack.actions],
                            "company_knowledge_updates": pack.company_knowledge_updates}))
    for path, row in citations:
        raw_url = row.get("url") or row.get("source_url")
        url = _url(raw_url)
        rows = catalogue.get(url, [])
        flags = []
        is_lead = bool(re.match(r'^/actions/\d+/extra/unverified_source_leads/',path))
        if not url:
            flags.append("invalid_url")
        elif not rows:
            flags.append("body_link_not_fetched" if str(raw_url) in bodies else "url_not_in_inputs")
        if url and _non_document(url):
            flags.append("non_document_url")
        if is_lead:
            counts['unverified_lead'] += 1
            flags = []
        supplied_dates = {day for source in rows if (day := normalize_published_date(
            source.get("published_date"), source.get("publish_date"), source.get("published_at")))}
        cited_date = normalize_published_date(row.get("publish_date"), row.get("published_date"))
        if as_of and (any(day > as_of for day in supplied_dates) or (cited_date and cited_date > as_of)):
            flags.append("future_evidence")
        if cited_date and supplied_dates and cited_date not in supplied_dates:
            flags.append("citation_date_mismatch")
        if rows and not supplied_dates:
            counts["date_unknown"] += 1
        if as_of and supplied_dates and max(supplied_dates) < f"{int(as_of[:4]) - 1}{as_of[4:]}":
            counts["historical_context"] += 1
        if not flags and not is_lead:
            counts["traceable_url"] += 1
        for flag in flags:
            counts[flag] += 1
            issues.append({"path": path, "url": str(raw_url), "reason": flag,
                           "supplied_dates": sorted(supplied_dates), "cited_date": cited_date})
    # A source-less AI action must not be treated as an independently verified update.
    for index, action in enumerate(pack.actions):
        if not any(path.startswith(f"/actions/{index}/") and '/extra/unverified_source_leads/' not in path for path, _ in citations):
            counts["action_without_citation"] += 1
            issues.append({"path": f"/actions/{index}", "reason": "action_without_citation"})
    return {"schema_version": 1, "report_date": as_of, "citation_count": len(citations),
            "counts": dict(counts), "issues": issues, "blocking_count": len(issues),
            "passed": not issues,
            "scope": "僅驗證引用可追溯、日期與頁型；不表示已獨立證實主張。舊資料可作背景，不代表近期訂單。"}
