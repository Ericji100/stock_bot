"""Single routing boundary reused by reports, news, radar and topic upkeep."""
from __future__ import annotations

from contextvars import ContextVar
from decimal import Decimal
import os
from pathlib import Path
import threading

from .deepseek_service import (DeepSeekAccount, DeepSeekService, DeepSeekUnavailable,
                               account_options, TASK, RUN_ID)
from .openai_credit_service import OpenAICreditLedger, OpenAICreditsUnavailable
from .openai_service import OpenAIService, MODEL
from .ai_budget_service import BudgetBlocked, REASONS

LAST_MODEL = ContextVar('routed_actual_model', default=('', ''))
ROUTE_HISTORY = ContextVar('ai_route_history', default=())


def _fallback_unavailable(primary, fallback, **diagnostics):
    error = DeepSeekUnavailable(f'{primary}；備援也不可用：{fallback}', fallback.status)
    error.diagnostics.update(fallback.diagnostics, primary_status=primary.status, **diagnostics)
    return error


class BudgetNotices:
    def __init__(self, openai, deepseek):
        self.openai, self.deepseek = openai, deepseek

    def claim_notice(self):
        if self.openai.purpose != 'production':
            return None
        for provider, budget in [('openai', self.openai), ('deepseek', self.deepseek)]:
            notice = budget.claim_notice()
            if notice:
                return provider + ':' + notice[0], notice[1]

    def notice_result(self, notice_id, success):
        if self.openai.purpose != 'production':
            return False
        provider, ident = notice_id.split(':', 1)
        if provider not in {'openai', 'deepseek'}:
            raise ValueError('Unknown AI notice provider')
        return (self.openai if provider == 'openai' else self.deepseek).notice_result(ident, success)

    def summary(self, *args, **kwargs):
        return {**self.deepseek.summary(*args, **kwargs), 'openai': self.openai.summary()}


class RoutedAccount(DeepSeekAccount):
    """Compatibility for existing caches/notifiers, without DeepSeek preflight first."""
    def __init__(self, service):
        self.service = service
        self.root = service.primary.ledger.artifact_root / 'workflow'
        self.key = bool(service.primary.accounts or service.fallback.account.key)
        self.lock = threading.RLock()
        self.request_locks = {}
        self.budget = BudgetNotices(service.primary.ledger, service.fallback.account.budget)

    def preflight(self, estimate=0):
        try:
            return self.service.primary.ledger.choose(self.service.primary.accounts, estimate)
        except OpenAICreditsUnavailable as exc:
            self.service.primary.ledger.route('deepseek', exc.status)
            try:
                return self.service._fallback_preflight(estimate)
            except DeepSeekUnavailable as fallback_exc:
                raise _fallback_unavailable(exc, fallback_exc) from fallback_exc


class RoutedAIService:
    provider = 'openai_router'

    @property
    def model(self):
        return self.primary.model

    def __init__(self, primary, fallback):
        self.primary, self.fallback = primary, fallback
        self.account = RoutedAccount(self)
        self.timeout_seconds = primary.timeout_seconds
        self.max_output_tokens = primary.max_output_tokens
        self.organizer = primary.organizer

    @property
    def actual_model(self):
        run_id, model = LAST_MODEL.get()
        return model if run_id == RUN_ID.get() and model else self.model

    @property
    def routing_history(self):
        return [dict(row) for row in ROUTE_HISTORY.get() if row['run_id'] == RUN_ID.get()]

    def is_configured(self):
        return bool(self.primary.is_configured() or self.fallback.is_configured())

    def ensure_ready(self):
        return self.account.preflight()

    def _fallback_preflight(self, estimate=0):
        try:
            self.fallback.account.budget.check(estimate=Decimal(str(estimate)),
                background=TASK.get()[1], run_id=RUN_ID.get())
        except BudgetBlocked as exc:
            self.fallback.account.budget.incident(exc.reason, background=TASK.get()[1])
            raise DeepSeekUnavailable(REASONS[exc.reason], exc.reason) from None
        return self.fallback.account.preflight()

    def prompt_capacity(self, prompt, *, output_tokens=None):
        primary = self.primary.prompt_capacity(prompt, output_tokens=output_tokens)
        fallback = self.fallback.prompt_capacity(prompt, output_tokens=output_tokens)
        # A smaller fallback must not reduce the primary model's data capacity.
        # Its own adapter still refuses an oversized packet without trimming.
        return {**primary, 'fallback_capacity': fallback}

    def with_timeout(self, seconds):
        return RoutedAIService(self.primary.with_timeout(seconds), self.fallback.with_timeout(seconds))

    def with_output_limit(self, limit):
        return RoutedAIService(self.primary.with_output_limit(limit), self.fallback.with_output_limit(limit))

    def _record(self, provider, model, reason='', diagnostics=None):
        LAST_MODEL.set((RUN_ID.get(), model))
        row = {'run_id': RUN_ID.get(), 'provider': provider, 'model': model, 'fallback_reason': reason}
        row.update({key: value for key, value in (diagnostics or {}).items()
                    if key in {'workflow_stage', 'search_reason', 'search_command', 'cache_hit'}})
        history = tuple(r for r in ROUTE_HISTORY.get() if r['run_id'] == RUN_ID.get())
        ROUTE_HISTORY.set(history + (row,))
        self.primary.ledger.route(provider, reason, {'model': model})

    def _generate(self, method, prompt, **kwargs):
        try:
            pinned = self.account.cached('route:' + RUN_ID.get()) if RUN_ID.get() else None
            if pinned:
                raise OpenAICreditsUnavailable('已依完整已知階段估費，改用 DeepSeek 備援。', pinned['reason'])
            result = getattr(self.primary, method)(prompt, **kwargs)
            self._record('openai', result.diagnostics['actual_model'], diagnostics=result.diagnostics)
        except OpenAICreditsUnavailable as exc:
            # Never catch timeout, malformed replies, rate limits, auth or generic errors here.
            self.primary.ledger.route('deepseek', exc.status)
            try:
                result = getattr(self.fallback, method)(prompt, **kwargs)
            except DeepSeekUnavailable as fallback_exc:
                raise _fallback_unavailable(exc, fallback_exc) from fallback_exc
            self._record('deepseek', result.diagnostics['actual_model'], exc.status)
            result.diagnostics['fallback_reason'] = exc.status
        result.diagnostics.update(requested_model=self.model, routing_history=self.routing_history)
        return result

    def preflight_workflow(self, prompts):
        """Check known stages together before paying; generated text is rechecked later."""
        estimate = sum((self.primary.estimate_request(p) for p in prompts))
        try:
            self.primary.ledger.choose(self.primary.accounts, estimate)
            return {'provider': 'openai', 'estimated_usd': str(estimate)}
        except OpenAICreditsUnavailable as exc:
            if exc.status == 'openai_job_budget_exceeded':
                estimate = sum(self.primary.estimate_request(p, exact=True) for p in prompts)
                try:
                    self.primary.ledger.choose(self.primary.accounts, estimate)
                except OpenAICreditsUnavailable as counted_exc:
                    exc = counted_exc
                else:
                    return {'provider': 'openai', 'estimated_usd': str(estimate),
                            'estimate_basis': 'official_input_count_plus_full_output_reserve'}
            from .deepseek_service import _reserve_model_price
            fallback_estimate = sum((_reserve_model_price(len(p) + 1024, self.max_output_tokens,
                                    self.timeout_seconds * 2 + 30) for p in prompts))
            try:
                self._fallback_preflight(fallback_estimate)
            except DeepSeekUnavailable as fallback_exc:
                raise _fallback_unavailable(exc, fallback_exc, primary_preflight_status=exc.status,
                    primary_workflow_estimate_usd=str(estimate)) from fallback_exc
            if RUN_ID.get():
                self.account.cache('route:' + RUN_ID.get(), {'reason': exc.status}, 86400)
            self.primary.ledger.route('deepseek', exc.status, {'known_workflow_estimate_usd': str(estimate)})
            return {'provider': 'deepseek', 'estimated_usd': str(fallback_estimate), 'reason': exc.status}

    def generate_report(self, prompt, **kwargs):
        return self._generate('generate_report', prompt, **kwargs)

    def generate_json(self, prompt):
        return self._generate('generate_json', prompt)

    def _post(self, operation, body, estimate):
        if operation.endswith('search'):
            try:
                result = self.primary.search(body['query'], TASK.get()[2])
                self._record('openai', result['diagnostics']['actual_model'], diagnostics=result['diagnostics'])
                return result
            except OpenAICreditsUnavailable as exc:
                self.primary.ledger.route('deepseek', exc.status)
                try:
                    result = self.fallback._post(operation, body, estimate)
                except DeepSeekUnavailable as fallback_exc:
                    raise _fallback_unavailable(exc, fallback_exc) from fallback_exc
                from .search_routing import stage_metadata
                diagnostics = {**(result.get('diagnostics') or {}), **stage_metadata(),
                    'actual_model': self.fallback.model, 'fallback_reason': exc.status}
                self._record('deepseek', self.fallback.model, exc.status, diagnostics=diagnostics)
                return {**result, 'provider': 'deepseek_search', 'diagnostics': diagnostics}
        if operation.endswith('fetch'):
            # Hosted search output is NOT original article text. Keep free direct
            # fetch as the primary path; do not pass a generated summary as a body.
            from .web_fetch_service import _fetch_with_requests
            from dataclasses import asdict
            result = _fetch_with_requests(body['url'], min(self.timeout_seconds, 20))
            if result.content_status == 'success':
                return {**asdict(result), 'provider': 'requests_bs4'}
            try:
                self.primary.ensure_ready()
            except OpenAICreditsUnavailable as exc:
                self.primary.ledger.route('deepseek', exc.status)
                try:
                    fetched = self.fallback._post(operation, body, estimate)
                except DeepSeekUnavailable as fallback_exc:
                    raise _fallback_unavailable(exc, fallback_exc) from fallback_exc
                return {**fetched, 'provider': 'deepseek_fetch'}
            return {**asdict(result), 'provider': 'requests_bs4'}
        raise ValueError('未支援的共用 AI 操作。')


def create_ai_client(config, root, *, organizer=False, transport=None):
    root = Path(root)
    options = account_options(config, root)
    account = DeepSeekAccount.shared(config.deepseek_api_key, root / '.cache/deepseek', **options)
    fallback = DeepSeekService(account, model=config.deepseek_model, organizer=organizer,
                               max_output_tokens=8192 if organizer else 32768)
    if not getattr(config, 'openai_primary_enabled', False):
        return fallback
    from .ai_budget_service import shared_budget_root
    storage = (options['budget_root'] if os.environ.get('DEEPSEEK_BUDGET_TEST_ROOT') else shared_budget_root(root)) / 'openai'
    artifacts = options['budget_root'] / 'openai-artifacts' if options['development'] else storage
    ledger = OpenAICreditLedger(storage, config.openai_credit_grants, job_limit=config.openai_job_limit_usd,
                                development=options['development'], artifact_root=artifacts,
                                round_id=os.environ.get('OPENAI_LIVE_ROUND_ID', ''),
                                round_limit=os.environ.get('OPENAI_LIVE_ROUND_LIMIT_USD'),
                                case_id=os.environ.get('OPENAI_LIVE_CASE_ID', ''))
    primary = OpenAIService(ledger, config.openai_api_keys, organizer=organizer,
                            max_output_tokens=32768, transport=transport,
                            validation_snapshot=os.environ.get('OPENAI_LIVE_RESUME_SNAPSHOT') == '1')
    return RoutedAIService(primary, fallback)


def current_routing_metadata():
    rows = [dict(row) for row in ROUTE_HISTORY.get() if row['run_id'] == RUN_ID.get()]
    return {'actual_model': rows[-1]['model'] if rows else '', 'routing_history': rows}


def resolved_model(client, default='deepseek-flash'):
    value = getattr(client, 'actual_model', None)
    if not isinstance(value, str) or not value:
        value = getattr(client, 'model', default)
    return value if isinstance(value, str) and value else default
