import json
import unittest
from pathlib import Path

from wayfarer.crypto import mldsa
from wayfarer.near.keys import KeyPair, KeyType, MlDsaKeyPair
from wayfarer.near.transaction import (
    DelegateCall,
    action_add_full_access_key,
    action_add_function_call_key,
    action_delete_key,
    action_transfer,
    delegate_action_hash,
    encode_delegate_action,
    encode_function_call,
    encode_relay_transaction,
    encode_transaction,
    sign_delegate_action,
    sign_transaction,
)

V = json.loads((Path(__file__).parent / "vectors" / "delegate.json").read_text())
VAULT = "vault.test.near"
ARGS = json.loads(V["call"]["args_json"])
CALL = DelegateCall(
    sender_id=VAULT,
    receiver_id=VAULT,
    method="pay_direct",
    args=ARGS,
    gas=100_000_000_000_000,
    nonce=7,
    max_block_height=999,
)
ED = KeyPair(VAULT, bytes(range(1, 33)))
ML = MlDsaKeyPair(VAULT, bytes(range(101, 133)))


class DelegateEncodingTest(unittest.TestCase):
    """Expected bytes come from nearcore's near-primitives via tools/pqref (scripts/crosscheck_encoding.py)."""

    def test_unsigned_bytes_and_nep461_hash_match_nearcore(self) -> None:
        for name, key in (("ed25519", ED), ("ml-dsa-65", ML)):
            with self.subTest(name):
                encoded = encode_delegate_action(CALL, key)
                self.assertEqual(encoded.hex(), V["keys"][name]["unsigned_hex"])
                self.assertEqual(delegate_action_hash(encoded).hex(), V["keys"][name]["hash_hex"])

    def test_ed25519_signed_delegate_is_byte_identical(self) -> None:
        self.assertEqual(sign_delegate_action(CALL, ED).hex(), V["keys"]["ed25519"]["signed_hex"])

    def test_ml_dsa_delegate_has_expected_size_and_valid_signature(self) -> None:
        signed = sign_delegate_action(CALL, ML)
        unsigned = encode_delegate_action(CALL, ML)
        self.assertEqual(len(signed), len(unsigned) + 1 + mldsa.SIGNATURE_SIZE)
        self.assertEqual(signed[len(unsigned)], KeyType.MLDSA65)
        signature = signed[len(unsigned) + 1 :]
        self.assertTrue(mldsa.verify(ML.public_key, signature, delegate_action_hash(unsigned)))

    def test_nearcore_signed_ml_dsa_delegate_verifies_here(self) -> None:
        reference = bytes.fromhex(V["keys"]["ml-dsa-65"]["signed_hex"])
        self.assertTrue(
            mldsa.verify(
                ML.public_key, reference[-mldsa.SIGNATURE_SIZE :], bytes.fromhex(V["keys"]["ml-dsa-65"]["hash_hex"])
            )
        )

    def test_changing_any_field_changes_the_hash(self) -> None:
        base = delegate_action_hash(encode_delegate_action(CALL, ED))
        for field, value in (("nonce", 8), ("max_block_height", 1000), ("method", "pay_via_intents"), ("gas", 1)):
            changed = DelegateCall(**{**_fields(CALL), field: value})  # type: ignore[arg-type]
            with self.subTest(field):
                self.assertNotEqual(delegate_action_hash(encode_delegate_action(changed, ED)), base)

    def test_relay_transaction_wraps_the_signed_delegate(self) -> None:
        relayer = KeyPair("relayer.test.near", bytes(range(201, 233)))
        signed = sign_delegate_action(CALL, ML)
        tx = encode_relay_transaction(
            relayer_id=relayer.account_id,
            relayer_public_key=relayer.public_key,
            relayer_key_type=relayer.key_type,
            nonce=42,
            receiver_id=VAULT,
            block_hash=bytes(32),
            signed_delegate=signed,
        )
        self.assertTrue(tx.endswith(bytes([8]) + signed))
        self.assertEqual(len(sign_transaction(relayer, tx)), len(tx) + 1 + 64)

    def test_ml_dsa_transaction_uses_key_type_2_and_full_key(self) -> None:
        tx = encode_function_call(
            signer_id=VAULT,
            public_key=ML.public_key,
            nonce=3,
            receiver_id=VAULT,
            block_hash=bytes(32),
            method="pay_direct",
            args=ARGS,
            gas=1,
            key_type=ML.key_type,
        )
        self.assertIn(bytes([2]) + ML.public_key, tx)
        signed = sign_transaction(ML, tx)
        self.assertEqual(len(signed), len(tx) + 1 + mldsa.SIGNATURE_SIZE)


class ActionBuilderTest(unittest.TestCase):
    """Byte layouts were confirmed by nearcore's decoder in scripts/crosscheck_encoding.py."""

    def test_action_sizes_and_tags(self) -> None:
        self.assertEqual(action_transfer(7), bytes([3]) + (7).to_bytes(16, "little"))
        add = action_add_full_access_key(KeyType.MLDSA65, ML.public_key)
        self.assertEqual(add[0], 5)
        self.assertEqual(len(add), 1 + 1 + mldsa.PUBLIC_KEY_SIZE + 8 + 1)
        self.assertEqual(add[-1], 1)  # FullAccess
        delete = action_delete_key(KeyType.ED25519, ED.public_key)
        self.assertEqual(delete, bytes([6, 0]) + ED.public_key)

    def test_function_call_key_permission_layout(self) -> None:
        limited = action_add_function_call_key(
            KeyType.ED25519, ED.public_key, receiver_id="v.near", methods=["a", "bc"], allowance=5
        )
        unlimited = action_add_function_call_key(
            KeyType.ED25519, ED.public_key, receiver_id="v.near", methods=["a", "bc"], allowance=None
        )
        self.assertEqual(len(limited) - len(unlimited), 16)
        self.assertIn(b"\x00\x01" + (5).to_bytes(16, "little"), limited)  # FunctionCall tag, Some(allowance)

    def test_transaction_counts_its_actions(self) -> None:
        tx = encode_transaction(
            signer_id="a.near",
            key_type=KeyType.ED25519,
            public_key=ED.public_key,
            nonce=1,
            receiver_id="b.near",
            block_hash=bytes(32),
            actions=[action_transfer(1), action_transfer(2)],
        )
        self.assertTrue(tx.endswith((2).to_bytes(4, "little") + action_transfer(1) + action_transfer(2)))


class MlDsaKeyPairTest(unittest.TestCase):
    def test_public_key_string_and_handle(self) -> None:
        self.assertTrue(ML.public_key_str.startswith("ml-dsa-65:"))
        self.assertEqual(ML.public_key_handle_str, V["handle"])
        self.assertEqual(len(ML.public_key_handle), 32)

    def test_secret_round_trip_and_repr_hides_seed(self) -> None:
        again = MlDsaKeyPair.from_secret(VAULT, ML.secret_str())
        self.assertEqual(again.seed, ML.seed)
        self.assertNotIn(ML.seed.hex(), repr(ML))
        self.assertNotIn(ML.secret_str(), repr(ML))

    def test_rejects_malformed_secrets(self) -> None:
        from wayfarer.errors import WayfarerError

        for bad in ("ed25519:abc", "ml-dsa-65-seed:1111", ""):
            with self.subTest(bad), self.assertRaises(WayfarerError):
                MlDsaKeyPair.from_secret(VAULT, bad)

    def test_key_file_with_mismatched_public_key_is_rejected(self) -> None:
        import tempfile

        from wayfarer.errors import WayfarerError

        with tempfile.TemporaryDirectory() as tmp:
            good = Path(tmp) / "good.json"
            good.write_text(
                json.dumps({"account_id": VAULT, "public_key": ML.public_key_str, "private_key": ML.secret_str()})
            )
            self.assertEqual(MlDsaKeyPair.from_file(good).seed, ML.seed)
            bad = Path(tmp) / "bad.json"
            bad.write_text(
                json.dumps({"account_id": VAULT, "public_key": ED.public_key_str, "private_key": ML.secret_str()})
            )
            with self.assertRaises(WayfarerError):
                MlDsaKeyPair.from_file(bad)


def _fields(call: DelegateCall) -> dict[str, object]:
    return {f: getattr(call, f) for f in call.__slots__}


if __name__ == "__main__":
    unittest.main()
