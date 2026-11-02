"""Evidence-preserving stage inputs for official topic maintenance."""
from __future__ import annotations

import json
import re
from typing import Any


def merge_topic_sources(data: dict[str, Any]) -> list[dict]:
    merged={}
    for key in ('discovery_sources','base_sources','web_fetched_sources'):
        for row in data.get(key) or []:
            if not isinstance(row,dict):
                continue
            identity=row.get('url') or row.get('source_id') or json.dumps(row,sort_keys=True,default=str)
            previous=merged.setdefault(identity,{})
            for field,value in row.items():
                if value is not None and value!='' and value!=[]:
                    if field in {'source_id','published_date','source_level'} and previous.get(field):
                        continue
                    previous[field]=value
    for row in merged.values():
        body=row.get('content')
        if isinstance(body,str) and isinstance(row.get('snippet'),str) and row['snippet'] in body:
            row['snippet']=None
    return list(merged.values())


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


def topic_stage_variables(data,variables,batch=None):
    result=dict(variables)
    sources=merge_topic_sources(data)
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
        urls=set(_references(batch))
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
    for key,value in payloads.items():
        result[key]=json.dumps(value,ensure_ascii=False,default=str)
    # Both templates receive the same unique full source rows, never duplicated body dumps.
    if batch is not None:
        result['web_fetched_sources_json']=result['discovery_sources_json']
        result['discovery_sources_json']='[]'
    return result
