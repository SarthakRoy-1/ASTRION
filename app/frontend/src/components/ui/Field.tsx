"use client";

import { useId } from "react";

import styles from "./Field.module.css";

/** For a control this module does not wrap — a textarea, a custom widget. */
export function controlClass(invalid = false): string {
  return [styles.control, invalid ? styles.invalid : null].filter(Boolean).join(" ");
}

interface Shared {
  label: string;
  /** Hidden visually but kept for assistive technology. */
  labelHidden?: boolean;
  hint?: React.ReactNode;
  /** A validation message. Its presence is what marks the control invalid. */
  error?: string | null;
  className?: string;
}

/**
 * Wire a label, a hint and an error to one control.
 *
 * The three ids are generated rather than passed in because every screen that
 * hand-wired them got at least one wrong: a `for` that pointed at nothing, or
 * an `aria-describedby` naming an element that only exists while the error is
 * showing. Here the description is assembled from whichever of hint and error
 * are actually rendered, so it can never name a missing node.
 */
function useFieldIds(hint: unknown, error: unknown) {
  const id = useId();
  const hintId = `${id}-hint`;
  const errorId = `${id}-error`;
  const describedBy =
    [hint ? hintId : null, error ? errorId : null].filter(Boolean).join(" ") ||
    undefined;
  return { id, hintId, errorId, describedBy };
}

function Frame({
  label,
  labelHidden,
  hint,
  error,
  className,
  id,
  hintId,
  errorId,
  children,
}: Shared & {
  id: string;
  hintId: string;
  errorId: string;
  children: React.ReactNode;
}) {
  return (
    <div className={[styles.field, className].filter(Boolean).join(" ")}>
      <label
        className={labelHidden ? "visually-hidden" : styles.label}
        htmlFor={id}
      >
        {label}
      </label>
      {children}
      {hint ? (
        <p className={styles.hint} id={hintId}>
          {hint}
        </p>
      ) : null}
      {error ? (
        <p className={styles.error} id={errorId}>
          {error}
        </p>
      ) : null}
    </div>
  );
}

export function TextField({
  label,
  labelHidden,
  hint,
  error,
  className,
  ...rest
}: Shared & Omit<React.InputHTMLAttributes<HTMLInputElement>, "className" | "id">) {
  const { id, hintId, errorId, describedBy } = useFieldIds(hint, error);
  return (
    <Frame
      label={label}
      labelHidden={labelHidden}
      hint={hint}
      error={error}
      className={className}
      id={id}
      hintId={hintId}
      errorId={errorId}
    >
      <input
        {...rest}
        id={id}
        className={controlClass(Boolean(error))}
        aria-invalid={error ? true : undefined}
        aria-describedby={describedBy}
      />
    </Frame>
  );
}

export function SelectField({
  label,
  labelHidden,
  hint,
  error,
  className,
  children,
  ...rest
}: Shared &
  Omit<React.SelectHTMLAttributes<HTMLSelectElement>, "className" | "id">) {
  const { id, hintId, errorId, describedBy } = useFieldIds(hint, error);
  return (
    <Frame
      label={label}
      labelHidden={labelHidden}
      hint={hint}
      error={error}
      className={className}
      id={id}
      hintId={hintId}
      errorId={errorId}
    >
      <select
        {...rest}
        id={id}
        className={controlClass(Boolean(error))}
        aria-invalid={error ? true : undefined}
        aria-describedby={describedBy}
      >
        {children}
      </select>
    </Frame>
  );
}
