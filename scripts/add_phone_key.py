#!/usr/bin/env python3
"""Register a phone's ML-DSA-65 public key on the TESTNET vault as a tap agent key.

    python3 scripts/add_phone_key.py --suffix 9bda7c --public-key-hex <hex from the app> --hours 24

The Wayfarer Tap app (buyer) shares ``key_id`` and ``public_key_hex``. The vault owner's ML-DSA seed must be in
~/.near-credentials/wayfarer-pq/ (see docs/TESTNET-PQ.md). Testnet only; the key can be revoked with revoke_pq_agent.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from wayfarer.cheque import key_id  # noqa: E402
from wayfarer.near.keys import MlDsaKeyPair  # noqa: E402
from wayfarer.near.rpc import NearRpc  # noqa: E402
from wayfarer.pqvault import PqVaultAdmin  # noqa: E402

PQ_DIR = Path.home() / ".near-credentials" / "wayfarer-pq"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--suffix", required=True, help="the wfpq-*-SUFFIX.testnet accounts")
    parser.add_argument("--public-key-hex", required=True, help="1,952-byte ML-DSA-65 public key, hex")
    parser.add_argument("--hours", type=int, default=24, help="how long the key may be used (0 = no end date)")
    args = parser.parse_args()

    public_key = bytes.fromhex(args.public_key_hex.strip())
    vault = f"wfpq-vault-{args.suffix}.testnet"
    owner = MlDsaKeyPair.from_file(PQ_DIR / f"wfpq-owner-{args.suffix}.testnet.json")
    expires = int(time.time() * 1000) + args.hours * 3_600_000 if args.hours else 0
    print(f"Registering key {key_id(public_key)} on {vault} (testnet), valid {args.hours or 'unlimited'} hours")
    admin = PqVaultAdmin(vault, owner, NearRpc("https://rpc.testnet.near.org"))
    registered, tx = admin.add_agent(public_key, expires_at_ms=expires)
    print(f"Done: {registered[:16]}... https://explorer.testnet.near.org/transactions/{tx}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
