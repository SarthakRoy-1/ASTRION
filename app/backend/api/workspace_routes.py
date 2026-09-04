"""Workspace, membership and invitation endpoints.

**Workspace is the product term for what the schema calls an organisation.**
One entity, two names: `Workspace` is what a person reads, `org_id` is what the
column is called. There is deliberately no second concept.

Every route here takes the workspace in its *path*, not from the session. That
is a deliberate design choice and it is the reason `_require` exists: a user
may belong to several workspaces, and asking them to "switch" before they can
list the members of another one would be hostile. What makes it safe is that
the path id is never trusted — it is resolved to a membership row for the
authenticated user on every single request, and a workspace the caller does not
belong to is reported as **absent**, exactly as an out-of-scope record is
elsewhere in this API.

    path workspace_id  ──>  membership for THIS user  ──>  permission  ──>  act
                            (404 if none)                  (403 if not)

The active workspace on the session (`sessions.org_id`) is a separate thing: it
decides which tenant the *agent* runs against. It is changed only through
`POST /api/workspaces/{id}/activate`, which re-checks membership before writing
it, so a client can never point the agent at a tenant it does not belong to.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from app.backend.api.authentication import AuthenticatedCaller, audit_denial, authenticate
from app.backend.api.dependencies import DbDep
from app.backend.api.ratelimit import client_address
from app.backend.auth import repository as repo
from app.backend.auth import workspaces as workspace_service
from app.backend.auth.permissions import OrgRole, Permission, parse_role, permissions_for
from app.backend.core.config import AuthMode, Settings
from app.backend.core.errors import (
    AuthorizationError,
    InvalidRequestError,
    NotFoundError,
)
from app.backend.services.audit import AuditEvent, record_event

workspace_router = APIRouter(tags=["workspaces"])

MAX_NAME = 120
MAX_EMAIL = 320
MAX_ID = 64


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _reject_in_demo_mode(settings: Settings) -> None:
    """Workspaces require a real identity to belong to.

    In demo mode the caller is a persona from a fixed directory with no user
    row, so there is nothing a membership could reference. Refusing plainly is
    better than creating half a workspace owned by a fiction.
    """
    if settings.auth_mode is AuthMode.DEMO_HEADER:
        raise AuthorizationError(
            "This deployment runs in demo identity mode; workspace management "
            "is disabled. Set AUTH_MODE=session to enable it."
        )


def _caller(request: Request, conn: sqlite3.Connection) -> AuthenticatedCaller:
    settings = _settings(request)
    _reject_in_demo_mode(settings)
    return authenticate(request, conn, settings)


def _require(
    request: Request,
    conn: sqlite3.Connection,
    workspace_id: str,
    permission: Permission,
) -> tuple[AuthenticatedCaller, repo.Membership]:
    """Resolve the caller's membership of *this* workspace and check permission.

    The single authorization gate for every route below, and the reason none of
    them repeats the logic.

    A non-member gets **404, not 403**. A 403 would confirm that the workspace
    id is real, letting anyone enumerate other tenants by watching which ids
    answer differently. The same reasoning governs records, documents and
    actions elsewhere in this API: absence is not authorization, and refusing
    to distinguish "yours and forbidden" from "not yours" is what keeps the API
    from becoming an existence oracle.
    """
    caller = _caller(request, conn)
    membership = repo.get_membership(
        conn, org_id=workspace_id, user_id=caller.user_id
    )
    if membership is None:
        audit_denial(
            conn,
            caller,
            permission=permission,
            request_id=getattr(request.state, "request_id", None),
            client_ip=client_address(request),
            detail=f"not a member of {workspace_id}",
        )
        raise NotFoundError("That workspace was not found.")

    if not membership.has(permission):
        audit_denial(
            conn,
            caller,
            permission=permission,
            request_id=getattr(request.state, "request_id", None),
            client_ip=client_address(request),
            detail=f"workspace {workspace_id}",
        )
        raise AuthorizationError(
            f"This action requires the {permission.value!r} permission, which "
            f"your role in this workspace does not grant."
        )
    return caller, membership


# --- request models ---------------------------------------------------------


class CreateWorkspaceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=MAX_NAME)


class UpdateWorkspaceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=MAX_NAME)


class InviteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=MAX_EMAIL)
    role: str = Field(min_length=1, max_length=32)


class ChangeRoleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str = Field(min_length=1, max_length=32)


class TransferOwnershipRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=1, max_length=MAX_ID)


class AcceptInvitationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Carried in the body, never in the path or a query string. A token in a
    #: URL is written to browser history, sent in `Referer` to any third party
    #: the next page loads from, and recorded in every access log between the
    #: client and here.
    token: str = Field(min_length=1, max_length=512)


# --- serialisation ----------------------------------------------------------


def _workspace_view(workspace: dict, membership: repo.Membership | None) -> dict:
    view = {
        "workspace_id": workspace["org_id"],
        "name": workspace["name"],
        "slug": workspace["slug"],
        "created_at_utc": workspace["created_at_utc"],
    }
    if membership is not None:
        view["role"] = membership.role.value
        # Sent so the UI can hide what the caller cannot do. This is a
        # *rendering* hint and never an authorization decision: every
        # permission listed is re-checked server-side at the point of use.
        view["permissions"] = sorted(p.value for p in permissions_for(membership.role))
    return view


def _invitation_view(invitation: repo.Invitation) -> dict:
    return {
        "invitation_id": invitation.invitation_id,
        "email": invitation.email,
        "role": invitation.role.value,
        "status": invitation.status,
        "invited_by": invitation.invited_by,
        "created_at_utc": invitation.created_at.isoformat(),
        "expires_at_utc": invitation.expires_at.isoformat(),
        # No token, and no token digest. Neither is any use to a client, and
        # the digest is the stored credential.
    }


# --- workspaces -------------------------------------------------------------


@workspace_router.get("/api/workspaces")
def list_workspaces(request: Request, conn: sqlite3.Connection = DbDep) -> dict:
    """Every workspace the caller belongs to, and their role in each.

    Derived from membership rows, so it lists what the caller can actually
    reach rather than what they asked about.
    """
    caller = _caller(request, conn)
    memberships = repo.list_memberships(conn, caller.user_id)

    items = []
    for membership in memberships:
        workspace = repo.get_organization(conn, membership.org_id)
        if workspace is not None:
            items.append(_workspace_view(workspace, membership))

    return {
        "workspaces": items,
        "active_workspace_id": caller.org_id,
        # The onboarding signal. A user with no workspace is not in an error
        # state; they are at the beginning, and the UI needs to tell those apart.
        "needs_workspace": not items,
    }


@workspace_router.post("/api/workspaces", status_code=201)
def create_workspace(
    request: Request, payload: CreateWorkspaceRequest, conn: sqlite3.Connection = DbDep
) -> dict:
    """Create a workspace. The caller becomes its owner.

    Any authenticated user may create one — this is the path out of the
    no-workspace onboarding state, so gating it on a permission would leave a
    new account with no way forward. `MAX_WORKSPACES_PER_USER` is the ceiling.
    """
    caller = _caller(request, conn)
    try:
        workspace = workspace_service.create_workspace(
            conn,
            owner_user_id=caller.user_id,
            name=payload.name,
            request_id=getattr(request.state, "request_id", None),
            client_ip=client_address(request),
        )
    except workspace_service.WorkspaceError as exc:
        raise InvalidRequestError(str(exc)) from exc

    membership = repo.get_membership(
        conn, org_id=workspace["org_id"], user_id=caller.user_id
    )
    # A user creating their first workspace should land in it, not have to
    # switch to it. Only when they have no active one, so creating a second
    # workspace never silently moves someone out of the one they were using.
    if caller.auth_session_id and caller.org_id is None:
        repo.set_session_org(conn, caller.auth_session_id, workspace["org_id"])

    return _workspace_view(workspace, membership)


@workspace_router.get("/api/workspaces/{workspace_id}")
def get_workspace(
    request: Request, workspace_id: str, conn: sqlite3.Connection = DbDep
) -> dict:
    _caller_, membership = _require(
        request, conn, workspace_id, Permission.WORKSPACE_READ
    )
    workspace = repo.get_organization(conn, workspace_id)
    if workspace is None:
        raise NotFoundError("That workspace was not found.")
    return _workspace_view(workspace, membership)


@workspace_router.patch("/api/workspaces/{workspace_id}")
def update_workspace(
    request: Request,
    workspace_id: str,
    payload: UpdateWorkspaceRequest,
    conn: sqlite3.Connection = DbDep,
) -> dict:
    caller, membership = _require(
        request, conn, workspace_id, Permission.WORKSPACE_UPDATE
    )
    try:
        workspace = workspace_service.rename_workspace(
            conn,
            org_id=workspace_id,
            name=payload.name,
            actor_user_id=caller.user_id,
            actor_role=membership.role.value,
            request_id=getattr(request.state, "request_id", None),
        )
    except workspace_service.WorkspaceError as exc:
        raise InvalidRequestError(str(exc)) from exc
    return _workspace_view(workspace, membership)


@workspace_router.post("/api/workspaces/{workspace_id}/activate")
def activate_workspace(
    request: Request, workspace_id: str, conn: sqlite3.Connection = DbDep
) -> dict:
    """Make this the workspace the agent and the tenant-scoped APIs run against.

    Written to the *session row*, never held in the request. That is what stops
    a client selecting a tenant per-request: there is no field to put it in,
    and this endpoint re-checks membership before writing.
    """
    caller, membership = _require(
        request, conn, workspace_id, Permission.WORKSPACE_READ
    )
    if caller.auth_session_id is None:
        raise AuthorizationError("Switching workspace requires a real session.")

    repo.set_session_org(conn, caller.auth_session_id, workspace_id)
    record_event(
        conn,
        AuditEvent.WORKSPACE_ACTIVATED,
        actor_user_id=caller.user_id,
        actor_role=membership.role.value,
        org_id=workspace_id,
        request_id=getattr(request.state, "request_id", None),
    )
    workspace = repo.get_organization(conn, workspace_id)
    assert workspace is not None
    return {"status": "activated", **_workspace_view(workspace, membership)}


# --- members ----------------------------------------------------------------


@workspace_router.get("/api/workspaces/{workspace_id}/members")
def list_members(
    request: Request, workspace_id: str, conn: sqlite3.Connection = DbDep
) -> dict:
    _caller_, _m = _require(request, conn, workspace_id, Permission.MEMBERS_READ)
    return {
        "workspace_id": workspace_id,
        "members": repo.list_org_members(conn, workspace_id),
        "owner_count": repo.count_owners(conn, workspace_id),
    }


@workspace_router.patch("/api/workspaces/{workspace_id}/members/{member_user_id}")
def change_member_role(
    request: Request,
    workspace_id: str,
    member_user_id: str,
    payload: ChangeRoleRequest,
    conn: sqlite3.Connection = DbDep,
) -> dict:
    caller, membership = _require(
        request, conn, workspace_id, Permission.MEMBERS_CHANGE_ROLE
    )
    role = parse_role(payload.role)
    if role is None:
        raise InvalidRequestError(
            f"role must be one of: {', '.join(r.value for r in OrgRole)}"
        )
    try:
        workspace_service.change_member_role(
            conn,
            org_id=workspace_id,
            target_user_id=member_user_id,
            role=role,
            actor_user_id=caller.user_id,
            actor_role=membership.role,
            request_id=getattr(request.state, "request_id", None),
        )
    except workspace_service.WorkspaceError as exc:
        raise AuthorizationError(str(exc)) from exc
    return {"status": "role_updated", "user_id": member_user_id, "role": role.value}


@workspace_router.delete("/api/workspaces/{workspace_id}/members/{member_user_id}")
def remove_member(
    request: Request,
    workspace_id: str,
    member_user_id: str,
    conn: sqlite3.Connection = DbDep,
) -> dict:
    """Remove a member, or leave the workspace yourself.

    Leaving needs no `members.remove` permission — anyone may leave — so the
    gate is chosen from who the target is.
    """
    caller = _caller(request, conn)
    membership = repo.get_membership(
        conn, org_id=workspace_id, user_id=caller.user_id
    )
    if membership is None:
        raise NotFoundError("That workspace was not found.")

    leaving = member_user_id == caller.user_id
    if not leaving and not membership.has(Permission.MEMBERS_REMOVE):
        audit_denial(
            conn,
            caller,
            permission=Permission.MEMBERS_REMOVE,
            request_id=getattr(request.state, "request_id", None),
            client_ip=client_address(request),
            detail=f"remove {member_user_id} from {workspace_id}",
        )
        raise AuthorizationError(
            "This action requires the 'members.remove' permission, which your "
            "role in this workspace does not grant."
        )

    try:
        workspace_service.remove_member(
            conn,
            org_id=workspace_id,
            target_user_id=member_user_id,
            actor_user_id=caller.user_id,
            actor_role=membership.role,
            request_id=getattr(request.state, "request_id", None),
        )
    except workspace_service.WorkspaceError as exc:
        raise AuthorizationError(str(exc)) from exc

    # Someone who just left must not keep acting in the workspace they left.
    if leaving and caller.auth_session_id and caller.org_id == workspace_id:
        repo.set_session_org(conn, caller.auth_session_id, None)

    return {"status": "removed", "user_id": member_user_id, "left": leaving}


@workspace_router.post("/api/workspaces/{workspace_id}/ownership")
def transfer_ownership(
    request: Request,
    workspace_id: str,
    payload: TransferOwnershipRequest,
    conn: sqlite3.Connection = DbDep,
) -> dict:
    """Hand the workspace to another member. Owner only; the actor becomes admin."""
    caller, _m = _require(
        request, conn, workspace_id, Permission.OWNERSHIP_TRANSFER
    )
    try:
        workspace_service.transfer_ownership(
            conn,
            org_id=workspace_id,
            to_user_id=payload.user_id,
            actor_user_id=caller.user_id,
            request_id=getattr(request.state, "request_id", None),
        )
    except workspace_service.WorkspaceError as exc:
        raise InvalidRequestError(str(exc)) from exc
    return {"status": "ownership_transferred", "new_owner_user_id": payload.user_id}


# --- invitations ------------------------------------------------------------


def _may_disclose_token(settings: Settings) -> bool:
    """Whether the invitation token may be returned in the response.

    Never in production. This deployment has no mail transport, so the token
    has to reach a developer somehow; returning it is acceptable on a laptop
    and is credential disclosure anywhere else. When it is withheld, the
    invitation still exists and can be delivered by whatever channel is wired
    up later — this is the integration boundary, not a pretence that mail works.
    """
    return not settings.is_production


@workspace_router.get("/api/workspaces/{workspace_id}/invitations")
def list_invitations(
    request: Request,
    workspace_id: str,
    conn: sqlite3.Connection = DbDep,
    include_settled: bool = False,
) -> dict:
    _caller_, _m = _require(request, conn, workspace_id, Permission.MEMBERS_INVITE)
    invitations = repo.list_invitations(
        conn, workspace_id, include_settled=include_settled
    )
    return {
        "workspace_id": workspace_id,
        "invitations": [_invitation_view(i) for i in invitations],
    }


@workspace_router.post("/api/workspaces/{workspace_id}/invitations", status_code=201)
def create_invitation(
    request: Request,
    workspace_id: str,
    payload: InviteRequest,
    conn: sqlite3.Connection = DbDep,
) -> dict:
    caller, membership = _require(
        request, conn, workspace_id, Permission.MEMBERS_INVITE
    )
    role = parse_role(payload.role)
    if role is None:
        raise InvalidRequestError(
            f"role must be one of: {', '.join(r.value for r in OrgRole)}"
        )
    try:
        invitation, token = workspace_service.invite_member(
            conn,
            org_id=workspace_id,
            email=payload.email,
            role=role,
            actor_user_id=caller.user_id,
            actor_role=membership.role,
            request_id=getattr(request.state, "request_id", None),
            client_ip=client_address(request),
        )
    except workspace_service.InvitationError as exc:
        raise InvalidRequestError(str(exc)) from exc

    body = _invitation_view(invitation)
    if _may_disclose_token(_settings(request)):
        body["invitation_token"] = token
        body["note"] = (
            "This deployment has no mail transport, so the token is returned "
            "here. It is withheld when APP_ENV is production."
        )
    return body


@workspace_router.delete(
    "/api/workspaces/{workspace_id}/invitations/{invitation_id}"
)
def revoke_invitation(
    request: Request,
    workspace_id: str,
    invitation_id: str,
    conn: sqlite3.Connection = DbDep,
) -> dict:
    caller, membership = _require(
        request, conn, workspace_id, Permission.MEMBERS_INVITE
    )
    try:
        workspace_service.revoke_invitation(
            conn,
            org_id=workspace_id,
            invitation_id=invitation_id,
            actor_user_id=caller.user_id,
            actor_role=membership.role,
            request_id=getattr(request.state, "request_id", None),
        )
    except workspace_service.InvitationError as exc:
        raise NotFoundError(str(exc)) from exc
    return {"status": "revoked", "invitation_id": invitation_id}


@workspace_router.post("/api/invitations/accept")
def accept_invitation(
    request: Request, payload: AcceptInvitationRequest, conn: sqlite3.Connection = DbDep
) -> dict:
    """Redeem an invitation into a membership.

    Requires an authenticated account, because the invited address is checked
    against the *authenticated* user's own — which they cannot choose. That is
    what makes a leaked link useless to whoever finds it.
    """
    caller = _caller(request, conn)
    try:
        workspace = workspace_service.accept_invitation(
            conn,
            token=payload.token,
            user_id=caller.user_id,
            request_id=getattr(request.state, "request_id", None),
            client_ip=client_address(request),
        )
    except workspace_service.InvitationError as exc:
        raise InvalidRequestError(str(exc)) from exc

    membership = repo.get_membership(
        conn, org_id=workspace["org_id"], user_id=caller.user_id
    )
    if caller.auth_session_id and caller.org_id is None:
        repo.set_session_org(conn, caller.auth_session_id, workspace["org_id"])
    return {"status": "joined", **_workspace_view(workspace, membership)}
