"""HTTP routes (Phase 5).

Deliberately thin. Each handler resolves who is asking, hands the request to
the Phase 4 orchestrator, and projects the typed result onto the API contract.
No route computes a fee, ranks a source, decides what an account may see, or
performs a state change; all of that already exists below and is reached
through the same entry points a non-HTTP caller would use.

The two boundaries visible from here:

- `POST /api/chat` can *prepare* an action. There is no argument, phrasing or
  parameter that makes it execute one — the orchestrator's `handle` has no
  path to execution, and the model's tool surface has no path to it either.
- `POST /api/actions/{id}/confirm` is the only execution path, takes a closed
  decision vocabulary, and re-validates identity, session, scope, state,
  expiry and parameters before anything is written.
"""

from __future__ import annotations

import sqlite3
import uuid

from fastapi import APIRouter, Request

from app.backend.api.dependencies import (
    DbDep,
    UserHeaderDep,
    build_orchestrator,
    new_session_id,
    now_utc,
    require_dataset,
    resolve_context,
    resolve_principal,
)
from app.backend.api.schemas import (
    ActionConfirmationRequest,
    ActionConfirmationResponse,
    ActionDetailResponse,
    ActionState,
    ChatRequest,
    ChatResponse,
    ConfirmationDecision,
    ExecutedActionView,
    HealthResponse,
    PendingActionsResponse,
    PrincipalsResponse,
    PrincipalView,
    ProposedActionView,
)
from app.backend.core.config import Settings
from app.backend.core.errors import AuthorizationError, NotFoundError
from app.backend.models.agent import AgentRequest
from app.backend.services.actions import get_action_audit
from app.backend.services.records import get_dataset_metadata

router = APIRouter()


def _settings(request: Request) -> Settings:
    return request.app.state.settings


# --- health --------------------------------------------------------------------


@router.get("/health", response_model=HealthResponse, tags=["system"])
def health(request: Request) -> HealthResponse:
    """Liveness plus what is actually configured.

    Answers 200 even when the database has not been built, reporting
    `status: "degraded"` — a health check that fails to respond tells an
    operator less than one that responds saying what is missing.
    """
    settings = _settings(request)
    summary = settings.public_summary()

    database_ready = False
    documents = 0
    snapshot: str | None = None
    if settings.database_path.exists():
        conn: sqlite3.Connection | None = None
        try:
            from app.backend.services.database import get_connection

            conn = get_connection(settings.database_path)
            metadata = get_dataset_metadata(conn)
            if metadata is not None:
                snapshot = metadata.dataset_snapshot_raw
            row = conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()
            documents = int(row["n"]) if row else 0
            database_ready = metadata is not None and documents > 0
        except sqlite3.Error:
            database_ready = False
        finally:
            if conn is not None:
                conn.close()

    return HealthResponse(
        status="ok" if database_ready else "degraded",
        app_env=summary["app_env"],
        provider_mode=summary["provider_mode"],
        model=summary["model"],
        max_tool_steps=summary["max_tool_steps"],
        state_changing_actions_enabled=summary["state_changing_actions_enabled"],
        database_ready=database_ready,
        documents_indexed=documents,
        dataset_snapshot=snapshot,
        checked_at_utc=now_utc(),
    )


# --- chat ------------------------------------------------------------------------


@router.post("/api/chat", response_model=ChatResponse, tags=["agent"])
def chat(
    request: Request,
    payload: ChatRequest,
    conn: sqlite3.Connection = DbDep,
    header_user: str | None = UserHeaderDep,
) -> ChatResponse:
    """Answer one natural-language request with its evidence.

    The authorization context is built from the caller's identity before the
    message is read, and nothing in the message can widen it. Account ids in
    prose are treated as text; the agent's scope comes from records it was
    permitted to resolve.
    """
    request_id = payload.request_id or f"REQ-{uuid.uuid4().hex[:12]}"
    request.state.request_id = request_id

    settings = _settings(request)
    require_dataset(conn)

    principal = resolve_principal(payload.user_id, header_user)
    session_id = payload.session_id or new_session_id()
    context = resolve_context(
        conn, principal, session_id=session_id, account_scope=payload.account_scope
    )

    orchestrator = build_orchestrator(conn, settings)
    response = orchestrator.handle(
        AgentRequest(message=payload.message, context=context, request_id=request_id)
    )

    return ChatResponse.of(
        response,
        session_id=session_id,
        user_id=principal.user_id,
        role=principal.role,
        account_scope=sorted(context.allowed_account_ids or []),
        responded_at_utc=now_utc(),
    )


# --- actions ----------------------------------------------------------------------


@router.post(
    "/api/actions/{action_id}/confirm",
    response_model=ActionConfirmationResponse,
    tags=["actions"],
)
def confirm_action(
    request: Request,
    action_id: str,
    payload: ActionConfirmationRequest,
    conn: sqlite3.Connection = DbDep,
    header_user: str | None = UserHeaderDep,
) -> ActionConfirmationResponse:
    """Execute or reject one prepared action. The only path to a state change.

    Every check the Phase 4 state machine offers is applied, under the
    *confirming* caller rather than the preparing one: the action must exist
    within this caller's account scope, belong to this conversation, still be
    pending, not have expired, still have a live target, and — when a
    fingerprint is supplied — still describe exactly what was reviewed. The
    status guard inside the UPDATE makes execution single-use even if two
    confirmations race.
    """
    request_id = payload.request_id or f"REQ-{uuid.uuid4().hex[:12]}"
    request.state.request_id = request_id

    settings = _settings(request)
    if not settings.enable_state_changing_actions:
        # The kill switch also closes the execution path, not just the tools
        # that propose. Half a switch would be worse than none.
        raise AuthorizationError(
            "State-changing actions are disabled on this deployment."
        )
    require_dataset(conn)

    principal = resolve_principal(payload.user_id, header_user)
    context = resolve_context(conn, principal, session_id=payload.session_id)

    orchestrator = build_orchestrator(conn, settings)
    approve = payload.decision is ConfirmationDecision.APPROVE
    executed = orchestrator.confirm_action(
        action_id,
        context,
        approve=approve,
        expected_fingerprint=payload.expected_fingerprint,
    )

    return ActionConfirmationResponse(
        request_id=request_id,
        action_status=ActionState.of(executed.status),
        action=ExecutedActionView.of(executed),
        message=(
            f"Action {action_id} executed."
            if approve
            else f"Action {action_id} rejected; nothing was changed."
        ),
    )


@router.get(
    "/api/actions/pending", response_model=PendingActionsResponse, tags=["actions"]
)
def pending_actions(
    request: Request,
    conn: sqlite3.Connection = DbDep,
    header_user: str | None = UserHeaderDep,
    user_id: str | None = None,
) -> PendingActionsResponse:
    """Proposals awaiting confirmation, within the caller's account scope."""
    settings = _settings(request)
    principal = resolve_principal(user_id, header_user)
    context = resolve_context(conn, principal, session_id=None)

    orchestrator = build_orchestrator(conn, settings)
    actions = orchestrator.pending_actions(context)
    return PendingActionsResponse(
        count=len(actions),
        actions=[ProposedActionView.of(action) for action in actions],
    )


@router.get(
    "/api/actions/{action_id}", response_model=ActionDetailResponse, tags=["actions"]
)
def action_detail(
    action_id: str,
    conn: sqlite3.Connection = DbDep,
    header_user: str | None = UserHeaderDep,
    user_id: str | None = None,
) -> ActionDetailResponse:
    """The audit record for one action: state, timeline, and its effect.

    Scoped like every other read — an action on another customer's account is
    reported as absent, not as forbidden, for the same reason the record
    layer does.
    """
    principal = resolve_principal(user_id, header_user)
    context = resolve_context(conn, principal, session_id=None)

    audit = get_action_audit(conn, action_id, allowed_account_ids=context.scope())
    if audit is None:
        raise NotFoundError(f"action {action_id!r} was not found within your scope")
    return ActionDetailResponse(
        action_status=ActionState.of(audit.status),
        action=ExecutedActionView.of(audit),
    )


# --- demo identities ----------------------------------------------------------------


@router.get("/api/principals", response_model=PrincipalsResponse, tags=["system"])
def principals(conn: sqlite3.Connection = DbDep) -> PrincipalsResponse:
    """The mock identities this deployment accepts, and the scope each holds.

    Exists so the demo is self-describing. It lists no credentials, because
    there are none: Phase 5's authentication is deliberately a mock, and a
    real identity provider replaces `auth/principals.py` without touching
    anything below it.
    """
    from app.backend.auth.principals import MOCK_PRINCIPALS

    # Declaration order, not alphabetical: the directory is a demo aid, and a
    # client that offers the first entry as its default should land on the
    # primary internal-support persona rather than on whichever identity sorts
    # first. `MOCK_PRINCIPALS` is insertion-ordered for exactly this reason.
    return PrincipalsResponse(
        principals=[
            PrincipalView(
                user_id=principal.user_id,
                display_name=principal.display_name,
                role=principal.role,
                description=principal.description,
                account_scope=sorted(principal.resolve_scope(conn)),
            )
            for principal in MOCK_PRINCIPALS.values()
        ]
    )
