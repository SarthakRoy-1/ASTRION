"use client";

import type { PrincipalView } from "@/lib/types";

import styles from "./ContextSelector.module.css";

/**
 * Chooses which demo identity the conversation runs as.
 *
 * The options come from `GET /api/principals` — the server's own directory —
 * so the browser can only ever assert an identity the backend already knows.
 * There is deliberately no way to type an account id or edit a scope here: the
 * request asserts *who*, and the server decides *what they may see*. Letting
 * the UI propose a scope, even a narrower one, would blur that line in exactly
 * the place the product needs it sharp.
 */
export function ContextSelector({
  principals,
  identity,
  disabled,
  onSelect,
}: {
  principals: PrincipalView[];
  identity: string | null;
  disabled: boolean;
  onSelect: (userId: string) => void;
}) {
  return (
    <div className={styles.wrapper}>
      <label className={styles.label} htmlFor="context-select">
        Context
      </label>
      <select
        id="context-select"
        className={styles.select}
        value={identity ?? ""}
        disabled={disabled || principals.length === 0}
        onChange={(event) => onSelect(event.target.value)}
      >
        {principals.length === 0 && <option value="">Loading…</option>}
        {principals.map((principal) => (
          <option key={principal.user_id} value={principal.user_id}>
            {principal.display_name}
          </option>
        ))}
      </select>
    </div>
  );
}
