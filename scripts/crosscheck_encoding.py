"""Check our borsh encodings of delegate actions and transactions against nearcore (tools/pqref).

    cargo build --manifest-path tools/pqref/Cargo.toml
    python3 scripts/crosscheck_encoding.py [--write-vectors tests/vectors/delegate.json]

For an ed25519 key and an ML-DSA-65 key: our unsigned DelegateAction bytes and NEP-461 hash must equal the ones
``near-primitives`` computes, nearcore must accept our signed delegate action and our signed relay transaction, and
(ed25519, deterministic) the signed bytes must be identical. Test keys are fixed and worthless. No network.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from wayfarer.crypto import mldsa  # noqa: E402
from wayfarer.crypto.base58 import b58encode  # noqa: E402
from wayfarer.near.keys import KeyPair, MlDsaKeyPair, SigningKey  # noqa: E402
from wayfarer.near.transaction import (  # noqa: E402
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

PQREF = ROOT / "tools" / "pqref" / "target" / "debug" / "pqref"
ED_SEED = bytes(range(1, 33))
ML_SEED = bytes(range(101, 133))
RELAYER_SEED = bytes(range(201, 233))
VAULT = "vault.test.near"
ARGS = {"intent_id": "i1", "token_id": "near", "to": "ada.test.near", "amount": "1000", "receipt_hash": "wf1:abc"}
CALL = DelegateCall(
    sender_id=VAULT,
    receiver_id=VAULT,
    method="pay_direct",
    args=ARGS,
    gas=100_000_000_000_000,
    nonce=7,
    max_block_height=999,
)


def ref(*args: str) -> dict[str, object]:
    out = subprocess.run([str(PQREF), *args], check=True, capture_output=True, text=True).stdout.strip()
    return dict(json.loads(out))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write-vectors", type=Path)
    args = parser.parse_args()
    if not PQREF.exists():
        print(f"missing {PQREF}; build it first (see the module docstring)")
        return 2
    ed = KeyPair(VAULT, ED_SEED)
    ml = MlDsaKeyPair(VAULT, ML_SEED)
    relayer = KeyPair("relayer.test.near", RELAYER_SEED)
    ed_secret = "ed25519:" + b58encode(ED_SEED + ed.public_key)
    args_json = json.dumps(ARGS, separators=(",", ":"))
    vectors: dict[str, object] = {"call": {"args_json": args_json}, "keys": {}}
    failures = 0
    for name, key, secret in (("ed25519", ed, ed_secret), ("ml-dsa-65", ml, "ml:" + ML_SEED.hex())):
        key_: SigningKey = key
        vec = ref(
            "delegate-vector",
            secret,
            VAULT,
            VAULT,
            "pay_direct",
            args_json,
            str(CALL.gas),
            str(CALL.nonce),
            str(CALL.max_block_height),
        )
        ours_unsigned = encode_delegate_action(CALL, key_)
        signed = sign_delegate_action(CALL, key_)
        check = ref("check-delegate", signed.hex())
        relay_tx = encode_relay_transaction(
            relayer_id=relayer.account_id,
            relayer_public_key=relayer.public_key,
            relayer_key_type=relayer.key_type,
            nonce=42,
            receiver_id=VAULT,
            block_hash=bytes(range(32)),
            signed_delegate=signed,
        )
        tx_check = ref("check-tx", sign_transaction(relayer, relay_tx).hex())
        own_tx = encode_function_call(
            signer_id=VAULT,
            public_key=key_.public_key,
            nonce=3,
            receiver_id=VAULT,
            block_hash=bytes(range(32)),
            method="pay_direct",
            args=ARGS,
            gas=CALL.gas,
            key_type=key_.key_type,
        )
        own_check = ref("check-tx", sign_transaction(key_, own_tx).hex())
        results = {
            "nearcore verifies a transaction this key signs itself": own_check["verifies"] is True,
            "unsigned delegate bytes equal": ours_unsigned.hex() == vec["unsigned_hex"],
            "NEP-461 hash equal": delegate_action_hash(ours_unsigned).hex() == vec["hash_hex"],
            "nearcore verifies our signed delegate": check["verifies"] is True and check["nonce"] == CALL.nonce,
            "nearcore verifies our relay transaction": tx_check["verifies"] is True and tx_check["actions"] == 1,
        }
        if name == "ed25519":
            results["signed bytes identical (deterministic)"] = signed.hex() == vec["signed_hex"]
        else:
            ref_signed = bytes.fromhex(str(vec["signed_hex"]))
            sig = ref_signed[-mldsa.SIGNATURE_SIZE :]
            results["we verify nearcore's delegate signature"] = mldsa.verify(
                key_.public_key, sig, delegate_action_hash(ours_unsigned)
            )
            handle = subprocess.run(
                [str(PQREF), "handle", key_.public_key.hex()], check=True, capture_output=True, text=True
            ).stdout.strip()
            results["on-chain key handle equal"] = handle == ml.public_key_handle_str
            vectors["handle"] = handle
        for label, ok in results.items():
            print(f"{name}: {label}: {'ok' if ok else 'FAIL'}")
            failures += not ok
        keys = vectors["keys"]
        assert isinstance(keys, dict)
        keys[name] = {"unsigned_hex": vec["unsigned_hex"], "hash_hex": vec["hash_hex"], "signed_hex": vec["signed_hex"]}
    # Account-management actions, decoded by nearcore.
    actions = [
        action_transfer(10**24),
        action_add_full_access_key(ml.key_type, ml.public_key),
        action_add_function_call_key(
            ml.key_type, ml.public_key, receiver_id=VAULT, methods=["pay_direct", "pay_via_intents"], allowance=10**23
        ),
        action_delete_key(ed.key_type, ed.public_key),
    ]
    mgmt = encode_transaction(
        signer_id=VAULT,
        key_type=ml.key_type,
        public_key=ml.public_key,
        nonce=9,
        receiver_id=VAULT,
        block_hash=bytes(32),
        actions=actions,
    )
    mgmt_check = ref("check-tx", sign_transaction(ml, mgmt).hex())
    debug = " ".join(str(d) for d in mgmt_check["action_debug"])  # type: ignore[attr-defined]
    mgmt_results = {
        "nearcore verifies an ML-DSA-signed multi-action transaction": mgmt_check["verifies"] is True,
        "four actions decoded": mgmt_check["actions"] == 4,
        "kinds are Transfer, AddKey, AddKey, DeleteKey": all(k in debug for k in ("Transfer", "AddKey", "DeleteKey")),
        "function-call permission fields intact": "pay_via_intents" in debug and "100000000000000000000000" in debug,
        "full-access permission decoded": "FullAccess" in debug,
    }
    for label, ok in mgmt_results.items():
        print(f"actions: {label}: {'ok' if ok else 'FAIL'}")
        failures += not ok
    if args.write_vectors and not failures:
        args.write_vectors.write_text(json.dumps(vectors))
        print(f"wrote {args.write_vectors}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
