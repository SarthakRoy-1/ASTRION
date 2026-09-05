"""Phase 5: the accountable service-credit action.

Two properties this file exists for, and they pull in opposite directions —
which is the point:

- **The amount is never the caller's to choose.** `prepare_service_credit`
  reads it from `policies/service_credit.py` and refuses an argument that
  tries to supply one. A credit is a payment, and the only thing in this
  system permitted to decide what a customer is owed is the policy engine.
- **Authority is decided at confirmation, under the confirming caller.**
  The SOP's "any individual credit above INR 1,000 requires manager approval"
  is re-derived at confirm time from the live decision, so a role that changed
  after the proposal was written is respected and a stale flag cannot buy a
  large credit a cheap approval.

Where a credit above the threshold is needed, the terms are raised and the
**real engine** computes `requires_manager_approval` from them — the same
technique `test_policy_engine.py` already uses. Nothing here asserts a
hard-coded answer; the supplied pack's only eligible order is ORD-2002 at
INR 300, and that is used as-is wherever the threshold is not the subject.
"""

from datetime import timedelta
from decimal import Decimal

import pytest

from app.backend.models.actions import ActionStatus, ActionType
from app.backend.models.agent import AgentContext, Role, ToolStatus
from app.backend.services.actions import (
    ActionForbidden,
    ActionStateError,
    execute_action,
    get_action,
    get_action_audit,
    get_order_service_credits,
    prepare_action,
    reject_action,
)
from app.backend.tools.action_tools import _prepare_service_credit
from conftest import LUMENWORKS_ACCOUNT

#: The one order the supplied pack makes eligible, and the account it belongs
#: to. Read from the data rather than asserted into existence.
ELIGIBLE_ORDER = "ORD-2002"


@pytest.fixture
def lumenworks_context():
    """Scoped to the account the eligible order belongs to."""
    return AgentContext(
        user_id="lw.agent",
        role=Role.SUPPORT_AGENT,
        allowed_account_ids=frozenset({LUMENWORKS_ACCOUNT}),
    )


def raise_credit_above_threshold(monkeypatch, amount="2500"):
    """Make the governing terms produce a credit over the SOP threshold.

    The engine still decides: it reads the raised `fixed_amount`, applies the
    same threshold comparison it always does, and sets
    `requires_manager_approval` itself. Only the *input* is substituted, which
    is what makes this a test of the rule rather than of a constant.
    """
    from app.backend.policies import service_credit as module

    real = module.extract_service_credit_terms

    def bigger(evidence):
        return real(evidence).model_copy(update={"fixed_amount": Decimal(amount)})

    monkeypatch.setattr(module, "extract_service_credit_terms", bigger)


def prepare_credit(conn, context, order_id=ELIGIBLE_ORDER, **arguments):
    return _prepare_service_credit(conn, context, {"order_id": order_id, **arguments})


# --- the amount comes from the policy engine, never the caller --------------


def test_an_eligible_credit_can_be_prepared(conn, lumenworks_context):
    result = prepare_credit(conn, lumenworks_context)

    assert result.status is ToolStatus.OK
    assert result.proposed_action is not None
    assert result.proposed_action.status is ActionStatus.PENDING_CONFIRMATION
    assert result.proposed_action.action_type is ActionType.ISSUE_SERVICE_CREDIT


def test_the_amount_is_the_policy_engine_s_own_figure(conn, lumenworks_context):
    from app.backend.policies.service_credit import evaluate_service_credit

    decision = evaluate_service_credit(conn, ELIGIBLE_ORDER)
    result = prepare_credit(conn, lumenworks_context)

    assert result.proposed_action.parameters["amount"] == str(decision.credit_amount)
    assert result.proposed_action.parameters["currency"] == decision.currency


def test_a_caller_supplied_amount_is_refused_rather_than_ignored(
    conn, lumenworks_context
):
    # Silently dropping it would let a caller believe they had set one.
    for argument in ("amount", "credit_amount", "currency"):
        result = prepare_credit(conn, lumenworks_context, **{argument: "999999"})
        assert result.status is ToolStatus.INVALID_INPUT
        assert argument in result.message
        assert result.proposed_action is None


def test_the_customer_agreement_amount_overrides_the_sop_default(
    conn, lumenworks_context
):
    """LumenWorks' agreement replaces the SOP's default credit, not adds to it."""
    from app.backend.policies.service_credit import evaluate_service_credit

    decision = evaluate_service_credit(conn, ELIGIBLE_ORDER)
    result = prepare_credit(conn, lumenworks_context)

    # Whatever the agreement says is what gets proposed, and the proposal
    # records which rule produced it.
    assert result.proposed_action.parameters["amount"] == str(decision.credit_amount)
    assert result.proposed_action.reason == decision.controlling_rule
    assert decision.overrides, "the agreement should be recorded as overriding"


def test_preparation_carries_the_decision_s_own_evidence(conn, lumenworks_context):
    result = prepare_credit(conn, lumenworks_context)
    assert result.proposed_action.evidence_chunk_ids


def test_an_ineligible_order_produces_no_executable_action(conn, lumenworks_context):
    result = prepare_credit(conn, lumenworks_context, order_id="ORD-2001")

    assert result.status is ToolStatus.NO_EVIDENCE
    assert result.proposed_action is None


def test_an_order_outside_scope_is_not_found(conn, lumenworks_context):
    result = prepare_credit(conn, lumenworks_context, order_id="ORD-1001")
    assert result.status is ToolStatus.NOT_FOUND
    assert result.proposed_action is None


def test_missing_facts_produce_uncertainty_not_a_credit(
    conn, lumenworks_context, monkeypatch
):
    """An unsettled decision must not become an invented payment."""
    from app.backend.models.policy import ServiceCreditTerms
    from app.backend.policies import service_credit as module

    monkeypatch.setattr(
        module,
        "extract_service_credit_terms",
        lambda evidence: ServiceCreditTerms(),
    )

    result = prepare_credit(conn, lumenworks_context)
    assert result.status is ToolStatus.UNCERTAIN
    assert result.proposed_action is None


def test_preparation_writes_no_credit(conn, lumenworks_context):
    prepare_credit(conn, lumenworks_context)
    assert get_order_service_credits(conn, ELIGIBLE_ORDER) == []


# --- manager authority, decided at confirmation -----------------------------


def confirm(orchestrator, action_id, context, **kwargs):
    return orchestrator.confirm_action(action_id, context, **kwargs)


def test_a_credit_within_the_threshold_follows_the_normal_path(
    conn, orchestrator, lumenworks_context
):
    prepared = prepare_credit(conn, lumenworks_context).proposed_action
    assert prepared.parameters["requires_manager_approval"] == "false"

    executed = confirm(orchestrator, prepared.action_id, lumenworks_context)

    assert executed.status is ActionStatus.EXECUTED
    credits = get_order_service_credits(conn, ELIGIBLE_ORDER)
    assert len(credits) == 1
    assert credits[0]["amount"] == prepared.parameters["amount"]
    assert credits[0]["required_manager_approval"] == 0


def test_a_credit_over_the_threshold_is_refused_to_a_non_manager(
    conn, orchestrator, lumenworks_context, monkeypatch
):
    raise_credit_above_threshold(monkeypatch)
    prepared = prepare_credit(conn, lumenworks_context).proposed_action
    assert prepared.parameters["requires_manager_approval"] == "true"

    with pytest.raises(ActionForbidden):
        confirm(orchestrator, prepared.action_id, lumenworks_context)


def test_a_refused_confirmation_changes_nothing(
    conn, orchestrator, lumenworks_context, monkeypatch
):
    raise_credit_above_threshold(monkeypatch)
    prepared = prepare_credit(conn, lumenworks_context).proposed_action

    with pytest.raises(ActionForbidden):
        confirm(orchestrator, prepared.action_id, lumenworks_context)

    assert get_order_service_credits(conn, ELIGIBLE_ORDER) == []
    # And it stays confirmable by someone who *is* authorised, rather than
    # being burned by the failed attempt.
    assert get_action(conn, prepared.action_id).status is (
        ActionStatus.PENDING_CONFIRMATION
    )


def test_a_credit_over_the_threshold_is_confirmed_by_a_manager(
    conn, orchestrator, lumenworks_context, monkeypatch
):
    raise_credit_above_threshold(monkeypatch)
    prepared = prepare_credit(conn, lumenworks_context).proposed_action

    manager = AgentContext(
        user_id="lw.manager",
        role=Role.SUPPORT_MANAGER,
        allowed_account_ids=frozenset({LUMENWORKS_ACCOUNT}),
    )
    executed = confirm(orchestrator, prepared.action_id, manager)

    assert executed.status is ActionStatus.EXECUTED
    assert executed.confirmed_by == "lw.manager"
    credits = get_order_service_credits(conn, ELIGIBLE_ORDER)
    assert credits[0]["required_manager_approval"] == 1
    assert credits[0]["approved_by"] == "lw.manager"


def test_authority_is_read_from_the_confirming_caller_not_the_preparer(
    conn, orchestrator, monkeypatch
):
    """A manager preparing it does not make it approved."""
    raise_credit_above_threshold(monkeypatch)
    manager = AgentContext(
        user_id="lw.manager",
        role=Role.SUPPORT_MANAGER,
        allowed_account_ids=frozenset({LUMENWORKS_ACCOUNT}),
    )
    prepared = prepare_credit(conn, manager).proposed_action

    agent = AgentContext(
        user_id="lw.agent",
        role=Role.SUPPORT_AGENT,
        allowed_account_ids=frozenset({LUMENWORKS_ACCOUNT}),
    )
    with pytest.raises(ActionForbidden):
        confirm(orchestrator, prepared.action_id, agent)


def test_authority_gained_after_preparation_is_respected(
    conn, orchestrator, lumenworks_context, monkeypatch
):
    """The stored row has no say in who may approve it."""
    raise_credit_above_threshold(monkeypatch)
    prepared = prepare_credit(conn, lumenworks_context).proposed_action

    promoted = lumenworks_context.model_copy(
        update={"role": Role.SUPPORT_MANAGER}
    )
    executed = confirm(orchestrator, prepared.action_id, promoted)
    assert executed.status is ActionStatus.EXECUTED


def test_permissions_decide_authority_when_a_workspace_issued_them(
    conn, orchestrator, monkeypatch
):
    """Under real authentication the permission set is the authority.

    `approve_high_value_action` is granted from ADMIN up; OPERATIONS may
    execute routine actions but not confirm one over the threshold.
    """
    raise_credit_above_threshold(monkeypatch)
    operations = AgentContext(
        user_id="ops.user",
        role=Role.SUPPORT_AGENT,
        allowed_account_ids=frozenset({LUMENWORKS_ACCOUNT}),
        permissions=frozenset({"execute_action", "propose_action", "run_agent"}),
    )
    prepared = prepare_credit(conn, operations).proposed_action

    with pytest.raises(ActionForbidden):
        confirm(orchestrator, prepared.action_id, operations)

    admin = operations.model_copy(
        update={
            "user_id": "admin.user",
            "permissions": frozenset(
                {"execute_action", "propose_action", "run_agent",
                 "approve_high_value_action"}
            ),
        }
    )
    assert confirm(orchestrator, prepared.action_id, admin).status is (
        ActionStatus.EXECUTED
    )


def test_a_stale_approval_flag_cannot_buy_a_cheap_confirmation(
    conn, orchestrator, lumenworks_context, monkeypatch
):
    """The stored flag is what the reviewer saw; it is not the authority.

    A proposal written while the credit was small must not execute unchanged
    once the governing terms make it large.
    """
    prepared = prepare_credit(conn, lumenworks_context).proposed_action
    assert prepared.parameters["requires_manager_approval"] == "false"

    raise_credit_above_threshold(monkeypatch)

    with pytest.raises(ActionStateError):
        confirm(orchestrator, prepared.action_id, lumenworks_context)
    assert get_order_service_credits(conn, ELIGIBLE_ORDER) == []


def test_a_credit_that_stopped_qualifying_cannot_be_confirmed(
    conn, orchestrator, lumenworks_context, monkeypatch
):
    prepared = prepare_credit(conn, lumenworks_context).proposed_action

    from app.backend.models.policy import ServiceCreditTerms
    from app.backend.policies import service_credit as module

    monkeypatch.setattr(
        module, "extract_service_credit_terms", lambda evidence: ServiceCreditTerms()
    )

    with pytest.raises(ActionStateError):
        confirm(orchestrator, prepared.action_id, lumenworks_context)
    assert get_order_service_credits(conn, ELIGIBLE_ORDER) == []


def test_a_policy_that_cannot_be_re_evaluated_fails_closed(
    conn, orchestrator, lumenworks_context, monkeypatch
):
    prepared = prepare_credit(conn, lumenworks_context).proposed_action

    from app.backend.policies.base import PolicyDataError
    from app.backend.agent import orchestrator as module

    def boom(*args, **kwargs):
        raise PolicyDataError("terms unavailable")

    monkeypatch.setattr(module, "evaluate_service_credit", boom)

    with pytest.raises(ActionStateError):
        confirm(orchestrator, prepared.action_id, lumenworks_context)
    assert get_order_service_credits(conn, ELIGIBLE_ORDER) == []


# --- the confirmation gate, unchanged for the new type ----------------------


def test_a_prepared_credit_does_not_execute_on_its_own(conn, lumenworks_context):
    prepared = prepare_credit(conn, lumenworks_context).proposed_action
    assert get_action(conn, prepared.action_id).status is (
        ActionStatus.PENDING_CONFIRMATION
    )
    assert get_order_service_credits(conn, ELIGIBLE_ORDER) == []


def test_execution_is_single_use(conn, orchestrator, lumenworks_context):
    prepared = prepare_credit(conn, lumenworks_context).proposed_action
    confirm(orchestrator, prepared.action_id, lumenworks_context)

    with pytest.raises(ActionStateError):
        confirm(orchestrator, prepared.action_id, lumenworks_context)
    assert len(get_order_service_credits(conn, ELIGIBLE_ORDER)) == 1


def test_an_expired_credit_cannot_be_confirmed(conn, orchestrator, lumenworks_context):
    prepared = prepare_credit(conn, lumenworks_context).proposed_action
    conn.execute(
        "UPDATE agent_actions SET expires_at_utc = ? WHERE action_id = ?",
        (
            (prepared.prepared_at_utc - timedelta(minutes=1)).isoformat(),
            prepared.action_id,
        ),
    )
    conn.commit()

    with pytest.raises(ActionStateError):
        confirm(orchestrator, prepared.action_id, lumenworks_context)
    assert get_order_service_credits(conn, ELIGIBLE_ORDER) == []


def test_a_fingerprint_mismatch_refuses_confirmation(
    conn, orchestrator, lumenworks_context
):
    prepared = prepare_credit(conn, lumenworks_context).proposed_action

    with pytest.raises(ActionStateError):
        confirm(
            orchestrator,
            prepared.action_id,
            lumenworks_context,
            expected_fingerprint="not-the-reviewed-proposal",
        )
    assert get_order_service_credits(conn, ELIGIBLE_ORDER) == []


def test_the_reviewed_fingerprint_confirms(conn, orchestrator, lumenworks_context):
    prepared = prepare_credit(conn, lumenworks_context).proposed_action
    executed = confirm(
        orchestrator,
        prepared.action_id,
        lumenworks_context,
        expected_fingerprint=prepared.parameter_fingerprint(),
    )
    assert executed.status is ActionStatus.EXECUTED


def test_a_rejected_credit_cannot_execute(conn, orchestrator, lumenworks_context):
    prepared = prepare_credit(conn, lumenworks_context).proposed_action
    reject_action(conn, prepared.action_id, rejected_by="lw.agent")

    with pytest.raises(ActionStateError):
        confirm(orchestrator, prepared.action_id, lumenworks_context)
    assert get_order_service_credits(conn, ELIGIBLE_ORDER) == []


def test_rejection_remains_possible_and_writes_nothing(
    conn, orchestrator, lumenworks_context
):
    prepared = prepare_credit(conn, lumenworks_context).proposed_action
    rejected = confirm(
        orchestrator, prepared.action_id, lumenworks_context, approve=False
    )

    assert rejected.status is ActionStatus.REJECTED
    assert get_order_service_credits(conn, ELIGIBLE_ORDER) == []


def test_a_read_only_caller_cannot_confirm(conn, orchestrator, lumenworks_context):
    prepared = prepare_credit(conn, lumenworks_context).proposed_action
    viewer = lumenworks_context.model_copy(update={"role": Role.READ_ONLY})

    with pytest.raises(ActionForbidden):
        confirm(orchestrator, prepared.action_id, viewer)
    assert get_order_service_credits(conn, ELIGIBLE_ORDER) == []


def test_a_credit_is_invisible_outside_its_account_scope(
    conn, orchestrator, lumenworks_context
):
    prepared = prepare_credit(conn, lumenworks_context).proposed_action
    outsider = AgentContext(
        user_id="ns.agent",
        role=Role.SUPPORT_AGENT,
        allowed_account_ids=frozenset({"ACCT-001"}),
    )

    from app.backend.services.actions import ActionNotFound

    with pytest.raises(ActionNotFound):
        confirm(orchestrator, prepared.action_id, outsider)
    assert get_order_service_credits(conn, ELIGIBLE_ORDER) == []


def test_the_executed_credit_records_its_full_lifecycle(
    conn, orchestrator, lumenworks_context
):
    prepared = prepare_credit(conn, lumenworks_context).proposed_action
    confirm(orchestrator, prepared.action_id, lumenworks_context)

    audit = get_action_audit(conn, prepared.action_id)
    assert audit.status is ActionStatus.EXECUTED
    assert audit.requested_by == "lw.agent"
    assert audit.confirmed_by == "lw.agent"
    assert audit.executed_at_utc is not None
    assert audit.result["credit_id"].startswith("CRD-")


# --- the model can never reach execution ------------------------------------


def test_no_registered_tool_can_issue_a_credit():
    """Preparation is exposed; execution is not, for this type as for the others."""
    from app.backend.tools.registry import build_default_registry

    registry = build_default_registry()
    names = set(registry.names())

    assert "prepare_service_credit" in names
    for forbidden in ("issue_service_credit", "confirm_action", "execute_action"):
        assert forbidden not in names


def test_preparing_requires_no_manager_authority(conn):
    """Proposing is not approving: a plain support agent may draft a large one."""
    agent = AgentContext(
        user_id="lw.agent",
        role=Role.SUPPORT_AGENT,
        allowed_account_ids=frozenset({LUMENWORKS_ACCOUNT}),
    )
    assert agent.may_approve_high_value is False
    assert prepare_credit(conn, agent).status is ToolStatus.OK


def test_a_hand_written_action_row_still_meets_the_gate(
    conn, orchestrator, lumenworks_context, monkeypatch
):
    """Bypassing the tool does not bypass the authorization.

    The gate re-derives the threshold from the policy engine, so an action row
    written directly — with whatever flag its author chose — is judged on the
    live decision rather than on what the row claims.
    """
    raise_credit_above_threshold(monkeypatch)
    forged = prepare_action(
        conn,
        action_type=ActionType.ISSUE_SERVICE_CREDIT,
        target_type="order",
        target_id=ELIGIBLE_ORDER,
        parameters={
            "amount": "2500.00",
            "currency": "INR",
            "requires_manager_approval": "false",  # the lie
        },
        requested_by="lw.agent",
        requested_by_role="support_agent",
        account_id=LUMENWORKS_ACCOUNT,
    )

    with pytest.raises(ActionForbidden):
        confirm(orchestrator, forged.action_id, lumenworks_context)
    assert get_order_service_credits(conn, ELIGIBLE_ORDER) == []
