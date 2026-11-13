"""Verifying that a completion was signed by the TEE that served it.

What this proves: the signature is valid for the exact request and response bytes we exchanged, under the signing key
the gateway reported. What it does NOT prove: that this key belongs to a genuine, unmodified TEE. That requires
verifying the hardware attestation report (``/v1/attestation/report``), which this prototype does not do.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass

from wayfarer.crypto import ed25519
from wayfarer.errors import UpstreamError
from wayfarer.nearai import ChatResult, NearAiClient


@dataclass(frozen=True, slots=True)
class Attestation:
    """Evidence stored with a receipt."""

    completion_id: str
    model: str
    signed_text: str
    signature: str
    signing_address: str
    signing_algo: str
    signature_kind: str | None
    signature_valid: bool
    text_matches_exchange: bool

    def to_json(self) -> dict[str, object]:
        return asdict(self)


def expected_signed_text(result: ChatResult) -> str:
    """The text NEAR AI Cloud signs: ``<model>:<sha256(request)>:<sha256(response)>``."""
    return f"{result.model}:{hashlib.sha256(result.request_body).hexdigest()}:{hashlib.sha256(result.response_body).hexdigest()}"


def attest_completion(client: NearAiClient, result: ChatResult) -> Attestation:
    """Fetch the ed25519 signature for ``result`` and verify it locally."""
    record = client.signature(result.completion_id, algo="ed25519")
    if record.signing_algo != "ed25519":
        raise UpstreamError("Unexpected signing algorithm from NEAR AI Cloud.")
    try:
        valid = ed25519.verify(
            bytes.fromhex(record.signing_address), bytes.fromhex(record.signature), record.text.encode()
        )
    except ValueError:
        valid = False
    return Attestation(
        completion_id=result.completion_id,
        model=result.model,
        signed_text=record.text,
        signature=record.signature,
        signing_address=record.signing_address,
        signing_algo=record.signing_algo,
        signature_kind=record.signature_kind,
        signature_valid=valid,
        text_matches_exchange=record.text == expected_signed_text(result),
    )
