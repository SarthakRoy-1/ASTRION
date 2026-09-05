"use client";

import { useState } from "react";

import { Button } from "./ui/Button";
import { Callout } from "./ui/Callout";
import { TextField } from "./ui/Field";

import styles from "./WorkspaceOnboarding.module.css";

/**
 * What a signed-in user sees when they belong to no workspace.
 *
 * This is not an error state and must not read like one. A new account is at
 * the *beginning*: the backend reports `needs_workspace`, not a failure, and
 * the copy here explains what a workspace is before asking for a name.
 *
 * A user who was invited rather than signing up arrives here too, so the panel
 * also says what to do with an invitation link — and, unlike before, there is
 * now a screen at the other end of that link.
 */
const HOLDS: string[] = [
  "The accounts, orders and tickets the assistant may look up.",
  "The policies, SOPs and signed customer agreements it answers from.",
  "The people you work with, and what each of them is allowed to do.",
  "Every action it prepares, and the audit trail of who confirmed what.",
];

export function WorkspaceOnboarding({
  displayName,
  busy,
  error,
  onCreate,
  onSignOut,
}: {
  displayName: string;
  busy: boolean;
  error: string | null;
  onCreate(name: string): void;
  onSignOut(): void;
}) {
  const [name, setName] = useState("");

  return (
    <div className={styles.panel}>
      <p className={styles.eyebrow}>Welcome, {displayName}</p>
      <h1 className={styles.title}>Create your first workspace</h1>

      <p className={styles.lede}>
        A workspace is where your operation lives in ParcelPilot. It holds:
      </p>

      <ul className={styles.holds}>
        {HOLDS.map((item) => (
          <li key={item} className={styles.holdsItem}>
            <span className={styles.holdsMark} aria-hidden="true" />
            <span>{item}</span>
          </li>
        ))}
      </ul>

      <p className={styles.lede}>
        Workspaces are how ParcelPilot keeps one operation&apos;s data separate
        from another&apos;s. The separation is enforced on the server, not in
        this browser: the assistant can only ever see what your workspace owns.
      </p>

      <form
        className={styles.form}
        onSubmit={(event) => {
          event.preventDefault();
          onCreate(name);
        }}
      >
        <TextField
          label="Workspace name"
          value={name}
          onChange={(event) => setName(event.target.value)}
          placeholder="Acme Logistics"
          hint="Usually your company or team name. You can change it later."
          minLength={2}
          maxLength={120}
          required
          autoFocus
        />

        {error ? (
          <Callout tone="fail" role="alert" title="Could not create the workspace">
            {error}
          </Callout>
        ) : null}

        <Button type="submit" variant="primary" block disabled={busy}>
          {busy ? "Creating…" : "Create workspace"}
        </Button>
      </form>

      <p className={styles.footnote}>
        A new workspace starts empty. Records reach it when an operator attaches
        them on the server — nothing is invented here, so the assistant will say
        it cannot find an order rather than answer about one you do not have.
      </p>

      <div className={styles.actions}>
        <Button variant="ghost" size="sm" onClick={onSignOut}>
          Sign out
        </Button>
      </div>
    </div>
  );
}
