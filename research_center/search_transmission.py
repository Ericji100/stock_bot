"""Lossless search text transport, separate from verified source evidence."""
from __future__ import annotations

import json


def merge_search_text(first, second):
    first, second = str(first or ''), str(second or '')
    if not second or second in first:
        return first
    if not first or first in second:
        return second
    return first + '\n\n' + second


def merge_search_records(first, second):
    records, seen = [], set()
    for row in [*(first or []), *(second or [])]:
        identity = json.dumps(row, ensure_ascii=False, sort_keys=True)
        if identity not in seen:
            records.append(row)
            seen.add(identity)
    return records


def retain_search_provenance(previous, replacement):
    """Rebuilding a derived packet must not erase discovery diagnostics."""
    result = dict(replacement)
    for key in ('search_context_notes', 'search_attempts'):
        records = merge_search_records((previous or {}).get(key), result.get(key))
        if records:
            result[key] = records
    return result


def collect_search_attempts(data, report_date):
    records = []
    for key in ('deepseek_search_discovery', 'required_gap_deepseek_discovery',
                'minimax_diagnostics', 'minimax_search_discovery'):
        diagnostic = (data or {}).get(key)
        if not isinstance(diagnostic, dict):
            continue
        record = {name: diagnostic[name] for name in
                  ('provider', 'status', 'stop_reason', 'runs', 'critical_gap_checks') if name in diagnostic}
        if record:
            records.append({'analysis_date': str(report_date), **record})
    return merge_search_records([], records)


def source_events_packet(sources):
    by_url = {}
    metadata = {}
    for source in sources:
        url = source.get('url') if isinstance(source, dict) else source.url
        events = source.get('search_summary_events', []) if isinstance(source, dict) else source.search_summary_events
        if events:
            by_url[url] = merge_search_records(by_url.get(url), events)
            metadata.setdefault(url, {key: source.get(key) if isinstance(source, dict) else getattr(source, key)
                                      for key in ('published_date', 'date_status', 'data_date', 'updated_date')})
    return [{'url': url, 'events': events, 'verified': False, **metadata[url]} for url, events in by_url.items()]


def collect_search_context(data, sources=None, report_date=None):
    """Read only explicit search diagnostics, never treat orphan notes as sources."""
    notes = []
    seen = set()
    eligible_urls = None if sources is None else {s.get('url') if isinstance(s, dict) else s.url for s in sources}
    cutoff = str(report_date or (data or {}).get('report_date') or '')[:10]
    for key in ('search_context_notes', 'deepseek_search_discovery', 'required_gap_deepseek_discovery',
                'minimax_diagnostics', 'minimax_search_discovery'):
        value = (data or {}).get(key)
        rows = value if isinstance(value, list) else value.get('search_context_notes', []) if isinstance(value, dict) else []
        if isinstance(value, dict) and eligible_urls is not None:
            rows = list(rows)
            for row in value.get('search_event_inventory', []):
                from .evidence_governance import source_company_conflicts
                issues = source_company_conflicts(row, data or {})
                if issues:
                    rows.append({'kind': 'company_issuer_conflict', 'verified': False,
                                 'reason': '發行公司與目標公司不符；只作待核背景，不得支持目標公司的主張。',
                                 'company_mapping_issues': issues, **row})
                if row.get('url') in eligible_urls:
                    continue
                from .source_date_normalizer import normalize_published_date
                future = cutoff and any(day and day > cutoff for day in
                    (normalize_published_date(row.get(k)) for k in ('published_date', 'data_date', 'updated_date')))
                if future:
                    # Keep the record in diagnostics/archive, not historical AI input.
                    rows.append({'kind': 'after_cutoff_source_excluded', 'verified': False,
                                 'url': row.get('url'), 'event_count': len(row.get('events') or []),
                                 'reason': '已知日期晚於分析日；原事件保留於搜尋診斷，不送入歷史分析。'})
                else:
                    rows.append({'kind': 'source_not_in_analysis_sources', 'verified': False,
                                 'reason': '未列入本次合格來源，僅保留待查底稿；不可據此宣稱日期或主張已驗證。', **row})
        for row in rows:
            if isinstance(row, dict):
                from .evidence_governance import source_company_conflicts
                embedded = row.get('record') or row.get('source') or row
                issues = source_company_conflicts(embedded, data or {}) if isinstance(embedded, dict) else []
                if issues:
                    row = {**row, 'verified': False,
                           'company_mapping_issues': merge_search_records(row.get('company_mapping_issues'), issues),
                           'company_usage': '公司／發行公司仍需核對；本底稿不得支持目標公司的已核實主張。'}
            identity = json.dumps(row, ensure_ascii=False, sort_keys=True)
            if identity not in seen:
                notes.append(row)
                seen.add(identity)
    return notes


def search_context_prompt(notes):
    if not notes:
        return ''
    return ('\n\n搜尋限制與待查資料（不是已驗證來源，不可增加來源數或視為已確認事實；'
            '保留缺口、矛盾及後續查證條件）：\n' + json.dumps(notes, ensure_ascii=False))
