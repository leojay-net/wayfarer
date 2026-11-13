"""Client for NEAR AI Cloud (OpenAI-compatible API running in TEEs): speech-to-text, chat and response signatures."""

from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from wayfarer.config import Settings
from wayfarer.cost import (
    FALLBACK_COMPLETION_USD,
    FALLBACK_PROMPT_USD,
    WHISPER_USD_PER_MINUTE,
    CostMeter,
    Pricing,
)
from wayfarer.errors import UpstreamError, WayfarerError
from wayfarer.transport import HttpResponse, Transport, build_request, encode_multipart, urllib_transport

_SIGNATURE_RETRIES = 4
_SIGNATURE_RETRY_DELAY_S = 1.0


@dataclass(frozen=True, slots=True)
class Transcription:
    text: str
    language: str | None
    duration_s: float | None


@dataclass(frozen=True, slots=True)
class ChatResult:
    """A chat completion plus the exact bytes exchanged (needed to check the TEE's signature)."""

    content: str
    completion_id: str
    model: str
    finish_reason: str | None
    request_body: bytes
    response_body: bytes


@dataclass(frozen=True, slots=True)
class SignatureRecord:
    """The ``/v1/signature`` payload for one completion."""

    text: str
    signature: str
    signing_address: str
    signing_algo: str
    signature_kind: str | None


class NearAiClient:
    """Thin, dependency-free wrapper around the NEAR AI Cloud HTTP API."""

    def __init__(
        self, settings: Settings, *, transport: Transport = urllib_transport, meter: CostMeter | None = None
    ) -> None:
        self._settings = settings
        self._transport = transport
        self._meter = meter
        self._pricing: dict[str, Pricing] = {}

    # -- plumbing ------------------------------------------------------------------------------------------------
    def _auth(self) -> dict[str, str]:
        if not self._settings.near_ai_api_key:
            raise UpstreamError("NEAR_AI_API_KEY is not set.")
        return {"Authorization": f"Bearer {self._settings.near_ai_api_key}"}

    def _send(self, path: str, *, body: bytes | None, content_type: str | None, timeout: float) -> HttpResponse:
        headers = self._auth()
        if content_type:
            headers["Content-Type"] = content_type
        request = build_request(self._settings.near_ai_base_url + path, body=body, headers=headers)
        response = self._transport(request, timeout)
        if response.status == 402:
            raise UpstreamError("The NEAR AI Cloud account has no credits or spending limit configured.")
        if response.status == 429:
            raise UpstreamError("NEAR AI Cloud is rate limiting requests; try again shortly.")
        if response.status >= 400:
            raise UpstreamError(
                f"NEAR AI Cloud answered {response.status}: {response.body[:160].decode('utf-8', 'replace')}"
            )
        return response

    @staticmethod
    def _json(response: HttpResponse) -> dict[str, Any]:
        try:
            data = json.loads(response.body)
        except ValueError as error:
            raise UpstreamError("NEAR AI Cloud sent an unreadable answer.") from error
        if not isinstance(data, dict):
            raise UpstreamError("NEAR AI Cloud sent an unexpected answer.")
        return data

    def pricing(self, model: str) -> Pricing:
        """USD-per-token prices from the model catalogue (cached); conservative fallback if unavailable."""
        if model not in self._pricing:
            price = Pricing(FALLBACK_PROMPT_USD, FALLBACK_COMPLETION_USD)
            try:
                request = build_request(self._settings.near_ai_base_url + "/models", headers=self._auth())
                catalogue = self._json(self._transport(request, 20.0))
                for entry in catalogue.get("data", []):
                    if entry.get("id") == model and "pricing" in entry:
                        price = Pricing(
                            Decimal(str(entry["pricing"]["prompt"])),
                            Decimal(str(entry["pricing"]["completion"])),
                        )
            except (WayfarerError, ArithmeticError, KeyError, TypeError):
                pass
            self._pricing[model] = price
        return self._pricing[model]

    # -- speech to text -------------------------------------------------------------------------------------------
    def transcribe(self, audio: bytes, *, filename: str = "voice.ogg", language: str | None = None) -> Transcription:
        """Transcribe audio with Whisper inside NEAR AI Cloud. ``language`` is an optional ISO 639-1 hint."""
        if self._meter:
            self._meter.ensure_headroom()
        fields = {"model": self._settings.asr_model, "response_format": "verbose_json"}
        if language:
            fields["language"] = language
        body, content_type = encode_multipart(fields, "file", filename, "audio/ogg", audio)
        data = self._json(
            self._send(
                "/audio/transcriptions",
                body=body,
                content_type=content_type,
                timeout=self._settings.asr_timeout_s,
            )
        )
        text = str(data.get("text", "")).strip()
        duration = data.get("duration")
        if self._meter:
            minutes = Decimal(str(duration or 60)) / 60
            self._meter.record(minutes * WHISPER_USD_PER_MINUTE)
        if not text:
            raise UpstreamError("No speech was recognised.")
        return Transcription(text, data.get("language"), float(duration) if duration is not None else None)

    # -- chat ------------------------------------------------------------------------------------------------------
    def chat(self, messages: Sequence[Mapping[str, str]], *, max_tokens: int = 800) -> ChatResult:
        """One chat completion. ``reasoning_effort`` is kept low: the default model cannot switch reasoning off."""
        if self._meter:
            self._meter.ensure_headroom()
        payload = {
            "model": self._settings.llm_model,
            "messages": list(messages),
            "max_tokens": max_tokens,
            "temperature": 0,
            "reasoning_effort": "low",
        }
        request_body = json.dumps(payload).encode()
        response = self._send(
            "/chat/completions",
            body=request_body,
            content_type="application/json",
            timeout=self._settings.llm_timeout_s,
        )
        data = self._json(response)
        try:
            choice = data["choices"][0]
            content = choice["message"].get("content") or ""
            completion_id = str(data["id"])
        except (KeyError, IndexError, TypeError, AttributeError) as error:
            raise UpstreamError("NEAR AI Cloud sent an unexpected chat answer.") from error
        if self._meter:
            usage = data.get("usage", {})
            price = self.pricing(self._settings.llm_model)
            self._meter.record(
                price.chat_cost(int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0)))
            )
        if not content:
            raise UpstreamError(f"The model returned no answer (finish_reason={choice.get('finish_reason')}).")
        return ChatResult(
            content,
            completion_id,
            str(data.get("model", self._settings.llm_model)),
            choice.get("finish_reason"),
            request_body,
            response.body,
        )

    # -- response signatures -----------------------------------------------------------------------------------------
    def signature(self, completion_id: str, *, algo: str = "ed25519") -> SignatureRecord:
        """Fetch the TEE's signature for a completion; retries briefly because it may not be ready immediately."""
        last: Exception | None = None
        for attempt in range(_SIGNATURE_RETRIES):
            try:
                path = f"/signature/{completion_id}?signing_algo={algo}"
                data = self._json(self._send(path, body=None, content_type=None, timeout=15.0))
                if "signature" in data and "text" in data:
                    return SignatureRecord(
                        text=str(data["text"]),
                        signature=str(data["signature"]),
                        signing_address=str(data["signing_address"]),
                        signing_algo=str(data.get("signing_algo", algo)),
                        signature_kind=data.get("signature_kind"),
                    )
                last = UpstreamError("Signature not available yet.")
            except UpstreamError as error:
                last = error
            if attempt + 1 < _SIGNATURE_RETRIES:
                time.sleep(_SIGNATURE_RETRY_DELAY_S)
        raise UpstreamError(f"Could not fetch the response signature: {last}")
