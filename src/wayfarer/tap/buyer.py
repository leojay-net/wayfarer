"""The buyer's phone: review a request, ask the human to confirm, sign a cheque with the ML-DSA agent key."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from wayfarer.cheque import sign_cheque
from wayfarer.errors import TapError
from wayfarer.near.keys import MlDsaKeyPair
from wayfarer.tap.envelope import ChequeEnvelope, cheque_for_request
from wayfarer.tap.rates import TOKEN_DECIMALS
from wayfarer.tap.request import PaymentRequest


@dataclass(frozen=True, slots=True)
class ApprovalPrompt:
    """What the human sees before confirming, in their own terms first."""

    merchant_name: str
    merchant_account: str
    fiat_text: str
    asset_text: str
    settle_by_ms: int

    def lines(self) -> list[str]:
        return [
            f"Pay {self.fiat_text} to {self.merchant_name}",
            f"= {self.asset_text} from your vault",
            f"Merchant account: {self.merchant_account}",
        ]


class Confirmer(Protocol):
    """The human approval step (biometric or PIN on a phone). Must return True only for an explicit yes."""

    def confirm(self, prompt: ApprovalPrompt) -> bool: ...


@dataclass(frozen=True, slots=True)
class TapLimits:
    """Per-tap caps in each token's smallest unit, like contactless limits. A token not listed cannot be paid by tap."""

    per_tap: Mapping[str, int]


def asset_text(token_id: str, amount: int) -> str:
    decimals = TOKEN_DECIMALS.get(token_id)
    if decimals is None:
        return f"{amount} {token_id} (smallest units)"
    whole = Decimal(amount).scaleb(-decimals)
    return f"{whole:.6f} {token_id.upper()}"


class BuyerWallet:
    def __init__(
        self,
        *,
        vault: str,
        agent_key: MlDsaKeyPair,
        confirmer: Confirmer,
        limits: TapLimits,
        clock_ms: Callable[[], int],
        include_public_key: bool = True,
    ) -> None:
        self._vault = vault
        self._key = agent_key
        self._confirmer = confirmer
        self._limits = limits
        self._clock = clock_ms
        self._include_public_key = include_public_key
        self._signed: set[bytes] = set()

    def review(self, request: PaymentRequest) -> ApprovalPrompt:
        """Validate a request and build the prompt. Raises :class:`TapError` with a user-safe reason when refusing."""
        if self._clock() > request.quote_expires_ms:
            raise TapError("This price has expired. Ask the merchant for a new one.")
        limit = self._limits.per_tap.get(request.token_id)
        if limit is None:
            raise TapError(f"Paying with {request.token_id} is not enabled on this phone.")
        if request.asset_amount > limit:
            raise TapError(f"That is above your tap limit of {asset_text(request.token_id, limit)}.")
        if request.nonce in self._signed:
            raise TapError("You already approved this request.")
        return ApprovalPrompt(
            merchant_name=request.merchant_name,
            merchant_account=request.merchant_account,
            fiat_text=request.fiat_display(),
            asset_text=asset_text(request.token_id, request.asset_amount),
            settle_by_ms=request.settle_by_ms,
        )

    def pay(self, request: PaymentRequest) -> ChequeEnvelope:
        """Review, get an explicit confirmation, then sign. Nothing is signed unless the human says yes."""
        prompt = self.review(request)
        if not self._confirmer.confirm(prompt):
            raise TapError("Payment cancelled.")
        if self._clock() > request.quote_expires_ms:
            raise TapError("This price expired while you were deciding.")
        cheque = cheque_for_request(request, self._vault)
        signature = sign_cheque(self._key, cheque)
        self._signed.add(request.nonce)
        return ChequeEnvelope(
            request_hash=request.request_hash(),
            vault=self._vault,
            key_id=hashlib.sha256(self._key.public_key).digest(),
            signature=signature,
            public_key=self._key.public_key if self._include_public_key else None,
        )
