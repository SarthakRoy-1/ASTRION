"""Email delivery for authentication flows.

This package owns one job: get a verification link to the address that just
registered. Everything else — token generation, rate limiting, expiry — lives
in the auth layer. This layer only sends.

The abstraction is intentionally thin: a single `send_verification_email`
function that takes the parts the caller knows, and a `provider` object whose
implementation is chosen at startup. The null provider is the default; it is
replaced by the Resend provider when RESEND_API_KEY is present.

Failure contract: a delivery failure raises `EmailDeliveryError`. The caller
decides whether to surface that or absorb it — registration succeeds either
way, but callers that swallow the error must not claim delivery happened.
"""

from app.backend.email.provider import EmailDeliveryError, EmailProvider, NullEmailProvider

__all__ = ["EmailDeliveryError", "EmailProvider", "NullEmailProvider"]
