/**
 * The naming layer, checked against real recorded payloads.
 *
 * These functions decide how the backend's vocabulary reads on screen. The
 * risk they carry is not a crash — it is a label that quietly misrepresents a
 * verdict, which is why the provisional-credit case is tested hardest.
 */

import { describe, expect, it } from "vitest";

import {
  authorityLabel,
  decisionFacts,
  decisionSubject,
  parseAnswer,
  undisplayedNotes,
  withoutActionRestatement,
  decisionTitle,
  decisionVerdict,
  decisionVerdictLabel,
  formatAmount,
  slaFacts,
  splitEvidence,
  summariseInvestigation,
  toolLabel,
  toolStatusTone,
} from "./presentation";
import { fixtures } from "@/test/helpers";
import type { PolicyDecisionView } from "./types";

describe("tool naming", () => {
  it("names every tool the recorded responses actually used", () => {
    const used = new Set(
      [
        ...fixtures.cancellation.tools_used!,
        ...fixtures.pendingAction.tools_used!,
        ...fixtures.serviceCredit.tools_used!,
      ].map((tool) => tool.tool_name),
    );

    for (const name of used) {
      // A fallback label is legible, but an unmapped tool means the UI has
      // fallen behind the registry.
      expect(toolLabel(name)).not.toBe(name);
    }
  });

  it("keeps an unknown tool visible rather than dropping it", () => {
    expect(toolLabel("some_future_tool")).toBe("Some future tool");
  });

  it("treats an out-of-scope lookup as a failure, not a caution", () => {
    expect(toolStatusTone("not_found")).toBe("fail");
    expect(toolStatusTone("forbidden")).toBe("fail");
    expect(toolStatusTone("uncertain")).toBe("caution");
    expect(toolStatusTone("ok")).toBe("ok");
  });
});

describe("investigation summary", () => {
  it("groups repeated calls into one capability row", () => {
    const steps = summariseInvestigation(fixtures.pendingAction.tools_used!);

    // The recorded response calls lookup_record twice; it is one capability.
    const structured = steps.find((step) => step.category === "structured_data");
    expect(structured).toBeDefined();
    expect(steps.filter((step) => step.category === "structured_data")).toHaveLength(1);
  });

  it("gives a group the worst tone among its calls", () => {
    const steps = summariseInvestigation(fixtures.crossAccountDenied.tools_used!);
    const structured = steps.find((step) => step.category === "structured_data");

    // One lookup was refused. The row must not read as clean.
    expect(structured?.tone).toBe("fail");
  });

  it("preserves the order capabilities were first reached for", () => {
    const steps = summariseInvestigation(fixtures.cancellation.tools_used!);

    expect(steps.map((step) => step.category)).toEqual([
      "structured_data",
      "policy_calculation",
      "document_retrieval",
    ]);
  });
});

describe("decision verdicts", () => {
  const base: PolicyDecisionView = {
    decision_type: "service_credit",
    order_id: "ORD-2002",
    account_id: "ACCT-002",
    outcome: "eligible",
    controlling_rule: "rule",
    controlling_sources: [],
    calculation: null,
    currency: "INR",
    amount: "300.00",
    amount_label: "service_credit",
    applies: true,
    requires_verification: false,
    requires_immediate_escalation: false,
    verification_reasons: [],
    overrides: [],
    evidence_chunk_ids: [],
    inputs: {},
  };

  it("reads a settled decision as a plain yes or no", () => {
    expect(decisionVerdict(base).label).toBe("Yes");
    expect(decisionVerdict({ ...base, applies: false }).label).toBe("No");
  });

  it("reads a cancellation from its outcome, not from the fee flag", () => {
    // `applies` on a cancellation is `fee_applies`. A waived fee and an order
    // that cannot be cancelled at all both arrive as `applies: false`, so the
    // verdict has to come from `outcome` or the two are indistinguishable.
    const waived: PolicyDecisionView = {
      ...base,
      decision_type: "cancellation",
      order_id: "ORD-1001",
      outcome: "allowed",
      applies: false,
      amount: "0.00",
      amount_label: "cancellation_fee",
    };
    const refused: PolicyDecisionView = { ...waived, outcome: "not_allowed", amount: null };

    expect(decisionVerdictLabel(waived)).toBe("Outcome");
    expect(decisionVerdict(waived).label).toBe("Allowed");
    expect(decisionVerdict(waived).tone).toBe("ok");

    expect(decisionVerdict(refused).label).toBe("Not allowed");
    expect(decisionVerdict(refused).tone).toBe("fail");

    // The regression itself: identical `applies`, opposite verdicts.
    expect(waived.applies).toBe(refused.applies);
    expect(decisionVerdict(waived).label).not.toBe(decisionVerdict(refused).label);
  });

  it("reads the recorded cancellation as allowed with no fee", () => {
    const decision = fixtures.cancellation.policy_decisions![0]!;

    expect(decision.applies).toBe(false);
    expect(decisionVerdict(decision).label).toBe("Allowed");
    expect(decisionVerdict(decision).tone).toBe("ok");
  });

  it("still defers to verification on an unsettled cancellation", () => {
    const unsettled: PolicyDecisionView = {
      ...base,
      decision_type: "cancellation",
      outcome: "requires_verification",
      requires_verification: true,
      applies: false,
    };

    expect(decisionVerdict(unsettled).label).toBe("Uncertain");
    expect(decisionVerdict(unsettled).tone).toBe("caution");
  });

  it("never reads a provisional credit as approved", () => {
    // The single most consequential rendering rule in the product: a figure
    // that came back with "verify this first" must not display as a yes.
    const provisional = { ...base, requires_verification: true, applies: true };

    expect(decisionVerdict(provisional).label).toBe("Uncertain");
    expect(decisionVerdict(provisional).tone).toBe("caution");
  });

  it("reads a recorded provisional service credit as uncertain", () => {
    const decision = fixtures.serviceCreditProvisional.policy_decisions![0]!;

    expect(decision.requires_verification).toBe(true);
    expect(decisionVerdict(decision).label).toBe("Uncertain");
  });

  it("reads a recorded settled service credit as a yes", () => {
    const decision = fixtures.serviceCredit.policy_decisions![0]!;

    expect(decision.requires_verification).toBe(false);
    expect(decisionVerdict(decision).label).toBe("Yes");
  });

  it("reads a breached SLA as breached, not as an eligibility verdict", () => {
    // Regression: `applies` is never set on an SLA decision, so this used to
    // fall through to the outcome string and render as "Eligible: not allowed"
    // — which states neither that a target exists nor that it was missed.
    const decision = fixtures.slaBreach.policy_decisions!.find(
      (d) => d.decision_type === "sla",
    )!;

    expect(decision.breached).toBe(true);
    expect(decisionVerdictLabel(decision)).toBe("First response");
    expect(decisionVerdict(decision).label).toBe("Breached");
    expect(decisionVerdict(decision).tone).toBe("fail");
  });

  it("reads an SLA within its target as within target", () => {
    const decision = fixtures.slaBreach.policy_decisions!.find(
      (d) => d.decision_type === "sla",
    )!;
    const within = { ...decision, breached: false, outcome: "allowed" };

    expect(decisionVerdict(within).label).toBe("Within target");
    expect(decisionVerdict(within).tone).toBe("ok");
  });

  it("never reads an unsettled SLA as within target", () => {
    // `breached: null` means the question was not answered — most often a
    // business-hours target with no calendar defined. Rendering that as
    // "Within target" would invent an all-clear.
    const decision = fixtures.slaBreach.policy_decisions!.find(
      (d) => d.decision_type === "sla",
    )!;
    const unsettled = { ...decision, breached: null, requires_verification: true };

    expect(decisionVerdict(unsettled).label).toBe("Uncertain");
    expect(decisionVerdict(unsettled).tone).toBe("caution");

    const noVerdict = { ...decision, breached: null, outcome: "allowed" };
    expect(decisionVerdict(noVerdict).label).toBe("Not determined");
  });

  it("surfaces the SLA facts a breach verdict rests on", () => {
    const decision = fixtures.slaBreach.policy_decisions!.find(
      (d) => d.decision_type === "sla",
    )!;
    const facts = Object.fromEntries(
      slaFacts(decision).map((f) => [f.label, f.value]),
    );

    expect(facts["Severity"]).toBe("P1");
    expect(facts["Target"]).toBe("15 minutes, 24x7");
    expect(facts["Elapsed"]).toBe("30.00 min");
  });

  it("adds no SLA facts to a decision about an order", () => {
    // The card renders slaFacts unconditionally, so cancellation and credit
    // cards depend on this being empty.
    expect(slaFacts(base)).toEqual([]);
    expect(decisionVerdictLabel(base)).toBe("Eligible");
  });

  it("surfaces the order status a cancellation rested on", () => {
    const decision = fixtures.cancellation.policy_decisions![0]!;
    const facts = Object.fromEntries(
      decisionFacts(decision).map((f) => [f.label, f.value]),
    );

    expect(facts["Order status"]).toBe("BOOKED");
  });

  it("surfaces the fault inputs a service credit rested on", () => {
    const settled = fixtures.serviceCredit.policy_decisions![0]!;
    const provisional = fixtures.serviceCreditProvisional.policy_decisions![0]!;

    const settledFacts = Object.fromEntries(
      decisionFacts(settled).map((f) => [f.label, f.value]),
    );
    const provisionalFacts = Object.fromEntries(
      decisionFacts(provisional).map((f) => [f.label, f.value]),
    );

    expect(settledFacts["Pickup delay"]).toBe("4.50 h");
    expect(settledFacts["Carrier fault"]).toBe("Confirmed");
    expect(settledFacts["Customer fault"]).toBe("Ruled out");

    // The unknown that forced verification is stated, not omitted.
    expect(provisionalFacts["Carrier fault"]).toBe("Unknown");
  });

  it("keeps fact values clear of the words a verdict uses", () => {
    // A fact reading "Yes" beside a service-credit verdict reading "Yes"
    // leaves the card ambiguous about which question was answered.
    for (const decision of [
      fixtures.serviceCredit.policy_decisions![0]!,
      fixtures.serviceCreditProvisional.policy_decisions![0]!,
      fixtures.cancellation.policy_decisions![0]!,
    ]) {
      for (const fact of decisionFacts(decision)) {
        expect(["Yes", "No"]).not.toContain(fact.value);
      }
    }
  });

  it("delegates SLA decisions to the SLA facts", () => {
    const decision = fixtures.slaBreach.policy_decisions!.find(
      (d) => d.decision_type === "sla",
    )!;

    expect(decisionFacts(decision)).toEqual(slaFacts(decision));
  });

  it("names an SLA decision for what it is", () => {
    // A decision about a ticket's response target must not be captioned
    // "Service credit" just because it is not a cancellation.
    const sla: PolicyDecisionView = {
      ...base,
      decision_type: "sla",
      order_id: null,
      ticket_id: "TKT-501",
      amount: null,
      amount_label: null,
      currency: null,
    };

    expect(decisionTitle(sla)).toBe("Response SLA");
    expect(decisionSubject(sla)).toBe("TKT-501");
    expect(formatAmount(sla.currency, sla.amount)).toBeNull();
  });

  it("names the order a decision about an order concerns", () => {
    expect(decisionSubject(base)).toBe("ORD-2002");
  });
});

describe("money", () => {
  it("renders the backend's decimal string without parsing it", () => {
    // Parsing into a JS number would reintroduce the float error the policy
    // engine works in Decimal to avoid.
    expect(formatAmount("INR", "0.00")).toBe("INR 0");
    expect(formatAmount("INR", "300.00")).toBe("INR 300");
    expect(formatAmount("INR", "1234.56")).toBe("INR 1234.56");
  });

  it("renders nothing when no amount was computed", () => {
    expect(formatAmount("INR", null)).toBeNull();
  });

  it("renders nothing when the decision carries no currency", () => {
    // An SLA decision has neither. A bare "300" beside a credit would be
    // worse than showing nothing at all.
    expect(formatAmount(null, "300.00")).toBeNull();
    expect(formatAmount(undefined, "300.00")).toBeNull();
  });
});

describe("evidence", () => {
  it("splits on the backend's own authority decision", () => {
    const { governing, contextual } = splitEvidence(fixtures.supersededPolicy.sources!);

    expect(governing.length).toBeGreaterThan(0);
    expect(contextual.length).toBeGreaterThan(0);
    expect(contextual.every((source) => !source.is_authoritative)).toBe(true);
    expect(contextual.some((source) => source.is_deprecated)).toBe(true);
  });

  it("labels each authority tier", () => {
    expect(authorityLabel(1)).toBe("Customer agreement");
    expect(authorityLabel(4)).toBe("Not authoritative");
    expect(authorityLabel(9)).toBe("Tier 9");
  });
});

describe("answer notes", () => {
  it("drops every note the decision card and sources already render", () => {
    const response = fixtures.cancellation;
    const { notes } = parseAnswer(response.answer);

    // The recorded answer really does append all four kinds.
    expect(notes.map((n) => n.label)).toEqual([
      "Rule applied",
      "Calculation",
      "Source",
      "Precedence",
    ]);

    expect(
      undisplayedNotes(notes, response.policy_decisions!, response.sources!),
    ).toEqual([]);
  });

  it("leaves nothing over on a response with three precedence lines", () => {
    const response = fixtures.slaBreach;
    const { notes } = parseAnswer(response.answer);

    expect(notes.length).toBeGreaterThan(4);
    expect(
      undisplayedNotes(notes, response.policy_decisions!, response.sources!),
    ).toEqual([]);
  });

  it("keeps a citation nothing else on screen displays", () => {
    // The safety property: deduplication must never be able to drop a line
    // the structured sections never rendered.
    const orphan = { label: "Source", text: "99_Unlisted_Document.pdf p.4" };
    const response = fixtures.cancellation;

    expect(
      undisplayedNotes([orphan], response.policy_decisions!, response.sources!),
    ).toEqual([orphan]);
  });
});

describe("prepared-action prose", () => {
  it("removes the answer line that only restates the proposal", () => {
    const response = fixtures.pendingAction;
    const { lead } = parseAnswer(response.answer);

    expect(lead).toHaveLength(1);
    expect(lead[0]).toContain(response.proposed_action!.action_id);

    expect(withoutActionRestatement(lead, response.proposed_action)).toEqual([]);
  });

  it("keeps answer lines that are not the restatement", () => {
    const proposal = fixtures.pendingAction.proposed_action!;
    const lead = ["The outage matches KI-211.", "Something else entirely."];

    expect(withoutActionRestatement(lead, proposal)).toEqual(lead);
  });

  it("leaves an answer untouched when no action was proposed", () => {
    const { lead } = parseAnswer(fixtures.cancellation.answer);

    expect(withoutActionRestatement(lead, null)).toEqual(lead);
    expect(lead.length).toBeGreaterThan(0);
  });
});
