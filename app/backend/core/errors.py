"""Application error types and their API-facing contract (Phase 5).

Every failure a client can provoke is represented here as a typed exception
carrying a stable machine-readable `code` and an HTTP status. The API layer
translates these into one error envelope and nothing else, so a client never
receives a stack trace, a SQL fragment, an API key, or a filesystem path.

Two rules this module exists to hold:

- **A failure keeps its shape.** An authorization failure is an authorization
  failure at the boundary that raised it. What the *tool* layer does — return
  `NOT_FOUND` for out-of-scope records so existence cannot be probed — is a
  deliberate, separate decision made below this layer and left untouched here
  (see `docs/architecture.md` §7.8). This module never invents a "not found"
  to paper over a rejected request it can see is a rejection.

- **Messages are safe to show.** `message` is written for a client. Anything
  that would leak internals belongs in the server log, not in `details`.
"""

from __future__ import annotations

from http import HTTPStatus


class AppError(Exception):
    """Base class for every failure the API reports in a structured envelope."""

    code = "internal_error"
    status = HTTPStatus.INTERNAL_SERVER_ERROR

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


# --- configuration -----------------------------------------------------------


class ConfigurationError(AppError):
    """The application is misconfigured. Raised at startup, not per request."""

    code = "configuration_error"
    status = HTTPStatus.INTERNAL_SERVER_ERROR


class ProviderConfigurationError(ConfigurationError):
    """The real provider was requested but its required settings are missing.

    Deliberately fatal rather than a silent downgrade to the deterministic
    planner: an operator who asked for a live model and got a rule-based one
    without noticing would be shipping a different system than they think.
    """

    code = "provider_not_configured"
    status = HTTPStatus.SERVICE_UNAVAILABLE


class DataUnavailableError(AppError):
    """The ingested database is missing or empty. Run the ingestion scripts."""

    code = "data_unavailable"
    status = HTTPStatus.SERVICE_UNAVAILABLE


# --- request-level ------------------------------------------------------------


class InvalidRequestError(AppError):
    code = "invalid_request"
    status = HTTPStatus.BAD_REQUEST


class AuthenticationError(AppError):
    """No recognised principal. The mock directory did not know this identity."""

    code = "unauthenticated"
    status = HTTPStatus.UNAUTHORIZED


class AuthorizationError(AppError):
    """A recognised principal attempted something their role does not permit.

    Used for *role* refusals, which are not existence-revealing. Account-scope
    refusals are handled below this layer by the scoped repositories.
    """

    code = "forbidden"
    status = HTTPStatus.FORBIDDEN


class NotFoundError(AppError):
    code = "not_found"
    status = HTTPStatus.NOT_FOUND


class ActionStateConflictError(AppError):
    """The action exists but is not in a state permitting this transition.

    409 rather than 404: the caller may see it, so hiding it would be less
    clear, not more secure.
    """

    code = "action_not_pending"
    status = HTTPStatus.CONFLICT


class ActionSessionMismatchError(AppError):
    """The action was prepared in a different conversation.

    Distinct from `ActionStateConflictError` so a client can tell "you are
    confirming from the wrong place" apart from "this was already decided".
    """

    code = "action_session_mismatch"
    status = HTTPStatus.CONFLICT


class ActionExecutionError(AppError):
    """A confirmed action failed while being performed."""

    code = "action_execution_failed"
    status = HTTPStatus.CONFLICT


# --- provider / agent ----------------------------------------------------------


class ProviderError(AppError):
    """The language-model provider failed, timed out, or returned nonsense.

    Never converted into an answer. A provider failure that produced prose
    anyway would be the single most dangerous failure mode in this system.
    """

    code = "provider_error"
    status = HTTPStatus.BAD_GATEWAY


class ProviderTimeoutError(ProviderError):
    code = "provider_timeout"
    status = HTTPStatus.GATEWAY_TIMEOUT


ERROR_CODES: tuple[str, ...] = (
    AppError.code,
    ConfigurationError.code,
    ProviderConfigurationError.code,
    DataUnavailableError.code,
    InvalidRequestError.code,
    AuthenticationError.code,
    AuthorizationError.code,
    NotFoundError.code,
    ActionStateConflictError.code,
    ActionSessionMismatchError.code,
    ActionExecutionError.code,
    ProviderError.code,
    ProviderTimeoutError.code,
    "validation_error",
)
