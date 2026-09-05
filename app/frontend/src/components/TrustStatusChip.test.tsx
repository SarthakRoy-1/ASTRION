import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { TrustStatusChip } from "./TrustStatusChip";

import cancellation from "@/test/fixtures/chat-cancellation.json";
import knownIssue from "@/test/fixtures/chat-known-issue.json";
import provisionalCredit from "@/test/fixtures/chat-service-credit-provisional.json";
import slaBreach from "@/test/fixtures/chat-sla-breach.json";
import uncertain from "@/test/fixtures/chat-uncertain.json";

import type { ChatResponse } from "@/lib/types";

/**
 * The reliability chip in an answer's byline.
 *
 * `TrustNotice` stays silent on a settled answer, deliberately — a bordered
 * block on every answer stops being read. But the five states still have to be
 * tellable apart at a glance, so the status itself lives in the byline beside
 * the timestamp, where it reads as metadata rather than as an interruption.
 *
 * Driven by responses recorded from the real backend, so what is under test is
 * a trust block the API actually produced.
 */

const trustOf = (fixture: unknown) => (fixture as ChatResponse).trust;

describe("TrustStatusChip", () => {
  it("names a settled answer as confident, where the notice says nothing", () => {
    render(<TrustStatusChip trust={trustOf(knownIssue)} />);
    expect(screen.getByText("Confident")).toBeInTheDocument();
  });

  it("distinguishes each unsettled state by name, not by colour", () => {
    const cases: [unknown, string][] = [
      [provisionalCredit, "Conditional"],
      [uncertain, "Not enough information"],
      [slaBreach, "Needs a person"],
    ];

    for (const [fixture, label] of cases) {
      const { unmount } = render(<TrustStatusChip trust={trustOf(fixture)} />);
      expect(screen.getByText(label)).toBeInTheDocument();
      unmount();
    }
  });

  it("says which authority decided the answer", () => {
    // "Confident" alone leaves the more useful half of the question
    // unanswered: whether a signed agreement or the standard policy governed
    // is what changes what a support agent tells the customer.
    render(<TrustStatusChip trust={trustOf(cancellation)} />);
    expect(
      screen.getByText(/signed agreement/i),
    ).toBeInTheDocument();
  });

  it("names the standard policy when that is what governed", () => {
    render(<TrustStatusChip trust={trustOf(slaBreach)} />);
    expect(screen.getByText(/decided by/i)).toBeInTheDocument();
  });

  it("renders nothing when the response carries no trust block", () => {
    // Defensive: an older cached response, or a fixture recorded before the
    // field existed, must not crash the turn it belongs to.
    const { container } = render(
      <TrustStatusChip trust={undefined as unknown as ChatResponse["trust"]} />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("never reports an unknown status as confident", () => {
    // The most damaging default available: a state this interface has not been
    // taught about must not be flattened onto the reassuring end of the scale.
    render(
      <TrustStatusChip
        trust={{
          ...trustOf(knownIssue),
          status: "something_new",
        } as unknown as ChatResponse["trust"]}
      />,
    );
    expect(screen.queryByText("Confident")).toBeNull();
    expect(screen.getByText("something new")).toBeInTheDocument();
  });
});
