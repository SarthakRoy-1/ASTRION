"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { ConnectionNotice } from "@/components/ConnectionNotice";
import { ErrorNotice } from "@/components/ErrorNotice";
import { WorkspaceSwitcher } from "@/components/WorkspaceSwitcher";
import { Button } from "@/components/ui/Button";
import { Callout } from "@/components/ui/Callout";
import { useChat, useSession } from "@/app/providers";

import styles from "./AppShell.module.css";

/**
 * The product's frame: which workspace you are in, where you can go, and who
 * you are — in that order, once, at the top of every screen.
 *
 * Two bands rather than one. The first answers *which tenant am I acting in*,
 * which is the question a multi-workspace product must never let a user get
 * wrong; the second answers *where am I*. Merging them is what produced the
 * previous header, where a workspace name, a role badge, two navigation
 * buttons and a sign-out link shared one row and overlapped each other on a
 * phone.
 *
 * Navigation is hidden where the caller's role does not grant the area — a
 * rendering decision only, and the server refuses the underlying requests
 * regardless of what this component draws.
 */
export function AppShell({ children }: { children: React.ReactNode }) {
  const session = useSession();
  const chat = useChat();
  const pathname = usePathname();

  const demo = session.stage === "demo";
  // Operations and workspace management both require a real workspace session:
  // the demo personas belong to no workspace, and the API says so plainly
  // rather than returning an empty list that would read as "nothing is wrong".
  const areas = [
    { href: "/" as const, label: "Support", show: true },
    { href: "/operations" as const, label: "Operations", show: !demo && session.can("operations.read") },
    { href: "/workspace" as const, label: "Workspace", show: !demo },
  ].filter((area) => area.show);

  return (
    <div className={styles.app}>
      <a className="skip-link" href="#main">
        Skip to main content
      </a>

      <header className={styles.top}>
        <div className={styles.topInner}>
          <div className={styles.brand}>
            <span className={styles.brandName}>ASTRION</span>
            <span className={styles.brandRole}>Support &amp; Operations</span>
          </div>

          {session.activeWorkspace ? (
            <>
              <span className={styles.brandDivider} aria-hidden="true" />
              <div className={styles.workspace}>
                <WorkspaceSwitcher
                  workspaces={session.workspaces}
                  activeWorkspace={session.activeWorkspace}
                  busy={session.busy}
                  onSwitch={(id) => {
                    // A workspace switch changes which tenant's data the
                    // answers came from, so the transcript on screen no longer
                    // belongs to the workspace now selected.
                    chat.reset();
                    void session.switchWorkspace(id);
                  }}
                />
              </div>
            </>
          ) : null}

          <div className={styles.spacer} />

          <div className={styles.account}>
            {session.user ? (
              <span className={styles.accountName}>{session.user.display_name}</span>
            ) : null}
            {demo ? null : (
              <Button
                variant="ghost"
                size="sm"
                onClick={() => void session.signOut()}
              >
                Sign out
              </Button>
            )}
          </div>
        </div>
      </header>

      {areas.length > 1 ? (
        <nav className={styles.nav} aria-label="Primary">
          <div className={styles.navInner}>
            {areas.map((area) => {
              const active =
                area.href === "/"
                  ? pathname === "/"
                  : pathname?.startsWith(area.href);
              return (
                <Link
                  key={area.href}
                  href={area.href}
                  className={`${styles.navItem} ${active ? styles.navItemActive : ""}`}
                  aria-current={active ? "page" : undefined}
                >
                  {area.label}
                </Link>
              );
            })}
          </div>
        </nav>
      ) : null}

      <div className={styles.body}>
        <div className={styles.notices}>
          {/* Rendered as a callout rather than through `ErrorNotice`, which
              takes an `ApiError` and branches on its structured code. A session
              error is already a resolved, user-safe message; wrapping it in a
              synthetic error object would fake a code the backend never sent. */}
          {session.error && !demo ? (
            <Callout tone="fail" role="alert" title="Session problem">
              {session.error}
            </Callout>
          ) : null}

          {chat.principalsError && demo ? (
            <ErrorNotice error={chat.principalsError} />
          ) : (
            <ConnectionNotice state={chat.connection} />
          )}

          {demo ? (
            <Callout tone="info" title="Demo identity mode">
              This deployment authenticates with a mock identity header, so
              there are no accounts, workspaces or members. Operations
              intelligence and workspace management need a real session.
            </Callout>
          ) : null}
        </div>

        {children}
      </div>
    </div>
  );
}
