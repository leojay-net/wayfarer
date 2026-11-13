import base64
import hashlib
import json
import struct
import unittest
from decimal import Decimal
from typing import Any

from tests.fakes import FakeTransport, json_response
from wayfarer.crypto import ed25519
from wayfarer.crypto.base58 import b58encode
from wayfarer.errors import UpstreamError, WayfarerError
from wayfarer.intent import PaymentIntent, Token
from wayfarer.near.events import extract_events, failure_message
from wayfarer.near.keys import KeyPair
from wayfarer.near.rpc import NearRpc
from wayfarer.near.transaction import encode_function_call, sign_transaction
from wayfarer.vault import PaymentStatus, VaultClient

SEED = bytes(range(32))
SECRET = "ed25519:" + b58encode(SEED + ed25519.public_key_from_seed(SEED))
VAULT = "vault.owner.testnet"


def event_log(name: str) -> str:
    return "EVENT_JSON:" + json.dumps({"standard": "wayfarer_vault", "version": "1.0.0", "event": name, "data": {}})


def outcome(*, status: Any = None, events: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "transaction": {"hash": "TXHASH"},
        "status": status or {"SuccessReceiptId": "r"},
        "receipts_outcome": [{"outcome": {"logs": [event_log(e) for e in events] + ["plain log line"]}}],
    }


def panic(message: str) -> dict[str, Any]:
    return {
        "Failure": {
            "ActionError": {
                "index": 0,
                "kind": {"FunctionCallError": {"ExecutionError": f"Smart contract panicked: {message}"}},
            }
        }
    }


class KeyPairTest(unittest.TestCase):
    def test_parses_secret_and_derives_public_key(self) -> None:
        key = KeyPair.from_secret("a.testnet", SECRET)
        self.assertEqual(key.public_key, ed25519.public_key_from_seed(SEED))
        self.assertTrue(key.public_key_str.startswith("ed25519:"))

    def test_repr_never_contains_the_seed(self) -> None:
        self.assertNotIn(SEED.hex(), repr(KeyPair.from_secret("a.testnet", SECRET)))
        self.assertNotIn(SECRET, repr(KeyPair.from_secret("a.testnet", SECRET)))

    def test_rejects_wrong_type_length_and_mismatched_halves(self) -> None:
        for bad in (
            "secp256k1:" + b58encode(SEED),
            "ed25519:" + b58encode(b"short"),
            "ed25519:" + b58encode(SEED + bytes(32)),
        ):
            with self.subTest(bad=bad[:20]), self.assertRaises(WayfarerError):
                KeyPair.from_secret("a.testnet", bad)


class TransactionTest(unittest.TestCase):
    def build(self) -> bytes:
        return encode_function_call(
            signer_id="a.testnet", public_key=b"\x01" * 32, nonce=7, receiver_id="v.a.testnet", block_hash=b"\x02" * 32,
            method="pay_direct", args={"x": 1}, gas=50, deposit=3,
        )  # fmt: skip

    def test_layout(self) -> None:
        tx = self.build()
        self.assertTrue(
            tx.startswith(struct.pack("<I", 9) + b"a.testnet" + b"\x00" + b"\x01" * 32 + struct.pack("<Q", 7))
        )
        action = (
            struct.pack("<I", 1) + b"\x02" + struct.pack("<I", 10) + b"pay_direct" + struct.pack("<I", 7) + b'{"x":1}'
        )
        self.assertIn(action, tx)
        self.assertTrue(tx.endswith(struct.pack("<Q", 50) + (3).to_bytes(16, "little")))

    def test_signature_is_over_the_sha256_of_the_transaction_and_verifies(self) -> None:
        key = KeyPair.from_secret("a.testnet", SECRET)
        tx = self.build()
        signed = sign_transaction(key, tx)
        self.assertEqual(signed[: len(tx)], tx)
        self.assertEqual(signed[len(tx)], 0)  # ed25519 key type
        self.assertTrue(ed25519.verify(key.public_key, signed[len(tx) + 1 :], hashlib.sha256(tx).digest()))


class EventsTest(unittest.TestCase):
    def test_extracts_only_event_json_lines(self) -> None:
        names = [e["event"] for e in extract_events(outcome(events=("payment_authorized", "payment_settled")))]
        self.assertEqual(names, ["payment_authorized", "payment_settled"])

    def test_ignores_malformed_events(self) -> None:
        bad = {"receipts_outcome": [{"outcome": {"logs": ["EVENT_JSON:{nope", 'EVENT_JSON:"str"']}}]}
        self.assertEqual(extract_events(bad), [])

    def test_failure_message_extracts_contract_panic(self) -> None:
        self.assertEqual(failure_message(outcome(status=panic("Over the daily limit"))), "Over the daily limit")
        self.assertIsNone(failure_message(outcome()))
        self.assertIn("Foo", failure_message({"status": {"Failure": {"Foo": 1}}}) or "")


class FakeRpc:
    """Stands in for NearRpc in vault tests."""

    def __init__(self, result: dict[str, Any] | Exception, budget: Any = None) -> None:
        self.result, self.budget_value, self.calls = result, budget, []  # type: ignore[var-annotated]

    def function_call(self, key: KeyPair, receiver: str, method: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((receiver, method, args))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def view_function(self, account: str, method: str, args: dict[str, Any]) -> Any:
        return self.budget_value


class VaultClientTest(unittest.TestCase):
    INTENT = PaymentIntent(Decimal("0.05"), Token.NEAR, "ada.testnet")

    def vault(self, result: dict[str, Any] | Exception, budget: Any = None) -> tuple[VaultClient, FakeRpc]:
        rpc = FakeRpc(result, budget)
        return VaultClient(VAULT, KeyPair.from_secret(VAULT, SECRET), rpc, explorer_base="https://x/"), rpc  # type: ignore[arg-type]

    def pay(self, result: dict[str, Any] | Exception) -> tuple[Any, FakeRpc]:
        vault, rpc = self.vault(result)
        return vault.pay(self.INTENT, commitment="wf1:c", nullifier="wfn:n"), rpc

    def test_paid_only_when_the_contract_emits_settlement(self) -> None:
        result, rpc = self.pay(outcome(events=("payment_authorized", "payment_settled")))
        self.assertEqual((result.status, result.explorer_url), (PaymentStatus.PAID, "https://x/TXHASH"))
        receiver, method, args = rpc.calls[0]
        self.assertEqual((receiver, method), (VAULT, "pay_direct"))
        self.assertEqual(
            args,
            {
                "intent_id": "wfn:n",
                "token_id": "near",
                "to": "ada.testnet",
                "amount": str(5 * 10**22),
                "receipt_hash": "wf1:c",
            },
        )

    def test_contract_refusal(self) -> None:
        result, _ = self.pay(outcome(status=panic("Over the per-transaction limit")))
        self.assertEqual((result.status, result.message), (PaymentStatus.REFUSED, "Over the per-transaction limit"))

    def test_failed_transfer_is_never_reported_as_paid(self) -> None:
        result, _ = self.pay(outcome(events=("payment_authorized", "payment_failed")))
        self.assertEqual(result.status, PaymentStatus.FAILED)

    def test_missing_settlement_event_is_not_paid(self) -> None:
        result, _ = self.pay(outcome(events=("payment_authorized",)))
        self.assertEqual(result.status, PaymentStatus.ERROR)

    def test_network_failure_is_not_paid(self) -> None:
        result, _ = self.pay(UpstreamError("down"))
        self.assertEqual(result.status, PaymentStatus.ERROR)

    def test_unsupported_tokens_send_nothing(self) -> None:
        vault, rpc = self.vault(outcome())
        result = vault.pay(PaymentIntent(Decimal(5), Token.USDC, "ada.near"), commitment="c", nullifier="n")
        self.assertEqual(result.status, PaymentStatus.UNSUPPORTED)
        self.assertEqual(rpc.calls, [])

    def test_key_must_belong_to_the_vault_account(self) -> None:
        with self.assertRaises(WayfarerError):
            VaultClient(VAULT, KeyPair.from_secret("someone.else", SECRET), FakeRpc(outcome()))  # type: ignore[arg-type]

    def test_budget(self) -> None:
        vault, _ = self.vault(outcome(), budget=["300", "700"])
        budget = vault.budget()
        assert budget is not None
        self.assertEqual((budget.spent, budget.remaining), (300, 700))
        self.assertIsNone(self.vault(outcome(), budget=None)[0].budget())


class NearRpcTest(unittest.TestCase):
    def test_function_call_flow_and_wire_format(self) -> None:
        transport = FakeTransport()
        seen: dict[str, Any] = {}

        def rpc(request: Any) -> Any:
            body = json.loads(request.data)
            method, params = body["method"], body["params"]
            if method == "query":
                return json_response({"result": {"nonce": 41}})
            if method == "block":
                return json_response({"result": {"header": {"hash": b58encode(b"\x09" * 32)}}})
            seen.update(params)
            return json_response({"result": outcome(events=("payment_settled",))})

        transport.on("rpc.test", rpc)
        key = KeyPair.from_secret(VAULT, SECRET)
        result = NearRpc("https://rpc.test", transport=transport).function_call(key, VAULT, "pay_direct", {"a": 1})
        self.assertEqual(result["transaction"]["hash"], "TXHASH")
        self.assertEqual(seen["wait_until"], "FINAL")
        signed = base64.b64decode(seen["signed_tx_base64"])
        self.assertIn(struct.pack("<Q", 42), signed)  # nonce = access-key nonce + 1

    def test_rpc_error_becomes_upstream_error(self) -> None:
        transport = FakeTransport()
        transport.on("rpc.test", lambda _r: json_response({"error": {"name": "HANDLER_ERROR"}}))
        with self.assertRaises(UpstreamError):
            NearRpc("https://rpc.test", transport=transport).call("status", [])

    def test_view_function_decodes_bytes(self) -> None:
        transport = FakeTransport()
        transport.on("rpc.test", lambda _r: json_response({"result": {"result": list(b'["1","2"]')}}))
        self.assertEqual(NearRpc("https://rpc.test", transport=transport).view_function("a", "m", {}), ["1", "2"])


if __name__ == "__main__":
    unittest.main()
