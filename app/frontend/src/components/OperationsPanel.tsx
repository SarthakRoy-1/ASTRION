"use client";

import { useCallback, useEffect, useState } from "react";

import styles from "./OperationsPanel.module.css";

import { ApiError } from "@/lib/client";
import { fetchSignals } from "@/lib/operations-client";
import {
  SIGNAL_TYPE_LABELS,
  type OperationalSignal,
  type SignalReport,
} from "@/lib/operations-types";

/**
 * "What needs my attention right now, and why?"
 *
 * Deliberately not a dashboard. There are no charts, no counters and no
 * time-series, because none of those answer that question — a ranked list with
 * reasons does. Everything shown was computed by a deterministic detector on
 * the server; this component renders and never re-ranks, re-scores, or infers.
 *
 * Three things it is careful about:
 *
 * - **The ranking is explainable on demand.** Expanding a signal shows the
 *   priority arithmetic itemised, so "why is this above that?" has an answer
 *   rather than a number.
 * - **Unsettled signals say so.** A detector that could not confirm something
 *   carries a trust status and its reasons, and they are shown next to the
 *   claim rather than buried.
 * - **Nothing here acts.** `recommended_next_step` is advice. Investigating
 *   hands the question to the existing agent, which can at most *prepare* an
 *   action for someone to confirm.
 */

/** CSS-module lookups are `string | undefined` under this project's typing,
 *  and an unknown severity legitimately has no class — the value is typed to
 *  admit that rather than asserted away. */
const SEVERITY_CLASS: Record<string, string | undefined> = {
  critical: styles.critical,
  high: styles.high,
  medium: styles.medium,
  low: styles.low,
};

export function OperationsPanel({
  onInvestigate,
  onClose,
}: {
  /** Hands a question to the existing conversation. */
  onInvestigate(question: string): void;
  onClose(): void;
}) {
  const [report, setReport] = useState<SignalReport | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setReport(await fetchSignals());
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : String(cause));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <section className={styles.panel} aria-label="Operations signals">
      <header className={styles.header}>
        <div>
          <h2 className={styles.title}>What needs attention</h2>
          <p className={styles.subtitle}>
            {report
              ? `${report.count} signal${report.count === 1 ? "" : "s"} detected from this workspace's tickets and orders`
              : "Detecting…"}
            {report?.reference_time ? (
              <>
                {" · measured against the dataset snapshot "}
                <time dateTime={report.reference_time}>
                  {report.reference_time.slice(0, 16).replace("T", " ")}
                </time>
              </>
            ) : null}
          </p>
        </div>
        <button className={styles.close} type="button" onClick={onClose}>
          Close
        </button>
      </header>

      {error ? <p className={styles.error}>{error}</p> : null}
      {loading ? <p className={styles.muted}>Detecting…</p> : null}

      {report && report.count === 0 && !loading ? (
        <p className={styles.empty}>
          Nothing was detected in this workspace&apos;s current data. That is an
          absence of detected signals, not a guarantee that nothing is wrong —
          detection runs over the tickets and orders in scope, and only reports
          patterns it can evidence.
        </p>
      ) : null}

      <ul className={styles.list}>
        {report?.signals.map((signal) => (
          <SignalRow
            key={signal.signal_id}
            signal={signal}
            open={expanded === signal.signal_id}
            onToggle={() =>
              setExpanded(expanded === signal.signal_id ? null : signal.signal_id)
            }
            onInvestigate={() =>
              onInvestigate(
                `Investigate ${signal.signal_id}. What is happening, which ` +
                  `customers are affected, and should we escalate?`,
              )
            }
          />
        ))}
      </ul>
    </section>
  );
}

function SignalRow({
  signal,
  open,
  onToggle,
  onInvestigate,
}: {
  signal: OperationalSignal;
  open: boolean;
  onToggle(): void;
  onInvestigate(): void;
}) {
  const unsettled = signal.trust_status !== "confident";

  return (
    <li className={styles.item}>
      <div className={styles.row}>
        <span
          className={`${styles.severity} ${SEVERITY_CLASS[signal.severity] ?? ""}`}
        >
          {signal.severity}
        </span>
        <div className={styles.summary}>
          <button
            className={styles.titleButton}
            type="button"
            onClick={onToggle}
            aria-expanded={open}
          >
            {signal.title}
          </button>
          <p className={styles.meta}>
            {SIGNAL_TYPE_LABELS[signal.signal_type] ?? signal.signal_type}
            {" · "}
            {signal.affected_account_count} account
            {signal.affected_account_count === 1 ? "" : "s"}
            {signal.affected_ticket_count > 0
              ? ` · ${signal.affected_ticket_count} ticket${signal.affected_ticket_count === 1 ? "" : "s"}`
              : ""}
            {signal.affected_order_count > 0
              ? ` · ${signal.affected_order_count} order${signal.affected_order_count === 1 ? "" : "s"}`
              : ""}
            {" · priority "}
            {signal.priority_score}
            {unsettled ? (
              <span className={styles.unsettled}> · {signal.trust_status}</span>
            ) : null}
          </p>
        </div>
      </div>

      {open ? (
        <div className={styles.detail}>
          <p className={styles.why}>{signal.detail}</p>

          {signal.trust_reasons.length > 0 ? (
            <div className={styles.caution}>
              <p className={styles.cautionTitle}>Not settled</p>
              <ul>
                {signal.trust_reasons.map((reason) => (
                  <li key={reason}>{reason}</li>
                ))}
              </ul>
            </div>
          ) : null}

          {signal.records.length > 0 ? (
            <>
              <h4 className={styles.sectionTitle}>Affected records</h4>
              <ul className={styles.records}>
                {signal.records.map((record) => (
                  <li key={`${record.kind}-${record.record_id}`}>
                    <code>{record.record_id}</code>
                    {record.label ? ` — ${record.label}` : null}
                  </li>
                ))}
              </ul>
            </>
          ) : null}

          {/* The ranking, itemised. "Why is this above that one?" must have an
              answer a support lead can read, not a number they must trust. */}
          <h4 className={styles.sectionTitle}>How this was prioritised</h4>
          <ul className={styles.factors}>
            {signal.priority_factors.map((factor) => (
              <li key={factor.name}>
                <span className={styles.points}>
                  {factor.points > 0 ? `+${factor.points}` : factor.points}
                </span>
                <span className={styles.factorName}>{factor.name}</span>
                <span className={styles.basis}>{factor.basis}</span>
              </li>
            ))}
            <li className={styles.total}>
              <span className={styles.points}>{signal.priority_score}</span>
              <span className={styles.factorName}>total</span>
            </li>
          </ul>

          {signal.documentation_chunk_ids.length > 0 ? (
            <p className={styles.docs}>
              Matches {signal.documentation_chunk_ids.length} documented
              section(s) — ask the assistant to quote them.
            </p>
          ) : null}

          {signal.recommended_next_step ? (
            <p className={styles.next}>
              <strong>Suggested next step:</strong> {signal.recommended_next_step}
            </p>
          ) : null}

          <button className={styles.investigate} type="button" onClick={onInvestigate}>
            Ask the assistant to investigate
          </button>
        </div>
      ) : null}
    </li>
  );
}
