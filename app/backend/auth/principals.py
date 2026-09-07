"""Mock authentication and the authorization context it produces (Phase 5).

Full production authentication is out of scope for this phase, but the
*boundary* it protects is not. What matters is where authority comes from, and
that is what this module fixes in place:

    identity asserted by the client  ->  principal looked up on the server
                                     ->  role and account scope read from the
                                         directory, never from the request
                                     ->  AgentContext handed to the agent

The client says *who* it is. The server decides what that identity may see.
Replacing this module with a real identity provider means producing an
`AgentContext` from a verified token instead of from `MOCK_PRINCIPALS`;
nothing downstream changes, because nothing downstream trusts the request.

Two rules, both load-bearing:

- **A request can narrow scope, never widen it.** `requested_account_ids` is
  intersected with the principal's scope. A customer asking about another
  customer's account gets an unchanged scope and, below this layer, no data.
- **The natural-language message is never consulted here.** Account ids named
  in prose are text, not authority. That is the whole point of establishing
  the context separately from the message.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Collection
from dataclasses import dataclass
from enum import StrEnum

from app.backend.core.errors import AuthenticationError
from app.backend.models.agent import AgentContext, Role
from app.backend.services.records import get_all_account_ids


class ScopeKind(StrEnum):
    """How a principal's account scope is determined."""

    #: Internal staff: every account present in the ingested dataset. Resolved
    #: from the database at request time so no account id is hard-coded here.
    ALL_ACCOUNTS = "all_accounts"
    #: External callers: exactly the accounts listed on the principal.
    EXPLICIT = "explicit"


@dataclass(frozen=True)
class Principal:
    """A demo identity and the authority the server grants it."""

    user_id: str
    display_name: str
    role: Role
    scope_kind: ScopeKind
    account_ids: frozenset[str] = frozenset()
    description: str = ""

    def resolve_scope(self, conn: sqlite3.Connection) -> frozenset[str]:
        if self.scope_kind is ScopeKind.ALL_ACCOUNTS:
            return frozenset(get_all_account_ids(conn))
        return self.account_ids


#: The demo directory. Deliberately small and explicit: three internal contexts
#: differing only in what they may *do*, and two customer contexts scoped to one
#: account each.
#:
#: Insertion order is part of the contract. `GET /api/principals` returns this
#: order verbatim, and a client that offers the first entry as its default
#: should land on the primary internal-support persona — so the support agent
#: leads and the narrower external contexts follow.
MOCK_PRINCIPALS: dict[str, Principal] = {
    p.user_id: p
    for p in (
        Principal(
            user_id="support.agent",
            display_name="ASTRION support agent",
            role=Role.SUPPORT_AGENT,
            scope_kind=ScopeKind.ALL_ACCOUNTS,
            description="Internal support staff. May prepare and confirm actions.",
        ),
        # Carries a distinct role but, today, identical capabilities to
        # `support.agent`: `AgentContext.may_change_state` is the only
        # authorization distinction the system enforces, and it admits both.
        # The description says so rather than implying a manager-only power
        # that no code checks — see the note above `Role.SUPPORT_MANAGER`.
        Principal(
            user_id="support.manager",
            display_name="ASTRION support manager",
            role=Role.SUPPORT_MANAGER,
            scope_kind=ScopeKind.ALL_ACCOUNTS,
            description=(
                "Internal support manager. Same capabilities as a support agent "
                "today: may prepare and confirm actions. The role is carried "
                "through the system but grants nothing extra yet."
            ),
        ),
        Principal(
            user_id="support.readonly",
            display_name="ASTRION support (read-only)",
            role=Role.READ_ONLY,
            scope_kind=ScopeKind.ALL_ACCOUNTS,
            description="Internal read-only viewer. May not change any state.",
        ),
        Principal(
            user_id="customer.northstar",
            display_name="Northstar Logistics (customer)",
            role=Role.CUSTOMER,
            scope_kind=ScopeKind.EXPLICIT,
            account_ids=frozenset({"ACCT-001"}),
            description="External customer contact, scoped to their own account.",
        ),
        Principal(
            user_id="customer.lumenworks",
            display_name="LumenWorks (customer)",
            role=Role.CUSTOMER,
            scope_kind=ScopeKind.EXPLICIT,
            account_ids=frozenset({"ACCT-002"}),
            description="External customer contact, scoped to their own account.",
        ),
    )
}

DEFAULT_PRINCIPAL_ID = "support.agent"


def get_principal(user_id: str | None) -> Principal:
    """Resolve an asserted identity, or refuse.

    Refusing an unknown identity rather than defaulting to one is what keeps
    the mock honest: there is no anonymous path into the agent.
    """
    if not user_id or not user_id.strip():
        raise AuthenticationError(
            "No user identity supplied. Provide `user_id` in the request body or "
            "the X-Astrion-User header.",
            details={"known_identities": sorted(MOCK_PRINCIPALS)},
        )
    principal = MOCK_PRINCIPALS.get(user_id.strip())
    if principal is None:
        # The directory is a fixed demo list, so naming its members leaks
        # nothing; it is documentation, not customer data.
        raise AuthenticationError(
            f"Unknown user identity {user_id.strip()!r}.",
            details={"known_identities": sorted(MOCK_PRINCIPALS)},
        )
    return principal


def build_context(
    conn: sqlite3.Connection,
    principal: Principal,
    *,
    session_id: str | None = None,
    requested_account_ids: Collection[str] | None = None,
) -> AgentContext:
    """Produce the authorization envelope the agent will run under.

    `requested_account_ids` may only *narrow*. It exists so a support agent can
    deliberately work within one customer's scope; it can never grant access
    the principal does not already hold.
    """
    scope = principal.resolve_scope(conn)
    if requested_account_ids is not None:
        scope = frozenset(scope) & {str(a).strip() for a in requested_account_ids}

    return AgentContext(
        user_id=principal.user_id,
        role=principal.role,
        allowed_account_ids=scope,
        session_id=session_id,
    )
