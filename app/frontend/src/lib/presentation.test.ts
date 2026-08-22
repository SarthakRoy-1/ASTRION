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
  decisionVerdict,
  formatAmount,
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
    verification_reasons: [],
    overrides: [],
    evidence_chunk_ids: [],
    inputs: {},
  };

  it("reads a settled decision as a plain yes or no", () => {
    expect(decisionVerdict(base).label).toBe("Yes");
    expect(decisionVerdict({ ...base, applies: false }).label).toBe("No");
  });

  it("never reads a provisional credit as approved", () => {
    // The single most consequential rendering rule in the product: a figure
    // that came back with "verify this first" must not display as a yes.
    const provisional = { ...base, requires_verification: true, applies: true };

    expect(decisionVerdict(provisional).label).toBe("Uncertain");
    expect(decisionVerdict(provisional).tone).toBe("caution");
  });

  it("reads the recorded service-credit decision as uncertain", () => {
    const decision = fixtures.serviceCredit.policy_decisions![0]!;

    expect(decision.requires_verification).toBe(true);
    expect(decisionVerdict(decision).label).toBe("Uncertain");
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
