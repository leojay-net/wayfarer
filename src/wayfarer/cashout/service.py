"""One-button merchant cash-out: quote and read back, then swap NEAR to USDC and pay the bank rail.

The merchant sees naira; the swap is an implementation detail. Honest states: if the swap does not deliver, no payout is
made; if the swap delivered but the payout failed, the result says the USDC is held at the destination and needs follow-up.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from wayfarer.cashout.legs import USDC_UNIT, SwapLeg, SwapQuote
from wayfarer.cashout.rails import BankAccount, PayoutResult, PayoutStatus, Rail, RailQuote
from wayfarer.errors import WayfarerError
from wayfarer.swap import SwapOutcome

NEAR_UNIT = Decimal(10) ** 24


class CashOutStatus(StrEnum):
    PAID = "paid"
    PAYOUT_PENDING = "payout_pending"
    SWAP_PENDING = "swap_pending"  # swap not final: do NOT retry blindly, check the swap status first
    SWAP_FAILED = "swap_failed"  # nothing was swapped or the funds were refunded; no payout made
    USDC_HELD = "usdc_held"  # swap delivered, payout failed: needs manual follow-up


@dataclass(frozen=True, slots=True)
class CashOutQuote:
    reference: str
    swap: SwapQuote
    rail: RailQuote
    bank: BankAccount

    def read_back(self) -> str:
        near = Decimal(self.swap.amount_in) / NEAR_UNIT
        usdc_min = Decimal(self.swap.min_usdc_out) / USDC_UNIT
        return (
            f"Cash out {near:.6f} NEAR to {self.bank.account_name} ({self.bank.masked}): "
            f"about NGN {self.rail.ngn_out:,.2f} after a NGN {self.rail.fee_ngn:,.2f} fee, based on at least "
            f"{usdc_min:.2f} USDC at NGN {self.rail.ngn_per_usd:,.2f}/USD. This is an estimate. "
            f"[{self.swap.label}; {self.rail.label}] Nothing happens until you confirm."
        )


@dataclass(frozen=True, slots=True)
class CashOutResult:
    status: CashOutStatus
    message: str
    reference: str
    payout: PayoutResult | None = None
    swap_detail: str = ""


class CashOutService:
    def __init__(self, swap: SwapLeg, rail: Rail) -> None:
        self._swap = swap
        self._rail = rail

    def quote(self, *, reference: str, amount_in: int, bank: BankAccount, refund_to: str) -> CashOutQuote:
        """Quote the whole path. ``reference`` is the idempotency key for the payout (e.g. derived from a sale)."""
        if not reference or len(reference) > 64:
            raise WayfarerError("The cash-out reference must be 1-64 characters.")
        swap = self._swap.quote(amount_in, self._rail.usdc_target(reference), refund_to)
        return CashOutQuote(reference, swap, self._rail.quote(swap.min_usdc_out), bank)

    def execute(self, quote: CashOutQuote) -> CashOutResult:
        """Run the swap, then the payout sized on the swap's guaranteed minimum. Call only after the user confirmed."""
        swap = self._swap.execute(quote.swap)
        if swap.outcome is SwapOutcome.PENDING:
            return CashOutResult(
                CashOutStatus.SWAP_PENDING,
                "The swap has not finished. Check its status before trying again; no bank payout was made.",
                quote.reference,
                swap_detail=swap.detail,
            )
        if swap.outcome is not SwapOutcome.DELIVERED:
            return CashOutResult(
                CashOutStatus.SWAP_FAILED,
                f"The swap did not complete ({swap.outcome.value}); no bank payout was made.",
                quote.reference,
                swap_detail=swap.detail,
            )
        payout = self._rail.payout(quote.reference, swap.usdc_received, quote.bank)
        if payout.status is PayoutStatus.PAID:
            return CashOutResult(
                CashOutStatus.PAID,
                f"NGN {payout.ngn_amount:,.2f} paid. [{payout.rail_label}]",
                quote.reference,
                payout,
                swap.detail,
            )
        if payout.status is PayoutStatus.PENDING:
            return CashOutResult(
                CashOutStatus.PAYOUT_PENDING, "The bank payout is processing.", quote.reference, payout, swap.detail
            )
        return CashOutResult(
            CashOutStatus.USDC_HELD,
            "The swap delivered USDC but the bank payout failed. The USDC is held at the destination; contact support with "
            f"reference {quote.reference}.",
            quote.reference,
            payout,
            swap.detail,
        )
