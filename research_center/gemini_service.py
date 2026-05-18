from __future__ import annotations

from dataclasses import dataclass
import time
from datetime import date, datetime
from typing import Any

import httpx

from .models import CommandRequest, SourceItem
from .prompt_registry import build_prompt_from_request
from .source_rank import make_source_items

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


@dataclass(frozen=True)
class GeminiResult:
    markdown: str
    sources: list[SourceItem]
    raw: dict[str, Any]
    diagnostics: dict[str, Any]


class GeminiService:
    def __init__(
        self,
        api_key: str | None,
        model: str,
        enable_grounding: bool = True,
        timeout_seconds: float = 90.0,
        max_retries: int = 1,
        fallback_models: tuple[str, ...] = (),
    ):
        self.api_key = api_key
        self.model = model
        self.enable_grounding = enable_grounding
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.fallback_models = tuple(item for item in fallback_models if item and item != model)

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def generate_report(self, prompt: str, enable_grounding: bool | None = None) -> GeminiResult:
        if not self.api_key:
            raise RuntimeError("Gemini API Key 尚未設定。")

        payload: dict[str, Any] = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.25},
        }
        use_grounding = self.enable_grounding if enable_grounding is None else bool(enable_grounding)
        if use_grounding:
            payload["tools"] = [{"google_search": {}}]

        attempts: list[dict[str, Any]] = []
        last_error: Exception | None = None
        model_chain = (self.model, *self.fallback_models)
        for model_index, model_name in enumerate(model_chain):
            url = GEMINI_BASE_URL.format(model=model_name)
            data: dict[str, Any] | None = None
            for attempt in range(self.max_retries + 1):
                try:
                    with httpx.Client(timeout=self.timeout_seconds, follow_redirects=True) as client:
                        response = client.post(url, params={"key": self.api_key}, json=payload)
                        response.raise_for_status()
                        data = response.json()
                    break
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    last_error = exc
                    attempts.append({"model": model_name, "attempt": attempt + 1, "error": str(exc), "fallback_eligible": True})
                    if attempt < self.max_retries:
                        time.sleep(1.5 * (attempt + 1))
                        continue
                    break
                except httpx.HTTPStatusError as exc:
                    last_error = exc
                    status = exc.response.status_code
                    fallback_eligible = _is_fallback_eligible_http_error(exc)
                    attempts.append({"model": model_name, "attempt": attempt + 1, "status_code": status, "error": _http_error_message(exc), "fallback_eligible": fallback_eligible})
                    if attempt < self.max_retries and status in {429, 500, 502, 503, 504}:
                        time.sleep(2.0 * (attempt + 1))
                        continue
                    if not fallback_eligible or model_index >= len(model_chain) - 1:
                        raise
                    break
            if data is None:
                if model_index >= len(model_chain) - 1:
                    if last_error:
                        raise last_error
                    raise RuntimeError("Gemini request failed without response.")
                continue

            text = _extract_text(data)
            sources = _extract_sources(data)
            diagnostics = _grounding_diagnostics(data, sources)
            diagnostics["requested_model"] = self.model
            diagnostics["actual_model"] = model_name
            diagnostics["fallback_models"] = list(self.fallback_models)
            diagnostics["fallback_used"] = model_name != self.model
            diagnostics["fallback_attempts"] = attempts
            diagnostics["model"] = model_name
            return GeminiResult(markdown=text, sources=sources, raw=data, diagnostics=diagnostics)

        raise RuntimeError(f"Gemini request failed without response: {last_error}")


def _is_fallback_eligible_http_error(exc: httpx.HTTPStatusError) -> bool:
    status = exc.response.status_code
    message = _http_error_message(exc).lower()
    if status in {429, 500, 502, 503, 504}:
        return True
    if status in {400, 403, 404} and any(token in message for token in ("quota", "resource_exhausted", "rate", "not found", "not supported", "permission", "model")):
        return True
    return False


def _http_error_message(exc: httpx.HTTPStatusError) -> str:
    try:
        payload = exc.response.json()
        return str(payload.get("error") or payload)
    except Exception:
        return exc.response.text[:500]


def build_prompt(
    request_or_kind: CommandRequest | str,
    request_payload: dict[str, Any] | None = None,
    structured_data: dict[str, Any] | None = None,
    source_list: list[SourceItem] | None = None,
) -> str:
    if isinstance(request_or_kind, CommandRequest):
        return build_prompt_from_request(request_or_kind, structured_data or {}, source_list or [])

    # Backward compatibility for older tests/callers. New code should pass CommandRequest.
    from .models import CommandRequest as RequestModel

    payload = request_payload or {}
    request = RequestModel(
        command=str(request_or_kind),
        raw_text=str(payload.get("raw_text") or request_or_kind),
        target=payload.get("target"),
        target_type=payload.get("target_type"),
        market_scope=payload.get("market_scope"),
        theme_scope=payload.get("theme_scope"),
        region_scope=payload.get("region_scope"),
        candidate_pool=payload.get("candidate_pool"),
        mode=str(payload.get("mode") or "normal"),
        source_only=bool(payload.get("source_only")),
        score=bool(payload.get("score")),
        brief=bool(payload.get("brief")),
        top=payload.get("top"),
        report_date=_coerce_report_date(payload.get("report_date")),
    )
    return build_prompt_from_request(request, structured_data or {}, source_list or [])


def _coerce_report_date(value: Any) -> date | None:
    if value in (None, "", False):
        return None
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except ValueError:
        return None


def _extract_text(data: dict[str, Any]) -> str:
    parts = (((data.get("candidates") or [{}])[0].get("content") or {}).get("parts") or [])
    text = "\n".join(str(part.get("text") or "") for part in parts if part.get("text"))
    if not text.strip():
        raise RuntimeError("Gemini 沒有回傳可用文字內容。")
    return text.strip()


def _extract_sources(data: dict[str, Any]) -> list[SourceItem]:
    candidate = (data.get("candidates") or [{}])[0]
    metadata = candidate.get("groundingMetadata") or candidate.get("grounding_metadata") or {}
    chunks = metadata.get("groundingChunks") or metadata.get("grounding_chunks") or []
    raw_sources: list[dict[str, str]] = []
    for chunk in chunks:
        web = chunk.get("web") or {}
        uri = web.get("uri") or web.get("url")
        title = web.get("title") or uri
        if uri:
            raw_sources.append({"url": str(uri), "title": str(title or uri), "snippet": "Gemini grounding source", "provider": "gemini_grounding", "provider_detail": "google_search_grounding"})
    return make_source_items(raw_sources)



def _grounding_diagnostics(data: dict[str, Any], sources: list[SourceItem]) -> dict[str, Any]:
    candidate = (data.get("candidates") or [{}])[0]
    metadata = candidate.get("groundingMetadata") or candidate.get("grounding_metadata") or {}
    return {
        "grounding_metadata_present": bool(metadata),
        "grounding_chunk_count": len(metadata.get("groundingChunks") or metadata.get("grounding_chunks") or []),
        "grounding_support_count": len(metadata.get("groundingSupports") or metadata.get("grounding_supports") or []),
        "web_search_query_count": len(metadata.get("webSearchQueries") or metadata.get("web_search_queries") or []),
        "search_entry_point_present": bool(metadata.get("searchEntryPoint") or metadata.get("search_entry_point")),
        "extracted_source_count": len(sources),
        "finish_reason": candidate.get("finishReason") or candidate.get("finish_reason"),
    }

