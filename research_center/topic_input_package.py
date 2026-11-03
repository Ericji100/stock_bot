"""Evidence-preserving stage inputs for official topic maintenance."""
from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any


def intern_topic_payloads(payloads: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Replace only byte-identical repeated JSON values with in-prompt references."""
    counts: Counter[str] = Counter()

    def identity(value):
        if not isinstance(value, (dict, list, str)):
            return None
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        return encoded if len(encoded) >= 128 else None

    def count(value):
        key = identity(value)
        if key is not None:
            counts[key] += 1
        if isinstance(value, dict):
            for item in value.values():
                count(item)
        elif isinstance(value, list):
            for item in value:
                count(item)

    count(payloads)
    records = {}
    keys = {}

    def encode(value):
        key = identity(value)
        if key is not None and counts[key] > 1:
            if key not in keys:
                record_id = f"R{len(keys) + 1:04d}"
                keys[key] = record_id
                records[record_id] = children(value)
            return {"$topic_ref": keys[key]}
        return children(value)

    def children(value):
        if isinstance(value, dict):
            return {key: encode(item) for key, item in value.items()}
        if isinstance(value, list):
            return [encode(item) for item in value]
        return value

    # Reserve this exact object shape so source data cannot be mistaken for a reference.
    def has_reserved_reference(value):
        if isinstance(value, dict):
            return set(value) == {"$topic_ref"} or any(has_reserved_reference(v) for v in value.values())
        if isinstance(value, list):
            return any(has_reserved_reference(v) for v in value)
        return False

    if has_reserved_reference(payloads):
        return payloads, {}
    return {key: encode(value) for key, value in payloads.items()}, records


def lossless_evidence_json(payload: dict[str, Any]) -> str:
    """One reversible envelope shared by topic and report inputs, not a digest."""
    values, records = intern_topic_payloads(payload)
    base = {'values': values, 'shared_records': records}
    def render(value, schemas=None):
        envelope, names = intern_field_names(value)
        envelope['field_names'] = names
        envelope['references_enabled'] = bool(records)
        if schemas:
            envelope['record_schemas'] = schemas
        return json.dumps(envelope, ensure_ascii=False, separators=(',', ':'), default=str)
    text = render(base)
    tabular, schemas = intern_record_tables(base)
    if schemas:
        candidate = render(tabular, schemas)
        if len(candidate.encode('utf-8')) < len(text.encode('utf-8')):
            text = candidate
    # Keep suspicious source glyphs reversible even when the instruction-text
    # sanitizer removes such characters. This is JSON escaping, not repair.
    return re.sub(r'[\ue000-\uf8ff\ufffd]', lambda m: '\\u%04x' % ord(m.group()), text)


def intern_record_tables(payload):
    """Store repeated record keys once; row values and missing/null stay distinct."""
    def reserved(value):
        if isinstance(value, dict):
            return '$records' in value or any(reserved(v) for v in value.values())
        return any(reserved(v) for v in value) if isinstance(value, list) else False
    if reserved(payload):
        return payload, {}
    schemas, identities = {}, {}
    def encode(value):
        if isinstance(value, dict):
            children = {k: encode(v) for k,v in value.items()}
            rows, keys = list(children.values()), list(children)
        elif isinstance(value, list):
            children = [encode(v) for v in value]
            rows, keys = children, None
        else:
            return value
        if len(rows) < 3 or not all(isinstance(r,dict) and r and '$records' not in r for r in rows):
            return children
        proposed, encoded = {}, []
        for row in rows:
            fields = tuple(row)
            schema = identities.get(fields) or proposed.get(fields)
            if schema is None:
                schema = f'Q{len(schemas)+len(proposed)+1:04d}'
                proposed[fields] = schema
            encoded.append([schema, *row.values()])
        table = {'$records': encoded}
        if keys is not None:
            table['$keys'] = keys
        serialize = lambda item: json.dumps(item,ensure_ascii=False,separators=(',', ':'),default=str).encode('utf-8')
        overhead = {schema:list(fields) for fields,schema in proposed.items()}
        if len(serialize(table)) + len(serialize(overhead)) + 32 >= len(serialize(children)):
            return children
        for fields, schema in proposed.items():
            schemas[schema], identities[fields] = list(fields), schema
        return table
    return encode(payload), schemas


def intern_field_names(payload):
    """Reversible dictionary for repeated long JSON field names, not values."""
    counts = Counter()
    def visit(value):
        if isinstance(value, dict):
            counts.update(value.keys())
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
    visit(payload)
    prefix = '~'
    while any(str(key).startswith(prefix) for key in counts):
        prefix += '~'
    names, aliases = {}, {}
    for key, count in sorted(counts.items()):
        if key in {'values', 'shared_records', '$topic_ref', '$records', '$keys', 'source_id', 'company_code', 'stock_code', 'code', 'name', 'url', 'data_date', 'published_date', 'report_date'}:
            continue
        alias = prefix + str(len(names))
        saved = (len(str(key).encode()) - len(alias)) * count
        if count > 1 and saved > len(str(key).encode()) + len(alias) + 8:
            names[alias], aliases[key] = key, alias
    def encode(value):
        if isinstance(value, dict):
            return {aliases.get(k,k): encode(v) for k,v in value.items()}
        return [encode(v) for v in value] if isinstance(value,list) else value
    return encode(payload), names


def expand_evidence_json(envelope):
    """Used by audit/tests to prove exact value roundtrips, never to fabricate facts."""
    def expand(value):
        if isinstance(value, dict):
            if set(value) == {'$topic_ref'} and envelope.get('references_enabled', True):
                return expand(envelope['shared_records'][value['$topic_ref']])
            if envelope.get('record_schemas') and set(value) in ({'$records'}, {'$records','$keys'}):
                rows = [dict(zip(envelope['record_schemas'][row[0]], (expand(v) for v in row[1:]), strict=True))
                        for row in value['$records']]
                rows = [expand(row) if set(row) == {'$topic_ref'} else row for row in rows]
                return dict(zip(value['$keys'], rows, strict=True)) if '$keys' in value else rows
            return {envelope.get('field_names', {}).get(k,k): expand(v) for k,v in value.items()}
        return [expand(v) for v in value] if isinstance(value,list) else value
    return expand(envelope['values'])


def append_topic_shared_records(prompt: str, variables: dict[str, str]) -> str:
    from .reader_quality_rules import reader_quality_rules
    prompt += '\n\n' + reader_quality_rules('topic_maintain')
    if variables.get('topic_field_names_json'):
        prompt += '\n全局欄位名稱字典：同代碼等於其原欄位名，閱讀時須展開，不可把代碼寫入正式輸出。\n' + variables['topic_field_names_json']
    prompt += '\n\n本批日期與證據檢核：\n' + variables.get('evidence_governance_json', '{}')
    records = variables.get("topic_shared_records_json", "{}")
    if records == "{}":
        return prompt
    return prompt + (
        '\n\n## 本批完整共用資料（精確去重，非摘要）\n'
        '任何 {"$topic_ref":"R編號"} 都等同於下方相同編號的完整值，必須展開理解；'
        '可含巢狀引用。引用不是資料缺漏，不可跳過。每個不同主張、日期與反證均保留，'
        '同網址的不同內容不視為相同證據。只回傳本次分析結果，不抄回整份共用表。\n'
        f'```json\n{records}\n```'
    )


def merge_topic_sources(data: dict[str, Any]) -> list[dict]:
    merged=[]
    for key in ('discovery_sources','base_sources','web_fetched_sources'):
        for row in data.get(key) or []:
            if not isinstance(row,dict):
                continue
            identity=row.get('url') or row.get('source_id') or json.dumps(row,sort_keys=True,default=str)
            body=str(row.get('content') or row.get('snippet') or '')
            def compatible(previous):
                old_id=previous.get('url') or previous.get('source_id') or json.dumps(previous,sort_keys=True,default=str)
                if old_id != identity:
                    return False
                if any(previous.get(key) and row.get(key) and previous[key] != row[key]
                       for key in ('published_date','data_date','period_end','updated_date')):
                    return False
                old_body=str(previous.get('content') or previous.get('snippet') or '')
                return not old_body or not body or old_body in body or body in old_body
            previous=next((item for item in merged if compatible(item)), None)
            if previous is None:
                previous={}
                merged.append(previous)
            aliases=list(dict.fromkeys([*(previous.get('source_id_aliases') or []),
                                      *(row.get('source_id_aliases') or []),
                                      *([previous['source_id']] if previous.get('source_id') else []),
                                      *([row['source_id']] if row.get('source_id') else [])]))
            for field,value in row.items():
                if value is not None and value!='' and value!=[]:
                    if field in {'source_id','published_date','source_level'} and previous.get(field):
                        continue
                    if field in {'snippet','content'} and previous.get(field) and str(value) in str(previous[field]):
                        continue
                    previous[field]=value
            if aliases:
                previous['source_id_aliases']=aliases
    for row in merged:
        body=row.get('content')
        if isinstance(body,str) and isinstance(row.get('snippet'),str) and row['snippet'] in body:
            row['snippet']=None
    return merged


def topic_source_references(value):
    urls=set(_references(value))
    ids=set()
    def visit(item, reference=False):
        if isinstance(item,dict):
            for key,child in item.items():
                visit(child, key in {'source_id','source_refs','evidence_source_ids','source_id_aliases'})
        elif isinstance(item,list):
            for child in item:
                visit(child,reference)
        elif reference and isinstance(item,str):
            ids.update(re.findall(r'(?<![A-Za-z0-9])S\d+(?!\d)',item))
    visit(value)
    return urls,ids


def referenced_topic_sources(candidates, sources):
    urls,ids=topic_source_references(candidates)
    return [row for row in sources if row.get('url') in urls or row.get('source_id') in ids or ids.intersection(row.get('source_id_aliases') or [])]


def _references(value):
    if isinstance(value,dict):
        for key,item in value.items():
            if key in {'url','source_url'} and isinstance(item,str):
                yield item
            else:
                yield from _references(item)
    elif isinstance(value,list):
        for item in value:
            yield from _references(item)
    elif isinstance(value,str):
        yield from re.findall(r'https?://[^\s"<>]+',value)


def topic_stage_variables(data,variables,batch=None,*,source_rows=None):
    result=dict(variables)
    sources=merge_topic_sources(data) if source_rows is None else source_rows
    from datetime import date
    from .evidence_governance import build_evidence_governance
    raw_date = data.get('report_date')
    try:
        report_date = date.fromisoformat(str(raw_date)[:10]) if raw_date else None
    except ValueError:
        report_date = None
    profiles=data.get('existing_topic_profiles') or []
    evidence=data.get('webfetch_evidence') or {}
    if batch is None:
        # Full historical profiles remain in the local deduper and matching detail stage.
        profiles=[{k:p.get(k) for k in ('theme_id','theme_name','keywords','industries')} for p in profiles if isinstance(p,dict)]
        knowledge={'scope':'公司完整底稿在相關題材細節階段提供；候選階段以本次完整來源為準。'}
        company_map={}
        nodes=[]
        scan_rows=[{k:row.get(k) for k in ('code','name','scan_type','report_date','score','strategy_signals')} for row in data.get('recent_scan_candidates') or [] if isinstance(row,dict)]
    else:
        urls,source_ids=topic_source_references(batch)
        codes=set()
        ids=set()
        terms=set()
        for candidate in batch:
            ids.update(str(candidate.get(k) or '') for k in ('theme_id','target_theme_id'))
            if candidate.get('theme_name'):
                terms.add(str(candidate['theme_name']))
            for company in candidate.get('candidate_companies') or []:
                if isinstance(company,dict):
                    if company.get('company_code'): codes.add(str(company['company_code']))
                    if company.get('company_name'): terms.add(str(company['company_name']))
        terms.update(codes)
        def related(row):
            if isinstance(row, dict) and (row.get('source_id') in source_ids or source_ids.intersection(row.get('source_id_aliases') or [])):
                return True
            if any(url in urls for url in _references(row)):
                return True
            text=json.dumps(row,ensure_ascii=False,default=str)
            return any(term and term in text for term in terms)
        sources=[row for row in sources if related(row)]
        profiles=[p for p in profiles if p.get('theme_id') in ids or p.get('theme_name') in terms]
        if isinstance(evidence,dict) and isinstance(evidence.get('items'),list):
            evidence={**evidence,'items':[row for row in evidence['items'] if related(row)]}
        raw_knowledge=data.get('company_knowledge') or {}
        companies=raw_knowledge.get('companies') or {}
        knowledge={'companies':{code:info for code,info in companies.items() if str(code) in codes or info.get('company_name') in terms}}
        company_map=data.get('company_topic_map') or {}
        company_map={code:row for code,row in company_map.items() if str(code) in codes} if isinstance(company_map,dict) else [row for row in company_map if related(row)]
        nodes=[row for row in data.get('supply_chain_nodes') or [] if row.get('theme_id') in ids or str(row.get('company_code')) in codes]
        scan_rows=[row for row in data.get('recent_scan_candidates') or [] if str(row.get('code')) in codes]
    payloads={'discovery_sources_json':sources,'web_fetched_sources_json':[],
        'webfetch_evidence_json':evidence,'existing_topic_profiles_json':profiles,
        'company_knowledge_json':knowledge,'company_topic_map_json':company_map,'supply_chain_nodes_json':nodes,
        'recent_scan_candidates_json':scan_rows,'market_signals_json':data.get('market_signals') or {},
        'external_topic_source_caches_json':(data.get('external_topic_source_caches') or {}) if batch is None else {},'low_model_digest_json':{}}
    if batch is not None:
        payloads['topic_candidates_json']=batch
    # Both templates receive the same unique full source rows, never duplicated body dumps.
    if batch is not None:
        payloads['web_fetched_sources_json']=payloads['discovery_sources_json']
        payloads['discovery_sources_json']=[]
    payloads['evidence_governance_json'] = build_evidence_governance('topic_maintain', report_date, sources, data)
    payloads, records = intern_topic_payloads(payloads)
    envelope, names = intern_field_names({'values': payloads, 'shared_records': records})
    payloads, records = envelope['values'], envelope['shared_records']
    result['topic_field_names_json'] = json.dumps(names, ensure_ascii=False, separators=(',', ':'))
    for key,value in payloads.items():
        result[key]=json.dumps(value,ensure_ascii=False,separators=(',', ':'),default=str)
    result['topic_shared_records_json']=json.dumps(records,ensure_ascii=False,separators=(',', ':'),default=str)
    return result
