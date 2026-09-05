"""Prepare, confirm, reject, and execute state-changing actions (Phase 4).

The confirmation gate is a persisted state machine, not a prompt instruction.
`prepare_*` writes a PENDING_CONFIRMATION row and touches nothing else;
`execute_action` is the only function that writes an operational effect, and
it refuses to run on anything that is not still pending.

Three properties this module is responsible for:

- **Preparation is inert.** Preparing an action makes no operational change.
  A caller that stops after preparing has changed nothing.
- **Execution is single-use.** The status transition is the guard, so a
  replayed confirmation cannot execute twice.
- **Execution re-validates.** Authorization, expiry, and the target's
  continued existence are all rechecked at execution time, because the world
  may have moved since the preview was produced.

Effects are written to `ticket_escalations` / `ticket_notes` rather than into
the Phase 2 `tickets` table. Those tables are rebuilt from the workbook on
every ingest run, so an effect written there would be silently reverted.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Collection
from datetime import datetime, timedelta, timezone

from app.backend.models.actions import (
    ActionStatus,
    ActionType,
    ExecutedAction,
    ProposedAction,
)

# A prepared action is a snapshot of a decision made at a moment in time.
# Long-lived proposals invite confirming something whose justification has
# gone stale, so they expire.
DEFAULT_TTL_MINUTES = 30

# Required parameters per action type. Validated at preparation, so a
# malformed proposal fails immediately rather than at execution.
_REQUIRED_PARAMETERS: dict[ActionType, tuple[str, ...]] = {
    ActionType.CREATE_ESCALATION: ("reason",),
    ActionType.ADD_TICKET_NOTE: ("note",),
    # `amount` and `currency` are written by `prepare_service_credit` from the
    # policy engine's decision, never copied from a caller's arguments. They
    # are required here so a proposal that somehow reached this function
    # without them fails at preparation rather than at execution.
    ActionType.ISSUE_SERVICE_CREDIT: ("amount", "currency"),
}


class ActionError(Exception):
    """An action could not be prepared, confirmed, or executed."""


class ActionNotFound(ActionError):
    """No such action, or it belongs outside the caller's scope."""


class ActionStateError(ActionError):
    """The action exists but is not in a state permitting this transition."""


class ActionSessionError(ActionStateError):
    """The action was prepared in a different conversation (Phase 5).

    A subclass of `ActionStateError` so every existing handler still catches
    it, but distinct so the API can say *why* the confirmation was refused —
    "prepared elsewhere" and "already executed" are different situations and
    a client should not have to guess which happened.
    """


class ActionForbidden(ActionError):
    """The caller's role does not permit confirming state-changing actions.

    A subclass of `ActionError` — code written against the base type (a bare
    `except ActionError`, or `pytest.raises(ActionError, ...)`) still catches
    it — but distinct so the API layer can map it to an authorization refusal
    (403) rather than a state conflict (409). This is a role check, not a
    disagreement about the action's current state: the action may be
    perfectly valid and pending, and would still be refused to this caller.
    """


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _row_to_proposed(row: sqlite3.Row) -> ProposedAction:
    return ProposedAction(
        action_id=row["action_id"],
        action_type=row["action_type"],
        status=row["status"],
        account_id=row["account_id"],
        target_type=row["target_type"],
        target_id=row["target_id"],
        parameters=json.loads(row["parameters_json"]),
        preview=row["preview"],
        requested_by=row["requested_by"],
        requested_by_role=row["requested_by_role"],
        session_id=row["session_id"],
        prepared_at_utc=row["prepared_at_utc"],
        expires_at_utc=row["expires_at_utc"],
        evidence_chunk_ids=json.loads(row["evidence_chunk_ids_json"]),
        reason=row["reason"],
    )


def _row_to_executed(row: sqlite3.Row) -> ExecutedAction:
    return ExecutedAction(
        action_id=row["action_id"],
        action_type=row["action_type"],
        status=row["status"],
        account_id=row["account_id"],
        target_type=row["target_type"],
        target_id=row["target_id"],
        parameters=json.loads(row["parameters_json"]),
        preview=row["preview"],
        requested_by=row["requested_by"],
        requested_by_role=row["requested_by_role"],
        confirmed_by=row["confirmed_by"],
        session_id=row["session_id"],
        prepared_at_utc=row["prepared_at_utc"],
        confirmed_at_utc=row["confirmed_at_utc"],
        executed_at_utc=row["executed_at_utc"],
        rejected_at_utc=row["rejected_at_utc"],
        evidence_chunk_ids=json.loads(row["evidence_chunk_ids_json"]),
        result=json.loads(row["result_json"]) if row["result_json"] else None,
        failure_reason=row["failure_reason"],
    )


def _build_preview(
    action_type: ActionType, target_id: str, parameters: dict[str, str]
) -> str:
    """The exact change a confirmation dialog will show. Says what *will*
    happen, in the words a reviewer needs to approve it deliberately."""
    if action_type is ActionType.CREATE_ESCALATION:
        severity = parameters.get("severity")
        suffix = f" at severity {severity}" if severity else ""
        return (
            f"Create an escalation against ticket {target_id}{suffix}. "
            f"Reason: {parameters['reason']}"
        )
    if action_type is ActionType.ISSUE_SERVICE_CREDIT:
        # The figure is stated first and in full, because it is the thing being
        # approved. The manager requirement is stated on the preview itself so
        # a reviewer learns it before clicking, not from a refusal afterwards.
        amount = f"{parameters['currency']} {parameters['amount']}"
        approval = (
            " This exceeds the SOP threshold and requires manager approval."
            if parameters.get("requires_manager_approval") == "true"
            else ""
        )
        return (
            f"Issue a service credit of {amount} against order {target_id}."
            f"{approval}"
        )
    return f"Add an internal note to ticket {target_id}: {parameters['note']}"


def prepare_action(
    conn: sqlite3.Connection,
    *,
    action_type: ActionType,
    target_type: str,
    target_id: str,
    parameters: dict[str, str],
    requested_by: str,
    requested_by_role: str,
    account_id: str | None = None,
    session_id: str | None = None,
    evidence_chunk_ids: Collection[str] | None = None,
    reason: str | None = None,
    ttl_minutes: int = DEFAULT_TTL_MINUTES,
) -> ProposedAction:
    """Record an action awaiting confirmation. Changes nothing operationally.

    Callers are responsible for having already checked that `target_id` is
    visible to `requested_by` — the tool layer does this via the scoped
    repositories before calling here.
    """
    missing = [
        key for key in _REQUIRED_PARAMETERS[action_type] if not parameters.get(key)
    ]
    if missing:
        raise ActionError(
            f"{action_type.value} requires parameter(s): {', '.join(sorted(missing))}"
        )

    prepared_at = _now()
    proposed = ProposedAction(
        action_id=f"ACT-{uuid.uuid4().hex[:12]}",
        action_type=action_type,
        status=ActionStatus.PENDING_CONFIRMATION,
        account_id=account_id,
        target_type=target_type,
        target_id=target_id,
        parameters=dict(parameters),
        preview=_build_preview(action_type, target_id, parameters),
        requested_by=requested_by,
        requested_by_role=requested_by_role,
        session_id=session_id,
        prepared_at_utc=prepared_at,
        expires_at_utc=prepared_at + timedelta(minutes=ttl_minutes),
        evidence_chunk_ids=sorted(evidence_chunk_ids or []),
        reason=reason,
    )

    with conn:
        conn.execute(
            """
            INSERT INTO agent_actions
                (action_id, action_type, status, account_id, target_type, target_id,
                 parameters_json, preview, reason, evidence_chunk_ids_json,
                 requested_by, requested_by_role, session_id, prepared_at_utc,
                 expires_at_utc)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                proposed.action_id,
                proposed.action_type.value,
                proposed.status.value,
                proposed.account_id,
                proposed.target_type,
                proposed.target_id,
                json.dumps(proposed.parameters, sort_keys=True),
                proposed.preview,
                proposed.reason,
                json.dumps(proposed.evidence_chunk_ids),
                proposed.requested_by,
                proposed.requested_by_role,
                proposed.session_id,
                proposed.prepared_at_utc.isoformat(),
                proposed.expires_at_utc.isoformat(),
            ),
        )
    return proposed


def get_action(
    conn: sqlite3.Connection,
    action_id: str,
    *,
    allowed_account_ids: Collection[str] | None = None,
) -> ProposedAction | None:
    row = conn.execute(
        "SELECT * FROM agent_actions WHERE action_id = ?", (action_id,)
    ).fetchone()
    if row is None:
        return None
    if (
        allowed_account_ids is not None
        and row["account_id"] is not None
        and row["account_id"] not in allowed_account_ids
    ):
        return None
    return _row_to_proposed(row)


def get_action_audit(
    conn: sqlite3.Connection,
    action_id: str,
    *,
    allowed_account_ids: Collection[str] | None = None,
) -> ExecutedAction | None:
    """The full audit record: who, what, which evidence, and every timestamp."""
    row = conn.execute(
        "SELECT * FROM agent_actions WHERE action_id = ?", (action_id,)
    ).fetchone()
    if row is None:
        return None
    if (
        allowed_account_ids is not None
        and row["account_id"] is not None
        and row["account_id"] not in allowed_account_ids
    ):
        return None
    return _row_to_executed(row)


def list_pending_actions(
    conn: sqlite3.Connection, *, allowed_account_ids: Collection[str] | None = None
) -> list[ProposedAction]:
    rows = conn.execute(
        "SELECT * FROM agent_actions WHERE status = ? ORDER BY prepared_at_utc",
        (ActionStatus.PENDING_CONFIRMATION.value,),
    ).fetchall()
    actions = [_row_to_proposed(r) for r in rows]
    if allowed_account_ids is None:
        return actions
    return [
        a for a in actions if a.account_id is None or a.account_id in allowed_account_ids
    ]


def reject_action(
    conn: sqlite3.Connection,
    action_id: str,
    *,
    rejected_by: str,
    allowed_account_ids: Collection[str] | None = None,
) -> ExecutedAction:
    """Decline a pending action. Terminal — it can never be executed after."""
    action = get_action(conn, action_id, allowed_account_ids=allowed_account_ids)
    if action is None:
        raise ActionNotFound(f"action {action_id!r} not found or not in scope")
    if action.status is not ActionStatus.PENDING_CONFIRMATION:
        raise ActionStateError(
            f"action {action_id!r} is {action.status.value}, not pending confirmation"
        )

    with conn:
        conn.execute(
            """
            UPDATE agent_actions
               SET status = ?, rejected_at_utc = ?, confirmed_by = ?
             WHERE action_id = ? AND status = ?
            """,
            (
                ActionStatus.REJECTED.value,
                _now().isoformat(),
                rejected_by,
                action_id,
                ActionStatus.PENDING_CONFIRMATION.value,
            ),
        )
    audit = get_action_audit(conn, action_id, allowed_account_ids=allowed_account_ids)
    assert audit is not None
    return audit


def execute_action(
    conn: sqlite3.Connection,
    action_id: str,
    *,
    confirmed_by: str,
    allowed_account_ids: Collection[str] | None = None,
    target_exists: bool = True,
) -> ExecutedAction:
    """Perform a confirmed action. The only function here that mutates state.

    `target_exists` is re-validated by the caller against the scoped
    repository immediately before this call, so a target that disappeared or
    left the caller's scope between preparation and confirmation fails rather
    than executing against a stale premise.
    """
    action = get_action(conn, action_id, allowed_account_ids=allowed_account_ids)
    if action is None:
        raise ActionNotFound(f"action {action_id!r} not found or not in scope")

    if action.status is not ActionStatus.PENDING_CONFIRMATION:
        raise ActionStateError(
            f"action {action_id!r} is {action.status.value}, not pending confirmation"
        )

    now = _now()
    if action.is_expired(now):
        with conn:
            conn.execute(
                "UPDATE agent_actions SET status = ? WHERE action_id = ?",
                (ActionStatus.EXPIRED.value, action_id),
            )
        raise ActionStateError(
            f"action {action_id!r} expired at {action.expires_at_utc.isoformat()}; "
            f"prepare it again"
        )

    if not target_exists:
        with conn:
            conn.execute(
                """
                UPDATE agent_actions
                   SET status = ?, failure_reason = ?, confirmed_at_utc = ?, confirmed_by = ?
                 WHERE action_id = ?
                """,
                (
                    ActionStatus.FAILED.value,
                    f"{action.target_type} {action.target_id} is no longer available",
                    now.isoformat(),
                    confirmed_by,
                    action_id,
                ),
            )
        raise ActionStateError(
            f"{action.target_type} {action.target_id} is no longer available; "
            f"action {action_id!r} was not executed"
        )

    with conn:
        # The status guard in the WHERE clause makes execution single-use even
        # under concurrent confirmation attempts.
        cursor = conn.execute(
            """
            UPDATE agent_actions
               SET status = ?, confirmed_at_utc = ?, executed_at_utc = ?, confirmed_by = ?
             WHERE action_id = ? AND status = ?
            """,
            (
                ActionStatus.EXECUTED.value,
                now.isoformat(),
                now.isoformat(),
                confirmed_by,
                action_id,
                ActionStatus.PENDING_CONFIRMATION.value,
            ),
        )
        if cursor.rowcount != 1:
            raise ActionStateError(
                f"action {action_id!r} was not pending confirmation at execution time"
            )

        result = _apply_effect(conn, action, confirmed_by=confirmed_by, now=now)
        conn.execute(
            "UPDATE agent_actions SET result_json = ? WHERE action_id = ?",
            (json.dumps(result, sort_keys=True), action_id),
        )

    audit = get_action_audit(conn, action_id, allowed_account_ids=allowed_account_ids)
    assert audit is not None
    return audit


def _apply_effect(
    conn: sqlite3.Connection, action: ProposedAction, *, confirmed_by: str, now: datetime
) -> dict[str, str]:
    """Write the operational effect. Runs inside the caller's transaction."""
    if action.action_type is ActionType.CREATE_ESCALATION:
        escalation_id = f"ESC-{uuid.uuid4().hex[:10]}"
        conn.execute(
            """
            INSERT INTO ticket_escalations
                (escalation_id, action_id, ticket_id, account_id, severity, reason,
                 created_by, created_at_utc)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                escalation_id,
                action.action_id,
                action.target_id,
                action.account_id,
                action.parameters.get("severity"),
                action.parameters["reason"],
                confirmed_by,
                now.isoformat(),
            ),
        )
        return {"escalation_id": escalation_id, "ticket_id": action.target_id}

    if action.action_type is ActionType.ISSUE_SERVICE_CREDIT:
        credit_id = f"CRD-{uuid.uuid4().hex[:10]}"
        conn.execute(
            """
            INSERT INTO service_credits
                (credit_id, action_id, order_id, account_id, amount, currency,
                 required_manager_approval, approved_by, created_at_utc)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                credit_id,
                action.action_id,
                action.target_id,
                action.account_id,
                action.parameters["amount"],
                action.parameters["currency"],
                1 if action.parameters.get("requires_manager_approval") == "true" else 0,
                confirmed_by,
                now.isoformat(),
            ),
        )
        return {
            "credit_id": credit_id,
            "order_id": action.target_id,
            "amount": action.parameters["amount"],
            "currency": action.parameters["currency"],
        }

    note_id = f"NOTE-{uuid.uuid4().hex[:10]}"
    conn.execute(
        """
        INSERT INTO ticket_notes
            (note_id, action_id, ticket_id, account_id, note, created_by, created_at_utc)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            note_id,
            action.action_id,
            action.target_id,
            action.account_id,
            action.parameters["note"],
            confirmed_by,
            now.isoformat(),
        ),
    )
    return {"note_id": note_id, "ticket_id": action.target_id}


def get_ticket_escalations(conn: sqlite3.Connection, ticket_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM ticket_escalations WHERE ticket_id = ? ORDER BY created_at_utc",
        (ticket_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_ticket_notes(conn: sqlite3.Connection, ticket_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM ticket_notes WHERE ticket_id = ? ORDER BY created_at_utc",
        (ticket_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_order_service_credits(conn: sqlite3.Connection, order_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM service_credits WHERE order_id = ? ORDER BY created_at_utc",
        (order_id,),
    ).fetchall()
    return [dict(row) for row in rows]
