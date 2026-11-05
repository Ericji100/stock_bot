"""Operation roles are independent of task billing pools and source truth."""
from contextlib import contextmanager
from contextvars import ContextVar
import re

SEARCH_ROUTE = ContextVar('search_route', default=None)
HYBRID_COMMANDS = frozenset({'news', 'topic_maintain', 'radar'})
GAP_LABELS = {'publication_date': '可核對發布日期', 'news_freshness_and_quality': '近期原始新聞與來源品質',
              'company_mapping': '公司代號與發行公司', 'official_document': '官方公告原文',
              'counter_evidence': '風險與反證'}


def command_name(value):
    return str(value or '').strip().lstrip('/').split(' ')[0].removeprefix('scheduled_')


def discovery_command(request):
    from .deepseek_service import TASK
    current = command_name(TASK.get()[0])
    if str(getattr(request, 'raw_text', '')).startswith('/radar'):
        return 'radar'
    return current if current in HYBRID_COMMANDS else command_name(request.command)


@contextmanager
def search_route(command, stage='initial_search', reason=''):
    if stage not in {'initial_search', 'critical_gap_search'}:
        raise ValueError('Unsupported search role')
    token = SEARCH_ROUTE.set({'command': command_name(command), 'stage': stage, 'reason': reason})
    try:
        yield
    finally:
        SEARCH_ROUTE.reset(token)


def stage_metadata(*, organizer=False):
    route = SEARCH_ROUTE.get()
    return {'workflow_stage': route['stage'] if route else ('text_organization' if organizer else 'final_analysis'),
            'search_reason': route['reason'] if route else '',
            'search_command': route['command'] if route else ''}


def important_search_gaps(request, task, sources, as_of, command):
    """Conservative coverage checks, never a semantic fact-verification pass."""
    from .evidence_governance import official_evidence_status, source_matches_company
    from .source_date_normalizer import source_temporal_record
    from .deepseek_search_service import _news_task_quality

    records = []
    for source in sources:
        temporal = source_temporal_record(source, as_of)
        if (temporal['publication_status'] == 'explicit' and temporal['available_by_report_date'] is not False
                and not temporal['data_after_report_date'] and not temporal['content_updated_after_report_date']):
            records.append(source)
    gaps = []
    if not records:
        gaps.append('publication_date')
    policy = task.get('news_quality_policy') or {}
    if command == 'news' and policy and not _news_task_quality(sources, policy, as_of)['passed']:
        gaps.append('news_freshness_and_quality')
    target = str(getattr(request, 'target', '') or '')
    match = re.match(r'^(\d{4,6})(?!\d)', target)
    code = match.group(1) if match else ''
    expected_name = target[match.end():].strip() if match else ''
    def company_matches(row):
        return not code or source_matches_company(row, code, expected_name)
    if code and not any(company_matches(row) for row in records):
        gaps.append('company_mapping')
    intent = str(task.get('objective') or '') + ' ' + str(task.get('label') or '')
    needs_official = command in {'radar', 'topic_maintain'} or policy.get('require_official') or any(
        word in intent for word in ('官方', '財報', '營收', '訂單', '客戶', '供應鏈', '政策'))
    if needs_official and not any(company_matches(row) and official_evidence_status(row) ==
                                  'retrieved_requires_claim_check' for row in records):
        gaps.append('official_document')
    if any(word in intent for word in ('反證', '風險', '矛盾')) and not any(
            any(word in str(row.get('title') or '') + str(row.get('snippet') or '')
                for word in ('反證', '風險', '衰退', '下修', '不確定', '否認', 'risk', 'decline')) for row in records):
        gaps.append('counter_evidence')
    return list(dict.fromkeys(gaps))


def gap_query(request, task, gaps, as_of):
    task_label = str(task.get('label') or '')
    target = str(request.target or (task_label if re.search(r'[\u4e00-\u9fff]', task_label) else '台股'))
    needs = ' '.join(GAP_LABELS.get(gap, '必要證據') for gap in gaps)
    official = ' (site:twse.com.tw OR site:tpex.org.tw)' if 'official_document' in gaps and re.match(r'^\d{4,6}(?!\d)', target) else ''
    # A report deadline is not a requirement for a same-day announcement.
    question = f'{target[:24]} 截至{as_of.isoformat()}已公開最近資料（不限當日） {needs}{official}'
    return question[:128]


def candidate_search_plan(code, name, industry, as_of, structured_data):
    """Cover all five perspectives without re-searching available numeric tables."""
    from datetime import timedelta
    from .mops_sources import financial_prompt_context
    from .date_aware_context import parse_date_like
    from math import isfinite

    data = structured_data or {}
    financial = financial_prompt_context(data.get('financial_data') or [], as_of)
    def numeric(value):
        return isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value)
    financial_values = any(numeric(value) for row in financial['rows'][:1] for key, value in row.items()
                           if str(key).casefold() in {'eps', 'revenue', 'net_income', 'operating_income', 'interest_income'})
    chip_dates = [parse_date_like(row.get('Date') or row.get('date'))
                  for row in data.get('institutional_data') or [] if isinstance(row, dict)
                  and any(numeric(value) for key, value in row.items() if key not in ('Date', 'date', 'code', 'stock_id'))]
    chip_covered = any(day and as_of - timedelta(days=7) <= day <= as_of for day in chip_dates)
    target = f'{code} {name}'
    queries = [
        f'{target} {as_of.isoformat()} 近期官方公告 法說 訂單 {industry or "產業"} 供應鏈 題材催化',
        f'{target} {as_of.isoformat()} 營運風險 反證 需求變化 競爭 估值重估 失效條件',
    ]
    missing = []
    if financial['stale_or_missing'] or not financial_values:
        missing.append('最新已公布財報 財務表現 每股盈餘 公布日期')
    if not chip_covered:
        missing.append('交易所近期法人買賣超 融資融券 集保 期間與單位')
    if missing:
        queries.append(f'{target} {as_of.isoformat()} 官方原始數據 ' + ' '.join(missing))
    return {'queries': queries, 'coverage_topics': ['催化劑', '營收與基本面', '籌碼資金', '風險與反證', '題材想像空間'],
            'local_coverage': {'financial_latest_period': financial['latest_period'],
                               'financial_stale_or_missing': financial['stale_or_missing'] or not financial_values,
                               'institutional_data_recent': chip_covered},
            'policy': '核心數值由共用資料層完整入模；新增搜尋只補近期事件、反證及缺少的官方數據，不刪已取得來源。'}
