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

    **`SUPPORT_MANAGER` now grants something `SUPPORT_AGENT` does not.** Both
    satisfy `may_change_state`, so either may confirm a routine action; only
    the manager satisfies `may_approve_high_value`, which gates confirming a
    service credit above the SOP's stated threshold. Until `issue_service_credit`
    existed the distinction had nothing to attach to — the SOP's "any
    individual credit above INR 1,000 requires manager approval" was computed
    and reported by `policies/service_credit.py` but could not be enforced,
    because no action this system shipped issued a credit. See docs/product.md.
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

    #: The organisation this context acts within, when the caller authenticated
    #: into one. Set from the *session*, never from the request body: it is the
    #: tenant boundary, and `allowed_account_ids` above is derived from it by
    #: `auth/repository.py::accounts_for_org`.
    org_id: str | None = None

    #: The permissions the caller's membership grants, as
    #: `app/backend/auth/permissions.py` defines them. `None` means the context
    #: was built without an organisation — a script, or a Phase 4 caller — in
    #: which case the legacy `Role` mapping below decides.
    permissions: frozenset[str] | None = None

    def has_permission(self, permission: str) -> bool:
        """Whether this caller holds one named permission.

        When a permission set is present it is authoritative and the role is
        ignored entirely. Consulting both would mean two answers to one
        question, and the looser one would eventually win somewhere.
        """
        if self.permissions is None:
            return False
        return permission in self.permissions

    @property
    def may_change_state(self) -> bool:
        """Whether this caller may prepare and confirm state-changing actions.

        Reads the permission set when the caller authenticated into an
        organisation, and falls back to the original role mapping only for a
        context built without one. The fallback is what keeps every Phase 4
        caller — and the deterministic test suite — working unchanged; it is
        not a second authorization path for authenticated users, because the
        branch above returns first whenever `permissions` is set.
        """
        if self.permissions is not None:
            return "execute_action" in self.permissions
        return self.role in (Role.SUPPORT_AGENT, Role.SUPPORT_MANAGER)

    @property
    def may_approve_high_value(self) -> bool:
        """Whether this caller may confirm an action needing manager approval.

        The SOP's "any individual credit above INR 1,000 requires manager
        approval" is the rule this answers. It is deliberately *narrower* than
        `may_change_state`: an OPERATIONS member confirms routine actions, and
        the SOP asks for a second, higher signature on the ones that cost
        money.

        Same two-branch shape as `may_change_state`, and for the same reason.
        Under real authentication the answer comes from the permission set the
        server issued; the role fallback exists only for a context built
        without one, where `SUPPORT_MANAGER` is what the role has always meant
        and now, for the first time, is enforced rather than merely modelled.

        Never read from a request body, a tool argument, or model output.
        """
        if self.permissions is not None:
            return "approve_high_value_action" in self.permissions
        return self.role is Role.SUPPORT_MANAGER

    @property
    def may_propose_action(self) -> bool:
        """Whether this caller may *prepare* an action for someone to confirm.

        Separate from `may_change_state` on purpose: a SUPPORT member drafts
        and an OPERATIONS member executes. Collapsing the two would hand every
        support user the confirmation right the whole gate exists to withhold.
        """
        if self.permissions is not None:
            return "propose_action" in self.permissions
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

    # --- trust (Phase 2) -----------------------------------------------------
    #
    # `outcome` above says what shape this response has; the fields below say
    # how far it can be relied on. The two are separate axes and come apart
    # routinely — a well-formed answer resting on two contradictory sources is
    # ANSWERED and not trustworthy. Both are derived in code from tool results
    # (see `agent/trust.py`); neither is asserted by a model.

    #: `TrustStatus` as a plain string, so this model stays importable from
    #: `agent/trust.py` without a cycle.
    trust_status: str = "confident"
    #: Why the status is what it is. Safe to show a user: every entry is drawn
    #: from a tool message or a policy verification reason.
    trust_reasons: list[str] = []
    #: The strongest authority tier that actually governed, as an int, or None
    #: when nothing governed.
    governing_authority_tier: int | None = None
    #: True when a signed customer agreement decided the answer rather than the
    #: default policy — the single most consequential fact about a support
    #: answer, and the one a reader is most likely to assume wrongly.
    customer_agreement_applied: bool = False
    #: Composed notes from the authority layer: what outranked what.
    authority_overrides: list[str] = []
    #: Sources of equal authority that precedence could not separate.
    authority_conflicts: list[str] = []
    #: Set only when the trust status is `escalate`.
    escalation_reason: str | None = None

    #: The intents the planner recognised, recorded so an operator can see why
    #: a given set of tools ran. Not chain-of-thought: these are the routing
    #: labels the deterministic planner matched, not the model's reasoning.
    intents: list[str] = []

    @property
    def tools_used(self) -> list[str]:
        seen: list[str] = []
        for invocation in self.tool_invocations:
            if invocation.tool_name not in seen:
                seen.append(invocation.tool_name)
        return seen
