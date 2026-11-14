"""Bank rails: where the USDC from the swap becomes a bank credit.

* :class:`SandboxRail` is a SANDBOX: it validates the bank details, applies a configured demo FX rate and fee, and writes
  a local ledger file. No bank, no money, no network. It exists so anyone can run the whole cash-out flow.
* A real bank payout needs a licensed partner (Wayfarer does not custody or convert fiat). A partner rail would
  implement the same :class:`Rail` protocol; none is shipped in this repository.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from wayfarer.errors import WayfarerError

_BANK_CODE = re.compile(r"[0-9]{3,6}", re.ASCII)
_ACCOUNT_NUMBER = re.compile(r"[0-9]{6,17}", re.ASCII)
_USDC_UNIT = Decimal(10) ** 6


class RailError(WayfarerError):
    """The rail refused or could not process a request."""


@dataclass(frozen=True, slots=True)
class BankAccount:
    """A bank account (6-17 digits). The sandbox does not verify the name or any check digit."""

    bank_code: str
    account_number: str
    account_name: str

    def __post_init__(self) -> None:
        if not _BANK_CODE.fullmatch(self.bank_code):
            raise RailError("The bank code must be 3-6 digits.")
        if not _ACCOUNT_NUMBER.fullmatch(self.account_number):
            raise RailError("The account number must be 6-17 digits.")
        if not 1 <= len(self.account_name.strip()) <= 80:
            raise RailError("The account name must be 1-80 characters.")

    @property
    def masked(self) -> str:
        return f"{self.bank_code} ****{self.account_number[-4:]}"


@dataclass(frozen=True, slots=True)
class DepositTarget:
    """Where the swap must deliver USDC for this payout: a 1Click destination asset id and address."""

    asset_id: str
    address: str


@dataclass(frozen=True, slots=True)
class RailQuote:
    usdc_in: int  # smallest unit (6 decimals)
    fiat_out: Decimal
    fee_fiat: Decimal
    fiat_per_usd: Decimal
    label: str
    currency: str = "USD"


class PayoutStatus(StrEnum):
    PAID = "paid"
    PENDING = "pending"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class PayoutResult:
    status: PayoutStatus
    reference: str
    fiat_amount: Decimal
    message: str
    rail_label: str
    currency: str = "USD"


class Rail(Protocol):
    @property
    def label(self) -> str: ...

    def usdc_target(self, reference: str) -> DepositTarget: ...

    def quote(self, usdc_in: int) -> RailQuote: ...

    def payout(self, reference: str, usdc_in: int, bank: BankAccount) -> PayoutResult:
        """Pay ``bank``. Must be idempotent per ``reference``: repeating a call returns the first result."""
        ...


class SandboxRail:
    """SANDBOX ONLY. Demo FX rate and fee are supplied by the caller; the 'bank' is a JSON ledger on disk."""

    label = "SANDBOX rail (no money moves)"

    def __init__(
        self,
        ledger: Path,
        *,
        fiat_per_usd: Decimal,
        currency: str = "USD",
        fee_bps: int = 100,
        clock_iso: Callable[[], str] = lambda: "",
    ) -> None:
        if fiat_per_usd <= 0 or not 0 <= fee_bps < 10_000:
            raise RailError("The sandbox FX rate and fee are invalid.")
        self._ledger = ledger
        self._rate = fiat_per_usd
        self._currency = currency
        self._fee_bps = fee_bps
        self._clock = clock_iso

    def usdc_target(self, reference: str) -> DepositTarget:
        return DepositTarget("nep141:sandbox-usdc.invalid", f"sandbox-deposit-{reference}")

    def quote(self, usdc_in: int) -> RailQuote:
        if usdc_in <= 0:
            raise RailError("The USDC amount must be positive.")
        gross = (Decimal(usdc_in) / _USDC_UNIT * self._rate).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        fee = (gross * self._fee_bps / Decimal(10_000)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        return RailQuote(usdc_in, gross - fee, fee, self._rate, self.label, self._currency)

    def _read(self) -> dict[str, dict[str, str]]:
        if not self._ledger.exists():
            return {}
        data = json.loads(self._ledger.read_text())
        return dict(data) if isinstance(data, dict) else {}

    def payout(self, reference: str, usdc_in: int, bank: BankAccount) -> PayoutResult:
        entries = self._read()
        known = entries.get(reference)
        if known is not None:
            return PayoutResult(
                PayoutStatus.PAID, reference, Decimal(known["fiat"]), "Already paid (sandbox ledger).", self.label, self._currency
            )
        quote = self.quote(usdc_in)
        entries[reference] = {
            "fiat": str(quote.fiat_out),
            "currency": self._currency,
            "usdc_in": str(usdc_in),
            "bank": bank.masked,
            "name": bank.account_name,
            "at": self._clock(),
            "note": "SANDBOX ledger entry: no real money moved",
        }
        self._ledger.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._ledger.with_suffix(".tmp")
        tmp.write_text(json.dumps(entries, indent=1))
        os.replace(tmp, self._ledger)
        return PayoutResult(
            PayoutStatus.PAID, reference, quote.fiat_out, "Credited in the sandbox ledger only.", self.label, self._currency
        )
