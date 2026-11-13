import hashlib
import json
import random
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

from wayfarer.crypto import mldsa
from wayfarer.errors import TapError
from wayfarer.near.keys import MlDsaKeyPair
from wayfarer.pqvault import MandateTerms
from wayfarer.tap.buyer import ApprovalPrompt, BuyerWallet, Confirmer, TapLimits, asset_text
from wayfarer.tap.codec import Reader, Writer
from wayfarer.tap.envelope import ChequeEnvelope, cheque_for_request
from wayfarer.tap.link import FrameAssembler, LoopbackLink, encode_frames
from wayfarer.tap.merchant import MerchantTerminal
from wayfarer.tap.rates import FixedRateSource, RateDesk, RateQuote, asset_amount
from wayfarer.tap.request import PaymentRequest

VECTORS = json.loads((Path(__file__).parent / "vectors" / "tap.json").read_text())
NOW = VECTORS["now_ms"]
VAULT = VECTORS["vault"]
SEED = bytes.fromhex(VECTORS["buyer_seed"])
NEAR = 10**24


class Clock:
    def __init__(self, now: int = NOW) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now


class Yes:
    def __init__(self, answer: bool = True) -> None:
        self.answer, self.seen = answer, []  # type: ignore[var-annotated]

    def confirm(self, prompt: ApprovalPrompt) -> bool:
        self.seen.append(prompt)
        return self.answer


def terminal(clock: Clock, nonce: bytes = bytes(range(16))) -> MerchantTerminal:
    desk = RateDesk(FixedRateSource({("NGN", "near"): Decimal("5000")}), clock, new_id=lambda: "q-test-0001")
    return MerchantTerminal(
        account="ada-shop.testnet", name="Ada's Shop", rates=desk, clock_ms=clock, nonce_factory=lambda: nonce
    )


def wallet(clock: Clock, confirmer: Confirmer | None = None, limit: int = NEAR) -> BuyerWallet:
    yes = confirmer or Yes()
    return BuyerWallet(
        vault=VAULT,
        agent_key=MlDsaKeyPair(VAULT, SEED),
        confirmer=yes,
        limits=TapLimits({"near": limit}),
        clock_ms=clock,
    )


class CodecTest(unittest.TestCase):
    def test_round_trip_and_strictness(self) -> None:
        data = Writer().string("héllo").u8(7).u64(2**63).u128(2**100).getvalue()
        reader = Reader(data)
        self.assertEqual((reader.string(), reader.u8(), reader.u64(), reader.u128()), ("héllo", 7, 2**63, 2**100))
        reader.finish()
        short = Reader(data[:-1])
        short.string(), short.u8(), short.u64()
        with self.assertRaises(TapError):
            short.u128()
        extra = Reader(Writer().u8(1).getvalue() + b"x")
        extra.u8()
        with self.assertRaises(TapError):
            extra.finish()
        with self.assertRaises(TapError):
            Writer().string("x" * 300)
        with self.assertRaises(TapError):
            Reader(Writer().u32(10_000).getvalue()).string()
        with self.assertRaises(TapError):
            Reader(Writer().u32(2).raw(b"\xff\xfe").getvalue()).string()


class RatesTest(unittest.TestCase):
    def quote(self, price: str) -> RateQuote:
        return RateQuote("q", "NGN", "near", Decimal(price), 1, 2)

    def test_naira_to_near(self) -> None:
        self.assertEqual(asset_amount(self.quote("5000"), 150_000, 2), 3 * NEAR // 10)

    def test_rounds_up_so_the_merchant_never_gets_less(self) -> None:
        self.assertEqual(asset_amount(self.quote("3"), 1, 2), 3_333_333_333_333_333_333_334)

    def test_unsupported_token_and_missing_rate(self) -> None:
        desk = RateDesk(FixedRateSource({("NGN", "near"): Decimal(1)}), Clock())
        with self.assertRaises(TapError):
            desk.lock("NGN", "usdc.near")
        with self.assertRaises(TapError):
            RateDesk(FixedRateSource({}), Clock()).lock("NGN", "near")

    def test_lock_window_is_sixty_seconds(self) -> None:
        quote = RateDesk(FixedRateSource({("NGN", "near"): Decimal(1)}), Clock()).lock("NGN", "near")
        self.assertEqual(quote.expires_ms - quote.created_ms, 60_000)


class RequestTest(unittest.TestCase):
    def setUp(self) -> None:
        self.request = terminal(Clock()).create_request(150_000)

    def test_matches_shared_vectors(self) -> None:
        self.assertEqual(self.request.message().hex(), VECTORS["request_message_hex"])
        self.assertEqual(self.request.request_hash().hex(), VECTORS["request_hash_hex"])
        self.assertEqual(self.request.intent_id(), VECTORS["intent_id"])
        self.assertEqual(self.request.receipt_hash(), VECTORS["receipt_hash"])
        self.assertEqual(self.request.fiat_display(), "NGN 1,500.00")
        self.assertEqual(cheque_for_request(self.request, VAULT).message().hex(), VECTORS["cheque_message_hex"])

    def test_decode_round_trip(self) -> None:
        self.assertEqual(PaymentRequest.decode(self.request.message()), self.request)

    def test_decode_rejects_garbage(self) -> None:
        for bad in (b"", b"nope", self.request.message()[:-1], self.request.message() + b"x"):
            with self.subTest(bad[:8]), self.assertRaises(TapError):
                PaymentRequest.decode(bad)

    def test_validation(self) -> None:
        for change in (
            {"merchant_account": "Not An Account"},
            {"merchant_name": ""},
            {"fiat_currency": "ngn"},
            {"fiat_minor": 0},
            {"fiat_exponent": 9},
            {"asset_amount": 0},
            {"nonce": b"short"},
            {"quote_expires_ms": 1},
            {"settle_by_ms": NOW},
            {"rate": "abc"},
            {"rate": "-1"},
        ):
            with self.subTest(change), self.assertRaises(TapError):
                replace(self.request, **change)

    def test_one_changed_field_changes_the_hash(self) -> None:
        other = replace(self.request, asset_amount=self.request.asset_amount + 1)
        self.assertNotEqual(other.request_hash(), self.request.request_hash())


class EnvelopeTest(unittest.TestCase):
    def test_matches_shared_vector_and_round_trips(self) -> None:
        raw = bytes.fromhex(VECTORS["envelope_hex"])
        envelope = ChequeEnvelope.decode(raw)
        self.assertEqual(envelope.encode(), raw)
        self.assertEqual(envelope.signature.hex(), VECTORS["signature_hex"])
        slim = replace(envelope, public_key=None)
        self.assertEqual(ChequeEnvelope.decode(slim.encode()), slim)
        self.assertEqual(len(slim.encode()), len(raw) - mldsa.PUBLIC_KEY_SIZE)

    def test_rejects_malformed(self) -> None:
        raw = bytes.fromhex(VECTORS["envelope_hex"])
        for bad in (b"XXXX" + raw[4:], raw[:-1], raw + b"\x00", b""):
            with self.assertRaises(TapError):
                ChequeEnvelope.decode(bad)
        with self.assertRaises(TapError):
            ChequeEnvelope(bytes(31), VAULT, bytes(32), bytes(mldsa.SIGNATURE_SIZE))


class BuyerTest(unittest.TestCase):
    def test_confirmed_request_yields_a_valid_cheque(self) -> None:
        clock = Clock()
        request = terminal(clock).create_request(150_000)
        yes = Yes()
        buyer = wallet(clock, yes)
        envelope = buyer.pay(request)
        self.assertEqual(yes.seen[0].lines()[0], "Pay NGN 1,500.00 to Ada's Shop")
        self.assertEqual(yes.seen[0].asset_text, "0.300000 NEAR")
        self.assertTrue(
            mldsa.verify(
                MlDsaKeyPair(VAULT, SEED).public_key, envelope.signature, cheque_for_request(request, VAULT).message()
            )
        )

    def test_declined_request_signs_nothing(self) -> None:
        clock = Clock()
        request = terminal(clock).create_request(150_000)
        answer = Yes(False)
        buyer = wallet(clock, answer)
        with self.assertRaisesRegex(TapError, "cancelled"):
            buyer.pay(request)
        answer.answer = True
        buyer.pay(request)  # not blocked: the earlier refusal did not count as approval

    def test_refusals(self) -> None:
        clock = Clock()
        request = terminal(clock).create_request(150_000)
        buyer = wallet(clock, limit=NEAR // 10)  # tap limit 0.1 NEAR < 0.3 NEAR
        with self.assertRaisesRegex(TapError, "tap limit"):
            buyer.pay(request)
        buyer = wallet(clock)
        clock.now = request.quote_expires_ms + 1
        with self.assertRaisesRegex(TapError, "expired"):
            buyer.pay(request)
        clock.now = NOW
        buyer.pay(request)
        with self.assertRaisesRegex(TapError, "already approved"):
            buyer.pay(request)
        other = replace(request, token_id="usdc.near", nonce=bytes(16))
        with self.assertRaisesRegex(TapError, "not enabled"):
            buyer.pay(other)

    def test_price_expiring_during_confirmation_is_refused(self) -> None:
        clock = Clock()
        request = terminal(clock).create_request(150_000)

        class Slow:
            def confirm(self, prompt: ApprovalPrompt) -> bool:
                clock.now = request.quote_expires_ms + 1
                return True

        buyer = wallet(clock, Slow())
        with self.assertRaisesRegex(TapError, "expired while"):
            buyer.pay(request)

    def test_asset_text(self) -> None:
        self.assertEqual(asset_text("near", 5 * NEAR // 100), "0.050000 NEAR")
        self.assertIn("smallest units", asset_text("usdc.near", 5))


class MerchantTest(unittest.TestCase):
    def sale(self) -> tuple[Clock, MerchantTerminal, PaymentRequest, BuyerWallet]:
        clock = Clock()
        merchant = terminal(clock)
        request = merchant.create_request(150_000)
        buyer = wallet(clock)
        return clock, merchant, request, buyer

    def test_accepts_the_shared_vector_envelope(self) -> None:
        _, merchant, request, _ = self.sale()
        accepted = merchant.accept(bytes.fromhex(VECTORS["envelope_hex"]))
        self.assertEqual(accepted.request, request)
        self.assertEqual(accepted.key_id, VECTORS["buyer_public_key_sha256"])
        self.assertEqual(accepted.cheque.amount, 3 * NEAR // 10)

    def test_accepts_a_fresh_cheque_once(self) -> None:
        _, merchant, request, buyer = self.sale()
        raw = buyer.pay(request).encode()
        merchant.accept(raw)
        with self.assertRaisesRegex(TapError, "already accepted"):
            merchant.accept(raw)

    def test_rejections(self) -> None:
        clock, merchant, request, _ = self.sale()
        raw = bytes.fromhex(VECTORS["envelope_hex"])
        envelope = ChequeEnvelope.decode(raw)
        flipped = bytearray(envelope.signature)
        flipped[10] ^= 1
        cases: dict[str, ChequeEnvelope] = {
            "not for any request": replace(envelope, request_hash=bytes(32)),
            "not valid for this request": replace(envelope, signature=bytes(flipped)),
            "does not match the public key": replace(envelope, key_id=bytes(32)),
            "not valid for this request (vault)": replace(envelope, vault="other-vault.testnet"),
            "cannot be checked offline": replace(envelope, public_key=None),
        }
        for message, bad in cases.items():
            with self.subTest(message), self.assertRaisesRegex(TapError, message.split(" (")[0].replace(" ", ".")):
                merchant.accept(bad.encode())
        clock.now = request.settle_by_ms + 1
        with self.assertRaisesRegex(TapError, "too old"):
            merchant.accept(raw)
        with self.assertRaises(TapError):
            merchant.accept(b"garbage")

    def test_a_cheque_for_a_different_amount_cannot_be_forged(self) -> None:
        clock = Clock()
        merchant = terminal(clock)
        cheap = merchant.create_request(150_000)
        # A buyer signs the real request; a thief swaps in a request hash for a pricier sale: signature must fail.
        envelope = wallet(clock).pay(cheap)
        pricey = merchant.create_request(300_000)
        with self.assertRaisesRegex(TapError, "not valid"):
            merchant.accept(replace(envelope, request_hash=pricey.request_hash()).encode())


class FakeChain:
    def __init__(self, **overrides: Any) -> None:
        self.terms: MandateTerms | None = MandateTerms(
            vault=VAULT,
            owner="o.testnet",
            key_id="k",
            paused=False,
            expires_at_ms=0,
            min_interval_ms=0,
            last_payment_ms=0,
            token_id="near",
            per_tx_max=NEAR,
            daily_max=2 * NEAR,
            allowlist_enabled=False,
            allowlist=(),
        )
        self.remaining: int | None = NEAR
        self.balance = 5 * NEAR
        self.used = False
        for key, value in overrides.items():
            setattr(self, key, value)

    def mandate(self, key_id: str, token_id: str) -> MandateTerms | None:
        return self.terms

    def remaining_today(self, token_id: str) -> int | None:
        return self.remaining

    def spendable_balance(self, token_id: str) -> int:
        return self.balance

    def intent_used(self, intent_id: str) -> bool:
        return self.used


class StrongCheckTest(unittest.TestCase):
    def accepted(self) -> tuple[MerchantTerminal, Any]:
        clock = Clock()
        merchant = terminal(clock)
        merchant.create_request(150_000)
        return merchant, merchant.accept(bytes.fromhex(VECTORS["envelope_hex"]))

    def test_healthy_vault_passes(self) -> None:
        merchant, accepted = self.accepted()
        result = merchant.strong_check(accepted, FakeChain())
        self.assertEqual((result.ok, result.problems), (True, ()))

    def test_each_problem_is_reported(self) -> None:
        merchant, accepted = self.accepted()
        base = FakeChain().terms
        assert base is not None
        scenarios: dict[str, FakeChain] = {
            "does not know this key": FakeChain(terms=None),
            "paused": FakeChain(terms=replace(base, paused=True)),
            "ends before": FakeChain(terms=replace(base, expires_at_ms=NOW + 1000)),
            "above the per-payment cap": FakeChain(terms=replace(base, per_tx_max=NEAR // 10)),
            "paid too recently": FakeChain(terms=replace(base, min_interval_ms=60_000, last_payment_ms=NOW - 1)),
            "allow-list": FakeChain(terms=replace(base, allowlist_enabled=True)),
            "daily budget": FakeChain(remaining=1),
            "balance is too low": FakeChain(balance=1),
            "already used": FakeChain(used=True),
        }
        for message, chain in scenarios.items():
            with self.subTest(message):
                result = merchant.strong_check(accepted, chain)
                self.assertFalse(result.ok)
                self.assertTrue(any(message in p for p in result.problems), result.problems)


class LinkTest(unittest.TestCase):
    def test_loopback_pair(self) -> None:
        a, b = LoopbackLink.pair()
        a.send(b"ping")
        self.assertEqual(b.receive(0.1), b"ping")
        b.send(b"pong")
        self.assertEqual(a.receive(0.1), b"pong")
        self.assertIsNone(a.receive(0.01))

    def test_frames_survive_shuffle_and_duplicates(self) -> None:
        payload = bytes.fromhex(VECTORS["envelope_hex"])
        frames = encode_frames(payload, b"\x01\x02\x03\x04")
        self.assertEqual(encode_frames(payload, bytes([0xA1, 0xB2, 0xC3, 0xD4])), VECTORS["envelope_frames"])
        noisy = frames + frames[:2]
        random.Random(7).shuffle(noisy)
        assembler = FrameAssembler()
        for frame in noisy:
            assembler.add(frame)
        self.assertEqual(assembler.payload(), payload)

    def test_missing_frames_are_reported(self) -> None:
        frames = encode_frames(b"x" * 3000, b"\x00" * 4, chunk_bytes=1000)
        assembler = FrameAssembler()
        progress = assembler.add(frames[2])
        self.assertEqual((progress.received, progress.total, progress.missing), (1, 3, (0, 1)))
        with self.assertRaisesRegex(TapError, "missing"):
            assembler.payload()

    def test_damaged_and_foreign_frames_are_rejected(self) -> None:
        frames = encode_frames(b"hello world" * 200, b"\x00" * 4, chunk_bytes=500)
        assembler = FrameAssembler()
        assembler.add(frames[0])
        damaged = frames[1][:-10] + ("A" if frames[1][-10] != "A" else "B") + frames[1][-9:]
        with self.assertRaisesRegex(TapError, "damaged"):
            assembler.add(damaged)
        other = encode_frames(b"hello world" * 200, b"\x09" * 4, chunk_bytes=500)
        with self.assertRaisesRegex(TapError, "different transfer"):
            assembler.add(other[1])
        for bad in ("", "WF2.a.0.1.AA.00", "WF1.a.x.1.AA.00"):
            with self.assertRaises(TapError):
                FrameAssembler().add(bad)

    def test_empty_payload_and_bad_arguments(self) -> None:
        assembler = FrameAssembler()
        for frame in encode_frames(b"", b"\x00" * 4):
            assembler.add(frame)
        self.assertEqual(assembler.payload(), b"")
        with self.assertRaises(TapError):
            encode_frames(b"x", b"short")
        with self.assertRaises(TapError):
            encode_frames(b"x", b"\x00" * 4, chunk_bytes=0)

    def test_shared_vector_frames_reassemble(self) -> None:
        assembler = FrameAssembler()
        for frame in VECTORS["envelope_frames"]:
            assembler.add(frame)
        self.assertEqual(assembler.payload().hex(), VECTORS["envelope_hex"])
        self.assertEqual(len(hashlib.sha256(assembler.payload()).hexdigest()), 64)


class TwoPhonesTest(unittest.TestCase):
    """The whole tap over a link: request to the buyer, cheque back, merchant accepts."""

    def test_over_loopback_link(self) -> None:
        clock = Clock()
        merchant, (buyer_link, merchant_link) = terminal(clock), LoopbackLink.pair()
        buyer = wallet(clock)
        request = merchant.create_request(150_000)
        merchant_link.send(request.message())
        received = PaymentRequest.decode(buyer_link.receive(0.1) or b"")
        buyer_link.send(buyer.pay(received).encode())
        accepted = merchant.accept(merchant_link.receive(0.1) or b"")
        self.assertEqual(accepted.cheque.to, "ada-shop.testnet")

    def test_over_lossy_qr_frames(self) -> None:
        clock = Clock()
        merchant = terminal(clock)
        buyer = wallet(clock)
        request = merchant.create_request(150_000)
        scan = FrameAssembler()
        for frame in encode_frames(request.message(), b"\x01\x01\x01\x01"):
            scan.add(frame)
        received = PaymentRequest.decode(scan.payload())
        frames = encode_frames(buyer.pay(received).encode(), b"\x02\x02\x02\x02")
        back = FrameAssembler()
        for index in (3, 0, 5, 0, 2):  # a lossy, repeating scan
            back.add(frames[index])
        self.assertEqual(back.progress().missing, (1, 4))
        for index in (4, 1):
            back.add(frames[index])
        accepted = merchant.accept(back.payload())
        self.assertEqual(accepted.request, request)


if __name__ == "__main__":
    unittest.main()
