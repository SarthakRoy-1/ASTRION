"""Resend email provider.

Imported lazily (only when RESEND_API_KEY is present) so the entire test
suite runs without the SDK installed or a network connection. The SDK itself
is imported inside the method body for the same reason.

Failure contract: any Resend API error becomes `EmailDeliveryError`. The API
key is never logged, never included in error messages.
"""

from __future__ import annotations

import logging
import re

from app.backend.email.provider import EmailDeliveryError
from app.backend.email.templates import (
    VERIFICATION_CODE_SUBJECT,
    verification_code_email_html,
    verification_code_email_text,
    verification_email_html,
    verification_email_text,
)

logger = logging.getLogger("astrion.email.resend")

_DIGIT_RUN = re.compile(r"\d{4,}")


def describe_failure(exc: BaseException) -> str:
    """What to log about a failed send: the provider's own reason, and nothing else.

    A Resend API rejection ("the domain is not verified", "you can only send to
    your own address while testing", a bad key) carries its reason in the SDK's
    error object, which comes from the API's *response*. That is the one thing
    an operator needs and the old log line ("ResendError") discarded. Anything
    that is not the provider's own error (a network failure, say) is reported
    by class name only, because those messages can echo the request. Digit runs
    are masked in any case, so a code could never reach a log.
    """
    name = type(exc).__name__
    if not type(exc).__module__.startswith("resend"):
        return name
    parts = [name]
    for attribute in ("code", "error_type", "message"):
        value = getattr(exc, attribute, None)
        if value not in (None, ""):
            parts.append(f"{attribute}={_DIGIT_RUN.sub('#', str(value))[:300]}")
    return " ".join(parts)


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
                describe_failure(exc),
            )
            raise EmailDeliveryError(
                f"Email delivery failed ({type(exc).__name__}). "
                "The verification link will be returned in the response "
                "if this is not a production deployment."
            ) from exc

    def send_verification_code(
        self,
        *,
        to_address: str,
        display_name: str,
        code: str,
        expires_minutes: int,
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
                "subject": VERIFICATION_CODE_SUBJECT,
                "html": verification_code_email_html(
                    display_name=display_name, code=code, expires_minutes=expires_minutes
                ),
                "text": verification_code_email_text(
                    display_name=display_name, code=code, expires_minutes=expires_minutes
                ),
            }
            resend.Emails.send(params)  # type: ignore[attr-defined]
            # The address's domain only: the log is not a record of who signed
            # up, and it must never hold the code.
            logger.info(
                "email.resend: verification code sent domain=%s",
                to_address.rsplit("@", 1)[-1],
            )
        except Exception as exc:
            # Only the provider's own reason is logged (see `describe_failure`);
            # the raw exception text is not, because some SDK versions echo the
            # request body, which carries the code.
            logger.error(
                "email.resend: code delivery failed reason=%s", describe_failure(exc)
            )
            raise EmailDeliveryError(
                f"Email delivery failed ({type(exc).__name__})."
            ) from None
