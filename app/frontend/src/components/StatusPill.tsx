import type { Tone } from "@/lib/presentation";

import styles from "./StatusPill.module.css";

/**
 * Every tone a pill can wear.
 *
 * `Tone` is the three-value semantic scale the answer vocabulary uses. Two
 * more exist only for pills: `neutral`, for a fact that carries no judgement
 * (a role, a record type), and `info`, for the accent-coloured statement that
 * something governed — neither of which is an ok/caution/fail question.
 */
export type PillTone = Tone | "neutral" | "info";

const TONE_CLASS: Record<PillTone, string | undefined> = {
  ok: styles.ok,
  caution: styles.caution,
  fail: styles.fail,
  neutral: styles.neutral,
  info: styles.info,
};

/**
 * A small labelled status marker.
 *
 * Always carries text. Colour reinforces the state; it never encodes it alone,
 * so the meaning survives greyscale, colour blindness, and a screen reader.
 *
 * `quiet` drops the semantic weight for pills that label rather than warn — a
 * member's role, a record's kind. Without it, a row of six pills all set in
 * 600 reads as six alarms.
 */
export function StatusPill({
  tone,
  quiet = false,
  children,
}: {
  tone: PillTone;
  quiet?: boolean;
  children: React.ReactNode;
}) {
  return (
    <span
      className={[styles.pill, TONE_CLASS[tone], quiet ? styles.quiet : null]
        .filter(Boolean)
        .join(" ")}
    >
      {children}
    </span>
  );
}
