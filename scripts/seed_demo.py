#!/usr/bin/env python
"""Seed the one shared public-demo workspace, idempotently.

A deployment running real authentication has no way in for a visitor: there is
no mail transport, so the verification link a registration issues reaches
nobody, and an unverified account cannot sign in. This script closes that gap
the only way that does not weaken anything — by creating ordinary accounts that
sign in through the ordinary endpoint.

    python scripts/seed_demo.py --db data/processed/parcelpilot.db

with the account password supplied through the DEMO_SEED_PASSWORD
environment variable (or --password).

What it creates, once:

- one workspace, slug ``parcelpilot-demo``, holding the ingested dataset
  accounts that no other workspace has claimed;
- three members at three different roles, so the authorization boundaries are
  something a visitor can *walk into* rather than read about.

**Why this is not `bootstrap_workspace.py`.** That script exists to open a
brand-new database for an operator, and it creates a workspace every time it
runs — `_unique_slug` suffixes the second one `-2` rather than refusing. That
is right for its job and wrong for this one: a container that restarts must
converge on the same demo tenant, not accumulate `parcelpilot-demo-7`. The two
scripts share every function that touches the database; only the idempotency
contract differs.

Idempotency rests on the schema's own uniqueness rather than on anything this
file assumes: `organizations.slug`, `users.email` and the unique index on
`organization_accounts.account_id` are the keys, and each step checks for its
row before writing one. Running this twice produces the same logical state as
running it once.

What it will not do:

- **Overwrite an existing user.** An address that already exists keeps its
  password, its verification state and its display name. Rotating a demo
  password means rebuilding the database, which is also the documented reset.
- **Move an account between workspaces.** An account another workspace already
  owns is reported and skipped. That unique index *is* the tenant boundary.
- **Delete anything.** There is no path here that drops a row.
- **Grant anything a role does not already carry.** Roles come from
  `ROLE_PERMISSIONS`; there is no demo-only permission, and this script cannot
  invent one.

The seeded accounts are marked verified here, for the same reason
`bootstrap_workspace.py` marks its owner verified: the operator running this on
the server is the out-of-band proof, and there is no transport to send a link
through. `REQUIRE_VERIFIED_EMAIL` is untouched, and every other account on the
deployment still has to verify.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.backend.auth import repository as repo  # noqa: E402
from app.backend.auth import workspaces as workspace_service  # noqa: E402
from app.backend.auth.passwords import (  # noqa: E402
    PasswordError,
    hash_password,
    validate_password,
)
from app.backend.auth.permissions import OrgRole  # noqa: E402
from app.backend.core.config import DEFAULT_DB_PATH  # noqa: E402
from app.backend.services.database import (  # noqa: E402
    get_connection,
    initialize_schema,
)

#: The workspace's identity. The slug is the idempotency key, so it is fixed
#: here rather than derived at runtime — and `slugify(DEMO_WORKSPACE_NAME)`
#: must equal it, which a test asserts so a renamed workspace cannot silently
#: start creating a second tenant on every boot.
DEMO_WORKSPACE_SLUG = "parcelpilot-demo"
DEMO_WORKSPACE_NAME = "ParcelPilot Demo"

#: The demo members, and the roles whose boundaries they exist to demonstrate.
#:
#: Addresses are under `.example`, which RFC 2606 reserves and which therefore
#: cannot receive mail anywhere. That is the honest choice for accounts whose
#: verification is granted by this script: nobody can be misled into thinking a
#: message was sent to a real inbox.
#:
#: The owner is first because the workspace's creator becomes its owner, and
#: `create_workspace` refuses to make a workspace without one.
DEMO_USERS: tuple[tuple[str, str, OrgRole], ...] = (
    ("owner@demo.parcelpilot.example", "Demo owner", OrgRole.OWNER),
    ("operations@demo.parcelpilot.example", "Demo operations", OrgRole.OPERATIONS),
    ("support@demo.parcelpilot.example", "Demo support", OrgRole.SUPPORT),
)


class SeedError(Exception):
    """The demo tenant cannot be brought to its intended state."""


def dataset_accounts(conn) -> list[str]:
    return [
        row["account_id"]
        for row in conn.execute("SELECT account_id FROM accounts ORDER BY account_id")
    ]


def find_workspace(conn, slug: str) -> dict | None:
    """The workspace with this slug, or None. The idempotency check."""
    row = conn.execute(
        "SELECT org_id, name, slug, status FROM organizations WHERE slug = ?",
        (slug,),
    ).fetchone()
    return None if row is None else dict(row)


def ensure_user(conn, *, email: str, display_name: str, password_hash: str):
    """The user with this address, created verified if absent.

    Returns `(user, created)`. An existing account is returned untouched — this
    script never changes a password, a display name or a verification state it
    did not write, because it cannot tell a demo address someone re-registered
    from one it created itself.
    """
    normalized = repo.normalize_email(email)
    existing = repo.get_user_by_email(conn, normalized)
    if existing is not None:
        return existing, False
    user = repo.create_user(
        conn,
        email=normalized,
        display_name=display_name,
        password_hash=password_hash,
        email_verified=True,
    )
    return user, True


def ensure_membership(conn, *, org_id: str, user_id: str, role: OrgRole) -> bool:
    """Add the membership if it is missing. Returns whether one was added.

    An existing membership is left exactly as it is, including its role: a
    re-seed must not quietly re-promote someone an operator demoted.
    """
    if repo.get_membership(conn, org_id=org_id, user_id=user_id) is not None:
        return False
    repo.add_member(conn, org_id=org_id, user_id=user_id, role=role)
    return True


def ensure_accounts(conn, *, org_id: str, account_ids: list[str]):
    """Attach every unclaimed dataset account. Returns (attached, skipped).

    `uq_organization_accounts_account` allows an account exactly one workspace.
    An account another workspace holds is reported, never moved — deciding who
    owns a tenant's data is not a decision a boot script may take.
    """
    attached: list[str] = []
    skipped: list[tuple[str, str]] = []
    for account_id in account_ids:
        owner = repo.org_owning_account(conn, account_id)
        if owner == org_id:
            continue
        if owner is not None:
            skipped.append((account_id, owner))
            continue
        repo.grant_account(conn, org_id=org_id, account_id=account_id)
        attached.append(account_id)
    return attached, skipped


def seed(conn, *, password: str, account_ids: list[str] | None = None) -> dict:
    """Bring the demo tenant into existence, or confirm it already is.

    Returns a summary of what changed, so a caller (and the container log) can
    tell a first boot from every boot after it.
    """
    initialize_schema(conn)

    password_hash = hash_password(password)
    summary: dict = {
        "workspace_created": False,
        "users_created": [],
        "memberships_created": [],
        "accounts_attached": [],
        "accounts_skipped": [],
    }

    users = []
    for email, display_name, role in DEMO_USERS:
        user, created = ensure_user(
            conn, email=email, display_name=display_name, password_hash=password_hash
        )
        users.append((user, role))
        if created:
            summary["users_created"].append(email)

    owner_user = users[0][0]
    workspace = find_workspace(conn, DEMO_WORKSPACE_SLUG)
    wanted = dataset_accounts(conn) if account_ids is None else list(account_ids)

    if workspace is None:
        created = workspace_service.create_workspace(
            conn,
            owner_user_id=owner_user.user_id,
            name=DEMO_WORKSPACE_NAME,
            # Attached below rather than here, so the claimed-account report is
            # produced by one code path whether or not this is the first boot.
            account_ids=[],
        )
        if created["slug"] != DEMO_WORKSPACE_SLUG:
            # Only reachable if something else already holds the slug under a
            # different name. Refusing beats seeding a second demo tenant that
            # the next boot would not find.
            raise SeedError(
                f"expected slug {DEMO_WORKSPACE_SLUG!r} but the workspace was "
                f"created as {created['slug']!r}; something else already holds "
                f"that slug"
            )
        workspace = created
        summary["workspace_created"] = True

    org_id = workspace["org_id"]

    for user, role in users:
        if ensure_membership(conn, org_id=org_id, user_id=user.user_id, role=role):
            summary["memberships_created"].append((user.email, role.value))

    attached, skipped = ensure_accounts(conn, org_id=org_id, account_ids=wanted)
    summary["accounts_attached"] = attached
    summary["accounts_skipped"] = skipped
    summary["org_id"] = org_id
    summary["slug"] = workspace["slug"]
    return summary



def main() -> int:
    parser = argparse.ArgumentParser(description="Seed the shared demo workspace.")
    parser.add_argument(
        "--password",
        help="Password for the demo accounts. Read from DEMO_SEED_PASSWORD if "
        "omitted. Never write it into a file this repository tracks.",
    )
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="Database path.")
    parser.add_argument(
        "--accounts",
        nargs="*",
        help="Dataset account ids to attach. Defaults to every ingested account.",
    )
    args = parser.parse_args()

    password = args.password or os.environ.get("DEMO_SEED_PASSWORD", "")
    if not password:
        print(
            "error: no demo password. Set DEMO_SEED_PASSWORD or pass --password.",
            file=sys.stderr,
        )
        return 1
    try:
        validate_password(password)
    except PasswordError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    db_path = Path(args.db)
    if not db_path.exists():
        print(
            f"error: no database at {db_path}. Run the ingestion scripts first.",
            file=sys.stderr,
        )
        return 1

    conn = get_connection(db_path)
    try:
        summary = seed(conn, password=password, account_ids=args.accounts)
    except SeedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()

    # ASCII only: this runs on a server console, and a Windows cp1252 terminal
    # renders an em-dash here as a replacement character.
    verb = "created" if summary["workspace_created"] else "already present"
    print(f"demo workspace {summary['slug']} ({summary['org_id']}): {verb}")
    print(f"  users created:       {', '.join(summary['users_created']) or '(none)'}")
    print(
        "  memberships created: "
        + (
            ", ".join(f"{e} as {r}" for e, r in summary["memberships_created"])
            or "(none)"
        )
    )
    print(
        f"  accounts attached:   {', '.join(summary['accounts_attached']) or '(none)'}"
    )
    for account_id, owner in summary["accounts_skipped"]:
        print(f"  skipped {account_id}: already owned by workspace {owner}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
