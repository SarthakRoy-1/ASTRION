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

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.backend.api.errors import register_error_handlers
from app.backend.api.routes import router
from app.backend.core.config import DEFAULT_ENV_FILE, Settings, load_settings

API_TITLE = "ParcelPilot Support & Operations Agent API"
API_VERSION = "0.5.0"

API_DESCRIPTION = """\
Natural-language support and operations assistant over ParcelPilot's policy
pack and operational records.

The agent reasons; deterministic code decides. Account scoping, source
precedence, cancellation and service-credit arithmetic, and every state change
are enforced below the model layer and cannot be influenced by the contents of
a request.

State-changing actions are prepared by `POST /api/chat` and executed only by
`POST /api/actions/{action_id}/confirm`. Nothing in a chat message can perform
one.
"""


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application. Raises on invalid configuration."""
    if settings is None:
        settings = load_settings(env_file=DEFAULT_ENV_FILE)

    # Fail at startup, not at the first request: an operator should learn that
    # the model is unreachable before a user does.
    settings.validate_provider()

    logging.getLogger("parcelpilot").setLevel(logging.INFO)

    app = FastAPI(
        title=API_TITLE,
        version=API_VERSION,
        description=API_DESCRIPTION,
    )
    app.state.settings = settings

    if settings.cors_allow_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_allow_origins),
            allow_credentials=False,
            allow_methods=["GET", "POST"],
            allow_headers=["Content-Type", "X-ParcelPilot-User"],
        )

    register_error_handlers(app)
    app.include_router(router)
    return app
