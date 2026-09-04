#!/usr/bin/env python
"""Create the first workspace and attach the ingested dataset accounts to it.

The Phase 1 tenancy model derives what a caller may see from
`organization_accounts`. A freshly ingested database has dataset accounts
(`ACCT-001`…) that belong to no workspace, which is **fail-closed**: nobody can
see them, because no membership grants them. This script is how an operator
opens that door deliberately, once.

    python scripts/bootstrap_workspace.py \\
        --email you@example.com --name "Acme Logistics"

What it does, and refuses to do:

- **Never overwrites.** An existing user keeps their password; an account
  already claimed by a workspace is reported and skipped rather than moved.
  Re-running is safe.
- **Never invents a password.** If the user does not exist it is created with a
  password read from `--password` or prompted for, and marked verified —
  because the operator running this on the server *is* the out-of-band proof
  that the address is theirs.
- **Never deletes anything.** There is no path here that drops a row.

Existing conversations, actions and audit entries are untouched. An executed
action whose account joins a workspace becomes visible to that workspace's
members; one whose account stays unclaimed stays invisible to everyone, which
is the safe direction.
"""

from __future__ import annotations

import argparse
import getpass
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
from app.backend.core.config import DEFAULT_DB_PATH  # noqa: E402
from app.backend.services.database import (  # noqa: E402
    get_connection,
    initialize_schema,
)


def dataset_accounts(conn) -> list[str]:
    return [row["account_id"] for row in conn.execute(
        "SELECT account_id FROM accounts ORDER BY account_id"
    )]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", required=True, help="Owner's email address.")
    parser.add_argument("--name", required=True, help="Workspace name.")
    parser.add_argument(
        "--password",
        help="Owner's password when the account does not exist yet. Prompted "
        "for if omitted. Ignored for an existing account.",
    )
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="Database path.")
    parser.add_argument(
        "--accounts",
        nargs="*",
        help="Dataset account ids to attach. Defaults to every ingested account.",
    )
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"error: no database at {db_path}. Run the ingestion scripts first.")
        return 1

    conn = get_connection(db_path)
    try:
        initialize_schema(conn)

        email = repo.normalize_email(args.email)
        user = repo.get_user_by_email(conn, email)
        if user is None:
            password = args.password or getpass.getpass(f"Password for {email}: ")
            try:
                validate_password(password)
            except PasswordError as exc:
                print(f"error: {exc}")
                return 1
            user = repo.create_user(
                conn,
                email=email,
                display_name=email.split("@")[0],
                password_hash=hash_password(password),
                # Verified deliberately: the operator running this on the server
                # is the out-of-band proof, and there is no mail transport to
                # send a verification link through.
                email_verified=True,
            )
            print(f"created user {user.user_id} ({email})")
        else:
            # ASCII only: this runs on a server console, and a Windows cp1252
            # terminal renders an em-dash here as a replacement character.
            print(f"using existing user {user.user_id} ({email}) - password unchanged")

        wanted = args.accounts if args.accounts is not None else dataset_accounts(conn)
        if not wanted:
            print("warning: no dataset accounts found; the workspace will be empty.")

        # Attach only what is still unclaimed, and say what was skipped.
        available, claimed = [], []
        for account_id in wanted:
            owner = repo.org_owning_account(conn, account_id)
            (claimed if owner is not None else available).append((account_id, owner))

        workspace = workspace_service.create_workspace(
            conn,
            owner_user_id=user.user_id,
            name=args.name,
            account_ids=[a for a, _ in available],
        )

        print(f"created workspace {workspace['org_id']} ({workspace['name']})")
        print(f"  slug:     {workspace['slug']}")
        print(f"  owner:    {email}")
        print(f"  accounts: {', '.join(a for a, _ in available) or '(none)'}")
        for account_id, owner in claimed:
            print(f"  skipped {account_id}: already owned by workspace {owner}")

        print("\nSign in at POST /api/auth/login with that address to use it.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
