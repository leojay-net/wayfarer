"""Clients for the quantum-safe side of ``wayfarer-vault`` v2: owner administration and cheque settlement.

* :class:`PqVaultAdmin` is used by the owner (ideally an account whose only keys are ML-DSA-65) to set the policy and
  register or revoke ML-DSA agent keys, i.e. to write the mandate.
* :class:`ChequeRelayer` is used by anyone (typically the merchant's app) to submit a signed cheque and pay the gas.
  The ML-DSA signature, not the submitting account, is the authority; the contract decides whether it is valid.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from wayfarer.cheque import Cheque, key_id
from wayfarer.errors import UpstreamError, WayfarerError
from wayfarer.near.events import extract_events, failure_message
from wayfarer.near.keys import SigningKey
from wayfarer.near.rpc import NearRpc
from wayfarer.vault import NATIVE_TOKEN_ID, TESTNET_EXPLORER, PaymentResult, PaymentStatus, result_from_outcome

SETTLE_GAS = 100 * 10**12  # prepaid; a native-NEAR cheque burned 7.2 Tgas in the sandbox, the rest is refunded


@dataclass(frozen=True, slots=True)
class MandateTerms:
    """What one agent key may do, as reported by the contract's ``get_mandate`` view."""

    vault: str
    owner: str
    key_id: str
    paused: bool
    expires_at_ms: int
    min_interval_ms: int
    last_payment_ms: int
    token_id: str
    per_tx_max: int
    daily_max: int
    allowlist_enabled: bool
    allowlist: tuple[str, ...]

    @classmethod
    def from_view(cls, raw: Mapping[str, Any]) -> MandateTerms:
        return cls(
            vault=str(raw["vault"]),
            owner=str(raw["owner"]),
            key_id=str(raw["key_id"]),
            paused=bool(raw["paused"]),
            expires_at_ms=int(raw["expires_at_ms"]),
            min_interval_ms=int(raw["min_interval_ms"]),
            last_payment_ms=int(raw["last_payment_ms"]),
            token_id=str(raw["token_id"]),
            per_tx_max=int(raw["per_tx_max"]),
            daily_max=int(raw["daily_max"]),
            allowlist_enabled=bool(raw["allowlist_enabled"]),
            allowlist=tuple(str(a) for a in raw["allowlist"]),
        )


def _require_success(outcome: Mapping[str, Any], what: str) -> str:
    refusal = failure_message(outcome)
    if refusal:
        raise WayfarerError(f"{what} was refused: {refusal}")
    return str(outcome["transaction"]["hash"])


class PqVaultAdmin:
    def __init__(self, vault_account: str, owner_key: SigningKey, rpc: NearRpc) -> None:
        self._vault = vault_account
        self._key = owner_key
        self._rpc = rpc

    def set_policy(self, token_id: str, per_tx_max: int, daily_max: int) -> str:
        """Set the per-payment and daily caps (smallest unit). Returns the transaction hash."""
        args = {"token_id": token_id, "per_tx_max": str(per_tx_max), "daily_max": str(daily_max)}
        return _require_success(self._rpc.function_call(self._key, self._vault, "set_policy", args), "set_policy")

    def add_agent(self, public_key: bytes, *, expires_at_ms: int = 0, min_interval_ms: int = 0) -> tuple[str, str]:
        """Register an ML-DSA-65 agent key. Returns ``(key_id, transaction hash)``."""
        args = {
            "public_key": base64.b64encode(public_key).decode(),
            "expires_at_ms": str(expires_at_ms),
            "min_interval_ms": str(min_interval_ms),
        }
        outcome = self._rpc.function_call(self._key, self._vault, "add_pq_agent", args)
        tx = _require_success(outcome, "add_pq_agent")
        expected = key_id(public_key)
        added = [e for e in extract_events(outcome) if e.get("event") == "agent_added"]
        if not added or added[0]["data"].get("key_id") != expected:
            raise WayfarerError("The vault did not confirm the agent key; check the transaction.")
        return expected, tx

    def revoke_agent(self, agent_key_id: str) -> str:
        outcome = self._rpc.function_call(self._key, self._vault, "revoke_pq_agent", {"key_id": agent_key_id})
        return _require_success(outcome, "revoke_pq_agent")


class ChequeRelayer:
    """Submits cheques. The relayer's own key only pays gas; it has no say in whether a cheque is valid."""

    def __init__(
        self, vault_account: str, relayer_key: SigningKey, rpc: NearRpc, *, explorer_base: str = TESTNET_EXPLORER
    ) -> None:
        self._vault = vault_account
        self._key = relayer_key
        self._rpc = rpc
        self._explorer = explorer_base

    def mandate(self, agent_key_id: str, token_id: str) -> MandateTerms | None:
        raw = self._rpc.view_function(self._vault, "get_mandate", {"key_id": agent_key_id, "token_id": token_id})
        return None if raw is None else MandateTerms.from_view(raw)

    def settle(self, cheque: Cheque, *, agent_key_id: str, signature: bytes) -> PaymentResult:
        """Send the cheque to the vault. ``PAID`` only after the contract's own ``payment_settled`` event."""
        if cheque.vault != self._vault:
            raise WayfarerError("This cheque is for a different vault.")
        args = {
            "intent_id": cheque.intent_id,
            "token_id": cheque.token_id,
            "to": cheque.to,
            "amount": str(cheque.amount),
            "expires_at_ms": str(cheque.expires_at_ms),
            "receipt_hash": cheque.receipt_hash,
            "key_id": agent_key_id,
            "signature": base64.b64encode(signature).decode(),
        }
        try:
            outcome = self._rpc.function_call(self._key, self._vault, "pay_with_cheque", args, gas=SETTLE_GAS)
        except UpstreamError as error:
            return PaymentResult(PaymentStatus.ERROR, str(error))
        return result_from_outcome(outcome, self._explorer)


class VaultChainView:
    """Read-only vault queries over RPC, shaped for the merchant's strong check."""

    def __init__(self, vault_account: str, rpc: NearRpc) -> None:
        self._vault = vault_account
        self._rpc = rpc

    def mandate(self, key_id: str, token_id: str) -> MandateTerms | None:
        raw = self._rpc.view_function(self._vault, "get_mandate", {"key_id": key_id, "token_id": token_id})
        return None if raw is None else MandateTerms.from_view(raw)

    def remaining_today(self, token_id: str) -> int | None:
        raw = self._rpc.view_function(self._vault, "get_budget", {"token_id": token_id})
        return None if raw is None else int(raw[1])

    def spendable_balance(self, token_id: str) -> int:
        """Native NEAR above the storage reserve. Token balances are not read here (not supported yet)."""
        if token_id != NATIVE_TOKEN_ID:
            raise WayfarerError("Only native NEAR balances can be checked.")
        info = self._rpc.call("query", {"request_type": "view_account", "finality": "final", "account_id": self._vault})
        reserve = int(info["storage_usage"]) * 10**19
        return max(0, int(info["amount"]) - reserve)

    def intent_used(self, intent_id: str) -> bool:
        return bool(self._rpc.view_function(self._vault, "intent_used", {"intent_id": intent_id}))
