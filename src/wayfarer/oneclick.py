"""NEAR Intents 1Click API client: quote, deposit notification and status.

1Click has no testnet and rejects testnet refund addresses, so real settlement means mainnet funds. This module only
*talks to* the API: it never signs or sends a deposit. A non-dry quote creates a deposit address but moves nothing; funds
move only if someone later transfers to that address. No Wayfarer code path does that today.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from wayfarer.errors import UpstreamError
from wayfarer.transport import Transport, build_request, urllib_transport

BASE_URL = "https://1click.chaindefuser.com"
USDC_BASE = "nep141:base-0x833589fcd6edb6e08f4c7c32d4f71b54bda02913.omft.near"
USDC_NEAR = "nep141:17208628f84f5d6ad33f0da3bbbeb27ffcb398eac501a31bd6ad2011e36133a1"
WNEAR = "nep141:wrap.near"
# A well-known public EVM address used only as a placeholder refund address in dry quotes.
PLACEHOLDER_REFUND = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
REQUEST_TIMEOUT_S = 30.0
DEFAULT_SLIPPAGE_BPS = 100
QUOTE_VALIDITY = timedelta(hours=1)


class SwapStatus(StrEnum):
    PENDING_DEPOSIT = "PENDING_DEPOSIT"
    KNOWN_DEPOSIT_TX = "KNOWN_DEPOSIT_TX"
    PROCESSING = "PROCESSING"
    SUCCESS = "SUCCESS"
    INCOMPLETE_DEPOSIT = "INCOMPLETE_DEPOSIT"
    REFUNDED = "REFUNDED"
    FAILED = "FAILED"

    @property
    def terminal(self) -> bool:
        return self in {SwapStatus.SUCCESS, SwapStatus.REFUNDED, SwapStatus.FAILED}


@dataclass(frozen=True, slots=True)
class QuoteRequest:
    """What to swap. Amounts are in the origin token's smallest unit."""

    origin_asset: str
    destination_asset: str
    amount: str
    recipient: str
    refund_to: str
    recipient_type: str = "DESTINATION_CHAIN"
    refund_type: str = "ORIGIN_CHAIN"
    slippage_bps: int = DEFAULT_SLIPPAGE_BPS


@dataclass(frozen=True, slots=True)
class Quote:
    amount_in: str
    amount_out: str
    min_amount_out: str
    eta_seconds: int | None
    deposit_address: str | None  # None for dry quotes
    deadline: str | None


class OneClick:
    def __init__(self, *, transport: Transport = urllib_transport, api_key: str | None = None) -> None:
        self._transport = transport
        self._api_key = api_key

    def quote(self, request: QuoteRequest, *, dry: bool = True) -> Quote:
        """A dry quote prices a swap. A non-dry quote also reserves a deposit address; it still moves no funds."""
        deadline = (datetime.now(UTC) + QUOTE_VALIDITY).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        body = {
            "dry": dry,
            "swapType": "EXACT_INPUT",
            "slippageTolerance": request.slippage_bps,
            "originAsset": request.origin_asset,
            "depositType": "ORIGIN_CHAIN",
            "destinationAsset": request.destination_asset,
            "amount": request.amount,
            "recipient": request.recipient,
            "recipientType": request.recipient_type,
            "refundTo": request.refund_to,
            "refundType": request.refund_type,
            "deadline": deadline,
        }
        quote = self._call("POST", "/v0/quote", body)["quote"]
        deposit_address = quote.get("depositAddress")
        if not dry and not deposit_address:
            raise UpstreamError("1Click returned no deposit address for a non-dry quote.")
        return Quote(
            amount_in=quote["amountInFormatted"],
            amount_out=quote["amountOutFormatted"],
            min_amount_out=str(quote["minAmountOut"]),
            eta_seconds=quote.get("timeEstimate"),
            deposit_address=deposit_address,
            deadline=quote.get("deadline"),
        )

    def submit_deposit(self, *, tx_hash: str, deposit_address: str) -> None:
        """Tell 1Click which transaction funded a deposit address (speeds up detection)."""
        self._call("POST", "/v0/deposit/submit", {"txHash": tx_hash, "depositAddress": deposit_address})

    def status(self, deposit_address: str) -> SwapStatus:
        data = self._call("GET", f"/v0/status?depositAddress={deposit_address}")
        try:
            return SwapStatus(data["status"])
        except (KeyError, ValueError) as error:
            raise UpstreamError(f"1Click sent an unknown status: {data.get('status')!r}") from error

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["X-API-Key"] = self._api_key
        request = build_request(
            BASE_URL + path, body=None if body is None else json.dumps(body).encode(), headers=headers, method=method
        )
        response = self._transport(request, REQUEST_TIMEOUT_S)
        try:
            data = json.loads(response.body)
        except ValueError as error:
            raise UpstreamError("1Click sent an unreadable answer.") from error
        if response.status not in (200, 201) or not isinstance(data, dict):
            message = data.get("message", "") if isinstance(data, dict) else ""
            raise UpstreamError(f"1Click answered {response.status}: {str(message)[:160]}")
        return data


def dry_quote(
    *,
    amount: str = "5000000",
    recipient: str = "ada.near",
    transport: Transport = urllib_transport,
    api_key: str | None = None,
) -> Quote:
    """Quote USDC on Base -> USDC on NEAR (``amount`` in 6-decimal units). Used by ``wayfarer quote``."""
    request = QuoteRequest(USDC_BASE, USDC_NEAR, amount, recipient, PLACEHOLDER_REFUND)
    return OneClick(transport=transport, api_key=api_key).quote(request, dry=True)
