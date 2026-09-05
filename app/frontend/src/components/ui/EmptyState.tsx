import styles from "./EmptyState.module.css";

/**
 * Nothing here — said as a fact, with what to do about it.
 *
 * Deliberately not an error treatment. "No signals detected" and "you have no
 * permission to see this" are both empty screens, and only one of them is a
 * problem; giving both a red box would teach a reader to ignore the red one.
 * The caller supplies the words, because the honest sentence differs every
 * time and a generic "No data" is the thing this component exists to prevent.
 */
export function EmptyState({
  title,
  centred = false,
  actions,
  headingLevel = 3,
  children,
}: {
  title: React.ReactNode;
  centred?: boolean;
  actions?: React.ReactNode;
  headingLevel?: 2 | 3 | 4;
  children?: React.ReactNode;
}) {
  const Heading = `h${headingLevel}` as "h2" | "h3" | "h4";

  return (
    <div
      className={[styles.empty, centred ? styles.centred : null]
        .filter(Boolean)
        .join(" ")}
    >
      <Heading className={styles.title}>{title}</Heading>
      {children ? <div className={styles.body}>{children}</div> : null}
      {actions ? <div className={styles.actions}>{actions}</div> : null}
    </div>
  );
}
