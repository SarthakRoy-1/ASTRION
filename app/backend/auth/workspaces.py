"""Workspace lifecycle: creation, membership, and the invitation flow.

The repository below stores things; this module decides *what may happen*. It
sits between the API routes and `repository.py` so that every rule about
workspaces has one home, and so a second entry point (a CLI, a bootstrap
script) reaches the same rules rather than reimplementing them.

Three decisions worth stating up front, because each is a security property
rather than a convenience:

**An invitation is bound to an address, and the binding is checked at
redemption.** The token alone is not enough. Someone who finds a forwarded
invitation link cannot redeem it into their own account, because acceptance
compares the invited address against the *authenticated* user's own — which
they cannot choose, having verified it at registration.

**Acceptance is idempotent, and settled invitations stay settled.** Redeeming
a link twice succeeds twice from the user's point of view (they are a member
either way) but consumes the invitation exactly once. That matters because the
alternative — an error on the second click — trains people to retry, and retry
loops around credentials are how replay windows get found.

**A workspace always has an owner.** Enforced in the repository, where every
path goes through it, and surfaced here as a typed refusal the API can explain.
"""

from __future__ import annotations

import re
import secrets
import sqlite3
import unicodedata

from app.backend.auth import repository as repo
from app.backend.auth import service as auth_service
from app.backend.auth.passwords import (
    PasswordError,
    hash_password,
    validate_password,
    verify_password,
    waste_time,
)
from app.backend.auth.permissions import OrgRole
from app.backend.services.audit import (
    AuditEvent,
    AuditOutcome,
    hash_identifier,
    record_event,
)

MAX_WORKSPACE_NAME = 120
MIN_WORKSPACE_NAME = 2

#: How many workspaces one user may own. A cheap ceiling on a cheap-to-create
#: object: without it, an authenticated user can mint rows indefinitely.
MAX_WORKSPACES_PER_USER = 20


#: What a joiner is told for every way a join can fail -- no such code, a wrong
#: password, a workspace with no join password -- so the answer says nothing
#: about which workspaces exist.
JOIN_REFUSAL = "That workspace code and password don't match."

#: The role someone gets on joining with the shared password. The least that
#: still lets them use the workspace (read the records, ask the assistant); the
#: owner raises it with the ordinary member controls. A password shared with a
#: team is not a reason to hand everyone who learns it authority to act.
JOIN_ROLE = OrgRole.VIEWER

#: No I, O, 0 or 1: a code people read out or copy by hand should not have
#: characters that can be mistaken for one another.
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 10
_CODE_ATTEMPTS = 8

#: Failed joins allowed inside `auth_service.LOCKOUT_WINDOW_MINUTES`. A workspace
#: password is guessable in a way a random code is not, so a wrong guess costs
#: something -- per person, per code (so a crowd of accounts cannot share the
#: guessing), and per address.
MAX_JOIN_FAILURES_PER_USER = 5
MAX_JOIN_FAILURES_PER_CODE = 20
MAX_JOIN_FAILURES_PER_CLIENT = 30


class WorkspaceError(Exception):
    """A workspace operation was refused. The message is safe to show."""


class JoinLockedError(WorkspaceError):
    """Too many recent failed joins. Carries no hint about the workspace."""


class InvitationError(Exception):
    """An invitation could not be created or redeemed. Message safe to show."""


# --- naming -----------------------------------------------------------------

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    """A url-safe stable identifier derived from a workspace name.

    Constructive rather than subtractive, like `sanitize_filename`: the result
    is assembled from characters known to be safe, so no separator, control
    character or encoding trick survives it.
    """
    normalized = unicodedata.normalize("NFKD", name or "")
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii").lower()
    slug = _SLUG_STRIP.sub("-", ascii_only).strip("-")
    return slug[:60] or "workspace"


def _unique_slug(conn: sqlite3.Connection, name: str) -> str:
    """A slug nobody is using yet.

    Collisions are resolved by suffixing rather than by refusing: two people
    naming their workspace "Operations" is ordinary, and making the second one
    rename is a worse experience than `operations-2`.
    """
    base = slugify(name)
    if not repo.slug_exists(conn, base):
        return base
    for suffix in range(2, 100):
        candidate = f"{base}-{suffix}"
        if not repo.slug_exists(conn, candidate):
            return candidate
    # Ninety-eight collisions on one name means something automated is running;
    # fall back to something certainly unique rather than looping further.
    return f"{base}-{repo.new_id('x').split('-')[1][:8]}"


def validate_name(name: str) -> str:
    cleaned = (name or "").strip()
    if len(cleaned) < MIN_WORKSPACE_NAME:
        raise WorkspaceError(
            f"A workspace name must be at least {MIN_WORKSPACE_NAME} characters."
        )
    if len(cleaned) > MAX_WORKSPACE_NAME:
        raise WorkspaceError(
            f"A workspace name must be at most {MAX_WORKSPACE_NAME} characters."
        )
    return cleaned


# --- workspace codes --------------------------------------------------------


def generate_workspace_code() -> str:
    """A random code, from the operating system's CSPRNG. Not derived from
    anything -- not an id, a name or a counter -- so it cannot be predicted from
    another workspace's.
    """
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def normalize_workspace_code(raw: str) -> str:
    """How a person's typing is compared with a stored code: case and any
    spaces or hyphens ignored, so a code read aloud or pasted with a gap works.
    """
    return re.sub(r"[\s-]+", "", raw or "").upper()


def _validate_workspace_password(password: str) -> None:
    """The same rules as an account password, and the same messages."""
    try:
        validate_password(password)
    except PasswordError as exc:
        raise WorkspaceError(
            str(exc).replace("Password", "Workspace password", 1)
        ) from exc


def _issue_workspace_access(
    conn: sqlite3.Connection, *, org_id: str, password: str, owner_user_id: str
) -> str:
    """Store a hash and a fresh unique code for a workspace. Returns the code.

    A collision is astronomically unlikely (32^10 codes) and is handled anyway:
    the UNIQUE constraint is what guarantees uniqueness, and a violation just
    means "generate another".
    """
    password_hash = hash_password(password)
    for _ in range(_CODE_ATTEMPTS):
        code = generate_workspace_code()
        try:
            repo.create_workspace_access(
                conn,
                org_id=org_id,
                workspace_code=code,
                password_hash=password_hash,
                owner_user_id=owner_user_id,
            )
        except sqlite3.IntegrityError:
            continue
        return code
    raise WorkspaceError("A workspace code could not be generated. Try again.")


# --- workspaces -------------------------------------------------------------


def create_workspace(
    conn: sqlite3.Connection,
    *,
    owner_user_id: str,
    name: str,
    workspace_password: str | None = None,
    account_ids: list[str] | None = None,
    request_id: str | None = None,
    client_ip: str | None = None,
) -> dict:
    """Create a workspace with its creator as owner.

    The creator becomes OWNER unconditionally — a workspace created with no
    owner would be immediately unmanageable, and there is nobody else to be one
    at this point.

    With a `workspace_password` the workspace also gets a generated code and the
    hash of that password, and can be joined with the pair. Without one (the
    bootstrap scripts, the seeded demo) it has neither and cannot be joined by
    code. The API always supplies one.
    """
    cleaned = validate_name(name)
    if workspace_password is not None:
        _validate_workspace_password(workspace_password)

    owned = [
        m
        for m in repo.list_memberships(conn, owner_user_id)
        if m.role is OrgRole.OWNER
    ]
    if len(owned) >= MAX_WORKSPACES_PER_USER:
        raise WorkspaceError(
            f"You already own {MAX_WORKSPACES_PER_USER} workspaces, which is "
            f"the limit."
        )

    org_id, slug = repo.create_organization(
        conn, name=cleaned, slug=_unique_slug(conn, cleaned)
    )
    repo.add_member(conn, org_id=org_id, user_id=owner_user_id, role=OrgRole.OWNER)
    if workspace_password is not None:
        _issue_workspace_access(
            conn, org_id=org_id, password=workspace_password, owner_user_id=owner_user_id
        )

    # Granting dataset accounts is an operator/bootstrap concern, not something
    # a signing-up user chooses. It is accepted here so the bootstrap script
    # and the tests share one code path, and it is not exposed on the API.
    for account_id in account_ids or []:
        repo.grant_account(conn, org_id=org_id, account_id=account_id)

    record_event(
        conn,
        AuditEvent.ORGANIZATION_CREATED,
        actor_user_id=owner_user_id,
        org_id=org_id,
        request_id=request_id,
        ip_hash=hash_identifier(client_ip),
        details={"slug": slug, "accounts_granted": len(account_ids or [])},
    )
    record_event(
        conn,
        AuditEvent.MEMBERSHIP_CREATED,
        actor_user_id=owner_user_id,
        actor_role=OrgRole.OWNER.value,
        org_id=org_id,
        target_type="user",
        target_id=owner_user_id,
        request_id=request_id,
        details={"role": OrgRole.OWNER.value, "reason": "workspace_creator"},
    )
    workspace = repo.get_organization(conn, org_id)
    assert workspace is not None
    return workspace


def rename_workspace(
    conn: sqlite3.Connection,
    *,
    org_id: str,
    name: str,
    actor_user_id: str,
    actor_role: str | None = None,
    request_id: str | None = None,
) -> dict:
    """Rename a workspace. The slug is deliberately not regenerated.

    A slug that followed the name would break any link anyone had saved. It is
    an identifier, not a label; the name is the label.
    """
    cleaned = validate_name(name)
    before = repo.get_organization(conn, org_id)
    if before is None:
        raise WorkspaceError("That workspace was not found.")
    if not repo.update_organization(conn, org_id=org_id, name=cleaned):
        raise WorkspaceError("That workspace was not found.")

    record_event(
        conn,
        AuditEvent.ORGANIZATION_UPDATED,
        actor_user_id=actor_user_id,
        actor_role=actor_role,
        org_id=org_id,
        target_type="workspace",
        target_id=org_id,
        request_id=request_id,
        details={"from": before["name"], "to": cleaned},
    )
    updated = repo.get_organization(conn, org_id)
    assert updated is not None
    return updated


# --- joining by code and password ------------------------------------------


def _join_failures(conn: sqlite3.Connection, scope: str, identifier: str) -> int:
    return auth_service._recent_failures(conn, identifier=identifier, scope=scope)


def _note_join_failure(
    conn: sqlite3.Connection, *, user_id: str, code: str, client_ip: str | None
) -> None:
    for scope, identifier in (
        ("workspace_join_user", user_id),
        ("workspace_join_code", code),
        ("workspace_join_client", client_ip or ""),
    ):
        if identifier:
            auth_service._record_attempt(
                conn, identifier=identifier, scope=scope, successful=False
            )


def join_workspace(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    workspace_code: str,
    workspace_password: str,
    request_id: str | None = None,
    client_ip: str | None = None,
) -> dict:
    """Add an authenticated user to the workspace a code and password name.

    The account is authenticated before this is called, and the code alone
    grants nothing: access needs the code *and* the password, checked against
    the stored hash. Every failure -- no such code, a wrong password, a
    workspace nobody set a password on -- raises the same `JOIN_REFUSAL`, after
    the same amount of work, so the answer cannot be used to find out which
    workspaces exist. Guessing is throttled (see `MAX_JOIN_FAILURES_*`).
    """
    user = repo.get_user(conn, user_id)
    if user is None or not user.is_active:
        raise WorkspaceError("Your account cannot join workspaces.")

    code = normalize_workspace_code(workspace_code)
    ip_hash = hash_identifier(client_ip)

    if (
        _join_failures(conn, "workspace_join_user", user_id) >= MAX_JOIN_FAILURES_PER_USER
        or _join_failures(conn, "workspace_join_code", code) >= MAX_JOIN_FAILURES_PER_CODE
        or (
            client_ip
            and _join_failures(conn, "workspace_join_client", client_ip)
            >= MAX_JOIN_FAILURES_PER_CLIENT
        )
    ):
        record_event(
            conn,
            AuditEvent.WORKSPACE_JOIN_FAILED,
            outcome=AuditOutcome.DENIED,
            actor_user_id=user_id,
            request_id=request_id,
            ip_hash=ip_hash,
            details={"reason": "locked_out"},
        )
        raise JoinLockedError(
            "Too many failed attempts. Try again in "
            f"{auth_service.LOCKOUT_WINDOW_MINUTES} minutes."
        )

    found = repo.find_workspace_for_join(conn, code) if code else None
    if found is None:
        waste_time()  # the cost of a real check, so timing reveals nothing
        matched = False
    else:
        matched = verify_password(workspace_password, found["password_hash"])

    if not matched:
        _note_join_failure(conn, user_id=user_id, code=code, client_ip=client_ip)
        record_event(
            conn,
            AuditEvent.WORKSPACE_JOIN_FAILED,
            outcome=AuditOutcome.FAILURE,
            actor_user_id=user_id,
            request_id=request_id,
            ip_hash=ip_hash,
            details={"reason": "no_match"},
        )
        raise WorkspaceError(JOIN_REFUSAL)

    org_id = found["org_id"]
    workspace = repo.get_organization(conn, org_id)
    if workspace is None:
        raise WorkspaceError(JOIN_REFUSAL)

    if repo.get_membership(conn, org_id=org_id, user_id=user_id) is None:
        # A removed member keeps their row (see `remove_member`), so returning
        # reactivates it -- as a Viewer, whatever they were before.
        rejoined = repo.reactivate_member(
            conn, org_id=org_id, user_id=user_id, role=JOIN_ROLE
        )
        if not rejoined:
            repo.add_member(conn, org_id=org_id, user_id=user_id, role=JOIN_ROLE)
        record_event(
            conn,
            AuditEvent.MEMBERSHIP_CREATED,
            actor_user_id=user_id,
            actor_role=JOIN_ROLE.value,
            org_id=org_id,
            target_type="user",
            target_id=user_id,
            request_id=request_id,
            details={
                "role": JOIN_ROLE.value,
                "reason": "workspace_code",
                **({"rejoined": True} if rejoined else {}),
            },
        )
    record_event(
        conn,
        AuditEvent.WORKSPACE_JOINED,
        actor_user_id=user_id,
        org_id=org_id,
        request_id=request_id,
        ip_hash=ip_hash,
    )
    return workspace


def change_workspace_password(
    conn: sqlite3.Connection,
    *,
    org_id: str,
    new_password: str,
    actor_user_id: str,
    actor_role: str | None = None,
    request_id: str | None = None,
) -> str:
    """Replace the password people join with. Returns the workspace's code.

    The new hash replaces the old in one statement, so the old password stops
    working at once. Memberships are untouched: this decides who can *join*, not
    who already has. A workspace that never had a join password (the seeded
    demo) gets one, and a code, the first time its owner sets it.
    """
    _validate_workspace_password(new_password)
    if repo.get_organization(conn, org_id) is None:
        raise WorkspaceError("That workspace was not found.")

    code = repo.get_workspace_code(conn, org_id)
    if code is None:
        code = _issue_workspace_access(
            conn, org_id=org_id, password=new_password, owner_user_id=actor_user_id
        )
    else:
        repo.set_workspace_password_hash(
            conn, org_id=org_id, password_hash=hash_password(new_password)
        )

    record_event(
        conn,
        AuditEvent.WORKSPACE_PASSWORD_CHANGED,
        actor_user_id=actor_user_id,
        actor_role=actor_role,
        org_id=org_id,
        target_type="workspace",
        target_id=org_id,
        request_id=request_id,
    )
    return code


# --- membership -------------------------------------------------------------


def change_member_role(
    conn: sqlite3.Connection,
    *,
    org_id: str,
    target_user_id: str,
    role: OrgRole,
    actor_user_id: str,
    actor_role: OrgRole,
    request_id: str | None = None,
) -> None:
    """Change a member's role, with the guards the permission alone does not give.

    `members.change_role` says the actor may re-role *somebody*. It does not
    say whom, and three cases need refusing beyond it:

    - **Acting on a peer or a superior.** An admin re-roling an owner, or
      another admin, is lateral or upward movement. Requiring the actor to
      outrank the target keeps role changes flowing downward only.
    - **Granting a role the actor does not hold.** Otherwise an admin promotes
      someone to owner and then asks them for anything.
    - **Re-roling yourself.** Self-promotion is the whole attack; self-demotion
      is how an only-owner orphans a workspace. Both are refused here, and
      ownership handover has its own audited path.
    """
    if target_user_id == actor_user_id:
        raise WorkspaceError(
            "You cannot change your own role. Ask another owner or admin, or "
            "transfer ownership."
        )

    target = repo.get_membership(conn, org_id=org_id, user_id=target_user_id)
    if target is None:
        raise WorkspaceError("That person is not a member of this workspace.")

    from app.backend.auth.permissions import outranks

    if not outranks(actor_role, target.role):
        raise WorkspaceError(
            f"Your role does not permit changing a {target.role.label}'s role."
        )
    if role is OrgRole.OWNER:
        raise WorkspaceError(
            "Use ownership transfer to make someone an owner, so the change is "
            "recorded as the handover it is."
        )
    if not outranks(actor_role, role):
        raise WorkspaceError(
            f"You cannot grant the {role.label} role, which is not below your own."
        )

    previous = target.role
    try:
        changed = repo.set_member_role(
            conn, org_id=org_id, user_id=target_user_id, role=role
        )
    except repo.LastOwnerError as exc:
        raise WorkspaceError(str(exc)) from exc
    if not changed:
        raise WorkspaceError("That person is not a member of this workspace.")

    record_event(
        conn,
        AuditEvent.MEMBERSHIP_ROLE_CHANGED,
        actor_user_id=actor_user_id,
        actor_role=actor_role.value,
        org_id=org_id,
        target_type="user",
        target_id=target_user_id,
        request_id=request_id,
        details={"from": previous.value, "to": role.value},
    )


def remove_member(
    conn: sqlite3.Connection,
    *,
    org_id: str,
    target_user_id: str,
    actor_user_id: str,
    actor_role: OrgRole,
    request_id: str | None = None,
) -> None:
    """Remove a member. Leaving is allowed; removing an equal or superior is not."""
    target = repo.get_membership(conn, org_id=org_id, user_id=target_user_id)
    if target is None:
        raise WorkspaceError("That person is not a member of this workspace.")

    from app.backend.auth.permissions import outranks

    # Removing yourself is "leave workspace", which anyone may do — subject to
    # the last-owner guard in the repository.
    if target_user_id != actor_user_id and not outranks(actor_role, target.role):
        raise WorkspaceError(
            f"Your role does not permit removing a {target.role.label}."
        )

    try:
        removed = repo.remove_member(conn, org_id=org_id, user_id=target_user_id)
    except repo.LastOwnerError as exc:
        raise WorkspaceError(str(exc)) from exc
    if not removed:
        raise WorkspaceError("That person is not a member of this workspace.")

    # Every session that was acting in this workspace loses its authority on
    # its next request, because membership is re-read per request. Nothing to
    # revoke here — but the event is what makes the removal reviewable.
    record_event(
        conn,
        AuditEvent.MEMBERSHIP_REMOVED,
        actor_user_id=actor_user_id,
        actor_role=actor_role.value,
        org_id=org_id,
        target_type="user",
        target_id=target_user_id,
        request_id=request_id,
        details={
            "removed_role": target.role.value,
            "self_removal": target_user_id == actor_user_id,
        },
    )


def transfer_ownership(
    conn: sqlite3.Connection,
    *,
    org_id: str,
    to_user_id: str,
    actor_user_id: str,
    request_id: str | None = None,
) -> None:
    """Hand the workspace to another member. The outgoing owner becomes admin.

    Kept separate from `change_member_role` because it is the one membership
    change that reduces the actor's own authority, and because the workspace
    must never be observed with zero owners in between — `repo.transfer_ownership`
    does both halves in one transaction.
    """
    if to_user_id == actor_user_id:
        raise WorkspaceError("You are already the owner of this workspace.")

    target = repo.get_membership(conn, org_id=org_id, user_id=to_user_id)
    if target is None:
        raise WorkspaceError("That person is not a member of this workspace.")

    if not repo.transfer_ownership(
        conn, org_id=org_id, from_user_id=actor_user_id, to_user_id=to_user_id
    ):
        raise WorkspaceError("Ownership could not be transferred.")

    record_event(
        conn,
        AuditEvent.OWNERSHIP_TRANSFERRED,
        actor_user_id=actor_user_id,
        actor_role=OrgRole.OWNER.value,
        org_id=org_id,
        target_type="user",
        target_id=to_user_id,
        request_id=request_id,
        details={"from_user": actor_user_id, "to_user": to_user_id},
    )


# --- invitations ------------------------------------------------------------


def invite_member(
    conn: sqlite3.Connection,
    *,
    org_id: str,
    email: str,
    role: OrgRole,
    actor_user_id: str,
    actor_role: OrgRole,
    request_id: str | None = None,
    client_ip: str | None = None,
) -> tuple[repo.Invitation, str]:
    """Issue an invitation. Returns (invitation, plaintext token).

    The token is the caller's only chance to see it. With no mail transport in
    this deployment, the route decides whether to hand it back — see
    `_may_disclose_link` in the API layer, which withholds it in production.
    """
    normalized = repo.normalize_email(email)
    if "@" not in normalized or len(normalized) > 320:
        raise InvitationError("A valid email address is required.")

    from app.backend.auth.permissions import outranks

    # An inviter cannot mint a role above their own; otherwise `members.invite`
    # is a self-promotion primitive one acceptance away.
    if role is OrgRole.OWNER:
        raise InvitationError(
            "An owner cannot be created by invitation. Invite the person, then "
            "transfer ownership to them."
        )
    if not outranks(actor_role, role):
        raise InvitationError(
            f"You cannot invite someone as {role.label}, which is not below "
            f"your own role."
        )

    # Already a member: refuse plainly. This tells the inviter something about
    # their *own* workspace, which they can already see on the member list, so
    # it leaks nothing.
    existing_user = repo.get_user_by_email(conn, normalized)
    if existing_user is not None:
        membership = repo.get_membership(
            conn, org_id=org_id, user_id=existing_user.user_id
        )
        if membership is not None:
            raise InvitationError(
                "That person is already a member of this workspace."
            )

    try:
        invitation, token = repo.create_invitation(
            conn,
            org_id=org_id,
            email=normalized,
            role=role,
            invited_by=actor_user_id,
        )
    except repo.DuplicateInvitationError as exc:
        raise InvitationError(str(exc)) from exc

    record_event(
        conn,
        AuditEvent.INVITATION_CREATED,
        actor_user_id=actor_user_id,
        actor_role=actor_role.value,
        org_id=org_id,
        target_type="invitation",
        target_id=invitation.invitation_id,
        request_id=request_id,
        ip_hash=hash_identifier(client_ip),
        # The invited address is recorded as a domain only. The audit log is
        # read more widely and kept longer than the invitation itself.
        details={"role": role.value, "email_domain": normalized.split("@")[-1]},
    )
    return invitation, token


def revoke_invitation(
    conn: sqlite3.Connection,
    *,
    org_id: str,
    invitation_id: str,
    actor_user_id: str,
    actor_role: OrgRole,
    request_id: str | None = None,
) -> None:
    if not repo.revoke_invitation(
        conn, invitation_id=invitation_id, org_id=org_id, revoked_by=actor_user_id
    ):
        # Covers "no such invitation", "belongs to another workspace" and
        # "already settled" alike — an invitation id from another workspace
        # must not be distinguishable from one that does not exist.
        raise InvitationError("That invitation was not found or is no longer open.")

    record_event(
        conn,
        AuditEvent.INVITATION_REVOKED,
        actor_user_id=actor_user_id,
        actor_role=actor_role.value,
        org_id=org_id,
        target_type="invitation",
        target_id=invitation_id,
        request_id=request_id,
    )


def accept_invitation(
    conn: sqlite3.Connection,
    *,
    token: str,
    user_id: str,
    request_id: str | None = None,
    client_ip: str | None = None,
) -> dict:
    """Redeem an invitation for an authenticated user. Returns the workspace.

    Every refusal below is deliberate, and the order matters — the address
    check comes *before* anything that would confirm which workspace the token
    belongs to, so a found link tells its finder nothing.
    """
    user = repo.get_user(conn, user_id)
    if user is None or not user.is_active:
        raise InvitationError("Your account cannot accept invitations.")

    # An invitation is bound to an *address*, so holding it proves something only
    # if the account's address has been proven. Signing in used to guarantee that;
    # a deployment that does not require a verified address for sign-in
    # (REQUIRE_VERIFIED_EMAIL=false) has accounts whose address is just what was
    # typed, and anyone who could register the invited address would otherwise be
    # able to redeem a leaked link.
    if not user.email_verified:
        record_event(
            conn,
            AuditEvent.INVITATION_ACCEPT_FAILED,
            outcome=AuditOutcome.DENIED,
            actor_user_id=user_id,
            request_id=request_id,
            ip_hash=hash_identifier(client_ip),
            details={"reason": "email_unverified"},
        )
        raise InvitationError(
            "Verify your email address before accepting an invitation."
        )

    invitation = repo.find_invitation_by_token(conn, token)
    if invitation is None:
        record_event(
            conn,
            AuditEvent.INVITATION_ACCEPT_FAILED,
            outcome=AuditOutcome.FAILURE,
            actor_user_id=user_id,
            request_id=request_id,
            ip_hash=hash_identifier(client_ip),
            details={"reason": "unknown_token"},
        )
        raise InvitationError("That invitation link is not valid.")

    # The binding that makes a leaked link useless to whoever finds it. Checked
    # before status, so the finder cannot even learn whether it is still open.
    if invitation.email != repo.normalize_email(user.email):
        record_event(
            conn,
            AuditEvent.INVITATION_ACCEPT_FAILED,
            outcome=AuditOutcome.DENIED,
            actor_user_id=user_id,
            org_id=invitation.org_id,
            target_type="invitation",
            target_id=invitation.invitation_id,
            request_id=request_id,
            ip_hash=hash_identifier(client_ip),
            details={"reason": "email_mismatch"},
        )
        raise InvitationError(
            "This invitation was issued to a different email address. Sign in "
            "with the address it was sent to."
        )

    # Already a member: succeed without consuming anything. Acceptance is
    # idempotent from the user's point of view — they are a member either way —
    # and an error here would only teach people to retry.
    existing = repo.get_membership(conn, org_id=invitation.org_id, user_id=user_id)
    if existing is not None:
        workspace = repo.get_organization(conn, invitation.org_id)
        if workspace is None:
            raise InvitationError("That workspace is no longer available.")
        return workspace

    status = invitation.status
    if status != "pending":
        record_event(
            conn,
            AuditEvent.INVITATION_ACCEPT_FAILED,
            outcome=AuditOutcome.DENIED,
            actor_user_id=user_id,
            org_id=invitation.org_id,
            target_type="invitation",
            target_id=invitation.invitation_id,
            request_id=request_id,
            ip_hash=hash_identifier(client_ip),
            details={"reason": status},
        )
        raise InvitationError(
            {
                "expired": "That invitation has expired. Ask for a new one.",
                "revoked": "That invitation has been withdrawn.",
                "accepted": "That invitation has already been used.",
            }.get(status, "That invitation is no longer valid.")
        )

    workspace = repo.get_organization(conn, invitation.org_id)
    if workspace is None:
        raise InvitationError("That workspace is no longer available.")

    # Consume first. If the membership write then failed, the invitation would
    # be spent and no membership created — recoverable by issuing a new
    # invitation. The other order would let two concurrent acceptances both
    # create a membership before either consumed the token.
    if not repo.consume_invitation(
        conn, invitation_id=invitation.invitation_id, user_id=user_id
    ):
        raise InvitationError("That invitation has already been used.")

    # A removed member keeps their row, so accepting a fresh invitation
    # reactivates it at the invited role rather than inserting a duplicate.
    rejoined = repo.reactivate_member(
        conn, org_id=invitation.org_id, user_id=user_id, role=invitation.role
    )
    if not rejoined:
        repo.add_member(
            conn, org_id=invitation.org_id, user_id=user_id, role=invitation.role
        )

    record_event(
        conn,
        AuditEvent.INVITATION_ACCEPTED,
        actor_user_id=user_id,
        actor_role=invitation.role.value,
        org_id=invitation.org_id,
        target_type="invitation",
        target_id=invitation.invitation_id,
        request_id=request_id,
        ip_hash=hash_identifier(client_ip),
        details={"role": invitation.role.value},
    )
    record_event(
        conn,
        AuditEvent.MEMBERSHIP_CREATED,
        actor_user_id=user_id,
        actor_role=invitation.role.value,
        org_id=invitation.org_id,
        target_type="user",
        target_id=user_id,
        request_id=request_id,
        details={
            "role": invitation.role.value,
            "reason": "invitation_accepted",
            **({"rejoined": True} if rejoined else {}),
        },
    )
    return workspace
