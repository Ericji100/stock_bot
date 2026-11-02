"""Official DeepSeek transport, shared balance guard, cache and usage ledger."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
import hashlib
import inspect
from functools import wraps
import json
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx

TASK = ContextVar("deepseek_task", default=("interactive", False, ""))


@contextmanager
def ai_task(command: str, background: bool = False, report_date: str = ""):
    token = TASK.set((command, background, report_date))
    try:
        yield
    finally:
        TASK.reset(token)


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
        identity = (str(root.resolve()), hashlib.sha256((key or "").encode()).hexdigest())
        with cls._instances_lock:
            if identity not in cls._instances:
                cls._instances[identity] = cls(key, root, **kwargs)
            return cls._instances[identity]

    def __init__(self, key: str | None, root: Path, *, balance_ttl: float = 60,
                 background_reserve_usd: float = 0.20, daily_limit_usd: float = 5.0,
                 usd_cny_budget_rate: float = 7.5,
                 transport=None):
        self.key = key
        self.root = Path(root)
        self.balance_ttl = balance_ttl
        self.background_reserve = Decimal(str(background_reserve_usd))
        self.daily_limit = Decimal(str(daily_limit_usd))
        self.usd_cny_budget_rate = Decimal(str(usd_cny_budget_rate))
        self.transport = transport
        self.lock = threading.RLock()
        self.balance_checked_at = 0.0
        self.available: bool | None = None
        self.balances: list[dict[str, str]] = []
        self.block_reason = ""
        self.cooldown_until = 0.0
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

    def refresh_balance(self, *, force: bool = False):
        with self.lock:
            if not self.key:
                raise DeepSeekUnavailable("尚未設定 DeepSeek 官方 API Key。", "not_configured")
            if not force and time.time() < self.cooldown_until:
                raise DeepSeekUnavailable("DeepSeek 暫時限流，稍後再試。", "rate_limited", 429)
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
        raise DeepSeekUnavailable(self.block_reason, "insufficient_balance" if status == 402 else "authentication_failed", status)

    def reserve(self, operation: str, estimate: Decimal) -> str:
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
                conn.execute("INSERT INTO usage VALUES (?,?,?,?,?,?,?,?,?)", (request_id, day, command, int(background), operation, "pending", str(estimate), None, "{}"))
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


def _price(usage: dict) -> Decimal:
    now = datetime.now(ZoneInfo("Asia/Taipei"))
    # Conservative peak pricing; ledger stores an estimate, not a provider invoice.
    factor = Decimal("2") if now.weekday() < 5 and (9 <= now.hour < 12 or 14 <= now.hour < 18) else Decimal("1")
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
        return self.account.refresh_balance()

    def with_timeout(self, seconds):
        return DeepSeekService(self.account, model=self.model, organizer=self.organizer,
            timeout_seconds=seconds, max_output_tokens=self.max_output_tokens)

    def _post(self, operation: str, body: dict, estimate: Decimal):
        request_id = self.account.reserve(operation, estimate)
        started = time.monotonic()
        attempts = 0
        prompt_chars = sum(len(str(m.get("content") or "")) for m in body.get("messages", []))
        def metrics(detail):
            return {**detail, "attempts": attempts, "retry_count": max(0, attempts - 1),
                    "elapsed_seconds": round(time.monotonic() - started, 3), "prompt_chars": prompt_chars}
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
                        self.account.cooldown_until = time.time() + 30
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
                    return data
                except httpx.TransportError:
                    # A timed-out generation may still be billed. Do not blindly duplicate it.
                    raise DeepSeekError("DeepSeek 連線中斷或逾時；費用未知，已保留進度。", "transport_unknown") from None
        except DeepSeekError as exc:
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
        command, _, report_date = TASK.get()
        system = ("請以繁體中文整理事實與來源，只輸出有效 JSON，不提供買賣結論。" if self.organizer
                  else "請以繁體中文完成使用者要求的分析。區分事實、推論及情境假設，保留來源、反證、風險與資料缺口，不顯示內部推理。")
        if json_mode:
            system += "只輸出有效 JSON 物件。"
        body = {"model": self.model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                "thinking": {"type": "disabled" if self.organizer else "enabled"}, "max_tokens": self.max_output_tokens}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        cache_key = "model:" + hashlib.sha256(json.dumps([body, command, report_date], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        data = self.account.cached(cache_key)
        cache_hit = data is not None
        if data is None:
            estimate = (Decimal(len(prompt) + len(system)) * Decimal("0.30") + Decimal(self.max_output_tokens) * Decimal("1.20")) / 1000000
            data = self._post("chat/completions", body, estimate)
        choice = (data.get("choices") or [{}])[0]
        content = str((choice.get("message") or {}).get("content") or "").strip()
        if not content or choice.get("finish_reason") in ("length", "content_filter"):
            raise DeepSeekError("DeepSeek 未完成完整輸出，已保留成功階段，請重試。", "incomplete_output")
        if json_mode:
            try:
                if not isinstance(json.loads(content), dict):
                    raise ValueError()
            except (ValueError, TypeError):
                raise DeepSeekError("DeepSeek 結構化輸出格式不完整。", "invalid_json") from None
        if not cache_hit:
            self.account.cache(cache_key, data, 86400)
        return DeepSeekResult(content, data, {"provider": "deepseek", "model": data.get("model") or self.model, "actual_model": data.get("model") or self.model,
                "requested_model": self.model, "usage": data.get("usage") or {}, "cache_hit": cache_hit,
                "finish_reason": choice.get("finish_reason"), "cost_estimate_usd": "0" if cache_hit else (str(_price(data["usage"])) if data.get("usage") else None)})

    def generate_report(self, prompt: str, **kwargs):
        return self._generate(prompt, False)

    def generate_json(self, prompt: str):
        return self._generate(prompt, True)


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


def configured_client(*, organizer=False):
    from .config import ROOT_DIR, load_research_config
    config = load_research_config()
    account = DeepSeekAccount.shared(config.deepseek_api_key, ROOT_DIR / ".cache" / "deepseek",
        background_reserve_usd=config.deepseek_background_reserve_usd, daily_limit_usd=config.deepseek_daily_limit_usd,
        usd_cny_budget_rate=config.deepseek_usd_cny_budget_rate)
    return DeepSeekService(account, model=config.deepseek_model, organizer=organizer,
        max_output_tokens=8192 if organizer else 32768)
