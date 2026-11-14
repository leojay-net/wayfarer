"""The merchant's phone: price a sale, accept a signed cheque offline, optionally strong-check it against the chain.

Offline acceptance proves the buyer's ML-DSA key signed exactly this request. It does NOT prove the payment will settle:
the vault may be empty, the key revoked, the mandate ended or the daily cap used up. ``strong_check`` reads the vault
(online) to reduce that risk; even then only on-chain settlement is final.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from wayfarer.cheque import Cheque, verify_cheque
from wayfarer.errors import TapError
from wayfarer.pqvault import MandateTerms
from wayfarer.tap.envelope import ChequeEnvelope, cheque_for_request
from wayfarer.tap.rates import RateDesk, asset_amount
from wayfarer.tap.request import NONCE_BYTES, PaymentRequest
from wayfarer.vault import NATIVE_TOKEN_ID

DEFAULT_SETTLE_WINDOW_MS = 10 * 60_000


@dataclass(frozen=True, slots=True)
class AcceptedCheque:
    """A cheque that passed offline checks: pending settlement, not paid."""

    request: PaymentRequest
    cheque: Cheque
    key_id: str
    signature: bytes
    public_key: bytes


class ChainView(Protocol):
    """Read-only vault queries used by the strong check."""

    def mandate(self, key_id: str, token_id: str) -> MandateTerms | None: ...

    def remaining_today(self, token_id: str) -> int | None: ...

    def spendable_balance(self, token_id: str) -> int: ...

    def intent_used(self, intent_id: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class StrongCheck:
    ok: bool
    problems: tuple[str, ...]


class MerchantTerminal:
    def __init__(
        self,
        *,
        account: str,
        name: str,
        rates: RateDesk,
        clock_ms: Callable[[], int],
        settle_window_ms: int = DEFAULT_SETTLE_WINDOW_MS,
        nonce_factory: Callable[[], bytes] = lambda: secrets.token_bytes(NONCE_BYTES),
    ) -> None:
        self._account = account
        self._name = name
        self._rates = rates
        self._clock = clock_ms
        self._window = settle_window_ms
        self._nonce = nonce_factory
        self._open: dict[bytes, PaymentRequest] = {}
        self._accepted: set[bytes] = set()

    def create_request(
        self, fiat_minor: int, *, fiat_currency: str = "USD", fiat_exponent: int = 2, token_id: str = NATIVE_TOKEN_ID
    ) -> PaymentRequest:
        """Lock a rate and build the request to show to the buyer's phone."""
        quote = self._rates.lock(fiat_currency, token_id)
        request = PaymentRequest(
            merchant_account=self._account,
            merchant_name=self._name,
            fiat_currency=fiat_currency,
            fiat_minor=fiat_minor,
            fiat_exponent=fiat_exponent,
            token_id=token_id,
            asset_amount=asset_amount(quote, fiat_minor, fiat_exponent),
            quote_id=quote.quote_id,
            rate=format(quote.fiat_per_token, "f"),
            quote_expires_ms=quote.expires_ms,
            settle_by_ms=quote.expires_ms + self._window,
            nonce=self._nonce(),
            created_ms=quote.created_ms,
        )
        self._open[request.request_hash()] = request
        return request

    def accept(self, raw_envelope: bytes) -> AcceptedCheque:
        """Check a cheque offline. Raises :class:`TapError` with a reason if it must not be accepted."""
        envelope = ChequeEnvelope.decode(raw_envelope)
        request = self._open.get(envelope.request_hash)
        if request is None:
            raise TapError("This cheque is not for any request of this terminal.")
        if envelope.request_hash in self._accepted:
            raise TapError("A cheque for this request was already accepted.")
        if self._clock() > request.settle_by_ms:
            raise TapError("This request is too old to settle.")
        if envelope.public_key is None:
            raise TapError("The cheque does not carry the buyer's public key; it cannot be checked offline.")
        if hashlib.sha256(envelope.public_key).digest() != envelope.key_id:
            raise TapError("The key id does not match the public key.")
        cheque = cheque_for_request(request, envelope.vault)
        if not verify_cheque(envelope.public_key, envelope.signature, cheque):
            raise TapError("The cheque signature is not valid for this request.")
        self._accepted.add(envelope.request_hash)
        return AcceptedCheque(request, cheque, envelope.key_id.hex(), envelope.signature, envelope.public_key)

    def strong_check(self, accepted: AcceptedCheque, chain: ChainView) -> StrongCheck:
        """Online check of what the chain can already tell us. A pass lowers the risk; it is not finality."""
        request, problems = accepted.request, []
        mandate = chain.mandate(accepted.key_id, request.token_id)
        if mandate is None:
            problems.append("The vault does not know this key (or has no policy for this token).")
        else:
            now = self._clock()
            if mandate.paused:
                problems.append("The vault is paused.")
            if mandate.expires_at_ms and mandate.expires_at_ms < request.settle_by_ms:
                problems.append("The buyer's mandate ends before the settlement deadline.")
            if request.asset_amount > mandate.per_tx_max:
                problems.append("The amount is above the per-payment cap.")
            if mandate.min_interval_ms and now < mandate.last_payment_ms + mandate.min_interval_ms:
                problems.append("The key paid too recently (minimum interval).")
            if mandate.allowlist_enabled and request.merchant_account not in mandate.allowlist:
                problems.append("This merchant is not on the vault's allow-list.")
        remaining = chain.remaining_today(request.token_id)
        if remaining is not None and request.asset_amount > remaining:
            problems.append("The vault's daily budget is too low for this amount.")
        if chain.spendable_balance(request.token_id) < request.asset_amount:
            problems.append("The vault balance is too low.")
        if chain.intent_used(request.intent_id()):
            problems.append("This payment id was already used on-chain.")
        return StrongCheck(not problems, tuple(problems))
