"""Cross-chain payment through NEAR Intents: quote, pay the deposit address from the vault, then follow the swap.

The vault enforces the spending policy on the amount it sends to the deposit address; 1Click delivers the result to the
recipient on the destination chain. The final recipient is *declared* in the vault event, not enforced on-chain.

Mainnet only in practice (1Click has no testnet). This module is covered by fakes and a sandbox test of the contract path;
it has NOT been run with real funds.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from wayfarer.errors import UpstreamError, WayfarerError
from wayfarer.oneclick import OneClick, Quote, QuoteRequest, SwapStatus
from wayfarer.vault import PaymentResult, PaymentStatus, VaultClient

POLL_INTERVAL_S = 5.0
SETTLEMENT_TIMEOUT_S = 300.0


class SwapOutcome(StrEnum):
    DELIVERED = "delivered"
    REFUNDED = "refunded"  # 1Click returned the funds to the refund address
    FAILED = "failed"
    NOT_SENT = "not_sent"  # the vault did not pay, so nothing is in flight
    PENDING = "pending"  # paid, but no final state within the timeout: check the status link


@dataclass(frozen=True, slots=True)
class SwapPlan:
    """What the user is shown before confirming."""

    request: QuoteRequest
    quote: Quote
    deposit_address: str
    destination_label: str

    def read_back(self) -> str:
        eta = f"~{self.quote.eta_seconds}s" if self.quote.eta_seconds is not None else "unknown time"
        return (
            f"Send {self.quote.amount_in} -> about {self.quote.amount_out} to {self.destination_label} "
            f"(at least {self.quote.min_amount_out} base units), {eta}. No payment is made until you confirm."
        )


@dataclass(frozen=True, slots=True)
class SwapReport:
    outcome: SwapOutcome
    payment: PaymentResult
    deposit_address: str
    last_status: SwapStatus | None = None


class IntentsSettlement:
    def __init__(
        self,
        oneclick: OneClick,
        vault: VaultClient,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._oneclick = oneclick
        self._vault = vault
        self._sleep = sleep
        self._clock = clock

    def plan(self, request: QuoteRequest, *, destination_label: str) -> SwapPlan:
        """Create a real quote (reserves a deposit address; moves no funds)."""
        quote = self._oneclick.quote(request, dry=False)
        if quote.deposit_address is None:
            raise UpstreamError("1Click returned no deposit address.")
        return SwapPlan(request, quote, quote.deposit_address, destination_label)

    def execute(
        self,
        plan: SwapPlan,
        *,
        token_id: str,
        amount: int,
        declared_recipient: str,
        commitment: str,
        nullifier: str,
        timeout_s: float = SETTLEMENT_TIMEOUT_S,
    ) -> SwapReport:
        """Pay the deposit address from the vault, tell 1Click, then poll until the swap reaches a final state."""
        if str(amount) != plan.request.amount:
            raise WayfarerError("The amount to pay does not match the quoted amount.")
        payment = self._vault.pay_via_intents(
            token_id=token_id,
            deposit_address=plan.deposit_address,
            amount=amount,
            declared_recipient=declared_recipient,
            commitment=commitment,
            nullifier=nullifier,
        )
        if payment.status is not PaymentStatus.PAID:
            return SwapReport(SwapOutcome.NOT_SENT, payment, plan.deposit_address)
        if payment.tx_hash:
            with contextlib.suppress(UpstreamError):  # optional speed-up only; status polling still works
                self._oneclick.submit_deposit(tx_hash=payment.tx_hash, deposit_address=plan.deposit_address)
        return self._follow(payment, plan.deposit_address, timeout_s)

    def _follow(self, payment: PaymentResult, deposit_address: str, timeout_s: float) -> SwapReport:
        outcome, last = follow_swap(
            self._oneclick, deposit_address, timeout_s=timeout_s, sleep=self._sleep, clock=self._clock
        )
        return SwapReport(outcome, payment, deposit_address, last)


def follow_swap(
    oneclick: OneClick,
    deposit_address: str,
    *,
    timeout_s: float = SETTLEMENT_TIMEOUT_S,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[SwapOutcome, SwapStatus | None]:
    """Poll 1Click until the swap reaches a final state or ``timeout_s`` passes (then ``PENDING``, never success)."""
    deadline = clock() + timeout_s
    last: SwapStatus | None = None
    while clock() < deadline:
        with contextlib.suppress(UpstreamError):  # transient; keep polling until the deadline
            last = oneclick.status(deposit_address)
        if last is not None and last.terminal:
            outcome = {
                SwapStatus.SUCCESS: SwapOutcome.DELIVERED,
                SwapStatus.REFUNDED: SwapOutcome.REFUNDED,
            }.get(last, SwapOutcome.FAILED)
            return outcome, last
        sleep(POLL_INTERVAL_S)
    return SwapOutcome.PENDING, last
