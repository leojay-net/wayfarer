import unittest
from typing import Any

from tests.fakes import FakeTransport, json_response
from tests.test_near import SECRET, VAULT, FakeRpc, outcome, panic
from wayfarer.errors import UpstreamError, WayfarerError
from wayfarer.near.keys import KeyPair
from wayfarer.oneclick import USDC_BASE, WNEAR, OneClick, QuoteRequest
from wayfarer.swap import IntentsSettlement, SwapOutcome
from wayfarer.vault import VaultClient

AMOUNT = 10**24
REQUEST = QuoteRequest(WNEAR, USDC_BASE, str(AMOUNT), "0xabc", "refund.near")
QUOTE = {
    "amountInFormatted": "1.0",
    "amountOutFormatted": "3.4",
    "minAmountOut": 3366000,
    "timeEstimate": 24,
    "depositAddress": "ab" * 32,
}


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def build(
    statuses: list[Any], vault_result: dict[str, Any] | Exception | None = None
) -> tuple[IntentsSettlement, FakeTransport, FakeRpc]:
    transport = FakeTransport()
    transport.on("/v0/quote", lambda _r: json_response({"quote": QUOTE}, 201))
    transport.on("/v0/deposit/submit", lambda _r: json_response({}))
    queue = list(statuses)
    transport.on("/v0/status", lambda _r: queue.pop(0) if len(queue) > 1 else queue[0])
    rpc = FakeRpc(vault_result or outcome(events=("payment_authorized", "payment_settled")))
    vault = VaultClient(VAULT, KeyPair.from_secret(VAULT, SECRET), rpc, explorer_base="https://x/")  # type: ignore[arg-type]
    clock = Clock()
    return IntentsSettlement(OneClick(transport=transport), vault, sleep=clock.sleep, clock=clock), transport, rpc


def status(value: str) -> Any:
    return json_response({"status": value})


def run(settlement: IntentsSettlement, **overrides: Any) -> Any:
    plan = settlement.plan(REQUEST, destination_label="0xabc on Base")
    args = {
        "token_id": "wrap.near",
        "amount": AMOUNT,
        "declared_recipient": "0xabc",
        "commitment": "wf1:c",
        "nullifier": "wfn:n",
    }
    return plan, settlement.execute(plan, **{**args, **overrides})


class SettlementTest(unittest.TestCase):
    def test_happy_path_pays_the_quoted_deposit_address_then_follows_to_delivery(self) -> None:
        settlement, transport, rpc = build([status("PROCESSING"), status("SUCCESS")])
        plan, report = run(settlement)
        self.assertEqual(report.outcome, SwapOutcome.DELIVERED)
        receiver, method, args = rpc.calls[0]
        self.assertEqual((receiver, method), (VAULT, "pay_via_intents"))
        self.assertEqual(args["deposit_address"], "ab" * 32)
        self.assertEqual((args["amount"], args["declared_recipient"]), (str(AMOUNT), "0xabc"))
        self.assertTrue(any(r.full_url.endswith("/v0/deposit/submit") for r in transport.requests))
        self.assertIn("about 3.4", plan.read_back())
        self.assertIn("No payment is made until you confirm", plan.read_back())

    def test_refund_and_failure_are_reported_distinctly(self) -> None:
        for raw, expected in (("REFUNDED", SwapOutcome.REFUNDED), ("FAILED", SwapOutcome.FAILED)):
            settlement, _, _ = build([status(raw)])
            with self.subTest(raw):
                self.assertEqual(run(settlement)[1].outcome, expected)

    def test_vault_refusal_means_nothing_is_in_flight(self) -> None:
        settlement, transport, _ = build([status("SUCCESS")], outcome(status=panic("Over the daily limit")))
        _, report = run(settlement)
        self.assertEqual(report.outcome, SwapOutcome.NOT_SENT)
        self.assertFalse(any("/v0/status" in r.full_url for r in transport.requests))

    def test_rpc_failure_is_not_treated_as_sent(self) -> None:
        settlement, _, _ = build([status("SUCCESS")], UpstreamError("down"))
        self.assertEqual(run(settlement)[1].outcome, SwapOutcome.NOT_SENT)

    def test_no_final_state_in_time_is_pending_not_success(self) -> None:
        settlement, _, _ = build([status("PROCESSING")])
        self.assertEqual(run(settlement, timeout_s=20)[1].outcome, SwapOutcome.PENDING)

    def test_transient_status_errors_do_not_abort_polling(self) -> None:
        settlement, _, _ = build([json_response({"message": "busy"}, 503), status("SUCCESS")])
        self.assertEqual(run(settlement)[1].outcome, SwapOutcome.DELIVERED)

    def test_amount_must_match_the_quote(self) -> None:
        settlement, _, rpc = build([status("SUCCESS")])
        with self.assertRaises(WayfarerError):
            run(settlement, amount=AMOUNT * 2)
        self.assertEqual(rpc.calls, [])


if __name__ == "__main__":
    unittest.main()
