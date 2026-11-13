"""Spend tracking with a hard cap, so automated tests cannot burn through a limited credit balance.

Costs are *estimates* computed from token usage and the catalogue prices (speech is estimated per minute).
The meter is a safety net, not an invoice; the NEAR AI Cloud dashboard is the source of truth.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from wayfarer.errors import BudgetExceededError

# Conservative fallbacks (USD per token) used when the model catalogue cannot be read.
FALLBACK_PROMPT_USD = Decimal("0.000002")
FALLBACK_COMPLETION_USD = Decimal("0.000006")
# Whisper is billed in a unit we could not confirm from the docs; assume a deliberately high per-minute price.
WHISPER_USD_PER_MINUTE = Decimal("0.006")


@dataclass(frozen=True, slots=True)
class Pricing:
    """USD per token for one model."""

    prompt: Decimal
    completion: Decimal

    def chat_cost(self, prompt_tokens: int, completion_tokens: int) -> Decimal:
        return self.prompt * prompt_tokens + self.completion * completion_tokens


class CostMeter:
    """Accumulates estimated spend, optionally persisted across runs, and refuses to exceed ``cap_usd``."""

    def __init__(self, cap_usd: Decimal, state_path: Path | None = None) -> None:
        self._cap = cap_usd
        self._path = state_path
        self._lock = threading.Lock()
        self._spent = Decimal(0)
        self._calls = 0
        if state_path is not None and state_path.exists():
            state = json.loads(state_path.read_text())
            self._spent = Decimal(state["spent_usd"])
            self._calls = int(state["calls"])

    @property
    def spent(self) -> Decimal:
        return self._spent

    @property
    def calls(self) -> int:
        return self._calls

    @property
    def remaining(self) -> Decimal:
        return self._cap - self._spent

    def ensure_headroom(self, estimate: Decimal = Decimal("0.01")) -> None:
        """Raise before a call if the cap would be reached."""
        if self._spent + estimate > self._cap:
            raise BudgetExceededError(
                f"Spending cap reached: spent ~${self._spent:.4f} of ${self._cap:.2f}. Raise WAYFARER_COST_CAP_USD to continue."
            )

    def record(self, usd: Decimal) -> None:
        """Add the estimated cost of a completed call and persist the total."""
        with self._lock:
            self._spent += usd
            self._calls += 1
            self._persist()

    def _persist(self) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"spent_usd": str(self._spent), "calls": self._calls}))
        os.replace(tmp, self._path)
