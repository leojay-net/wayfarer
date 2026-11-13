"""Conversation logic: voice or typed instruction -> read-back -> explicit confirmation -> vault payment.

Security properties enforced here:
* nothing is paid unless the user taps Confirm on the read-back for *that* instruction (buttons carry the nullifier);
* a pending instruction expires, so a stale button cannot fire later;
* payments are attempted only for the allow-listed chat; every other chat can never trigger a payment;
* transcripts are never written to the application log.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from wayfarer.attestation import Attestation, attest_completion
from wayfarer.errors import WayfarerError
from wayfarer.intent import IntentExtractor, PaymentIntent, read_back
from wayfarer.nearai import NearAiClient
from wayfarer.receipts import ReceiptLedger, ReceiptOpening
from wayfarer.telegram.api import TelegramApi
from wayfarer.vault import PaymentStatus, VaultClient

log = logging.getLogger("wayfarer.bot")

PENDING_TTL_S = 300.0
_CONFIRM, _CANCEL = "ok", "no"


@dataclass(slots=True)
class PendingPayment:
    opening: ReceiptOpening
    intent: PaymentIntent
    expires_at: float


class BotService:
    def __init__(
        self,
        *,
        api: TelegramApi,
        nearai: NearAiClient,
        extractor: IntentExtractor,
        ledger: ReceiptLedger,
        vault: VaultClient | None,
        allowed_chat_id: int | None,
        language: str | None = None,
        clock: Callable[[], float] = time.monotonic,
        attest: bool = True,
    ) -> None:
        self._api = api
        self._nearai = nearai
        self._extractor = extractor
        self._ledger = ledger
        self._vault = vault
        self._allowed = allowed_chat_id
        self._language = language
        self._clock = clock
        self._attest = attest
        self._pending: dict[int, PendingPayment] = {}

    # -- entry point ---------------------------------------------------------------------------------------------
    def handle_update(self, update: dict[str, Any]) -> None:
        """Handle one update. Any failure is reported in the chat, never swallowed silently."""
        chat_id = self._chat_id(update)
        try:
            if "callback_query" in update:
                self._on_callback(update["callback_query"])
            elif "message" in update:
                self._on_message(update["message"])
        except WayfarerError as error:
            self._reply_error(chat_id, str(error))
        except Exception as error:
            log.exception("unhandled error while handling an update")
            self._reply_error(chat_id, f"something went wrong ({type(error).__name__})")

    @staticmethod
    def _chat_id(update: dict[str, Any]) -> int | None:
        message = update.get("message") or (update.get("callback_query") or {}).get("message") or {}
        chat = message.get("chat", {}).get("id")
        return int(chat) if chat is not None else None

    def _is_authorised(self, chat_id: int | None) -> bool:
        return self._allowed is not None and chat_id == self._allowed

    def _reply_error(self, chat_id: int | None, text: str) -> None:
        if chat_id is None or (self._allowed is not None and chat_id != self._allowed):
            return
        try:
            self._api.send_message(chat_id, f"Sorry: {text}\nNothing was sent.")
        except WayfarerError:
            log.warning("could not deliver an error message to the chat")

    # -- messages ---------------------------------------------------------------------------------------------------
    def _on_message(self, message: dict[str, Any]) -> None:
        chat_id = int(message["chat"]["id"])
        if str(message.get("text", "")).startswith("/start"):
            self._api.send_message(
                chat_id,
                f'Wayfarer (testnet prototype). Your chat id is {chat_id}.\nSend a voice note or type an instruction, e.g. "send 0.1 NEAR to ada.testnet".',
            )
            return
        if self._allowed is not None and chat_id != self._allowed:
            return
        if "voice" in message:
            started = time.monotonic()
            audio = self._api.download_file(message["voice"]["file_id"])
            transcript = self._nearai.transcribe(audio, language=self._language).text
            log.info("voice note handled: %d bytes, transcribed in %.1fs", len(audio), time.monotonic() - started)
            self._propose(chat_id, transcript, typed=False)
        elif message.get("text"):
            self._propose(chat_id, str(message["text"]), typed=True)
        else:
            self._api.send_message(
                chat_id,
                'Send a voice note (or type an instruction), for example: "send 0.1 NEAR to ada.testnet".',
            )

    def _propose(self, chat_id: int, transcript: str, *, typed: bool) -> None:
        extraction = self._extractor.extract(transcript)
        opening = ReceiptOpening.create(transcript, extraction.intent)
        attestation = self._try_attest(extraction.completion) if self._attest else None
        self._ledger.append("proposed", opening, attestation=attestation.to_json() if attestation else None)
        self._pending[chat_id] = PendingPayment(opening, extraction.intent, self._clock() + PENDING_TTL_S)
        self._api.send_message(
            chat_id,
            read_back(extraction.intent, transcript, typed=typed),
            buttons=[
                ("Confirm", f"{_CONFIRM}:{opening.nullifier}"),
                ("Cancel", f"{_CANCEL}:{opening.nullifier}"),
            ],
        )

    def _try_attest(self, completion: Any) -> Attestation | None:
        try:
            return attest_completion(self._nearai, completion)
        except WayfarerError as error:
            log.warning("could not attest the parse: %s", error)
            return None

    # -- confirmation ---------------------------------------------------------------------------------------------
    def _on_callback(self, callback: dict[str, Any]) -> None:
        chat_id = int(callback["message"]["chat"]["id"])
        try:  # only stops the button spinner; Telegram rejects it for stale taps
            self._api.answer_callback(callback["id"])
        except WayfarerError as error:
            log.info("callback acknowledgement ignored: %s", error)
        if not self._is_authorised(chat_id):
            return
        action, _, nullifier = str(callback.get("data", "")).partition(":")
        pending = self._pending.get(chat_id)
        if pending is None or pending.opening.nullifier != nullifier:
            self._api.send_message(chat_id, "That button is out of date. Nothing was sent.")
            return
        del self._pending[chat_id]
        if self._clock() > pending.expires_at:
            self._ledger.append("expired", pending.opening)
            self._api.send_message(chat_id, "That instruction expired. Nothing was sent.")
        elif action != _CONFIRM:
            self._ledger.append("cancelled", pending.opening)
            self._api.send_message(chat_id, "Cancelled. Nothing was sent.")
        else:
            self._settle(chat_id, pending)

    def _settle(self, chat_id: int, pending: PendingPayment) -> None:
        summary = "{amount} {token} to {recipient}".format(**pending.intent.canonical())
        if self._vault is None:
            self._api.send_message(chat_id, f"Confirmed: {summary}.\nNO payment was made: no vault is configured.")
            return
        result = self._vault.pay(
            pending.intent, commitment=pending.opening.commitment, nullifier=pending.opening.nullifier
        )
        self._ledger.append(result.status.value, pending.opening, tx_hash=result.tx_hash, detail=result.message)
        link = f"\n{result.explorer_url}" if result.explorer_url else ""
        if result.status is PaymentStatus.PAID:
            text = f"Paid {summary} on NEAR testnet (prototype, no real value).{link}"
        elif result.status is PaymentStatus.REFUSED:
            text = f"Refused by the vault contract: {result.message}. Nothing was sent.{link}"
        else:
            text = f"NO payment was confirmed: {result.message}{link}"
        self._api.send_message(chat_id, text)
