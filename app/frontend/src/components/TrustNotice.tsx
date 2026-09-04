"use client";

import styles from "./TrustNotice.module.css";

import type { ChatResponse } from "@/lib/types";

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
 * `confident` with no override renders nothing at all. A badge that appears on
 * every answer stops being read within a day, and "nothing outstanding" is
 * exactly the case where the answer should speak for itself.
 */

type Trust = ChatResponse["trust"];

const LABELS: Record<string, { title: string; lede: string }> = {
  conditional: {
    title: "Conditional",
    lede: "This holds only if the points below are true. Check them before acting.",
  },
  conflict: {
    title: "Sources conflict",
    lede: "Sources of equal authority disagree. This has not been resolved.",
  },
  insufficient_data: {
    title: "Not enough information",
    lede: "Some of what this answer needed was missing or unavailable.",
  },
  escalate: {
    title: "Needs a person",
    lede: "This cannot be settled automatically and should be escalated.",
  },
};

/** Tier 1 is a signed customer agreement — see the backend authority model. */
const CUSTOMER_AGREEMENT_TIER = 1;

export function TrustNotice({ trust }: { trust: Trust }) {
  if (!trust) return null;

  const unsettled = trust.status !== "confident";
  const agreementApplied =
    trust.customer_agreement_applied ||
    trust.governing_authority_tier === CUSTOMER_AGREEMENT_TIER;

  // A settled answer with no override is left to speak for itself.
  if (!unsettled && !agreementApplied) return null;

  const label = LABELS[trust.status];

  return (
    <div className={styles.wrap}>
      {agreementApplied ? (
        <p className={styles.override}>
          <span className={styles.overrideBadge}>Customer agreement</span>
          This account&apos;s signed agreement governs here, in place of the
          standard policy.
        </p>
      ) : null}

      {unsettled && label ? (
        <section
          className={
            trust.status === "escalate" || trust.status === "conflict"
              ? styles.escalate
              : styles.caution
          }
          aria-label={`Answer reliability: ${label.title}`}
        >
          <p className={styles.title}>{label.title}</p>
          <p className={styles.lede}>{label.lede}</p>

          {trust.reasons?.length ? (
            <ul className={styles.reasons}>
              {trust.reasons.map((reason) => (
                <li key={reason}>{reason}</li>
              ))}
            </ul>
          ) : null}

          {trust.escalation_reason ? (
            <p className={styles.escalationReason}>
              Escalate because: {trust.escalation_reason}
            </p>
          ) : null}
        </section>
      ) : null}
    </div>
  );
}
