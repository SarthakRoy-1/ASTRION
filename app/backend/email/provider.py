"""Email provider abstraction.

One protocol, one concrete implementation (Resend), and a null provider that
logs and raises rather than silently doing nothing. The null provider is the
default when no API key is configured; code that catches `EmailDeliveryError`
and falls through to the token-in-response path does so explicitly, not by
accident.

No template logic lives here. Templates belong in `templates.py`. This module
is only about the transport.
"""

from __future__ import annotations

import logging
from typing import Protocol

logger = logging.getLogger("astrion.email")


class EmailDeliveryError(RuntimeError):
    """Raised when the provider could not deliver (or is not configured)."""


class EmailProvider(Protocol):
    """What any concrete provider must be able to do."""

    def send_verification_email(
        self,
        *,
        to_address: str,
        display_name: str,
        verification_url: str,
    ) -> None:
        """Deliver a verification email. Raises `EmailDeliveryError` on failure."""
        ...


class NullEmailProvider:
    """Used when no API key is configured.

    Raises rather than silently succeeding so callers that check the return
    value cannot be fooled into thinking delivery happened.
    """

    def send_verification_email(
        self,
        *,
        to_address: str,
        display_name: str,
        verification_url: str,
    ) -> None:
        logger.debug(
            "email.null_provider: no delivery configured (to=%s)", to_address
        )
        raise EmailDeliveryError(
            "No email provider is configured. Set RESEND_API_KEY to enable delivery."
        )
