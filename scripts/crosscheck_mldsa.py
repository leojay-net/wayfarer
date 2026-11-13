"""Cross-check the pure-Python ML-DSA-65 signer against nearcore's ``near-crypto`` (tools/pqref).

    cargo build --manifest-path tools/pqref/Cargo.toml
    python3 scripts/crosscheck_mldsa.py [--cases 5] [--write-vectors tests/vectors/mldsa65.json]

For random seeds and messages it checks that public keys are byte-identical, that our signatures verify in the
reference, and that the reference's signatures verify here. ``--write-vectors`` stores the first three cases as known
answers for the offline unit tests. Needs the built ``pqref`` binary; spends nothing and touches no network.
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

PQREF = ROOT / "tools" / "pqref" / "target" / "debug" / "pqref"


def ref(*args: str) -> str:
    return subprocess.run([str(PQREF), *args], check=True, capture_output=True, text=True).stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=int, default=5)
    parser.add_argument("--write-vectors", type=Path)
    args = parser.parse_args()
    if not PQREF.exists():
        print(f"missing {PQREF}; build it first (see the module docstring)")
        return 2
    vectors = []
    for i in range(args.cases):
        seed = bytes((i * 37 + j * 11 + 5) % 256 for j in range(32))
        message = bytes((i * 13 + j * 7) % 256 for j in range(32 + 29 * i))
        ref_pk = bytes.fromhex(json.loads(ref("keygen", seed.hex()))["public_hex"])
        ours_pk = mldsa.public_key_from_seed(seed)
        ours_sig = mldsa.sign(seed, message, randomness=bytes(32))
        ref_sig = bytes.fromhex(ref("sign", seed.hex(), message.hex()))
        checks = {
            "public key identical": ours_pk == ref_pk,
            "reference verifies our signature": ref("verify", ours_pk.hex(), message.hex(), ours_sig.hex()) == "true",
            "we verify the reference signature": mldsa.verify(ref_pk, ref_sig, message),
            "reference rejects tampered message": ref("verify", ours_pk.hex(), (message + b"!").hex(), ours_sig.hex())
            == "false",
        }
        print(f"case {i}: " + ", ".join(f"{k}={'ok' if v else 'FAIL'}" for k, v in checks.items()))
        if not all(checks.values()):
            return 1
        vectors.append(
            {
                "seed": seed.hex(),
                "message": message.hex(),
                "public_key": ours_pk.hex(),
                "signature_deterministic": ours_sig.hex(),
                "signature_reference": ref_sig.hex(),
            }
        )
    if args.write_vectors:
        args.write_vectors.write_text(json.dumps({"source": "tools/pqref (near-crypto 0.38.1)", "cases": vectors[:3]}))
        print(f"wrote {args.write_vectors}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
