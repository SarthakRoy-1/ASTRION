/**
 * Types for operations intelligence.
 *
 * Mirrors what `GET /api/operations/signals` returns. Everything here was
 * produced by a deterministic detector reading real records — there is no
 * model output in this shape, and the UI must not present any of it as an
 * inference.
 */

export type SignalType =
  | "sla_risk"
  | "recurring_issue"
  | "cross_customer_issue"
  | "operational_anomaly";

export type SignalSeverity = "critical" | "high" | "medium" | "low";

export interface PriorityFactor {
  name: string;
  /** What this contributed to the total. Negative values are reductions. */
  points: number;
  /** The observation that earned it, e.g. "3 accounts affected". */
  basis: string;
}

export interface SignalRecord {
  kind: "account" | "order" | "ticket";
  record_id: string;
  account_id: string | null;
  label: string | null;
}

export interface OperationalSignal {
  signal_id: string;
  signal_type: SignalType;
  severity: SignalSeverity;
  /** Additive and fully itemised by `priority_factors` — never opaque. */
  priority_score: number;
  priority_factors: PriorityFactor[];
  title: string;
  /** The rule that fired and the numbers behind it. */
  detail: string;
  affected_account_ids: string[];
  affected_account_count: number;
  affected_ticket_count: number;
  affected_order_count: number;
  records: SignalRecord[];
  documentation_chunk_ids: string[];
  first_observed_at: string | null;
  last_observed_at: string | null;
  /** Phase 2 trust vocabulary, reused rather than parallelled. */
  trust_status: string;
  trust_reasons: string[];
  /** Advice a person reads. It triggers nothing. */
  recommended_next_step: string | null;
}

export interface SignalReport {
  signals: OperationalSignal[];
  count: number;
  highest_severity: SignalSeverity | null;
  /** The dataset snapshot every detector measured against — not the wall clock. */
  reference_time: string | null;
  scope_account_ids: string[];
}

/** How each signal type is written for a person. */
export const SIGNAL_TYPE_LABELS: Record<SignalType, string> = {
  sla_risk: "SLA risk",
  recurring_issue: "Recurring issue",
  cross_customer_issue: "Affects several customers",
  operational_anomaly: "Unusual pattern",
};

/** Display order: most urgent band first. */
export const SEVERITY_ORDER: SignalSeverity[] = [
  "critical",
  "high",
  "medium",
  "low",
];
