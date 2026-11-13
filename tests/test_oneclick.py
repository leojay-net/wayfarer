import json
import unittest
from typing import Any

from tests.fakes import FakeTransport, json_response
from wayfarer.errors import UpstreamError
from wayfarer.oneclick import USDC_NEAR, WNEAR, OneClick, QuoteRequest, SwapStatus, dry_quote
from wayfarer.transport import HttpResponse

REQUEST = QuoteRequest(WNEAR, USDC_NEAR, "1000000000000000000000000", "ada.near", "refund.near")
QUOTE = {
    "amountInFormatted": "1.0",
    "amountOutFormatted": "3.4",
    "minAmountOut": 3_366_000,
    "timeEstimate": 24,
    "deadline": "2026-11-16T00:10:08.000Z",
}


def sent_body(transport: FakeTransport) -> dict[str, Any]:
    data = transport.requests[-1].data
    assert isinstance(data, bytes)
    return json.loads(data)  # type: ignore[no-any-return]


class QuoteTest(unittest.TestCase):
    def test_dry_quote_has_no_deposit_address(self) -> None:
        transport = FakeTransport()
        transport.on("/v0/quote", lambda _r: json_response({"quote": QUOTE}, 201))
        quote = OneClick(transport=transport).quote(REQUEST)
        self.assertEqual((quote.amount_out, quote.eta_seconds, quote.deposit_address), ("3.4", 24, None))
        body = sent_body(transport)
        self.assertTrue(body["dry"])
        self.assertEqual(
            (body["originAsset"], body["refundTo"], body["amount"]), (WNEAR, "refund.near", REQUEST.amount)
        )

    def test_non_dry_quote_returns_the_deposit_address(self) -> None:
        transport = FakeTransport()
        transport.on("/v0/quote", lambda _r: json_response({"quote": {**QUOTE, "depositAddress": "ab" * 32}}, 201))
        quote = OneClick(transport=transport).quote(REQUEST, dry=False)
        self.assertEqual(quote.deposit_address, "ab" * 32)
        self.assertFalse(sent_body(transport)["dry"])

    def test_non_dry_quote_without_address_is_an_error(self) -> None:
        transport = FakeTransport()
        transport.on("/v0/quote", lambda _r: json_response({"quote": QUOTE}, 201))
        with self.assertRaises(UpstreamError):
            OneClick(transport=transport).quote(REQUEST, dry=False)

    def test_api_key_is_sent_only_when_configured(self) -> None:
        transport = FakeTransport()
        transport.on("/v0/quote", lambda _r: json_response({"quote": QUOTE}, 201))
        OneClick(transport=transport).quote(REQUEST)
        self.assertIsNone(transport.requests[-1].get_header("X-api-key"))
        OneClick(transport=transport, api_key="jwt").quote(REQUEST)
        self.assertEqual(transport.requests[-1].get_header("X-api-key"), "jwt")

    def test_errors(self) -> None:
        for response in (
            json_response({"message": "refundTo is not valid"}, 400),
            HttpResponse(502, b"<html>bad gateway</html>"),
            json_response(["not", "an", "object"]),
        ):
            transport = FakeTransport()
            transport.on("/v0/quote", lambda _r, response=response: response)  # type: ignore[misc]
            with self.subTest(status=response.status), self.assertRaises(UpstreamError):
                OneClick(transport=transport).quote(REQUEST)

    def test_dry_quote_helper(self) -> None:
        transport = FakeTransport()
        transport.on("/v0/quote", lambda _r: json_response({"quote": QUOTE}, 201))
        self.assertEqual(dry_quote(transport=transport).amount_in, "1.0")


class StatusTest(unittest.TestCase):
    def test_status_and_terminal_states(self) -> None:
        transport = FakeTransport()
        transport.on("/v0/status", lambda _r: json_response({"status": "PROCESSING"}))
        client = OneClick(transport=transport)
        self.assertEqual(client.status("addr"), SwapStatus.PROCESSING)
        self.assertIn("depositAddress=addr", transport.requests[-1].full_url)
        self.assertEqual(transport.requests[-1].get_method(), "GET")
        self.assertEqual(
            {s for s in SwapStatus if s.terminal}, {SwapStatus.SUCCESS, SwapStatus.REFUNDED, SwapStatus.FAILED}
        )

    def test_unknown_status_is_an_error(self) -> None:
        transport = FakeTransport()
        transport.on("/v0/status", lambda _r: json_response({"status": "TELEPORTED"}))
        with self.assertRaises(UpstreamError):
            OneClick(transport=transport).status("addr")

    def test_submit_deposit(self) -> None:
        transport = FakeTransport()
        transport.on("/v0/deposit/submit", lambda _r: json_response({}, 200))
        OneClick(transport=transport).submit_deposit(tx_hash="H", deposit_address="addr")
        self.assertEqual(sent_body(transport), {"txHash": "H", "depositAddress": "addr"})


if __name__ == "__main__":
    unittest.main()
