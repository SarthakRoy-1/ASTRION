"""Workspace roles and the permissions each one carries.

The authorization question this module answers is *"may this role do this
thing"*, and it answers it with a table rather than with conditionals spread
across routes. One table means the whole permission surface can be read in one
screen, diffed in one review, and asserted over exhaustively in one test.

**Workspace and organisation are the same thing.** `Workspace` is the product
term — it is what the UI says and what the API path spells. `organization` /
`org_id` is the internal identifier, fixed by the Phase 0 schema. There is one
entity, with a product name and a column name; there is deliberately no second
concept.

Five roles, ordered by authority:

    OWNER       everything, including deleting the workspace and transferring
                ownership
    ADMIN       everything operational, plus membership and rules
    OPERATIONS  may execute state-changing actions; no membership control
    SUPPORT     may investigate and *propose*; may not execute
    VIEWER      read only

Three rules that are easy to get wrong and are therefore stated here:

- **Proposing and executing are separate permissions.** SUPPORT can prepare an
  escalation and cannot confirm one. That split is the whole point of the
  confirmation gate; collapsing it into a single "actions" permission would
  quietly hand every support user execution rights.
- **Inviting, removing and re-roling are separate permissions.** They were one
  coarse `manage_members` before Phase 1. Splitting them is what makes it
  possible to grant a role that can add people without also granting it the
  power to remove them.
- **A role is meaningless without a workspace.** Permission checks take the
  membership, never the user — the same person may be an ADMIN in one
  workspace and a VIEWER in another, and asking "what is this user's role"
  without naming the workspace is always a bug.
"""

from __future__ import annotations

from enum import StrEnum


class OrgRole(StrEnum):
    """A member's role within one workspace.

    Named `OrgRole` because the column it is stored in is `memberships.role`
    and the internal entity is the organisation; `WorkspaceRole` is exported
    below as an alias for code that reads better in product terms.
    """

    OWNER = "owner"
    ADMIN = "admin"
    OPERATIONS = "operations"
    SUPPORT = "support"
    VIEWER = "viewer"

    @property
    def label(self) -> str:
        """How the role is written for a person to read."""
        return self.value.capitalize()


#: Product-facing alias. Same enum, same values — not a second type.
WorkspaceRole = OrgRole

#: Roles ordered from least to most authority. Used to answer "is this role at
#: least as powerful as that one", which several guards need and which string
#: comparison would get silently wrong.
ROLE_ORDER: tuple[OrgRole, ...] = (
    OrgRole.VIEWER,
    OrgRole.SUPPORT,
    OrgRole.OPERATIONS,
    OrgRole.ADMIN,
    OrgRole.OWNER,
)


def role_rank(role: OrgRole) -> int:
    """Position in `ROLE_ORDER`. Higher means more authority."""
    try:
        return ROLE_ORDER.index(role)
    except ValueError:
        return -1


def outranks(actor: OrgRole, target: OrgRole) -> bool:
    """Whether `actor` holds strictly more authority than `target`.

    The guard behind "an admin may not act on an owner". Without it, an admin
    with `members.remove` could remove the owner and leave the workspace
    ownerless — or remove the only person who could have stopped them.
    """
    return role_rank(actor) > role_rank(target)


class Permission(StrEnum):
    """A single capability. Checked server-side, never inferred from the UI.

    The string values are stable: they are carried on `AgentContext.permissions`
    and compared by value in `models/agent.py`, so renaming one is a wire
    change, not a refactor.
    """

    # Workspace
    WORKSPACE_READ = "workspace.read"
    WORKSPACE_UPDATE = "workspace.update"
    WORKSPACE_DELETE = "workspace.delete"

    # Membership
    MEMBERS_READ = "members.read"
    MEMBERS_INVITE = "members.invite"
    MEMBERS_REMOVE = "members.remove"
    MEMBERS_CHANGE_ROLE = "members.change_role"
    #: Deliberately separate from `MEMBERS_CHANGE_ROLE`: granting the owner
    #: role is the one membership change that can strip the actor's own
    #: authority, and only an owner may do it.
    OWNERSHIP_TRANSFER = "ownership.transfer"

    # Reading operational data
    READ_RECORDS = "read_records"
    READ_DOCUMENTS = "read_documents"
    READ_AUDIT_LOG = "read_audit_log"
    #: Operations intelligence — the detected-signal view.
    #:
    #: Granted from VIEWER up, deliberately. A signal is an *aggregation* of
    #: tickets and orders the viewer can already read one at a time; gating the
    #: summary above the underlying records would be security theatre while the
    #: data stayed reachable. What a viewer still cannot do is act on a signal:
    #: that needs PROPOSE_ACTION and EXECUTE_ACTION, which are unchanged.
    READ_OPERATIONS = "operations.read"

    # The agent
    RUN_AGENT = "run_agent"

    # State-changing actions, split at the confirmation gate
    PROPOSE_ACTION = "propose_action"
    EXECUTE_ACTION = "execute_action"

    # Configuration
    MANAGE_RULES = "manage_rules"


#: Every member can see the workspace they belong to and who else is in it.
#: Hiding the member list from a member protects nothing — they can see their
#: colleagues' work in the audit trail and the conversation history anyway —
#: and it makes "who do I ask about this" unanswerable.
_VIEWER: frozenset[Permission] = frozenset(
    {
        Permission.WORKSPACE_READ,
        Permission.MEMBERS_READ,
        Permission.READ_RECORDS,
        Permission.READ_DOCUMENTS,
        Permission.READ_OPERATIONS,
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
    Permission.MEMBERS_INVITE,
    Permission.MEMBERS_REMOVE,
    Permission.MEMBERS_CHANGE_ROLE,
    Permission.WORKSPACE_UPDATE,
}

#: Only the owner may delete the workspace or hand ownership on. Both are
#: irreversible from the actor's own point of view, which is exactly why they
#: sit above the role that runs the workspace day to day.
_OWNER: frozenset[Permission] = _ADMIN | {
    Permission.WORKSPACE_DELETE,
    Permission.OWNERSHIP_TRANSFER,
}

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


def parse_role(value: str) -> OrgRole | None:
    """Parse a client-supplied role name, or None.

    Returns None rather than raising so a caller cannot accidentally let an
    unparseable role through an `except` that was meant for something else.
    """
    try:
        return OrgRole(str(value).strip().lower())
    except (ValueError, AttributeError):
        return None
