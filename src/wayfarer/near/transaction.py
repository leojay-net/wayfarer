"""Borsh encoding of NEAR transactions and NEP-366 delegate actions, for ed25519 and ML-DSA-65 keys.

Only the shapes Wayfarer needs are encoded: a transaction with one ``FunctionCall`` or one ``Delegate`` action, and a
delegate action holding one ``FunctionCall``. Encodings are checked against nearcore's own crates by
``tools/pqref`` (see ``tests/test_delegate.py``).
"""

from __future__ import annotations

import base64
import hashlib
import json
import struct
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from wayfarer.near.keys import KeyType, SigningKey

_ACTION_FUNCTION_CALL = 2
_ACTION_TRANSFER = 3
_ACTION_ADD_KEY = 5
_ACTION_DELETE_KEY = 6
_ACTION_DELEGATE = 8
_PERMISSION_FUNCTION_CALL = 0
_PERMISSION_FULL_ACCESS = 1
# NEP-461: on-chain message discriminant 2^30 + NEP number, prefixed to the borsh bytes before hashing.
_NEP461_DELEGATE_PREFIX = struct.pack("<I", (1 << 30) + 366)


def _string(value: str) -> bytes:
    raw = value.encode()
    return struct.pack("<I", len(raw)) + raw


def _public_key(key_type: KeyType, public_key: bytes) -> bytes:
    return bytes([key_type]) + public_key


def _function_call_action(method: str, args: Mapping[str, Any], gas: int, deposit: int) -> bytes:
    arg_bytes = json.dumps(args, separators=(",", ":")).encode()
    return (
        bytes([_ACTION_FUNCTION_CALL])
        + _string(method)
        + struct.pack("<I", len(arg_bytes))
        + arg_bytes
        + struct.pack("<Q", gas)
        + deposit.to_bytes(16, "little")
    )


def _transaction(
    *,
    signer_id: str,
    key_type: KeyType,
    public_key: bytes,
    nonce: int,
    receiver_id: str,
    block_hash: bytes,
    action: bytes,
) -> bytes:
    return encode_transaction(
        signer_id=signer_id,
        key_type=key_type,
        public_key=public_key,
        nonce=nonce,
        receiver_id=receiver_id,
        block_hash=block_hash,
        actions=[action],
    )


def encode_transaction(
    *,
    signer_id: str,
    key_type: KeyType,
    public_key: bytes,
    nonce: int,
    receiver_id: str,
    block_hash: bytes,
    actions: list[bytes],
) -> bytes:
    """Serialise an unsigned transaction from already-encoded actions (see the ``action_*`` builders)."""
    return (
        _string(signer_id)
        + _public_key(key_type, public_key)
        + struct.pack("<Q", nonce)
        + _string(receiver_id)
        + block_hash
        + struct.pack("<I", len(actions))
        + b"".join(actions)
    )


def action_function_call(method: str, args: Mapping[str, Any], gas: int, deposit: int = 0) -> bytes:
    return _function_call_action(method, args, gas, deposit)


def action_transfer(deposit: int) -> bytes:
    return bytes([_ACTION_TRANSFER]) + deposit.to_bytes(16, "little")


def action_add_full_access_key(key_type: KeyType, public_key: bytes) -> bytes:
    """``AddKey`` with ``FullAccess`` permission and a zero starting nonce."""
    return (
        bytes([_ACTION_ADD_KEY])
        + _public_key(key_type, public_key)
        + struct.pack("<Q", 0)
        + bytes([_PERMISSION_FULL_ACCESS])
    )


def action_add_function_call_key(
    key_type: KeyType, public_key: bytes, *, receiver_id: str, methods: list[str], allowance: int | None
) -> bytes:
    """``AddKey`` with ``FunctionCall`` permission (``allowance`` in yoctoNEAR, ``None`` for unlimited gas)."""
    allowance_bytes = b"\x00" if allowance is None else b"\x01" + allowance.to_bytes(16, "little")
    return (
        bytes([_ACTION_ADD_KEY])
        + _public_key(key_type, public_key)
        + struct.pack("<Q", 0)
        + bytes([_PERMISSION_FUNCTION_CALL])
        + allowance_bytes
        + _string(receiver_id)
        + struct.pack("<I", len(methods))
        + b"".join(_string(m) for m in methods)
    )


def action_delete_key(key_type: KeyType, public_key: bytes) -> bytes:
    return bytes([_ACTION_DELETE_KEY]) + _public_key(key_type, public_key)


def encode_function_call(
    *,
    signer_id: str,
    public_key: bytes,
    nonce: int,
    receiver_id: str,
    block_hash: bytes,
    method: str,
    args: Mapping[str, Any],
    gas: int,
    deposit: int = 0,
    key_type: KeyType = KeyType.ED25519,
) -> bytes:
    """Serialise an unsigned transaction containing one ``FunctionCall`` action."""
    return _transaction(
        signer_id=signer_id,
        key_type=key_type,
        public_key=public_key,
        nonce=nonce,
        receiver_id=receiver_id,
        block_hash=block_hash,
        action=_function_call_action(method, args, gas, deposit),
    )


def sign_transaction(key: SigningKey, transaction: bytes) -> bytes:
    """Return the borsh-encoded ``SignedTransaction`` (the key signs SHA-256 of the transaction)."""
    signature = key.sign(hashlib.sha256(transaction).digest())
    return transaction + bytes([key.key_type]) + signature


def to_base64(signed: bytes) -> str:
    return base64.b64encode(signed).decode()


@dataclass(frozen=True, slots=True)
class DelegateCall:
    """One function call that ``sender_id`` authorises someone else to submit (NEP-366)."""

    sender_id: str
    receiver_id: str
    method: str
    args: Mapping[str, Any]
    gas: int
    nonce: int
    max_block_height: int
    deposit: int = 0


def encode_delegate_action(call: DelegateCall, key: SigningKey) -> bytes:
    """Borsh ``DelegateAction``: sender, receiver, one function-call action, nonce, expiry height, signer public key."""
    return (
        _string(call.sender_id)
        + _string(call.receiver_id)
        + struct.pack("<I", 1)
        + _function_call_action(call.method, call.args, call.gas, call.deposit)
        + struct.pack("<Q", call.nonce)
        + struct.pack("<Q", call.max_block_height)
        + _public_key(key.key_type, key.public_key)
    )


def delegate_action_hash(delegate_action: bytes) -> bytes:
    """NEP-461 hash that the delegate key signs: ``sha256(borsh(2^30 + 366) || borsh(DelegateAction))``."""
    return hashlib.sha256(_NEP461_DELEGATE_PREFIX + delegate_action).digest()


def sign_delegate_action(call: DelegateCall, key: SigningKey) -> bytes:
    """Borsh ``SignedDelegateAction`` = delegate action followed by the typed signature."""
    encoded = encode_delegate_action(call, key)
    return encoded + bytes([key.key_type]) + key.sign(delegate_action_hash(encoded))


def encode_relay_transaction(
    *,
    relayer_id: str,
    relayer_public_key: bytes,
    relayer_key_type: KeyType,
    nonce: int,
    receiver_id: str,
    block_hash: bytes,
    signed_delegate: bytes,
) -> bytes:
    """Unsigned transaction ``relayer -> receiver`` carrying one ``Delegate`` action (the relayer then signs and pays gas)."""
    return _transaction(
        signer_id=relayer_id,
        key_type=relayer_key_type,
        public_key=relayer_public_key,
        nonce=nonce,
        receiver_id=receiver_id,
        block_hash=block_hash,
        action=bytes([_ACTION_DELEGATE]) + signed_delegate,
    )
