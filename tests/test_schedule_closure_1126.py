"""Interrupted schedules and resource contention, with no network or live writes."""
import asyncio
from contextlib import contextmanager
from datetime import date
import json
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from research_center.ai_execution_control import CONTROL, WorkStopped, check_work, execution_scope
from research_center.command_runtime_service import CommandRuntimeService
from research_center.deepseek_service import ai_task
from research_center.resource_guard_service import ResourceGuardService
from research_center.scheduled_run_store import CURRENT_RUN, ScheduledRunStore, checkpoint, tracked_scheduled_run
from research_center.scheduled_task_service import ScheduledTaskService, ScheduledTaskSpec, run_cooperative_thread


@contextmanager
def stored(store, run_id='offline', day='2026-11-25'):
    store.begin(run_id, 'radar', day, owner_pid=999999, owner_creation='original')
    token = CURRENT_RUN.set((store, run_id))
    try:
        yield
    finally:
        CURRENT_RUN.reset(token)


def test_dead_owner_recovery_keeps_all_checkpoints_and_claims_notice_once(tmp_path):
    store = ScheduledRunStore(tmp_path)
    with stored(store):
        checkpoint('local_data', {'core': ['2330', '2241'], 'is_final_report': False})
        checkpoint('sources', {'raw': '完整正文、風險、反證' * 900})
        checkpoint('ai_batch', {'completed_comments': {'2330': {'reason': '有依據的推論'}}})
    rows = store.recover(is_owner_alive=lambda pid, creation: False)
    assert len(rows) == 1 and store.get('offline')['status'] == 'interrupted'
    with store.db() as conn:
        assert conn.execute('SELECT COUNT(*) FROM checkpoints').fetchone()[0] == 3
        assert '完整正文' in conn.execute("SELECT payload FROM checkpoints WHERE stage='sources'").fetchone()[0]
    sent = []
    async def send(text):
        sent.append(text)
    asyncio.run(store.notify_recovered(send))
    asyncio.run(store.notify_recovered(send))
    assert len(sent) == 1 and '不是額度耗盡' in sent[0]
    assert store.get('offline')['recovery_notice'] == 'sent'


@pytest.mark.parametrize('alive', [True, None])
def test_alive_or_unverifiable_owner_is_never_reclaimed(tmp_path, alive):
    store = ScheduledRunStore(tmp_path)
    with stored(store):
        checkpoint('sources', {'raw': '證據'})
    assert store.recover(is_owner_alive=lambda *args: alive) == []
    assert store.get('offline')['status'] == 'running'


def test_ambiguous_recovery_delivery_is_not_resent(tmp_path):
    store = ScheduledRunStore(tmp_path)
    with stored(store):
        checkpoint('delivery_started', {'delivery': 'unconfirmed'})
    store.recover(is_owner_alive=lambda *args: False)
    sender = AsyncMock(side_effect=TimeoutError('unknown delivery'))
    asyncio.run(store.notify_recovered(sender))
    asyncio.run(store.notify_recovered(sender))
    assert sender.await_count == 1
    assert store.get('offline')['recovery_notice'] == 'unknown'


def test_checkpoint_failure_stops_before_next_model(tmp_path):
    store = ScheduledRunStore(tmp_path)
    with stored(store), execution_scope() as control, patch.object(store, 'checkpoint', side_effect=OSError('disk full')):
        with pytest.raises(WorkStopped) as error:
            checkpoint('sources', {'core': '完整資料'})
        assert error.value.status == 'schedule_checkpoint_failed' and control.event.is_set()
        with pytest.raises(WorkStopped):
            check_work()


@pytest.mark.parametrize('status', ['completed', 'partial_success', 'failed'])
def test_terminal_runs_are_not_recovered_or_overwritten(tmp_path, status):
    store = ScheduledRunStore(tmp_path)
    with stored(store):
        checkpoint('report_ready', {'text': '報告'})
    store.finish('offline', status, {'reason': 'preserved'})
    store.finish('offline', 'completed', {})
    assert store.recover(is_owner_alive=lambda *args: False) == []
    assert store.get('offline')['status'] == status
    with pytest.raises(RuntimeError):
        store.checkpoint('offline', 'sources', {})


def test_tracked_run_preserves_ai_state_and_separate_delivery(tmp_path, monkeypatch):
    import research_center.scheduled_run_store as module
    store = ScheduledRunStore(tmp_path)
    monkeypatch.setattr(module, 'scheduled_store', lambda: store)
    @tracked_scheduled_run('radar')
    async def task():
        checkpoint('report_ready', {'text': '完整內容', 'ai_complete': True})
        checkpoint('delivery_started', {'delivery': 'unconfirmed'})
        raise TimeoutError('TG unconfirmed, not AI failure')
    with ai_task('radar', background=True):
        from research_center.deepseek_service import RUN_ID
        run_id = RUN_ID.get()
        with pytest.raises(TimeoutError):
            asyncio.run(task())
    row = store.get(run_id)
    assert row['status'] == 'failed' and row['stage'] == 'delivery_started'
    with store.db() as conn:
        assert json.loads(conn.execute("SELECT payload FROM checkpoints WHERE stage='report_ready'").fetchone()[0])['ai_complete']


def test_resource_wait_defers_without_entering_runner_or_stopping_owner():
    async def run():
        pool = ResourceGuardService({'background_backfill': 1})
        events = []
        service = ScheduledTaskService(resource_guard=pool, audit_sink=lambda *a: events.append(a))
        runner = AsyncMock()
        async with pool.acquire('background_backfill'):
            result = await service._run_with_timeout(ScheduledTaskSpec('wait', 'wait', resource_group='background_backfill',
                resource_wait_seconds=.02, timeout_seconds=.01), runner)
            assert result['status'] == 'deferred'
            assert pool.snapshot()['pools']['background_backfill']['active'] == 1
        runner.assert_not_awaited()
        assert pool.snapshot()['pools']['background_backfill']['active'] == 0
    asyncio.run(run())


def test_cancelled_worker_keeps_lease_and_reports_drain_then_exit():
    async def run():
        pool = ResourceGuardService({'background_backfill': 1})
        events = []
        service = ScheduledTaskService(resource_guard=pool,
            audit_sink=lambda spec, state, detail: events.append(state))
        entered, stopped, release = Event(), Event(), Event()
        def worker():
            entered.set()
            CONTROL.get().event.wait(2)
            stopped.set()
            release.wait(2)
            check_work()
        async def runner():
            return await run_cooperative_thread(worker)
        task = asyncio.create_task(service._run_with_timeout(ScheduledTaskSpec('drain', 'drain',
            resource_group='background_backfill', timeout_seconds=.03), runner))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            assert await asyncio.to_thread(stopped.wait, 2)
            assert not task.done() and 'stopping' in events
            assert pool.snapshot()['pools']['background_backfill']['active'] == 1
        finally:
            release.set()
        with pytest.raises(asyncio.TimeoutError):
            await task
        assert events.index('stopping') < events.index('worker_stopped')
        assert pool.snapshot()['pools']['background_backfill']['active'] == 0
    asyncio.run(run())


def test_finished_stopped_runtime_is_not_active_anymore():
    runtime = CommandRuntimeService()
    runtime.start_task('backfill', label='回補', task_type='backfill', stop_event=Event())
    runtime.request_stop('backfill')
    assert runtime.is_task_active('backfill')
    runtime.finish_stopped_task('backfill')
    assert not runtime.is_task_active('backfill')
    assert runtime.get_task('backfill')['status'] == 'stopped'


def test_full_backfill_next_run_gets_a_fresh_stop_event(monkeypatch):
    import main
    async def run():
        old_event = Event()
        old_event.set()
        monkeypatch.setattr(main, '_SCHEDULED_BACKFILL_STOP_EVENT', old_event)
        monkeypatch.setattr(main, '_SCHEDULED_BACKFILL_TASK', None)
        monkeypatch.setattr(main, '_SCHEDULED_BACKFILL_RUNNING', False)
        captured = []
        async def guarded(stop_event):
            captured.append(stop_event)
        monkeypatch.setattr(main, '_scheduled_backfill_guarded_background', guarded)
        monkeypatch.setattr(main, 'write_scheduled_task_audit_event', lambda *a: None)
        monkeypatch.setattr(main, '_record_scheduled_event', lambda *a: None)
        ctx = SimpleNamespace(application=SimpleNamespace(create_task=asyncio.create_task))
        await main.scheduled_full_backfill_check(ctx)
        task = main._SCHEDULED_BACKFILL_TASK
        await task
        assert captured[0] is not old_event and not captured[0].is_set() and old_event.is_set()
    asyncio.run(run())


def test_scheduler_owner_reference_survives_inner_worker_exit(monkeypatch):
    import main
    from stock_ai_bot.backfill import backfill_service
    async def run():
        marker = object()
        stop = Event()
        stop.set()
        runtime = CommandRuntimeService()
        monkeypatch.setattr(main, 'GLOBAL_COMMAND_RUNTIME', runtime)
        monkeypatch.setattr(main, '_SCHEDULED_BACKFILL_TASK', marker)
        monkeypatch.setattr(main, '_SCHEDULED_BACKFILL_RUNNING', True)
        monkeypatch.setattr(backfill_service, 'run_backfill_if_needed', lambda *a:
            SimpleNamespace(status='stopped', report_date=date(2026, 11, 25)))
        result = await main._scheduled_backfill_background(stop)
        assert result['status'] == 'partial_success'
        assert main._SCHEDULED_BACKFILL_TASK is marker and main._SCHEDULED_BACKFILL_RUNNING
        assert runtime.get_task('backfill:scheduled')['status'] == 'stopped'
    asyncio.run(run())


def test_radar_batch_checkpoint_retains_success_when_next_batch_is_unknown(tmp_path, monkeypatch):
    from stock_ai_bot.monitoring import radar_service as radar
    from research_center.deepseek_service import DeepSeekUnavailable
    candidates = [radar.RadarCandidate(code=str(2000+i), name='測試', symbol=f'{2000+i}.TW') for i in range(2)]
    store = ScheduledRunStore(tmp_path)
    monkeypatch.setattr(radar, '_radar_discovery_stopped', lambda _: None)
    monkeypatch.setattr(radar, '_attach_radar_low_model_digest', lambda *a: {})
    monkeypatch.setattr(radar, '_build_ai_comment_prompt_jobs', lambda *a, **k: [
        {'prompt': '完整資料1', 'codes': ['2000'], 'profile': 'test'},
        {'prompt': '完整資料2', 'codes': ['2001'], 'profile': 'test'}])
    first = json.dumps({'comments': [{'code': '2000', 'reason': '證據', 'risk': '風險', 'watch': '觀察'}]})
    monkeypatch.setattr(radar, '_call_ai_comment_model', __import__('unittest.mock', fromlist=['Mock']).Mock(
        side_effect=[first, DeepSeekUnavailable('unknown', 'transport_unknown')]))
    with stored(store):
        radar._attach_ai_comments(candidates, ['2000', '2001'], 'deepseek', date(2026, 11, 25), None)
    with store.db() as conn:
        saved = [json.loads(r[0]) for r in conn.execute('SELECT payload FROM checkpoints')]
    assert '2000' in saved[-1]['completed_comments']
    assert candidates[1].ai_comment['status'] == 'failed'
    assert not saved[-1]['is_final_report']


def test_full_wrapper_retains_ownership_while_timeout_drains(monkeypatch):
    import main
    from stock_ai_bot.backfill import backfill_service
    async def run():
        entered, stopping, release = Event(), Event(), Event()
        stop_event = Event()
        runtime = CommandRuntimeService()
        guard = ResourceGuardService({'background_backfill': 1})
        events = []
        monkeypatch.setattr(main, 'GLOBAL_COMMAND_RUNTIME', runtime)
        monkeypatch.setattr(main, 'DEFAULT_RESOURCE_GUARD', guard)
        monkeypatch.setattr(main, '_scheduled_timeout_seconds', lambda _: .03)
        monkeypatch.setattr(main, 'write_scheduled_task_audit_event', lambda spec, state, *args: events.append(state))
        monkeypatch.setattr(main, '_SCHEDULED_BACKFILL_RUNNING', True)
        def collect(*args):
            entered.set()
            stop_event.wait(2)
            stopping.set()
            release.wait(2)
            check_work()
        monkeypatch.setattr(backfill_service, 'run_backfill_if_needed', collect)
        task = asyncio.create_task(main._scheduled_backfill_guarded_background(stop_event))
        monkeypatch.setattr(main, '_SCHEDULED_BACKFILL_TASK', task)
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            assert await asyncio.to_thread(stopping.wait, 2)
            assert not task.done() and main._SCHEDULED_BACKFILL_TASK is task
            assert main._SCHEDULED_BACKFILL_RUNNING
            assert guard.snapshot()['pools']['background_backfill']['active'] == 1
        finally:
            release.set()
        with pytest.raises(asyncio.TimeoutError):
            await task
        assert main._SCHEDULED_BACKFILL_TASK is None and not main._SCHEDULED_BACKFILL_RUNNING
        assert runtime.get_task('backfill:scheduled')['status'] == 'stopped'
        assert events.index('stopping') < events.index('worker_stopped') < events.index('timeout')
        assert guard.snapshot()['pools']['background_backfill']['active'] == 0
    asyncio.run(run())


def test_pid_reuse_does_not_treat_new_process_as_old_owner(monkeypatch):
    import research_center.scheduled_run_store as module
    monkeypatch.setattr(module, 'process_creation', lambda _: 'new-process-creation')
    assert module.owner_alive(123, 'old-process-creation') is False
    assert module.owner_alive(123, None) is None


def test_scheduled_radar_uses_trigger_date_even_when_queue_crosses_midnight(monkeypatch):
    import main
    captured = []
    async def enqueue(context, label, runner):
        captured.append(runner)
    result = AsyncMock()
    monkeypatch.setattr(main, 'get_tw_today', lambda: date(2026, 11, 25))
    monkeypatch.setattr(main, 'enqueue_scheduled_task', enqueue)
    monkeypatch.setattr(main, '_scheduled_radar_push', result)
    asyncio.run(main.scheduled_radar_push(SimpleNamespace()))
    monkeypatch.setattr(main, 'get_tw_today', lambda: date(2026, 11, 26))
    asyncio.run(captured[0]())
    assert result.call_args.kwargs['report_date'] == date(2026, 11, 25)


@pytest.mark.parametrize('terminal', ['partial_success', 'stopped', 'deferred', 'cancelled', 'skipped'])
def test_terminal_backfill_clears_only_its_own_heartbeat(tmp_path, terminal):
    from stock_ai_bot.monitoring.bot_runtime_health import record_scheduled_status_heartbeat_event as record
    path = tmp_path / 'heartbeat.json'
    record('started', {'task_id': 'radar', 'queued': True}, path=path)
    record('started', {'task_id': 'backfill', 'queued': False}, path=path)
    state = record(terminal, {'task_id': 'backfill'}, path=path)
    assert state['schedule_health']['current_task']['task_id'] == 'radar'
    state = record(terminal, {'task_id': 'radar'}, path=path)
    assert state['schedule_health']['current_task'] is None


def test_news_adapter_does_not_invent_empty_gap_or_zero_pending(tmp_path, monkeypatch):
    import main
    bot = SimpleNamespace(send_message=AsyncMock())
    center = SimpleNamespace(config=SimpleNamespace(database_path=tmp_path/'news.db', scheduled_news_push_enabled=False))
    notice = AsyncMock()
    monkeypatch.setattr(main, 'load_config', lambda: {'chat_id': 'offline'})
    monkeypatch.setattr('research_center.orchestrator.ResearchCenter', lambda: center)
    monkeypatch.setattr(main, 'NewsRepository', lambda *a: object())
    monkeypatch.setattr(main, '_scheduled_ai_gate', AsyncMock(return_value=''))
    monkeypatch.setattr(main, '_flush_ai_notices', AsyncMock(return_value=0))
    monkeypatch.setattr(main, '_scheduled_outcome_notice', notice)
    monkeypatch.setattr(main, 'run_news_refresh', lambda *a, **k: ([], {'saved': 3}))
    monkeypatch.setattr(main, 'run_news_scheduled_latest', lambda *a: [])
    monkeypatch.setattr(main, 'build_scheduled_news_diagnostics', lambda *a: {
        'primary24': 8, 'display_categories': {key: 1 for key in (
            'market_focus', 'sector_rotation', 'ai_semiconductor', 'supply_chain', 'macro_policy', 'company_news')}})
    monkeypatch.setattr(main, 'format_news_digest', lambda *a, **k: '不推送的新聞')
    asyncio.run(main._scheduled_news_refresh(SimpleNamespace(bot=bot)))
    detail = notice.call_args.args[3]
    assert all(detail[key] is None for key in ('pending', 'classified', 'missing_categories',
                                               'publication_date_gaps', 'search_complete'))
    bot.send_message.assert_not_awaited()
