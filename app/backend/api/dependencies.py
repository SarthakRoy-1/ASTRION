"""Per-request wiring for the API layer (Phase 5).

Everything a route needs is assembled here so the routes themselves stay thin:
a database connection, the resolved authorization context, and an orchestrator
built with the configured provider. No business logic lives in this module and
none belongs here — it decides *who is asking* and *what will answer*, not
what the answer is.

Two mechanics worth stating:

- **One connection per request, closed with it.** SQLite connections are not
  shareable across threads, and FastAPI runs synchronous handlers in a thread
  pool. Opening per request is both correct and cheap for a file database.

- **One provider instance per request.** The real provider accumulates the
  transcript it is building; sharing one across requests would splice
  conversations together.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone

from fastapi import Depends, Header, Request

from app.backend.agent.factory import build_provider
from app.backend.agent.orchestrator import AgentOrchestrator
from app.backend.auth.principals import Principal, build_context, get_principal
from app.backend.core.config import Settings
from app.backend.core.errors import DataUnavailableError
from app.backend.models.agent import AgentContext
from app.backend.policies.base import PolicyDataError, load_evaluation_context
from app.backend.db import SchemaNotReadyError
from app.backend.tools.registry import build_default_registry

logger = logging.getLogger("astrion.api")

#: What a caller is told when the data is not there.
#:
#: Two audiences, and only one of them can act. An operator needs the commands
#: and the path, and gets them from the log line beside each raise; whoever is
#: holding the browser needs to know whether to wait or to give up, and gets
#: that. The message used to carry the commands, which meant a member of the
#: public arriving at a sleeping deployment was told to run two Python scripts
#: on a machine they have no access to.
DATA_UNAVAILABLE_MESSAGE = (
    "The ASTRION environment is still initialising. Please retry in a moment."
)


def now_utc() -> datetime:
    """Wall-clock time, for request/audit metadata only.

    Never for business reasoning: policy decisions are judged against the
    dataset snapshot (`load_evaluation_context`), not against today.
    """
    return datetime.now(timezone.utc)


def new_session_id() -> str:
    return f"SES-{uuid.uuid4().hex[:12]}"


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_db(request: Request) -> Iterator[sqlite3.Connection]:
    """Open a scoped connection, refusing clearly if the data is not built."""
    database = request.app.state.database
    if not database.exists():
        logger.error(
            "no database at %s. Build it with "
            "`python scripts/ingest_dataset.py` and `python scripts/ingest_documents.py`, "
            "or set DEMO_LOGIN_ENABLED=true to have the application build it itself.",
            database.location,
        )
        raise DataUnavailableError(DATA_UNAVAILABLE_MESSAGE)
    try:
        # Once per process, and never DDL on PostgreSQL: a database behind the
        # code is reported, not repaired from a request handler.
        database.ensure_ready()
    except SchemaNotReadyError as exc:
        logger.error("%s", exc)
        raise DataUnavailableError(DATA_UNAVAILABLE_MESSAGE) from exc
    conn = database.connect()
    try:
        yield conn
    finally:
        conn.close()


def resolve_principal(
    body_user_id: str | None, header_user_id: str | None
) -> Principal:
    """Identify the caller. The header wins when both are supplied.

    Raises `AuthenticationError` for an unknown or absent identity — there is
    no anonymous path into the agent.
    """
    return get_principal(header_user_id or body_user_id)


def resolve_context(
    conn: sqlite3.Connection,
    principal: Principal,
    *,
    session_id: str | None,
    account_scope: list[str] | None = None,
) -> AgentContext:
    """Build the authorization envelope the agent will run under."""
    return build_context(
        conn,
        principal,
        session_id=session_id,
        requested_account_ids=account_scope,
    )


def build_orchestrator(
    conn: sqlite3.Connection, settings: Settings, org_id: str | None
) -> AgentOrchestrator:
    """Assemble the agent for one request, under the configured provider."""
    try:
        reference_time = load_evaluation_context(conn, org_id).reference_time
    except PolicyDataError:
        reference_time = None

    provider = build_provider(settings, reference_time=reference_time)
    registry = build_default_registry(
        include_state_changing=settings.enable_state_changing_actions
    )
    return AgentOrchestrator(
        conn,
        provider=provider,
        registry=registry,
        max_steps=settings.agent_max_tool_steps,
    )


def user_header(
    x_astrion_user: str | None = Header(default=None),
) -> str | None:
    """The mock identity header, declared once so it appears in the OpenAPI docs."""
    return x_astrion_user


SettingsDep = Depends(get_settings)
DbDep = Depends(get_db)
UserHeaderDep = Depends(user_header)
