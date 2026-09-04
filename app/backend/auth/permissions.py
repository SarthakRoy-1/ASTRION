"""Organisation roles and the permissions each one carries.

The authorization question this module answers is *"may this role do this
thing"*, and it answers it with a table rather than with conditionals spread
across routes. One table means the whole permission surface can be read in one
screen, diffed in one review, and asserted over exhaustively in one test.

Five roles, ordered by authority:

    OWNER       everything, including deleting the organisation
    ADMIN       everything operational, plus membership and rules
    OPERATIONS  may execute state-changing actions; no membership control
    SUPPORT     may investigate and *propose*; may not execute
    VIEWER      read only

Two rules that are easy to get wrong and are therefore stated here:

- **Proposing and executing are separate permissions.** SUPPORT can prepare an
  escalation and cannot confirm one. That split is the whole point of the
  confirmation gate; collapsing it into a single "actions" permission would
  quietly hand every support user execution rights.
- **A role is meaningless without an organisation.** Permission checks take
  the membership, never the user — the same person may be an ADMIN in one
  organisation and a VIEWER in another, and asking "what is this user's role"
  without naming the organisation is always a bug.
"""

from __future__ import annotations

from enum import StrEnum


class OrgRole(StrEnum):
    """A member's role within one organisation."""

    OWNER = "owner"
    ADMIN = "admin"
    OPERATIONS = "operations"
    SUPPORT = "support"
    VIEWER = "viewer"


class Permission(StrEnum):
    """A single capability. Checked server-side, never inferred from the UI."""

    # Reading
    READ_RECORDS = "read_records"
    READ_DOCUMENTS = "read_documents"
    READ_AUDIT_LOG = "read_audit_log"

    # The agent
    RUN_AGENT = "run_agent"

    # State-changing actions, split at the confirmation gate
    PROPOSE_ACTION = "propose_action"
    EXECUTE_ACTION = "execute_action"

    # Configuration
    MANAGE_RULES = "manage_rules"
    MANAGE_MEMBERS = "manage_members"
    MANAGE_ORGANIZATION = "manage_organization"
    DELETE_ORGANIZATION = "delete_organization"


_VIEWER: frozenset[Permission] = frozenset(
    {
        Permission.READ_RECORDS,
        Permission.READ_DOCUMENTS,
        Permission.RUN_AGENT,
    }
)

#: SUPPORT adds *proposal* only. The absence of EXECUTE_ACTION here is the
#: control, not an oversight: a support user investigates and drafts, and a
#: second person with operational authority confirms.
_SUPPORT: frozenset[Permission] = _VIEWER | {Permission.PROPOSE_ACTION}

_OPERATIONS: frozenset[Permission] = _SUPPORT | {
    Permission.EXECUTE_ACTION,
    Permission.READ_AUDIT_LOG,
}

_ADMIN: frozenset[Permission] = _OPERATIONS | {
    Permission.MANAGE_RULES,
    Permission.MANAGE_MEMBERS,
    Permission.MANAGE_ORGANIZATION,
}

_OWNER: frozenset[Permission] = _ADMIN | {Permission.DELETE_ORGANIZATION}

#: The complete matrix. Every authorization decision in the application reads
#: from here; there is no second table and no route that hard-codes a role.
ROLE_PERMISSIONS: dict[OrgRole, frozenset[Permission]] = {
    OrgRole.OWNER: _OWNER,
    OrgRole.ADMIN: _ADMIN,
    OrgRole.OPERATIONS: _OPERATIONS,
    OrgRole.SUPPORT: _SUPPORT,
    OrgRole.VIEWER: _VIEWER,
}


def permissions_for(role: OrgRole) -> frozenset[Permission]:
    """Every permission a role carries. Unknown roles get nothing."""
    return ROLE_PERMISSIONS.get(role, frozenset())


def role_has(role: OrgRole, permission: Permission) -> bool:
    """The single predicate the rest of the application asks.

    Deliberately total: an unrecognised role is not an error here, it simply
    holds no permissions. A role that fails to parse must deny, never crash
    into a handler that treats the exception as a pass.
    """
    return permission in permissions_for(role)
