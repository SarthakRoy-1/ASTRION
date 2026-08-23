/**
 * End-to-end behaviour of the chat page, driven through the UI a user touches.
 *
 * Every response served here was recorded from the real backend
 * (`scripts/export_ui_fixtures.py`), so these tests check that the UI renders
 * what the API actually returns rather than what it would be convenient to
 * assume. No test reaches the network, and none needs an API key.
 */

import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import ChatPage from "./page";
import { chatCalls, confirmCalls, fixtures, stubApi } from "@/test/helpers";
import type { ChatResponse } from "@/lib/types";

async function renderPage() {
  const user = userEvent.setup();
  render(<ChatPage />);
  // The page cannot be used until the server's identity directory has loaded.
  await screen.findByRole("combobox", { name: /context/i });
  return user;
}

async function ask(user: ReturnType<typeof userEvent.setup>, message: string) {
  const input = screen.getByLabelText(/ask the parcelpilot support agent/i);
  await user.type(input, message);
  await user.click(screen.getByRole("button", { name: /^send$/i }));
}

describe("chat page", () => {
  it("renders the composer and the example prompts before anything is asked", async () => {
    stubApi();
    await renderPage();

    expect(
      screen.getByLabelText(/ask the parcelpilot support agent/i),
    ).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /try one of these/i })).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /can northstar cancel ord-1001/i }),
    ).toBeInTheDocument();
  });

  it("populates the context selector from the server's identity directory", async () => {
    stubApi();
    await renderPage();

    const select = screen.getByRole("combobox", { name: /context/i });
    const options = within(select).getAllByRole("option");

    expect(options.map((option) => option.textContent)).toEqual(
      fixtures.principals.principals!.map((p) => p.display_name),
    );
  });

  it("shows the active role and the accounts it is authorised for", async () => {
    stubApi();
    await renderPage();

    // Account isolation is a behaviour the demo has to make visible, not just
    // enforce, so the active scope is stated on screen.
    expect(await screen.findByText(/authorised accounts:/i)).toBeInTheDocument();
    expect(screen.getByText(/ACCT-001, ACCT-002, ACCT-003, ACCT-004/)).toBeInTheDocument();
  });

  it("sends the typed message under the selected identity", async () => {
    const stub = stubApi();
    const user = await renderPage();

    await ask(user, "Can ORD-1001 be cancelled?");

    await waitFor(() => expect(chatCalls(stub)).toHaveLength(1));
    const call = chatCalls(stub)[0]!;
    expect(call.body).toMatchObject({
      message: "Can ORD-1001 be cancelled?",
      user_id: "support.agent",
    });
    // The identity also travels as a header, which is what the backend reads.
    expect(call.identity).toBe("support.agent");
  });

  it("never sends an account scope from the browser", async () => {
    const stub = stubApi();
    const user = await renderPage();

    await ask(user, "Can ORD-1001 be cancelled?");
    await waitFor(() => expect(chatCalls(stub)).toHaveLength(1));

    // The server owns authorization. A scope proposed by the client — even a
    // narrower one — would blur where authority comes from.
    expect(chatCalls(stub)[0]!.body).not.toHaveProperty("account_scope");
  });

  it("shows the user's message and the agent's answer as distinct turns", async () => {
    stubApi();
    const user = await renderPage();

    await ask(user, "Can Northstar cancel ORD-1001?");

    const transcript = within(screen.getByRole("main"));
    expect(await transcript.findByText("Can Northstar cancel ORD-1001?")).toBeInTheDocument();
    expect(
      await transcript.findByText(/can be cancelled with no cancellation fee/i),
    ).toBeInTheDocument();
  });

  it("shows a loading state while the investigation runs", async () => {
    const stub = stubApi();
    let release: (() => void) | undefined;
    stub.onChat({ body: fixtures.cancellation });

    const user = await renderPage();
    const pending = new Promise<void>((resolve) => {
      release = resolve;
    });
    // Delay the first reply so the in-flight state is observable.
    const original = globalThis.fetch;
    globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url.includes("/api/chat")) await pending;
      return original(input, init);
    }) as typeof fetch;

    await ask(user, "Can ORD-1001 be cancelled?");

    await waitFor(() =>
      expect(screen.getAllByText(/investigating…/i).length).toBeGreaterThan(0),
    );
    release?.();
    await waitFor(() =>
      expect(screen.queryAllByText(/investigating…/i)).toHaveLength(0),
    );
  });
});

describe("response rendering", () => {
  it("renders evidence with its document, page and section", async () => {
    stubApi();
    const user = await renderPage();
    await ask(user, "Can Northstar cancel ORD-1001?");

    const sources = await screen.findByRole("region", { name: /sources/i });
    expect(
      within(sources).getAllByText(/Northstar Logistics Enterprise Agreement/).length,
    ).toBeGreaterThan(0);
    expect(
      within(sources).getAllByText(/2\. Shipment cancellation/).length,
    ).toBeGreaterThan(0);
  });

  it("separates governing evidence from context-only material", async () => {
    stubApi({ chat: [{ body: fixtures.supersededPolicy }] });
    const user = await renderPage();
    await ask(user, "What is the P1 first response target?");

    const sources = await screen.findByRole("region", { name: /sources/i });
    // A heading for each half, so outranked material can never read as though
    // it decided the answer.
    expect(
      within(sources).getByText("Governing", { selector: "p" }),
    ).toBeInTheDocument();
    expect(
      within(sources).getByText(/outranked or superseded/i),
    ).toBeInTheDocument();
  });

  it("marks a superseded document so it cannot read as current policy", async () => {
    stubApi({ chat: [{ body: fixtures.supersededPolicy }] });
    const user = await renderPage();
    await ask(user, "What is the P1 first response target?");

    const sources = await screen.findByRole("region", { name: /sources/i });
    const deprecated = within(sources)
      .getAllByRole("group")
      .find((card) => card.textContent?.includes("Deprecated"));

    expect(deprecated).toBeDefined();
    await user.click(within(deprecated!).getByText(/Page \d/));
    expect(
      within(deprecated!).getByText(/cannot be used as current policy/i),
    ).toBeInTheDocument();
  });

  it("expands an evidence card to reveal its excerpt", async () => {
    stubApi();
    const user = await renderPage();
    await ask(user, "Can Northstar cancel ORD-1001?");

    const sources = await screen.findByRole("region", { name: /sources/i });
    const first = within(sources).getAllByRole("group")[0]!;
    expect(first).not.toHaveAttribute("open");

    await user.click(within(first).getByText(/Page 1/));
    expect(first).toHaveAttribute("open");
    expect(within(first).getByText(/^Authority$/)).toBeInTheDocument();
  });

  it("renders a policy decision with its verdict, amount and rule", async () => {
    stubApi();
    const user = await renderPage();
    await ask(user, "Can Northstar cancel ORD-1001?");

    const decision = await screen.findByRole("region", { name: /cancellation decision/i });
    // The recorded order can be cancelled, and the agreement waives the fee.
    expect(within(decision).getByText("Outcome")).toBeInTheDocument();
    expect(within(decision).getByText("Allowed")).toBeInTheDocument();
    expect(within(decision).getByText("INR 0")).toBeInTheDocument();
    expect(within(decision).getByText("no fee")).toBeInTheDocument();
    expect(within(decision).getByText(/signed customer agreement waives/i)).toBeInTheDocument();

    // `applies` on a cancellation carries `fee_applies`, so labelling it
    // "Eligible" rendered this exact response — an order that CAN be
    // cancelled, free — as "Eligible: No".
    expect(within(decision).queryByText("Eligible")).not.toBeInTheDocument();
  });

  it("shows the order status a cancellation verdict rested on", async () => {
    stubApi();
    const user = await renderPage();
    await ask(user, "Can Northstar cancel ORD-1001?");

    const decision = await screen.findByRole("region", { name: /cancellation decision/i });
    expect(within(decision).getByText("Order status")).toBeInTheDocument();
    expect(within(decision).getByText("BOOKED")).toBeInTheDocument();
  });

  it("never renders an allowed and a refused cancellation the same way", async () => {
    // The regression this guards: `fee_applies` is false both when a fee was
    // waived and when the order cannot be cancelled at all, so a card driven
    // by `applies` drew these two opposite outcomes identically.
    const allowed = fixtures.cancellation;
    const refused: ChatResponse = {
      ...allowed,
      answer: "Order ORD-1001 has already been delivered and cannot be cancelled.",
      policy_decisions: [
        {
          ...allowed.policy_decisions![0]!,
          outcome: "not_allowed",
          applies: false,
          amount: null,
          inputs: { ...allowed.policy_decisions![0]!.inputs, order_status: "DELIVERED" },
          controlling_rule: "A DELIVERED order cannot be cancelled.",
        },
      ],
    };

    stubApi({ chat: [{ body: allowed }, { body: refused }] });
    const user = await renderPage();

    await ask(user, "Can Northstar cancel ORD-1001?");
    const first = await screen.findByRole("region", { name: /cancellation decision/i });
    expect(within(first).getByText("Allowed")).toBeInTheDocument();

    await ask(user, "What about a delivered order?");
    await waitFor(() =>
      expect(
        screen.getAllByRole("region", { name: /cancellation decision/i }),
      ).toHaveLength(2),
    );
    const second = screen.getAllByRole("region", { name: /cancellation decision/i })[1]!;

    expect(within(second).getByText("Not allowed")).toBeInTheDocument();
    expect(within(second).queryByText("Allowed")).not.toBeInTheDocument();
    expect(within(second).getByText("DELIVERED")).toBeInTheDocument();
    // No fee figure at all on an order that cannot be cancelled.
    expect(within(second).queryByText(/^INR /)).not.toBeInTheDocument();
  });

  it("does not repeat the rule, calculation and citations under the answer", async () => {
    stubApi();
    const user = await renderPage();
    await ask(user, "Can Northstar cancel ORD-1001?");

    await screen.findByRole("region", { name: /cancellation decision/i });

    // Everything the answer appended is rendered structurally by the decision
    // card and the sources section, so the residual block renders nothing.
    expect(screen.queryByText(/more from the agent/i)).not.toBeInTheDocument();

    // And the rule itself still appears exactly once.
    expect(
      screen.getAllByText(/signed customer agreement waives/i),
    ).toHaveLength(1);
  });

  it("shows the tool categories the agent used, without their arguments", async () => {
    stubApi();
    const user = await renderPage();
    await ask(user, "Can Northstar cancel ORD-1001?");

    const investigation = await screen.findByRole("region", { name: /investigation/i });
    expect(within(investigation).getByText(/3 steps/)).toBeInTheDocument();
    expect(within(investigation).getByText("Structured data")).toBeInTheDocument();
    expect(within(investigation).getByText("Document retrieval")).toBeInTheDocument();
    expect(within(investigation).getByText("Policy calculation")).toBeInTheDocument();

    // Arguments are part of the planning trace and must not be rendered.
    expect(within(investigation).queryByText(/ORD-1001/)).not.toBeInTheDocument();
  });

  it("reports the dataset reference time rather than today's date", async () => {
    stubApi();
    const user = await renderPage();
    await ask(user, "Can Northstar cancel ORD-1001?");

    expect(await screen.findByText(/^As of /)).toBeInTheDocument();
  });
});

describe("uncertainty", () => {
  it("renders uncertainty distinctly from an answer and from an error", async () => {
    stubApi({ chat: [{ body: fixtures.uncertain }] });
    const user = await renderPage();
    await ask(user, "Should I get a service credit?");

    const notice = await screen.findByRole("region", { name: /unable to determine/i });
    expect(notice).toBeInTheDocument();
    // Not an error: nothing failed, the agent declined to guess.
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("does not present a provisional credit as an approved one", async () => {
    stubApi({ chat: [{ body: fixtures.serviceCreditProvisional }] });
    const user = await renderPage();
    await ask(user, "Is ORD-2002 eligible for a service credit?");

    const decision = await screen.findByRole("region", { name: /service credit decision/i });
    expect(within(decision).getByText("Uncertain")).toBeInTheDocument();
    expect(within(decision).getByText("provisional")).toBeInTheDocument();
    expect(within(decision).getByText(/verify before committing/i)).toBeInTheDocument();
    expect(within(decision).queryByText("Yes")).not.toBeInTheDocument();
  });

  it("presents a breached SLA as breached, with the target and elapsed time", async () => {
    stubApi({ chat: [{ body: fixtures.slaBreach }] });
    const user = await renderPage();
    await ask(user, "TKT-501 is a P1. Has its first response SLA been breached?");

    const decision = await screen.findByRole("region", { name: /response sla decision/i });
    expect(within(decision).getByText("Breached")).toBeInTheDocument();
    expect(within(decision).getByText("15 minutes, 24x7")).toBeInTheDocument();
    expect(within(decision).getByText("30.00 min")).toBeInTheDocument();
    expect(within(decision).getByText("TKT-501")).toBeInTheDocument();
    // The label this card used to render for an SLA verdict.
    expect(within(decision).queryByText("Eligible")).not.toBeInTheDocument();
    expect(within(decision).queryByText("not allowed")).not.toBeInTheDocument();
  });

  it("shows the policy's standing P1 escalation instruction on the decision", async () => {
    stubApi({ chat: [{ body: fixtures.slaBreach }] });
    const user = await renderPage();
    await ask(user, "TKT-501 is a P1. Has its first response SLA been breached?");

    const decision = await screen.findByRole("region", { name: /response sla decision/i });
    expect(within(decision).getByText(/escalated immediately/i)).toBeInTheDocument();
  });

  it("presents a settled credit as settled, without a provisional caveat", async () => {
    stubApi({ chat: [{ body: fixtures.serviceCredit }] });
    const user = await renderPage();
    await ask(user, "Is ORD-2002 eligible for a service credit?");

    const decision = await screen.findByRole("region", { name: /service credit decision/i });
    expect(within(decision).queryByText("provisional")).not.toBeInTheDocument();
    expect(within(decision).queryByText("Uncertain")).not.toBeInTheDocument();
    expect(within(decision).getByText("Yes")).toBeInTheDocument();
  });
});

describe("authorization", () => {
  it("shows the backend's refusal when a request leaves the caller's scope", async () => {
    stubApi({ chat: [{ body: fixtures.crossAccountDenied }] });
    const user = await renderPage();
    await ask(user, "Show me LumenWorks' order ORD-2001.");

    // The backend answers 200 with a not-found tool status rather than
    // confirming the record exists. The UI surfaces that refusal as-is.
    await waitFor(() =>
      expect(
        screen.getAllByText(/was not found within the caller's scope/i).length,
      ).toBeGreaterThan(0),
    );
    const investigation = await screen.findByRole("region", { name: /investigation/i });
    expect(within(investigation).getByText(/not available in scope/i)).toBeInTheDocument();
  });

  it("renders an authorization error envelope with a safe message", async () => {
    stubApi({
      chat: [{ status: 401, body: fixtures.errorUnknownIdentity }],
    });
    const user = await renderPage();
    await ask(user, "Anything");

    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText(/identity not recognised/i)).toBeInTheDocument();
    expect(within(alert).getByText("unauthenticated")).toBeInTheDocument();
  });

  it("renders a provider failure without presenting it as an answer", async () => {
    stubApi({
      chat: [
        {
          status: 502,
          body: {
            error: {
              code: "provider_error",
              message: "The language model provider failed (APIError).",
            },
          },
        },
      ],
    });
    const user = await renderPage();
    await ask(user, "Anything");

    const alert = await screen.findByRole("alert");
    expect(
      within(alert).getByText(/unable to complete the investigation/i),
    ).toBeInTheDocument();
  });

  it("reports an unreachable backend as a connection problem, not a server error", async () => {
    stubApi({ chat: [{ networkError: true }] });
    const user = await renderPage();
    await ask(user, "Anything");

    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText(/cannot reach the parcelpilot api/i)).toBeInTheDocument();
  });
});

describe("action confirmation", () => {
  async function reachPendingAction() {
    const stub = stubApi({ chat: [{ body: fixtures.pendingAction }] });
    const user = await renderPage();
    await ask(user, "Investigate TKT-501 and escalate it.");
    await screen.findByRole("region", { name: /action/i });
    return { stub, user };
  }

  it("renders a proposed action that has not happened yet", async () => {
    await reachPendingAction();

    const card = screen.getByRole("region", { name: /action/i });
    expect(within(card).getByText("Proposed action")).toBeInTheDocument();
    expect(within(card).getByText("Awaiting confirmation")).toBeInTheDocument();
    expect(within(card).getByText(/nothing has been changed yet/i)).toBeInTheDocument();
    expect(within(card).getByText(/escalate ticket/i)).toBeInTheDocument();
    expect(within(card).getByText("TKT-501")).toBeInTheDocument();
  });

  it("lets the card speak for the action instead of repeating it in prose", async () => {
    await reachPendingAction();

    // The answer only restated the proposal and exposed the internal action
    // id. The card says all of it, better, and without the id.
    expect(screen.queryByText(/prepared action \(not yet performed\)/i)).not.toBeInTheDocument();
    expect(
      screen.queryByText(new RegExp(fixtures.pendingAction.proposed_action!.action_id)),
    ).not.toBeInTheDocument();

    const card = screen.getByRole("region", { name: /action/i });
    expect(within(card).getByText(/create an escalation against ticket/i)).toBeInTheDocument();
    expect(within(card).getByText(/nothing has been changed yet/i)).toBeInTheDocument();
  });

  it("offers explicit confirm and reject controls", async () => {
    await reachPendingAction();

    const card = screen.getByRole("region", { name: /action/i });
    expect(
      within(card).getByRole("button", { name: /confirm escalation/i }),
    ).toBeEnabled();
    expect(within(card).getByRole("button", { name: /^reject$/i })).toBeEnabled();
  });

  it("confirms through the confirmation endpoint with the reviewed fingerprint", async () => {
    const { stub, user } = await reachPendingAction();

    await user.click(screen.getByRole("button", { name: /confirm escalation/i }));

    await waitFor(() => expect(confirmCalls(stub)).toHaveLength(1));
    const call = confirmCalls(stub)[0]!;
    expect(call.url).toContain(fixtures.pendingAction.proposed_action!.action_id);
    expect(call.body).toMatchObject({
      decision: "approve",
      session_id: fixtures.pendingAction.session_id,
      expected_fingerprint:
        fixtures.pendingAction.proposed_action!.parameter_fingerprint,
    });
  });

  it("shows the executed outcome and stops offering confirmation", async () => {
    const { user } = await reachPendingAction();

    await user.click(screen.getByRole("button", { name: /confirm escalation/i }));

    expect(await screen.findByText(/escalation created\./i)).toBeInTheDocument();
    expect(screen.getByText("Executed")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /confirm escalation/i }),
    ).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^reject$/i })).not.toBeInTheDocument();
  });

  it("rejects without changing anything", async () => {
    const stub = stubApi({
      chat: [{ body: fixtures.pendingAction }],
      confirm: [
        {
          body: {
            ...fixtures.actionExecuted,
            action_status: "rejected",
            action: { ...fixtures.actionExecuted.action, status: "rejected", result: null },
          },
        },
      ],
    });
    const user = await renderPage();
    await ask(user, "Escalate TKT-501.");
    await screen.findByRole("region", { name: /action/i });

    await user.click(screen.getByRole("button", { name: /^reject$/i }));

    expect(await screen.findByText(/rejected\. nothing was changed\./i)).toBeInTheDocument();
    expect(confirmCalls(stub)[0]!.body).toMatchObject({ decision: "reject" });
  });

  it("prevents a second confirmation while the first is in flight", async () => {
    const { stub, user } = await reachPendingAction();

    // Hold the confirmation open so the buttons' disabled state is observable.
    let release: (() => void) | undefined;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    const original = globalThis.fetch;
    globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url.includes("/confirm")) await held;
      return original(input, init);
    }) as typeof fetch;

    const confirmButton = screen.getByRole("button", { name: /confirm escalation/i });
    await user.click(confirmButton);

    expect(await screen.findByRole("button", { name: /confirming…/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /^reject$/i })).toBeDisabled();

    release?.();
    await waitFor(() => expect(confirmCalls(stub)).toHaveLength(1));
  });

  it("stops presenting an action as pending once the backend says it is not", async () => {
    const { user } = await reachPendingAction();

    // The backend refuses a replayed confirmation. Leaving the card pending
    // would invite a click that can never succeed.
    const stub2 = stubApi({
      chat: [{ body: fixtures.pendingAction }],
      confirm: [{ status: 409, body: fixtures.errorActionNotPending }],
    });
    void stub2;

    await user.click(screen.getByRole("button", { name: /confirm escalation/i }));

    expect(await screen.findByText(/is executed, not pending confirmation/i)).toBeInTheDocument();
    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: /confirm escalation/i }),
      ).not.toBeInTheDocument(),
    );
  });

  it("tells a customer context that it cannot approve state changes", async () => {
    stubApi({ chat: [{ body: fixtures.pendingAction }] });
    const user = await renderPage();
    // A customer context cannot confirm: the backend would refuse it, and the
    // UI says so rather than offering a button that cannot work.
    await user.selectOptions(
      screen.getByRole("combobox", { name: /context/i }),
      "customer.northstar",
    );
    await ask(user, "Escalate TKT-501.");

    const card = await screen.findByRole("region", { name: /action/i });
    expect(
      within(card).getByText(/cannot approve state-changing actions/i),
    ).toBeInTheDocument();
    expect(
      within(card).queryByRole("button", { name: /confirm/i }),
    ).not.toBeInTheDocument();
  });
});

describe("session handling", () => {
  it("reuses the session the backend issued for later messages", async () => {
    const stub = stubApi({
      chat: [{ body: fixtures.cancellation }, { body: fixtures.knownIssue }],
    });
    const user = await renderPage();

    await ask(user, "First question");
    await waitFor(() => expect(chatCalls(stub)).toHaveLength(1));
    await ask(user, "Second question");
    await waitFor(() => expect(chatCalls(stub)).toHaveLength(2));

    // The first request has no session; the backend issues one, and every
    // later message carries it. That binding is what ties a prepared action
    // to this conversation.
    expect(chatCalls(stub)[0]!.body).not.toHaveProperty("session_id");
    expect(chatCalls(stub)[1]!.body).toMatchObject({
      session_id: fixtures.cancellation.session_id,
    });
  });

  it("starts a new session when the conversation is reset", async () => {
    const stub = stubApi({
      chat: [{ body: fixtures.cancellation }, { body: fixtures.knownIssue }],
    });
    const user = await renderPage();

    await ask(user, "First question");
    await screen.findByText(/can be cancelled with no cancellation fee/i);

    await user.click(screen.getByRole("button", { name: /new conversation/i }));
    // Cleared from the transcript. It is still reachable from history, which
    // lives in the header — see the conversation-history tests below.
    expect(
      within(screen.getByRole("main")).queryByText("First question"),
    ).not.toBeInTheDocument();

    await ask(user, "Fresh question");
    await waitFor(() => expect(chatCalls(stub)).toHaveLength(2));
    expect(chatCalls(stub)[1]!.body).not.toHaveProperty("session_id");
  });

  it("starts a new conversation when the context changes", async () => {
    const stub = stubApi({
      chat: [{ body: fixtures.cancellation }, { body: fixtures.knownIssue }],
    });
    const user = await renderPage();

    await ask(user, "First question");
    await screen.findByText(/can be cancelled with no cancellation fee/i);

    await user.selectOptions(
      screen.getByRole("combobox", { name: /context/i }),
      "customer.northstar",
    );

    // Carrying a session across an identity change would leave a proposal made
    // under one scope sitting in a conversation running under another.
    expect(screen.queryByText("First question")).not.toBeInTheDocument();
    await ask(user, "Second question");
    await waitFor(() => expect(chatCalls(stub)).toHaveLength(2));
    expect(chatCalls(stub)[1]!.body).toMatchObject({ user_id: "customer.northstar" });
    expect(chatCalls(stub)[1]!.body).not.toHaveProperty("session_id");
  });
});

describe("conversation history", () => {
  /** Open the history disclosure and return a scoped query set. */
  async function openHistory(user: ReturnType<typeof userEvent.setup>) {
    const summary = screen.getByText(/^Conversations$/);
    await user.click(summary);
    // Scope to the disclosure itself: several cards render their own <header>,
    // which jsdom also reports as role="banner".
    const panel = summary.closest("details");
    if (!panel) throw new Error("conversation history panel not found");
    return within(panel as HTMLElement);
  }

  function transcript() {
    return within(screen.getByRole("main"));
  }

  async function switchTo(
    user: ReturnType<typeof userEvent.setup>,
    userId: string,
  ) {
    await user.selectOptions(
      screen.getByRole("combobox", { name: /context/i }),
      userId,
    );
  }

  it("starts a new conversation empty while keeping the previous one", async () => {
    stubApi({ chat: [{ body: fixtures.cancellation }] });
    const user = await renderPage();

    await ask(user, "First question");
    await transcript().findByText(/can be cancelled with no cancellation fee/i);

    await user.click(screen.getByRole("button", { name: /new conversation/i }));

    // The new conversation is empty...
    expect(transcript().queryByText("First question")).not.toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: /ask about an order/i }),
    ).toBeInTheDocument();

    // ...and the previous one was preserved, not discarded.
    const history = await openHistory(user);
    expect(history.getByRole("button", { name: /First question/ })).toBeInTheDocument();
  });

  it("preserves a conversation when the context switches away and back", async () => {
    stubApi({
      chat: [{ body: fixtures.cancellation }, { body: fixtures.knownIssue }],
    });
    const user = await renderPage();

    // Northstar-scoped internal context asks two questions.
    await ask(user, "Northstar question one");
    await transcript().findByText(/can be cancelled with no cancellation fee/i);

    await switchTo(user, "customer.lumenworks");
    expect(transcript().queryByText("Northstar question one")).not.toBeInTheDocument();

    await ask(user, "LumenWorks question one");
    await waitFor(() =>
      expect(transcript().getByText("LumenWorks question one")).toBeInTheDocument(),
    );

    // Back to the first context: its transcript returns intact.
    await switchTo(user, "support.agent");
    await waitFor(() =>
      expect(transcript().getByText("Northstar question one")).toBeInTheDocument(),
    );
    expect(
      transcript().getByText(/can be cancelled with no cancellation fee/i),
    ).toBeInTheDocument();
    expect(transcript().queryByText("LumenWorks question one")).not.toBeInTheDocument();
  });

  it("never shows one context's history while another context is active", async () => {
    stubApi({
      chat: [{ body: fixtures.cancellation }, { body: fixtures.knownIssue }],
    });
    const user = await renderPage();

    await ask(user, "Northstar private question");
    await transcript().findByText(/can be cancelled with no cancellation fee/i);

    // LumenWorks must see neither the transcript nor the history entry.
    await switchTo(user, "customer.lumenworks");
    expect(screen.queryByText("Northstar private question")).not.toBeInTheDocument();

    await ask(user, "LumenWorks private question");
    await waitFor(() =>
      expect(transcript().getByText("LumenWorks private question")).toBeInTheDocument(),
    );

    const lumenHistory = await openHistory(user);
    expect(
      lumenHistory.queryByRole("button", { name: /Northstar private question/ }),
    ).not.toBeInTheDocument();
    expect(
      lumenHistory.getByRole("button", { name: /LumenWorks private question/ }),
    ).toBeInTheDocument();

    // ...and the reverse direction holds too.
    await switchTo(user, "support.agent");
    expect(screen.queryByText("LumenWorks private question")).not.toBeInTheDocument();
    const northstarHistory = await openHistory(user);
    expect(
      northstarHistory.queryByRole("button", { name: /LumenWorks private question/ }),
    ).not.toBeInTheDocument();
  });

  it("restores an earlier conversation's messages when it is selected", async () => {
    stubApi({
      chat: [{ body: fixtures.cancellation }, { body: fixtures.knownIssue }],
    });
    const user = await renderPage();

    await ask(user, "The older question");
    await transcript().findByText(/can be cancelled with no cancellation fee/i);

    await user.click(screen.getByRole("button", { name: /new conversation/i }));
    await ask(user, "The newer question");
    await waitFor(() =>
      expect(transcript().getByText("The newer question")).toBeInTheDocument(),
    );
    expect(transcript().queryByText("The older question")).not.toBeInTheDocument();

    const history = await openHistory(user);
    await user.click(history.getByRole("button", { name: /The older question/ }));

    // The earlier transcript comes back, agent turn and all.
    await waitFor(() =>
      expect(transcript().getByText("The older question")).toBeInTheDocument(),
    );
    expect(
      transcript().getByText(/can be cancelled with no cancellation fee/i),
    ).toBeInTheDocument();
    expect(transcript().queryByText("The newer question")).not.toBeInTheDocument();
  });

  it("marks which conversation is the current one", async () => {
    stubApi({
      chat: [{ body: fixtures.cancellation }, { body: fixtures.knownIssue }],
    });
    const user = await renderPage();

    await ask(user, "Older thread");
    await transcript().findByText(/can be cancelled with no cancellation fee/i);
    await user.click(screen.getByRole("button", { name: /new conversation/i }));
    await ask(user, "Newer thread");
    await waitFor(() =>
      expect(transcript().getByText("Newer thread")).toBeInTheDocument(),
    );

    const history = await openHistory(user);
    const current = history.getByRole("button", { name: /Newer thread/ });
    const older = history.getByRole("button", { name: /Older thread/ });

    expect(current).toHaveAttribute("aria-current", "true");
    expect(older).not.toHaveAttribute("aria-current");
  });

  it("gives each context its own independent session", async () => {
    const stub = stubApi({
      chat: [{ body: fixtures.cancellation }, { body: fixtures.knownIssue }],
    });
    const user = await renderPage();

    await ask(user, "First");
    await waitFor(() => expect(chatCalls(stub)).toHaveLength(1));

    // A second context starts its own session rather than inheriting one.
    await switchTo(user, "customer.lumenworks");
    await ask(user, "Second");
    await waitFor(() => expect(chatCalls(stub)).toHaveLength(2));

    expect(chatCalls(stub)[1]!.body).toMatchObject({ user_id: "customer.lumenworks" });
    expect(chatCalls(stub)[1]!.body).not.toHaveProperty("session_id");
  });

  it("resumes the original session when a context is returned to", async () => {
    const stub = stubApi({
      chat: [
        { body: fixtures.cancellation },
        { body: fixtures.knownIssue },
        { body: fixtures.knownIssue },
      ],
    });
    const user = await renderPage();

    await ask(user, "First");
    await waitFor(() => expect(chatCalls(stub)).toHaveLength(1));

    await switchTo(user, "customer.lumenworks");
    await ask(user, "Second");
    await waitFor(() => expect(chatCalls(stub)).toHaveLength(2));

    // Returning resumes that context's own session, not the other one's.
    await switchTo(user, "support.agent");
    await ask(user, "Third");
    await waitFor(() => expect(chatCalls(stub)).toHaveLength(3));

    expect(chatCalls(stub)[2]!.body).toMatchObject({
      user_id: "support.agent",
      session_id: fixtures.cancellation.session_id,
    });
  });

  it("offers no history until there is something to go back to", async () => {
    stubApi({ chat: [{ body: fixtures.cancellation }] });
    const user = await renderPage();

    // A single untouched conversation is the starting state, not history.
    expect(screen.queryByText(/^Conversations$/)).not.toBeInTheDocument();

    await ask(user, "Something");
    await transcript().findByText(/can be cancelled with no cancellation fee/i);
    expect(screen.getByText(/^Conversations$/)).toBeInTheDocument();
  });

  it("keeps history in memory only, so a remount starts clean", async () => {
    stubApi({ chat: [{ body: fixtures.cancellation }] });
    const user = await renderPage();

    await ask(user, "Before reload");
    await transcript().findByText(/can be cancelled with no cancellation fee/i);

    // A remount stands in for a browser reload: this feature is deliberately
    // client-side only, with no backend persistence behind it.
    cleanup();
    stubApi({ chat: [{ body: fixtures.cancellation }] });
    render(<ChatPage />);
    await screen.findByRole("combobox", { name: /context/i });

    expect(screen.queryByText("Before reload")).not.toBeInTheDocument();
    expect(screen.queryByText(/^Conversations$/)).not.toBeInTheDocument();
    void user;
  });
});
