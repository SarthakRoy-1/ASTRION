"""FastAPI application assembly (Phase 5).

`create_app` is the single place the application is wired together, and it
fails fast: if `LLM_PROVIDER=real` is configured without the credentials it
needs, the process refuses to start rather than serving a quietly different
agent (see `Settings.validate_provider`).

The app object itself owns almost nothing. Configuration lives on
`app.state.settings`; everything else — the database connection, the
authorization context, the provider, the orchestrator — is built per request
in `dependencies.py`, because all of it is request-scoped by nature.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.backend.api.auth_routes import auth_router
from app.backend.api.document_routes import router as document_router
from app.backend.api.errors import register_error_handlers
from app.backend.api.identity_routes import identity_router
from app.backend.api.middleware import (
    UPLOAD_PATH,
    BodySizeLimitMiddleware,
    CsrfOriginMiddleware,
    RateLimitMiddleware,
    SecurityHeadersMiddleware,
)
from app.backend.api.ratelimit import RateLimiter
from app.backend.api.operations_routes import operations_router
from app.backend.api.routes import router
from app.backend.api.workspace_routes import workspace_router
from app.backend.db import open_database
from app.backend.storage import open_store
from app.backend.core.config import DEFAULT_ENV_FILE, Settings, load_settings
from app.backend.services.bootstrap import ensure_demo_environment

logger = logging.getLogger("astrion.app")

API_TITLE = "ASTRION Support & Operations Agent API"
#: Tracks the implementation phase, and is kept in step with the frontend's
#: package version so a deployed pair can be identified from `/health` and the
#: OpenAPI document alone.
API_VERSION = "0.8.0"

API_DESCRIPTION = """\
Natural-language support and operations assistant over ASTRION's policy
pack and operational records.

The agent reasons; deterministic code decides. Account scoping, source
precedence, cancellation and service-credit arithmetic, and every state change
are enforced below the model layer and cannot be influenced by the contents of
a request.

State-changing actions are prepared by `POST /api/chat` and executed only by
`POST /api/actions/{action_id}/confirm`. Nothing in a chat message can perform
one.
"""


def _demo_lifespan(settings: Settings):
    """Prepare the public demo environment as the process comes up.

    An optimisation, not a guarantee, and the difference matters. The
    guarantee lives in `POST /api/auth/demo-login`, which checks the database
    itself on every call; this hook exists so the *sign-in page* is right
    before anyone clicks anything — without it, the first request after a cold
    start finds no database, and a page whose whole job is to let you in
    instead greets you with an error about a missing one.

    Failure is logged and survived. A deployment whose source pack is
    unreadable should still serve `/health` so an operator can see what is
    wrong, rather than crash-looping where nobody can read the reason.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if settings.demo_login_enabled:
            try:
                report = ensure_demo_environment(settings)
                if report.changed:
                    logger.info(
                        "demo environment prepared at startup in %.2fs",
                        report.duration_seconds,
                    )
            except Exception:
                # Broad on purpose: nothing about preparing a demo justifies
                # refusing to start. The endpoint retries on the first click.
                logger.exception(
                    "demo environment could not be prepared at startup; "
                    "it will be retried on the first demo sign-in"
                )
        try:
            yield
        finally:
            # Returns the PostgreSQL pool's connections; a no-op for SQLite.
            app.state.database.close()

    return lifespan


class RedactOAuthQuery(logging.Filter):
    """Keep provider authorization codes and state out of the access log.

    uvicorn logs every request line, query string included, and the OAuth
    callback carries the provider's single-use authorization code there. The
    code is useless without the PKCE verifier held server-side, but a log is a
    second copy of a credential retained far longer than the credential, so the
    query is dropped from those lines entirely.
    """

    MARKER = "/api/auth/oauth/"

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple):
            record.args = tuple(
                arg.split("?", 1)[0] + "?[redacted]"
                if isinstance(arg, str) and self.MARKER in arg and "?" in arg
                else arg
                for arg in args
            )
        return True


_REDACTOR = RedactOAuthQuery()


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application. Raises on invalid configuration."""
    if settings is None:
        settings = load_settings(env_file=DEFAULT_ENV_FILE)
        # Only for the settings this process builds itself: a production
        # deployment must not start on storage that will not survive it. Tests
        # hand in their own Settings and are not held to it.
        settings.validate_persistence()

    # Fail at startup, not at the first request: an operator should learn that
    # the model is unreachable before a user does.
    settings.validate_provider()
    # The same argument applies, more sharply, to the security configuration.
    # A deployment serving real users through the demo identity header, or
    # advertising a wildcard CORS origin alongside cookie authentication, is
    # broken in a way that will not show up in any request it answers
    # successfully — so it must not start at all.
    settings.validate_auth()
    settings.validate_cors()

    logging.getLogger("astrion").setLevel(logging.INFO)
    access_log = logging.getLogger("uvicorn.access")
    if _REDACTOR not in access_log.filters:
        access_log.addFilter(_REDACTOR)

    app = FastAPI(
        title=API_TITLE,
        version=API_VERSION,
        description=API_DESCRIPTION,
        lifespan=_demo_lifespan(settings),
    )
    app.state.settings = settings
    app.state.database = open_database(settings)
    app.state.store = open_store(settings)
    app.state.rate_limiter = RateLimiter()
    #: Whether `X-Forwarded-For` may be believed. False unless a proxy that
    #: sets it is known to be in front, because a client that can choose its
    #: own rate-limit key is not rate-limited.
    app.state.trust_forwarded_for = False

    # Starlette applies middleware in REVERSE registration order. The intended
    # execution order, outermost first, is:
    #
    #     SecurityHeaders -> CORS -> BodySizeLimit -> CsrfOrigin -> RateLimit
    #
    # so the registrations below are written bottom-up. Two properties depend
    # on this and are easy to lose:
    #
    # - **The body limit precedes everything expensive.** Checking the rate
    #   limit first would mean an oversized body had already been buffered
    #   before anything refused it.
    # - **SecurityHeaders is outermost, so it sees every response** — including
    #   a 413 from the size limit, a 429 from the rate limiter, and a CORS
    #   preflight. Registering it first (which reads more naturally) would make
    #   it innermost, and every refusal produced above it would go out with no
    #   security headers at all.
    if settings.rate_limit_enabled:
        app.add_middleware(
            RateLimitMiddleware,
            limiter=app.state.rate_limiter,
            default_limit=settings.rate_limit_per_minute,
            agent_limit=settings.agent_rate_limit_per_minute,
            auth_limit=settings.auth_rate_limit_per_minute,
        )

    if settings.cors_allow_origins:
        app.add_middleware(
            CsrfOriginMiddleware, allowed_origins=settings.cors_allow_origins
        )

    app.add_middleware(
        BodySizeLimitMiddleware,
        max_bytes=settings.max_request_bytes,
        # Route-aware: only the upload route may carry a document-sized body.
        overrides={UPLOAD_PATH: settings.max_upload_bytes},
    )

    if settings.cors_allow_origins:
        # `allow_credentials=True` is required for the session cookie to travel
        # on a cross-origin request, and is only safe because
        # `Settings.validate_cors` has already refused a wildcard origin.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_allow_origins),
            allow_credentials=True,
            allow_methods=["GET", "POST", "PATCH", "DELETE"],
            allow_headers=["Content-Type", "X-Astrion-User"],
        )

    if settings.security_headers_enabled:
        app.add_middleware(
            SecurityHeadersMiddleware,
            hsts_enabled=settings.hsts_enabled,
            hsts_max_age=settings.hsts_max_age_seconds,
        )

    register_error_handlers(app)
    app.include_router(router)
    app.include_router(auth_router)
    app.include_router(identity_router)
    app.include_router(workspace_router)
    app.include_router(operations_router)
    app.include_router(document_router)
    return app
