"""The agent orchestration loop (Phase 4).

Runs a bounded plan/execute cycle: ask the provider what to do next, execute
the tool calls it names under the caller's authorization context, feed the
results back, repeat until the provider stops or the step budget is spent.

Two boundaries this class exists to hold:

- **Authorization is the orchestrator's, not the planner's.** The same
  `AgentContext` instance goes to every tool call. Nothing the provider emits
  can alter it.

- **Confirmation is out of band.** `handle` can *prepare* a state-changing
  action, and that is all it can do. `confirm_action` and `reject_action` are
  separate methods, reachable only by the application calling them on behalf
  of a human — never by the provider, because no tool exposes them.
"""

from __future__ import annotations

import sqlite3
import uuid

from app.backend.agent.composer import compose
from app.backend.agent.provider import (
    DeterministicPlanner,
    PlannerStep,
    PlanningProvider,
    StepRecord,
    detect_intents,
)
from app.backend.models.actions import ExecutedAction
from app.backend.models.agent import (
    AgentContext,
    AgentRequest,
    AgentResponse,
    ResponseOutcome,
    ToolInvocation,
    ToolStatus,
)
from app.backend.policies.base import PolicyDataError, load_evaluation_context
from app.backend.services.actions import (
    ActionForbidden,
    ActionNotFound,
    ActionSessionError,
    ActionStateError,
    execute_action,
    get_action,
    list_pending_actions,
    reject_action,
)
from app.backend.services.records import get_ticket
from app.backend.tools.base import ToolRegistry
from app.backend.tools.registry import build_default_registry

DEFAULT_MAX_STEPS = 8


class AgentOrchestrator:
    """Entry point for natural-language requests and action confirmation."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        provider: PlanningProvider | None = None,
        registry: ToolRegistry | None = None,
        max_steps: int = DEFAULT_MAX_STEPS,
    ) -> None:
        self._conn = conn
        self._provider = provider or DeterministicPlanner()
        self._registry = registry or build_default_registry()
        self._max_steps = max_steps

    @property
    def registry(self) -> ToolRegistry:
        return self._registry

    # --- natural-language requests -------------------------------------------

    def handle(self, request: AgentRequest) -> AgentResponse:
        """Investigate a request and return an evidence-backed response.

        Never executes a state-changing action, whatever the request says. The
        strongest it can do is return a `pending_action` awaiting confirmation.
        """
        request_id = request.request_id or f"REQ-{uuid.uuid4().hex[:12]}"
        context = request.context
        history: list[StepRecord] = []
        invocations: list[ToolInvocation] = []
        step = 0
        # Initialised so the budget check below is well-defined even when the
        # loop never runs (max_steps = 0).
        plan = PlannerStep()

        while step < self._max_steps:
            plan = self._provider.next_step(
                request.message, context, history, self._registry
            )
            if plan.is_final:
                break

            for call in plan.tool_calls:
                step += 1
                result = self._registry.execute(
                    self._conn, context, call.tool_name, call.arguments
                )
                history.append(StepRecord(call.tool_name, dict(call.arguments), result))
                invocations.append(
                    ToolInvocation(
                        step=step,
                        tool_name=call.tool_name,
                        arguments=dict(call.arguments),
                        status=result.status,
                        message=result.message,
                        summary=_summarise(result),
                    )
                )
            if step >= self._max_steps:
                break

        # True only when the loop stopped because the budget ran out while the
        # provider still had work queued — a genuinely truncated investigation,
        # not a provider that simply finished on its last allowed step.
        budget_exhausted = step >= self._max_steps and not plan.is_final

        answer, outcome, uncertainties = compose(
            request.message, history, detect_intents(request.message)
        )
        if plan_answer := _provider_answer(self._provider, history):
            answer = plan_answer
        if budget_exhausted:
            note = (
                f"The investigation stopped after the maximum of {self._max_steps} "
                f"tool steps and may be incomplete."
            )
            uncertainties = [*uncertainties, note]
            answer = "\n".join([answer, note]) if answer else note

        evidence = _collect_evidence(history)
        decisions = [d for record in history for d in record.result.decisions]
        proposals = [
            record.result.proposed_action
            for record in history
            if record.result.proposed_action
        ]

        return AgentResponse(
            request_id=request_id,
            outcome=outcome,
            answer=answer,
            evidence=evidence,
            tool_invocations=invocations,
            decisions=decisions,
            pending_action=proposals[-1] if proposals else None,
            uncertainties=uncertainties,
            escalation_recommended=_should_escalate(history, uncertainties),
            reference_time=self._reference_time(),
            step_budget_exhausted=budget_exhausted,
        )

    # --- confirmation, out of the provider's reach ----------------------------

    def confirm_action(
        self,
        action_id: str,
        context: AgentContext,
        *,
        approve: bool = True,
        expected_fingerprint: str | None = None,
        require_matching_session: bool = True,
    ) -> ExecutedAction:
        """Execute or reject a prepared action on a human's explicit instruction.

        This is not a tool and is not registered anywhere the provider can see.
        Reaching it requires the application to call it, which is what makes
        "the user confirmed" a fact about application state rather than a claim
        in a transcript.

        Authorization is re-checked here, not inherited from whoever prepared
        the action, and the target is re-validated before any effect is written.
        """
        if not context.may_change_state:
            raise ActionForbidden(
                f"role {context.role.value!r} may not confirm state-changing actions"
            )

        scope = context.scope()
        action = get_action(self._conn, action_id, allowed_account_ids=scope)
        if action is None:
            raise ActionNotFound(f"action {action_id!r} not found or not in scope")

        self._check_session(action, context, require_matching_session)

        if not approve:
            return reject_action(
                self._conn, action_id, rejected_by=context.user_id, allowed_account_ids=scope
            )

        if expected_fingerprint is not None:
            # What the human approved must be what is about to run. A stored
            # proposal whose parameters no longer digest to the fingerprint the
            # confirmation dialog showed is refused rather than executed.
            actual = action.parameter_fingerprint()
            if actual != expected_fingerprint:
                raise ActionStateError(
                    f"action {action_id!r} no longer matches the reviewed proposal; "
                    f"re-read the action and confirm again"
                )

        # Re-validate the target under the *confirming* caller's scope: the
        # preview may have been produced by someone else, or minutes ago.
        target_exists = True
        if action.target_type == "ticket":
            target_exists = (
                get_ticket(self._conn, action.target_id, allowed_account_ids=scope)
                is not None
            )

        return execute_action(
            self._conn,
            action_id,
            confirmed_by=context.user_id,
            allowed_account_ids=scope,
            target_exists=target_exists,
        )

    @staticmethod
    def _check_session(action, context: AgentContext, required: bool) -> None:
        """Refuse a confirmation arriving from a different conversation.

        An action bound to no session (a Phase 4 caller, or a script) stays
        confirmable by any authorized caller — binding did not happen, so
        there is nothing to violate. Once bound, the binding is enforced:
        account scope alone is not enough, because two conversations under one
        support agent are still two separate approvals.
        """
        if not required or action.session_id is None:
            return
        if context.session_id != action.session_id:
            # Same wording as "not found" would give, but a distinct type: the
            # caller can see the action, so hiding it here would confuse
            # rather than protect.
            raise ActionSessionError(
                f"action {action.action_id!r} was prepared in a different "
                f"conversation and cannot be confirmed from this one"
            )

    def pending_actions(self, context: AgentContext):
        return list_pending_actions(self._conn, allowed_account_ids=context.scope())

    # --- helpers ---------------------------------------------------------------

    def _reference_time(self):
        try:
            return load_evaluation_context(self._conn).reference_time
        except PolicyDataError:
            return None


def _provider_answer(provider: PlanningProvider, history: list[StepRecord]) -> str | None:
    """Prose supplied by the provider itself, if any.

    `DeterministicPlanner` supplies none, so the deterministic composer's text
    stands. A future LLM provider can return its own explanation here.
    """
    return getattr(provider, "final_answer", None)


def _collect_evidence(history: list[StepRecord]):
    seen: set[str] = set()
    evidence = []
    for record in history:
        for item in record.result.evidence:
            if item.chunk_id not in seen:
                seen.add(item.chunk_id)
                evidence.append(item)
    return evidence


def _summarise(result) -> str | None:
    if result.decisions:
        return "; ".join(f"{d.decision_type}={d.outcome.value}" for d in result.decisions)
    if result.proposed_action:
        return f"prepared {result.proposed_action.action_id} (pending confirmation)"
    if result.evidence:
        return f"{len(result.evidence)} evidence item(s)"
    if entity := result.data.get("entity"):
        return f"{entity} lookup"
    return None


def _should_escalate(history: list[StepRecord], uncertainties: list[str]) -> bool:
    """Recommend escalation when the sources say to, not as a default.

    Triggers on a policy decision that demands verification, an unresolved
    cross-source conflict, or a tool error — each of which the supplied
    documents treat as a reason to involve a human rather than proceed.

    A *settled* decision can also demand escalation. The current support
    policy directs that P1 incidents be escalated immediately and that a
    breached response target be stated and escalated rather than reported
    quietly — neither of which is an uncertainty, so neither would be caught
    by the checks above. Both are read from fields the policy engine set, so
    this stays a projection of a deterministic decision rather than a second
    opinion about it.
    """
    for record in history:
        if record.result.status in (ToolStatus.UNCERTAIN, ToolStatus.ERROR):
            return True
        conflicts = record.result.data.get("conflicts")
        if isinstance(conflicts, list) and conflicts:
            return True
        for decision in record.result.decisions:
            if getattr(decision, "requires_immediate_escalation", False):
                return True
            if getattr(decision, "breached", None) is True:
                return True
    return bool(uncertainties)
