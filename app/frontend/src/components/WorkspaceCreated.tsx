"use client";

import { useState } from "react";

import { Button } from "./ui/Button";
import { Callout } from "./ui/Callout";

import styles from "./WorkspaceOnboarding.module.css";

/**
 * Copy text without assuming a secure context: the async clipboard API where
 * it exists, and a report of failure where it does not, so the button never
 * claims a copy that did not happen.
 */
export async function copyText(text: string): Promise<boolean> {
  try {
    if (typeof navigator !== "undefined" && navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // fall through to "not copied"
  }
  return false;
}

/**
 * The owner's first sight of a new workspace's code.
 *
 * The password they just chose is deliberately not here: the server keeps only
 * a hash and there is nothing to show. What they have to hand to a colleague is
 * this code *and* that password, so the screen says both, and says the password
 * cannot be recovered (it can be replaced, from the Workspace page).
 */
export function WorkspaceCreated({
  name,
  code,
  onContinue,
}: {
  name: string;
  code: string;
  onContinue(): void;
}) {
  const [copied, setCopied] = useState<"yes" | "no" | null>(null);

  return (
    <div className={styles.panel}>
      <p className={styles.eyebrow}>Workspace created</p>
      <h1 className={styles.title}>{name} is ready</h1>

      <p className={styles.lede}>
        To add people, give them this workspace code <strong>and</strong> the
        workspace password you just chose. They sign in to their own account,
        then enter both. The code alone opens nothing.
      </p>

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
      </div>

      <div role="status" aria-live="polite">
        {copied === "yes" ? <p className={styles.lede}>Copied.</p> : null}
        {copied === "no" ? (
          <p className={styles.lede}>
            Couldn&apos;t copy automatically — select the code and copy it.
          </p>
        ) : null}
      </div>

      <Callout tone="info" title="The password isn't shown again">
        Only a scrambled form of it is stored, so it can&apos;t be looked up
        later. If it is lost, set a new one from the Workspace page; the old one
        then stops working and current members are unaffected.
      </Callout>

      <div className={styles.form}>
        <Button variant="primary" block onClick={onContinue}>
          Continue to your workspace
        </Button>
      </div>
    </div>
  );
}
