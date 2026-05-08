from __future__ import annotations

from dataclasses import dataclass
import re
import time
from typing import Any

import httpx


DEFAULT_MINIMAX_BASE_URL = "https://api.minimax.io/v1"
DEFAULT_MINIMAX_MODEL = "MiniMax-M2.7"


@dataclass(frozen=True)
class MiniMaxResult:
    markdown: str
    raw: dict[str, Any]
    diagnostics: dict[str, Any]


class MiniMaxService:
    def __init__(
        self,
        api_key: str | None,
        model: str = DEFAULT_MINIMAX_MODEL,
        base_url: str = DEFAULT_MINIMAX_BASE_URL,
        timeout_seconds: float = 1200.0,
        max_retries: int = 1,
    ):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def generate_report(self, prompt: str) -> MiniMaxResult:
        if not self.api_key:
            raise RuntimeError("MiniMax API Key 尚未設定。")

        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": "You are a cautious Taiwan stock investment research analyst. Return Markdown only. Do not expose hidden reasoning.",
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.25,
        }
        data = self._post_json(f"{self.base_url}/chat/completions", payload)
        text = _extract_minimax_text(data)
        return MiniMaxResult(
            markdown=text,
            raw=data,
            diagnostics={
                "model": data.get("model") or self.model,
                "finish_reason": (((data.get("choices") or [{}])[0]).get("finish_reason")),
                "usage": data.get("usage") or {},
            },
        )

    def summarize_search_content(self, query: str, content_blocks: list[dict[str, str]], max_chars: int = 12000) -> MiniMaxResult:
        joined_blocks = []
        remaining = max_chars
        for index, block in enumerate(content_blocks, 1):
            title = block.get("title") or block.get("url") or f"source_{index}"
            url = block.get("url") or ""
            text = block.get("content") or block.get("snippet") or ""
            piece = f"[{index}] {title}\nURL: {url}\n{text}\n"
            if len(piece) > remaining:
                piece = piece[:remaining]
            if piece.strip():
                joined_blocks.append(piece)
                remaining -= len(piece)
            if remaining <= 0:
                break
        prompt = (
            "請根據下列 Google/Serper 搜尋結果與 Jina 讀取內容，整理可用於台股投研報告的來源摘要。\n"
            "要求：只引用輸入內容，不得補腦；每點附 URL；區分已證實、推論、資料不足；使用繁體中文。\n\n"
            f"搜尋任務：{query}\n\n"
            + "\n---\n".join(joined_blocks)
        )
        return self.generate_report(prompt)

    def _post_json(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                with httpx.Client(timeout=self.timeout_seconds, follow_redirects=True) as client:
                    response = client.post(url, headers=headers, json=payload)
                    response.raise_for_status()
                    return response.json()
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    raise
                time.sleep(1.5 * (attempt + 1))
            except httpx.HTTPStatusError as exc:
                last_error = exc
                status = exc.response.status_code
                if attempt >= self.max_retries or status not in {429, 500, 502, 503, 504}:
                    raise
                time.sleep(2.0 * (attempt + 1))
        raise RuntimeError(f"MiniMax request failed without response: {last_error}")


def _extract_minimax_text(data: dict[str, Any]) -> str:
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    text = str(message.get("content") or "").strip()
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE).strip()
    if not text:
        raise RuntimeError("MiniMax 沒有回傳可用文字內容。")
    return text


