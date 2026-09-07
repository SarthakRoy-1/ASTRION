"use client";

import Image from "next/image";
import Link from "next/link";
import { usePathname } from "next/navigation";

import { ConnectionNotice } from "@/components/ConnectionNotice";
import { ErrorNotice } from "@/components/ErrorNotice";
import { WorkspaceSwitcher } from "@/components/WorkspaceSwitcher";
import { Button } from "@/components/ui/Button";
import { Callout } from "@/components/ui/Callout";
import { useChat, useSession } from "@/app/providers";

import styles from "./AppShell.module.css";

export function AppShell({ children }: { children: React.ReactNode }) {
  const session = useSession();
  const chat = useChat();
  const pathname = usePathname();
  const demo = session.stage === "demo";
  const areas = [
    { href: "/" as const, label: "Support", show: true },
    { href: "/operations" as const, label: "Operations", show: !demo && session.can("operations.read") },
    { href: "/workspace" as const, label: "Workspace", show: !demo },
  ].filter((area) => area.show);

  return (
    <div className={styles.app}>
      <a className="skip-link" href="#main">Skip to main content</a>
      <header className={styles.top}>
        <div className={styles.topInner}>
          <Link href="/" className={styles.brand} aria-label="ASTRION home">
            <Image src="/astrion-wordmark.svg" alt="ASTRION" width={210} height={36} priority />
          </Link>
          {session.activeWorkspace ? (
            <>
              <span className={styles.brandDivider} aria-hidden="true" />
              <div className={styles.workspace}>
                <WorkspaceSwitcher workspaces={session.workspaces} activeWorkspace={session.activeWorkspace} busy={session.busy} onSwitch={(id) => { chat.reset(); void session.switchWorkspace(id); }} />
              </div>
            </>
          ) : null}
          <div className={styles.spacer} />
          <div className={styles.account}>
            {session.user ? <span className={styles.accountName}>{session.user.display_name}</span> : null}
            {demo ? null : <Button variant="ghost" size="sm" onClick={() => void session.signOut()}>Sign out</Button>}
          </div>
        </div>
      </header>
      {areas.length > 1 ? (
        <nav className={styles.nav} aria-label="Primary">
          <div className={styles.navInner}>
            {areas.map((area) => {
              const active = area.href === "/" ? pathname === "/" : pathname?.startsWith(area.href);
              return <Link key={area.href} href={area.href} className={`${styles.navItem} ${active ? styles.navItemActive : ""}`} aria-current={active ? "page" : undefined}>{area.label}</Link>;
            })}
          </div>
        </nav>
      ) : null}
      <div className={styles.body}>
        <div className={styles.notices}>
          {session.error && !demo ? <Callout tone="fail" role="alert" title="Session problem">{session.error}</Callout> : null}
          {chat.principalsError && demo ? <ErrorNotice error={chat.principalsError} /> : <ConnectionNotice state={chat.connection} />}
          {demo ? <Callout tone="info" title="Demo identity mode">This deployment authenticates with a mock identity header, so there are no accounts, workspaces or members. Operations intelligence and workspace management need a real session.</Callout> : null}
        </div>
        {children}
      </div>
    </div>
  );
}
