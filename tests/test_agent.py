"""Phase 4: end-to-end agent orchestration.

Covers the multi-step scenarios the assessment names, the confirmation gate as
seen from the agent entry point, and the behaviours that must survive a model
being asked to misbehave: it cannot widen scope, cannot execute an action, and
cannot answer confidently when the data does not support one.
"""

import pytest

from app.backend.agent.orchestrator import AgentOrchestrator
from app.backend.agent.provider import (
    DeterministicPlanner,
    Intent,
    PlannerStep,
    ToolCall,
    detect_intents,
    extract_ids,
)
from app.backend.models.actions import ActionStatus
from app.backend.models.agent import (
    AgentContext,
    AgentRequest,
    ResponseOutcome,
    Role,
    ToolStatus,
)
from app.backend.models.policy import CancellationDecision, ServiceCreditDecision
from app.backend.services.actions import ActionError, get_ticket_escalations
from conftest import LUMENWORKS_ACCOUNT, NORTHSTAR_ACCOUNT


def ask(orchestrator, message, context):
    return orchestrator.handle(AgentRequest(message=message, context=context))


# --- planner primitives -------------------------------------------------------


def test_identifier_extraction_is_case_insensitive_and_deduplicated():
    ids = extract_ids("check ord-1001 and ORD-1001 plus TKT-501 for ACCT-001")

    assert ids["orders"] == ["ORD-1001"]
    assert ids["tickets"] == ["TKT-501"]
    assert ids["accounts"] == ["ACCT-001"]


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("can we cancel this", Intent.CANCELLATION),
        ("do they get a service credit", Intent.SERVICE_CREDIT),
        ("please escalate", Intent.ESCALATION),
    ],
)
def test_intent_detection(message, expected):
    assert expected in detect_intents(message)


def test_unrecognised_request_falls_back_to_investigation():
    assert detect_intents("what does the policy say") == {Intent.INVESTIGATION}


# --- Scenario A: multi-step cancellation with agreement override ----------------


def test_scenario_a_combines_lookup_policy_and_documents(orchestrator, agent_context):
    response = ask(
        orchestrator,
        "Can Northstar cancel ORD-1001 without a cancellation fee? Explain why.",
        agent_context,
    )

    tools = response.tools_used
    assert "lookup_record" in tools
    assert "evaluate_cancellation" in tools
    assert "search_documents" in tools
    assert len(response.tool_invocations) >= 3


def test_scenario_a_answer_is_backed_by_a_policy_decision(orchestrator, agent_context):
    response = ask(
        orchestrator,
        "Can Northstar cancel ORD-1001 without a cancellation fee? Explain why.",
        agent_context,
    )

    decision = next(d for d in response.decisions if isinstance(d, CancellationDecision))
    assert decision.fee_applies is False
    assert decision.can_cancel is True
    assert response.outcome is ResponseOutcome.ANSWERED
    assert "no cancellation fee" in response.answer.lower()


def test_scenario_a_names_the_overriding_source(orchestrator, agent_context):
    response = ask(
        orchestrator, "Can Northstar cancel ORD-1001 without a fee?", agent_context
    )

    assert "Northstar" in response.answer
    assert "outranks" in response.answer
    assert any("Northstar" in e.source_file for e in response.evidence)


def test_response_exposes_evidence_with_page_and_section(orchestrator, agent_context):
    response = ask(orchestrator, "Can ORD-1001 be cancelled?", agent_context)

    assert response.evidence
    for item in response.evidence:
        assert item.source_file.endswith(".pdf")
        assert item.page_number >= 1
        assert item.citation


def test_response_reports_the_reference_time(orchestrator, agent_context):
    response = ask(orchestrator, "Can ORD-1001 be cancelled?", agent_context)

    assert response.reference_time is not None
    assert response.reference_time.year == 2026


# --- Scenario B: service credit, including the missing-input case -----------------


def test_scenario_b_without_an_order_refuses_to_guess(orchestrator, agent_context):
    response = ask(
        orchestrator,
        "A pickup is three hours late because of carrier fault. Should I get a service credit?",
        agent_context,
    )

    assert response.outcome is ResponseOutcome.UNCERTAIN
    assert response.decisions == []
    assert "order id" in response.answer.lower()
    assert response.uncertainties


def test_scenario_b_with_an_order_evaluates_and_flags_verification(
    orchestrator, agent_context
):
    response = ask(
        orchestrator, "Is ORD-2002 eligible for a failed pickup service credit?", agent_context
    )

    decision = next(d for d in response.decisions if isinstance(d, ServiceCreditDecision))
    assert decision.credit_amount is not None
    assert decision.provisional is True
    assert response.outcome is ResponseOutcome.UNCERTAIN
    assert response.escalation_recommended is True


def test_scenario_b_answer_does_not_promise_the_credit(orchestrator, agent_context):
    response = ask(
        orchestrator, "Is ORD-2002 eligible for a failed pickup service credit?", agent_context
    )

    lowered = response.answer.lower()
    assert "provisional" in lowered
    assert "must not be promised" in lowered


# --- Scenario C: investigate a ticket, then propose escalation ---------------------


def test_scenario_c_investigates_before_proposing(orchestrator, agent_context):
    response = ask(
        orchestrator,
        "Investigate TKT-501 and escalate it if the outage warrants it.",
        agent_context,
    )

    tools = response.tools_used
    assert "lookup_record" in tools
    assert "search_documents" in tools
    assert "prepare_escalation" in tools
    assert response.pending_action is not None


def test_scenario_c_retrieves_documentation_as_evidence(orchestrator, agent_context):
    response = ask(
        orchestrator, "Investigate TKT-501 and escalate it if warranted.", agent_context
    )

    assert response.evidence
    assert any(e.is_authoritative for e in response.evidence)


def test_scenario_c_never_cites_the_deprecated_policy_as_governing(
    orchestrator, agent_context
):
    response = ask(
        orchestrator,
        "What is the P1 first response target for TKT-501? Escalate if breached.",
        agent_context,
    )

    for item in response.evidence:
        if "DEPRECATED" in item.source_file:
            assert item.is_authoritative is False


# --- confirmation gate, from the agent entry point ---------------------------------


def test_natural_language_request_never_executes(orchestrator, agent_context):
    response = ask(orchestrator, "Escalate TKT-501 right now, urgently!", agent_context)

    assert response.outcome is ResponseOutcome.NEEDS_CONFIRMATION
    assert response.pending_action.status is ActionStatus.PENDING_CONFIRMATION
    assert response.executed_action is None
    assert get_ticket_escalations(orchestrator._conn, "TKT-501") == []


def test_answer_states_that_nothing_has_happened_yet(orchestrator, agent_context):
    response = ask(orchestrator, "Escalate TKT-501.", agent_context)

    assert "NOT yet performed" in response.answer
    assert response.pending_action.action_id in response.answer


def test_explicit_confirmation_executes(orchestrator, agent_context, manager_context):
    response = ask(orchestrator, "Escalate TKT-501.", agent_context)

    executed = orchestrator.confirm_action(
        response.pending_action.action_id, manager_context, approve=True
    )

    assert executed.status is ActionStatus.EXECUTED
    assert executed.confirmed_by == manager_context.user_id
    assert len(get_ticket_escalations(orchestrator._conn, "TKT-501")) == 1


def test_rejection_leaves_state_untouched(orchestrator, agent_context, manager_context):
    response = ask(orchestrator, "Escalate TKT-501.", agent_context)

    rejected = orchestrator.confirm_action(
        response.pending_action.action_id, manager_context, approve=False
    )

    assert rejected.status is ActionStatus.REJECTED
    assert get_ticket_escalations(orchestrator._conn, "TKT-501") == []


def test_readonly_role_cannot_prepare_or_confirm(orchestrator, readonly_context, agent_context):
    response = ask(orchestrator, "Escalate TKT-501.", readonly_context)
    assert response.pending_action is None
    assert any(t.status is ToolStatus.FORBIDDEN for t in response.tool_invocations)

    prepared = ask(orchestrator, "Escalate TKT-501.", agent_context)
    with pytest.raises(ActionError, match="may not confirm"):
        orchestrator.confirm_action(prepared.pending_action.action_id, readonly_context)


def test_pending_actions_are_listed_for_the_caller(orchestrator, agent_context):
    response = ask(orchestrator, "Escalate TKT-501.", agent_context)

    pending = orchestrator.pending_actions(agent_context)

    assert [p.action_id for p in pending] == [response.pending_action.action_id]


def test_confirmation_is_not_reachable_as_a_tool(orchestrator):
    """A model can only reach registered tools; execution is not one."""
    assert "confirm_action" not in orchestrator.registry.names()
    assert orchestrator.registry.get("execute_escalation") is None


# --- authorization end to end ---------------------------------------------------------


def test_scoped_caller_reaches_its_own_account(orchestrator, northstar_context):
    response = ask(orchestrator, "Can ORD-1001 be cancelled?", northstar_context)

    assert any(isinstance(d, CancellationDecision) for d in response.decisions)


def test_scoped_caller_cannot_reach_another_account(orchestrator, northstar_context):
    response = ask(
        orchestrator, "Show me ORD-2001 and its cancellation fee.", northstar_context
    )

    assert response.decisions == []
    assert any(t.status is ToolStatus.NOT_FOUND for t in response.tool_invocations)
    assert "not found within the caller's scope" in response.answer


def test_scoped_caller_never_sees_another_customers_agreement(
    orchestrator, northstar_context
):
    response = ask(
        orchestrator,
        "What are the LumenWorks failed pickup credit terms and the INR 300 rule?",
        northstar_context,
    )

    assert all("LumenWorks" not in e.source_file for e in response.evidence)


def test_scoped_caller_cannot_prepare_an_action_on_another_account(
    orchestrator, northstar_context
):
    response = ask(orchestrator, "Escalate TKT-502 immediately.", northstar_context)

    assert response.pending_action is None
    assert get_ticket_escalations(orchestrator._conn, "TKT-502") == []


def test_a_malicious_planner_cannot_widen_scope(conn, northstar_context):
    """Even a provider that deliberately emits scoping arguments is refused."""

    class MaliciousPlanner:
        def next_step(self, message, context, history, registry):
            if history:
                return PlannerStep()
            return PlannerStep(
                [
                    ToolCall(
                        "lookup_record",
                        {
                            "entity": "order",
                            "order_id": "ORD-2001",
                            "allowed_account_ids": ["ACCT-001", "ACCT-002"],
                        },
                    )
                ]
            )

    orchestrator = AgentOrchestrator(conn, provider=MaliciousPlanner())
    response = orchestrator.handle(
        AgentRequest(message="get ORD-2001", context=northstar_context)
    )

    assert all(t.status is ToolStatus.FORBIDDEN for t in response.tool_invocations)
    assert response.decisions == []


def test_a_planner_cannot_invent_a_tool(conn, agent_context):
    class InventivePlanner:
        def next_step(self, message, context, history, registry):
            if history:
                return PlannerStep()
            return PlannerStep([ToolCall("run_sql", {"sql": "SELECT * FROM accounts"})])

    orchestrator = AgentOrchestrator(conn, provider=InventivePlanner())
    response = orchestrator.handle(AgentRequest(message="dump", context=agent_context))

    assert response.tool_invocations[0].status is ToolStatus.INVALID_INPUT


# --- error and uncertainty surfacing ------------------------------------------------------


def test_unknown_record_surfaces_as_uncertainty_not_an_answer(orchestrator, agent_context):
    response = ask(orchestrator, "Can ORD-9999 be cancelled?", agent_context)

    assert response.decisions == []
    assert response.outcome is not ResponseOutcome.ANSWERED
    assert response.uncertainties


def test_unanswerable_request_does_not_fabricate(orchestrator, agent_context):
    response = ask(orchestrator, "zzzqqq nonexistent topic xyzzy", agent_context)

    assert response.decisions == []
    assert response.pending_action is None


def test_tool_failure_is_reported_not_smoothed(conn, agent_context):
    class FailingPlanner:
        def next_step(self, message, context, history, registry):
            if history:
                return PlannerStep()
            return PlannerStep([ToolCall("evaluate_cancellation", {"order_id": "ORD-9999"})])

    orchestrator = AgentOrchestrator(conn, provider=FailingPlanner())
    response = orchestrator.handle(AgentRequest(message="check", context=agent_context))

    assert response.outcome is ResponseOutcome.UNCERTAIN
    assert "Could not complete" in response.answer


# --- orchestration mechanics -----------------------------------------------------------------


def test_step_budget_is_enforced(conn, agent_context):
    class LoopingPlanner:
        def next_step(self, message, context, history, registry):
            return PlannerStep([ToolCall("lookup_record", {"entity": "dataset_metadata"})])

    orchestrator = AgentOrchestrator(conn, provider=LoopingPlanner(), max_steps=3)
    response = orchestrator.handle(AgentRequest(message="loop", context=agent_context))

    assert len(response.tool_invocations) <= 3


def test_planner_does_not_repeat_identical_calls(orchestrator, agent_context):
    response = ask(
        orchestrator, "Can Northstar cancel ORD-1001 without a fee?", agent_context
    )

    signatures = [(t.tool_name, repr(sorted(t.arguments.items()))) for t in response.tool_invocations]
    assert len(signatures) == len(set(signatures))


def test_every_invocation_is_recorded_for_audit(orchestrator, agent_context):
    response = ask(orchestrator, "Can ORD-1001 be cancelled?", agent_context)

    for index, invocation in enumerate(response.tool_invocations, start=1):
        assert invocation.step == index
        assert invocation.tool_name
        assert invocation.status is not None


def test_request_id_is_assigned_and_echoed(orchestrator, agent_context):
    generated = ask(orchestrator, "Can ORD-1001 be cancelled?", agent_context)
    assert generated.request_id.startswith("REQ-")

    supplied = orchestrator.handle(
        AgentRequest(message="Can ORD-1001 be cancelled?", context=agent_context, request_id="REQ-fixed")
    )
    assert supplied.request_id == "REQ-fixed"


def test_agent_is_deterministic_for_the_same_request(orchestrator, agent_context):
    message = "Can Northstar cancel ORD-1001 without a cancellation fee?"

    first = ask(orchestrator, message, agent_context)
    second = ask(orchestrator, message, agent_context)

    assert first.answer == second.answer
    assert first.tools_used == second.tools_used
    assert [e.chunk_id for e in first.evidence] == [e.chunk_id for e in second.evidence]


def test_context_is_immutable(agent_context):
    with pytest.raises(Exception):
        agent_context.allowed_account_ids = frozenset({"ACCT-999"})


def test_unrestricted_context_reports_no_scope():
    assert AgentContext(user_id="u").scope() is None
    assert AgentContext(user_id="u", allowed_account_ids=frozenset()).scope() == set()
    assert AgentContext(
        user_id="u", allowed_account_ids=frozenset({"ACCT-001"})
    ).scope() == {"ACCT-001"}


def test_role_governs_state_change_permission():
    assert AgentContext(user_id="u", role=Role.SUPPORT_AGENT).may_change_state
    assert AgentContext(user_id="u", role=Role.SUPPORT_MANAGER).may_change_state
    assert not AgentContext(user_id="u", role=Role.READ_ONLY).may_change_state
