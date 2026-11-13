import contextlib
import io
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from tests.fakes import FakeTransport, chat_payload, json_response
from wayfarer import cli
from wayfarer.config import Settings
from wayfarer.errors import UpstreamError
from wayfarer.nearai import NearAiClient

CASES = [
    {
        "category": "plain",
        "text": "send 5 NEAR to ada.near",
        "expect": {"amount": "5", "token": "NEAR", "recipient": "ada.near"},
    },
    {"category": "must-refuse", "text": "weather?", "expect": None},
]


def run_eval(directory: str, transport: FakeTransport) -> tuple[int, str, str]:
    path = Path(directory, "cases.jsonl")
    path.write_text("\n".join(json.dumps(c) for c in CASES))
    settings = Settings(near_ai_api_key="k", cost_state_path=Path(directory, "cost.json"))
    out, err = io.StringIO(), io.StringIO()
    with (
        mock.patch.object(Settings, "from_env", return_value=settings),
        mock.patch.object(cli, "NearAiClient", lambda s, meter=None: NearAiClient(s, transport=transport, meter=meter)),
        contextlib.redirect_stdout(out),
        contextlib.redirect_stderr(err),
    ):
        code = cli.main(["eval", "--cases", str(path)])
    return code, out.getvalue(), err.getvalue()


class EvalTest(unittest.TestCase):
    def test_reports_per_category_and_spend(self) -> None:
        answers = iter(['{"action":"send","amount":"5","token":"NEAR","recipient":"ada.near"}', '{"action":"none"}'])
        transport = FakeTransport()
        transport.on("/chat/completions", lambda _r: json_response(chat_payload(next(answers))))
        transport.on("/models", lambda _r: json_response({"data": []}))
        with TemporaryDirectory() as directory:
            code, out, _ = run_eval(directory, transport)
        self.assertEqual(code, 0)
        self.assertIn("plain", out)
        self.assertIn("2/2 passed (100%)", out)
        self.assertIn("estimated spend", out)

    def test_wrong_answers_are_failures_not_passes(self) -> None:
        transport = FakeTransport()
        transport.on("/chat/completions", lambda _r: json_response(chat_payload('{"action":"none"}')))
        transport.on("/models", lambda _r: json_response({"data": []}))
        with TemporaryDirectory() as directory:
            _, out, _ = run_eval(directory, transport)
        self.assertIn("1/2 passed (50%)", out)
        self.assertIn("FAIL [plain]", out)

    def test_upstream_errors_abort_instead_of_counting(self) -> None:
        transport = FakeTransport()
        transport.on("/chat/completions", lambda _r: UpstreamError("down"))
        transport.on("/models", lambda _r: json_response({"data": []}))
        with TemporaryDirectory() as directory:
            code, out, err = run_eval(directory, transport)
        self.assertEqual(code, 2)
        self.assertIn("not a valid pass rate", err)
        self.assertNotIn("passed", out)


class ParserTest(unittest.TestCase):
    def test_every_command_is_registered(self) -> None:
        parser = cli.build_parser()
        for argv in (["quote"], ["transcribe", "a.ogg"], ["eval"], ["attest", "x"], ["export-csv", "--out", "o.csv"],
                     ["verify-receipt", "--opening", "o", "--tx", "t", "--signer", "s"]):  # fmt: skip
            with self.subTest(argv=argv[0]):
                self.assertTrue(callable(parser.parse_args(argv).func))

    def test_labelled_cases_file_is_well_formed(self) -> None:
        rows = [json.loads(x) for x in Path("testdata/intents.jsonl").read_text().splitlines() if x.strip()]
        self.assertGreaterEqual(len(rows), 50)
        for row in rows:
            self.assertEqual({"category", "lang", "text", "expect"}, set(row))
            if row["expect"] is not None:
                cli._canonical_expectation(row["expect"], Settings(near_ai_api_key="k").max_amount)


if __name__ == "__main__":
    unittest.main()
