import hashlib
import unittest

from tests.fakes import FakeTransport, json_response, settings
from wayfarer.attestation import attest_completion, expected_signed_text
from wayfarer.crypto import ed25519
from wayfarer.nearai import ChatResult, NearAiClient

SEED = bytes(range(1, 33))


def exchange() -> ChatResult:
    return ChatResult("{}", "cmpl-1", "z-ai/glm-5.3-flash", "stop", b'{"req":1}', b'{"resp":1}')


def client_signing(text: str, *, seed: bytes = SEED, signature_of: str | None = None) -> NearAiClient:
    transport = FakeTransport()
    signature = ed25519.sign(seed, (signature_of if signature_of is not None else text).encode())
    transport.on(
        "/signature/cmpl-1",
        lambda _r: json_response(
            {
                "text": text,
                "signature": signature.hex(),
                "signing_address": ed25519.public_key_from_seed(SEED).hex(),
                "signing_algo": "ed25519",
                "signature_kind": "provider_tee",
            }
        ),
    )
    return NearAiClient(settings(), transport=transport)


class AttestationTest(unittest.TestCase):
    def test_expected_text_is_model_request_response(self) -> None:
        result = exchange()
        request_hash = hashlib.sha256(b'{"req":1}').hexdigest()
        response_hash = hashlib.sha256(b'{"resp":1}').hexdigest()
        expected = f"z-ai/glm-5.3-flash:{request_hash}:{response_hash}"
        self.assertEqual(expected_signed_text(result), expected)

    def test_valid_signature_over_our_exchange(self) -> None:
        result = exchange()
        attestation = attest_completion(client_signing(expected_signed_text(result)), result)
        self.assertTrue(attestation.signature_valid)
        self.assertTrue(attestation.text_matches_exchange)
        self.assertEqual(attestation.signature_kind, "provider_tee")

    def test_signature_over_different_bytes_is_flagged(self) -> None:
        result = exchange()
        attestation = attest_completion(client_signing("model:other:other"), result)
        self.assertTrue(attestation.signature_valid)
        self.assertFalse(attestation.text_matches_exchange)

    def test_forged_signature_is_flagged(self) -> None:
        result = exchange()
        text = expected_signed_text(result)
        attestation = attest_completion(client_signing(text, seed=bytes(32), signature_of=text), result)
        self.assertFalse(attestation.signature_valid)

    def test_signature_that_does_not_cover_the_reported_text_is_flagged(self) -> None:
        result = exchange()
        attestation = attest_completion(
            client_signing(expected_signed_text(result), signature_of="something else"), result
        )
        self.assertFalse(attestation.signature_valid)


if __name__ == "__main__":
    unittest.main()
