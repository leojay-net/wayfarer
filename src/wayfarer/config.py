"""Runtime configuration, read once from the environment."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

DEFAULT_BASE_URL = "https://cloud-api.near.ai/v1"
DEFAULT_ASR_MODEL = "openai/whisper-large-v3"
DEFAULT_LLM_MODEL = "z-ai/glm-5.3-flash"
DEFAULT_ASR_TIMEOUT_S = 45.0
DEFAULT_LLM_TIMEOUT_S = 60.0
DEFAULT_MAX_AMOUNT = Decimal(1000)
DEFAULT_COST_CAP_USD = Decimal("2.00")
DEFAULT_LEDGER = Path(".wayfarer/ledger.jsonl")
DEFAULT_COST_STATE = Path(".wayfarer/cost.json")


@dataclass(frozen=True, slots=True)
class Settings:
    """Immutable settings. Secrets are excluded from ``repr`` so they never reach logs."""

    near_ai_api_key: str = field(repr=False)
    near_ai_base_url: str = DEFAULT_BASE_URL
    asr_model: str = DEFAULT_ASR_MODEL
    llm_model: str = DEFAULT_LLM_MODEL
    asr_timeout_s: float = DEFAULT_ASR_TIMEOUT_S
    llm_timeout_s: float = DEFAULT_LLM_TIMEOUT_S
    max_amount: Decimal = DEFAULT_MAX_AMOUNT
    language: str | None = None
    cost_cap_usd: Decimal = DEFAULT_COST_CAP_USD
    cost_state_path: Path | None = None
    ledger_path: Path = DEFAULT_LEDGER

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        """Build settings from environment variables (``NEAR_AI_API_KEY`` is required for network calls)."""
        e = os.environ if env is None else env
        state = e.get("WAYFARER_COST_STATE")
        return cls(
            near_ai_api_key=e.get("NEAR_AI_API_KEY", ""),
            near_ai_base_url=e.get("NEAR_AI_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
            asr_model=e.get("WAYFARER_ASR_MODEL", DEFAULT_ASR_MODEL),
            llm_model=e.get("WAYFARER_LLM_MODEL", DEFAULT_LLM_MODEL),
            asr_timeout_s=float(e.get("WAYFARER_ASR_TIMEOUT", str(DEFAULT_ASR_TIMEOUT_S))),
            llm_timeout_s=float(e.get("WAYFARER_LLM_TIMEOUT", str(DEFAULT_LLM_TIMEOUT_S))),
            max_amount=Decimal(e.get("WAYFARER_MAX_AMOUNT", str(DEFAULT_MAX_AMOUNT))),
            language=e.get("WAYFARER_LANG") or None,
            cost_cap_usd=Decimal(e.get("WAYFARER_COST_CAP_USD", str(DEFAULT_COST_CAP_USD))),
            cost_state_path=Path(state) if state else DEFAULT_COST_STATE,
            ledger_path=Path(e.get("WAYFARER_LEDGER", str(DEFAULT_LEDGER))),
        )
