import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ActionCard } from "./ActionCard";
import type { ActionProgress } from "@/hooks/useConversation";
import type { ProposedActionView } from "@/lib/types";

/**
 * The confirmation gate, and the manager threshold it now carries.
 *
 * Nothing here is a security boundary — the confirmation endpoint re-derives
 * the threshold from the policy engine under whoever is confirming, so a card
 * that got this wrong would produce a refusal rather than an unauthorised
 * payment. What it is responsible for is that a reviewer learns the
 * requirement *before* clicking, and that the interface never offers a button
 * certain to fail.
 */

function proposal(overrides: Partial<ProposedActionView> = {}): ProposedActionView {
  return {
    action_id: "ACT-abc123",
    action_type: "issue_service_credit",
    status: "pending_confirmation",
    account_id: "ACCT-002",
    target_type: "order",
    target_id: "ORD-2002",
    parameters: { amount: "300.00", currency: "INR", requires_manager_approval: "false" },
    preview: "Issue a service credit of INR 300.00 against order ORD-2002.",
    reason: null,
    confirmation_required: true,
    evidence_chunk_ids: [],
    expires_at_utc: "2030-01-01T00:00:00Z",
    parameter_fingerprint: "fp-1",
    ...overrides,
  } as ProposedActionView;
}

const pending: ActionProgress = {
  state: "pending_confirmation",
  submitting: null,
  executed: null,
  error: null,
};

function renderCard(options: {
  proposal?: ProposedActionView;
  canConfirm?: boolean;
  canApproveHighValue?: boolean;
} = {}) {
  const onRespond = vi.fn();
  render(
    <ActionCard
      proposal={options.proposal ?? proposal()}
      progress={pending}
      canConfirm={options.canConfirm ?? true}
      canApproveHighValue={options.canApproveHighValue ?? false}
      onRespond={onRespond}
    />,
  );
  return { onRespond, user: userEvent.setup() };
}

const overThreshold = proposal({
  parameters: {
    amount: "2500.00",
    currency: "INR",
    requires_manager_approval: "true",
  },
  preview:
    "Issue a service credit of INR 2500.00 against order ORD-2002. This exceeds the SOP threshold and requires manager approval.",
});

describe("a credit within the threshold", () => {
  it("is confirmable by anyone who may change state", async () => {
    renderCard();
    expect(
      screen.getByRole("button", { name: /confirm/i }),
    ).toBeInTheDocument();
    expect(screen.queryByText(/manager approval/i)).toBeNull();
  });

  it("still says nothing has happened yet", () => {
    renderCard();
    expect(
      screen.getByText(/nothing has been changed yet/i),
    ).toBeInTheDocument();
  });

  it("states the amount being approved", () => {
    renderCard();
    expect(screen.getByText(/INR 300\.00/)).toBeInTheDocument();
  });
});

describe("a credit above the threshold", () => {
  it("says the requirement before the reviewer clicks", () => {
    renderCard({ proposal: overThreshold });
    expect(screen.getByText("Manager approval")).toBeInTheDocument();
    expect(
      screen.getByText(/confirmed by someone with manager authority/i),
    ).toBeInTheDocument();
  });

  it("offers no confirm button to a caller without manager authority", () => {
    // Rendering a button certain to fail teaches people to distrust the UI.
    renderCard({ proposal: overThreshold, canApproveHighValue: false });

    expect(screen.queryByRole("button", { name: /confirm/i })).toBeNull();
    expect(
      screen.getByText(/not one above the SOP's manager-approval threshold/i),
    ).toBeInTheDocument();
  });

  it("cannot be confirmed through the UI by an unauthorised user", async () => {
    const { onRespond } = renderCard({
      proposal: overThreshold,
      canApproveHighValue: false,
    });
    expect(onRespond).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: /^reject$/i })).toBeNull();
  });

  it("is confirmable by a caller who holds manager authority", async () => {
    const { onRespond, user } = renderCard({
      proposal: overThreshold,
      canApproveHighValue: true,
    });

    // The requirement is still stated — it explains why this person is the
    // one being asked.
    expect(screen.getByText("Manager approval")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /confirm/i }));
    expect(onRespond).toHaveBeenCalledWith("approve");
  });
});

describe("the confirmation gate itself", () => {
  it("remains explicit: nothing executes without a click", () => {
    const { onRespond } = renderCard();
    expect(onRespond).not.toHaveBeenCalled();
    expect(screen.getByText(/only when you confirm it/i)).toBeInTheDocument();
  });

  it("offers rejection as well as approval", async () => {
    const { onRespond, user } = renderCard();
    await user.click(screen.getByRole("button", { name: /^reject$/i }));
    expect(onRespond).toHaveBeenCalledWith("reject");
  });

  it("withholds both controls from a role that cannot change state", () => {
    renderCard({ canConfirm: false });
    expect(screen.queryByRole("button", { name: /confirm/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /^reject$/i })).toBeNull();
    expect(
      screen.getByText(/cannot approve state-changing actions/i),
    ).toBeInTheDocument();
  });
});
