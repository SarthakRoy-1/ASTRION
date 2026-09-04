"""Unit tests for the trust layer.

The evaluation suite checks trust statuses that the *supplied corpus* happens
to produce. This file checks the derivation itself, including the paths the
corpus does not currently exercise — most importantly the unresolved-conflict
path, which is a headline Phase 2 claim and would otherwise be asserted only as
an implication that never fires.

Everything here is constructed in-process from typed tool results. No database,
no documents, no network.
"""

from __future__ import annotations

import pytest

from app.backend.agent.provider import StepRecord
from app.backend.agent.trust import (
    TrustStatus,
    assess,
    worst,
)
from app.backend.models.agent import ToolResult, ToolStatus


def step(tool_name: str = "search_documents", **kwargs) -> StepRecord:
    """One completed tool call, with whatever result the case needs."""
    return StepRecord(tool_name, {}, ToolResult(**kwargs))


# --- worst-wins ordering ----------------------------------------------------


def test_an_empty_investigation_is_confident():
    assert worst([]) is TrustStatus.CONFIDENT


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ([TrustStatus.CONFIDENT], TrustStatus.CONFIDENT),
        ([TrustStatus.CONFIDENT, TrustStatus.CONDITIONAL], TrustStatus.CONDITIONAL),
        (
            [TrustStatus.CONFIDENT, TrustStatus.INSUFFICIENT_DATA],
            TrustStatus.INSUFFICIENT_DATA,
        ),
        (
            [TrustStatus.CONDITIONAL, TrustStatus.INSUFFICIENT_DATA],
            TrustStatus.INSUFFICIENT_DATA,
        ),
        ([TrustStatus.INSUFFICIENT_DATA, TrustStatus.CONFLICT], TrustStatus.CONFLICT),
        ([TrustStatus.CONFLICT, TrustStatus.ESCALATE], TrustStatus.ESCALATE),
        (
            [TrustStatus.ESCALATE, TrustStatus.CONFIDENT, TrustStatus.CONDITIONAL],
            TrustStatus.ESCALATE,
        ),
    ],
)
def test_the_least_trustworthy_status_wins(statuses, expected):
    """A reader acting on the confident half of a mixed answer is acting on an
    incomplete one, so the worst status is the honest summary."""
    assert worst(statuses) is expected


# --- the conflict path ------------------------------------------------------


def test_an_unresolved_conflict_becomes_an_escalation():
    """The path the corpus does not currently produce, exercised directly.

    When the authority layer reports that precedence *cannot* separate two
    equal-authority sources, no further computation helps — only a person. So
    CONFLICT is promoted to ESCALATE and carries the conflict as its reason.
    """
    conflict = (
        "2 different documents share authority tier 2 (CURRENT_SUPPORT_POLICY) "
        "on topic 'cancellation'; precedence cannot resolve this — escalate"
    )
    assessment = assess([step(data={"conflicts": [conflict]})])

    assert assessment.status is TrustStatus.ESCALATE
    assert conflict in assessment.authority.conflicts
    assert assessment.escalation_reason == conflict
    assert conflict in assessment.reasons
    assert not assessment.is_actionable


def test_a_conflict_is_never_silently_dropped():
    """Even alongside a settled decision, the conflict decides the status."""
    assessment = assess(
        [
            step(data={"governing": [{"authority_tier": 1, "chunk_id": "c1"}]}),
            step(data={"conflicts": ["two policies disagree"]}),
        ]
    )
    assert assessment.status is TrustStatus.ESCALATE
    assert assessment.authority.has_conflict


# --- tool outcomes ----------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (ToolStatus.NOT_FOUND, TrustStatus.INSUFFICIENT_DATA),
        (ToolStatus.FORBIDDEN, TrustStatus.INSUFFICIENT_DATA),
        (ToolStatus.INVALID_INPUT, TrustStatus.INSUFFICIENT_DATA),
        (ToolStatus.UNCERTAIN, TrustStatus.CONDITIONAL),
        (ToolStatus.ERROR, TrustStatus.ESCALATE),
    ],
)
def test_each_tool_failure_maps_to_a_status(status, expected):
    assessment = assess([step(status=status, message="something went wrong")])
    assert assessment.status is expected


def test_not_found_and_forbidden_are_indistinguishable():
    """The record layer refuses to separate them; so does this.

    Distinguishing them here would leak the difference the tool layer works to
    hide — that a record exists but belongs to someone else.
    """
    missing = assess([step(status=ToolStatus.NOT_FOUND, message="x was not found")])
    refused = assess([step(status=ToolStatus.FORBIDDEN, message="x was not found")])
    assert missing.status is refused.status
    assert missing.reasons == refused.reasons


def test_an_ok_result_contributes_nothing():
    assert assess([step(status=ToolStatus.OK)]).status is TrustStatus.CONFIDENT


# --- prerequisites and budget ----------------------------------------------


def test_a_missing_prerequisite_is_insufficient_data():
    assessment = assess(
        [step()], unmet_requirements=["no order identified, so X was not evaluated"]
    )
    assert assessment.status is TrustStatus.INSUFFICIENT_DATA
    assert "no order identified, so X was not evaluated" in assessment.reasons


def test_a_truncated_investigation_is_never_confident():
    """The loop stopped with work queued, so the evidence set is incomplete by
    construction — whatever it happens to contain."""
    assessment = assess([step()], step_budget_exhausted=True)
    assert assessment.status is TrustStatus.INSUFFICIENT_DATA
    assert any("step limit" in reason for reason in assessment.reasons)


# --- authority summary ------------------------------------------------------


def test_a_customer_agreement_is_reported_as_having_applied():
    assessment = assess(
        [step(data={"governing": [{"authority_tier": 1, "chunk_id": "agreement#1"}]})]
    )
    assert assessment.authority.customer_agreement_applied is True
    assert int(assessment.authority.governing_tier) == 1
    assert "agreement#1" in assessment.authority.governing_chunk_ids


def test_a_general_policy_is_not_reported_as_an_agreement():
    assessment = assess(
        [step(data={"governing": [{"authority_tier": 2, "chunk_id": "policy#1"}]})]
    )
    assert assessment.authority.customer_agreement_applied is False
    assert int(assessment.authority.governing_tier) == 2


def test_the_strongest_governing_tier_is_reported():
    """Several topics can govern at once; the strongest is the headline."""
    assessment = assess(
        [
            step(
                data={
                    "governing": [
                        {"authority_tier": 3, "chunk_id": "sop#1"},
                        {"authority_tier": 1, "chunk_id": "agreement#1"},
                    ]
                }
            )
        ]
    )
    assert int(assessment.authority.governing_tier) == 1
    assert assessment.authority.customer_agreement_applied is True


def test_retrieved_non_authoritative_material_is_flagged_but_does_not_govern():
    """Deprecated material stays retrievable — quoting a superseded rule is how
    you explain that a rule changed — and must never be reported as governing."""
    assessment = assess(
        [
            step(
                data={
                    "governing": [{"authority_tier": 2, "chunk_id": "current#1"}],
                    "contextual": [
                        {"authority_tier": 4, "chunk_id": "old#1", "is_authoritative": False}
                    ],
                }
            )
        ]
    )
    assert assessment.authority.non_authoritative_seen is True
    assert int(assessment.authority.governing_tier) == 2


def test_override_notes_are_carried_through():
    note = "agreement (tier 1) outranks SOP (tier 3) on topic 'cancellation'"
    assessment = assess([step(data={"overrides": [note]})])
    assert note in assessment.authority.overrides


def test_a_malformed_authority_tier_is_ignored_rather_than_crashing():
    """Defensive: a tool payload this layer cannot parse must not take down the
    request. Trust degrades to 'nothing governed', which is the safe direction."""
    assessment = assess([step(data={"governing": [{"authority_tier": "nonsense"}]})])
    assert assessment.authority.governing_tier is None
    assert assessment.status is TrustStatus.CONFIDENT


# --- actionability ----------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "actionable"),
    [
        (TrustStatus.CONFIDENT, True),
        (TrustStatus.CONDITIONAL, True),
        (TrustStatus.INSUFFICIENT_DATA, False),
        (TrustStatus.CONFLICT, False),
        (TrustStatus.ESCALATE, False),
    ],
)
def test_actionability(status, actionable):
    """Which statuses may safely have an action *offered* against them.

    CONDITIONAL is actionable because the premise is stated and the human
    confirming can check it. INSUFFICIENT_DATA and ESCALATE are not: confirming
    on evidence the system has just said it cannot reconcile would make the
    confirmation gate a rubber stamp on a guess.
    """
    from app.backend.agent.trust import AuthoritySummary, TrustAssessment

    assessment = TrustAssessment(status=status, authority=AuthoritySummary())
    assert assessment.is_actionable is actionable


# --- determinism ------------------------------------------------------------


def test_assessment_is_deterministic():
    history = [
        step(data={"governing": [{"authority_tier": 1, "chunk_id": "a"}]}),
        step(status=ToolStatus.NOT_FOUND, message="missing"),
    ]
    first, second = assess(history), assess(history)
    assert first.status is second.status
    assert first.reasons == second.reasons
    assert first.authority == second.authority


def test_reasons_are_deduplicated_but_ordered():
    """The same failure reported by two tools should be stated once."""
    history = [
        step(status=ToolStatus.NOT_FOUND, message="ORD-1 was not found"),
        step(status=ToolStatus.NOT_FOUND, message="ORD-1 was not found"),
        step(status=ToolStatus.NOT_FOUND, message="ORD-2 was not found"),
    ]
    reasons = assess(history).reasons
    assert reasons == ("ORD-1 was not found", "ORD-2 was not found")
