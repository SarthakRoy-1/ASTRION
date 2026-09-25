"""Email delivery for authentication flows.

This package owns one job: get a verification message to the address being
proven — the one-time code that registration and sign-in send
(`send_verification_code`), or the legacy verification link
(`send_verification_email`). Everything else — code and token generation, rate
limiting, expiry — lives in the auth layer. This layer only sends.

The abstraction is intentionally thin: a `provider` object whose
implementation is chosen at startup. The null provider is the default; it is
replaced by the Resend provider when RESEND_API_KEY is present, and, outside
production, by a development outbox that writes each message to a file when
EMAIL_OUTBOX_DIR is set (and no Resend key is).

Failure contract: a delivery failure raises `EmailDeliveryError`. The caller
decides whether to surface that or absorb it — registration succeeds either
way, but callers that swallow the error must not claim delivery happened.
"""

from app.backend.email.provider import EmailDeliveryError, EmailProvider, NullEmailProvider

__all__ = ["EmailDeliveryError", "EmailProvider", "NullEmailProvider"]
