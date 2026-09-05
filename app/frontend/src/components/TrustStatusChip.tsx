import { governedByLabel, trustDescriptor } from "@/lib/trust-presentation";
import type { ChatResponse } from "@/lib/types";

import styles from "./TrustStatusChip.module.css";

const TONE_CLASS: Record<string, string | undefined> = {
  ok: styles.ok,
  caution: styles.caution,
  fail: styles.fail,
  neutral: styles.neutral,
  info: styles.neutral,
};

/**
 * The answer's reliability, in the byline.
 *
 * This is the one place trust appears on *every* answer, and it is deliberately
 * metadata rather than a notice: a chip beside the timestamp, at caption size,
 * where a timestamp lives. `TrustNotice` still carries the explanation, and
 * still says nothing when there is nothing to explain — so a settled answer
 * gets one short word here and no box below, while an unsettled one gets both.
 *
 * The governing authority is stated next to it because "Confident" alone
 * leaves the more useful half of the question unanswered. Knowing that a
 * signed agreement decided the answer, rather than the standard policy, is
 * what changes what a support agent says to the customer.
 */
export function TrustStatusChip({ trust }: { trust: ChatResponse["trust"] }) {
  if (!trust) return null;

  const descriptor = trustDescriptor(trust.status);
  const governed = governedByLabel(trust);

  return (
    <>
      <span
        className={`${styles.chip} ${TONE_CLASS[descriptor.tone] ?? styles.neutral}`}
      >
        <span className={styles.glyph} aria-hidden="true">
          {descriptor.glyph}
        </span>
        {descriptor.label}
      </span>
      {governed ? <span className={styles.governed}>{governed}</span> : null}
    </>
  );
}
