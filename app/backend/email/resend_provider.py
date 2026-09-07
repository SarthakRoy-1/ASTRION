"""Resend email provider.

Imported lazily (only when RESEND_API_KEY is present) so the entire test
suite runs without the SDK installed or a network connection. The SDK itself
is imported inside the method body for the same reason.

Failure contract: any Resend API error becomes `EmailDeliveryError`. The API
key is never logged, never included in error messages.
"""

from __future__ import annotations

import logging

from app.backend.email.provider import EmailDeliveryError
from app.backend.email.templates import verification_email_html, verification_email_text

logger = logging.getLogger("astrion.email.resend")


class ResendEmailProvider:
    """Sends transactional email via the Resend API."""

    def __init__(self, *, api_key: str, from_address: str) -> None:
        self._api_key = api_key
        self._from_address = from_address

    def send_verification_email(
        self,
        *,
        to_address: str,
        display_name: str,
        verification_url: str,
    ) -> None:
        try:
            import resend
        except ImportError as exc:
            raise EmailDeliveryError(
                "resend package is not installed. Add resend to requirements.txt."
            ) from exc

        resend.api_key = self._api_key  # type: ignore[attr-defined]

        try:
            params: resend.Emails.SendParams = {  # type: ignore[attr-defined]
                "from": self._from_address,
                "to": [to_address],
                "subject": "Confirm your ASTRION email address",
                "html": verification_email_html(
                    display_name=display_name,
                    verification_url=verification_url,
                ),
                "text": verification_email_text(
                    display_name=display_name,
                    verification_url=verification_url,
                ),
            }
            resend.Emails.send(params)  # type: ignore[attr-defined]
            logger.info("email.resend: verification sent to=%s", to_address)
        except Exception as exc:
            # Never include the API key in the message; exc may contain it via
            # the HTTP request body in some SDK versions.
            logger.error(
                "email.resend: delivery failed to=%s reason=%s",
                to_address,
                type(exc).__name__,
            )
            raise EmailDeliveryError(
                f"Email delivery failed ({type(exc).__name__}). "
                "The verification link will be returned in the response "
                "if this is not a production deployment."
            ) from exc
