"use client";

import Link from "next/link";

import { AuditTrail } from "@/components/audit/AuditTrail";
import { EmptyState } from "@/components/ui/EmptyState";
import { Panel } from "@/components/ui/Panel";
import { useSession } from "@/app/providers";

import styles from "../workspace.module.css";

/**
 * The workspace's audit trail.
 *
 * A place rather than a panel on an already-long page: reviewing what happened
 * is its own task, and it benefits from a URL a reader can send to someone.
 *
 * Nothing here is a security boundary. `AuditTrail` calls the endpoint
 * regardless of what this page believes, and the server refuses a caller
 * without `read_audit_log` — so a support user who types this URL gets the
 * backend's refusal, not a blank page that merely looks like one. The
 * permission check below decides what to *offer*, never what to permit.
 */
export default function AuditPage() {
  const session = useSession();
  const workspace = session.activeWorkspace;

  if (!workspace) {
    return (
      <main id="main" className={styles.page}>
        <EmptyState title="No workspace is active" headingLevel={2}>
          Choose a workspace from the switcher at the top of the page.
        </EmptyState>
      </main>
    );
  }

  return (
    <main id="main" className={styles.page}>
      <header className={styles.header}>
        <p className={styles.breadcrumb}>
          <Link href="/workspace">{workspace.name}</Link>
        </p>
        <h1 className={styles.title}>Audit trail</h1>
        <p className={styles.subtitle}>
          Every sign-in, every question asked of the assistant, and every action
          proposed, refused, rejected or executed in this workspace. Entries are
          append-only and each one commits to the one before it, so an altered
          trail is detectable rather than merely unlikely.
        </p>
      </header>

      <Panel title="Recorded events">
        <AuditTrail />
      </Panel>
    </main>
  );
}
