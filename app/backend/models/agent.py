"""Agent request/response and tool-invocation models (Phase 4).

`AgentContext` is the authorization envelope. It is constructed by the caller
(later: from an authenticated session) and threaded through every tool call.
Tool *arguments* proposed by the model never carry scoping — the executor
injects it from the context — so there is no argument a model can emit that
widens what it can see.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.backend.models.actions import ExecutedAction, ProposedAction
from app.backend.models.documents import Evidence
from app.backend.models.policy import PolicyDecision


class Role(StrEnum):
    """Who is asking, in the terms the authorization layer cares about.

    Phase 4 shipped the three internal roles. Phase 5 adds `CUSTOMER` for the
    external context the API must be able to demonstrate — an account holder
    talking to the assistant about their own account. It is additive: a
    customer is scoped to their own account ids and, like `READ_ONLY`, may not
    change state, so no existing behaviour shifts.
    """

    SUPPORT_AGENT = "support_agent"
    SUPPORT_MANAGER = "support_manager"
    READ_ONLY = "read_only"
    CUSTOMER = "customer"


class AgentContext(BaseModel):
    """Who is asking, and what they are permitted to see.

    `allowed_account_ids = None` means unrestricted, matching the existing
    Phase 2/3 convention for a caller with no scoping applied. An empty set
    means authorized for no customer-specific data at all.

    Frozen, and never derived from model output: the orchestrator receives
    this from its caller and passes the same instance to every tool.
    """

    model_config = ConfigDict(frozen=True)

    user_id: str
    role: Role = Role.SUPPORT_AGENT
    allowed_account_ids: frozenset[str] | None = None

    #: The conversation this context belongs to, when the caller has one.
    #: Phase 5 binds prepared actions to it so a confirmation arriving from a
    #: different conversation is refused even when the account scope matches.
    #: `None` keeps every Phase 4 caller working unchanged.
    session_id: str | None = None

    @property
    def may_change_state(self) -> bool:
        return self.role in (Role.SUPPORT_AGENT, Role.SUPPORT_MANAGER)

    def scope(self) -> set[str] | None:
        """The value to hand to Phase 2/3 repository functions."""
        return None if self.allowed_account_ids is None else set(self.allowed_account_ids)


class ToolStatus(StrEnum):
    OK = "ok"
    NOT_FOUND = "not_found"
    FORBIDDEN = "forbidden"
    INVALID_INPUT = "invalid_input"
    NO_EVIDENCE = "no_evidence"
    UNCERTAIN = "uncertain"
    ERROR = "error"


class ToolResult(BaseModel):
    """Outcome of one tool call.

    Failures stay failures: a missing record, an out-of-scope request, or an
    unparseable argument returns a non-OK status with a message. Nothing here
    is smoothed into prose — that would let the orchestrator present a
    guess as an answer.
    """

    model_config = ConfigDict(frozen=True)

    status: ToolStatus = ToolStatus.OK
    data: dict = Field(default_factory=dict)
    message: str | None = None
    evidence: list[Evidence] = []
    decisions: list[PolicyDecision] = []
    proposed_action: ProposedAction | None = None

    @property
    def ok(self) -> bool:
        return self.status is ToolStatus.OK


class ToolInvocation(BaseModel):
    """An audit entry: which tool ran, with what arguments, and what came back.

    Recorded for every step so a response can show its work and a reviewer can
    reconstruct how an answer was reached.
    """

    model_config = ConfigDict(frozen=True)

    step: int
    tool_name: str
    arguments: dict
    status: ToolStatus
    message: str | None = None
    summary: str | None = None


class ResponseOutcome(StrEnum):
    ANSWERED = "answered"
    NEEDS_CONFIRMATION = "needs_confirmation"
    UNCERTAIN = "uncertain"
    REFUSED = "refused"
    ERROR = "error"


class AgentRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    message: str
    context: AgentContext
    request_id: str | None = None


class AgentResponse(BaseModel):
    """Everything a UI needs to render an answer with its provenance.

    Deliberately flat and fully typed so the future chat surface can show the
    answer, the evidence behind it, the tools that ran, and the action state
    without re-deriving any of it.
    """

    model_config = ConfigDict(frozen=True)

    request_id: str
    outcome: ResponseOutcome
    answer: str

    evidence: list[Evidence] = []
    tool_invocations: list[ToolInvocation] = []
    decisions: list[PolicyDecision] = []

    pending_action: ProposedAction | None = None
    executed_action: ExecutedAction | None = None

    uncertainties: list[str] = []
    escalation_recommended: bool = False
    reference_time: datetime | None = None

    #: True when the orchestration loop stopped because it ran out of tool
    #: steps while the provider still had work queued. Reported rather than
    #: hidden: a truncated investigation is a reason to distrust the answer.
    step_budget_exhausted: bool = False

    @property
    def tools_used(self) -> list[str]:
        seen: list[str] = []
        for invocation in self.tool_invocations:
            if invocation.tool_name not in seen:
                seen.append(invocation.tool_name)
        return seen
