import csv
import os
import stat
import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from wayfarer.errors import ReceiptError
from wayfarer.intent import PaymentIntent, Token
from wayfarer.receipts import COMMITMENT_PREFIX, NULLIFIER_PREFIX, ReceiptLedger, ReceiptOpening, verify_opening

INTENT = PaymentIntent(Decimal("0.1"), Token.NEAR, "ada.testnet")
YOCTO_0_1 = str(10**23)


def event(opening: ReceiptOpening, **overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "kind": "direct", "intent_id": opening.nullifier, "token": "near", "to": "ada.testnet", "amount": YOCTO_0_1,
        "declared_recipient": "ada.testnet", "receipt_hash": opening.commitment,
    }  # fmt: skip
    data.update(overrides)
    return {"standard": "wayfarer_vault", "version": "1.0.0", "event": "payment_authorized", "data": data}


class CommitmentTest(unittest.TestCase):
    def test_commitment_is_hiding_two_receipts_for_the_same_instruction_differ(self) -> None:
        a, b = (
            ReceiptOpening.create("send 0.1 near to ada", INTENT),
            ReceiptOpening.create("send 0.1 near to ada", INTENT),
        )
        self.assertNotEqual(a.commitment, b.commitment)
        self.assertNotEqual(a.nullifier, b.nullifier)

    def test_binding_changing_anything_changes_the_commitment(self) -> None:
        a = ReceiptOpening.create("t", INTENT)
        for changed in (
            ReceiptOpening(a.salt, "t2", a.intent),
            ReceiptOpening(a.salt, "t", {**a.intent, "amount": "0.2"}),
            ReceiptOpening(bytes(32), "t", a.intent),
        ):
            self.assertNotEqual(changed.commitment, a.commitment)

    def test_framing_prevents_field_boundary_ambiguity(self) -> None:
        a = ReceiptOpening(bytes(32), "ab", {"k": "c"})
        b = ReceiptOpening(bytes(32), "a", {"k": "bc"})
        self.assertNotEqual(a.commitment, b.commitment)

    def test_formats_fit_the_contract_limits(self) -> None:
        opening = ReceiptOpening.create("t", INTENT)
        self.assertTrue(opening.commitment.startswith(COMMITMENT_PREFIX))
        self.assertTrue(opening.nullifier.startswith(NULLIFIER_PREFIX))
        self.assertLessEqual(len(opening.nullifier), 64)  # contract: intent_id 1-64 chars
        self.assertLessEqual(len(opening.commitment), 128)  # contract: receipt_hash <= 128 chars

    def test_nullifier_does_not_reveal_the_commitment(self) -> None:
        opening = ReceiptOpening.create("t", INTENT)
        self.assertNotIn(opening.nullifier[len(NULLIFIER_PREFIX) :][:16], opening.commitment)

    def test_json_roundtrip(self) -> None:
        opening = ReceiptOpening.create("send it", INTENT)
        again = ReceiptOpening.from_json(opening.to_json())
        self.assertEqual((again.commitment, again.nullifier), (opening.commitment, opening.nullifier))

    def test_malformed_opening_is_rejected(self) -> None:
        for bad in (
            {},
            {"version": 2},
            {"version": 1, "salt": "zz", "transcript": "", "intent": {}},
            {"version": 1, "salt": "00", "transcript": "", "intent": 5},
        ):
            with self.subTest(bad=bad), self.assertRaises(ReceiptError):
                ReceiptOpening.from_json(bad)


class VerifyTest(unittest.TestCase):
    def checks(self, opening: ReceiptOpening, events: list[dict[str, object]]) -> dict[str, bool]:
        return {c.name: c.ok for c in verify_opening(opening, events, vault_token_id="near", decimals=24)}

    def test_valid_opening_passes_every_check(self) -> None:
        opening = ReceiptOpening.create("t", INTENT)
        self.assertTrue(all(self.checks(opening, [event(opening)]).values()))

    def test_unknown_commitment_fails(self) -> None:
        opening, other = ReceiptOpening.create("t", INTENT), ReceiptOpening.create("t", INTENT)
        self.assertFalse(self.checks(opening, [event(other)])["commitment appears on-chain"])

    def test_mismatched_amount_token_or_recipient_fail(self) -> None:
        opening = ReceiptOpening.create("t", INTENT)
        self.assertFalse(self.checks(opening, [event(opening, amount="1")])["amount matches"])
        self.assertFalse(self.checks(opening, [event(opening, token="usdc.near")])["token matches"])
        self.assertFalse(
            self.checks(opening, [event(opening, to="eve.testnet", declared_recipient="eve.testnet")])[
                "recipient matches"
            ]
        )
        self.assertFalse(self.checks(opening, [event(opening, intent_id="wfn:other")])["nullifier matches payment id"])

    def test_other_standards_and_events_are_ignored(self) -> None:
        opening = ReceiptOpening.create("t", INTENT)
        foreign = {**event(opening), "standard": "nep141"}
        settled = {**event(opening), "event": "payment_settled"}
        self.assertFalse(self.checks(opening, [foreign, settled])["commitment appears on-chain"])


class LedgerTest(unittest.TestCase):
    def test_ledger_is_private_append_only_and_exports_without_secrets(self) -> None:
        with TemporaryDirectory() as directory:
            ledger = ReceiptLedger(Path(directory, "sub", "ledger.jsonl"))
            opening = ReceiptOpening.create("my secret instruction", INTENT)
            ledger.append("proposed", opening, attestation={"signature_valid": True})
            ledger.append("paid", opening, tx_hash="TX123", detail="Paid.")
            mode = stat.S_IMODE(os.stat(Path(directory, "sub", "ledger.jsonl")).st_mode)
            self.assertEqual(mode, 0o600)
            self.assertEqual(ledger.find_opening(opening.nullifier).commitment, opening.commitment)
            with self.assertRaises(ReceiptError):
                ledger.find_opening("wfn:missing")
            out = Path(directory, "out.csv")
            self.assertEqual(ledger.export_csv(out, explorer_base="https://x/"), 1)
            text = out.read_text()
            self.assertNotIn("my secret instruction", text)
            self.assertNotIn(opening.salt.hex(), text)
            row = next(csv.DictReader(text.splitlines()))
            self.assertEqual(
                (row["status"], row["token"], row["amount"], row["tx_hash"], row["explorer_url"]),
                ("paid", "NEAR", "0.1", "TX123", "https://x/TX123"),
            )
            self.assertEqual(row["model_signature_valid"], "True")

    def test_proposed_without_outcome_is_exported_as_proposed(self) -> None:
        with TemporaryDirectory() as directory:
            ledger = ReceiptLedger(Path(directory, "l.jsonl"))
            ledger.append("proposed", ReceiptOpening.create("t", INTENT))
            ledger.export_csv(Path(directory, "o.csv"), explorer_base="")
            self.assertEqual(
                next(csv.DictReader(Path(directory, "o.csv").read_text().splitlines()))["status"], "proposed"
            )

    def test_empty_ledger(self) -> None:
        with TemporaryDirectory() as directory:
            self.assertEqual(ReceiptLedger(Path(directory, "none.jsonl")).records(), [])


if __name__ == "__main__":
    unittest.main()
