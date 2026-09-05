"use client";

import Link from "next/link";

import { MembersPanel } from "@/components/MembersPanel";
import { StatusPill } from "@/components/StatusPill";
import { EmptyState } from "@/components/ui/EmptyState";
import { Panel } from "@/components/ui/Panel";
import { useSession } from "@/app/providers";
import {
  orderPermissions,
  permissionLabel,
  roleLabel,
} from "@/lib/workspace-presentation";

import styles from "./workspace.module.css";

/**
 * Workspace: which tenant this is, who is in it, and what each of them may do.
 *
 * The word "workspace" is used throughout and the API's internal `org` /
 * `org_id` vocabulary never surfaces. A user should not have to learn that
 * their workspace is also an organization to understand a sentence about it.
 *
 * The identity block exists because "why can't I do this here?" was previously
 * an invisible difference: the same person can hold different roles in
 * different workspaces, and nothing on screen said which one was in force or
 * what it granted.
 */
export default function WorkspacePage() {
  const session = useSession();
  const workspace = session.activeWorkspace;

  if (!workspace || !session.user) {
    return (
      <main id="main" className={styles.page}>
        <EmptyState title="No workspace is active" headingLevel={2}>
          Choose a workspace from the switcher at the top of the page. If you
          belong to none, you will be asked to create one.
        </EmptyState>
      </main>
    );
  }

  const permissions = orderPermissions(workspace.permissions ?? []);

  return (
    <main id="main" className={styles.page}>
      <header className={styles.header}>
        <h1 className={styles.title}>{workspace.name}</h1>
        <p className={styles.subtitle}>
          Everything the assistant can reach belongs to this workspace — its
          accounts, orders, tickets, documents and policies, and every action it
          prepares. The separation between workspaces is enforced on the server,
          not in this browser.
        </p>
      </header>

      <Panel
        title="Your access"
        actions={
          // Offered, not enforced. The server refuses a caller without
          // `read_audit_log` whether or not this link is drawn.
          session.can("read_audit_log") ? (
            <Link className={styles.auditLink} href="/workspace/audit">
              View audit trail
            </Link>
          ) : null
        }
      >
        <dl className={styles.identity}>
          <div className={styles.fact}>
            <dt>Signed in as</dt>
            <dd>{session.user.display_name}</dd>
          </div>
          <div className={styles.fact}>
            <dt>Your role here</dt>
            <dd>
              {workspace.role ? (
                <StatusPill tone="neutral" quiet>
                  {roleLabel(workspace.role)}
                </StatusPill>
              ) : (
                "—"
              )}
            </dd>
          </div>
          <div className={styles.fact}>
            {/* The slug is the only stable, human-readable way to tell two
                workspaces with the same name apart. */}
            <dt>Workspace reference</dt>
            <dd className={styles.slug}>{workspace.slug}</dd>
          </div>
          <div className={styles.fact}>
            <dt>Accounts in scope</dt>
            <dd>
              {session.user.account_scope.length > 0
                ? session.user.account_scope.join(", ")
                : "None attached yet"}
            </dd>
          </div>
        </dl>
      </Panel>

      {permissions.length > 0 ? (
        <Panel
          className={styles.section}
          title="What your role grants"
          description="Issued by the server for this workspace. Every one of them is re-checked on each request, so this list describes what will be permitted, not what this page decided to show you."
        >
          <ul className={styles.permissions}>
            {permissions.map((permission) => (
              <li key={permission}>
                <StatusPill tone="neutral" quiet>
                  {permissionLabel(permission)}
                </StatusPill>
              </li>
            ))}
          </ul>
        </Panel>
      ) : null}

      {session.can("members.read") ? (
        <div className={styles.section}>
          <MembersPanel
            workspace={workspace}
            currentUserId={session.user.user_id}
            onChanged={() => void session.refresh()}
          />
        </div>
      ) : (
        <div className={styles.section}>
          <EmptyState title="You cannot see the member list" headingLevel={2}>
            Seeing who else is in this workspace needs the{" "}
            <code>members.read</code> permission, which your role does not
            grant. A workspace admin can change your role.
          </EmptyState>
        </div>
      )}
    </main>
  );
}
