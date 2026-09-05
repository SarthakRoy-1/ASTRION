"use client";

import { StatusPill } from "@/components/StatusPill";
import {
  scopeSummary,
  severityLabel,
  severityTone,
  signalTypeLabel,
} from "@/lib/operations-presentation";
import { trustDescriptor } from "@/lib/trust-presentation";
import type { OperationalSignal } from "@/lib/operations-types";

import styles from "./SignalList.module.css";

/**
 * The ranked inbox: what needs attention, most urgent first.
 *
 * Deliberately not a dashboard. There are no charts, no counters and no
 * time-series, because none of those answer "what should I do next" — a ranked
 * list with reasons does. Everything shown was computed by a deterministic
 * detector on the server; this component renders and never re-ranks,
 * re-scores, or infers.
 *
 * Each row carries the five things a support lead reads before deciding
 * whether to open it: how severe, what kind, how much it touches, how settled
 * the detection is, and what the server suggests doing. The priority number is
 * shown because the order has to be checkable — and it is explained in full,
 * factor by factor, in the detail beside it.
 */
export function SignalList({
  signals,
  selectedId,
  onSelect,
}: {
  signals: OperationalSignal[];
  selectedId: string | null;
  onSelect(signalId: string): void;
}) {
  return (
    <ul className={styles.list}>
      {signals.map((signal) => {
        const unsettled = signal.trust_status !== "confident";
        const trust = trustDescriptor(signal.trust_status);

        return (
          <li key={signal.signal_id}>
            <button
              type="button"
              className={styles.row}
              aria-current={signal.signal_id === selectedId ? "true" : undefined}
              onClick={() => onSelect(signal.signal_id)}
            >
              <span
                className={styles.rail}
                data-severity={signal.severity}
                aria-hidden="true"
              />

              <span className={styles.body}>
                <span className={styles.top}>
                  <StatusPill tone={severityTone(signal.severity)}>
                    {severityLabel(signal.severity)}
                  </StatusPill>
                  {/* Doubt travels with the claim, never a footnote below it. */}
                  {unsettled ? (
                    <StatusPill tone={trust.tone} quiet>
                      {trust.label}
                    </StatusPill>
                  ) : null}
                </span>

                <span className={styles.title}>{signal.title}</span>

                <span className={styles.meta}>
                  <span>{signalTypeLabel(signal.signal_type)}</span>
                  <span className={styles.divider} aria-hidden="true">
                    ·
                  </span>
                  <span>{scopeSummary(signal)}</span>
                </span>

                {signal.recommended_next_step ? (
                  <span className={styles.next}>
                    {signal.recommended_next_step}
                  </span>
                ) : null}
              </span>

              <span className={styles.priority}>
                <span className={styles.priorityValue}>
                  {signal.priority_score}
                </span>
                <span className={styles.priorityLabel}>priority</span>
              </span>
            </button>
          </li>
        );
      })}
    </ul>
  );
}
