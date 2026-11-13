"""Shared official DeepSeek Search/Fetch adapters with date-aware cache keys."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime, timedelta
import hashlib
import json
import re
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from .deepseek_service import DeepSeekService, DeepSeekUnavailable, TASK
from .models import SourceItem
from .source_rank import make_source_items
from .source_text_cleaner import clean_source_title
from .preferred_sources import match_preferred_source
from .search_transmission import source_events_packet


_NEWS_SEED_SOURCES = ContextVar('scheduled_news_seed_sources', default=())


@contextmanager
def scheduled_news_seed_sources(sources):
    """Pass saved news through the existing planner without changing other commands."""
    token = _NEWS_SEED_SOURCES.set(tuple(sources))
    try:
        yield
    finally:
        _NEWS_SEED_SOURCES.reset(token)


@dataclass(frozen=True)
class DiscoveryResult:
    sources: list[SourceItem]
    diagnostics: dict[str, Any]


def _news_task_quality(rows, policy, as_of):
    """Judge fresh substantive article coverage, not the raw search-result count."""
    from .news_models import NewsItem
    from .news_service import _is_non_article_page, _correct_news_category
    from .source_date_normalizer import normalize_published_date
    fresh = []
    trusted = 0
    official = 0
    url_dated = 0
    domains = set()
    seen = set()
    terms = [str(term).casefold() for term in policy.get('terms', []) if term]
    for row in rows:
        url = str(row.get('url') or '')
        from .source_text_cleaner import source_body_text, article_text_sections
        primary, _ = article_text_sections(source_body_text(row))
        text = f"{row.get('title') or ''} {row.get('snippet') or ''} {primary}"
        day = normalize_published_date(row.get('published_date'))
        url_day = normalize_published_date(url) if not day else None
        day = day or url_day
        if (not url or url in seen or not day or not as_of-timedelta(days=1) <= date.fromisoformat(day) <= as_of or
                len(text) < 120 or not terms or not any(term in text.casefold() for term in terms)):
            continue
        seen.add(url)
        article = NewsItem(title=str(row.get('title') or ''), url=url, summary=text)
        if _is_non_article_page(article):
            continue
        if policy.get('category') == 'supply_chain' and _correct_news_category(article) != 'supply_chain':
            continue
        entry = match_preferred_source(url) or {}
        level = str(entry.get('level') or '')
        trusted += int(level in {'Level 1','Level 2','L1_official','L2_media','L2_industry'})
        official += int(level in {'Level 1','L1_official'})
        url_dated += int(bool(url_day))
        fresh.append(url)
        domains.add(urlsplit(url).hostname or '')
    passed = len(fresh) >= 4 and len(domains) >= 2 and trusted >= 2 and (official >= 1 or not policy.get('require_official'))
    return {'passed':passed,'recent_substantive_articles':len(fresh),'distinct_domains':len(domains),
            'trusted_articles':trusted,'official_articles':official,'url_dated_articles':url_dated,
            'as_of_date':as_of.isoformat()}


def _safe_result_title(row: dict, url: str = "") -> dict:
    title = str(row.get("title") or "")
    normalized = clean_source_title(title, row.get("url") or url)
    if normalized != title:
        # The untouched provider response stays in the cache for provenance.
        return {**row, "title": normalized, "title_normalization": "code_isolated"}
    return row


def _recover_news_quality_dates(request, rows, attempted, diagnostics, progress):
    from .web_fetch_enrichment import recover_search_publication_dates
    from .source_date_normalizer import source_publication_status

    remaining = max(0, 12 - len(attempted))
    candidates = [item for item in make_source_items(rows)
                  if item.url not in attempted and source_publication_status(item) != 'explicit'][:min(4, remaining)]
    if not candidates:
        return
    attempted.update(item.url for item in candidates)
    receipt = {}
    recover_search_publication_dates(request, candidates, receipt, progress)
    by_url = {item.url: item for item in candidates}
    for row in rows:
        fetched = by_url.get(row.get('url'))
        if fetched:
            row.update(asdict(fetched))
    diagnostics['quality_date_recovery'] = {
        'unique_urls_attempted': len(attempted), 'max_urls_per_category': 12,
        'latest': receipt.get('publication_date_recovery'),
        'policy': '品質檢查前補原文日期；未知不造日期，既有來源與正文保留。',
    }


def _stock_table_excerpt(text, request):
    """Project all-market API rows onto an explicit single-stock request; raw cache is unchanged."""
    if request.command not in {"research", "value_scan"}:
        return text, False
    match = re.fullmatch(r"(\d{4,6})(?:\.(?:TW|TWO))?", str(request.target or ""), re.I)
    if not match:
        return text, False
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return text, False
    if not isinstance(data,dict) or not isinstance(data.get("fields"),list) or not isinstance(data.get("data"),list):
        return text, False
    index = next((i for i,field in enumerate(data["fields"]) if str(field).strip() in {"證券代號","公司代號"}),None)
    if index is None:
        return text, False
    selected = [row for row in data["data"] if isinstance(row,list) and len(row)>index and str(row[index]).strip()==match.group(1)]
    projected = {**data,"data":selected,"source_scope":{"requested_code":match.group(1),"original_rows":len(data["data"]),"matching_rows":len(selected),"policy":"僅抽取本次個股列；全市場原始回應保存在搜尋快取，不把無關個股當成標的證據。"}}
    return json.dumps(projected,ensure_ascii=False), True


class DeepSeekSearchService:
    provider = "deepseek_search"

    def __init__(self, client: DeepSeekService, *, radar_critical_gap_search_enabled=False):
        self.client = client
        self.radar_critical_gap_search_enabled = radar_critical_gap_search_enabled
        if getattr(client, 'provider', '') == 'openai_router':
            self.provider = 'openai_router_search'
        self.max_queries_per_task = 0

    def is_configured(self):
        return self.client.is_configured()

    def search(self, query: str, *, report_date: str = "") -> dict:
        from .ai_execution_control import check_work
        check_work()
        from .search_routing import SEARCH_ROUTE, search_route
        if self.provider == 'openai_router_search' and SEARCH_ROUTE.get() is None:
            with search_route(TASK.get()[0]):
                return self.search(query, report_date=report_date)
        if not query.strip() or len(query) > 128:
            raise ValueError("搜尋查詢必須介於 1 至 128 字，請拆成具體問題。")
        body = {"query": query, "count": 10, "need_content": True}
        identity = [body, report_date]
        if getattr(self.client, 'provider', '') == 'openai_router':
            # The Responses cache keeps the raw reply; this processed cache
            # must not reuse pre-fix URL/summary matching from the old adapter.
            identity.extend(['openai-search-events-v4', SEARCH_ROUTE.get(),
                             getattr(self.client, 'model', 'unconfigured_adapter'), TASK.get()[1]])
        key = "search:" + hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        with self.client.account.singleflight(key):
            data = self.client.account.cached(key)
            cached = data is not None
            if data is None:
                data = self.client._post("v1/tools/search", body, Decimal("0.00075"))
                if data.get("results"):
                    self.client.account.cache(key, data, 900)
        effective_cached = cached or (data.get('diagnostics') or {}).get('cache_hit') is True
        self.client.account.activity("search", effective_cached, {"query":query,"result_count":len(data.get("results") or []),"report_date":report_date,
            **{key: (data.get('diagnostics') or {}).get(key) for key in
               ('workflow_stage', 'search_reason', 'search_command', 'actual_model')},
            'incremental_cost_usd': '0' if effective_cached else (data.get('diagnostics') or {}).get('cost_estimate_usd'),
            'cache_scope': (data.get('diagnostics') or {}).get('cache_scope'),
            'original_request_id': (data.get('diagnostics') or {}).get('original_request_id')})
        return {**data, "results": [_safe_result_title(row) for row in data.get("results") or []], "cache_hit": effective_cached}

    def fetch(self, url: str) -> dict:
        key = "fetch:" + hashlib.sha256(url.encode()).hexdigest()
        with self.client.account.singleflight(key):
            data = self.client.account.cached(key)
            if data is not None:
                self.client.account.activity("fetch", True, {"url":url})
                return {**_safe_result_title(data, url), "cache_hit": True}
            data = self.client._post("v1/tools/fetch", {"url": url}, Decimal("0.00075"))
            self.client.account.activity("fetch", False, {"url":url})
            if data.get("content"):
                self.client.account.cache(key, data, 3600)
            return {**_safe_result_title(data, url), "cache_hit": False}

    def discover(self, request, tasks, progress=None, *, seed_sources=None) -> DiscoveryResult:
        from .ai_execution_control import check_work, checkpoint_sources
        from .search_routing import (HYBRID_COMMANDS, GAP_LABELS, discovery_command, search_route,
                                     important_search_gaps, gap_query)
        command = discovery_command(request)
        hybrid = self.provider == 'openai_router_search' and command in HYBRID_COMMANDS
        followup_enabled = command != 'radar' or self.radar_critical_gap_search_enabled
        report_day = request.report_date.isoformat() if request.report_date else TASK.get()[2]
        as_of = date.fromisoformat(report_day) if report_day else datetime.now(ZoneInfo('Asia/Taipei')).date()
        scheduled_news = command == 'news' and TASK.get()[1]
        seeds = (seed_sources if seed_sources is not None else _NEWS_SEED_SOURCES.get()) if scheduled_news else ()
        raw_sources = [asdict(source) if isinstance(source, SourceItem) else dict(source) for source in seeds]
        if raw_sources:
            checkpoint_sources(make_source_items(raw_sources))
        seen_queries = set()
        runs = []
        context_notes = []
        initial_groups = []
        for task in tasks:
            queries = []
            for group in task.get("queries") or []:
                queries.extend(group.get("items", []) if isinstance(group, dict) else [str(group)])
            run = {"label": task.get("label"), "query_count": 0, "cache_hit_count": 0, "error_count": 0, "title_code_isolated_count": 0, "nonmatching_table_count": 0}
            policy = task.get('news_quality_policy') if request.command == 'news' else None
            terms = [str(term).casefold() for term in (policy or {}).get('terms', []) if term]
            task_sources = [dict(row) for row in raw_sources if scheduled_news and policy and any(
                term in f"{row.get('title') or ''} {row.get('snippet') or ''} {row.get('content') or ''}".casefold()
                for term in terms)]
            quality_date_attempts = set()
            source_keys = set()
            no_new_source_streak = 0
            radar_gap = task.get('label') == 'radar_evidence_gap'
            stage = task.get('search_stage', 'critical_gap_search' if radar_gap else 'initial_search') if hybrid else 'initial_search'
            if hybrid and not followup_enabled and stage == 'critical_gap_search':
                runs.append({**run, 'workflow_stage': stage, 'early_stop': 'radar_followup_disabled'})
                initial_groups.append((task, set(), 'initial_search'))
                continue
            reason = str(task.get('search_reason') or ('official_document_or_counter_evidence' if radar_gap else ''))
            run.update(workflow_stage=stage, search_reason=reason)
            if scheduled_news and policy and stage == 'initial_search':
                # Shared quality and critical-gap policies run before any new query.
                quality = _news_task_quality(task_sources, policy, as_of)
                gaps = important_search_gaps(request, task, task_sources, as_of, command)
                run.update(seed_source_count=len(task_sources), seed_quality=quality, seed_critical_gaps=gaps)
                if quality['passed'] and not gaps:
                    run.update(quality=quality, early_stop='saved_source_quality_satisfied',
                               unexecuted_query_count=len(queries))
                    runs.append(run)
                    if hybrid:
                        initial_groups.append((task, {row.get('url') for row in task_sources}, stage))
                    continue
            source_keys.update((str(row.get('url') or ''), str(row.get('published_date') or '')) for row in task_sources)
            for query in queries:
                if query in seen_queries:
                    continue
                if self.max_queries_per_task and run["query_count"] >= self.max_queries_per_task:
                    break
                seen_queries.add(query)
                if progress:
                    progress(f"AI 搜尋：{task.get('label', '')}｜查詢 {run['query_count'] + 1}")
                try:
                    check_work()
                    with search_route(command, stage, reason):
                        result = self.search(query, report_date=report_day)
                    run["query_count"] += 1
                    run["cache_hit_count"] += bool(result.get("cache_hit"))
                    context_notes.extend({**note, 'query': query, 'task_label': task.get('label', '')}
                                         for note in result.get('search_context_notes') or [])
                    new_source_keys = set()
                    for row in result.get("results") or []:
                        isolated = row.get("title_normalization") == "code_isolated"
                        run["title_code_isolated_count"] += int(isolated)
                        text, projected = _stock_table_excerpt(row.get("content") or row.get("summary") or row.get("snippet"), request)
                        published_date=row.get("date")
                        if projected:
                            table=json.loads(text)
                            if not table['source_scope']['matching_rows']:
                                run['nonmatching_table_count']+=1
                                continue
                            # A crawler timestamp must not make an old official table look current.
                            dataset_date=str(table.get('date') or '')
                            if re.fullmatch(r'\d{8}',dataset_date):
                                try:
                                    published_date=datetime.strptime(dataset_date,'%Y%m%d').date().isoformat()
                                except ValueError:
                                    pass
                        source = {"title": row.get("title"), "url": row.get("url"),
                            "search_summary_events": row.get('summary_events') or [],
                            "snippet": text,
                            "published_date": published_date, "provider": row.get('provider') or result.get('provider') or self.provider,
                            "found_by": [query, *(["source_text:provided_content"] if row.get('content') else []), *(["source_title:code_isolated"] if isolated else []), *(["source_table:stock_projection"] if projected else [])],
                            "provider_detail": task.get("label", "") + str(row.get('provider_detail') or '') + ("；標題程式碼已隔離，原始回應保留" if isolated else "")}
                        raw_sources.append(source)
                        task_sources.append(source)
                        new_source_keys.add((str(source.get('url') or ''), str(source.get('published_date') or '')))
                    checkpoint_sources(make_source_items(raw_sources))
                    if new_source_keys - source_keys:
                        no_new_source_streak = 0
                    else:
                        no_new_source_streak += 1
                    source_keys.update(new_source_keys)
                    if policy and run['query_count'] >= 2:
                        if self.provider == 'openai_router_search':
                            _recover_news_quality_dates(request, task_sources, quality_date_attempts, run, progress)
                        quality = _news_task_quality(task_sources, policy, as_of)
                        run['quality'] = quality
                        if quality['passed']:
                            run['early_stop'] = 'quality_satisfied'
                            run['unexecuted_query_count'] = sum(q not in seen_queries for q in queries)
                            if progress:
                                progress(f"新聞搜尋品質達標：{task.get('label', '')}；保留全部已取得來源，略過重複補搜")
                            break
                        if no_new_source_streak >= 2:
                            run['early_stop'] = 'no_new_sources_quality_insufficient'
                            run['unexecuted_query_count'] = sum(q not in seen_queries for q in queries)
                            if progress:
                                progress(f"新聞補搜停止：{task.get('label', '')} 連續兩次未新增來源／發布日期線索；品質仍不足，保留全部資料及缺口")
                            break
                except DeepSeekUnavailable as exc:
                    from .ai_execution_control import is_graceful_search_stop
                    if is_graceful_search_stop(exc.status):
                        run['unexecuted_query_count'] = 1 + sum(q not in seen_queries for q in queries)
                    exc.partial_discovery_sources = make_source_items(raw_sources)
                    exc.partial_discovery_diagnostics = {
                        'enabled': True, 'provider': self.provider, 'status': 'partial_stopped',
                        'stop_reason': exc.status, 'runs': [*runs, run],
                        'query_count': sum(row['query_count'] for row in [*runs, run]),
                        'source_count': len(exc.partial_discovery_sources),
                        'complete': False,
                        'search_context_notes': context_notes,
                        'search_event_inventory': source_events_packet(exc.partial_discovery_sources),
                    }
                    if is_graceful_search_stop(exc.status):
                        exc.partial_discovery_diagnostics['deferred_search'] = {
                            'query': query, 'task_label': task.get('label'), 'workflow_stage': stage,
                            'reason': exc.status, **exc.diagnostics}
                    raise
                except Exception:
                    run["error_count"] += 1
            if scheduled_news and policy:
                run['quality'] = _news_task_quality(task_sources, policy, as_of)
            runs.append(run)
            if hybrid:
                initial_groups.append((task, {row.get('url') for row in task_sources}, stage))
        items = make_source_items(raw_sources)
        diagnostics = {"enabled": True, "provider": self.provider,
            "query_count": sum(r["query_count"] for r in runs), "source_count": len(items),
            "raw_source_count": len(raw_sources), "runs": runs, 'search_context_notes': context_notes,
            'search_event_inventory': source_events_packet(items)}
        if self.provider == 'openai_router_search':
            from .web_fetch_enrichment import recover_search_publication_dates
            recover_search_publication_dates(request, items, diagnostics, progress)
        if hybrid and initial_groups:
            checks = []
            for task, urls, stage in initial_groups:
                records = [asdict(item) for item in items if item.url in urls]
                gaps = important_search_gaps(request, task, records, as_of, command)
                checks.append({'task': task, 'urls': urls, 'before': gaps, 'stage': stage})
            checks.sort(key=lambda row: (not any(gap in row['before'] for gap in
                                                ('company_mapping', 'official_document')), not bool(row['before'])))
            supplements = []
            supplemented = set()
            extra_raw_count = 0
            for check in checks:
                task, gaps = check['task'], check['before']
                receipt = {'label': task.get('label'), 'before': gaps,
                           'attempted': check['stage'] == 'critical_gap_search'}
                if followup_enabled and gaps and check['stage'] == 'initial_search' and len(supplemented) < 3:
                    query = gap_query(request, task, gaps, as_of)
                    if query not in supplemented:
                        supplemented.add(query)
                        receipt.update(attempted=True, query=query)
                        if progress:
                            progress(f"Terra 定向補查：{task.get('label', '')}；缺口：{'、'.join(GAP_LABELS.get(gap, gap) for gap in gaps)}")
                        follow_task = {**task, 'queries': [query], 'search_stage': 'critical_gap_search',
                                       'search_reason': ','.join(gaps)}
                        try:
                            extra = self.discover(request, [follow_task], progress)
                        except DeepSeekUnavailable as exc:
                            exc.partial_discovery_sources = make_source_items([
                                *[asdict(item) for item in items],
                                *[asdict(item) for item in getattr(exc, 'partial_discovery_sources', [])]])
                            stopped = dict(getattr(exc, 'partial_discovery_diagnostics', {}) or {})
                            exc.partial_discovery_diagnostics = {**diagnostics, 'complete': False,
                                'status': 'partial_stopped', 'stop_reason': exc.status,
                                'runs': [*runs, *stopped.get('runs', [])],
                                'search_context_notes': [*context_notes, *stopped.get('search_context_notes', [])],
                                'critical_gap_checks': [*supplements, receipt],
                                'search_event_inventory': source_events_packet(exc.partial_discovery_sources)}
                            raise
                        items = make_source_items([*[asdict(item) for item in items], *[asdict(item) for item in extra.sources]])
                        check['urls'].update(item.url for item in extra.sources)
                        runs.extend(extra.diagnostics['runs'])
                        extra_raw_count += extra.diagnostics['raw_source_count']
                        context_notes.extend(extra.diagnostics.get('search_context_notes') or [])
                receipt['remaining'] = important_search_gaps(request, task,
                    [asdict(item) for item in items if item.url in check['urls']], as_of, command)
                receipt['status'] = 'coverage_checked_not_fact_verified' if not receipt['remaining'] else 'unresolved'
                if receipt['remaining'] and not receipt['attempted']:
                    receipt['deferred_reason'] = ('bounded_followup_or_duplicate_query' if followup_enabled
                                                  else 'radar_followup_disabled')
                supplements.append(receipt)
            diagnostics.update(critical_gap_checks=supplements, critical_followup_limit=3 if followup_enabled else 0,
                critical_followup_enabled=followup_enabled,
                complete=not any(row['remaining'] for row in supplements),
                status='coverage_checked_not_fact_verified' if not any(row['remaining'] for row in supplements) else 'evidence_incomplete',
                policy=('Luna 初搜；獨立日期／官方正文／公司映射／反證檢查後 Terra 定向補查。'
                        if followup_enabled else 'Radar 額外補查已停用；Luna 初搜後仍檢查日期／公司映射／官方證據／反證。')
                       + '保留全部初搜來源與未解缺口，不宣稱語意已驗證。')
            diagnostics.update(query_count=sum(row['query_count'] for row in runs), source_count=len(items),
                raw_source_count=len(raw_sources)+extra_raw_count,
                search_event_inventory=source_events_packet(items))
            remaining = [row for row in supplements if row['remaining']]
            if remaining:
                context_notes.append({'kind': 'unresolved_critical_search_gaps', 'verified': False,
                    'reason': '重要搜尋缺口仍未補齊；不得宣稱官方證據、日期或公司映射已驗證。保留條件式推論，不冒充已核實事實。',
                    'checks': remaining})
        if scheduled_news:
            complete = (diagnostics.get('complete', True) and not any(run['error_count'] for run in runs)
                        and all(run.get('quality', {}).get('passed', True) for run in runs))
            diagnostics['complete'] = complete
            if not complete:
                diagnostics['status'] = 'evidence_incomplete'
        return DiscoveryResult(items, diagnostics)
