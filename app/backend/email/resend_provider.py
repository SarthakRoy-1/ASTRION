"""Resend email provider.

Imported lazily (only when RESEND_API_KEY is present) so the entire test
suite runs without the SDK installed or a network connection. The SDK itself
is imported inside the method body for the same reason.

**What "sent" means.** A send counts as delivered to the provider only when
Resend has answered with an `id` for the message. The SDK raises on any HTTP
status of 400 or more, and a response with no `id` is treated as a failure too:
the caller turns this into "the email was sent", which the interface then tells
a person, so it must not be true on a guess.

Failure contract: any Resend API error becomes `EmailDeliveryError`. The API
key is never logged, never included in error messages.
"""

from __future__ import annotations

import logging
import re

from app.backend.email.provider import EmailDeliveryError
from app.backend.email.templates import (
    EXISTING_ACCOUNT_SUBJECT,
    VERIFICATION_CODE_SUBJECT,
    existing_account_email_html,
    existing_account_email_text,
    verification_code_email_html,
    verification_code_email_text,
    verification_email_html,
    verification_email_text,
)

logger = logging.getLogger("astrion.email.resend")

#: A single request must not hold a worker for the SDK's default half minute.
REQUEST_TIMEOUT_SECONDS = 10

_DIGIT_RUN = re.compile(r"\d{4,}")
_ADDRESS = re.compile(r"[^\s<>\"'()\[\],;]+@[^\s<>\"'()\[\],;]+")


def _sanitise(value: object) -> str:
    """A provider's text, made safe for a log line: no addresses, no long numbers."""
    text = _ADDRESS.sub("<address>", str(value))
    return _DIGIT_RUN.sub("#", text)[:300]


def describe_failure(exc: BaseException) -> str:
    """What to log about a failed send: the provider's own reason, and nothing else.

    A Resend API rejection ("the domain is not verified", "you can only send to
    your own address while testing", a bad key) carries its reason in the SDK's
    error object, which comes from the API's *response*. That is the one thing
    an operator needs and the old log line ("ResendError") discarded. Anything
    that is not the provider's own error (a network failure, say) is reported
    by class name only, because those messages can echo the request. Addresses
    (Resend's testing-mode refusal names the account owner's own) and digit runs
    (a code) are masked in any case.
    """
    name = type(exc).__name__
    if not type(exc).__module__.startswith("resend"):
        return name
    parts = [name]
    for attribute in ("code", "error_type", "message"):
        value = getattr(exc, attribute, None)
        if value not in (None, ""):
            parts.append(f"{attribute}={_sanitise(value)}")
    return " ".join(parts)


class ResendEmailProvider:
    """Sends transactional email via the Resend API."""

    def __init__(self, *, api_key: str, from_address: str) -> None:
        self._api_key = api_key
        self._from_address = from_address

    # -- the one place a request is made -----------------------------------------

    def _deliver(
        self, *, kind: str, to_address: str, subject: str, html: str, text: str
    ) -> None:
        """POST /emails, and return only if Resend accepted the message.

        The exception text is not logged or re-raised as it stands: some SDK
        versions echo the request body, which can carry a code. What is logged is
        `describe_failure`'s sanitised reason.
        """
        try:
            import resend
        except ImportError as exc:
            logger.error("email.resend: the resend package is not installed")
            raise EmailDeliveryError(
                "resend package is not installed. Add resend to requirements.txt."
            ) from exc

        resend.api_key = self._api_key  # type: ignore[attr-defined]
        _bound_the_request_time(resend)

        try:
            response = resend.Emails.send(  # type: ignore[attr-defined]
                {
                    "from": self._from_address,
                    "to": [to_address],
                    "subject": subject,
                    "html": html,
                    "text": text,
                }
            )
            message_id = response.get("id") if hasattr(response, "get") else None
        except Exception as exc:
            logger.error("email.resend: %s failed reason=%s", kind, describe_failure(exc))
            raise EmailDeliveryError(f"Email delivery failed ({type(exc).__name__}).") from None

        if not message_id:
            logger.error("email.resend: %s failed reason=no message id in the response", kind)
            raise EmailDeliveryError("Email delivery failed (no message id).")

        # The address's domain only: the log is not a record of who signed up,
        # and it must never hold a code.
        logger.info(
            "email.resend: %s accepted domain=%s", kind, to_address.rsplit("@", 1)[-1]
        )

    # -- the messages ---------------------------------------------------------------

    def send_verification_email(
        self,
        *,
        to_address: str,
        display_name: str,
        verification_url: str,
    ) -> None:
        self._deliver(
            kind="verification link",
            to_address=to_address,
            subject="Confirm your ASTRION email address",
            html=verification_email_html(
                display_name=display_name, verification_url=verification_url
            ),
            text=verification_email_text(
                display_name=display_name, verification_url=verification_url
            ),
        )

    def send_verification_code(
        self,
        *,
        to_address: str,
        display_name: str,
        code: str,
        expires_minutes: int,
    ) -> None:
        self._deliver(
            kind="verification code",
            to_address=to_address,
            subject=VERIFICATION_CODE_SUBJECT,
            html=verification_code_email_html(
                display_name=display_name, code=code, expires_minutes=expires_minutes
            ),
            text=verification_code_email_text(
                display_name=display_name, code=code, expires_minutes=expires_minutes
            ),
        )

    def send_existing_account_notice(self, *, to_address: str) -> None:
        self._deliver(
            kind="existing-account notice",
            to_address=to_address,
            subject=EXISTING_ACCOUNT_SUBJECT,
            html=existing_account_email_html(),
            text=existing_account_email_text(),
        )


def _bound_the_request_time(resend_module) -> None:
    """Give the SDK's HTTP client a short timeout, where it lets us.

    Best effort: the client class is the SDK's own, and a version that moves it
    simply keeps its default rather than failing a send over a tuning detail.
    """
    try:
        from resend.http_client_requests import RequestsClient

        resend_module.default_http_client = RequestsClient(timeout=REQUEST_TIMEOUT_SECONDS)
    except Exception:  # noqa: BLE001 - see docstring
        pass
