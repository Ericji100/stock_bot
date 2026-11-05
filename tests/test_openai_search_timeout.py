from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import httpx
import pytest

from research_center.ai_provider_service import RoutedAIService
from research_center.deepseek_service import ai_task
from research_center.openai_credit_service import OpenAICreditLedger, OpenAIUnavailable
from research_center.openai_service import OpenAIService


def make_client(handler):
    root = Path(os.environ.get('CODEX_TASK_TEST_ROOT', '.cache/test_tmp')) / uuid4().hex
    grant = dict(grant_id='fixture', account_id='secondary', organization_id='fixture',
                 starts_at='2026-11-01T00:00:00Z', expires_at='2026-12-01T00:00:00Z',
                 verified_at='2026-11-01T00:00:00Z', evidence='synthetic', allowance_usd='100')
    ledger = OpenAICreditLedger(root, [grant], job_limit=0,
        now=lambda: datetime(2026, 11, 5, tzinfo=timezone.utc))
    return OpenAIService(ledger, {'secondary': 'synthetic-key'}, transport=httpx.MockTransport(handler))


def completed(request):
    body = json.loads(request.content)
    output = [{'type': 'message', 'content': [{'type': 'output_text', 'text': '{"results": []}'}]}]
    if body.get('tools'):
        output.insert(0, {'type': 'web_search_call', 'status': 'completed', 'action': {'type': 'search', 'sources': []}})
    return httpx.Response(200, headers={'x-request-id': 'req_synthetic'}, json={
        'id': 'resp_synthetic', 'model': body['model'], 'status': 'completed', 'output': output,
        'usage': {'input_tokens': 1000, 'output_tokens': 100}})


@pytest.mark.parametrize('command', ['radar', 'news', 'topic_maintain', 'research'])
def test_search_can_wait_past_old_180_seconds_without_resending(command):
    calls = []
    def handler(request):
        calls.append(request)
        # Virtual provider latency: test configuration, not a real 201-second wait.
        if request.extensions['timeout']['read'] < 201:
            raise httpx.ReadTimeout('synthetic response after 201 seconds', request=request)
        assert request.extensions['timeout'] == {'read': 600.0, 'connect': 15.0, 'write': 60.0, 'pool': 15.0}
        body = json.loads(request.content)
        assert body['reasoning'] == {'effort': 'xhigh'}
        assert body['truncation'] == 'disabled' and not body['store']
        assert '不可刪除的核心資料' in body['input']
        return completed(request)
    client = make_client(handler)
    with ai_task(command):
        result = client.search('不可刪除的核心資料', '2026-11-04')
    assert len(calls) == 1
    detail = result['diagnostics']
    assert detail['client_request_id'] == calls[0].headers['X-Client-Request-Id']
    assert detail['provider_request_id'] == 'req_synthetic'
    assert detail['request_timeout']['read'] == 600
    assert detail['elapsed_seconds'] >= 0


def test_analysis_keeps_180_seconds_and_clones_keep_search_budget():
    calls = []
    def handler(request):
        calls.append(request)
        return completed(request)
    original = make_client(handler)
    client = original.with_output_limit(12000)
    with ai_task('radar'):
        client.generate_report('完整分析')
    assert calls[0].extensions['timeout']['read'] == 180
    assert client.search_timeout_seconds == 600
    assert original.search_timeout_seconds == 600


def test_explicit_timeout_caps_search_as_well_as_analysis():
    client = make_client(completed).with_timeout(45).with_output_limit(12000)
    assert client.timeout_seconds == client.search_timeout_seconds == 45
    assert client._request_timeout(True).read == 45
    assert client._request_timeout(False).read == 45


@pytest.mark.parametrize('error_class', [httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout])
def test_timeout_preserves_reservation_diagnostics_and_does_not_fallback_or_retry(error_class):
    calls = []
    def handler(request):
        calls.append(request)
        raise error_class('synthetic failure', request=request)
    client = make_client(handler)
    fallback = Mock(model='deepseek-flash', organizer=False, timeout_seconds=180, max_output_tokens=32768)
    routed = RoutedAIService(client, fallback)
    with ai_task('radar'):
        with pytest.raises(OpenAIUnavailable) as caught:
            routed._post('v1/tools/search', {'query': '完整資料'}, Decimal('.01'))
        with pytest.raises(OpenAIUnavailable):
            client.search('完整資料', '2026-11-04')
    fallback._post.assert_not_called()
    assert len(calls) == 1 and client.max_retries == 0
    assert caught.value.status == 'transport_unknown'
    assert caught.value.diagnostics['transport_error_type'] == error_class.__name__
    with client.ledger.db() as conn:
        rows = conn.execute('SELECT status,estimate,actual,detail FROM requests').fetchall()
    assert len(rows) == 1 and rows[0][0] == 'unknown' and rows[0][2] is None
    assert Decimal(rows[0][1]) > 0
    detail = json.loads(rows[0][3])
    assert detail['client_request_id'] == calls[0].headers['X-Client-Request-Id']
    assert detail['request_timeout']['read'] == 600
    assert detail['transport_error_type'] == error_class.__name__
    assert 'synthetic-key' not in rows[0][3]


@pytest.mark.parametrize('seconds', [0, -1, float('nan'), float('inf')])
def test_invalid_search_timeout_rejected_before_request(seconds):
    client = make_client(completed)
    with pytest.raises(ValueError):
        client._clone(search_timeout_seconds=seconds)
