/**
 * Audit vocabulary, written for a person.
 *
 * The same rule as the other presentation modules: this only *names* things.
 * It never re-derives an outcome, never infers who did what, and never decides
 * whether the chain is intact — the server settles all three, and rendering is
 * all that happens here.
 *
 * The wire values are stable event identifiers (`action.executed`,
 * `authz.denied`). Putting them on screen unchanged is how an audit trail ends
 * up readable only by the people who wrote it.
 */

import type { PillTone } from "@/components/StatusPill";
import type { AuditEntry } from "./auth-types";

/** Broad groupings a reader actually filters by. */
export type AuditCategory = "actions" | "access" | "workspace" | "agent" | "other";

interface EventDescriptor {
  label: string;
  category: AuditCategory;
}

/**
 * How each recorded event reads.
 *
 * Deliberately phrased from the actor's side — "Confirmed and executed an
 * action", not "action.executed" — because the question a reader brings to an
 * audit trail is what somebody did, not what the system called it.
 */
const EVENTS: Record<string, EventDescriptor> = {
  // Actions
  "action.proposed": { label: "Prepared an action for confirmation", category: "actions" },
  "action.executed": { label: "Confirmed and executed an action", category: "actions" },
  "action.rejected": { label: "Rejected an action", category: "actions" },
  "action.confirmation_refused": {
    label: "Confirmation refused",
    category: "actions",
  },

  // Access and authorization
  "login.succeeded": { label: "Signed in", category: "access" },
  "login.failed": { label: "Sign-in failed", category: "access" },
  "login.locked_out": { label: "Account locked out", category: "access" },
  logout: { label: "Signed out", category: "access" },
  "user.registered": { label: "Created an account", category: "access" },
  "user.email_verified": { label: "Verified an email address", category: "access" },
  "user.password_changed": { label: "Changed a password", category: "access" },
  "user.password_reset_requested": {
    label: "Requested a password reset",
    category: "access",
  },
  "user.password_reset_completed": {
    label: "Completed a password reset",
    category: "access",
  },
  "mfa.enrolment_started": { label: "Started two-factor setup", category: "access" },
  "mfa.enabled": { label: "Enabled two-factor authentication", category: "access" },
  "mfa.disabled": { label: "Disabled two-factor authentication", category: "access" },
  "mfa.challenge_failed": { label: "Failed a two-factor challenge", category: "access" },
  "session.revoked": { label: "Revoked a session", category: "access" },
  "authz.denied": { label: "Refused: not permitted", category: "access" },
  "authz.tenant_denied": { label: "Refused: outside this workspace", category: "access" },

  // Workspace and membership
  "org.created": { label: "Created the workspace", category: "workspace" },
  "org.updated": { label: "Updated the workspace", category: "workspace" },
  "org.membership_created": { label: "Added a member", category: "workspace" },
  "org.membership_role_changed": { label: "Changed a member's role", category: "workspace" },
  "org.membership_removed": { label: "Removed a member", category: "workspace" },
  "invitation.created": { label: "Invited someone", category: "workspace" },
  "invitation.accepted": { label: "Accepted an invitation", category: "workspace" },
  "invitation.accept_failed": { label: "Invitation could not be accepted", category: "workspace" },
  "invitation.revoked": { label: "Revoked an invitation", category: "workspace" },

  // The agent
  "agent.invoked": { label: "Asked the assistant", category: "agent" },
  "operations.signals_viewed": { label: "Viewed operational signals", category: "agent" },
  "operations.signal_inspected": { label: "Inspected a signal", category: "agent" },

  // Abuse controls
  "abuse.rate_limited": { label: "Rate limited", category: "other" },
  "abuse.payload_rejected": { label: "Oversized request rejected", category: "other" },
};

/**
 * An unrecognised event is reported as itself, made readable, rather than
 * hidden. An audit trail that silently drops rows it has not been taught about
 * is worse than one that shows an ugly name.
 */
export function auditEventLabel(eventType: string): string {
  return (
    EVENTS[eventType]?.label ??
    eventType.replace(/[._]/g, " ").replace(/^./, (c) => c.toUpperCase())
  );
}

export function auditEventCategory(eventType: string): AuditCategory {
  return EVENTS[eventType]?.category ?? "other";
}

export const AUDIT_CATEGORY_LABELS: Record<AuditCategory, string> = {
  actions: "Actions",
  access: "Access",
  workspace: "Workspace",
  agent: "Assistant",
  other: "Other",
};

/**
 * How an outcome reads, and its tone.
 *
 * `denied` is not an error in the system's sense — it is the control working —
 * so it wears caution rather than failure. A refused confirmation is the event
 * a reviewer most wants to find, and colouring it like a crash would bury it
 * among genuine faults.
 */
export function auditOutcome(outcome: string): { label: string; tone: PillTone } {
  switch (outcome) {
    case "success":
      return { label: "Succeeded", tone: "ok" };
    case "denied":
      return { label: "Refused", tone: "caution" };
    case "failure":
      return { label: "Failed", tone: "fail" };
    default:
      return { label: outcome.replace(/_/g, " "), tone: "neutral" };
  }
}

/**
 * The detail lines worth showing beside an entry.
 *
 * Curated rather than dumped: `details` carries whatever the recording call
 * site passed, and rendering the raw object turns a readable trail back into
 * JSON. Values are stringified but never re-interpreted — the backend already
 * redacted anything credential-shaped before the row was written.
 */
export function auditDetails(entry: AuditEntry): { label: string; value: string }[] {
  const shown: { label: string; value: string }[] = [];

  for (const [key, value] of Object.entries(entry.details ?? {})) {
    if (value === null || value === undefined || value === "") continue;
    if (Array.isArray(value) && value.length === 0) continue;
    shown.push({
      label: key.replace(/[._]/g, " ").replace(/^./, (c) => c.toUpperCase()),
      value: readableValue(value),
    });
  }
  return shown;
}

/** How many members of a list are worth reading in a summary row. */
const LIST_PREVIEW = 3;

/**
 * One recorded value, as a phrase rather than as JSON.
 *
 * Long lists are summarised. `agent.invoked` records every evidence chunk it
 * read, and rendering all of them turns one entry into eight lines of
 * identifiers that push the next four events off the screen. The count is
 * what a reader acts on; nothing is hidden that the entry did not already
 * say, because the total is stated.
 */
function readableValue(value: unknown): string {
  if (typeof value === "string") return value;
  if (Array.isArray(value)) {
    const parts = value.map((item) =>
      typeof item === "string" ? item : JSON.stringify(item),
    );
    if (parts.length <= LIST_PREVIEW) return parts.join(", ");
    const remaining = parts.length - LIST_PREVIEW;
    return `${parts.slice(0, LIST_PREVIEW).join(", ")} and ${remaining} more`;
  }
  return JSON.stringify(value);
}

/** The dataset-independent timestamp of a recorded event, formatted. */
export function formatAuditTime(iso: string): string {
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return iso;
  return parsed.toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "medium",
  });
}
