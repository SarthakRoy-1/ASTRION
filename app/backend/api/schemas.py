"""Typed request/response contracts for the HTTP API (Phase 5).

These are a *projection* of the Phase 4 models, not a replacement for them.
`AgentResponse` already carries everything a client needs; this module decides
what crosses the wire and in what shape:

- **Evidence becomes citations.** Every source keeps its file, page, section
  and authority so a UI can render "Sources:" without re-deriving anything.
- **Tool use becomes names and outcomes.** Which tools ran, and how each
  finished. Tool *arguments* and the model's transcript stay server-side:
  the API contract promises conclusions and provenance, not reasoning traces.
- **Policy decisions keep their arithmetic.** The controlling rule, the
  inputs, the calculation and the citations travel with the figure, because a
  figure a client cannot check is a figure a client should not act on.
- **Action state is explicit.** `action_status` is always present, so "nothing
  is pending" is an answer rather than an absence.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.backend.models.actions import ActionStatus, ExecutedAction, ProposedAction
from app.backend.models.agent import AgentResponse, ResponseOutcome, Role
from app.backend.models.documents import Evidence
from app.backend.models.policy import (
    CancellationDecision,
    ServiceCreditDecision,
    SlaDecision,
)

MAX_MESSAGE_CHARS = 4000
#: How much of a cited chunk travels with the citation. Enough to show the
#: clause a claim rests on; the full text stays retrievable by chunk id.
EXCERPT_CHARS = 400


class ActionState(StrEnum):
    """Action lifecycle as the API reports it.

    Extends `ActionStatus` with `NONE`, so a response always states the action
    situation instead of leaving a client to infer it from a null field.

    `CONFIRMED` is part of the contract but is not currently emitted: Phase 4's
    state machine confirms and executes inside one guarded transaction, so a
    successfully confirmed action is already `EXECUTED` by the time anyone can
    observe it. It is listed here so a future asynchronous executor does not
    need a breaking contract change.
    """

    NONE = "none"
    PENDING_CONFIRMATION = "pending_confirmation"
    CONFIRMED = "confirmed"
    EXECUTED = "executed"
    REJECTED = "rejected"
    EXPIRED = "expired"
    FAILED = "failed"

    @classmethod
    def of(cls, status: ActionStatus | None) -> ActionState:
        return cls.NONE if status is None else cls(status.value)


# --- requests -------------------------------------------------------------------


class ChatRequest(BaseModel):
    """A natural-language request plus the identity it is made under.

    `user_id` establishes the authorization context; it is looked up in the
    mock principal directory server-side. It is *not* trusted to describe its
    own permissions, and nothing in `message` can alter them —
    see `app/backend/auth/principals.py`.
    """

    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)

    #: Mock identity. May also be supplied as the `X-ParcelPilot-User` header,
    #: which takes precedence when both are present.
    user_id: str | None = None

    #: Continues an existing conversation. Generated when omitted, and echoed
    #: back; prepared actions are bound to it.
    session_id: str | None = None

    #: Optional *narrowing* of the caller's account scope — for a support
    #: agent deliberately working within one customer's view. Intersected with
    #: the principal's own scope, so it can never widen access.
    account_scope: list[str] | None = None

    #: Client-supplied correlation id, echoed back in the response and in
    #: error envelopes.
    request_id: str | None = None


class ConfirmationDecision(StrEnum):
    """Explicit, unambiguous. Free text is never interpreted as consent."""

    APPROVE = "approve"
    REJECT = "reject"


class ActionConfirmationRequest(BaseModel):
    """Confirm or reject one prepared action.

    Confirmation is a distinct API call with a closed vocabulary. A user typing
    "okay" into the chat endpoint does not confirm anything: `/api/chat` has no
    path to execution at all, which is what makes the gate structural rather
    than a matter of parsing intent correctly.
    """

    model_config = ConfigDict(extra="forbid")

    decision: ConfirmationDecision
    user_id: str | None = None

    #: Must match the session the action was prepared in.
    session_id: str | None = None

    #: The `parameter_fingerprint` shown alongside the preview. When supplied,
    #: the action is executed only if its stored parameters still digest to
    #: this value — so what was approved is what runs.
    expected_fingerprint: str | None = None

    request_id: str | None = None


# --- response parts ---------------------------------------------------------------


class SourceRef(BaseModel):
    """One citation: where a claim came from, and how much it is worth."""

    model_config = ConfigDict(frozen=True)

    chunk_id: str
    document_id: str
    source_file: str
    page: int
    section: str | None
    citation: str
    document_title: str
    authority_tier: int
    is_authoritative: bool
    is_deprecated: bool
    topic: str
    account_id: str | None
    excerpt: str

    @classmethod
    def of(cls, item: Evidence) -> SourceRef:
        text = item.text.strip()
        excerpt = text if len(text) <= EXCERPT_CHARS else text[:EXCERPT_CHARS] + "…"
        return cls(
            chunk_id=item.chunk_id,
            document_id=item.document_id,
            source_file=item.source_file,
            page=item.page_number,
            section=item.section_path,
            citation=item.citation,
            document_title=item.document_title,
            authority_tier=int(item.authority_tier),
            is_authoritative=item.is_authoritative,
            is_deprecated=item.is_deprecated,
            topic=item.topic.value,
            account_id=item.account_id,
            excerpt=excerpt,
        )


class ToolUse(BaseModel):
    """One tool call, as an auditor would want to see it summarised.

    Arguments are omitted on purpose: they are part of the planning trace, and
    the contract promises what was consulted and how it finished, not how the
    agent phrased its query.
    """

    model_config = ConfigDict(frozen=True)

    step: int
    tool_name: str
    status: str
    summary: str | None = None
    message: str | None = None


class PolicyDecisionView(BaseModel):
    """A deterministic decision with the arithmetic that produced it."""

    model_config = ConfigDict(frozen=True)

    decision_type: str
    #: Present on decisions about an order. An SLA decision concerns a ticket
    #: instead, so exactly one of these two is populated.
    order_id: str | None = None
    ticket_id: str | None = None
    account_id: str
    outcome: str
    controlling_rule: str
    controlling_sources: list[str] = []
    calculation: str | None = None
    #: Absent on decisions that produce no monetary figure.
    currency: str | None = None

    amount: str | None = None
    amount_label: str | None = None
    applies: bool | None = None

    #: SLA-only projections. `breached` is deliberately tri-state: null means
    #: the question was not settled, which is not the same as "not breached".
    severity: str | None = None
    target_text: str | None = None
    elapsed_minutes: str | None = None
    breached: bool | None = None
    requires_immediate_escalation: bool = False

    requires_verification: bool = False
    verification_reasons: list[str] = []
    overrides: list[str] = []
    evidence_chunk_ids: list[str] = []
    inputs: dict[str, str | None] = {}

    @classmethod
    def of(
        cls, decision: CancellationDecision | ServiceCreditDecision | SlaDecision
    ) -> PolicyDecisionView:
        common = {
            "decision_type": decision.decision_type,
            "account_id": decision.account_id,
            "outcome": decision.outcome.value,
            "controlling_rule": decision.controlling_rule,
            "controlling_sources": list(decision.controlling_sources),
            "calculation": decision.calculation,
            "requires_verification": decision.requires_verification,
            "verification_reasons": list(decision.verification_reasons),
            "overrides": list(decision.overrides),
            "evidence_chunk_ids": list(decision.evidence_chunk_ids),
            "inputs": dict(decision.inputs),
        }

        if isinstance(decision, SlaDecision):
            return cls(
                ticket_id=decision.ticket_id,
                severity=None if decision.severity is None else decision.severity.value,
                target_text=decision.target_text,
                # Elapsed time is a Decimal for the same reason money is: it
                # is compared against a stated target, so it crosses the wire
                # as a string rather than as a float.
                elapsed_minutes=(
                    None
                    if decision.elapsed_minutes is None
                    else str(decision.elapsed_minutes)
                ),
                breached=decision.breached,
                requires_immediate_escalation=decision.requires_immediate_escalation,
                **common,
            )

        if isinstance(decision, CancellationDecision):
            amount = decision.fee_amount
            label = "cancellation_fee"
            applies = decision.fee_applies
        else:
            amount = decision.credit_amount
            label = "service_credit"
            applies = decision.eligible
        return cls(
            order_id=decision.order_id,
            currency=decision.currency,
            # Money crosses the wire as a string: a Decimal that becomes a
            # float in JSON stops being the number the policy engine computed.
            amount=None if amount is None else str(amount),
            amount_label=label,
            applies=applies,
            **common,
        )


class ProposedActionView(BaseModel):
    """A prepared action awaiting a human. Nothing here has happened yet."""

    model_config = ConfigDict(frozen=True)

    action_id: str
    action_type: str
    status: ActionState
    target_type: str
    target_id: str
    account_id: str | None
    parameters: dict[str, str]
    preview: str
    reason: str | None
    expires_at_utc: datetime
    evidence_chunk_ids: list[str] = []

    #: Digest of what this action would do. Echo it back on confirmation to
    #: guarantee the approved proposal is the executed one.
    parameter_fingerprint: str

    #: Restated on every proposal so a client cannot render it as done.
    confirmation_required: bool = True

    @classmethod
    def of(cls, action: ProposedAction) -> ProposedActionView:
        return cls(
            action_id=action.action_id,
            action_type=action.action_type.value,
            status=ActionState.of(action.status),
            target_type=action.target_type,
            target_id=action.target_id,
            account_id=action.account_id,
            parameters=dict(action.parameters),
            preview=action.preview,
            reason=action.reason,
            expires_at_utc=action.expires_at_utc,
            evidence_chunk_ids=list(action.evidence_chunk_ids),
            parameter_fingerprint=action.parameter_fingerprint(),
        )


class ExecutedActionView(BaseModel):
    """The audit record of an action that reached a terminal state.

    Carries the timeline and the effect's identifier — enough to prove what
    happened and when — without exposing table names or row internals.
    """

    model_config = ConfigDict(frozen=True)

    action_id: str
    action_type: str
    status: ActionState
    target_type: str
    target_id: str
    account_id: str | None
    preview: str
    parameters: dict[str, str]

    requested_by: str
    requested_by_role: str
    confirmed_by: str | None
    prepared_at_utc: datetime
    confirmed_at_utc: datetime | None
    executed_at_utc: datetime | None
    rejected_at_utc: datetime | None

    evidence_chunk_ids: list[str] = []
    result: dict[str, str] | None = None
    failure_reason: str | None = None

    @classmethod
    def of(cls, action: ExecutedAction) -> ExecutedActionView:
        return cls(
            action_id=action.action_id,
            action_type=action.action_type.value,
            status=ActionState.of(action.status),
            target_type=action.target_type,
            target_id=action.target_id,
            account_id=action.account_id,
            preview=action.preview,
            parameters=dict(action.parameters),
            requested_by=action.requested_by,
            requested_by_role=action.requested_by_role,
            confirmed_by=action.confirmed_by,
            prepared_at_utc=action.prepared_at_utc,
            confirmed_at_utc=action.confirmed_at_utc,
            executed_at_utc=action.executed_at_utc,
            rejected_at_utc=action.rejected_at_utc,
            evidence_chunk_ids=list(action.evidence_chunk_ids),
            result=action.result,
            failure_reason=action.failure_reason,
        )


# --- responses ---------------------------------------------------------------------


class ChatResponse(BaseModel):
    """The full result of one natural-language request."""

    model_config = ConfigDict(frozen=True)

    request_id: str
    session_id: str
    outcome: ResponseOutcome

    answer: str
    sources: list[SourceRef] = []
    tools_used: list[ToolUse] = []
    policy_decisions: list[PolicyDecisionView] = []

    uncertainties: list[str] = []
    escalation_recommended: bool = False
    step_budget_exhausted: bool = False

    action_status: ActionState = ActionState.NONE
    proposed_action: ProposedActionView | None = None

    #: The caller's resolved context, echoed so a UI can show which identity
    #: and scope produced this answer.
    user_id: str
    role: Role
    account_scope: list[str]

    #: The dataset snapshot every time-based decision was judged against.
    #: Never the wall clock — see docs/architecture.md §9.5.
    reference_time: datetime | None = None
    #: Wall-clock time this response was produced. Technical metadata only;
    #: no business decision uses it.
    responded_at_utc: datetime

    @classmethod
    def of(
        cls,
        response: AgentResponse,
        *,
        session_id: str,
        user_id: str,
        role: Role,
        account_scope: list[str],
        responded_at_utc: datetime,
    ) -> ChatResponse:
        proposed = response.pending_action
        return cls(
            request_id=response.request_id,
            session_id=session_id,
            outcome=response.outcome,
            answer=response.answer,
            sources=[SourceRef.of(item) for item in response.evidence],
            tools_used=[
                ToolUse(
                    step=call.step,
                    tool_name=call.tool_name,
                    status=call.status.value,
                    summary=call.summary,
                    message=call.message,
                )
                for call in response.tool_invocations
            ],
            policy_decisions=[
                PolicyDecisionView.of(decision) for decision in response.decisions
            ],
            uncertainties=list(response.uncertainties),
            escalation_recommended=response.escalation_recommended,
            step_budget_exhausted=response.step_budget_exhausted,
            action_status=ActionState.of(proposed.status if proposed else None),
            proposed_action=None if proposed is None else ProposedActionView.of(proposed),
            user_id=user_id,
            role=role,
            account_scope=account_scope,
            reference_time=response.reference_time,
            responded_at_utc=responded_at_utc,
        )


class ActionConfirmationResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    request_id: str
    action_status: ActionState
    action: ExecutedActionView
    message: str


class PendingActionsResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    count: int
    actions: list[ProposedActionView] = []


class ActionDetailResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    action_status: ActionState
    action: ExecutedActionView


class PrincipalView(BaseModel):
    """A demo identity the API will accept. No credentials involved."""

    model_config = ConfigDict(frozen=True)

    user_id: str
    display_name: str
    role: Role
    description: str
    account_scope: list[str]


class PrincipalsResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    principals: list[PrincipalView] = []


class HealthResponse(BaseModel):
    """Liveness plus enough configuration to know what is running.

    Reports the provider *mode*, never its credentials, and no filesystem
    paths: `/health` is often the most-exposed endpoint in a deployment.
    """

    model_config = ConfigDict(frozen=True)

    status: str
    app_env: str
    provider_mode: str
    model: str | None = None
    max_tool_steps: int
    state_changing_actions_enabled: bool
    #: How this deployment establishes identity. Exposed so a misconfigured
    #: deployment running without real authentication is visible from outside
    #: rather than only in its own configuration.
    auth_mode: str = "session"

    database_ready: bool
    documents_indexed: int
    dataset_snapshot: str | None = None

    checked_at_utc: datetime


class ErrorBody(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str
    message: str
    details: dict = {}
    request_id: str | None = None


class ErrorResponse(BaseModel):
    """The single error envelope every failure path produces.

    Carries a stable machine-readable `code`, a message written to be shown to
    a user, and nothing else. No stack trace, no SQL, no configuration value.
    """

    model_config = ConfigDict(frozen=True)

    error: ErrorBody
