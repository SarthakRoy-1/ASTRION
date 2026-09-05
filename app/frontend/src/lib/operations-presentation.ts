/**
 * Operations vocabulary, written for a person.
 *
 * As with `presentation.ts`, this module only *names*. It never re-ranks a
 * signal, never re-scores one, never decides that something is urgent. Every
 * number and every basis string it labels was computed by a deterministic
 * detector on the server, and the interface's whole job is to render that
 * judgement legibly rather than to form one of its own.
 */

import type { PillTone } from "@/components/StatusPill";
import type {
  SignalRecord,
  SignalSeverity,
  SignalType,
} from "./operations-types";

/** How each signal type is written for a person. */
export const SIGNAL_TYPE_LABELS: Record<SignalType, string> = {
  sla_risk: "SLA risk",
  recurring_issue: "Recurring issue",
  cross_customer_issue: "Affects several customers",
  operational_anomaly: "Unusual pattern",
};

export function signalTypeLabel(type: SignalType | string): string {
  return (
    SIGNAL_TYPE_LABELS[type as SignalType] ??
    String(type).replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase())
  );
}

const SEVERITY_LABELS: Record<SignalSeverity, string> = {
  critical: "Critical",
  high: "High",
  medium: "Medium",
  low: "Low",
};

export function severityLabel(severity: SignalSeverity | string): string {
  return SEVERITY_LABELS[severity as SignalSeverity] ?? String(severity);
}

/**
 * Severity as a tone.
 *
 * `low` is neutral rather than green: a low-severity signal is not an
 * all-clear, it is a small problem, and a green chip beside four red ones
 * reads as "this one is fine".
 */
export function severityTone(severity: SignalSeverity | string): PillTone {
  switch (severity) {
    case "critical":
    case "high":
      return "fail";
    case "medium":
      return "caution";
    default:
      return "neutral";
  }
}

/**
 * The ranking factors, named.
 *
 * These arrive as wire identifiers — `affected_records`, `signal_type`,
 * `documented` — and were rendered raw, which made the one part of the product
 * whose whole purpose is to be checkable by a support lead read like a
 * database dump. The `basis` string beside each is the server's own
 * explanation and is never rewritten here.
 */
const FACTOR_LABELS: Record<string, string> = {
  severity: "How severe the detector rated it",
  affected_accounts: "How many customers it touches",
  affected_records: "How many records it touches",
  signal_type: "What kind of problem it is",
  documented: "Already documented",
  confidence: "How confident the detection is",
};

export function priorityFactorLabel(name: string): string {
  return (
    FACTOR_LABELS[name] ??
    name.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase())
  );
}

const RECORD_KIND_LABELS: Record<SignalRecord["kind"], string> = {
  account: "Account",
  order: "Order",
  ticket: "Ticket",
};

export function recordKindLabel(kind: SignalRecord["kind"] | string): string {
  return (
    RECORD_KIND_LABELS[kind as SignalRecord["kind"]] ??
    String(kind).replace(/^./, (c) => c.toUpperCase())
  );
}

/**
 * What a signal affects, as a short phrase.
 *
 * Assembled from the counts the server returned, and it says nothing when
 * there is nothing to say — "0 orders" on every row is noise that trains a
 * reader to stop reading the line.
 */
export function scopeSummary(signal: {
  affected_account_count: number;
  affected_ticket_count: number;
  affected_order_count: number;
}): string {
  const parts: string[] = [];
  const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;

  parts.push(plural(signal.affected_account_count, "account"));
  if (signal.affected_ticket_count > 0) {
    parts.push(plural(signal.affected_ticket_count, "ticket"));
  }
  if (signal.affected_order_count > 0) {
    parts.push(plural(signal.affected_order_count, "order"));
  }
  return parts.join(" · ");
}

/**
 * The question handed to the assistant when a signal is investigated.
 *
 * It *names* the signal rather than describing it. That is deliberate: the
 * agent then fetches the signal through its own tool, under its own scope,
 * instead of being told what the operations screen already believes — which
 * would let the browser's copy of a detection become the premise of an answer.
 */
export function investigationQuestion(signalId: string): string {
  return (
    `Investigate ${signalId}. What is happening, which customers are ` +
    `affected, and should we escalate?`
  );
}

/**
 * The dataset snapshot every detector measured against, formatted.
 *
 * Never the wall clock. "150 minutes overdue" is meaningless without saying
 * overdue relative to what, and this is that clock.
 */
export function formatReferenceTime(iso: string): string {
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return iso;
  return parsed.toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  });
}
