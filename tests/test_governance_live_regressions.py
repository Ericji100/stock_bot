"""Offline regressions from the Nov 3 live review; no API or Bot required."""
import json
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from research_center.models import CommandRequest, SourceItem


def test_dynamic_historical_actuals_are_quarantined_not_forecasts():
    from research_center.date_guard import filter_sources_for_report_date
    from research_center.source_date_normalizer import future_actual_periods
    cutoff = date(2026, 6, 4)
    text = '2026/06 370,328\n2026/06/09 收盤 25.90'
    assert future_actual_periods(text, cutoff) == ['2026-06', '2026-06-09']
    source = SourceItem('S040', '歷史營收', 'https://example.com/a', 'Level 3', '2026-03-12', text)
    assert not filter_sources_for_report_date([source], cutoff)[0]
    assert source.snippet == text
    assert future_actual_periods('預估 2026/12 營收 500，目標價 30', cutoff) == []
    assert future_actual_periods('Schlusskurs 09.06.2026 | 15.06.2026 Kurs | 25,90 | 28,05', cutoff) == ['2026-06-09', '2026-06-15']
    assert future_actual_periods('日期：2026年11月 3日。收盤44920.14點',date(2026,11,4)) == []
    assert future_actual_periods('2026/06/ 09 收盤25.90',cutoff) == ['2026-06-09']


def test_latest_mode_rejects_future_actuals_but_keeps_undated_leads(tmp_path,monkeypatch):
    from datetime import timedelta
    from research_center.orchestrator import _guard_enriched_sources
    monkeypatch.setenv('CODEX_TASK_TEST_ROOT',str(tmp_path))
    target=datetime.now(ZoneInfo('Asia/Taipei')).date()
    future=SourceItem('S001','未來實績','https://example.com/future','Level 2',
                     (target+timedelta(days=1)).isoformat(),'已公布營收實績')
    unknown=SourceItem('S002','日期未知線索','https://example.com/unknown','Level 3')
    data={}
    kept=_guard_enriched_sources(CommandRequest('theme','/theme 測試'),[future,unknown],data)
    assert kept==[unknown]
    assert data['post_fetch_date_guard']['excluded_count']==1
    assert future.snippet=='已公布營收實績'


def test_corporate_official_host_not_lookalike():
    from research_center.evidence_governance import is_official_url
    from research_center.source_rank import rank_source
    assert rank_source('https://pr.tsmc.com/english/news/a') == 'Level 1'
    assert is_official_url('https://investor.cld.tsmc.com/financial.pdf')
    assert not is_official_url('https://pr.tsmc.com.evil.example/a')


def test_us_intraday_date_is_not_a_confirmed_close():
    from research_center.macro_indicators import _quote_session_context
    from research_center.macro_data_guard import build_macro_data_guard
    early=datetime(2026,11,4,1,3,tzinfo=ZoneInfo('Asia/Taipei'))
    context=_quote_session_context('SOX','2026-11-03',early)
    assert context['session_status']=='intraday_or_unconfirmed'
    facts=build_macro_data_guard({'quantitative_market':{'global_public_macro':{'SOX':{'latest_date':'2026-11-03','latest_close':10661.8,**context}}}})['facts']
    assert facts[0]['指標']=='最新盤中／未確認快照'
    assert facts[0]['數值']==10661.8
    assert _quote_session_context('SOX','2026-11-03',early.replace(hour=6))['session_status']=='daily_bar_after_regular_close'


def test_company_code_conflict_and_other_issuer_are_explicit():
    from research_center.evidence_governance import source_company_conflicts
    source = SourceItem('S072', '合勤控（3703）', 'https://mops.twse.com.tw/STR/3703/a.pdf', 'Level 1')
    issues = source_company_conflicts(source, {'stock_index': {'3703': {'name': '欣陸'}}, 'stock': {'code': '2330'}})
    assert len(issues) == 2
    assert issues[0]['expected_name'] == '欣陸'


def test_every_source_text_survives_rank_and_string_limits():
    from research_center.ai_workflow_service import _source_excerpts
    from research_center.segmented_analysis_service import _compact_segment_payload, _source_refs
    sources = [SourceItem(f'S{i:03}', '證據', f'https://example.com/{i}', 'Level 2', snippet='文' * 2000 + f'事實{i}') for i in range(1, 76)]
    excerpts = _source_excerpts(sources, limit=18)
    assert len(excerpts) == 75
    assert excerpts[-1]['snippet'].endswith('事實75')
    assert _compact_segment_payload({'evidence': excerpts})['evidence'] == excerpts
    assert len(_source_refs(sources)) == 75
    assert _source_refs(sources)[-1]['url'] == sources[-1].url


def test_value_financial_actual_nested_schema_is_preserved():
    from research_center.ai_workflow_service import _value_scan_financial_key_metrics
    row = {'status': 'covered', 'latest_period': '2026Q2', 'sample': {'EPS': 22.08, 'Gross_Margin': 60.2}, 'fields': ['EPS']}
    result = _value_scan_financial_key_metrics(row)
    assert result['sample'] == row['sample']
    assert result['latest_period'] == '2026Q2'


def test_lossless_shared_payload_roundtrip_and_actual_size():
    from research_center.topic_input_package import lossless_evidence_json, expand_evidence_json
    item = {'facts': ['公司甲 2026Q2 數值 99，反證尚待驗證。' * 100]}
    payload = {'core': [item] * 100, 'duplicate': item}
    packed = json.loads(lossless_evidence_json(payload))
    def expand(value):
        if isinstance(value, dict):
            if set(value) == {'$topic_ref'}:
                return expand(packed['shared_records'][value['$topic_ref']])
            return {k: expand(v) for k, v in value.items()}
        return [expand(v) for v in value] if isinstance(value, list) else value
    assert expand_evidence_json(packed) == payload
    assert len(lossless_evidence_json(payload)) < len(json.dumps(payload)) / 4


def test_topic_capacity_is_bytes_not_inconsistent_character_limit():
    from research_center.topic_pipeline_service import _full_prompt_fits
    assert _full_prompt_fits('x' * 640412)
    assert not _full_prompt_fits('營' * 640412)


def test_prediction_tracking_accepts_reader_friendly_aliases():
    from research_center.analysis_followup_service import explicit_prediction_records
    text = '## 預測追蹤（來源預測與待驗假說）\n| 標的或主題 | 假設或預測 | 觀察截止日 | 驗證條件 | 失效條件 | 來源編號 |\n|---|---|---|---|---|---|\n| 公司甲 | 接單改善 | 2027-01-01 | 營收成長 | 訂單取消 | [S073] |'
    rows = explicit_prediction_records(text)
    assert len(rows) == 1
    assert rows[0]['source_ids'] == ['S073']


def test_news_keywords_not_official_facts_and_old_tag_is_removed():
    from research_center.news_models import NewsItem, apply_news_signal_tags
    item = NewsItem(title='財報營收成長但尚無官方確認', url='https://example.com/news', tags=['official_fact', 'ai_classified'])
    apply_news_signal_tags(item)
    assert 'official_fact' not in item.tags
    assert 'disclosure_clue' in item.tags
    assert 'ai_classified' in item.tags


def test_official_search_excerpt_is_not_a_retrieved_document():
    from research_center.news_models import NewsItem, apply_news_signal_tags
    item = NewsItem(title='財報公告', url='https://pr.tsmc.com/a', full_text='搜尋取得的財報摘要')
    apply_news_signal_tags(item)
    assert 'official_fact' not in item.tags
    item.tags.append('official_body_retrieved')
    apply_news_signal_tags(item)
    assert 'official_fact' in item.tags


def test_budget_preflight_cannot_falsely_recover_pacing(tmp_path):
    from research_center.ai_budget_service import MonthlyBudget
    budget = MonthlyBudget(tmp_path)
    budget.incident('schedule_pacing', background=True)
    first = budget.claim_notice()
    assert '不代表官方餘額用完' in first[1]
    budget.notice_result(first[0], True)
    budget.incident(None, background=True, preflight=True)
    assert budget.claim_notice() is None
    budget.incident(None, background=True)
    assert '實際請求恢復成功' in budget.claim_notice()[1]


def test_tomorrow_pacing_increases_without_monthly_reset(tmp_path):
    from research_center.ai_budget_service import MonthlyBudget, BudgetBlocked
    budget = MonthlyBudget(tmp_path)
    now = datetime(2026, 11, 3, 22, 30, tzinfo=ZoneInfo('Asia/Taipei'))
    budget.reserve_request('past', 'news', True, '', Decimal('1.199492280'), now=now)
    import pytest
    with pytest.raises(BudgetBlocked, match='schedule_pacing'):
        budget.check(estimate=Decimal('.01'), background=True, now=now)
    budget.check(estimate=Decimal('.01'), background=True, now=now.replace(day=4))


def test_mixed_market_sessions_separated_without_deleting_rows():
    from research_center.market_movers_service import _assemble_market_movers
    rows = [{'code': 'A', 'price_date': '2026-11-02', 'industry': 'X', 'change_pct': 9},
            {'code': 'B', 'price_date': '2026-11-03', 'industry': 'X', 'change_pct': 1}]
    result = _assemble_market_movers(date(2026, 11, 3), rows)
    assert result['top_gainers'][0]['code'] == 'B'
    assert result['other_session_rows'][0]['code'] == 'A'
    assert result['data_quality']['price_date_audit']['mixed_dates']


def test_reserved_reference_shape_is_literal_not_broken_reference():
    from research_center.topic_input_package import lossless_evidence_json, expand_evidence_json
    original = {'literal': {'$topic_ref': 'original-source-text'}}
    assert expand_evidence_json(json.loads(lossless_evidence_json(original))) == original


def test_known_annual_eps_normalizes_only_with_proven_periods():
    from research_center.mops_sources import normalize_research_financial_periods
    original = [{'Quarter': f'2025Q{i}', 'EPS': val, 'EPS_Period_Basis': basis} for i,val,basis in
                [(1,13.94,'quarterly'),(2,15.36,'quarterly'),(3,17.45,'quarterly'),(4,66.26,'annual')]]
    normalized, audit = normalize_research_financial_periods(original)
    assert normalized[-1]['EPS'] == 19.51
    assert normalized[-1]['EPS_Raw'] == 66.26
    assert original[-1]['EPS'] == 66.26
    assert normalized[-1]['EPS_Period_Basis'] == 'quarterly'


def test_unknown_eps_is_not_added_to_false_ttm_or_marked_poor():
    from research_center.mops_sources import normalize_research_financial_periods
    from research_center.scoring_engine import _eps_score
    rows, audit = normalize_research_financial_periods([{'Quarter':'2025Q4','EPS':66.26}])
    score, reason, risk = _eps_score(rows)
    assert score == 50 and '中性占位' in reason
    assert rows[0]['EPS'] == 66.26
    assert audit['warnings']


def test_parsed_official_series_has_actual_values_and_not_fake_publication():
    from research_center.evidence_governance import parsed_official_sources, official_evidence_status
    data={'quantitative_market':{'official_cash_institutional_flow':{'status':'official_public',
          'source':'https://www.twse.com.tw/fund/BFI82U?response=json','data_date':'2026-11-03',
          'rows':[{'name':'投信','net_amount':18460158558}]}}}
    sources=parsed_official_sources(data, start=20)
    assert sources[0].source_id == 'S020'
    assert '18460158558' in sources[0].snippet
    assert sources[0].published_date is None
    assert sources[0].data_date == '2026-11-03'
    assert official_evidence_status(sources[0]) == 'retrieved_requires_claim_check'


def test_enriched_sources_rechecked_after_fetch_and_archived(tmp_path, monkeypatch):
    from research_center.orchestrator import _guard_enriched_sources
    monkeypatch.setenv('CODEX_TASK_TEST_ROOT', str(tmp_path))
    request=CommandRequest(command='research',raw_text='/research 3550 --date 2026-06-04',report_date=date(2026,6,4))
    source=SourceItem('S076','歷史報價','https://example.com/article','Level 2','2026-05-13','2026/06/09 收盤 25.90')
    data={}
    assert _guard_enriched_sources(request,[source],data) == []
    archive=data['post_fetch_date_guard']['archive']
    assert archive['excluded_record_count'] == 1
    assert '25.90' in __import__('pathlib').Path(archive['archive_path']).read_text(encoding='utf-8')


def test_theme_admission_not_only_first_fifty_sources():
    from research_center.orchestrator import _select_sources_for_prompt
    sources=[SourceItem(f'S{i:03}',f'AI電源證據{i}',f'https://example.com/{i}','Level 2',snippet=f'實際數字{i}') for i in range(1,81)]
    data={'theme':'AI電源'}
    result=_select_sources_for_prompt(CommandRequest(command='theme',raw_text='/theme AI電源',target='AI電源'),sources,data)
    assert {s.source_id for s in result} == {s.source_id for s in sources}
    assert data['theme_prompt_source_selection']['admitted_source_count'] == 80


def test_zero_numeric_checks_is_not_a_numeric_verification():
    from research_center.report_numeric_validation import validate_report_numbers
    result=validate_report_numbers('資料不足，只有來源線索。')
    assert result['verification_status'] == 'not_checked' and result['warnings']
    result=validate_report_numbers('預估每股盈餘 113.04×28=3129。')
    assert not result['passed']
    assert result['explicit_arithmetic']['checked_formulas'] == 1


def test_user_friendly_eps_valuation_header_is_checked():
    from research_center.report_numeric_validation import validate_valuation_tables
    text='| 情境 | 每股盈餘 | P/E | 目標價 |\n|---|---|---|---|\n| 中性 | 10 | 20 | 999 |'
    result=validate_valuation_tables(text)
    assert result['checked_rows'] == 1 and not result['passed']


def test_related_articles_retained_separately_not_attributed_to_primary():
    from research_center.source_text_cleaner import article_text_sections
    text='公司甲營收 99。\n## 延伸閱讀\n公司乙毛利率 45%。'
    primary, related=article_text_sections(text)
    assert '公司乙' not in primary
    assert '45%' in related
    assert '公司甲營收 99。' in primary


def test_suspicious_original_glyphs_remain_reversible_in_json():
    from research_center.topic_input_package import lossless_evidence_json, expand_evidence_json
    from research_center.segmented_analysis_service import _sanitize_prompt_text
    original={'原始欄位': '原文\ue001與疑似解碼符\ufffd仍保留'}
    encoded=lossless_evidence_json(original)
    assert expand_evidence_json(json.loads(_sanitize_prompt_text(encoded))) == original


def test_tabular_encoding_preserves_every_record_and_missing_vs_null():
    from research_center.topic_input_package import lossless_evidence_json, expand_evidence_json
    rows = [{'code':str(i), 'company_name':'公司甲', 'observed_period':'2026Q2',
             'revenue_amount':123+i, 'counter_evidence_note':'仍須核對'} for i in range(200)]
    rows[3]['nullable'] = None
    original = {'stock_index':{r['code']:r for r in rows}, 'rows':rows}
    encoded = lossless_evidence_json(original)
    envelope = json.loads(encoded)
    assert envelope['record_schemas']
    assert expand_evidence_json(envelope) == original
    assert len(encoded.encode()) < len(json.dumps(original,ensure_ascii=False).encode())
    literal = {'literal':{'$records':[['original',123]],'$keys':['original']}}
    assert expand_evidence_json(json.loads(lossless_evidence_json(literal))) == literal


def test_bad_topic_quote_is_retained_as_lead_not_claimed_support():
    from research_center.topic_models import TopicChangePack, TopicChangeMode, TopicChangeStatus
    from research_center.topic_schema_normalizer import normalize_topic_detail_action
    from research_center.topic_citation_audit import quarantine_topic_source_leads, audit_topic_citations
    action = normalize_topic_detail_action({'theme_id':'test','theme_name':'測試題材',
        'evidence':[{'source_id':'S001','url':'https://example.com/a','claim':'有效資料'},
                    {'source_id':'S002','url':'https://example.com/b','claim':'模型主張',
                     'supporting_quote':'這是正文沒有提供的完整模型引句'}],
        'counter_evidence':['S002 稱個股跌破均線'],
        'supply_chain_nodes':[{'company_code':'2330','role':'待核角色','evidence':[]}]})
    pack = TopicChangePack('test',None,TopicChangeMode.UPDATE,TopicChangeStatus.PENDING,'test','2026-11-04','2026-11-04','test','low',actions=[action])
    data = {'report_date':'2026-11-04','discovery_sources':[
        {'source_id':'S001','url':'https://example.com/a','content':'有效的來源資料內容'},
        {'source_id':'S002','url':'https://example.com/b','content':'來源其實討論其他個股，不能支持此主張'}]}
    quarantine_topic_source_leads(pack,data)
    assert len(action.evidence) == 1
    assert action.extra['unverified_source_leads'][0]['supporting_quote'] == '這是正文沒有提供的完整模型引句'
    assert action.counter_evidence[0].startswith('待核線索（')
    assert audit_topic_citations(pack,data)['passed']
    action.evidence.clear()
    assert not audit_topic_citations(pack,data)['passed']


def test_payables_cannot_masquerade_as_local_operating_cash_flow():
    from research_center.report_numeric_validation import validate_report_numbers, financial_fact_bindings
    data={'stock':{'code':'3550'},'financial_data':[{'Quarter':'2026Q1',
          'Accounts_Payable':248206000,'Operating_Cash_Flow':-201533000,
          'Capital_Expenditure':-7187000,'Free_Cash_Flow':-208720000}]}
    assert financial_fact_bindings(data)[0]['原始欄位數值']['應付帳款'] == 248206000
    wrong='本地財報底稿顯示 2026Q1 營業現金流 +248,206 千元、資本支出 -201,533 千元、自由現金流 -201,533 千元。'
    assert len(validate_report_numbers(wrong,structured_data=data)['financial_bindings']['errors']) == 3
    correct='本地財報資料 2026Q1 營業現金流 -201,533 千元、資本支出 -7,187 千元、自由現金流 -208,720 千元。'
    assert validate_report_numbers(correct,structured_data=data)['passed']
