"""Shared mechanical evidence checks; never an investment or truth classifier."""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import date, datetime
import re
from typing import Any
from urllib.parse import urlsplit, parse_qs
from zoneinfo import ZoneInfo

from .source_date_normalizer import source_temporal_record

OFFICIAL_DOMAINS = ("twse.com.tw", "tpex.org.tw", "mops.twse.com.tw", "tdcc.com.tw",
                    "taifex.com.tw", "sec.gov", "federalreserve.gov", "cbc.gov.tw", "stat.gov.tw")
# Explicit corporate IR hosts; this is provenance, not claim verification.
CORPORATE_IR_HOSTS = {'pr.tsmc.com', 'investor.tsmc.com', 'investor.cld.tsmc.com'}


def is_official_url(url: str) -> bool:
    try:
        host = (urlsplit(url).hostname or '').lower()
    except ValueError:
        return False
    return host in CORPORATE_IR_HOSTS or any(host == domain or host.endswith('.' + domain) for domain in OFFICIAL_DOMAINS)
GOVERNANCE_RULES = (
    "報告日、發布日、更新日、事件日、數據所屬期與取得時間必須分開；未知不可補造。",
    "官方入口網址僅是查詢線索；須取得對應公司與期間的正文或數值，才可支持具體主張。",
    "公司代號、名稱及產品／客戶／供應鏈角色需逐項核對；名稱或題材關鍵字命中只屬候選。",
    "同代號的法定全名與簡稱差異只列名稱待核，不得僅因名稱長短宣稱錯公司或錯代號；產品、客戶關係仍須證據。",
    "區分已核對事實、分析推論與條件式預測。重要主張附來源、期間及反證；未取得須明說。",
    "保留具現實基礎的想像力；推演列出假設、催化、時間範圍、驗證與失效條件，不虛構機率或績效。",
    "正式長報告若有具體預測，請在『預測追蹤』表列標的、假設、觀察截止日、驗證條件、失效條件、來源；截止日用西元年月日。新聞分類及短評不強制新增長表。",
    "本地檢查只是風險提示，不代表語意已完全驗證；不得把資料缺漏直接當成公司表現差。",
)


def source_record(source: Any) -> dict[str, Any]:
    return asdict(source) if is_dataclass(source) else dict(source)


def official_evidence_status(source: Any) -> str:
    row = source_record(source)
    try:
        host = (urlsplit(str(row.get("url") or row.get("source_url") or "")).hostname or "").lower()
    except ValueError:
        return 'invalid_url'
    if not is_official_url(str(row.get('url') or row.get('source_url') or '')):
        return "non_official"
    status = (row.get("payload") or {}).get("status") if isinstance(row.get("payload"), dict) else None
    if row.get("document_kind") in {"reference_link", "unavailable"} or status in {"reference_link", "reachable", "unavailable"}:
        return "reference_only"
    if row.get("document_kind") in {"parsed_record", "document_body"} and (row.get("content") or row.get("snippet") or row.get("payload")):
        return "retrieved_requires_claim_check"
    return "official_origin_unverified_content"


def build_evidence_governance(command: str, report_date: date | None, sources: list[Any], data: dict[str, Any] | None = None) -> dict[str, Any]:
    target = report_date or datetime.now(ZoneInfo("Asia/Taipei")).date()
    from .entity_resolver import _load_stock_universe
    from .config import ROOT_DIR
    names = {str(row.get('code')): row['name'] for row in _load_stock_universe(ROOT_DIR) if row.get('code') and row.get('name')}
    names.update((data or {}).get('authoritative_company_names') or {})
    checked_data = {**(data or {}), 'authoritative_company_names': names}
    records = []
    warnings = []
    for source in sources:
        record = source_temporal_record(source, target)
        record["official_evidence_status"] = official_evidence_status(source)
        record['company_mapping_issues'] = source_company_conflicts(source, checked_data)
        if record["publication_status"] != "explicit":
            record["usage"] = "發布時間未確認，不可證明當時已知"
        elif record["available_by_report_date"] is False or record["data_after_report_date"] or record['content_updated_after_report_date']:
            record["usage"] = "晚於分析日，不得作為當時已知事實"
        else:
            record["usage"] = "日期符合，仍須核對主張語意與公司"
        records.append(record)
    if any(row["publication_status"] != "explicit" for row in records):
        warnings.append("部分來源發布日期未確認；不得以事件日期或取得時間冒充發布日期。")
    if any(row["available_by_report_date"] is False or row["data_after_report_date"] or row['content_updated_after_report_date'] for row in records):
        warnings.append("存在晚於分析日的來源或數據，不能用於歷史事實判斷。")
    if any(row["official_evidence_status"] == "reference_only" for row in records):
        warnings.append("部分官方來源只有查詢入口，尚未取得可支持主張的文件。")
    mappings = audit_company_mappings(checked_data)
    official = official_evidence_coverage(sources)
    if not official['retrieved_unique_urls']:
        warnings.append('尚未取得可核對的官方正文或解析數值；官方入口與媒體轉述不算官方證據已齊全。')
    if mappings:
        warnings.append("公司關聯含待查證候選或代號／名稱衝突，不能當成已確認供應關係。")
    if any(row['company_mapping_issues'] for row in records):
        warnings.append('來源原文存在公司代號／名稱或發行公司衝突，請以主檔核對，原文只作待核線索，不可直接沿用錯誤公司主張。')
    return {"schema_version": "evidence_governance_v1", "command": command,
            "report_date": target.isoformat(), "sources": records,
            "company_mapping_issues": mappings, "warnings": warnings,
            "official_evidence_coverage": official,
            "point_in_time_audit": (data or {}).get('point_in_time_audit'),
            "rules": list(GOVERNANCE_RULES), "semantic_verified": False}


def official_evidence_coverage(sources: list[Any]) -> dict[str, Any]:
    groups: dict[str, set[str]] = {}
    for source in sources:
        row = source_record(source)
        status = official_evidence_status(row)
        groups.setdefault(status, set()).add(str(row.get('url') or row.get('source_url') or ''))
    return {'retrieved_unique_urls': len(groups.get('retrieved_requires_claim_check', set())),
            'reference_only_unique_urls': len(groups.get('reference_only', set())),
            'unverified_official_unique_urls': len(groups.get('official_origin_unverified_content', set())),
            'claims_verified': False}


def source_company_conflicts(source: Any, data: dict[str, Any]) -> list[dict[str, str]]:
    row = source_record(source)
    names = dict(data.get('authoritative_company_names') or {})
    master = data.get('stock_index') or {}
    if isinstance(master, list):
        master = {str(row.get('code')): row for row in master if isinstance(row, dict)}
    for code, company in master.items():
        if isinstance(company, dict):
            names.setdefault(str(code), company.get('name') or company.get('stock_name'))
    text = str(row.get('title') or '') + '\n' + str(row.get('content') or row.get('snippet') or '')
    issues = []
    for match in re.finditer(r'([\u4e00-\u9fffA-Za-z]{2,12}(?:-[A-Za-z]{2,4})?)\s*[（(]\s*(\d{4,6})\s*[)）]', text):
        name, code = match.groups()
        expected = names.get(code)
        if expected and not (name.endswith(str(expected)) or name.startswith(str(expected) + '股份')):
            abbreviation = str(expected).startswith(name) or name.startswith(str(expected))
            issues.append({'code': code, 'stated_name': name, 'expected_name': str(expected),
                'kind': 'possible_abbreviation' if abbreviation else 'name_mismatch',
                'usage': '名稱可能為簡稱，列待核而非已確認錯代號' if abbreviation else '公司代號與名稱衝突；原句僅作待核線索'})
    stock = data.get('stock') or {}
    target = str(stock.get('code') or '')
    for issuer in document_issuer_codes(row):
        if target and issuer != target:
            issues.append({'code': issuer, 'expected_code': target, 'kind': 'issuer_mismatch',
                           'usage': '其他發行公司文件，只能作產業背景，不可支持目標公司的主張'})
    return issues


def document_issuer_codes(source: Any) -> set[str]:
    """Recognize explicit document issuers, not related companies in prose."""
    row = source_record(source)
    issuers = set()
    pending = [str(row.get('url') or row.get('source_url') or '')]
    for _ in range(3):
        next_urls = []
        for url in pending:
            try:
                parsed = urlsplit(url)
                if is_official_url(url):
                    filename = re.search(r'/STR/(\d{4,6})((?:19|20)\d{6})M\d+\.pdf$', parsed.path, re.I)
                    if filename:
                        try:
                            datetime.strptime(filename.group(2), '%Y%m%d')
                            issuers.add(filename.group(1))
                        except ValueError:
                            pass
                    else:
                        exact = re.match(r'.*/STR/(\d{4,6})(?:/|$)', parsed.path, re.I)
                        if exact:
                            issuers.add(exact.group(1))
                for key in ('url', 'source_url'):
                    next_urls.extend(parse_qs(parsed.query).get(key, []))
            except ValueError:
                continue
        pending = next_urls
    if row.get('document_kind') == 'document_body' and row.get('fetch_status') in ('success', 'ok'):
        body = str(row.get('content') or row.get('snippet') or '')
        issuers.update(re.findall(r'(?:股票代碼|股票代號|證券代號)\s*[:：]\s*(\d{4,6})(?!\d)', body))
    return issuers


def source_matches_company(source: Any, code: str, name: str = '') -> bool:
    row = source_record(source)
    text = str(row.get('title') or '') + '\n' + str(row.get('content') or row.get('snippet') or '')
    issues = source_company_conflicts(row, {'stock': {'code': code},
        'authoritative_company_names': {code: name} if name else {}})
    return bool(code and (re.search(r'(?<!\d)' + re.escape(code) + r'(?!\d)', text)
                          or code in document_issuer_codes(row))) and not any(
        issue.get('kind') != 'possible_abbreviation' for issue in issues)


def parsed_official_sources(data: dict[str, Any], start: int = 1) -> list[Any]:
    """Bind actual parsed official series to citable IDs, never just a homepage."""
    import json
    from .models import SourceItem
    sources = []
    quantitative = data.get('quantitative_market') or {}
    for key in ('official_cash_institutional_flow', 'official_futures_institutional'):
        record = quantitative.get(key) or {}
        url = str(record.get('source') or '')
        if record.get('status') != 'official_public' or not is_official_url(url):
            continue
        if not (record.get('rows') or record.get('tx_futures_rows')):
            continue
        data_date = record.get('data_date')
        sources.append(SourceItem(f'S{start + len(sources):03}', record.get('title') or key,
            url, 'Level 1', published_date=None, data_date=data_date,
            snippet=json.dumps(record, ensure_ascii=False, separators=(',', ':'), default=str),
            document_kind='parsed_record', provider='official_parsed', fetch_status='success',
            found_by=['source_text:provided_content']))
    candidates = [data, *(data.get('ai_candidates') or [])]
    seen = set()
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        stock = candidate.get('stock') or candidate
        if not isinstance(stock, dict):
            continue
        code = str(stock.get('code') or candidate.get('code') or '')
        record = (candidate.get('free_public_sources') or {}).get('valuation') or {}
        url = str(record.get('source') or '')
        rows = record.get('rows') or []
        if not rows and isinstance(record.get('latest'), dict):
            rows = [record['latest']]
        if not code or record.get('status') != 'official_public' or not is_official_url(url):
            continue
        if not rows or any(not isinstance(row, dict) or str(row.get('code') or '') != code for row in rows):
            continue
        identity = (code, url)
        if identity in seen:
            continue
        seen.add(identity)
        sources.append(SourceItem(f'S{start + len(sources):03}', f'{code} 官方估值解析資料',
            url, 'Level 1', published_date=None,
            data_date=record.get('data_date') or (record.get('latest') or {}).get('date'),
            snippet=json.dumps(record, ensure_ascii=False, separators=(',', ':'), default=str),
            document_kind='parsed_record', provider='official_parsed', fetch_status='success',
            found_by=['source_text:provided_content']))
    return sources


def audit_company_mappings(data: dict[str, Any]) -> list[dict[str, Any]]:
    issues = []
    master = data.get("stock_index") or {}
    authoritative = data.get('authoritative_company_names') or {}
    def visit(value: Any, path: str) -> None:
        if isinstance(value, dict):
            code = str(value.get("stock_code") or value.get("company_code") or value.get("code") or "")
            name = value.get("stock_name") or value.get("company_name") or value.get("name")
            known = master.get(code) if isinstance(master, dict) else None
            expected = authoritative.get(code) or ((known.get("name") or known.get("stock_name")) if isinstance(known, dict) else None)
            if expected and name and str(name).strip() != str(expected).strip():
                issues.append({"path": path, "code": code, "name": name, "expected_name": expected,
                    "reason": "名稱可能為簡稱，尚待核對，不代表錯代號" if str(expected).startswith(str(name)) or str(name).startswith(str(expected)) else "代號與主檔名稱不一致"})
            if value.get("usage_policy") == "hypothesis_only" or value.get("verification_status") == "candidate":
                issues.append({"path": path, "code": code, "reason": "候選線索，產品／客戶／營收曝險未核實"})
            for key, item in value.items():
                if key not in {"ai_data_center", "high_model_input_package", "evidence_governance"}:
                    visit(item, f"{path}.{key}")
        elif isinstance(value, list):
            for index, item in enumerate(value):
                visit(item, f"{path}[{index}]")
    visit(data, "data")
    return issues


def review_claim_evidence(markdown: str, sources: list[Any]) -> dict[str, Any]:
    """Flag definite provenance gaps and possible contradictions, not entailment."""
    by_id = {row["source_id"]: row for row in map(source_record, sources) if row.get("source_id")}
    claims = []
    for line in markdown.splitlines():
        refs = list(dict.fromkeys(re.findall(r"\[(S\d+)\]", line)))
        if not refs:
            continue
        kind = "forecast" if re.search(r"預估|預期|可能|若.+則|情境|目標價", line) else "inference" if re.search(r"推論|因此|意味|顯示", line) else "claim"
        issues = []
        for ref in refs:
            source = by_id.get(ref)
            if source is None:
                issues.append("引用不存在的來源")
                continue
            if official_evidence_status(source) == "reference_only":
                issues.append("官方查詢入口不能證明具體結論")
            body = str(source.get("content") or source.get("snippet") or "")
            if not body:
                issues.append("未取得來源內容，僅有網址或標題")
            for url in re.findall(r'https?://[^\s<>\])|]+', line):
                if url.rstrip('。，；,;') != str(source.get('url') or '').rstrip('/') and len(refs) == 1:
                    issues.append('引用編號與行內網址不一致，需核對原始來源')
            # Opposite direction may concern different metrics: require review, never auto-rewrite.
            if re.search(r"(?:營收|獲利|訂單).{0,8}(?:成長|增加|上升)", line) and re.search(r"(?:營收|獲利|訂單).{0,8}(?:衰退|減少|下滑)", body):
                issues.append("來源含相反方向描述，需核對指標、期間與公司")
        claims.append({"text": line.strip(), "kind": kind, "source_ids": refs,
                       "issues": list(dict.fromkeys(issues)), "semantic_status": "requires_review"})
    return {"claims": claims, "flagged_count": sum(bool(row["issues"]) for row in claims),
            "scope": "局部來源與語意衝突提示；未證明無標記主張正確", "semantic_verified": False}
