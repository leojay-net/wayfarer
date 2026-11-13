"""Rate lock: the merchant fixes a local-currency price in tokens for a short window.

``FixedRateSource`` is a DEMO source (a rate you type in); no live FX feed is wired. The lock only fixes the amount for
the cheque window: if the market moves afterwards the merchant carries that risk, which is why tap amounts stay small.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal, localcontext
from typing import Protocol

from wayfarer.errors import TapError

TOKEN_DECIMALS: Mapping[str, int] = {"near": 24}
DEFAULT_LOCK_MS = 60_000


class RateSource(Protocol):
    def fiat_per_token(self, fiat_currency: str, token_id: str) -> Decimal:
        """Fiat units (major, e.g. naira) for one whole token."""
        ...


class FixedRateSource:
    """A rate table supplied by the caller. Labelled demo data, not a market feed."""

    def __init__(self, rates: Mapping[tuple[str, str], Decimal]) -> None:
        self._rates = dict(rates)

    def fiat_per_token(self, fiat_currency: str, token_id: str) -> Decimal:
        try:
            return self._rates[(fiat_currency, token_id)]
        except KeyError:
            raise TapError(f"No rate for {token_id} in {fiat_currency}.") from None


@dataclass(frozen=True, slots=True)
class RateQuote:
    quote_id: str
    fiat_currency: str
    token_id: str
    fiat_per_token: Decimal
    created_ms: int
    expires_ms: int


class RateDesk:
    def __init__(
        self,
        source: RateSource,
        clock_ms: Callable[[], int],
        *,
        lock_ms: int = DEFAULT_LOCK_MS,
        new_id: Callable[[], str] = lambda: secrets.token_hex(8),
    ) -> None:
        self._source = source
        self._clock = clock_ms
        self._lock_ms = lock_ms
        self._new_id = new_id

    def lock(self, fiat_currency: str, token_id: str) -> RateQuote:
        if token_id not in TOKEN_DECIMALS:
            raise TapError(f"{token_id} is not supported for tap payments.")
        price = self._source.fiat_per_token(fiat_currency, token_id)
        if price <= 0:
            raise TapError("The rate must be positive.")
        now = self._clock()
        return RateQuote(self._new_id(), fiat_currency, token_id, price, now, now + self._lock_ms)


def asset_amount(quote: RateQuote, fiat_minor: int, fiat_exponent: int) -> int:
    """Token amount (smallest unit) for a local-currency amount, rounded UP so the merchant never receives less."""
    with localcontext() as ctx:
        ctx.prec = 80
        fiat = Decimal(fiat_minor).scaleb(-fiat_exponent)
        tokens = fiat / quote.fiat_per_token
        units = (tokens * Decimal(10) ** TOKEN_DECIMALS[quote.token_id]).to_integral_value(rounding=ROUND_CEILING)
    return int(units)
