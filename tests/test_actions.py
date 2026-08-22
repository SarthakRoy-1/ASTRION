"""Phase 4: the state-changing action lifecycle.

The property under test throughout: preparation is inert, and only an
explicit confirmation performs anything. Every test that prepares an action
also asserts that nothing changed until confirmation.
"""

from datetime import timedelta

import pytest

from app.backend.models.actions import ActionStatus, ActionType
from app.backend.services.actions import (
    ActionError,
    ActionNotFound,
    ActionStateError,
    execute_action,
    get_action,
    get_action_audit,
    get_ticket_escalations,
    get_ticket_notes,
    list_pending_actions,
    prepare_action,
    reject_action,
)
from conftest import LUMENWORKS_ACCOUNT, NORTHSTAR_ACCOUNT


def prepare_escalation(conn, ticket_id="TKT-501", account_id=NORTHSTAR_ACCOUNT, **kwargs):
    return prepare_action(
        conn,
        action_type=ActionType.CREATE_ESCALATION,
        target_type="ticket",
        target_id=ticket_id,
        parameters={"reason": "Complete outage reported", **kwargs.pop("parameters", {})},
        requested_by="agent.test",
        requested_by_role="support_agent",
        account_id=account_id,
        evidence_chunk_ids=["chunk-a", "chunk-b"],
        **kwargs,
    )


# --- preparation is inert ----------------------------------------------------


def test_prepare_creates_pending_state(conn):
    action = prepare_escalation(conn)

    assert action.status is ActionStatus.PENDING_CONFIRMATION
    assert action.action_id.startswith("ACT-")
    assert action.preview


def test_prepare_does_not_mutate_anything(conn):
    prepare_escalation(conn)

    assert get_ticket_escalations(conn, "TKT-501") == []
    assert get_ticket_notes(conn, "TKT-501") == []


def test_prepare_does_not_touch_the_source_derived_ticket(conn):
    before = conn.execute(
        "SELECT * FROM tickets WHERE ticket_id = 'TKT-501'"
    ).fetchone()

    prepare_escalation(conn)

    after = conn.execute("SELECT * FROM tickets WHERE ticket_id = 'TKT-501'").fetchone()
    assert tuple(before) == tuple(after)


def test_prepare_records_the_evidence_that_justified_it(conn):
    action = prepare_escalation(conn)

    assert action.evidence_chunk_ids == ["chunk-a", "chunk-b"]


def test_prepare_rejects_missing_required_parameters(conn):
    with pytest.raises(ActionError, match="requires parameter"):
        prepare_action(
            conn,
            action_type=ActionType.CREATE_ESCALATION,
            target_type="ticket",
            target_id="TKT-501",
            parameters={},
            requested_by="agent.test",
            requested_by_role="support_agent",
        )


def test_prepared_actions_are_listed_as_pending(conn):
    action = prepare_escalation(conn)

    pending = list_pending_actions(conn)

    assert [p.action_id for p in pending] == [action.action_id]


# --- execution requires confirmation -------------------------------------------


def test_execution_creates_the_effect(conn):
    action = prepare_escalation(conn)

    audit = execute_action(conn, action.action_id, confirmed_by="manager.test")

    assert audit.status is ActionStatus.EXECUTED
    escalations = get_ticket_escalations(conn, "TKT-501")
    assert len(escalations) == 1
    assert escalations[0]["reason"] == "Complete outage reported"


def test_execution_records_the_full_audit_trail(conn):
    action = prepare_escalation(conn)

    audit = execute_action(conn, action.action_id, confirmed_by="manager.test")

    assert audit.requested_by == "agent.test"
    assert audit.requested_by_role == "support_agent"
    assert audit.confirmed_by == "manager.test"
    assert audit.prepared_at_utc is not None
    assert audit.confirmed_at_utc is not None
    assert audit.executed_at_utc is not None
    assert audit.parameters["reason"] == "Complete outage reported"
    assert audit.evidence_chunk_ids == ["chunk-a", "chunk-b"]
    assert audit.result["ticket_id"] == "TKT-501"


def test_execution_is_single_use(conn):
    action = prepare_escalation(conn)
    execute_action(conn, action.action_id, confirmed_by="manager.test")

    with pytest.raises(ActionStateError, match="not pending confirmation"):
        execute_action(conn, action.action_id, confirmed_by="manager.test")

    assert len(get_ticket_escalations(conn, "TKT-501")) == 1


def test_execution_of_unknown_action_fails(conn):
    with pytest.raises(ActionNotFound):
        execute_action(conn, "ACT-nope", confirmed_by="manager.test")


def test_expired_action_cannot_execute(conn):
    action = prepare_escalation(conn, ttl_minutes=0)
    # Force expiry deterministically rather than sleeping.
    conn.execute(
        "UPDATE agent_actions SET expires_at_utc = ? WHERE action_id = ?",
        ((action.prepared_at_utc - timedelta(minutes=1)).isoformat(), action.action_id),
    )
    conn.commit()

    with pytest.raises(ActionStateError, match="expired"):
        execute_action(conn, action.action_id, confirmed_by="manager.test")

    assert get_ticket_escalations(conn, "TKT-501") == []
    assert get_action(conn, action.action_id).status is ActionStatus.EXPIRED


def test_execution_revalidates_the_target(conn):
    """The world may have moved since the preview was produced."""
    action = prepare_escalation(conn)

    with pytest.raises(ActionStateError, match="no longer available"):
        execute_action(
            conn, action.action_id, confirmed_by="manager.test", target_exists=False
        )

    assert get_ticket_escalations(conn, "TKT-501") == []
    audit = get_action_audit(conn, action.action_id)
    assert audit.status is ActionStatus.FAILED
    assert "no longer available" in audit.failure_reason


# --- rejection -------------------------------------------------------------------


def test_rejection_is_terminal_and_performs_nothing(conn):
    action = prepare_escalation(conn)

    audit = reject_action(conn, action.action_id, rejected_by="manager.test")

    assert audit.status is ActionStatus.REJECTED
    assert audit.rejected_at_utc is not None
    assert get_ticket_escalations(conn, "TKT-501") == []


def test_rejected_action_cannot_be_executed(conn):
    action = prepare_escalation(conn)
    reject_action(conn, action.action_id, rejected_by="manager.test")

    with pytest.raises(ActionStateError):
        execute_action(conn, action.action_id, confirmed_by="manager.test")

    assert get_ticket_escalations(conn, "TKT-501") == []


def test_executed_action_cannot_be_rejected(conn):
    action = prepare_escalation(conn)
    execute_action(conn, action.action_id, confirmed_by="manager.test")

    with pytest.raises(ActionStateError):
        reject_action(conn, action.action_id, rejected_by="manager.test")


def test_rejected_action_leaves_the_pending_list(conn):
    action = prepare_escalation(conn)
    reject_action(conn, action.action_id, rejected_by="manager.test")

    assert list_pending_actions(conn) == []


# --- note actions -------------------------------------------------------------------


def test_ticket_note_action_lifecycle(conn):
    action = prepare_action(
        conn,
        action_type=ActionType.ADD_TICKET_NOTE,
        target_type="ticket",
        target_id="TKT-502",
        parameters={"note": "Matches a documented known issue."},
        requested_by="agent.test",
        requested_by_role="support_agent",
        account_id=LUMENWORKS_ACCOUNT,
    )
    assert get_ticket_notes(conn, "TKT-502") == []

    execute_action(conn, action.action_id, confirmed_by="manager.test")

    notes = get_ticket_notes(conn, "TKT-502")
    assert len(notes) == 1
    assert notes[0]["note"] == "Matches a documented known issue."


# --- scoping -------------------------------------------------------------------------


def test_action_is_invisible_outside_its_account_scope(conn):
    action = prepare_escalation(conn, ticket_id="TKT-502", account_id=LUMENWORKS_ACCOUNT)

    assert get_action(conn, action.action_id, allowed_account_ids={NORTHSTAR_ACCOUNT}) is None
    assert list_pending_actions(conn, allowed_account_ids={NORTHSTAR_ACCOUNT}) == []


def test_out_of_scope_action_cannot_be_executed(conn):
    action = prepare_escalation(conn, ticket_id="TKT-502", account_id=LUMENWORKS_ACCOUNT)

    with pytest.raises(ActionNotFound):
        execute_action(
            conn,
            action.action_id,
            confirmed_by="ns.agent",
            allowed_account_ids={NORTHSTAR_ACCOUNT},
        )

    assert get_ticket_escalations(conn, "TKT-502") == []


def test_out_of_scope_action_cannot_be_rejected(conn):
    action = prepare_escalation(conn, ticket_id="TKT-502", account_id=LUMENWORKS_ACCOUNT)

    with pytest.raises(ActionNotFound):
        reject_action(
            conn,
            action.action_id,
            rejected_by="ns.agent",
            allowed_account_ids={NORTHSTAR_ACCOUNT},
        )
