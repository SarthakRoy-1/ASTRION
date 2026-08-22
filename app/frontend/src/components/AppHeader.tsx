"use client";

import { ContextSelector } from "./ContextSelector";
import type { PrincipalView } from "@/lib/types";

import styles from "./AppHeader.module.css";

const ROLE_LABELS: Record<string, string> = {
  customer: "Customer",
  support_agent: "Support agent",
  support_manager: "Support manager",
  read_only: "Read only",
};

function roleLabel(role: string): string {
  return ROLE_LABELS[role] ?? role.replace(/_/g, " ");
}

/**
 * The application bar: what this is, who you are, and what you can reach.
 *
 * The active context is stated in full — role *and* the accounts it covers —
 * because account isolation is a behaviour the product needs to make visible,
 * not just enforce. A demo that switches identity without the screen changing
 * proves nothing.
 */
export function AppHeader({
  principals,
  principal,
  identity,
  busy,
  canReset,
  onSelectIdentity,
  onReset,
}: {
  principals: PrincipalView[];
  principal: PrincipalView | null;
  identity: string | null;
  busy: boolean;
  canReset: boolean;
  onSelectIdentity: (userId: string) => void;
  onReset: () => void;
}) {
  const scope = principal?.account_scope ?? [];

  return (
    <header className={styles.header}>
      <div className={styles.inner}>
        <div className={styles.brand}>
          <h1 className={styles.title}>ParcelPilot</h1>
          <span className={styles.subtitle}>Support &amp; Operations Agent</span>
        </div>

        <div className={styles.controls}>
          <ContextSelector
            principals={principals}
            identity={identity}
            disabled={busy}
            onSelect={onSelectIdentity}
          />
          <button
            type="button"
            className={styles.reset}
            onClick={onReset}
            disabled={busy || !canReset}
          >
            New conversation
          </button>
        </div>
      </div>

      {principal && (
        <div className={styles.contextBar}>
          <span className={styles.contextRole}>{roleLabel(principal.role)}</span>
          <span className={styles.contextName}>{principal.display_name}</span>
          <span className={styles.contextDivider} aria-hidden="true">
            ·
          </span>
          <span className={styles.contextScope}>
            <span className={styles.scopeLabel}>Authorised accounts:</span>{" "}
            {scope.length > 0 ? scope.join(", ") : "none"}
          </span>
        </div>
      )}
    </header>
  );
}
