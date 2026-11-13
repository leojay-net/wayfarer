"""Write ML-DSA-65 cheque fixtures for the contract's Rust tests from the Python signer.

    python3 scripts/make_cheque_fixtures.py

The fixture key is a fixed, worthless test seed. Signing is deterministic so the file is reproducible. The Rust
tests rebuild the message with the contract and verify these signatures with the real ``ml_dsa_verify`` host code,
which checks the Python encoding and signer against the contract.
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from wayfarer.cheque import Cheque, key_id  # noqa: E402
from wayfarer.crypto import mldsa  # noqa: E402
from wayfarer.near.keys import MlDsaKeyPair  # noqa: E402

VAULT = "vault.alice.near"
SEED = bytes(range(31, 63))
OUT = ROOT / "contracts" / "wayfarer-vault" / "tests" / "fixtures" / "cheques.json"


def entry(key: MlDsaKeyPair, cheque: Cheque) -> dict[str, object]:
    signature = mldsa.sign(key.seed, cheque.message(), randomness=bytes(32))
    return {
        "intent_id": cheque.intent_id,
        "token_id": cheque.token_id,
        "to": cheque.to,
        "amount": str(cheque.amount),
        "expires_at_ms": str(cheque.expires_at_ms),
        "receipt_hash": cheque.receipt_hash,
        "message_b64": base64.b64encode(cheque.message()).decode(),
        "signature_b64": base64.b64encode(signature).decode(),
    }


def main() -> None:
    key = MlDsaKeyPair(VAULT, SEED)

    def make(intent_id: str, expires_at_ms: int, to: str = "ada.test.near") -> Cheque:
        return Cheque(
            vault=VAULT,
            token_id="near",  # noqa: S106 (a contract token id, not a credential)
            to=to,
            amount=10**24,
            intent_id=intent_id,
            expires_at_ms=expires_at_ms,
            receipt_hash="wf1:abc",
        )

    expiry = 4_000_000_000_000  # far future in the mock environment
    cheques = {
        "ok1": make("c1", expiry),
        "ok2": make("c2", expiry),
        "ok3": make("c3", expiry),
        "other_payee": make("c4", expiry, to="eve.test.near"),
        "stale": make("c5", 1_000),
    }
    data = {
        "vault": VAULT,
        "public_key_b64": base64.b64encode(key.public_key).decode(),
        "key_id": key_id(key.public_key),
        "cheques": {name: entry(key, c) for name, c in cheques.items()},
    }
    OUT.write_text(json.dumps(data, indent=1))
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
