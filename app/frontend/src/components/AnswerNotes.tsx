"use client";

import { useId } from "react";

import type { AnswerNote } from "@/lib/presentation";

import styles from "./AnswerNotes.module.css";

/**
 * Lines from the agent's answer that nothing else on screen rendered.
 *
 * The caller has already removed every note the decision card or the evidence
 * section displays structurally, so this block is a remainder, not a copy —
 * usually empty, and present only so a citation the structured sections do
 * not cover can never be silently lost.
 *
 * `<details>` rather than a scripted disclosure, for the same reasons the
 * evidence cards use it — keyboard-operable, announced correctly, and findable
 * by the browser's own in-page search even while collapsed.
 */
export function AnswerNotes({ notes }: { notes: AnswerNote[] }) {
  const headingId = useId();

  if (notes.length === 0) return null;

  return (
    <details className={styles.notes}>
      <summary className={styles.summary} id={headingId}>
        <span className={styles.label}>More from the agent&apos;s answer</span>
        <span className={styles.count}>
          {notes.length} {notes.length === 1 ? "line" : "lines"}
        </span>
      </summary>
      <dl className={styles.list}>
        {notes.map((note, index) => (
          <div key={`${note.label}-${index}`} className={styles.row}>
            <dt>{note.label}</dt>
            <dd>{note.text}</dd>
          </div>
        ))}
      </dl>
    </details>
  );
}
