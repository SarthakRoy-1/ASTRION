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

import type {
  ActionState,
  PolicyDecisionView,
  ProposedActionView,
  SourceRef,
  ToolUse,
} from "./types";

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
const DECISION_TITLES: Record<string, string> = {
  cancellation: "Cancellation",
  service_credit: "Service credit",
  sla: "Response SLA",
};

export function decisionTitle(decision: PolicyDecisionView): string {
  return DECISION_TITLES[decision.decision_type] ?? "Service credit";
}

/**
 * The record a decision is about.
 *
 * Cancellation and service-credit decisions concern an order; an SLA decision
 * concerns a ticket. Exactly one is populated, so the card shows whichever it
 * was given rather than an empty slot.
 */
export function decisionSubject(decision: PolicyDecisionView): string {
  return decision.order_id ?? decision.ticket_id ?? "";
}

/** The label for the decision's monetary figure, if it has one. */
export function decisionAmountLabel(decision: PolicyDecisionView): string {
  return decision.amount_label === "cancellation_fee" ? "Fee" : "Credit";
}

/**
 * What the verdict row is a verdict *about*.
 *
 * A cancellation or credit decision answers "does this apply"; an SLA decision
 * answers "has the target been missed". Labelling the second one "Eligible"
 * made a breached SLA read as "Eligible: not allowed", which is not what the
 * backend said and not a sentence anyone can act on.
 */
export function decisionVerdictLabel(decision: PolicyDecisionView): string {
  if (decision.decision_type === "sla") return "First response";
  // Not "Cancellation": that is already the card's heading, and repeating it
  // as the field label makes the card say the same word twice about two
  // different things.
  if (decision.decision_type === "cancellation") return "Outcome";
  return "Eligible";
}

/**
 * How the decision's verdict should read.
 *
 * `requires_verification` wins over every other signal, because the backend
 * returning a provisional figure alongside "verify this first" must never be
 * shown as a settled yes. That is the single most important rendering rule in
 * this file, and it applies to an unresolved SLA exactly as it does to a
 * provisional credit.
 *
 * An SLA verdict is then driven by `breached`, which is deliberately tri-state
 * on the wire: `null` means the question was not settled — not that nothing was
 * breached — so it must never render as "Within target".
 */
export function decisionVerdict(decision: PolicyDecisionView): {
  label: string;
  tone: Tone;
} {
  if (decision.requires_verification || decision.outcome === "requires_verification") {
    return { label: "Uncertain", tone: "caution" };
  }
  if (decision.decision_type === "sla") {
    if (decision.breached === true) return { label: "Breached", tone: "fail" };
    if (decision.breached === false) return { label: "Within target", tone: "ok" };
    return { label: "Not determined", tone: "caution" };
  }
  // A cancellation's `applies` carries `fee_applies`, not "may this be
  // cancelled". Reading it as an eligibility verdict rendered an order that
  // could be cancelled free as "Eligible: No" — and rendered a DELIVERED
  // order, which cannot be cancelled at all, exactly the same way. The
  // question the reader is asking is answered by `outcome`.
  if (decision.decision_type === "cancellation") {
    if (decision.outcome === "allowed") return { label: "Allowed", tone: "ok" };
    if (decision.outcome === "not_allowed") return { label: "Not allowed", tone: "fail" };
    return { label: decision.outcome.replace(/_/g, " "), tone: "caution" };
  }
  if (decision.applies === true) return { label: "Yes", tone: "ok" };
  if (decision.applies === false) return { label: "No", tone: "ok" };
  return { label: decision.outcome.replace(/_/g, " "), tone: "caution" };
}

/**
 * The SLA facts worth showing beside the verdict, as label/value pairs.
 *
 * Empty for a decision that is not an SLA, so the card can render this
 * unconditionally without branching twice. Every value is passed through as the
 * backend computed it: `elapsed_minutes` arrives as a string for the same
 * reason money does, and is not parsed here.
 */
export function slaFacts(
  decision: PolicyDecisionView,
): { label: string; value: string }[] {
  if (decision.decision_type !== "sla") return [];
  const facts: { label: string; value: string }[] = [];
  if (decision.severity) facts.push({ label: "Severity", value: decision.severity });
  if (decision.target_text) facts.push({ label: "Target", value: decision.target_text });
  if (decision.elapsed_minutes !== null && decision.elapsed_minutes !== undefined) {
    facts.push({ label: "Elapsed", value: `${decision.elapsed_minutes} min` });
  }
  return facts;
}

/** How a nullable boolean-ish input reads on a decision card. */
function faultLabel(value: string | null | undefined): string {
  if (value === null || value === undefined || value === "") return "Unknown";
  return value === "True" ? "Confirmed" : "Ruled out";
}

/**
 * The inputs a verdict actually rested on, as label/value pairs.
 *
 * `decision.inputs` is the backend's own record of what it read — order
 * status, measured delay, whether fault was established. Showing a curated
 * subset answers "why" with the system's evidence rather than its prose.
 *
 * Deliberately curated rather than dumped: `inputs` also carries raw ISO
 * timestamps and `reference_time_source`, which would bury the two or three
 * facts a reader is actually looking for.
 *
 * The vocabulary here avoids "Yes" and "No" on purpose. Those words are the
 * service-credit *verdict*, and a fact reading "Yes" beside a verdict reading
 * "Yes" makes the card ambiguous about which question was answered.
 */
export function decisionFacts(
  decision: PolicyDecisionView,
): { label: string; value: string }[] {
  if (decision.decision_type === "sla") return slaFacts(decision);

  const inputs = decision.inputs ?? {};
  const facts: { label: string; value: string }[] = [];

  if (decision.decision_type === "cancellation") {
    if (inputs.order_status) {
      facts.push({ label: "Order status", value: inputs.order_status });
    }
    return facts;
  }

  if (decision.decision_type === "service_credit") {
    if (inputs.order_status) {
      facts.push({ label: "Order status", value: inputs.order_status });
    }
    if (inputs.delay_hours) {
      facts.push({ label: "Pickup delay", value: `${inputs.delay_hours} h` });
    }
    if ("carrier_fault" in inputs) {
      facts.push({ label: "Carrier fault", value: faultLabel(inputs.carrier_fault) });
    }
    if ("customer_fault" in inputs) {
      facts.push({ label: "Customer fault", value: faultLabel(inputs.customer_fault) });
    }
    return facts;
  }

  return facts;
}

/**
 * Format money exactly as the backend computed it.
 *
 * `amount` arrives as a string because the policy engine works in `Decimal`;
 * parsing it into a JavaScript number here would reintroduce the float error
 * the backend went out of its way to avoid. So it is never parsed — only
 * trimmed of a redundant `.00` for display.
 */
/**
 * Render a decision's monetary figure, or nothing.
 *
 * Both arguments are nullable because not every decision produces money — an
 * SLA decision has neither an amount nor a currency. A figure without its
 * currency is never rendered: "300" beside a credit is worse than silence.
 */
export function formatAmount(
  currency: string | null | undefined,
  amount: string | null | undefined,
): string | null {
  if (amount === null || amount === undefined) return null;
  if (currency === null || currency === undefined) return null;
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

/** One labelled line the backend appended to its answer. */
export interface AnswerNote {
  label: string;
  text: string;
}

/** An answer, separated into what it concluded and what it cited. */
export interface ParsedAnswer {
  /** The conclusion, in the backend's own words. */
  lead: string[];
  /** Rule, calculation, source and precedence lines, in order. */
  notes: AnswerNote[];
}

/**
 * The prefixes the backend uses when it appends provenance to an answer.
 *
 * Each of these is already rendered structurally elsewhere — `Rule applied`
 * and `Calculation` on the decision card, `Source` in the evidence section,
 * `Precedence` in the decision card's precedence block. Left inline they
 * outnumbered the conclusion roughly eight to one on an SLA answer.
 */
const ANSWER_NOTE_PREFIXES = ["Rule applied", "Calculation", "Source", "Precedence"];

/**
 * Separate an answer's conclusion from the citation lines beneath it.
 *
 * The split is purely by prefix, and anything unrecognised stays in `lead`.
 * That direction matters: a line the backend adds later must default to being
 * *shown*, never silently folded away.
 */
export function parseAnswer(answer: string): ParsedAnswer {
  const lead: string[] = [];
  const notes: AnswerNote[] = [];

  for (const raw of answer.split("\n")) {
    const line = raw.trim();
    if (!line) continue;

    const separator = line.indexOf(":");
    const prefix = separator === -1 ? "" : line.slice(0, separator).trim();

    if (separator !== -1 && ANSWER_NOTE_PREFIXES.includes(prefix)) {
      notes.push({ label: prefix, text: line.slice(separator + 1).trim() });
    } else {
      lead.push(line);
    }
  }

  return { lead, notes };
}

/** Compare two pieces of backend prose ignoring incidental whitespace. */
function sameText(value: string): string {
  return value.trim().replace(/\s+/g, " ").toLowerCase();
}

/**
 * Drop the notes that are already on screen as structure.
 *
 * Every `Rule applied` / `Calculation` / `Source` / `Precedence` line the
 * backend appends is normally also rendered by the decision card or the
 * evidence section — better, because there it is laid out rather than
 * concatenated. Showing both puts the same sentence on the page twice.
 *
 * The comparison is against what this response *actually renders*, not
 * against the prefix: a line the structured sections do not cover survives
 * and stays available. That direction is the point — deduplicating must never
 * be able to drop a citation the rest of the UI never showed.
 */
export function undisplayedNotes(
  notes: AnswerNote[],
  decisions: PolicyDecisionView[],
  sources: SourceRef[],
): AnswerNote[] {
  const rendered = new Set<string>();

  for (const decision of decisions) {
    if (decision.controlling_rule) rendered.add(sameText(decision.controlling_rule));
    if (decision.calculation) rendered.add(sameText(decision.calculation));
    for (const override of decision.overrides) rendered.add(sameText(override));
    for (const source of decision.controlling_sources) rendered.add(sameText(source));
  }

  for (const source of sources) {
    if (source.citation) rendered.add(sameText(source.citation));
  }

  return notes.filter((note) => !rendered.has(sameText(note.text)));
}

/**
 * Drop the answer line that only restates a prepared action.
 *
 * A `needs_confirmation` answer reads "Prepared action (NOT yet performed):
 * <preview>. Confirm action ACT-… to execute it." The action card states all
 * of that — what will happen, that nothing has happened yet, and the controls
 * to decide — without exposing the internal action id.
 *
 * Keyed on the proposal's own `preview` and `action_id` rather than on the
 * "Prepared action" wording, so a line that merely *mentions* the action in
 * passing is kept and only a genuine restatement is removed.
 */
export function withoutActionRestatement(
  lead: string[],
  proposal: ProposedActionView | null | undefined,
): string[] {
  if (!proposal) return lead;

  const preview = proposal.preview?.trim();

  return lead.filter((line) => {
    if (preview && line.includes(preview)) return false;
    if (proposal.action_id && line.includes(proposal.action_id)) return false;
    return true;
  });
}
