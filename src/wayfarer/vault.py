"""Client for the ``wayfarer-vault`` contract: an agent pays only within the owner's on-chain policy."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from wayfarer.errors import UpstreamError, WayfarerError
from wayfarer.intent import PaymentIntent, Token
from wayfarer.near.events import extract_events, failure_message
from wayfarer.near.keys import SigningKey
from wayfarer.near.rpc import NearRpc

NATIVE_TOKEN_ID = "near"  # noqa: S105 (a contract token id, not a credential)
TESTNET_EXPLORER = "https://explorer.testnet.near.org/transactions/"
# Intent token -> vault token id. Only native NEAR has a policy on the testnet vault; USDC/USDT settlement is not implemented.
SUPPORTED_TOKENS: dict[Token, str] = {Token.NEAR: NATIVE_TOKEN_ID}


class PaymentStatus(StrEnum):
    PAID = "paid"
    REFUSED = "refused"  # the contract rejected the payment (policy)
    FAILED = "failed"  # authorised but the token transfer failed; budget restored
    UNSUPPORTED = "unsupported"
    ERROR = "error"  # we could not tell, or the network failed: treat as NOT paid until checked


@dataclass(frozen=True, slots=True)
class PaymentResult:
    status: PaymentStatus
    message: str
    tx_hash: str | None = None
    explorer_url: str | None = None


@dataclass(frozen=True, slots=True)
class Budget:
    """Today's spend and remaining allowance in the token's smallest unit."""

    spent: int
    remaining: int


class VaultClient:
    def __init__(
        self, vault_account: str, key: SigningKey, rpc: NearRpc, *, explorer_base: str = TESTNET_EXPLORER
    ) -> None:
        if key.account_id != vault_account:
            raise WayfarerError("The agent key belongs to a different account than the vault.")
        self._vault = vault_account
        self._key = key
        self._rpc = rpc
        self._explorer = explorer_base

    @property
    def account_id(self) -> str:
        return self._vault

    @property
    def explorer_base(self) -> str:
        return self._explorer

    def budget(self, token_id: str = NATIVE_TOKEN_ID) -> Budget | None:
        result: Any = self._rpc.view_function(self._vault, "get_budget", {"token_id": token_id})
        return None if result is None else Budget(int(result[0]), int(result[1]))

    def pay(self, intent: PaymentIntent, *, commitment: str, nullifier: str) -> PaymentResult:
        """Ask the vault to pay ``intent``. ``PAID`` is reported only after the contract's own settlement event is seen."""
        token_id = SUPPORTED_TOKENS.get(intent.token)
        if token_id is None:
            return PaymentResult(
                PaymentStatus.UNSUPPORTED,
                f"{intent.token.value} settlement is not available in this testnet build (only NEAR is wired to the vault).",
            )
        return self.pay_direct(
            token_id=token_id,
            to=intent.recipient,
            amount=intent.base_units(),
            commitment=commitment,
            nullifier=nullifier,
        )

    def pay_direct(self, *, token_id: str, to: str, amount: int, commitment: str, nullifier: str) -> PaymentResult:
        args = {
            "intent_id": nullifier,
            "token_id": token_id,
            "to": to,
            "amount": str(amount),
            "receipt_hash": commitment,
        }
        return self._submit("pay_direct", args)

    def pay_via_intents(
        self,
        *,
        token_id: str,
        deposit_address: str,
        amount: int,
        declared_recipient: str,
        commitment: str,
        nullifier: str,
    ) -> PaymentResult:
        """Pay a 1Click deposit address. ``declared_recipient`` is recorded in the event, not enforced on-chain."""
        args = {
            "intent_id": nullifier,
            "token_id": token_id,
            "deposit_address": deposit_address,
            "amount": str(amount),
            "declared_recipient": declared_recipient,
            "receipt_hash": commitment,
        }
        return self._submit("pay_via_intents", args)

    def _submit(self, method: str, args: dict[str, str]) -> PaymentResult:
        try:
            outcome = self._rpc.function_call(self._key, self._vault, method, args)
        except UpstreamError as error:
            return PaymentResult(PaymentStatus.ERROR, str(error))
        return result_from_outcome(outcome, self._explorer)


def result_from_outcome(outcome: Mapping[str, Any], explorer_base: str) -> PaymentResult:
    """Turn a final transaction outcome into a ``PaymentResult``. ``PAID`` needs the contract's own settlement event."""
    tx_hash = str(outcome["transaction"]["hash"])
    url = explorer_base + tx_hash
    refusal = failure_message(outcome)
    if refusal:
        return PaymentResult(PaymentStatus.REFUSED, refusal, tx_hash, url)
    names = {event.get("event") for event in extract_events(outcome)}
    if "payment_failed" in names:
        return PaymentResult(
            PaymentStatus.FAILED, "The transfer failed on-chain; the budget was restored.", tx_hash, url
        )
    if "payment_settled" in names:
        return PaymentResult(PaymentStatus.PAID, "Paid.", tx_hash, url)
    return PaymentResult(
        PaymentStatus.ERROR,
        "The transaction was sent but no settlement event was seen; check the explorer.",
        tx_hash,
        url,
    )
