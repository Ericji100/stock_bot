"""Responses API adapter: scheduled Luna xhigh, interactive Terra xhigh."""
from __future__ import annotations

from decimal import Decimal
import hashlib
import json
import math
import re
import threading
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from .deepseek_service import DeepSeekError, DeepSeekUnavailable, DeepSeekResult, TASK, RUN_ID
from .openai_credit_service import OpenAICreditsUnavailable, OpenAIUnavailable

MODEL = 'gpt-6.1-terra'
BACKGROUND_MODEL = 'gpt-6-luna'
SEARCH_READ_TIMEOUT_SECONDS = 600
MODEL_RATES = {
    MODEL: (Decimal('1'), Decimal('.05'), Decimal('1.25'), Decimal('2.5'), Decimal('5')),
    BACKGROUND_MODEL: (Decimal('.1'), Decimal('.01'), Decimal('.125'), Decimal('.25'), Decimal('.5')),
}
SYSTEM = ('請以繁體中文完成指定任務，區分事實、推論與情境假設；保留來源、日期、'
          '公司對照、風險、反證、資料缺口與可驗證的後續觀察條件。不得編造數值、來源或內部推理草稿。')
_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


class OpenAIError(DeepSeekError):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.diagnostics['provider'] = 'openai'


def model_rates(model):
    for name, rates in MODEL_RATES.items():
        if model == name or model.startswith(name + '-20'):
            return rates
    raise ValueError('未核對模型費率；不得沿用其他模型價格。')


def reserve_price(inputs, outputs, model=MODEL):
    _, _, write, _, output = model_rates(model)
    return (Decimal(inputs) * write * (2 if inputs > 272000 else 1)
            + Decimal(outputs) * output * (Decimal('1.5') if inputs > 272000 else 1)) / 1000000


def price(usage, model=MODEL):
    if not isinstance(usage, dict) or not {'input_tokens', 'output_tokens'} <= usage.keys():
        raise ValueError('缺少官方計費用量。')
    inputs, outputs = int(usage['input_tokens']), int(usage['output_tokens'])
    cached = int((usage.get('input_tokens_details') or {}).get('cached_tokens', 0))
    writes = int((usage.get('input_tokens_details') or {}).get('cache_write_tokens', 0))
    write_details = (usage.get('input_tokens_details') or {}).get('cache_write_tokens_details')
    long_writes = int((write_details or {}).get('ttl_12h_tokens', 0))
    if write_details is not None and (int(write_details.get('ttl_30m_tokens', 0)) + long_writes != writes):
        raise ValueError('官方快取寫入期間用量不一致，費用待核對。')
    if min(inputs, outputs, cached, writes, long_writes) < 0 or long_writes > writes or cached + writes > inputs:
        raise ValueError('官方用量不合法。')
    # output_tokens already includes reasoning_tokens; do not count it twice.
    regular, cached_rate, short_write, long_write, output = model_rates(model)
    multiplier = 2 if inputs > 272000 else 1
    return ((Decimal(inputs - cached - writes) * regular
             + Decimal(writes - long_writes) * short_write
             + Decimal(long_writes) * long_write + Decimal(cached) * cached_rate) * multiplier
            + Decimal(outputs) * output * (Decimal('1.5') if inputs > 272000 else 1)) / 1000000


def output_text(data):
    return '\n'.join(str(part.get('text', '')) for item in data.get('output', [])
        if item.get('type') == 'message' for part in item.get('content', [])
        if part.get('type') == 'output_text').strip()


def billable_search_calls(data):
    # open_page/find_in_page are not search actions. With max_tool_calls, the
    # response may also contain an unprocessed attempt still marked searching.
    return sum(item.get('type') == 'web_search_call' and item.get('status') == 'completed'
               and (item.get('action') or {}).get('type') == 'search'
               for item in data.get('output', []))


def evidence_url(url):
    """Match cited URLs without tracking parameters; retain substantive queries."""
    parsed = urlsplit(url)
    udn_article = (parsed.hostname or '').lower() == 'money.udn.com' and re.fullmatch(r'/money/story/\d+/\d+/?', parsed.path)
    query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
             if not key.lower().startswith('utm_') and key.lower() != 'trk'
             and not (udn_article and key.lower() == 'from')]
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))


def parse_search_response(data):
    """Pure replayable conversion: one cited URL, all model events, explicit orphans."""
    sources = {}
    for item in data.get('output', []):
        candidates = []
        if item.get('type') == 'web_search_call':
            candidates.extend((item.get('action') or {}).get('sources', []))
        if item.get('type') == 'message':
            for part in item.get('content', []):
                candidates.extend(a for a in part.get('annotations', []) if a.get('type') == 'url_citation')
        for source in candidates:
            raw_url = source.get('url')
            if not isinstance(raw_url, str):
                continue
            try:
                key = evidence_url(raw_url)
                if urlsplit(key).scheme not in ('http', 'https') or not urlsplit(key).hostname:
                    continue
            except ValueError:
                continue
            entry = sources.setdefault(key, {'source': {}, 'urls': [], 'events': []})
            entry['source'].update({k: v for k, v in source.items() if v is not None})
            if raw_url not in entry['urls']:
                entry['urls'].append(raw_url)
    text = output_text(data)
    if text.startswith('```') and text.endswith('```'):
        text = text.split('\n', 1)[-1].rsplit('```', 1)[0].strip()
    notes = []
    try:
        parsed = json.loads(text)
        if not isinstance(parsed, dict) or not isinstance(parsed.get('results'), list):
            raise ValueError('Missing results list')
    except (ValueError, TypeError):
        parsed = {'results': []}
        if text:
            notes.append({'kind': 'unparsed_model_text', 'verified': False, 'text': text})
    records = parsed['results']
    for index, row in enumerate(records):
        raw_url = row.get('url') if isinstance(row, dict) else None
        try:
            key = evidence_url(raw_url) if isinstance(raw_url, str) and raw_url else None
        except ValueError:
            key = None
        event = {'result_index': index, 'record': row}
        if key in sources:
            sources[key]['events'].append(event)
        else:
            notes.append({**event, 'kind': 'uncited_url' if raw_url else 'no_url', 'verified': False})
    for key, value in parsed.items():
        if key != 'results':
            notes.append({'kind': 'model_context', 'field': key, 'value': value, 'verified': False})
    results = []
    for url, entry in sources.items():
        source, events = entry['source'], entry['events']
        first = events[0]['record'] if events else {}
        summaries = [str(event['record'].get('summary') or '') for event in events]
        results.append({'title': source.get('title') or first.get('title') or url, 'url': url,
            'search_evidence_url': entry['urls'][0], 'search_evidence_urls': entry['urls'],
            'summary': '\n\n'.join(summaries), 'summary_events': events,
            'date': source.get('published_date'), 'content': '',
            'evidence_kind': 'search_summary_not_original_body', 'provider': 'openai_search',
            'provider_detail': '真實搜尋引用；模型整理摘要不是原文，發布日期未知時不補造。'})
    matched = sum(len(row['summary_events']) for row in results)
    return {'results': results, 'search_context_notes': notes, 'provider': 'openai_search',
            'transmission_audit': {'model_result_count': len(records), 'matched_event_count': matched,
                'unverified_event_count': len(records) - matched, 'source_count': len(results),
                'status': 'prepared_not_sent'}}


class OpenAIService:
    provider = 'openai'

    @property
    def model(self):
        from .search_routing import SEARCH_ROUTE, HYBRID_COMMANDS, command_name
        route = SEARCH_ROUTE.get()
        if route and route['command'] in HYBRID_COMMANDS:
            return MODEL if route['stage'] == 'critical_gap_search' else BACKGROUND_MODEL
        if not self.organizer and command_name(TASK.get()[0]) == 'radar':
            return MODEL
        return BACKGROUND_MODEL if TASK.get()[1] else MODEL

    def __init__(self, ledger, accounts, *, organizer=False, timeout_seconds=180, max_output_tokens=32768, transport=None,
                 validation_snapshot=False, search_timeout_seconds=SEARCH_READ_TIMEOUT_SECONDS):
        self.ledger = ledger
        self.accounts = dict(accounts)
        self.organizer = organizer
        self.timeout_seconds = float(timeout_seconds)
        self.search_timeout_seconds = float(search_timeout_seconds)
        if any(not math.isfinite(value) or value <= 0 for value in (self.timeout_seconds, self.search_timeout_seconds)):
            raise ValueError('OpenAI 等待時間必須是正的有限秒數。')
        self.max_output_tokens = max_output_tokens
        self.transport = transport
        self.validation_snapshot = validation_snapshot
        if validation_snapshot and (ledger.purpose != 'development' or not ledger.round_id or not ledger.case_id):
            raise ValueError('僅具固定輪次與案例的隔離開發驗證可重用本輪搜尋快照。')
        self.max_retries = 0
        with _LOCKS_GUARD:
            self._lock = _LOCKS.setdefault(str(ledger.root.resolve()), threading.RLock())

    def with_timeout(self, seconds):
        return self._clone(timeout_seconds=seconds, search_timeout_seconds=seconds)

    def with_output_limit(self, limit):
        if not 1 <= limit <= 128000:
            raise ValueError('OpenAI 輸出上限必須介於 1 與 128000。')
        return self._clone(max_output_tokens=limit)

    def _clone(self, **kwargs):
        return OpenAIService(self.ledger, self.accounts, organizer=self.organizer,
            timeout_seconds=kwargs.get('timeout_seconds', self.timeout_seconds),
            search_timeout_seconds=kwargs.get('search_timeout_seconds', self.search_timeout_seconds),
            max_output_tokens=kwargs.get('max_output_tokens', self.max_output_tokens), transport=self.transport,
            validation_snapshot=self.validation_snapshot)

    def _request_timeout(self, search):
        read = self.search_timeout_seconds if search else self.timeout_seconds
        return httpx.Timeout(read=read, connect=min(read, 15), write=min(read, 60), pool=min(read, 15))

    def _validation_search_snapshot(self, body):
        if not self.validation_snapshot:
            return None
        with self.ledger.db() as conn:
            rows = conn.execute("SELECT id FROM requests WHERE round_id=? AND case_id=? AND operation='web_search' AND status='success' ORDER BY rowid DESC",
                                (self.ledger.round_id, self.ledger.case_id)).fetchall()
        directory = self.ledger.artifact_root/'requests'
        for (ident,) in rows:
            request_path = directory/(ident+'-request.json')
            response_path = directory/(ident+'-response.json')
            if not request_path.exists() or not response_path.exists():
                continue
            saved = json.loads(request_path.read_text(encoding='utf-8'))
            if saved.get('body') != body:
                continue
            data = json.loads(response_path.read_text(encoding='utf-8'))
            if data.get('status') != 'completed' or not billable_search_calls(data):
                continue
            return data, {'provider': 'openai', 'model': data['model'], 'actual_model': data['model'],
                'requested_model': self.model, 'reasoning_effort': 'xhigh', 'cache_hit': True,
                'cache_scope': 'authorized_validation_round_snapshot', 'original_request_id': ident,
                'original_response_path': str(response_path), 'original_created_at': data.get('created_at'),
                'cost_estimate_usd': '0', 'usage': data['usage']}
        return None

    def is_configured(self):
        return bool(self.accounts)

    def ensure_ready(self):
        self.ledger.choose(self.accounts)

    def prompt_capacity(self, prompt, *, output_tokens=None):
        output = output_tokens or self.max_output_tokens
        upper = len((SYSTEM + prompt).encode('utf-8')) + 4096 + output
        return {'fits': upper <= 1050000, 'context_tokens': 1050000,
                'conservative_total_bound': upper, 'output_reserved_tokens': output,
                'input_utf8_bytes': len(prompt.encode('utf-8')),
                'reference': 'https://developers.openai.com/api/docs/models/' + self.model}

    def verified_prompt_capacity(self, prompt, *, output_tokens=None, json_mode=False):
        capacity = self.prompt_capacity(prompt, output_tokens=output_tokens)
        if capacity['fits']:
            return capacity
        instructions = SYSTEM + ('只輸出有效 JSON 物件。' if json_mode else '')
        count = self.count_input_tokens(prompt, instructions=instructions)
        bound = count + 4096 + capacity['output_reserved_tokens']
        return {**capacity, 'fits': bound <= capacity['context_tokens'],
                'counted_input_tokens': count, 'verified_total_bound': bound,
                'capacity_basis': 'official_input_count_plus_output_and_framing'}

    def count_input_tokens(self, prompt, *, instructions=None):
        """Use the free count-only endpoint, never a generated response."""
        from .ai_execution_control import check_work, bounded_timeout
        check_work()
        body = {'model': self.model, 'instructions': SYSTEM if instructions is None else instructions, 'input': prompt}
        digest = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        key = 'input-count:' + digest
        with self.ledger.db() as conn:
            cached = conn.execute('SELECT value FROM cache WHERE key=? AND expires>?', (key, time.time())).fetchone()
        if cached:
            return int(json.loads(cached[0])['input_tokens'])
        if not any(self.accounts.values()):
            raise OpenAIUnavailable('尚未設定計數所需的 OpenAI 金鑰，未啟動付費分析。', 'input_count_unavailable')
        account = self.ledger.choose(self.accounts)[0]['account_id']
        try:
            check_work()
            with httpx.Client(transport=self.transport, timeout=bounded_timeout(min(self.timeout_seconds, 60))) as client:
                response = client.post('https://api.openai.com/v1/responses/input_tokens',
                    headers={'Authorization': 'Bearer ' + self.accounts[account]}, json=body)
            data = response.json()
            count = data.get('input_tokens')
            if response.status_code != 200 or data.get('object') != 'response.input_tokens' or type(count) is not int or count < 0:
                raise ValueError('官方計數回應無效。')
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise OpenAIUnavailable('無法核對官方輸入 Token，未降低預留額或啟動付費分析。', 'input_count_unavailable') from exc
        receipt = {'request_sha256': digest, 'model': self.model, 'input_tokens': count,
                   'account_id': account, 'counted_at': time.time(), 'cost_usd': '0',
                   'endpoint': '/v1/responses/input_tokens',
                   'reference': 'https://developers.openai.com/api/docs/guides/token-counting'}
        directory = self.ledger.artifact_root/'token-counts'
        directory.mkdir(parents=True, exist_ok=True)
        (directory/(digest+'.json')).write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
        with self.ledger.db() as conn:
            conn.execute('INSERT OR REPLACE INTO cache VALUES (?,?,?)', (key, time.time()+900, json.dumps(receipt)))
        return count

    def estimate_request(self, prompt, *, exact=False):
        # Count-only requests avoid treating every UTF-8 byte as a token when a
        # complete multi-stage workflow appears to exceed its existing cap.
        input_bound = self.count_input_tokens(prompt) + 4096 if exact else len((SYSTEM + prompt).encode('utf-8')) + 8192
        return reserve_price(input_bound, self.max_output_tokens, self.model)

    def _request(self, prompt, *, json_mode=False, search=False):
        with self._lock:
            try:
                return self._request_once(prompt, json_mode=json_mode, search=search)
            except OpenAICreditsUnavailable:
                raise
            except OpenAIUnavailable as exc:
                self.ledger.warn(exc.status, str(exc), operation='web_search' if search else 'model')
                raise

    def _request_once(self, prompt, *, json_mode=False, search=False):
        from .ai_execution_control import check_work, bounded_timeout, WorkStopped
        check_work()
        from .search_routing import stage_metadata
        stage = stage_metadata(organizer=self.organizer)
        model = self.model
        capacity = self.verified_prompt_capacity(prompt, json_mode=json_mode)
        if not capacity['fits']:
            raise OpenAIError('完整資料超過 OpenAI 模型上下文容量；未送出，也未刪減核心資料。', 'prompt_too_large')
        body = {'model': model, 'reasoning': {'effort': 'xhigh'}, 'store': False,
                'truncation': 'disabled', 'service_tier': 'default', 'instructions': SYSTEM,
                'input': prompt, 'max_output_tokens': self.max_output_tokens,
                'prompt_cache_options': {'mode': 'implicit', 'ttl': '30m'}}
        if json_mode:
            body['text'] = {'format': {'type': 'json_object'}}
            body['instructions'] += '只輸出有效 JSON 物件。'
        if search:
            body.update(tools=[{'type': 'web_search', 'external_web_access': True}],
                        tool_choice={'type': 'web_search'}, max_tool_calls=1,
                        include=['web_search_call.action.sources'])
        cache_key = hashlib.sha256(json.dumps([body, TASK.get()], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        with self.ledger.db() as conn:
            cached = conn.execute('SELECT value FROM cache WHERE key=? AND expires>?', (cache_key, time.time())).fetchone()
        if cached:
            data = json.loads(cached[0])
            return data, {'provider': 'openai', 'model': data['model'], 'actual_model': data['model'], 'requested_model': model,
                          'reasoning_effort': 'xhigh', 'cache_hit': True, 'cost_estimate_usd': '0', 'usage': data['usage'], **stage}
        if search and (snapshot := self._validation_search_snapshot(body)):
            return snapshot[0], {**snapshot[1], **stage}
        if search and stage['workflow_stage'] == 'critical_gap_search':
            from .ai_execution_control import require_work_time
            require_work_time(min(60, self.search_timeout_seconds), reason='insufficient_followup_time')
        # UTF-8 byte bound is deliberately conservative, not a claimed tokenizer.
        input_bound = len(json.dumps(body, ensure_ascii=False).encode('utf-8')) + 4096
        if 'counted_input_tokens' in capacity:
            input_bound = capacity['counted_input_tokens'] + 4096
        # Search expansion is provider-controlled. Reserve a full context bound,
        # not a made-up maximum number of returned tokens.
        search_tokens = max(0, 1050000 - self.max_output_tokens - input_bound) if search else 0
        estimate = reserve_price(input_bound + search_tokens, self.max_output_tokens, model)
        if search:
            estimate += Decimal('.01')
        operation = 'web_search' if search else 'model'
        request_timeout = self._request_timeout(search)
        request_timeout = httpx.Timeout(**{key: bounded_timeout(value)
                                         for key, value in request_timeout.as_dict().items()})
        check_work()
        grant, request_id = self.ledger.choose(self.accounts, estimate, reserve=True, operation=operation)
        directory = self.ledger.artifact_root / 'requests'
        detail = {'request_id': request_id, 'run_id': RUN_ID.get(), 'command': TASK.get()[0],
                  'account_id': grant['account_id'], 'grant_id': grant['grant_id'],
                  'provider': 'openai', 'requested_model': model, 'reasoning_effort': 'xhigh',
                  'operation': operation, 'prompt_chars': len(prompt), 'estimated_usd': str(estimate),
                  'client_request_id': request_id, 'request_timeout': request_timeout.as_dict()}
        detail.update(stage)
        if 'counted_input_tokens' in capacity:
            detail['input_capacity_check'] = capacity
        try:
            directory.mkdir(parents=True, exist_ok=True)
            (directory / (request_id + '-request.json')).write_text(json.dumps({**detail, 'body': body}, ensure_ascii=False, indent=2), encoding='utf-8')
            with self.ledger.db() as conn:
                conn.execute("UPDATE requests SET detail=? WHERE id=? AND status='pending'",
                             (json.dumps(detail, ensure_ascii=False), request_id))
        except OSError:
            self.ledger.finish(request_id, 'not_sent', Decimal(0), detail)
            raise OpenAIUnavailable('無法保存 OpenAI 請求紀錄，未送出付費請求。', 'request_log_unavailable') from None
        started = time.monotonic()
        try:
            check_work()
            with httpx.Client(timeout=request_timeout, transport=self.transport) as client:
                check_work()
                response = client.post('https://api.openai.com/v1/responses',
                    headers={'Authorization': 'Bearer ' + self.accounts[grant['account_id']],
                             'X-Client-Request-Id': request_id}, json=body)
        except WorkStopped:
            self.ledger.finish(request_id, 'not_sent', Decimal(0), {**detail, 'http_sent': False})
            raise
        except httpx.TransportError as exc:
            detail.update(status='transport_unknown', transport_error_type=type(exc).__name__,
                          elapsed_seconds=round(time.monotonic() - started, 3))
            self.ledger.finish(request_id, 'unknown', None, detail)
            error = OpenAIUnavailable('OpenAI 連線逾時／中斷，費用未知；保留成果，不跨帳戶或模型重送。', 'transport_unknown')
            error.diagnostics.update(detail)
            raise error from None
        detail.update(elapsed_seconds=round(time.monotonic() - started, 3),
                      provider_request_id=response.headers.get('x-request-id', ''))
        detail['http_status'] = response.status_code
        try:
            data = response.json()
            (directory / (request_id + '-response.json')).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        except (ValueError, OSError):
            self.ledger.finish(request_id, 'unknown', None, {**detail, 'status': 'response_invalid'})
            raise OpenAIUnavailable('OpenAI 回應無法解析／保存，費用待核對，不重送。', 'transport_unknown') from None
        if not isinstance(data, dict):
            self.ledger.finish(request_id, 'unknown', None, {**detail, 'status': 'response_invalid'})
            raise OpenAIUnavailable('OpenAI 回應結構不符，費用待核對，不重送。', 'transport_unknown')
        if response.status_code >= 400:
            code = str((data.get('error') or {}).get('code') or '')
            if code == 'credit_balance_exhausted':
                self.ledger.finish(request_id, 'rejected', Decimal(0), {**detail, 'error_code': code})
                self.ledger.block(grant, code)
                return self._request(prompt, json_mode=json_mode, search=search)
            # 5xx may have reached execution. Preserve its reservation as unknown.
            unknown = response.status_code >= 500 or response.status_code in (408, 409)
            status = 'transport_unknown' if unknown else 'rate_limited' if response.status_code == 429 else 'authentication_failed' if response.status_code in (401, 403) else 'http_error'
            self.ledger.finish(request_id, 'unknown' if unknown else 'rejected', None if unknown else Decimal(0), {**detail, 'error_code': code})
            raise OpenAIUnavailable(f'OpenAI 請求受阻（HTTP {response.status_code}）；不切帳戶繞過限流或權限。', status, response.status_code)
        actual_model = str(data.get('model') or '')
        if actual_model != model and not actual_model.startswith(model + '-20'):
            self.ledger.finish(request_id, 'unknown', None, {**detail, 'actual_model': actual_model})
            raise OpenAIUnavailable('OpenAI 回傳模型與本次指定設定不符，費用待核對，不視為成功。', 'transport_unknown')
        try:
            actual = price(data.get('usage'), actual_model)
            calls = billable_search_calls(data)
            actual += Decimal(calls) * Decimal('.01')
        except (TypeError, ValueError, KeyError):
            self.ledger.finish(request_id, 'unknown', None, {**detail, 'status': 'usage_missing'})
            raise OpenAIUnavailable('OpenAI 缺少有效用量，費用未知，不重送。', 'transport_unknown') from None
        detail.update(actual_model=data.get('model'), usage=data['usage'], search_calls=calls,
                      cost_estimate_usd=str(actual), cache_hit=False, finish_reason=data.get('status'),
                      response_path=str(directory / (request_id + '-response.json')))
        text = output_text(data)
        complete = data.get('status') == 'completed' and bool(text)
        self.ledger.finish(request_id, 'success' if complete else 'incomplete', actual, detail)
        if not complete:
            error = OpenAIError('OpenAI 正式輸出未完成；已保存原始回應與費用，不冒充成功。', 'incomplete_output')
            error.diagnostics.update(detail, finish_reason='length' if (data.get('incomplete_details') or {}).get('reason') == 'max_output_tokens' else data.get('status'))
            raise error
        if search and not calls:
            raise OpenAIError('OpenAI 未執行真實搜尋，不將模型記憶冒充搜尋來源。', 'search_not_executed')
        if json_mode:
            try:
                if not isinstance(json.loads(text), dict):
                    raise ValueError()
            except ValueError:
                error = OpenAIError('OpenAI JSON 不完整；保留原始回應，不快取失敗內容。', 'invalid_json')
                error.diagnostics.update(detail)
                raise error from None
        with self.ledger.db() as conn:
            conn.execute('INSERT OR REPLACE INTO cache VALUES (?,?,?)', (cache_key, time.time() + (900 if search else 86400), json.dumps(data, ensure_ascii=False)))
        return data, detail

    def generate_report(self, prompt, **kwargs):
        data, diagnostics = self._request(prompt)
        return DeepSeekResult(output_text(data), data, diagnostics)

    def generate_json(self, prompt):
        data, diagnostics = self._request(prompt, json_mode=True)
        # Existing classifiers consume Chat Completions-shaped raw content.
        raw = {**data, 'choices': [{'message': {'content': output_text(data)}, 'finish_reason': 'stop'}]}
        return DeepSeekResult(output_text(data), raw, diagnostics)

    def search(self, query, report_date=''):
        from .search_routing import SEARCH_ROUTE, search_route
        prompt = ('請實際搜尋網路，不要只依模型記憶。查詢：' + query + '\n分析截止日：' + (report_date or '最新') +
                  '\n輸出 JSON 物件 results：各筆含 title、url、summary。摘要須依搜尋證據，勿虛構正文或發布日期。')
        # Hosted web search rejects JSON mode. Ask for JSON in plain output;
        # only tool/citation URLs below are accepted as actual search results.
        route = SEARCH_ROUTE.get() or {'command': TASK.get()[0], 'stage': 'initial_search', 'reason': ''}
        with search_route(**route):
            data, diagnostics = self._request(prompt, search=True)
        return {**parse_search_response(data), 'diagnostics': diagnostics}
