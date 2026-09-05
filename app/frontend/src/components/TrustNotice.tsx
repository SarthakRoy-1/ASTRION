"use client";

import { agreementGoverned, trustDescriptor } from "@/lib/trust-presentation";
import type { ChatResponse } from "@/lib/types";

import styles from "./TrustNotice.module.css";

/**
 * How far this answer can be relied on, and what governed it.
 *
 * The backend derives all of this in code from tool results — there is no
 * score, no model self-assessment, and nothing here is inferred in the browser.
 * This component only renders what the `trust` block already says.
 *
 * Two things it makes visible that were previously buried in prose:
 *
 * - **A customer agreement decided the answer.** The single most consequential
 *   fact about a support answer, and the one a reader is most likely to get
 *   wrong by assuming the standard policy applied.
 * - **The answer is not settled.** A conflict, a missing input, or a premise
 *   that needs checking, each stated with its reasons rather than as a colour.
 *
 * `confident` with no override renders nothing *here*. A bordered block on
 * every answer stops being read within a day, and "nothing outstanding" is
 * exactly the case where the answer should speak for itself. The state is
 * still always visible: `TrustStatusChip` puts it in the message byline
 * alongside the timestamp, where it reads as metadata rather than as an
 * interruption.
 */

type Trust = ChatResponse["trust"];

const TONE_CLASS: Record<string, string | undefined> = {
  conditional: styles.caution,
  insufficient_data: styles.neutral,
  conflict: styles.conflict,
  escalate: styles.escalate,
};

export function TrustNotice({ trust }: { trust: Trust }) {
  if (!trust) return null;

  const unsettled = trust.status !== "confident";
  const agreementApplied = agreementGoverned(trust);

  // A settled answer with no override is left to speak for itself.
  if (!unsettled && !agreementApplied) return null;

  const descriptor = trustDescriptor(trust.status);

  return (
    <div className={styles.wrap}>
      {agreementApplied ? (
        <p className={styles.override}>
          <span className={styles.overrideBadge}>Customer agreement</span>
          This account&apos;s signed agreement governs here, in place of the
          standard policy.
        </p>
      ) : null}

      {unsettled ? (
        <section
          className={`${styles.notice} ${TONE_CLASS[trust.status] ?? styles.neutral}`}
          aria-label={`Answer reliability: ${descriptor.label}`}
        >
          <span className={styles.glyph} aria-hidden="true">
            {descriptor.glyph}
          </span>

          <div className={styles.content}>
            <p className={styles.title}>{descriptor.label}</p>
            <p className={styles.lede}>{descriptor.lede}</p>

            {trust.reasons?.length ? (
              <>
                <p className={styles.reasonsLabel}>
                  {trust.status === "conflict" ? "What disagrees" : "Why"}
                </p>
                <ul className={styles.reasons}>
                  {trust.reasons.map((reason) => (
                    <li key={reason}>{reason}</li>
                  ))}
                </ul>
              </>
            ) : null}

            {trust.escalation_reason ? (
              <p className={styles.escalationReason}>
                Escalate because: {trust.escalation_reason}
              </p>
            ) : null}

            <p className={styles.next}>
              <span className={styles.nextLabel}>Next</span>
              <span>{descriptor.next}</span>
            </p>
          </div>
        </section>
      ) : null}
    </div>
  );
}
