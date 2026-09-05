/**
 * Types for authentication, workspaces and membership.
 *
 * Kept beside `types.ts` rather than inside it: those types describe the
 * *conversation* contract, these describe who is having it and where. Mixing
 * them would make the chat contract look like it depended on tenancy, which it
 * deliberately does not — the backend resolves tenancy before the agent runs.
 *
 * **Nothing here is an authorization decision.** `permissions` exists so the UI
 * can hide a button the caller cannot use; every one of them is re-checked
 * server-side at the point of use, and a client that lies to itself about this
 * gains exactly nothing.
 */

/** A workspace role, in ascending order of authority. */
export type WorkspaceRole = "viewer" | "support" | "operations" | "admin" | "owner";

/** How this deployment establishes identity. */
export type AuthMode = "session" | "demo_header";

/** The signed-in user, as `GET /api/auth/me` reports them. */
export interface CurrentUser {
  user_id: string;
  display_name: string;
  org_id: string | null;
  org_name: string | null;
  role: string;
  permissions: string[];
  account_scope: string[];
  memberships: MembershipSummary[];
  auth_mode: AuthMode;
}

export interface MembershipSummary {
  org_id: string;
  org_name: string;
  role: WorkspaceRole;
}

/** One workspace the caller belongs to. */
export interface Workspace {
  workspace_id: string;
  name: string;
  slug: string;
  created_at_utc: string;
  /** Present whenever the caller is a member — which, in a listing, is always. */
  role?: WorkspaceRole;
  permissions?: string[];
}

export interface WorkspaceListing {
  workspaces: Workspace[];
  active_workspace_id: string | null;
  /**
   * True when the caller belongs to no workspace. An onboarding signal, not an
   * error: a new account is at the beginning, not in a broken state, and the
   * UI needs to tell those two apart.
   */
  needs_workspace: boolean;
}

export interface WorkspaceMember {
  user_id: string;
  email: string;
  display_name: string;
  role: WorkspaceRole;
  status: string;
}

export interface MemberListing {
  workspace_id: string;
  members: WorkspaceMember[];
  owner_count: number;
}

export type InvitationStatus = "pending" | "accepted" | "revoked" | "expired";

export interface Invitation {
  invitation_id: string;
  email: string;
  role: WorkspaceRole;
  status: InvitationStatus;
  invited_by: string;
  created_at_utc: string;
  expires_at_utc: string;
  /**
   * Returned **only** on creation and **only** outside production, because
   * this deployment has no mail transport. It is a credential; it is never
   * stored by the client and never appears in a listing.
   */
  invitation_token?: string;
  note?: string;
}

export interface LoginResult {
  status: "authenticated" | "mfa_required";
  mfa_required: boolean;
  user_id: string;
  org_id: string | null;
}

/** Roles an inviter may choose. `owner` is absent by design — ownership moves
 *  only through the audited transfer path, never through an invitation. */
export const ASSIGNABLE_ROLES: WorkspaceRole[] = [
  "viewer",
  "support",
  "operations",
  "admin",
];

export const ROLE_DESCRIPTIONS: Record<WorkspaceRole, string> = {
  viewer: "Read-only. Can ask questions and read evidence.",
  support: "Can investigate and draft actions for someone else to confirm.",
  operations: "Can confirm and execute actions, and read the audit trail.",
  admin: "Can manage members, invitations and workspace settings.",
  owner: "Full control, including ownership transfer.",
};

/**
 * One entry in the workspace's security audit trail.
 *
 * Mirrors what `GET /api/auth/audit` returns. Every field here has already
 * been through the backend's redaction pass — `details` cannot carry a
 * credential, because `services/audit.py` drops any key that names one before
 * the row is written. Nothing in this shape needs sanitising in the browser,
 * and nothing in it should be treated as safe to widen: the entries a caller
 * receives are the ones their own session's workspace produced.
 */
export interface AuditEntry {
  seq: number;
  event_id: string;
  occurred_at_utc: string;
  event_type: string;
  outcome: string;
  actor_user_id: string | null;
  actor_role: string | null;
  org_id: string | null;
  target_type: string | null;
  target_id: string | null;
  request_id: string | null;
  details: Record<string, unknown>;
}

export interface AuditListing {
  org_id: string;
  /**
   * Whether the hash chain still verifies.
   *
   * The endpoint computes this; the browser only reports it. A reader has to
   * be able to tell whether what they are looking at is intact, and a UI that
   * quietly rendered a broken chain as an ordinary list would be worse than
   * showing nothing.
   */
  chain_intact: boolean;
  first_invalid_seq: number | null;
  events: AuditEntry[];
}
