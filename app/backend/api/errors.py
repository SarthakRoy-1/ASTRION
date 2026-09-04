"""Turning failures into one safe, structured response (Phase 5).

Every error path — a typed `AppError`, a Phase 4 action-service exception, a
request that failed validation, or an unhandled bug — leaves the API as the
same `ErrorResponse` envelope with a stable `code`. Nothing else does.

What this module refuses to do:

- **It never leaks internals.** An unexpected exception becomes a generic
  `internal_error`; the traceback goes to the server log, never to the client.
  Exception *text* from below the API is only forwarded for errors whose
  messages this codebase writes deliberately.
- **It never rewrites the security model.** The tool layer answers "out of
  scope" and "does not exist" identically on purpose, so an out-of-scope
  caller cannot use the API as an existence oracle. That decision is made
  below this layer and is passed through unchanged — this module does not
  invent a 404 to hide a 403, nor a 403 to explain a 404.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.backend.api.schemas import ErrorBody, ErrorResponse
from app.backend.core.errors import (
    ActionExecutionError,
    ActionSessionMismatchError,
    ActionStateConflictError,
    AppError,
    AuthorizationError,
    NotFoundError,
)
from app.backend.services.actions import (
    ActionError,
    ActionForbidden,
    ActionNotFound,
    ActionSessionError,
    ActionStateError,
)

logger = logging.getLogger("parcelpilot.api")


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def error_response(
    status: int, code: str, message: str, *, details: dict | None = None,
    request_id: str | None = None,
) -> JSONResponse:
    body = ErrorResponse(
        error=ErrorBody(
            code=code, message=message, details=details or {}, request_id=request_id
        )
    )
    return JSONResponse(status_code=status, content=body.model_dump(mode="json"))


def translate_action_error(exc: ActionError) -> AppError:
    """Map the Phase 4 action exceptions onto the API's error vocabulary.

    Kept here rather than in `services/actions.py`: the action service should
    not know that an HTTP API exists.
    """
    if isinstance(exc, ActionNotFound):
        return NotFoundError(str(exc))
    if isinstance(exc, ActionForbidden):
        # A role refusal, not a disagreement about the action's state — the
        # action may be perfectly valid and pending. 403, not 409.
        return AuthorizationError(str(exc))
    if isinstance(exc, ActionSessionError):
        return ActionSessionMismatchError(str(exc))
    if isinstance(exc, ActionStateError):
        return ActionStateConflictError(str(exc))
    # A bare ActionError is a refusal the service wrote deliberately — e.g. a
    # proposal missing a required parameter.
    return ActionExecutionError(str(exc))


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        logger.warning("%s: %s", exc.code, exc.message)
        return error_response(
            int(exc.status),
            exc.code,
            exc.message,
            details=exc.details,
            request_id=_request_id(request),
        )

    @app.exception_handler(ActionError)
    async def _action_error(request: Request, exc: ActionError) -> JSONResponse:
        return await _app_error(request, translate_action_error(exc))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Pydantic's own report names the offending field and constraint, which
        # is exactly what a client needs and contains nothing server-side.
        problems = [
            {
                "field": ".".join(str(part) for part in err.get("loc", ())[1:]),
                "problem": err.get("msg", "invalid value"),
            }
            for err in exc.errors()
        ]
        return error_response(
            422,
            "validation_error",
            "The request body did not match the expected schema.",
            details={"problems": problems},
            request_id=_request_id(request),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        codes = {404: "not_found", 405: "method_not_allowed", 401: "unauthenticated"}
        return error_response(
            exc.status_code,
            codes.get(exc.status_code, "http_error"),
            str(exc.detail),
            request_id=_request_id(request),
        )

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        # Logged with the traceback; reported without it. A client learning the
        # shape of an internal failure learns nothing it can act on and
        # something an attacker can.
        # `scope["path"]`, not `request.url.path`: the latter is rebuilt from
        # the Host header and can be made to name a different path than the one
        # that ran (PYSEC-2026-161), which would misdirect whoever reads this.
        logger.exception(
            "unhandled error on %s %s",
            request.method,
            request.scope.get("path", "") or request.url.path,
        )
        return error_response(
            500,
            "internal_error",
            "The server could not complete this request.",
            request_id=_request_id(request),
        )
