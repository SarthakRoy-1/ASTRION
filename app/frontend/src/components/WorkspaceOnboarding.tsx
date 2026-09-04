"use client";

import { useState } from "react";

import styles from "./WorkspaceOnboarding.module.css";

/**
 * What a signed-in user sees when they belong to no workspace.
 *
 * This is not an error state and must not read like one. A new account is at
 * the *beginning*: the backend reports `needs_workspace`, not a failure, and
 * the copy here explains what a workspace is before asking for a name — because
 * "Create your first workspace" means nothing to someone who has not been told
 * what one holds.
 *
 * A user who was invited rather than signing up arrives here too, so the panel
 * also says what to do with an invitation link.
 */
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
        A workspace is where your operation lives in ParcelPilot. Everything the
        assistant can reach belongs to one — your accounts, orders, tickets,
        documents and policies, and every action it prepares for you.
      </p>
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
        <label className={styles.label} htmlFor="workspace-name">
          Workspace name
        </label>
        <input
          id="workspace-name"
          className={styles.input}
          value={name}
          onChange={(event) => setName(event.target.value)}
          placeholder="Acme Logistics"
          minLength={2}
          maxLength={120}
          required
          autoFocus
        />
        <p className={styles.hint}>
          Usually your company or team name. You can change it later.
        </p>

        {error ? <p className={styles.error}>{error}</p> : null}

        <button className={styles.primary} type="submit" disabled={busy}>
          {busy ? "Creating…" : "Create workspace"}
        </button>
      </form>

      <p className={styles.footnote}>
        Been invited to an existing workspace? Open the invitation link you were
        sent while signed in as the address it was issued to.
      </p>
      <button className={styles.link} type="button" onClick={onSignOut}>
        Sign out
      </button>
    </div>
  );
}
