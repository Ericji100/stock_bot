"""Batch-scoped evidence choices and one bounded reference-only correction."""
from copy import deepcopy
import hashlib
import json

from .topic_quote_binding import quote_catalog, bind_quote_reference, _rows


def quote_choices(catalog, scope):
    identity = json.dumps([scope, [s['quote_id'] for s in catalog]], ensure_ascii=False)
    prefix = 'B' + hashlib.sha256(identity.encode()).hexdigest()[:8]
    return [{**row, 'quote_choice': f'{prefix}-{index:03d}'} for index, row in enumerate(catalog, 1)]


def _catalog(data, variables):
    from .topic_input_package import merge_topic_sources
    sources = merge_topic_sources(data)
    supplied = {s['quote_id']: s for s in quote_catalog(sources)}
    metadata = json.loads(variables.get('topic_quote_catalog_json', '[]'))
    spans = {s['quote_id']: supplied[s['quote_id']] for s in metadata if s['quote_id'] in supplied}
    choices = {s['quote_choice']: spans[s['quote_id']] for s in metadata
               if s.get('quote_choice') and s['quote_id'] in spans}
    return sources, spans, choices


def _check(payload, data, batch, variables):
    from .topic_models import TopicChangePack
    from .topic_schema_normalizer import normalize_topic_detail_actions, resolve_topic_company_identities
    from .topic_quality import normalize_change_pack_quality
    from .topic_quote_binding import bind_topic_quotes
    from .topic_pipeline_service import _hydrate_topic_evidence
    from .topic_citation_audit import (audit_topic_citations, restore_topic_source_quotes,
        quarantine_topic_source_leads, quarantine_unproven_company_claims)
    from .entity_resolver import company_aliases_from_sources

    result = deepcopy(payload)
    root = {'actions': result} if isinstance(result, list) else result
    sources, spans, choices = _catalog(data, variables)
    issues, bindings = [], []
    for path, row in _rows(root):
        choice = row.get('quote_choice')
        canonical = row.get('quote_id')
        has_choice = 'quote_choice' in row
        if not has_choice and not canonical:
            continue
        span = (choices.get(choice) if isinstance(choice, str) and choice
                else spans.get(canonical) if isinstance(canonical, str) else None)
        reason = None
        if has_choice and (not isinstance(choice,str) or not choice or not span):
            reason = 'quote_choice_invalid'
        elif not span or canonical and canonical != span['quote_id']:
            reason = 'quote_reference_mismatch'
        elif has_choice and row.get('supporting_quote') not in (None, '', span['text']):
            reason = 'quote_choice_conflict'
        if reason:
            issues.append({'path':path, 'reason':reason})
            continue
        original = deepcopy(row)
        if not bind_quote_reference(row, span, sources):
            issues.append({'path':path, 'reason':'quote_reference_mismatch'})
            continue
        row['quote_id'] = span['quote_id']
        row.pop('quote_choice', None)
        bindings.append({'path':path, 'quote_id':span['quote_id'],
            'body_sha256':span['body_sha256'], 'original':original})
    actions = root.get('actions', []) if isinstance(root, dict) else []
    pack = TopicChangePack.from_dict({'change_id':'batch-check','created_at':str(data.get('report_date') or ''),
        'actions':[], 'company_knowledge_updates':root.get('company_knowledge_updates', {}) if isinstance(root,dict) else {}})
    pack.actions = normalize_topic_detail_actions(actions, fallback_candidates=batch or [])
    bind_topic_quotes(pack, data)
    _hydrate_topic_evidence(pack, data)
    resolve_topic_company_identities(pack.actions, data.get('authoritative_company_names') or {}, company_aliases_from_sources(data))
    normalize_change_pack_quality(pack)
    restore_topic_source_quotes(pack, data)
    quarantine_topic_source_leads(pack, data)
    quarantine_unproven_company_claims(pack, data)
    audit = audit_topic_citations(pack, data)
    unique = {json.dumps(issue,sort_keys=True,ensure_ascii=False):issue for issue in [*issues,*audit['issues']]}
    audit.update(issues=list(unique.values()), passed=not unique, blocking_count=len(unique), bindings=bindings)
    return result, audit


def _repair_request(payload, audit, data, variables):
    root = {'actions':payload} if isinstance(payload,list) else payload
    rows = dict(_rows(root))
    paths = sorted({issue['path'] for issue in audit['issues'] if issue['path'] in rows
        and issue['reason'] in {'quote_reference_mismatch','quote_choice_invalid','quote_choice_conflict',
                               'supporting_quote_not_in_body','supporting_quote_missing'}})
    if not paths:
        return None
    sources, _, choices = _catalog(data,variables)
    requests = []
    for path in paths:
        row = rows[path]
        url = row.get('url') or row.get('source_url')
        ref = row.get('source_id')
        if url and not isinstance(url,str) or ref and not isinstance(ref,str):
            return None
        from .source_identity import reference_sources, source_url
        matched = reference_sources(ref,sources,url=url,
            published_date=row.get('publish_date') or row.get('published_date')) if ref else []
        if url:
            matched = [s for s in sources if source_url(s.get('url'))==source_url(url)
                and (not ref or s in matched)]
        ids = {s['source_id'] for s in matched}
        available = {key:span for key,span in choices.items() if not (ref or url) or span['source_id'] in ids}
        if not available:
            return None
        owner = '/'.join(path.split('/')[:3]) if path.startswith('/actions/') else ''
        context = rows.get(owner, {})
        requests.append({'path':path, 'original_reference':row,
            'analysis_context':{k:context.get(k) for k in ('theme_id','reason','risk_notes','counter_evidence','missing_data')},
            'allowed_choices':available})
    return requests


def check_batch_evidence(payload, *, data, batch, variables, stage, call_ai_json, emit):
    """Retain every action; repair only explicit reference rows, never analysis."""
    bound, audit = _check(payload,data,batch,variables)
    record = {'stage':stage+'_evidence_check', **audit, 'retry_count':0}
    if audit['passed']:
        return bound, record
    requests = _repair_request(bound,audit,data,variables)
    record['initial_issues'] = deepcopy(audit['issues'])
    if not requests:
        record['repair_status'] = 'no_exact_source_choices'
        return bound, record
    prompt = ('本次只修復引用選取，不重做研究、不更改主張或公司身分。原分析完整保留在程式中。'
        '只從每筆 allowed_choices 選擇直接支持原主張與反證的 quote_choice；不能借用別筆或別批。'
        '網址、日期、公司身分矛盾不能靠換引句掩蓋。無合適證據時回傳空 repairs，保留待查。'
        '只輸出 {"repairs":[{"path":"原樣路徑","quote_choice":"合法選項"}]}，不得新增其他欄位，'
        '不得輸出引句、網址、日期、評分或新的分析。程式回填不可變原文並重新嚴格檢核。\n'
        + json.dumps(requests,ensure_ascii=False))
    from .ai_execution_control import require_work_time, CONTROL
    from .deepseek_service import DeepSeekUnavailable
    try:
        require_work_time(120,reason='insufficient_evidence_repair_time')
        emit(f'本批引用待修正 {len(requests)} 筆，僅重選引用一次，不重跑題材或搜尋。')
        record['retry_count'] = 1
        response = call_ai_json(prompt,stage+'_evidence_retry')
        if not isinstance(response,dict) or set(response)!={'repairs'} or not isinstance(response['repairs'],list):
            raise ValueError('repair_schema_invalid')
        expected = {r['path']:r['allowed_choices'] for r in requests}
        replacements = {}
        for row in response['repairs']:
            if not isinstance(row,dict) or set(row)!={'path','quote_choice'}:
                raise ValueError('repair_schema_invalid')
            path, choice = row['path'], row['quote_choice']
            if not isinstance(path,str) or path not in expected or path in replacements or not isinstance(choice,str) or choice not in expected[path]:
                raise ValueError('repair_outside_allowed_choices')
            replacements[path] = choice
        if set(replacements)!=set(expected):
            raise ValueError('repair_incomplete')
        corrected = deepcopy(bound)
        root = {'actions':corrected} if isinstance(corrected,list) else corrected
        rows = dict(_rows(root))
        for path, choice in replacements.items():
            row = rows[path]
            for key in ('quote_id','quote_choice','supporting_quote'):
                row.pop(key,None)
            row['quote_choice'] = choice
        corrected, checked = _check(corrected,data,batch,variables)
        record.update(checked,repair_status='recovered' if checked['passed'] else 'still_blocked')
        record['repair_response'] = deepcopy(response)
        return corrected, record
    except DeepSeekUnavailable as exc:
        if control := CONTROL.get():
            control.stop()
        record.update(repair_status='stopped',stop_reason=exc.status)
    except (ValueError,TypeError,KeyError) as exc:
        record.update(repair_status='invalid_response',repair_error=str(exc))
    return bound, record
