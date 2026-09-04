import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { TrustNotice } from "./TrustNotice";

import cancellation from "@/test/fixtures/chat-cancellation.json";
import crossAccountDenied from "@/test/fixtures/chat-cross-account-denied.json";
import knownIssue from "@/test/fixtures/chat-known-issue.json";
import provisionalCredit from "@/test/fixtures/chat-service-credit-provisional.json";
import slaBreach from "@/test/fixtures/chat-sla-breach.json";
import uncertain from "@/test/fixtures/chat-uncertain.json";

import type { ChatResponse } from "@/lib/types";

/**
 * The trust notice, driven by real recorded API responses.
 *
 * These fixtures are re-recorded from the live application by
 * `scripts/export_ui_fixtures.py`, so the trust blocks under test are ones the
 * backend actually produced — not shapes invented here that could drift from
 * what ships.
 */

const trustOf = (fixture: unknown) => (fixture as ChatResponse).trust;

describe("TrustNotice", () => {
  it("says nothing when the answer is settled and nothing overrode the default", () => {
    // A badge that appears on every answer stops being read. `known-issue` is
    // confident, tier 3, no agreement — the case where the answer speaks for
    // itself.
    const { container } = render(<TrustNotice trust={trustOf(knownIssue)} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("announces when a customer agreement governed instead of the standard policy", () => {
    render(<TrustNotice trust={trustOf(cancellation)} />);

    expect(screen.getByText("Customer agreement")).toBeInTheDocument();
    expect(
      screen.getByText(/signed agreement governs here, in place of the standard policy/i),
    ).toBeInTheDocument();
  });

  it("does not announce an agreement when none applied", () => {
    render(<TrustNotice trust={trustOf(uncertain)} />);
    expect(screen.queryByText("Customer agreement")).not.toBeInTheDocument();
  });

  it("marks a conditional answer and lists what must be checked", () => {
    const trust = trustOf(provisionalCredit);
    render(<TrustNotice trust={trust} />);

    expect(screen.getByText("Conditional")).toBeInTheDocument();
    expect(screen.getByText(/holds only if the points below are true/i)).toBeInTheDocument();
    // Every reason the backend gave is shown; none is summarised away.
    for (const reason of trust.reasons) {
      expect(screen.getByText(reason)).toBeInTheDocument();
    }
  });

  it("marks a missing-information answer rather than letting it read as settled", () => {
    render(<TrustNotice trust={trustOf(uncertain)} />);

    expect(screen.getByText("Not enough information")).toBeInTheDocument();
    expect(screen.getByText(/missing or unavailable/i)).toBeInTheDocument();
  });

  it("marks an escalation and states why a person is needed", () => {
    const trust = trustOf(slaBreach);
    render(<TrustNotice trust={trust} />);

    expect(screen.getByText("Needs a person")).toBeInTheDocument();
    if (trust.escalation_reason) {
      expect(screen.getByText(/Escalate because:/)).toBeInTheDocument();
    }
  });

  it("marks a cross-account refusal as missing information, not as an answer", () => {
    // The security-relevant rendering case: a refused lookup must not present
    // as a settled answer just because the prose reads calmly.
    render(<TrustNotice trust={trustOf(crossAccountDenied)} />);
    expect(screen.getByText("Not enough information")).toBeInTheDocument();
  });

  it("renders nothing at all when the response carries no trust block", () => {
    // Defensive: an older cached response, or a fixture recorded before the
    // field existed, must not crash the turn it belongs to.
    const { container } = render(
      <TrustNotice trust={undefined as unknown as ChatResponse["trust"]} />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("labels the region so the status is reachable without colour", () => {
    render(<TrustNotice trust={trustOf(slaBreach)} />);
    expect(
      screen.getByRole("region", { name: /answer reliability/i }),
    ).toBeInTheDocument();
  });
});
