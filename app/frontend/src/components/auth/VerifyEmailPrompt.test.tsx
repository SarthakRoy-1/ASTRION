import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { VerifyEmailPrompt } from "./VerifyEmailPrompt";

/**
 * What a newly registered user is told.
 *
 * Three honest branches:
 * 1. Email was actually sent (Resend configured) — show inbox callout + resend.
 * 2. No email sent, but token returned (non-production, no Resend) — show link.
 * 3. No email sent, no token (misconfigured production) — show contact notice.
 */

const MESSAGE = "Your account has been created. Check your inbox for a verification link.";
const MESSAGE_NO_SEND = "If that address is available, an account was created and a verification link issued.";

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

describe("when email was sent (Resend configured)", () => {
  function renderSent() {
    const user = userEvent.setup();
    const onDone = vi.fn();
    render(
      <VerifyEmailPrompt
        email="ada@example.com"
        message={MESSAGE}
        emailSent={true}
        onDone={onDone}
      />,
    );
    return { user, onDone };
  }

  it("tells the user a verification email was sent", () => {
    renderSent();
    expect(screen.getByText(/verification email sent/i)).toBeInTheDocument();
    expect(screen.getByText(/sent to/i)).toBeInTheDocument();
  });

  it("does not show a bare verification link", () => {
    renderSent();
    expect(screen.queryByRole("link", { name: /verify-email\?token=/i })).toBeNull();
  });

  it("shows a resend button (initially disabled during countdown)", () => {
    renderSent();
    const resendBtn = screen.getByRole("button", { name: /resend link/i });
    // Button is present but disabled for the 30s countdown.
    expect(resendBtn).toBeDisabled();
  });

  it("does not show the no-email-configured callout", () => {
    renderSent();
    expect(screen.queryByText(/email delivery not configured/i)).toBeNull();
    expect(screen.queryByText(/no verification link was issued/i)).toBeNull();
  });
});

describe("when no email sent but token returned (non-production, no Resend)", () => {
  function renderIssued() {
    const user = userEvent.setup();
    const onDone = vi.fn();
    render(
      <VerifyEmailPrompt
        email="ada@example.com"
        message={MESSAGE_NO_SEND}
        emailSent={false}
        verificationToken="tok-abc-123"
        onDone={onDone}
      />,
    );
    return { user, onDone };
  }

  it("says plainly that email delivery is not configured", () => {
    renderIssued();
    expect(
      screen.getByText(/email delivery not configured/i),
    ).toBeInTheDocument();
    expect(screen.queryByText(/check your inbox/i)).toBeNull();
  });

  it("shows the link, pointing at the route an emailed link would land on", () => {
    renderIssued();
    const link = screen.getByRole("link", { name: /\/verify-email\?token=/ });
    expect(link).toHaveAttribute(
      "href",
      expect.stringContaining("/verify-email?token=tok-abc-123"),
    );
    expect(link).toHaveTextContent("/verify-email?token=tok-abc-123");
  });

  it("percent-encodes a token that would otherwise break the query", () => {
    render(
      <VerifyEmailPrompt
        email="ada@example.com"
        message={MESSAGE_NO_SEND}
        emailSent={false}
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
    expect(screen.getByRole("link")).toBeInTheDocument();
  });
});

describe("when no email sent and no token (misconfigured or production)", () => {
  function renderWithheld() {
    render(
      <VerifyEmailPrompt
        email="ada@example.com"
        message={MESSAGE_NO_SEND}
        emailSent={false}
        onDone={vi.fn()}
      />,
    );
  }

  it("never renders a token or a verification link", () => {
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
      screen.getByText(/no verification link was issued/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/nothing was sent to/i)).toBeInTheDocument();
  });

  it("says who can unblock the account", () => {
    renderWithheld();
    expect(
      screen.getByText(/contact whoever operates this deployment/i),
    ).toBeInTheDocument();
  });
});
