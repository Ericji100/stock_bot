from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from research_center.deepseek_service import ai_task
from research_center.evidence_governance import official_evidence_coverage, parsed_official_sources
from research_center.models import SourceItem
from research_center.openai_credit_service import OpenAICreditLedger, OpenAICreditsUnavailable
from research_center.report_quality_service import review_reader_content
from tests.test_openai_routing import grant


def test_secondary_priority_is_fixed_even_when_primary_expires_first(tmp_path):
    now = datetime(2026, 11, 5, tzinfo=timezone.utc)
    ledger = OpenAICreditLedger(tmp_path, [grant('primary', end='2026-11-10T00:00:00Z'),
                                         grant('secondary')], now=lambda: now, job_limit=0)
    with ai_task('research'):
        assert ledger.choose({'primary', 'secondary'})[0]['account_id'] == 'secondary'
    assert ledger.summary()['account_priority'] == ['secondary', 'primary']


def test_background_cannot_spend_manual_or_buffer_across_accounts(tmp_path):
    now = datetime(2026, 11, 5, tzinfo=timezone.utc)
    ledger = OpenAICreditLedger(tmp_path, [grant('primary'), grant('secondary')], now=lambda: now, job_limit=0)
    for background, amount in [(True, '70'), (False, '20')]:
        for account in ('secondary', 'primary'):
            with ai_task('fixture', background=background):
                chosen, ident = ledger.choose({'primary', 'secondary'}, Decimal(amount), reserve=True)
                assert chosen['account_id'] == account
                ledger.finish(ident, 'success', Decimal(amount), {})
        with ai_task('fixture', background=background), pytest.raises(OpenAICreditsUnavailable):
            ledger.choose({'primary', 'secondary'}, Decimal('.01'))
    assert [row['used_or_reserved'] for row in ledger.summary()['grants']] == ['90', '90']


@pytest.mark.parametrize('reason', ['expired', 'blocked', 'missing_key'])
def test_ineligible_secondary_can_use_primary_without_resetting_usage(tmp_path, reason):
    now = datetime(2026, 11, 5, tzinfo=timezone.utc)
    secondary = grant('secondary', end='2026-11-04T00:00:00Z') if reason == 'expired' else grant('secondary')
    ledger = OpenAICreditLedger(tmp_path, [grant('primary'), secondary], now=lambda: now)
    if reason == 'blocked':
        ledger.block(ledger.grants[1], 'credit_balance_exhausted')
    keys = {'primary'} if reason == 'missing_key' else {'primary', 'secondary'}
    with ai_task('research'):
        assert ledger.choose(keys)[0]['account_id'] == 'primary'


def test_reference_or_duplicate_url_never_inflates_official_coverage():
    ref = SourceItem('S001', '入口', 'https://mops.twse.com.tw/entry', 'Level 1',
                     document_kind='reference_link', snippet='請查閱公告')
    parsed = SourceItem('S002', '解析數值', 'https://www.twse.com.tw/record', 'Level 1',
                        document_kind='parsed_record', snippet='{"code":"2330","pe_ratio":26.2}')
    duplicate = replace(parsed, source_id='S003')
    audit = official_evidence_coverage([ref, parsed, duplicate])
    assert audit['retrieved_unique_urls'] == 1
    assert audit['reference_only_unique_urls'] == 1
    assert audit['claims_verified'] is False


def test_existing_official_values_become_sources_without_faking_publication_or_company():
    record = {'status': 'official_public', 'source': 'https://www.twse.com.tw/valuation?date=20261104',
              'rows': [{'code': '2330', 'pe_ratio': 26.2}]}
    data = {'stock': {'code': '2330'}, 'free_public_sources': {'valuation': record}}
    original = deepcopy(data)
    sources = parsed_official_sources(data, start=9)
    assert len(sources) == 1 and sources[0].source_id == 'S009'
    assert sources[0].published_date is None and sources[0].data_date is None
    assert '26.2' in sources[0].snippet and data == original
    data['stock']['code'] = '2241'
    assert parsed_official_sources(data) == []
    data['stock']['code'] = '2330'
    record['status'] = 'reference_link'
    assert parsed_official_sources(data) == []


def test_name_typo_is_not_cleared_by_a_separate_correction_note():
    text = '| 排名 | 公司 |\n|---|---|\n| 1 | 艾勒姆 |\n\n公司名稱為艾姆勒（2241），上表為筆誤。'
    data = {'ai_candidates': [{'code': '2241', 'name': '艾姆勒'}]}
    result = review_reader_content(text, [], data)
    assert not result['mechanical_checks_passed']
    assert result['company_name_review']['issues'][0]['kind'] == 'possible_transposition'
    assert '艾勒姆' in text
    assert review_reader_content(text.replace('| 艾勒姆 |', '| 艾姆勒 |'), [], data)['company_name_review']['errors'] == []


def test_unknown_company_is_not_guessed_and_abbreviation_is_not_wrong_code():
    data = {'stock': {'code': '5347', 'name': '世界先進'}}
    assert review_reader_content('世界（5347）\n| 公司 |\n|---|\n| 未知公司 |', [], data)['company_name_review']['errors'] == []


def test_market_population_has_separate_raw_and_ranking_dates_without_losing_rows():
    from research_center.market_movers_service import _assemble_market_movers
    rows = [{'code': '1001', 'sector': '甲', 'price_date': '2026-11-03', 'price': 10},
            {'code': '1002', 'sector': '乙', 'price_date': '2026-11-04', 'price': 11}]
    before = deepcopy(rows)
    result = _assemble_market_movers(date(2026, 11, 4), rows)
    quality = result['data_quality']
    assert quality['raw_price_date_audit']['mixed_dates'] is True
    assert quality['ranking_price_date_audit']['mixed_dates'] is False
    assert quality['ranking_population']['stock_count'] == 1
    assert quality['ranking_population']['raw_stock_count'] == 2
    assert result['other_session_rows'][0]['code'] == '1001' and rows == before


def test_old_cached_population_is_explained_without_changing_rankings():
    from research_center.market_movers_service import _annotate_cached_population, market_population_summary
    cached = {'active_movers': [{'code': '1002'}], 'data_quality': {
        'input_stock_count': 1, 'ranking_price_date': '2026-11-04',
        'price_date_audit': {'date_counts': {'2026-11-03': 2, '2026-11-04': 1},
                             'undated_stock_count': 0, 'mixed_dates': True}}}
    before = deepcopy(cached)
    annotated = _annotate_cached_population(cached)
    assert cached == before and annotated['active_movers'] == cached['active_movers']
    assert annotated['data_quality']['ranking_population']['other_session_stock_count'] == 2
    assert not annotated['data_quality']['ranking_price_date_audit']['mixed_dates']
    cached['data_quality']['input_stock_count'] = 99
    assert market_population_summary(cached) == {}
