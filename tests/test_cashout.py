import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from typing import Any

from tests.fakes import FakeTransport, json_response
from tests.test_swap import Clock
from wayfarer.cashout.legs import (
    STORAGE_REGISTRATION,
    IntentsSwapLeg,
    NearWrapFunder,
    SandboxSwapLeg,
    SwapQuote,
    SwapResult,
)
from wayfarer.cashout.rails import (
    BankAccount,
    DepositTarget,
    PayoutResult,
    PayoutStatus,
    RailError,
    RailQuote,
    SandboxRail,
)
from wayfarer.cashout.service import CashOutService, CashOutStatus
from wayfarer.errors import UpstreamError, WayfarerError
from wayfarer.near.keys import MlDsaKeyPair
from wayfarer.near.transaction import action_function_call
from wayfarer.oneclick import OneClick
from wayfarer.swap import SwapOutcome

NEAR = 10**24
BANK = BankAccount("058", "0123456789", "Sam Carter")
TARGET = DepositTarget("nep141:sandbox-usdc.invalid", "dest")


def rail(tmp: str, **kw: Any) -> SandboxRail:
    return SandboxRail(Path(tmp) / "ledger.json", fiat_per_usd=Decimal("1500"), **kw)


class BankAccountTest(unittest.TestCase):
    def test_validation_and_masking(self) -> None:
        self.assertEqual(BANK.masked, "058 ****6789")
        for bank_code, number, name in (
            ("5", "0123456789", "A"),
            ("058", "12345", "A"),
            ("058", "012345678901234567", "A"),
            ("058", "0123456789", " "),
        ):
            with self.subTest(number), self.assertRaises(RailError):
                BankAccount(bank_code, number, name)


class SandboxRailTest(unittest.TestCase):
    def test_quote_math_and_label(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            quote = rail(tmp).quote(100 * 10**6)  # 100 USDC
            self.assertEqual((quote.fee_fiat, quote.fiat_out), (Decimal("1500.00"), Decimal("148500.00")))
            self.assertIn("SANDBOX", quote.label)

    def test_payout_is_idempotent_and_writes_a_labelled_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = rail(tmp, clock_iso=lambda: "2026-11-13T12:00:00Z")
            first = sandbox.payout("ref-1", 10 * 10**6, BANK)
            again = sandbox.payout("ref-1", 99 * 10**6, BANK)  # a retry must not pay twice or change the amount
            self.assertEqual((first.status, again.fiat_amount), (PayoutStatus.PAID, first.fiat_amount))
            ledger = json.loads((Path(tmp) / "ledger.json").read_text())
            self.assertEqual(list(ledger), ["ref-1"])
            self.assertIn("no real money", ledger["ref-1"]["note"])
            self.assertEqual(ledger["ref-1"]["bank"], "058 ****6789")
            self.assertNotIn("0123456789", json.dumps(ledger))

    def test_bad_configuration_and_amounts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RailError):
                SandboxRail(Path(tmp) / "l.json", fiat_per_usd=Decimal(0))
            with self.assertRaises(RailError):
                rail(tmp).quote(0)


class SandboxSwapLegTest(unittest.TestCase):
    def test_quote_and_execute(self) -> None:
        leg = SandboxSwapLeg(usd_per_near=Decimal("3.4"))
        quote = leg.quote(3 * NEAR // 10, TARGET, "me.near")
        self.assertEqual((quote.usdc_out, quote.min_usdc_out), (1_016_940, 1_016_940))
        result = leg.execute(quote)
        self.assertEqual((result.outcome, result.usdc_received), (SwapOutcome.DELIVERED, 1_016_940))
        self.assertIn("no funds moved", result.detail)

    def test_failure_modes(self) -> None:
        leg = SandboxSwapLeg(usd_per_near=Decimal(1), outcome=SwapOutcome.REFUNDED)
        self.assertEqual(leg.execute(leg.quote(NEAR, TARGET, "me.near")).usdc_received, 0)
        with self.assertRaises(WayfarerError):
            SandboxSwapLeg(usd_per_near=Decimal(0))
        with self.assertRaises(WayfarerError):
            leg.quote(0, TARGET, "me.near")


class FailingRail:
    label = "failing rail"

    def __init__(self, status: PayoutStatus) -> None:
        self.status, self.calls = status, []  # type: ignore[var-annotated]

    def usdc_target(self, reference: str) -> DepositTarget:
        return TARGET

    def quote(self, usdc_in: int) -> RailQuote:
        return RailQuote(usdc_in, Decimal("1.00"), Decimal("0.01"), Decimal("1500"), self.label)

    def payout(self, reference: str, usdc_in: int, bank: BankAccount) -> PayoutResult:
        self.calls.append((reference, usdc_in))
        return PayoutResult(self.status, reference, Decimal("1.00"), "x", self.label)


class CashOutServiceTest(unittest.TestCase):
    def service(self, tmp: str, outcome: SwapOutcome = SwapOutcome.DELIVERED) -> CashOutService:
        return CashOutService(SandboxSwapLeg(usd_per_near=Decimal("3.4"), outcome=outcome), rail(tmp))

    def test_happy_path_pays_fiat_sized_on_the_swap_minimum(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self.service(tmp)
            quote = service.quote(reference="sale-1", amount_in=3 * NEAR // 10, bank=BANK, refund_to="me.near")
            text = quote.read_back()
            for needle in (
                "0.300000 NEAR",
                "Sam Carter",
                "058 ****6789",
                "estimate",
                "SANDBOX",
                "Nothing happens until you confirm",
            ):
                self.assertIn(needle, text)
            result = service.execute(quote)
            self.assertEqual(result.status, CashOutStatus.PAID)
            self.assertEqual(result.payout.fiat_amount, quote.rail.fiat_out)  # type: ignore[union-attr]
            self.assertEqual(service.execute(quote).status, CashOutStatus.PAID)  # retry: still paid once
            self.assertEqual(len(json.loads((Path(tmp) / "ledger.json").read_text())), 1)

    def test_no_payout_unless_the_swap_delivered(self) -> None:
        for outcome, expected in (
            (SwapOutcome.REFUNDED, CashOutStatus.SWAP_FAILED),
            (SwapOutcome.FAILED, CashOutStatus.SWAP_FAILED),
            (SwapOutcome.NOT_SENT, CashOutStatus.SWAP_FAILED),
            (SwapOutcome.PENDING, CashOutStatus.SWAP_PENDING),
        ):
            with self.subTest(outcome), tempfile.TemporaryDirectory() as tmp:
                service = self.service(tmp, outcome)
                result = service.execute(service.quote(reference="s", amount_in=NEAR, bank=BANK, refund_to="me.near"))
                self.assertEqual(result.status, expected)
                self.assertFalse((Path(tmp) / "ledger.json").exists())

    def test_payout_failure_after_delivery_is_reported_as_usdc_held(self) -> None:
        for status, expected in (
            (PayoutStatus.FAILED, CashOutStatus.USDC_HELD),
            (PayoutStatus.PENDING, CashOutStatus.PAYOUT_PENDING),
        ):
            with self.subTest(status):
                bad = FailingRail(status)
                service = CashOutService(SandboxSwapLeg(usd_per_near=Decimal(3)), bad)
                quote = service.quote(reference="r1", amount_in=NEAR, bank=BANK, refund_to="me.near")
                result = service.execute(quote)
                self.assertEqual(result.status, expected)
                self.assertEqual(bad.calls, [("r1", quote.swap.min_usdc_out)])
                if status is PayoutStatus.FAILED:
                    self.assertIn("r1", result.message)

    def test_reference_is_validated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for bad in ("", "x" * 65):
                with self.assertRaises(WayfarerError):
                    self.service(tmp).quote(reference=bad, amount_in=NEAR, bank=BANK, refund_to="me.near")


QUOTE = {
    "amountInFormatted": "0.3",
    "amountOutFormatted": "1.0",
    "minAmountOut": 990000,
    "amountOut": "1000000",
    "timeEstimate": 24,
    "depositAddress": "ab" * 32,
}


class FakeFunder:
    def __init__(self, error: Exception | None = None) -> None:
        self.error, self.calls = error, []  # type: ignore[var-annotated]

    def fund(self, deposit_address: str, amount: int) -> str:
        self.calls.append((deposit_address, amount))
        if self.error:
            raise self.error
        return "FUNDTX"


class IntentsLegTest(unittest.TestCase):
    def leg(
        self, statuses: list[str], funder: FakeFunder | None = None
    ) -> tuple[IntentsSwapLeg, FakeTransport, FakeFunder]:
        transport = FakeTransport()
        transport.on("/v0/quote", lambda r: json_response({"quote": QUOTE}, 201))
        transport.on("/v0/deposit/submit", lambda r: json_response({}))
        queue = list(statuses)
        transport.on("/v0/status", lambda r: json_response({"status": queue.pop(0) if len(queue) > 1 else queue[0]}))
        clock = Clock()
        funder = funder or FakeFunder()
        return (
            IntentsSwapLeg(OneClick(transport=transport), funder, sleep=clock.sleep, clock=clock, timeout_s=30),
            transport,
            funder,
        )

    def test_quote_asks_for_wnear_to_the_rail_target_with_refund_to_the_merchant(self) -> None:
        leg, transport, _ = self.leg(["SUCCESS"])
        quote = leg.quote(3 * NEAR // 10, TARGET, "shop.near")
        body = json.loads(transport.requests[0].data)  # type: ignore[arg-type]
        self.assertEqual((body["originAsset"], body["destinationAsset"]), ("nep141:wrap.near", TARGET.asset_id))
        self.assertEqual(
            (body["recipient"], body["refundTo"], body["amount"]), ("dest", "shop.near", str(3 * NEAR // 10))
        )
        self.assertEqual((quote.deposit_address, quote.min_usdc_out, quote.usdc_out), ("ab" * 32, 990_000, 1_000_000))

    def test_delivered_swap_counts_the_guaranteed_minimum(self) -> None:
        leg, transport, funder = self.leg(["PROCESSING", "SUCCESS"])
        quote = leg.quote(NEAR, TARGET, "shop.near")
        result = leg.execute(quote)
        self.assertEqual(
            (result.outcome, result.usdc_received, result.tx_hash), (SwapOutcome.DELIVERED, 990_000, "FUNDTX")
        )
        self.assertEqual(funder.calls, [("ab" * 32, NEAR)])
        self.assertTrue(any(r.full_url.endswith("/v0/deposit/submit") for r in transport.requests))

    def test_refund_and_timeout_pay_nothing(self) -> None:
        leg, _, _ = self.leg(["REFUNDED"])
        self.assertEqual(leg.execute(leg.quote(NEAR, TARGET, "s.near")).usdc_received, 0)
        leg, _, _ = self.leg(["PROCESSING"])
        result = leg.execute(leg.quote(NEAR, TARGET, "s.near"))
        self.assertEqual((result.outcome, result.usdc_received), (SwapOutcome.PENDING, 0))

    def test_funding_errors_are_not_confused(self) -> None:
        leg, _, _ = self.leg(["SUCCESS"], FakeFunder(WayfarerError("refused: not enough NEAR")))
        self.assertEqual(leg.execute(leg.quote(NEAR, TARGET, "s.near")).outcome, SwapOutcome.NOT_SENT)
        leg, _, _ = self.leg(["SUCCESS"], FakeFunder(UpstreamError("timeout")))
        pending = leg.execute(leg.quote(NEAR, TARGET, "s.near"))
        self.assertEqual(pending.outcome, SwapOutcome.PENDING)  # may have been sent: never "nothing happened"

    def test_missing_deposit_address_is_an_error(self) -> None:
        leg, transport, _ = self.leg(["SUCCESS"])
        transport.on("/v0/quote", lambda r: json_response({"quote": {**QUOTE, "depositAddress": None}}, 201))
        with self.assertRaises(UpstreamError):
            leg.quote(NEAR, TARGET, "s.near")


class FakeRpc:
    def __init__(self, result: dict[str, Any]) -> None:
        self.result, self.sent = result, []  # type: ignore[var-annotated]

    def send_actions(self, key: Any, receiver: str, actions: list[bytes]) -> dict[str, Any]:
        self.sent.append((key.account_id, receiver, actions))
        return self.result


class NearWrapFunderTest(unittest.TestCase):
    KEY = MlDsaKeyPair("shop.near", bytes(range(1, 33)))

    def test_one_transaction_registers_wraps_and_transfers(self) -> None:
        rpc = FakeRpc({"transaction": {"hash": "H"}, "status": {"SuccessValue": ""}, "receipts_outcome": []})
        self.assertEqual(NearWrapFunder(rpc, self.KEY).fund("ab" * 32, NEAR), "H")  # type: ignore[arg-type]
        account, receiver, actions = rpc.sent[0]
        self.assertEqual((account, receiver), ("shop.near", "wrap.near"))
        self.assertEqual(
            actions,
            [
                action_function_call(
                    "storage_deposit",
                    {"account_id": "ab" * 32, "registration_only": True},
                    10 * 10**12,
                    STORAGE_REGISTRATION,
                ),
                action_function_call("near_deposit", {}, 10 * 10**12, NEAR),
                action_function_call("ft_transfer", {"receiver_id": "ab" * 32, "amount": str(NEAR)}, 30 * 10**12, 1),
            ],
        )

    def test_failure_raises(self) -> None:
        failed = {"transaction": {"hash": "H"}, "status": {"Failure": {"ActionError": "boom"}}, "receipts_outcome": []}
        with self.assertRaises(WayfarerError):
            NearWrapFunder(FakeRpc(failed), self.KEY).fund("ab" * 32, NEAR)  # type: ignore[arg-type]


class SwapResultTest(unittest.TestCase):
    def test_value_objects_are_frozen(self) -> None:
        result = SwapResult(SwapOutcome.DELIVERED, 1, "x")
        with self.assertRaises(AttributeError):
            result.usdc_received = 2  # type: ignore[misc]
        self.assertIsInstance(SwapQuote(1, 1, 1, None, "d", "l"), SwapQuote)


if __name__ == "__main__":
    unittest.main()
