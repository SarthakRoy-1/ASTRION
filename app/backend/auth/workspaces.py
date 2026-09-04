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
import sqlite3
import unicodedata

from app.backend.auth import repository as repo
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


class WorkspaceError(Exception):
    """A workspace operation was refused. The message is safe to show."""


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


# --- workspaces -------------------------------------------------------------


def create_workspace(
    conn: sqlite3.Connection,
    *,
    owner_user_id: str,
    name: str,
    account_ids: list[str] | None = None,
    request_id: str | None = None,
    client_ip: str | None = None,
) -> dict:
    """Create a workspace with its creator as owner.

    The creator becomes OWNER unconditionally — a workspace created with no
    owner would be immediately unmanageable, and there is nobody else to be one
    at this point.
    """
    cleaned = validate_name(name)

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
        details={"role": invitation.role.value, "reason": "invitation_accepted"},
    )
    return workspace
