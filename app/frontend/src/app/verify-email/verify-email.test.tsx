import { screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import VerifyEmailPage from "./page";
import { setTestRoute } from "@/test/next-navigation";
import { renderApp } from "@/test/render";

/**
 * Email verification, where a link lands.
 *
 * The backend refuses a password sign-in until an address is verified, and it
 * answers the refusal with the same wording it uses for a wrong password — so
 * the endpoint cannot be used to discover who has an account. That is correct,
 * and it is why this screen has to exist: without it, a newly registered user
 * only ever sees "Incorrect email address or password" and has no way onward.
 */

function stubVerify(reply: { status: number; body?: unknown }) {
  const calls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      calls.push(url);
      if (url.includes("/api/auth/verify-email")) {
        return {
          ok: reply.status < 300,
          status: reply.status,
          statusText: "",
          json: async () => reply.body ?? { status: "verified" },
        } as Response;
      }
      // The rest of the app boots behind this screen; a signed-out visitor is
      // exactly who opens it.
      return {
        ok: true,
        status: 200,
        statusText: "",
        json: async () =>
          url.includes("/health")
            ? { status: "ok", auth_mode: "session", documents_indexed: 0 }
            : url.includes("/api/principals")
              ? { principals: [] }
              : null,
      } as Response;
    }),
  );
  return { calls };
}

describe("verifying an email address", () => {
  it("verifies on arrival rather than asking for another click", async () => {
    // The user already expressed intent by opening the link, and the token is
    // single-use — a confirmation step would only add a way to lose it.
    const { calls } = stubVerify({ status: 200 });
    setTestRoute("/verify-email?token=abc123");
    renderApp(<VerifyEmailPage />);

    expect(
      await screen.findByRole("heading", { name: /address verified/i }),
    ).toBeInTheDocument();
    expect(
      calls.some((url) => url.includes("/api/auth/verify-email")),
    ).toBe(true);
  });

  it("shows the backend's refusal rather than a generic apology", async () => {
    stubVerify({
      status: 400,
      body: {
        error: {
          code: "invalid_request",
          message: "That verification link is invalid or has expired.",
        },
      },
    });
    setTestRoute("/verify-email?token=stale");
    renderApp(<VerifyEmailPage />);

    expect(
      await screen.findByText(/invalid or has expired/i),
    ).toBeInTheDocument();
  });

  it("says so plainly when the link carries no token", async () => {
    stubVerify({ status: 200 });
    setTestRoute("/verify-email");
    renderApp(<VerifyEmailPage />);

    expect(
      await screen.findByText(/this link has no token/i),
    ).toBeInTheDocument();
  });

  it("never shows the signed-in chrome to a signed-out visitor", async () => {
    stubVerify({ status: 200 });
    setTestRoute("/verify-email?token=abc123");
    renderApp(<VerifyEmailPage />);

    await screen.findByRole("heading", { name: /address verified/i });
    expect(screen.queryByRole("navigation", { name: /primary/i })).toBeNull();
  });
});

describe("a single-use link", () => {
  it("is redeemed once, however many times the effect runs", async () => {
    // React StrictMode invokes an effect twice in development. The token is
    // consumed on first use, so a second request is refused — and rendering
    // that refusal told a user whose account had just been verified that their
    // link was invalid. Found by walking the flow in a browser; this is what
    // stops it coming back.
    const { calls } = stubVerify({ status: 200 });
    setTestRoute("/verify-email?token=single-use");

    const { StrictMode } = await import("react");
    const { render } = await import("@testing-library/react");
    const { AppProviders } = await import("@/app/providers");
    const { AppFrame } = await import("@/app/AppFrame");
    const { default: VerifyEmailPage } = await import("./page");

    render(
      <StrictMode>
        <AppProviders>
          <AppFrame>
            <VerifyEmailPage />
          </AppFrame>
        </AppProviders>
      </StrictMode>,
    );

    expect(
      await screen.findByRole("heading", { name: /address verified/i }),
    ).toBeInTheDocument();
    expect(
      calls.filter((url) => url.includes("/api/auth/verify-email")),
    ).toHaveLength(1);
  });

  it("says an already-used link looks the same as a bad one", async () => {
    // The backend answers both identically on purpose — telling them apart
    // would say whether an address is registered.
    stubVerify({
      status: 400,
      body: {
        error: {
          code: "invalid_request",
          message: "That verification link is invalid or has expired.",
        },
      },
    });
    setTestRoute("/verify-email?token=spent");
    renderApp(<VerifyEmailPage />);

    expect(
      await screen.findByText(/if you already opened this link/i),
    ).toBeInTheDocument();
  });
});
