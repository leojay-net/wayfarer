import base64
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from wayfarer.crypto import ed25519
from wayfarer.crypto.base58 import b58decode, b58encode

RFC_SEED = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
RFC_PUBLIC = "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
RFC_SIGNATURE = "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"


class Ed25519Test(unittest.TestCase):
    def test_rfc8032_vector(self) -> None:
        self.assertEqual(ed25519.public_key_from_seed(RFC_SEED).hex(), RFC_PUBLIC)
        self.assertEqual(ed25519.sign(RFC_SEED, b"").hex(), RFC_SIGNATURE)

    def test_verify_accepts_valid_and_rejects_tampering(self) -> None:
        public = bytes.fromhex(RFC_PUBLIC)
        signature = bytes.fromhex(RFC_SIGNATURE)
        self.assertTrue(ed25519.verify(public, signature, b""))
        self.assertFalse(ed25519.verify(public, signature, b"x"))
        flipped = bytes([signature[0] ^ 1]) + signature[1:]
        self.assertFalse(ed25519.verify(public, flipped, b""))
        self.assertFalse(ed25519.verify(public[:31], signature, b""))
        self.assertFalse(ed25519.verify(public, signature[:63], b""))

    def test_verify_rejects_non_canonical_scalar(self) -> None:
        signature = bytes.fromhex(RFC_SIGNATURE)
        bad = signature[:32] + (2**253).to_bytes(32, "little")
        self.assertFalse(ed25519.verify(bytes.fromhex(RFC_PUBLIC), bad, b""))

    def test_sign_then_verify_roundtrip(self) -> None:
        seed = bytes(range(32))
        message = b"wayfarer"
        self.assertTrue(ed25519.verify(ed25519.public_key_from_seed(seed), ed25519.sign(seed, message), message))

    def test_matches_openssl(self) -> None:
        seed = bytes(range(32))
        message = b"wayfarer test message"
        der = bytes.fromhex("302e020100300506032b657004220420") + seed
        with tempfile.TemporaryDirectory() as directory:
            key_path, message_path = Path(directory, "k.pem"), Path(directory, "m")
            key_path.write_text(
                "-----BEGIN PRIVATE KEY-----\n" + base64.b64encode(der).decode() + "\n-----END PRIVATE KEY-----\n"
            )
            message_path.write_bytes(message)
            expected = subprocess.run(  # noqa: S603
                ["openssl", "pkeyutl", "-sign", "-inkey", str(key_path), "-rawin", "-in", str(message_path)],  # noqa: S607
                capture_output=True,
                check=True,
            ).stdout
        self.assertEqual(ed25519.sign(seed, message), expected)

    def test_seed_length_is_checked(self) -> None:
        with self.assertRaises(ValueError):
            ed25519.sign(b"short", b"")


class Base58Test(unittest.TestCase):
    def test_roundtrip(self) -> None:
        for data in (b"\0\0abc", bytes(range(32)), b"\xff" * 5, b""):
            self.assertEqual(b58decode(b58encode(data)), data)

    def test_rejects_characters_outside_alphabet(self) -> None:
        with self.assertRaises(ValueError):
            b58decode("0OIl")


if __name__ == "__main__":
    unittest.main()
    _ = os  # keep import used for linters when run directly
