import json
import unittest
from pathlib import Path

from wayfarer.crypto import mldsa

VECTORS = json.loads((Path(__file__).parent / "vectors" / "mldsa65.json").read_text())["cases"]
ZERO = bytes(32)


class MlDsaKnownAnswerTest(unittest.TestCase):
    """Vectors come from nearcore's near-crypto via tools/pqref (see scripts/crosscheck_mldsa.py)."""

    def test_public_key_from_seed_matches_reference(self) -> None:
        for case in VECTORS:
            with self.subTest(seed=case["seed"][:8]):
                public = mldsa.public_key_from_seed(bytes.fromhex(case["seed"]))
                self.assertEqual(len(public), mldsa.PUBLIC_KEY_SIZE)
                self.assertEqual(public.hex(), case["public_key"])

    def test_deterministic_signature_matches_recorded_answer(self) -> None:
        case = VECTORS[0]
        signature = mldsa.sign(bytes.fromhex(case["seed"]), bytes.fromhex(case["message"]), randomness=ZERO)
        self.assertEqual(len(signature), mldsa.SIGNATURE_SIZE)
        self.assertEqual(signature.hex(), case["signature_deterministic"])

    def test_reference_signatures_verify_here(self) -> None:
        for case in VECTORS:
            with self.subTest(seed=case["seed"][:8]):
                self.assertTrue(
                    mldsa.verify(
                        bytes.fromhex(case["public_key"]),
                        bytes.fromhex(case["signature_reference"]),
                        bytes.fromhex(case["message"]),
                    )
                )


class MlDsaBehaviourTest(unittest.TestCase):
    def setUp(self) -> None:
        case = VECTORS[1]
        self.seed = bytes.fromhex(case["seed"])
        self.public = bytes.fromhex(case["public_key"])
        self.message = bytes.fromhex(case["message"])
        self.signature = mldsa.sign(self.seed, self.message, randomness=ZERO)

    def test_sign_then_verify(self) -> None:
        self.assertTrue(mldsa.verify(self.public, self.signature, self.message))

    def test_hedged_signatures_differ_but_both_verify(self) -> None:
        other = mldsa.sign(self.seed, self.message)
        self.assertNotEqual(other, self.signature)
        self.assertTrue(mldsa.verify(self.public, other, self.message))

    def test_rejects_wrong_message_key_and_corrupted_signature(self) -> None:
        self.assertFalse(mldsa.verify(self.public, self.signature, self.message + b"!"))
        other_public = bytes.fromhex(VECTORS[0]["public_key"])
        self.assertFalse(mldsa.verify(other_public, self.signature, self.message))
        for index in (0, 100, 3000, mldsa.SIGNATURE_SIZE - 1):
            corrupted = bytearray(self.signature)
            corrupted[index] ^= 1
            with self.subTest(index=index):
                self.assertFalse(mldsa.verify(self.public, bytes(corrupted), self.message))

    def test_rejects_wrong_lengths_without_raising(self) -> None:
        self.assertFalse(mldsa.verify(self.public[:-1], self.signature, self.message))
        self.assertFalse(mldsa.verify(self.public, self.signature[:-1], self.message))
        self.assertFalse(mldsa.verify(self.public, self.signature + b"\x00", self.message))

    def test_context_is_bound_into_the_signature(self) -> None:
        signed = mldsa.sign(self.seed, self.message, context=b"wayfarer", randomness=ZERO)
        self.assertTrue(mldsa.verify(self.public, signed, self.message, context=b"wayfarer"))
        self.assertFalse(mldsa.verify(self.public, signed, self.message))
        with self.assertRaises(mldsa.MlDsaError):
            mldsa.sign(self.seed, self.message, context=bytes(256))

    def test_rejects_bad_seed_and_randomness_sizes(self) -> None:
        with self.assertRaises(mldsa.MlDsaError):
            mldsa.public_key_from_seed(self.seed[:31])
        with self.assertRaises(mldsa.MlDsaError):
            mldsa.sign(self.seed, self.message, randomness=b"short")

    def test_malformed_hint_encoding_is_rejected(self) -> None:
        hint_start = mldsa.SIGNATURE_SIZE - (mldsa.OMEGA + mldsa.K)
        corrupted = self.signature[:hint_start] + bytes([255] * mldsa.OMEGA) + bytes([mldsa.OMEGA] * mldsa.K)
        self.assertFalse(mldsa.verify(self.public, corrupted, self.message))


if __name__ == "__main__":
    unittest.main()
