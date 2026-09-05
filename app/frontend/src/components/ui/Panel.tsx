import { useId } from "react";

import styles from "./Panel.module.css";

/**
 * A titled surface.
 *
 * The title is rendered as a real heading and wired to the section with
 * `aria-labelledby`, so the document outline a screen reader walks matches the
 * boxes a sighted reader sees. `headingLevel` exists because a panel's correct
 * level depends on where it sits, and a component that always emits `<h2>`
 * produces a broken outline the moment one is nested in another.
 */
export function Panel({
  title,
  description,
  actions,
  headingLevel = 2,
  tight = false,
  flush = false,
  className,
  bodyClassName,
  children,
}: {
  title?: React.ReactNode;
  description?: React.ReactNode;
  actions?: React.ReactNode;
  headingLevel?: 2 | 3 | 4;
  /** Denser padding, for a panel that is a list rather than a page section. */
  tight?: boolean;
  /** Edge-to-edge: no rounded corners or side borders, for a full-width band. */
  flush?: boolean;
  className?: string;
  bodyClassName?: string;
  children: React.ReactNode;
}) {
  const headingId = useId();
  const Heading = `h${headingLevel}` as "h2" | "h3" | "h4";

  const classes = [
    styles.panel,
    tight ? styles.tight : null,
    flush ? styles.flush : null,
    title ? null : styles.headless,
    className,
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <section className={classes} aria-labelledby={title ? headingId : undefined}>
      {title ? (
        <header className={styles.header}>
          <div className={styles.headings}>
            <Heading id={headingId} className={styles.title}>
              {title}
            </Heading>
            {description ? (
              <p className={styles.description}>{description}</p>
            ) : null}
          </div>
          {actions ? <div className={styles.actions}>{actions}</div> : null}
        </header>
      ) : null}
      <div className={[styles.body, bodyClassName].filter(Boolean).join(" ")}>
        {children}
      </div>
    </section>
  );
}
