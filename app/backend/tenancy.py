"""The tenant boundary, as a value.

A workspace (an `organizations` row) owns its data: accounts, orders, tickets,
documents, prepared actions. Every read and write of that data is made *for* a
`Scope`, and every scoped query is built from it, so the boundary is stated once
here rather than re-derived, slightly differently, in each query.

    Scope(org_id="ORG-1")                          the whole workspace
    Scope(org_id="ORG-1", account_ids={"ACCT-7"})  one account within it
    Scope(org_id=None)                             no workspace: matches nothing

Two levels, and the first is the security boundary:

- `org_id` is the **tenant**. It comes from the authenticated session's
  membership, never from a request, a tool argument or model output. Every
  query filters on it, so one workspace's rows are not merely hidden from
  another -- they are never selected.
- `account_ids` *narrows within* a workspace: a customer persona that may see
  one account, or an integration limited to some. `None` means every account
  the workspace owns. An account id is a name inside a workspace, not an
  identity across them: two workspaces may both have an `ACCT-001`.

A `Scope` with no `org_id` is deliberately usable and deliberately empty. A
caller that reaches the data layer without a workspace -- a script that forgot
one, a context built wrongly -- gets nothing back, not everything. Failing open
is how an isolation bug becomes a data leak.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass


@dataclass(frozen=True)
class Scope:
    org_id: str | None
    account_ids: frozenset[str] | None = None

    @classmethod
    def of(cls, org_id: str | None, account_ids: Collection[str] | None = None) -> "Scope":
        return cls(org_id, None if account_ids is None else frozenset(account_ids))

    @property
    def is_empty(self) -> bool:
        """Whether this scope can match anything at all."""
        return self.org_id is None or self.account_ids == frozenset()

    def allows_account(self, account_id: str | None) -> bool:
        """Whether an account (already known to be in this workspace) is visible."""
        if self.org_id is None:
            return False
        if self.account_ids is None:
            return True
        return account_id in self.account_ids

    def clause(self, alias: str = "", *, by_account: bool = True) -> tuple[str, list[str]]:
        """The WHERE fragment implementing this scope, and its parameters.

        Compiled into the query rather than applied to its results, so a row
        outside the scope is never loaded into the process. `by_account=False`
        is for tables that have no `account_id` to narrow by.

        An empty scope compiles to a predicate that matches nothing (`1 = 0`),
        which is not the same as no predicate.
        """
        prefix = f"{alias}." if alias else ""
        if self.org_id is None:
            return "1 = 0", []
        params: list[str] = [self.org_id]
        parts = [f"{prefix}org_id = ?"]
        if by_account and self.account_ids is not None:
            ordered = sorted(self.account_ids)
            if not ordered:
                return "1 = 0", []
            parts.append(f"{prefix}account_id IN ({','.join('?' * len(ordered))})")
            params.extend(ordered)
        return " AND ".join(parts), params


#: The workspace the assessment workbook is imported into unless another is
#: named, and the one the development-only demo personas (AUTH_MODE=demo_header)
#: act within. It is an ordinary workspace row with no members and no special
#: powers; a production deployment that never imports the workbook never has it.
LEGACY_ORG_ID = "ORG-legacy-assessment"

#: The whole legacy workspace: every account in it, no narrowing. What a script
#: or a test that works on the imported assessment data passes.
LEGACY_SCOPE = Scope(LEGACY_ORG_ID)
