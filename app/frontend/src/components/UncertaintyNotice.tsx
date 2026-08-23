import { useId } from "react";

import styles from "./UncertaintyNotice.module.css";

/**
 * The agent declining to answer, rendered as a result rather than a fault.
 *
 * Uncertainty is a success case in this system: the SOP forbids promising a
 * credit when fault or timing is unknown, so "I cannot determine this" is the
 * correct output, not a failure of the request. The visual treatment says so —
 * it is distinct from an answer and distinct from an error, and it never
 * borrows the red of something that broke.
 *
 * The frontend adds no words of its own to the reasons. Softening them into
 * "we think probably…" would convert a deliberate refusal into a guess, which
 * is the exact failure the backend is built to prevent.
 */
export function UncertaintyNotice({
  reasons,
  escalationRecommended,
}: {
  reasons: string[];
  escalationRecommended: boolean;
}) {
  const headingId = useId();

  if (reasons.length === 0) return null;

  return (
    <section className={styles.notice} aria-labelledby={headingId}>
      <h3 id={headingId} className={styles.heading}>
        Unable to determine
      </h3>
      <ul className={styles.list}>
        {reasons.map((reason) => (
          <li key={reason}>{reason}</li>
        ))}
      </ul>
      {escalationRecommended && (
        <p className={styles.escalation}>
          A human should review this before anything is promised to the customer.
        </p>
      )}
    </section>
  );
}
