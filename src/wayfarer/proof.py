"""Approval receipt v2: proof that a human-approved, mandate-bounded payment happened.

One JSON document links, with checks anyone can repeat from public data:

1. the merchant's request (what was asked, in local currency and tokens) and its hash;
2. the buyer's ML-DSA-65 signature over the cheque derived from that request (the approval);
3. the signed Mandate Passport that the agent key operated under;
4. the on-chain ``payment_authorized`` / ``payment_settled`` events (settlement).

What it proves: the holder of the agent key signed exactly this request, the owner signed this mandate, and the vault's
contract emitted these events. What it does NOT prove: that a particular human pressed the button (the key lives on a
phone behind a biometric the chain cannot see), or that goods were delivered. Sharing a receipt reveals the merchant and
the local amount; only their hash is on-chain. Prototype, testnet, unaudited.
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from wayfarer.cheque import Cheque, verify_cheque
from wayfarer.errors import WayfarerError
from wayfarer.mandate import MandatePassport, owner_key_listed, verify_passport
from wayfarer.near.events import extract_events, failure_message
from wayfarer.receipts import Check
from wayfarer.tap.envelope import cheque_for_request
from wayfarer.tap.merchant import AcceptedCheque
from wayfarer.tap.request import PaymentRequest

RECEIPT_TYPE = "wayfarer.approval-receipt/v2"
DISCLAIMER = (
    "Prototype receipt. Proves the signed request, the signed mandate and the on-chain events agree; "
    "does not prove who pressed the button or that goods were delivered."
)


@dataclass(frozen=True, slots=True)
class Settlement:
    tx_hash: str
    relayer: str  # the account that submitted the cheque and signed the transaction
    explorer_url: str


def request_to_json(request: PaymentRequest) -> dict[str, Any]:
    return {
        "merchant_account": request.merchant_account,
        "merchant_name": request.merchant_name,
        "fiat_currency": request.fiat_currency,
        "fiat_minor": request.fiat_minor,
        "fiat_exponent": request.fiat_exponent,
        "token_id": request.token_id,
        "asset_amount": str(request.asset_amount),
        "quote_id": request.quote_id,
        "rate": request.rate,
        "quote_expires_ms": request.quote_expires_ms,
        "settle_by_ms": request.settle_by_ms,
        "nonce": request.nonce.hex(),
        "created_ms": request.created_ms,
    }


def request_from_json(data: Mapping[str, Any]) -> PaymentRequest:
    try:
        return PaymentRequest(
            merchant_account=str(data["merchant_account"]),
            merchant_name=str(data["merchant_name"]),
            fiat_currency=str(data["fiat_currency"]),
            fiat_minor=int(data["fiat_minor"]),
            fiat_exponent=int(data["fiat_exponent"]),
            token_id=str(data["token_id"]),
            asset_amount=int(data["asset_amount"]),
            quote_id=str(data["quote_id"]),
            rate=str(data["rate"]),
            quote_expires_ms=int(data["quote_expires_ms"]),
            settle_by_ms=int(data["settle_by_ms"]),
            nonce=bytes.fromhex(str(data["nonce"])),
            created_ms=int(data["created_ms"]),
        )
    except (KeyError, ValueError, TypeError) as error:
        raise WayfarerError("Malformed receipt request.") from error


def cheque_to_json(cheque: Cheque) -> dict[str, Any]:
    return {
        "vault": cheque.vault,
        "token_id": cheque.token_id,
        "to": cheque.to,
        "amount": str(cheque.amount),
        "intent_id": cheque.intent_id,
        "expires_at_ms": cheque.expires_at_ms,
        "receipt_hash": cheque.receipt_hash,
    }


def build_receipt(
    *,
    network: str,
    accepted: AcceptedCheque,
    passport: MandatePassport,
    settlement: Settlement,
    cashout: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "type": RECEIPT_TYPE,
        "network": network,
        "disclaimer": DISCLAIMER,
        "request": request_to_json(accepted.request),
        "request_hash": accepted.request.request_hash().hex(),
        "cheque": cheque_to_json(accepted.cheque),
        "approval": {
            "algorithm": "ml-dsa-65",
            "key_id": accepted.key_id,
            "public_key": base64.b64encode(accepted.public_key).decode(),
            "signature": base64.b64encode(accepted.signature).decode(),
        },
        "passport": passport.to_json(),
        "settlement": {
            "tx_hash": settlement.tx_hash,
            "relayer": settlement.relayer,
            "explorer_url": settlement.explorer_url,
        },
        "cashout": dict(cashout) if cashout else None,
    }


def verify_receipt_offline(receipt: Mapping[str, Any]) -> list[Check]:
    """Everything that can be checked without the network. Never raises on a bad receipt: a failed check is reported."""
    try:
        request = request_from_json(receipt["request"])
        approval = receipt["approval"]
        public_key = base64.b64decode(approval["public_key"])
        signature = base64.b64decode(approval["signature"])
        key_id = str(approval["key_id"])
        passport = MandatePassport.from_json(receipt["passport"])
        claimed_cheque = receipt["cheque"]
        vault = str(claimed_cheque["vault"])
    except (KeyError, ValueError, TypeError, WayfarerError) as error:
        return [Check("receipt is well formed", False, str(error))]
    expected = cheque_for_request(request, vault)
    checks = [
        Check("receipt is well formed", receipt.get("type") == RECEIPT_TYPE),
        Check("request hash recomputes", receipt.get("request_hash") == request.request_hash().hex()),
        Check("cheque is exactly what the request asks for", claimed_cheque == cheque_to_json(expected)),
        Check("agent key id is the hash of the public key", hashlib.sha256(public_key).hexdigest() == key_id),
        Check("buyer's ML-DSA-65 signature covers the cheque", verify_cheque(public_key, signature, expected)),
    ]
    checks += verify_passport(passport)
    body = passport.body
    limits = body.get("limits", {})
    checks += [
        Check("passport hash matches the receipt", receipt["passport"].get("hash") == passport.hash_hex()),
        Check(
            "passport is for this vault and key",
            body.get("vault") == vault and body.get("agent", {}).get("key_id") == key_id,
        ),
        Check("amount is within the passport's per-payment cap", _within(limits, request)),
        Check("payee is allowed by the passport", _payee_ok(body, request.merchant_account)),
        Check(
            "payment was inside the mandate's validity",
            _valid_until(limits, request.created_ms),
        ),
    ]
    return checks


def _within(limits: Mapping[str, Any], request: PaymentRequest) -> bool:
    try:
        return limits.get("token") == request.token_id and request.asset_amount <= int(limits["per_payment_max"])
    except (KeyError, ValueError):
        return False


def _payee_ok(body: Mapping[str, Any], payee: str) -> bool:
    payees = body.get("payees", {})
    return not payees.get("allowlist_enabled") or payee in payees.get("accounts", [])


def _valid_until(limits: Mapping[str, Any], when_ms: int) -> bool:
    try:
        end = int(limits["valid_until_ms"])
    except (KeyError, ValueError):
        return False
    return end == 0 or when_ms <= end


def verify_receipt_on_chain(
    receipt: Mapping[str, Any], outcome: Mapping[str, Any], owner_chain_keys: Sequence[str] | None = None
) -> list[Check]:
    """Check the settlement ``outcome`` (a final transaction status fetched by the caller) against the receipt."""
    cheque = receipt["cheque"]
    key_id = receipt["approval"]["key_id"]
    checks = [
        Check("transaction succeeded", failure_message(outcome) is None, str(failure_message(outcome) or "")),
        Check(
            "transaction hash matches",
            outcome.get("transaction", {}).get("hash") == receipt["settlement"]["tx_hash"],
        ),
    ]
    events = [e for e in extract_events(outcome) if e.get("standard") == "wayfarer_vault"]
    authorised = [e["data"] for e in events if e.get("event") == "payment_authorized"]
    settled = [e["data"] for e in events if e.get("event") == "payment_settled"]
    match = next((d for d in authorised if d.get("intent_id") == cheque["intent_id"]), None)
    checks.append(Check("vault emitted payment_authorized for this cheque", match is not None))
    if match is not None:
        checks += [
            Check(
                "on-chain receipt hash is the request commitment", match.get("receipt_hash") == cheque["receipt_hash"]
            ),
            Check(
                "on-chain payee, token and amount match",
                (match.get("to"), match.get("token"), match.get("amount"))
                == (cheque["to"], cheque["token_id"], cheque["amount"]),
            ),
            Check("on-chain signer is the ML-DSA agent key", match.get("agent_key") == f"ml-dsa-65:{key_id}"),
        ]
    checks.append(
        Check(
            "vault emitted payment_settled (transfer completed)",
            any(d.get("intent_id") == cheque["intent_id"] for d in settled),
        )
    )
    if owner_chain_keys is not None:
        checks.append(owner_key_listed(MandatePassport.from_json(receipt["passport"]), owner_chain_keys))
    return checks
