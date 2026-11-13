import base64
import json
import unittest
from pathlib import Path
from typing import Any

from wayfarer.cheque import CHEQUE_DOMAIN, Cheque, key_id, sign_cheque, verify_cheque
from wayfarer.errors import WayfarerError
from wayfarer.near.keys import MlDsaKeyPair

FIXTURES = json.loads(
    (Path(__file__).parent.parent / "contracts" / "wayfarer-vault" / "tests" / "fixtures" / "cheques.json").read_text()
)
KEY = MlDsaKeyPair(FIXTURES["vault"], bytes(range(31, 63)))


def make(**overrides: Any) -> Cheque:
    fields: dict[str, Any] = {
        "vault": FIXTURES["vault"],
        "token_id": "near",
        "to": "ada.test.near",
        "amount": 10**24,
        "intent_id": "c1",
        "expires_at_ms": 4_000_000_000_000,
        "receipt_hash": "wf1:abc",
    }
    return Cheque(**{**fields, **overrides})


class ChequeEncodingTest(unittest.TestCase):
    """The contract's Rust tests rebuild these exact bytes with ``cheque_message`` and verify these signatures."""

    def test_message_matches_the_contract_fixture(self) -> None:
        for name, entry in FIXTURES["cheques"].items():
            with self.subTest(name):
                cheque = Cheque(
                    vault=FIXTURES["vault"],
                    token_id=entry["token_id"],
                    to=entry["to"],
                    amount=int(entry["amount"]),
                    intent_id=entry["intent_id"],
                    expires_at_ms=int(entry["expires_at_ms"]),
                    receipt_hash=entry["receipt_hash"],
                )
                self.assertEqual(cheque.message(), base64.b64decode(entry["message_b64"]))

    def test_layout_is_domain_then_borsh_fields(self) -> None:
        cheque = make(expires_at_ms=5)
        message = cheque.message()
        self.assertTrue(message.startswith(CHEQUE_DOMAIN))
        self.assertTrue(message.endswith(b"\x07\x00\x00\x00wf1:abc"))
        self.assertEqual(len(cheque.digest()), 64)

    def test_fixture_signatures_verify_and_key_id_matches(self) -> None:
        public = base64.b64decode(FIXTURES["public_key_b64"])
        self.assertEqual(key_id(public), FIXTURES["key_id"])
        self.assertEqual(public, KEY.public_key)
        entry = FIXTURES["cheques"]["ok1"]
        cheque = make(expires_at_ms=int(entry["expires_at_ms"]))
        self.assertTrue(verify_cheque(public, base64.b64decode(entry["signature_b64"]), cheque))

    def test_any_changed_field_breaks_the_signature(self) -> None:
        cheque = make()
        signature = sign_cheque(KEY, cheque)
        self.assertTrue(verify_cheque(KEY.public_key, signature, cheque))
        changes: dict[str, object] = {
            "vault": "other.vault.near",
            "token_id": "usdc.near",
            "to": "eve.test.near",
            "amount": 10**24 + 1,
            "intent_id": "c2",
            "expires_at_ms": 4_000_000_000_001,
            "receipt_hash": "wf1:abd",
        }
        for field, value in changes.items():
            with self.subTest(field):
                self.assertFalse(verify_cheque(KEY.public_key, signature, make(**{field: value})))

    def test_invalid_cheques_are_rejected(self) -> None:
        bad: list[dict[str, Any]] = [
            {"amount": 0},
            {"amount": 1 << 128},
            {"expires_at_ms": 0},
            {"intent_id": ""},
            {"intent_id": "x" * 65},
            {"receipt_hash": "h" * 129},
        ]
        for override in bad:
            with self.subTest(override), self.assertRaises(WayfarerError):
                make(**override)

    def test_key_id_rejects_wrong_size(self) -> None:
        with self.assertRaises(WayfarerError):
            key_id(b"short")


if __name__ == "__main__":
    unittest.main()
