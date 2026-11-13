"""NEAR JSON-RPC client."""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from typing import Any

from wayfarer.crypto.base58 import b58decode
from wayfarer.errors import UpstreamError
from wayfarer.near.keys import SigningKey
from wayfarer.near.transaction import (
    action_function_call,
    encode_transaction,
    sign_transaction,
    to_base64,
)
from wayfarer.transport import Transport, build_request, urllib_transport

DEFAULT_TESTNET_RPC = "https://rpc.testnet.near.org"
DEFAULT_GAS = 50 * 10**12


class NearRpc:
    def __init__(
        self,
        url: str = DEFAULT_TESTNET_RPC,
        *,
        transport: Transport = urllib_transport,
        timeout_s: float = 60.0,
    ) -> None:
        self._url = url
        self._transport = transport
        self._timeout = timeout_s

    def call(self, method: str, params: Mapping[str, Any] | list[Any]) -> Any:
        body = json.dumps({"jsonrpc": "2.0", "id": "wayfarer", "method": method, "params": params}).encode()
        response = self._transport(
            build_request(self._url, body=body, headers={"Content-Type": "application/json"}), self._timeout
        )
        try:
            payload = json.loads(response.body)
        except ValueError as error:
            raise UpstreamError("The NEAR RPC sent an unreadable answer.") from error
        if "error" in payload:
            raise UpstreamError(f"NEAR RPC error: {json.dumps(payload['error'])[:240]}")
        return payload["result"]

    def view_function(self, account_id: str, method: str, args: Mapping[str, Any]) -> Any:
        result = self.call(
            "query",
            {
                "request_type": "call_function",
                "finality": "final",
                "account_id": account_id,
                "method_name": method,
                "args_base64": base64.b64encode(json.dumps(args).encode()).decode(),
            },
        )
        return json.loads(bytes(result["result"]))

    def function_call(
        self,
        key: SigningKey,
        receiver_id: str,
        method: str,
        args: Mapping[str, Any],
        *,
        gas: int = DEFAULT_GAS,
        deposit: int = 0,
    ) -> dict[str, Any]:
        """Sign and send one function call; returns once every receipt has finalised."""
        return self.send_actions(key, receiver_id, [action_function_call(method, args, gas, deposit)])

    def send_actions(self, key: SigningKey, receiver_id: str, actions: list[bytes]) -> dict[str, Any]:
        """Sign ``actions`` (see ``near.transaction.action_*``) with ``key`` and send them as one transaction."""
        access_key = self.call(
            "query",
            {
                "request_type": "view_access_key",
                "finality": "final",
                "account_id": key.account_id,
                "public_key": key.public_key_str,
            },
        )
        block_hash = b58decode(self.call("block", {"finality": "final"})["header"]["hash"])
        unsigned = encode_transaction(
            signer_id=key.account_id,
            key_type=key.key_type,
            public_key=key.public_key,
            nonce=int(access_key["nonce"]) + 1,
            receiver_id=receiver_id,
            block_hash=block_hash,
            actions=actions,
        )
        signed = to_base64(sign_transaction(key, unsigned))
        return dict(self.call("send_tx", {"signed_tx_base64": signed, "wait_until": "FINAL"}))

    def access_keys(self, account_id: str) -> list[dict[str, Any]]:
        """The account's access keys as the chain lists them (ML-DSA keys appear as ``ml-dsa-65-hash:`` handles)."""
        result = self.call(
            "query", {"request_type": "view_access_key_list", "finality": "final", "account_id": account_id}
        )
        return [dict(k) for k in result["keys"]]

    def transaction_status(self, tx_hash: str, sender_id: str) -> dict[str, Any]:
        return dict(self.call("tx", {"tx_hash": tx_hash, "sender_account_id": sender_id, "wait_until": "FINAL"}))
