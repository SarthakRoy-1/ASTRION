import styles from "./Callout.module.css";

export type CalloutTone = "info" | "ok" | "caution" | "fail" | "neutral";

const TONE_CLASS: Record<CalloutTone, string | undefined> = {
  info: styles.info,
  ok: styles.ok,
  caution: styles.caution,
  fail: styles.fail,
  neutral: styles.neutral,
};

/**
 * Default glyphs per tone.
 *
 * A shape, not a colour: the trust states are the reason this component
 * exists, and "conditional" and "not enough information" both sit in the warm
 * half of a three-colour palette. `!` and `?` tell them apart on a greyscale
 * monitor, and the visible title tells them apart to a screen reader — the
 * glyph itself is hidden from assistive tech precisely because it would
 * otherwise be read as a punctuation mark before every message.
 */
const TONE_GLYPH: Record<CalloutTone, string> = {
  info: "i",
  ok: "✓",
  caution: "!",
  fail: "!",
  neutral: "?",
};

/**
 * A short, tone'd message: what happened, and what it means.
 *
 * `role` defaults to nothing. A callout that describes state the user just
 * navigated to must not announce itself; only one that *interrupts* — a failed
 * action, a refused request — should be `alert`, and that is the caller's
 * decision because only the caller knows whether the message arrived or was
 * always there.
 */
export function Callout({
  tone = "info",
  title,
  glyph,
  role,
  className,
  children,
}: {
  tone?: CalloutTone;
  title?: React.ReactNode;
  /** Overrides the tone's default shape. */
  glyph?: string;
  role?: "alert" | "status";
  className?: string;
  children?: React.ReactNode;
}) {
  return (
    <div
      className={[styles.callout, TONE_CLASS[tone], className]
        .filter(Boolean)
        .join(" ")}
      role={role}
    >
      <span className={styles.glyph} aria-hidden="true">
        {glyph ?? TONE_GLYPH[tone]}
      </span>
      <div className={styles.content}>
        {title ? <p className={styles.title}>{title}</p> : null}
        {children ? <div className={styles.body}>{children}</div> : null}
      </div>
    </div>
  );
}
