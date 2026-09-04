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
import time
import uuid

from fastapi import APIRouter, Request

from app.backend.api.authentication import (
    audit_denial,
    authenticate,
    demo_identities_available,
)
from app.backend.api.dependencies import (
    DbDep,
    UserHeaderDep,
    build_orchestrator,
    new_session_id,
    now_utc,
    require_dataset,
)
from app.backend.api.ratelimit import client_address
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
from app.backend.auth import repository as auth_repo
from app.backend.auth.permissions import Permission
from app.backend.core.config import Settings
from app.backend.core.errors import AuthorizationError, NotFoundError
from app.backend.models.agent import AgentRequest
from app.backend.services.actions import get_action_audit
from app.backend.services.audit import (
    AuditEvent,
    AuditOutcome,
    hash_identifier,
    record_event,
)
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
        auth_mode=summary["auth_mode"],
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
    # Read by the demo-mode identity path only; ignored entirely under session
    # authentication, where identity comes from the cookie.
    request.state.body_user_id = payload.user_id

    settings = _settings(request)
    require_dataset(conn)

    caller = authenticate(request, conn, settings)
    if not caller.is_demo:
        caller.require(Permission.RUN_AGENT)

    session_id = payload.session_id or new_session_id()
    # A conversation id is a client-supplied string. Binding it to its owner
    # here is what makes the action/session binding downstream meaningful: a
    # caller who guessed someone else's conversation id would otherwise satisfy
    # that check and be able to confirm their prepared action.
    if not caller.is_demo and not auth_repo.claim_conversation(
        conn,
        conversation_id=session_id,
        user_id=caller.user_id,
        org_id=caller.org_id,
    ):
        audit_denial(
            conn,
            caller,
            request_id=request_id,
            client_ip=client_address(request),
            detail="conversation_owned_by_another_user",
        )
        raise NotFoundError("That conversation was not found.")

    context = caller.agent_context(
        session_id=session_id, account_scope=payload.account_scope
    )

    orchestrator = build_orchestrator(conn, settings)
    started = time.perf_counter()
    response = orchestrator.handle(
        AgentRequest(message=payload.message, context=context, request_id=request_id)
    )
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    record_event(
        conn,
        AuditEvent.AGENT_INVOKED,
        actor_user_id=caller.user_id,
        actor_role=caller.org_role,
        org_id=caller.org_id,
        request_id=request_id,
        ip_hash=hash_identifier(client_address(request)),
        details={
            # The message itself is deliberately not logged: it is customer
            # content, and an audit trail is metadata about access, not a
            # transcript of everything anyone typed. The same rule governs
            # everything added below — these are *labels about* the
            # investigation (which tools, which sources, how it concluded),
            # never its content and never the model's reasoning.
            "outcome": response.outcome.value,
            "tools_used": response.tools_used,
            "prepared_action": (
                response.pending_action.action_id if response.pending_action else None
            ),
            # Phase 2 observability. Enough to reconstruct *why* an answer came
            # out the way it did without replaying the request.
            "intents": response.intents,
            "trust_status": response.trust_status,
            "governing_authority_tier": response.governing_authority_tier,
            "customer_agreement_applied": response.customer_agreement_applied,
            "conflict_count": len(response.authority_conflicts),
            "override_count": len(response.authority_overrides),
            "escalation_reason": response.escalation_reason,
            "escalation_recommended": response.escalation_recommended,
            "step_budget_exhausted": response.step_budget_exhausted,
            # Chunk ids, not chunk text: an id identifies the source for a
            # reviewer while keeping document contents out of the log.
            "source_chunk_ids": [item.chunk_id for item in response.evidence][:25],
            # Phase 3: which operational signals the investigation surfaced, by
            # id only. The ids are derived from record ids the caller can
            # already read, and the signal's contents stay out of the log.
            "operational_signal_ids": sorted(
                {
                    signal_id
                    for invocation in response.tool_invocations
                    if invocation.tool_name
                    in ("get_operational_signals", "investigate_signal")
                    for signal_id in [invocation.arguments.get("signal_id")]
                    if signal_id
                }
            ),
            "duration_ms": elapsed_ms,
        },
    )
    if response.pending_action is not None:
        record_event(
            conn,
            AuditEvent.ACTION_PROPOSED,
            actor_user_id=caller.user_id,
            actor_role=caller.org_role,
            org_id=caller.org_id,
            target_type=response.pending_action.target_type,
            target_id=response.pending_action.target_id,
            request_id=request_id,
            details={
                "action_id": response.pending_action.action_id,
                "action_type": response.pending_action.action_type.value,
            },
        )

    return ChatResponse.of(
        response,
        session_id=session_id,
        user_id=caller.user_id,
        role=caller.role,
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
    request.state.body_user_id = payload.user_id

    settings = _settings(request)
    if not settings.enable_state_changing_actions:
        # The kill switch also closes the execution path, not just the tools
        # that propose. Half a switch would be worse than none.
        raise AuthorizationError(
            "State-changing actions are disabled on this deployment."
        )
    require_dataset(conn)

    caller = authenticate(request, conn, settings)
    approve = payload.decision is ConfirmationDecision.APPROVE

    # The permission is checked here, before the state machine is consulted, so
    # a caller without it cannot even learn whether the action exists. Approval
    # and rejection are gated alike: rejecting someone else's proposal is also
    # a state change, and one an attacker would happily make.
    if not caller.is_demo:
        try:
            caller.require(Permission.EXECUTE_ACTION)
        except AuthorizationError:
            audit_denial(
                conn,
                caller,
                permission=Permission.EXECUTE_ACTION,
                request_id=request_id,
                client_ip=client_address(request),
                detail=f"confirm {action_id}",
            )
            raise

    # A conversation the caller does not own cannot be used to satisfy the
    # action's session binding.
    if not caller.is_demo and payload.session_id:
        owner = auth_repo.conversation_owner(conn, payload.session_id)
        if owner is not None and owner[0] != caller.user_id:
            audit_denial(
                conn,
                caller,
                request_id=request_id,
                client_ip=client_address(request),
                detail="confirmation_from_foreign_conversation",
            )
            raise NotFoundError(f"action {action_id!r} was not found within your scope")

    context = caller.agent_context(session_id=payload.session_id)

    orchestrator = build_orchestrator(conn, settings)
    try:
        executed = orchestrator.confirm_action(
            action_id,
            context,
            approve=approve,
            expected_fingerprint=payload.expected_fingerprint,
        )
    except Exception as exc:
        # Every refusal is recorded, whatever its cause: an expired action, a
        # fingerprint that no longer matches, a replayed confirmation. These are
        # exactly the events that distinguish an attack from a slow user.
        record_event(
            conn,
            AuditEvent.ACTION_CONFIRMATION_REFUSED,
            outcome=AuditOutcome.DENIED,
            actor_user_id=caller.user_id,
            actor_role=caller.org_role,
            org_id=caller.org_id,
            target_type="action",
            target_id=action_id,
            request_id=request_id,
            ip_hash=hash_identifier(client_address(request)),
            details={"reason": type(exc).__name__},
        )
        raise

    record_event(
        conn,
        AuditEvent.ACTION_EXECUTED if approve else AuditEvent.ACTION_REJECTED,
        actor_user_id=caller.user_id,
        actor_role=caller.org_role,
        org_id=caller.org_id,
        target_type=executed.target_type,
        target_id=executed.target_id,
        request_id=request_id,
        ip_hash=hash_identifier(client_address(request)),
        details={
            "action_id": action_id,
            "action_type": executed.action_type.value,
            "status": executed.status.value,
        },
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
    request.state.body_user_id = user_id
    caller = authenticate(request, conn, settings)
    context = caller.agent_context(session_id=None)

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
    request: Request,
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
    request.state.body_user_id = user_id
    caller = authenticate(request, conn, _settings(request))
    context = caller.agent_context(session_id=None)

    audit = get_action_audit(conn, action_id, allowed_account_ids=context.scope())
    if audit is None:
        raise NotFoundError(f"action {action_id!r} was not found within your scope")
    return ActionDetailResponse(
        action_status=ActionState.of(audit.status),
        action=ExecutedActionView.of(audit),
    )


# --- demo identities ----------------------------------------------------------------


@router.get("/api/principals", response_model=PrincipalsResponse, tags=["system"])
def principals(request: Request, conn: sqlite3.Connection = DbDep) -> PrincipalsResponse:
    """The mock identities this deployment accepts, and the scope each holds.

    Exists so the demo is self-describing. It lists no credentials, because
    there are none: Phase 5's authentication is deliberately a mock, and a
    real identity provider replaces `auth/principals.py` without touching
    anything below it.
    """
    from app.backend.auth.principals import MOCK_PRINCIPALS

    # Under session authentication the mock directory is not an identity
    # source, and publishing it would advertise personas that grant nothing.
    # An empty list is the honest answer, and it keeps the frontend's context
    # selector from offering a sign-in that does not exist.
    if not demo_identities_available(_settings(request)):
        return PrincipalsResponse(principals=[])

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
