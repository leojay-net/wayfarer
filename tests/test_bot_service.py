import unittest
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from tests.fakes import FakeTransport, chat_payload, json_response, settings
from tests.test_near import SECRET, VAULT, FakeRpc, outcome, panic
from wayfarer.crypto import ed25519
from wayfarer.errors import UpstreamError
from wayfarer.intent import IntentExtractor
from wayfarer.near.keys import KeyPair
from wayfarer.nearai import NearAiClient
from wayfarer.receipts import ReceiptLedger
from wayfarer.telegram.service import PENDING_TTL_S, BotService
from wayfarer.vault import VaultClient

CHAT = 111
INTENT_JSON = '{"action":"send","amount":"0.1","token":"NEAR","recipient":"ada.testnet"}'


class FakeApi:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str, list[tuple[str, str]] | None]] = []
        self.fail_callback_ack = False
        self.fail_download = False
        self.audio = b"AUDIO"

    def send_message(self, chat_id: int, text: str, *, buttons: list[tuple[str, str]] | None = None) -> None:
        self.sent.append((chat_id, text, buttons))

    def answer_callback(self, callback_id: str) -> None:
        if self.fail_callback_ack:
            raise UpstreamError("query is too old")

    def download_file(self, file_id: str) -> bytes:
        if self.fail_download:
            raise RuntimeError("boom")
        return self.audio


class Harness:
    def __init__(
        self, directory: str, *, allowed: int | None = CHAT, vault_result: Any = None, content: str = INTENT_JSON
    ) -> None:
        transport = FakeTransport()
        transport.on("/chat/completions", lambda _r: json_response(chat_payload(content)))
        transport.on(
            "/audio/transcriptions",
            lambda _r: json_response({"text": "send point one near to ada testnet", "duration": 3}),
        )
        seed = bytes(range(1, 33))
        transport.on(
            "/signature/cmpl-1",
            lambda _r: json_response(
                {
                    "text": "t",
                    "signature": ed25519.sign(seed, b"t").hex(),
                    "signing_address": ed25519.public_key_from_seed(seed).hex(),
                    "signing_algo": "ed25519",
                }
            ),
        )
        self.nearai = NearAiClient(settings(), transport=transport)
        self.api = FakeApi()
        self.rpc = FakeRpc(
            vault_result if vault_result is not None else outcome(events=("payment_authorized", "payment_settled"))
        )
        self.vault = VaultClient(VAULT, KeyPair.from_secret(VAULT, SECRET), self.rpc, explorer_base="https://x/")  # type: ignore[arg-type]
        self.ledger = ReceiptLedger(Path(directory, "ledger.jsonl"))
        self.now = 1000.0
        self.service = BotService(
            api=self.api,  # type: ignore[arg-type]
            nearai=self.nearai,
            extractor=IntentExtractor(self.nearai, max_amount=settings().max_amount),
            ledger=self.ledger,
            vault=self.vault,
            allowed_chat_id=allowed,
            clock=lambda: self.now,
            attest=False,
        )

    def text(self, text: str, chat: int = CHAT) -> None:
        self.service.handle_update({"update_id": 1, "message": {"chat": {"id": chat}, "message_id": 5, "text": text}})

    def voice(self, chat: int = CHAT) -> None:
        self.service.handle_update(
            {"update_id": 1, "message": {"chat": {"id": chat}, "message_id": 5, "voice": {"file_id": "f"}}}
        )

    def tap(self, data: str, chat: int = CHAT) -> None:
        self.service.handle_update(
            {"update_id": 2, "callback_query": {"id": "cb", "data": data, "message": {"chat": {"id": chat}}}}
        )

    def last(self) -> str:
        return self.api.sent[-1][1]

    def buttons(self) -> dict[str, str]:
        raw = self.api.sent[-1][2]
        assert raw
        return dict(raw)


def with_harness(
    test: Callable[[unittest.TestCase, Harness], None], **kwargs: Any
) -> Callable[[unittest.TestCase], None]:
    def run(case: unittest.TestCase) -> None:
        with TemporaryDirectory() as directory:
            test(case, Harness(directory, **kwargs))

    return run


class ConfirmationFlowTest(unittest.TestCase):
    def test_typed_instruction_is_read_back_and_nothing_is_paid_before_confirm(self) -> None:
        with TemporaryDirectory() as d:
            h = Harness(d)
            h.text("send 0.1 near to ada.testnet")
            self.assertIn("Send 0.1 NEAR to ada.testnet?", h.last())
            self.assertIn("You typed", h.last())
            self.assertEqual(h.rpc.calls, [])

    def test_voice_note_flow(self) -> None:
        with TemporaryDirectory() as d:
            h = Harness(d)
            h.voice()
            self.assertIn("I heard", h.last())
            h.tap(h.buttons()["Confirm"])
            self.assertIn("Paid 0.1 NEAR to ada.testnet", h.last())
            self.assertIn("https://x/TXHASH", h.last())
            self.assertEqual(len(h.rpc.calls), 1)

    def test_commitment_and_nullifier_not_chat_derived_go_on_chain(self) -> None:
        with TemporaryDirectory() as d:
            h = Harness(d)
            h.text("send 0.1 near to ada.testnet")
            h.tap(h.buttons()["Confirm"])
            args = h.rpc.calls[0][2]
            self.assertTrue(args["receipt_hash"].startswith("wf1:"))
            self.assertTrue(args["intent_id"].startswith("wfn:"))
            self.assertNotIn(str(CHAT), args["intent_id"])
            opening = h.ledger.find_opening(args["intent_id"])
            self.assertEqual(opening.commitment, args["receipt_hash"])

    def test_cancel(self) -> None:
        with TemporaryDirectory() as d:
            h = Harness(d)
            h.text("send 0.1 near to ada.testnet")
            h.tap(h.buttons()["Cancel"])
            self.assertIn("Cancelled", h.last())
            self.assertEqual(h.rpc.calls, [])

    def test_a_button_can_only_be_used_once(self) -> None:
        with TemporaryDirectory() as d:
            h = Harness(d)
            h.text("send 0.1 near to ada.testnet")
            confirm = h.buttons()["Confirm"]
            h.tap(confirm)
            h.tap(confirm)
            self.assertIn("out of date", h.last())
            self.assertEqual(len(h.rpc.calls), 1)

    def test_an_old_button_cannot_confirm_a_newer_instruction(self) -> None:
        with TemporaryDirectory() as d:
            h = Harness(d)
            h.text("send 0.1 near to ada.testnet")
            old = h.buttons()["Confirm"]
            h.text("send 0.1 near to ada.testnet")
            h.tap(old)
            self.assertIn("out of date", h.last())
            self.assertEqual(h.rpc.calls, [])

    def test_pending_instruction_expires(self) -> None:
        with TemporaryDirectory() as d:
            h = Harness(d)
            h.text("send 0.1 near to ada.testnet")
            confirm = h.buttons()["Confirm"]
            h.now += PENDING_TTL_S + 1
            h.tap(confirm)
            self.assertIn("expired", h.last())
            self.assertEqual(h.rpc.calls, [])

    def test_stale_callback_acknowledgement_does_not_block_payment(self) -> None:
        with TemporaryDirectory() as d:
            h = Harness(d)
            h.api.fail_callback_ack = True
            h.text("send 0.1 near to ada.testnet")
            h.tap(h.buttons()["Confirm"])
            self.assertIn("Paid", h.last())


class AuthorisationTest(unittest.TestCase):
    def test_other_chats_are_ignored_and_can_never_pay(self) -> None:
        with TemporaryDirectory() as d:
            h = Harness(d)
            h.text("send 0.1 near to ada.testnet", chat=999)
            h.tap("ok:wfn:whatever", chat=999)
            self.assertEqual(h.api.sent, [])
            self.assertEqual(h.rpc.calls, [])

    def test_without_an_allowed_chat_nothing_is_ever_paid(self) -> None:
        with TemporaryDirectory() as d:
            h = Harness(d, allowed=None)
            h.text("send 0.1 near to ada.testnet")
            h.tap(h.buttons()["Confirm"])
            self.assertEqual(h.rpc.calls, [])

    def test_start_shows_the_chat_id_to_anyone(self) -> None:
        with TemporaryDirectory() as d:
            h = Harness(d)
            h.text("/start", chat=999)
            self.assertIn("999", h.last())


class FailureHandlingTest(unittest.TestCase):
    def test_contract_refusal_is_reported_with_link(self) -> None:
        with TemporaryDirectory() as d:
            h = Harness(d, vault_result=outcome(status=panic("Over the per-transaction limit")))
            h.text("send 0.1 near to ada.testnet")
            h.tap(h.buttons()["Confirm"])
            self.assertIn("Refused by the vault contract: Over the per-transaction limit", h.last())
            self.assertIn("Nothing was sent", h.last())

    def test_usdc_is_reported_unsupported(self) -> None:
        with TemporaryDirectory() as d:
            h = Harness(d, content='{"action":"send","amount":"5","token":"USDC","recipient":"ada.near"}')
            h.text("send 5 usdc to ada.near")
            h.tap(h.buttons()["Confirm"])
            self.assertIn("NO payment", h.last())
            self.assertEqual(h.rpc.calls, [])

    def test_unparseable_instruction_gets_a_clear_message(self) -> None:
        with TemporaryDirectory() as d:
            h = Harness(d, content='{"action":"none"}')
            h.text("what is the weather")
            self.assertIn("did not sound like a payment", h.last())
            self.assertIn("Nothing was sent", h.last())

    def test_unexpected_exception_is_reported_not_swallowed(self) -> None:
        with TemporaryDirectory() as d:
            h = Harness(d)
            h.api.fail_download = True
            with self.assertLogs("wayfarer.bot", level="ERROR"):
                h.voice()
            self.assertIn("something went wrong (RuntimeError)", h.last())

    def test_errors_are_not_sent_to_unauthorised_chats(self) -> None:
        with TemporaryDirectory() as d:
            h = Harness(d, content="garbage")
            h.text("hello", chat=999)
            self.assertEqual(h.api.sent, [])

    def test_transcripts_are_not_logged(self) -> None:
        with TemporaryDirectory() as d, self.assertLogs("wayfarer.bot", level="INFO") as logs:
            h = Harness(d)
            h.voice()
            self.assertNotIn("ada", "\n".join(logs.output).lower())


if __name__ == "__main__":
    unittest.main()
