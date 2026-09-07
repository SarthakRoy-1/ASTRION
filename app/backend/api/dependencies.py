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
from app.backend.services.database import get_connection, initialize_schema
from app.backend.tools.registry import build_default_registry


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
    settings: Settings = request.app.state.settings
    if not settings.database_path.exists():
        raise DataUnavailableError(
            "The ASTRION database has not been built. Run "
            "`python scripts/ingest_dataset.py` and "
            "`python scripts/ingest_documents.py`, then retry."
        )
    conn = get_connection(settings.database_path)
    try:
        # Idempotent, and applies any column added since this file was built.
        initialize_schema(conn)
        yield conn
    finally:
        conn.close()


def require_dataset(conn: sqlite3.Connection) -> None:
    """Refuse to answer against an empty database rather than answering badly."""
    try:
        load_evaluation_context(conn)
    except PolicyDataError as exc:
        raise DataUnavailableError(
            "The dataset has not been ingested, so no time-based question can be "
            "answered. Run `python scripts/ingest_dataset.py`."
        ) from exc


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
    conn: sqlite3.Connection, settings: Settings
) -> AgentOrchestrator:
    """Assemble the agent for one request, under the configured provider."""
    try:
        reference_time = load_evaluation_context(conn).reference_time
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
