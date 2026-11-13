"""The buyer's reply: a signed cheque in a compact envelope.

A cheque is fully determined by the merchant's request plus the buyer's vault, so the envelope carries only the vault,
the agent key id, the signature and (by default) the 1,952-byte public key the merchant needs to check the signature
offline. The merchant rebuilds the cheque from its own request: any difference makes the signature fail.
"""

from __future__ import annotations

from dataclasses import dataclass

from wayfarer.cheque import Cheque
from wayfarer.crypto import mldsa
from wayfarer.errors import TapError
from wayfarer.intent import is_valid_near_account
from wayfarer.tap.codec import Reader, Writer
from wayfarer.tap.request import PaymentRequest

ENVELOPE_MAGIC = b"WFC1"


def cheque_for_request(request: PaymentRequest, vault: str) -> Cheque:
    """The exact cheque a buyer signs for ``request``: pays the merchant, expires at ``settle_by_ms``."""
    return Cheque(
        vault=vault,
        token_id=request.token_id,
        to=request.merchant_account,
        amount=request.asset_amount,
        intent_id=request.intent_id(),
        expires_at_ms=request.settle_by_ms,
        receipt_hash=request.receipt_hash(),
    )


@dataclass(frozen=True, slots=True)
class ChequeEnvelope:
    request_hash: bytes
    vault: str
    key_id: bytes
    signature: bytes
    public_key: bytes | None = None

    def __post_init__(self) -> None:
        if len(self.request_hash) != 32 or len(self.key_id) != 32:
            raise TapError("The envelope hashes must be 32 bytes.")
        if len(self.signature) != mldsa.SIGNATURE_SIZE:
            raise TapError("The signature has the wrong size.")
        if self.public_key is not None and len(self.public_key) != mldsa.PUBLIC_KEY_SIZE:
            raise TapError("The public key has the wrong size.")
        if not is_valid_near_account(self.vault):
            raise TapError("The vault is not a valid NEAR account.")

    def encode(self) -> bytes:
        writer = Writer().raw(ENVELOPE_MAGIC).raw(self.request_hash).string(self.vault).raw(self.key_id)
        writer.u8(0 if self.public_key is None else 1)
        if self.public_key is not None:
            writer.raw(self.public_key)
        return writer.raw(self.signature).getvalue()

    @classmethod
    def decode(cls, data: bytes) -> ChequeEnvelope:
        reader = Reader(data)
        if reader.raw(len(ENVELOPE_MAGIC)) != ENVELOPE_MAGIC:
            raise TapError("This is not a Wayfarer cheque.")
        request_hash = reader.raw(32)
        vault = reader.string()
        key_id = reader.raw(32)
        flag = reader.u8()
        if flag not in (0, 1):
            raise TapError("The envelope flags are invalid.")
        public_key = reader.raw(mldsa.PUBLIC_KEY_SIZE) if flag else None
        signature = reader.raw(mldsa.SIGNATURE_SIZE)
        reader.finish()
        return cls(request_hash, vault, key_id, signature, public_key)
