"use client";

import { useState } from "react";

import { copyText } from "./WorkspaceCreated";
import { Button } from "./ui/Button";
import { Callout } from "./ui/Callout";
import { TextField } from "./ui/Field";
import { Panel } from "./ui/Panel";

import { changeWorkspacePassword } from "@/lib/auth-client";
import { ApiError } from "@/lib/client";
import type { Workspace } from "@/lib/auth-types";

import styles from "./WorkspaceOnboarding.module.css";

const MIN_PASSWORD_LENGTH = 8;

/**
 * How people join this workspace — shown to its owner only.
 *
 * The server sends the code to owners only and enforces the same on the
 * password change, so hiding this panel from anyone else is presentation; it is
 * not what stops them.
 *
 * The current password is not shown and cannot be: only its hash is stored.
 * Setting a new one replaces the old at once and leaves every member where they
 * are.
 */
export function WorkspaceAccessPanel({ workspace }: { workspace: Workspace }) {
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [passwordError, setPasswordError] = useState<string | null>(null);
  const [confirmError, setConfirmError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [changed, setChanged] = useState(false);
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState<"yes" | "no" | null>(null);

  const code = workspace.workspace_code;

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setChanged(false);
    setError(null);
    const tooShort =
      password.length < MIN_PASSWORD_LENGTH
        ? `Workspace password must be at least ${MIN_PASSWORD_LENGTH} characters.`
        : null;
    const mismatched =
      !tooShort && password !== confirm ? "The workspace passwords do not match." : null;
    setPasswordError(tooShort);
    setConfirmError(mismatched);
    if (tooShort || mismatched) return;

    setBusy(true);
    try {
      await changeWorkspacePassword(workspace.workspace_id, {
        newPassword: password,
        confirmNewPassword: confirm,
      });
      // Neither copy outlives the request that used it.
      setPassword("");
      setConfirm("");
      setChanged(true);
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : String(cause));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Panel
      title="Workspace access"
      description="People join with the workspace code and the workspace password. Give them both; the code alone opens nothing."
    >
      {code ? (
        <div className={styles.codeRow}>
          <span className={styles.code} aria-label="Workspace code">
            {code}
          </span>
          <Button
            variant="secondary"
            size="sm"
            onClick={async () => setCopied((await copyText(code)) ? "yes" : "no")}
          >
            Copy code
          </Button>
          <span role="status" aria-live="polite">
            {copied === "yes" ? "Copied." : null}
            {copied === "no" ? "Couldn’t copy — select the code and copy it." : null}
          </span>
        </div>
      ) : (
        <p className={styles.lede}>
          This workspace has no join password yet. Set one below to get a
          workspace code people can join with.
        </p>
      )}

      <form className={styles.form} aria-label="Change the workspace password" onSubmit={submit}>
        <TextField
          label="New workspace password"
          type="password"
          value={password}
          onChange={(event) => {
            setPassword(event.target.value);
            setPasswordError(null);
            setConfirmError(null);
          }}
          autoComplete="new-password"
          hint="The current password isn’t shown — only a hash of it is stored. The new one replaces it at once; current members stay."
          error={passwordError}
          required
        />
        <TextField
          label="Confirm new workspace password"
          type="password"
          value={confirm}
          onChange={(event) => {
            setConfirm(event.target.value);
            setConfirmError(null);
          }}
          autoComplete="new-password"
          error={confirmError}
          required
        />

        {error ? (
          <Callout tone="fail" role="alert" title="Could not change the password">
            {error}
          </Callout>
        ) : null}
        {changed ? (
          <Callout tone="ok" role="status" title="Password changed">
            The old workspace password no longer works. Members who already joined
            are unaffected.
          </Callout>
        ) : null}

        <Button type="submit" variant="primary" disabled={busy}>
          {busy ? "Saving…" : "Change workspace password"}
        </Button>
      </form>
    </Panel>
  );
}
