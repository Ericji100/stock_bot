import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from research_center.scheduled_outcome_notice import ScheduledOutcomeNotices, format_outcome_notice
from research_center.topic_pipeline_service import restore_topic_candidate_ids, _detail_covers_batch, run_topic_pipeline
from research_center.topic_models import TopicChangeMode


def test_only_unique_explicit_update_target_restores_identity_without_mutating_original():
    batch = [{'theme_id': 'candidate', 'target_theme_id': 'existing', 'action_type': 'update_theme'}]
    payload = {'actions': [{'theme_id': 'existing', 'action_type': 'update_theme',
                            'evidence': [{'url': 'https://example.test', 'claim': '全部保留'}]}]}
    original = deepcopy(payload)
    repaired, receipt = restore_topic_candidate_ids(payload, batch)
    assert payload == original
    assert repaired['actions'][0]['theme_id'] == 'candidate'
    assert repaired['actions'][0]['target_theme_id'] == 'existing'
    assert repaired['actions'][0]['evidence'] == original['actions'][0]['evidence']
    assert receipt and _detail_covers_batch(repaired, batch)


@pytest.mark.parametrize('batch,payload', [
    ([{'theme_id': 'a'}, {'theme_id': 'b'}], {'actions': [{'theme_id': 'a'}]}),
    ([{'theme_id': 'a', 'target_theme_id': 'x', 'action_type': 'update_theme'},
      {'theme_id': 'b', 'target_theme_id': 'x', 'action_type': 'update_theme'}],
     {'actions': [{'theme_id': 'x', 'action_type': 'update_theme'}, {'theme_id': 'b'}]}),
    ([{'theme_id': 'a', 'target_theme_id': 'x', 'action_type': 'create_theme'}],
     {'actions': [{'theme_id': 'x', 'action_type': 'create_theme'}]}),
    ([{'theme_id': 'a', 'target_theme_id': 'x', 'action_type': 'update_theme'}],
     {'actions': [{'theme_id': 'x', 'action_type': 'update_theme', 'target_theme_id': 'wrong'}]}),
])
def test_topic_identity_does_not_guess_missing_ambiguous_create_or_conflicting_target(batch, payload):
    result, receipt = restore_topic_candidate_ids(payload, batch)
    assert result == payload and receipt == []
    assert not _detail_covers_batch(result, batch)


def _pipeline(call):
    from tests.test_topic_pipeline_service import TopicPipelineServiceTests
    fixtures = TopicPipelineServiceTests()
    with patch('research_center.topic_pipeline_service._full_evidence_detail_batches',
               side_effect=lambda rows, *a: [[row] for row in rows]):
        return run_topic_pipeline(mode=TopicChangeMode.UPDATE, ai_model='deepseek', change_id='offline',
            iso_ts='2026-11-11T10:00:00+0800', structured_data={}, prompt_variables=fixtures._variables(),
            load_prompt=fixtures._load_prompt, render_prompt=fixtures._render_prompt,
            call_ai_json=call, require_full_ai=True)


def test_content_error_does_not_disable_later_independent_batches():
    calls = []
    def call(prompt, stage):
        calls.append(stage)
        if stage == 'candidate_extract':
            return {'candidates': [{'theme_id': n, 'theme_name': n} for n in ['a', 'b', 'c']]}
        return {'actions': [{'theme_id': 'wrong' if stage == 'detail_expand_1' else
                             ('b' if stage == 'detail_expand_2' else 'c')}]}
    pack, _ = _pipeline(call)
    assert calls == ['candidate_extract', 'detail_expand_1', 'detail_expand_2', 'detail_expand_3']
    assert not pack.extra['ai_review_complete']
    assert pack.extra['detail_review']['ai_completed_count'] == 2


def test_unknown_cost_stops_actual_model_calls_but_preserves_partial_pack():
    from research_center.deepseek_service import DeepSeekUnavailable
    calls = []
    def call(prompt, stage):
        calls.append(stage)
        if stage == 'candidate_extract':
            return {'candidates': [{'theme_id': n, 'theme_name': n} for n in ['a', 'b']]}
        raise DeepSeekUnavailable('費用未知', 'transport_unknown')
    pack, _ = _pipeline(call)
    assert calls == ['candidate_extract', 'detail_expand_1']
    assert not pack.extra['ai_review_complete']
    assert len(pack.actions) == 2


def test_topic_partial_notice_once_across_restart_and_unknown_no_resend(tmp_path):
    async def run():
        store = ScheduledOutcomeNotices(tmp_path)
        send = AsyncMock(side_effect=TimeoutError())
        detail = {'status': 'partial_success', 'selected': 8, 'completed': 5, 'artifact': 'saved-pack'}
        assert await store.observe('topic_maintain', 'one', '2026-11-10', detail, send) == 'unknown'
        assert await ScheduledOutcomeNotices(tmp_path).observe('topic_maintain', 'two', '2026-11-10', detail, send) == 'duplicate'
        assert send.await_count == 1
        text = send.call_args.args[0]
        assert '待補 3 題' in text and '不可將草稿' in text
    asyncio.run(run())


def test_news_normal_defer_is_silent_but_repeated_growing_backlog_notifies(tmp_path):
    async def run():
        store, send = ScheduledOutcomeNotices(tmp_path), AsyncMock()
        detail = {'status': 'partial_success', 'classified': 18, 'pending': 75}
        assert await store.observe('news', 'one', '2026-11-10', detail, send) == 'silent'
        assert await store.observe('news', 'two', '2026-11-11', {**detail, 'pending': 90}, send) == 'sent'
        assert send.await_count == 1
        assert await store.observe('news', 'three', '2026-11-11', detail, send) == 'duplicate'
    asyncio.run(run())


def test_provider_notice_avoids_duplicate_summary_and_news_real_failure_alerts(tmp_path):
    async def run():
        store, send = ScheduledOutcomeNotices(tmp_path), AsyncMock()
        detail = {'failed': True, 'status': 'partial_success'}
        assert await store.observe('news', 'one', '2026-11-10', detail, send,
                                   provider_notice_sent=True) == 'provider_notice'
        send.assert_not_awaited()
        assert await store.observe('news', 'two', '2026-11-11', detail, send) == 'sent'
    asyncio.run(run())


def test_scheduled_topic_partial_is_disclosed_without_confirmation_buttons():
    import main
    from research_center.deepseek_service import ai_task
    async def run():
        context = SimpleNamespace(bot=SimpleNamespace())
        result = SimpleNamespace(status='partial_success', report_json={'change_pack': {
            'change_id': 'saved', 'extra': {'ai_review_complete': False,
            'detail_review': {'selected_count': 8, 'ai_completed_count': 5}}}})
        center = SimpleNamespace(run=lambda *a: result, deepseek=None)
        with patch('research_center.orchestrator.ResearchCenter', return_value=center), \
             patch.object(main, 'load_config', return_value={'chat_id': 1}), \
             patch.object(main, '_scheduled_ai_gate', AsyncMock(return_value='')), \
             patch.object(main, '_scheduled_outcome_notice', AsyncMock()) as notice, \
             patch('research_center.telegram_handlers.send_topic_result_to_chat', AsyncMock()) as full:
            outcome = await main._scheduled_topic_maintain(context)
        assert outcome['status'] == 'partial_success'
        assert notice.call_args.args[3]['completed'] == 5
        full.assert_not_awaited()
    with ai_task('topic_maintain', background=True):
        asyncio.run(run())


def test_backfill_cancellation_keeps_resource_until_worker_really_exits():
    from threading import Event
    from research_center.ai_execution_control import CONTROL
    from research_center.resource_guard_service import ResourceGuardService
    from research_center.scheduled_task_service import ScheduledTaskService, ScheduledTaskSpec, run_cooperative_thread
    async def run():
        pool = ResourceGuardService({'background_backfill': 1})
        service = ScheduledTaskService(resource_guard=pool, audit_sink=lambda *a: None)
        started, cancelled, release = Event(), Event(), Event()
        def worker():
            control = CONTROL.get()
            started.set()
            while not control.event.wait(.005):
                pass
            cancelled.set()
            release.wait(2)
        async def runner():
            return await run_cooperative_thread(worker)
        spec = ScheduledTaskSpec('isolation', 'isolation', resource_group='background_backfill', timeout_seconds=.03)
        task = asyncio.create_task(service._run_with_timeout(spec, runner))
        try:
            while not cancelled.is_set():
                await asyncio.sleep(.005)
            assert started.is_set() and not task.done()
            assert pool.snapshot()['pools']['background_backfill']['active'] == 1
        finally:
            release.set()
        with pytest.raises(asyncio.TimeoutError):
            await task
        assert pool.snapshot()['pools']['background_backfill']['active'] == 0
    asyncio.run(run())


def test_backfill_waiting_for_resource_does_not_spend_its_execution_timeout():
    from research_center.resource_guard_service import ResourceGuardService
    from research_center.scheduled_task_service import ScheduledTaskService, ScheduledTaskSpec
    async def run():
        pool = ResourceGuardService({'background_backfill': 1})
        events, seen = [], []
        service = ScheduledTaskService(resource_guard=pool, audit_sink=lambda spec, status, detail: events.append((status, detail)))
        async def runner():
            seen.append('started')
            await asyncio.sleep(.01)
            return 'ok'
        async with pool.acquire('background_backfill'):
            task = asyncio.create_task(service._run_with_timeout(
                ScheduledTaskSpec('wait', 'wait', resource_group='background_backfill', timeout_seconds=.03), runner))
            await asyncio.sleep(.06)
            assert not task.done() and not seen
        assert await task == 'ok'
        acquired = next(detail for status, detail in events if status == 'resource_acquired')
        assert acquired['resource_wait_seconds'] >= .05
    asyncio.run(run())


def test_chip_worker_errors_return_partial_not_false_completion():
    import main
    with patch.object(main, 'warmup_chip_data_cache', side_effect=RuntimeError('synthetic failure')):
        outcome = asyncio.run(main._scheduled_chip_cache_backfill({}))
    assert outcome['status'] == 'partial_success'


def test_backfill_frame_uses_owned_worker_not_detached_nested_executor():
    from research_center.data_services import _fetch_research_frame
    from research_center.ai_execution_control import ExecutionControl, CONTROL, WorkStopped
    from stock_ai_bot.backfill.deadline_control import backfill_scope
    now = [0.]
    parent = ExecutionControl(5, clock=lambda: now[0])
    token = CONTROL.set(parent)
    def fetch():
        now[0] = 6.
        return 'late'
    try:
        with backfill_scope(), patch('concurrent.futures.ThreadPoolExecutor') as executor:
            with pytest.raises(WorkStopped):
                _fetch_research_frame('slow', fetch, None, [])
            executor.assert_not_called()
    finally:
        CONTROL.reset(token)
