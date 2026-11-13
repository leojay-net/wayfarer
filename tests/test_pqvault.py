import base64
import json
import unittest
from typing import Any

from wayfarer.cheque import Cheque, key_id
from wayfarer.errors import UpstreamError, WayfarerError
from wayfarer.near.keys import MlDsaKeyPair, SigningKey
from wayfarer.pqvault import SETTLE_GAS, ChequeRelayer, PqVaultAdmin
from wayfarer.vault import PaymentStatus

VAULT = "vault.test.near"
AGENT = MlDsaKeyPair(VAULT, bytes(range(60, 92)))
RELAYER = MlDsaKeyPair("shop.test.near", bytes(range(120, 152)))
CHEQUE = Cheque(
    vault=VAULT,
    token_id="near",
    to="shop.test.near",
    amount=5 * 10**22,
    intent_id="c1",
    expires_at_ms=4_000_000_000_000,
    receipt_hash="wf1:r",
)


def event(name: str, **data: Any) -> str:
    payload = {"standard": "wayfarer_vault", "version": "1.0.0", "event": name, "data": data}
    return "EVENT_JSON:" + json.dumps(payload)


def outcome(*logs: str, panic: str | None = None) -> dict[str, Any]:
    status: dict[str, Any] = {"SuccessReceiptId": "r"}
    if panic:
        err = {"ExecutionError": f"Smart contract panicked: {panic}"}
        status = {"Failure": {"ActionError": {"index": 0, "kind": {"FunctionCallError": err}}}}
    return {"transaction": {"hash": "TX"}, "status": status, "receipts_outcome": [{"outcome": {"logs": list(logs)}}]}


class FakeRpc:
    def __init__(self, result: dict[str, Any] | Exception, view: Any = None) -> None:
        self.result, self.view, self.calls = result, view, []  # type: ignore[var-annotated]

    def function_call(
        self, key: SigningKey, receiver: str, method: str, args: dict[str, Any], *, gas: int = 0, deposit: int = 0
    ) -> dict[str, Any]:
        self.calls.append((key.account_id, receiver, method, args, gas))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def view_function(self, account: str, method: str, args: dict[str, Any]) -> Any:
        self.calls.append((account, method, args))
        return self.view


class ChequeRelayerTest(unittest.TestCase):
    def relayer(self, result: dict[str, Any] | Exception, view: Any = None) -> tuple[ChequeRelayer, FakeRpc]:
        rpc = FakeRpc(result, view)
        return ChequeRelayer(VAULT, RELAYER, rpc, explorer_base="https://x/"), rpc  # type: ignore[arg-type]

    def test_settled_cheque_is_paid_and_sends_the_contract_arguments(self) -> None:
        relayer, rpc = self.relayer(outcome(event("payment_authorized"), event("payment_settled")))
        result = relayer.settle(CHEQUE, agent_key_id=key_id(AGENT.public_key), signature=b"\x01" * 3309)
        self.assertEqual(
            (result.status, result.tx_hash, result.explorer_url), (PaymentStatus.PAID, "TX", "https://x/TX")
        )
        account, receiver, method, args, gas = rpc.calls[0]
        self.assertEqual((account, receiver, method, gas), ("shop.test.near", VAULT, "pay_with_cheque", SETTLE_GAS))
        self.assertEqual(args["amount"], str(5 * 10**22))
        self.assertEqual(args["expires_at_ms"], "4000000000000")
        self.assertEqual(args["key_id"], key_id(AGENT.public_key))
        self.assertEqual(base64.b64decode(args["signature"]), b"\x01" * 3309)

    def test_contract_refusal_is_reported_not_paid(self) -> None:
        relayer, _ = self.relayer(outcome(panic="Invalid cheque signature"))
        result = relayer.settle(CHEQUE, agent_key_id="k", signature=b"x")
        self.assertEqual((result.status, result.message), (PaymentStatus.REFUSED, "Invalid cheque signature"))

    def test_failed_transfer_is_reported_failed(self) -> None:
        relayer, _ = self.relayer(outcome(event("payment_authorized"), event("payment_failed")))
        self.assertEqual(relayer.settle(CHEQUE, agent_key_id="k", signature=b"x").status, PaymentStatus.FAILED)

    def test_no_settlement_event_is_never_reported_as_paid(self) -> None:
        relayer, _ = self.relayer(outcome(event("payment_authorized")))
        self.assertEqual(relayer.settle(CHEQUE, agent_key_id="k", signature=b"x").status, PaymentStatus.ERROR)

    def test_network_error_is_an_error_status(self) -> None:
        relayer, _ = self.relayer(UpstreamError("down"))
        result = relayer.settle(CHEQUE, agent_key_id="k", signature=b"x")
        self.assertEqual((result.status, result.message), (PaymentStatus.ERROR, "down"))

    def test_cheque_for_another_vault_is_not_sent(self) -> None:
        relayer, rpc = self.relayer(outcome())
        other = Cheque(**{**_fields(CHEQUE), "vault": "other.test.near"})
        with self.assertRaises(WayfarerError):
            relayer.settle(other, agent_key_id="k", signature=b"x")
        self.assertEqual(rpc.calls, [])

    def test_mandate_view(self) -> None:
        raw = {
            "vault": VAULT,
            "owner": "owner.test.near",
            "key_id": "k",
            "paused": False,
            "expires_at_ms": "9",
            "min_interval_ms": "5",
            "last_payment_ms": "0",
            "token_id": "near",
            "per_tx_max": "500",
            "daily_max": "1000",
            "allowlist_enabled": False,
            "allowlist": [],
        }
        relayer, _ = self.relayer(outcome(), view=raw)
        mandate = relayer.mandate("k", "near")
        assert mandate is not None
        self.assertEqual((mandate.per_tx_max, mandate.expires_at_ms, mandate.min_interval_ms), (500, 9, 5))
        relayer, _ = self.relayer(outcome(), view=None)
        self.assertIsNone(relayer.mandate("k", "near"))


class PqVaultAdminTest(unittest.TestCase):
    def admin(self, result: dict[str, Any]) -> tuple[PqVaultAdmin, FakeRpc]:
        rpc = FakeRpc(result)
        return PqVaultAdmin(VAULT, MlDsaKeyPair("owner.test.near", bytes(range(1, 33))), rpc), rpc  # type: ignore[arg-type]

    def test_add_agent_returns_key_id_after_the_vault_confirms_it(self) -> None:
        expected = key_id(AGENT.public_key)
        admin, rpc = self.admin(outcome(event("agent_added", kind="ml-dsa-65", key_id=expected)))
        self.assertEqual(admin.add_agent(AGENT.public_key, expires_at_ms=7, min_interval_ms=3), (expected, "TX"))
        args = rpc.calls[0][3]
        self.assertEqual((args["expires_at_ms"], args["min_interval_ms"]), ("7", "3"))
        self.assertEqual(base64.b64decode(args["public_key"]), AGENT.public_key)

    def test_add_agent_without_confirmation_raises(self) -> None:
        admin, _ = self.admin(outcome())
        with self.assertRaises(WayfarerError):
            admin.add_agent(AGENT.public_key)

    def test_refusals_raise_with_the_contract_message(self) -> None:
        admin, _ = self.admin(outcome(panic="Only the owner can call this"))
        with self.assertRaisesRegex(WayfarerError, "Only the owner"):
            admin.set_policy("near", 1, 2)
        with self.assertRaisesRegex(WayfarerError, "Only the owner"):
            admin.revoke_agent("k")

    def test_set_policy_sends_string_amounts(self) -> None:
        admin, rpc = self.admin(outcome())
        self.assertEqual(admin.set_policy("near", 10**23, 10**24), "TX")
        self.assertEqual(rpc.calls[0][3], {"token_id": "near", "per_tx_max": str(10**23), "daily_max": str(10**24)})


def _fields(cheque: Cheque) -> dict[str, Any]:
    return {f: getattr(cheque, f) for f in cheque.__slots__}


if __name__ == "__main__":
    unittest.main()
