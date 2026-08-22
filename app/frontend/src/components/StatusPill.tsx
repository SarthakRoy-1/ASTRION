import type { Tone } from "@/lib/presentation";

import styles from "./StatusPill.module.css";

const TONE_CLASS: Record<Tone, string> = {
  ok: styles.ok!,
  caution: styles.caution!,
  fail: styles.fail!,
};

/**
 * A small labelled status marker.
 *
 * Always carries text. Colour reinforces the state; it never encodes it alone,
 * so the meaning survives greyscale, colour blindness, and a screen reader.
 */
export function StatusPill({
  tone,
  children,
}: {
  tone: Tone;
  children: React.ReactNode;
}) {
  return <span className={`${styles.pill} ${TONE_CLASS[tone]}`}>{children}</span>;
}
