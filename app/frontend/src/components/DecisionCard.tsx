import {
  decisionAmountLabel,
  decisionFacts,
  decisionSubject,
  decisionTitle,
  decisionVerdict,
  decisionVerdictLabel,
  formatAmount,
} from "@/lib/presentation";
import { StatusPill } from "./StatusPill";
import type { PolicyDecisionView } from "@/lib/types";

import styles from "./DecisionCard.module.css";

/**
 * A deterministic policy result, shown with the reasoning that produced it.
 *
 * Every value here was computed by `app/backend/policies/` — the rule, the
 * arithmetic, the amount, the citations. The card's whole job is to keep them
 * together: a figure without the rule and the inputs behind it is a number the
 * reader has to take on faith, which is precisely what this system is built
 * not to ask of them.
 *
 * The verdict deliberately reads "Uncertain" whenever verification is
 * required, even when a provisional amount came back. A support agent glancing
 * at this card must not be able to mistake a provisional credit for an
 * approved one.
 */
export function DecisionCard({ decision }: { decision: PolicyDecisionView }) {
  const verdict = decisionVerdict(decision);
  const amount = formatAmount(decision.currency, decision.amount ?? null);
  const provisional = decision.requires_verification;

  // On a cancellation, `applies` is the *fee* question, not the verdict — so
  // the figure says which of the two it is. "INR 0" alone leaves a reader
  // working out whether zero means waived or means not calculated.
  const qualifier = provisional
    ? "provisional"
    : decision.decision_type === "cancellation"
      ? decision.applies === true
        ? "fee applies"
        : "no fee"
      : null;

  return (
    <section className={styles.card} aria-label={`${decisionTitle(decision)} decision`}>
      <header className={styles.header}>
        <h3 className={styles.title}>{decisionTitle(decision)}</h3>
        <span className={styles.order}>{decisionSubject(decision)}</span>
      </header>

      <dl className={styles.verdictRow}>
        <div className={styles.field}>
          <dt>{decisionVerdictLabel(decision)}</dt>
          <dd className={styles.verdict}>
            <StatusPill tone={verdict.tone}>{verdict.label}</StatusPill>
          </dd>
        </div>

        {amount && (
          <div className={styles.field}>
            <dt>{decisionAmountLabel(decision)}</dt>
            <dd className={styles.amount}>
              {qualifier ? (
                <span className={styles.amountValue}>{amount}</span>
              ) : (
                amount
              )}
              {qualifier && (
                <span
                  className={
                    provisional ? styles.provisional : styles.amountNote
                  }
                >
                  {qualifier}
                </span>
              )}
            </dd>
          </div>
        )}

        {/* The inputs the verdict rested on: SLA target and elapsed time, an
            order's status, a measured delay and whether fault was settled. */}
        {decisionFacts(decision).map((fact) => (
          <div key={fact.label} className={styles.field}>
            <dt>{fact.label}</dt>
            <dd>{fact.value}</dd>
          </div>
        ))}
      </dl>

      {decision.requires_immediate_escalation && (
        <p className={styles.escalation}>
          The current support policy requires this to be escalated immediately,
          independently of the response-target arithmetic.
        </p>
      )}

      {provisional && decision.verification_reasons.length > 0 && (
        <div className={styles.verification}>
          <p className={styles.verificationTitle}>Verify before committing</p>
          <ul className={styles.verificationList}>
            {decision.verification_reasons.map((reason) => (
              <li key={reason}>{reason}</li>
            ))}
          </ul>
        </div>
      )}

      <div className={styles.rule}>
        <p className={styles.ruleLabel}>Rule applied</p>
        <p className={styles.ruleText}>{decision.controlling_rule}</p>
      </div>

      {decision.calculation && (
        <p className={styles.calculation}>
          <span className={styles.calculationLabel}>Calculation</span>
          <code>{decision.calculation}</code>
        </p>
      )}

      {decision.overrides.length > 0 && (
        <div className={styles.overrides}>
          <p className={styles.overrideLabel}>Precedence applied</p>
          {decision.overrides.map((override) => (
            <p key={override} className={styles.overrideText}>
              {override}
            </p>
          ))}
        </div>
      )}

      {decision.controlling_sources.length > 0 && (
        <ul className={styles.sources}>
          {decision.controlling_sources.map((source) => (
            <li key={source}>{source}</li>
          ))}
        </ul>
      )}
    </section>
  );
}
