#!/usr/bin/env python3
"""Settle a cheque that the Wayfarer Tap merchant app accepted, on the TESTNET vault.

    python3 scripts/settle_cheque.py accepted.json --check-only        # parse and verify offline, nothing is sent
    python3 scripts/settle_cheque.py accepted.json --suffix 9bda7c     # strong-check the vault, then settle on testnet
    python3 scripts/settle_cheque.py accepted.json --suffix 9bda7c --save out/ --revoke
                                                                       # also write a checked receipt, then revoke the key

``accepted.json`` is what the app's "Share the accepted cheque" button produces: ``request_hex`` (the merchant's
PaymentRequest) and ``envelope_hex`` (the buyer's signed cheque). The cheque is rebuilt from the request, so nothing the
buyer sent can change what is paid. The merchant phone does not submit transactions itself in this prototype; this script
does, with the merchant account's ML-DSA key from ~/.near-credentials/wayfarer-pq/.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from wayfarer.cheque import verify_cheque  # noqa: E402
from wayfarer.mandate import issue_passport  # noqa: E402
from wayfarer.near.keys import MlDsaKeyPair  # noqa: E402
from wayfarer.near.rpc import NearRpc  # noqa: E402
from wayfarer.pqvault import ChequeRelayer, PqVaultAdmin, VaultChainView  # noqa: E402
from wayfarer.proof import Settlement, build_receipt, verify_receipt_offline, verify_receipt_on_chain  # noqa: E402
from wayfarer.tap.envelope import ChequeEnvelope, cheque_for_request  # noqa: E402
from wayfarer.tap.merchant import AcceptedCheque  # noqa: E402
from wayfarer.tap.request import PaymentRequest  # noqa: E402

PQ_DIR = Path.home() / ".near-credentials" / "wayfarer-pq"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("file", type=Path)
    parser.add_argument("--suffix", help="the wfpq-*-SUFFIX.testnet accounts; required unless --check-only")
    parser.add_argument("--check-only", action="store_true", help="verify offline and stop")
    parser.add_argument("--save", type=Path, help="after a paid settlement, write a checked receipt to this folder")
    parser.add_argument(
        "--revoke", action="store_true", help="after settling, revoke the buyer's key (owner key needed)"
    )
    args = parser.parse_args()

    data = json.loads(args.file.read_text())
    request = PaymentRequest.decode(bytes.fromhex(data["request_hex"]))
    envelope = ChequeEnvelope.decode(bytes.fromhex(data["envelope_hex"]))
    if envelope.request_hash != request.request_hash():
        print("REFUSED: the cheque is for a different request.")
        return 1
    if envelope.public_key is None or hashlib.sha256(envelope.public_key).digest() != envelope.key_id:
        print("REFUSED: the cheque has no valid public key.")
        return 1
    cheque = cheque_for_request(request, envelope.vault)
    if not verify_cheque(envelope.public_key, envelope.signature, cheque):
        print("REFUSED: the signature is not valid for this request.")
        return 1
    print(f"Offline check OK: {request.fiat_display()} = {cheque.amount} yocto{cheque.token_id.upper()} to {cheque.to}")
    print(f"  vault {cheque.vault}, key {envelope.key_id.hex()[:16]}..., intent {cheque.intent_id}")
    if args.check_only:
        return 0
    if not args.suffix or not cheque.vault.endswith(".testnet"):
        print("Refusing to submit: pass --suffix and use a .testnet vault (testnet only).")
        return 1

    rpc = NearRpc("https://rpc.testnet.near.org")
    shop_key = MlDsaKeyPair.from_file(PQ_DIR / f"wfpq-shop-{args.suffix}.testnet.json")
    if shop_key.account_id != request.merchant_account:
        print(
            f"Refusing: the relayer key belongs to {shop_key.account_id}, the cheque pays {request.merchant_account}."
        )
        return 1
    relayer = ChequeRelayer(cheque.vault, shop_key, rpc)
    problems = []
    terms = VaultChainView(cheque.vault, rpc).mandate(envelope.key_id.hex(), cheque.token_id)
    if terms is None:
        problems.append("the vault does not know this key")
    if problems:
        print("Strong check failed: " + "; ".join(problems))
        return 1
    result = relayer.settle(cheque, agent_key_id=envelope.key_id.hex(), signature=envelope.signature)
    print(f"{result.status.value.upper()}: {result.message} {result.explorer_url or ''}")
    if result.status.value != "paid":
        return 1
    if args.save or args.revoke:
        owner = MlDsaKeyPair.from_file(PQ_DIR / f"wfpq-owner-{args.suffix}.testnet.json")
        assert result.tx_hash is not None
        if args.save:
            accepted = AcceptedCheque(request, cheque, envelope.key_id.hex(), envelope.signature, envelope.public_key)
            live = relayer.mandate(accepted.key_id, cheque.token_id)
            assert live is not None
            passport = issue_passport(
                network="testnet", terms=live, owner_key=owner, owner_account=owner.account_id,
                issued_at_ms=int(time.time() * 1000),
            )  # fmt: skip
            receipt = build_receipt(
                network="testnet", accepted=accepted, passport=passport,
                settlement=Settlement(result.tx_hash, shop_key.account_id, result.explorer_url or ""),
            )  # fmt: skip
            outcome = rpc.transaction_status(result.tx_hash, shop_key.account_id)
            owner_keys = [k["public_key"] for k in rpc.access_keys(owner.account_id)]
            checks = verify_receipt_offline(receipt) + verify_receipt_on_chain(receipt, outcome, owner_keys)
            for item in checks:
                print(f"  [{'ok' if item.ok else 'FAIL'}] {item.name}")
            args.save.mkdir(parents=True, exist_ok=True)
            (args.save / "receipt.json").write_text(json.dumps(receipt, indent=1) + "\n")
            print(f"Receipt saved to {args.save / 'receipt.json'} (open verifier/index.html and paste it)")
        if args.revoke:
            tx = PqVaultAdmin(cheque.vault, owner, rpc).revoke_agent(envelope.key_id.hex())
            print(f"Buyer key revoked: https://explorer.testnet.near.org/transactions/{tx}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
