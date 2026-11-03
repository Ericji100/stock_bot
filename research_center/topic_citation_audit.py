"""Trace topic citations to supplied evidence, without claiming factual verification."""
from __future__ import annotations

from collections import Counter
import re
from typing import Any
from urllib.parse import parse_qs, urlsplit, urlunsplit

from .source_date_normalizer import normalize_published_date
from .topic_input_package import merge_topic_sources
from .topic_quality import normalize_status


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


def _provided_body(row):
    # Search API content and fetched HTML are both supplied text. A plain search
    # snippet is not promoted without provenance, regardless of its length.
    return str(row.get('content') or (row.get('snippet') if 'source_text:provided_content' in (row.get('found_by') or []) else '') or '')


def _company_quote_matches(claim, catalogue):
    code = str(claim.get('company_code') or claim.get('code') or '')
    name = str(claim.get('company_name') or claim.get('name') or '')
    for item in claim.get('evidence') or []:
        if not isinstance(item, dict):
            continue
        quote = ''.join(str(item.get('supporting_quote') or '').split())
        url = _url(item.get('url') or item.get('source_url'))
        for doc in catalogue.get(url, []):
            if len(quote) < 16 or quote not in ''.join(_provided_body(doc).split()):
                continue
            title = str(doc.get('title') or '')
            parts = urlsplit(url)
            official_identity = (parts.hostname in {'emops.twse.com.tw','mops.twse.com.tw'} and code and
                                 re.fullmatch(r'/nas/STR/' + re.escape(code) + r'\d{8}[A-Z]\d+\.pdf', parts.path, re.I))
            if (code and code in quote) or (name and (name in quote or name in title)) or official_identity:
                return True
    return False


def quarantine_unproven_company_claims(pack, data):
    """Keep unsupported relations as hypotheses, never promote by source level alone."""
    catalogue = {}
    for key in ('discovery_sources','base_sources','web_fetched_sources'):
        for row in data.get(key) or []:
            if isinstance(row,dict):
                catalogue.setdefault(_url(row.get('url')),[]).append(row)
    count = 0
    for action in pack.actions:
        for kind in ('company_relations','affected_companies','supply_chain_nodes'):
            for claim in getattr(action,kind) or []:
                if not isinstance(claim,dict) or normalize_status(claim.get('verification_status')) not in {'verified','inferred'}:
                    continue
                if _company_quote_matches(claim,catalogue):
                    continue
                claim['validation_original_status'] = claim['verification_status']
                claim['verification_status'] = 'candidate'
                warning = '未取得可直接對照此公司主張的原文，保留完整內容為候選，不作已確認關係套用。'
                missing = claim.setdefault('missing_data',[])
                if warning not in missing:
                    missing.append(warning)
                count += 1
    pack.extra['company_claims_quarantined'] = count
    return count


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
    collected = [row for key in ('discovery_sources','base_sources','web_fetched_sources')
                 for row in structured_data.get(key) or [] if isinstance(row,dict)]
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
    company_names = {}
    for code, name in (structured_data.get('authoritative_company_names') or {}).items():
        if code and name:
            company_names[str(code)] = {str(name)}
    def company_rows(value):
        if isinstance(value, dict):
            code = str(value.get('company_code') or value.get('code') or '')
            name = str(value.get('company_name') or value.get('name') or '')
            if code and name:
                company_names.setdefault(code, set()).add(name)
            for item in value.values():
                company_rows(item)
        elif isinstance(value, list):
            for item in value:
                company_rows(item)
    for key in ('matched_companies', 'matched_universe', 'company_knowledge', 'company_topic_map', 'supply_chain_nodes'):
        company_rows(structured_data.get(key))
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
        quote = str(row.get('supporting_quote') or '').strip()
        if quote:
            normalized = ''.join(quote.split())
            if len(normalized) < 16 or not any(normalized in ''.join(_provided_body(source).split()) for source in rows):
                flags.append('supporting_quote_not_in_body')
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
        for kind, claims in (('company_relations', action.company_relations), ('supply_chain_nodes', action.supply_chain_nodes)):
            for claim_index, claim in enumerate(claims):
                if not isinstance(claim, dict):
                    continue
                status = normalize_status(claim.get('verification_status') or claim.get('status'))
                if status not in {'verified', 'inferred'}:
                    continue
                code = str(claim.get('company_code') or claim.get('code') or '')
                name = str(claim.get('company_name') or claim.get('name') or '')
                if code in company_names and name and name not in company_names[code]:
                    counts['company_identity_mismatch'] += 1
                    issues.append({'path':f'/actions/{index}/{kind}/{claim_index}',
                                   'reason':'company_identity_mismatch', 'company_code':code, 'company_name':name,
                                   'known_names':sorted(company_names[code])})
                if not _company_quote_matches(claim,catalogue):
                    counts['verified_company_without_body_quote'] += 1
                    issues.append({'path': f'/actions/{index}/{kind}/{claim_index}',
                                   'reason': 'verified_company_without_body_quote', 'company_code': code})
    return {"schema_version": 1, "report_date": as_of, "citation_count": len(citations),
            "counts": dict(counts), "issues": issues, "blocking_count": len(issues),
            "passed": not issues,
            "scope": "驗證引用可追溯、日期、頁型與已確認公司主張的原文對照；不表示已獨立證實主張。舊資料可作背景，不代表近期訂單。"}
