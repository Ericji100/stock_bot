from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(frozen=True)
class OpenCodeResult:
    markdown: str
    raw: dict[str, Any]
    diagnostics: dict[str, Any]


class OpenCodeService:
    def __init__(
        self,
        api_key: str | None,
        model: str = "deepseek-v4-pro",
        base_url: str = "https://opencode.ai/zen/go/v1",
        reasoning_effort: str = "medium",
        timeout_seconds: float = 1200.0,
    ):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.reasoning_effort = reasoning_effort
        self.timeout_seconds = timeout_seconds

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def generate_report(self, prompt: str) -> OpenCodeResult:
        if not self.api_key:
            raise RuntimeError("OpenCode Go API Key is not configured.")

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": "你是台股 AI 投研資料中心，請使用繁體中文輸出完整 Markdown 報告。"},
                {"role": "user", "content": prompt},
            ],
            "reasoning_effort": self.reasoning_effort,
            "temperature": 0.25,
        }
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        with httpx.Client(timeout=self.timeout_seconds, follow_redirects=True) as client:
            response = client.post(f"{self.base_url}/chat/completions", headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()

        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        text = str(message.get("content") or "").strip()
        if not text:
            reasoning = str(message.get("reasoning_content") or "").strip()
            raise RuntimeError(
                "OpenCode Go returned empty content"
                + (f" after reasoning output ({len(reasoning)} chars)." if reasoning else ".")
            )

        diagnostics = {
            "provider": "opencode_go",
            "model": data.get("model") or self.model,
            "requested_model": self.model,
            "actual_model": data.get("model") or self.model,
            "reasoning_effort": self.reasoning_effort,
            "finish_reason": choice.get("finish_reason"),
            "usage": data.get("usage"),
            "reasoning_content_present": bool(message.get("reasoning_content")),
            "reasoning_content_length": len(str(message.get("reasoning_content") or "")),
        }
        return OpenCodeResult(markdown=text, raw=data, diagnostics=diagnostics)
