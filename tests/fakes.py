"""Test doubles shared by the suite. No test in this package touches the network."""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Callable
from typing import Any

from wayfarer.config import Settings
from wayfarer.transport import HttpResponse

Handler = Callable[[urllib.request.Request], HttpResponse | Exception]


class FakeTransport:
    """Routes requests by URL suffix to handlers and records every request."""

    def __init__(self) -> None:
        self.handlers: dict[str, Handler] = {}
        self.requests: list[urllib.request.Request] = []

    def on(self, suffix: str, handler: Handler) -> None:
        self.handlers[suffix] = handler

    def __call__(self, request: urllib.request.Request, timeout: float) -> HttpResponse:
        self.requests.append(request)
        for suffix, handler in self.handlers.items():
            if request.full_url.split("?")[0].endswith(suffix):
                result = handler(request)
                if isinstance(result, Exception):
                    raise result
                return result
        raise AssertionError(f"unexpected request to {request.full_url}")


def json_response(payload: Any, status: int = 200) -> HttpResponse:
    return HttpResponse(status, json.dumps(payload).encode())


def settings(**overrides: Any) -> Settings:
    return Settings(near_ai_api_key="test-key", **overrides)


def chat_payload(
    content: str, *, completion_id: str = "cmpl-1", prompt_tokens: int = 100, completion_tokens: int = 50
) -> dict[str, Any]:
    return {
        "id": completion_id,
        "model": "z-ai/glm-5.3-flash",
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
    }
