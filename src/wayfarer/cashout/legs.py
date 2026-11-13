"""Swap legs: NEAR -> USDC for a merchant cash-out.

* :class:`SandboxSwapLeg`: deterministic fake with a demo price. SANDBOX: nothing is swapped.
* :class:`IntentsSwapLeg`: the real leg over NEAR Intents 1Click. MAINNET ONLY (1Click has no testnet) and NOT RUN with real
  funds. It wraps NEAR, funds a fresh 1Click deposit address with wNEAR and follows the swap. Unit-tested with fakes only.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Protocol

from wayfarer.cashout.rails import DepositTarget
from wayfarer.errors import UpstreamError, WayfarerError
from wayfarer.near.events import failure_message
from wayfarer.near.keys import SigningKey
from wayfarer.near.rpc import NearRpc
from wayfarer.near.transaction import action_function_call
from wayfarer.oneclick import OneClick, QuoteRequest
from wayfarer.swap import SETTLEMENT_TIMEOUT_S, SwapOutcome, follow_swap

WRAP_NEAR = "wrap.near"
WRAPPED_NEAR_ASSET = "nep141:wrap.near"
NEAR_UNIT = Decimal(10) ** 24
USDC_UNIT = Decimal(10) ** 6
STORAGE_REGISTRATION = 1_250_000_000_000_000_000_000  # 0.00125 NEAR, NEP-145 minimum


@dataclass(frozen=True, slots=True)
class SwapQuote:
    """``min_usdc_out`` is the guaranteed floor; the cash-out is sized on it, never on the optimistic estimate."""

    amount_in: int  # yoctoNEAR
    usdc_out: int  # estimate, smallest USDC unit
    min_usdc_out: int
    eta_seconds: int | None
    deposit_address: str
    label: str


@dataclass(frozen=True, slots=True)
class SwapResult:
    outcome: SwapOutcome
    usdc_received: int  # the guaranteed minimum when DELIVERED, else 0
    detail: str
    tx_hash: str | None = None


class SwapLeg(Protocol):
    @property
    def label(self) -> str: ...

    def quote(self, amount_in: int, destination: DepositTarget, refund_to: str) -> SwapQuote: ...

    def execute(self, quote: SwapQuote) -> SwapResult: ...


class SandboxSwapLeg:
    """SANDBOX ONLY: converts at a demo ``usd_per_near`` price with a demo fee; no network, no funds."""

    label = "SANDBOX swap (nothing is swapped)"

    def __init__(
        self, *, usd_per_near: Decimal, fee_bps: int = 30, outcome: SwapOutcome = SwapOutcome.DELIVERED
    ) -> None:
        if usd_per_near <= 0 or not 0 <= fee_bps < 10_000:
            raise WayfarerError("The sandbox price and fee are invalid.")
        self._price = usd_per_near
        self._fee_bps = fee_bps
        self._outcome = outcome

    def quote(self, amount_in: int, destination: DepositTarget, refund_to: str) -> SwapQuote:
        if amount_in <= 0:
            raise WayfarerError("The amount must be positive.")
        gross = Decimal(amount_in) / NEAR_UNIT * self._price * USDC_UNIT
        net = int((gross * (10_000 - self._fee_bps) / 10_000).to_integral_value(rounding=ROUND_DOWN))
        return SwapQuote(amount_in, net, net, 1, f"sandbox-{destination.address}", self.label)

    def execute(self, quote: SwapQuote) -> SwapResult:
        if self._outcome is SwapOutcome.DELIVERED:
            return SwapResult(self._outcome, quote.min_usdc_out, "Sandbox swap 'delivered' (no funds moved).")
        return SwapResult(self._outcome, 0, f"Sandbox swap ended as {self._outcome.value}.")


class DepositFunder(Protocol):
    """Sends ``amount`` yoctoNEAR, as wNEAR, to a 1Click deposit address. Returns the transaction hash."""

    def fund(self, deposit_address: str, amount: int) -> str: ...


class NearWrapFunder:
    """Wrap NEAR and send the wNEAR to a deposit address in ONE transaction: register the receiver, ``near_deposit``,
    ``ft_transfer``. Needs ``amount`` + 0.00125 NEAR + gas in the account. Mainnet use only; never run live here."""

    def __init__(self, rpc: NearRpc, key: SigningKey, *, wrap_contract: str = WRAP_NEAR) -> None:
        self._rpc = rpc
        self._key = key
        self._wrap = wrap_contract

    def fund(self, deposit_address: str, amount: int) -> str:
        actions = [
            action_function_call(
                "storage_deposit",
                {"account_id": deposit_address, "registration_only": True},
                10 * 10**12,
                STORAGE_REGISTRATION,
            ),
            action_function_call("near_deposit", {}, 10 * 10**12, amount),
            action_function_call(
                "ft_transfer", {"receiver_id": deposit_address, "amount": str(amount)}, 30 * 10**12, 1
            ),
        ]
        outcome = self._rpc.send_actions(self._key, self._wrap, actions)
        refusal = failure_message(outcome)
        if refusal:
            raise WayfarerError(f"Funding the deposit address failed: {refusal}")
        return str(outcome["transaction"]["hash"])


class IntentsSwapLeg:
    label = "NEAR Intents (1Click), mainnet"

    def __init__(
        self,
        oneclick: OneClick,
        funder: DepositFunder,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        timeout_s: float = SETTLEMENT_TIMEOUT_S,
    ) -> None:
        self._oneclick = oneclick
        self._funder = funder
        self._sleep = sleep
        self._clock = clock
        self._timeout = timeout_s

    def quote(self, amount_in: int, destination: DepositTarget, refund_to: str) -> SwapQuote:
        request = QuoteRequest(
            origin_asset=WRAPPED_NEAR_ASSET,
            destination_asset=destination.asset_id,
            amount=str(amount_in),
            recipient=destination.address,
            refund_to=refund_to,
        )
        quote = self._oneclick.quote(request, dry=False)
        if quote.deposit_address is None:
            raise UpstreamError("1Click returned no deposit address.")
        return SwapQuote(
            amount_in=amount_in,
            usdc_out=int(
                Decimal(quote.amount_out) * USDC_UNIT
            ),  # `amount_out` is formatted (whole USDC); the floor is base units
            min_usdc_out=int(quote.min_amount_out),
            eta_seconds=quote.eta_seconds,
            deposit_address=quote.deposit_address,
            label=self.label,
        )

    def execute(self, quote: SwapQuote) -> SwapResult:
        try:
            tx_hash = self._funder.fund(quote.deposit_address, quote.amount_in)
        except UpstreamError as error:
            # The transaction may or may not have been sent: never report this as "nothing happened".
            return SwapResult(
                SwapOutcome.PENDING, 0, f"Could not confirm the funding transaction ({error}); check your account."
            )
        except WayfarerError as error:
            return SwapResult(SwapOutcome.NOT_SENT, 0, str(error))
        with contextlib.suppress(UpstreamError):  # optional speed-up; status polling still works
            self._oneclick.submit_deposit(tx_hash=tx_hash, deposit_address=quote.deposit_address)
        outcome, last = follow_swap(
            self._oneclick, quote.deposit_address, timeout_s=self._timeout, sleep=self._sleep, clock=self._clock
        )
        received = quote.min_usdc_out if outcome is SwapOutcome.DELIVERED else 0
        return SwapResult(outcome, received, f"1Click status: {last.value if last else 'unknown'}", tx_hash)
