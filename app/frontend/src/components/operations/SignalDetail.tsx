"use client";

import { StatusPill } from "@/components/StatusPill";
import { Button } from "@/components/ui/Button";
import { Callout } from "@/components/ui/Callout";
import {
  formatReferenceTime,
  priorityFactorLabel,
  recordKindLabel,
  scopeSummary,
  severityLabel,
  severityTone,
  signalTypeLabel,
} from "@/lib/operations-presentation";
import { trustDescriptor } from "@/lib/trust-presentation";
import type { OperationalSignal } from "@/lib/operations-types";

import styles from "./SignalDetail.module.css";

/**
 * One signal in full: what fired, on what, why it ranks where it does, and
 * what to do next.
 *
 * Everything here came from the detector. The component adds no judgement of
 * its own, and the one control it offers does not act — investigating hands
 * the question to the existing agent, which can at most *prepare* an action
 * for someone to confirm.
 *
 * The priority breakdown is not an implementation detail on display. It is the
 * answer to "why is this above that one", and an operations inbox whose order
 * cannot be interrogated is a number a support lead has to take on faith.
 */
export function SignalDetail({
  signal,
  onInvestigate,
}: {
  signal: OperationalSignal;
  onInvestigate(signal: OperationalSignal): void;
}) {
  const unsettled = signal.trust_status !== "confident";
  const trust = trustDescriptor(signal.trust_status);

  return (
    <article className={styles.detail}>
      <header className={styles.header}>
        <div className={styles.badges}>
          <StatusPill tone={severityTone(signal.severity)}>
            {severityLabel(signal.severity)}
          </StatusPill>
          <StatusPill tone="neutral" quiet>
            {signalTypeLabel(signal.signal_type)}
          </StatusPill>
          <StatusPill tone={unsettled ? trust.tone : "ok"} quiet>
            {trust.label}
          </StatusPill>
        </div>

        <h2 className={styles.title}>{signal.title}</h2>
        <p className={styles.id}>
          {signal.signal_id} · {scopeSummary(signal)}
        </p>
      </header>

      <section className={styles.section}>
        <h3 className={styles.sectionTitle}>Why this was detected</h3>
        <p className={styles.why}>{signal.detail}</p>
        {/* Formatted, not the raw ISO string the detector recorded. Both are
            timestamps in the dataset's own timeline, not the wall clock. */}
        {(signal.first_observed_at || signal.last_observed_at) && (
          <p className={styles.observed}>
            {signal.first_observed_at ? (
              <span>
                First observed{" "}
                <time dateTime={signal.first_observed_at}>
                  {formatReferenceTime(signal.first_observed_at)}
                </time>
              </span>
            ) : null}
            {signal.last_observed_at ? (
              <span>
                Last observed{" "}
                <time dateTime={signal.last_observed_at}>
                  {formatReferenceTime(signal.last_observed_at)}
                </time>
              </span>
            ) : null}
          </p>
        )}
      </section>

      {unsettled && signal.trust_reasons.length > 0 ? (
        <Callout tone={trust.tone === "ok" ? "neutral" : trust.tone} title="Not settled">
          <ul>
            {signal.trust_reasons.map((reason) => (
              <li key={reason}>{reason}</li>
            ))}
          </ul>
        </Callout>
      ) : null}

      {signal.records.length > 0 ? (
        <section className={styles.section}>
          <h3 className={styles.sectionTitle}>Affected records</h3>
          <ul className={styles.records}>
            {signal.records.map((record) => (
              <li
                key={`${record.kind}-${record.record_id}`}
                className={styles.record}
              >
                <StatusPill tone="neutral" quiet>
                  {recordKindLabel(record.kind)}
                </StatusPill>
                <span className={styles.recordId}>{record.record_id}</span>
                {record.label ? (
                  <span className={styles.recordLabel}>{record.label}</span>
                ) : null}
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      <section className={styles.section}>
        <h3 className={styles.sectionTitle}>How this was prioritised</h3>
        <ul className={styles.factors}>
          {signal.priority_factors.map((factor) => (
            <li key={factor.name} className={styles.factor}>
              <span
                className={`${styles.points} ${factor.points < 0 ? styles.negative : ""}`}
              >
                {factor.points > 0 ? `+${factor.points}` : factor.points}
              </span>
              <span className={styles.factorName}>
                {priorityFactorLabel(factor.name)}
              </span>
              {/* The server's own explanation of what earned the points. Never
                  rewritten here — it is the evidence for the arithmetic. */}
              <span className={styles.basis}>{factor.basis}</span>
            </li>
          ))}
        </ul>
        <p className={styles.total}>
          <span className={styles.points}>{signal.priority_score}</span>
          <span className={styles.factorName}>total</span>
        </p>
      </section>

      {signal.documentation_chunk_ids.length > 0 ? (
        <section className={styles.section}>
          <h3 className={styles.sectionTitle}>Known issue</h3>
          <p className={styles.docs}>
            This matches {signal.documentation_chunk_ids.length} documented
            section
            {signal.documentation_chunk_ids.length === 1 ? "" : "s"} in the
            workspace&apos;s own material. Investigating below will quote them
            with their authority and page references.
          </p>
        </section>
      ) : null}

      {signal.recommended_next_step ? (
        <Callout tone="info" title="Suggested next step">
          {signal.recommended_next_step}
        </Callout>
      ) : null}

      <div className={styles.actions}>
        <Button variant="primary" onClick={() => onInvestigate(signal)}>
          Investigate with the assistant
        </Button>
        <p className={styles.actionNote}>
          Nothing here acts. The assistant looks the signal up under your own
          scope and, at most, prepares an action for you to confirm.
        </p>
      </div>
    </article>
  );
}
