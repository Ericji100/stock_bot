"""Observe Telegram transport without retrying business work or changing PTB retries."""
from __future__ import annotations

from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import asyncio
import json
import logging
from pathlib import Path
import traceback
from typing import Callable
from uuid import uuid4

from telegram.error import NetworkError
from telegram.request import HTTPXRequest

from stock_ai_bot.telegram.telegram_push_service import sanitize_telegram_error

HEALTH_SERVICE_KEY = "telegram_transport_health"
_NOTICE_REQUEST = ContextVar("telegram_health_notice_request", default=False)
_TAIPEI = timezone(timedelta(hours=8))
_CHANNEL_LABELS = {"polling": "接收指令連線", "api": "訊息及 API 連線"}


class TelegramHealthService:
    def __init__(self, root: Path, *, sensitive_values=(),
                 clock: Callable[[], datetime] | None = None, emit: Callable[[str], None] = print,
                 cooldown_seconds: float = 300, outage_seconds: float = 60,
                 instability_window_seconds: float = 600, instability_count: int = 3):
        self.root = Path(root)
        self.state_path = self.root / "state.json"
        self.sensitive_values = tuple(sensitive_values)
        self.clock = clock or (lambda: datetime.now(_TAIPEI))
        self.emit = emit
        self.cooldown_seconds = cooldown_seconds
        self.outage_seconds = outage_seconds
        self.instability_window_seconds = instability_window_seconds
        self.instability_count = instability_count
        self._flushing = False
        self._storage_warned = False
        self._state_writable = True
        self._handler_notices: dict[str, datetime] = {}
        self.state = {"schema_version": 1, "channels": {
            name: {"status": "unverified", "last_success_at": None} for name in _CHANNEL_LABELS
        }, "notices": []}
        if self.state_path.exists():
            try:
                saved = json.loads(self.state_path.read_text(encoding="utf-8"))
                if (saved.get("schema_version") != 1 or not isinstance(saved.get("notices"), list)
                        or any(not isinstance(saved.get("channels", {}).get(name), dict) for name in _CHANNEL_LABELS)
                        or any(not isinstance(item, dict)
                               or item.get("channel") not in _CHANNEL_LABELS
                               or item.get("kind") not in {"outage", "recovered"}
                               or item.get("delivery") not in {"pending", "sending", "sent", "unknown", "merged_into_recovery", "superseded", "suppressed_threshold"}
                               or not isinstance(item.get("incident"), dict)
                               or not item["incident"].get("first_error_at")
                               for item in saved["notices"])):
                    raise ValueError("Unsupported Telegram health state")
                self.state = saved
                for notice in self.state["notices"]:
                    if notice.get("delivery") == "sending":
                        notice["delivery"] = "unknown"
                for channel in self.state["channels"].values():
                    if channel.get("status") == "healthy":
                        channel["status"] = "unverified"
            except (OSError, ValueError, AttributeError, TypeError) as exc:
                self._state_writable = False
                self._diagnostic("state_load_failed", "state", exc)
                self._say("健康狀態檔無法讀取，保留原檔；本次只用記憶體追蹤及診斷紀錄。")

    def _timestamp(self) -> str:
        return self.clock().astimezone(_TAIPEI).isoformat(timespec="seconds")

    def _clean(self, value: object) -> str:
        return sanitize_telegram_error(value, *self.sensitive_values, max_length=None)

    def _say(self, text: str) -> None:
        self.emit(f"[{self._timestamp()}] [Telegram] {self._clean(text)}")

    def _storage_failure(self, exc: Exception, fallback: str = "") -> None:
        if not self._storage_warned:
            self._storage_warned = True
            self._say(f"診斷檔案寫入失敗：{type(exc).__name__}；不影響原請求及排程。")
        if fallback:
            self.emit(self._clean(fallback))

    def _diagnostic(self, event: str, operation: str, error: BaseException | None = None, **details) -> str:
        identifier = uuid4().hex
        record = {"id": identifier, "at": self._timestamp(), "event": event,
                  "operation": operation, **details}
        if error is not None:
            record.update(error_type=type(error).__name__, error=self._clean(error),
                          traceback=self._clean("".join(traceback.format_exception(error))))
        text = json.dumps(record, ensure_ascii=False)
        try:
            folder = self.root / "diagnostics"
            folder.mkdir(parents=True, exist_ok=True)
            with (folder / f"{record['at'][:10]}.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(text + "\n")
        except OSError as exc:
            self._storage_failure(exc, text)
        return identifier

    def persist(self) -> bool:
        if not self._state_writable:
            return False
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            temporary = self.state_path.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self.state_path)
            return True
        except OSError as exc:
            self._storage_failure(exc)
            return False

    def _notice(self, kind: str, channel: str, data: dict) -> None:
        for previous in self.state["notices"]:
            if (previous["channel"] == channel and previous["delivery"] == "pending"
                    and previous["incident"].get("incident_id") != data["incident_id"]):
                previous["delivery"] = "superseded"
        self.state["notices"].append({"id": uuid4().hex, "kind": kind, "channel": channel,
                                      "incident": deepcopy(data), "delivery": "pending"})

    def failed(self, operation: str, error: BaseException) -> None:
        name = "polling" if operation.lower() == "getupdates" else "api"
        now = self._timestamp()
        channel = self.state["channels"][name]
        fresh_incident = channel.get("status") != "degraded"
        if fresh_incident:
            channel.update(status="degraded", incident_id=uuid4().hex, first_error_at=now,
                           failures=0, last_notice_at=None)
            channel.pop('recovered_at', None)
            recent = channel.get('recent_incident_starts') or []
            cutoff = self.clock() - timedelta(seconds=self.instability_window_seconds)
            channel['recent_incident_starts'] = [at for at in recent if datetime.fromisoformat(at) >= cutoff] + [now]
        channel.update(last_error_at=now, last_error_type=type(error).__name__,
                       failures=channel["failures"] + 1)
        channel["diagnostic_id"] = self._diagnostic("transport_error", operation, error,
                                                    channel=name, incident_id=channel["incident_id"])
        if fresh_incident:
            self._notice("outage", name, channel)
        last = channel.get("last_notice_at")
        if last is None or (self.clock() - datetime.fromisoformat(last)).total_seconds() >= self.cooldown_seconds:
            self._say(f"{_CHANNEL_LABELS[name]}異常：{type(error).__name__}，本次累計{channel['failures']}次。"
                      f"完整診斷：{channel['diagnostic_id']}。不重跑 AI 或交易工作。")
            channel["last_notice_at"] = now
        # The error object reaches Application.process_error unchanged.
        setattr(error, "_telegram_health_recorded", True)
        self.persist()

    def succeeded(self, operation: str) -> None:
        name = "polling" if operation.lower() == "getupdates" else "api"
        channel = self.state["channels"][name]
        now = self._timestamp()
        was_degraded = channel.get("status") == "degraded"
        channel.update(status="healthy", last_success_at=now)
        if not was_degraded:
            return
        channel["recovered_at"] = now
        outage_sent = False
        for notice in self.state["notices"]:
            same = notice["incident"].get("incident_id") == channel["incident_id"]
            if same and notice["kind"] == "outage" and notice["delivery"] == "sent":
                outage_sent = True
            if (notice["incident"].get("incident_id") == channel["incident_id"]
                    and notice["kind"] == "outage" and notice["delivery"] == "pending"):
                notice["delivery"] = "suppressed_threshold"
        if outage_sent:
            self._notice("recovered", name, channel)
        else:
            self._queue_instability(name)
        self._diagnostic("transport_recovered", operation, channel=name,
                         incident_id=channel["incident_id"], failures=channel["failures"])
        self._say(f"{_CHANNEL_LABELS[name]}已收到成功回應；先前異常{channel['failures']}次。"
                  "不代表先前訊息已送達或工作已補跑。")
        self.persist()

    def application_error(self, error: BaseException, *, update_id=None) -> None:
        if getattr(error, "_telegram_health_recorded", False):
            return
        identifier = self._diagnostic("application_error", "handler", error, update_id=update_id)
        fingerprint = type(error).__name__ + ":" + self._clean(error)
        now = self.clock()
        last = self._handler_notices.get(fingerprint)
        if last is None or (now - last).total_seconds() >= self.cooldown_seconds:
            self._handler_notices[fingerprint] = now
            self._say(f"未處理錯誤：{type(error).__name__}。完整診斷：{identifier}；沒有自動重跑工作。")

    def snapshot(self) -> dict:
        return {"channels": deepcopy(self.state["channels"]),
                "notice_counts": {name: sum(n["delivery"] == name for n in self.state["notices"])
                                  for name in ("pending", "sent", "unknown")},
                "diagnostic_storage_warning": self._storage_warned or not self._state_writable}

    def _queue_instability(self, name):
        channel = self.state['channels'][name]
        cutoff = self.clock() - timedelta(seconds=self.instability_window_seconds)
        starts = [at for at in channel.get('recent_incident_starts', [])
                  if datetime.fromisoformat(at) >= cutoff]
        last = channel.get('last_instability_notice_at')
        if len(starts) < self.instability_count or last and datetime.fromisoformat(last) >= cutoff:
            return
        # A sustained incident already has an outage alert; annotate it instead of queueing two.
        if channel.get('status') == 'degraded':
            outage = next((n for n in self.state['notices'] if n['channel'] == name
                and n['kind'] == 'outage' and n['incident'].get('incident_id') == channel['incident_id']
                and n['delivery'] in {'pending', 'sending', 'sent', 'unknown'}), None)
            if outage and (self.clock() - datetime.fromisoformat(channel['first_error_at'])).total_seconds() >= self.outage_seconds:
                if outage['delivery'] == 'pending':
                    outage['incident'].update(instability_summary=True, incident_count=len(starts))
                else:
                    channel['last_instability_notice_at'] = self._timestamp()
                return
        if any(n['channel'] == name and n['delivery'] in {'pending','sending'}
               and n['incident'].get('instability_summary') for n in self.state['notices']):
            return
        data = {**channel, 'instability_summary': True, 'incident_count': len(starts)}
        self._notice('outage', name, data)

    async def flush_notices(self, bot, chat_id) -> None:
        if self._flushing:
            return
        self._flushing = True
        token = _NOTICE_REQUEST.set(True)
        try:
            for name in _CHANNEL_LABELS:
                self._queue_instability(name)
            for notice in [n for n in self.state["notices"] if n["delivery"] == "pending"][:2]:
                channel = notice["channel"]
                data = self.state["channels"][channel]
                if notice["kind"] == "recovered" and data.get("status") != "healthy":
                    continue
                if notice['kind'] == 'recovered' and not any(
                        prior['channel'] == channel and prior['kind'] == 'outage'
                        and prior['delivery'] == 'sent'
                        and prior['incident'].get('incident_id') == notice['incident'].get('incident_id')
                        for prior in self.state['notices']):
                    notice['delivery'] = 'suppressed_threshold'
                    self.persist()
                    continue
                if data.get("incident_id") != notice["incident"].get("incident_id"):
                    data = notice["incident"]
                summary = notice['incident'].get('instability_summary')
                if summary:
                    data = notice['incident']
                if notice['kind'] == 'outage' and not summary:
                    if data.get('status') != 'degraded':
                        notice['delivery'] = 'suppressed_threshold'
                        self.persist()
                        continue
                    elapsed = (self.clock() - datetime.fromisoformat(data['first_error_at'])).total_seconds()
                    if elapsed < self.outage_seconds:
                        continue
                recovered = notice["kind"] == "recovered"
                text = (f"Telegram {_CHANNEL_LABELS[channel]}{'已恢復' if recovered else '異常'}\n"
                        f"開始：{data['first_error_at']}\n"
                        f"錯誤類型：{data.get('last_error_type', '連線讀取失敗')}；異常{data.get('failures', 0)}次。\n")
                if recovered:
                    text += f"恢復：{data['recovered_at']}；已有實際請求成功。\n"
                if summary:
                    text = (f"Telegram {_CHANNEL_LABELS[channel]}短時間連線不穩\n"
                            f"近10分鐘發生{data['incident_count']}次異常；完整診斷已保留。\n")
                text += "異常期間指令或通知可能延遲；不代表先前訊息已送達，不會因此重跑 AI、交易或補發業務訊息。"
                notice["delivery"] = "sending"
                if summary:
                    self.state['channels'][channel]['last_instability_notice_at'] = self._timestamp()
                if not self.persist():
                    notice["delivery"] = "pending"
                    break
                try:
                    response = await bot.send_message(chat_id=chat_id, text=text)
                except asyncio.CancelledError as exc:
                    notice["delivery"] = "unknown"
                    self._diagnostic("notice_delivery_unknown", "sendHealthNotice", exc, notice_id=notice["id"])
                    raise
                except Exception as exc:
                    notice["delivery"] = "unknown"
                    self._diagnostic("notice_delivery_unknown", "sendHealthNotice", exc, notice_id=notice["id"])
                    self._say("狀態提示送出結果不明，保留紀錄、不自動重送。")
                    break
                else:
                    notice["delivery"] = "sent"
                    notice["message_id"] = getattr(response, "message_id", None)
                    self._diagnostic("notice_sent", "sendHealthNotice", notice_id=notice["id"],
                                     message_id=notice["message_id"])
                finally:
                    self.persist()
        finally:
            _NOTICE_REQUEST.reset(token)
            self._flushing = False


class MonitoredHTTPXRequest(HTTPXRequest):
    def __init__(self, *, health: TelegramHealthService, **kwargs):
        self.health = health
        super().__init__(**kwargs)

    async def do_request(self, url, method, *args, **kwargs):
        operation = url.rsplit("/", 1)[-1].split("?", 1)[0]
        try:
            result = await super().do_request(url, method, *args, **kwargs)
        except Exception as exc:
            if not _NOTICE_REQUEST.get():
                self._observe(lambda: self.health.failed(operation, exc) if isinstance(exc, NetworkError)
                              else self.health.application_error(exc))
            raise
        if not _NOTICE_REQUEST.get():
            status, body = result
            if status >= 500:
                self._observe(lambda: self.health.failed(operation, NetworkError(f"Telegram HTTP {status}")))
            elif status == 200:
                try:
                    payload = json.loads(body)
                except (ValueError, UnicodeError):
                    payload = None
                if isinstance(payload, dict) and payload.get("ok") is True and "result" in payload:
                    self._observe(lambda: self.health.succeeded(operation))
        return result

    def _observe(self, action) -> None:
        try:
            action()
        except Exception as exc:
            # Diagnostic failures must not replace the original request result.
            try:
                self.health._storage_failure(exc)
            except Exception:
                pass


async def handle_telegram_error(update, context) -> None:
    error = context.error
    if not isinstance(error, BaseException):
        return
    health = context.application.bot_data.get(HEALTH_SERVICE_KEY)
    try:
        if health is not None:
            health.application_error(error, update_id=getattr(update, "update_id", None))
            return
    except Exception:
        pass
    full_trace = "".join(traceback.format_exception(error))
    sensitive = getattr(health, "sensitive_values", ())
    logging.getLogger(__name__).error(
        "[%s] Telegram 錯誤處理器無法存檔，原始完整診斷：\n%s",
        datetime.now(_TAIPEI).isoformat(timespec="seconds"),
        sanitize_telegram_error(full_trace, *sensitive, max_length=None),
    )
