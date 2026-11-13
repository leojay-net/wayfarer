"""Mandate Passport: the vault's permission for one agent key, in a form a bank, processor or auditor can read.

Shape is INSPIRED by traditional mandates (AP2 intent mandates, UK commercial VRP: maximum per payment, frequency, end
date, payee scope, revocation). It is NOT AP2 or cVRP compliant and no payment network or bank has reviewed it.

The passport is signed by the vault owner's ML-DSA-65 key and its hash goes into every approval receipt. The contract,
not the passport, is what enforces the limits: a verifier can compare the passport with the live ``get_mandate`` view and
with the owner's on-chain key list.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from wayfarer.crypto import mldsa
from wayfarer.errors import WayfarerError
from wayfarer.near.keys import MlDsaKeyPair, ml_dsa_handle_str
from wayfarer.pqvault import MandateTerms
from wayfarer.receipts import Check

PASSPORT_TYPE = "wayfarer.mandate-passport/v1"
INSPIRED_BY = "Inspired by AP2 intent mandates and UK commercial VRP parameters; not compliant with either."
_DOMAIN = b"wayfarer-mandate-passport-v1\x00"
_UNITS = {"near": "yoctoNEAR"}


def canonical_json(value: Any) -> bytes:
    """Deterministic JSON bytes: sorted keys, no whitespace, ASCII only."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


@dataclass(frozen=True, slots=True)
class MandatePassport:
    body: dict[str, Any]
    owner_public_key: bytes
    signature: bytes

    def signed_bytes(self) -> bytes:
        return _DOMAIN + canonical_json(self.body)

    def hash_hex(self) -> str:
        """SHA-256 of the signed bytes: the value receipts carry."""
        return hashlib.sha256(self.signed_bytes()).hexdigest()

    def to_json(self) -> dict[str, Any]:
        return {
            "body": self.body,
            "owner_public_key": base64.b64encode(self.owner_public_key).decode(),
            "signature": base64.b64encode(self.signature).decode(),
            "hash": self.hash_hex(),
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> MandatePassport:
        try:
            return cls(
                dict(data["body"]), base64.b64decode(data["owner_public_key"]), base64.b64decode(data["signature"])
            )
        except (KeyError, ValueError, TypeError) as error:
            raise WayfarerError("Malformed mandate passport.") from error


def issue_passport(
    *, network: str, terms: MandateTerms, owner_key: MlDsaKeyPair, owner_account: str, issued_at_ms: int
) -> MandatePassport:
    """Describe ``terms`` (read from the vault's ``get_mandate``) and sign the description with the owner's key."""
    if terms.owner != owner_account:
        raise WayfarerError("The mandate's owner is not the signing account.")
    body: dict[str, Any] = {
        "type": PASSPORT_TYPE,
        "inspired_by": INSPIRED_BY,
        "network": network,
        "vault": terms.vault,
        "owner": owner_account,
        "agent": {"algorithm": "ml-dsa-65", "key_id": terms.key_id},
        "limits": {
            "token": terms.token_id,
            "unit": _UNITS.get(terms.token_id, "smallest unit"),
            "per_payment_max": str(terms.per_tx_max),
            "daily_max": str(terms.daily_max),
            "min_interval_ms": str(terms.min_interval_ms),
            "valid_until_ms": str(terms.expires_at_ms),
        },
        "payees": {"allowlist_enabled": terms.allowlist_enabled, "accounts": sorted(terms.allowlist)},
        "revocation": {"method": "revoke_pq_agent", "by": "owner", "effect": "immediate, on-chain"},
        "issued_at_ms": issued_at_ms,
    }
    passport = MandatePassport(body, owner_key.public_key, b"")
    return MandatePassport(body, owner_key.public_key, owner_key.sign(passport.signed_bytes()))


def owner_key_listed(passport: MandatePassport, owner_chain_keys: Sequence[str]) -> Check:
    """Is the key that signed the passport one of the owner account's on-chain keys (listed as a handle)?"""
    handle = ml_dsa_handle_str(passport.owner_public_key)
    return Check("owner key is on the owner account on-chain", handle in owner_chain_keys, handle)


def verify_passport(
    passport: MandatePassport,
    *,
    live: MandateTerms | None = None,
    owner_chain_keys: Sequence[str] | None = None,
) -> list[Check]:
    """Offline: signature and shape. With ``live`` / ``owner_chain_keys`` (from the chain): also consistency with them."""
    body = passport.body
    checks = [
        Check("passport type", body.get("type") == PASSPORT_TYPE),
        Check(
            "owner signature (ML-DSA-65) valid",
            mldsa.verify(passport.owner_public_key, passport.signature, passport.signed_bytes()),
        ),
    ]
    if owner_chain_keys is not None:
        checks.append(owner_key_listed(passport, owner_chain_keys))
    if live is not None:
        limits = body.get("limits", {})
        same = (
            body.get("vault") == live.vault
            and body.get("owner") == live.owner
            and body.get("agent", {}).get("key_id") == live.key_id
            and limits.get("per_payment_max") == str(live.per_tx_max)
            and limits.get("daily_max") == str(live.daily_max)
            and limits.get("min_interval_ms") == str(live.min_interval_ms)
            and limits.get("valid_until_ms") == str(live.expires_at_ms)
            and body.get("payees", {}).get("allowlist_enabled") == live.allowlist_enabled
            and body.get("payees", {}).get("accounts") == sorted(live.allowlist)
        )
        checks.append(Check("matches the vault's live mandate", same))
    return checks
