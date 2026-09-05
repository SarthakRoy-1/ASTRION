"use client";

import Link from "next/link";

import { ContextSelector } from "@/components/ContextSelector";
import { StatusPill } from "@/components/StatusPill";
import {
  orderPermissions,
  permissionLabel,
  roleLabel,
} from "@/lib/workspace-presentation";
import type { HealthResponse, PrincipalView } from "@/lib/types";
import type { Workspace } from "@/lib/auth-types";
import type { CurrentUser } from "@/lib/auth-types";

import styles from "./ContextRail.module.css";

/**
 * The capabilities that bear on a support conversation.
 *
 * A cut by *relevance*, not by count. An owner holds sixteen permissions, and
 * listing all of them beside a transcript is noise that stops being read —
 * but "can I confirm the action this answer just prepared?" is exactly what a
 * reader is here to know, so it must never be the one that falls off the end.
 * Everything administrative belongs on the Workspace screen, which the link
 * below goes to.
 */
const SUPPORT_PERMISSIONS: readonly string[] = [
  "run_agent",
  "read_records",
  "read_documents",
  "propose_action",
  "execute_action",
  "operations.read",
  "read_audit_log",
];

/**
 * Who is asking, what they may reach, and what this deployment is running.
 *
 * The three questions a support agent needs answered before they trust an
 * answer, kept beside the transcript instead of scattered across a header
 * strip, a status line and nowhere. Every value comes from the server —
 * `/api/auth/me` for the scope, the workspace listing for the role and
 * permissions, `/health` for the deployment — and nothing here is computed in
 * the browser.
 *
 * The accounts strip matters more than it looks. An answer that says an order
 * "was not found" means something quite different depending on whether the
 * account it belongs to is in scope at all, and that list is the only place
 * the reader can check.
 */
export function ContextRail({
  demo,
  user,
  workspace,
  principal,
  principals,
  identity,
  busy,
  health,
  onSelectIdentity,
}: {
  demo: boolean;
  user: CurrentUser | null;
  workspace: Workspace | null;
  principal: PrincipalView | null;
  principals: PrincipalView[];
  identity: string | null;
  busy: boolean;
  health: HealthResponse | null;
  onSelectIdentity: (userId: string) => void;
}) {
  // Under demo authentication the persona *is* the identity, and its scope and
  // capabilities come from the server's principal directory rather than from a
  // workspace membership.
  const scope = demo ? (principal?.account_scope ?? []) : (user?.account_scope ?? []);
  const granted = demo ? [] : orderPermissions(workspace?.permissions ?? []);
  const permissions = granted.filter((permission) =>
    SUPPORT_PERMISSIONS.includes(permission),
  );
  const elsewhere = granted.length - permissions.length;

  return (
    <div className={styles.rail}>
      <section className={styles.block} aria-label="Acting as">
        <h2 className={styles.blockTitle}>Acting as</h2>

        {demo ? (
          <>
            <ContextSelector
              principals={principals}
              identity={identity}
              disabled={busy}
              onSelect={onSelectIdentity}
            />
            {principal?.description ? (
              <p className={styles.note}>{principal.description}</p>
            ) : null}
          </>
        ) : (
          <>
            <div className={styles.identity}>
              <span className={styles.name}>{user?.display_name ?? "—"}</span>
              {workspace?.role ? (
                <StatusPill tone="neutral" quiet>
                  {roleLabel(workspace.role)}
                </StatusPill>
              ) : null}
            </div>
            <p className={styles.note}>
              {workspace
                ? `In ${workspace.name}. Your role decides what you can do here, and the same person can hold a different role in another workspace.`
                : "No workspace is active."}
            </p>
          </>
        )}
      </section>

      {permissions.length > 0 ? (
        <section className={styles.block} aria-label="What you can do here">
          <h2 className={styles.blockTitle}>What you can do here</h2>
          <ul className={styles.list}>
            {/* Only what bears on a conversation. See SUPPORT_PERMISSIONS. */}
            {permissions.map((permission) => (
              <li key={permission} className={styles.listItem}>
                <span className={styles.mark} aria-hidden="true" />
                <span>{permissionLabel(permission)}</span>
              </li>
            ))}
          </ul>
          {elsewhere > 0 ? (
            <Link className={styles.more} href="/workspace">
              {elsewhere} more, for managing the workspace — see Workspace
            </Link>
          ) : null}
        </section>
      ) : null}

      <section className={styles.block} aria-label="Accounts in scope">
        <h2 className={styles.blockTitle}>Accounts in scope</h2>
        {scope.length > 0 ? (
          <div className={styles.accounts}>
            {scope.map((account) => (
              <span key={account} className={styles.account}>
                {account}
              </span>
            ))}
          </div>
        ) : (
          <p className={styles.note}>
            None. The assistant can answer from policy documents, but it will
            not find an order, ticket or account — because none has been
            attached to this workspace on the server.
          </p>
        )}
      </section>

      {health ? (
        <section className={styles.block} aria-label="This deployment">
          <h2 className={styles.blockTitle}>This deployment</h2>
          <dl className={styles.facts}>
            <div className={styles.fact}>
              <dt>Engine</dt>
              <dd>
                {health.provider_mode === "deterministic"
                  ? "Deterministic — no model API key"
                  : health.model
                    ? `Model-assisted (${health.model})`
                    : "Model-assisted"}
              </dd>
            </div>
            {health.documents_indexed > 0 ? (
              <div className={styles.fact}>
                <dt>Documents indexed</dt>
                <dd>{health.documents_indexed}</dd>
              </div>
            ) : null}
            {health.dataset_snapshot ? (
              <div className={styles.fact}>
                <dt>Judged against</dt>
                {/* Every answer is stamped with this date, which reads as a bug
                    until you know the system judges business decisions against
                    a fixed dataset snapshot rather than the wall clock. */}
                <dd>{health.dataset_snapshot}</dd>
              </div>
            ) : null}
            <div className={styles.fact}>
              <dt>State-changing actions</dt>
              <dd>
                {health.state_changing_actions_enabled
                  ? "Enabled, behind confirmation"
                  : "Disabled in this deployment"}
              </dd>
            </div>
          </dl>
        </section>
      ) : null}
    </div>
  );
}
