import base64
import copy
import json
import unittest
from dataclasses import replace
from decimal import Decimal
from typing import Any

from wayfarer.errors import WayfarerError
from wayfarer.mandate import MandatePassport, issue_passport, verify_passport
from wayfarer.near.keys import MlDsaKeyPair
from wayfarer.pqvault import MandateTerms
from wayfarer.proof import Settlement, build_receipt, verify_receipt_offline, verify_receipt_on_chain
from wayfarer.tap.buyer import ApprovalPrompt, BuyerWallet, TapLimits
from wayfarer.tap.merchant import MerchantTerminal
from wayfarer.tap.rates import FixedRateSource, RateDesk

NOW = 1_800_000_000_000
NEAR = 10**24
VAULT = "buyer-vault.testnet"
OWNER = "owner.testnet"
OWNER_KEY = MlDsaKeyPair(OWNER, bytes(range(3, 35)))
BUYER_KEY = MlDsaKeyPair(VAULT, bytes(range(31, 63)))


class Yes:
    def confirm(self, prompt: ApprovalPrompt) -> bool:
        return True


def terms(**overrides: Any) -> MandateTerms:
    base = MandateTerms(
        vault=VAULT,
        owner=OWNER,
        key_id=__import__("hashlib").sha256(BUYER_KEY.public_key).hexdigest(),
        paused=False,
        expires_at_ms=0,
        min_interval_ms=5000,
        last_payment_ms=0,
        token_id="near",
        per_tx_max=NEAR // 2,
        daily_max=NEAR,
        allowlist_enabled=False,
        allowlist=(),
    )
    return replace(base, **overrides)


def make_receipt(passport_terms: MandateTerms | None = None) -> tuple[dict[str, Any], MandatePassport]:
    desk = RateDesk(FixedRateSource({("USD", "near"): Decimal("5000")}), lambda: NOW, new_id=lambda: "q1")
    merchant = MerchantTerminal(
        account="cafe-shop.testnet", name="Sam's Cafe", rates=desk, clock_ms=lambda: NOW, nonce_factory=lambda: bytes(16)
    )
    request = merchant.create_request(150_000)
    wallet = BuyerWallet(
        vault=VAULT, agent_key=BUYER_KEY, confirmer=Yes(), limits=TapLimits({"near": NEAR}), clock_ms=lambda: NOW
    )
    accepted = merchant.accept(wallet.pay(request).encode())
    passport = issue_passport(
        network="testnet", terms=passport_terms or terms(), owner_key=OWNER_KEY, owner_account=OWNER, issued_at_ms=NOW
    )
    receipt = build_receipt(
        network="testnet",
        accepted=accepted,
        passport=passport,
        settlement=Settlement("TXHASH", "cafe-shop.testnet", "https://x/TXHASH"),
    )
    return receipt, passport


def outcome(receipt: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    cheque, key_id = receipt["cheque"], receipt["approval"]["key_id"]
    authorised = {
        "kind": "cheque",
        "intent_id": cheque["intent_id"],
        "token": cheque["token_id"],
        "to": cheque["to"],
        "amount": cheque["amount"],
        "declared_recipient": cheque["to"],
        "receipt_hash": cheque["receipt_hash"],
        "agent_key": f"ml-dsa-65:{key_id}",
        "relayer": "cafe-shop.testnet",
        **overrides.get("authorised", {}),
    }
    events = [{"standard": "wayfarer_vault", "version": "1.0.0", "event": "payment_authorized", "data": authorised}]
    if overrides.get("settled", True):
        events.append(
            {
                "standard": "wayfarer_vault",
                "version": "1.0.0",
                "event": "payment_settled",
                "data": {"intent_id": cheque["intent_id"], "token": "near", "amount": cheque["amount"]},
            }
        )
    status = overrides.get("status", {"SuccessValue": ""})
    logs = ["EVENT_JSON:" + json.dumps(e) for e in events]
    return {"transaction": {"hash": "TXHASH"}, "status": status, "receipts_outcome": [{"outcome": {"logs": logs}}]}


def failed(checks: list[Any]) -> list[str]:
    return [c.name for c in checks if not c.ok]


class PassportTest(unittest.TestCase):
    def test_issue_and_verify(self) -> None:
        passport = issue_passport(
            network="testnet", terms=terms(), owner_key=OWNER_KEY, owner_account=OWNER, issued_at_ms=NOW
        )
        checks = verify_passport(passport, live=terms(), owner_chain_keys=[OWNER_KEY.public_key_handle_str])
        self.assertEqual(failed(checks), [])
        self.assertEqual(passport.body["limits"]["per_payment_max"], str(NEAR // 2))
        self.assertIn("not compliant", passport.body["inspired_by"])
        self.assertEqual(MandatePassport.from_json(json.loads(json.dumps(passport.to_json()))), passport)

    def test_detects_tampering_and_mismatches(self) -> None:
        passport = issue_passport(
            network="testnet", terms=terms(), owner_key=OWNER_KEY, owner_account=OWNER, issued_at_ms=NOW
        )
        forged_body = copy.deepcopy(passport.body)
        forged_body["limits"]["per_payment_max"] = str(10 * NEAR)
        forged = replace(passport, body=forged_body)
        self.assertIn("owner signature (ML-DSA-65) valid", failed(verify_passport(forged)))
        self.assertIn("matches the vault's live mandate", failed(verify_passport(passport, live=terms(per_tx_max=1))))
        self.assertIn(
            "matches the vault's live mandate", failed(verify_passport(passport, live=terms(allowlist=("x.near",))))
        )
        self.assertIn(
            "owner key is on the owner account on-chain",
            failed(verify_passport(passport, owner_chain_keys=["ml-dsa-65-hash:other"])),
        )

    def test_refuses_to_sign_for_another_owner(self) -> None:
        with self.assertRaises(WayfarerError):
            issue_passport(
                network="testnet",
                terms=terms(owner="someone.else"),
                owner_key=OWNER_KEY,
                owner_account=OWNER,
                issued_at_ms=NOW,
            )

    def test_malformed_json(self) -> None:
        with self.assertRaises(WayfarerError):
            MandatePassport.from_json({"body": {}})


class ReceiptOfflineTest(unittest.TestCase):
    def test_honest_receipt_passes_every_check(self) -> None:
        receipt, _ = make_receipt()
        checks = verify_receipt_offline(json.loads(json.dumps(receipt)))
        self.assertGreaterEqual(len(checks), 12)
        self.assertEqual(failed(checks), [])

    def test_each_kind_of_tampering_is_caught(self) -> None:
        receipt, _ = make_receipt()

        def tamper(path: tuple[str, ...], value: Any) -> dict[str, Any]:
            copy_ = copy.deepcopy(receipt)
            node = copy_
            for key in path[:-1]:
                node = node[key]
            node[path[-1]] = value
            return copy_

        cases = {
            "request amount changed": (tamper(("request", "asset_amount"), "1"), "request hash recomputes"),
            "cheque payee changed": (
                tamper(("cheque", "to"), "eve.testnet"),
                "cheque is exactly what the request asks for",
            ),
            "different signature": (
                tamper(("approval", "signature"), base64.b64encode(bytes(3309)).decode()),
                "buyer's ML-DSA-65 signature covers the cheque",
            ),
            "key id swapped": (tamper(("approval", "key_id"), "00" * 32), "agent key id is the hash of the public key"),
            "passport vault changed": (
                tamper(("passport", "hash"), "00" * 32),
                "passport hash matches the receipt",
            ),
        }
        for label, (data, expected_failure) in cases.items():
            with self.subTest(label):
                self.assertIn(expected_failure, failed(verify_receipt_offline(data)))

    def test_passport_limits_are_enforced_in_the_receipt(self) -> None:
        low, _ = make_receipt(terms(per_tx_max=NEAR // 100))  # cap 0.01 NEAR but the payment is 0.3 NEAR
        self.assertIn("amount is within the passport's per-payment cap", failed(verify_receipt_offline(low)))
        blocked, _ = make_receipt(terms(allowlist_enabled=True, allowlist=("other.testnet",)))
        self.assertIn("payee is allowed by the passport", failed(verify_receipt_offline(blocked)))
        ended, _ = make_receipt(terms(expires_at_ms=NOW - 1))
        self.assertIn("payment was inside the mandate's validity", failed(verify_receipt_offline(ended)))
        other_key, _ = make_receipt(terms(key_id="11" * 32))
        self.assertIn("passport is for this vault and key", failed(verify_receipt_offline(other_key)))

    def test_garbage_never_raises(self) -> None:
        garbage_cases: list[dict[str, Any]] = [
            {},
            {"type": "x"},
            {"request": 1},
            {"request": {}, "approval": {}, "passport": {}, "cheque": {}},
        ]
        for garbage in garbage_cases:
            with self.subTest(garbage):
                checks = verify_receipt_offline(garbage)
                self.assertTrue(checks and not checks[0].ok)


class ReceiptOnChainTest(unittest.TestCase):
    def test_matching_events_pass(self) -> None:
        receipt, _ = make_receipt()
        checks = verify_receipt_on_chain(receipt, outcome(receipt), [OWNER_KEY.public_key_handle_str])
        self.assertEqual(failed(checks), [])

    def test_problems_are_reported(self) -> None:
        receipt, _ = make_receipt()
        cases: dict[str, tuple[dict[str, Any], str]] = {
            "wrong amount": (outcome(receipt, authorised={"amount": "1"}), "on-chain payee, token and amount match"),
            "wrong agent key": (
                outcome(receipt, authorised={"agent_key": "ml-dsa-65:00"}),
                "on-chain signer is the ML-DSA agent key",
            ),
            "wrong commitment": (
                outcome(receipt, authorised={"receipt_hash": "wft:00"}),
                "on-chain receipt hash is the request commitment",
            ),
            "no settlement event": (
                outcome(receipt, settled=False),
                "vault emitted payment_settled (transfer completed)",
            ),
            "other intent": (
                outcome(receipt, authorised={"intent_id": "tap-other"}),
                "vault emitted payment_authorized for this cheque",
            ),
        }
        for label, (data, expected) in cases.items():
            with self.subTest(label):
                self.assertIn(expected, failed(verify_receipt_on_chain(receipt, data)))
        refused = outcome(
            receipt,
            status={
                "Failure": {
                    "ActionError": {"kind": {"FunctionCallError": {"ExecutionError": "Smart contract panicked: no"}}}
                }
            },
        )
        self.assertIn("transaction succeeded", failed(verify_receipt_on_chain(receipt, refused)))

    def test_wrong_transaction_and_unlisted_owner_key(self) -> None:
        receipt, _ = make_receipt()
        data = outcome(receipt)
        data["transaction"]["hash"] = "OTHER"
        self.assertIn("transaction hash matches", failed(verify_receipt_on_chain(receipt, data)))
        self.assertIn(
            "owner key is on the owner account on-chain",
            failed(verify_receipt_on_chain(receipt, outcome(receipt), ["ml-dsa-65-hash:nope"])),
        )


if __name__ == "__main__":
    unittest.main()
