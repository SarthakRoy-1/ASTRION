"""Models for state-changing actions and their confirmation lifecycle (Phase 4).

An action moves through explicit, persisted states:

    prepare_*  ->  PENDING_CONFIRMATION
                        |
              +---------+---------+
              |                   |
        human confirms      human rejects
              |                   |
          EXECUTED            REJECTED
              |
         (or FAILED if re-validation fails at execution time)

The confirmation state lives in the database, not in a prompt instruction and
not in conversation history. That is the whole point: an agent cannot talk its
way past a state machine, and a transcript replay cannot re-trigger an
execution.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class ActionType(StrEnum):
    """The state-changing operations available.

    Deliberately small: every one is an operation the supplied dataset can
    actually evidence, and each exists to prove the confirmation mechanism is
    not type-specific rather than to pad a feature list.

    `ISSUE_SERVICE_CREDIT` is the first that moves money, and it is the reason
    the SOP's manager-approval threshold is now enforceable rather than merely
    computed — see `AgentContext.may_approve_high_value`. Its amount is never
    taken from the caller: `prepare_service_credit` reads it from
    `policies/service_credit.py`, which is the only thing in this system
    permitted to decide what a customer is owed.
    """

    CREATE_ESCALATION = "create_escalation"
    ADD_TICKET_NOTE = "add_ticket_note"
    ISSUE_SERVICE_CREDIT = "issue_service_credit"


class ActionStatus(StrEnum):
    PENDING_CONFIRMATION = "pending_confirmation"
    EXECUTED = "executed"
    REJECTED = "rejected"
    EXPIRED = "expired"
    FAILED = "failed"


class ProposedAction(BaseModel):
    """A prepared but not-yet-performed action.

    Preparing one writes a row describing what *would* happen; it never
    touches the operational tables. `preview` is the human-readable summary a
    confirmation dialog shows, and `evidence_chunk_ids` records what justified
    proposing it.
    """

    model_config = ConfigDict(frozen=True)

    action_id: str
    action_type: ActionType
    status: ActionStatus
    account_id: str | None
    target_type: str
    target_id: str
    parameters: dict[str, str]
    preview: str

    requested_by: str
    requested_by_role: str
    #: The conversation this proposal was produced in (Phase 5), or None for a
    #: caller with no session. Re-checked at confirmation time, so a proposal
    #: cannot be confirmed from a different conversation.
    session_id: str | None = None
    prepared_at_utc: datetime
    expires_at_utc: datetime

    evidence_chunk_ids: list[str] = []
    reason: str | None = None

    def is_expired(self, now: datetime) -> bool:
        return now >= self.expires_at_utc

    def parameter_fingerprint(self) -> str:
        """A stable digest of exactly what this action would do.

        A confirmation may carry the fingerprint the human was shown. If the
        stored parameters no longer match it, the thing being approved is not
        the thing that was reviewed, and confirmation is refused. This closes
        the gap between "the operator approved a preview" and "the system
        executed a proposal".
        """
        payload = json.dumps(
            {
                "action_type": self.action_type.value,
                "target_type": self.target_type,
                "target_id": self.target_id,
                "parameters": self.parameters,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


class ExecutedAction(BaseModel):
    """The audit record of an action that reached a terminal state.

    Retains who initiated it, what was requested, with which parameters, what
    evidence supported it, and the full timeline — prepared, confirmed,
    executed.
    """

    model_config = ConfigDict(frozen=True)

    action_id: str
    action_type: ActionType
    status: ActionStatus
    account_id: str | None
    target_type: str
    target_id: str
    parameters: dict[str, str]
    preview: str

    requested_by: str
    requested_by_role: str
    confirmed_by: str | None
    session_id: str | None = None
    prepared_at_utc: datetime
    confirmed_at_utc: datetime | None
    executed_at_utc: datetime | None
    rejected_at_utc: datetime | None

    evidence_chunk_ids: list[str] = []
    result: dict[str, str] | None = None
    failure_reason: str | None = None
