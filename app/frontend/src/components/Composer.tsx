"use client";

import { useState } from "react";

import { Button } from "./ui/Button";

import styles from "./Composer.module.css";

/**
 * The message input.
 *
 * A real `<form>` with a labelled `<textarea>`, so Enter submits, Shift+Enter
 * inserts a newline, and screen readers announce the field — all behaviour the
 * platform already provides correctly and that a `<div>` would have to
 * reimplement badly.
 *
 * The field stays enabled while a request is in flight so the user can compose
 * their next question; only submission is blocked. Clearing the box on submit
 * rather than on success is deliberate: the message is already in the
 * transcript above, so leaving a duplicate in the composer reads as a failure
 * to send.
 *
 * `unavailableReason` exists because a disabled composer with nothing to say
 * for itself is indistinguishable from a broken one. It is shown, not merely
 * used to grey the control out.
 */
export function Composer({
  disabled,
  sending,
  unavailableReason,
  onSend,
}: {
  disabled: boolean;
  sending: boolean;
  /** Why the composer cannot be used, when it cannot. */
  unavailableReason?: string | null;
  onSend: (message: string) => void;
}) {
  const [value, setValue] = useState("");
  const canSubmit = value.trim().length > 0 && !sending && !disabled;

  function submit() {
    if (!canSubmit) return;
    onSend(value.trim());
    setValue("");
  }

  return (
    <>
      <form
        className={styles.composer}
        onSubmit={(event) => {
          event.preventDefault();
          submit();
        }}
      >
        <label className="visually-hidden" htmlFor="composer-input">
          Ask the ASTRION support agent
        </label>
        <textarea
          id="composer-input"
          className={styles.input}
          value={value}
          rows={1}
          placeholder="Ask about an order, ticket, policy or known issue…"
          disabled={disabled}
          onChange={(event) => setValue(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              submit();
            }
          }}
        />
        <Button
          type="submit"
          variant="primary"
          className={styles.send}
          disabled={!canSubmit}
        >
          {sending ? "Investigating…" : "Send"}
        </Button>
      </form>

      {disabled && unavailableReason ? (
        <p className={styles.blocked}>{unavailableReason}</p>
      ) : null}
    </>
  );
}
