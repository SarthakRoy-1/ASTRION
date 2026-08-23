"""Phase 7: deterministic first-response SLA evaluation.

Covers the four behaviours the assessment names for SLA reasoning — the
plan-specific target, a customer agreement overriding it, breach detection, and
P1 escalation — plus the two refusals that keep those answers honest: no
severity is invented, and no business-hours target is converted into a deadline
the corpus does not define.

No test asserts a duration that is written in this file rather than read from
the corpus; the expected values below are the ones the supplied documents
state.
"""

from decimal import Decimal

import pytest

from app.backend.models.policy import PolicyOutcome, Severity
from app.backend.models.agent import ToolStatus
from app.backend.policies.base import PolicyLookupError
from app.backend.policies.sla import evaluate_sla
from app.backend.policies.terms import extract_response_targets, severity_stated_in
from app.backend.tools.registry import build_default_registry
from conftest import NORTHSTAR_ACCOUNT


@pytest.fixture
def registry():
    return build_default_registry()


# --- target selection ---------------------------------------------------------


def test_plan_default_target_is_read_from_the_policy_table(conn):
    """ACCT-004 is an Enterprise account with no agreement in the pack, so the
    current policy's Enterprise row governs."""
    decision = evaluate_sla(conn, "TKT-505", severity="P1")

    assert decision.plan == "Enterprise"
    assert decision.target_minutes == 30
    assert "30 minutes" in decision.target_text
    assert any("Support_Policy_v3" in s for s in decision.controlling_sources)


def test_customer_agreement_overrides_the_plan_default(conn):
    """Northstar's agreement replaces the Enterprise P1 target. The agreement
    must both win and be named as having won."""
    decision = evaluate_sla(conn, "TKT-501", severity="P1")

    assert decision.plan == "Enterprise"
    assert decision.target_minutes == 15
    assert any("Northstar" in s for s in decision.controlling_sources)
    assert any("outranks" in note for note in decision.overrides)


def test_agreement_override_is_per_severity_not_wholesale(conn):
    """An agreement overrides the severities it states. Northstar states all
    three, so each comes from the agreement rather than the plan table."""
    targets = {
        severity: evaluate_sla(conn, "TKT-501", severity=severity).target_minutes
        for severity in ("P1", "P2")
    }

    assert targets["P1"] == 15
    assert targets["P2"] == 60


def test_another_accounts_agreement_never_supplies_a_target(conn):
    """ACCT-004 has no agreement; the Enterprise default must not be replaced
    by whichever agreement happens to sit in the corpus."""
    northstar = evaluate_sla(conn, "TKT-501", severity="P1")
    axis = evaluate_sla(conn, "TKT-505", severity="P1")

    assert northstar.target_minutes == 15
    assert axis.target_minutes == 30
    assert not any("Northstar" in s for s in axis.controlling_sources)


# --- breach detection ----------------------------------------------------------


def test_breach_is_measured_against_the_dataset_snapshot(conn):
    decision = evaluate_sla(conn, "TKT-501", severity="P1")

    assert decision.breached is True
    assert decision.elapsed_minutes == Decimal("30.00")
    assert decision.outcome is PolicyOutcome.NOT_ALLOWED
    assert "breached" in decision.calculation
    assert decision.inputs["reference_time_source"].startswith("dataset snapshot")


def test_a_target_still_within_reach_is_not_reported_as_breached(conn):
    """The engine must be able to say "no breach", or a breach carries no
    information. TKT-501 is 30 minutes old against a 1-hour P2 target."""
    decision = evaluate_sla(conn, "TKT-501", severity="P2")

    assert decision.breached is False
    assert decision.outcome is PolicyOutcome.ALLOWED
    assert "within target" in decision.calculation


# --- P1 escalation --------------------------------------------------------------


def test_p1_requires_immediate_escalation_independently_of_the_clock(conn):
    """The policy escalates P1 immediately; it does not wait for a breach."""
    breached = evaluate_sla(conn, "TKT-501", severity="P1")
    within = evaluate_sla(conn, "TKT-503", severity="P1")

    assert breached.requires_immediate_escalation is True
    assert within.requires_immediate_escalation is True


def test_lower_severities_do_not_demand_immediate_escalation(conn):
    decision = evaluate_sla(conn, "TKT-501", severity="P2")

    assert decision.requires_immediate_escalation is False


# --- the two refusals -----------------------------------------------------------


def test_no_severity_supplied_defers_instead_of_guessing(conn):
    """Severity is a judgement about impact. Inferring one and then asserting a
    breach against it would be a confident answer built on a guess."""
    decision = evaluate_sla(conn, "TKT-501")

    assert decision.severity is None
    assert decision.breached is None
    assert decision.outcome is PolicyOutcome.REQUIRES_VERIFICATION
    assert decision.verification_reasons


def test_deferring_on_severity_still_reports_the_facts_it_has(conn):
    """A refusal should still be useful: elapsed time and the candidate targets
    are facts that do not depend on the severity."""
    decision = evaluate_sla(conn, "TKT-501")

    assert decision.elapsed_minutes == Decimal("30.00")
    assert "15 minutes" in " ".join(decision.verification_reasons)


def test_business_hours_target_is_reported_but_not_converted(conn):
    """The corpus defines no business calendar, so a business-hours target
    cannot yield a breach verdict without inventing one."""
    decision = evaluate_sla(conn, "TKT-503", severity="P3")

    assert decision.target_text == "2 business days"
    assert decision.target_minutes is None
    assert decision.breached is None
    assert decision.outcome is PolicyOutcome.REQUIRES_VERIFICATION
    assert any("business calendar" in r for r in decision.verification_reasons)


def test_an_undefined_severity_is_refused(conn):
    with pytest.raises(PolicyLookupError):
        evaluate_sla(conn, "TKT-501", severity="P9")


# --- scope ----------------------------------------------------------------------


def test_sla_evaluation_is_account_scoped(conn):
    """Out of scope is indistinguishable from absent, as everywhere else."""
    with pytest.raises(PolicyLookupError):
        evaluate_sla(conn, "TKT-502", severity="P1", allowed_account_ids=NORTHSTAR_ACCOUNT)


# --- extraction primitives -------------------------------------------------------


def test_severity_stated_in_reads_a_label_it_does_not_judge_one():
    assert severity_stated_in("treat TKT-501 as a P1 outage") is Severity.P1
    assert severity_stated_in("no severity here") is None
    # Two severities named is an ambiguity to surface, not to resolve.
    assert severity_stated_in("is this a P1 or a P2?") is None


def test_targets_extracted_without_a_plan_still_read_agreement_clauses(conn):
    """The plan table needs a plan to select a row; an agreement's inline
    targets do not, so they must still be recovered."""
    from app.backend.models.documents import Topic
    from app.backend.policies.base import gather_policy_evidence

    evidence, _ = gather_policy_evidence(
        conn,
        topic=Topic.SUPPORT_RESPONSE,
        account_id="ACCT-001",
        allowed_account_ids=None,
    )
    targets = extract_response_targets(evidence, plan=None)

    assert targets.for_severity(Severity.P1).minutes == 15
    assert targets.escalate_p1_immediately is True


def test_every_extracted_target_names_the_chunk_that_stated_it(conn):
    """A target with no source was never stated by any document."""
    decision = evaluate_sla(conn, "TKT-501", severity="P1")

    assert decision.targets is not None
    for target in decision.targets.targets.values():
        assert target.source is not None
        assert target.source.chunk_id


# --- tool and orchestration wiring ------------------------------------------------


def test_sla_tool_is_registered_and_scoped(conn, registry, northstar_context):
    result = registry.execute(
        conn, northstar_context, "evaluate_sla", {"ticket_id": "TKT-502"}
    )

    assert result.status is ToolStatus.NOT_FOUND
    assert result.decisions == []


def test_sla_tool_passes_severity_through(conn, registry, agent_context):
    result = registry.execute(
        conn, agent_context, "evaluate_sla", {"ticket_id": "TKT-501", "severity": "P1"}
    )

    assert result.status is ToolStatus.OK
    assert result.decisions[0].breached is True


def test_sla_tool_without_severity_reports_uncertainty(conn, registry, agent_context):
    result = registry.execute(conn, agent_context, "evaluate_sla", {"ticket_id": "TKT-501"})

    assert result.status is ToolStatus.UNCERTAIN
    assert result.message


def test_a_breached_target_recommends_escalation(orchestrator, agent_context):
    """The policy directs that a breach be stated and escalated, not reported
    quietly — a settled decision, so no uncertainty flag would catch it."""
    from app.backend.models.agent import AgentRequest

    response = orchestrator.handle(
        AgentRequest(
            message="Has the first response SLA been breached on TKT-501? It is a P1.",
            context=agent_context,
        )
    )

    decision = next(d for d in response.decisions if d.decision_type == "sla")
    assert decision.breached is True
    assert response.escalation_recommended is True
    assert "BREACHED" in response.answer
