"""The cheque: a payment authorisation signed by an ML-DSA-65 agent key and settled by ``pay_with_cheque``.

The signed message is ``b"wayfarer-cheque-v1"`` followed by the borsh encoding of the fields below (u32
length-prefixed UTF-8 strings, little-endian integers). The contract rebuilds the same bytes from the call
arguments and verifies the signature with the ``ml_dsa_verify`` host function, so nothing the contract acts on
can differ from what the agent signed. The layout is mirrored by ``ChequeBody`` in
``contracts/wayfarer-vault/src/lib.rs`` and checked by ``tests/test_cheque.py`` and the contract's tests.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass

from wayfarer.crypto import mldsa
from wayfarer.errors import WayfarerError
from wayfarer.near.keys import MlDsaKeyPair

CHEQUE_DOMAIN = b"wayfarer-cheque-v1"
_MAX_U64 = (1 << 64) - 1
_MAX_U128 = (1 << 128) - 1


def _string(value: str) -> bytes:
    raw = value.encode()
    return struct.pack("<I", len(raw)) + raw


@dataclass(frozen=True, slots=True)
class Cheque:
    """What the buyer's phone authorises. ``amount`` is in the token's smallest unit (yoctoNEAR for ``near``)."""

    vault: str
    token_id: str
    to: str
    amount: int
    intent_id: str
    expires_at_ms: int
    receipt_hash: str

    def __post_init__(self) -> None:
        if not 0 < self.amount <= _MAX_U128:
            raise WayfarerError("Cheque amount must be positive and fit in 128 bits.")
        if not 0 < self.expires_at_ms <= _MAX_U64:
            raise WayfarerError("Cheque expiry must be a positive unix-millisecond time.")
        if not 1 <= len(self.intent_id) <= 64:
            raise WayfarerError("Cheque intent id must be 1-64 characters.")
        if len(self.receipt_hash) > 128:
            raise WayfarerError("Cheque receipt hash is too long.")

    def message(self) -> bytes:
        """The exact bytes that are signed."""
        return (
            CHEQUE_DOMAIN
            + _string(self.vault)
            + _string(self.token_id)
            + _string(self.to)
            + self.amount.to_bytes(16, "little")
            + _string(self.intent_id)
            + struct.pack("<Q", self.expires_at_ms)
            + _string(self.receipt_hash)
        )

    def digest(self) -> str:
        """SHA-256 of the message, hex: a short stable handle for logs and receipts."""
        return hashlib.sha256(self.message()).hexdigest()


def key_id(public_key: bytes) -> str:
    """The contract's id for an agent key: hex SHA-256 of the 1,952-byte public key."""
    if len(public_key) != mldsa.PUBLIC_KEY_SIZE:
        raise WayfarerError("Not an ML-DSA-65 public key.")
    return hashlib.sha256(public_key).hexdigest()


def sign_cheque(key: MlDsaKeyPair, cheque: Cheque) -> bytes:
    """Sign a cheque; the result is the 3,309-byte ``signature`` argument of ``pay_with_cheque``."""
    return key.sign(cheque.message())


def verify_cheque(public_key: bytes, signature: bytes, cheque: Cheque) -> bool:
    """Check a cheque signature locally (what the merchant's phone does before showing 'accepted, pending settlement')."""
    return mldsa.verify(public_key, signature, cheque.message())
