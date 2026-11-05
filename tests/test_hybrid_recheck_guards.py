from copy import deepcopy
from datetime import date
from decimal import Decimal

from research_center.date_aware_context import report_quote_context


def test_gap_query_is_company_evidence_not_an_internal_report_search():
    from research_center.models import CommandRequest
    from research_center.search_routing import gap_query, GAP_LABELS
    query = gap_query(CommandRequest(command='research', target='2603 長榮', raw_text='/radar'),
        {'objective': 'Radar AI短評：radar_ai_sources 所需證據', 'gap_field': 'internal_field'},
        list(GAP_LABELS), date(2026, 11, 4))
    assert query.startswith('2603 長榮 截至2026-11-04')
    assert '不限當日' in query and len(query) <= 128
    for term in ('Radar', '短評', 'radar_ai_sources', 'internal_field'):
        assert term not in query
    assert all(label in query for label in GAP_LABELS.values())
    assert 'site:twse.com.tw' in query and 'site:tpex.org.tw' in query


def test_web_fetch_respects_html_charset_without_mutating_raw_bytes():
    from unittest.mock import patch
    import requests
    from research_center.web_fetch_service import _fetch_with_requests
    for encoding, header in (('utf-8', 'text/html'), ('big5', 'text/html'),
                             ('utf-8', 'text/html; charset=utf-8')):
        html = (f'<html><head><meta charset="{encoding}"><title>官方財務資訊</title>'
                '<meta property="article:published_time" content="2026-11-03T10:00:00+08:00">'
                '</head><body><article>' + '營收與風險原始資料。' * 50 + '</article></body></html>')
        raw = html.encode(encoding)
        response = requests.Response()
        response.status_code = 200
        response.url = 'https://www.twse.com.tw/example'
        response.headers['content-type'] = header
        response.encoding = requests.utils.get_encoding_from_headers(response.headers)
        response._content = raw
        with patch('research_center.web_fetch_service.requests.get', return_value=response):
            result = _fetch_with_requests(response.url, 15)
        assert result.title == '官方財務資訊'
        assert '營收與風險原始資料' in result.content
        assert result.published_date == '2026-11-03'
        assert response.content == raw


def test_topic_completed_model_never_uses_a_failed_attempt_or_search_model():
    from research_center.topic_maintain_service import _completed_topic_model
    assert _completed_topic_model([{'stage': 'candidate_extract', 'raw': '',
        'requested_model': 'gpt-6-luna', 'error': 'transport_unknown',
        'diagnostics': {'requested_model': 'gpt-6-luna'}}]) is None
    stages = [{'raw': '{}', 'diagnostics': {'actual_model': 'gpt-6-luna'}},
        {'raw': '', 'error': 'transport_unknown', 'requested_model': 'gpt-6.1-terra'}]
    assert _completed_topic_model(stages) == 'gpt-6-luna'
    assert _completed_topic_model([]) is None


def test_large_topic_stage_clones_timeout_without_changing_model_limits_or_fallback():
    from types import SimpleNamespace
    from research_center.ai_provider_service import RoutedAIService
    from research_center.topic_maintain_service import _topic_stage_client
    from tests.test_luna_routing import LunaRoutingTests
    from research_center.deepseek_service import ai_task
    fixture = LunaRoutingTests('test_nested_native_context_and_clones_preserve_background')
    fixture.setUp()
    try:
        fallback = fixture.fallback()
        fallback.with_output_limit.return_value = fallback
        router = RoutedAIService(fixture.make_client(), fallback)
        center = SimpleNamespace(deepseek_organizer=router, deepseek=router)
        with ai_task('topic_maintain', background=True):
            small = _topic_stage_client(center, 'candidate_extract', 99999)
            large = _topic_stage_client(center, 'candidate_extract', 368899)
            assert large.timeout_seconds == 600 and small.timeout_seconds == router.timeout_seconds == 180
            assert large.primary.ledger is router.primary.ledger
            assert large.model == small.model == 'gpt-6-luna'
            assert large.fallback.timeout_seconds == router.fallback.timeout_seconds
            assert large.max_output_tokens == small.max_output_tokens == 32768
            assert fixture.calls == []
    finally:
        fixture.doCleanups()


def test_recheck_budget_accumulates_cases_and_preserves_old_unknown():
    from tools.hybrid_recheck_guard import ROUND, check_ledger_rows
    import pytest
    old = [{'id': 'old', 'status': 'unknown', 'estimate': '.288884', 'actual': None}]
    new = {'id': 'new', 'status': 'success', 'estimate': '2.8', 'actual': '2.9',
           'purpose': 'development', 'round_id': ROUND, 'case_id': 'radar'}
    assert check_ledger_rows([*old, new], old, 'news', 3)['actual_or_reserved'] == Decimal('2.9')
    with pytest.raises(ValueError):
        check_ledger_rows([*old, new], old, 'news', 3.11)
    with pytest.raises(ValueError):
        check_ledger_rows([*old, {**new, 'status': 'unknown', 'actual': None}], old, 'news', 0)
    with pytest.raises(ValueError):
        check_ledger_rows([{**old[0], 'estimate': '0'}], old, 'news', 0)


def test_explicit_manual_stop_does_not_authorize_restart_or_larger_budget():
    from tools.hybrid_recheck_guard import ROUND, validate_authorization
    import pytest
    original = [{'id': 'u', 'status': 'unknown', 'estimate': '.288884'}]
    valid = {'round_id': ROUND, 'approved': True, 'total_limit_usd': '6',
             'case_limits': {'radar': '5', 'news': '4', 'topic_maintain': '4'},
             'account_priority': ['secondary', 'primary'], 'purpose': 'development',
             'user_confirmation': 'synthetic', 'production_state': 'user_intentionally_stopped',
             'deployment': False, 'telegram_send': False, 'restart': False, 'paid_deepseek_fallback': False,
             'retained_unknown': {'u': '.288884'}}
    validate_authorization(valid, original)
    for invalid in ({'total_limit_usd': '7'}, {'restart': True}, {'retained_unknown': {}}, {'production_state': 'assumed_healthy'}):
        with pytest.raises(ValueError):
            validate_authorization({**valid, **invalid}, original)


def test_report_quote_keeps_conflicting_raw_values_and_does_not_change_scores():
    snapshot = {'last_date': '2026-11-04', 'previous_date': '2026-11-03', 'row_count': 2,
        'close': 268, 'previous_close': 266.5,
        'price_metrics': {'previous_close': 268, 'change_pct': -3.36, 'price_date': '2026-11-04'}}
    original = deepcopy(snapshot)
    view = report_quote_context(snapshot, date(2026, 11, 4))
    assert snapshot == original
    assert abs(view['view']['change_pct'] - .56285178) < .00001
    assert view['original_cached_metrics'] == original['price_metrics']
    assert {row['field'] for row in view['conflicts']} == {'previous_close', 'change_pct'}
    assert not view['official_verified']


def test_future_or_single_row_does_not_create_a_daily_return():
    for extra in ({'last_date': '2026-11-05'}, {'row_count': 1}, {'previous_date': '2026-11-04'}):
        raw = {'last_date': '2026-11-04', 'previous_date': '2026-11-03', 'row_count': 2, 'close': 10, 'previous_close': 9, **extra}
        assert report_quote_context(raw, date(2026, 11, 4))['view']['change_pct'] is None


def test_old_snapshot_discloses_missing_prior_date_and_zero_return_is_valid():
    view = report_quote_context({'last_date': '2026-11-04', 'row_count': 2, 'close': 10, 'previous_close': 10}, date(2026, 11, 4))
    assert view['view']['change_pct'] == 0
    assert any('前收盤日期' in row for row in view['warnings'])


def test_radar_label_uses_completed_actual_model_not_configured_provider():
    from stock_ai_bot.monitoring import radar_service as radar
    request = radar.RadarRequest(model='deepseek', ai_top=15, ai_comment_enabled=True)
    item = radar.RadarCandidate(code='2603', name='長榮')
    assert '未完成' in radar._radar_mode_line(request, [item])
    assert 'DeepSeek' not in radar._radar_mode_line(request, [item])
    item.ai_comment = {'status': 'ok', 'model': 'gpt-6.1-terra'}
    assert 'Terra' in radar._radar_mode_line(request, [item])


def test_report_quote_is_wired_from_feature_snapshot_not_strategy_summary():
    from stock_ai_bot.monitoring import radar_service as radar
    item = radar.RadarCandidate(code='2603', name='長榮', evidence_pack={
        'technical': {'summary': '策略摘要不是日價'},
        'radar_feature_snapshot': {'technical': {'last_date': '2026-11-04', 'row_count': 2,
            'close': 268, 'previous_close': 266.5, 'price_metrics': {'change_pct': -3.36}}}})
    view = radar._build_ai_comment_payload(item, date(2026, 11, 4))['ai_compact_pack']['payload']['technical_quote_context']
    assert view['view']['close'] == 268
    assert len(view['conflicts']) == 1
