#!/usr/bin/env python3
"""Quantum-safe vault on NEAR TESTNET: rotate accounts to ML-DSA-65 keys, set the mandate, pay with cheques.

TESTNET ONLY, throwaway accounts, prototype, unaudited. Spends only free testnet NEAR; no NEAR AI Cloud credit.

Prerequisites (done once with the NEAR CLI, see docs/TESTNET-PQ.md): three faucet-funded accounts
``wfpq-owner-<suffix>.testnet``, ``wfpq-vault-<suffix>.testnet`` (contract v2 deployed and initialised with the owner)
and ``wfpq-shop-<suffix>.testnet``, each with its ed25519 key in ~/.near-credentials/testnet/.

    python3 scripts/pq_testnet.py --suffix 9bda7c rotate     # ML-DSA full-access key in, ed25519 key out (all 3 accounts)
    python3 scripts/pq_testnet.py --suffix 9bda7c setup      # policy + register the buyer's ML-DSA agent key
    python3 scripts/pq_testnet.py --suffix 9bda7c pay        # one cheque, settled by the shop's account
    python3 scripts/pq_testnet.py --suffix 9bda7c refusals   # replay, over cap, forged amount, expired, revoked
    python3 scripts/pq_testnet.py --suffix 9bda7c interval   # a second key with a 120 s minimum interval
    python3 scripts/pq_testnet.py --suffix 9bda7c report     # access keys of every account, as the chain lists them

ML-DSA seeds are written (mode 600) to ~/.near-credentials/wayfarer-pq/, never into the repository.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from wayfarer.cheque import Cheque, key_id, sign_cheque  # noqa: E402
from wayfarer.near.keys import KeyPair, MlDsaKeyPair  # noqa: E402
from wayfarer.near.rpc import NearRpc  # noqa: E402
from wayfarer.near.transaction import action_add_full_access_key, action_delete_key  # noqa: E402
from wayfarer.pqvault import ChequeRelayer, PqVaultAdmin  # noqa: E402

NEAR = 10**24
CRED = Path.home() / ".near-credentials"
PQ_DIR = CRED / "wayfarer-pq"
EXPLORER = "https://explorer.testnet.near.org/transactions/"
PER_TX = NEAR // 2  # 0.5 NEAR
DAILY = NEAR  # 1 NEAR
CHEQUE_AMOUNT = NEAR // 20  # 0.05 NEAR
INTERVAL_MS = 5_000
MANDATE_DAYS = 7


class Names:
    def __init__(self, suffix: str) -> None:
        self.owner = f"wfpq-owner-{suffix}.testnet"
        self.vault = f"wfpq-vault-{suffix}.testnet"
        self.shop = f"wfpq-shop-{suffix}.testnet"
        self.buyer_file = PQ_DIR / f"buyer-{suffix}.json"


def save_ml_key(path: Path, key: MlDsaKeyPair) -> None:
    PQ_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    body = {"account_id": key.account_id, "public_key": key.public_key_str, "private_key": key.secret_str()}
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(json.dumps(body))


def load_or_create_ml_key(account: str, path: Path) -> MlDsaKeyPair:
    if path.exists():
        return MlDsaKeyPair.from_file(path)
    key = MlDsaKeyPair.generate(account)
    save_ml_key(path, key)
    return key


def ed_key(account: str) -> KeyPair:
    data = json.loads((CRED / "testnet" / f"{account}.json").read_text())
    return KeyPair.from_secret(account, data["private_key"])


def tx_line(label: str, outcome: dict[str, object]) -> None:
    tx = outcome["transaction"]
    assert isinstance(tx, dict)
    print(f"  {label}: {EXPLORER}{tx['hash']}")


def key_summary(rpc: NearRpc, account: str) -> list[str]:
    rows = []
    for item in rpc.access_keys(account):
        permission = item["access_key"]["permission"]
        kind = "full access" if permission == "FullAccess" else "function call"
        rows.append(f"{item['public_key'][:40]}...  ({kind})")
    return rows


def stage_rotate(rpc: NearRpc, names: Names) -> None:
    for account in (names.owner, names.vault, names.shop):
        ml = load_or_create_ml_key(account, PQ_DIR / f"{account}.json")
        classic_path = CRED / "testnet" / f"{account}.json"
        keys = [k["public_key"] for k in rpc.access_keys(account)]
        if ml.public_key_handle_str not in keys and ml.public_key_str not in keys:
            classic = ed_key(account)
            outcome = rpc.send_actions(classic, account, [action_add_full_access_key(ml.key_type, ml.public_key)])
            tx_line(f"{account}: AddKey ML-DSA-65 full access (signed by ed25519)", outcome)
        if classic_path.exists():
            classic = ed_key(account)
            if classic.public_key_str in [k["public_key"] for k in rpc.access_keys(account)]:
                outcome = rpc.send_actions(ml, account, [action_delete_key(classic.key_type, classic.public_key)])
                tx_line(f"{account}: DeleteKey ed25519 (signed by the ML-DSA key)", outcome)
        print(f"{account} keys now:")
        for row in key_summary(rpc, account):
            print(f"    {row}")


def owner_key(names: Names) -> MlDsaKeyPair:
    return MlDsaKeyPair.from_file(PQ_DIR / f"{names.owner}.json")


def shop_key(names: Names) -> MlDsaKeyPair:
    return MlDsaKeyPair.from_file(PQ_DIR / f"{names.shop}.json")


def stage_setup(rpc: NearRpc, names: Names) -> None:
    admin = PqVaultAdmin(names.vault, owner_key(names), rpc)
    print("set_policy near:", EXPLORER + admin.set_policy("near", PER_TX, DAILY))
    buyer = load_or_create_ml_key(names.vault, names.buyer_file)
    ends = int((time.time() + MANDATE_DAYS * 86400) * 1000)
    agent_id, tx = admin.add_agent(buyer.public_key, expires_at_ms=ends, min_interval_ms=INTERVAL_MS)
    print(f"add_pq_agent key_id={agent_id}: {EXPLORER}{tx}")


def balance(rpc: NearRpc, account: str) -> int:
    return int(
        rpc.call("query", {"request_type": "view_account", "finality": "final", "account_id": account})["amount"]
    )


def make_cheque(names: Names, intent: str, *, amount: int = CHEQUE_AMOUNT, ttl_s: int = 120) -> Cheque:
    return Cheque(
        vault=names.vault,
        token_id="near",  # noqa: S106 (a contract token id, not a credential)
        to=names.shop,
        amount=amount,
        intent_id=intent,
        expires_at_ms=int((time.time() + ttl_s) * 1000),
        receipt_hash=f"wf1:testnet-{intent}",
    )


def settle(
    rpc: NearRpc,
    names: Names,
    label: str,
    cheque: Cheque,
    signature: bytes | None = None,
    buyer: MlDsaKeyPair | None = None,
) -> str:
    buyer = buyer or MlDsaKeyPair.from_file(names.buyer_file)
    relayer = ChequeRelayer(names.vault, shop_key(names), rpc, explorer_base=EXPLORER)
    sig = signature if signature is not None else sign_cheque(buyer, cheque)
    result = relayer.settle(cheque, agent_key_id=key_id(buyer.public_key), signature=sig)
    print(f"  {label}: {result.status.value.upper()} | {result.message} | {result.explorer_url}")
    return result.status.value


def stage_pay(rpc: NearRpc, names: Names) -> None:
    before = balance(rpc, names.shop)
    status = settle(
        rpc,
        names,
        "0.05 NEAR cheque, buyer -> shop, submitted by the shop",
        make_cheque(names, f"tap-{int(time.time())}"),
    )
    after = balance(rpc, names.shop)
    print(f"  shop balance change (includes the shop's own gas): {(after - before) / NEAR:+.6f} NEAR; status {status}")


def stage_refusals(rpc: NearRpc, names: Names) -> None:
    buyer = MlDsaKeyPair.from_file(names.buyer_file)
    base = f"r{int(time.time())}"
    first = make_cheque(names, f"{base}a")
    print("1. a valid cheque")
    settle(rpc, names, "first (expected PAID)", first)
    print("2. replay of the same cheque")
    settle(rpc, names, "replay (expected REFUSED: intent_id already used)", first, sign_cheque(buyer, first))
    print("3. validly signed but over the 0.5 NEAR per-payment cap (0.6 NEAR)")
    settle(
        rpc,
        names,
        "0.6 NEAR (expected REFUSED: per-transaction limit)",
        make_cheque(names, f"{base}c", amount=PER_TX + NEAR // 10),
    )
    print("4. signature made for 0.05 NEAR presented for 0.5 NEAR (tampered amount)")
    honest = make_cheque(names, f"{base}d")
    tampered = Cheque(**{**{f: getattr(honest, f) for f in honest.__slots__}, "amount": PER_TX})
    settle(rpc, names, "tampered (expected REFUSED: Invalid cheque signature)", tampered, sign_cheque(buyer, honest))
    print("5. cheque already past its own expiry")
    stale = make_cheque(names, f"{base}e", ttl_s=-30)
    settle(rpc, names, "stale (expected REFUSED: Cheque has expired)", stale)
    print("6. owner revokes the buyer's key; a fresh valid cheque is then refused")
    admin = PqVaultAdmin(names.vault, owner_key(names), rpc)
    print("  revoke:", EXPLORER + admin.revoke_agent(key_id(buyer.public_key)))
    settle(rpc, names, "after revoke (expected REFUSED: Unknown agent key)", make_cheque(names, f"{base}f"))


def stage_interval(rpc: NearRpc, names: Names) -> None:
    """Finality takes several seconds, so a short interval cannot be shown; use a long one with its own key."""
    admin = PqVaultAdmin(names.vault, owner_key(names), rpc)
    buyer = load_or_create_ml_key(names.vault, PQ_DIR / f"buyer2-{names.vault}.json")
    agent_id, tx = admin.add_agent(buyer.public_key, expires_at_ms=0, min_interval_ms=120_000)
    print(f"add_pq_agent (min interval 120 s, no end date) key_id={agent_id}: {EXPLORER}{tx}")
    base = f"i{int(time.time())}"
    settle(rpc, names, "first (expected PAID)", make_cheque(names, f"{base}a"), buyer=buyer)
    settle(
        rpc,
        names,
        "second right after (expected REFUSED: minimum interval)",
        make_cheque(names, f"{base}b"),
        buyer=buyer,
    )
    mandate = ChequeRelayer(names.vault, shop_key(names), rpc).mandate(agent_id, "near")
    print(f"  get_mandate: {mandate}")
    print("  revoke:", EXPLORER + admin.revoke_agent(agent_id))


def stage_report(rpc: NearRpc, names: Names) -> None:
    for account in (names.owner, names.vault, names.shop):
        info = rpc.call("query", {"request_type": "view_account", "finality": "final", "account_id": account})
        print(f"{account}: balance {int(info['amount']) / NEAR:.4f} NEAR, code {str(info['code_hash'])[:10]}")
        for row in key_summary(rpc, account):
            print(f"    key {row}")
    ml = shop_key(names)
    print(f"shop ML-DSA key handle expected on chain: {ml.public_key_handle_str}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--suffix", required=True, help="the suffix of the three wfpq-* testnet accounts")
    parser.add_argument("stage", choices=["rotate", "setup", "pay", "refusals", "interval", "report"])
    args = parser.parse_args()
    rpc = NearRpc("https://rpc.testnet.near.org")
    names = Names(args.suffix)
    stages = {
        "rotate": stage_rotate,
        "setup": stage_setup,
        "pay": stage_pay,
        "refusals": stage_refusals,
        "interval": stage_interval,
        "report": stage_report,
    }
    stages[args.stage](rpc, names)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
