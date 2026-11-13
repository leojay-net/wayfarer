"""Command line tools: ``wayfarer <command>``."""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

from wayfarer import oneclick
from wayfarer.attestation import attest_completion
from wayfarer.config import Settings
from wayfarer.cost import CostMeter
from wayfarer.errors import UpstreamError, WayfarerError
from wayfarer.intent import IntentExtractor, parse_intent
from wayfarer.near.events import extract_events
from wayfarer.near.rpc import DEFAULT_TESTNET_RPC, NearRpc
from wayfarer.nearai import NearAiClient
from wayfarer.receipts import ReceiptLedger, ReceiptOpening, verify_opening
from wayfarer.vault import NATIVE_TOKEN_ID, TESTNET_EXPLORER

DEFAULT_CASES = Path("testdata/intents.jsonl")


def _client(settings: Settings) -> tuple[NearAiClient, CostMeter]:
    meter = CostMeter(settings.cost_cap_usd, settings.cost_state_path)
    return NearAiClient(settings, meter=meter), meter


def cmd_quote(_: argparse.Namespace) -> int:
    quote = oneclick.dry_quote()
    print(
        f"[dry quote] {quote.amount_in} USDC (Base) -> {quote.amount_out} USDC (NEAR), ~{quote.eta_seconds}s, floor {quote.min_amount_out}"
    )
    return 0


def cmd_transcribe(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    client, meter = _client(settings)
    result = client.transcribe(Path(args.audio).read_bytes(), filename=Path(args.audio).name, language=args.lang)
    print(f"text: {result.text!r} | language: {result.language} | duration: {result.duration_s}")
    print(f"estimated spend so far: ${meter.spent:.4f}")
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    """Measure intent extraction on a labelled file. Upstream failures abort: they must never count as results."""
    settings = Settings.from_env()
    client, meter = _client(settings)
    extractor = IntentExtractor(client, max_amount=settings.max_amount)
    cases = [json.loads(line) for line in Path(args.cases).read_text().splitlines() if line.strip()]
    by_category: dict[str, list[bool]] = defaultdict(list)
    failures: list[str] = []
    for case in cases:
        try:
            got = extractor.extract(case["text"]).intent.canonical()
        except UpstreamError as error:
            print(f"ABORT: upstream error, this run is not a valid pass rate: {error}", file=sys.stderr)
            return 2
        except WayfarerError:
            got = None
        expected = None if case["expect"] is None else _canonical_expectation(case["expect"], settings.max_amount)
        ok = got == expected
        by_category[case.get("category", "uncategorised")].append(ok)
        if not ok:
            failures.append(
                f"  FAIL [{case.get('category', '-')}] {case['text']!r}\n       expected {expected}\n       got      {got}"
            )
    total = sum(len(v) for v in by_category.values())
    passed = sum(sum(v) for v in by_category.values())
    for category, results in sorted(by_category.items()):
        print(f"{category:<16} {sum(results):>3}/{len(results):<3}")
    print("\n".join(failures))
    print(
        f"\n{passed}/{total} passed ({100 * passed / total:.0f}%) | model {settings.llm_model} | estimated spend ${meter.spent:.4f} of ${settings.cost_cap_usd}"
    )
    return 0


def _canonical_expectation(expect: dict[str, str], max_amount: Decimal) -> dict[str, str]:
    return parse_intent({"action": "send", **expect}, max_amount=max(max_amount, Decimal(10) ** 12)).canonical()


def cmd_attest(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    client, _ = _client(settings)
    extraction = IntentExtractor(client, max_amount=settings.max_amount).extract(args.text)
    attestation = attest_completion(client, extraction.completion)
    print(json.dumps({"intent": extraction.intent.canonical(), "attestation": attestation.to_json()}, indent=2))
    return 0 if attestation.signature_valid and attestation.text_matches_exchange else 1


def cmd_verify_receipt(args: argparse.Namespace) -> int:
    opening = ReceiptOpening.from_json(json.loads(Path(args.opening).read_text()))
    rpc = NearRpc(args.rpc)
    outcome = rpc.transaction_status(args.tx, args.signer)
    decimals = 24 if args.token_id == NATIVE_TOKEN_ID else args.decimals
    checks = verify_opening(opening, extract_events(outcome), vault_token_id=args.token_id, decimals=decimals)
    for check in checks:
        print(f"[{'ok' if check.ok else 'FAIL'}] {check.name} {check.detail}")
    return 0 if all(check.ok for check in checks) else 1


def cmd_export_csv(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    rows = ReceiptLedger(settings.ledger_path).export_csv(Path(args.out), explorer_base=TESTNET_EXPLORER)
    print(f"wrote {rows} rows to {args.out} (transcripts and salts are not included)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wayfarer", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("quote", help="fetch a dry NEAR Intents 1Click quote (no funds move)").set_defaults(func=cmd_quote)
    p = sub.add_parser("transcribe", help="transcribe an audio file with NEAR AI Cloud Whisper")
    p.add_argument("audio")
    p.add_argument("--lang", help="ISO 639-1 language hint, e.g. en")
    p.set_defaults(func=cmd_transcribe)
    p = sub.add_parser("eval", help="measure intent extraction on labelled cases")
    p.add_argument("--cases", default=str(DEFAULT_CASES))
    p.set_defaults(func=cmd_eval)
    p = sub.add_parser("attest", help="parse one instruction and verify the TEE signature on the model's answer")
    p.add_argument("text")
    p.set_defaults(func=cmd_attest)
    p = sub.add_parser("verify-receipt", help="verify an opened receipt against a vault transaction")
    p.add_argument("--opening", required=True, help="JSON file with the receipt opening")
    p.add_argument("--tx", required=True, help="transaction hash")
    p.add_argument("--signer", required=True, help="account that signed the transaction (the vault account)")
    p.add_argument("--token-id", default=NATIVE_TOKEN_ID)
    p.add_argument("--decimals", type=int, default=24)
    p.add_argument("--rpc", default=DEFAULT_TESTNET_RPC)
    p.set_defaults(func=cmd_verify_receipt)
    p = sub.add_parser("export-csv", help="export the receipt ledger as CSV (no transcripts)")
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_export_csv)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except WayfarerError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
