"""Ticket investigations: the response clock, pickup conflicts, retrieval, trust.

What these hold, in the order a person investigating a ticket meets them:

- **The clock is always read.** Investigating a ticket runs the SLA calculation
  without the request having to say "SLA". Severity is still never inferred: a
  ticket whose text matches the current policy's P1 definition is *indicated* as
  such, for a person to verify, and no breach is asserted from it.
- **A BOOKED order is not proof the parcel is still there.** An open ticket from
  the same customer saying the driver has been makes cancellation
  unconfirmable until someone checks with the carrier. The customer's fee waiver
  is untouched: the doubt is about the shipment's state, not the fee.
- **A ticket says what to look up.** Its subject and description join the query,
  and a resolved known issue is context, never the rule that decides an answer.
- **Caution carries into the trust status**, and an escalation is grounded in
  the findings, not in the operator's own sentence.

Nothing here pins a memorised answer: the expectations are about provenance and
process, and the synthetic tickets use wording the pack never does.
"""

from __future__ import annotations

import pytest

from app.backend.agent.orchestrator import AgentOrchestrator
from app.backend.agent.provider import _search_query
from app.backend.models.agent import AgentContext, AgentRequest, Role, ToolStatus
from app.backend.models.policy import PolicyOutcome, Severity
from app.backend.policies.cancellation import evaluate_cancellation
from app.backend.policies.pickup_evidence import find_pickup_reports
from app.backend.policies.severity_indication import (
    extract_definition_criteria,
    indicate,
)
from app.backend.policies.sla import evaluate_sla
from app.backend.services.actions import ActionForbidden, ActionStateError
from app.backend.services.database import get_connection
from app.backend.services.records import get_order
from app.backend.tenancy import LEGACY_ORG_ID, LEGACY_SCOPE
from conftest import NORTHSTAR_ACCOUNT


def ask(orchestrator, message, context):
    return orchestrator.handle(AgentRequest(message=message, context=context))


def tools_run(response):
    return [(t.tool_name, t.arguments) for t in response.tool_invocations]


def sla_decision(response):
    return next(d for d in response.decisions if d.decision_type == "sla")


def cancellation_decision(response):
    return next(d for d in response.decisions if d.decision_type == "cancellation")


# --- 1. the clock is always read; severity is only ever indicated --------------------------


@pytest.mark.parametrize(
    "message",
    ["Investigate TKT-501 and tell me what to do", "What should we do about TKT-501?", "TKT-501"],
)
def test_investigating_a_ticket_runs_the_sla_calculation_without_being_asked(
    orchestrator, agent_context, message
):
    response = ask(orchestrator, message, agent_context)

    assert "evaluate_sla" in [name for name, _ in tools_run(response)]
    assert "sla" not in response.intents


def test_a_ticket_matching_the_p1_definition_is_indicated_never_classified(
    orchestrator, agent_context
):
    response = ask(orchestrator, "Investigate TKT-501 and tell me what to do", agent_context)
    decision = sla_decision(response)

    assert decision.severity is None  # not set from the indication
    assert decision.breached is None  # no breach asserted from it
    assert decision.outcome is PolicyOutcome.REQUIRES_VERIFICATION
    (indication,) = decision.severity_indications
    assert indication.severity is Severity.P1
    assert "01_Support_Policy_v3_CURRENT.pdf" in indication.source
    # What it would mean if confirmed, on the governing (agreement) target.
    assert indication.target_minutes == 15 and indication.elapsed_exceeds_target is True
    assert "matches the current policy's P1 definition" in response.answer
    assert "not a classification" in response.answer
    assert response.trust_status == "conditional"


def test_a_p1_indication_advises_escalation_and_says_why(orchestrator, agent_context):
    response = ask(orchestrator, "Investigate TKT-505 and tell me what to do", agent_context)

    assert response.escalation_recommended is True
    assert "P1 definition" in (response.escalation_reason or "")
    assert response.pending_action is None  # advice only; nothing is prepared


def test_a_ticket_that_does_not_resemble_a_severity_definition_is_not_indicated(
    orchestrator, agent_context
):
    for ticket in ("TKT-502", "TKT-503", "TKT-504"):
        response = ask(orchestrator, f"What is {ticket} about?", agent_context)
        decision = sla_decision(response)
        assert decision.severity_indications == [], ticket
        assert decision.informational, ticket


def test_a_background_reading_leaves_a_plain_lookup_confident(orchestrator, agent_context):
    response = ask(orchestrator, "What is TKT-503 about?", agent_context)

    assert response.trust_status == "confident"
    assert response.escalation_recommended is False
    assert "Response clock for TKT-503" in response.answer
    # The clock accompanies the answer; it does not replace the record.
    assert "billing" in response.answer.lower()


def test_a_closed_ticket_is_not_given_a_response_clock_unless_asked(
    orchestrator, agent_context
):
    quiet = ask(orchestrator, "What was TKT-450 about?", agent_context)
    assert "evaluate_sla" not in [name for name, _ in tools_run(quiet)]


def test_a_stated_severity_is_still_passed_through_and_still_escalates(
    orchestrator, agent_context
):
    response = ask(orchestrator, "TKT-501 is a P1. Has it breached its SLA?", agent_context)
    decision = sla_decision(response)

    assert decision.severity is Severity.P1 and decision.severity_source == "supplied by caller"
    assert decision.breached is True and decision.severity_indications == []
    assert response.trust_status == "escalate"


def test_the_indication_reads_the_policy_not_the_ticket_author():
    """Any policy's definitions can be matched; a different wording changes nothing
    about how, and an unrecognised wording matches nothing."""

    class Chunk:
        account_id = None
        text = (
            "2. Severity definitions\n"
            "P1 - Critical: Total loss of the booking service for a customer, or "
            "a leaked password or token.\n"
            "P3 - Normal: A how-to question."
        )
        citation = "policy.pdf p.1 §2"
        chunk_id = "policy#p1#c02"

    criteria = extract_definition_criteria([Chunk()])
    assert {c.severity for c in criteria} == {Severity.P1, Severity.P3}

    hit = indicate("Our password was leaked on a paste site", criteria)
    assert [m.criterion.severity for m in hit] == [Severity.P1]
    assert indicate("How do I change my billing contact?", criteria) == []
    assert indicate("The invoice total shows an error", criteria) == []
    assert indicate("", criteria) == []


def test_a_ticket_saying_something_still_works_is_not_a_complete_outage():
    class Chunk:
        account_id = None
        text = "P1 - Critical: Complete outage preventing all shipment creation for a customer."
        citation = "policy.pdf p.1 §2"
        chunk_id = "policy#p1#c02"

    criteria = extract_definition_criteria([Chunk()])
    assert indicate("All shipment creation is failing for every user", criteria)
    assert not indicate(
        "Shipment creation fails from the bulk page; creating them one by one still works",
        criteria,
    )


def test_only_the_policys_own_text_defines_severity(conn):
    """A customer agreement states targets ("P1: 15 minutes"), not definitions."""
    decision = evaluate_sla(conn, "TKT-501", scope=LEGACY_SCOPE)
    assert {i.chunk_id.split("#")[0] for i in decision.severity_indications} == {
        "01_support_policy_v3_current"
    }


# --- 2. a BOOKED order is not proof the parcel is still there ---------------------------------


def test_an_open_ticket_saying_the_driver_has_been_blocks_cancellation(conn):
    decision = evaluate_cancellation(conn, "ORD-1001", scope=LEGACY_SCOPE)

    assert decision.outcome is PolicyOutcome.REQUIRES_VERIFICATION
    assert decision.can_cancel is False
    assert decision.requires_verification is True
    assert decision.inputs["conflicting_tickets"] == "TKT-504"
    reasons = " ".join(decision.verification_reasons)
    assert "TKT-504" in reasons and "already been collected" in reasons
    # The documented carrier delay is offered as the explanation, and cited.
    assert "20 minutes" in reasons
    assert any("KI-211" in source for source in decision.controlling_sources)


def test_the_contractual_fee_waiver_survives_the_conflict(conn):
    decision = evaluate_cancellation(conn, "ORD-1001", scope=LEGACY_SCOPE)

    assert decision.fee_applies is False
    assert str(decision.fee_amount) == "0.00"
    assert "waives the cancellation fee" in decision.controlling_rule
    assert decision.overrides  # the agreement still outranks the SOP here


def test_once_the_ticket_is_resolved_the_cancellation_is_allowed_again(conn):
    conn.execute("UPDATE tickets SET status = 'closed' WHERE ticket_id = 'TKT-504'")
    conn.commit()
    decision = evaluate_cancellation(conn, "ORD-1001", scope=LEGACY_SCOPE)

    assert decision.outcome is PolicyOutcome.ALLOWED and decision.can_cancel is True


def test_the_answer_names_the_conflict_and_does_not_authorise_cancelling(
    orchestrator, agent_context
):
    response = ask(orchestrator, "Can Northstar cancel ORD-1001 without a fee?", agent_context)

    assert "cannot be confirmed as cancellable yet" in response.answer
    assert "TKT-504" in " ".join(response.uncertainties)
    assert response.trust_status == "conditional"
    assert response.escalation_recommended is True
    assert response.pending_action is None
    # Still says which source waives the fee.
    assert "05_Northstar_Logistics_Enterprise_Agreement.pdf" in response.answer


@pytest.mark.parametrize(
    ("subject", "description", "expected"),
    [
        ("Driver has not arrived", "The driver has not collected our parcel", False),
        ("Where is my order", "Someone from BlueDart came and picked up the box", False),
        ("Picked up already", "SwiftShip driver collected the shipment at noon", True),
        ("Label question", "How do I print a label for SwiftShip?", False),
    ],
)
def test_only_a_ticket_saying_this_shipments_pickup_happened_counts(
    conn, subject, description, expected
):
    conn.execute(
        "UPDATE tickets SET subject = ?, description = ? WHERE ticket_id = 'TKT-504'",
        (subject, description),
    )
    conn.commit()
    order = get_order(conn, "ORD-1001", scope=LEGACY_SCOPE)

    assert bool(find_pickup_reports(conn, order, scope=LEGACY_SCOPE)) is expected


def test_a_ticket_naming_a_different_order_does_not_count(conn):
    conn.execute(
        "UPDATE tickets SET description = 'ORD-1002 was collected by the SwiftShip driver' "
        "WHERE ticket_id = 'TKT-504'"
    )
    conn.commit()
    order = get_order(conn, "ORD-1001", scope=LEGACY_SCOPE)
    assert find_pickup_reports(conn, order, scope=LEGACY_SCOPE) == []


def test_a_ticket_older_than_the_order_cannot_concern_it(conn):
    conn.execute("UPDATE tickets SET created_at = '2026-08-16T08:00:00+05:30' WHERE ticket_id = 'TKT-504'")
    conn.commit()
    order = get_order(conn, "ORD-1001", scope=LEGACY_SCOPE)
    assert find_pickup_reports(conn, order, scope=LEGACY_SCOPE) == []


def test_another_customers_ticket_is_never_evidence(conn):
    """LumenWorks' SwiftShip order must not be doubted because of Northstar's ticket."""
    decision = evaluate_cancellation(conn, "ORD-2001", scope=LEGACY_SCOPE)
    assert decision.outcome is PolicyOutcome.ALLOWED
    assert "conflicting_tickets" not in decision.inputs


def test_a_caller_cannot_see_a_ticket_outside_their_scope(conn):
    """Scoped to another account, the ticket is not readable, so it is not evidence."""
    from app.backend.tenancy import Scope

    order = get_order(conn, "ORD-1001", scope=LEGACY_SCOPE)
    narrowed = Scope.of(LEGACY_ORG_ID, {"ACCT-002"})
    assert find_pickup_reports(conn, order, scope=narrowed) == []


# --- 3. ticket-aware retrieval; resolved issues never govern -------------------------------------


def test_the_query_carries_the_tickets_subject_and_description_not_its_history(
    orchestrator, agent_context
):
    response = ask(orchestrator, "Investigate TKT-451", agent_context)
    query = next(
        t.arguments["query"] for t in response.tool_invocations if t.tool_name == "search_documents"
    )

    assert "Bulk upload fails for large CSV" in query
    assert "3,500-row" in query
    assert "Agent told customer" not in query  # the historical resolution stays out


def test_investigating_the_bulk_upload_ticket_surfaces_the_known_issue(
    orchestrator, agent_context
):
    response = ask(orchestrator, "Investigate TKT-502, what applies?", agent_context)

    assert any("KI-208" in (e.section_path or "") for e in response.evidence)
    assert "KI-208" in response.answer


def _search(orchestrator, context, query):
    result = orchestrator.registry.execute(
        orchestrator._conn, context, "search_documents", {"query": query}
    )
    assert result.status is ToolStatus.OK
    return result.data["governing"], result.data["contextual"]


@pytest.mark.parametrize(
    "query",
    [
        "Investigate TKT-501 and tell me what to do",
        "address validation known issue",
        "resolved issue KI-176",
    ],
)
def test_a_resolved_known_issue_never_governs(orchestrator, agent_context, query):
    governing, contextual = _search(orchestrator, agent_context, query)

    assert not [g for g in governing if "Resolved" in (g["section"] or "")]
    assert all(not g["governing"] for g in contextual if "Resolved" in (g["section"] or ""))


def test_a_resolved_issue_is_still_retrievable_as_context(orchestrator, agent_context):
    governing, contextual = _search(orchestrator, agent_context, "resolved issue KI-176 address validation")

    resolved = [c for c in contextual if "Resolved issue" in (c["section"] or "")]
    assert resolved, "the resolved issue is context, not gone"
    assert resolved[0]["is_authoritative"] is True  # in force, but not a rule
    assert resolved[0]["governing"] is False


def test_the_answer_does_not_present_a_resolved_issue_as_governing(orchestrator, agent_context):
    for message in (
        "Investigate TKT-501 and tell me what to do",
        "What should we do about TKT-505?",
    ):
        answer = ask(orchestrator, message, agent_context).answer
        assert "§3. Resolved issue" not in answer


def test_source_precedence_is_unchanged(orchestrator, agent_context):
    response = ask(orchestrator, "Can Northstar cancel ORD-1001 without a fee?", agent_context)
    assert response.governing_authority_tier == 1 and response.customer_agreement_applied
    v2 = ask(orchestrator, "What is the Enterprise P1 first response target?", agent_context)
    assert all("DEPRECATED" not in e.source_file for e in v2.evidence if e.is_authoritative)
    assert not any(
        e.is_authoritative and "02_Support_Policy_v2" in e.source_file for e in v2.evidence
    )


# --- 4. trust carries the caution; an escalation is grounded in findings ------------------------


def test_a_historical_resolution_lowers_trust_and_is_never_relied_on(
    orchestrator, agent_context
):
    response = ask(
        orchestrator,
        "TKT-451 says the Growth plan only supports 3000 rows. Is that right?",
        agent_context,
    )

    assert response.trust_status != "confident"
    assert any("historical resolution" in reason for reason in response.trust_reasons)


def test_an_escalation_is_grounded_in_the_findings(orchestrator, agent_context):
    response = ask(
        orchestrator, "Investigate TKT-501 and escalate it if the outage warrants it", agent_context
    )
    proposal = response.pending_action

    assert proposal is not None
    reason = proposal.parameters["reason"]
    assert "matches the current policy's P1 definition" in reason
    assert "15 minutes" in reason and "30.00 minutes have elapsed" in reason
    assert "severity is not yet verified" in reason
    # Not simply the operator's sentence.
    assert reason != response_message_reason("Investigate TKT-501 and escalate it if the outage warrants it")
    # Severity is recorded only when a person supplied it.
    assert "severity" not in proposal.parameters
    # The evidence is what the finding rested on: the policy definition and the
    # governing agreement, not whatever a search happened to return.
    assert any("01_support_policy_v3_current" in c for c in proposal.evidence_chunk_ids)
    assert any("05_northstar" in c for c in proposal.evidence_chunk_ids)
    assert response.trust_status in {"conditional", "escalate"}


def response_message_reason(message: str) -> str:
    return f"Escalation requested via support agent: {message}"


def test_a_caller_supplied_severity_is_recorded_on_the_escalation(orchestrator, agent_context):
    response = ask(orchestrator, "TKT-501 is a P1, escalate it", agent_context)
    proposal = response.pending_action

    assert proposal.parameters["severity"] == "P1"
    assert "breached" in proposal.parameters["reason"]


def test_an_off_topic_question_recommends_no_escalation(orchestrator, agent_context):
    response = ask(orchestrator, "What is the weather in Mumbai today?", agent_context)

    assert response.trust_status == "insufficient_data"
    assert response.escalation_recommended is False


def test_a_record_that_is_not_visible_is_not_an_escalation(orchestrator, northstar_context):
    response = ask(orchestrator, "What is TKT-502 about?", northstar_context)

    assert response.trust_status == "insufficient_data"
    assert response.escalation_recommended is False


def test_no_assessment_order_id_is_hardcoded_in_user_facing_prose(orchestrator, agent_context):
    response = ask(orchestrator, "Can this be cancelled without a fee?", agent_context)

    assert "for example ORD" not in response.answer
    assert "ORD-1001" not in response.answer


# --- 7. confirmation must say what was reviewed ---------------------------------------------------


def test_a_confirmation_without_a_fingerprint_is_refused_and_changes_nothing(
    orchestrator, agent_context, manager_context
):
    prepared = ask(orchestrator, "Escalate TKT-501.", agent_context).pending_action

    for missing in (None, "", "   "):
        with pytest.raises(ActionStateError, match="fingerprint"):
            orchestrator.confirm_action(
                prepared.action_id, manager_context, approve=True, expected_fingerprint=missing
            )
    with pytest.raises(ActionStateError, match="fingerprint"):
        orchestrator.confirm_action(prepared.action_id, manager_context, approve=False)

    still = orchestrator.pending_actions(manager_context)
    assert [a.action_id for a in still] == [prepared.action_id]


def test_a_wrong_fingerprint_is_refused_for_reject_too(
    orchestrator, agent_context, manager_context
):
    prepared = ask(orchestrator, "Escalate TKT-501.", agent_context).pending_action

    with pytest.raises(ActionStateError, match="no longer matches"):
        orchestrator.confirm_action(
            prepared.action_id, manager_context, approve=False, expected_fingerprint="0" * 64
        )
    assert orchestrator.pending_actions(manager_context)


def test_the_role_check_still_comes_first(orchestrator, agent_context, readonly_context):
    prepared = ask(orchestrator, "Escalate TKT-501.", agent_context).pending_action

    with pytest.raises(ActionForbidden):
        orchestrator.confirm_action(
            prepared.action_id,
            readonly_context,
            approve=True,
            expected_fingerprint=prepared.parameter_fingerprint(),
        )


def test_the_api_rejects_a_confirmation_that_omits_the_fingerprint(client):
    from tests.test_api import escalate, SUPPORT_MANAGER

    body = escalate(client)
    response = client.post(
        f"/api/actions/{body['proposed_action']['action_id']}/confirm",
        json={
            "decision": "approve",
            "user_id": SUPPORT_MANAGER,
            "session_id": body["session_id"],
        },
    )
    assert response.status_code == 422
    assert "expected_fingerprint" in response.text
    pending = client.get(f"/api/actions/pending?user_id={SUPPORT_MANAGER}").json()
    assert pending["count"] == 1
