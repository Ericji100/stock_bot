import asyncio
import json
import sqlite3
from unittest.mock import AsyncMock

import pytest

from research_center.scheduled_outcome_notice import ScheduledOutcomeNotices


DAY = '2026-11-26'
NEXT_DAY = '2026-11-27'


def healthy():
    return dict(status='ai_success', classified=18, pending=0, failed=False,
                reason='', missing_categories=[], classification_status='ai_success',
                search_stop_reason=None, historical_pending=400,
                publication_date_gaps=0)


def detail(**changes):
    result = healthy()
    result.update(changes)
    return result


def rows(store, table):
    with sqlite3.connect(store.path) as conn:
        return conn.execute(f'SELECT * FROM {table}').fetchall()


@pytest.mark.parametrize('changes,expected', [
    ({'status': 'partial_success', 'classification_status': 'partial_success'}, '分類未完整'),
    ({'missing_categories': ['macro_policy', 'supply_chain']}, '類別覆蓋不足'),
    ({'search_stop_reason': 'transport_unknown'}, '搜尋未完整：請求結果尚未確認'),
    ({'search_stop_reason': 'search_phase_deadline'}, '搜尋未完整：搜尋階段時間已到'),
    ({'publication_date_gaps': {'missing': 2, 'invalid': 1}}, '來源日期缺口'),
    ({'publication_date_gaps': ['source-without-date']}, '來源日期缺口'),
    ({'failed': True, 'reason': '離線診斷原因'}, '離線診斷原因'),
    ({'status': 'interrupted'}, '處理中斷'),
])
def test_first_impactful_gap_notifies_without_waiting_for_backlog(tmp_path, changes, expected):
    store, send = ScheduledOutcomeNotices(tmp_path), AsyncMock()
    assert asyncio.run(store.observe('news', 'first', DAY, detail(**changes), send)) == 'sent'
    assert send.await_count == 1
    text = send.call_args.args[0]
    assert expected in text
    assert '近期待分類 0 則' in text and '歷史待分類 400 則' in text
    assert '400' not in text.split('近期待分類')[1].split('。')[0]


def test_normal_and_historical_only_backlog_remain_silent(tmp_path):
    async def run():
        store, send = ScheduledOutcomeNotices(tmp_path), AsyncMock()
        for day in (DAY, NEXT_DAY):
            assert await store.observe('news', day, day, healthy(), send) == 'silent'
        assert len(rows(store, 'outcomes')) == 2
        send.assert_not_awaited()
    asyncio.run(run())


def test_legacy_normal_defer_and_two_day_recent_backlog_are_preserved(tmp_path):
    async def run():
        store, send = ScheduledOutcomeNotices(tmp_path), AsyncMock()
        old = {'status': 'partial_success', 'classified': 18, 'pending': 75}
        assert await store.observe('news', 'one', DAY, old, send) == 'silent'
        assert await store.observe('news', 'two', NEXT_DAY, {**old, 'pending': 90}, send) == 'sent'
        assert await store.observe('news', 'three', NEXT_DAY, old, send) == 'duplicate'
        assert '連續兩天' in send.call_args.args[0]
        # A decreasing recent backlog is not a completed recovery.
        assert await store.observe('news', 'four', '2026-11-28', {**old, 'pending': 20}, send) == 'silent'
        assert await store.observe('news', 'five', '2026-11-29', {**old, 'pending': 0}, send) == 'sent'
        assert '恢復摘要' in send.call_args.args[0]
        assert send.await_count == 2
    asyncio.run(run())


def test_same_day_gaps_merge_locally_without_rewriting_sent_snapshot(tmp_path):
    async def run():
        store, send = ScheduledOutcomeNotices(tmp_path), AsyncMock()
        first = detail(classification_status='partial_success')
        assert await store.observe('news', 'one', DAY, first, send) == 'sent'
        legacy = rows(store, 'deliveries')
        second = detail(missing_categories=['macro_policy'], publication_date_gaps=3)
        restarted = ScheduledOutcomeNotices(tmp_path)
        assert await restarted.observe('news', 'two', DAY, second, send) == 'duplicate'
        payload = json.loads(rows(store, 'news_notice_deliveries')[0][-1])
        assert set(payload['issues']) == {'classification', 'categories', 'dates'}
        assert set(payload['notified_issues']) == {'classification'}
        assert payload['latest_detail'] == second
        assert rows(store, 'deliveries') == legacy
        assert restarted.recorded_run_ids('news') == {'one', 'two'}
        assert send.await_count == 1
    asyncio.run(run())


def test_all_gaps_in_one_observation_share_one_explicit_summary(tmp_path):
    store, send = ScheduledOutcomeNotices(tmp_path), AsyncMock()
    outcome = detail(status='partial_success', classification_status='partial_success',
                     missing_categories=['macro_policy'], search_stop_reason='transport_unknown',
                     publication_date_gaps={'missing': 2}, reason='保存了來源與分類結果')
    assert asyncio.run(store.observe('news', 'one', DAY, outcome, send)) == 'sent'
    text = send.call_args.args[0]
    assert all(reason in text for reason in ('分類未完整', '類別覆蓋不足', '來源日期缺口',
                                            '請求結果尚未確認', '保存了來源與分類結果'))
    assert send.await_count == 1


def test_provider_only_failure_is_suppressed_but_later_content_gap_is_not(tmp_path):
    async def run():
        store, send = ScheduledOutcomeNotices(tmp_path), AsyncMock()
        old = {'failed': True, 'status': 'partial_success'}
        assert await store.observe('news', 'one', DAY, old, send, provider_notice_sent=True) == 'provider_notice'
        legacy = rows(store, 'deliveries')
        assert await store.observe('news', 'two', DAY, detail(missing_categories=['macro_policy']),
                                   send, provider_notice_sent=True) == 'sent'
        assert rows(store, 'deliveries') == legacy
        assert len(rows(store, 'outcomes')) == 2
        assert send.await_count == 1
    asyncio.run(run())


def test_provider_flag_on_first_content_gap_does_not_hide_it(tmp_path):
    store, send = ScheduledOutcomeNotices(tmp_path), AsyncMock()
    assert asyncio.run(store.observe('news', 'one', DAY,
                       detail(classification_status='partial_success'), send,
                       provider_notice_sent=True)) == 'sent'
    send.assert_awaited_once()


@pytest.mark.parametrize('cancelled', [False, True])
def test_unknown_or_interrupted_send_never_retries_across_restart(tmp_path, cancelled):
    async def run():
        store = ScheduledOutcomeNotices(tmp_path)
        send = AsyncMock(side_effect=asyncio.CancelledError() if cancelled else TimeoutError())
        gap = detail(classification_status='partial_success')
        if cancelled:
            with pytest.raises(asyncio.CancelledError):
                await store.observe('news', 'one', DAY, gap, send)
        else:
            assert await store.observe('news', 'one', DAY, gap, send) == 'unknown'
        restarted = ScheduledOutcomeNotices(tmp_path)
        assert await restarted.observe('news', 'two', DAY, gap, send) == 'duplicate'
        # Do not call an unconfirmed notice a delivered incident in recovery text.
        assert await restarted.observe('news', 'three', DAY, healthy(), send) == 'silent'
        assert send.await_count == 1
        assert rows(store, 'news_notice_deliveries')[0][3] == ('sending' if cancelled else 'unknown')
    asyncio.run(run())


def test_delivery_is_claimed_and_outcome_saved_before_send(tmp_path):
    store = ScheduledOutcomeNotices(tmp_path)
    async def inspect(text):
        assert len(rows(store, 'outcomes')) == 1
        assert rows(store, 'news_notice_deliveries')[0][3] == 'sending'
        assert rows(store, 'deliveries')[0][2] == 'sending'
    send = AsyncMock(side_effect=inspect)
    assert asyncio.run(store.observe('news', 'one', DAY, detail(publication_date_gaps=2), send)) == 'sent'
    send.assert_awaited_once()


def test_concurrent_observations_share_one_claim(tmp_path):
    async def run():
        store = ScheduledOutcomeNotices(tmp_path)
        async def wait_once(text):
            await asyncio.sleep(0)
        send = AsyncMock(side_effect=wait_once)
        result = await asyncio.gather(
            store.observe('news', 'one', DAY, detail(missing_categories=['macro_policy']), send),
            ScheduledOutcomeNotices(tmp_path).observe('news', 'two', DAY,
                                                     detail(publication_date_gaps=2), send))
        assert result == ['sent', 'duplicate']
        payload = json.loads(rows(store, 'news_notice_deliveries')[0][-1])
        assert set(payload['issues']) == {'categories', 'dates'}
        assert len(rows(store, 'outcomes')) == 2
        send.assert_awaited_once()
    asyncio.run(run())


def test_same_day_recovery_has_separate_once_only_key_and_no_news_digest(tmp_path):
    async def run():
        store, send = ScheduledOutcomeNotices(tmp_path), AsyncMock()
        gap = detail(classification_status='partial_success', missing_categories=['macro_policy'],
                     publication_date_gaps=2, search_stop_reason='transport_unknown')
        assert await store.observe('news', 'one', DAY, gap, send) == 'sent'
        legacy = rows(store, 'deliveries')
        # Missing checks and partially repaired results do not imply full recovery.
        assert await store.observe('news', 'two', DAY, {'status': 'success'}, send) == 'silent'
        assert await store.observe('news', 'unconfirmed', DAY,
                                   detail(missing_categories=None, publication_date_gaps=None), send) == 'silent'
        assert await store.observe('news', 'three', DAY, detail(publication_date_gaps=1), send) == 'duplicate'
        assert await store.observe('news', 'four', DAY, healthy(), send, provider_notice_sent=True) == 'sent'
        assert rows(store, 'deliveries') == legacy
        assert {row[2] for row in rows(store, 'news_notice_deliveries')} == {'alert', 'recovery'}
        assert '恢復摘要' in send.call_args.args[0]
        assert '不推送一般新聞' in send.call_args.args[0]
        assert await ScheduledOutcomeNotices(tmp_path).observe('news', 'five', DAY, healthy(), send) == 'silent'
        assert await store.observe('news', 'six', NEXT_DAY, healthy(), send) == 'silent'
        assert send.await_count == 2
    asyncio.run(run())


def test_ordinary_deferred_work_does_not_block_content_recovery(tmp_path):
    async def run():
        store, send = ScheduledOutcomeNotices(tmp_path), AsyncMock()
        assert await store.observe('news', 'one', DAY,
                                   detail(classification_status='partial_success', pending=8), send) == 'sent'
        assert await store.observe('news', 'two', DAY, detail(pending=8), send) == 'sent'
        assert '恢復摘要' in send.call_args.args[0]
    asyncio.run(run())


def test_unknown_recovery_is_not_retried_and_next_incident_can_recover(tmp_path):
    async def run():
        store, send = ScheduledOutcomeNotices(tmp_path), AsyncMock()
        gap = detail(missing_categories=['macro_policy'])
        assert await store.observe('news', 'one', DAY, gap, send) == 'sent'
        send.side_effect = TimeoutError()
        assert await store.observe('news', 'two', DAY, healthy(), send) == 'unknown'
        send.side_effect = None
        assert await store.observe('news', 'three', NEXT_DAY, healthy(), send) == 'silent'
        assert await store.observe('news', 'four', NEXT_DAY, gap, send) == 'sent'
        assert await store.observe('news', 'five', NEXT_DAY, healthy(), send) == 'sent'
        assert send.await_count == 4
    asyncio.run(run())


@pytest.mark.parametrize('legacy_state', ['sent', 'unknown', 'sending', 'provider_notice'])
def test_old_deliveries_pk_and_rows_survive_migration(tmp_path, legacy_state):
    store, send = ScheduledOutcomeNotices(tmp_path), AsyncMock()
    old = json.dumps({'failed': True, 'status': 'failed', 'reason': 'saved failure'})
    with sqlite3.connect(store.path) as conn:
        conn.execute('CREATE TABLE deliveries (command TEXT, day TEXT, state TEXT, detail TEXT, PRIMARY KEY(command,day))')
        conn.execute('INSERT INTO deliveries VALUES (?,?,?,?)', ('news', DAY, legacy_state, old))
    expected = 'sent' if legacy_state == 'provider_notice' else 'duplicate'
    assert asyncio.run(store.observe('news', 'one', DAY, detail(missing_categories=['macro_policy']), send)) == expected
    assert rows(store, 'deliveries') == [('news', DAY, legacy_state, old)]
    with sqlite3.connect(store.path) as conn:
        pk = {row[1]: row[5] for row in conn.execute('PRAGMA table_info(deliveries)')}
    assert pk == {'command': 1, 'day': 2, 'state': 0, 'detail': 0}


def test_confirmed_legacy_history_can_recover_without_rewriting_it(tmp_path):
    async def run():
        store, send = ScheduledOutcomeNotices(tmp_path), AsyncMock()
        with store._db() as conn:
            conn.execute('INSERT INTO deliveries VALUES (?,?,?,?)',
                         ('news', DAY, 'sent', json.dumps({'failed': True, 'status': 'failed'})))
        legacy = rows(store, 'deliveries')
        assert await store.observe('news', 'one', NEXT_DAY, healthy(), send) == 'sent'
        assert '恢復摘要' in send.call_args.args[0]
        assert rows(store, 'deliveries') == legacy
        assert await store.observe('news', 'two', NEXT_DAY, healthy(), send) == 'silent'
    asyncio.run(run())


def test_existing_outcomes_feed_recent_backlog_without_replacing_history(tmp_path):
    async def run():
        store, send = ScheduledOutcomeNotices(tmp_path), AsyncMock()
        old = ('news', DAY, 'legacy-run', '2026-11-26T18:00:00+08:00',
               json.dumps({'status': 'partial_success', 'classified': 18, 'pending': 20}))
        with sqlite3.connect(store.path) as conn:
            conn.execute('CREATE TABLE outcomes (command TEXT, day TEXT, run_id TEXT, recorded_at TEXT, '
                         'detail TEXT, PRIMARY KEY(command,day,run_id))')
            conn.execute('INSERT INTO outcomes VALUES (?,?,?,?,?)', old)
        assert await store.observe('news', 'new-run', NEXT_DAY, detail(pending=20), send) == 'sent'
        assert old in rows(store, 'outcomes')
        assert store.recorded_run_ids('news') == {'legacy-run', 'new-run'}
        assert '近期待辦連續兩天未減少' in send.call_args.args[0]
    asyncio.run(run())


def test_provider_incident_after_recovery_is_not_lost_to_day_boundary(tmp_path):
    async def run():
        store, send = ScheduledOutcomeNotices(tmp_path), AsyncMock()
        assert await store.observe('news', 'one', DAY, detail(failed=True), send) == 'sent'
        assert await store.observe('news', 'two', NEXT_DAY, healthy(), send) == 'sent'
        old = {'failed': True, 'status': 'failed'}
        assert await store.observe('news', 'three', NEXT_DAY, old, send,
                                   provider_notice_sent=True) == 'provider_notice'
        assert await store.observe('news', 'four', '2026-11-28', healthy(), send) == 'sent'
        assert '恢復摘要' in send.call_args.args[0]
        assert send.await_count == 3
    asyncio.run(run())


@pytest.mark.parametrize('reason,expected_issues', [
    ('新聞類別覆蓋不足：政策 / 匯率 / 總經', {'categories'}),
    ('本輪分類已完成；新聞類別覆蓋不足', {'categories'}),
    ('搜尋中止，成果已保存', {'search'}),
    ('搜尋或分類中止，成果已保存', {'search'}),
    ('類別覆蓋不足，搜尋未完整', {'categories', 'search'}),
    ('分類未完整，待辦已保存', {'classification'}),
    ('分類中止，成果已保存', {'classification'}),
    ('來源整理尚未完整，成果保留', {'processing'}),
])
def test_legacy_detail_category_reason_still_notifies(tmp_path, reason, expected_issues):
    store, send = ScheduledOutcomeNotices(tmp_path), AsyncMock()
    old = {'status': 'partial_success', 'classified': 19, 'pending': 20, 'reason': reason}
    assert asyncio.run(store.observe('news', 'one', DAY, old, send)) == 'sent'
    text = send.call_args.args[0]
    assert text.count(reason) == 1
    assert '分類或整理未完整' not in text
    payload = json.loads(rows(store, 'news_notice_deliveries')[0][-1])
    assert set(payload['notified_issues']) == expected_issues


@pytest.mark.parametrize('code', ['search_phase_deadline', 'insufficient_followup_time', 'insufficient_search_time'])
@pytest.mark.parametrize('classification', ['ai_success', 'partial_success'])
def test_normal_search_close_is_silent_when_search_and_selected_classification_are_complete(tmp_path, code, classification):
    store, send = ScheduledOutcomeNotices(tmp_path), AsyncMock()
    outcome = detail(search_stop_reason=code, search_complete=True,
                     selected_classification_complete=True,
                     classification_status=classification,
                     remaining_search_gaps_status='coverage_checked_not_fact_verified')
    assert asyncio.run(store.observe('news', 'one', DAY, outcome, send)) == 'silent'
    send.assert_not_awaited()
    assert json.loads(rows(store, 'outcomes')[0][-1]) == outcome


@pytest.mark.parametrize('changes,expected_issues', [
    ({'search_complete': False, 'selected_classification_complete': True,
      'classification_status': 'partial_success', 'remaining_search_gaps_status': 'declared_incomplete'}, {'search'}),
    ({'search_complete': True, 'selected_classification_complete': False}, {'classification'}),
    ({'search_complete': False, 'selected_classification_complete': False}, {'search', 'classification'}),
    ({'search_complete': True, 'selected_classification_complete': True,
      'remaining_search_gaps_status': 'declared_incomplete'}, {'search'}),
    ({'search_complete': False, 'selected_classification_complete': True,
      'search_stop_reason': 'insufficient_search_time'}, {'search'}),
    ({'search_complete': True, 'selected_classification_complete': True,
      'remaining_search_gaps_status': 'future_coverage_state'}, {'search'}),
])
def test_selected_classification_and_search_coverage_are_distinct(tmp_path, changes, expected_issues):
    store, send = ScheduledOutcomeNotices(tmp_path), AsyncMock()
    outcome = detail(**changes)
    assert asyncio.run(store.observe('news', 'one', DAY, outcome, send)) == 'sent'
    payload = json.loads(rows(store, 'news_notice_deliveries')[0][-1])
    assert set(payload['notified_issues']) == expected_issues
    text = send.call_args.args[0]
    assert ('分類未完整' in text) == ('classification' in expected_issues)
    assert ('搜尋未完整' in text) == ('search' in expected_issues)
    assert all(code not in text for code in ('partial_success', 'declared_incomplete',
                                            'insufficient_search_time', 'future_coverage_state'))


@pytest.mark.parametrize('unknown', [False, True])
def test_codes_stay_local_and_telegram_uses_chinese_labels_including_reason(tmp_path, unknown):
    store, send = ScheduledOutcomeNotices(tmp_path), AsyncMock()
    classification = 'future_classification_state' if unknown else 'partial_success'
    category = 'future_category' if unknown else 'ai_semiconductor'
    stop = 'future_search_stop' if unknown else 'insufficient_search_time'
    outcome = detail(status='partial_success', classification_status=classification,
                     missing_categories=[category], search_stop_reason=stop,
                     publication_date_gaps={'missing': 2},
                     reason=f'分類狀態：{classification}；類別：{category}；搜尋停止：{stop}；ai_success')
    assert asyncio.run(store.observe('news', 'one', DAY, outcome, send)) == 'sent'
    text = send.call_args.args[0]
    assert all(code not in text for code in (classification, category, stop, 'ai_success', 'missing'))
    if unknown:
        assert '分類完成狀態尚待確認' in text and '搜尋停止原因尚待確認' in text
        assert '其他台股財經新聞' in text
    else:
        assert '部分新聞尚未完成判讀' in text
        assert 'AI / 半導體' in text and '剩餘搜尋時間不足' in text
    assert json.loads(rows(store, 'outcomes')[0][-1]) == outcome
    assert json.loads(rows(store, 'news_notice_deliveries')[0][-1])['latest_detail'] == outcome


def test_complete_search_can_recover_with_normal_stop_and_old_english_issue_text(tmp_path):
    async def run():
        store, send = ScheduledOutcomeNotices(tmp_path), AsyncMock()
        old = {'notified_issues': {'classification': '分類未完整：partial_success',
                                  'categories': '類別覆蓋不足：ai_semiconductor',
                                  'search': '搜尋未完整：transport_unknown'}}
        old['issues'] = old['notified_issues']
        with store._db() as conn:
            conn.execute('INSERT INTO news_notice_deliveries VALUES (?,?,?,?,?)',
                         ('news', DAY, 'alert', 'sent', json.dumps(old)))
        outcome = detail(search_complete=True, selected_classification_complete=True,
                         remaining_search_gaps_status='coverage_checked_not_fact_verified',
                         search_stop_reason='insufficient_search_time')
        assert await store.observe('news', 'one', DAY, outcome, send) == 'sent'
        text = send.call_args.args[0]
        assert '恢復摘要' in text and '搜尋覆蓋缺口已清除' in text
        assert all(code not in text for code in ('partial_success', 'ai_semiconductor',
                                                'transport_unknown', 'insufficient_search_time'))
        assert await store.observe('news', 'two', DAY, outcome, send) == 'silent'
        send.assert_awaited_once()
    asyncio.run(run())


def test_topic_notice_behavior_is_unchanged(tmp_path):
    async def run():
        store, send = ScheduledOutcomeNotices(tmp_path), AsyncMock()
        topic = {'status': 'partial_success', 'selected': 8, 'completed': 5}
        assert await store.observe('topic_maintain', 'one', DAY, topic, send) == 'sent'
        assert await store.observe('topic_maintain', 'two', DAY, topic, send) == 'duplicate'
        assert '待補 3 題' in send.call_args.args[0]
        assert rows(store, 'news_notice_deliveries') == []
    asyncio.run(run())


@pytest.mark.parametrize('prior', [
    {'failed': True, 'status': 'failed'},
    {'missing_categories': ['supply_chain'], 'status': 'partial_success'},
    {'pending': 3, 'status': 'partial_success'},
])
def test_missing_completion_evidence_cannot_restore_prior_alerts(tmp_path, prior):
    async def run():
        store, send = ScheduledOutcomeNotices(tmp_path), AsyncMock()
        if 'pending' in prior:
            await store.observe('news', 'previous', '2026-11-25', prior, send)
        assert await store.observe('news', 'bad', DAY, prior, send) == 'sent'
        calls = send.await_count
        counts_only = dict(status=None, failed=False, classified=None, pending=None,
            missing_categories=None, classification_status=None, search_complete=None,
            selected_classification_complete=None, publication_date_gaps=None)
        assert await store.observe('news', 'unknown', NEXT_DAY, counts_only, send) == 'silent'
        assert send.await_count == calls
    asyncio.run(run())


def test_unknown_counts_use_chinese_not_none():
    from research_center.scheduled_outcome_notice import format_outcome_notice
    text = format_outcome_notice('news', DAY, {'failed': True, 'classified': None, 'pending': None,
                                              'historical_pending': None})
    assert 'None' not in text and '尚待確認' in text
