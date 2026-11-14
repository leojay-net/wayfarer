#!/usr/bin/env python3
"""Two simulated phones tap in one process: price in fiat, buyer confirms, signed ML-DSA-65 cheque back, merchant accepts.

    python3 scripts/tap_demo.py                       # offline: no network, no keys, nothing on any chain
    python3 scripts/tap_demo.py --amount 3 --testnet 9bda7c
                                                      # also settle the cheque on the TESTNET vault from docs/TESTNET-PQ.md
                                                      # (needs the ML-DSA seeds in ~/.near-credentials/wayfarer-pq/)

What is real: the protocol messages, the ML-DSA-65 signature, the QR-frame transfer, and (with --testnet) the on-chain
settlement on testnet. What is SIMULATED: both phones (one process, no NFC/Bluetooth/camera), the rate (a demo
fiat-per-NEAR you can set with --rate), the buyer's biometric confirmation (auto-yes), and the cash-out at the end
(SandboxSwapLeg + SandboxRail: no swap, no bank, no money). Testnet NEAR has no value.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from wayfarer.cashout.legs import SandboxSwapLeg  # noqa: E402
from wayfarer.cashout.rails import BankAccount, SandboxRail  # noqa: E402
from wayfarer.cashout.service import CashOutService  # noqa: E402
from wayfarer.mandate import issue_passport  # noqa: E402
from wayfarer.near.keys import MlDsaKeyPair  # noqa: E402
from wayfarer.near.rpc import NearRpc  # noqa: E402
from wayfarer.pqvault import ChequeRelayer, PqVaultAdmin, VaultChainView  # noqa: E402
from wayfarer.proof import Settlement, build_receipt, verify_receipt_offline, verify_receipt_on_chain  # noqa: E402
from wayfarer.tap.buyer import ApprovalPrompt, BuyerWallet, TapLimits  # noqa: E402
from wayfarer.tap.link import FrameAssembler, encode_frames  # noqa: E402
from wayfarer.tap.merchant import MerchantTerminal  # noqa: E402
from wayfarer.tap.rates import FixedRateSource, RateDesk  # noqa: E402
from wayfarer.tap.request import PaymentRequest  # noqa: E402

NEAR = 10**24
PQ_DIR = Path.home() / ".near-credentials" / "wayfarer-pq"
EXPLORER = "https://explorer.testnet.near.org/transactions/"


class AutoYes:
    """Stands in for the biometric prompt. A real phone must ask the human."""

    def confirm(self, prompt: ApprovalPrompt) -> bool:
        print("  [buyer phone shows]")
        for line in prompt.lines():
            print(f"    {line}")
        print("  [buyer confirms: SIMULATED biometric]")
        return True


def clock_ms() -> int:
    return int(time.time() * 1000)


def transfer(label: str, payload: bytes, session: bytes, lose: tuple[int, ...] = ()) -> bytes:
    frames = encode_frames(payload, session)
    assembler = FrameAssembler()
    for index, frame in enumerate(frames):
        if index not in lose:
            assembler.add(frame)
    if lose:
        print(
            f"  {label}: {len(payload)} bytes in {len(frames)} QR frames; frames {list(lose)} missed on the first pass"
        )
        for index in lose:
            assembler.add(frames[index])
    else:
        print(f"  {label}: {len(payload)} bytes in {len(frames)} QR frame(s)")
    return assembler.payload()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--amount", type=int, default=5, help="sale amount in whole units of --currency")
    parser.add_argument("--currency", default="USD", help="ISO 4217 code of the sale currency")
    parser.add_argument("--rate", default="10", help="DEMO fiat units per NEAR")
    parser.add_argument("--fiat-per-usd", default="1", help="DEMO fiat units per USD, used by the sandbox cash-out")
    parser.add_argument("--testnet", metavar="SUFFIX", help="settle on the wfpq-*-SUFFIX.testnet vault")
    parser.add_argument("--save", type=Path, help="with --testnet: write receipt.json and outcome.json to this folder")
    args = parser.parse_args()

    vault = f"wfpq-vault-{args.testnet}.testnet" if args.testnet else "buyer-vault.testnet"
    shop = f"wfpq-shop-{args.testnet}.testnet" if args.testnet else "cafe-shop.testnet"
    buyer_key = MlDsaKeyPair.generate(vault)
    rpc = NearRpc("https://rpc.testnet.near.org")
    owner_admin = None
    owner: MlDsaKeyPair | None = None
    if args.testnet:
        owner = MlDsaKeyPair.from_file(PQ_DIR / f"wfpq-owner-{args.testnet}.testnet.json")
        owner_admin = PqVaultAdmin(vault, owner, rpc)
        key_id, tx = owner_admin.add_agent(buyer_key.public_key, expires_at_ms=clock_ms() + 3_600_000)
        print(f"Setup (testnet): owner registered a fresh buyer key {key_id[:16]}...: {EXPLORER}{tx}")

    desk = RateDesk(FixedRateSource({(args.currency, "near"): Decimal(args.rate)}), clock_ms)
    merchant = MerchantTerminal(account=shop, name="Sam's Cafe", rates=desk, clock_ms=clock_ms)
    buyer = BuyerWallet(
        vault=vault, agent_key=buyer_key, confirmer=AutoYes(), limits=TapLimits({"near": NEAR // 2}), clock_ms=clock_ms
    )

    print(f"1. Merchant prices {args.currency} {args.amount:,} at a DEMO rate of {args.currency} {args.rate}/NEAR (60 s lock)")
    request = merchant.create_request(args.amount * 100, fiat_currency=args.currency)
    print(
        f"   -> {request.asset_amount / NEAR:.6f} NEAR to {request.merchant_account}, request hash {request.request_hash().hex()[:16]}..."
    )

    print("2. Tap: request goes to the buyer's phone")
    received = PaymentRequest.decode(transfer("request", request.message(), b"\x01\x01\x01\x01"))

    print("3. Buyer reviews, confirms, signs an ML-DSA-65 cheque")
    started = time.perf_counter()
    envelope = buyer.pay(received)
    print(
        f"   signing + envelope took {time.perf_counter() - started:.2f} s in pure Python on this computer (no phone measurement)"
    )

    print("4. Cheque goes back; merchant checks it OFFLINE")
    raw = transfer("cheque", envelope.encode(), b"\x02\x02\x02\x02", lose=(1, 4))
    accepted = merchant.accept(raw)
    print(
        f"   ACCEPTED, PENDING SETTLEMENT (not paid yet): {accepted.cheque.amount / NEAR:.6f} NEAR, key {accepted.key_id[:16]}..."
    )

    if args.testnet:
        print("5. Merchant strong-checks the vault, then settles on TESTNET")
        check = merchant.strong_check(accepted, VaultChainView(vault, rpc))
        print(f"   strong check: {'ok' if check.ok else 'PROBLEMS: ' + '; '.join(check.problems)}")
        relayer = ChequeRelayer(vault, MlDsaKeyPair.from_file(PQ_DIR / f"wfpq-shop-{args.testnet}.testnet.json"), rpc)
        result = relayer.settle(accepted.cheque, agent_key_id=accepted.key_id, signature=accepted.signature)
        print(f"   settlement: {result.status.value.upper()} | {result.message} | {result.explorer_url}")
        if result.status.value != "paid":
            return 1
        print("6. Receipt: Mandate Passport + approval receipt, verified offline and against the chain")
        terms = relayer.mandate(accepted.key_id, "near")
        assert terms is not None
        assert result.tx_hash is not None
        assert owner is not None
        passport = issue_passport(
            network="testnet", terms=terms, owner_key=owner, owner_account=owner.account_id, issued_at_ms=clock_ms()
        )
        receipt = build_receipt(
            network="testnet",
            accepted=accepted,
            passport=passport,
            settlement=Settlement(result.tx_hash, shop, result.explorer_url or ""),
        )
        tx_outcome = rpc.transaction_status(result.tx_hash, shop)
        owner_keys = [k["public_key"] for k in rpc.access_keys(owner.account_id)]
        for item in verify_receipt_offline(receipt) + verify_receipt_on_chain(receipt, tx_outcome, owner_keys):
            print(f"   [{'ok' if item.ok else 'FAIL'}] {item.name}")
        if args.save:
            args.save.mkdir(parents=True, exist_ok=True)
            (args.save / "receipt.json").write_text(json.dumps(receipt, indent=1) + "\n")
            (args.save / "outcome.json").write_text(json.dumps(tx_outcome, indent=1) + "\n")
            (args.save / "owner-keys.json").write_text(json.dumps(owner_keys) + "\n")
            print(f"   saved receipt.json, outcome.json, owner-keys.json in {args.save}")
        if owner_admin is not None:
            print(f"   buyer key revoked after the demo: {EXPLORER}{owner_admin.revoke_agent(accepted.key_id)}")
    else:
        print("5. Settlement skipped (offline run). Use --testnet SUFFIX to settle on the testnet vault.")

    print("7. Merchant cash-out, SANDBOX ONLY (no swap, no bank, no money)")
    with tempfile.TemporaryDirectory() as tmp:
        service = CashOutService(
            SandboxSwapLeg(usd_per_near=Decimal(args.rate) / Decimal(args.fiat_per_usd)),  # consistent with the demo rate
            SandboxRail(Path(tmp) / "ledger.json", fiat_per_usd=Decimal(args.fiat_per_usd), currency=args.currency),
        )
        quote = service.quote(
            reference=request.intent_id()[:40],
            amount_in=accepted.cheque.amount,
            bank=BankAccount("021", "0123456789", "Sam Carter"),
            refund_to=shop,
        )
        print("   " + quote.read_back())
        cash_out = service.execute(quote)
        print(f"   {cash_out.status.value}: {cash_out.message}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
