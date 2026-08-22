/**
 * Turning backend vocabulary into words a support agent reads.
 *
 * This module is *only* naming. It maps tool identifiers to categories,
 * statuses to tone, and authority tiers to labels — all of which are
 * presentation decisions the backend has no reason to make.
 *
 * What it deliberately does not do is decide anything. It never computes
 * eligibility, never ranks a source, never infers that evidence governs. Where
 * a label reflects authority, the value comes from the backend's own
 * `authority_tier` / `is_authoritative` fields; the UI renders that judgement,
 * it does not form one.
 */

import type { ActionState, PolicyDecisionView, SourceRef, ToolUse } from "./types";

/** Broad capability a tool belongs to, for the investigation summary. */
export type ToolCategory =
  | "structured_data"
  | "document_retrieval"
  | "policy_calculation"
  | "action_preparation"
  | "other";

interface ToolDescriptor {
  label: string;
  category: ToolCategory;
}

const TOOL_DESCRIPTORS: Record<string, ToolDescriptor> = {
  lookup_record: { label: "Record lookup", category: "structured_data" },
  lookup_record_provenance: { label: "Record provenance", category: "structured_data" },
  search_documents: { label: "Document search", category: "document_retrieval" },
  get_document_evidence: { label: "Document evidence", category: "document_retrieval" },
  evaluate_cancellation: { label: "Cancellation policy", category: "policy_calculation" },
  evaluate_service_credit: { label: "Service-credit policy", category: "policy_calculation" },
  prepare_escalation: { label: "Escalation prepared", category: "action_preparation" },
  prepare_ticket_note: { label: "Ticket note prepared", category: "action_preparation" },
};

export const CATEGORY_LABELS: Record<ToolCategory, string> = {
  structured_data: "Structured data",
  document_retrieval: "Document retrieval",
  policy_calculation: "Policy calculation",
  action_preparation: "Action preparation",
  other: "Other",
};

/**
 * A readable name for a tool.
 *
 * An unrecognised name falls back to its identifier made readable rather than
 * being hidden: a tool the UI has not been taught about still ran, and
 * silently dropping it from the investigation summary would misrepresent what
 * the agent did.
 */
export function toolLabel(toolName: string): string {
  const descriptor = TOOL_DESCRIPTORS[toolName];
  if (descriptor) return descriptor.label;
  return toolName.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
}

export function toolCategory(toolName: string): ToolCategory {
  return TOOL_DESCRIPTORS[toolName]?.category ?? "other";
}

export type Tone = "ok" | "caution" | "fail";

const STATUS_TONES: Record<string, Tone> = {
  ok: "ok",
  uncertain: "caution",
  no_evidence: "caution",
  not_found: "fail",
  forbidden: "fail",
  invalid_input: "fail",
  error: "fail",
};

const STATUS_LABELS: Record<string, string> = {
  ok: "Completed",
  uncertain: "Needs verification",
  no_evidence: "No matching evidence",
  not_found: "Not available in scope",
  forbidden: "Not permitted",
  invalid_input: "Rejected",
  error: "Failed",
};

export function toolStatusTone(status: string): Tone {
  return STATUS_TONES[status] ?? "caution";
}

export function toolStatusLabel(status: string): string {
  return STATUS_LABELS[status] ?? status.replace(/_/g, " ");
}

/** One row per capability used, in the order the agent first reached for it. */
export interface InvestigationStep {
  category: ToolCategory;
  categoryLabel: string;
  tools: string[];
  tone: Tone;
}

/**
 * Collapse the tool log into the capabilities that were exercised.
 *
 * Grouping by category rather than listing every call keeps a five-step
 * investigation readable while still being faithful — the raw step count is
 * reported alongside, so nothing is hidden by the grouping. A category's tone
 * is its worst outcome: one failed lookup among three should not read green.
 */
export function summariseInvestigation(tools: ToolUse[]): InvestigationStep[] {
  const order: ToolCategory[] = [];
  const grouped = new Map<ToolCategory, InvestigationStep>();

  const severity: Record<Tone, number> = { ok: 0, caution: 1, fail: 2 };

  for (const call of tools) {
    const category = toolCategory(call.tool_name);
    const label = toolLabel(call.tool_name);
    const tone = toolStatusTone(call.status);

    let entry = grouped.get(category);
    if (!entry) {
      entry = {
        category,
        categoryLabel: CATEGORY_LABELS[category],
        tools: [],
        tone: "ok",
      };
      grouped.set(category, entry);
      order.push(category);
    }
    if (!entry.tools.includes(label)) entry.tools.push(label);
    if (severity[tone] > severity[entry.tone]) entry.tone = tone;
  }

  return order.map((category) => grouped.get(category)!);
}

/** Heading for a policy decision card. */
export function decisionTitle(decision: PolicyDecisionView): string {
  return decision.decision_type === "cancellation" ? "Cancellation" : "Service credit";
}

/** The label for the decision's monetary figure, if it has one. */
export function decisionAmountLabel(decision: PolicyDecisionView): string {
  return decision.amount_label === "cancellation_fee" ? "Fee" : "Credit";
}

/**
 * How the decision's verdict should read.
 *
 * `requires_verification` wins over `applies`, because the backend returning a
 * provisional figure alongside "verify this first" must never be shown as a
 * settled yes. That is the single most important rendering rule in this file.
 */
export function decisionVerdict(decision: PolicyDecisionView): {
  label: string;
  tone: Tone;
} {
  if (decision.requires_verification || decision.outcome === "requires_verification") {
    return { label: "Uncertain", tone: "caution" };
  }
  if (decision.applies === true) return { label: "Yes", tone: "ok" };
  if (decision.applies === false) return { label: "No", tone: "ok" };
  return { label: decision.outcome.replace(/_/g, " "), tone: "caution" };
}

/**
 * Format money exactly as the backend computed it.
 *
 * `amount` arrives as a string because the policy engine works in `Decimal`;
 * parsing it into a JavaScript number here would reintroduce the float error
 * the backend went out of its way to avoid. So it is never parsed — only
 * trimmed of a redundant `.00` for display.
 */
export function formatAmount(currency: string, amount: string | null): string | null {
  if (amount === null || amount === undefined) return null;
  const trimmed = amount.replace(/\.00$/, "");
  return `${currency} ${trimmed}`;
}

const AUTHORITY_LABELS: Record<number, string> = {
  1: "Customer agreement",
  2: "Current support policy",
  3: "Current operational doc",
  4: "Not authoritative",
};

export function authorityLabel(tier: number): string {
  return AUTHORITY_LABELS[tier] ?? `Tier ${tier}`;
}

/**
 * Split evidence into what decided the answer and what is context.
 *
 * The split keys on `is_authoritative`, which the backend's authority layer
 * computed from what each document states about itself. The UI is showing that
 * decision, not making it — a deprecated policy stays in the list, clearly
 * marked, because explaining that a rule changed needs the rule that changed.
 */
export function splitEvidence(sources: SourceRef[]): {
  governing: SourceRef[];
  contextual: SourceRef[];
} {
  return {
    governing: sources.filter((source) => source.is_authoritative),
    contextual: sources.filter((source) => !source.is_authoritative),
  };
}

/** A document's display name: its title, falling back to the filename. */
export function sourceName(source: SourceRef): string {
  return source.document_title?.trim() || source.source_file;
}

export const ACTION_STATE_LABELS: Record<ActionState, string> = {
  none: "No action",
  pending_confirmation: "Awaiting confirmation",
  confirmed: "Confirmed",
  executed: "Executed",
  rejected: "Rejected",
  expired: "Expired",
  failed: "Failed",
};

const ACTION_TYPE_LABELS: Record<string, string> = {
  create_escalation: "Escalate ticket",
  add_ticket_note: "Add internal note to ticket",
};

export function actionTypeLabel(actionType: string): string {
  return (
    ACTION_TYPE_LABELS[actionType] ??
    actionType.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase())
  );
}

/** Past-tense confirmation of what an executed action did. */
export function actionOutcomeMessage(
  actionType: string,
  state: ActionState,
): string {
  if (state === "rejected") return "Rejected. Nothing was changed.";
  if (state === "failed") return "The action could not be completed.";
  if (state === "expired") return "The proposal expired before it was confirmed.";
  if (state !== "executed") return ACTION_STATE_LABELS[state];
  return actionType === "create_escalation"
    ? "Escalation created."
    : "Internal note added.";
}
