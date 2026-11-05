"""Official DeepSeek transport, shared balance guard, cache and usage ledger."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
import hashlib
import inspect
from functools import wraps
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
from .ai_budget_service import MonthlyBudget, BudgetBlocked, REASONS

TASK = ContextVar("deepseek_task", default=("interactive", False, ""))
RUN_ID = ContextVar("deepseek_run_id", default="")
DEVELOPMENT_ACCOUNTING = ContextVar('deepseek_development_accounting', default=False)


@contextmanager
def development_accounting():
    """Validation entrypoints must never inherit the production spending scope."""
    token = DEVELOPMENT_ACCOUNTING.set(True)
    try:
        yield
    finally:
        DEVELOPMENT_ACCOUNTING.reset(token)


@contextmanager
def ai_task(command: str, background: bool = False, report_date: str = ""):
    token = TASK.set((command, background, report_date))
    run_token = RUN_ID.set(RUN_ID.get() or uuid4().hex)
    try:
        yield
    finally:
        TASK.reset(token)
        RUN_ID.reset(run_token)


def task_context(command: str = "", *, background: bool | None = None):
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            request = next((a for a in args if hasattr(a, "command") and hasattr(a, "report_date")), None)
            current = TASK.get()
            label = command or (request.command if request else current[0])
            day = request.report_date.isoformat() if request and request.report_date else current[2]
            with ai_task(label, current[1] if background is None else background, day):
                return function(*args, **kwargs)
        if inspect.iscoroutinefunction(function):
            @wraps(function)
            async def async_wrapped(*args, **kwargs):
                with ai_task(command or TASK.get()[0], TASK.get()[1] if background is None else background, TASK.get()[2]):
                    return await function(*args, **kwargs)
            return async_wrapped
        return wrapped
    return decorate


class DeepSeekError(RuntimeError):
    def __init__(self, message: str, status: str = "failed", http_status: int | None = None):
        super().__init__(message)
        self.status = status
        self.http_status = http_status
        self.diagnostics = {"provider": "deepseek", "status": status, "http_status": http_status}


class DeepSeekUnavailable(DeepSeekError):
    """Do not retry in outer model/segment loops until the account recovers."""


@dataclass(frozen=True)
class DeepSeekResult:
    markdown: str
    raw: dict[str, Any]
    diagnostics: dict[str, Any]


class DeepSeekAccount:
    _instances: dict[tuple[str, str], "DeepSeekAccount"] = {}
    _instances_lock = threading.Lock()

    @classmethod
    def shared(cls, key: str | None, root: Path, **kwargs):
        identity = (str(Path(kwargs.get('usage_root') or root).resolve()) + '|' + str(Path(kwargs.get('budget_root') or root).resolve()), hashlib.sha256((key or "").encode()).hexdigest())
        with cls._instances_lock:
            if identity not in cls._instances:
                cls._instances[identity] = cls(key, root, **kwargs)
            return cls._instances[identity]

    def __init__(self, key: str | None, root: Path, *, balance_ttl: float = 60,
                 background_reserve_usd: float = 0.20, daily_limit_usd: float = 0.0,
                 usd_cny_budget_rate: float = 7.5,
                 budget_root=None, monthly_limit_usd=10, background_monthly_usd=7,
                 manual_monthly_usd=2, monthly_reserve_usd=1, job_limit_usd=.30, schedule_pacing=True,
                 manual_allowances=None,
                 usage_root=None, development=False,
                 transport=None):
        self.key = key
        self.root = Path(usage_root or root)
        self.balance_ttl = balance_ttl
        self.background_reserve = Decimal(str(background_reserve_usd))
        self.daily_limit = Decimal(str(daily_limit_usd))
        self.usd_cny_budget_rate = Decimal(str(usd_cny_budget_rate))
        self.transport = transport
        self.budget = MonthlyBudget(budget_root or self.root, total=monthly_limit_usd,
            background=background_monthly_usd, manual=manual_monthly_usd, reserve=monthly_reserve_usd,
            job_limit=job_limit_usd, pacing=schedule_pacing, manual_allowances=manual_allowances,
            enforce_limits=not development)
        if not development:
            self.budget.import_legacy(self.root / "usage.sqlite3")
        self.lock = threading.RLock()
        self.balance_checked_at = 0.0
        self.available: bool | None = None
        self.balances: list[dict[str, str]] = []
        self.block_reason = ""
        self.cooldown_until = 0.0
        self.cooldown_reason = "rate_limited"
        self.pending: dict[str, Decimal] = {}
        self.request_locks: dict[str, threading.RLock] = {}
        self.spent_since_check = Decimal("0")

    def singleflight(self, key: str):
        with self.lock:
            return self.request_locks.setdefault(key, threading.RLock())

    @contextmanager
    def _db(self):
        self.root.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.root / "usage.sqlite3", timeout=15)
        conn.execute("CREATE TABLE IF NOT EXISTS usage (id TEXT PRIMARY KEY, day TEXT, command TEXT, background INTEGER, operation TEXT, status TEXT, estimated_usd TEXT, actual_usd TEXT, detail TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, expires REAL, value TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS activity (id TEXT PRIMARY KEY, command TEXT, operation TEXT, cache_hit INTEGER, detail TEXT)")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def cached(self, key: str) -> dict | None:
        with self.lock, self._db() as conn:
            row = conn.execute("SELECT value FROM cache WHERE key=? AND expires>?", (key, time.time())).fetchone()
        return json.loads(row[0]) if row else None

    def cache(self, key: str, value: dict, ttl: float):
        with self.lock, self._db() as conn:
            conn.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?)", (key, time.time() + ttl, json.dumps(value, ensure_ascii=False)))

    def activity(self, operation, cache_hit, detail):
        with self.lock, self._db() as conn:
            conn.execute("INSERT INTO activity VALUES (?,?,?,?,?)", (uuid4().hex,TASK.get()[0],operation,int(cache_hit),
                         json.dumps({**detail, 'run_id': RUN_ID.get()},ensure_ascii=False)))

    def refresh_balance(self, *, force: bool = False):
        with self.lock:
            if not self.key:
                raise DeepSeekUnavailable("尚未設定 DeepSeek 官方 API Key。", "not_configured")
            if not force and time.time() < self.cooldown_until:
                reason = self.cooldown_reason
                raise DeepSeekUnavailable(REASONS[reason], reason, 429 if reason == "rate_limited" else None)
            if not force and self.available is not None and time.monotonic() - self.balance_checked_at < self.balance_ttl:
                if not self.available:
                    raise DeepSeekUnavailable(self.block_reason, "insufficient_balance" if "餘額" in self.block_reason else "authentication_failed")
                return self.balances
            try:
                with httpx.Client(timeout=10, transport=self.transport) as client:
                    response = client.get("https://api.deepseek.com/user/balance", headers={"Authorization": f"Bearer {self.key}"})
                if response.status_code in (401, 402):
                    self.block(response.status_code)
                response.raise_for_status()
                data = response.json()
                self.available = data.get("is_available") is True
                self.balances = data.get("balance_infos") or []
                self.balance_checked_at = time.monotonic()
                self.spent_since_check = Decimal("0")
                if not self.available:
                    self.block_reason = "DeepSeek 餘額不足；已保留資料，儲值後重新執行即可。"
                    raise DeepSeekUnavailable(self.block_reason, "insufficient_balance", 402)
                return self.balances
            except DeepSeekError:
                raise
            except Exception:
                raise DeepSeekUnavailable("DeepSeek 餘額查詢暫時失敗，未開始新的付費請求。", "balance_unknown") from None

    def block(self, status: int):
        self.available = False
        self.balance_checked_at = time.monotonic()
        self.block_reason = ("DeepSeek 餘額不足；已保留資料，儲值後重新執行即可。" if status == 402
                             else "DeepSeek 金鑰驗證失敗，請檢查本機設定。")
        self.budget.incident("insufficient_balance" if status == 402 else "authentication_failed", background=TASK.get()[1])
        raise DeepSeekUnavailable(self.block_reason, "insufficient_balance" if status == 402 else "authentication_failed", status)

    def preflight(self):
        background = TASK.get()[1]
        try:
            self.budget.check(background=background, run_id=RUN_ID.get())
            result = self.refresh_balance()
            self.budget.incident(None, background=background, preflight=True)
            return result
        except BudgetBlocked as exc:
            self.budget.incident(exc.reason, background=background)
            raise DeepSeekUnavailable(REASONS[exc.reason], exc.reason) from None
        except DeepSeekUnavailable as exc:
            self.budget.incident(exc.status, background=background)
            raise

    def reserve(self, operation: str, estimate: Decimal) -> str:
        try:
            return self._reserve(operation, estimate)
        except BudgetBlocked as exc:
            self.budget.incident(exc.reason, background=TASK.get()[1])
            raise DeepSeekUnavailable(REASONS[exc.reason], exc.reason) from None
        except DeepSeekUnavailable as exc:
            self.budget.incident(exc.status, background=TASK.get()[1])
            raise

    def _reserve(self, operation: str, estimate: Decimal) -> str:
        with self.lock:
            self.refresh_balance()
            command, background, _ = TASK.get()
            usd = next((Decimal(b["total_balance"]) for b in self.balances if b.get("currency") == "USD"), None)
            if usd is None and self.usd_cny_budget_rate > 0:
                cny = next((Decimal(b["total_balance"]) for b in self.balances if b.get("currency") == "CNY"), None)
                if cny is not None:
                    usd = cny / self.usd_cny_budget_rate
            # Conversion is a configurable budget assumption, not a bank exchange quote.
            floor = self.background_reserve if background else Decimal("0")
            if usd is not None and usd - self.spent_since_check - sum(self.pending.values()) < estimate + floor:
                raise DeepSeekUnavailable("DeepSeek 可用餘額低於本次預估需求；已保留進度。", "low_balance")
            now = datetime.now(ZoneInfo("Asia/Taipei"))
            request_id = uuid4().hex
            with self._db() as conn:
                conn.execute("BEGIN IMMEDIATE")
                day = now.date().isoformat()
                rows = conn.execute("SELECT status, estimated_usd, actual_usd FROM usage WHERE day=?", (day,)).fetchall()
                used = sum(Decimal(actual if actual is not None else est) for status, est, actual in rows if status not in ("rejected", "failed"))
                if self.daily_limit > 0 and used + estimate > self.daily_limit:
                    raise DeepSeekUnavailable("DeepSeek 已達每日費用保護上限，請調整設定或明日再試。", "daily_budget_exceeded")
                self.budget.reserve_request(request_id, command, background, RUN_ID.get(), estimate, now)
                conn.execute("INSERT INTO usage VALUES (?,?,?,?,?,?,?,?,?)", (request_id, day, command, int(background), operation, "pending", str(estimate), None,
                             json.dumps({'run_id': RUN_ID.get()})))
            self.pending[request_id] = estimate
            return request_id

    def finish(self, request_id: str, status: str, detail: dict, actual: Decimal | None = None):
        with self.lock:
            estimate = self.pending.pop(request_id, Decimal("0"))
            if status == "unknown":
                self.spent_since_check += estimate
            elif actual is not None:
                self.spent_since_check += actual
            with self._db() as conn:
                conn.execute("UPDATE usage SET status=?, actual_usd=?, detail=? WHERE id=?", (status, str(actual) if actual is not None else None, json.dumps(detail, ensure_ascii=False), request_id))
            self.budget.finish(request_id, status, actual)


def _request_body_sha256(body):
    return hashlib.sha256(json.dumps(body,ensure_ascii=False,sort_keys=True).encode('utf-8')).hexdigest()


def _model_price_factor(now=None, horizon_seconds=0) -> Decimal:
    now = now or datetime.now(ZoneInfo('Asia/Taipei'))
    # Ignore holiday discounts conservatively; include any peak in the request window.
    minutes = max(0, int(horizon_seconds + 59) // 60)
    for minute in range(minutes + 1):
        candidate = now + timedelta(minutes=minute)
        if candidate.weekday() < 5 and (9 <= candidate.hour < 12 or 14 <= candidate.hour < 18):
            return Decimal('2')
    return Decimal('1')


def _reserve_model_price(chars, output_tokens, horizon_seconds=0, now=None):
    # One input token per character is a conservative estimate for these JSON/text
    # packets, not an exact tokenizer or an invoice. Reserve the entire output cap.
    return (Decimal(chars) * Decimal('0.15') + Decimal(output_tokens) * Decimal('0.60')) * _model_price_factor(now, horizon_seconds) / 1000000


def _price(usage: dict) -> Decimal:
    factor = _model_price_factor()
    total = int(usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0)
    hit = int(usage.get("prompt_cache_hit_tokens", (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)) or 0)
    out = int(usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0)
    return (Decimal(max(0, total - hit)) * Decimal("0.15") + Decimal(hit) * Decimal("0.003") + Decimal(out) * Decimal("0.60")) * factor / 1000000


class DeepSeekService:
    provider = "deepseek"

    def __init__(self, account: DeepSeekAccount, *, model: str = "deepseek-flash", organizer: bool = False,
                 timeout_seconds: float = 180, max_output_tokens: int = 32768):
        if model != "deepseek-flash":
            raise ValueError("本次統一設定僅支援 deepseek-flash，避免模型與費用估算不一致。")
        self.account = account
        self.model = model
        self.organizer = organizer
        self.timeout_seconds = timeout_seconds
        self.max_output_tokens = max_output_tokens
        self.max_retries = 1

    def is_configured(self):
        return bool(self.account.key)

    def ensure_ready(self):
        return self.account.preflight()

    def with_timeout(self, seconds):
        return DeepSeekService(self.account, model=self.model, organizer=self.organizer,
            timeout_seconds=seconds, max_output_tokens=self.max_output_tokens)

    def with_output_limit(self, limit):
        client=DeepSeekService(self.account,model=self.model,organizer=self.organizer,
            timeout_seconds=self.timeout_seconds,max_output_tokens=limit)
        client.max_retries=self.max_retries
        return client

    def prompt_capacity(self, prompt, *, output_tokens=None):
        # UTF-8 bytes are a conservative text-token upper bound, not a billing
        # estimate. Reserve output plus message framing; never trim input here.
        output = self.max_output_tokens if output_tokens is None else output_tokens
        upper_bound = len(prompt.encode('utf-8')) + 4096 + output
        return {'fits': upper_bound <= 1_000_000, 'context_tokens': 1_000_000,
                'input_utf8_bytes': len(prompt.encode('utf-8')), 'output_reserved_tokens': output,
                'conservative_total_bound': upper_bound,
                'reference': 'https://api-docs.deepseek.com/quick_start/pricing/'}

    def _post(self, operation: str, body: dict, estimate: Decimal):
        request_id = self.account.reserve(operation, estimate)
        tool_request_path = None
        if operation.startswith('v1/tools/'):
            try:
                directory = self.account.root / 'requests'
                directory.mkdir(parents=True, exist_ok=True)
                tool_request_path = directory / (request_id + '.json')
                tool_request_path.write_text(json.dumps({'request_id':request_id, 'run_id':RUN_ID.get(),
                    'command':TASK.get()[0], 'operation':operation, 'body':body}, ensure_ascii=False, indent=2), encoding='utf-8')
            except OSError:
                self.account.finish(request_id, 'failed', {'reason':'request_log_unavailable', 'http_sent':False}, Decimal(0))
                raise DeepSeekError('無法保存搜尋請求，尚未連線或扣款。', 'request_log_unavailable') from None
        started = time.monotonic()
        attempts = 0
        prompt_chars = sum(len(str(m.get("content") or "")) for m in body.get("messages", []))
        def metrics(detail):
            return {**detail, "attempts": attempts, "retry_count": max(0, attempts - 1),
                    "elapsed_seconds": round(time.monotonic() - started, 3), "prompt_chars": prompt_chars,
                    'run_id':RUN_ID.get(), 'request_path':str(tool_request_path) if tool_request_path else None,
                    'request_body_sha256':_request_body_sha256(body)}
        try:
            for attempt in range(self.max_retries + 1):
                try:
                    attempts += 1
                    with httpx.Client(timeout=self.timeout_seconds, transport=self.account.transport) as client:
                        response = client.post("https://api.deepseek.com/" + operation, headers={"Authorization": f"Bearer {self.account.key}"}, json=body)
                    if response.status_code in (401, 402):
                        self.account.block(response.status_code)
                    if response.status_code in (429, 500, 502, 503, 504):
                        if attempt < self.max_retries:
                            time.sleep(1 + attempt)
                            continue
                        if operation.startswith("v1/tools/") and response.status_code != 429:
                            raise DeepSeekError("搜尋或正文服務暫時無法取得此來源，保留缺口後繼續分析已有資料。",
                                                "source_unavailable", response.status_code)
                        self.account.cooldown_until = time.time() + 30
                        status = "rate_limited" if response.status_code == 429 else "service_unavailable"
                        self.account.cooldown_reason = status
                        self.account.budget.incident(status, background=TASK.get()[1])
                        raise DeepSeekUnavailable(REASONS[status], status, response.status_code)
                    if response.status_code >= 400:
                        raise DeepSeekError(f"DeepSeek 請求失敗（HTTP {response.status_code}）。", "http_error", response.status_code)
                    data = response.json()
                    usage = data.get("usage") or {}
                    actual = _price(usage) if usage else (Decimal("0.00075") if operation.startswith("v1/tools/") else None)
                    if operation.endswith("search") and not data.get("results"):
                        actual = Decimal("0")
                    if operation.endswith("fetch") and not data.get("content"):
                        actual = Decimal("0")
                    self.account.finish(request_id, "success" if actual is not None else "unknown", metrics({"usage": usage, "model": data.get("model", self.model), "http_status": response.status_code}), actual)
                    self.account.budget.incident(None, background=TASK.get()[1], preflight=operation != "chat/completions")
                    return data
                except httpx.TransportError:
                    # A timed-out generation may still be billed. Do not blindly duplicate it.
                    self.account.cooldown_until = time.time() + 30
                    self.account.cooldown_reason = "transport_unknown"
                    raise DeepSeekUnavailable("DeepSeek 連線中斷或逾時；費用未知，已保留進度。", "transport_unknown") from None
        except DeepSeekError as exc:
            if exc.status == "transport_unknown":
                self.account.budget.incident(exc.status, background=TASK.get()[1])
            self.account.finish(request_id, "unknown" if exc.status == "transport_unknown" else "failed", metrics(exc.diagnostics))
            raise
        except Exception:
            self.account.finish(request_id, "unknown", metrics({"status": "response_invalid"}))
            raise DeepSeekError("DeepSeek 回應無法解析，費用未知。", "response_invalid") from None

    def _generate(self, prompt: str, json_mode: bool):
        identity = hashlib.sha256(json.dumps([prompt, json_mode, self.organizer, self.model, self.max_output_tokens, TASK.get()], ensure_ascii=False).encode()).hexdigest()
        with self.account.singleflight(identity):
            return self._generate_once(prompt, json_mode)

    def _generate_once(self, prompt: str, json_mode: bool):
        capacity = self.prompt_capacity(prompt)
        if not capacity['fits']:
            error = DeepSeekError('完整資料超過模型上下文容量；未送出付費請求，也未刪減資料。', 'prompt_too_large')
            error.diagnostics.update(capacity)
            raise error
        command, _, report_date = TASK.get()
        system = ("請以繁體中文整理事實與來源，只輸出有效 JSON，不提供買賣結論。" if self.organizer
                  else "請以繁體中文完成使用者要求的分析。區分事實、推論及情境假設，保留來源、反證、風險與資料缺口，不顯示內部推理。估值表EPS乘本益比上下限須精確驗算，折溢價另列；新官方財報若在指定日前公布，核對後優先於本地舊報表，明列期間衝突。")
        if json_mode:
            system += "只輸出有效 JSON 物件。"
        body = {"model": self.model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                "thinking": {"type": "disabled" if self.organizer else "enabled"}, "max_tokens": self.max_output_tokens}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        prompt_dir = self.account.root / "prompts"
        prompt_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = prompt_dir / (uuid4().hex + ".json")
        prompt_path.write_text(json.dumps({"command":command,"report_date":report_date,"run_id":RUN_ID.get(),"organizer":self.organizer,"body":body},ensure_ascii=False,indent=2),encoding="utf-8")
        cache_key = "model:" + hashlib.sha256(json.dumps([body, command, report_date], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        data = self.account.cached(cache_key)
        cache_hit = data is not None
        self.account.activity("model", cache_hit, {"prompt_path":str(prompt_path),"prompt_chars":len(prompt),"model":self.model,"organizer":self.organizer,"run_id":RUN_ID.get(),
                                                   "request_body_sha256":_request_body_sha256(body)})
        if data is None:
            estimate = _reserve_model_price(len(prompt) + len(system), self.max_output_tokens,
                self.timeout_seconds * (self.max_retries + 1) + 30)
            data = self._post("chat/completions", body, estimate)
        choice = (data.get("choices") or [{}])[0]
        content = str((choice.get("message") or {}).get("content") or "").strip()
        if not content or choice.get("finish_reason") in ("length", "content_filter"):
            directory=self.account.root/'incomplete_outputs'
            directory.mkdir(parents=True,exist_ok=True)
            path=directory/(uuid4().hex+'.json')
            path.write_text(json.dumps({'prompt_path':str(prompt_path),'finish_reason':choice.get('finish_reason'),
                'content':content,'usage':data.get('usage') or {}},ensure_ascii=False,indent=2),encoding='utf-8')
            error=DeepSeekError("DeepSeek 未完成完整輸出，已保留成功階段，請重試。", "incomplete_output")
            error.diagnostics.update({'finish_reason':choice.get('finish_reason'),'output_chars':len(content),
                'max_output_tokens':self.max_output_tokens,'incomplete_output_path':str(path)})
            self.account.activity('generation_incomplete',False,error.diagnostics)
            raise error
        if json_mode:
            try:
                if not isinstance(json.loads(content), dict):
                    raise ValueError()
            except (ValueError, TypeError) as exc:
                directory=self.account.root/'incomplete_outputs'
                directory.mkdir(parents=True,exist_ok=True)
                path=directory/(uuid4().hex+'.json')
                path.write_text(json.dumps({'prompt_path':str(prompt_path),'finish_reason':choice.get('finish_reason'),
                    'content':content,'usage':data.get('usage') or {},'validation_error':str(exc)},ensure_ascii=False,indent=2),encoding='utf-8')
                error=DeepSeekError("DeepSeek 結構化輸出格式不完整，原始回應已保留供診斷。", "invalid_json")
                error.diagnostics.update({'incomplete_output_path':str(path),'output_chars':len(content)})
                self.account.activity('generation_invalid_json',False,error.diagnostics)
                raise error from None
        if not cache_hit:
            self.account.cache(cache_key, data, 86400)
        return DeepSeekResult(content, data, {"provider": "deepseek", "model": data.get("model") or self.model, "actual_model": data.get("model") or self.model,
                "requested_model": self.model, "usage": data.get("usage") or {}, "cache_hit": cache_hit,
                "finish_reason": choice.get("finish_reason"), "cost_estimate_usd": "0" if cache_hit else (str(_price(data["usage"])) if data.get("usage") else None)})

    def generate_report(self, prompt: str, **kwargs):
        return self._generate(prompt, False)

    def generate_json(self, prompt: str):
        try:
            return self._generate(prompt, True)
        except DeepSeekError as exc:
            if (exc.status != 'incomplete_output' or exc.diagnostics.get('finish_reason') != 'length'
                    or self.max_output_tokens >= 65536):
                raise
            limit=min(65536,self.max_output_tokens*2)
            result=self.with_output_limit(limit)._generate(prompt, True)
            result.diagnostics.update({'output_limit_retry':True,'previous_max_output_tokens':self.max_output_tokens,
                                       'retry_max_output_tokens':limit})
            return result


class DisabledProvider:
    """Compatibility handle for historical report readers; never contacts a provider."""
    model = "disabled"
    fallback_models = ()

    def is_configured(self):
        return False

    def __getattr__(self, name):
        if name in {"generate_report", "generate_json", "discover", "search", "fetch"}:
            def disabled(*args, **kwargs):
                raise DeepSeekUnavailable("此模型或搜尋服務已停用，請使用 DeepSeek。", "provider_disabled")
            return disabled
        raise AttributeError(name)


def account_options(config, root):
    from .ai_budget_service import shared_budget_root
    root = Path(root).resolve()
    production_budget = shared_budget_root(root)
    common = production_budget.parent.parent
    test_root = os.environ.get('DEEPSEEK_BUDGET_TEST_ROOT')
    profile = os.environ.get('STOCK_AI_BOT_WORKDATA_PROFILE', 'prod')
    development = DEVELOPMENT_ACCOUNTING.get() or bool(test_root) or (root / '.git').is_file() or profile != 'prod'
    if test_root:
        budget_root = Path(test_root).resolve()
        protected = (production_budget, common / '.cache' / 'deepseek')
        if any(budget_root == p or budget_root in p.parents or p in budget_root.parents for p in protected):
            raise ValueError('開發測試帳本不可使用或包覆正式帳本路徑。')
    else:
        budget_root = common / '.workdata' / 'ai_development' / 'budget' if development else production_budget
    return dict(background_reserve_usd=config.deepseek_background_reserve_usd,
        usd_cny_budget_rate=config.deepseek_usd_cny_budget_rate,
        budget_root=budget_root,
        usage_root=budget_root / 'account' if development else root / '.cache' / 'deepseek',
        development=development,
        monthly_limit_usd=config.deepseek_monthly_limit_usd,
        background_monthly_usd=config.deepseek_background_monthly_usd,
        manual_monthly_usd=config.deepseek_manual_monthly_usd,
        manual_allowances=getattr(config, 'deepseek_manual_allowances_usd', {}),
        monthly_reserve_usd=config.deepseek_monthly_reserve_usd,
        job_limit_usd=config.deepseek_job_limit_usd)


def configured_client(*, organizer=False):
    from .config import ROOT_DIR, load_research_config
    config = load_research_config()
    if getattr(config, 'openai_primary_enabled', False):
        from .ai_provider_service import create_ai_client
        return create_ai_client(config, ROOT_DIR, organizer=organizer)
    account = DeepSeekAccount.shared(config.deepseek_api_key, ROOT_DIR / ".cache" / "deepseek", **account_options(config, ROOT_DIR))
    return DeepSeekService(account, model=config.deepseek_model, organizer=organizer,
        max_output_tokens=8192 if organizer else 32768)
