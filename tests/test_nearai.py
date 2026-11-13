import json
import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from tests.fakes import FakeTransport, chat_payload, json_response, settings
from wayfarer.cost import CostMeter
from wayfarer.errors import BudgetExceededError, UpstreamError
from wayfarer.nearai import NearAiClient
from wayfarer.transport import HttpResponse


class TranscribeTest(unittest.TestCase):
    def test_request_shape_and_result(self) -> None:
        transport = FakeTransport()
        transport.on(
            "/audio/transcriptions",
            lambda _r: json_response({"text": " Send 20 USDC ", "language": "en", "duration": 2.5}),
        )
        result = NearAiClient(settings(), transport=transport).transcribe(b"OGGDATA", language="yo")
        request = transport.requests[0]
        body = request.data
        assert isinstance(body, bytes)
        self.assertEqual((result.text, result.language, result.duration_s), ("Send 20 USDC", "en", 2.5))
        self.assertEqual(request.get_header("Authorization"), "Bearer test-key")
        self.assertIn("multipart/form-data; boundary=", request.get_header("Content-type") or "")
        self.assertIn(b'name="language"\r\n\r\nyo', body)
        self.assertIn(b'name="model"\r\n\r\nopenai/whisper-large-v3', body)
        self.assertIn(b"OGGDATA", body)

    def test_language_hint_is_optional(self) -> None:
        transport = FakeTransport()
        transport.on("/audio/transcriptions", lambda _r: json_response({"text": "hi"}))
        NearAiClient(settings(), transport=transport).transcribe(b"x")
        body = transport.requests[0].data
        assert isinstance(body, bytes)
        self.assertNotIn(b'name="language"', body)

    def test_silence_is_an_error(self) -> None:
        transport = FakeTransport()
        transport.on("/audio/transcriptions", lambda _r: json_response({"text": "  "}))
        with self.assertRaises(UpstreamError):
            NearAiClient(settings(), transport=transport).transcribe(b"x")

    def test_network_failure_surfaces_as_upstream_error(self) -> None:
        transport = FakeTransport()
        transport.on(
            "/audio/transcriptions",
            lambda _r: UpstreamError("cloud-api.near.ai did not answer in time (TimeoutError)."),
        )
        with self.assertRaises(UpstreamError) as caught:
            NearAiClient(settings(), transport=transport).transcribe(b"x")
        self.assertIn("did not answer in time", str(caught.exception))


class StatusMappingTest(unittest.TestCase):
    def call(self, status: int, body: bytes = b"{}") -> str:
        transport = FakeTransport()
        transport.on("/chat/completions", lambda _r: HttpResponse(status, body))
        with self.assertRaises(UpstreamError) as caught:
            NearAiClient(settings(), transport=transport).chat([{"role": "user", "content": "hi"}])
        return str(caught.exception)

    def test_payment_required(self) -> None:
        self.assertIn("no credits", self.call(402))

    def test_rate_limited(self) -> None:
        self.assertIn("rate limiting", self.call(429))

    def test_server_error_includes_status(self) -> None:
        self.assertIn("500", self.call(500, b"boom"))

    def test_missing_api_key(self) -> None:
        from wayfarer.config import Settings

        with self.assertRaises(UpstreamError):
            NearAiClient(Settings(near_ai_api_key=""), transport=FakeTransport()).chat(
                [{"role": "user", "content": "x"}]
            )

    def test_unreadable_answer(self) -> None:
        transport = FakeTransport()
        transport.on("/chat/completions", lambda _r: HttpResponse(200, b"<html>"))
        with self.assertRaises(UpstreamError):
            NearAiClient(settings(), transport=transport).chat([{"role": "user", "content": "x"}])


class CostMeterTest(unittest.TestCase):
    def client(self, meter: CostMeter) -> NearAiClient:
        transport = FakeTransport()
        transport.on(
            "/models",
            lambda _r: json_response(
                {"data": [{"id": "z-ai/glm-5.3-flash", "pricing": {"prompt": "0.00000008", "completion": "0.0000005"}}]}
            ),
        )
        transport.on(
            "/chat/completions",
            lambda _r: json_response(chat_payload('{"action":"none"}', prompt_tokens=1000, completion_tokens=1000)),
        )
        return NearAiClient(settings(), transport=transport, meter=meter)

    def test_chat_cost_uses_catalogue_prices(self) -> None:
        meter = CostMeter(Decimal("1"))
        self.client(meter).chat([{"role": "user", "content": "x"}])
        self.assertEqual(meter.spent, Decimal("0.00000008") * 1000 + Decimal("0.0000005") * 1000)
        self.assertEqual(meter.calls, 1)

    def test_cap_blocks_further_calls(self) -> None:
        meter = CostMeter(Decimal("0.005"))
        client = self.client(meter)
        with self.assertRaises(BudgetExceededError):
            client.chat([{"role": "user", "content": "x"}])  # headroom check (0.01) already exceeds the cap

    def test_state_survives_restart(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory, "cost.json")
            first = CostMeter(Decimal("1"), path)
            first.record(Decimal("0.25"))
            second = CostMeter(Decimal("1"), path)
            self.assertEqual((second.spent, second.calls, second.remaining), (Decimal("0.25"), 1, Decimal("0.75")))
            self.assertEqual(json.loads(path.read_text())["spent_usd"], "0.25")

    def test_fallback_pricing_is_conservative_when_catalogue_fails(self) -> None:
        transport = FakeTransport()
        transport.on("/models", lambda _r: HttpResponse(500, b"down"))
        price = NearAiClient(settings(), transport=transport).pricing("z-ai/glm-5.3-flash")
        self.assertGreaterEqual(price.completion, Decimal("0.000005"))


class SignatureTest(unittest.TestCase):
    def test_retries_until_available(self) -> None:
        transport = FakeTransport()
        answers = [
            HttpResponse(404, b"{}"),
            json_response({"text": "t", "signature": "ab", "signing_address": "cd", "signing_algo": "ed25519"}),
        ]
        transport.on("/signature/cmpl-1", lambda _r: answers.pop(0))
        with mock.patch("wayfarer.nearai.time.sleep"):
            record = NearAiClient(settings(), transport=transport).signature("cmpl-1")
        self.assertEqual(record.signing_address, "cd")
        self.assertIn("signing_algo=ed25519", transport.requests[0].full_url)

    def test_gives_up_after_retries(self) -> None:
        transport = FakeTransport()
        transport.on("/signature/cmpl-1", lambda _r: HttpResponse(404, b"{}"))
        with mock.patch("wayfarer.nearai.time.sleep"), self.assertRaises(UpstreamError):
            NearAiClient(settings(), transport=transport).signature("cmpl-1")


if __name__ == "__main__":
    unittest.main()
