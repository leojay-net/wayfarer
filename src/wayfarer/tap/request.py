"""The merchant's PaymentRequest: what the buyer's phone is asked to pay, bound by a hash the cheque commits to."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from decimal import Decimal

from wayfarer.errors import TapError
from wayfarer.intent import is_valid_near_account
from wayfarer.tap.codec import Reader, Writer

REQUEST_DOMAIN = b"wayfarer-request-v1"
NONCE_BYTES = 16
_CURRENCY = re.compile(r"[A-Z]{3}", re.ASCII)
_U64 = (1 << 64) - 1
_U128 = (1 << 128) - 1


@dataclass(frozen=True, slots=True)
class PaymentRequest:
    """``fiat_minor`` is in the currency's minor unit (US cents with ``fiat_exponent`` 2); ``asset_amount`` in the token's
    smallest unit. ``rate`` is the locked price as text (fiat units per whole token) for display and audit only: the
    payment is exactly ``asset_amount``."""

    merchant_account: str
    merchant_name: str
    fiat_currency: str
    fiat_minor: int
    fiat_exponent: int
    token_id: str
    asset_amount: int
    quote_id: str
    rate: str
    quote_expires_ms: int
    settle_by_ms: int
    nonce: bytes
    created_ms: int

    def __post_init__(self) -> None:
        if not is_valid_near_account(self.merchant_account):
            raise TapError("The merchant account is not a valid NEAR account.")
        if not 1 <= len(self.merchant_name) <= 64 or not self.merchant_name.isprintable():
            raise TapError("The merchant name must be 1-64 printable characters.")
        if not _CURRENCY.fullmatch(self.fiat_currency):
            raise TapError("The currency must be a 3-letter ISO code.")
        if not 0 < self.fiat_minor <= _U64 or not 0 <= self.fiat_exponent <= 6:
            raise TapError("The local amount is out of range.")
        if not 0 < self.asset_amount <= _U128:
            raise TapError("The asset amount is out of range.")
        if not self.token_id or len(self.token_id) > 64:
            raise TapError("The token id is invalid.")
        if len(self.nonce) != NONCE_BYTES:
            raise TapError(f"The nonce must be {NONCE_BYTES} bytes.")
        if not 0 < self.created_ms < self.quote_expires_ms <= self.settle_by_ms <= _U64:
            raise TapError("The request times are inconsistent.")
        try:
            if Decimal(self.rate) <= 0:
                raise TapError("The rate must be positive.")
        except ArithmeticError as error:
            raise TapError("The rate is not a number.") from error

    def message(self) -> bytes:
        return (
            Writer()
            .raw(REQUEST_DOMAIN)
            .string(self.merchant_account)
            .string(self.merchant_name)
            .string(self.fiat_currency)
            .u64(self.fiat_minor)
            .u8(self.fiat_exponent)
            .string(self.token_id)
            .u128(self.asset_amount)
            .string(self.quote_id)
            .string(self.rate)
            .u64(self.quote_expires_ms)
            .u64(self.settle_by_ms)
            .raw(self.nonce)
            .u64(self.created_ms)
            .getvalue()
        )

    @classmethod
    def decode(cls, data: bytes) -> PaymentRequest:
        reader = Reader(data)
        if reader.raw(len(REQUEST_DOMAIN)) != REQUEST_DOMAIN:
            raise TapError("This is not a Wayfarer payment request.")
        request = cls(
            merchant_account=reader.string(),
            merchant_name=reader.string(),
            fiat_currency=reader.string(),
            fiat_minor=reader.u64(),
            fiat_exponent=reader.u8(),
            token_id=reader.string(),
            asset_amount=reader.u128(),
            quote_id=reader.string(),
            rate=reader.string(),
            quote_expires_ms=reader.u64(),
            settle_by_ms=reader.u64(),
            nonce=reader.raw(NONCE_BYTES),
            created_ms=reader.u64(),
        )
        reader.finish()
        return request

    def request_hash(self) -> bytes:
        return hashlib.sha256(self.message()).digest()

    def intent_id(self) -> str:
        """The one-time id the vault records; derived from the nonce so one request can settle at most once."""
        return "tap-" + self.nonce.hex()

    def receipt_hash(self) -> str:
        """What the cheque commits to on-chain: ties the payment to this exact request."""
        return "wft:" + self.request_hash().hex()

    def fiat_display(self) -> str:
        value = Decimal(self.fiat_minor).scaleb(-self.fiat_exponent)
        return f"{self.fiat_currency} {value:,.{self.fiat_exponent}f}"
