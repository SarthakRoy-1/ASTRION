"use client";

import { useCallback, useEffect, useId, useRef } from "react";

import { Button } from "./Button";
import styles from "./Dialog.module.css";

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/**
 * A modal confirmation.
 *
 * Used sparingly and only where a click is not undoable — removing a member,
 * leaving a workspace. Everything else in this product confirms in place,
 * because a dialog that appears for reversible work trains people to dismiss
 * dialogs.
 *
 * It implements the four things a modal must do and that a styled `<div>` does
 * not: it takes focus on open, keeps Tab inside itself, closes on Escape, and
 * returns focus to whatever opened it. `aria-modal` tells assistive technology
 * the rest of the page is inert; the focus trap is what makes that true for a
 * keyboard.
 *
 * Rendered inline rather than through a portal. The shell has no transformed
 * or clipped ancestor, `position: fixed` therefore resolves against the
 * viewport, and a portal would add a mounting concern for no behavioural gain.
 */
export function Dialog({
  title,
  confirmLabel,
  confirmVariant = "danger",
  busy = false,
  onConfirm,
  onCancel,
  children,
}: {
  title: string;
  confirmLabel: string;
  confirmVariant?: "primary" | "danger";
  busy?: boolean;
  onConfirm(): void;
  onCancel(): void;
  children: React.ReactNode;
}) {
  const titleId = useId();
  const dialogRef = useRef<HTMLDivElement>(null);
  const opener = useRef<Element | null>(null);

  // Captured before focus moves, and restored on unmount. Without it, closing
  // the dialog drops the keyboard back at the top of the document, several
  // hundred pixels from the row the user was working on.
  useEffect(() => {
    opener.current = document.activeElement;
    const first = dialogRef.current?.querySelector<HTMLElement>(FOCUSABLE);
    first?.focus();

    return () => {
      if (opener.current instanceof HTMLElement) opener.current.focus();
    };
  }, []);

  const onKeyDown = useCallback(
    (event: React.KeyboardEvent) => {
      if (event.key === "Escape") {
        event.stopPropagation();
        onCancel();
        return;
      }
      if (event.key !== "Tab") return;

      const focusable = Array.from(
        dialogRef.current?.querySelectorAll<HTMLElement>(FOCUSABLE) ?? [],
      );
      if (focusable.length === 0) return;

      const first = focusable[0]!;
      const last = focusable[focusable.length - 1]!;
      const active = document.activeElement;

      if (event.shiftKey && active === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && active === last) {
        event.preventDefault();
        first.focus();
      }
    },
    [onCancel],
  );

  return (
    <div
      className={styles.backdrop}
      // Clicking the backdrop dismisses, but only the backdrop itself — a
      // click that started inside the dialog and ended outside it must not
      // count as "cancel".
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onCancel();
      }}
    >
      <div
        ref={dialogRef}
        className={styles.dialog}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        onKeyDown={onKeyDown}
      >
        <h2 id={titleId} className={styles.title}>
          {title}
        </h2>
        <div className={styles.body}>{children}</div>
        <div className={styles.actions}>
          <Button variant="secondary" onClick={onCancel} disabled={busy}>
            Cancel
          </Button>
          <Button variant={confirmVariant} onClick={onConfirm} disabled={busy}>
            {confirmLabel}
          </Button>
        </div>
      </div>
    </div>
  );
}
