import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import SupportPage from "./page";
import { chatCalls, fixtures, stubApi } from "@/test/helpers";
import { setTestRoute } from "@/test/next-navigation";
import { renderApp } from "@/test/render";

/**
 * The application shell under real session authentication.
 *
 * This is the mode a deployment actually runs, and until Phase 4 almost
 * nothing exercised it: every page test ran against the demo identity header,
 * where `/api/principals` returns personas and the composer waits for one to be
 * chosen. Under session authentication that directory is empty by design —
 * identity comes from an `HttpOnly` cookie — and the interface has to work
 * anyway.
 */

async function renderSupport(options: Parameters<typeof stubApi>[0] = {}) {
  const user = userEvent.setup();
  setTestRoute("/");
  const stub = stubApi({ session: {}, ...options });
  renderApp(<SupportPage />);
  // Wait for the session itself, not just for the shell: the workspace name is
  // the first thing on screen that only appears once identity, memberships and
  // the active workspace have all resolved.
  await screen.findByText("Northstar Logistics");
  return { user, stub };
}

describe("workspace context", () => {
  it("names the active workspace and the caller's role in it", async () => {
    await renderSupport();

    const banner = screen.getByRole("banner");
    expect(
      within(banner).getByText("Northstar Logistics"),
    ).toBeInTheDocument();
    // The same person can hold a different role in another workspace, so the
    // one in force is stated rather than implied.
    expect(within(banner).getByText("Operations")).toBeInTheDocument();
    expect(within(banner).getByText("Ada Support")).toBeInTheDocument();
  });

  it("shows the accounts the session may reach", async () => {
    await renderSupport();

    const scope = await screen.findByRole("region", { name: /accounts in scope/i });
    expect(await within(scope).findByText("ACCT-001")).toBeInTheDocument();
    expect(within(scope).getByText("ACCT-002")).toBeInTheDocument();
  });

  it("says plainly when a workspace has no records attached", async () => {
    // A new workspace is empty, and the assistant will report that it cannot
    // find an order. Saying so up front is the difference between an expected
    // state and an apparent fault.
    await renderSupport({ session: { user: { account_scope: [] } } });

    const scope = await screen.findByRole("region", { name: /accounts in scope/i });
    expect(within(scope).getByText(/none has been attached/i)).toBeInTheDocument();
  });

  it("states what the role grants rather than leaving it to be discovered", async () => {
    await renderSupport();

    const permissions = await screen.findByRole("region", {
      name: /what you can do here/i,
    });
    expect(
      within(permissions).getByText("Confirm and execute prepared actions"),
    ).toBeInTheDocument();
    // Wire identifiers never reach the screen.
    expect(screen.queryByText("execute_action")).not.toBeInTheDocument();
  });
});

describe("primary navigation", () => {
  it("marks the area the reader is in", async () => {
    await renderSupport();

    const nav = screen.getByRole("navigation", { name: /primary/i });
    expect(within(nav).getByRole("link", { name: "Support" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(
      within(nav).getByRole("link", { name: "Operations" }),
    ).not.toHaveAttribute("aria-current");
  });

  it("hides operations from a role that cannot read it", async () => {
    await renderSupport({
      session: { permissions: ["run_agent", "members.read"] },
    });

    const nav = screen.getByRole("navigation", { name: /primary/i });
    expect(within(nav).queryByRole("link", { name: "Operations" })).toBeNull();
    // Workspace stays: seeing your own role and what it grants is not gated.
    expect(within(nav).getByRole("link", { name: "Workspace" })).toBeInTheDocument();
  });

  it("offers a way past the navigation to the content", async () => {
    await renderSupport();
    expect(
      screen.getByRole("link", { name: /skip to main content/i }),
    ).toHaveAttribute("href", "#main");
  });
});

describe("the assistant under session authentication", () => {
  it("sends a message without waiting for a demo persona", async () => {
    // The regression this exists for: `/api/principals` is empty under session
    // auth, and gating the composer on a persona left it enabled and silent —
    // no request, no error, nothing.
    const { user, stub } = await renderSupport();

    const input = screen.getByLabelText(/ask the parcelpilot support agent/i);
    await user.type(input, "Can ORD-1001 be cancelled?");
    await user.click(screen.getByRole("button", { name: /^send$/i }));

    await waitFor(() => expect(chatCalls(stub)).toHaveLength(1));
    // Identity travels in the cookie. Sending an empty `user_id` would put a
    // meaningless value on the wire that the server ignores anyway.
    expect(chatCalls(stub)[0]!.body).not.toHaveProperty("user_id");
    expect(chatCalls(stub)[0]!.identity).toBeNull();
    expect(
      await screen.findByText(/can be cancelled with no cancellation fee/i),
    ).toBeInTheDocument();
  });

  it("offers confirmation to a role whose workspace permits it", async () => {
    const { user } = await renderSupport({
      chat: [{ body: fixtures.pendingAction }],
    });

    const input = screen.getByLabelText(/ask the parcelpilot support agent/i);
    await user.type(input, "Investigate TKT-501 and escalate it.");
    await user.click(screen.getByRole("button", { name: /^send$/i }));

    expect(
      await screen.findByRole("button", { name: /confirm escalation/i }),
    ).toBeInTheDocument();
  });

  it("withholds confirmation from a role whose workspace does not permit it", async () => {
    // Presentation only — the backend re-checks and refuses regardless. The
    // point is not to offer a button that cannot work.
    const { user } = await renderSupport({
      chat: [{ body: fixtures.pendingAction }],
      session: { permissions: ["run_agent"] },
    });

    const input = screen.getByLabelText(/ask the parcelpilot support agent/i);
    await user.type(input, "Investigate TKT-501 and escalate it.");
    await user.click(screen.getByRole("button", { name: /^send$/i }));

    expect(
      await screen.findByText(/cannot approve state-changing actions/i),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /confirm escalation/i }),
    ).toBeNull();
  });

  it("offers no demo persona picker when there are no personas", async () => {
    await renderSupport();
    // The old header rendered a permanently disabled "Loading…" select here,
    // for a directory that is empty by design.
    expect(screen.queryByRole("combobox", { name: /context/i })).toBeNull();
  });
});

describe("a backend that is still waking", () => {
  it(
    "waits it out under session authentication too, without a sign-in form",
    async () => {
      // The hosted deployment spins down when idle. A backend that cannot be
      // reached yet is not a signed-out user, and showing a sign-in form at
      // that moment is what makes a cold start look like a logout.
      setTestRoute("/");
      stubApi({ session: {}, sleeping: 2 });
      renderApp(<SupportPage />);

      await screen.findByText(/waking the parcelpilot api/i, undefined, {
        timeout: 4000,
      });
      expect(screen.queryByRole("button", { name: /^sign in$/i })).toBeNull();

      // And it recovers on its own, with no reload.
      await screen.findByText("Northstar Logistics", undefined, {
        timeout: 8000,
      });
      await waitFor(() =>
        expect(
          screen.queryByText(/waking the parcelpilot api/i),
        ).not.toBeInTheDocument(),
      );
    },
    15_000,
  );
});
