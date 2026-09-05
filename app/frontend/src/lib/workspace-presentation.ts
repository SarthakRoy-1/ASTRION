/**
 * Workspace, role and permission vocabulary, written for a person.
 *
 * The same rule as `presentation.ts`: this module only *names* things. It
 * never decides what a role may do — every permission shown here came from the
 * server's own `permissions` array, and the server re-checks each one at the
 * point of use regardless of what this file calls it.
 *
 * Two vocabulary rules the product holds to:
 *
 * - **"Workspace", never "organization".** The API calls the entity an `org`
 *   internally and the database column is `org_id`; neither word appears in
 *   the interface. A user should not have to learn that their workspace is
 *   also an org to understand a sentence about it.
 * - **No raw enum values on screen.** `viewer`, `pending`, `sla_risk` and
 *   `affected_records` are wire values. Rendering them directly is how an
 *   interface ends up reading like a database dump.
 */

import type { InvitationStatus, WorkspaceRole } from "./auth-types";

const ROLE_LABELS: Record<WorkspaceRole, string> = {
  viewer: "Viewer",
  support: "Support",
  operations: "Operations",
  admin: "Admin",
  owner: "Owner",
};

export function roleLabel(role: WorkspaceRole | string): string {
  return ROLE_LABELS[role as WorkspaceRole] ?? titleCase(role);
}

const INVITATION_STATUS_LABELS: Record<InvitationStatus, string> = {
  pending: "Awaiting acceptance",
  accepted: "Accepted",
  revoked: "Revoked",
  expired: "Expired",
};

export function invitationStatusLabel(status: InvitationStatus | string): string {
  return INVITATION_STATUS_LABELS[status as InvitationStatus] ?? titleCase(status);
}

/**
 * What each permission lets a person do, in a sentence.
 *
 * Only permissions this deployment actually issues are named. An unrecognised
 * one falls back to its identifier made readable rather than being dropped: a
 * capability the UI has not been taught about is still a capability the holder
 * has, and hiding it would understate what their role grants.
 */
const PERMISSION_LABELS: Record<string, string> = {
  run_agent: "Ask the assistant questions",
  read_records: "Look up orders, tickets and accounts",
  read_documents: "Read the workspace's documents",
  propose_action: "Have the assistant prepare actions",
  execute_action: "Confirm and execute prepared actions",
  "operations.read": "See operational signals",
  read_audit_log: "Read the audit trail",
  "members.read": "See who is in the workspace",
  "members.invite": "Invite people",
  "members.change_role": "Change members' roles",
  "members.remove": "Remove members",
  "workspace.read": "See this workspace",
  "workspace.update": "Rename the workspace",
  manage_rules: "Manage policy rules",
  "ownership.transfer": "Transfer ownership",
  "workspace.delete": "Delete the workspace",
};

export function permissionLabel(permission: string): string {
  return PERMISSION_LABELS[permission] ?? titleCase(permission.replace(/[._]/g, " "));
}

/**
 * Permissions ordered so the list reads from "what you do daily" downward.
 *
 * The declaration order above *is* the ordering, which keeps the two from
 * drifting apart — and the names are the server's own, taken from
 * `app/backend/auth/permissions.py`. Inventing plausible-looking ones instead
 * meant almost every permission fell through to the identifier fallback, and
 * `execute_action` — the one that decides whether a confirmation button is
 * offered at all — was never matched.
 */
const PERMISSION_ORDER = Object.keys(PERMISSION_LABELS);

export function orderPermissions(permissions: readonly string[]): string[] {
  return [...permissions].sort((a, b) => {
    const left = PERMISSION_ORDER.indexOf(a);
    const right = PERMISSION_ORDER.indexOf(b);
    // Anything unrecognised sorts last rather than first, so a new permission
    // the UI has not been taught about does not lead the list.
    return (left === -1 ? 999 : left) - (right === -1 ? 999 : right);
  });
}

function titleCase(value: string): string {
  return value.replace(/[_-]/g, " ").replace(/^./, (c) => c.toUpperCase());
}
