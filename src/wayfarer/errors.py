"""Exception hierarchy. Messages are written to be safe to show to an end user."""

from __future__ import annotations


class WayfarerError(Exception):
    """Base class for every error raised deliberately by this package."""


class InvalidIntentError(WayfarerError):
    """The user's instruction is not a payment we are willing to attempt."""


class UpstreamError(WayfarerError):
    """An external service (NEAR AI Cloud, NEAR RPC, Telegram) failed or did not answer."""


class BudgetExceededError(WayfarerError):
    """The configured spending cap for paid API calls would be exceeded."""


class ReceiptError(WayfarerError):
    """A receipt could not be created, stored or verified."""


class TapError(WayfarerError):
    """A tap-to-pay message was malformed, expired, inconsistent or declined."""
