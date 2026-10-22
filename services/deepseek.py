"""DeepSeek 流式调用封装。

把 HTTP 细节放在这里，主应用只负责鉴权、校验和组装 SSE 事件。
"""

from __future__ import annotations

import json
from typing import AsyncIterator

import httpx


class DeepSeekStreamError(Exception):
    """DeepSeek 在流式调用中返回了错误。"""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


async def stream_review(
    *,
    api_url: str,
    api_key: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int,
    timeout: float,
    client: httpx.AsyncClient | None = None,
) -> AsyncIterator[str]:
    """调用 DeepSeek 流式接口，逐段产出文本。

    Args:
        client: 可选的自定义 AsyncClient，主要用于测试时注入 MockTransport。
    """
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.3,
        "max_tokens": max_tokens,
        "stream": True,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }

    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=10.0),
        )

    try:
        async with client.stream(
            "POST",
            api_url,
            headers=headers,
            json=payload,
        ) as response:
            if response.status_code >= 400:
                body = (await response.aread()).decode("utf-8", errors="replace")
                raise DeepSeekStreamError(body[:1000], response.status_code)

            async for line in response.aiter_lines():
                line = line.strip()
                if not line or not line.startswith("data:"):
                    continue

                data = line[5:].strip()
                if data == "[DONE]":
                    break

                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue

                if not isinstance(event, dict):
                    continue

                if event.get("error"):
                    raise DeepSeekStreamError(str(event["error"]), None)

                choices = event.get("choices") or []
                if not choices or not isinstance(choices[0], dict):
                    continue

                delta = choices[0].get("delta") or {}
                if not isinstance(delta, dict):
                    continue

                content = delta.get("content")
                if isinstance(content, str) and content:
                    yield content
    finally:
        if owns_client:
            await client.aclose()
