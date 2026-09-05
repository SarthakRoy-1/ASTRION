import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { VerifyEmailPrompt } from "./VerifyEmailPrompt";

/**
 * What a newly registered user is told.
 *
 * This application sends no email in any environment — there is no SMTP
 * client, no provider SDK and no mail setting in the codebase. The screen's
 * whole job is therefore to be truthful about that while still leaving the
 * user a way forward, and the two branches are decided by what the server
 * returned, never by a flag in the browser.
 */

const MESSAGE = "If that address is available, an account was created and a verification link issued.";

function stubVerify(reply: { ok: boolean; body?: unknown }) {
  const fetchMock = vi.fn(async () => ({
    ok: reply.ok,
    status: reply.ok ? 200 : 400,
    statusText: "",
    json: async () => reply.body ?? { status: "verified" },
  }));
  vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);
  return fetchMock;
}

describe("after registering, when the link was returned", () => {
  function renderIssued() {
    const user = userEvent.setup();
    const onDone = vi.fn();
    render(
      <VerifyEmailPrompt
        email="ada@example.com"
        message={MESSAGE}
        verificationToken="tok-abc-123"
        onDone={onDone}
      />,
    );
    return { user, onDone };
  }

  it("says plainly that nothing was emailed", async () => {
    renderIssued();
    expect(
      screen.getByText(/this deployment sends no email/i),
    ).toBeInTheDocument();
    // The wording it replaced. Telling someone to check an inbox nothing was
    // sent to is the most misleading thing this screen could do.
    expect(screen.queryByText(/check your inbox/i)).toBeNull();
  });

  it("shows the link, pointing at the route an emailed link would land on", async () => {
    renderIssued();

    const link = screen.getByRole("link", { name: /\/verify-email\?token=/ });
    expect(link).toHaveAttribute(
      "href",
      expect.stringContaining("/verify-email?token=tok-abc-123"),
    );
    // Shown in full so it can be copied into another browser or a curl call.
    expect(link).toHaveTextContent("/verify-email?token=tok-abc-123");
  });

  it("percent-encodes a token that would otherwise break the query", async () => {
    render(
      <VerifyEmailPrompt
        email="ada@example.com"
        message={MESSAGE}
        verificationToken="a+b/c=d&e"
        onDone={vi.fn()}
      />,
    );
    expect(screen.getByRole("link")).toHaveAttribute(
      "href",
      expect.stringContaining("token=a%2Bb%2Fc%3Dd%26e"),
    );
  });

  it("also verifies in place, and confirms which address was settled", async () => {
    stubVerify({ ok: true });
    const { user } = renderIssued();

    await user.click(
      screen.getByRole("button", { name: /verify this address/i }),
    );

    expect(
      await screen.findByRole("heading", { name: /address verified/i }),
    ).toBeInTheDocument();
    expect(screen.getByText("ada@example.com")).toBeInTheDocument();
  });

  it("shows the server's refusal rather than guessing at it", async () => {
    stubVerify({
      ok: false,
      body: {
        error: {
          code: "invalid_request",
          message: "That verification link is invalid or has expired.",
        },
      },
    });
    const { user } = renderIssued();

    await user.click(
      screen.getByRole("button", { name: /verify this address/i }),
    );

    expect(
      await screen.findByText("That verification link is invalid or has expired."),
    ).toBeInTheDocument();
    // And it stays on this screen, so the link is still there to retry with.
    expect(screen.getByRole("link")).toBeInTheDocument();
  });
});

describe("after registering, when no link was returned", () => {
  function renderWithheld() {
    render(
      <VerifyEmailPrompt
        email="ada@example.com"
        message={MESSAGE}
        onDone={vi.fn()}
      />,
    );
  }

  it("never renders a token or a verification link", () => {
    // Production withholds the token (`_may_disclose_link`), and the UI must
    // not manufacture one — this is the assertion that a raw verification
    // token cannot reach a production screen.
    renderWithheld();
    expect(screen.queryByRole("link")).toBeNull();
    expect(screen.queryByText(/verify-email\?token=/)).toBeNull();
    expect(
      screen.queryByRole("button", { name: /verify this address/i }),
    ).toBeNull();
  });

  it("does not claim an email was sent", () => {
    renderWithheld();
    expect(screen.queryByText(/check your inbox/i)).toBeNull();
    expect(screen.queryByText(/link sent to/i)).toBeNull();
    expect(
      screen.getByText(/no verification link was issued to you/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/nothing was sent to/i)).toBeInTheDocument();
  });

  it("says who can unblock the account", () => {
    renderWithheld();
    expect(
      screen.getByText(/whoever operates this deployment has to issue the link/i),
    ).toBeInTheDocument();
  });
});
