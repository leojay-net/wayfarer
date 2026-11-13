"""Hiding receipts with selective disclosure.

Idea adapted from commitment/nullifier privacy schemes: what goes on-chain is only a
*salted commitment* to the user's instruction and a *nullifier* used as the one-time payment id. Because the salt is
random and kept by the user, nobody can recover or even guess the instruction ("send 5 NEAR to ada.near") from the
public chain, and payments cannot be linked to a chat. The user can later *open* a single receipt to an accountant or
auditor, who can verify it against the chain without seeing anything else.

This is hiding + binding via SHA-256; it is not a zero-knowledge proof system.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import secrets
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from wayfarer.errors import ReceiptError
from wayfarer.intent import PaymentIntent

_COMMIT_DOMAIN = b"wayfarer/receipt/v1"
_NULLIFIER_DOMAIN = b"wayfarer/nullifier/v1"
COMMITMENT_PREFIX = "wf1:"
NULLIFIER_PREFIX = "wfn:"
SALT_BYTES = 32


def _frame(*parts: bytes) -> bytes:
    """Length-prefix every part so different field splits can never produce the same byte string."""
    return b"".join(len(part).to_bytes(4, "big") + part for part in parts)


@dataclass(frozen=True, slots=True)
class ReceiptOpening:
    """Everything needed to prove one receipt. Keep it private; share it only with whoever must verify."""

    salt: bytes
    transcript: str
    intent: dict[str, str]

    @classmethod
    def create(cls, transcript: str, intent: PaymentIntent) -> ReceiptOpening:
        return cls(secrets.token_bytes(SALT_BYTES), transcript, intent.canonical())

    @property
    def commitment(self) -> str:
        """Public, hiding commitment placed on-chain as ``receipt_hash``."""
        body = _frame(
            _COMMIT_DOMAIN,
            self.salt,
            self.transcript.encode(),
            json.dumps(self.intent, sort_keys=True, separators=(",", ":")).encode(),
        )
        return COMMITMENT_PREFIX + hashlib.sha256(body).hexdigest()

    @property
    def nullifier(self) -> str:
        """One-time payment id placed on-chain as ``intent_id``; unlinkable to the chat or the commitment."""
        return NULLIFIER_PREFIX + hashlib.sha256(_frame(_NULLIFIER_DOMAIN, self.salt)).hexdigest()[:48]

    def to_json(self) -> dict[str, Any]:
        return {"version": 1, "salt": self.salt.hex(), "transcript": self.transcript, "intent": self.intent}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ReceiptOpening:
        try:
            if data["version"] != 1:
                raise ReceiptError("Unsupported receipt version.")
            salt = bytes.fromhex(data["salt"])
            intent = {str(k): str(v) for k, v in data["intent"].items()}
            return cls(salt, str(data["transcript"]), intent)
        except (KeyError, ValueError, AttributeError, TypeError) as error:
            raise ReceiptError("Malformed receipt opening.") from error


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    ok: bool
    detail: str = ""


def verify_opening(
    opening: ReceiptOpening, events: list[dict[str, Any]], *, vault_token_id: str, decimals: int
) -> list[Check]:
    """Check an opened receipt against the vault's on-chain ``payment_authorized`` events.

    ``events`` are the parsed ``EVENT_JSON`` payloads of one transaction.
    """
    authorised = [
        e["data"] for e in events if e.get("standard") == "wayfarer_vault" and e.get("event") == "payment_authorized"
    ]
    matching = [d for d in authorised if d.get("receipt_hash") == opening.commitment]
    checks = [Check("commitment appears on-chain", bool(matching), opening.commitment)]
    if not matching:
        return checks
    data = matching[0]
    expected_units = int(Decimal(opening.intent["amount"]).scaleb(decimals))
    checks.append(Check("nullifier matches payment id", data.get("intent_id") == opening.nullifier))
    checks.append(Check("token matches", data.get("token") == vault_token_id, str(data.get("token"))))
    checks.append(Check("amount matches", data.get("amount") == str(expected_units), str(data.get("amount"))))
    checks.append(
        Check(
            "recipient matches",
            opening.intent["recipient"] in (data.get("to"), data.get("declared_recipient")),
            str(data.get("to")),
        )
    )
    return checks


class ReceiptLedger:
    """Append-only local log (JSON lines, mode 0600) of proposals and outcomes. The ledger holds openings: protect it."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()

    def append(self, event: str, opening: ReceiptOpening, **fields: Any) -> None:
        record = {
            "time": datetime.now(UTC).isoformat(timespec="seconds"),
            "event": event,
            "nullifier": opening.nullifier,
            "commitment": opening.commitment,
            **fields,
        }
        if event == "proposed":
            record["opening"] = opening.to_json()
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self._path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, sort_keys=True) + "\n")

    def records(self) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        return [json.loads(line) for line in self._path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def find_opening(self, nullifier: str) -> ReceiptOpening:
        for record in self.records():
            if record["event"] == "proposed" and record["nullifier"] == nullifier:
                return ReceiptOpening.from_json(record["opening"])
        raise ReceiptError("No such receipt in the ledger.")

    def export_csv(self, destination: Path, *, explorer_base: str) -> int:
        """Write a summary an accountant can open. It deliberately omits transcripts and salts. Returns the row count."""
        proposals: dict[str, dict[str, Any]] = {}
        outcomes: dict[str, dict[str, Any]] = {}
        for record in self.records():
            if record["event"] == "proposed":
                proposals[record["nullifier"]] = record
            else:
                outcomes[record["nullifier"]] = record
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                [
                    "time_utc",
                    "status",
                    "token",
                    "amount",
                    "recipient",
                    "tx_hash",
                    "explorer_url",
                    "commitment",
                    "model_signature_valid",
                ]
            )
            for nullifier, proposal in proposals.items():
                intent = proposal["opening"]["intent"]
                outcome = outcomes.get(nullifier, {})
                tx = outcome.get("tx_hash", "")
                attestation = proposal.get("attestation") or {}
                writer.writerow(
                    [
                        proposal["time"],
                        outcome.get("event", "proposed"),
                        intent["token"],
                        intent["amount"],
                        intent["recipient"],
                        tx,
                        f"{explorer_base}{tx}" if tx else "",
                        proposal["commitment"],
                        attestation.get("signature_valid", ""),
                    ]
                )
        return len(proposals)


__all__ = [
    "COMMITMENT_PREFIX",
    "NULLIFIER_PREFIX",
    "Check",
    "ReceiptLedger",
    "ReceiptOpening",
    "verify_opening",
]
