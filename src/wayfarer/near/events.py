"""Parsing NEP-297 ``EVENT_JSON`` log lines from transaction outcomes."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

_PREFIX = "EVENT_JSON:"


def extract_events(outcome: Mapping[str, Any]) -> list[dict[str, Any]]:
    """All parseable events from every receipt of a final execution outcome, in order."""
    events: list[dict[str, Any]] = []
    for receipt in outcome.get("receipts_outcome", []):
        for line in receipt["outcome"].get("logs", []):
            if not line.startswith(_PREFIX):
                continue
            try:
                parsed = json.loads(line[len(_PREFIX) :])
            except ValueError:
                continue
            if isinstance(parsed, dict):
                events.append(parsed)
    return events


def failure_message(outcome: Mapping[str, Any]) -> str | None:
    """The contract's panic message if the transaction failed, a short description for other failures, else None."""
    status = outcome.get("status")
    failure = status.get("Failure") if isinstance(status, dict) else None
    if not failure:
        return None
    text = json.dumps(failure)
    marker = "Smart contract panicked: "
    if marker in text:
        return text.split(marker, 1)[1].split('"')[0]
    return text[:200]
