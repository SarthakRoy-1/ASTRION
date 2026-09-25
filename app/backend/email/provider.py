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


#: Logged at ERROR on every attempted send with no provider, because "the email
#: didn't send" with nothing in the log is how a missing key goes unnoticed.
NO_PROVIDER_LOG = (
    "email.null_provider: cannot send, RESEND_API_KEY is not set on this deployment"
)


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

    def send_verification_code(
        self,
        *,
        to_address: str,
        display_name: str,
        code: str,
        expires_minutes: int,
    ) -> None:
        """Deliver a one-time code. Raises `EmailDeliveryError` on failure.

        The code is a credential for as long as it lives. An implementation
        must never log it, and must never put it in an exception message.
        """
        ...

    def send_existing_account_notice(self, *, to_address: str) -> None:
        """Tell an address that it already has an account. Raises `EmailDeliveryError`.

        Sent when someone registers an address that is taken. It carries no code
        and no link, so whoever asked learns nothing they could use, and the
        mailbox owner learns that the request was made.
        """
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
        logger.error(NO_PROVIDER_LOG)
        raise EmailDeliveryError(
            "No email provider is configured. Set RESEND_API_KEY to enable delivery."
        )

    def send_verification_code(
        self,
        *,
        to_address: str,
        display_name: str,
        code: str,
        expires_minutes: int,
    ) -> None:
        logger.error(NO_PROVIDER_LOG)
        raise EmailDeliveryError(
            "No email provider is configured. Set RESEND_API_KEY to enable delivery."
        )

    def send_existing_account_notice(self, *, to_address: str) -> None:
        logger.error(NO_PROVIDER_LOG)
        raise EmailDeliveryError(
            "No email provider is configured. Set RESEND_API_KEY to enable delivery."
        )
