"""NEAR account keys: ed25519 and ML-DSA-65 (NEP-645). Both satisfy :class:`SigningKey`."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Protocol

from wayfarer.crypto import ed25519, mldsa
from wayfarer.crypto.base58 import b58decode, b58encode
from wayfarer.errors import WayfarerError

_PREFIX = "ed25519:"
_MLDSA_PREFIX = "ml-dsa-65:"
_MLDSA_SEED_PREFIX = "ml-dsa-65-seed:"
_MLDSA_HANDLE_PREFIX = "ml-dsa-65-hash:"
_MLDSA_HANDLE_DOMAIN = b"near:ml-dsa-65-pubkey-hash:v1"


class KeyType(IntEnum):
    """Borsh tag of ``PublicKey`` / ``Signature`` variants."""

    ED25519 = 0
    MLDSA65 = 2


class SigningKey(Protocol):
    """What the transaction and delegate-action encoders need from an account key."""

    @property
    def account_id(self) -> str: ...

    @property
    def key_type(self) -> KeyType: ...

    @property
    def public_key(self) -> bytes: ...

    @property
    def public_key_str(self) -> str: ...

    def sign(self, message: bytes) -> bytes: ...


@dataclass(frozen=True, slots=True)
class KeyPair:
    """An account's signing key. ``seed`` is excluded from ``repr``."""

    account_id: str
    seed: bytes = field(repr=False)

    @classmethod
    def from_secret(cls, account_id: str, secret: str) -> KeyPair:
        """Parse NEAR's ``ed25519:<base58>`` secret (32-byte seed, optionally followed by the 32-byte public key)."""
        if not secret.startswith(_PREFIX):
            raise WayfarerError("Unsupported key type; only ed25519 keys are supported.")
        raw = b58decode(secret[len(_PREFIX) :])
        if len(raw) not in (32, 64):
            raise WayfarerError("Malformed secret key.")
        pair = cls(account_id, raw[:32])
        if len(raw) == 64 and raw[32:] != pair.public_key:
            raise WayfarerError("Secret key does not match its public half.")
        return pair

    @classmethod
    def from_file(cls, path: Path) -> KeyPair:
        data = json.loads(path.expanduser().read_text())
        return cls.from_secret(data["account_id"], data["private_key"])

    @property
    def key_type(self) -> KeyType:
        return KeyType.ED25519

    @property
    def public_key(self) -> bytes:
        return ed25519.public_key_from_seed(self.seed)

    @property
    def public_key_str(self) -> str:
        return _PREFIX + b58encode(self.public_key)

    def sign(self, message: bytes) -> bytes:
        return ed25519.sign(self.seed, message)


def ml_dsa_handle(public_key: bytes) -> bytes:
    """The 32-byte SHA3-256 handle NEAR stores in the trie instead of the 1,952-byte key (NEP-645)."""
    return hashlib.sha3_256(_MLDSA_HANDLE_DOMAIN + public_key).digest()


def ml_dsa_handle_str(public_key: bytes) -> str:
    """How the chain lists an ML-DSA key: ``ml-dsa-65-hash:<base58 handle>``."""
    return _MLDSA_HANDLE_PREFIX + b58encode(ml_dsa_handle(public_key))


@dataclass(frozen=True, slots=True)
class MlDsaKeyPair:
    """An ML-DSA-65 key, stored as its 32-byte FIPS 204 seed (``seed`` is excluded from ``repr``).

    The key file holds ``"private_key": "ml-dsa-65-seed:<base58 seed>"``. That label is Wayfarer's own: NEAR tooling
    does not read it. The reference signer is not constant-time; use it for testnet keys.
    """

    account_id: str
    seed: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if len(self.seed) != mldsa.SEED_SIZE:
            raise WayfarerError("Malformed ML-DSA seed.")

    @classmethod
    def generate(cls, account_id: str) -> MlDsaKeyPair:
        return cls(account_id, os.urandom(mldsa.SEED_SIZE))

    @classmethod
    def from_secret(cls, account_id: str, secret: str) -> MlDsaKeyPair:
        if not secret.startswith(_MLDSA_SEED_PREFIX):
            raise WayfarerError("Not an ML-DSA seed; expected 'ml-dsa-65-seed:<base58>'.")
        return cls(account_id, b58decode(secret[len(_MLDSA_SEED_PREFIX) :]))

    @classmethod
    def from_file(cls, path: Path) -> MlDsaKeyPair:
        data = json.loads(path.expanduser().read_text())
        pair = cls.from_secret(data["account_id"], data["private_key"])
        if data.get("public_key") not in (None, pair.public_key_str):
            raise WayfarerError("Key file public key does not match its seed.")
        return pair

    def secret_str(self) -> str:
        return _MLDSA_SEED_PREFIX + b58encode(self.seed)

    @property
    def key_type(self) -> KeyType:
        return KeyType.MLDSA65

    @property
    def public_key(self) -> bytes:
        return mldsa.public_key_from_seed(self.seed)

    @property
    def public_key_str(self) -> str:
        return _MLDSA_PREFIX + b58encode(self.public_key)

    @property
    def public_key_handle(self) -> bytes:
        return ml_dsa_handle(self.public_key)

    @property
    def public_key_handle_str(self) -> str:
        return ml_dsa_handle_str(self.public_key)

    def sign(self, message: bytes) -> bytes:
        return mldsa.sign(self.seed, message)
