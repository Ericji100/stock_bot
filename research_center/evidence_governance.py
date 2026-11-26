"""Shared mechanical evidence checks; never an investment or truth classifier."""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from functools import lru_cache
import hashlib
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


def company_identity_context(data: dict[str, Any] | None = None) -> dict[str, Any]:
    from .config import ROOT_DIR
    data = data or {}
    path = ROOT_DIR / 'stock_list.json'
    names = dict(_company_master(str(path), path.stat().st_mtime_ns if path.exists() else 0))
    names.update(data.get('authoritative_company_names') or {})
    for code, row in (data.get('stock_index') or {}).items() if isinstance(data.get('stock_index'), dict) else []:
        if isinstance(row, dict) and (row.get('name') or row.get('stock_name')):
            names.setdefault(str(code), row.get('name') or row.get('stock_name'))
    stock = data.get('stock') or data.get('candidate') or {}
    stock = stock if isinstance(stock, dict) else {}
    from .entity_resolver import company_aliases_from_sources
    aliases = company_aliases_from_sources({**data, 'authoritative_company_names': names})
    return {**data, 'stock': stock, 'authoritative_company_names': names, 'company_aliases': aliases}


@lru_cache(maxsize=8)
def _company_master(path: str, modified: int) -> tuple:
    from pathlib import Path
    from .entity_resolver import _load_stock_universe
    return tuple((str(row['code']), row['name']) for row in _load_stock_universe(Path(path).parent)
                 if row.get('code') and row.get('name'))


def _source_text(row: dict[str, Any]) -> str:
    return '\n'.join(dict.fromkeys(str(row.get(key) or '') for key in
        ('title', 'content', 'snippet', 'summary', 'full_text', 'text') if row.get(key)))


@lru_cache(maxsize=16)
def _company_labels(names: tuple, aliases: tuple):
    labels = {}
    for code, name in names:
        if name and len(name) >= 2:
            labels.setdefault(name, set()).add(code)
    for code, values in aliases:
        for name in values:
            if len(name) >= 2:
                labels.setdefault(name, set()).add(code)
    pattern = '|'.join(re.escape(name) for name in sorted(labels, key=len, reverse=True))
    return re.compile(pattern or r'(?!)'), labels


def _label_matcher(data):
    return _company_labels(tuple(sorted((data.get('authoritative_company_names') or {}).items())),
        tuple(sorted((code, tuple(values)) for code, values in (data.get('company_aliases') or {}).items())))


def company_source_admission(source: Any, data: dict[str, Any]) -> dict[str, Any]:
    row = source_record(source)
    issues = source_company_conflicts(row, data)
    hard = [issue for issue in issues if issue['kind'] != 'possible_abbreviation']
    if 'company_identity_conflict' in (row.get('tags') or []):
        persisted = {'kind': 'persisted_identity_conflict', 'usage': '原來源仍列身分衝突，未經核對不得恢復。'}
        issues.append(persisted)
        hard.append(persisted)
    stock = data.get('stock') or {}
    code = str(stock.get('code') or '')
    name = str(stock.get('name') or (data.get('authoritative_company_names') or {}).get(code) or '')
    text = _source_text(row)
    direct = bool(code and (re.search(r'(?<!\d)' + re.escape(code) + r'(?!\d)', text)
                            or code in document_issuer_codes(row)))
    if not direct and name:
        matcher, labels = _label_matcher(data)
        direct = any(labels[match.group()] == {code} for match in matcher.finditer(text))
    pending = bool(issues) and not hard
    status = 'quarantined' if hard else 'identity_pending' if pending else 'context_only' if code and not direct else 'eligible'
    return {'status': status, 'company_code': code or None, 'issues': issues,
            'supports_target_company': not hard and not pending and (direct or not code),
            'semantic_verified': False}


def company_safe_prompt_data(value: Any, data: dict[str, Any] | None = None) -> Any:
    """Keep raw inputs untouched; conflicting evidence is a pointer, never a fact packet."""
    context = company_identity_context(data)
    def visit(item, current):
        if isinstance(item, list):
            return [visit(row, current) for row in item]
        if not isinstance(item, dict):
            if isinstance(item, str) and re.search(r'[（(]\s*\d{4,6}|(?<!\d)\d{4,6}[ \t：:]*[\u4e00-\u9fffA-Za-z]', item):
                gate = company_source_admission({'content': item}, current)
                if gate['status'] == 'quarantined':
                    return '公司身分衝突；原文留存，不作個股事實。SHA256：' + hashlib.sha256(item.encode()).hexdigest()
            return item
        stock = item.get('stock') or item.get('candidate')
        if isinstance(stock, dict) and stock.get('code'):
            current = {**current, 'stock': stock}
        is_source = bool(item.get('url') or item.get('source_url')) and any(
            item.get(key) for key in ('title', 'content', 'snippet', 'summary', 'text', 'full_text'))
        inherited = (current.get('source_identity_issues') or {}).get(str(item.get('source_id') or ''))
        admission = company_source_admission(item, current) if is_source else None
        audit_row = {'text_chars', 'text_sha256'} <= item.keys()
        if (inherited and not audit_row) or (admission and admission['status'] == 'quarantined'):
            return {'source_id': item.get('source_id') or item.get('id'),
                'url': item.get('url') or item.get('source_url'),
                '資料用途': '公司身分衝突，原文保留於來源檔；不可支持個股主張或評分。',
                '身分問題': inherited or admission['issues'],
                '原始紀錄SHA256': hashlib.sha256(__import__('json').dumps(item, ensure_ascii=False,
                    sort_keys=True, default=str).encode()).hexdigest()}
        result = {}
        isolated_fields = []
        for key, row in item.items():
            if not is_source and key in {'summary', 'snippet', 'content', 'text', 'full_text', '文字'} and isinstance(row, str):
                field_gate = company_source_admission({'content': row}, current)
                if field_gate['status'] == 'quarantined':
                    result[key] = '公司身分衝突；原文留存，不作個股事實。'
                    isolated_fields.append({'field': key, 'issues': field_gate['issues'],
                        'sha256': hashlib.sha256(row.encode()).hexdigest()})
                    continue
            result[key] = visit(row, current)
        if isolated_fields:
            result['公司身分隔離'] = isolated_fields
        if admission and admission['status'] == 'context_only':
            matcher, _ = _label_matcher(current)
            if matcher.search(_source_text(item)):
                result['證據用途'] = '其他公司背景，不能支持目標公司的主張。'
        if admission and admission['status'] == 'identity_pending':
            result['證據用途'] = '公司名稱尚待核對，不能視為公司身分已確認。'
        return result
    return visit(value, context)


def official_source_record(source: Any, data: dict[str, Any], report_date: date) -> dict[str, Any]:
    row = source_record(source)
    from .source_date_normalizer import source_temporal_record
    reference_label = row.get('source_type') == 'official_basis' or row.get('provider') in {'local_official_cache', 'official_reference'}
    temporal_row = {**row, 'published_date': None, 'date_status': 'unknown'} if reference_label else row
    temporal = source_temporal_record(temporal_row, report_date)
    admission = company_source_admission(row, data)
    status = official_evidence_status(row)
    retrieved = status == 'retrieved_requires_claim_check'
    body = str(row.get('content') or row.get('snippet') or '')
    return {'source_id': row.get('source_id'), 'url': row.get('url') or row.get('source_url'),
        'provider': row.get('provider'), 'fetch_provider': row.get('fetch_provider'),
        'document_kind': row.get('document_kind'), 'fetch_status': row.get('fetch_status'),
        'published_date': temporal['published_date'], 'publication_status': temporal['publication_status'],
        'data_date': row.get('data_date'), 'period_start': row.get('period_start'),
        'period_end': row.get('period_end'), 'fetched_at': row.get('fetched_at'),
        'official_evidence_status': status, 'company_admission': admission,
        'parsed_official_data_retrieved': retrieved and row.get('document_kind') == 'parsed_record',
        'official_document_body_retrieved': retrieved and row.get('document_kind') == 'document_body',
        'usable_by_report_date': row.get('date_status') == 'explicit' and temporal['publication_status'] == 'explicit'
            and temporal['available_by_report_date'] is True and not temporal['data_after_report_date']
            and not temporal['content_updated_after_report_date'],
        'body_sha256': hashlib.sha256(body.encode()).hexdigest() if retrieved else None,
        'body_chars': len(body) if retrieved else 0, 'claims_verified': False}


def official_evidence_status(source: Any) -> str:
    row = source_record(source)
    try:
        host = (urlsplit(str(row.get("url") or row.get("source_url") or "")).hostname or "").lower()
    except ValueError:
        return 'invalid_url'
    if not is_official_url(str(row.get('url') or row.get('source_url') or '')):
        return "non_official"
    status = (row.get("payload") or {}).get("status") if isinstance(row.get("payload"), dict) else None
    if row.get('source_type') == 'official_basis' or row.get('provider') in {'local_official_cache', 'official_reference'}:
        return 'reference_only'
    if row.get("document_kind") in {"reference_link", "unavailable"} or status in {"reference_link", "reachable", "unavailable"}:
        return "reference_only"
    if row.get('fetch_status') in {'failed', 'error', 'unavailable'}:
        return 'official_origin_unverified_content'
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
    checked_data = company_identity_context(checked_data)
    records = []
    warnings = []
    for source in sources:
        record = source_temporal_record(source, target)
        record["official_evidence_status"] = official_evidence_status(source)
        record['company_mapping_issues'] = source_company_conflicts(source, checked_data)
        record['source_provenance'] = official_source_record(source, checked_data, target)
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
    text = _source_text(row)
    issues = []
    for match in re.finditer(r'([\u4e00-\u9fffA-Za-z]{2,128}(?:-[A-Za-z]{2,4})?)\s*[（(]\s*(\d{4,6})\s*[)）]', text):
        name, code = match.groups()
        expected = names.get(code)
        if any(name.endswith(alias) for alias in (data.get('company_aliases') or {}).get(code, [])):
            continue
        if expected and not (name.endswith(str(expected)) or name.startswith(str(expected) + '股份')):
            abbreviation = str(expected).startswith(name) or name.startswith(str(expected))
            issues.append({'code': code, 'stated_name': name, 'expected_name': str(expected),
                'kind': 'possible_abbreviation' if abbreviation else 'name_mismatch',
                'usage': '名稱可能為簡稱，列待核而非已確認錯代號' if abbreviation else '公司代號與名稱衝突；原句僅作待核線索'})
    from .entity_resolver import exact_company_identity
    matcher, labels = _label_matcher({**data, 'authoritative_company_names': names})
    for match in re.finditer(r'(?<!\d)(\d{4,6})(?!\d)[ \t：:]*([\u4e00-\u9fffA-Za-z]{2,128}(?:-[A-Za-z]{2,4})?)', text):
        code, stated = match.groups()
        if code not in names:
            continue
        if 1900 <= int(code) <= 2099 and code != str((data.get('stock') or {}).get('code') or '') and not re.search(r'股票|證券|代號|股號', text[max(0,match.start()-8):match.start()]):
            continue  # An outlook year without a stock-code context is not an issuer.
        label = matcher.match(stated)
        if not label:
            continue
        known = label.group()
        if labels[known] == {code}:
            continue
        if (('股份' in stated[len(known):] or '有限公司' in stated[len(known):])
                and str(names[code]).startswith(known)):
            issues.append({'code':code, 'stated_name':stated, 'expected_name':names[code],
                'kind':'possible_abbreviation', 'usage':'同前綴法定名稱缺官方別名證據，保留待核而非視為身分已確認。'})
            continue
        identity = exact_company_identity(known, code, names, data.get('company_aliases'))
        if identity['status'] == 'conflict':
            issue = {'code': code, 'stated_name': known, 'expected_name': names[code],
                     'kind': 'name_mismatch', 'usage': '公司代號與名稱衝突；原句僅作待核線索'}
            if issue not in issues:
                issues.append(issue)
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
    text = _source_text(row)
    issues = source_company_conflicts(row, {'stock': {'code': code},
        'authoritative_company_names': {code: name} if name else {}})
    return bool(code and (re.search(r'(?<!\d)' + re.escape(code) + r'(?!\d)', text)
                          or code in document_issuer_codes(row))) and not issues


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
