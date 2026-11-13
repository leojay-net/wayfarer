"""The few Telegram Bot API calls we use."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from wayfarer.errors import UpstreamError
from wayfarer.transport import Transport, build_request, urllib_transport

_MAX_AUDIO_BYTES = 25 * 1024 * 1024


class TelegramApi:
    def __init__(self, token: str, *, transport: Transport = urllib_transport) -> None:
        if not token:
            raise UpstreamError("TELEGRAM_BOT_TOKEN is not set.")
        self._base = f"https://api.telegram.org/bot{token}/"
        self._file_base = f"https://api.telegram.org/file/bot{token}/"
        self._transport = transport

    def call(self, method: str, params: Mapping[str, Any], *, timeout_s: float = 60.0) -> Any:
        request = build_request(
            self._base + method,
            body=json.dumps(params).encode(),
            headers={"Content-Type": "application/json"},
        )
        response = self._transport(request, timeout_s)
        try:
            payload = json.loads(response.body)
        except ValueError as error:
            raise UpstreamError(f"Telegram {method} sent an unreadable answer.") from error
        if not payload.get("ok"):
            raise UpstreamError(f"Telegram {method} failed: {payload.get('description', response.status)}")
        return payload["result"]

    def get_updates(self, offset: int) -> list[dict[str, Any]]:
        result = self.call(
            "getUpdates",
            {"offset": offset, "timeout": 30, "allowed_updates": ["message", "callback_query"]},
            timeout_s=45.0,
        )
        return list(result)

    def send_message(self, chat_id: int, text: str, *, buttons: list[tuple[str, str]] | None = None) -> None:
        params: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if buttons:
            params["reply_markup"] = {
                "inline_keyboard": [[{"text": label, "callback_data": data} for label, data in buttons]]
            }
        self.call("sendMessage", params)

    def answer_callback(self, callback_id: str) -> None:
        self.call("answerCallbackQuery", {"callback_query_id": callback_id})

    def download_file(self, file_id: str) -> bytes:
        file_path = self.call("getFile", {"file_id": file_id})["file_path"]
        response = self._transport(build_request(self._file_base + file_path, method="GET"), 60.0)
        if response.status != 200:
            raise UpstreamError(f"Telegram file download answered {response.status}.")
        if len(response.body) > _MAX_AUDIO_BYTES:
            raise UpstreamError("The voice note is too large.")
        return response.body
